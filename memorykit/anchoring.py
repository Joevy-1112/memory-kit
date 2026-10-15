#!/usr/bin/env python3
"""记忆锚定模块 — P1 写入侧经验锚定 (2026-08-29)。

提案: 新记忆 learn() 入库前先检索 top-k 相似旧记忆, 让模型显式输出
"与 [[旧记忆X]] 的关系(延续/修正/无关)"再存储。语义消歧从 FFN 参数记忆
搬到情景记忆(检索本机对话史)。

本模块提供:
  - AVAILABLE_RELATIONS: 受控关系词表 {"延续","修正","无关"}。
  - classify_relation(new_content, candidates, llm=None): 分类新记忆与
    top-k 候选旧记忆的关系。llm 可调用时走模型(结构化 JSON); llm=None 或
    调用失败时降级到纯规则(实体重叠 + 相似度阈值) —— 安静降级, 绝不阻塞
    写入路径(every failure path returns a well-formed dict)。
  - HTTPAnchorLLM: OpenAI 兼容 chat/completions 客户端。默认指向 opendesign
    反代 @8792 / deepseek-v4-flash(本地免费零成本), 可通过 env 覆盖:
      MEMORY_ANCHOR_LLM_URL / MEMORY_ANCHOR_LLM_KEY / MEMORY_ANCHOR_LLM_MODEL

设计原则:
  - 零硬依赖: 仅标准库 urllib/json/re。
  - 纯函数核心, 易于单测(假 LLM / 假 candidates)。
  - 所有 LLM 调用失败都返回 dict(不抛异常), 由调用方决定入库策略。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request

logger = logging.getLogger(__name__)

AVAILABLE_RELATIONS = ("延续", "修正", "无关")

# 默认 LLM 端点: deepseek 官方直连(有 reasoning_content 的模型须 max_tokens≥600 才出 content)。
# 可通过 env 覆盖(见模块 docstring)。
DEFAULT_LLM_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"


class HTTPAnchorLLM:
    """OpenAI 兼容 chat/completions 客户端, 供 classify_relation 使用。

    读环境变量(惰性):
      MEMORY_ANCHOR_LLM_URL   默认 https://api.deepseek.com/v1 (官方直连)
      MEMORY_ANCHOR_LLM_KEY   默认读 config.yaml providers.deepseek-v4-flash.api_key
      MEMORY_ANCHOR_LLM_MODEL 默认 deepseek-v4-flash

    timeout 默认 15s; temperature=0; 任何网络/解析异常返回 None(不抛出),
    由 classify_relation 降级到规则 fallback。
    """

    def __init__(self, url: str | None = None, key: str | None = None,
                 model: str | None = None, timeout: int = 15):
        self.url = url or os.environ.get("MEMORY_ANCHOR_LLM_URL", DEFAULT_LLM_URL)
        self.key = key if key is not None else _default_key()
        self.model = model or os.environ.get("MEMORY_ANCHOR_LLM_MODEL", DEFAULT_LLM_MODEL)
        self.timeout = timeout

    def chat(self, prompt: str, max_tokens: int = 600) -> str | None:
        """调用 chat/completions, 返回 assistant content; 失败返回 None。

        max_tokens 默认 600: deepseek-v4-flash/glm-5.3 等先出 reasoning_content,
        小 max_tokens 会把它截断成空 content(实测坑)。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            req = urllib.request.Request(
                self.url.rstrip("/") + "/chat/completions",
                data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            return ((body.get("choices") or [{}])[0]
                    .get("message", {}).get("content", ""))
        except Exception as e:  # noqa: BLE001 — 安静降级
            logger.warning("[anchoring] LLM chat failed: %s", type(e).__name__)
            return None


def _default_key() -> str:
    """默认 key: env MEMORY_ANCHOR_LLM_KEY → config.yaml providers.deepseek-v4-flash.api_key。
    找不到返回 ""(此时上游会 401, 触发规则降级——安全网设计)。"""
    env = os.environ.get("MEMORY_ANCHOR_LLM_KEY", "").strip()
    if env:
        return env
    try:
        # 标准库正则解析(避免 yaml 依赖): 取 providers.deepseek-v4-flash 段内的 api_key 行
        txt = open(os.path.expanduser("~/.hermes/config.yaml"), encoding="utf-8").read()
        seg = txt.split("providers:", 1)[1] if "providers:" in txt else txt
        seg = seg.split("deepseek-v4-flash:", 1)[1] if "deepseek-v4-flash:" in seg else seg
        seg = seg.split("\n  deepseek-v4-pro:", 1)[0] if "\n  deepseek-v4-pro:" in seg else seg
        m = re.search(r"api_key\s*:\s*[\"']?([^\"'\n]+)", seg)
        if m:
            return m.group(1).strip()
        return ""
    except Exception:  # noqa: BLE001 — 读不到 config 就无 key
        return ""


def _extract_json(text: str) -> dict | None:
    """宽松提取 JSON 对象(容忍 ```json 围栏 / 前后噪音)。失败返回 None。"""
    if not text:
        return None
    text = text.strip()
    # 去 markdown 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # 直接尝试
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    # 提取首个 {...} 块
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _norm_relation(value) -> str:
    """把模型/规则输出归一化到受控词表。未知值归入 '无关'。"""
    if not value:
        return "无关"
    v = str(value).strip()
    for rel in AVAILABLE_RELATIONS:
        if rel in v:
            return rel
    # 兼容英文同义词
    low = v.lower()
    if low in ("continue", "extends", "延续", "extend"):
        return "延续"
    if low in ("correct", "revise", "修正", "update", "modify", "conflict"):
        return "修正"
    return "无关"


# 规则 fallback: 关系判定的关键词对(hit 新内容 → 倾向修正)
_CORRECTION_HINTS = ("上涨", "降价", "改了", "更新", "修正", "纠正", "不对",
                     "错了", "不再", "不是", "涨价", "降到", "升到", "变化")
_CONTINUATION_HINTS = ("也是", "仍然", "继续", "同样", "再", "又", "依然")


def _rule_relation(new_content: str, best: dict) -> str:
    """纯规则关系判定: 基于新内容里的修正/延续线索 + 候选相似度。

    - 新内容含修正线索(上涨/改了/不是...) → '修正'
    - 新内容含延续线索 → '延续'
    - 否则按相似度高低给 '延续'(高相似) / '无关'(低相似)
    非零实体重叠作为前提(完全无关的实体集不锚定)。
    """
    lower = (new_content or "").lower()
    for kw in _CORRECTION_HINTS:
        if kw in lower:
            return "修正"
    for kw in _CONTINUATION_HINTS:
        if kw in lower:
            return "延续"
    sim = float(best.get("similarity") or 0.0)
    overlap = int(best.get("entity_overlap") or 0)
    if overlap >= 1 and sim >= 0.70:
        return "延续"
    if overlap >= 1:
        return "延续"  # 有实体重叠即视为同一主题延续
    return "无关"


def classify_relation(new_content: str, candidates: list, llm=None) -> dict:
    """分类新记忆与 top-k 候选旧记忆的关系。

    参数:
      new_content: 待入库的新记忆内容。
      candidates:  检索到的候选列表, 每项 dict, 至少含 key/content,
                   可含 id/similarity/entity_overlap。空列表 → 返回 '无关'。
      llm:         可调用 `llm(prompt) -> str`(如 HTTPAnchorLLM.chat)。
                   None 或调用失败 → 纯规则 fallback。

    返回稳定 dict:
      {relation, anchored_key, anchored_id, reason, source, model,
       top_candidates}
      relation ∈ AVAILABLE_RELATIONS;
      anchored_key/anchored_id = 被锚定的旧记忆(仅 relation∈{延续,修正} 或有
        最相似候选时给, 否则 None);
      source = "llm" | "rule";
      model = LLM 模型名(仅 llm 路径) | None;
      top_candidates = 原样候选(供上层写 provenance 参考)。
    """
    candidates = candidates or []
    if not candidates:
        return {"relation": "无关", "anchored_key": None, "anchored_id": None,
                "reason": "no-candidates", "source": "rule", "model": None,
                "top_candidates": []}

    # 候选已按相似度降序(pre-sorted by search_multi); 取 top 作最相似参照
    top = candidates[0]

    # ---- LLM 路径 ----
    if llm is not None:
        try:
            prompt = _build_prompt(new_content, candidates)
            raw = llm(prompt) if callable(llm) else llm.chat(prompt)
            obj = _extract_json(raw) if isinstance(raw, str) and raw else None
            if obj is not None:
                relation = _norm_relation(obj.get("relation"))
                anchored_key = obj.get("anchored_key") or top.get("key")
                reason = str(obj.get("reason") or "")[:200]
                anchored_id = _find_id(candidates, anchored_key) or top.get("id")
                model = getattr(llm, "model", None)
                return {"relation": relation, "anchored_key": anchored_key,
                        "anchored_id": anchored_id, "reason": reason,
                        "source": "llm", "model": model,
                        "top_candidates": candidates}
        except Exception as e:  # noqa: BLE001
            logger.warning("[anchoring] llm classify failed: %s", type(e).__name__)

    # ---- 规则 fallback ----
    relation = _rule_relation(new_content, top)
    anchored_key = top.get("key") if relation in ("延续", "修正") else None
    anchored_id = top.get("id") if relation in ("延续", "修正") else None
    return {"relation": relation, "anchored_key": anchored_key,
            "anchored_id": anchored_id,
            "reason": "rule-fallback" + ((":" + str(top.get("key"))) if anchored_key else ""),
            "source": "rule", "model": None,
            "top_candidates": candidates}


def _find_id(candidates, key):
    if not key:
        return None
    for c in candidates:
        if c.get("key") == key:
            return c.get("id")
    return None


def _build_prompt(new_content: str, candidates: list) -> str:
    """构造关系分类 prompt。要求输出受控 JSON。"""
    cand_lines = []
    for i, c in enumerate(candidates, 1):
        cand_lines.append(
            f"[候选{i}] key={c.get('key')} 相似度={c.get('similarity', '-')} "
            f"内容={str(c.get('content', ''))[:120]}")
    cand_block = "\n".join(cand_lines)
    return (
        "你在为一条新记忆标注它与已有记忆的关系(经验锚定)。\n"
        "新记忆:\n" + str(new_content)[:500] + "\n\n"
        "检索到的相似旧记忆:\n" + cand_block + "\n\n"
        "请判断新记忆与哪个旧记忆的关系。只输出 JSON:\n"
        '{"relation":"延续|修正|无关", "anchored_key":"<最相关的旧记忆key>", '
        '"reason":"一句话理由"}\n'
        "- 延续: 新记忆是旧记忆的补充/延续/补充细节\n"
        "- 修正: 新记忆纠正/更新了旧记忆(如价格/状态/结论变化)\n"
        "- 无关: 与所有旧记忆实质无关\n"
        "anchored_key 从候选 key 里选; 若无相关候选则回空串。"
    )
