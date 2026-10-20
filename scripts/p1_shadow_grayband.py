#!/usr/bin/env python3
"""P1-6 灰带 shadow 实测: 沙箱库真实向量对 → would_veto 统计（2026-10-20）。

做什么:
  1. 用 SQLite backup 把沙箱库复制到临时文件（**绝不写源库**）；
  2. 2096 条带向量记忆两两算余弦，取 [0.85, 0.92) 的灰带对；
  3. 用 learn 的同一套融合公式 _fusion_score(sim, overlap) 复算 fusion
     （overlap 取双方库内 entities 交集），保留 boost 后仍在灰带的对；
  4. 每个候选跑 anchoring.classify_relation（--llm 时用真实 HTTPAnchorLLM，
     否则纯规则兜底），再用 _merge_decision(mode=shadow, anchor_mode=shadow)
     计算 would_veto；
  5. 输出 relation 分布 / degraded 比例 / would_veto 计数 / 样本 key，并把
     逐对明细写 JSONL（可 --resume）。

用法:
  ./.venv/bin/python scripts/p1_shadow_grayband.py --llm --limit 200
  ./.venv/bin/python scripts/p1_shadow_grayband.py            # 纯规则（无网络）
"""
import argparse
import collections
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from memorykit.anchoring import HTTPAnchorLLM, classify_relation  # noqa: E402
from memorykit.lifecycle import (  # noqa: E402
    MERGE_FLOOR, MERGE_HIGH_THRESHOLD, SOURCE_VECTOR, _fusion_score,
    _merge_decision)

DEFAULT_DB = os.path.join(HERE, "var", "memory-sandbox.db")


def copy_db(src):
    """sqlite3 backup → 临时副本（读源库，不写源库）。"""
    fd, dst = tempfile.mkstemp(prefix="p1-shadow-", suffix=".db")
    os.close(fd)
    os.remove(dst)
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def parse_entities(raw):
    if not raw:
        return set()
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return set()
    if isinstance(v, list):
        return {str(x) for x in v if x}
    return set()


def load_rows(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, key, content, entities, vector_bin, importance, "
        "created_at FROM memories "
        "WHERE vector IS NOT NULL AND COALESCE(status,'active') != 'merged' "
        "AND COALESCE(vector_stale,0) = 0 ORDER BY id").fetchall()]
    conn.close()
    out = []
    for r in rows:
        raw = r.get("vector_bin")
        if not raw or len(raw) % 4:
            continue
        v = np.frombuffer(raw, dtype=np.float32)
        if v.size == 0:
            continue
        r["vec"] = v
        r["ents"] = parse_entities(r.get("entities"))
        out.append(r)
    return out


def gray_pairs(rows):
    """返回 boost 后仍落在 [0.85,0.92) 的真实向量对。"""
    X = np.stack([r["vec"] for r in rows]).astype(np.float32)
    norms = np.linalg.norm(X, axis=1)
    sims = (X @ X.T) / np.maximum(norms[:, None] * norms[None, :], 1e-10)
    np.fill_diagonal(sims, -1.0)
    raw = 0
    pairs = []
    n = len(rows)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sims[i, j])
            if not (0.85 <= s < 0.92):
                continue
            raw += 1
            overlap = len(rows[i]["ents"] & rows[j]["ents"])
            fusion = _fusion_score(s, overlap)
            if 0.85 <= fusion < 0.92:
                pairs.append({
                    "i": i, "j": j, "sim": s, "fusion": float(fusion),
                    "overlap": overlap,
                })
    pairs.sort(key=lambda p: p["fusion"], reverse=True)
    return raw, pairs


