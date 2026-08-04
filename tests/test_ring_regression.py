#!/usr/bin/env python3
"""记忆系统 v2 三环 — 回归测试

覆盖：环3 验证加权 / 环1 纠正检测+confidence engine / 环2 状态机 / 读不写库修复
数据源：临时自建库（不依赖任何现有环境）。

运行：
  python run_tests.py
  或：python -m pytest tests/ (需安装 pytest)
"""
import os
import sys
import sqlite3
import tempfile
import unittest

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit.gate import detect_correction_layer1
from memorykit import MemoryDB, MemorySearch, MemoryLifecycle


def make_tmp_db():
    """建全新临时库（不碰正式库）"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class TestVerifyWeights(unittest.TestCase):
    """环3 验证回流：VERIFY_WEIGHTS 加权"""

    def test_weights_exist(self):
        self.assertTrue(hasattr(MemorySearch, "VERIFY_WEIGHTS"))

    def test_verified_weight_greater_than_1(self):
        w = MemorySearch.VERIFY_WEIGHTS
        self.assertGreater(w.get("verified", 0), 1.0)

    def test_disputed_penalized_harder_than_stale(self):
        w = MemorySearch.VERIFY_WEIGHTS
        self.assertLess(w.get("disputed", 9), w.get("STALE", 0))


class TestCorrectionDetection(unittest.TestCase):
    """环1 纠正检测（修复：空尾/句号必须命中）"""

    def test_dui(self):
        self.assertIsNotNone(detect_correction_layer1("不对"))

    def test_dui_period(self):
        self.assertIsNotNone(detect_correction_layer1("不对。"))

    def test_dui_ya(self):
        self.assertIsNotNone(detect_correction_layer1("不对啊"))

    def test_dui_full(self):
        self.assertIsNotNone(detect_correction_layer1("不对，应该是550块"))

    def test_ni_jicuo(self):
        self.assertIsNotNone(detect_correction_layer1("你记错了"))

    def test_bie_ji_excluded(self):
        self.assertIsNone(detect_correction_layer1("别急"))

    def test_question_excluded(self):
        self.assertIsNone(detect_correction_layer1("是不是这样"))

    def test_neutral_excluded(self):
        self.assertIsNone(detect_correction_layer1("这里有个问题"))


class TestConfidenceEngine(unittest.TestCase):
    """环1 confidence engine + correction 闭环（临时自建库）"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp)
        self.db.set("regression/test-conf", "回归测试记忆：显卡价格是550块",
                    category="env", embed=False)

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_correction_downgrade(self):
        key = "regression/test-conf"
        r = self.db.correction(key, session_id="regression-test", signal="测试")
        self.assertTrue(r.get("ok"))
        self.assertLess(r["new_conf"], r["old_conf"])

    def test_status_disputed(self):
        key = "regression/test-conf"
        self.db.correction(key, session_id="regression-test", signal="测试")
        st = self.db._conn.execute(
            "SELECT verification_status FROM memories WHERE key=?", (key,)
        ).fetchone()[0]
        self.assertEqual(st, "disputed")

    def test_correction_log_written(self):
        key = "regression/test-conf"
        self.db.correction(key, session_id="regression-test", signal="测试")
        lg = self.db._conn.execute(
            "SELECT count(*) FROM correction_log WHERE memory_key=?", (key,)
        ).fetchone()[0]
        self.assertGreaterEqual(lg, 1)

    def test_reinforce_boost(self):
        key = "regression/test-conf"
        self.db.correction(key, session_id="regression-test", signal="测试")
        r2 = self.db.reinforce(key, mode="explicit")
        self.assertTrue(r2.get("ok"))
        self.assertGreater(r2["new_conf"], r2["old_conf"])


class TestLifecycleConstants(unittest.TestCase):
    """环2 状态机常量"""

    def test_decay_tau(self):
        self.assertEqual(MemoryLifecycle.DECAY_TAU, {"slow": 90, "medium": 30, "fast": 7})

    def test_protected_categories(self):
        self.assertIn("protocol", MemoryLifecycle.PROTECTED_CATEGORIES)


class TestReadNoWrite(unittest.TestCase):
    """读不写库修复：检索后无挂起事务"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp)
        self.db.set("regression/gpu", "显卡价格是550块，显存12G",
                    category="env", embed=False)
        self.db.set("regression/cpu", "CPU是R7-7800X3D，8核16线程",
                    category="env", embed=False)

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_no_hanging_transaction_after_search(self):
        before = self.db._conn.in_transaction
        r = self.db.search_multi("显卡", limit=3, scope="permanent")
        after = self.db._conn.in_transaction
        self.assertFalse(before)
        self.assertFalse(after)

    def test_search_returns_results(self):
        r = self.db.search_multi("显卡", limit=3, scope="permanent")
        self.assertGreaterEqual(len(r), 1)

    def test_flush_touches(self):
        n = self.db.flush_touches()
        self.assertGreaterEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
