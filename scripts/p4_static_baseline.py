#!/usr/bin/env python3
"""P4 2.4：A 方案基线 —— 静态全量索引的隐含召回率。

做法（与任务书一致）：
- 把活跃 248 个 skill 的完整 `name — description` 列表塞进 system prompt
  （不裁剪，等价于 Hermes 现在往系统提示里注入的全量清单）
- 29 条 E1 query（默认剔除 unreachable）逐条问 deepseek-flash：
  "只输出最该用的 1-3 个 skill 名，按优先级排序"
- 算 R@1 / R@3 / MRR，并记录每次 token 消耗
- 解析不出任何合法 skill 名的样本单独计数；指标同时给"可解析子集"和
  "不可解析按 0 分"两种口径，绝不静默当 0 分

用法：
  ./.venv/bin/python scripts/p4_static_baseline.py            # 29 条
  ./.venv/bin/python scripts/p4_static_baseline.py --dry-run   # 只构造 prompt 不发请求
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import split_frontmatter  # noqa: E402

DEFAULT_CONFIG = os.path.expanduser("~/.hermes/config.yaml")


def load_items(path: Path, include_unreachable: bool):
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"]
    dropped = [it["id"] for it in items if it.get("unreachable")]
    if include_unreachable:
        return items, []
    return [it for it in items if not it.get("unreachable")], dropped


def load_skills(root: Path):
    """活跃 skill 的 (name, description)，按目录排序。跳过任意层 .archive。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".archive")
        if "SKILL.md" not in filenames:
            continue
        p = Path(dirpath) / "SKILL.md"
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, _, _ = split_frontmatter(text)
        name = str(meta.get("name") or p.parent.name).strip()
        desc = " ".join(str(meta.get("description") or "").split())
        out.append((name, desc))
    out.sort(key=lambda x: x[0])
    return out


def build_system_prompt(skills):
    lines = [f"- {name} — {desc}" for name, desc in skills]
    return (
        "你是技能路由器。下面是当前可用的全部 skill（name — description）。\n"
        "用户会给出一句任务描述；你只能从下面列表里选择最该使用的技能。\n\n"
        "规则：\n"
        "1. 只输出 skill 的 name，每行一个，最多 3 个；\n"
        "2. 按优先级从高到低排序；\n"
        "3. 不要输出序号、解释、标点或任何其他文字；\n"
        "4. 必须逐字使用列表里的 name。\n\n"
        "可用 skill 列表：\n" + "\n".join(lines)
    )


def build_user_prompt(query):
    return f"任务：{query}\n\n只输出最该用的 1-3 个 skill name。"


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_names(text, valid_names):
    """从模型回复里提取合法 skill 名（保序、去重、最多 3 个）。"""
    found = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•\s]+", "", s)
        s = re.sub(r"^\d+[.)、]\s*", "", s)
        s = s.strip("`'\"“”‘’ \t")
        # 一行里可能带 "name: 理由" 或 "name（理由）" —— 取第一个 token
        cand = re.split(r"[\s，,：:（(]", s)[0].strip("`'\"“”‘’")
        if cand in valid_names and cand not in found:
            found.append(cand)
        if len(found) >= 3:
            break
    return found


def call_chat(base_url, api_key, model, system, user, timeout=120, retries=3):
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
            return body, round(time.time() - t0, 2)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = 2 * (attempt + 1)
            print(f"  [retry {attempt + 1}/{retries}] {type(exc).__name__}: "
                  f"{str(exc)[:160]}", flush=True)
            if attempt < retries - 1:
                time.sleep(wait)
    raise RuntimeError(f"API 调用失败（{retries} 次）：{last_err}")


