#!/usr/bin/env python3
"""
memory_gate.py — 记忆写入语义闸门

在 sync_turn 的正则候选之后、db.learn() 之前，用 LLM 快速判断
"这条内容值不值得作为长期记忆"。把规则从"正则+关键词"升级为"语义判断"。

设计:
  - 用 DeepSeek (flash) 做二元判断, 快而便宜
  - 只处理非空、长度 20-300 的候选
  - 判断标准: 是否包含可复用的稳定事实/偏好/教训/环境信息
  - 闲聊碎语/瞬时状态/情绪感慨 → 丢弃

用法:
  python3 memory_gate.py "候选记忆文本"
  输出: PASS|DROP|ERROR + 简短理由

集成: hermes-local 插件 sync_turn 在 db.learn() 前调用本脚本
"""
import os, sys, json, urllib.request, urllib.error, re, hashlib, sqlite3, time

# 缓存库路径：默认当前目录 .memory_gate_cache.db（可用 MEMORY_GATE_CACHE_DB 覆盖）
_CACHE_DB = os.environ.get(
    "MEMORY_GATE_CACHE_DB",
    os.path.join(os.getcwd(), ".memory_gate_cache.db"),
)
_CACHE_TTL = 86400  # 24h

def _cache_get(text: str):
    """查缓存: 返回 (verdict, reason) 或 None"""
    try:
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        db = sqlite3.connect(_CACHE_DB, timeout=3)
        r = db.execute("SELECT verdict, reason, ts FROM gate_cache WHERE hash=? AND ts > ?",
                       (h, time.time() - _CACHE_TTL)).fetchone()
        db.close()
        if r:
            return {"verdict": r[0], "reason": r[1] + " (cached)"}
    except Exception:
        pass
    return None

def _cache_put(text: str, result: dict):
    """写缓存"""
    try:
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        db = sqlite3.connect(_CACHE_DB, timeout=3)
        db.execute("CREATE TABLE IF NOT EXISTS gate_cache(hash TEXT PRIMARY KEY, verdict TEXT, reason TEXT, ts REAL)")
        db.execute("INSERT OR REPLACE INTO gate_cache(hash, verdict, reason, ts) VALUES (?,?,?,?)",
                   (h, result.get("verdict", "ERROR"), result.get("reason", ""), time.time()))
        db.commit()
        db.close()
    except Exception:
        pass

def _load_env():
    """兼容占位：API key 直接从环境变量读取（不再自动加载 ~/.hermes/.env）。"""
    return

