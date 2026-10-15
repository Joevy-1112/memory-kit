#!/usr/bin/env python3
"""mcp_memory_server.py(已修补版) 的回归测试 —— 覆盖 REVIEW.md 的 P0-A/B/C.

不需要 pytest: 直接 `python3 tests/test_mcp_server_patched.py`, 失败返回 1。
所有用例跑在 /tmp 的临时库上, 不碰 ~/.hermes/memory.db。
"""
import json
import os
import subprocess
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "mcp_memory_server.py")
BASELINE_PY = "/usr/bin/python3"          # 已知没装 jieba
VENV_PY = "/home/joe/venv-flagembed/bin/python3"

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def rpc(calls, env_extra=None, tmpdir=None):
    """拉起 MCP server, 走一遍 calls, 返回 (responses, stderr)。"""
    env = dict(os.environ)
    env.pop("MEMORYKIT_PY", None)
    # 关键: 必须指向工作区这份"已修补"的 bridge, 否则测的是线上旧 bridge
    env["MEMORYKIT_OVERLAY_BRIDGE"] = os.path.join(ROOT, "bridge.py")
    env["MEMORYKIT_DB"] = os.path.join(tmpdir, "mem.db") if tmpdir else "/tmp/_no_such_dir_/mem.db"
    env["MEMORYKIT_STATE_DB"] = os.path.join(tmpdir, "state.db") if tmpdir else "/tmp/_no_such_dir_/state.db"
    env["MEMORYKIT_AGENT_ID"] = "dsh"
    if env_extra:
        env.update(env_extra)
    lines = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in calls)
    proc = subprocess.run([sys.executable, SERVER], input=lines, capture_output=True,
                          text=True, timeout=300, env=env)
    responses = []
    for ln in proc.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        responses.append(json.loads(ln))          # 任何非 JSON 行 → 直接抛错
    return responses, proc.stderr, proc.stdout


def call(tool, args=None):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}}}


