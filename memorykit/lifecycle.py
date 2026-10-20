#!/usr/bin/env python3
"""记忆生命周期模块 — 衰减/去重/冲突/学习。MemoryLifecycle mixin class。"""
import json
import logging
import os
import re
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .anchoring import classify_relation

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


def _parse_ts(value) -> datetime | None:
    """宽松解析时间戳：'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'；失败返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _critical_merge_mismatch(old_content: str, new_content: str) -> bool:
    """P0 保守否决：两条文本在关键事实（数字/单位/否定）上明显冲突时不许合并。

    背景（2026-10-20）：`learn` 的向量闸门是 fusion>=0.85，`deduplicate`
    默认 0.92；bge-m3 对 "显卡550" vs "显卡650" 的余弦实测 0.9434，
    对 "2GB" vs "2048MB" 0.9178，对 "Python 3.10" vs "3.12" 0.9314。
    光靠余弦会把"改了数字/单位/版本/否定"的两条记忆合并，属于误合并。

    这里只做**否决**（不改 _fusion_score 公式，不参与打分）：
      - 数字冲突：两边数字集合都非空，且各自都有对方没有的数字
        （{550} vs {650}、{2} vs {2048}、{3.10} vs {3.12}）。
        一边是另一边的子集（旧 550 + 新增显存 12G）→ 不否决，允许补充。
      - 单位冲突：数字相同但单位集合不同（2GB vs 2MB、550块 vs 550元）。
      - 否定冲突：一边有明确否定词、另一边没有
        （"不能上线" vs "可以上线"）。
    宁可少合并（新条目另存），不可错合并（内容被折叠后难以还原）。
    """
    a = str(old_content or "")
    b = str(new_content or "")
    if not a or not b or a == b:
        return False

    nums_a = set(re.findall(r"\d+(?:\.\d+)?", a))
    nums_b = set(re.findall(r"\d+(?:\.\d+)?", b))
    if nums_a and nums_b:
        only_a = nums_a - nums_b
        only_b = nums_b - nums_a
        if only_a and only_b:
            return True

    # 单位只在"数字完全相同"时用来区分同数不同单位；数字不同由上面管。
    unit_re = re.compile(
        r"(?i)(?:\b(?:gb|mb|tb|kb|ghz|mhz|mah|kg|km|cm|mm|ml|hz|ms)\b"
        r"|(?<![a-z])(?:g|m|t|v|w|a)(?![a-z])"
        r"|℃|°c|%|％|块|元|美元|小时|分钟|天|年|月|日)")
    units_a = {m.group(0).lower() for m in unit_re.finditer(a)}
    units_b = {m.group(0).lower() for m in unit_re.finditer(b)}
    if nums_a and nums_a == nums_b and units_a and units_b and units_a != units_b:
        return True

    neg_re = re.compile(
        r"(?:不能|不会|不再|不是|没有|无法|禁止|不支持|不可|"
        r"未通过|未完成|未验证|别用|\bnot\b|\bnever\b|\bno\b|"
        r"cannot|can't|don't|doesn't)", re.I)
    if bool(neg_re.search(a)) != bool(neg_re.search(b)):
        return True
    return False


# ── P1 合并决策单点（2026-10-20）─────────────────────────────────────────
# 所有合并路径（learn 快路径 / learn JSON 慢路径 / 实体兜底 / deduplicate）
# 都只调 _merge_decision()，不允许在调用点自己写 `if fusion >= ...`。
#
# 两个 flag（默认都 off = 完全复刻 P0 的 if，回归安全线）:
#   MERGE_SAFETY_V2   off | shadow | on
#     off    → 不走矩阵，P0 原样（fusion>=0.85 / overlap>=3 / sim>=threshold，
#              数字/单位/否定冲突否决）；anchor 结果不参与决策。
#     shadow → 走矩阵，但灰带 anchor 否决只记 would_veto，不改变合并。
#     on     → 走矩阵，灰带否决在 ANCHOR_GRAY_BAND=veto 时真正生效。
#   ANCHOR_GRAY_BAND  off | shadow | veto
#     off    → 不分类（省掉 LLM/检索；灰带候选按 vector_only 合并）。
#     shadow → 灰带分类，would_veto 记录但不否决。
#     veto   → 灰带分类，显式"无关"且 anchored_key 命中当前候选时否决
#              （需 MERGE_SAFETY_V2=on 才真正生效；否则降级为 shadow）。
#
# 阈值参数化：灰带 = [floor, threshold)，threshold 默认 0.92；
# deduplicate 把自己的 threshold 实参传进来（0.85 的调用无灰带，行为不变）。
MERGE_FLOOR = 0.85
MERGE_HIGH_THRESHOLD = 0.92

SOURCE_VECTOR = "vector"
SOURCE_VECTOR_JSON = "vector_json"
SOURCE_ENTITY = "entity_fallback"
SOURCE_DEDUPE = "dedupe"
_MERGE_SOURCES = (SOURCE_VECTOR, SOURCE_VECTOR_JSON, SOURCE_ENTITY, SOURCE_DEDUPE)

ACTION_MERGE = "MERGE"
ACTION_KEEP = "KEEP"

MERGE_SAFETY_ENV = "MERGE_SAFETY_V2"
ANCHOR_GRAY_BAND_ENV = "ANCHOR_GRAY_BAND"
MERGE_SAFETY_MODES = ("off", "shadow", "on")
ANCHOR_GRAY_BAND_MODES = ("off", "shadow", "veto")

# 关系判定受控词表（anchoring.py）+ 降级态。只有显式"无关"才有资格否决；
# unknown = 无候选 / LLM 失败 / 解析失败 / 未知标签 / 规则兜底的否定。
REL_CONTINUE = "延续"
REL_CORRECT = "修正"
REL_UNRELATED = "无关"
REL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MergePolicy:
    """合并安全开关的解析结果（一次 learn/dedupe 读一次 env）。"""

    mode: str = "off"
    anchor_mode: str = "off"

    @property
    def matrix(self) -> bool:
        """是否走 P1 矩阵（off = P0 复刻）。"""
        return self.mode != "off"

    @property
    def classify_gray(self) -> bool:
        """灰带是否值得调锚定分类（off = 惰性跳过，省 LLM）。"""
        return self.anchor_mode != "off"

    @property
    def veto_applies(self) -> bool:
        """灰带否决是否真正生效（要求矩阵 on + 灰带 veto）。"""
        return self.mode == "on" and self.anchor_mode == "veto"


@dataclass(frozen=True)
class MergeDecision:
    """_merge_decision() 的唯一输出。合并/去重所有路径共用。"""

    action: str                 # MERGE | KEEP
    reason: str                 # 见 _merge_decision docstring
    source: str                 # vector | vector_json | entity_fallback | dedupe
    mode: str                   # off | shadow | on
    anchor_mode: str            # off | shadow | veto
    sim: float
    fusion: float
    overlap: int
    boost: float
    threshold: float
    floor: float
    candidate_key: str
    relation: str | None
    anchored_key: str | None
    gray_band: bool
    # 灰带否决条件成立（显式无关 + anchored_key 命中当前候选）。
    # shadow 下 = "本应否决"，on 下 = "已否决"；用 veto_applied 区分。
    would_veto: bool
    veto_applied: bool
    # fusion>=threshold 时 anchor 与向量结论分歧（仅供审计；惰性执行下
    # 生产路径不会分类，见 REPORT 说明）。
    anchor_disagrees: bool
    critical_mismatch: bool


def _read_merge_mode(name: str, valid: tuple, default: str = "off") -> str:
    """读三档 flag。空/非法值 → default（off）+ warning，绝不猜。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in valid:
        logger.warning("[merge] 非法 %s=%r（可选 %s）→ 按 %s 处理",
                       name, raw, "/".join(valid), default)
        return default
    return raw


def read_merge_policy() -> MergePolicy:
    """解析两个合并安全 flag（默认 off/off）。"""
    return MergePolicy(
        mode=_read_merge_mode(MERGE_SAFETY_ENV, MERGE_SAFETY_MODES),
        anchor_mode=_read_merge_mode(ANCHOR_GRAY_BAND_ENV, ANCHOR_GRAY_BAND_MODES),
    )


def _fusion_score(sim, overlap):
    """向量+图解信号融合（P0 公式原样搬到模块级：learn 与 shadow 脚本共用）。

    乘性 boost：overlap>=2 → 1.0+0.05*min(overlap,5)（2→1.10, 5→1.25），
    fusion = min(sim*boost, 1.0)。只在 >=2 时放大，日志打全 sim/overlap/boost。
    """
    if overlap >= 2:
        boost = 1.0 + 0.05 * min(overlap, 5)
        fusion = min(sim * boost, 1.0)
    else:
        boost = 1.0
        fusion = sim
    logger.debug("[learn] fusion sim=%.4f overlap=%d boost=%.4f fusion=%.4f",
                 sim, overlap, boost, fusion)
    return fusion


def _merge_decision(*, source: str, sim: float, fusion: float, overlap: int,
                    candidate_key: str, relation: str | None = None,
                    anchored_key: str | None = None,
                    old_content: str = "", new_content: str = "",
                    threshold: float = MERGE_HIGH_THRESHOLD,
                    floor: float = MERGE_FLOOR,
                    mode: str | None = None,
                    anchor_mode: str | None = None) -> MergeDecision:
    """唯一合并判定点（P1，2026-10-20）。

    矩阵（fusion = sim * entity_boost；overlap = 实体重叠数）:

      fusion < floor                         → KEEP  below_floor
      floor <= fusion < threshold, 无关+命中  → 灰带否决（shadow 记 would_veto）
      floor <= fusion < threshold, 延续/修正  → MERGE anchor_continued/corrected
      floor <= fusion < threshold, 其它/未知  → MERGE vector_only
      fusion >= threshold                    → MERGE high_similarity
                                               （anchor 不否决，只记 anchor_disagrees）
      source=entity_fallback, overlap>=3     → MERGE entity_only
      source=dedupe, sim>=threshold          → MERGE dedupe
      任一来源，关键事实冲突                  → KEEP critical_mismatch

    P0 兼容：mode=off 时只保留 P0 的三条 if（fusion>=floor / overlap>=3 /
    sim>=threshold）+ 关键事实否决，reason 也保持 P0 字面值
    （vector_only / entity_only / dedupe），保证 merge_events 逐字节一致。

    anchor_disagrees 只在 relation 由调用方显式给出时才可能为 True：
    生产惰性路径在 fusion>=threshold 时根本不分类，因此该字段在生产中
    实际恒为 False，仅作审计预留（REPORT-P1 有说明）。
    """
    if source not in _MERGE_SOURCES:
        raise ValueError("unknown merge source: %r" % (source,))
    if mode is None or anchor_mode is None:
        _p = read_merge_policy()
        mode = _p.mode if mode is None else mode
        anchor_mode = _p.anchor_mode if anchor_mode is None else anchor_mode

    sim = float(sim)
    fusion = float(fusion)
    threshold = float(threshold)
    floor = float(floor)
    overlap = int(overlap)
    boost = (fusion / sim) if sim > 0 else 1.0
    critical = _critical_merge_mismatch(old_content, new_content)
    gray_band = (source in (SOURCE_VECTOR, SOURCE_VECTOR_JSON)
                 and floor <= fusion < threshold)
    # 唯一有资格否决的形态：显式"无关" + anchored_key 正好是当前候选。
    # 无候选/失败/未知标签都归一成 unknown，不会落到这里（三态化的意义）。
    veto_condition = bool(
        relation == REL_UNRELATED and anchored_key
        and str(anchored_key) == str(candidate_key))
    applied = (mode == "on" and anchor_mode == "veto")

    def _decide(action: str, reason: str, *, would_veto: bool = False,
                veto_applied: bool = False,
                anchor_disagrees: bool = False) -> MergeDecision:
        return MergeDecision(
            action=action, reason=reason, source=source, mode=mode,
            anchor_mode=anchor_mode, sim=sim, fusion=fusion,
            overlap=overlap, boost=boost, threshold=threshold, floor=floor,
            candidate_key=candidate_key, relation=relation,
            anchored_key=anchored_key, gray_band=gray_band,
            would_veto=would_veto, veto_applied=veto_applied,
            anchor_disagrees=anchor_disagrees,
            critical_mismatch=critical)

    if critical:
        return _decide(ACTION_KEEP, "critical_mismatch")

    if source == SOURCE_ENTITY:
        if overlap >= 3:
            return _decide(ACTION_MERGE, "entity_only")
        return _decide(ACTION_KEEP, "entity_overlap_lt3")

    if source == SOURCE_DEDUPE:
        if sim >= threshold:
            return _decide(ACTION_MERGE, "dedupe")
        return _decide(ACTION_KEEP, "below_threshold")

    # 向量路径（vector / vector_json）
    if fusion < floor:
        return _decide(ACTION_KEEP, "below_floor")

    if mode == "off":
        # P0 原样：只看 fusion>=floor，anchor 不参与，reason 保持 vector_only。
        return _decide(ACTION_MERGE, "vector_only")

    if fusion >= threshold:
        return _decide(ACTION_MERGE, "high_similarity",
                       anchor_disagrees=veto_condition)

    # 灰带：唯一允许 anchor 干预的区间
    if relation == REL_CONTINUE:
        return _decide(ACTION_MERGE, "anchor_continued")
    if relation == REL_CORRECT:
        return _decide(ACTION_MERGE, "anchor_corrected")
    if veto_condition:
        if applied:
            return _decide(ACTION_KEEP, "gray_band_veto",
                           would_veto=True, veto_applied=True)
        # shadow：记 would_veto，但保持旧行为（合并）
        return _decide(ACTION_MERGE, "vector_only", would_veto=True)
    return _decide(ACTION_MERGE, "vector_only")