def _gate_api_key():
    """读取语义判断用 API key：MEMORY_GATE_API_KEY 优先，回退 DEEPSEEK_API_KEY/YAMII_GPT_KEY。"""
    return (os.environ.get("MEMORY_GATE_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("YAMII_GPT_KEY"))

def _deepseek_judge(text: str) -> dict:
    """用 DeepSeek 判断: 该存记忆(MEMORY) / 该生成skill(SKILL) / 丢弃(DROP)"""
    key = _gate_api_key()
    if not key:
        return {"verdict": "MEMORY", "reason": "no-key-fallback"}  # 无 key 时按记忆放行(不阻塞)

    prompt = (
        "你是知识路由过滤器。判断下面这条内容该去向哪里。\n"
        "三类去向:\n"
        "1. MEMORY — 稳定的用户偏好/身份/环境配置/硬件规格/事实/个人特点，以及进行中的硬件/项目规划与决策(如机箱改造方案、电源选型、接线问题、采购状态)。是'世界是什么样'和'正在怎么变'。\n"
        "2. SKILL — 可复用的操作流程/步骤/命令模板/工作流/方法论。是'遇到X该怎么做'。\n"
        "3. DROP — 闲聊碎语/瞬时心情/疑问/感叹/零碎状态/无信息量日常。\n"
        '只回复JSON: {"verdict": "MEMORY"或"SKILL"或"DROP", "reason": "一句话理由"}\n\n'
        f"内容: {text[:500]}"
    )
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 150,
        "temperature": 0,
        "reasoning_effort": "none",
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            m = re.search(r'"verdict"\s*:\s*"(\w+)"', content)
            verdict = m.group(1).upper() if m else None
            if verdict not in ("MEMORY", "SKILL", "DROP"):
                verdict = "MEMORY" if "MEMORY" in content.upper() else ("SKILL" if "SKILL" in content.upper() else "DROP")
            reason_m = re.search(r'"reason"\s*:\s*"([^"]*)"', content)
            reason = reason_m.group(1) if reason_m else content.strip()[:60]
            return {"verdict": verdict, "reason": reason}
    except Exception as e:
        return {"verdict": "MEMORY", "reason": f"error-fallback:{type(e).__name__}"}  # 出错按记忆放行

_SKILL_CANDIDATES = os.environ.get(
    "MEMORY_GATE_SKILL_CANDIDATES",
    os.path.join(os.path.dirname(_CACHE_DB), "skill_candidates.md"),
)

def _collect_skill_candidate(text: str, reason: str):
    """SKILL 类候选攒进清单，供定期审视生成 skill"""
    try:
        os.makedirs(os.path.dirname(_SKILL_CANDIDATES), exist_ok=True)
        with open(_SKILL_CANDIDATES, "a", encoding="utf-8") as f:
            f.write(f"- [{time.strftime('%m-%d %H:%M')}] {text[:200].strip()} (reason: {reason})\n")
    except Exception:
        pass

def gate(text: str) -> dict:
    """主入口: 三分类判断 (MEMORY/SKILL/DROP, 带缓存)"""
    text = (text or "").strip()
    if not text:
        return {"verdict": "DROP", "reason": "empty"}
    if len(text) < 20:
        return {"verdict": "DROP", "reason": "too-short"}
    if len(text) > 300:
        text = text[:300]
    # ── 格式黑名单（零 LLM 调用，2026-08-02 加严）──
    # 纯时间戳/纯数字（如 1785647493.8874457）
    if re.fullmatch(r"[\d\s.\-+/:]+", text):
        return {"verdict": "DROP", "reason": "numeric-only"}
    # 无中文（记忆库是中文语料，纯英文/符号无信息量）
    if not re.search(r"[\u4e00-\u9fff]", text):
        return {"verdict": "DROP", "reason": "no-chinese"}
    # 纯标点/表情/无意义符号
    if re.fullmatch(r"[\W_]+", text, flags=re.UNICODE):
        return {"verdict": "DROP", "reason": "symbols-only"}
    # 缓存优先
    cached = _cache_get(text)
    if cached:
        return cached
    result = _deepseek_judge(text)
    _cache_put(text, result)
    if result.get("verdict") == "SKILL":
        _collect_skill_candidate(text, result.get("reason", ""))
    return result

if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    result = gate(text)
    print(json.dumps(result, ensure_ascii=False))


# ═══════════════════════════════════════════════════════════════════════════
# 环1 应用反馈 — 纠正信号检测（memory-v2-plan.md 环1）
#
# 挂载点说明：框架无 on_user_message 钩子，本模块被插件 on_turn_start
# 调用（每轮开始时拿到用户下一条消息原文，早于 prefetch_all）。
#
# 双层检测：
#   Layer1  — 高精度规则（不对/错了/别/不是/搞错了/你记错了 + 后跟名词/事实）
#   Layer2  — flash LLM 兜底（规则未命中 + 有否定词 + 上一轮注入过记忆 → flash 判断）
#            调用方需自行控制频率（<5% 消息，见 detect_correction 的 layer2_budget）
# ═══════════════════════════════════════════════════════════════════════════

# ── Layer1 规则 ────────────────────────────────────────────────────────────
# 强纠正词：单独出现（或后跟任何名词性内容）即可判定
_CORRECTION_STRONG = [
    "你记错了", "记错了吧", "搞错了", "弄错了", "说错了", "理解错了",
    "记错了", "听错了", "记岔了",
]
# 中强纠正词：要求后跟名词/事实（≥1 个汉字或 2 个字符的实质内容）
_CORRECTION_NOUN_REQUIRED = [
    "不对", "错了", "不是", "别",
]
# 疑问/非纠正排除：命中则整句不算纠正（"是不是…？"是疑问，"别担心"是安慰）
_CORRECTION_EXCLUDE = [
    "是不是", "对不对", "不是吗", "不是吧", "别担心", "别急", "别怕",
    "别这样", "别客气", "别生气", "别难过", "别哭", "别闹",
    "别走", "别管", "别说了", "别慌", "别怕", "别想太多",
]
# 名词性后续的判定：紧跟 ≥1 个中文字符 或 ≥2 个 ASCII 字符（单词/数字）
_NON_EMPTY_TAIL = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]{2,}")
# 纯语气/标点结尾（"不对啊""不对。"→ 无实质名词，降级弱信号）
_WEAK_TAIL = re.compile(r"^[啊呀吧呢嘛哦嗯哈唉诶]?[，。！？,.!?\s]*$")


def detect_correction_layer1(text: str) -> dict | None:
    """Layer1 高精度纠正信号规则。

    规则（memory-v2-plan.md 环1）：
      "不对/错了/别/不是/搞错了/你记错了" + 后跟名词/事实 → 纠正信号

    返回 {"signal": "不对", "layer": 1, "matched": "...", "tail": "..."} 或 None。
    纯规则、零网络调用、零 LLM 成本。
    """
    if not text or not text.strip():
        return None
    t = text.strip()

    # 强信号词直接命中（优先级最高，不受排除表影响）
    for kw in _CORRECTION_STRONG:
        if kw in t:
            return {"signal": kw, "layer": 1, "matched": kw, "tail": ""}

    # 排除疑问/安慰句式（只作用于中强信号词）
    for excl in _CORRECTION_EXCLUDE:
        if excl in t:
            return None

    # 中强信号词：要求后跟名词/事实
    for kw in _CORRECTION_NOUN_REQUIRED:
        for m in re.finditer(re.escape(kw), t):
            tail = t[m.end():].strip(" ，。！？、：；,.!?;:")
            # 后跟名词性内容（≥1 汉字 或 ≥2 字母数字）→ 明确纠正
            if tail and _NON_EMPTY_TAIL.search(tail):
                return {"signal": kw, "layer": 1, "matched": kw, "tail": tail[:30]}
            # 尾部为空或纯语气词（"不对。""不对啊"）→ 弱信号，明确否定词也接受
            if not tail or _WEAK_TAIL.match(tail):
                return {"signal": kw, "layer": 1, "matched": kw, "tail": tail[:30]}
            # 尾部有非名词内容但非纯语气 → 不判断（留给 Layer2 flash 兜底）
    return None


# ── Layer2 flash 兜底 ──────────────────────────────────────────────────────
_NEGATION_WORDS = ["不对", "不是", "错了", "错", "别", "没有", "并不", "压根不", "才不是", "并非"]

def _flash_judge_correction(text: str) -> dict:
    """用 DeepSeek flash 判断: 用户消息是否在纠正 agent 之前的说法。"""
    key = _gate_api_key()
    if not key:
        return {"is_correction": False, "reason": "no-key"}
    prompt = (
        "判断下面这条用户消息是否在纠正/否定助手之前给出的信息或说法。\n"
        "只回复JSON: {\"is_correction\": true或false, \"reason\": \"一句话理由\"}\n"
        "注意: 疑问('是不是这样?')、安慰('别担心')、单纯表达观点不算纠正。\n\n"
        f"消息: {text[:300]}"
    )
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "temperature": 0,
        "reasoning_effort": "none",
    }
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            m = re.search(r'"is_correction"\s*:\s*(true|false)', content)
            is_c = m.group(1) == "true" if m else "true" in content.lower()
            reason_m = re.search(r'"reason"\s*:\s*"([^"]*)"', content)
            return {"is_correction": bool(is_c),
                    "reason": reason_m.group(1) if reason_m else content.strip()[:60]}
    except Exception as e:
        return {"is_correction": False, "reason": f"error:{type(e).__name__}"}


