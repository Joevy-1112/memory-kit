#!/usr/bin/env python3
"""dsh 记忆 MCP server —— 把 memorykit(memory-kit + memorykit-overlay bridge)暴露为 MCP stdio 工具。

用法: dsh 通过 '@deepseek-ai/dsh-mcp-client' 以 stdio 拉起本脚本。
实现: 手写最小 MCP stdio(JSON-RPC 2.0, newline-delimited), 不依赖 mcp 库。
调用链: MCP 工具 → bridge.py(execFile) → memory-kit(db.search_multi / db.learn)

═══ 2026-10-15 修补 (dsh code review, 见 REVIEW.md) ═══════════════════════
P0-A 解释器自检: 原实现用 `sys.executable`(dsh 拉起的裸 python3, 无 jieba)→
     memorykit 的实体抽取**静默降级**, entities 直接落 NULL。现在:
     MEMORYKIT_PY 未指定时自动挑一个带 jieba 的解释器; 全都没有才退回
     sys.executable 并在 stderr 显式告警(附一行修复配置)。
P0-B 工具面补齐: 8 → 15, 覆盖 bridge 全部 17 个子命令中的 15 个
     (cleanup/age 是运维命令, 故意不暴露)。recall 修真——走 bridge 真正的
     episodic 事件时间线, 不再冒充 search。
P0-C 错误不再吞: bridge 返回 ok=false 时以 isError 上报调用方, 而不是伪装成
     "没搜到"({"hits": []})。
治本   启动自检: jieba / 嵌入服务不可达都在 stderr 报警。
      ⚠ stdout 是 JSON-RPC 通道, 本文件所有诊断输出只走 stderr。

⚠ rebase 说明 (2026-10-15 第二轮): 上一版补丁基于当天 18:37 的生产快照，
  漏掉了 21:26 才落到生产的 learn_batch（bridge 子命令 + 本文件工具 + func_test
  第 8b 节）。若直接上线会把批量写入能力删掉。本版以「当前生产」为基线重新落
  补丁：learn_batch 保留（含 600s 批量超时 _TOOL_TIMEOUT），其余修补不变。

回归: tests/test_mcp_server_patched.py (初始化 / 工具面 / 实体抽取 / 错误上报 / learn_batch)
"""
import json
import os
import subprocess
import sys
import urllib.request

BRIDGE = os.environ.get("MEMORYKIT_OVERLAY_BRIDGE", "/home/joe/memorykit-overlay/bridge.py")
BRIDGE_TIMEOUT = int(os.environ.get("MEMORYKIT_BRIDGE_TIMEOUT", "30"))
EMBED_URL = os.environ.get("MEMORY_EMBED_URL", "http://127.0.0.1:8082").rstrip("/")

# 带 jieba 的解释器优先(实体抽取中文专名必需); 见 REVIEW.md P0-A
PY_CANDIDATES = (
    os.environ.get("MEMORYKIT_PY"),
    "/home/joe/venv-flagembed/bin/python3",
)
PY = None          # 启动时由 pick_python() 决定
PY_HAS_JIEBA = None


def _log(msg: str) -> None:
    """诊断输出——只能走 stderr, stdout 是 JSON-RPC 通道。"""
    sys.stderr.write(f"[memorykit-overlay] {msg}\n")
    sys.stderr.flush()


