#!/usr/bin/env python3
"""记忆生命周期模块 — 衰减/去重/冲突/学习。MemoryLifecycle mixin class。"""
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _anchor_time(text: str, now: datetime | None = None) -> str:
    """写时时间锚定：强时态词→绝对日期，弱时态词→"当时"。

    强时态词基于 now（默认当前时间）计算绝对日期：
      今天→YYYY-MM-DD, 昨天→前一天, 明天→后一天, 前天/后天,
      今年/去年/明年→年份, 上个月/这个月→YYYY-MM

    弱时态词→"当时"（表示历史语境）：
      现在/当前/目前/这会儿/刚刚/刚才→当时, 最近→近期

    纯文本正则替换，不改路径/URL/代码块。
    """
    if not text or not isinstance(text, str):
        return text
    if now is None:
        now = datetime.now()

    today = now.date()

    # ── 强时态词 → 绝对日期 ──
    # 注意：按长词优先顺序，避免残留
    strong_replacements = [
        # Day-level（长→短：前天/后天 先于 昨天/今天/明天）
        ("前天", (today - timedelta(days=2)).strftime("%Y-%m-%d")),
        ("后天", (today + timedelta(days=2)).strftime("%Y-%m-%d")),
        ("昨天", (today - timedelta(days=1)).strftime("%Y-%m-%d")),
        ("明天", (today + timedelta(days=1)).strftime("%Y-%m-%d")),
        ("今天", today.strftime("%Y-%m-%d")),
        # Year-level
        ("去年", str(now.year - 1) + "年"),
        ("明年", str(now.year + 1) + "年"),
        ("今年", str(now.year) + "年"),
        # Month-level
        ("上个月", (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")),
        ("这个月", now.strftime("%Y-%m")),
    ]

    for pattern, replacement in strong_replacements:
        text = text.replace(pattern, replacement)

    # ── 弱时态词 → "当时" ──
    weak_to_dangshi = ["现在", "当前", "目前", "这会儿", "刚刚", "刚才"]
    for w in weak_to_dangshi:
        text = text.replace(w, "当时")

    text = text.replace("最近", "近期")

    return text


class MemoryLifecycle:
    """Mixin: lifecycle methods for MemoryDB."""

    DECAY_TAU = {"slow": 90, "medium": 30, "fast": 7}
    # ── 环2 遗忘状态机阈值（memory-v2-plan.md 环2，评审 #4 分档确认）──
    # 双信号：TTL 显式过期（ttl 列，秒） + accessed_at 超时未访问
    STALE_AFTER_DAYS = {"slow": 90, "medium": 30, "fast": 7}      # active→stale 超时阈值
    ARCHIVE_AFTER_DAYS = {"slow": 180, "medium": 60, "fast": 14}  # stale→archived（stale_since 超期）
    DELETE_AFTER_DAYS = {"slow": 270, "medium": 90, "fast": 30}   # archived→物理删（stale_since 超期，保守）
    ARCHIVED_IMPORTANCE = 0.05   # archived 降权标记（检索沉底）
    PROTECTED_CATEGORIES = {"protocol", "user", "hardware", "env"}  # 配置类/铁律/画像 不物理删

    def age(self):
        """记忆生命周期：艾宾浩斯衰减 + 环2 遗忘状态机（TTL + 衰减双信号）。

        衰减（保留，评审 #5：降 importance 而非 confidence——时间流逝只应降排名，
        置信度属环1/环3 验证回流领域；衰减与状态机互补不重叠）:
          importance *= e^(-days/τ)，τ 按 decay_profile 分档 {slow:90, medium:30, fast:7}

        状态机（环2 补充）:
          active → stale:   TTL 显式过期 或 超时未访问 > STALE_AFTER_DAYS[profile]
                            写 stale_since + verification_status='STALE'
          stale  → archived: stale_since 超期 > ARCHIVE_AFTER_DAYS[profile]
                            降权 importance=ARCHIVED_IMPORTANCE + verification_status='ARCHIVED'
          archived → 物理删: water=2（可自动淘汰）+ 非配置类 + stale_since 超期 > DELETE_AFTER_DAYS
          (复活: stale/archived 后又被访问 accessed_at > stale_since → 清标记回 active，
                 防误删"低频但关键"，呼应设计"不删原则"）

        状态写入 verification_status 的 STALE/ARCHIVED 值——与环3 search 加权
        (verified/STALE/disputed) 设计衔接；archived 用 importance 垫底实现检索降权，
        不改 search() 排序逻辑（避免环3 未落地前引入回归）。
        """
        rows = self._conn.execute(
            "SELECT key, importance, accessed_at, decay_profile, ttl, created_at, "
            "stale_since, water, category, verification_status "
            "FROM memories WHERE scope='permanent'"
        ).fetchall()
        now = datetime.now()
        counts = {"slow": 0, "medium": 0, "fast": 0}
        total = 0
        staled = archived = deleted = revived = 0
        # 写循环包 _tx，异常自动 rollback，防悬挂写事务
        with self._tx():
            for r in rows:
                try:
                    accessed = datetime.strptime(r["accessed_at"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    accessed = now
                days = (now - accessed).total_seconds() / 86400.0
                profile = r["decay_profile"] or "medium"
                if profile not in self.DECAY_TAU:
                    profile = "medium"
                tau = self.DECAY_TAU[profile]
                status = r["verification_status"] or "active"

                # 解析 stale_since（可能为 NULL/畸形）
                stale_since = None
                if r["stale_since"]:
                    try:
                        stale_since = datetime.strptime(r["stale_since"], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        stale_since = None

                # ── 复活：stale/archived 后又被访问 → 回 active（不衰减不重判，下次自然走流程）──
                if status in ("STALE", "ARCHIVED") and stale_since and accessed > stale_since:
                    self._conn.execute(
                        "UPDATE memories SET stale_since=NULL, verification_status='active', "
                        "updated_at=datetime('now') WHERE key=?",
                        (r["key"],))
                    revived += 1
                    continue

                # ── TTL 显式过期（双信号之一）──
                ttl_expired = False
                if r["ttl"]:
                    try:
                        created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        created = now
                    ttl_expired = (now - created).total_seconds() > r["ttl"]

                # ── 1) 艾宾浩斯衰减（archived 已降权，不再衰减）──
                if days >= 0.5 and status != "ARCHIVED":
                    new_imp = max(0.1, r["importance"] * (2.71828 ** (-days / tau)))
                    if r["importance"] >= 0.8:
                        new_imp = max(new_imp, 0.5)
                    self._conn.execute("UPDATE memories SET importance=?, updated_at=datetime('now') WHERE key=?",
                                       (round(new_imp, 2), r["key"]))
                    counts[profile] = counts.get(profile, 0) + 1
                    total += 1

                # ── 2) 状态机转移 ──
                if status == "STALE":
                    if stale_since and (now - stale_since).days > self.ARCHIVE_AFTER_DAYS[profile]:
                        # stale → archived：降权标记（importance 垫底 + 状态标记）
                        self._conn.execute(
                            "UPDATE memories SET verification_status='ARCHIVED', "
                            "importance=?, updated_at=datetime('now') WHERE key=?",
                            (min(r["importance"], self.ARCHIVED_IMPORTANCE), r["key"]))
                        archived += 1
                elif status == "ARCHIVED":
                    if stale_since:
                        stale_days = (now - stale_since).days
                        water = r["water"] if r["water"] is not None else 2
                        # archived → 物理删：仅 water=2 可淘汰 + 非配置类 + stale_since 超期
                        if (water == 2 and r["category"] not in self.PROTECTED_CATEGORIES
                                and stale_days > self.DELETE_AFTER_DAYS[profile]):
                            self._conn.execute("DELETE FROM memories WHERE key=?", (r["key"],))
                            deleted += 1
                elif status == "disputed":
                    # 环1 标记的争议记忆：钉住不参与 active→stale 转移（等 review），
                    # 但 importance 仍按衰减（L142 已处理），避免它长期霸占检索位。
                    # 不做任何状态转移，disputed 保持到用户确认（reinforce）或
                    # 验证管线重新评估（verify_recent 会把它降回 unverified 重验）。
                    pass
                else:
                    # active → stale：TTL 到期 或 超时未访问（写 stale_since）
                    if ttl_expired or days > self.STALE_AFTER_DAYS[profile]:
                        # 注意：stale_since 必须用 Python 本地时间（与 accessed_at 同源）——
                        # SQLite datetime('now') 是 UTC，与 Python datetime.now() 差 8 小时，
                        # 混用会导致下一轮 age() 把新 stale 记忆误判为"stale 后被访问"而复活。
                        self._conn.execute(
                            "UPDATE memories SET stale_since=?, "
                            "verification_status='STALE', updated_at=datetime('now') WHERE key=?",
                            (now.strftime("%Y-%m-%d %H:%M:%S"), r["key"]))
                        staled += 1
        return {"decayed": total, "staled": staled, "archived": archived,
                "deleted": deleted, "revived": revived, "profiles": counts}

    def deduplicate(self, threshold=0.92):
        """语义去重: 合并相似度>threshold的条目。"""
        rows = self._conn.execute(
            "SELECT id, key, content, importance, vector FROM memories "
            "WHERE vector IS NOT NULL ORDER BY id"
        ).fetchall()
        pairs = []; removed = 0
        # 2026-08-02: json.loads 加保护（畸形 vector 跳过而非中断循环），
        # 写循环包 _tx——异常自动 rollback，防悬挂写事务
        with self._tx():
            for i in range(len(rows)):
                for j in range(i+1, len(rows)):
                    try:
                        vi = json.loads(rows[i]["vector"]) if rows[i]["vector"] else None
                        vj = json.loads(rows[j]["vector"]) if rows[j]["vector"] else None
                    except Exception:
                        continue
                    if vi and vj and self._cosine_similarity(vi, vj) >= threshold:
                        keep = rows[i] if rows[i]["importance"] >= rows[j]["importance"] else rows[j]
                        remove = rows[j] if keep["key"] == rows[i]["key"] else rows[i]
                        nc = keep["content"]
                        if remove["content"] not in keep["content"]:
                            nc += "\n\n[合并自: %s]\n%s" % (remove["key"], remove["content"][:200])
                        self._conn.execute("UPDATE memories SET content=?, importance=? WHERE key=?",
                                           (nc, max(keep["importance"], remove["importance"]), keep["key"]))
                        self._conn.execute("DELETE FROM memories WHERE key=?", (remove["key"],))
                        pairs.append((keep["key"], remove["key"])); removed += 1; break
        return {"merged": len(pairs), "removed": removed, "pairs": pairs}

    def detect_conflicts(self):
        """跨Agent冲突检测。"""
        rows = self._conn.execute(
            "SELECT key, COUNT(DISTINCT agent_id) as agent_count, "
            "GROUP_CONCAT(DISTINCT agent_id) as agents, COUNT(*) as versions "
            "FROM memories GROUP BY key HAVING agent_count > 1"
        ).fetchall()
        conflicts = []
        for r in rows:
            d = dict(r)
            d["versions"] = [dict(v) for v in self._conn.execute(
                "SELECT agent_id, content, updated_at FROM memories WHERE key=? ORDER BY updated_at DESC",
                (d["key"],)
            ).fetchall()]
            conflicts.append(d)
        return conflicts

    def low_importance_count(self, threshold=0.2):
        return self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE importance < ? AND scope='permanent'",
            (threshold,)
        ).fetchone()[0]

    def compact(self):
        # Soft-archive: scope='archived' instead of hard DELETE.
        # Archived entries won't appear in normal prefetch but can be
        # found via explicit search — user can always ask about old topics.
        # Only truly expired temp/session entries get hard-deleted.
        # 2026-08-02: 包 _tx，异常自动 rollback，防悬挂写事务
        with self._tx():
            d1 = self._conn.execute(
                "UPDATE memories SET scope='archived' WHERE importance < 0.1 AND scope='permanent'"
            ).rowcount
            d2 = self._conn.execute(
                "DELETE FROM memories WHERE scope IN ('temp','session') AND ttl IS NOT NULL "
                "AND (julianday('now')-julianday(created_at))*86400 >= ttl"
            ).rowcount
        return {"archived_low_imp": d1, "deleted_expired": d2}

    # ══════════════════════════════════════════════════════════════
    # 验证管线 (Verification Pipeline)
    # ══════════════════════════════════════════════════════════════
    #
    # 设计原则:
    #   - 零schema变更，利用已有 provenance 列存储验证元数据JSON
    #   - 零新组件，纯 Python + SQLite，无 LLM 调用
    #   - 关键词+实体双重交叉检查，轻量统计匹配
    #   - 低置信自动降级 importance
    #
    # provenance JSON 格式:
    #   {"v": "pending|verified|conflict|degraded|unverifiable",
    #    "vt": "2026-07-12 17:20:00",      # verified_at 时间戳
    #    "vscore": 0.85,                    # 验证得分 0-1
    #    "sids": [123, 456],                # 匹配到的源消息ID
    #    "method": "keyword|entity|both",   # 验证方法
    #    "downgrade": 0.1}                  # importance 降级量

    # ── 中文停用词 (CJK stopwords) ──
    _STOPWORDS_CJK = {
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
        "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
        "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
        "什么", "怎么", "哪", "吗", "啊", "呢", "吧", "哦", "嗯", "哈",
        "因为", "所以", "但是", "而且", "如果", "虽然", "然后", "可以",
        "这个", "那个", "就是", "还是", "或者", "已经", "知道", "需要",
        "能够", "应该", "可能", "只是", "不过", "还是", "比较",
        "我们", "你们", "他们", "她们", "它们", "自己", "大家",
    }

    _STOPWORDS_EN = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "having", "do", "does", "did",
        "doing", "will", "would", "could", "should", "may", "might",
        "can", "shall", "to", "of", "in", "for", "on", "with", "at",
        "by", "from", "as", "into", "through", "during", "before",
        "after", "above", "below", "between", "under", "again",
        "further", "then", "once", "here", "there", "when", "where",
        "why", "how", "all", "both", "each", "few", "more", "most",
        "other", "some", "such", "no", "nor", "not", "only", "own",
        "same", "so", "than", "too", "very", "just", "about", "also",
        "if", "or", "and", "but", "it", "its", "that", "this", "these",
        "those", "which", "who", "whom",
    }

    def _tokenize_keywords(self, text: str) -> set:
        """提取文本中的关键实词（去停用词、标点、纯数字）。
        
        返回: set of significant tokens (中文2字词 + 英文≥3字母词)
        """
        if not text:
            return set()
        tokens = set()
        # 中文: 提取连续中文字符，按2字切分
        cjk_chars = re.findall(r'[\u4e00-\u9fff]+', text)
        for seg in cjk_chars:
            for i in range(0, len(seg) - 1):
                bigram = seg[i:i+2]
                if bigram not in self._STOPWORDS_CJK:
                    tokens.add(bigram)
        # 英文: 提取≥3字母的单词
        en_words = re.findall(r'[a-zA-Z]{3,}', text)
        for w in en_words:
            wl = w.lower()
            if wl not in self._STOPWORDS_EN:
                tokens.add(wl)
        # 技术术语 (保留原始大小写): API, GPU, etc.
        tech_terms = re.findall(r'\b[A-Z]{2,10}\b', text)
        for t in tech_terms:
            tokens.add(t)
        return tokens

    def _cross_check(self, memory_content: str, source_session: str,
                     source_msg_ids: list = None) -> dict:
        """交叉检查：记忆内容 vs 源session消息。
        
        双层验证：
          1. 关键词重叠度 → keyword_score (0-1)
          2. 实体重叠度   → entity_score  (0-1)
          融合: 0.6 * keyword + 0.4 * entity (关键词权重更高，更稳定)
        
        返回: {"score": float, "method": str, "matched_ids": list,
               "keyword_score": float, "entity_score": float}
        """
        import sqlite3 as _sql, os as _os
        result = {"score": 0.0, "method": "none", "matched_ids": [],
                  "keyword_score": 0.0, "entity_score": 0.0}

        if not memory_content or not source_session:
            return result

        # 获取源消息
        try:
            state_db = self.state_db_path or ""
            if not state_db or not _os.path.exists(state_db):
                return result
            conn = _sql.connect(state_db)
            conn.row_factory = _sql.Row
        except Exception:
            return result

        msgs = []
        try:
            if source_msg_ids and len(source_msg_ids) > 0:
                ph = ",".join("?" * len(source_msg_ids))
                rows = conn.execute(
                    f"SELECT id, content FROM messages WHERE id IN ({ph})",
                    source_msg_ids
                ).fetchall()
            else:
                # 按 session_id 查最近消息
                rows = conn.execute(
                    "SELECT id, content FROM messages WHERE session_id = ? "
                    "ORDER BY timestamp DESC LIMIT 50",
                    (source_session,)
                ).fetchall()
            msgs = [dict(r) for r in rows]
        except Exception:
            conn.close()
            return result
        finally:
            conn.close()

        if not msgs:
            result["method"] = "none"
            return result

        # 合并所有源消息文本
        source_text = " ".join(m.get("content", "") or "" for m in msgs)

        # ── 1. 关键词重叠度 ──
        mem_kw = self._tokenize_keywords(memory_content)
        src_kw = self._tokenize_keywords(source_text)

        if len(mem_kw) == 0:
            result["keyword_score"] = 0.0
        else:
            overlap = mem_kw & src_kw
            result["keyword_score"] = round(len(overlap) / len(mem_kw), 4)

        # ── 2. 实体重叠度 ──
        mem_entities = set(self._extract_entities(memory_content))
        src_entities = set(self._extract_entities(source_text))

        if len(mem_entities) == 0:
            result["entity_score"] = 0.0
        else:
            overlap = mem_entities & src_entities
            result["entity_score"] = round(len(overlap) / len(mem_entities), 4)

        # ── 融合评分: 关键词权重大于实体 ──
        if result["keyword_score"] > 0 or result["entity_score"] > 0:
            result["score"] = round(
                0.6 * result["keyword_score"] + 0.4 * result["entity_score"], 4
            )
            if result["keyword_score"] > 0 and result["entity_score"] > 0:
                result["method"] = "both"
            elif result["keyword_score"] > 0:
                result["method"] = "keyword"
            else:
                result["method"] = "entity"

        # 记录匹配到的消息ID
        result["matched_ids"] = [m["id"] for m in msgs[:10]]

        return result

    def verify(self, key: str, auto_degrade: bool = True,
               degrade_threshold: float = 0.3) -> dict:
        """验证单条记忆：交叉检查源session → 评分 → 可选自动降级。
        
        参数:
          key: 记忆 key
          auto_degrade: 低分是否自动降级 importance
          degrade_threshold: 低于此分的记忆降级 (默认 0.3)
        
        返回:
          {"ok": True, "verification": "verified|degraded|conflict|unverifiable",
           "score": 0.85, "keyword_score": 0.9, "entity_score": 0.75,
           "downgraded": True/False, "new_importance": 0.45}
        """
        row = self._conn.execute(
            "SELECT id, key, content, importance, source_session, provenance "
            "FROM memories WHERE key=?",
            (key,)
        ).fetchone()

        if not row:
            return {"ok": False, "error": f"记忆不存在: {key}"}

        d = dict(row)
        content = d["content"] or ""
        source_session = d.get("source_session") or ""
        importance = d["importance"] or 0.5

        # 解析已有 provenance
        prov = {}
        try:
            raw = d.get("provenance") or "{}"
            prov = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            prov = {}

        # 无源 session → 不可验证
        if not source_session:
            prov["v"] = "unverifiable"
            prov["vt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            prov["vscore"] = 0.0
            prov["method"] = "none"
            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET provenance=? WHERE id=?",
                    (json.dumps(prov, ensure_ascii=False), d["id"])
                )
            return {
                "ok": True, "verification": "unverifiable",
                "score": 0.0, "keyword_score": 0.0, "entity_score": 0.0,
                "downgraded": False, "new_importance": importance,
                "reason": "无 source_session"
            }

        # 执行交叉检查
        xc = self._cross_check(content, source_session)
        score = xc["score"]
        keyword_score = xc["keyword_score"]
        entity_score = xc["entity_score"]
        method = xc["method"]
        matched_ids = xc["matched_ids"]

        # 更新 provenance
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prov["v"] = "verified"
        prov["vt"] = now_str
        prov["vscore"] = score
        prov["method"] = method
        prov["sids"] = matched_ids[:20]

        result = {
            "ok": True, "verification": "verified",
            "score": score, "keyword_score": keyword_score,
            "entity_score": entity_score, "method": method,
            "matched_ids": matched_ids[:10],
            "downgraded": False, "new_importance": importance,
        }

        # 自动降级逻辑
        if auto_degrade and score < degrade_threshold:
            # 降级 importance: 乘以 (0.5 + score) 因子
            degrade_factor = 0.5 + score * 1.0  # score=0 → 0.5, score=0.29 → 0.79
            new_imp = round(importance * degrade_factor, 2)
            new_imp = max(0.1, min(new_imp, importance))  # 不低于 0.1, 不高于原值

            prov["v"] = "degraded"
            prov["downgrade"] = round(importance - new_imp, 2)

            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET importance=?, provenance=? WHERE id=?",
                    (new_imp, json.dumps(prov, ensure_ascii=False), d["id"])
                )

            result["verification"] = "degraded"
            result["downgraded"] = True
            result["new_importance"] = new_imp
            result["downgrade_amount"] = round(importance - new_imp, 2)
            logger.info("[verify] degraded key=%s score=%.3f imp=%.2f→%.2f",
                        key, score, importance, new_imp)
        else:
            # 高分 → 标记 verified
            if score >= 0.5:
                # 高验证分: 小幅提升 importance (但不超过原值+0.05)
                boost = min(0.05, score * 0.05)
                new_imp = round(min(importance + boost, 0.95), 2)
                if new_imp > importance:
                    self._conn.execute(
                        "UPDATE memories SET importance=? WHERE id=?",
                        (new_imp, d["id"])
                    )
                    result["new_importance"] = new_imp
                    result["boosted"] = True
                    prov["boost"] = round(new_imp - importance, 2)

            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET provenance=? WHERE id=?",
                    (json.dumps(prov, ensure_ascii=False), d["id"])
                )
            logger.info("[verify] key=%s score=%.3f method=%s → %s",
                        key, score, method, prov["v"])

        return result

    def verify_batch(self, limit: int = 100, auto_degrade: bool = True,
                     degrade_threshold: float = 0.3,
                     only_pending: bool = True) -> dict:
        """批量验证记忆。
        
        参数:
          limit: 单批最大数量
          auto_degrade: 低分自动降级
          degrade_threshold: 降级阈值
          only_pending: 只验证 pending 状态的记忆 (False = 全部)
        
        返回:
          {"total": N, "verified": N, "degraded": N, "unverifiable": N,
           "errors": N, "avg_score": 0.XX}
        """
        if only_pending:
            # 查 provenance 中 v="pending" 或无 provenance 的记忆
            rows = self._conn.execute(
                "SELECT key FROM memories WHERE provenance IS NULL "
                "OR provenance = '' "
                "OR provenance = '{}' "
                "OR json_extract(provenance, '$.v') = 'pending' "
                "ORDER BY id LIMIT ?",
                (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT key FROM memories ORDER BY id LIMIT ?",
                (limit,)
            ).fetchall()

        stats = {"total": len(rows), "verified": 0, "degraded": 0,
                 "unverifiable": 0, "errors": 0, "avg_score": 0.0}
        scores = []

        for i, r in enumerate(rows):
            try:
                res = self.verify(r["key"], auto_degrade=auto_degrade,
                                  degrade_threshold=degrade_threshold)
                if res.get("verification") == "degraded":
                    stats["degraded"] += 1
                elif res.get("verification") == "unverifiable":
                    stats["unverifiable"] += 1
                elif res.get("verification") == "verified":
                    stats["verified"] += 1
                if res.get("score", 0) > 0:
                    scores.append(res["score"])
                elif res.get("ok"):
                    stats["errors"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.warning("[verify_batch] error on key=%s: %s", r["key"], e)

            # 每50条提交一次
            if (i + 1) % 50 == 0:
                self._conn.commit()
                logger.info("[verify_batch] progress: %d/%d (v=%d d=%d u=%d err=%d)",
                            i + 1, len(rows), stats["verified"], stats["degraded"],
                            stats["unverifiable"], stats["errors"])

        self._conn.commit()
        if scores:
            stats["avg_score"] = round(sum(scores) / len(scores), 4)
        return stats

    def verify_stats(self) -> dict:
        """验证管线统计：按验证状态分组计数。
        
        纯 SQL 聚合，兼容 malformed JSON。"""
        # 统计各验证状态 —— 先取所有 prov，Python 侧解析避免 json_extract 炸
        all_rows = self._conn.execute(
            "SELECT provenance FROM memories"
        ).fetchall()

        by_status = {}
        
        for r in all_rows:
            prov_raw = r["provenance"] or ""
            if not prov_raw or prov_raw in ("", "{}", "null"):
                by_status["unverified"] = by_status.get("unverified", 0) + 1
                continue
                
            # 用正则快速提取 v 字段，避免 json.loads 抛异常
            m = re.search(r'"v"\s*:\s*"([^"]+)"', prov_raw)
            if m:
                v = m.group(1)
                if v in ("verified", "pending", "degraded", "conflict", "unverifiable"):
                    by_status[v] = by_status.get(v, 0) + 1
                else:
                    by_status["other"] = by_status.get("other", 0) + 1
            else:
                # JSON 失效或格式不对 → 尝试 json.loads
                try:
                    data = json.loads(prov_raw)
                    v = data.get("v", "unknown")
                    if v in ("verified", "pending", "degraded", "conflict", "unverifiable"):
                        by_status[v] = by_status.get(v, 0) + 1
                    else:
                        by_status["other"] = by_status.get("other", 0) + 1
                except (json.JSONDecodeError, TypeError):
                    by_status["unverified"] = by_status.get("unverified", 0) + 1

        total = sum(by_status.values())

        # 按置信度分布统计
        conf_rows = self._conn.execute("""
            SELECT
                CASE
                    WHEN importance < 0.3 THEN '极低(<0.3)'
                    WHEN importance < 0.6 THEN '低(0.3-0.6)'
                    WHEN importance < 0.8 THEN '中(0.6-0.8)'
                    ELSE '高(>0.8)'
                END as band,
                COUNT(*) as cnt
            FROM memories
            GROUP BY band
        """).fetchall()

        conf_dist = {r["band"]: r["cnt"] for r in conf_rows}

        # 平均验证分（Python侧逐行计算，兼容malformed JSON）
        avg_vscore = 0.0
        score_rows = self._conn.execute(
            "SELECT provenance FROM memories WHERE provenance IS NOT NULL "
            "AND provenance != '' AND provenance != '{}' AND provenance != 'null'"
        ).fetchall()
        scores_list = []
        for r in score_rows:
            prov_raw = r["provenance"] or ""
            try:
                m = re.search(r'"vscore"\s*:\s*([0-9.]+)', prov_raw)
                if m:
                    scores_list.append(float(m.group(1)))
                else:
                    data = json.loads(prov_raw)
                    s = data.get("vscore")
                    if s is not None:
                        scores_list.append(float(s))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if scores_list:
            avg_vscore = round(sum(scores_list) / len(scores_list), 4)

        return {
            "total": total,
            "by_status": by_status,
            "confidence_distribution": conf_dist,
            "avg_verification_score": avg_vscore,
            "unverified_pct": round(by_status.get("unverified", 0) / max(total, 1) * 100, 1),
            "pending_pct": round(by_status.get("pending", 0) / max(total, 1) * 100, 1),
        }

    def learn(self, content, source="task", source_session=None):
        """自动学习：评分分类+语义去重+原话保留+session关联。"""
        if not content or len(content.strip()) < 10:
            return {"error": "content too short"}
        content = content.strip()
        content = _anchor_time(content)  # 写时锚定：相对时间→绝对日期/当时
        lower = content.lower()

        # ---- 评分分类 ----
        lessons = {"lesson": ["教训","错误","别","不要","别再","注意","坑","bug","陷阱","崩溃","掉","crash","fix","fixed","failed"]}
        envs = {"env": ["环境","路径","配置","版本","硬件","GPU","端口","安装","驱动","内存","显存"]}
        protos = {"protocol": ["规则","必须","一定","禁止","铁律","流程"]}
        skills = {"skill": ["步骤","方法","怎么做","如何","命令","workflow","how to","step","command"]}
        scores = {}
        for cat, kws in [*lessons.items(), *envs.items(), *protos.items(), *skills.items()]:
            scores[cat] = sum(2 if len(kw)<=2 else 1 for kw in kws if kw in lower)
        priority = ["lesson","env","protocol","skill","general"]
        max_score = max(scores.values()) if scores else 0
        category = "general"
        if max_score > 0:
            for cat in priority:
                if scores.get(cat,0) == max_score: category = cat; break

        # ---- Tags ----
        tags = set()
        for kw in lessons["lesson"]+envs["env"]+protos["protocol"]+skills["skill"]:
            if kw in lower: tags.add(kw)
        if not tags:
            tags = {w for w in content.replace(","," ").replace("，"," ").split()[:5] if len(w)>1}

        # ---- Importance ----
        imp = 0.6
        high_imp = ["严重","必须","不要","别再","注意","critical","never","always","important","关键","重要"]
        imp += 0.15 * sum(1 for k in high_imp if k in lower)
        imp = min(imp, 0.95)
        if source == "correction": imp = max(imp, 0.9)
        elif source == "discovery": imp = max(imp, 0.8)

        # ---- Infer: 语义+图解混合去重 ----
        # Compute embedding once, use for both dedup and storage
        qvec = self._embed(content[:5000])
        new_entities = self._extract_entities(content)  # 提取实体用于图解信号
        merged = False

        def _entity_overlap(new_ents, old_ents_json):
            """计算实体重叠数。"""
            if not new_ents or not old_ents_json:
                return 0
            try:
                old_ents = json.loads(old_ents_json) if isinstance(old_ents_json, str) else old_ents_json
                if not old_ents:
                    return 0
                return len(set(new_ents) & set(old_ents))
            except (json.JSONDecodeError, TypeError):
                return 0

        def _fusion_score(sim, overlap):
            """向量+图解信号融合。乘性boost，Hindsight保守路线。"""
            if overlap >= 2:
                boost = 1.0 + 0.05 * min(overlap, 5)  # 2个→1.10, 5个→1.25
                return min(sim * boost, 1.0)
            return sim

        # 主路径: 向量去重（嵌入在线时）
        if qvec:
            import struct, numpy as np
            qvec_np = np.array(qvec, dtype=np.float32)
            rows = self._conn.execute(
                "SELECT id, key, content, importance, source_session, vector, vector_bin, entities FROM memories WHERE vector IS NOT NULL"
            ).fetchall()
            
            # Fast path: batch cosine over binary blobs
            blobs = []
            row_indices = []
            for idx, r in enumerate(rows):
                if r["vector_bin"] is not None:
                    blobs.append(r["vector_bin"])
                    row_indices.append(idx)
            
            if blobs:
                vecs_np = np.array([np.frombuffer(b, dtype=np.float32) for b in blobs])
                dots = np.dot(vecs_np, qvec_np)
                norms_v = np.linalg.norm(vecs_np, axis=1)
                norm_q = np.linalg.norm(qvec_np)
                if norm_q > 0:
                    scores = dots / np.maximum(norms_v * norm_q, 1e-10)
                else:
                    scores = np.zeros(len(blobs))
                # Check matches
                matches = []
                for i, idx in enumerate(row_indices):
                    sim = float(scores[i])
                    r = rows[idx]
                    overlap = _entity_overlap(new_entities, r["entities"])
                    fusion = _fusion_score(sim, overlap)
                    if fusion >= 0.85:
                        matches.append((fusion, sim, overlap, dict(r)))
            else:
                # Slow fallback: JSON parse
                matches = []
                for r in rows:
                    dv = json.loads(r["vector"]) if r.get("vector") else None
                    if dv:
                        sim = self._cosine_similarity(qvec, dv)
                        overlap = _entity_overlap(new_entities, r["entities"])
                        fusion = _fusion_score(sim, overlap)
                        if fusion >= 0.85:
                            matches.append((fusion, sim, overlap, dict(r)))
            if matches:
                matches.sort(key=lambda x: x[0], reverse=True)
                best = matches[0][3]
                old_key = best["key"]
                old_imp = best["importance"] or 0.5
                new_imp = min(max(imp, old_imp + 0.05), 0.98)
                old_sessions = best.get("source_session") or ""

                merged_sessions = old_sessions
                if source_session and source_session not in old_sessions:
                    merged_sessions = (old_sessions + "," + source_session).strip(",")

                # ── 验证管线: 合并后仍标记 pending，等待交叉检查 ──
                old_prov = best.get("provenance") or "{}"
                old_verif = {}
                try:
                    old_verif = json.loads(old_prov) if isinstance(old_prov, str) else old_prov
                except (json.JSONDecodeError, TypeError):
                    old_verif = {}
                # 如果之前已验证过，保留验证状态但标记需要重新验证
                if old_verif.get("v") in ("verified", "degraded"):
                    old_verif["v"] = "pending"  # 内容更新了，需重新验证
                    old_verif["prev_v"] = old_verif.get("v")
                else:
                    old_verif = {"v": "pending"}
                new_prov = json.dumps(old_verif, ensure_ascii=False)

                # ── 合并内容: 保留旧知识 + 拼接新知识（防止覆盖丢失） ──
                old_content = best.get("content") or ""
                if old_content and content and content not in old_content and old_content not in content:
                    merged_content = old_content + " ||| " + content
                else:
                    merged_content = content or old_content

                with self._tx():
                    self._conn.execute("""
                        UPDATE memories SET
                            content=?, importance=?, accessed_at=datetime('now'),
                            updated_at=datetime('now'), source_session=?,
                            tags=?, category=?, decay_profile=?, provenance=?
                        WHERE id=?
                    """, (merged_content, new_imp, merged_sessions, ",".join(sorted(tags)),
                          category, None, new_prov, best["id"]))
                merged = True
                logger.info("[learn] merged into existing key=%s (fusion=%.3f, sim=%.3f, entity_overlap=%d, imp=%.2f→%.2f, verify=pending)",
                            old_key, matches[0][0], matches[0][1], matches[0][2], old_imp, new_imp)
                return {"key": old_key, "category": category, "importance": new_imp,
                        "tags": ",".join(sorted(tags)), "merged": True,
                        "sim": round(matches[0][1], 3), "fusion": round(matches[0][0], 3)}

        # 兜底: 嵌入离线 + 实体去重
        if not qvec and new_entities and len(new_entities) >= 3:
            rows = self._conn.execute(
                "SELECT id, key, content, importance, source_session, entities FROM memories WHERE entities IS NOT NULL AND entities != 'null'"
            ).fetchall()
            matches = []
            for r in rows:
                overlap = _entity_overlap(new_entities, r["entities"])
                if overlap >= 3:
                    matches.append((overlap, dict(r)))
            if matches:
                matches.sort(key=lambda x: x[0], reverse=True)
                best = matches[0][1]
                old_key = best["key"]
                old_imp = best["importance"] or 0.5
                new_imp = min(max(imp, old_imp + 0.05), 0.98)
                old_sessions = best.get("source_session") or ""

                merged_sessions = old_sessions
                if source_session and source_session not in old_sessions:
                    merged_sessions = (old_sessions + "," + source_session).strip(",")

                # ── 验证管线: 实体去重合并后也标记 pending ──
                old_prov = best.get("provenance") or "{}"
                try:
                    old_verif = json.loads(old_prov) if isinstance(old_prov, str) else old_prov
                except (json.JSONDecodeError, TypeError):
                    old_verif = {}
                if old_verif.get("v") in ("verified", "degraded"):
                    old_verif["v"] = "pending"
                    old_verif["prev_v"] = old_verif.get("v")
                else:
                    old_verif = {"v": "pending"}
                new_prov = json.dumps(old_verif, ensure_ascii=False)

                # ── 合并内容: 保留旧知识 + 拼接新知识 ──
                old_content = best.get("content") or ""
                if old_content and content and content not in old_content and old_content not in content:
                    merged_content = old_content + " ||| " + content
                else:
                    merged_content = content or old_content

                with self._tx():
                    self._conn.execute("""
                        UPDATE memories SET
                            content=?, importance=?, accessed_at=datetime('now'),
                            updated_at=datetime('now'), source_session=?,
                            tags=?, category=?, decay_profile=?, provenance=?
                        WHERE id=?
                    """, (merged_content, new_imp, merged_sessions, ",".join(sorted(tags)),
                          category, None, new_prov, best["id"]))
                merged = True
                logger.info("[learn] entity-only merge into key=%s (overlap=%d, imp=%.2f→%.2f, verify=pending)",
                            old_key, matches[0][0], old_imp, new_imp)
                return {"key": old_key, "category": category, "importance": new_imp,
                        "tags": ",".join(sorted(tags)), "merged": True,
                        "sim": 0, "fusion": round(matches[0][0] / 5.0, 3)}

        # ---- No match found — create new entry ----
        short = content[:30].replace(" ","_").replace("\n","_")
        ts = datetime.now().strftime("%m%d")
        key = "%s/%s_%s" % (category, ts, short[:20])
        if self._conn.execute("SELECT id FROM memories WHERE key=?", (key,)).fetchone():
            key = "%s_%d" % (key, int(datetime.now().timestamp() % 1000))
        # ── 验证管线: 新条目标记 pending_verification ──
        init_prov = json.dumps({"v": "pending"}, ensure_ascii=False)
        self.set(key, content, category=category, tags=",".join(sorted(tags)),
                 importance=imp, embed=True, source_session=source_session,
                 vector=qvec)
        # set() 不传 provenance，这里补写（_tx 保证事务闭合）
        with self._tx():
            self._conn.execute("UPDATE memories SET provenance=? WHERE key=?", (init_prov, key))
        return {"key": key, "category": category, "importance": imp,
                "tags": ",".join(sorted(tags)), "merged": False, "verify": "pending"}
