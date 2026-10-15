#!/usr/bin/env python3
"""memorykit-overlay bridge — 把 memory-kit 全部能力暴露为 JSON 接口，供 OpenClaw 插件调用。

用法: python3 bridge.py <command> <json-args>
  例:  python3 bridge.py learn '{"content": "显卡预算550块", "source": "task"}'
       python3 bridge.py learn_batch '{"items": [{"content": "..."}, {"content": "..."}]}'
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


def _agent_env():
    """调用方身份: 环境变量 MEMORYKIT_AGENT_ID 决定记忆归属 (2026-10-15)。
    dsh 的 MCP 接线在 ~/.dsh/cordis.patch.yml 里传 MEMORYKIT_AGENT_ID=dsh,
    不传则保持默认 hermes (Hermes/openclaw 直调 bridge 的旧路径不受影响)。"""
    return os.environ.get("MEMORYKIT_AGENT_ID", "hermes")


def cmd_learn(a):
    db = get_db()
    content = a.get("content")
    if not content:
        return {"ok": False, "error": "content required"}
    agent = _agent_env()
    r = db.learn(
        content,
        source=a.get("source", "task" if agent == "hermes" else agent),
        source_session=a.get("source_session"),
        event_time=a.get("event_time"),
    )
    # learn() 内部 self.set() 写死 agent_id='hermes' (lifecycle.py L926),
    # 这里按调用方身份回写, 保证 dsh 写的条目不被记成 hermes。
    #
    # 2026-10-15 修补 (REVIEW.md P1-D): 原来是 `except Exception: pass` ——
    # 回写失败会把 dsh 的条目静默留在 hermes 名下, 从外部完全看不出来。
    # 现在改为「回写 + 回读校验 + 失败显式上报」, 调用方拿到 agent_attribution。
    attribution = "not-needed"        # hermes 调用方本来就是这个归属
    if agent != "hermes" and isinstance(r, dict) and r.get("key"):
        attribution = _attribute(db, r["key"], agent)
        if attribution != "ok":
            sys.stderr.write(
                f"[bridge] WARN agent_id 回写失败 key={r['key']} agent={agent} -> {attribution}\n")
    elif agent != "hermes":
        attribution = "skipped: learn() 未返回 key"
    return _ok(result=r, agent_attribution=attribution)


def _attribute(db, key: str, agent: str) -> str:
    """把 key 的 agent_id 改成 agent, 并回读校验。

    返回 "ok" 或失败原因。根因在 memorykit: learn() 没有 agent_id 形参、
    内部 set() 写死 hermes (lifecycle.py L926) —— 正解是给 learn() 加参数,
    这里只是不静默的权宜之计 (2026-10-15 修补, REVIEW.md P1-D)。
    """
    try:
        with db._tx():
            db._conn.execute("UPDATE memories SET agent_id=? WHERE key=?", (agent, key))
        row = db._conn.execute("SELECT agent_id FROM memories WHERE key=?", (key,)).fetchone()
        got = row[0] if row else None
        return "ok" if got == agent else f"verify-mismatch: want {agent}, got {got}"
    except Exception as e:  # noqa: BLE001 — 归属失败必须可见, 但不能打断写入
        return f"failed: {type(e).__name__}: {e}"


LEARN_BATCH_MAX_ITEMS = 200


def cmd_learn_batch(a):
    """批量灌记忆: 一次批量嵌入 + 逐条复用 learn() 去重/合并。

    items 每项 {content, source?, source_session?, event_time?}。
    content < 10 字符的条目由 memory-kit 记为 {"error": "content too short"}，
    结果与 items 顺序一一对应，不因单条失败中断整批。
    """
    db = get_db()
    items = a.get("items")
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "items must be a non-empty list"}
    if len(items) > LEARN_BATCH_MAX_ITEMS:
        return {"ok": False,
                "error": f"too many items: {len(items)} > {LEARN_BATCH_MAX_ITEMS}"}
    agent = _agent_env()
    default_source = a.get("source") or ("task" if agent == "hermes" else agent)
    norm = []
    for it in items:
        if not isinstance(it, dict):
            norm.append(it)  # 交给 learn_batch 记 invalid item，保持顺序对齐
            continue
        it = dict(it)
        if not it.get("source"):
            it["source"] = default_source
        norm.append(it)
    results = db.learn_batch(norm)
    # 与 cmd_learn 同理: learn_batch 逐条复用 learn(), 其内部 set() 写死
    # agent_id='hermes' (lifecycle.py L926) → 按调用方身份统一回写。
    #
    # 2026-10-15 修补 (REVIEW.md P1-D): 原来用 executemany 一把梭, 失败只记 fixed=0
    # 且不留痕; 现在逐条走 _attribute(回写 + 回读校验), 失败的 key 带原因上报。
    # agent_id / agent_id_fixed 两个字段名保持不变 —— func_test.py 第 8b 节依赖它们。
    fixed = 0
    attribution_failures = []
    if agent != "hermes" and isinstance(results, list):
        for r in results:
            if not (isinstance(r, dict) and r.get("key")):
                continue
            got = _attribute(db, r["key"], agent)
            if got == "ok":
                fixed += 1
            else:
                attribution_failures.append({"key": r["key"], "reason": got})
        if attribution_failures:
            sys.stderr.write(
                f"[bridge] WARN learn_batch agent_id 回写失败 "
                f"{len(attribution_failures)}/{fixed + len(attribution_failures)} 条\n")
    ok_n = sum(1 for r in (results or [])
               if isinstance(r, dict) and not r.get("error"))
    return _ok(results=results, total=len(results or []),
               succeeded=ok_n, failed=len(results or []) - ok_n,
               agent_id=agent, agent_id_fixed=fixed,
               agent_attribution=("not-needed" if agent == "hermes"
                                  else ("ok" if not attribution_failures else "partial")),
               agent_attribution_failures=attribution_failures)


def cmd_set(a):
    db = get_db()
    key = a.get("key")
    content = a.get("content")
    if not key or content is None:
        return {"ok": False, "error": "key and content required"}
    agent = _agent_env()
    r = db.set(
        key,
        content,
        category=a.get("category", "general"),
        scope=a.get("scope", "permanent"),
        importance=a.get("importance", 0.5),
        tags=a.get("tags", ""),
        ttl=a.get("ttl"),
        agent_id=agent,
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
    "learn_batch": cmd_learn_batch,
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
