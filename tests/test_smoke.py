#!/usr/bin/env python3
"""memorykit 冒烟测试 — pip 安装后验证核心链路。

覆盖：建库 / learn / search / correction / reinforce / age 状态机 /
      dirty_log 参数化 / state.db 缺省降级 / 自定义 embedder 注入。

运行：
  python run_tests.py
  或：python -m unittest tests.test_smoke -v
"""
import os
import sys
import tempfile
import unittest

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import (
    MemoryDB,
    MemorySearch,
    MemoryLifecycle,
    MemoryGate,
    detect_correction,
    HTTPEmbedder,
)


def make_tmp_db(suffix=".db"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(path)
    return path


class FakeEmbedder:
    """离线假嵌入器：不依赖 8082 服务，供注入测试。"""

    def __init__(self):
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        # 固定 4 维向量，模拟 bge-m3 输出
        return [0.1, 0.2, 0.3, 0.4]

    def is_available(self):
        return True


class TestInit(unittest.TestCase):
    """建库：参数化路径 + 默认当前目录"""

    def test_default_path_is_cwd_memory_db(self):
        # DB_PATH 在 import 时求值（= 运行测试时的 cwd）。验证默认路径为
        # 当前目录下的 memory.db，并清理测试产物。
        #
        # P0 安全（2026-10-20）：测试套件可能带着 MEMORY_DB_PATH 指向沙箱库
        # 运行。这里必须临时移除该变量；否则 MemoryDB() 会打开沙箱库，而
        # finally 的 os.remove 会把沙箱库删掉（2026-10-20 已实际发生一次）。
        from unittest import mock
        expected = os.path.join(os.getcwd(), "memory.db")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORY_DB_PATH", None)
            db = MemoryDB()
        try:
            self.assertEqual(os.path.abspath(db.db_path), os.path.abspath(expected))
            self.assertEqual(os.path.basename(db.db_path), "memory.db")
            self.assertTrue(os.path.exists(db.db_path))
        finally:
            db.close()
            if os.path.abspath(db.db_path) == os.path.abspath(expected):
                try:
                    os.remove(db.db_path)
                except OSError:
                    pass

    def test_custom_path(self):
        tmp = make_tmp_db()
        db = MemoryDB(db_path=tmp)
        self.assertEqual(os.path.abspath(db.db_path), os.path.abspath(tmp))
        self.assertTrue(os.path.exists(tmp))
        db.close()
        os.remove(tmp)

    def test_state_db_none_default(self):
        tmp = make_tmp_db()
        db = MemoryDB(db_path=tmp)
        self.assertIsNone(db.state_db_path)
        db.close()
        os.remove(tmp)


class TestLearnSearch(unittest.TestCase):
    """learn → search 闭环"""

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

    def test_learn_creates_entry(self):
        r = self.db.learn("显卡价格是550块，显存12G", source="task")
        self.assertNotIn("error", r)
        self.assertIn("key", r)
        row = self.db.get(r["key"])
        self.assertIsNotNone(row)
        self.assertIn("显卡", row["content"])

    def test_learn_too_short(self):
        r = self.db.learn("太短")
        self.assertIn("error", r)

    def test_search_multi_finds(self):
        self.db.learn("显卡价格是550块，显存12G", source="task")
        hits = self.db.search_multi("显卡", limit=5)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any("显卡" in (h.get("content") or "") for h in hits))

    def test_semantic_search_offline_fallback_empty(self):
        # 无嵌入服务时 search_semantic 应返回空列表而不是抛异常
        db2 = MemoryDB(db_path=make_tmp_db())
        try:
            self.assertEqual(db2.search_semantic("显卡"), [])
        finally:
            db2.close()

    def test_verify_weights_public(self):
        self.assertGreater(MemorySearch.VERIFY_WEIGHTS["verified"], 1.0)
        self.assertLess(MemorySearch.VERIFY_WEIGHTS["disputed"],
                        MemorySearch.VERIFY_WEIGHTS["STALE"])


