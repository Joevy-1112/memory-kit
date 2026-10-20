#!/usr/bin/env python3
"""P4 补充实验：B 的端到端形态 —— 检索 top-k 候选 + 同一个 LLM 选择器。

为什么补：§2.4 的 A 是"全量列表 + deepseek-flash 选"，而 E1 的 B 指标是纯
检索排序（没有模型参与）。直接拿 A 的 R@1 比 B 的检索 R@1 对 B 不公平。
这里把 B 补成端到端：

  1. 用当前索引（all scope、best 权重 0/0.25）对每条 query 取 **skill 级**
     top-k 候选（每 skill 取最高分 chunk）；
  2. 把这 k 个候选的 name — description 按字母序（不泄露检索名次）塞进
     system prompt；
  3. 用同一个 deepseek-flash、同一套"只输出 1-3 个 name"规则选择；
  4. 算 R@1/R@3/MRR + token，并记录候选集是否包含正确 skill（召回上限）。

用法：
  ./.venv/bin/python scripts/p4_b_llm.py
  ./.venv/bin/python scripts/p4_b_llm.py --ks 3 10
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

from p4_static_baseline import (  # noqa: E402
    call_chat, load_items, load_skills, metrics, parse_names,
)

DEFAULT_CONFIG = os.path.expanduser("~/.hermes/config.yaml")


def build_candidate_prompt(cands):
    lines = [f"- {name} — {desc}" for name, desc in cands]
    return (
        "你是技能路由器。下面是检索到的候选 skill（name — description）。\n"
        "用户会给出一句任务描述；你只能从下面列表里选择最该使用的技能。\n\n"
        "规则：\n"
        "1. 只输出 skill 的 name，每行一个，最多 3 个；\n"
        "2. 按优先级从高到低排序；\n"
        "3. 不要输出序号、解释、标点或任何其他文字；\n"
        "4. 必须逐字使用列表里的 name。\n\n"
        "候选 skill 列表：\n" + "\n".join(lines)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default=str(HERE / "var" / "p4-e1-results.json"))
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--provider", default="deepseek-v4-flash")
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--eval-set", default=str(HERE / "var" / "p4-eval-set-e1.json"))
    ap.add_argument("--scope", default="all")
    ap.add_argument("--weights", default="0.00,0.25")
    ap.add_argument("--ks", type=int, nargs="+", default=[3, 10])
    ap.add_argument("--out", default=str(HERE / "var" / "p4-b-llm.json"))
    args = ap.parse_args()

    ev = json.loads(Path(args.eval).read_text(encoding="utf-8"))
    per_query = ev["scopes"][args.scope]["per_query"][args.weights]
    items, dropped = load_items(Path(args.eval_set), include_unreachable=False)
    by_id = {it["id"]: it for it in items}
    meta = dict(load_skills(Path(args.root)))

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    prov = cfg["providers"][args.provider]

    results = {}
    for k in args.ks:
        records = []
        preds = []
        ptot = ctot = ttot = hit_cache = miss_cache = 0
        contain = 0
        t0 = time.time()
        print(f"\n## k={k}", flush=True)
        for p in per_query:
            item = by_id.get(p["id"])
            if item is None:
                continue
            cands = [c["skill"] for c in p["skill_top10"][:k]]
            cands = sorted(set(cands))
            expected = set(item["expected"])
            if expected & set(cands):
                contain += 1
            cand_meta = [(n, meta.get(n, "")) for n in cands]
            system = build_candidate_prompt(cand_meta)
            user = f"任务：{item['q']}\n\n只输出最该用的 1-3 个 skill name。"
            body, latency = call_chat(
                prov["base_url"], prov["api_key"], prov["model"], system, user)
            msg = (body.get("choices") or [{}])[0].get("message", {}) or {}
            content = msg.get("content") or ""
            usage = body.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            tt = int(usage.get("total_tokens") or (pt + ct))
            ptot += pt
            ctot += ct
            ttot += tt
            hit_cache += int(usage.get("prompt_cache_hit_tokens") or 0)
            miss_cache += int(usage.get("prompt_cache_miss_tokens") or 0)
            names = parse_names(content, set(cands))
            preds.append(names)
            rank = next((i for i, n in enumerate(names, 1) if n in expected), None)
            records.append({
                "id": item["id"], "type": item.get("type"), "q": item["q"],
                "expected": sorted(expected), "candidates": cands,
                "candidate_contains_expected": bool(expected & set(cands)),
                "raw_response": content, "predicted": names,
                "parse_status": "ok" if names else "unparseable",
                "hit_rank": rank, "usage": usage, "prompt_tokens": pt,
                "completion_tokens": ct, "total_tokens": tt,
                "latency_s": latency,
            })
            print(f"[{len(records)}] {item['q'][:30]}... pred={names or '(none)'} "
                  f"hit={rank} tok={tt}", flush=True)

        n = len(records)
        expected_list = [set(r["expected"]) for r in records]
        m, ranks = metrics(preds, expected_list, max(1, n))
        m["candidate_recall_at_k"] = round(contain / max(1, n) * 100, 2)
        m["n_unparseable"] = sum(1 for r in records
                                 if r["parse_status"] != "ok")
        m["tokens"] = {
            "prompt": ptot, "completion": ctot, "total": ttot,
            "avg_prompt": round(ptot / max(1, n), 1),
            "avg_total": round(ttot / max(1, n), 1),
            "cache_hit": hit_cache, "cache_miss": miss_cache,
        }
        m["elapsed_s"] = round(time.time() - t0, 1)
        results[str(k)] = {"metrics": m, "records": records}
        print(f"k={k}: R@1={m['r@1']} R@3={m['r@3']} MRR={m['mrr']} "
              f"cand_recall@{k}={m['candidate_recall_at_k']} "
              f"unparseable={m['n_unparseable']} "
              f"prompt/query={m['tokens']['avg_prompt']}", flush=True)

    payload = {
        "method": "B 端到端：检索 top-k skill 候选 + deepseek-flash 选择",
        "provider": args.provider, "model": prov["model"],
        "scope": args.scope, "weights": args.weights,
        "n_items": len(items), "dropped_unreachable": dropped,
        "results": results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
