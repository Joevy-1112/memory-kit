#!/usr/bin/env python3
"""P2 切块统计（只切块，不嵌入、不写库）。

用法：
  ./.venv/bin/python scripts/p2_chunk_stats.py --root ~/.hermes/skills
"""
import argparse
import json
import os
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import (  # noqa: E402
    MAX_CHUNK_CHARS, chunk_skill_text, skill_name_for)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = Path(os.path.expanduser(args.root))
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".archive")
        if "SKILL.md" in filenames:
            files.append(Path(dirpath) / "SKILL.md")
    files.sort()

    total = 0
    lengths = []
    types = {}
    over = []
    per_skill = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        name = skill_name_for(p, text)
        chunks = chunk_skill_text(text, name, MAX_CHUNK_CHARS)
        total += len(chunks)
        lengths.extend(len(c.content) for c in chunks)
        for c in chunks:
            types[c.chunk_type] = types.get(c.chunk_type, 0) + 1
            if len(c.content) > MAX_CHUNK_CHARS:
                over.append({"path": str(p.relative_to(root)),
                             "heading": c.heading,
                             "chars": len(c.content)})
        per_skill.append({"skill": name,
                          "path": str(p.relative_to(root)),
                          "chunks": len(chunks)})
    per_skill.sort(key=lambda x: -x["chunks"])
    out = {
        "root": str(root),
        "files": len(files),
        "chunks": total,
        "chunk_types": types,
        "chars_mean": round(statistics.mean(lengths), 2),
        "chars_median": statistics.median(lengths),
        "chars_p95": sorted(lengths)[int(len(lengths) * 0.95)],
        "chars_max": max(lengths),
        "over_limit": over,
        "top_skills_by_chunks": per_skill[:15],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: v for k, v in out.items()
                          if k != "top_skills_by_chunks"},
                         ensure_ascii=False, indent=2))
        print("top skills by chunks:")
        for x in per_skill[:10]:
            print(f"  {x['chunks']:4d}  {x['skill']}  ({x['path']})")


if __name__ == "__main__":
    main()
