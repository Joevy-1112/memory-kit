#!/usr/bin/env python3
"""自检: 按 dsh 的接线方式拉起 mcp_memory_server.py, 打印 MCP 工具清单。

用途: dsh 重启后确认记忆桥可达 + 工具已更新 (修补版 15 工具是否在列)。
      learn_batch 必须在列 —— 2026-10-15 第二轮 rebase 曾把整条能力删掉,
      这条断言就是那次事故留下的防线。
用法: python3 verify_mcp_tools.py            # 正常退出 0, 缺工具退出 1
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_memory_server.py")
REQUIRED = {"search", "learn", "learn_batch", "set", "get", "correct", "reinforce",
            "verify", "verify_stats", "recall", "episodic", "overlay",
            "search_state", "knowledge", "stats"}


def main() -> int:
    env = dict(os.environ)
    env.setdefault("MEMORYKIT_OVERLAY_BRIDGE", os.path.join(HERE, "bridge.py"))
    env.setdefault("MEMORYKIT_DB", "/home/joe/.hermes/memory.db")
    env.setdefault("MEMORYKIT_AGENT_ID", "dsh")
    p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, env=env)

    def call(obj):
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in tools["result"]["tools"]]
        missing = sorted(REQUIRED - set(names))
        print(f"serverInfo={init['result']['serverInfo']}")
        print(f"tools({len(names)})={names}")
        if missing:
            print(f"MISSING={missing}")
            return 1
        print("OK: 记忆桥可达, 工具清单完整")
        return 0
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            p.kill()


if __name__ == "__main__":
    sys.exit(main())