def _anchor_gray_band(new_content, matches, *, anchor: bool, anchor_llm,
                      anchor_top_k: int, policy: MergePolicy,
                      threshold: float = MERGE_HIGH_THRESHOLD,
                      floor: float = MERGE_FLOOR):
    """P1-3 灰带惰性锚定：只有灰带候选存在时才调 classify_relation()。

    参数 matches = [(fusion, sim, overlap, row_dict), ...]（已过 floor 的候选）。
    返回 (anchor_info | None, derived_from | None)。

    没有灰带候选（或 anchor=False / ANCHOR_GRAY_BAND=off）→ 直接返回 None，
    一次检索/LLM 都不做 —— "惰性真的省了"的落点。
    """
    if not anchor or not policy.classify_gray or not matches:
        return None, None

    gray = [m for m in matches if floor <= float(m[0]) < float(threshold)]
    if not gray:
        return None, None
    gray.sort(key=lambda m: m[0], reverse=True)

    cands = []
    for _fusion, _sim, _overlap, _row in gray[:max(1, int(anchor_top_k))]:
        cands.append({
            "key": _row.get("key"),
            "id": _row.get("id"),
            "content": _row.get("content") or "",
            "similarity": round(float(_sim), 6),
            "entity_overlap": int(_overlap),
        })

    try:
        rel = classify_relation(new_content, cands, llm=anchor_llm)
    except Exception as e:  # noqa: BLE001 — 锚定绝不阻塞写入
        logger.warning("[learn] anchoring failed: %s", type(e).__name__)
        rel = {"relation": REL_UNKNOWN, "anchored_key": None,
               "anchored_id": None, "reason": "error:" + type(e).__name__,
               "source": "error", "model": None, "degraded": True}

    anchor_info = {
        "relation": rel.get("relation"),
        "anchored_key": rel.get("anchored_key"),
        "anchored_id": rel.get("anchored_id"),
        "reason": rel.get("reason"),
        "source": rel.get("source"),
        "model": rel.get("model"),
        "degraded": bool(rel.get("degraded")),
    }
    derived_from = None
    if rel.get("relation") in (REL_CONTINUE, REL_CORRECT) and rel.get("anchored_key"):
        derived_from = rel.get("anchored_key")
    logger.info("[learn] anchor(gray-band): relation=%s key=%s source=%s degraded=%s",
                anchor_info["relation"], anchor_info["anchored_key"],
                anchor_info["source"], anchor_info["degraded"])
    return anchor_info, derived_from


# learn() 预计算向量哨兵（learn_batch 批量路径用）：
#   不传 precomputed_vec            → 单条原行为（learn 内部 _embed 嵌入）
#   显式传 precomputed_vec=<向量>   → 跳过内部嵌入，直接用预计算向量
#   显式传 precomputed_vec=None     → 跳过内部嵌入（批量嵌入已失败 → 离线降级，
#                                      走实体去重兜底，不二次嵌入）
# 用哨兵而非 None 作默认，是为了区分「未提供」（需要嵌入）与
# 「批量已尝试且失败」（不应再嵌入）两种语义。
_NO_PRECOMPUTED_VEC = object()

# learn() 预计算 sparse 哨兵（learn_batch 批量路径用，语义同 precomputed_vec）：
#   不传 precomputed_sparse            → 单条路径（learn 内部 _embed_with_sparse 自取）
#   显式传 precomputed_sparse=<dict>   → 直接用预计算 sparse（批量已嵌入）
#   显式传 precomputed_sparse=None     → 无 sparse（批量嵌入失败/服务不支持），
#                                        降级纯 dense，不二次请求
_NO_PRECOMPUTED_SPARSE = object()