def _has_jieba(py: str) -> bool:
    try:
        # 20s 足够（首次 import jieba 要建前缀词典缓存）；再长只会拖慢 MCP 启动
        r = subprocess.run([py, "-c", "import jieba"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def pick_python() -> tuple:
    """挑一个能抽中文实体的解释器; 全不行则退回 sys.executable 并告警。"""
    if os.environ.get("MEMORYKIT_SKIP_PY_PROBE"):
        py = os.environ.get("MEMORYKIT_PY") or sys.executable
        ok = _has_jieba(py)
        if not ok:
            _warn_no_jieba(py)
        return py, ok
    for cand in PY_CANDIDATES:
        if cand and os.path.exists(cand) and _has_jieba(cand):
            return cand, True
    fallback = os.environ.get("MEMORYKIT_PY") or sys.executable
    _warn_no_jieba(fallback)
    return fallback, False


def _warn_no_jieba(py: str) -> None:
    _log(
        "WARN 没有找到带 jieba 的解释器 → 实体抽取会静默降级(中文专名全灭, entities=NULL)。"
        f" 已退回 {py}。修复: 在 ~/.dsh/cordis.patch.yml 的 memory-memorykit.env 里加 "
        "MEMORYKIT_PY: /home/joe/venv-flagembed/bin/python3"
    )


def check_embed_service() -> None:
    """嵌入服务不可达时显式告警(否则表现为"检索变差"而无人知晓)。"""
    try:
        req = urllib.request.Request(f"{EMBED_URL}/v1/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                _log(f"WARN 嵌入服务 {EMBED_URL} 返回 HTTP {resp.status} → 语义检索将退化")
    except Exception as e:  # noqa: BLE001
        _log(f"WARN 嵌入服务 {EMBED_URL} 不可达({type(e).__name__}: {e}) → 语义检索将退化到 FTS/词表")


TOOLS = [
    {
        "name": "search",
        "description": "语义检索记忆库, 返回与 query 最相关的历史记忆条目(含来源/时间/得分)。适合在回答前回忆相关上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索查询(自然语言, 可用句子)"},
                "limit": {"type": "integer", "description": "返回条数, 默认 5, 最大 10"},
                "mode": {"type": "string", "enum": ["multi", "semantic", "two_stage"], "description": "检索模式, 默认 multi"},
                "category": {"type": "string", "description": "按类别过滤(如 env/lesson/user)"},
                "scope": {"type": "string", "description": "按 scope 过滤(permanent/session/temp)"},
                "max_age_days": {"type": "number", "description": "时间检索年龄窗口(天), 与 ref_time 成对生效"},
                "ref_time": {"type": "string", "description": "时间检索基准时间"},
                "strict_time_window": {"type": "boolean", "description": "时间窗硬过滤(默认 false 仅软重排)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "learn",
        "description": "把一条新知识/事实/结论写入记忆库(自动嵌入+去重+自动分类重要度)。适合在任务结束时沉淀值得记住的东西。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容(完整句子, 含上下文)"},
                "source": {"type": "string", "description": "来源标签, 默认当前 agent 身份"},
                "source_session": {"type": "string", "description": "来源会话 id(供后续 overlay expand 回溯原文)"},
                "event_time": {"type": "string", "description": "事件发生时间 YYYY-MM-DD HH:MM:SS"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "learn_batch",
        "description": "批量写入多条记忆(一次批量嵌入 + 逐条去重/合并, 比逐条 learn 快数倍)。适合任务结束时一次性沉淀多条结论。单条失败不影响整批, 结果按输入顺序返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "条目数组, 每项 {content, source?, source_session?, event_time?}; content 不足 10 字符会被跳过并记 error",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "要记住的内容(完整句子)"},
                            "source": {"type": "string", "description": "来源标签, 默认当前 agent 身份"},
                            "source_session": {"type": "string", "description": "来源会话(可选)"},
                            "event_time": {"type": "string", "description": "事件时间(可选)"},
                        },
                        "required": ["content"],
                    },
                },
                "source": {"type": "string", "description": "整批默认来源标签, 默认当前 agent 身份"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "set",
        "description": "显式写入一条结构化记忆(手动指定 key/category/importance/tags/scope)。适合需要精确控制分类的条目; 一般用 learn 即可。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "唯一键(如 general/日期_标题)"},
                "content": {"type": "string", "description": "内容"},
                "category": {"type": "string", "description": "分类, 默认 general"},
                "importance": {"type": "number", "description": "重要度 0-1, 默认 0.5"},
                "tags": {"type": "string", "description": "逗号分隔标签"},
                "scope": {"type": "string", "description": "permanent/session/temp, 默认 permanent"},
            },
            "required": ["key", "content"],
        },
    },
    {
        "name": "get",
        "description": "按 key 精确取一条记忆(含 confidence/verification_status/provenance; 默认不含向量)。key 从 search 结果里拿。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "记忆 key(如 env/1015_ds故障)"},
                "include_vector": {"type": "boolean", "description": "是否带上向量字段(默认 false, 很占 token)"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "correct",
        "description": "纠正一条记忆(发现它错了/过时时调用): 置信度下降 + 标 disputed。correction 是记忆库唯一的外部锚点。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "记忆的 key(从 search 结果拿)"},
                "signal": {"type": "string", "description": "纠正原因/说明"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "reinforce",
        "description": "正向强化一条记忆(确认它正确/重要时): 置信度上升, disputed 条目可转 verified。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "记忆的 key"},
                "mode": {"type": "string", "description": "explicit(+0.15) 或 implicit(+0.05), 默认 implicit"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "verify",
        "description": "验证记忆(失败自动降级 confidence, 类似蒸馏验证回流)。单条: 传 key。批量: 传 batch=true 扫整库 pending。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "单条模式: 记忆的 key"},
                "batch": {"type": "boolean", "description": "批量验证模式(整库扫描), 默认 false"},
                "limit": {"type": "integer", "description": "batch 模式单批最大条数, 默认 100"},
                "only_pending": {"type": "boolean", "description": "batch 模式只验 pending, 默认 true"},
                "auto_degrade": {"type": "boolean", "description": "低分自动降级, 默认 true"},
                "threshold": {"type": "number", "description": "降级阈值, 默认 0.3"},
            },
        },
    },
    {
        "name": "verify_stats",
        "description": "记忆库验证状态总览: verified/pending/unverified 分布、置信度分层、平均验证分。用于判断库是否该跑批量验证。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "recall",
        "description": "检索事件时间线(episodic): '之前/什么时候做过什么'类问题用这个, 不是语义记忆检索(search 才是)。带 query = 事件检索, 不带 = 最近事件列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "事件检索描述; 省略则返回最近事件"},
                "limit": {"type": "integer", "description": "返回条数, 默认 5, 最大 20"},
            },
        },
    },
    {
        "name": "episodic",
        "description": "事件时间索引的完整操作: start/end/checkin/close/window/list/search。checkin=遮蔽窗心跳(开窗/续窗/超 30min 旋转), window=按时间窗查。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "end", "checkin", "close", "window", "list", "search"], "description": "操作类型, 默认 list"},
                "session_id": {"type": "string", "description": "start/end/checkin/close 必填"},
                "summary": {"type": "string", "description": "end/close 时的会话摘要"},
                "query": {"type": "string", "description": "search 时的检索描述"},
                "start": {"type": "string", "description": "window 起始时间"},
                "end": {"type": "string", "description": "window 结束时间"},
                "limit": {"type": "integer", "description": "返回条数"},
            },
        },
    },
    {
        "name": "overlay",
        "description": "memory-overlay 核心: 遮蔽管理。action=resolve 取高置信密集记忆片段; action=expand 沿 source_session 指针回溯 state.db 原文(memory→state 方向, 深究时用)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["resolve", "expand"], "description": "resolve=密集片段 / expand=展开原文"},
                "query": {"type": "string", "description": "resolve 时的检索描述"},
                "key": {"type": "string", "description": "expand 时要展开的记忆 key"},
                "limit": {"type": "integer", "description": "resolve 返回条数, 默认 5"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "search_state",
        "description": "从记忆回溯到 state.db 原文(深究): 在 3.2GB 会话档案里做 FTS+向量检索, 返回原始对话片段。用于查证某条结论的出处。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要回溯的原文关键词/句子"},
                "limit": {"type": "integer", "description": "返回条数, 默认 5"},
                "session_id": {"type": "string", "description": "限定会话(可选; 给了会在该会话内加权)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "knowledge",
        "description": "知识层(话题/关键词辅索引): 把高价值记忆提升为知识节点、按话题检索、查关联图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["promote", "search", "list", "graph"], "description": "操作类型, 默认 search"},
                "memory_key": {"type": "string", "description": "promote 时: 要提升的记忆 key"},
                "title": {"type": "string", "description": "promote 时: 知识节点标题"},
                "domain": {"type": "string", "description": "promote/list 时: 领域分类"},
                "query": {"type": "string", "description": "search 时的检索描述"},
                "kid": {"type": "integer", "description": "graph 时的知识节点 id"},
                "limit": {"type": "integer", "description": "返回条数"},
            },
        },
    },
    {
        "name": "stats",
        "description": "记忆库总量与分类/scope/重要度分布、向量状态。回答'库里有什么'类问题。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# ── 工具 → (bridge 命令, 参数映射, 输出形态) ────────────────────────────
# 输出形态: hits=记忆命中列表 / episodes=事件列表 / learn=写入回执 / raw=原样透传
_MEM_KEYS = ("query", "limit", "mode", "category", "scope", "ref_time",
             "max_age_days", "strict_time_window", "time_window")


def _map_memory(args):
    return {k: args[k] for k in _MEM_KEYS if k in args}


def _map_passthrough(*keys):
    def f(args):
        return {k: args[k] for k in keys if k in args}
    return f


def _cap(v, default: int, hard: int) -> int:
    try:
        return max(1, min(int(v), hard))
    except (TypeError, ValueError):
        return default


# 各工具的 limit 硬上限（2026-10-15 补，本地模型复审提出：原来只 clamp 了
# search/search_state，episodic/overlay/knowledge/verify_batch 可以被传巨大 limit
# 把返回撑爆，白白烧上下文）
_LIMIT_HARD = {"search": 10, "search_state": 10, "recall": 20,
               "episodic": 200, "overlay": 50, "knowledge": 100, "verify": 500}


def _map_recall(args):
    """recall = 真 episodic: 有 query 走事件检索, 没有走最近事件列表。"""
    if args.get("query"):
        return {"action": "search", "query": args["query"], "limit": _cap(args.get("limit"), 5, 20)}
    return {"action": "list", "limit": _cap(args.get("limit"), 5, 20)}


TOOL_IMPL = {
    "search":       ("search",       _map_memory, "hits"),
    "learn":        ("learn",        _map_passthrough("content", "source", "source_session", "event_time"), "learn"),
    "learn_batch":  ("learn_batch",  _map_passthrough("items", "source"), "batch"),
    "set":          ("set",          _map_passthrough("key", "content", "category", "scope", "importance", "tags"), "raw"),
    "get":          ("get",          _map_passthrough("key", "include_vector"), "raw"),
    "correct":      ("correct",      _map_passthrough("key", "signal", "session_id"), "raw"),
    "reinforce":    ("reinforce",    _map_passthrough("key", "mode", "session_id"), "raw"),
    "verify":       ("verify",       _map_passthrough("key", "auto_degrade", "threshold"), "raw"),
    "verify_stats": ("verify_stats", lambda a: {}, "raw"),
    "recall":       ("episodic",     _map_recall, "episodes"),
    "episodic":     ("episodic",     _map_passthrough("action", "session_id", "summary", "query", "start", "end", "limit"), "raw"),
    "overlay":      ("overlay",      _map_passthrough("action", "query", "key", "limit"), "raw"),
    "search_state": ("search_state", _map_passthrough("query", "limit", "session_id"), "hits"),
    "knowledge":    ("knowledge",    _map_passthrough("action", "memory_key", "title", "domain", "query", "kid", "limit"), "raw"),
    "stats":        ("stats",        lambda a: {}, "raw"),
}


def _ensure_py() -> str:
    """惰性选解释器：模块被 import 后直接调 handle_tool 也不会拿到 PY=None。"""
    global PY, PY_HAS_JIEBA
    if PY is None:
        PY, PY_HAS_JIEBA = pick_python()
    return PY


# 单次调用超时(秒)。learn_batch 走批量嵌入, 200 条可能几分钟 —— 生产中该工具
# 原本按 600s 调用，rebase 时保留；否则批量写入会被 30s 默认超时砍掉。
_TOOL_TIMEOUT = {"learn_batch": int(os.environ.get("MEMORYKIT_BATCH_TIMEOUT", "600"))}


def call_bridge(cmd: str, args: dict, timeout: int = None) -> dict:
    try:
        py = _ensure_py()      # 允许 import 后直接调 handle_tool（不必先进 main()）
        r = subprocess.run(
            [py, BRIDGE, cmd, json.dumps(args, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout or BRIDGE_TIMEOUT,
        )
        if r.returncode != 0:
            # bridge 约定: 退出码 2 = 业务失败(仍会打印 JSON), 其他 = 崩溃
            try:
                parsed = json.loads(r.stdout)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            return {"ok": False, "error": (r.stderr or f"bridge exit {r.returncode}")[-300:]}
        return json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


def _text(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _error(resp: dict, label: str) -> dict:
    """P0-C: bridge 失败必须显式上报, 不能伪装成空结果。"""
    msg = resp.get("error") or f"{label}: bridge returned ok=false"
    return {"isError": True, "content": [{"type": "text", "text": json.dumps(
        {"ok": False, "tool": label, "error": msg}, ensure_ascii=False)}]}


def format_hits(resp: dict, label: str) -> list:
    items = []
    for h in (resp.get("hits") or [])[:10]:
        items.append({"content": h.get("content", ""), "source": h.get("source_session") or label,
                      "time": (h.get("created_at") or "")[:19],
                      "score": h.get("rrf_score") or h.get("importance"),
                      "category": h.get("category", ""), "scope": h.get("scope", ""),
                      "tags": h.get("tags", ""), "key": h.get("key", "")})
    return items


def format_episodes(resp: dict) -> list:
    out = []
    for e in (resp.get("episodes") or [])[:20]:
        out.append({"key": e.get("key", ""), "title": e.get("title", ""),
                    "summary": e.get("summary", ""), "started_at": e.get("started_at"),
                    "ended_at": e.get("ended_at"), "source": e.get("source", ""),
                    "importance": e.get("importance")})
    return out


def _dispatch(name: str, args: dict):
    # verify 的 batch 分支: 与 OpenClaw TS 侧 mk_verify 行为保持一致
    if name == "verify" and args.get("batch"):
        return ("verify_batch",
                _map_passthrough("limit", "only_pending", "auto_degrade", "threshold")(args), "raw")
    cmd, mapper, shape = TOOL_IMPL[name]
    return cmd, mapper(args), shape


def handle_tool(name: str, args: dict) -> dict:
    if name not in TOOL_IMPL:
        return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}
    try:
        cmd, mapped, shape = _dispatch(name, args)
        if "limit" in mapped:
            hard = _LIMIT_HARD.get(name)
            if hard:
                mapped["limit"] = _cap(mapped["limit"], min(10, hard), hard)
        resp = call_bridge(cmd, mapped, timeout=_TOOL_TIMEOUT.get(name))
        if not resp.get("ok"):
            return _error(resp, f"{name}→{cmd}")
        if shape == "hits":
            return _text({"hits": format_hits(resp, "memory")})
        if shape == "episodes":
            return _text({"episodes": format_episodes(resp)})
        if shape == "learn":
            return _text({"learned": True, "result": resp.get("result"),
                          "agent_attribution": resp.get("agent_attribution")})
        if shape == "batch":
            return _text({"learned": True,
                          "total": resp.get("total"),
                          "succeeded": resp.get("succeeded"),
                          "failed": resp.get("failed"),
                          "agent_attribution": resp.get("agent_attribution"),
                          "agent_attribution_failures": resp.get("agent_attribution_failures"),
                          "results": resp.get("results")})
        return _text(resp)
    except Exception as e:  # noqa: BLE001
        return {"isError": True, "content": [{"type": "text", "text": f"error: {type(e).__name__}: {e}"}]}


def main() -> None:
    global PY, PY_HAS_JIEBA
    PY, PY_HAS_JIEBA = pick_python()
    _log(f"ready: python={PY} jieba={'yes' if PY_HAS_JIEBA else 'NO'} bridge={BRIDGE}")
    if not os.environ.get("MEMORYKIT_SKIP_PREFLIGHT"):
        check_embed_service()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dsh-memorykit", "version": "0.2.0"}}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = msg.get("params", {})
            result = handle_tool(params.get("name", ""), params.get("arguments", {}) or {})
            resp = {"jsonrpc": "2.0", "id": mid, "result": result}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": mid, "result": {}}
        else:
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"unknown method {method}"}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