class TestCorrectionReinforce(unittest.TestCase):
    """correction / reinforce 置信度闭环"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())
        self.key = "smoke/gpu-price"
        self.db.set(self.key, "显卡价格是550块", category="env", embed=False)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_correction_lowers_confidence(self):
        r = self.db.correction(self.key, session_id="smoke", signal="不对")
        self.assertTrue(r["ok"])
        self.assertLess(r["new_conf"], r["old_conf"])
        self.assertEqual(r["verification_status"], "disputed")

    def test_reinforce_raises_confidence(self):
        self.db.correction(self.key, session_id="smoke", signal="不对")
        r = self.db.reinforce(self.key, mode="explicit")
        self.assertTrue(r["ok"])
        self.assertGreater(r["new_conf"], r["old_conf"])


class TestAge(unittest.TestCase):
    """age() 状态机：active→stale→archived"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=FakeEmbedder())
        self.key = "smoke/age-me"
        self.db.set(self.key, "这是一条用于测试遗忘状态机的记忆内容",
                    category="env", embed=False)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_age_returns_stats(self):
        r = self.db.age()
        self.assertIn("decayed", r)
        self.assertIn("profiles", r)

    def test_age_state_transition_to_stale(self):
        # 人为把 accessed_at 拨到 90 天前（slow 档阈值），age 后应进入 stale
        import datetime
        old = (datetime.datetime.now() - datetime.timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
        self.db._conn.execute(
            "UPDATE memories SET accessed_at=? WHERE key=?", (old, self.key))
        self.db._conn.commit()
        self.db.age()
        st = self.db._conn.execute(
            "SELECT verification_status, stale_since FROM memories WHERE key=?",
            (self.key,)).fetchone()
        self.assertIsNotNone(st["stale_since"])


class TestDegradation(unittest.TestCase):
    """无 state.db / 无 API key 时的优雅降级"""

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

    def test_verify_recent_without_state_db_degrades(self):
        self.db.set("smoke/v1", "无源记忆内容测试一", embed=False)
        r = self.db.verify_recent(limit=10)
        # 无 state.db：源不可达 → 降级计数，不抛异常
        self.assertEqual(r["degraded"] + r["verified"], 1)

    def test_search_state_fast_without_state_db_empty(self):
        self.assertEqual(self.db.search_state_fast("显卡"), [])

    def test_cross_check_without_state_db(self):
        r = self.db._cross_check("显卡价格是550块", "session-x")
        self.assertEqual(r["score"], 0.0)

    def test_gate_no_key_rule_fallback(self):
        # 不设置任何 API key：gate 应走规则/放行路径，不抛异常
        mg = MemoryGate()
        res = mg.gate("这是一条长度超过二十个字符的稳定事实记忆内容")
        self.assertIn(res["verdict"], ("MEMORY", "SKILL", "DROP"))

    def test_detect_correction_no_key(self):
        r = detect_correction("不对，应该是550块", layer2=True)
        # Layer1 规则命中，无需 key
        self.assertIsNotNone(r)
        self.assertEqual(r["layer"], 1)


class TestDirtyLog(unittest.TestCase):
    """dirty_log 参数化"""

    def test_dirty_log_file_written(self):
        fd, log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        tmp = make_tmp_db()
        db = MemoryDB(db_path=tmp, dirty_log=log_path, embedder=FakeEmbedder())
        try:
            db.set("smoke/dirty", "写入 dirty 日志测试内容", embed=False)
            content = open(log_path, encoding="utf-8").read()
            self.assertIn("smoke/dirty", content)
        finally:
            db.close()
            os.remove(tmp)
            os.remove(log_path)

    def test_dirty_log_none_no_crash(self):
        tmp = make_tmp_db()
        db = MemoryDB(db_path=tmp, embedder=FakeEmbedder())
        try:
            db.set("smoke/dirty2", "无 dirty_log 时走 logging 不崩溃", embed=False)
        finally:
            db.close()
            os.remove(tmp)


class TestEmbedderInjection(unittest.TestCase):
    """自定义 embedder 注入"""

    def test_injected_embedder_used(self):
        tmp = make_tmp_db()
        fake = FakeEmbedder()
        db = MemoryDB(db_path=tmp, embedder=fake)
        try:
            db.set("smoke/emb", "注入嵌入器测试内容文本足够长以触发嵌入调用", embed=True)
            self.assertGreater(fake.calls, 0)
        finally:
            db.close()
            os.remove(tmp)

    def test_http_embedder_offline_graceful(self):
        # 指向不存在的端口：embed 返回 None 而非抛异常
        e = HTTPEmbedder(url="http://127.0.0.1:1")
        self.assertIsNone(e.embed("任意文本"))
        self.assertFalse(e.is_available())


if __name__ == "__main__":
    unittest.main(verbosity=2)
