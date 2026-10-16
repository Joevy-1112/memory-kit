#!/usr/bin/env python3
"""去重性能重写回归测试（2026-10-16）。

覆盖：
- 语义不变性：同输入下新实现产出的 (keep, remove) pairs 与旧实现逐条一致
  （旧实现原文内嵌为 _legacy_deduplicate，作为对照 oracle）
- 边界语义：畸形 JSON 向量跳过、零向量不合并、keep/remove 由 importance 决定、
  「被删行后续再作为外层 i」的陈旧快照语义一致
- 解析次数：新实现每行向量只 json.loads 一次（旧实现是 O(N^2) 次）
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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def _legacy_deduplicate(self, threshold=0.92):
    """旧实现原文（2026-08-02 版本）—— 语义不变性对照 oracle，不要改。"""
    rows = self._conn.execute(
        "SELECT id, key, content, importance, vector FROM memories "
        "WHERE vector IS NOT NULL ORDER BY id"
    ).fetchall()
    pairs = []; removed = 0
    with self._tx():
        for i in range(len(rows)):
            for j in range(i+1, len(rows)):
                try:
                    vi = json.loads(rows[i]["vector"]) if rows[i]["vector"] else None
                    vj = json.loads(rows[j]["vector"]) if rows[j]["vector"] else None
                except Exception:
                    continue
                if vi and vj and self._cosine_similarity(vi, vj) >= threshold:
                    keep = rows[i] if rows[i]["importance"] >= rows[j]["importance"] else rows[j]
                    remove = rows[j] if keep["key"] == rows[i]["key"] else rows[i]
                    nc = keep["content"]
                    if remove["content"] not in keep["content"]:
                        nc += "\n\n[合并自: %s]\n%s" % (remove["key"], remove["content"][:200])
                    self._conn.execute("UPDATE memories SET content=?, importance=? WHERE key=?",
                                       (nc, max(keep["importance"], remove["importance"]), keep["key"]))
                    self._conn.execute("DELETE FROM memories WHERE key=?", (remove["key"],))
                    pairs.append((keep["key"], remove["key"])); removed += 1; break
    return {"merged": len(pairs), "removed": removed, "pairs": pairs}


# 精心构造：会产生多次合并 + 覆盖 keep/remove 双向 + 畸形/零向量 + 被删行后续复用
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


class TestDedupeSemanticInvariance(unittest.TestCase):
    def setUp(self):
        self.tmp_old = make_tmp_db()
        self.tmp_new = make_tmp_db()
        self.db_old = MemoryDB(self.tmp_old)
        self.db_new = MemoryDB(self.tmp_new)
        _seed(self.db_old)
        _seed(self.db_new)

    def tearDown(self):
        for db, p in ((self.db_old, self.tmp_old), (self.db_new, self.tmp_new)):
            try:
                db._conn.close()
            except Exception:
                pass
            try:
                os.remove(p)
            except OSError:
                pass

    def test_pairs_and_rows_identical_to_legacy(self):
        for thr in (0.92, 0.85, 0.70, 0.50):
            # 每个阈值都从同一 seed 重置，保证 old/new 输入逐字节相同
            for db in (self.db_old, self.db_new):
                db._conn.execute("DELETE FROM memories")
                _seed(db)
            ro = _legacy_deduplicate(self.db_old, threshold=thr)
            rn = self.db_new.deduplicate(threshold=thr, max_seconds=None)
            self.assertEqual(ro["pairs"], rn["pairs"], msg="pairs differ at thr=%s" % thr)
            self.assertEqual(ro["merged"], rn["merged"], msg="merged differ at thr=%s" % thr)
            self.assertEqual(ro["removed"], rn["removed"], msg="removed differ at thr=%s" % thr)
            self.assertEqual(_snapshot(self.db_old), _snapshot(self.db_new),
                             msg="resulting rows differ at thr=%s" % thr)

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
        # 新实现只解析一遍；旧实现是 2*C(N,2) 次
        self.assertLessEqual(calls["n"], n_rows,
                             "expected <= 1 parse/row, got %d for %d rows" % (calls["n"], n_rows))
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
