#!/usr/bin/env python3
"""forgetting 治理壳回归测试（P1 #6，2026-08-08）

设计（design-notes/scope-recall-borrowing.md §6）：包在 age() 外面，
不动衰减状态机——加"可审计 + 可回滚 + dry-run"：
  forgetting_report() 只读分类 → forgetting(dry_run=True) 出计划 →
  forgetting() apply 记审计（before/after + batch_id）→ rollback() 整批还原。

覆盖：
  #1 report 分类：soft_archive / hard_delete(secret) / review_debt(pending 老)
  #2 dry_run 零写库（无审计、无状态变更）
  #3 soft_archive apply：审计记录 + verification_status→ARCHIVED + importance→0.05
  #4 hard_delete apply(delete=True)：物理删 + 审计 before 含 content
  #5 rollback：soft_archive 还原全行 + hard_delete 重建行
  #6 空库：全空分类不崩
  #7 普通健康记忆不进任何分类

运行：
  python run_tests.py forgetting
  或：python -m unittest tests.test_forgetting -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(prefix="mk-forget-", suffix=".db")
    os.close(fd)
    db = MemoryDB(db_path=path)
    return db, path


def _set_status(db, key, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    db._conn.execute(f"UPDATE memories SET {sets} WHERE key=?",
                     (*kw.values(), key))
    db._conn.commit()


class ForgettingTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_tmp_db()
        # 种子数据（learn 后手工调状态）
        self.k_soft = self.db.learn("一条已降级的低价值记忆应该被软归档处理掉")
        self.k_hard = self.db.learn("这个配置里有 api_key=sk-abcdef1234567890 需要清理")
        self.k_debt = self.db.learn("一条一直没验证的老记忆进入待复核队列")
        self.k_ok = self.db.learn("服务器嵌入服务端口是 8082 而且必须保持运行")
        _set_status(self.db, self.k_soft["key"],
                    verification_status="degraded", water=2, importance=0.1)
        _set_status(self.db, self.k_debt["key"],
                    verification_status="pending",
                    created_at=(datetime.now() - timedelta(days=30))
                    .strftime("%Y-%m-%d %H:%M:%S"))
        _set_status(self.db, self.k_ok["key"], water=1)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_01_report_classification(self):
        r = self.db.forgetting_report()
        self.assertIn(self.k_soft["key"],
                      [self.db._conn.execute(
                          "SELECT key FROM memories WHERE id=?", (i,)).fetchone()["key"]
                       for i in r["soft_archive"]])
        self.assertIn(self.k_hard["key"],
                      [self.db._conn.execute(
                          "SELECT key FROM memories WHERE id=?", (i,)).fetchone()["key"]
                       for i in r["hard_delete"]])
        self.assertIn(self.k_debt["key"],
                      [self.db._conn.execute(
                          "SELECT key FROM memories WHERE id=?", (i,)).fetchone()["key"]
                       for i in r["review_debt"]])
        # 健康记忆不进任何分类
        all_ids = set(r["soft_archive"]) | set(r["hard_delete"]) | set(r["review_debt"])
        ok_id = self.db._conn.execute(
            "SELECT id FROM memories WHERE key=?", (self.k_ok["key"],)).fetchone()["id"]
        self.assertNotIn(ok_id, all_ids)

    def test_02_dry_run_no_writes(self):
        before = self.db._conn.execute(
            "SELECT COUNT(*) FROM governance_audit_events").fetchone()[0]
        r = self.db.forgetting(dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertIn("plan", r)
        after = self.db._conn.execute(
            "SELECT COUNT(*) FROM governance_audit_events").fetchone()[0]
        self.assertEqual(before, after)
        # 状态未变
        st = self.db._conn.execute(
            "SELECT verification_status FROM memories WHERE key=?",
            (self.k_soft["key"],)).fetchone()["verification_status"]
        self.assertEqual(st, "degraded")

    def test_03_soft_archive_apply(self):
        r = self.db.forgetting(dry_run=False, batch_id="test-batch-1")
        self.assertFalse(r["dry_run"])
        self.assertEqual(r["soft_archived"], 1)
        self.assertEqual(r["batch_id"], "test-batch-1")
        st = self.db._conn.execute(
            "SELECT verification_status, importance FROM memories WHERE key=?",
            (self.k_soft["key"],)).fetchone()
        self.assertEqual(st["verification_status"], "ARCHIVED")
        self.assertEqual(st["importance"], 0.05)
        # 审计记录
        aud = self.db._conn.execute(
            "SELECT action, memory_key, before FROM governance_audit_events "
            "WHERE batch_id='test-batch-1'").fetchone()
        self.assertEqual(aud["action"], "soft_archive")
        self.assertIn("content", aud["before"])

    def test_04_hard_delete_apply(self):
        r = self.db.forgetting(dry_run=False, delete=True, batch_id="test-batch-2")
        self.assertEqual(r["hard_deleted"], 1)
        row = self.db._conn.execute(
            "SELECT id FROM memories WHERE key=?", (self.k_hard["key"],)).fetchone()
        self.assertIsNone(row)  # 物理删
        aud = self.db._conn.execute(
            "SELECT action, before FROM governance_audit_events "
            "WHERE batch_id='test-batch-2' AND action='hard_delete'").fetchone()
        self.assertIsNotNone(aud)
        self.assertIn("sk-abcdef1234567890", aud["before"])  # 快照含 content

    def test_05_rollback_restores(self):
        self.db.forgetting(dry_run=False, delete=True, batch_id="test-batch-3")
        r = self.db.rollback("test-batch-3")
        self.assertEqual(r["restored"], 2)  # soft + hard 都还原
        # soft 还原
        st = self.db._conn.execute(
            "SELECT verification_status FROM memories WHERE key=?",
            (self.k_soft["key"],)).fetchone()
        self.assertEqual(st["verification_status"], "degraded")
        # hard 重建
        row = self.db._conn.execute(
            "SELECT content FROM memories WHERE key=?",
            (self.k_hard["key"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("sk-abcdef1234567890", row["content"])

    def test_06_empty_db(self):
        self.db._conn.execute("DELETE FROM memories")
        self.db._conn.commit()
        r = self.db.forgetting_report()
        self.assertEqual(r["soft_archive"], [])
        self.assertEqual(r["hard_delete"], [])
        self.assertEqual(r["review_debt"], [])
        self.assertEqual(r["duplicate_groups"], [])


if __name__ == "__main__":
    unittest.main()
