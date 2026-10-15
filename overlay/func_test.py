#!/usr/bin/env python3
"""memorykit-overlay 功能测试：learn→search→correct→reinforce→verify→overlay 全链路。
跑在临时库上，不动小龙虾正式数据。"""
import json
import os
import subprocess
import sys

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
TEST_DB = "/tmp/mktest_func.db"

# 用临时库，隔离测试
env = dict(os.environ)
env["MEMORYKIT_DB"] = TEST_DB
env["MEMORYKIT_STATE_DB"] = "/tmp/mktest_state.db"

if os.path.exists(TEST_DB):
    os.remove(TEST_DB)


def call(cmd, args=None):
    r = subprocess.run(
        ["python3", BRIDGE, cmd, json.dumps(args or {}, ensure_ascii=False)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"bad json: {r.stdout[:200]} | stderr: {r.stderr[:200]}"}


PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


print("== 1. learn 写入 ==")
r = call("learn", {"content": "显卡预算550块，显存12G", "source": "task"})
check("learn 显卡", r.get("ok"), str(r)[:200])
gpu_key = r.get("result", {}).get("key")

r = call("learn", {"content": "服务器IP是192.168.5.50，Ubuntu系统", "source": "task"})
check("learn 服务器", r.get("ok"), str(r)[:200])
srv_key = r.get("result", {}).get("key")

r = call("learn", {"content": "Joe养了只银渐层猫叫小鸡翅", "source": "discovery"})
check("learn 猫", r.get("ok"), str(r)[:200])
cat_key = r.get("result", {}).get("key")

print("== 2. search 多路检索 ==")
r = call("search", {"query": "显卡多少钱", "limit": 5})
hits = r.get("hits", [])
check("search 显卡命中", any("显卡" in (h.get("content") or "") for h in hits), str(hits)[:200])
check("search 返回裁剪(无vector)", all("vector" not in h for h in hits))

r = call("search", {"query": "猫叫什么", "limit": 5})
hits = r.get("hits", [])
check("search 猫命中", any("小鸡翅" in (h.get("content") or "") for h in hits), str(hits)[:200])

r = call("search", {"query": "完全不相关的话题zzz", "limit": 5})
check("search 无关查询仍返回(容错)", r.get("ok"))

print("== 3. correct 纠正锚定 ==")
r = call("correct", {"key": gpu_key, "signal": "不对，是600块"})
check("correct 降置信", r.get("ok") and r.get("new_conf", 1) < r.get("old_conf", 0), str(r)[:200])
check("correct 标记disputed", r.get("verification_status") == "disputed", str(r)[:200])

r = call("get", {"key": gpu_key})
check("get 确认disputed", r.get("entry", {}).get("verification_status") == "disputed", str(r)[:200])

print("== 4. reinforce 正向强化 ==")
r = call("reinforce", {"key": cat_key, "mode": "explicit"})
check("reinforce 升置信", r.get("ok") and r.get("new_conf", 0) > r.get("old_conf", 0), str(r)[:200])

# 完整循环：先 correct 变 disputed，再 reinforce 应转回 verified
r = call("correct", {"key": cat_key, "signal": "不对"})
check("cat 先变 disputed", r.get("verification_status") == "disputed", str(r)[:200])
r = call("reinforce", {"key": cat_key, "mode": "explicit"})
check("reinforce 后转verified", r.get("ok"), str(r)[:200])
r = call("get", {"key": cat_key})
check("get 确认verified", r.get("entry", {}).get("verification_status") == "verified", str(r)[:200])

print("== 5. verify 验证管线 ==")
r = call("verify", {"key": srv_key})
check("verify 单条可跑", r.get("ok"), str(r)[:200])
r = call("verify_stats", {})
check("verify_stats 返回", r.get("ok") and "stats" in r, str(r)[:200])

print("== 5b. verify_batch 批量验证 (v1.2.0 fresh 统计) ==")
r = call("verify_batch", {"limit": 10})
stats = r.get("stats", {})
check("verify_batch 返回 stats", r.get("ok") and isinstance(stats, dict), str(r)[:300])
check("verify_batch 带 v1.2.0 fresh_checked 键", "fresh_checked" in stats, str(stats)[:300])
check("verify_batch 带 v1.2.0 fresh_resolved 键(非旧名 fresh_changed)", "fresh_resolved" in stats and "fresh_changed" not in stats, str(stats)[:300])
check("verify_batch 只扫 pending(被correct/reinforce改过的不在列)", 1 <= stats.get("total", 0) <= 3, str(stats)[:300])

print("== 6. overlay 遮蔽/回溯 ==")
r = call("overlay", {"action": "resolve", "query": "服务器", "limit": 5})
check("overlay resolve", r.get("ok") and r.get("count", 0) >= 1, str(r)[:200])
ov = r.get("overlay", [])
if ov:
    check("overlay 带置信度", "confidence" in ov[0], str(ov[0])[:200])
    check("overlay 带状态", ov[0].get("status") is not None, str(ov[0])[:200])

r = call("overlay", {"action": "expand", "key": srv_key})
check("overlay expand(回溯state)", r.get("ok"), str(r)[:300])
check("overlay expand 带memory", r.get("memory", {}).get("key") == srv_key, str(r)[:200])

print("== 7. episodic 时间索引 ==")
r = call("episodic", {"action": "start", "session_id": "test-sess-1", "source": "test"})
check("episodic start", r.get("ok"), str(r)[:200])
r = call("episodic", {"action": "end", "session_id": "test-sess-1", "summary": "测了记忆插件"})
check("episodic end", r.get("ok"), str(r)[:200])
r = call("episodic", {"action": "list", "limit": 5})
check("episodic list", r.get("ok") and len(r.get("episodes", [])) >= 1, str(r)[:200])

print("== 8. knowledge 知识层 ==")
r = call("knowledge", {"action": "promote", "memory_key": gpu_key, "title": "显卡预算", "domain": "hardware"})
check("knowledge promote", r.get("ok"), str(r)[:200])
r = call("knowledge", {"action": "search", "query": "显卡"})
check("knowledge search", r.get("ok"), str(r)[:200])

print("== 8b. learn_batch 批量写入 (2026-10-15 新增) ==")
r = call("learn_batch", {"items": [
    {"content": "批量写入验证条目一：机箱风扇是猫头鹰A12x25", "source": "task"},
    {"content": "批量写入验证条目二：显示器是27寸4K 60Hz", "source": "task"},
    {"content": "短"},  # <10 字符，应记 error 但不影响整批
]})
check("learn_batch 整批返回", r.get("ok"), str(r)[:200])
check("learn_batch 计数 3 条/2 成功/1 失败",
      r.get("total") == 3 and r.get("succeeded") == 2 and r.get("failed") == 1, str(r)[:300])
res = r.get("results") or []
check("learn_batch 结果与输入顺序对齐(第3条为error)",
      len(res) == 3 and res[2].get("error") == "content too short", str(res)[:300])
check("learn_batch 前两条有 key", bool(res[0].get("key")) and bool(res[1].get("key")), str(res)[:300])

r = call("learn_batch", {"items": []})
check("learn_batch 空 items 报错", not r.get("ok") and "non-empty" in str(r.get("error")), str(r)[:200])

# agent_id 回写: dsh 身份写入不应被记成 hermes (与 cmd_learn 同款修复)
_aenv = dict(env)
_aenv["MEMORYKIT_AGENT_ID"] = "dsh"
_ar = subprocess.run(
    ["python3", BRIDGE, "learn_batch", json.dumps(
        {"items": [{"content": "批量写入身份归属验证条目：dsh 写的应记 agent_id=dsh"}]},
        ensure_ascii=False)],
    capture_output=True, text=True, env=_aenv, timeout=60,
)
_a = json.loads(_ar.stdout or "{}")
check("learn_batch agent_id 回写 dsh",
      _a.get("agent_id") == "dsh" and _a.get("agent_id_fixed", 0) >= 1, str(_a)[:200])

print("== 9. stats/cleanup/age ==")
r = call("stats", {})
check("stats", r.get("ok") and r.get("stats", {}).get("total", 0) >= 3, str(r)[:200])
r = call("age", {})
check("age 可跑", r.get("ok"), str(r)[:200])
r = call("cleanup", {})
check("cleanup 可跑", r.get("ok"), str(r)[:200])

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
