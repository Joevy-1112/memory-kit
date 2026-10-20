#!/usr/bin/env python3
"""P2 权重敏感性：固定 5 query，w_e × w_l 各扫 3 值，看 top-3 是否变化。

用法：
  export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
  ./.venv/bin/python scripts/p2_weight_sensitivity.py
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import CorpusIndex  # noqa: E402

QUERIES = [
    "V100 上跑 vLLM 显存不够怎么办",
    "strata 解码速度慢",
    "飞书回复被截断，卡片显示不全",
    "耳机电池不行了，想改成 type-c 充电",
    "无穷小的阶 泰勒展开怎么算",
]
WE_VALUES = [0.0, 0.10, 0.25]
WL_VALUES = [0.0, 0.25, 0.50]
BASE_WE, BASE_WL = 0.10, 0.25


def sig(results, n=3):
    return [(r["skill_name"], r["heading"] or "(preamble)")
            for r in results[:n]]


def compact(names):
    """把连续重复技能压成 A×2，避免 3 个同名撑满表格。"""
    out = []
    for n in names:
        if out and out[-1][0] == n:
            out[-1][1] += 1
        else:
            out.append([n, 1])
    return " > ".join(f"{n}×{c}" if c > 1 else n for n, c in out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEMORY_DB_PATH"))
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--out", default=str(HERE / "var" / "p2-weight-sensitivity.json"))
    args = ap.parse_args()

    out = {"queries": QUERIES, "we_values": WE_VALUES,
           "wl_values": WL_VALUES, "base": {"we": BASE_WE, "wl": BASE_WL},
           "records": []}
    with CorpusIndex(args.db, args.root, quiet=True) as ci:
        for q in QUERIES:
            combos = []
            for we in WE_VALUES:
                for wl in WL_VALUES:
                    r = ci.query(q, top_k=3, w_entity=we, w_lexical=wl)
                    combos.append({
                        "we": we, "wl": wl,
                        "top3": sig(r["results"]),
                        "scores": [{"skill": x["skill_name"],
                                    "heading": x["heading"],
                                    "cos": x["cos"],
                                    "entity": x["entity_hits"],
                                    "lexical": x["lexical_norm"],
                                    "total": x["total"]}
                                   for x in r["results"]],
                    })
            base = next(c for c in combos
                        if c["we"] == BASE_WE and c["wl"] == BASE_WL)
            top1_chunk = {c["top3"][0] if c["top3"] else None for c in combos}
            top3_chunk = {tuple(c["top3"]) for c in combos}
            top1_skill = {c["top3"][0][0] if c["top3"] else None for c in combos}
            top3_skill = {tuple(x[0] for x in c["top3"]) for c in combos}
            rec = {"query": q, "combos": combos,
                   "distinct_top1_chunk": len(top1_chunk),
                   "distinct_top3_chunk": len(top3_chunk),
                   "distinct_top1_skill": len(top1_skill),
                   "distinct_top3_skill": len(top3_skill),
                   "top1_changed": len(top1_skill) > 1,
                   "top3_changed": len(top3_skill) > 1,
                   "base_top3": base["top3"]}
            out["records"].append(rec)

            print(f"\n### {q}")
            print("| w_e \\ w_l | " + " | ".join(f"{wl:.2f}" for wl in WL_VALUES) + " |")
            print("|---|" + "---|" * len(WL_VALUES))
            for we in WE_VALUES:
                cells = []
                for wl in WL_VALUES:
                    c = next(x for x in combos if x["we"] == we and x["wl"] == wl)
                    cell = compact([x[0] for x in c["top3"]]) or "(empty)"
                    cells.append(cell)
                print(f"| {we:.2f} | " + " | ".join(cells) + " |")
            print(f"- distinct top1 skill={rec['distinct_top1_skill']} "
                  f"top3 skill={rec['distinct_top3_skill']} "
                  f"top1 chunk={rec['distinct_top1_chunk']} "
                  f"top3 chunk={rec['distinct_top3_chunk']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
