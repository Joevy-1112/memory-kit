#!/usr/bin/env python3
"""P1 回归证明 ②: 惰性锚定真的省了（打桩 classify_relation 计数）。

跑 5 个场景，打印 classify_relation 的调用次数与 search_multi 调用次数：
  1. anchor=True, 库里没有任何候选            → 期望 0 / 0
  2. anchor=True, 唯一候选 fusion=0.99(高相似) → 期望 0 / 0
  3. anchor=True, 唯一候选 fusion=0.90(灰带)   → 期望 1 / 0
  4. anchor=False, 灰带                        → 期望 0 / 0
  5. anchor=True, 灰带, ANCHOR_GRAY_BAND=off   → 期望 0 / 0

用法: ./.venv/bin/python scripts/p1_lazy_proof.py
"""
import math
import os
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from memorykit import MemoryDB  # noqa: E402
from memorykit import lifecycle as lc  # noqa: E402

DIM = 64
OLD = "旧条目：项目代号 Orpheus 的部署窗口排在周五下午"
NEW = "新条目：食堂的黄焖鸡加了双份辣"


def at(cos, dim=DIM):
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))] + [0.0] * (dim - 2)


class Scripted:
    def __init__(self, mapping):
        self.mapping = mapping

    def embed(self, text):
        return list(self.mapping.get(text, [0.0] * DIM))

    def is_available(self):
        return True


class FakeLLM:
    model = "lazy-proof"

    def __init__(self):
        self.calls = 0

    def chat(self, prompt):
        self.calls += 1
        import re
        m = re.search(r"key=([^\s]+)", prompt or "")
        return '{"relation":"延续","anchored_key":"%s","reason":"proof"}' % (
            m.group(1) if m else "")


def scenario(label, cos, anchor=True, gray_band="shadow", seed=True):
    tmp = tempfile.mktemp(suffix=".db")
    db = MemoryDB(tmp, embedder=Scripted({OLD: at(1.0)} if seed else {}))
    try:
        if seed:
            db.learn(OLD, source="task", auto_verify=False)
        search_calls = []
        orig = db.search_multi
        db.search_multi = lambda *a, **kw: (search_calls.append(1), orig(*a, **kw))[1]
        os.environ["MERGE_SAFETY_V2"] = "shadow"
        os.environ["ANCHOR_GRAY_BAND"] = gray_band
        emb_new = at(cos)
        db._embed_with_sparse = lambda text: (emb_new, None)
        with mock.patch.object(lc, "classify_relation",
                               wraps=lc.classify_relation) as spy:
            db.learn(NEW, source="task", anchor=anchor, anchor_llm=FakeLLM(),
                     auto_verify=False)
        return spy.call_count, len(search_calls)
    finally:
        db.close()
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
        os.environ.pop("MERGE_SAFETY_V2", None)
        os.environ.pop("ANCHOR_GRAY_BAND", None)


def main():
    cases = [
        ("1. anchor=True, 无任何候选", dict(cos=0.90, seed=False)),
        ("2. anchor=True, 候选 fusion=0.99(>=0.92)", dict(cos=0.99)),
        ("3. anchor=True, 候选 fusion=0.90(灰带)", dict(cos=0.90)),
        ("4. anchor=False, 候选 fusion=0.90(灰带)", dict(cos=0.90, anchor=False)),
        ("5. anchor=True, 灰带, ANCHOR_GRAY_BAND=off",
         dict(cos=0.90, gray_band="off")),
    ]
    print("%-48s %-22s %s" % ("场景", "classify_relation 调用", "search_multi 调用"))
    print("-" * 90)
    for label, kw in cases:
        c, s = scenario(label, **kw)
        print("%-48s %-22d %d" % (label, c, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