class MemoryLifecycle:
    """Mixin: lifecycle methods for MemoryDB."""

    DECAY_TAU = {"slow": 90, "medium": 30, "fast": 7}
    # ── event_time merge 保护（memorykit v2）──
    # 旧条目已有 event_time 且新 event_time 与之相距超过该阈值（天）时，
    # 视为两次独立事件（内容相似只是巧合/演进），合并时保留旧 event_time 不覆盖。
    MERGE_EVENT_TIME_MAX_GAP_DAYS = 7
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
            "FROM memories WHERE scope='permanent' "
            "AND COALESCE(status,'active') != 'merged'"
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
                            self._conn.execute(
                                "DELETE FROM memories WHERE key=? "
                                "AND COALESCE(status,'active') != 'merged'",
                                (r["key"],))
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

    def deduplicate(self, threshold=0.92, max_seconds=15.0, dry_run=False):
        """语义去重: 合并相似度 >= threshold 的条目（P0 数据保全版, 2026-10-20）。

        与旧实现的区别（旧实现 = 截断 200 字 + 物理 DELETE + 只跑一遍贪心）:

        1. **不丢数据**: 被合并行不 DELETE, 改 `status='merged'` +
           `merged_into=保留行 key` + `merged_at`; 全文保留, 可 `get/peek`
           按 key 找回。保留行拼接被合并行**全文**（不再 `content[:200]`）。
        2. **可审计**: 每个 (keep, remove) 写一行 `merge_events`, 含双方
           合并前后全文、sim/overlap/boost、decision/reason。
        3. **幂等**: 用相似度图的连通分量做一次性归并（不再"首中即 break"），
           一轮后所有活跃行两两相似度都 < threshold；连跑第二次 merged=0。
           传递闭包可能把 A~B~C 归成一组——这是"相似就合并"的语义代价,
           但全文/审计行都在, 可人工回滚。
        4. **dry_run**: 只算计划, 不 UPDATE、不 INSERT merge_events。

        候选: vector IS NOT NULL 且 status 不是 merged 且 vector_stale=0。
        vector_stale=1 表示内容已变但重嵌失败, 旧向量不可信, 不能参与
        相似度判定（等 dirty 重嵌后再入池）。

        返回字段:
          merged      本次（或 dry_run 计划）折叠次数
          soft_merged 同 merged（语义名, 区别于物理删除）
          removed     物理删除数, 恒为 0
          pairs       [(keep_key, remove_key), ...]
          groups      发生合并的连通分量数
          dry_run     是否干跑
        """
        t_start = time.monotonic()
        # P1-2: dedupe 也接唯一决策点（flag 一次读，逐对复用）
        policy = read_merge_policy()
        rows = self._conn.execute(
            "SELECT id, key, content, importance, vector, vector_bin, sparse, "
            "status, vector_stale FROM memories "
            "WHERE vector IS NOT NULL "
            "AND COALESCE(status,'active') != 'merged' "
            "AND COALESCE(vector_stale,0) = 0 "
            "ORDER BY id"
        ).fetchall()
        n = len(rows)

        # ── 1) 每行向量只解析一次；优先 vector_bin（二进制）再回退 JSON ──
        skipped = False
        skip_reason = None
        vetoed = 0  # 相似度够但关键事实冲突（数字/单位/否定）被否决的对数
        vecs = [None] * n
        for idx in range(n):
            if (max_seconds is not None and idx and (idx % 512 == 0)
                    and (time.monotonic() - t_start) > max_seconds):
                skipped = True
                skip_reason = "max_seconds_exceeded_during_vector_parse"
                break
            raw_bin = rows[idx]["vector_bin"]
            arr = None
            if raw_bin is not None:
                try:
                    if len(raw_bin) % 4 == 0 and len(raw_bin) > 0:
                        arr = np.frombuffer(raw_bin, dtype=np.float32)
                except Exception:
                    arr = None
            if arr is None:
                raw = rows[idx]["vector"]
                if not raw:
                    continue
                try:
                    v = json.loads(raw)
                except Exception:
                    continue  # 畸形 JSON：跳过该行
                if not isinstance(v, (list, tuple)) or len(v) == 0:
                    continue  # 空向量/非向量：不参与
                try:
                    arr = np.asarray(v, dtype=np.float32)
                except Exception:
                    continue
            if arr is None or arr.ndim != 1 or arr.size == 0:
                continue
            vecs[idx] = arr

        if skipped:
            logger.warning(
                "deduplicate skipped (audit): reason=%s threshold=%s rows=%d "
                "elapsed=%.3fs; no writes applied, retry next window",
                skip_reason, threshold, n, time.monotonic() - t_start)
            return {"merged": 0, "soft_merged": 0, "removed": 0, "pairs": [],
                    "groups": 0, "vetoed": 0, "dry_run": bool(dry_run),
                    "skipped": True, "reason": skip_reason, "n_vectors": 0,
                    "elapsed_s": round(time.monotonic() - t_start, 3)}

        valid = [i for i in range(n) if vecs[i] is not None]

        # ── 2) 相似度图：所有 >= threshold 的边做 union-find ──
        # 按向量维度分组（库里可能混有 1024d/2048d；跨维度余弦无意义）。
        EPS = 1e-4  # 仅用于筛候选（覆盖 BLAS 舍入）；最终判定仍用精确余弦
        parent = list(range(n))

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        by_dim = {}
        for i in valid:
            by_dim.setdefault(int(vecs[i].size), []).append(i)

        for _dim, idxs in by_dim.items():
            if len(idxs) < 2:
                continue
            X = np.stack([vecs[i] for i in idxs]).astype(np.float32)
            norms = np.linalg.norm(X, axis=1)
            for i in idxs:
                if max_seconds is not None and (time.monotonic() - t_start) > max_seconds:
                    skipped = True
                    skip_reason = "max_seconds_exceeded_during_similarity_scan"
                    break
                vi = vecs[i]
                ni = float(np.linalg.norm(vi))
                if ni == 0.0:
                    continue  # 零向量永不合并
                with np.errstate(divide="ignore", invalid="ignore"):
                    sims = (X @ vi) / (norms * ni)
                cand = np.nonzero(sims >= (threshold - EPS))[0]
                for pos in cand:
                    j = idxs[int(pos)]
                    if j <= i:
                        continue
                    pair_sim = self._cosine_similarity(vi, vecs[j])
                    if pair_sim < threshold:
                        continue
                    # P1-2/P1-4：dedupe 也走唯一决策点（关键事实否决已并入）。
                    # floor=threshold → 灰带为空，dedupe 天然不走 anchor（同阈值同写回）。
                    pair_decision = _merge_decision(
                        source=SOURCE_DEDUPE, sim=pair_sim, fusion=pair_sim,
                        overlap=0, candidate_key=rows[j]["key"],
                        old_content=rows[i]["content"],
                        new_content=rows[j]["content"],
                        threshold=threshold, floor=threshold,
                        mode=policy.mode, anchor_mode=policy.anchor_mode)
                    if pair_decision.critical_mismatch:
                        vetoed += 1
                    if pair_decision.action != ACTION_MERGE:
                        continue
                    _union(i, j)
            if skipped:
                break

        if skipped:
            logger.warning(
                "deduplicate skipped (audit): reason=%s threshold=%s n_vectors=%d "
                "elapsed=%.3fs; no writes applied, retry next window",
                skip_reason, threshold, len(valid), time.monotonic() - t_start)
            return {"merged": 0, "soft_merged": 0, "removed": 0, "pairs": [],
                    "groups": 0, "vetoed": vetoed, "dry_run": bool(dry_run),
                    "skipped": True, "reason": skip_reason, "n_vectors": len(valid),
                    "elapsed_s": round(time.monotonic() - t_start, 3)}

        # ── 3) 每个连通分量选一个保留行（importance 最高, 平票取小 id）──
        groups = {}
        for i in valid:
            groups.setdefault(_find(i), []).append(i)

        merge_ops = []
        keep_final = {}
        merged_groups = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            merged_groups += 1
            keep_idx = min(
                members,
                key=lambda x: (-(rows[x]["importance"] or 0.0), rows[x]["id"]))
            keep_key = rows[keep_idx]["key"]
            cur_content = rows[keep_idx]["content"] or ""
            cur_imp = rows[keep_idx]["importance"] or 0.0

            for m in sorted(members, key=lambda x: rows[x]["id"]):
                if m == keep_idx:
                    continue
                mc = rows[m]["content"] or ""
                old_content = cur_content
                if mc and mc not in cur_content:
                    cur_content = (cur_content + "\n\n[合并自: %s]\n%s"
                                   % (rows[m]["key"], mc))
                elif not cur_content:
                    cur_content = mc
                cur_imp = max(cur_imp, rows[m]["importance"] or 0.0)
                sim = self._cosine_similarity(vecs[keep_idx], vecs[m])
                merge_ops.append({
                    "keep_key": keep_key,
                    "remove_key": rows[m]["key"],
                    "old_content": old_content,
                    "new_content": mc,
                    "merged_content": cur_content,
                    "sim": max(-1.0, min(1.0, float(sim))),
                    "overlap": 0,
                    "boost": 1.0,
                    "relation": None,
                    "anchored_key": None,
                    "decision": "MERGE",
                    # 连通分量可能传递合并：成员与保留行的直接 sim 可能低于
                    # threshold（A~B~C, A~C 低于阈值）。如实标注，避免审计
                    # 行看起来像"低于阈值的合并"。
                    "reason": ("dedupe" if sim >= threshold
                               else "dedupe_transitive"),
                    "would_veto": 0,
                })
            keep_final[keep_key] = (cur_content, cur_imp)

        pairs = [(op["keep_key"], op["remove_key"]) for op in merge_ops]

        # ── 4) 写库（dry_run 跳过）──
        # P0 补漏（2026-10-20）：dedupe 合并后必须重嵌。
        # 原实现只写 content/importance，vector 停在旧内容上 —— 语义检索继续用
        # 旧向量匹配新内容，越合并越偏（与 learn 路径的 _merge_writeback 不一致）。
        # 重嵌放在事务外（网络调用，不持锁）。
        vec_updates = {}
        if not dry_run:
            for keep_key, (content, _imp) in keep_final.items():
                old_row = self._conn.execute(
                    "SELECT id, content FROM memories WHERE key=?", (keep_key,)
                ).fetchone()
                if old_row and (old_row["content"] or "") != content:
                    nvec, nsparse = self._embed_with_sparse(content[:5000])
                    vec_updates[keep_key] = (nvec, nsparse, old_row["id"])

        if not dry_run:
            with self._tx():
                for op in merge_ops:
                    self._conn.execute(
                        "INSERT INTO merge_events"
                        "(keep_key, remove_key, old_content, new_content, "
                        " merged_content, sim, overlap, boost, relation, "
                        " anchored_key, decision, reason, would_veto, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                        (op["keep_key"], op["remove_key"], op["old_content"],
                         op["new_content"], op["merged_content"], op["sim"],
                         op["overlap"], op["boost"], op["relation"],
                         op["anchored_key"], op["decision"], op["reason"],
                         op["would_veto"]))
                for keep_key, (content, imp) in keep_final.items():
                    vu = vec_updates.get(keep_key)
                    if vu is None:
                        # 内容未变（或 dry_run）：向量不动
                        self._conn.execute(
                            "UPDATE memories SET content=?, importance=?, "
                            "updated_at=datetime('now','localtime') WHERE key=?",
                            (content, imp, keep_key))
                    elif vu[0]:
                        # 重嵌成功：新向量 + 解除 stale
                        nvec, nsparse, _mid = vu
                        self._conn.execute(
                            "UPDATE memories SET content=?, importance=?, "
                            "vector=?, vector_bin=?, sparse=?, vector_stale=0, "
                            "updated_at=datetime('now','localtime') WHERE key=?",
                            (content, imp, json.dumps(nvec),
                             struct.pack(f'{len(nvec)}f', *nvec),
                             json.dumps(nsparse) if nsparse else None, keep_key))
                    else:
                        # 重嵌失败：标记 stale（旧向量不再可信），写 dirty 等重嵌
                        nvec, nsparse, mid = vu
                        self._conn.execute(
                            "UPDATE memories SET content=?, importance=?, "
                            "vector_stale=1, "
                            "updated_at=datetime('now','localtime') WHERE key=?",
                            (content, imp, keep_key))
                        self._signal_dirty(keep_key, mid)
                        logger.warning(
                            "[dedupe] 合并后重嵌失败 key=%s → vector_stale=1", keep_key)
                for op in merge_ops:
                    self._conn.execute(
                        "UPDATE memories SET status='merged', merged_into=?, "
                        "merged_at=datetime('now','localtime'), "
                        "updated_at=datetime('now','localtime') "
                        "WHERE key=? AND COALESCE(status,'active') != 'merged'",
                        (op["keep_key"], op["remove_key"]))

        return {"merged": len(merge_ops), "soft_merged": len(merge_ops),
                "removed": 0, "pairs": pairs, "groups": merged_groups,
                "vetoed": vetoed, "dry_run": bool(dry_run), "skipped": False,
                "reason": None, "n_vectors": len(valid),
                "elapsed_s": round(time.monotonic() - t_start, 3)}

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
                "UPDATE memories SET scope='archived' WHERE importance < 0.1 "
                "AND scope='permanent' AND COALESCE(status,'active') != 'merged'"
            ).rowcount
            d2 = self._conn.execute(
                "DELETE FROM memories WHERE scope IN ('temp','session') AND ttl IS NOT NULL "
                "AND (julianday('now')-julianday(created_at))*86400 >= ttl "
                "AND COALESCE(status,'active') != 'merged'"
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
    #    "fallback": true,                  # state.db 缺失/无源 → 库内轻量验证
    #                                       # （method: similarity|time_anchor|
    #                                       #   entity_density，可 "+" 组合）
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

    # ── 遗忘治理壳（P1 #6，2026-08-08）────────────────────────────────────
    # 不动 age() 的状态机（衰减是护城河），外面包"可审计 + 可回滚 + dry-run"：
    #   forgetting_report() 只读分类 → forgetting(dry_run=True) 出计划 →
    #   forgetting() apply 记审计（before/after 快照 + batch_id）→ rollback() 整批还原。
    # 契约对齐 scope-recall forgetting 的"先出报告再执行，默认只软归档"，
    # 实现是 ours（审计表在 memory.db 内，非独立 governance 库）。

    _SECRET_PATTERNS = (
        r"sk-[a-zA-Z0-9]{16,}", r"api[_-]?key[\"']?\s*[:=]\s*[\"'][a-zA-Z0-9]{16,}",
        r"password[\"']?\s*[:=]\s*[\"'][^\s\"']+", r"token[\"']?\s*[:=]\s*[\"'][a-zA-Z0-9]{16,}",
    )

    def forgetting_report(self, review_debt_days: int = 7,
                          duplicate_window: int = 20) -> dict:
        """只读扫描分类遗忘候选（不写库）。

        分类规则（照 forgetting.py 语义，去掉 scope 维度）：
        - soft_archive: water>=2（可自动淘汰）且（degraded/disputed/STALE 或
          importance<=0.15）——低价值/已降级，软归档不物理删
        - hard_delete: 内容命中 secret 模式（api key/token/password 等）或
          极短噪音（<8 字符）——敏感或纯噪声才物理删
        - review_debt: verification_status ∈ {pending, unverified} 且
          created_at 超过 review_debt_days——待复核，不删挂起
        - duplicate_groups: 同 category 且 key 前缀（category/MMDD_前 12 字符）
          相同——疑似重复组（learn 去重失败残留）

        返回 {"soft_archive": [id...], "hard_delete": [id...],
              "review_debt": [id...], "duplicate_groups": [[id...], ...]}
        """
        import json as _json
        rows = self._conn.execute(
            "SELECT id, key, category, content, importance, water, "
            "verification_status, created_at, accessed_at FROM memories "
            "WHERE COALESCE(status,'active') != 'merged'"
        ).fetchall()
        soft, hard, debt = [], [], []
        dup_groups: dict = {}
        for r in rows:
            content = r["content"] or ""
            cat = r["category"] or ""
            # hard_delete: secret 模式 / 极短噪音
            import re as _re
            is_secret = any(_re.search(p, content, _re.IGNORECASE)
                            for p in self._SECRET_PATTERNS)
            if is_secret or (len(content.strip()) < 8):
                hard.append(r["id"])
                continue
            # review_debt: pending/unverified 且老
            if r["verification_status"] in ("pending", "unverified") and r["created_at"]:
                try:
                    from datetime import datetime as _dt
                    age_days = (_dt.now() - _dt.strptime(
                        r["created_at"][:19], "%Y-%m-%d %H:%M:%S")).days
                except ValueError:
                    age_days = 0
                if age_days >= review_debt_days:
                    debt.append(r["id"])
                    continue
            # soft_archive
            if (r["water"] or 0) >= 2 and (
                    r["verification_status"] in ("degraded", "disputed", "STALE")
                    or (r["importance"] or 0.5) <= 0.15):
                soft.append(r["id"])
                continue
            # duplicate_groups: 同 category + key 主体前缀相同
            key_prefix = str(r["key"] or "").split("_")[0] + "|" + cat
            dup_groups.setdefault(key_prefix, []).append(r["id"])
        dups = [ids for ids in dup_groups.values() if len(ids) > 1]
        return {
            "soft_archive": soft,
            "hard_delete": hard,
            "review_debt": debt,
            "duplicate_groups": dups,
        }

    def forgetting(self, dry_run: bool = True, delete: bool = False,
                   batch_id: str = None, limit: int = 200) -> dict:
        """遗忘治理执行：dry_run 只出计划；apply 记审计后可回滚。

        参数：
          dry_run: True（默认）只返回 forgetting_report() 分类计划，不写库
          delete:  False（默认）只软归档；True 才物理删 hard_delete 候选
          batch_id: 审计批次号（默认自动生成 YYYYMMDD-HHMMSS），同一批可整批回滚
          limit: 单批处理上限（soft_archive + hard_delete 合计）

        返回：
          dry_run: {"dry_run": True, "report": {...}, "plan": {...}}
          apply:   {"dry_run": False, "batch_id": ..., "soft_archived": N,
                    "hard_deleted": N, "audited": N}
        """
        import json as _json
        report = self.forgetting_report()
        if dry_run:
            return {"dry_run": True, "report": report,
                    "plan": {"soft_archive": len(report["soft_archive"]),
                             "hard_delete": len(report["hard_delete"]),
                             "review_debt": len(report["review_debt"]),
                             "duplicate_groups": len(report["duplicate_groups"])}}
        if batch_id is None:
            batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")

        targets = list(report["soft_archive"][:limit])
        if delete:
            targets += list(report["hard_delete"][:limit])
        soft_archived = hard_deleted = audited = 0
        for mid in targets:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            if not row:
                continue
            before = _json.dumps(dict(row), ensure_ascii=False, default=str)
            is_hard = delete and mid in report["hard_delete"]
            with self._tx():
                if is_hard:
                    self._conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                    action = "hard_delete"
                    hard_deleted += 1
                else:
                    self._conn.execute(
                        "UPDATE memories SET verification_status='ARCHIVED', "
                        "importance=0.05 WHERE id=?", (mid,))
                    action = "soft_archive"
                    soft_archived += 1
                self._conn.execute(
                    "INSERT INTO governance_audit_events"
                    "(batch_id, action, memory_id, memory_key, before, after) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, action, mid, row["key"], before,
                     _json.dumps({"verification_status": "ARCHIVED",
                                  "importance": 0.05}, ensure_ascii=False)))
                audited += 1
        return {"dry_run": False, "batch_id": batch_id,
                "soft_archived": soft_archived, "hard_deleted": hard_deleted,
                "audited": audited}

    def rollback(self, batch_id: str) -> dict:
        """按审计批次回滚 forgetting（before 快照还原行）。

        只回滚本批次记录过的 memory_id：
        - soft_archive 的 before 快照全行 → UPDATE 还原（verification_status/
          importance 等全部列）
        - hard_delete 的 before 快照 → INSERT 重建（id 保持原值）
        审计行保留（回滚记录也是审计的一部分）。

        返回 {"batch_id": ..., "restored": N, "not_found": N}
        """
        import json as _json
        rows = self._conn.execute(
            "SELECT id, action, memory_id, before FROM governance_audit_events "
            "WHERE batch_id=?", (batch_id,)).fetchall()
        restored = not_found = 0
        for r in rows:
            try:
                snap = _json.loads(r["before"]) if r["before"] else {}
            except (ValueError, TypeError):
                snap = {}
            if not snap:
                not_found += 1
                continue
            with self._tx():
                if r["action"] == "soft_archive":
                    cols = ", ".join(f"{k}=?" for k in snap)
                    self._conn.execute(
                        f"UPDATE memories SET {cols} WHERE id=?",
                        (*snap.values(), snap.get("id") or r["memory_id"]))
                elif r["action"] == "hard_delete":
                    cols = ", ".join(snap.keys())
                    ph = ", ".join("?" * len(snap))
                    self._conn.execute(
                        f"INSERT OR REPLACE INTO memories({cols}) VALUES({ph})",
                        tuple(snap.values()))
                restored += 1
        return {"batch_id": batch_id, "restored": restored,
                "not_found": not_found}

    # ── 轻量验证兜底（2026-08-07 小龙虾反馈 #4：验证闭环空转 vscore=0）──
    # OpenClaw 侧 bridge.py 调 MemoryDB 时 state_db_path 为 None（小龙虾库
    # 没有 state.db），_cross_check 永远拿不到源消息 → _maybe_auto_verify 恒
    # return "pending"、vscore 恒 0。这里提供 state.db 缺失/无源 session 时的
    # 库内轻量验证：不依赖外部源，只用记忆库自身信息给保守的非零分。
    # 只影响 confidence 与 provenance 展示，不参与去重/写入决策。
    def _lightweight_verify(self, content: str, exclude_key: str = None,
                            entities: list = None) -> dict:
        """轻量验证兜底：state.db 缺失 / 无源 session 时给非零 vscore。

        三个互不依赖的证据方案，取最高分：
          方案A similarity —— 库内检索与 content 高相似（cos>0.85，同维度）
            的其他记忆作为交叉证据：证据数 n → 0.3 + 0.15*(n-1)，封顶 0.7
          方案B time_anchor —— content 含绝对日期（YYYY-MM-DD / YYYY年M月D日
            等）且与 created_at/event_time 无矛盾（|Δ|≤400 天）→ 0.30
          方案C entity_density —— 实体数适中（2-8 个）且非空 → 0.25

        返回 {"score": 0-1, "method": "similarity|time_anchor|entity_density"
              （多方案 "+" 连接）, "evidence": {方案名: 详情}}；
        无任何证据 → {"score": 0.0, "method": "none", "evidence": {}}。
        纯读 + 轻量计算，任何异常内部消化，绝不抛给调用方。
        """
        result = {"score": 0.0, "method": "none", "evidence": {}}
        if not content or len(str(content).strip()) < 10:
            return result
        plans = {}

        # ── 方案A: 库内相似记忆一致性 ──
        try:
            import numpy as np  # noqa: PLC0415 — 与 learn() 本地导入一致
            own = None
            if exclude_key:
                own = self._conn.execute(
                    "SELECT vector_bin, vector FROM memories WHERE key=?",
                    (exclude_key,),
                ).fetchone()
            if own is not None:
                own_vec = None
                if own["vector_bin"] is not None:
                    own_vec = np.frombuffer(own["vector_bin"], dtype=np.float32)
                elif own["vector"]:
                    try:
                        dv = (json.loads(own["vector"])
                              if isinstance(own["vector"], str) else own["vector"])
                        own_vec = np.array(dv, dtype=np.float32)
                    except Exception:
                        own_vec = None
                if own_vec is not None and own_vec.size > 0 \
                        and np.linalg.norm(own_vec) > 0:
                    qdim = int(own_vec.size)
                    rows = self._conn.execute(
                        "SELECT key, vector_bin FROM memories "
                        "WHERE key != ? AND vector_bin IS NOT NULL",
                        (exclude_key,),
                    ).fetchall()
                    blobs = []
                    for r in rows:
                        if self._vector_dim_from_blob(r["vector_bin"]) == qdim:
                            blobs.append(r["vector_bin"])
                    evidence_n = 0
                    if blobs:
                        vecs = np.array([np.frombuffer(b, dtype=np.float32)
                                         for b in blobs])
                        sims = (np.dot(vecs, own_vec)
                                / np.maximum(
                                    np.linalg.norm(vecs, axis=1)
                                    * np.linalg.norm(own_vec), 1e-10))
                        evidence_n = int((sims > 0.85).sum())
                    if evidence_n > 0:
                        plans["similarity"] = (
                            round(min(0.7, 0.3 + 0.15 * (evidence_n - 1)), 3),
                            {"evidence_count": evidence_n})
        except Exception:
            pass  # 方案A失败不影响其余方案

        # ── 方案B: 时间锚定 ──
        try:
            m = re.search(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?", content)
            day = 1
            if not m:
                m = re.search(r"(\d{4})年(\d{1,2})月", content)
            elif m:
                day = int(m.group(3))
            if m:
                try:
                    content_dt = datetime(int(m.group(1)), int(m.group(2)), day)
                except ValueError:
                    content_dt = None
                if content_dt is not None:
                    ref_dt = None
                    if exclude_key:
                        row = self._conn.execute(
                            "SELECT created_at, event_time FROM memories WHERE key=?",
                            (exclude_key,),
                        ).fetchone()
                        if row is not None:
                            ref_dt = (_parse_ts(row["event_time"])
                                      or _parse_ts(row["created_at"]))
                    if ref_dt is None:
                        ref_dt = datetime.now()
                    if abs((content_dt - ref_dt).days) <= 400:
                        plans["time_anchor"] = (
                            0.30, {"date": content_dt.strftime("%Y-%m-%d")})
        except Exception:
            pass

        # ── 方案C: 实体密度 ──
        try:
            ents = entities if entities is not None else self._extract_entities(content)
            if ents and 2 <= len(ents) <= 8:
                plans["entity_density"] = (0.25, {"entity_count": len(ents)})
        except Exception:
            pass

        if plans:
            best = max(plans.items(), key=lambda kv: kv[1][0])
            order = ["similarity", "time_anchor", "entity_density"]
            result["score"] = best[1][0]
            result["method"] = "+".join(n for n in order if n in plans)
            result["evidence"] = {k: v[1] for k, v in plans.items()}
        return result

    def _check_freshness(self, prov: dict) -> dict:
        """检查 provenance.fresh：过期判定 + 活验证（契约对齐 scope-recall
        freshness 状态机语义，实现为 memorykit 自家 provenance 内嵌形态）。

        1) valid_until 过期 → status=expired / penalty=0.28（时间边界硬规则）
        2) 有 validator spec 且类型可自动执行（file_exists/command/http）
           → validator.validate() 活验证：ok → current(1.0) / fail → stale(0.35)
        3) manual/none/无 spec → 只做过期判定，不自动验证
        4) 无 fresh → 原样返回零影响（旧数据兼容）

        返回 {"fresh": fresh_or_None, "changed": bool}；调用方负责写回。
        """
        fresh = prov.get("fresh")
        if not fresh:
            return {"fresh": None, "changed": False}
        from .validator import validate
        now = datetime.now()
        changed = False

        # 1) 过期判定（valid_until 是日期 'YYYY-MM-DD'）
        vu = fresh.get("valid_until")
        if vu:
            try:
                vu_dt = datetime.strptime(str(vu), "%Y-%m-%d")
                if now.date() > vu_dt.date() and fresh.get("status") != "expired":
                    fresh["status"] = "expired"
                    fresh["penalty"] = 0.28
                    changed = True
            except ValueError:
                pass  # 畸形日期不判过期，留给活验证/人工

        # 2) 活验证
        vtype = fresh.get("type", "manual")
        spec = fresh.get("spec")
        if vtype in ("file_exists", "command", "http") and spec:
            check_spec = dict(spec)
            check_spec.setdefault("type", vtype)
            r = validate(check_spec)
            fresh["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            new_status = "current" if r.get("ok") else "stale"
            new_penalty = 1.0 if r.get("ok") else 0.35
            if fresh.get("status") != new_status or fresh.get("penalty") != new_penalty:
                fresh["status"] = new_status
                fresh["penalty"] = new_penalty
                changed = True
            # 活验证通过且配置了 ttl → 顺延有效期（循环验证的续期路径）
            if r.get("ok") and fresh.get("ttl_days"):
                new_vu = (now + timedelta(days=int(fresh["ttl_days"])))\
                    .strftime("%Y-%m-%d")
                if fresh.get("valid_until") != new_vu:
                    fresh["valid_until"] = new_vu
                    changed = True
        return {"fresh": fresh, "changed": changed}

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

        # ── freshness 检查（独立于 source 交叉验证）──
        # 1) valid_until 过期 → expired（时间边界硬规则，不靠 LLM）
        # 2) 有 validator spec → 活验证（file_exists/command/http），结果写回
        # 3) 无 fresh 配置 → 零影响（旧数据完全兼容）
        fresh_out = self._check_freshness(prov)
        fresh_result = fresh_out["fresh"]
        if fresh_out["changed"]:
            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET provenance=? WHERE id=?",
                    (json.dumps(prov, ensure_ascii=False), d["id"]))

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
                "reason": "无 source_session",
                "fresh": fresh_result
            }

        # 执行交叉检查
        xc = self._cross_check(content, source_session)
        score = xc["score"]
        keyword_score = xc["keyword_score"]
        entity_score = xc["entity_score"]
        method = xc["method"]
        matched_ids = xc["matched_ids"]

        # 降级路径（2026-08-07 修复）：state.db 缺失 → _cross_check 拿不到源
        # 消息（method=none 且无 matched_ids），此时 score=0 会被 auto_degrade
        # 误判成"证据不足"而把 importance 腰斩——改为库内轻量验证兜底给真实分。
        # 注意区分：源消息可达但零重叠（matched_ids 非空）是"查过源、无支撑"，
        # 保持原 degrade 语义不动。
        fallback_used = False
        if score == 0.0 and method == "none" and not matched_ids:
            lw = self._lightweight_verify(content, exclude_key=key)
            if lw and (lw.get("score") or 0.0) > 0.0:
                score = lw["score"]
                method = lw["method"]
                fallback_used = True
                logger.info(
                    "[verify] state.db 源消息不可达 → 库内轻量验证 key=%s "
                    "score=%.3f method=%s", key, score, method)
            else:
                prov.pop("fallback", None)

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
            "fresh": fresh_result
        }

        # 自动降级逻辑
        if fallback_used and score < degrade_threshold:
            # 降级路径（state.db 不可达）低分：无外部证据 ≠ 矛盾——
            # 不降 importance、不 boost，记录低分并标 unverifiable（等源可查重验）
            prov["fallback"] = True
            prov["v"] = "unverifiable"
            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET provenance=? WHERE id=?",
                    (json.dumps(prov, ensure_ascii=False), d["id"])
                )
            result["verification"] = "unverifiable"
            result["reason"] = "state.db 不可达，库内轻量验证分数不足"
            logger.info(
                "[verify] key=%s fallback score=%.3f < %.2f → unverifiable "
                "（源不可达不降级）", key, score, degrade_threshold)
        elif auto_degrade and score < degrade_threshold:
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
            if fallback_used:
                prov["fallback"] = True
            else:
                prov.pop("fallback", None)
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
                "SELECT key FROM memories WHERE (provenance IS NULL "
                "OR provenance = '' "
                "OR provenance = '{}' "
                "OR json_extract(provenance, '$.v') = 'pending') "
                "AND COALESCE(status,'active') != 'merged' "
                "ORDER BY id LIMIT ?",
                (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT key FROM memories WHERE COALESCE(status,'active') != 'merged' "
                "ORDER BY id LIMIT ?",
                (limit,)
            ).fetchall()

        stats = {"total": len(rows), "verified": 0, "degraded": 0,
                 "unverifiable": 0, "errors": 0, "avg_score": 0.0,
                 "fresh_checked": 0, "fresh_resolved": 0}
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
                # freshness 统计（2026-08-08 自审 P1-1：批量扫描时顺带报
                # freshness 处理量——过期判定/活验证随 verify 执行。
                # fresh_checked=有 fresh 配置的行；fresh_resolved=状态已定
                # （expired/stale/current 且有活验证时间戳）的行）
                f = res.get("fresh")
                if f:
                    stats["fresh_checked"] += 1
                    if f.get("status") in ("expired", "stale", "current") \
                            and f.get("last_checked_at"):
                        stats["fresh_resolved"] += 1
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

    def learn(self, content, source="task", source_session=None, event_time=None,
              precomputed_vec=_NO_PRECOMPUTED_VEC,
              precomputed_sparse=_NO_PRECOMPUTED_SPARSE,
              auto_verify: bool = True,
              source_session_id=None, source_msg_id=None,
              source_message_ids=None, raw_excerpt=None,
              anchor: bool = False, anchor_llm=None, anchor_top_k: int = 3,
              validator=None):
        """自动学习：评分分类+语义去重+原话保留+session关联。

        source_session_id / source_msg_id: state→learn 派生链指针（P0 #1，
        2026-08-08 修正设计）。learn 时记录"这条提炼来自哪个 session 的哪条
        原始消息"（state.db messages 表的 session_id + id），供
        reconstruct(memory_id) 回溯重建。可选——不传 → 存 NULL，行为与旧版
        完全一致（向后兼容，memory_db.py / overlay bridge 现有调用方不受影响）。
        合并路径：新指针不在旧值里时追加（逗号连接，语义同 source_session）。

        event_time: 事件发生时间（'YYYY-MM-DD[ HH:MM:SS]'），v2 检索元数据。
        仅参与检索时间偏置；不参与 TTL/遗忘状态机/写入去重年龄判断。
        合并去重时受 MERGE_EVENT_TIME_MAX_GAP_DAYS 保护（相距过远保留旧值）。

        auto_verify: 新写入记忆 importance>0.6（高于 learn 基数 0.6，即被
        高重要度关键词/source 抬升）时，做一次轻量验证：有 state.db + 有源
        session → _cross_check 交叉检查源消息；state.db 缺失/无源 session →
        _lightweight_verify 库内轻量验证（相似记忆/时间锚/实体密度，非零
        vscore + method，provenance 记 fallback=true）。只写 provenance 痕迹
        + 高分小幅提升 confidence，不参与去重/写入决策；无证据 → 保持 pending
        不降级，learn 主流程不受阻。
        返回 dict 的 verify 字段给出实际验证状态（pending|verified）。

        precomputed_vec: 预计算嵌入向量（learn_batch 批量路径传入）。
        传向量或显式 None 都会跳过内部 _embed 调用（None=批量嵌入失败，
        离线降级走实体去重兜底）；不传（默认）= 单条原行为，内部 _embed 嵌入。
        嵌入输入与 learn_batch 对齐：strip + _anchor_time 锚定 + 5000 截断。

        precomputed_sparse: 预计算稀疏词法权重 {token_id_str: weight}
        （learn_batch 批量路径传入，与 precomputed_vec 同一次请求解析）。
        单条路径（不传）内部 _embed_with_sparse 一次请求同时取 dense+sparse；
        批量路径传 dict 直接用、传 None 表示服务不支持/失败 → 降级纯 dense。
        合并去重时 sparse 与 vector 同策略：保留旧值不覆盖（内容合并后不
        重新嵌入，dense 也是旧的）。
        """
        if not content or len(content.strip()) < 10:
            return {"error": "content too short"}
        content = content.strip()
        content = _anchor_time(content)  # 写时锚定：相对时间→绝对日期/当时
        lower = content.lower()

        # ── 派生链指针归一化（P0 #1）：str 化后参与写入/合并（state.db 的
        # msg id 是 INTEGER，统一按 str 存，reconstruct 时再转回 int 查询）。
        # 非整数 msg id → 拒写 NULL + warning：一次脏指针经 merge 会永久污染
        # 指针列（后续 reconstruct 全部 bad_pointer），宁可丢数据不写垃圾 ──
        if source_session_id is not None:
            source_session_id = str(source_session_id)
        if source_msg_id is not None:
            try:
                int(str(source_msg_id))
            except (TypeError, ValueError):
                logger.warning("[learn] 忽略非整数 source_msg_id=%r（指针必须是 state.db messages.id）",
                               source_msg_id)
                source_msg_id = None
            else:
                source_msg_id = str(source_msg_id)

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
        if precomputed_vec is _NO_PRECOMPUTED_VEC:
            # 单条路径：一次请求同时取 dense 向量 + sparse 词法权重
            # （服务不支持 sparse → sparse=None，纯 dense 降级）
            qvec, qsparse = self._embed_with_sparse(content[:5000])
        else:
            # learn_batch 批量路径：向量已预计算（None=批量嵌入失败 → 离线降级，
            # 不二次嵌入，后续走实体去重兜底）；sparse 同批预计算（None=服务
            # 不支持/失败 → 降级纯 dense，不二次请求）
            qvec = precomputed_vec
            qsparse = (None if precomputed_sparse is _NO_PRECOMPUTED_SPARSE
                       else precomputed_sparse)
        new_entities = self._extract_entities(content)  # 提取实体用于图解信号
        merged = False

        # ── learn 侧自动接 episodic 索引 + 轻量验证（2026-08-07 小龙虾反馈 #2/#3）──
        # episodic_start/end 原本只被 CLI 手动调用，learn 流程从不记录，导致
        # source_session 有值但 episodic 表无对应行（mk_episodic list 空、
        # "追溯 state 原文"链路断裂）。这里在 learn 成功路径自动补索引。
        def _episodic_touch():
            """source_session 存在时确保 episodic 索引有记录（幂等）。

            （2026-08-15 收敛, 小龙虾第三轮反馈）原实现调 episodic_start/end
            写 legacy key 行(episodic/<sid16>), learn 每次重写 summary/ended_at,
            成"常青摘要桶"、和遮蔽窗时间窗并行双写打架。现改为:
            episodic_checkin 心跳(开/续/旋转, 时间窗语义) + learn 摘要沉淀到
            **当前开放窗**(summary/tags 空才盖, 不覆盖已有关键信息),
            随窗关闭自然沉淀。辅助索引失败不影响 learn 主流程。
            """
            if not source_session:
                return
            try:
                r = self.episodic_checkin(source_session, source="learn")
                key = r.get("key")
                if key:
                    # summary/tags 更新为最新 learn 内容(窗口内最新活动),
                    # 关窗时定格; importance 只升不降
                    self._conn.execute(
                        "UPDATE episodic SET "
                        "summary=?, tags=?, "
                        "outcome=?, importance=MAX(importance, 0.7) "
                        "WHERE key=?",
                        (content[:200], ",".join(sorted(tags))[:200],
                         "merged" if merged else "learned", key))
                    self._conn.commit()
            except Exception:
                pass

        def _maybe_auto_verify(final_key, final_imp, final_content):
            """importance>0.6 时做一次轻量验证（交叉检查或库内兜底）。

            保守策略（小龙虾反馈 #3/#4）：learn 后 verify 闭环不再空转——
            state.db 存在且有源 session → _cross_check 交叉检查源消息；
            state.db 缺失 / 无源 session → _lightweight_verify 库内轻量验证
            （相似记忆一致性/时间锚/实体密度，给非零 vscore + method）。
            不阻塞主流程、不参与去重/写入决策：无证据 → 保持 pending 不降级、
            不写痕迹；高分（>=0.5）→ provenance 记 v=verified + vscore +
            method + sids，并小幅提升 confidence（与 verify() 高分 boost 对齐）。
            阈值取 >0.6 而非 >=0.6：learn() 的 importance 基数就是 0.6（普通
            记忆默认值），只有被 high_imp 关键词/correction/discovery 抬高的
            "高重要度"记忆才值得立即验证，避免每条 learn 都背额外开销。
            返回 verify 状态字符串：pending | verified。
            """
            if not auto_verify or final_imp <= 0.6:
                return "pending"
            import os as _os
            state_path = getattr(self, "state_db_path", None)
            state_ok = bool(state_path) and _os.path.exists(state_path)
            xc = None
            lw = None
            if state_ok and source_session:
                try:
                    xc = self._cross_check(final_content, source_session)
                except Exception:
                    xc = None
            if xc is None:
                # 降级路径：state.db 缺失/无源 session → 库内轻量验证兜底
                try:
                    lw = self._lightweight_verify(final_content,
                                                  exclude_key=final_key)
                except Exception:
                    lw = None
            if xc is not None:
                score = xc.get("score") or 0.0
                method = xc.get("method") or "none"
                matched_ids = xc.get("matched_ids") or []
            elif lw is not None and (lw.get("score") or 0.0) > 0:
                score = lw["score"]
                method = lw.get("method") or "none"
                matched_ids = []
            else:
                # 两条路径都无证据 → 保持原样 pending（不写痕迹、不误报）
                return "pending"
            try:
                row = self._conn.execute(
                    "SELECT provenance, confidence FROM memories WHERE key=?",
                    (final_key,),
                ).fetchone()
                prov = {}
                old_conf = 0.45
                if row:
                    if row["provenance"]:
                        try:
                            prov = (json.loads(row["provenance"])
                                    if isinstance(row["provenance"], str)
                                    else row["provenance"])
                        except (json.JSONDecodeError, TypeError):
                            prov = {}
                    if row["confidence"] is not None:
                        old_conf = row["confidence"]
                prov["auto_vt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prov["vscore"] = round(score, 4)
                prov["method"] = method
                if matched_ids:
                    prov["sids"] = matched_ids[:20]
                prov["auto"] = True
                if lw is not None:
                    prov["fallback"] = True  # 降级路径（库内轻量验证）标记
                else:
                    prov.pop("fallback", None)
                if score >= 0.5:
                    prov["v"] = "verified"
                    new_conf = round(min(old_conf + 0.05, 0.95), 2)
                    if new_conf > old_conf:
                        self._conn.execute(
                            "UPDATE memories SET confidence=? WHERE key=?",
                            (new_conf, final_key),
                        )
                else:
                    prov["v"] = "pending"
                with self._tx():
                    self._conn.execute(
                        "UPDATE memories SET provenance=? WHERE key=?",
                        (json.dumps(prov, ensure_ascii=False), final_key),
                    )
                return prov["v"]
            except Exception:
                return "pending"

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

        def _merge_entities(new_ents, old_ents_json):
            """合并实体：new_entities ∪ 旧记录已有 entities（JSON 解析），去重保序。

            merge 路径不重抽实体导致旧记录 entities 恒为 null（中文专名丢失根因，
            2026-08-07 小龙虾实测）：这里把本条内容抽出的实体与旧记录已有实体
            做并集，保证 merge 后 entities 至少包含本条实词。纯附加，不影响
            match 判定（判定在 merge 前已完成）。旧实体解析失败/为空 → 直接用
            new_entities。
            """
            merged = list(new_ents) if new_ents else []
            seen = set(merged)
            if old_ents_json:
                try:
                    old = json.loads(old_ents_json) if isinstance(old_ents_json, str) else old_ents_json
                    if isinstance(old, list):
                        for e in old:
                            if e and e not in seen:
                                merged.append(e)
                                seen.add(e)
                except (json.JSONDecodeError, TypeError):
                    pass
            return merged

        def _merge_tags(new_tags, old_tags_str):
            """合并 tags：本条内容 tags ∪ 旧记录 tags（逗号分隔），去重排序。

            merge 内容为 old_content ||| new_content，旧 tags 依然适用；
            仅用新 tags 覆盖会把旧内容的关键词标签丢掉。
            """
            merged = set(new_tags) if new_tags else set()
            if old_tags_str:
                for t in str(old_tags_str).split(","):
                    t = t.strip()
                    if t:
                        merged.add(t)
            return sorted(merged)

        def _merge_pointer(old_val, new_val):
            """合并派生链指针（P0 #1）：新指针不在旧值里时追加（逗号连接）。

            语义与 merged_sessions 对齐：merge 是多次 learn 的聚合，旧指针
            保留、新指针附加——保证任意一条来源都可回溯。new_val 为空 → 原样
            返回旧值；旧值为空 → 直接取新值。
            """
            if not new_val:
                return old_val
            if not old_val:
                return new_val
            parts = [p for p in str(old_val).split(",") if p]
            if str(new_val) not in parts:
                parts.append(str(new_val))
            return ",".join(parts)

        def _merge_msg_ids(old_json, new_val):
            """合并 source_message_ids（JSON 数组字符串）: 旧 id ∪ 新 id, 去重保序。

            new_val 为 None/空 → 原样返回旧值（不覆盖, 与 source_msg_id 语义一致）；
            旧值为空 → 取新值。兼容两种旧格式: JSON 数组字符串 / 逗号分隔。
            """
            if new_val is None or new_val == "":
                return old_json
            old_ids = []
            if old_json:
                try:
                    parsed = json.loads(old_json)
                    if isinstance(parsed, list):
                        old_ids = [str(x) for x in parsed]
                    else:
                        old_ids = [str(parsed)]
                except (json.JSONDecodeError, TypeError):
                    old_ids = [x.strip() for x in str(old_json).split(",") if x.strip()]
            if isinstance(new_val, (list, tuple)):
                new_ids = [str(x) for x in new_val]
            else:
                new_ids = [str(x) for x in str(new_val).split(",") if x.strip()]
            merged = list(old_ids)
            for nid in new_ids:
                if nid and nid not in merged:
                    merged.append(nid)
            return json.dumps(merged, ensure_ascii=False)

        def _merge_raw_excerpt(old_val, new_val):
            """raw_excerpt 合并：新摘录非空时取新（最近原文优先）；否则保留旧值。"""
            if new_val:
                return str(new_val)[:800]
            return old_val


        def _merge_writeback(best, *, merged_content, new_imp, merged_sessions,
                             merged_tags, category, entities_json,
                             merged_event_time, merged_src_session_id,
                             merged_src_msg_id, merged_msg_ids,
                             merged_raw_excerpt, merged_derived_from, new_prov,
                             sim, overlap, boost, reason, decision=None):
            """唯一合并写回点（P0, 2026-10-20）：向量/实体两条 learn 合并路径共用。

            修复四个写回不变量：
              1. 内容变了 → 事务外重嵌；成功写新 vector/vector_bin/sparse 且
                 vector_stale=0；失败 vector_stale=1 + dirty 信号（旧向量保留）。
              2. decay_profile 用 COALESCE(?, decay_profile)，不写死 None。
              3. derived_from 由调用方算好（只在 anchor 指向别的 key 时才改），
                 绝不写成本行 key 自指。
              4. 每次合并写 merge_events 审计行（全文留痕）；P1 起
                 decision/relation/anchored_key/would_veto 取自 _merge_decision，
                 不再写死 None/0（off 档下与 P0 字面值一致）。
            """
            old_content = best.get("content") or ""
            vec_json = vec_bin = sparse_json = None
            vec_mode = 0  # 0=内容未变不写向量; 1=重嵌成功; 2=重嵌失败
            if merged_content != old_content:
                nvec, nsparse = self._embed_with_sparse(merged_content[:5000])
                if nvec:
                    vec_json = json.dumps(nvec)
                    vec_bin = struct.pack(f'{len(nvec)}f', *nvec)
                    sparse_json = json.dumps(nsparse) if nsparse else None
                    vec_mode = 1
                else:
                    vec_mode = 2
                    logger.warning(
                        "[learn] 合并后重嵌失败 key=%s → vector_stale=1, 写 dirty 信号等待重嵌",
                        best["key"])
                    self._signal_dirty(best["key"], best["id"])
            sets = [
                "content=?", "importance=?", "accessed_at=datetime('now')",
                "updated_at=datetime('now')", "source_session=?", "tags=?",
                "category=?", "entities=?",
                "decay_profile=COALESCE(?, decay_profile)", "provenance=?",
                "event_time=?", "source_session_id=?", "source_msg_id=?",
                "source_message_ids=?", "raw_excerpt=?", "derived_from=?",
            ]
            params = [merged_content, new_imp, merged_sessions,
                      ",".join(merged_tags), category, entities_json,
                      best.get("decay_profile"), new_prov, merged_event_time,
                      merged_src_session_id, merged_src_msg_id,
                      merged_msg_ids, merged_raw_excerpt, merged_derived_from]
            if vec_mode == 1:
                sets += ["vector=?", "vector_bin=?", "sparse=?", "vector_stale=0"]
                params += [vec_json, vec_bin, sparse_json]
            elif vec_mode == 2:
                sets += ["vector_stale=1"]
            else:
                sets += ["vector_stale=COALESCE(vector_stale,0)"]
            params.append(best["id"])
            with self._tx():
                self._conn.execute(
                    "UPDATE memories SET " + ", ".join(sets) + " WHERE id=?",
                    params)
                self._conn.execute(
                    "INSERT INTO merge_events"
                    "(keep_key, remove_key, old_content, new_content, "
                    " merged_content, sim, overlap, boost, relation, "
                    " anchored_key, decision, reason, would_veto, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                    (best["key"], "", old_content, content, merged_content,
                     max(-1.0, min(1.0, float(sim))), int(overlap),
                     max(1.0, float(boost)),
                     (decision.relation if decision else None),
                     (decision.anchored_key if decision else None),
                     (decision.action if decision else "MERGE"), reason,
                     int(bool(decision.would_veto)) if decision else 0))

        # ── P1 合并安全 v2（2026-10-20）──────────────────────────────────
        # 两个 flag 默认 off = 完全复刻 P0 的 if（回归安全线）；沙箱用 export 打开。
        # 锚定改为惰性（P1-3）：候选落进灰带才分类，无灰带一次检索/LLM 都不做。
        policy = read_merge_policy()
        merge_threshold = MERGE_HIGH_THRESHOLD
        anchor_info = None
        anchor_derived_from = None

        # 主路径: 向量去重（嵌入在线时）
        if qvec:
            # struct / numpy 已在模块顶部导入；这里不要再 import，否则
            # `struct` 变成 learn() 的局部名，实体兜底路径调用
            # _merge_writeback 时会 UnboundLocalError（P0, 2026-10-20）。
            qvec_np = np.array(qvec, dtype=np.float32)
            rows = self._conn.execute(
                "SELECT id, key, content, importance, source_session, source_session_id, source_msg_id, vector, vector_bin, sparse, entities, tags, event_time, provenance, decay_profile, derived_from, source_message_ids, raw_excerpt FROM memories WHERE vector IS NOT NULL AND COALESCE(status,'active') != 'merged' AND COALESCE(vector_stale,0) = 0"
            ).fetchall()
            
            # Fast path: batch cosine over binary blobs
            blobs = []
            row_indices = []
            # 维度安全：库里可能混有不同嵌入模型产生的向量（如 1024d/2048d），
            # 必须按当前查询向量维度过滤，否则 np.array 批量拼接抛
            # ValueError: inhomogeneous shape（2026-08-04 修复，小龙虾审查发现）
            qdim = int(qvec_np.size)
            for idx, r in enumerate(rows):
                if r["vector_bin"] is not None and self._vector_dim_from_blob(r["vector_bin"]) == qdim:
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
                # Slow fallback: JSON parse（同样做维度过滤）
                matches = []
                for r in rows:
                    dv_raw = r["vector"]
                    dv = json.loads(dv_raw) if dv_raw else None
                    if dv and len(dv) == qdim:
                        sim = self._cosine_similarity(qvec, dv)
                        overlap = _entity_overlap(new_entities, r["entities"])
                        fusion = _fusion_score(sim, overlap)
                        if fusion >= 0.85:
                            matches.append((fusion, sim, overlap, dict(r)))
            # ── P1-3 惰性锚定：只有灰带候选存在时才调 classify_relation ──
            anchor_info, anchor_derived_from = _anchor_gray_band(
                content, matches, anchor=anchor, anchor_llm=anchor_llm,
                anchor_top_k=anchor_top_k, policy=policy,
                threshold=merge_threshold)

            # ── P1-4 统一决策点：_critical_merge_mismatch 已并入
            # _merge_decision；每条候选一个决策，KEEP 的候选（关键事实冲突 /
            # 灰带否决）不参与 best 选择（与 P0 的"过滤后再选"等价）。──
            _vector_source = SOURCE_VECTOR if blobs else SOURCE_VECTOR_JSON
            graded = []
            for _fusion, _sim, _overlap, _row in matches:
                _dec = _merge_decision(
                    source=_vector_source, sim=_sim, fusion=_fusion,
                    overlap=_overlap, candidate_key=_row["key"],
                    relation=(anchor_info or {}).get("relation"),
                    anchored_key=(anchor_info or {}).get("anchored_key"),
                    old_content=(_row.get("content") or ""),
                    new_content=content, threshold=merge_threshold,
                    floor=MERGE_FLOOR, mode=policy.mode,
                    anchor_mode=policy.anchor_mode)
                if _dec.action == ACTION_MERGE:
                    graded.append((_fusion, _sim, _overlap, _row, _dec))
            if graded:
                graded.sort(key=lambda _t: _t[0], reverse=True)
                _fusion, _sim, _overlap, best, decision = graded[0]
                old_key = best["key"]
                old_imp = best["importance"] or 0.5
                new_imp = min(max(imp, old_imp + 0.05), 0.98)
                old_sessions = best.get("source_session") or ""

                merged_sessions = old_sessions
                if source_session and source_session not in old_sessions:
                    merged_sessions = (old_sessions + "," + source_session).strip(",")

                # ── 派生链指针合并（P0 #1）：旧指针保留 + 新指针追加 ──
                merged_src_session_id = _merge_pointer(
                    best.get("source_session_id"), source_session_id)
                merged_src_msg_id = _merge_pointer(
                    best.get("source_msg_id"), source_msg_id)

                # ── 验证管线: 合并后仍标记 pending，等待交叉检查 ──
                # P0 修复（2026-10-20）: 先存旧 v 再置 pending；原地改 dict，
                # 不整体替换成 {"v":"pending"}（否则 vscore/sids/method 全丢）。
                old_prov = best.get("provenance") or "{}"
                old_verif = {}
                try:
                    old_verif = json.loads(old_prov) if isinstance(old_prov, str) else old_prov
                except (json.JSONDecodeError, TypeError):
                    old_verif = {}
                if not isinstance(old_verif, dict):
                    old_verif = {}
                _prev_v = old_verif.get("v")
                if _prev_v in ("verified", "degraded"):
                    old_verif["prev_v"] = _prev_v
                old_verif["v"] = "pending"  # 内容更新了，需重新验证
                if anchor_info is not None:
                    old_verif["anchor"] = anchor_info
                new_prov = json.dumps(old_verif, ensure_ascii=False)

                # ── 合并内容: 保留旧知识 + 拼接新知识（防止覆盖丢失） ──
                old_content = best.get("content") or ""
                if old_content and content and content not in old_content and old_content not in content:
                    merged_content = old_content + " ||| " + content
                else:
                    merged_content = content or old_content

                # ── event_time merge 保护（memorykit v2）──
                # event_time 是事件发生时间（纯检索元数据，不参与去重年龄判断/
                # TTL/状态机）。旧条目已有 event_time 且新 event_time 与其相距
                # 超过 MERGE_EVENT_TIME_MAX_GAP_DAYS（7 天）时，保守保留旧值：
                # 相距如此之远说明是两次独立事件（只是内容相似），合并只做内容
                # 去重，不应篡改原事件的时点身份；≤7 天视为同一事件的补充描述，
                # 以新值为准；新条目未带 event_time 时同样保留旧值（不置空）。
                merged_event_time = best.get("event_time")
                _old_dt = _parse_ts(best.get("event_time"))
                _new_dt = _parse_ts(event_time)
                if _old_dt and _new_dt and abs((_new_dt - _old_dt).days) > self.MERGE_EVENT_TIME_MAX_GAP_DAYS:
                    merged_event_time = best.get("event_time")  # 保留旧值，不覆盖
                    logger.info(
                        "[learn] event_time gap > %d days (old=%s, new=%s) → 保留旧 event_time",
                        self.MERGE_EVENT_TIME_MAX_GAP_DAYS, best.get("event_time"), event_time)
                elif event_time:
                    merged_event_time = event_time

                # ── 实体/tags 合并（2026-08-07 小龙虾实测根因修复）──
                # merge 路径此前不重抽 entities：旧记录 entities 为 null 时
                # 合并后恒 null，中文专名在所有管道里都丢失。这里把本条新抽
                # 实体 ∪ 旧记录已有实体（去重保序）写回；tags 同理并入旧值
                # （merged_content 含旧内容，旧 tags 依然适用）。
                merged_entities = _merge_entities(new_entities, best.get("entities"))
                merged_tags = _merge_tags(tags, best.get("tags"))
                entities_json = (json.dumps(merged_entities, ensure_ascii=False)
                                 if merged_entities else None)

                # ── 原文指针合并（遮蔽机制, 2026-08-14）──
                # 与派生链指针同语义: 旧 id 保留 + 新 id 追加；新摘录优先。
                merged_msg_ids = _merge_msg_ids(best.get("source_message_ids"),
                                                source_message_ids)
                merged_raw_excerpt = _merge_raw_excerpt(best.get("raw_excerpt"),
                                                        raw_excerpt)

                # ── derived_from: 只在 anchor 指向别的记忆时改；否则保留旧值 ──
                # P1 #1 的旧实现把 best["key"] 传给 derived_from → 自指成环。
                merged_derived_from = best.get("derived_from")
                if anchor_derived_from and anchor_derived_from != best["key"]:
                    merged_derived_from = anchor_derived_from

                _merge_writeback(
                    best, merged_content=merged_content, new_imp=new_imp,
                    merged_sessions=merged_sessions, merged_tags=merged_tags,
                    category=category, entities_json=entities_json,
                    merged_event_time=merged_event_time,
                    merged_src_session_id=merged_src_session_id,
                    merged_src_msg_id=merged_src_msg_id,
                    merged_msg_ids=merged_msg_ids,
                    merged_raw_excerpt=merged_raw_excerpt,
                    merged_derived_from=merged_derived_from,
                    new_prov=new_prov,
                    sim=_sim, overlap=_overlap,
                    boost=(_fusion / _sim if _sim else 1.0),
                    reason=decision.reason, decision=decision)
                if merged_entities:
                    # 实体关联表同步（与 set() 的 _update_entity_links 对齐）
                    try:
                        self._update_entity_links(best["id"], old_key, merged_entities)
                    except Exception:
                        pass
                merged = True
                logger.info("[learn] merged into existing key=%s (fusion=%.3f, sim=%.3f, entity_overlap=%d, boost=%.3f, reason=%s, would_veto=%s, imp=%.2f→%.2f, verify=pending)",
                            old_key, _fusion, _sim, _overlap,
                            (_fusion / _sim if _sim else 1.0),
                            decision.reason, decision.would_veto,
                            old_imp, new_imp)
                _episodic_touch()
                v_status = _maybe_auto_verify(old_key, new_imp, merged_content)
                return {"key": old_key, "category": category, "importance": new_imp,
                        "tags": ",".join(merged_tags), "merged": True,
                        "sim": round(_sim, 3), "fusion": round(_fusion, 3),
                        "verify": v_status}

        # 兜底: 嵌入离线 + 实体去重
        if not qvec and new_entities and len(new_entities) >= 3:
            rows = self._conn.execute(
                "SELECT id, key, content, importance, source_session, source_session_id, source_msg_id, entities, tags, event_time, provenance, decay_profile, derived_from, source_message_ids, raw_excerpt FROM memories WHERE entities IS NOT NULL AND entities != 'null' AND COALESCE(status,'active') != 'merged'"
            ).fetchall()
            matches = []
            for r in rows:
                overlap = _entity_overlap(new_entities, r["entities"])
                if overlap >= 3:
                    matches.append((overlap, dict(r)))
            # ── P1-4 统一决策点（实体兜底路径）：关键事实冲突同样并入决策。──
            graded = []
            for _overlap, _row in matches:
                _dec = _merge_decision(
                    source=SOURCE_ENTITY, sim=0.0, fusion=0.0,
                    overlap=_overlap, candidate_key=_row["key"],
                    old_content=(_row.get("content") or ""),
                    new_content=content, threshold=merge_threshold,
                    floor=MERGE_FLOOR, mode=policy.mode,
                    anchor_mode=policy.anchor_mode)
                if _dec.action == ACTION_MERGE:
                    graded.append((_overlap, _row, _dec))
            if graded:
                graded.sort(key=lambda _t: _t[0], reverse=True)
                _overlap, best, decision = graded[0]
                old_key = best["key"]
                old_imp = best["importance"] or 0.5
                new_imp = min(max(imp, old_imp + 0.05), 0.98)
                old_sessions = best.get("source_session") or ""

                merged_sessions = old_sessions
                if source_session and source_session not in old_sessions:
                    merged_sessions = (old_sessions + "," + source_session).strip(",")

                # ── 派生链指针合并（P0 #1，同向量合并路径）──
                merged_src_session_id = _merge_pointer(
                    best.get("source_session_id"), source_session_id)
                merged_src_msg_id = _merge_pointer(
                    best.get("source_msg_id"), source_msg_id)

                # ── 验证管线: 实体去重合并后也标记 pending ──
                # P0 修复（2026-10-20）: 同向量路径——先存旧 v 再置 pending,
                # 原地改 dict 保留 vscore/sids/method。
                old_prov = best.get("provenance") or "{}"
                try:
                    old_verif = json.loads(old_prov) if isinstance(old_prov, str) else old_prov
                except (json.JSONDecodeError, TypeError):
                    old_verif = {}
                if not isinstance(old_verif, dict):
                    old_verif = {}
                _prev_v = old_verif.get("v")
                if _prev_v in ("verified", "degraded"):
                    old_verif["prev_v"] = _prev_v
                old_verif["v"] = "pending"
                if anchor_info is not None:
                    old_verif["anchor"] = anchor_info
                new_prov = json.dumps(old_verif, ensure_ascii=False)

                # ── 合并内容: 保留旧知识 + 拼接新知识 ──
                old_content = best.get("content") or ""
                if old_content and content and content not in old_content and old_content not in content:
                    merged_content = old_content + " ||| " + content
                else:
                    merged_content = content or old_content

                # ── event_time merge 保护（同向量合并路径，见上注释）──
                merged_event_time = best.get("event_time")
                _old_dt = _parse_ts(best.get("event_time"))
                _new_dt = _parse_ts(event_time)
                if _old_dt and _new_dt and abs((_new_dt - _old_dt).days) > self.MERGE_EVENT_TIME_MAX_GAP_DAYS:
                    merged_event_time = best.get("event_time")  # 保留旧值，不覆盖
                elif event_time:
                    merged_event_time = event_time

                # ── 实体/tags 合并（同向量合并路径，见上注释）──
                merged_entities = _merge_entities(new_entities, best.get("entities"))
                merged_tags = _merge_tags(tags, best.get("tags"))
                entities_json = (json.dumps(merged_entities, ensure_ascii=False)
                                 if merged_entities else None)

                # ── 原文指针合并（遮蔽机制, 2026-08-14）──
                # 与派生链指针同语义: 旧 id 保留 + 新 id 追加；新摘录优先。
                merged_msg_ids = _merge_msg_ids(best.get("source_message_ids"),
                                                source_message_ids)
                merged_raw_excerpt = _merge_raw_excerpt(best.get("raw_excerpt"),
                                                        raw_excerpt)

                # ── derived_from: 只在 anchor 指向别的记忆时改；否则保留旧值 ──
                merged_derived_from = best.get("derived_from")
                if anchor_derived_from and anchor_derived_from != best["key"]:
                    merged_derived_from = anchor_derived_from

                _merge_writeback(
                    best, merged_content=merged_content, new_imp=new_imp,
                    merged_sessions=merged_sessions, merged_tags=merged_tags,
                    category=category, entities_json=entities_json,
                    merged_event_time=merged_event_time,
                    merged_src_session_id=merged_src_session_id,
                    merged_src_msg_id=merged_src_msg_id,
                    merged_msg_ids=merged_msg_ids,
                    merged_raw_excerpt=merged_raw_excerpt,
                    merged_derived_from=merged_derived_from,
                    new_prov=new_prov,
                    sim=0.0, overlap=_overlap,
                    boost=1.0, reason=decision.reason, decision=decision)
                if merged_entities:
                    try:
                        self._update_entity_links(best["id"], old_key, merged_entities)
                    except Exception:
                        pass
                merged = True
                logger.info("[learn] entity-only merge into key=%s (overlap=%d, boost=1.000, reason=%s, imp=%.2f→%.2f, verify=pending)",
                            old_key, _overlap, decision.reason, old_imp, new_imp)
                _episodic_touch()
                v_status = _maybe_auto_verify(old_key, new_imp, merged_content)
                return {"key": old_key, "category": category, "importance": new_imp,
                        "tags": ",".join(merged_tags), "merged": True,
                        "sim": 0, "fusion": round(_overlap / 5.0, 3),
                        "verify": v_status}

        # ---- No match found — create new entry ----
        short = content[:30].replace(" ","_").replace("\n","_")
        ts = datetime.now().strftime("%m%d")
        key = "%s/%s_%s" % (category, ts, short[:20])
        if self._conn.execute("SELECT id FROM memories WHERE key=?", (key,)).fetchone():
            key = "%s_%d" % (key, int(datetime.now().timestamp() % 1000))
        # ── 验证管线: 新条目标记 pending_verification ──
        self.set(key, content, category=category, tags=",".join(sorted(tags)),
                 importance=imp,
                 # 批量路径向量已定（含 None=嵌入失败），不再二次嵌入；
                 # 单条路径保持原行为 embed=True（_embed 失败时 set 内兜底重试一次）
                 embed=(precomputed_vec is _NO_PRECOMPUTED_VEC),
                 source_session=source_session,
                 vector=qvec, sparse=qsparse, event_time=event_time,
                 source_session_id=source_session_id, source_msg_id=source_msg_id,
                 source_message_ids=source_message_ids, raw_excerpt=raw_excerpt,
                 derived_from=anchor_derived_from,
                 validator=validator)
        # set() 写入的 provenance 含 fresh（若传了 validator）——这里读回补
        # v=pending，不整体覆盖（否则 set 里组装的 fresh 会被抹掉）
        with self._tx():
            row = self._conn.execute(
                "SELECT provenance FROM memories WHERE key=?", (key,)).fetchone()
            prov = {}
            try:
                prov = json.loads(row["provenance"] or "{}") if row else {}
            except (json.JSONDecodeError, TypeError):
                prov = {}
            prov["v"] = "pending"
            if anchor_info is not None:
                prov["anchor"] = anchor_info
            self._conn.execute(
                "UPDATE memories SET provenance=? WHERE key=?",
                (json.dumps(prov, ensure_ascii=False), key))
        # ── learn 侧闭环：episodic 索引 + 高重要度轻量验证（反馈 #2/#3）──
        _episodic_touch()
        v_status = _maybe_auto_verify(key, imp, content)
        return {"key": key, "category": category, "importance": imp,
                "tags": ",".join(sorted(tags)), "merged": False,
                "verify": v_status}

    # learn_batch 批量嵌入的每批条数（embed_batch 一次 HTTP 请求的上限）。
    # 本地 llama.cpp 实测 500 条 ≈ 3.4s；128/批保证单请求 <1s 且不撑爆服务端
    # 内存/超时（embed_batch 超时下限 30s 覆盖）。
    EMBED_BATCH_CHUNK = 128

    def learn_batch(self, items: list) -> list:
        """批量学习：一次批量嵌入 + 逐条复用 learn 逻辑（零二次嵌入）。

        items 每项: {"content": str, "source": str, "source_session": str,
                     "event_time": str, "source_session_id": str,
                     "source_msg_id": str}（source/event_time/source_session/
        source_session_id/source_msg_id 均可省略，缺省与 learn() 相同——
        前两者向后兼容，后两者是 state→learn 派生链指针 P0 #1，逐条透传
        learn() 写入）。返回与 items 一一对应的 learn() 结果列表
        （过短条目返回 {"error": "content too short"}）。

        实现:
          1. 全量 content 先批量嵌入（_embed_batch，按 EMBED_BATCH_CHUNK 分块），
             向量与 items 顺序对齐；失败条目对应 None。
          2. 逐条调用 learn(precomputed_vec=向量) —— 评分分类/向量+实体融合去重/
             写库/merge+event_time 保护全部复用 learn() 原逻辑，唯一区别是跳过
             内部 _embed。批量嵌入失败的条目显式传 None，同样不二次嵌入
             （离线降级，走实体去重兜底，与 learn 嵌入离线行为一致）。

        嵌入输入与 learn() 完全对齐：strip + _anchor_time 锚定 + 5000 截断，
        保证预计算向量与单条 learn 自嵌入结果一致。
        """
        if not items:
            return []
        results: list = []
        valid: list = []  # (item, orig_index) —— 待 learn 的条目
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"error": "invalid item"})
                continue
            content = item.get("content")
            if not content or len(str(content).strip()) < 10:
                results.append({"error": "content too short"})
                continue
            valid.append((item, i))
            results.append(None)  # 占位，稍后回填 learn 结果
        if valid:
            # 与 learn() 对齐的嵌入输入：strip + 时间锚定 + 5000 截断
            texts = [_anchor_time(str(it["content"]).strip())[:5000] for it, _ in valid]
            pairs: list = []
            # 批量路径：分块调用 _embed_batch_with_sparse（每块一次 HTTP 请求）。
            # 内部自动处理：注入 embedder 无批量能力 → 逐条 _embed_with_sparse
            # 降级；注入 embedder 无 sparse 支持 → (dense, None) 纯 dense。
            for k in range(0, len(texts), self.EMBED_BATCH_CHUNK):
                pairs.extend(self._embed_batch_with_sparse(texts[k:k + self.EMBED_BATCH_CHUNK]))
            if not isinstance(pairs, list) or len(pairs) != len(valid):
                pairs = [(None, None)] * len(valid)  # 顺序对齐失败 → 全部走离线降级
            for (item, i), (vec, sparse) in zip(valid, pairs):
                results[i] = self.learn(
                    item["content"],
                    source=item.get("source", "task"),
                    source_session=item.get("source_session"),
                    event_time=item.get("event_time"),
                    precomputed_vec=vec,
                    precomputed_sparse=sparse,
                    source_session_id=item.get("source_session_id"),
                    source_msg_id=item.get("source_msg_id"),
                )
        return results

    # ══════════════════════════════════════════════════════════════
    # state→learn 派生链：回溯重建（P0 #1，2026-08-08 修正设计）
    # ══════════════════════════════════════════════════════════════
    #
    # 设计（design-notes/scope-recall-borrowing.md §2026-08-08 修正）：
    #   state.db（原始消息，Hermes 原生会话存档，永不删）── learn 记指针 ──→
    #   memory.db（提炼）。指针 = source_session_id + source_msg_id 两列。
    # 本方法只建"可回溯"能力：给定 memory_id，沿指针回 state.db 找回提炼
    # 来源的原始消息。**不承诺自动重建**（重放/蒸馏是后续管线的事）。

    def drain_journal(self, limit: int = 200, dry_run: bool = False) -> dict:
        """常驻提炼：按指针链从 state.db 取未提炼消息 → 复用 learn() 提炼。

        设计（design-notes §5 修正版）：不建 journal 表——state.db 就是原始
        消息存档（Hermes 原生会话库，永不删），本方法用 watermark 记住上次
        提炼位置，把"碰巧被提炼"变成"有节奏地扫库"。

        提炼消息规则：
        - role ∈ {user, assistant}（tool 输出是 JSON/噪音，不提炼）
        - content.strip() 长度 >= 10（learn 有最短长度门槛，预过滤避免空转）
        - learn() 带 source_session_id/source_msg_id 指针（可回溯重建）
        - learn 自身处理分类/去重/合并（drain 只负责喂料与收据）

        返回收据：
          {"ok": True, "processed": N, "inserted": N, "merged": N,
           "skipped": N, "backlog": N, "last_msg_id": N, "dry_run": bool}
          {"ok": False, "reason": "no_state_db"|"state_db_unreadable"}
          - 无积压 → processed=0, backlog=0（正常空跑，不算失败）
          - dry_run=True：只扫描统计，不 learn、不推进 watermark
        """
        import os as _os
        import sqlite3 as _sql
        state_path = getattr(self, "state_db_path", None)
        if not state_path or not _os.path.exists(state_path):
            return {"ok": False, "reason": "no_state_db"}
        # watermark：drain_state KV 表（self._conn 是 memory.db，不是 state.db）
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS drain_state "
            "(key TEXT PRIMARY KEY, value TEXT)")
        row = self._conn.execute(
            "SELECT value FROM drain_state WHERE key='last_msg_id'").fetchone()
        last_id = int(row["value"]) if row and row["value"] else 0

        try:
            conn = _sql.connect(state_path)
            conn.row_factory = _sql.Row
            try:
                rows = conn.execute(
                    "SELECT id, session_id, role, content FROM messages "
                    "WHERE id > ? AND role IN ('user','assistant') "
                    "AND length(content) >= 10 "
                    "ORDER BY id LIMIT ?",
                    (last_id, limit),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) c FROM messages WHERE id > ?",
                    (last_id,)).fetchone()["c"]
            finally:
                conn.close()
        except Exception:
            return {"ok": False, "reason": "state_db_unreadable"}

        inserted = merged = skipped = 0
        max_id = last_id
        for m in rows:
            mid = m["id"]
            max_id = max(max_id, mid)
            if dry_run:
                continue
            r = self.learn(
                m["content"], source="drain",
                source_session_id=m["session_id"], source_msg_id=mid)
            if r.get("merged"):
                merged += 1
            elif r.get("key"):
                inserted += 1
            else:
                skipped += 1

        if not dry_run and max_id > last_id:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO drain_state(key, value) VALUES('last_msg_id', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(max_id),))
        return {
            "ok": True,
            "processed": len(rows),
            "inserted": inserted,
            "merged": merged,
            "skipped": skipped,
            "backlog": max(0, total - len(rows)),
            "last_msg_id": max_id,
            "dry_run": dry_run,
        }

    def reconstruct(self, memory_id: int) -> dict:
        """state→learn 派生链回溯：返回该 memory 提炼来源的原始消息。

        从 memory 行的 source_session_id/source_msg_id 指针出发，在
        state.db（Hermes 会话存档，只读引用）messages 表定位原始消息。
        指针支持逗号连接的**多来源**形态（merge 路径旧指针保留 + 新指针
        追加）：按 session/msg id 列表做并集查询，返回全部能定位到的源。
        找不到 source（旧数据/无指针/state.db 缺失/消息已被清）→ 明确返回
        失败原因，绝不抛出（纯读能力，任何异常内部消化）。

        返回:
          {"found": True, "memory_id": int, "memory_key": str,
           "source": {session_id, msg_id, role, content, timestamp},   # 第一个定位到的源
           "sources": [ {...}, ... ]}                                  # 全部定位到的源（≥1 条）
          {"found": False, "memory_id": int, "memory_key": str|None,
           "reason": str}
            reason ∈ {"memory_not_found", "no_pointer", "no_state_db",
                      "bad_pointer", "msg_not_found", "state_db_unreadable"}
            - no_pointer: 旧数据 / learn 未带指针（两列均为 NULL）→ "无来源"
            - no_state_db: state_db_path 未配置或文件不存在
            - bad_pointer: 指针格式无法解析（非空但含非整数 msg id）
            - msg_not_found: 指针指向的消息均不在 state.db（归档/清理后）
            - state_db_unreadable: state.db 存在但不是可读的 sqlite 文件
        """
        base: dict = {"memory_id": memory_id}  # 显式 dict 注解：结果形状多变（str/int/dict 混用）
        try:
            row = self._conn.execute(
                "SELECT id, key, source_session_id, source_msg_id "
                "FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is None:
            base["memory_key"] = None
            base["found"] = False
            base["reason"] = "memory_not_found"
            return base
        base["memory_key"] = row["key"]
        sid = row["source_session_id"]
        mid = row["source_msg_id"]
        if not sid or not mid:
            base["found"] = False
            base["reason"] = "no_pointer"
            return base
        import os as _os
        import sqlite3 as _sql
        state_path = getattr(self, "state_db_path", None)
        if not state_path or not _os.path.exists(state_path):
            base["found"] = False
            base["reason"] = "no_state_db"
            return base
        # 多来源指针：逗号分隔的列表（merge 累积）。任一项非整数 → bad_pointer。
        sids = [p for p in str(sid).split(",") if p]
        try:
            mids = [int(p) for p in str(mid).split(",") if p]
        except (TypeError, ValueError):
            base["found"] = False
            base["reason"] = "bad_pointer"
            return base
        if not sids or not mids:
            base["found"] = False
            base["reason"] = "no_pointer"
            return base
        try:
            conn = _sql.connect(state_path)
            conn.row_factory = _sql.Row
            try:
                ph = ",".join("?" * len(sids))
                qh = ",".join("?" * len(mids))
                rows = conn.execute(
                    f"SELECT id, session_id, role, content, timestamp FROM messages "
                    f"WHERE session_id IN ({ph}) AND id IN ({qh})",
                    (*sids, *mids),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            base["found"] = False
            base["reason"] = "state_db_unreadable"
            return base
        if not rows:
            base["found"] = False
            base["reason"] = "msg_not_found"
            return base
        sources = [
            {"session_id": m["session_id"], "msg_id": m["id"],
             "role": m["role"], "content": m["content"], "timestamp": m["timestamp"]}
            for m in rows
        ]
        base["found"] = True
        base["source"] = sources[0]
        base["sources"] = sources
        return base
