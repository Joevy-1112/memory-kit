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

    # NOTE: MemoryDB（db.py）覆盖了本类的 search_semantic / backfill_vectors，
    # 提供更快的 vector_bin 二进制批量余弦 + json→bin 自动迁移。这里保留
    # 基础实现作为 mixin 独立使用时的参考/兜底，两处行为一致（维度不兼容时跳过）。
    def search_semantic(self, query, limit=10, category=None, scope=None):
        """语义搜索: embed query, cosine similarity against stored vectors."""
        qvec = self._embed(query)
        if not qvec:
            return []
        conditions = ["m.vector IS NOT NULL"]
        params = []
        if category: conditions.append("m.category = ?"); params.append(category)
        if scope: conditions.append("m.scope = ?"); params.append(scope)
        conditions.append("(m.ttl IS NULL OR (julianday('now')-julianday(m.created_at))*86400 < m.ttl)")
        rows = self._conn.execute(
            f"SELECT * FROM memories m WHERE {' AND '.join(conditions)}", params
        ).fetchall()
        scored = []
        for r in rows:
            d = dict(r)
            vec = json.loads(d["vector"]) if d.get("vector") else None
            if vec:
                d["similarity"] = round(self._cosine_similarity(qvec, vec), 4)
                scored.append(d)
        scored.sort(key=lambda x: x["similarity"], reverse=True)
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

    def search_multi(self, query, limit=10, category=None, scope=None):
        """四信号并行检索 + semantic-primary 加权融合。"""
        if not query.strip():
            return self.search(query, limit=limit, category=category, scope=scope)

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
        qvec = self._embed(query)
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

        # Signal 5: Recency
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
        recency_rows = self._conn.execute(
            "SELECT key, julianday(created_at) as jd FROM memories" + recency_where,
            recency_params
        ).fetchall()
        days_rank = [(r["key"], max(0, now_jd - r["jd"])) for r in recency_rows]
        days_rank.sort(key=lambda x: x[1])
        recency_rank = {key: i+1 for i, (key, _) in enumerate(days_rank)}

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
            if key in _intersection_keys and len(_match_terms) > 1:
                score *= 1.25
            if key in recency_rank:
                score += 0.001 * (1.0 / max(recency_rank[key], 1))
            _fused_scores[key] = score

        fused = []
        for key in all_keys:
            row = self._conn.execute("SELECT * FROM memories WHERE key=?", (key,)).fetchone()
            if row:
                d = dict(row)
                # 兜底（审查修复 #2）：最终结果再按 category/scope 过滤一次，
                # 保证 'category=protocol 只返回 protocol' 不变量，即使某个信号漏过滤也不泄漏。
                if category and d.get("category") != category:
                    continue
                if scope and d.get("scope") != scope:
                    continue
                _base = _fused_scores.get(key, 0.0)
                # 环3 验证回流 + 环4 时间感知：最终得分 = 融合分 × verification × time
                # （verified 前移 / disputed、STALE 沉底；近期记忆优先，老记忆保底 30%）
                d["rrf_raw"] = round(_base, 5)
                _tw = self._time_weight(d.get("created_at"))
                d["time_weight"] = round(_tw, 4)
                d["rrf_score"] = round(_base * self._verify_weight(d.get("verification_status")) * _tw, 5)
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
