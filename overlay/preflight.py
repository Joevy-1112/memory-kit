#!/usr/bin/env python3
"""memorykit-overlay 上线前自检 (2026-10-15, REVIEW.md 的"治本"项).

把三处**静默降级**变成显式结论:
  1. 解释器没有 jieba     → 实体抽取全灭(entities=NULL), 不报错
  2. 嵌入服务不可达        → 语义检索退化到 FTS/词表, 只表现为"搜得不准"
  3. memorykit 跑在脏工作树 → 线上行为 ≠ 任何已提交状态

只读: 不写库、不改配置。退出码 0=全绿 / 1=有 FAIL / 2=有 WARN。
用法: python3 preflight.py [--json]
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request

BRIDGE = os.environ.get("MEMORYKIT_OVERLAY_BRIDGE",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py"))
DB = os.environ.get("MEMORYKIT_DB", "/home/joe/.hermes/memory.db")
STATE_DB = os.environ.get("MEMORYKIT_STATE_DB", "/home/joe/.hermes/state.db")
EMBED_URL = os.environ.get("MEMORY_EMBED_URL", "http://127.0.0.1:8082").rstrip("/")
MEMORYKIT_SRC = "/home/joe/memory-kit"
JIEBA_PY_CANDIDATES = (
    os.environ.get("MEMORYKIT_PY"),
    "/home/joe/venv-flagembed/bin/python3",
    sys.executable,
)

results = []          # (level, name, detail)
CHOSEN_PY = sys.executable   # 由 check_python() 决定，后续探测都用它（贴近生产）


def add(level, name, detail=""):
    results.append((level, name, detail))


def check_python():
    """P0-A: 找一个带 jieba 的解释器。"""
    global CHOSEN_PY
    good = None
    for py in JIEBA_PY_CANDIDATES:
        if not py or not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", "import jieba"], capture_output=True, timeout=60)
            ok = r.returncode == 0
        except Exception:
            ok = False
        if ok:
            good = py
            CHOSEN_PY = py
            add("OK", "解释器 jieba", py)
            break
    if not good:
        add("FAIL", "解释器 jieba",
            "没有任何候选解释器能 import jieba → 实体抽取会静默降级(中文专名全灭)。"
            " 修复: 设 MEMORYKIT_PY=/home/joe/venv-flagembed/bin/python3")
    # dsh 实际会用的解释器(没设 MEMORYKIT_PY 时是裸 python3)是否也带 jieba
    if not os.environ.get("MEMORYKIT_PY") and good != sys.executable:
        add("WARN", "MEMORYKIT_PY 未设置",
            f"dsh 侧(mcp_memory_server.py)默认用 sys.executable={sys.executable};"
            f" 建议显式指向 {good or '带 jieba 的解释器'}")


def check_embed():
    """嵌入服务: 语义检索的前提。"""
    try:
        with urllib.request.urlopen(f"{EMBED_URL}/v1/models", timeout=4) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            model = (body.get("data") or [{}])[0].get("id", "?")
            add("OK", "嵌入服务", f"{EMBED_URL} model={model}")
    except Exception as e:  # noqa: BLE001
        add("FAIL", "嵌入服务", f"{EMBED_URL} 不可达: {type(e).__name__}: {e}")


def check_bridge():
    if not os.path.exists(BRIDGE):
        add("FAIL", "bridge.py", f"不存在: {BRIDGE}")
        return
    try:
        r = subprocess.run([CHOSEN_PY, BRIDGE, "stats", "{}"],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, MEMORYKIT_DB=DB, MEMORYKIT_STATE_DB=STATE_DB))
        out = json.loads(r.stdout or "{}")
        if out.get("ok"):
            s = out.get("stats", {})
            add("OK", "bridge.py stats", f"total={s.get('total')} categories={len(s.get('by_category') or {})}")
        else:
            add("FAIL", "bridge.py stats", str(out.get("error"))[:200])
    except Exception as e:  # noqa: BLE001
        add("FAIL", "bridge.py", f"{type(e).__name__}: {e}")


def check_db():
    if not os.path.exists(DB):
        add("FAIL", "memory.db", f"不存在: {DB}")
        return
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        n, agents = con.execute("select count(*), group_concat(distinct agent_id) from memories").fetchone()
        con.close()
        add("OK", "memory.db", f"{DB} rows={n} agents=[{agents}]")
    except Exception as e:  # noqa: BLE001
        add("FAIL", "memory.db", f"{type(e).__name__}: {e}")
    if os.path.exists(STATE_DB):
        add("OK", "state.db", f"{STATE_DB} ({os.path.getsize(STATE_DB) / 1e9:.2f} GB, overlay expand 依赖)")
    else:
        add("WARN", "state.db", f"不存在: {STATE_DB} → overlay expand / search_state 会返回空")


def check_worktree():
    """P1-E: 跑的是不是未提交的脏工作树。"""
    if not shutil.which("git") or not os.path.isdir(os.path.join(MEMORYKIT_SRC, ".git")):
        add("WARN", "memorykit 版本", f"不是 git 工作树: {MEMORYKIT_SRC}")
        return
    try:
        head = subprocess.run(["git", "-C", MEMORYKIT_SRC, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        dirty = subprocess.run(["git", "-C", MEMORYKIT_SRC, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as e:  # noqa: BLE001
        add("WARN", "memorykit 版本", f"{type(e).__name__}: {e}")
        return
    if dirty:
        # porcelain 行格式固定为 "XY path"，但用 split 更稳(避免状态位宽度假设)
        files = [ln.split(maxsplit=1)[1] if len(ln.split(maxsplit=1)) > 1 else ln
                 for ln in dirty.splitlines()][:6]
        add("WARN", "memorykit 工作树脏",
            f"HEAD={head} 有 {len(dirty.splitlines())} 处未提交改动: {', '.join(files)}"
            " → 线上行为 ≠ 任何已提交状态(不可复现)")
    else:
        add("OK", "memorykit 工作树", f"HEAD={head} 干净")


def check_agent():
    agent = os.environ.get("MEMORYKIT_AGENT_ID", "hermes(默认)")
    add("INFO", "写入身份", f"MEMORYKIT_AGENT_ID={agent}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    check_python()
    check_embed()
    check_bridge()
    check_db()
    check_worktree()
    check_agent()

    if args.json:
        print(json.dumps({"results": [{"level": l, "check": n, "detail": d} for l, n, d in results]},
                         ensure_ascii=False, indent=2))
    else:
        icon = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}
        for l, n, d in results:
            print(f"{icon.get(l, '?')} {n:22} {d}")
    fails = sum(1 for l, _, _ in results if l == "FAIL")
    warns = sum(1 for l, _, _ in results if l == "WARN")
    if not args.json:
        print(f"\n{len(results)} 项检查: {fails} FAIL / {warns} WARN")
    return 1 if fails else (2 if warns else 0)


if __name__ == "__main__":
    sys.exit(main())
