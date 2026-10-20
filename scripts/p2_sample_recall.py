#!/usr/bin/env python3
"""P2 召回抽样：5 个冻结 query，每个贴 top-3 的 skill/heading/三项分/总分。

用法：
  export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
  ./.venv/bin/python scripts/p2_sample_recall.py
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import CorpusIndex, W_ENTITY, W_LEXICAL  # noqa: E402

# 冻结的 5 个 query（结果出来之前先写死，避免事后挑题）
QUERIES = [
    "V100 上跑 vLLM 显存不够怎么办",
    "strata 解码速度慢",
    "飞书回复被截断，卡片显示不全",
    "耳机电池不行了，想改成 type-c 充电",
    "无穷小的阶 泰勒展开怎么算",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEMORY_DB_PATH"))
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--out", default=str(HERE / "var" / "p2-sample-recall.json"))
    args = ap.parse_args()

    with CorpusIndex(args.db, args.root, quiet=True) as ci:
        records = []
        for q in QUERIES:
            records.append(ci.query(q, top_k=3))
        stats = ci.stats()

    print(f"# corpus stats: {json.dumps(stats, ensure_ascii=False)}")
    print()
    print("| # | query | rank | skill | chunk heading | cos | entity | lexical | total |")
    print("|---|---|---|---|---|---|---|---|---|")
    for qi, rec in enumerate(records, 1):
        for rank, r in enumerate(rec["results"], 1):
            heading = (r["heading"] or "(preamble)").replace("|", "\\|")
            print(f"| {qi} | {rec['query']} | {rank} | {r['skill_name']} | "
                  f"{heading} | {r['cos']:.4f} | {r['entity_hits']} | "
                  f"{r['lexical_norm']:.4f} | {r['total']:.4f} |")
    print()
    for rec in records:
        print(f"- `{rec['query']}` entities={rec['entities']} tokens={rec['tokens']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"weights": {"entity": W_ENTITY, "lexical": W_LEXICAL},
                   "queries": QUERIES, "stats": stats, "records": records},
                  f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
