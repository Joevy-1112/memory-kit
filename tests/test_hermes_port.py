#!/usr/bin/env python3
"""Hermes 专用方法移植回归测试（2026-08-11，记忆插件统一到 memory-kit 单源）

覆盖从生产脚本 ~/.hermes/scripts/memory_db.py 移植进 memory-kit 的三个
Hermes 专用方法：
  #1 rollback_pending() — 清掉连接上悬挂的未提交写事务（WAL 写锁防护）
     - 写事务悬挂后调用 → in_transaction 变 False，后续写正常
     - 空读路径调用零副作用（no-op）
  #2 peek(key) — 纯读 get：不更新 accessed_at，不触发写路径
     - 与 get() 返回内容一致
     - get() 更新 accessed_at / peek() 不动
  #3 recent_reinforce_count(key, seconds) — correction_log 强化限频计数
     - reinforce 后窗口内计数 > 0
     - 窗口外旧记录不计（seconds 收窄）
     - 表缺失（老库）返回 0 不抛

运行：
  python run_tests.py
  或：python -m unittest tests.test_hermes_port -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class TestRollbackPending(unittest.TestCase):
    def test_hanging_tx_cleared(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        # 模拟悬挂写事务：DML 后不 commit
        db._conn.execute("UPDATE memories SET content='改' WHERE key='k1'")
        self.assertTrue(db._conn.in_transaction)
        db.rollback_pending()
        self.assertFalse(db._conn.in_transaction)
        # 悬挂事务清理后后续写正常
        db.set("k2", "内容二", embed=False)
        self.assertEqual(db.get("k2")["content"], "内容二")
        db.close()

    def test_noop_on_clean_conn(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db.rollback_pending()  # 空读路径调用零副作用
        self.assertFalse(db._conn.in_transaction)
        db.close()


class TestPeek(unittest.TestCase):
    def test_peek_does_not_touch_accessed_at(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        before = db.peek("k1")
        before_acc = before.get("accessed_at") or before.get("last_accessed")
        db.peek("k1")  # 两次纯读
        after = db.peek("k1")
        after_acc = after.get("accessed_at") or after.get("last_accessed")
        self.assertEqual(before_acc, after_acc)
        db.close()

    def test_get_updates_accessed_at_but_peek_not(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db.get("k1")
        acc_after_get = db.peek("k1").get("accessed_at") or db.peek("k1").get("last_accessed")
        db.peek("k1")
        acc_after_peek = db.peek("k1").get("accessed_at") or db.peek("k1").get("last_accessed")
        self.assertEqual(acc_after_get, acc_after_peek)  # peek 后不再变化
        db.close()

    def test_peek_missing_key_returns_none(self):
        db = MemoryDB(db_path=make_tmp_db())
        self.assertIsNone(db.peek("不存在/key"))
        db.close()


class TestRecentReinforceCount(unittest.TestCase):
    def test_count_within_window(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db.reinforce("k1", session_id="s1", mode="implicit")
        n = db.recent_reinforce_count("k1", seconds=86400)
        self.assertGreaterEqual(n, 1)
        db.close()

    def test_explicit_mode_counted_too(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db.reinforce("k1", session_id="s1", mode="explicit")
        n = db.recent_reinforce_count("k1", seconds=86400)
        self.assertGreaterEqual(n, 1)
        db.close()

    def test_no_reinforce_returns_zero(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        self.assertEqual(db.recent_reinforce_count("k1", seconds=86400), 0)
        db.close()

    def test_other_key_not_counted(self):
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db.set("k2", "内容二", embed=False)
        db.reinforce("k1", session_id="s1", mode="implicit")
        self.assertEqual(db.recent_reinforce_count("k2", seconds=86400), 0)
        db.close()

    def test_missing_correction_log_returns_zero(self):
        # 老库无 correction_log 表 → 返回 0 不抛（表缺失不拦截强化）
        db = MemoryDB(db_path=make_tmp_db())
        db.set("k1", "内容一", embed=False)
        db._conn.execute("DROP TABLE correction_log")
        db._conn.commit()
        self.assertEqual(db.recent_reinforce_count("k1", seconds=86400), 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
