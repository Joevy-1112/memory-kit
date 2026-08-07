#!/usr/bin/env python3
"""记忆搜索模块 — 多信号检索 + 两段级联 + 实体图。MemorySearch mixin class。"""
import json, math, re
import numpy as np


class MemorySearch:
    """Mixin: search/entity methods for MemoryDB."""

    # ── 检索融合权重（历史命名遗留）──────────────────────────────
    # 实际算法是 semantic-primary 加权融合（多信号打分后加权合并），
    # 并非标准 RRF。RRF_K/_rrf_score 为历史命名，字段名 rrf_score 保留
    # 以兼容旧数据/旧调用方；新文档一律称为"加权融合"。
    RRF_K = {
        "fts5":    40,
        "entity":  50,
        "semantic": 60,
        "tag":     70,
        "recency": 80,
    }

    # ── hybrid sparse lexical 融合权重（memorykit hybrid，2026-08-07）──
    # query 与候选的 bge-m3 sparse_weights 做 token 重叠得分
    # （sum(min(wq, ws) for token in 交集)，BM25 简化），max 归一化到 [0,1]
    # 后按此权重加到融合分。权重可配：调大 → 词法精确匹配更优先（适合
    # 术语/代码/专名查询），调小 → 更依赖语义。默认 0.3。
    # query sparse 不可用（GGUF 8082 / 注入 embedder 无 sparse 支持）→ 跳过
    # 整段，完全原逻辑；候选 sparse 为 NULL → 该条 lexical 分 0。
    LEXICAL_WEIGHT = 0.3

    # ── 环3 验证回流（design-docs/memory-v2-plan.md §环3）──────────────
    # verification_status 参与检索排序：最终得分 = 融合得分 × 系数。
    # 系数 = 1 + delta，delta 按设计定稿（评审修正 #6）：
    #   verified +0.15 / unverified +0.0 / STALE -0.1 / disputed -0.4~-0.5
    # （disputed 可能错了，比 STALE 只是久未用罚更重，取 -0.5 重罚端）
    # 补充状态兜底：pending 等同 unverified；unverifiable 是"无源可查"
    # 而非"错了"，不罚；degraded 是验证降级，轻度罚；deleted 软删沉底。
    # 未知状态一律按 unverified(1.0) 处理，保证新状态不会意外塌掉排序。
    VERIFY_WEIGHTS = {
        "verified":     1.15,
        "unverified":   1.00,
        "pending":      1.00,
        "unverifiable": 1.00,
        "STALE":        0.90,
        "degraded":     0.85,
        "ARCHIVED":     0.10,   # 环2 归档：检索沉底（importance 已垫底 0.05，双保险）
        "disputed":     0.50,
        "deleted":      0.10,
    }

    def _verify_weight(self, status):
        """verification_status → 检索加权系数（未知状态按 unverified=1.0 兜底）。"""
        return self.VERIFY_WEIGHTS.get(status, 1.00)

    # 时间感知排序（v2 优化点 #4，2026-08-03 同步自服务器实现）
    # 公式: time_weight = clamp(1 / (1 + age_days / half_life_days), min_weight, 1.0)
    # half_life=30: 30 天内权重衰减到一半；min_weight=0.3: 最老记忆保留 30% 权重，
    # 语义强相关的老记忆仍能浮上来。与 VERIFY_WEIGHTS 乘性叠加。
    # created_at 为 NULL / 解析失败 → 按最老处理（min_weight）。
    TIME_DECAY = {
        "half_life_days": 30.0,
        "min_weight": 0.30,
    }

    def _time_weight(self, created_at, now_jd=None):
        """created_at('YYYY-MM-DD HH:MM:SS') → 时间衰减权重，clamp 到 [min_weight, 1.0]。

        age 用 julianday('now') - julianday(created_at)（与全文 recency 信号口径一致）。
        created_at 为 NULL / 解析失败 → 按最老处理，返回 min_weight（0.3）。
        """
        cfg = self.TIME_DECAY
        hl = float(cfg.get("half_life_days", 30.0))
        lo = float(cfg.get("min_weight", 0.30))
        if hl <= 0:
            return 1.0  # 半衰期<=0 视为关闭时间衰减
        if not created_at:
            return lo
        try:
            jd = self._conn.execute("SELECT julianday(?)", (str(created_at),)).fetchone()[0]
        except Exception:
            return lo
        if jd is None:
            return lo
        if now_jd is None:
            now_jd = self._conn.execute("SELECT julianday('now')").fetchone()[0]
        age_days = max(0.0, now_jd - jd)
        w = 1.0 / (1.0 + age_days / hl)
        return max(lo, min(1.0, w))

    def _rrf_score(self, rank, signal):
        k = self.RRF_K.get(signal, 60)
        return 1.0 / (k + rank)

    # ── v2 时间偏置（memorykit v2: event_time 检索元数据）──────────────────
    # event_time（事件发生时间）仅参与检索时间偏置，不参与 TTL/遗忘状态机/
    # 写入去重年龄判断。temporal 值 = COALESCE(event_time, created_at)。
    # 窗口内条目 boost（1.25），窗口外降权（0.25）——软约束宽召回，不剔除；
    # strict_time_window=True 时改用 SQL WHERE 硬过滤（_strict_window_sql）。

    TIME_WINDOW_BIAS_IN = 1.25     # 落在时间窗口内
    TIME_WINDOW_BIAS_OUT = 0.25    # 落在时间窗口外（宽召回：仍返回，仅沉底）
    TIME_WINDOW_BIAS_UNKNOWN = 0.5 # temporal 值缺失/解析失败（无法确认归属）

    def _temporal_value(self, row):
        """temporal 值 = row.event_time or row.created_at（事件发生时间优先）。

        兼容 dict 与 sqlite3.Row；event_time 为 NULL 时回退 created_at，
        因此旧数据/默认调用（不传 event_time）行为与 v1 完全一致。
        """
        try:
            ev = row.get("event_time") if hasattr(row, "get") else row["event_time"]
        except Exception:
            ev = None
        if ev:
            return ev
        try:
            ca = row.get("created_at") if hasattr(row, "get") else row["created_at"]
        except Exception:
            ca = None
        return ca

    def _temporal_jd(self, row):
        """temporal 值 → julian day（SQL julianday 口径，与 recency 信号一致）。"""
        tv = self._temporal_value(row)
        if not tv:
            return None
        try:
            return self._conn.execute("SELECT julianday(?)", (str(tv),)).fetchone()[0]
        except Exception:
            return None

    def _resolve_time_params(self, time_window=None, ref_time=None,
                             max_age_days=None) -> dict:
        """归一化 v2 时间参数 → 内部表示（julian day）。

        返回 {"active": bool, "start": jd|None, "end": jd|None,
              "ref": jd|None, "max_age_days": float|None}
        time_window 接受 (start, end)，端点可为 None（开区间）；
        端点接受 'YYYY-MM-DD[ HH:MM:SS]' 字符串或 datetime。
        ref_time + max_age_days 语义：年龄窗口 [ref_time - max_age_days, ref_time]
        （temporal 值落在此闭区间内才算窗口内；晚于 ref_time 的时点视为超窗）。
        """
        start = end = None
        if time_window is not None:
            try:
                start, end = time_window
            except (TypeError, ValueError):
                start, end = None, None
        active = (start is not None or end is not None
                  or ref_time is not None or max_age_days is not None)
        tp = {"active": bool(active), "start": None, "end": None,
              "ref": None, "max_age_days": None}

        def _to_jd(v):
            if v is None:
                return None
            if hasattr(v, "strftime"):  # datetime / date
                v = v.strftime("%Y-%m-%d %H:%M:%S")
            try:
                return self._conn.execute(
                    "SELECT julianday(?)", (str(v),)).fetchone()[0]
            except Exception:
                return None

        tp["start"] = _to_jd(start)
        tp["end"] = _to_jd(end)
        tp["ref"] = _to_jd(ref_time)
        if max_age_days is not None:
            try:
                tp["max_age_days"] = float(max_age_days)
            except (TypeError, ValueError):
                tp["max_age_days"] = None
        # ref_time 与 max_age_days 必须成对才生效（只给一个视为未启用对应约束）
        if tp["ref"] is None or tp["max_age_days"] is None:
            tp["ref"] = None
            tp["max_age_days"] = None
        return tp

    def _in_time_window(self, temporal_jd, tp: dict) -> bool:
        """temporal 值是否落在时间约束内（strict 硬过滤用；soft 用 _window_bias）。"""
        if not tp or not tp.get("active"):
            return True
        if temporal_jd is None:
            return False  # 无法确认归属 → 硬过滤时剔除
        if tp.get("start") is not None and temporal_jd < tp["start"]:
            return False
        if tp.get("end") is not None and temporal_jd > tp["end"]:
            return False
        if tp.get("ref") is not None and tp.get("max_age_days") is not None:
            # 年龄窗口闭区间 [ref - max_age_days, ref]
            if temporal_jd < tp["ref"] - tp["max_age_days"] or temporal_jd > tp["ref"]:
                return False
        return True

    def _window_bias(self, temporal_jd, tp: dict) -> float:
        """时间窗口软重排系数。无时间参数 → 恒 1.0（完全兼容旧行为）。"""
        if not tp or not tp.get("active"):
            return 1.0
        if temporal_jd is None:
            return self.TIME_WINDOW_BIAS_UNKNOWN
        if self._in_time_window(temporal_jd, tp):
            return self.TIME_WINDOW_BIAS_IN
        return self.TIME_WINDOW_BIAS_OUT

    def _strict_window_sql(self, tp: dict, params: list) -> list:
        """strict 硬过滤的 SQL 条件片段（追加到 params）。temporal = COALESCE(event_time, created_at)。

        tp 内的时间点已是 julian day（_resolve_time_params 归一化），
        与 julianday(...) 结果同口径，直接比较即可。
        """
        conds = []
        base = "julianday(COALESCE(m.event_time, m.created_at))"
        if tp.get("start") is not None:
            conds.append(f"{base} >= ?")
            params.append(tp["start"])
        if tp.get("end") is not None:
            conds.append(f"{base} <= ?")
            params.append(tp["end"])
        if tp.get("ref") is not None and tp.get("max_age_days") is not None:
            conds.append(f"{base} >= ? - ?")
            params.extend([tp["ref"], tp["max_age_days"]])
            conds.append(f"{base} <= ?")
            params.append(tp["ref"])
        return conds

    # NOTE: MemoryDB（db.py）覆盖了本类的 search_semantic / backfill_vectors，
    # 提供更快的 vector_bin 二进制批量余弦 + json→bin 自动迁移。这里保留
    # 基础实现作为 mixin 独立使用时的参考/兜底，两处行为一致（维度不兼容时跳过）。
    def search_semantic(self, query, limit=10, category=None, scope=None,
                        time_window=None, ref_time=None, max_age_days=None,
                        strict_time_window=False):
        """语义搜索: embed query, cosine similarity against stored vectors.

        v2 时间参数与 search_multi 同语义（temporal = event_time 优先，
        TTL 仍用 created_at）。MemoryDB（db.py）有二进制批量优化覆盖版，
        本实现为 mixin 独立使用时的参考/兜底。
        """
        qvec = self._embed(query)
        if not qvec:
            return []
        tp = self._resolve_time_params(time_window, ref_time, max_age_days)
        conditions = ["m.vector IS NOT NULL"]
        params = []
        if category: conditions.append("m.category = ?"); params.append(category)
        if scope: conditions.append("m.scope = ?"); params.append(scope)
        conditions.append("(m.ttl IS NULL OR (julianday('now')-julianday(m.created_at))*86400 < m.ttl)")
        if strict_time_window and tp["active"]:
            conditions.extend(self._strict_window_sql(tp, params))
        rows = self._conn.execute(
            f"SELECT *, julianday(COALESCE(event_time, created_at)) AS _temporal_jd "
            f"FROM memories m WHERE {' AND '.join(conditions)}", params
        ).fetchall()
        scored = []
        for r in rows:
            d = dict(r)
            vec = json.loads(d["vector"]) if d.get("vector") else None
            if vec:
                tjd = d.pop("_temporal_jd", None)
                d["similarity"] = round(self._cosine_similarity(qvec, vec), 4)
                d["time_bias"] = round(self._window_bias(tjd, tp), 4)
                scored.append(d)
        # v2 软重排：bias 乘入排序键（无时间参数时 bias=1.0，行为不变）
        scored.sort(key=lambda x: x["similarity"] * x.get("time_bias", 1.0), reverse=True)
        return scored[:limit]

    def backfill_vectors(self, limit=500):
        """Compute embeddings for entries that lack them."""
        if not self._is_embedding_available():
            return -1
        rows = self._conn.execute(
            "SELECT id, key, content FROM memories WHERE vector IS NULL LIMIT ?",
            (limit,)
        ).fetchall()
        count = 0
        for r in rows:
            vec = self._embed(str(r["content"])[:5000])
            if vec:
                self._conn.execute("UPDATE memories SET vector=? WHERE id=?",
                                   (json.dumps(vec), r["id"]))
                count += 1
            if count % 10 == 0:
                self._conn.commit()
        self._conn.commit()
        return count

    def search_multi(self, query, limit=10, category=None, scope=None,
                     time_window=None, ref_time=None, max_age_days=None,
                     strict_time_window=False):
        """四信号并行检索 + semantic-primary 加权融合。

        v2 时间参数（memorykit v2, event_time 检索元数据）:
          time_window=(start, end)    软约束宽召回：temporal 值（event_time 优先，
                                      缺省 created_at）落在窗口内 → boost 1.25，
                                      窗口外 → 0.25 沉底但不剔除。
          ref_time + max_age_days     基准时间 + 年龄窗口（闭区间
                                      [ref_time - max_age_days, ref_time]）。
          strict_time_window=True     窗口改为硬过滤（最终结果 + SQL 条件双重保证）；
                                      默认 False 仅软重排。
        只替换 recency rank 与 final `_time_weight` 的输入（temporal 值），
        TTL 条件仍用 created_at；不传时间参数时行为与 v1 完全一致。
        """
        tp = self._resolve_time_params(time_window, ref_time, max_age_days)
        if not query.strip():
            results = self.search(query, limit=limit*4 if tp["active"] else limit,
                                  category=category, scope=scope)
            # 空查询 + 时间参数：宽取候选后按时间偏置软重排 / strict 硬过滤
            if tp["active"]:
                _tmp = []
                for d in results:
                    d["time_bias"] = round(self._window_bias(self._temporal_jd(d), tp), 4)
                    if not strict_time_window or self._in_time_window(self._temporal_jd(d), tp):
                        _tmp.append(d)
                results = sorted(_tmp, key=lambda d: (d.get("importance", 0.0)
                                                      * d.get("time_bias", 1.0)),
                                 reverse=True)[:limit]
            return results

        # Signal 1: FTS5
        fts = self.search(query, limit=limit*2, category=category, scope=scope)

        # ── FTS5 zero-hit LIKE fallback (calibration) ──
        _is_code_query = any(
            c.isdigit() or c in "xXabcdef_-.@:" for c in query
        ) and any(c.isalpha() or c == "0" for c in query)
        if len(fts) == 0 and _is_code_query:
            # 兜底 LIKE 路径同样要按 category/scope 过滤（审查修复 #2）
            like_conds = ["content LIKE ? OR key LIKE ?"]
            like_params = [f"%{query}%", f"%{query}%"]
            if category:
                like_conds.append("category = ?"); like_params.append(category)
            if scope:
                like_conds.append("scope = ?"); like_params.append(scope)
            like_rows = self._conn.execute(
                "SELECT * FROM memories WHERE " + " AND ".join(like_conds) + " LIMIT ?",
                (*like_params, limit * 2)
            ).fetchall()
            fts = [dict(r) for r in like_rows]

        fts_rank = {r["key"]: i+1 for i, r in enumerate(fts)}

        # ── FTS5 match ratio soft penalty ──
        # 对无空格 CJK 查询（整串是一个 token），整 token 子串匹配几乎必败，
        # 导致 FTS5 命中记忆的 match_ratio 恒为保底 0.15、信号被压扁。
        # 修复: CJK 查询拆 2-gram 计算命中率。
        _q_tokens = [t.lower() for t in query.split() if len(t) > 0]
        _match_terms = []
        for t in _q_tokens:
            if re.search(r'[\u4e00-\u9fff]', t) and len(t) >= 3:
                _match_terms.extend(t[i:i+2] for i in range(len(t) - 1))
            else:
                _match_terms.append(t)
        _match_terms = list(dict.fromkeys(_match_terms))  # 去重保序
        _fts_match_ratio: dict[str, float] = {}
        if _match_terms:
            for r in fts:
                content_lower = (r.get("content", "") or "").lower()
                matched = sum(1 for t in _match_terms if t in content_lower)
                _fts_match_ratio[r["key"]] = max(0.15, matched / len(_match_terms))

        # Signal 2: Semantic (binary batch + numpy)
        # 一次请求同时取 dense 向量 + sparse 词法权重（hybrid；服务不支持
        # sparse → qsparse=None，跳过 lexical 完全原逻辑）
        qvec, qsparse = self._embed_with_sparse(query)
        sem_results = []
        if qvec:
            qvec_np = np.array(qvec, dtype=np.float32)
            # 维度安全（2026-08-04 修复，同 learn()/db.py search_semantic）：
            # 库里可能混有不同嵌入模型产生的向量（如 1024d/2048d），必须按
            # 当前查询向量维度 qdim 过滤，否则 np.array 批量拼接抛
            # ValueError: inhomogeneous shape。
            qdim = int(qvec_np.size)
            # category/scope 过滤与 FTS 信号对齐（审查修复 #2）
            sem_conds = ["vector IS NOT NULL"]
            sem_params = []
            if category:
                sem_conds.append("category = ?"); sem_params.append(category)
            if scope:
                sem_conds.append("scope = ?"); sem_params.append(scope)
            all_rows = self._conn.execute(
                "SELECT key, vector, vector_bin FROM memories WHERE "
                + " AND ".join(sem_conds), sem_params
            ).fetchall()
            
            # Fast path: binary blobs（按 qdim 过滤后再批量拼接）
            blobs = []
            keys = []
            for r in all_rows:
                if (r["vector_bin"] is not None
                        and self._vector_dim_from_blob(r["vector_bin"]) == qdim):
                    blobs.append(r["vector_bin"])
                    keys.append(r["key"])
            
            try:
                if blobs:
                    vecs_np = np.array([np.frombuffer(b, dtype=np.float32) for b in blobs])
                    dots = np.dot(vecs_np, qvec_np)
                    norms_v = np.linalg.norm(vecs_np, axis=1)
                    norm_q = np.linalg.norm(qvec_np)
                    if norm_q > 0:
                        scores = dots / np.maximum(norms_v * norm_q, 1e-10)
                    else:
                        scores = np.zeros(len(blobs))
                    for i, key in enumerate(keys):
                        sem_results.append({"_sem_score": float(scores[i]), "key": key})
            except Exception:
                sem_results = []  # 批量路径兜底：失败则走下方 JSON 慢路径

            if not sem_results:
                # Fallback: JSON parse（同样做维度过滤）
                for r in all_rows:
                    v = json.loads(r["vector"]) if r.get("vector") else None
                    if v and len(v) == qdim:
                        sem_results.append({"_sem_score": self._cosine_similarity(qvec, v), "key": r["key"]})
            
            sem_results.sort(key=lambda x: x["_sem_score"], reverse=True)
            sem_results = sem_results[:limit*2]
        sem_rank = {r["key"]: i+1 for i, r in enumerate(sem_results)}
        _intersection_keys = set(fts_rank) & set(sem_rank)

        # Signal 3: Tags
        tag_results = []
        q_lower, q_tokens = query.lower(), query.lower().split()
        # category/scope 过滤与 FTS 信号对齐（审查修复 #2）
        tag_conds = ["(ttl IS NULL OR (julianday('now')-julianday(created_at))*86400 < ttl)"]
        tag_params = []
        if category:
            tag_conds.append("category = ?"); tag_params.append(category)
        if scope:
            tag_conds.append("scope = ?"); tag_params.append(scope)
        all_rows = self._conn.execute(
            "SELECT * FROM memories WHERE " + " AND ".join(tag_conds), tag_params
        ).fetchall()
        for r in all_rows:
            d = dict(r); score = 0.0
            for t in q_tokens:
                if t in (d.get("tags","") or "").lower(): score += 0.3
            if q_lower == (d.get("category","") or "").lower(): score += 0.4
            for t in q_tokens:
                if t in (d.get("key","") or "").lower(): score += 0.2
            if score > 0:
                d["_tag_score"] = score; tag_results.append(d)
        tag_results.sort(key=lambda x: x["_tag_score"], reverse=True)
        tag_rank = {r["key"]: i+1 for i, r in enumerate(tag_results)}

        # Signal 4: Entity
        entity_results = []
        q_entities = self._extract_entities(query)
        if q_entities:
            ph = ",".join("?" for _ in q_entities)
            erows = self._conn.execute(
                f"SELECT DISTINCT memory_key, entity FROM entity_links WHERE entity IN ({ph})",
                q_entities
            ).fetchall()
            hits = {}
            for r in erows: hits.setdefault(r["memory_key"], []).append(r["entity"])
            for key, ents in sorted(hits.items(), key=lambda x: -len(x[1])):
                row = self._conn.execute("SELECT * FROM memories WHERE key=?", (key,)).fetchone()
                if row:
                    d = dict(row)
                    # category/scope 过滤与 FTS 信号对齐（审查修复 #2）
                    if category and d.get("category") != category:
                        continue
                    if scope and d.get("scope") != scope:
                        continue
                    d["_entity_score"] = len(ents)/max(len(q_entities),1)
                    d["_entity_matches"] = ents; entity_results.append(d)
        entity_rank = {r["key"]: i+1 for i, r in enumerate(entity_results)}

        # Signal 5: Recency（v2：输入换成 temporal 值 = event_time 优先，缺省 created_at）
        import math as _math
        now_jd = self._conn.execute("SELECT julianday('now')").fetchone()[0]
        # category/scope 过滤与 FTS 信号对齐（审查修复 #2）
        recency_conds = []
        recency_params = []
        if category:
            recency_conds.append("category = ?"); recency_params.append(category)
        if scope:
            recency_conds.append("scope = ?"); recency_params.append(scope)
        recency_where = (" WHERE " + " AND ".join(recency_conds)) if recency_conds else ""
        # 只替换 recency rank 的输入（event_time 优先），TTL 条件仍用 created_at
        recency_rows = self._conn.execute(
            "SELECT key, julianday(COALESCE(event_time, created_at)) as jd FROM memories"
            + recency_where,
            recency_params
        ).fetchall()
        days_rank = [(r["key"], max(0, now_jd - r["jd"])) for r in recency_rows]
        days_rank.sort(key=lambda x: x[1])
        recency_rank = {key: i+1 for i, (key, _) in enumerate(days_rank)}

        # Signal 6: Sparse lexical（hybrid —— bge-m3 sparse token 权重重叠）
        # query sparse 不可用（GGUF 8082 / 注入 embedder 无 sparse 支持 /
        # 空 dict）→ 跳过整段，融合逻辑与旧版完全一致（零行为变化）。
        # 候选 sparse 为 NULL / 畸形 JSON → 该条 lexical 分 0（不剔除）。
        # 归一化：raw 除以候选池最大 raw（max-normalization，尺度无关）。
        _lex_norm: dict[str, float] = {}
        if qsparse:
            _lex_conds = ["sparse IS NOT NULL", "sparse != ''"]
            _lex_params = []
            # category/scope 过滤与其它信号对齐（审查修复 #2 口径）
            if category:
                _lex_conds.append("category = ?"); _lex_params.append(category)
            if scope:
                _lex_conds.append("scope = ?"); _lex_params.append(scope)
            _lex_rows = self._conn.execute(
                "SELECT key, sparse FROM memories WHERE "
                + " AND ".join(_lex_conds), _lex_params
            ).fetchall()
            _lex_raw: dict[str, float] = {}
            for _lr in _lex_rows:
                try:
                    _sd = json.loads(_lr["sparse"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(_sd, dict) or not _sd:
                    continue
                # BM25 简化重叠分：sum(min(wq, ws) for token in 交集)
                _s = 0.0
                for _tok, _wq in qsparse.items():
                    _ws = _sd.get(_tok)
                    if _ws:
                        _s += _wq if _wq < _ws else _ws
                if _s > 0:
                    _lex_raw[_lr["key"]] = _s
            _lex_max = max(_lex_raw.values()) if _lex_raw else 0.0
            if _lex_max > 0:
                _lex_norm = {_k: _v / _lex_max for _k, _v in _lex_raw.items()}

        # ── Semantic-primary fusion ──
        _all_content_keys = set(fts_rank) | set(sem_rank) | set(tag_rank) | set(entity_rank)
        all_keys = _all_content_keys | set(recency_rank)
        _fused_scores: dict[str, float] = {}
        _sem_scores: dict[str, float] = {}
        for r in sem_results:
            _sem_scores[r["key"]] = r.get("_sem_score", 0.0)

        for key in all_keys:
            score = _sem_scores.get(key, 0.0)
            if key in _fts_match_ratio:
                raw_ratio = _fts_match_ratio[key]
                if len(_match_terms) == 1:
                    # 真正单词查询（非 CJK 整串）：FTS5 命中给保底 0.15，防噪音
                    score = 0.55 * score + 0.15
                elif score > 0:
                    # 有语义分：按 FTS5 命中率放大
                    score *= 1.0 + 0.3 * raw_ratio
                else:
                    # 无向量（语义分=0）但 FTS5 强命中：给基础分，别被压成 0
                    score = raw_ratio
            if key in tag_rank:
                score += 0.015
            if key in entity_rank:
                score += 0.025
            if key in _lex_norm:
                # hybrid lexical：归一化重叠分 × 可配权重（默认 0.3）加性融合
                score += self.LEXICAL_WEIGHT * _lex_norm[key]
            if key in _intersection_keys and len(_match_terms) > 1:
                score *= 1.25
            if key in recency_rank:
                score += 0.001 * (1.0 / max(recency_rank[key], 1))
            _fused_scores[key] = score

        fused = []
        for key in all_keys:
            # v2: 顺带算 temporal julian day（event_time 优先），供时间偏置/硬过滤复用
            row = self._conn.execute(
                "SELECT *, julianday(COALESCE(event_time, created_at)) AS _temporal_jd "
                "FROM memories WHERE key=?", (key,)
            ).fetchone()
            if row:
                d = dict(row)
                _tjd = d.pop("_temporal_jd", None)
                # 兜底（审查修复 #2）：最终结果再按 category/scope 过滤一次，
                # 保证 'category=protocol 只返回 protocol' 不变量，即使某个信号漏过滤也不泄漏。
                if category and d.get("category") != category:
                    continue
                if scope and d.get("scope") != scope:
                    continue
                # v2 strict 时间窗口：最终结果硬过滤（与 category/scope 同级兜底）
                if strict_time_window and not self._in_time_window(_tjd, tp):
                    continue
                _base = _fused_scores.get(key, 0.0)
                # 环3 验证回流 + 环4 时间感知：最终得分 = 融合分 × verification × time
                # （verified 前移 / disputed、STALE 沉底；近期记忆优先，老记忆保底 30%）
                # v2: `_time_weight` 输入换成 temporal 值（event_time 优先，缺省 created_at）
                d["rrf_raw"] = round(_base, 5)
                # hybrid：lexical 激活时透明暴露 sparse_score（归一化重叠分，
                # 无重叠/候选 sparse NULL → 0.0）；未激活（GGUF 等）不出现该字段
                if _lex_norm:
                    d["sparse_score"] = round(_lex_norm.get(key, 0.0), 4)
                _tw = self._time_weight(self._temporal_value(d))
                d["time_weight"] = round(_tw, 4)
                # v2 时间窗口软重排：bias 乘入最终得分（无时间参数时恒 1.0）
                _wb = self._window_bias(_tjd, tp)
                d["time_bias"] = round(_wb, 4)
                d["rrf_score"] = round(_base * self._verify_weight(d.get("verification_status"))
                                       * _tw * _wb, 5)
                fused.append(d)
        fused.sort(key=lambda x: x["rrf_score"], reverse=True)
        result = fused[:limit]
        # 2026-08-02 修复：检索是读操作，不在此处写库。
        # search_multi 的 FTS 分支（self.search L95）已把 FTS 命中计入缓冲。
        # 语义命中的记忆（不在 FTS 结果里）也补记——它们被检索到=被用过，
        # 否则 accessed_at 不刷新，环2 会误衰减。archived/deleted 仍跳过。
        buf = getattr(self, "_touch_pending", None)
        if buf is None:
            buf = self._touch_pending = {}
        for d in result:
            st = d.get("verification_status")
            if st in ("ARCHIVED", "deleted"):
                continue
            buf.setdefault(d["key"], 0)
            buf[d["key"]] += 1
        if len(buf) >= 50:
            self.flush_touches()
        return result

    def flush_touches(self) -> int:
        """批量写回访问计数（读操作延迟落库，避免检索抢写锁）。"""
        buf = getattr(self, "_touch_pending", None)
        if not buf:
            return 0
        try:
            now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            n = 0
            for key, cnt in buf.items():
                self._conn.execute(
                    "UPDATE memories SET access_count = COALESCE(access_count,0) + ? , accessed_at = ? WHERE key = ?",
                    (cnt, now, key)
                )
                n += 1
            self._conn.commit()
        finally:
            buf.clear()
        return n

    # ── Entity graph ──

    def entity_graph(self, entity, depth=1):
        rows = self._conn.execute(
            "SELECT e.entity, e.memory_key, m.content, m.category FROM entity_links e "
            "JOIN memories m ON m.key = e.memory_key WHERE e.entity LIKE ? "
            "ORDER BY e.weight DESC LIMIT 20", (f"%{entity}%",)
        ).fetchall()
        return [dict(r) for r in rows]

    def entity_stats(self):
        total = self._conn.execute("SELECT COUNT(DISTINCT entity) FROM entity_links").fetchone()[0]
        top = self._conn.execute(
            "SELECT entity, COUNT(*) as cnt FROM entity_links "
            "GROUP BY entity ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        return {"total_entities": total, "top_entities": [dict(r) for r in top]}

    def backfill_entities(self):
        rows = self._conn.execute(
            "SELECT id, key, content FROM memories WHERE entities IS NULL LIMIT 500"
        ).fetchall()
        count = 0
        for r in rows:
            extracted = self._extract_entities(str(r["content"]))
            if extracted:
                self._conn.execute("UPDATE memories SET entities=? WHERE id=?",
                                   (json.dumps(extracted), r["id"]))
                self._update_entity_links(r["id"], r["key"], extracted)
                count += 1
        self._conn.commit()
        return count

    # ── Context injection ──

    def inject(self, query="", limit=8):
        if query.strip():
            results = self.search_multi(query, limit=limit)
        else:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE scope='permanent' ORDER BY importance DESC LIMIT ?",
                (limit,)
            ).fetchall()
            results = [dict(r) for r in rows]
        if not results:
            return ""
        lines = ["📖 相关记忆:"]
        for r in results:
            cat = r.get("category","?"); imp = r.get("importance",0)
            profile = r.get("decay_profile","?")
            rrf = r.get("rrf_score","")
            score = f" rrf={rrf}" if rrf else ""
            lines.append(f"  [{r['key']}] ({cat}, imp={imp}, {profile}{score})")
            lines.append(f"    {r.get('content','')[:150].replace(chr(10),' ')}")
        return "\n".join(lines)

    # ================================================================
    #  V2 Two-Stage Cascade
    #  Stage 1: bge-m3 semantic search on memory.db → topic summaries
    #  Stage 2: source_message_ids → direct ID lookup on state.db
    #  FTS5 used as reranker within the candidate set (not as primary)
    #  Fallback: standalone FTS5 when summaries have no source IDs
    # ================================================================

    def _parse_message_ids(self, raw):
        """Parse source_message_ids from JSON array or comma-separated format."""
        if not raw:
            return []
        raw = str(raw).strip()
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]

    def _fetch_messages_by_ids(self, msg_ids, limit=20):
        """Direct ID lookup on state.db messages table."""
        import sqlite3 as _sql, os as _os
        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return []
            sc = _sql.connect(state_db)
            sc.row_factory = _sql.Row
            placeholders = ",".join("?" * len(msg_ids))
            rows = sc.execute(
                f"SELECT id,session_id,role,content,raw_excerpt,timestamp,salience_score "
                f"FROM messages WHERE id IN ({placeholders}) "
                f"ORDER BY timestamp DESC LIMIT ?",
                (*msg_ids, limit)
            ).fetchall()
            sc.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def _fts5_rerank(self, candidates, query, limit=8):
        """Re-rank a candidate set of messages using FTS5 BM25."""
        import sqlite3 as _sql, os as _os, re as _re
        if not candidates:
            return candidates
        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return candidates[:limit]
            sc = _sql.connect(state_db)
            sc.row_factory = _sql.Row
            safe = _re.sub(r'[^\w\u4e00-\u9fff\s]', ' ', query).strip()
            cand_ids = [c["id"] for c in candidates]
            placeholders = ",".join("?" * len(cand_ids))
            for fts_table in ('messages_fts_trigram', 'messages_fts'):
                try:
                    rows = sc.execute(
                        f"SELECT m.id,m.role,m.content,m.raw_excerpt,m.timestamp,m.salience_score,rank "
                        f"FROM {fts_table} f JOIN messages m ON m.id=f.rowid "
                        f"WHERE {fts_table} MATCH ? AND m.id IN ({placeholders}) "
                        f"ORDER BY rank LIMIT ?",
                        (safe, *cand_ids, limit)
                    ).fetchall()
                    if rows:
                        sc.close()
                        return [dict(r) for r in rows]
                except Exception:
                    pass
            sc.close()
        except Exception:
            pass
        return candidates[:limit]

    def search_state_fast(self, query, limit=5, session_id=""):
        """Fast state.db retrieval: FTS5 candidate pool + cosine rerank.

        Uses existing self._embed() for bge-m3 query vector.
        Opens independent sqlite3 connection to state.db.
        Candidate pool: FTS5 double-table + session_id filter + 14-day window.
        Vectors: binary BLOB (np.frombuffer) with JSON fallback.
        Returns list[dict] with content/role/created_at/session_id/similarity.
        """
        import sqlite3 as _sql, os as _os, time as _time, re as _re

        if not query or not query.strip():
            return []

        # Validate limit
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return []
        if limit <= 0:
            return []
        limit = min(limit, 50)  # hard cap

        qvec = None
        try:
            qvec = self._embed(query)
        except Exception:
            qvec = None  # 嵌入失败不阻塞：FTS5/LIKE 是独立证据路径
        # 注意：qvec 为 None 时继续走候选池，评分阶段无向量候选用 0.26 基准

        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return []
            conn = _sql.connect(state_db)
            conn.row_factory = _sql.Row
        except Exception:
            return []

        try:
            cutoff_ts = int(_time.time()) - 60 * 24 * 3600  # 60-day window
            fetch_cap = min(max(100, limit * 20), 500)  # wide fetch for semantic coverage
            candidates = {}
            _seen = set()

            # Sanitize FTS5 query: strip operators that break MATCH
            safe_q = _re.sub(r'["\'*()^]', ' ', query.strip())
            safe_q = ' '.join(safe_q.split())
            if not safe_q:
                return []  # empty after sanitization, nothing to search

            # ── FTS5 double-table candidate pool ──
            for fts_table in ('messages_fts_trigram', 'messages_fts'):
                try:
                    # Try with time window first
                    rows = conn.execute(
                        f"SELECT m.id, m.content, m.role, m.vector, m.vector_bin, m.timestamp, m.session_id "
                        f"FROM {fts_table} f JOIN messages m ON m.id = f.rowid "
                        f"WHERE {fts_table} MATCH ? "
                        f"AND m.timestamp >= ? "
                        f"ORDER BY m.timestamp DESC LIMIT ?",
                        (safe_q, cutoff_ts, fetch_cap)
                    ).fetchall()
                    # If none within window, retry without time filter
                    if not rows:
                        rows = conn.execute(
                            f"SELECT m.id, m.content, m.role, m.vector, m.vector_bin, m.timestamp, m.session_id "
                            f"FROM {fts_table} f JOIN messages m ON m.id = f.rowid "
                            f"WHERE {fts_table} MATCH ? "
                            f"ORDER BY m.timestamp DESC LIMIT ?",
                            (safe_q, fetch_cap)
                        ).fetchall()
                    for r in rows:
                        rid = r["id"]
                        if rid in _seen:
                            continue
                        _seen.add(rid)
                        candidates[rid] = dict(r)
                except Exception:
                    pass

            # ── LIKE fallback: 短 CJK 词（≤2字）FTS5 分词不拆，用 LIKE 兜底 ──
            # 独立执行并与 FTS5 结果合并；优先 user 对话（证据），assistant 分析是次要
            if _re.search(r'[\u4e00-\u9fff]', query) and len(query.strip()) <= 6:
                try:
                    like_q = f"%{query.strip()}%"
                    # 先只取 user 消息（对话证据，最多 fetch_cap 条）
                    rows = conn.execute(
                        "SELECT id, content, role, vector, vector_bin, timestamp, session_id "
                        "FROM messages "
                        "WHERE content LIKE ? AND role = 'user' AND timestamp >= ? "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (like_q, cutoff_ts, fetch_cap)
                    ).fetchall()
                    for r in rows:
                        rid = r["id"]
                        if rid in _seen:
                            continue
                        _seen.add(rid)
                        candidates[rid] = dict(r)
                except Exception:
                    pass

            # ── If no FTS5 hits, fallback: recent from same session ──
            if not candidates and session_id:
                try:
                    rows = conn.execute(
                        "SELECT id, content, role, vector, vector_bin, timestamp, session_id "
                        "FROM messages "
                        "WHERE session_id = ? AND vector IS NOT NULL "
                        "ORDER BY timestamp DESC LIMIT ?",
                        (session_id, fetch_cap)
                    ).fetchall()
                    for r in rows:
                        rid = r["id"]
                        if rid not in _seen:
                            _seen.add(rid)
                            candidates[rid] = dict(r)
                except Exception:
                    pass

            if not candidates:
                return []

            # ── Cosine rerank (numpy batch from vector_bin, json fallback) ──
            scored = []
            # Separate candidates with vector_bin vs json-only
            bin_cands = {}  # rid -> blob
            json_cands = {}  # rid -> row
            for rid, r in candidates.items():
                blob = r.get("vector_bin")
                if blob is not None:
                    bin_cands[rid] = blob
                elif r.get("vector"):
                    json_cands[rid] = r

            # Batch numpy path for vector_bin
            if bin_cands and qvec is not None:
                qvec_np = np.array(qvec, dtype=np.float32)
                bin_ids = list(bin_cands.keys())
                blobs = [bin_cands[rid] for rid in bin_ids]
                try:
                    vecs_np = np.array([np.frombuffer(b, dtype=np.float32) for b in blobs])
                    dots = np.dot(vecs_np, qvec_np)
                    norms_v = np.linalg.norm(vecs_np, axis=1)
                    norm_q = np.linalg.norm(qvec_np)
                    if norm_q > 0:
                        scores_np = dots / np.maximum(norms_v * norm_q, 1e-10)
                    else:
                        scores_np = np.zeros(len(blobs))
                    for i, rid in enumerate(bin_ids):
                        sim = float(scores_np[i])
                        r = candidates[rid]
                        if session_id and r.get("session_id") == session_id:
                            sim += 0.02
                        if sim > 0.25:
                            scored.append({
                                "id": rid,
                                "content": r.get("content", ""),
                                "role": r.get("role", ""),
                                "timestamp": r.get("timestamp", ""),
                                "session_id": r.get("session_id", ""),
                                "similarity": round(sim, 4),
                                "source": "state",
                                "kind": "state",
                            })
                except Exception:
                    pass  # fall through to json path for these

            # JSON fallback for rows without vector_bin
            if qvec is not None:
                for rid, r in json_cands.items():
                    vraw = r.get("vector")
                    if not vraw:
                        continue
                    try:
                        vec = json.loads(vraw) if isinstance(vraw, str) else vraw
                        if not isinstance(vec, list) or len(vec) != len(qvec):
                            continue
                    except (json.JSONDecodeError, TypeError):
                        continue
                    try:
                        sim = self._cosine_similarity(qvec, vec)
                    except Exception:
                        continue
                    if session_id and r.get("session_id") == session_id:
                        sim += 0.02
                    if sim <= 0.25:
                        continue
                    scored.append({
                        "id": rid,
                        "content": r.get("content", ""),
                        "role": r.get("role", ""),
                        "timestamp": r.get("timestamp", ""),
                        "session_id": r.get("session_id", ""),
                        "similarity": round(sim, 4),
                        "source": "state",
                        "kind": "state",
                    })

            # ── No-vector fallback: FTS5 hits without usable vectors get baseline 0.26 ──
            scored_ids = {x["id"] for x in scored}
            for rid, r in candidates.items():
                if rid in scored_ids:
                    continue
                # 排除工具输出噪音（role=tool 的 JSON 不是对话证据）
                role = r.get("role", "")
                if role == "tool":
                    continue
                sim = 0.26
                if session_id and r.get("session_id") == session_id:
                    sim += 0.02
                if sim > 0.25:
                    scored.append({
                        "id": rid,
                        "content": r.get("content", ""),
                        "role": role,
                        "timestamp": r.get("timestamp", ""),
                        "session_id": r.get("session_id", ""),
                        "similarity": round(sim, 4),
                        "source": "state",
                        "kind": "state",
                    })

            # 排序：相似度降序；同分时 user 对话优先（证据 > assistant 分析）
            scored.sort(key=lambda x: (
                x["similarity"],
                1 if x.get("role") == "user" else 0,
            ), reverse=True)
            return scored[:limit]

        finally:
            conn.close()

    def search_summary(self, query, limit=10):
        """Stage 1: Semantic search on memory.db for topic routing."""
        qvec = self._embed(query)
        if not qvec:
            return self.search(query, limit=limit)
        rows = self._conn.execute("SELECT * FROM memories WHERE vector IS NOT NULL", []).fetchall()
        scored = []
        for r in rows:
            d = dict(r)
            vec = json.loads(d["vector"]) if d.get("vector") else None
            if vec:
                d["_cos_sim"] = round(self._cosine_similarity(qvec, vec), 4)
                scored.append(d)
        scored.sort(key=lambda x: x["_cos_sim"], reverse=True)
        return scored[:limit]

    def search_raw(self, query, limit=8):
        """Standalone FTS5 search on state.db — fallback when cascade has no IDs."""
        import sqlite3 as _sql, os as _os, re as _re
        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return []
            sc = _sql.connect(state_db)
            sc.row_factory = _sql.Row
            safe = _re.sub(r'[^\w\u4e00-\u9fff\s]', ' ', query).strip()
            if not safe:
                return []

            # Build tokens for AND/OR queries
            tokens = [t for t in safe.split() if t]
            cjk_tokens = []
            for t in tokens:
                cjk_tokens.extend(_re.findall(r'[\u4e00-\u9fff]{1,2}|[a-zA-Z0-9]+', t))
            cjk_tokens = [t for t in cjk_tokens if t]
            and_query = safe
            or_query = ' OR '.join(cjk_tokens) if len(cjk_tokens) > 1 else safe

            seen = set()
            results = []

            def _fetch(fts_table, fts_query, fetch_limit):
                try:
                    rows = sc.execute(
                        f"SELECT m.id,m.session_id,m.role,m.content,m.raw_excerpt,m.timestamp,m.salience_score "
                        f"FROM {fts_table} f JOIN messages m ON m.id=f.rowid "
                        f"WHERE {fts_table} MATCH ? ORDER BY rank LIMIT ?",
                        (fts_query, fetch_limit)
                    ).fetchall()
                    return rows
                except Exception:
                    return []

            for fts_table in ('messages_fts_trigram', 'messages_fts'):
                rows = _fetch(fts_table, and_query, limit)
                if len(rows) < 3 and or_query != and_query:
                    rows += _fetch(fts_table, or_query, limit)
                for r in rows:
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        d = {"id":r["id"],"session_id":r["session_id"],"role":r["role"],
                             "content":r["content"],"raw_excerpt":r["raw_excerpt"],
                             "timestamp":r["timestamp"],"salience_score":r["salience_score"]}
                        if d["raw_excerpt"] or d["content"]:
                            results.append(d)
            sc.close()
            return results[:limit]
        except Exception:
            return []

    def search_state_semantic(self, query, limit=10):
        """全量 bge-m3 cosine 语义检索 state.db messages。

        调用嵌入服务取查询向量，遍历 state.db 中所有有 vector 的行，
        计算 cosine similarity，返回 top-N 结果。
        """
        import sqlite3 as _sql, os as _os
        qvec = self._embed(query)
        if not qvec:
            return []

        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return []
            sc = _sql.connect(state_db)
            sc.row_factory = _sql.Row

            rows = sc.execute(
                "SELECT id,session_id,role,content,raw_excerpt,timestamp,salience_score,vector "
                "FROM messages WHERE vector IS NOT NULL AND vector != '[]'"
            ).fetchall()

            scored = []
            for r in rows:
                d = dict(r)
                try:
                    vec = json.loads(d["vector"]) if d.get("vector") else None
                except (json.JSONDecodeError, TypeError):
                    vec = None
                if vec and len(vec) == 1024:
                    sim = self._cosine_similarity(qvec, vec)
                    d["semantic_score"] = round(sim, 4)
                    scored.append(d)

            sc.close()

            scored.sort(key=lambda x: x["semantic_score"], reverse=True)
            return scored[:limit]
        except Exception:
            return []

    def search_two_stage(self, query, limit=8):
        """True two-stage cascade: semantic→message IDs→direct lookup+FTS5 rerank."""
        # Stage 1: Semantic search on memory.db
        summaries = self.search_summary(query, limit=limit)

        # Stage 2: Extract source_message_ids and cascade to real messages
        all_msg_ids = []
        for s in summaries:
            ids = self._parse_message_ids(s.get("source_message_ids"))
            all_msg_ids.extend(ids)

        # De-duplicate, preserving order (first summary = highest semantic match)
        msg_ids = list(dict.fromkeys(all_msg_ids))

        results = []
        # Add summaries
        for s in summaries:
            results.append({"type":"summary","key":s.get("key"),"content":s.get("content"),
                          "score":s.get("_cos_sim",0),"source_ids":s.get("source_message_ids")})

        # Add evidence from ID cascade
        if msg_ids:
            evidence = self._fetch_messages_by_ids(msg_ids, limit=limit * 2)
            if len(evidence) > limit:
                evidence = self._fts5_rerank(evidence, query, limit)
            for e in evidence:
                results.append({"type":"evidence","msg_id":e.get("id"),"role":e.get("role"),
                              "content":(e.get("raw_excerpt") or e.get("content",""))[:300],
                              "timestamp":e.get("timestamp"),"score":e.get("salience_score",0)})
        else:
            # Fallback: no source IDs — use standalone FTS5 search
            raw = self.search_raw(query, limit=limit)
            for r in raw:
                results.append({"type":"evidence","msg_id":r.get("id"),"role":r.get("role"),
                              "content":(r.get("raw_excerpt") or r.get("content",""))[:300],
                              "timestamp":r.get("timestamp"),"score":r.get("salience_score",0)})

        return results[:limit*2]

    def search_v2(self, query, limit=8):
        """V2 entry point: two-stage cascade with search_multi fallback."""
        results = self.search_two_stage(query, limit=limit)
        if not results:
            return self.search_multi(query, limit=limit)
        return results