def payload(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def main():
    tmp = tempfile.mkdtemp(prefix="mk_mcp_test_")

    print("\n[T1/T2] 初始化 + 工具面 (P0-B)")
    resp, err, out = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ], tmpdir=tmp)
    check("initialize 返回 serverInfo 0.2.0",
          resp[0]["result"]["serverInfo"]["version"] == "0.2.0", str(resp[0])[:120])
    names = [t["name"] for t in resp[1]["result"]["tools"]]
    check("工具数 = 15 (原 8)", len(names) == 15, f"got {len(names)}: {names}")
    for t in ("learn_batch", "overlay", "episodic", "knowledge", "stats", "get", "search_state", "verify_stats"):
        check(f"新增工具 {t}", t in names)
    check("stdout 全是合法 JSON-RPC (诊断只走 stderr)", len(resp) == 2 and "[" not in out.splitlines()[0])

    print("\n[T3] 解释器自动挑选 (P0-A)")
    check("挑选结果写进 stderr 且 jieba=yes", "jieba=yes" in err, err.strip()[:160])
    check("挑中的不是裸 python3", VENV_PY in err, err.strip()[:160])

    print("\n[T4] 没有可用解释器时必须显式告警 (不再静默)")
    # 强制用没 jieba 的解释器拉起 server, 并跳过探测 → 走降级路径
    env = dict(os.environ)
    env.pop("MEMORYKIT_PY", None)
    env["MEMORYKIT_SKIP_PY_PROBE"] = "1"
    env["MEMORYKIT_DB"] = os.path.join(tmp, "mem.db")
    proc = subprocess.run([BASELINE_PY, SERVER], input="",
                          capture_output=True, text=True, timeout=120, env=env)
    check("降级路径打印 WARN + 修复建议",
          "WARN" in proc.stderr and "MEMORYKIT_PY" in proc.stderr, proc.stderr.strip()[:200])
    check("降级路径标注 jieba=NO", "jieba=NO" in proc.stderr, proc.stderr.strip()[:200])

    print("\n[T5] 经 MCP learn 的中文专名能抽到实体 (P0-A 端到端)")
    resp, err, out = rpc([call("learn", {
        "content": "我的猫叫小鸡翅，是只银渐层，特别粘人，最喜欢趴键盘",
        "source": "test"})], tmpdir=tmp)
    check("learn 成功且回执带 learned=True", payload(resp[0]).get("learned") is True, str(payload(resp[0]))[:200])
    learned_key = (payload(resp[0]).get("result") or {}).get("key")
    check("回执带 agent_attribution=ok (P1-D)",
          payload(resp[0]).get("agent_attribution") == "ok", str(payload(resp[0]))[:200])
    con = sqlite3.connect(os.path.join(tmp, "mem.db"))
    row = con.execute("select agent_id, entities from memories order by id desc limit 1").fetchone()
    con.close()
    check("agent_id = dsh", row and row[0] == "dsh", str(row))
    ents = json.loads(row[1]) if row and row[1] else []
    check("entities 非空 (原实现是 NULL)", bool(ents), f"entities={row[1] if row else None}")
    check("抽到中文专名 小鸡翅", "小鸡翅" in ents, str(ents))

    print("\n[T6] recall 走真 episodic (P0-B)")
    resp, err, out = rpc([
        call("episodic", {"action": "checkin", "session_id": "test-sess-1"}),
        call("recall", {"limit": 5}),
        call("recall", {"query": "测试事件", "limit": 5}),
    ], tmpdir=tmp)
    check("episodic checkin 成功", payload(resp[0]).get("ok") is True, str(payload(resp[0]))[:200])
    p2, p3 = payload(resp[1]), payload(resp[2])
    check("recall 返回 episodes 结构而不是 hits",
          "episodes" in p2 and "hits" not in p2, str(p2)[:200])
    check("recall(list) 至少 1 条事件", len(p2.get("episodes") or []) >= 1, str(p2)[:200])
    check("recall(query) 也是 episodes 结构", "episodes" in p3, str(p3)[:200])

    print("\n[T7] bridge 失败必须上报, 不再伪装成空结果 (P0-C)")
    # (a) 业务级失败: DB 路径挂在一个"文件"底下 → sqlite 打不开
    resp, err, out = rpc([call("search", {"query": "任意"})],
                         env_extra={"MEMORYKIT_DB": "/etc/hostname/mem.db"}, tmpdir=tmp)
    r0 = resp[0]
    check("bridge 业务失败 → search 返回 isError", r0["result"].get("isError") is True, str(r0)[:240])
    check("错误信息里带 bridge 的原因",
          "error" in (r0["result"].get("content") or [{}])[0].get("text", ""), str(r0)[:240])
    # (b) 进程级失败: bridge 路径不存在
    resp2, _, _ = rpc([call("search", {"query": "任意"})],
                      env_extra={"MEMORYKIT_OVERLAY_BRIDGE": os.path.join(tmp, "nope.py")}, tmpdir=tmp)
    check("bridge 进程失败 → search 返回 isError", resp2[0]["result"].get("isError") is True, str(resp2[0])[:240])

    print("\n[T8] 未知工具")
    resp, err, out = rpc([call("no_such_tool")], tmpdir=tmp)
    check("未知工具返回 isError", resp[0]["result"].get("isError") is True, str(resp[0])[:160])

    print("\n[T9] verify 的 batch 分支与 TS 侧 mk_verify 对齐")
    resp, err, out = rpc([call("verify_stats"), call("verify", {"batch": True, "limit": 2})], tmpdir=tmp)
    check("verify_stats 返回 stats", "stats" in payload(resp[0]), str(payload(resp[0]))[:200])
    check("verify(batch=true) 走批量并返回 stats", "stats" in payload(resp[1]), str(payload(resp[1]))[:200])

    print("\n[T10] 其余新增工具的参数映射 + 返回形态")
    check("T5 拿到了可复用的 key", bool(learned_key), str(learned_key))
    resp, err, out = rpc([
        call("get", {"key": learned_key}),
        call("overlay", {"action": "resolve", "query": "猫", "limit": 3}),
        call("overlay", {"action": "expand", "key": learned_key}),
        call("stats"),
        call("knowledge", {"action": "list", "limit": 3}),
        call("search_state", {"query": "小鸡翅", "limit": 3}),
    ], tmpdir=tmp)
    pg, po, pe, ps, pk, pss = (payload(r) for r in resp)
    check("get 返回 entry 且内容对得上", "小鸡翅" in json.dumps(pg.get("entry") or {}, ensure_ascii=False),
          str(pg)[:200])
    check("overlay resolve 返回 overlay 列表", bool(po.get("overlay")), str(po)[:200])
    check("overlay expand 返回 memory + state_original",
          "memory" in pe and "state_original" in pe, str(pe)[:200])
    check("stats 返回总量", (ps.get("stats") or {}).get("total", 0) >= 1, str(ps)[:200])
    check("knowledge list 返回 nodes", "nodes" in pk, str(pk)[:200])
    check("search_state 在 state.db 缺失时优雅返回空而不是报错",
          pss.get("hits") == [] or isinstance(pss.get("hits"), list), str(pss)[:200])

    print("\n[T11] import 后直接调 handle_tool（不先进 main）—— 惰性选解释器")
    os.environ["MEMORYKIT_OVERLAY_BRIDGE"] = os.path.join(ROOT, "bridge.py")
    os.environ["MEMORYKIT_DB"] = os.path.join(tmp, "mem.db")
    os.environ["MEMORYKIT_STATE_DB"] = os.path.join(tmp, "state.db")
    sys.path.insert(0, ROOT)
    import mcp_memory_server as m          # noqa: E402
    m.PY = None                            # 模拟"还没进 main()"
    r = m.handle_tool("stats", {})
    check("PY=None 时不崩（原实现会 subprocess [None, ...]）", r.get("isError") is not True, str(r)[:200])
    check("各工具 limit 有硬上限", m._LIMIT_HARD.get("episodic") == 200 and m._LIMIT_HARD.get("search") == 10,
          str(m._LIMIT_HARD))

    print("\n[T12] learn_batch 保留 + 归属校验 (rebase 回归防线)")
    resp, err, out = rpc([call("learn_batch", {"items": [
        {"content": "批量条目一：机箱风扇是猫头鹰A12x25"},
        {"content": "批量条目二：显示器是27寸4K 60Hz"},
        {"content": "短"},                        # <10 字符 → 记 error, 但不中断整批
    ]})], tmpdir=tmp)
    pb = payload(resp[0])
    check("learn_batch 整批返回 learned=True", pb.get("learned") is True, str(pb)[:240])
    check("计数 3 条 / 2 成功 / 1 失败",
          pb.get("total") == 3 and pb.get("succeeded") == 2 and pb.get("failed") == 1, str(pb)[:300])
    res = pb.get("results") or []
    check("结果与输入顺序对齐(第 3 条为 error)",
          len(res) == 3 and (res[2] or {}).get("error") == "content too short", str(res)[:300])
    check("learn_batch 回执带 agent_attribution=ok (P1-D)",
          pb.get("agent_attribution") == "ok", str(pb)[:200])
    con = sqlite3.connect(os.path.join(tmp, "mem.db"))
    n_dsh = con.execute("select count(*) from memories where agent_id='dsh'").fetchone()[0]
    con.close()
    check("写入条目归属 dsh, 没漏给 hermes", n_dsh >= 3, f"dsh rows={n_dsh}")

    # 2026-10-15 第二轮 rebase 的教训: 补丁基线陈旧会把 learn_batch 整体删掉,
    # 而"工具数"这类计数断言抓不到(数量对得上、功能没了) —— 必须显式钉住名字。
    with open(os.path.join(ROOT, "bridge.py"), encoding="utf-8") as f:
        bridge_src = f.read()
    check("bridge.py 仍导出 learn_batch 子命令", '"learn_batch": cmd_learn_batch' in bridge_src)
    check("MCP TOOL_IMPL 仍含 learn_batch", "learn_batch" in m.TOOL_IMPL)
    check("learn_batch 有独立超时(不被 30s 默认值砍)",
          m._TOOL_TIMEOUT.get("learn_batch", 0) >= 300, str(m._TOOL_TIMEOUT))

    print(f"\n{'='*60}\n{len(fails)} 个失败" + (f": {fails}" if fails else " — 全绿 ✅"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
