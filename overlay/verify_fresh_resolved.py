#!/usr/bin/env python3
"""验证 bridge verify_batch 能调通 memory-kit v1.2.0 的 fresh_resolved 统计。
构造一条带 command 活验证(validator)配置的记忆 → 断言 fresh_checked>=1 / fresh_resolved>=1。"""
import json
import os
import subprocess
import sys

sys.path.insert(0, "/home/joe/memory-kit")

TEST_DB = "/tmp/mktest_fresh.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from memorykit import MemoryDB  # noqa: E402

# 临时库, 无 state.db
db = MemoryDB(db_path=TEST_DB, state_db_path=None)
# 带活验证配置: command true 一定 ok → current
r1 = db.learn(
    "服务器探针地址是 http://127.0.0.1:8082/health, 每分钟上报一次",
    source="task",
    validator={"type": "command", "spec": {"kind": "command", "command": "true"}},
)
print("learn1:", json.dumps(r1, ensure_ascii=False)[:200])
# 无 fresh 配置的普通记忆
r2 = db.learn("Joe 的笔电是 Windows 11, 内存 32G", source="discovery")
print("learn2:", json.dumps(r2, ensure_ascii=False)[:200])
db.close()

# 走 bridge verify_batch (用 venv python, 与插件运行时一致)
env = dict(os.environ)
env["MEMORYKIT_DB"] = TEST_DB
env["MEMORYKIT_STATE_DB"] = "/tmp/mktest_fresh_state.db"  # 不存在 → None
BRIDGE = "/home/joe/memorykit-overlay/bridge.py"
PY = "/home/joe/venv-flagembed/bin/python3"
if not os.path.exists(PY):
    PY = "python3"
r = subprocess.run(
    [PY, BRIDGE, "verify_batch", json.dumps({"limit": 10, "only_pending": True})],
    capture_output=True, text=True, env=env, timeout=120,
)
out = json.loads(r.stdout)
print("verify_batch:", json.dumps(out, ensure_ascii=False)[:500])
stats = out.get("stats", {})
ok = all([
    out.get("ok"),
    stats.get("fresh_checked", 0) >= 1,
    stats.get("fresh_resolved", 0) >= 1,
    "fresh_changed" not in stats,  # v1.2.0 已改名, 旧键不应出现
])
print("RESULT:", "PASS" if ok else "FAIL")
print("fresh_checked=%s fresh_resolved=%s (期望 >=1)" % (stats.get("fresh_checked"), stats.get("fresh_resolved")))
sys.exit(0 if ok else 1)