def run_one(pair, rows, llm):
    cand = rows[pair["i"]]
    new = rows[pair["j"]]
    cand_dict = {
        "key": cand["key"], "id": cand["id"],
        "content": cand.get("content") or "",
        "similarity": round(pair["sim"], 6),
        "entity_overlap": pair["overlap"],
    }
    rec = {
        "keep_key": cand["key"], "remove_key": new["key"],
        "sim": round(pair["sim"], 6), "fusion": round(pair["fusion"], 6),
        "overlap": pair["overlap"],
        "old_excerpt": (cand.get("content") or "")[:80],
        "new_excerpt": (new.get("content") or "")[:80],
    }
    for mode, client in (("rule", None), ("llm", llm)):
        if mode == "llm" and llm is None:
            continue
        t0 = time.time()
        rel = classify_relation(new.get("content") or "", [cand_dict], llm=client)
        dec = _merge_decision(
            source=SOURCE_VECTOR, sim=pair["sim"], fusion=pair["fusion"],
            overlap=pair["overlap"], candidate_key=cand["key"],
            relation=rel.get("relation"), anchored_key=rel.get("anchored_key"),
            old_content=cand.get("content") or "",
            new_content=new.get("content") or "",
            threshold=MERGE_HIGH_THRESHOLD, floor=MERGE_FLOOR,
            mode="shadow", anchor_mode="shadow")
        rec[mode] = {
            "relation": rel.get("relation"),
            "degraded": bool(rel.get("degraded")),
            "source": rel.get("source"),
            "anchored_key": rel.get("anchored_key"),
            "would_veto": dec.would_veto,
            "action": dec.action,
            "reason": dec.reason,
            "elapsed_s": round(time.time() - t0, 3),
        }
    return rec


def load_done(path):
    done = set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                done.add((r["keep_key"], r["remove_key"]))
    return done


def stats(recs, mode):
    rel = collections.Counter(r[mode]["relation"] for r in recs)
    src = collections.Counter(r[mode]["source"] for r in recs)
    deg = sum(1 for r in recs if r[mode]["degraded"])
    veto = [r for r in recs if r[mode]["would_veto"]]
    reasons = collections.Counter(r[mode]["reason"] for r in recs)
    out = {
        "n": len(recs),
        "relation": dict(rel),
        "source": dict(src),
        "degraded": deg,
        "would_veto": len(veto),
        "reason": dict(reasons),
        "samples": [
            {"keep_key": r["keep_key"], "remove_key": r["remove_key"],
             "sim": r["sim"], "fusion": r["fusion"], "overlap": r["overlap"],
             "relation": r[mode]["relation"],
             "reason": r[mode]["reason"]}
            for r in veto[:10]
        ],
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--llm", action="store_true", help="用真实 HTTPAnchorLLM")
    ap.add_argument("--out", default=os.path.join(HERE, "var", "p1-shadow-grayband.jsonl"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--rule-only", action="store_true")
    args = ap.parse_args()

    tmp = copy_db(args.db)
    print("源库(只读):", args.db)
    print("临时副本:", tmp)
    try:
        rows = load_rows(tmp)
        print("带向量活跃记忆:", len(rows), "维度:", int(rows[0]["vec"].size) if rows else 0)
        raw, pairs = gray_pairs(rows)
        boosted = len(pairs)
        print("原始余弦灰带对 [0.85,0.92):", raw)
        print("boost 后仍灰带的对:", boosted)
        pairs = pairs[:args.limit]
        print("本次评测候选:", len(pairs))

        done = load_done(args.out) if args.resume else set()
        llm = None
        if args.llm and not args.rule_only:
            llm = HTTPAnchorLLM(timeout=20)
            print("LLM:", llm.url, llm.model, "key?", bool(llm.key))

        recs = []
        fh = open(args.out, "a" if args.resume else "w", encoding="utf-8")
        try:
            for n, p in enumerate(pairs, 1):
                rec = run_one(p, rows, llm)
                recs.append(rec)
                if (rec["keep_key"], rec["remove_key"]) not in done:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                if n % 20 == 0:
                    print("  ... %d/%d" % (n, len(pairs)))
        finally:
            fh.close()

        report = {
            "db": os.path.realpath(args.db),
            "n_rows": len(rows),
            "raw_gray_pairs": raw,
            "boosted_gray_pairs": boosted,
            "evaluated": len(recs),
            "rule": stats(recs, "rule"),
        }
        if any("llm" in r for r in recs):
            report["llm"] = stats(recs, "llm")
        meta = os.path.splitext(args.out)[0] + "-summary.json"
        with open(meta, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print("明细:", args.out)
        print("统计:", meta)
        return 0
    finally:
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
