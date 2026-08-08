#!/usr/bin/env python3
"""freshness 接线回归测试（P0 #2，2026-08-08）

设计（design-notes/scope-recall-borrowing.md §2 + 借用纪律）：
  - validator 配置/状态存 provenance.fresh（零新表，最小侵入）
  - learn()/set() 可传 validator={"type","spec","ttl_days"} → 写 fresh
  - verify() 做过期判定（valid_until → expired/0.28）+ 活验证
    （file_exists/command/http → current/1.0 或 stale/0.35）
  - search_multi 乘性降权（freshness_penalty，无 fresh 配置 = 1.0 零影响）

覆盖：
  #1 learn 带 validator → provenance.fresh 写入（type/spec/status/penalty）
  #2 learn 带 ttl_days → valid_until = now + ttl
  #3 set 直接带 validator → 同 #1（learn 底层）
  #4 不传 validator → 零影响（provenance 无 fresh，旧行为不变）
  #5 verify 过期判定：valid_until 手工改过去 → expired + penalty 0.28
  #6 verify 活验证通过：file_exists 真实路径 → current + 1.0 + last_checked_at
  #7 verify 活验证失败：file_exists 不存在路径 → stale + 0.35
  #8 活验证通过 + ttl → valid_until 顺延（续期路径）
  #9 search_multi：expired 记忆降权排后 + freshness_penalty 字段暴露
  #10 未知 validator type → fail-closed 到 manual（不自动验证）
  #11 旧 provenance（无 fresh）verify 零影响

运行：
  python run_tests.py freshness
  或：python -m unittest tests.test_freshness -v
"""
import json
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
    """纯自建空库（测试隔离铁律：不依赖正式库副本）。"""
    fd, path = tempfile.mkstemp(prefix="mk-fresh-", suffix=".db")
    os.close(fd)
    db = MemoryDB(db_path=path)
    return db, path


def _prov(db, key):
    row = db._conn.execute(
        "SELECT provenance FROM memories WHERE key=?", (key,)).fetchone()
    if not row or not row["provenance"]:
        return {}
    try:
        return json.loads(row["provenance"])
    except (json.JSONDecodeError, TypeError):
        return {}


class FreshnessLearnTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_tmp_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        os.path.exists(self.path) and os.unlink(self.path)

    def test_01_learn_with_validator_writes_fresh(self):
        r = self.db.learn(
            "服务器嵌入服务端口是 8082 且必须保持运行",
            source="task",
            validator={"type": "file_exists", "spec": {"path": "/etc/hostname"}})
        prov = _prov(self.db, r["key"])
        self.assertIn("fresh", prov)
        f = prov["fresh"]
        self.assertEqual(f["type"], "file_exists")
        self.assertEqual(f["spec"], {"path": "/etc/hostname"})
        self.assertEqual(f["status"], "unchecked")
        self.assertEqual(f["penalty"], 1.0)
        self.assertNotIn("valid_until", f)  # 无 ttl 不设有效期

    def test_02_learn_with_ttl_sets_valid_until(self):
        r = self.db.learn(
            "每周五下午要跑一次磁盘健康检查",
            validator={"type": "command", "spec": {"command": "true"},
                       "ttl_days": 7})
        f = _prov(self.db, r["key"])["fresh"]
        expect = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertEqual(f["valid_until"], expect)
        self.assertEqual(f["ttl_days"], 7)

    def test_03_set_with_validator_writes_fresh(self):
        self.db.set("env/test-port", "测试端口 8082",
                    validator={"type": "http",
                               "spec": {"url": "http://127.0.0.1:8082/v1/models"}})
        f = _prov(self.db, "env/test-port")["fresh"]
        self.assertEqual(f["type"], "http")
        self.assertEqual(f["spec"]["url"], "http://127.0.0.1:8082/v1/models")

    def test_04_no_validator_zero_impact(self):
        r = self.db.learn("一条普通记忆不配置任何验证器，行为应与旧版完全一致",
                          source="task")
        prov = _prov(self.db, r["key"])
        self.assertNotIn("fresh", prov)
        self.assertEqual(prov.get("v"), "pending")

    def test_10_unknown_type_fail_closed_manual(self):
        r = self.db.learn(
            "未知验证器类型应降级到 manual",
            validator={"type": "量子验证", "spec": {"x": 1}})
        f = _prov(self.db, r["key"])["fresh"]
        self.assertEqual(f["type"], "manual")
        # manual 不自动验证：verify 后 status 保持 unchecked（无过期）
        self.db.verify(r["key"])
        f2 = _prov(self.db, r["key"])["fresh"]
        self.assertEqual(f2["status"], "unchecked")


class FreshnessVerifyTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_tmp_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        if os.path.exists(self.path): os.unlink(self.path)

    def _expire(self, key):
        """把某条记忆的 valid_until 改成昨天（模拟时间流逝）。"""
        prov = _prov(self.db, key)
        prov["fresh"]["valid_until"] = (datetime.now() - timedelta(days=1))\
            .strftime("%Y-%m-%d")
        self.db._conn.execute(
            "UPDATE memories SET provenance=? WHERE key=?",
            (json.dumps(prov, ensure_ascii=False), key))

    def test_05_verify_expired_marks_expired(self):
        # manual 类型无自动验证 → 纯过期判定生效（有 spec 的会活验证续期，那是 test_08）
        r = self.db.learn(
            "这条记忆的验证已过期且没有自动验证器",
            validator={"type": "manual", "ttl_days": 30})
        self._expire(r["key"])
        out = self.db.verify(r["key"])
        f = _prov(self.db, r["key"])["fresh"]
        self.assertEqual(f["status"], "expired")
        self.assertEqual(f["penalty"], 0.28)
        self.assertIn("fresh", out)
        self.assertEqual(out["fresh"]["status"], "expired")

    def test_06_verify_live_check_ok(self):
        r = self.db.learn(
            "hostname 文件存在性验证应该通过",
            validator={"type": "file_exists", "spec": {"path": "/etc/hostname"}})
        out = self.db.verify(r["key"])
        f = _prov(self.db, r["key"])["fresh"]
        self.assertEqual(f["status"], "current")
        self.assertEqual(f["penalty"], 1.0)
        self.assertTrue(f.get("last_checked_at"))
        self.assertIn("fresh", out)
        self.assertEqual(out["fresh"]["status"], "current")

    def test_07_verify_live_check_fail(self):
        r = self.db.learn(
            "这个路径不存在所以验证应该失败",
            validator={"type": "file_exists",
                       "spec": {"path": "/nonexistent/mk-freshness-xyz"}})
        out = self.db.verify(r["key"])
        f = _prov(self.db, r["key"])["fresh"]
        self.assertEqual(f["status"], "stale")
        self.assertEqual(f["penalty"], 0.35)

    def test_08_verify_ok_renews_valid_until(self):
        r = self.db.learn(
            "续期验证：通过后有效期顺延",
            validator={"type": "file_exists",
                       "spec": {"path": "/etc/hostname"}, "ttl_days": 7})
        self._expire(r["key"])
        self.db.verify(r["key"])  # 活验证通过 → 应续期
        f = _prov(self.db, r["key"])["fresh"]
        expect = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertEqual(f["valid_until"], expect)
        self.assertEqual(f["status"], "current")

    def test_11_legacy_provenance_zero_impact(self):
        # 无 fresh 的旧 provenance：verify 不崩、不新增 fresh、不影响交叉验证
        r = self.db.learn("旧记忆没有验证器配置", source="session",
                          source_session="sess-1")
        # 手工把 provenance 改成旧形态（无 fresh）
        self.db._conn.execute(
            "UPDATE memories SET provenance=? WHERE key=?",
            ('{"v": "pending", "src": "sess-1"}', r["key"]))
        out = self.db.verify(r["key"])  # 无 state.db → unverifiable 分支
        prov = _prov(self.db, r["key"])
        self.assertNotIn("fresh", prov)
        self.assertIn("verification", out)


class FreshnessSearchTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_tmp_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        if os.path.exists(self.path): os.unlink(self.path)

    def test_09_expired_ranked_below_fresh(self):
        # 用 set() 建两条不同 key（避免 learn 语义去重合并），一条 fresh 一条 expired
        self.db.set("fresh/test-port", "server embedding port 8082 config",
                    validator={"type": "file_exists",
                               "spec": {"path": "/etc/hostname"}})
        self.db.set("expired/test-mount", "disk mount options discussed long ago",
                    validator={"type": "manual", "ttl_days": 1})
        # expired 那条：手工置过期 + 无活验证器（manual）→ 保持 expired
        prov = _prov(self.db, "expired/test-mount")
        prov["fresh"].update({"valid_until": "2000-01-01",
                              "status": "expired", "penalty": 0.28})
        self.db._conn.execute(
            "UPDATE memories SET provenance=? WHERE key='expired/test-mount'",
            (json.dumps(prov, ensure_ascii=False),))
        # fresh 那条：verify 活验证通过 → current
        self.db.verify("fresh/test-port")
        res = self.db.search_multi("port 8082 mount options", limit=5)
        keys = [d["key"] for d in res]
        self.assertIn("fresh/test-port", keys)
        self.assertIn("expired/test-mount", keys)
        # expired 的应排在 current 后面
        self.assertLess(keys.index("fresh/test-port"),
                        keys.index("expired/test-mount"))
        # freshness_penalty 字段暴露
        by_key = {d["key"]: d for d in res}
        self.assertLess(by_key["expired/test-mount"]["freshness_penalty"],
                        by_key["fresh/test-port"]["freshness_penalty"])


if __name__ == "__main__":
    unittest.main()
