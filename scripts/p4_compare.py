#!/usr/bin/env python3
"""P4 A vs B 对照：静态全量（A）vs 检索 digest + top-k（B）。

读取：
  var/p4-e1-results.json       （scripts/p4_e1_eval.py 产出，B）
  var/p4-static-baseline.json  （scripts/p4_static_baseline.py 产出，A）

产出：
  - A / B 的 R@1、R@3、MRR 对照（B 分 chunk/skill 与权重）
  - B 的 top-k 注入上下文字符数，以及用 DeepSeek tokenizer 实测的 token 数
  - 每 29 条 query 的 token 成本对比与节省比例

用法：
  ./.venv/bin/python scripts/p4_compare.py                 # 只做字符估算
  ./.venv/bin/python scripts/p4_compare.py --measure-tokens  # 额外 4 次 API 调用实测
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = os.path.expanduser("~/.hermes/config.yaml")
DEFAULT_WEIGHTS = (0.10, 0.25)


def payload_text(topk, k):
    parts = []
    for row in topk[:k]:
        head = f"{row['skill']} / {row['heading']}" if row["heading"] else row["skill"]
        parts.append(f"[{head}]\n{row['content']}")
    return "\n\n".join(parts)


def measure_prompt_tokens(cfg, provider, user_text):
    prov = cfg["providers"][provider]
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": prov["model"],
        "messages": [
            {"role": "system", "content": "以下是从知识库检索到的片段。"},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "max_tokens": 1,
        "stream": False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {prov['api_key']}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read())
    return int((body.get("usage") or {}).get("prompt_tokens") or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default=str(HERE / "var" / "p4-e1-results.json"))
    ap.add_argument("--static", default=str(HERE / "var" / "p4-static-baseline.json"))
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--provider", default="deepseek-v4-flash")
    ap.add_argument("--measure-tokens", action="store_true")
    ap.add_argument("--out", default=str(HERE / "var" / "p4-compare.json"))
    args = ap.parse_args()

    ev = json.loads(Path(args.eval).read_text(encoding="utf-8"))
    st = json.loads(Path(args.static).read_text(encoding="utf-8"))
    key = f"{DEFAULT_WEIGHTS[0]:.2f},{DEFAULT_WEIGHTS[1]:.2f}"

    print(f"# E1 n={ev['n_items']}  A=静态全量(deepseek-flash)  B=检索")
    print(f"# A: R@1={st['metrics_parsed_only']['r@1']}% "
          f"R@3={st['metrics_parsed_only']['r@3']}% "
          f"MRR={st['metrics_parsed_only']['mrr']} "
          f"(parsed {st['n_parsed']}/{st['n_items']})")

    # 选 B 最优配置（按 MRR，其次 R@1）
    best = None
    for scope in ("all", "skill"):
        for agg in ("chunk", "skill"):
            for wkey, m in ev["scopes"][scope][agg].items():
                cand = (m["mrr"], m["r@1"], scope, agg, wkey, m)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    _, _, b_scope, b_agg, b_wkey, b_m = best
    print(f"# B best: scope={b_scope} agg={b_agg} weights=({b_wkey}) "
          f"R@1={b_m['r@1']}% R@3={b_m['r@3']}% MRR={b_m['mrr']}")

    print("\n## 表格：B 全配置")
    for scope in ("skill", "all"):
        for agg in ("chunk", "skill"):
            for wkey, m in ev["scopes"][scope][agg].items():
                print(f"| {scope} | {agg} | {wkey} | {m['r@1']:.1f} | "
                      f"{m['r@3']:.1f} | {m['r@5']:.1f} | {m['r@10']:.1f} | "
                      f"{m['mrr']:.4f} |")

    # top-k 上下文成本：跟着 B 最优配置走（召回数字和 token 数字必须同配置）
    per_query = ev["scopes"][b_scope]["per_query"][b_wkey]
    costs = {}
    for k in (1, 3, 5, 10):
        chars = [len(payload_text(p["top10"], k)) for p in per_query]
        costs[k] = {
            "avg_chars": round(sum(chars) / len(chars), 1),
            "total_chars": sum(chars),
        }
    print(f"\n## B 注入上下文（scope={b_scope}, weights={b_wkey}）")
    for k, c in costs.items():
        print(f"- top-{k}: avg {c['avg_chars']} chars/query, "
              f"29 条合计 {c['total_chars']} chars")

    measured = {}
    if args.measure_tokens:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        base = measure_prompt_tokens(cfg, args.provider, "x")
        for k in (3, 10):
            text = "\n\n====\n\n".join(
                payload_text(p["top10"], k) for p in per_query)
            try:
                total = measure_prompt_tokens(cfg, args.provider, text)
            except Exception as exc:  # noqa: BLE001
                measured[k] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                print(f"- top-{k}: 实测失败 {measured[k]['error']}")
                continue
            measured[k] = {
                "prompt_tokens_total": total,
                "baseline_overhead": base,
                "tokens_total": max(0, total - base),
                "tokens_per_query": round(max(0, total - base) / len(per_query), 1),
            }
            print(f"- top-{k}: 实测 prompt_tokens={total}（空载 {base}）→ "
                  f"净 {measured[k]['tokens_total']}，"
                  f"每 query {measured[k]['tokens_per_query']} tok")

    a_tokens = st["tokens"]["avg_prompt_per_call"]
    # 用实测 top-3 比率换算 top-1/5/10（若未实测，用 A 的 chars/token 比例）
    if measured.get(3):
        ratio = measured[3]["tokens_total"] / max(1, costs[3]["total_chars"])
    else:
        ratio = st["tokens"]["avg_prompt_per_call"] / (st["system_prompt_chars"] / 29)
    est = {}
    for k, c in costs.items():
        if measured.get(k):
            toks = measured[k]["tokens_per_query"]
        else:
            toks = round(c["avg_chars"] * ratio, 1)
        est[k] = {
            "avg_chars": c["avg_chars"],
            "tokens_per_query": toks,
            "a_over_b": round(a_tokens / toks, 1) if toks else None,
        }

    out = {
        "a": {
            "metrics": st["metrics_parsed_only"],
            "n_parsed": st["n_parsed"],
            "n_unparseable": st["n_unparseable"],
            "system_prompt_chars": st["system_prompt_chars"],
            "prompt_tokens_avg": a_tokens,
            "prompt_tokens_total_29": st["tokens"]["prompt"],
            "completion_tokens_total_29": st["tokens"]["completion"],
            "total_tokens_29": st["tokens"]["total"],
            "cache_hit_tokens_29": st["tokens"]["cache_hit"],
            "cache_miss_tokens_29": st["tokens"]["cache_miss"],
        },
        "b": {
            "best": {"scope": b_scope, "agg": b_agg, "weights": b_wkey,
                     "metrics": b_m},
            "all": ev["scopes"],
            "context": est,
            "measured": measured,
            "chars_per_token": round(1 / ratio, 3) if ratio else None,
        },
        "eval_file": args.eval,
        "static_file": args.static,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
