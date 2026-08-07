#!/usr/bin/env python3
"""memorykit v2 — event_time 时间结构化字段最小验证。

覆盖：
  1. schema：新库建表含 event_time；旧库 _migrate_columns 自动补列
  2. learn()/set() 写入 event_time；set() upsert 不传时保留旧值
  3. search_multi 时间偏置：ref_time+max_age_days / time_window 软重排
  4. strict_time_window=True 硬过滤
  5. search_semantic 时间偏置
  6. TTL 仍用 created_at（不用 event_time）
  7. learn() merge 保护：event_time 相距 >7 天保留旧值，≤7 天更新
  8. 检索 hit 返回里带 event_time 字段

运行：
  python run_tests.py event_time
"""
import os
import sys
import tempfile
import unittest
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB
from memorykit import db as db_mod


def make_tmp_db(suffix=".db"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(path)
    return path


class FakeEmbedder:
    """离线假嵌入器：固定 4 维向量（同内容同向量 → learn 去重必命中）。"""

    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]

    def is_available(self):
        return True


class TestEventTimeSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_new_db_has_event_time_column(self):
        cols = {r[1] for r in self.db._conn.execute("PRAGMA table_info(memories)")}
        self.assertIn("event_time", cols)

    def test_old_db_auto_migrates_event_time(self):
        # 用「去掉 event_time 行的旧版 SCHEMA_SQL」造一个 v1 老库
        old_schema = db_mod.SCHEMA_SQL.replace(
            "    event_time  TEXT DEFAULT NULL,"
            "   -- \u4e8b\u4ef6\u53d1\u751f\u65f6\u95f4\uff08v2 \u68c0\u7d22\u5143\u6570\u636e\uff1a\u4ec5\u53c2\u4e0e\u65f6\u95f4\u504f\u7f6e\u6392\u5e8f\uff0c\u4e0d\u53c2\u4e0e TTL/\u9057\u5fd8\u72b6\u6001\u673a/\u53bb\u91cd\u5e74\u9f84\u5224\u65ad\uff09\n",
            "")
        self.assertNotEqual(old_schema, db_mod.SCHEMA_SQL,
                            "old_schema \u672a\u6210\u529f\u53bb\u6389 event_time \u884c\uff0c\u6d4b\u8bd5\u65e0\u6548")
        conn = sqlite3.connect(self.tmp)
        conn.executescript(old_schema)
        conn.close()
        # 重新打开 → _migrate_columns 应补上 event_time
        db2 = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())
        try:
            cols = {r[1] for r in db2._conn.execute("PRAGMA table_info(memories)")}
            self.assertIn("event_time", cols)
        finally:
            db2.close()


class TestEventTimeWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_learn_stores_event_time(self):
        r = self.db.learn("数据库备份定时任务每日凌晨执行", event_time="2026-06-15")
        self.assertNotIn("error", r)
        row = self.db.get(r["key"])
        self.assertEqual(row["event_time"], "2026-06-15")

    def test_learn_without_event_time_is_null(self):
        r = self.db.learn("服务器风扇转速异常日志归档")
        row = self.db.get(r["key"])
        self.assertIsNone(row["event_time"])

    def test_set_upsert_preserves_event_time_when_not_passed(self):
        key = "ev/upsert"
        self.db.set(key, "内容更新测试A", embed=False, event_time="2026-05-01")
        self.db.set(key, "内容更新测试B", embed=False)  # 不传 → 保留旧值
        row = self.db.get(key)
        self.assertEqual(row["event_time"], "2026-05-01")
        # 显式传新值 → 覆盖
        self.db.set(key, "内容更新测试C", embed=False, event_time="2026-05-02")
        row = self.db.get(key)
        self.assertEqual(row["event_time"], "2026-05-02")


