#!/usr/bin/env python3
"""P2 诊断：5 个冻结 query 的期望技能在 top-10 里的排名（不参与调参）。

用法：
  export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
  ./.venv/bin/python scripts/p2_expected_ranks.py
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import CorpusIndex  # noqa: E402

CASES = [
    ("V100 上跑 vLLM 显存不够怎么办", ["v100-llm-inference", "serving-llms-vllm"]),
    ("strata 解码速度慢", ["local-gguf-serving"]),
    ("飞书回复被截断，卡片显示不全", ["feishu-message-size-limits"]),
    ("耳机电池不行了，想改成 type-c 充电",
     ["windows-peripherals", "windows-system-diagnostics"]),
    ("无穷小的阶 泰勒展开怎么算", ["teaching-concepts-quiz"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEMORY_DB_PATH"))
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--out", default=str(HERE / "var" / "p2-expected-ranks.json"))
    args = ap.parse_args()
    out = []
    with CorpusIndex(args.db, args.root, quiet=True) as ci:
        for q, expected in CASES:
            r = ci.query(q, top_k=10)
            ranks = {}
            for e in expected:
                ranks[e] = [i for i, x in enumerate(r["results"], 1)
                            if x["skill_name"] == e]
            out.append({"query": q, "expected": expected, "ranks": ranks,
                        "entities": r["entities"],
                        "top10": [{"rank": i, "skill": x["skill_name"],
                                   "heading": x["heading"], "total": x["total"]}
                                  for i, x in enumerate(r["results"], 1)]})
            print(f"\n{q}")
            for e in expected:
                print(f"  {e}: ranks={ranks[e] or 'not in top10'}")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}")


if __name__ == "__main__":
    main()