def metrics(preds, expected_list, n_denom):
    ranks = []
    for pred, expected in zip(preds, expected_list):
        rank = None
        for i, name in enumerate(pred, 1):
            if name in expected:
                rank = i
                break
        ranks.append(rank)
    return {
        "n": n_denom,
        "r@1": round(sum(1 for r in ranks if r == 1) / n_denom * 100, 2),
        "r@3": round(sum(1 for r in ranks if r is not None and r <= 3) / n_denom * 100, 2),
        "mrr": round(sum((1.0 / r) for r in ranks if r is not None) / n_denom, 4),
    }, ranks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--provider", default="deepseek-v4-flash")
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--eval-set", default=str(HERE / "var" / "p4-eval-set-e1.json"))
    ap.add_argument("--out", default=str(HERE / "var" / "p4-static-baseline.json"))
    ap.add_argument("--include-unreachable", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    prov = (cfg.get("providers") or {}).get(args.provider)
    if not prov:
        sys.exit(f"config 里没有 provider {args.provider!r}")
    base_url, model = prov["base_url"], prov["model"]
    api_key = prov["api_key"]

    skills = load_skills(Path(args.root))
    valid = {name for name, _ in skills}
    system = build_system_prompt(skills)
    items, dropped = load_items(Path(args.eval_set), args.include_unreachable)
    if args.limit:
        items = items[:args.limit]
    print(f"# skills={len(skills)} system_chars={len(system)} "
          f"items={len(items)} dropped={dropped}", flush=True)
    if args.dry_run:
        print(system[:1200])
        print("...")
        print(f"\n# dry-run only; total system chars={len(system)}")
        return

    records = []
    preds = []
    prompt_tokens = completion_tokens = total_tokens = 0
    cache_hit = cache_miss = 0
    t_all = time.time()
    for i, item in enumerate(items, 1):
        user = build_user_prompt(item["q"])
        body, latency = call_chat(base_url, api_key, model, system, user)
        msg = (body.get("choices") or [{}])[0].get("message", {}) or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        usage = body.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        prompt_tokens += pt
        completion_tokens += ct
        total_tokens += tt
        cache_hit += int(usage.get("prompt_cache_hit_tokens") or 0)
        cache_miss += int(usage.get("prompt_cache_miss_tokens") or 0)
        names = parse_names(content, valid)
        expected = set(item["expected"])
        status = "ok" if names else "unparseable"
        preds.append(names)
        records.append({
            "id": item["id"], "type": item.get("type"), "q": item["q"],
            "expected": sorted(expected), "raw_response": content,
            "reasoning_content": reasoning,
            "predicted": names, "parse_status": status,
            "hit_rank": next((i for i, n in enumerate(names, 1) if n in expected), None),
            "usage": usage, "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": tt, "latency_s": latency,
            "model": body.get("model"), "response_id": body.get("id"),
        })
        print(f"[{i}/{len(items)}] {item['q'][:34]}... "
              f"pred={names or '(UNPARSEABLE)'} hit={records[-1]['hit_rank']} "
              f"tokens={tt}", flush=True)

    expected_list = [set(it["expected"]) for it in items]
    n_all = len(items)
    parsed_idx = [i for i, r in enumerate(records) if r["parse_status"] == "ok"]
    n_parsed = len(parsed_idx)
    unparseable = [r["id"] for r in records if r["parse_status"] != "ok"]

    m_parsed, ranks_parsed = metrics(
        [preds[i] for i in parsed_idx], [expected_list[i] for i in parsed_idx],
        max(1, n_parsed))
    # 严格口径：不可解析 = 0 分（分母仍是全部样本）
    ranks_strict = []
    for i, r in enumerate(records):
        if r["parse_status"] != "ok":
            ranks_strict.append(None)
        else:
            ranks_strict.append(next((j for j, n in enumerate(preds[i], 1)
                                      if n in expected_list[i]), None))
    m_strict = {
        "n": n_all,
        "r@1": round(sum(1 for r in ranks_strict if r == 1) / n_all * 100, 2),
        "r@3": round(sum(1 for r in ranks_strict
                          if r is not None and r <= 3) / n_all * 100, 2),
        "mrr": round(sum((1.0 / r) for r in ranks_strict if r is not None) / n_all, 4),
    }
    payload = {
        "method": "A 方案：248 个 skill 的 name+description 全量进 system prompt，"
                  "deepseek-flash 自选 1-3 个",
        "provider": args.provider,
        "model": model,
        "n_skills": len(skills),
        "system_prompt_chars": len(system),
        "system_prompt_tokens_reported": records[0]["prompt_tokens"] if records else 0,
        "n_items": n_all,
        "dropped_unreachable": dropped,
        "n_parsed": n_parsed,
        "n_unparseable": len(unparseable),
        "unparseable_ids": unparseable,
        "metrics_parsed_only": m_parsed,
        "metrics_unparseable_as_zero": m_strict,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
            "avg_prompt_per_call": round(prompt_tokens / max(1, n_all), 1),
            "avg_total_per_call": round(total_tokens / max(1, n_all), 1),
            "cache_hit": cache_hit,
            "cache_miss": cache_miss,
        },
        "elapsed_s": round(time.time() - t_all, 1),
        "records": records,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n## A 方案基线")
    print(f"- skills={len(skills)} system_chars={len(system)} "
          f"prompt_tokens/次≈{payload['tokens']['avg_prompt_per_call']}")
    print(f"- 可解析子集 n={n_parsed}: R@1={m_parsed['r@1']}% "
          f"R@3={m_parsed['r@3']}% MRR={m_parsed['mrr']}")
    print(f"- 严格口径 n={n_all}（不可解析=0）: R@1={m_strict['r@1']}% "
          f"R@3={m_strict['r@3']}% MRR={m_strict['mrr']}")
    print(f"- 不可解析 {len(unparseable)} 条: {unparseable}")
    print(f"- tokens: prompt={prompt_tokens} completion={completion_tokens} "
          f"total={total_tokens} cache_hit={cache_hit}")
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