class TestEventTimeSearch(unittest.TestCase):
    """时间偏置：event_time 生效、TTL 仍用 created_at、hit 带 event_time。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())
        # 两条内容几乎相同、仅事件时间不同：
        #   A 事件发生 2026-01-05（写入时间=现在）
        #   B 事件发生 2026-03-10（写入时间=现在）
        # 不传时间参数时（同写入时间）两者基线并列，B 因事件更新稍占优；
        # 传 ref_time='2026-01-08', max_age_days=7 → A 落窗口内应反超。
        # embed=True（FakeEmbedder 恒返回同一向量）→ 语义信号两者并列，
        # 排序差异只可能来自时间偏置，测试确定性强。
        self.db.set("ev/a", "显卡掉驱动事件记录 A", embed=True, event_time="2026-01-05")
        self.db.set("ev/b", "显卡掉驱动事件记录 B", embed=True, event_time="2026-03-10")

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_hits_include_event_time_field(self):
        hits = self.db.search_multi("显卡掉驱动", limit=5)
        self.assertTrue(all("event_time" in h for h in hits))

    def test_ref_time_max_age_bias_reranks(self):
        # 软重排：A 在窗口内 → bias 1.25；B 在窗口外 → bias 0.25
        hits = self.db.search_multi("显卡掉驱动", limit=5,
                                    ref_time="2026-01-08", max_age_days=7)
        by_key = {h["key"]: h for h in hits}
        self.assertIn("ev/a", by_key)
        self.assertIn("ev/b", by_key)  # 宽召回：窗口外仍返回
        self.assertEqual(by_key["ev/a"]["time_bias"], 1.25)
        self.assertEqual(by_key["ev/b"]["time_bias"], 0.25)
        self.assertGreater(by_key["ev/a"]["rrf_score"], by_key["ev/b"]["rrf_score"])
        # 排序后 A 应在前
        self.assertEqual(hits[0]["key"], "ev/a")

    def test_time_window_soft_bias(self):
        hits = self.db.search_multi("显卡掉驱动", limit=5,
                                    time_window=("2026-01-01", "2026-01-31"))
        by_key = {h["key"]: h for h in hits}
        self.assertEqual(by_key["ev/a"]["time_bias"], 1.25)
        self.assertEqual(by_key["ev/b"]["time_bias"], 0.25)
        self.assertEqual(hits[0]["key"], "ev/a")

    def test_strict_time_window_hard_filters(self):
        hits = self.db.search_multi("显卡掉驱动", limit=5,
                                    time_window=("2026-01-01", "2026-01-31"),
                                    strict_time_window=True)
        keys = {h["key"] for h in hits}
        self.assertIn("ev/a", keys)
        self.assertNotIn("ev/b", keys)  # 硬过滤剔除窗口外

    def test_no_time_params_matches_v1_behavior(self):
        # 不传时间参数：bias 恒 1.0，B（事件更新）不劣于 A
        hits = self.db.search_multi("显卡掉驱动", limit=5)
        by_key = {h["key"]: h for h in hits}
        self.assertEqual(by_key["ev/a"]["time_bias"], 1.0)
        self.assertEqual(by_key["ev/b"]["time_bias"], 1.0)
        self.assertGreaterEqual(
            by_key["ev/b"]["rrf_score"], by_key["ev/a"]["rrf_score"])

    def test_search_semantic_time_bias(self):
        hits = self.db.search_semantic("显卡掉驱动", limit=5,
                                       ref_time="2026-01-08", max_age_days=7)
        by_key = {h["key"]: h for h in hits}
        self.assertEqual(by_key["ev/a"]["time_bias"], 1.25)
        self.assertEqual(by_key["ev/b"]["time_bias"], 0.25)
        # 相似度并列（同向量）时，窗口内条目应排前
        self.assertEqual(hits[0]["key"], "ev/a")

    def test_ttl_still_uses_created_at_not_event_time(self):
        # event_time 远在过去（2020）但 created_at=now、ttl=60s →
        # TTL 用 created_at 判断 → 未过期，应出现在结果里
        self.db.set("ev/ttl", "TTL测试记忆内容专用词", embed=False,
                    event_time="2020-01-01", ttl=60)
        hits = self.db.search_multi("TTL测试", limit=5)
        keys = {h["key"] for h in hits}
        self.assertIn("ev/ttl", keys)


class TestEventTimeMergeProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_merge_gap_gt_7d_keeps_old_event_time(self):
        content = "服务器GPU驱动安装配置记录"
        r1 = self.db.learn(content, event_time="2026-01-05")
        key = r1["key"]
        # 相距 74 天 > 7 天：合并保留旧 event_time
        r2 = self.db.learn(content, event_time="2026-03-20")
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["key"], key)
        row = self.db.get(key)
        self.assertEqual(row["event_time"], "2026-01-05")

    def test_merge_gap_le_7d_updates_event_time(self):
        content = "服务器GPU驱动安装配置记录"
        r1 = self.db.learn(content, event_time="2026-01-05")
        key = r1["key"]
        # 相距 3 天 ≤ 7 天：同一事件补充，更新 event_time
        r2 = self.db.learn(content, event_time="2026-01-08")
        self.assertTrue(r2["merged"])
        row = self.db.get(key)
        self.assertEqual(row["event_time"], "2026-01-08")

    def test_merge_without_new_event_time_keeps_old(self):
        content = "服务器GPU驱动安装配置记录"
        r1 = self.db.learn(content, event_time="2026-01-05")
        key = r1["key"]
        r2 = self.db.learn(content)  # 不带 event_time → 不置空
        self.assertTrue(r2["merged"])
        row = self.db.get(key)
        self.assertEqual(row["event_time"], "2026-01-05")


if __name__ == "__main__":
    unittest.main()