def detect_correction(text: str, layer2: bool = False) -> dict | None:
    """环1 纠正信号检测主入口：Layer1 规则优先，Layer2 flash 兜底。

    参数:
      text:   用户消息原文
      layer2: 是否启用 Layer2 flash 兜底（调用方必须控制频率 <5% 消息）

    返回统一结构 {"signal", "layer", "matched", "tail"} 或 None。
    """
    l1 = detect_correction_layer1(text)
    if l1:
        return l1
    if not layer2 or not text or not text.strip():
        return None
    # Layer2 门控：有否定词才值得 flash（否则绝大多数消息会被浪费）
    t = text.strip()
    if not any(w in t for w in _NEGATION_WORDS):
        return None
    cached = _cache_get("corr:" + t)
    if cached:
        if cached.get("verdict") == "CORRECTION":
            return {"signal": "flash-否定词兜底", "layer": 2,
                    "matched": cached.get("reason", ""), "tail": ""}
        return None
    res = _flash_judge_correction(t)
    _cache_put("corr:" + t, {
        "verdict": "CORRECTION" if res.get("is_correction") else "NOT",
        "reason": res.get("reason", ""),
    })
    if res.get("is_correction"):
        return {"signal": "flash-否定词兜底", "layer": 2,
                "matched": res.get("reason", ""), "tail": ""}
    return None


# ═══════════════════════════════════════════════════════════════════════════
# memorykit 类封装 — 兼容 OOP 调用（函数版 gate()/detect_correction() 保持原样）
# ═══════════════════════════════════════════════════════════════════════════

class MemoryGate:
    """记忆写入语义闸门（三分类 MEMORY/SKILL/DROP，带 24h 缓存）。

    是 gate() 函数的类封装，行为完全一致。缺 API key 时优雅降级：
    语义判定不可用时返回规则结果（no-key-fallback → 按记忆放行，不阻塞写入）。

    用法:
      mg = MemoryGate()
      result = mg.gate("这条内容值不值得作为长期记忆……")
      # → {"verdict": "MEMORY"|"SKILL"|"DROP", "reason": "..."}

      # 检测用户消息中的纠正信号（环1）
      sig = mg.detect_correction("不对，应该是550块", layer2=False)
    """

    def gate(self, text: str) -> dict:
        """三分类判断（等价于模块级 gate()）。"""
        return gate(text)

    def detect_correction(self, text: str, layer2: bool = False) -> dict | None:
        """纠正信号检测（等价于模块级 detect_correction()）。"""
        return detect_correction(text, layer2=layer2)
