#!/usr/bin/env python3
"""去重回归测试（2026-10-16 性能版 → 2026-10-20 P0 数据保全版）。

覆盖：
- P0 数据保全：被合并行不物理删除（status='merged' + merged_into），
  全文保留；keep 行拼接被合并行全文（不再 200 字截断）
- P0 幂等：连跑两次，第二次 merged=0，merge_events 不再增长
- P0 dry_run：只出计划，不 UPDATE、不写 merge_events
- merge_events 审计：每个 (keep, remove) 一行，含全文/sim/reason
- 边界语义：畸形 JSON 向量跳过、零向量不合并、keep/remove 由 importance 决定
- 解析次数：每行向量只 json.loads 一次
- 断链预算：max_seconds 超时 → 不写库、返回 skipped、结果集为空

运行：
  python run_tests.py dedupe
  或：python -m unittest tests.test_dedupe_fast -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


# 精心构造：会产生多次合并 + 覆盖 keep/remove 双向 + 畸形/零向量
FIXTURE = [
    # key, content, importance, vector(json str or None)
    ("a", "alpha", 0.50, json.dumps([1.0, 0.0, 0.0])),
    ("b", "bravo", 0.60, json.dumps([1.0, 0.0, 0.0])),   # 与 a 完全同向；imp 高 → keep=b
    ("c", "charlie", 0.50, json.dumps([0.9, 0.1, 0.0])),  # 与 b 近似同向
    ("d", "delta", 0.40, json.dumps([0.0, 1.0, 0.0])),
    ("e", "echo-malformed", 0.90, "{not valid json"),
    ("f", "foxtrot", 0.99, json.dumps([0.0, 0.0, 0.0])),  # 零向量，永不合并
    ("g", "golf", 0.30, json.dumps([0.95, 0.05, 0.0])),
    ("h", "hotel", 0.20, json.dumps([0.0, 0.0, 1.0])),
    ("i", "india", 0.80, None),                            # NULL 向量，查询层已过滤
]


def _seed(db):
    for key, content, imp, vec in FIXTURE:
        db._conn.execute(
            "INSERT INTO memories (key, content, importance, vector, scope) VALUES (?,?,?,?, 'permanent')",
            (key, content, imp, vec))
    db._conn.commit()


def _snapshot(db):
    return [tuple(r) for r in db._conn.execute(
        "SELECT key, content, importance FROM memories ORDER BY key").fetchall()]


class TestDedupeDataSafety(unittest.TestCase):
    def setUp(self):
        self.tmp_new = make_tmp_db()
        self.db_new = MemoryDB(self.tmp_new)
        _seed(self.db_new)

    def tearDown(self):
        try:
            self.db_new._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp_new)
        except OSError:
            pass

    def test_soft_merge_preserves_rows_and_full_content(self):
        """P0: 合并只软归档，remove 行还在、全文在、status/merged_into 正确。"""
        before = {r["key"]: r["content"] for r in self.db_new._conn.execute(
            "SELECT key, content FROM memories").fetchall()}
        r = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertGreater(r["merged"], 0, "fixture must exercise the merge path")
        self.assertEqual(r["removed"], 0, "P0 不允许物理删除")
        for keep, remove in r["pairs"]:
            row = self.db_new.peek(remove)
            self.assertIsNotNone(row, "被合并行不能物理删除: %s" % remove)
            self.assertEqual(row["status"], "merged")
            self.assertEqual(row["merged_into"], keep)
            self.assertIsNotNone(row["merged_at"])
            # 被合并行自身全文原样保留
            self.assertEqual(row["content"], before[remove])
            # keep 行必须包含 remove 全文（不是 200 字截断）
            keep_row = self.db_new.peek(keep)
            self.assertIn(before[remove], keep_row["content"],
                          "keep 行丢失了 %s 的全文" % remove)
        events = self.db_new._conn.execute(
            "SELECT keep_key, remove_key, old_content, new_content, "
            "merged_content, sim, reason FROM merge_events").fetchall()
        self.assertEqual(len(events), r["merged"])
        for ev in events:
            self.assertEqual(ev["reason"], "dedupe")
            self.assertGreaterEqual(ev["sim"], 0.85)
            self.assertTrue(ev["old_content"] and ev["new_content"] and ev["merged_content"])

    def test_no_200_char_truncation(self):
        """P0: 旧实现 content[:200] 截断必须消失。"""
        long_remove = "R" * 500
        long_keep = "K" * 400
        self.db_new._conn.execute("DELETE FROM memories")
        self.db_new._conn.execute(
            "INSERT INTO memories(key, content, importance, vector, scope) VALUES(?,?,?,?, 'permanent')",
            ("keep-long", long_keep, 0.9, json.dumps([1.0, 0.0, 0.0])))
        self.db_new._conn.execute(
            "INSERT INTO memories(key, content, importance, vector, scope) VALUES(?,?,?,?, 'permanent')",
            ("remove-long", long_remove, 0.5, json.dumps([1.0, 0.0, 0.0])))
        self.db_new._conn.commit()
        r = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertEqual(r["merged"], 1)
        keep = self.db_new.peek("keep-long")
        self.assertIn(long_remove, keep["content"], "remove 全文必须完整拼入 keep")
        self.assertGreaterEqual(len(keep["content"]), len(long_keep) + len(long_remove))
        removed = self.db_new.peek("remove-long")
        self.assertEqual(removed["content"], long_remove)

    def test_idempotent_second_run(self):
        """P0: 连跑两次，第二次 merged=0，审计行不增长。"""
        r1 = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertGreater(r1["merged"], 0)
        n_events = self.db_new._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0]
        active = self.db_new._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active') != 'merged'"
        ).fetchone()[0]
        r2 = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertEqual(r2["merged"], 0, "第二次必须没有新合并")
        self.assertEqual(r2["pairs"], [])
        self.assertEqual(self.db_new._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0], n_events)
        self.assertEqual(self.db_new._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active') != 'merged'"
        ).fetchone()[0], active)

    def test_dry_run_writes_nothing(self):
        """P0: dry_run 不 UPDATE、不 INSERT merge_events。"""
        before_rows = [tuple(r) for r in self.db_new._conn.execute(
            "SELECT key, content, importance, status, merged_into, merged_at "
            "FROM memories ORDER BY key").fetchall()]
        before_events = self.db_new._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0]
        r = self.db_new.deduplicate(threshold=0.85, max_seconds=None, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertGreater(r["merged"], 0, "dry_run 也应算出计划")
        self.assertEqual(r["removed"], 0)
        after_rows = [tuple(r) for r in self.db_new._conn.execute(
            "SELECT key, content, importance, status, merged_into, merged_at "
            "FROM memories ORDER BY key").fetchall()]
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(self.db_new._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0], before_events)

    def test_merge_events_created_at_is_cst(self):
        """P0 收尾: merge_events.created_at 与库内其它时间列一致（CST, +8h）。

        用 UTC 做参照：created_at - utcnow ≈ +8h。跨日由 timedelta 自然处理；
        留 5 分钟抖动，避免测试恰好在整点/调度延迟时误报。
        """
        r = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertGreater(r["merged"], 0, "fixture must exercise the merge path")
        rows = self.db_new._conn.execute(
            "SELECT created_at FROM merge_events").fetchall()
        self.assertGreater(len(rows), 0)
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in rows:
            created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            delta = created - utc_now
            self.assertGreaterEqual(delta, timedelta(hours=7, minutes=55),
                                    "created_at 比 UTC 早，疑似仍写 UTC: %s" % row["created_at"])
            self.assertLessEqual(delta, timedelta(hours=8, minutes=5),
                                 "created_at 偏移不是 +8h: %s" % row["created_at"])

    def test_fixture_actually_merges(self):
        r = self.db_new.deduplicate(threshold=0.85, max_seconds=None)
        self.assertGreater(r["merged"], 0, "fixture must exercise the merge path")
        self.assertFalse(r["skipped"])


class TestDedupeParseOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)
        _seed(self.db)

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def test_vector_json_parsed_once_per_row(self):
        n_rows = self.db._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE vector IS NOT NULL").fetchone()[0]
        real_loads = json.loads
        calls = {"n": 0}

        def counting_loads(*a, **k):
            calls["n"] += 1
            return real_loads(*a, **k)

        with mock.patch.object(json, "loads", side_effect=counting_loads):
            self.db.deduplicate(threshold=0.85, max_seconds=None)
        # 新实现只解析一遍；旧实现是 2*C(N,2) 次。
        # +1 余量：合并后重嵌（P0 补漏 2026-10-20）会额外解析一次 sparse 响应，
        # 这是固定成本，不随行数增长；断言仍能捕获 O(N^2) 退化。
        self.assertLessEqual(calls["n"], n_rows + 1,
                             "expected <= 1 parse/row (+1 for re-embed), got %d for %d rows"
                             % (calls["n"], n_rows))
        self.assertGreaterEqual(calls["n"], 1)


class TestDedupeDeadline(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)
        _seed(self.db)

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def test_deadline_skip_applies_no_writes(self):
        before = _snapshot(self.db)
        r = self.db.deduplicate(threshold=0.85, max_seconds=-1)
        self.assertTrue(r["skipped"])
        self.assertEqual(r["pairs"], [])
        self.assertEqual(r["merged"], 0)
        self.assertEqual(r["removed"], 0)
        self.assertEqual(before, _snapshot(self.db), "skipped run must not write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
