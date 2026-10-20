#!/usr/bin/env python3
"""P4 E1 评测：chunk 级 / skill 级 × 4 组权重 × 分来源归因。

用法：
  export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
  ./.venv/bin/python scripts/p4_e1_eval.py
  ./.venv/bin/python scripts/p4_e1_eval.py --include-unreachable   # 复现 30 条口径

口径：
- 默认剔除 `unreachable: true` 的条目（#19），分母 29。
- expected 是技能名集合，任一命中即算 recall@k 成功。
- chunk 级：直接对 chunk 排序，top-k 里出现任一 expected 技能即命中。
- skill 级：每 skill 取最高分 chunk（同分比 cos，再比名字）后对 skill 排序。
- 分数公式与 `CorpusIndex.query` 完全一致：total = cos + w_e*entity_hits
  + w_l*lexical_norm（实体命中封顶 3，lexical 是 query token 覆盖率）。
- 溯源：记录"首次正确命中"的 chunk 类型（reference vs SKILL.md），以及
  top-10 返回块的来源构成。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memorykit.corpus import CorpusIndex, extract_entities, tokenize, ENTITY_CAP  # noqa: E402

WEIGHT_COMBOS = [(0.0, 0.0), (0.0, 0.25), (0.05, 0.10), (0.10, 0.25)]
DEFAULT_WEIGHTS = (0.10, 0.25)
K_VALUES = (1, 3, 5, 10)
REFERENCES_PREFIX = "var/p4-"


def load_items(path: Path, include_unreachable: bool):
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"]
    dropped = [it["id"] for it in items if it.get("unreachable")]
    if include_unreachable:
        return items, []
    return [it for it in items if not it.get("unreachable")], dropped


class CorpusMatrices:
    """一次性载入某个 scope 的 chunk 向量与信号，避免 29×4 次全表扫描。"""

    def __init__(self, ci: CorpusIndex, scope: str):
        rows = ci.conn.execute(
            "SELECT id, source_id, skill_name, chunk_no, chunk_type, heading,"
            " content, entities_json, tokens_json, vector_bin "
            "FROM corpus_chunks WHERE source_type=? ORDER BY id",
            (ci.source_type,)).fetchall()
        if scope == "skill":
            rows = [r for r in rows if str(r["source_id"]).endswith("SKILL.md")]
        elif scope != "all":
            raise ValueError(scope)
        self.rows = []
        vecs = []
        for r in rows:
            if r["vector_bin"] is None:
                continue
            v = np.frombuffer(r["vector_bin"], dtype=np.float32)
            if v.size == 0:
                continue
            n = float(np.linalg.norm(v))
            if n == 0:
                continue
            vecs.append(v / n)
            self.rows.append(r)
        self.matrix = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
        self.entities = []
        self.tokens = []
        for r in self.rows:
            try:
                ents = set(json.loads(r["entities_json"] or "[]"))
            except Exception:
                ents = set()
            try:
                toks = set(json.loads(r["tokens_json"] or "[]"))
            except Exception:
                toks = set()
            self.entities.append(ents)
            self.tokens.append(toks)
        # 与 rebuild_entity_links 同口径：只在**当前 scope 的块**上算 df>=2。
        # 这样 scope=skill 等价于 references 上线前的独立索引（实体词表不被
        # references 扩容），A/B 归因才干净。
        df = {}
        for ents in self.entities:
            for e in ents:
                df[e] = df.get(e, 0) + 1
        self.frequent = {e for e, c in df.items() if c >= 2}
        self.scope = scope

    @property
    def size(self):
        return len(self.rows)

    def signals(self, qvec, qtext):
        q = np.asarray(qvec, dtype=np.float32)
        qn = float(np.linalg.norm(q))
        if qn == 0:
            raise RuntimeError("query 向量为零")
        q = q / qn
        if self.matrix.shape[0] == 0 or self.matrix.shape[1] != q.shape[0]:
            raise RuntimeError("query 维度与索引向量不一致")
        cos = self.matrix @ q
        q_entities = [e for e in extract_entities(qtext) if e in self.frequent]
        qset = set(tokenize(qtext))
        hits = np.zeros(len(self.rows), dtype=np.float32)
        lex = np.zeros(len(self.rows), dtype=np.float32)
        for i in range(len(self.rows)):
            h = len(set(q_entities) & self.entities[i])
            hits[i] = min(h, ENTITY_CAP)
            if qset:
                lex[i] = len(qset & self.tokens[i]) / len(qset)
        return cos, hits, lex


def rank_chunks(cos, hits, lex, we, wl, rows):
    total = cos + we * hits + wl * lex
    order = sorted(
        range(len(rows)),
        key=lambda i: (-float(total[i]), -float(cos[i]),
                       str(rows[i]["source_id"]), int(rows[i]["chunk_no"])))
    return order, total


def rank_skills(order, total, cos, rows):
    """每 skill 取最高分 chunk → skill 排序。返回 [(skill, chunk_idx, total)]。"""
    best = {}
    for i in order:
        skill = rows[i]["skill_name"]
        if skill not in best:
            best[skill] = i
    ranked = sorted(
        best.items(),
        key=lambda kv: (-float(total[kv[1]]), -float(cos[kv[1]]), kv[0]))
    return [(skill, i, float(total[i])) for skill, i in ranked]


def first_hit_rank(ranked_skills, expected):
    for rank, (skill, _, _) in enumerate(ranked_skills, 1):
        if skill in expected:
            return rank, skill
    return None, None


def metrics_from_ranks(ranks, n):
    out = {}
    for k in K_VALUES:
        out[f"r@{k}"] = round(
            sum(1 for r in ranks if r is not None and r <= k) / n * 100, 2)
    out["mrr"] = round(
        sum((1.0 / r) for r in ranks if r is not None) / n, 4)
    return out


def evaluate(items, cm: CorpusMatrices, qvecs, weights, progress=True):
    """返回 (chunk_metrics, skill_metrics, per_query)。

    qvecs 在 main 里每条 query 只嵌一次（29 次单条请求），避免 4 组权重
    × 2 个 scope 重复打共享 8082。
    """
    n = len(items)
    chunk_ranks = []
    skill_ranks = []
    per_query = []
    for item, qvec in zip(items, qvecs):
        q = item["q"]
        expected = set(item["expected"])
        cos, hits, lex = cm.signals(qvec, q)
        order, total = rank_chunks(cos, hits, lex, weights[0], weights[1], cm.rows)
        # chunk 级
        c_rank = None
        c_idx = None
        for rank, i in enumerate(order, 1):
            if cm.rows[i]["skill_name"] in expected:
                c_rank, c_idx = rank, i
                break
        chunk_ranks.append(c_rank)
        # skill 级
        skills = rank_skills(order, total, cos, cm.rows)
        s_rank, s_skill = first_hit_rank(skills, expected)
        skill_ranks.append(s_rank)
        s_idx = next((i for skill, i, _ in skills if skill == s_skill), None)
        skill_top10 = []
        for rank, (skill, i, tot) in enumerate(skills[:10], 1):
            r = cm.rows[i]
            skill_top10.append({
                "rank": rank, "skill": skill, "total": round(tot, 6),
                "type": r["chunk_type"], "source_id": r["source_id"],
                "heading": r["heading"], "chars": len(r["content"]),
                "content": r["content"],
            })
        top10 = order[:10]
        top10_rows = []
        for rank, i in enumerate(top10, 1):
            r = cm.rows[i]
            top10_rows.append({
                "rank": rank, "skill": r["skill_name"], "heading": r["heading"],
                "type": r["chunk_type"], "source_id": r["source_id"],
                "chars": len(r["content"]), "content": r["content"],
            })
        per_query.append({
            "id": item["id"],
            "type": item.get("type"),
            "q": q,
            "expected": sorted(expected),
            "chunk_rank": c_rank,
            "chunk_hit_skill": cm.rows[c_idx]["skill_name"] if c_idx is not None else None,
            "chunk_hit_source": cm.rows[c_idx]["source_id"] if c_idx is not None else None,
            "chunk_hit_type": cm.rows[c_idx]["chunk_type"] if c_idx is not None else None,
            "skill_rank": s_rank,
            "skill_hit_skill": s_skill,
            "skill_hit_type": cm.rows[s_idx]["chunk_type"] if s_idx is not None else None,
            "skill_hit_source": cm.rows[s_idx]["source_id"] if s_idx is not None else None,
            "top1_skill": cm.rows[order[0]]["skill_name"] if order else None,
            "top1_type": cm.rows[order[0]]["chunk_type"] if order else None,
            "top10_source_mix": {
                "skill": sum(1 for i in top10 if cm.rows[i]["chunk_type"] != "reference"),
                "reference": sum(1 for i in top10 if cm.rows[i]["chunk_type"] == "reference"),
            },
            "top10": top10_rows,
            "skill_top10": skill_top10,
        })
        if progress:
            print(f"[eval] {len(per_query)}/{n} {q[:36]}... "
                  f"chunk_rank={c_rank} skill_rank={s_rank}", flush=True)

    chunk_metrics = metrics_from_ranks(chunk_ranks, n)
    chunk_metrics["hits_by_source"] = {
        "skill": sum(1 for p in per_query
                     if p["chunk_rank"] and p["chunk_hit_type"] != "reference"),
        "reference": sum(1 for p in per_query
                         if p["chunk_rank"] and p["chunk_hit_type"] == "reference"),
        "none": sum(1 for p in per_query if p["chunk_rank"] is None),
    }
    skill_metrics = metrics_from_ranks(skill_ranks, n)
    skill_metrics["hits_by_source"] = {
        "skill": sum(1 for p in per_query
                     if p["skill_rank"] and p["skill_hit_type"] != "reference"),
        "reference": sum(1 for p in per_query
                         if p["skill_rank"] and p["skill_hit_type"] == "reference"),
        "none": sum(1 for p in per_query if p["skill_rank"] is None),
    }
    skills_hit_at_10 = sum(1 for r in skill_ranks if r is not None and r <= 10)
    chunk_hit_at_10 = sum(1 for r in chunk_ranks if r is not None and r <= 10)
    extra = {
        "skill_hit_at_10": skills_hit_at_10,
        "chunk_hit_at_10": chunk_hit_at_10,
        "top10_ref_share": round(
            sum(p["top10_source_mix"]["reference"] for p in per_query)
            / max(1, sum(sum(p["top10_source_mix"].values()) for p in per_query)), 4),
    }
    return chunk_metrics, skill_metrics, per_query, extra


def fmt_row(label, m):
    src = m["hits_by_source"]
    return (f"| {label} | {m['r@1']:.1f} | {m['r@3']:.1f} | "
            f"{m['r@5']:.1f} | {m['r@10']:.1f} | {m['mrr']:.4f} | "
            f"{src['skill']} / {src['reference']} / {src['none']} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEMORY_DB_PATH"))
    ap.add_argument("--root", default=os.path.expanduser("~/.hermes/skills"))
    ap.add_argument("--eval-set", default=str(HERE / "var" / "p4-eval-set-e1.json"))
    ap.add_argument("--out", default=str(HERE / "var" / "p4-e1-results.json"))
    ap.add_argument("--include-unreachable", action="store_true")
    args = ap.parse_args()
    if not args.db:
        sys.exit("需要 --db 或 MEMORY_DB_PATH")

    items, dropped = load_items(Path(args.eval_set), args.include_unreachable)
    print(f"# items={len(items)} dropped_unreachable={dropped} "
          f"weights={WEIGHT_COMBOS}", flush=True)

    t0 = time.time()
    with CorpusIndex(args.db, args.root, quiet=True) as ci:
        embedder = ci.embedder
        print(f"# embedding {len(items)} queries once", flush=True)
        qvecs = []
        for it in items:
            v = embedder.embed(it["q"])
            if v is None:
                raise RuntimeError(f"query 嵌入失败: {it['q']!r}")
            qvecs.append(v)
        scopes = {}
        for name in ("skill", "all"):
            cm = CorpusMatrices(ci, name)
            print(f"# scope={name} chunks={cm.size}", flush=True)
            scopes[name] = {"chunk": {}, "skill": {}, "per_query": {}, "extra": {}}
            for we, wl in WEIGHT_COMBOS:
                key = f"{we:.2f},{wl:.2f}"
                print(f"\n## scope={name} weights=({we},{wl})", flush=True)
                cm_metrics, sk_metrics, per_query, extra = evaluate(
                    items, cm, qvecs, (we, wl))
                scopes[name]["chunk"][key] = cm_metrics
                scopes[name]["skill"][key] = sk_metrics
                scopes[name]["per_query"][key] = per_query
                scopes[name]["extra"][key] = extra
                print(fmt_row(f"{name} chunk", cm_metrics))
                print(fmt_row(f"{name} skill", sk_metrics))

    payload = {
        "eval_set": args.eval_set,
        "n_items": len(items),
        "dropped_unreachable": dropped,
        "weight_combos": WEIGHT_COMBOS,
        "default_weights": list(DEFAULT_WEIGHTS),
        "k_values": list(K_VALUES),
        "elapsed_s": round(time.time() - t0, 1),
        "scopes": scopes,
        "chunk_counts": {name: scopes[name]["chunk"][f"{DEFAULT_WEIGHTS[0]:.2f},"
                                                   f"{DEFAULT_WEIGHTS[1]:.2f}"]["hits_by_source"]
                         for name in scopes},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n# wrote {args.out}  ({payload['elapsed_s']}s)")


if __name__ == "__main__":
    main()
