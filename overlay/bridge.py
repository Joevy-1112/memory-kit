#!/usr/bin/env python3
"""memorykit-overlay bridge — 把 memory-kit 全部能力暴露为 JSON 接口，供 OpenClaw 插件调用。

用法: python3 bridge.py <command> <json-args>
  例:  python3 bridge.py learn '{"content": "显卡预算550块", "source": "task"}'
       python3 bridge.py search '{"query": "显卡"}'
       python3 bridge.py correct '{"key": "user/显卡预算", "signal": "不对"}'

所有命令输出单行 JSON: {"ok": true, ...} 或 {"ok": false, "error": "..."}
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/joe/memory-kit")

from memorykit import MemoryDB  # noqa: E402

# 默认库路径（可被环境变量覆盖，测试时指向临时库）
DB_PATH = os.environ.get("MEMORYKIT_DB", "/home/joe/.openclaw/state/memorykit-overlay.db")
STATE_DB = os.environ.get("MEMORYKIT_STATE_DB", "/home/joe/.hermes/state.db")

_db = None


def get_db():
    global _db
    if _db is None:
        _db = MemoryDB(db_path=DB_PATH, state_db_path=STATE_DB if os.path.exists(STATE_DB) else None)
    return _db


def _ok(**kw):
    kw["ok"] = True
    return kw


# 检索命中裁剪：去掉向量等肥字段，只留对 LLM 有用的
_MEM_FIELDS = (
    "id", "key", "content", "category", "tags", "scope", "importance",
    "created_at", "updated_at", "event_time", "confidence", "verification_status",
    "source_session", "layer", "conflict", "decay_profile", "stale_since",
    "time_bias",  # v2 时间检索软重排得分（仅启用时间参数时携带）
    "sparse_score",  # v2.1 hybrid lexical 融合得分（仅 lexical 激活时携带）
    "rrf_score",  # 最终排序分 = 融合分 × verify_weight × time_weight × time_bias（自动注入阈值用）
    "rrf_raw",  # 融合原始分（未乘验证/时间权重）
)


def _trim(hit: dict) -> dict:
    if isinstance(hit, dict):
        return {k: hit.get(k) for k in _MEM_FIELDS if k in hit}
    return hit


def _trim_hits(hits) -> list:
    return [_trim(h) for h in (hits or [])]


def cmd_learn(a):
    db = get_db()
    content = a.get("content")
    if not content:
        return {"ok": False, "error": "content required"}
    r = db.learn(
        content,
        source=a.get("source", "task"),
        source_session=a.get("source_session"),
        event_time=a.get("event_time"),
    )
    return _ok(result=r)


def cmd_set(a):
    db = get_db()
    key = a.get("key")
    content = a.get("content")
    if not key or content is None:
        return {"ok": False, "error": "key and content required"}
    r = db.set(
        key,
        content,
        category=a.get("category", "general"),
        scope=a.get("scope", "permanent"),
        importance=a.get("importance", 0.5),
        tags=a.get("tags", ""),
        ttl=a.get("ttl"),
    )
    return _ok(result=r)


def cmd_get(a):
    db = get_db()
    key = a.get("key")
    if not key:
        return {"ok": False, "error": "key required"}
    r = db.get(key)
    if r is None:
        return {"ok": False, "error": f"not found: {key}"}
    # 默认裁剪重字段（vector/sparse/vector_bin 等），LLM 不需要原始向量——
    # 全量 dump 一条 20KB+ 纯烧 token（小龙虾实测反馈 2026-08-07）。
    # 需要向量时显式传 include_vector=true。
    if not a.get("include_vector"):
        for k in ("vector", "vector_bin", "sparse"):
            r.pop(k, None)
    return _ok(entry=r)


def _norm_time_window(tw):
    """time_window 兼容 list[(start, end)] 与 dict{start:…, end:…} → 二元组列表。

    memorykit 核心接受序列 (start, end)；dict 形态由 bridge 归一化后再透传。
    """
    if tw is None:
        return None
    if isinstance(tw, dict):
        return [tw.get("start"), tw.get("end")]
    return tw


def cmd_search(a):
    db = get_db()
    query = a.get("query", "")
    limit = a.get("limit", 10)
    mode = a.get("mode", "multi")  # multi | semantic | two_stage
    if mode == "semantic":
        hits = db.search_semantic(query, limit=limit, category=a.get("category"))
    elif mode == "two_stage":
        hits = db.search_two_stage(query, limit=limit)
    else:
        hits = db.search_multi(
            query, limit=limit, category=a.get("category"), scope=a.get("scope"),
            time_window=_norm_time_window(a.get("time_window")),
            ref_time=a.get("ref_time"),
            max_age_days=a.get("max_age_days"),
            strict_time_window=bool(a.get("strict_time_window", False)),
        )
    return _ok(hits=_trim_hits(hits), count=len(hits))


def cmd_search_state(a):
    """深究：从 memory 回溯到 state.db 原文（memory→state 方向）。"""
    db = get_db()
    query = a.get("query", "")
    limit = a.get("limit", 5)
    session_id = a.get("session_id", "")
    r = db.search_state_fast(query, limit=limit, session_id=session_id)
    return _ok(hits=_trim_hits(r), count=len(r))


def cmd_correct(a):
    """用户纠正：confidence -0.3 + disputed。correction 是唯一外部锚点。"""
    db = get_db()
    key = a.get("key")
    if not key:
        return {"ok": False, "error": "key required"}
    r = db.correction(key, session_id=a.get("session_id", ""), signal=a.get("signal", ""))
    return _ok(**r)


def cmd_reinforce(a):
    """正向强化：explicit +0.15 / implicit +0.05，disputed → verified。"""
    db = get_db()
    key = a.get("key")
    if not key:
        return {"ok": False, "error": "key required"}
    r = db.reinforce(key, session_id=a.get("session_id", ""), mode=a.get("mode", "implicit"))
    return _ok(**r)


def cmd_verify(a):
    db = get_db()
    key = a.get("key")
    if not key:
        return {"ok": False, "error": "key required"}
    r = db.verify(key, auto_degrade=a.get("auto_degrade", True),
                  degrade_threshold=a.get("threshold", 0.3))
    return _ok(result=r)


def cmd_verify_stats(a):
    db = get_db()
    return _ok(stats=db.verify_stats())


def cmd_verify_batch(a):
    """v1.2.0 批量验证：对整库 pending 记忆批量跑 verify。

    返回统计含 freshness 处理量（memorykit 1.2.0 起命名修正）：
      fresh_checked   = 有 fresh 配置（provenance.fresh）的行数
      fresh_resolved  = 状态已定（expired/stale/current 且带活验证时间戳）的行数
    """
    db = get_db()
    stats = db.verify_batch(
        limit=a.get("limit", 100),
        auto_degrade=a.get("auto_degrade", True),
        degrade_threshold=a.get("threshold", 0.3),
        only_pending=bool(a.get("only_pending", True)),
    )
    return _ok(stats=stats)


def cmd_overlay(a):
    """memory-overlay 核心：遮蔽管理。
    action=resolve 时返回 memory 条目 + 对应 state 原文（供上下文展示密集片段+指针）。
    """
    db = get_db()
    action = a.get("action", "resolve")
    if action == "resolve":
        query = a.get("query", "")
        limit = a.get("limit", 5)
        mems = db.search_multi(query, limit=limit)
        results = []
        for m in mems:
            key = m.get("key") or m.get("id")
            entry = db.get(key) if key else None
            results.append({
                "memory": _trim(entry) if entry else None,
                "confidence": entry.get("confidence") if entry else None,
                "status": entry.get("verification_status") if entry else None,
            })
        return _ok(overlay=results, count=len(results))
    elif action == "expand":
        # 展开：给定 memory key，回溯到 source_session 原文
        key = a.get("key")
        if not key:
            return {"ok": False, "error": "key required for expand"}
        entry = db.get(key)
        if not entry:
            return {"ok": False, "error": f"not found: {key}"}
        src = entry.get("source_session") or ""
        hits = db.search_state_fast(entry.get("content", "")[:50], limit=3, session_id=src)
        return _ok(memory=_trim(entry), state_original=_trim_hits(hits))
    else:
        return {"ok": False, "error": f"unknown action: {action}"}


def cmd_episodic(a):
    db = get_db()
    action = a.get("action", "list")
    if action == "start":
        sid = a.get("session_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        r = db.episodic_start(sid, source=a.get("source", "openclaw"))
        return _ok(result=r)
    elif action == "end":
        sid = a.get("session_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        r = db.episodic_end(sid, summary=a.get("summary", ""))
        return _ok(result=r)
    elif action == "checkin":
        # 遮蔽窗心跳: 开窗/续窗/超 30min gap 旋转, 会话粒度
        sid = a.get("session_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        r = db.episodic_checkin(sid, source=a.get("source", "openclaw"))
        return _ok(result=r)
    elif action == "close":
        sid = a.get("session_id")
        if not sid:
            return {"ok": False, "error": "session_id required"}
        r = db.episodic_close(sid, summary=a.get("summary", ""))
        return _ok(result=r)
    elif action == "window":
        return _ok(episodes=db.episodic_window(
            start=a.get("start"), end=a.get("end"), limit=a.get("limit", 50)))
    elif action == "list":
        return _ok(episodes=db.episodic_list(limit=a.get("limit", 10)))
    elif action == "search":
        return _ok(episodes=db.episodic_search(a.get("query", ""), limit=a.get("limit", 5)))
    return {"ok": False, "error": f"unknown episodic action: {action}"}


def cmd_knowledge(a):
    db = get_db()
    action = a.get("action", "search")
    if action == "promote":
        r = db.knowledge_promote(
            a.get("memory_key", ""), a.get("title", ""),
            domain=a.get("domain", ""), tags=a.get("tags", ""),
            confidence=a.get("confidence", 0.5),
        )
        return _ok(result=r)
    elif action == "search":
        return _ok(nodes=db.knowledge_search(a.get("query", ""), limit=a.get("limit", 10)))
    elif action == "list":
        return _ok(nodes=db.knowledge_list(domain=a.get("domain", ""), limit=a.get("limit", 50)))
    elif action == "graph":
        return _ok(graph=db.knowledge_graph(a.get("kid", 0), max_hops=a.get("max_hops", 2)))
    return {"ok": False, "error": f"unknown knowledge action: {action}"}


def cmd_stats(a):
    db = get_db()
    return _ok(stats=db.stats())


def cmd_cleanup(a):
    db = get_db()
    return _ok(removed=db.cleanup())


def cmd_age(a):
    db = get_db()
    r = db.age()
    return _ok(result=r)


COMMANDS = {
    "learn": cmd_learn,
    "set": cmd_set,
    "get": cmd_get,
    "search": cmd_search,
    "search_state": cmd_search_state,
    "correct": cmd_correct,
    "reinforce": cmd_reinforce,
    "verify": cmd_verify,
    "verify_stats": cmd_verify_stats,
    "verify_batch": cmd_verify_batch,
    "overlay": cmd_overlay,
    "episodic": cmd_episodic,
    "knowledge": cmd_knowledge,
    "stats": cmd_stats,
    "cleanup": cmd_cleanup,
    "age": cmd_age,
}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: bridge.py <cmd> <json-args>"}))
        return 1
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(json.dumps({"ok": False, "error": f"unknown command: {cmd}"}))
        return 1
    try:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad json args: {e}"}))
        return 1
    try:
        out = COMMANDS[cmd](args)
    except Exception as e:  # noqa: BLE001 — bridge 边界，全部转 JSON 错误
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
