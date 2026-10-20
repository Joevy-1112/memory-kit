#!/usr/bin/env python3
"""P1 统一合并决策点测试（2026-10-20）。

六组（design-notes/memory-pipeline-v2-2026-10.md §1.8 的验收清单）:

  G1 TestMergeDecisionMatrix     fusion 0.84/0.85/0.91/0.92 × relation
                                 延续/修正/无关/unknown × mode/anchor_mode 的纯函数矩阵
  G2 TestMergeSafetyModes        三档 flag: off 复刻 P0 / shadow 只记 would_veto /
                                 on+veto 真正否决；非法值 fail-safe 到 off
  G3 TestLazyAnchor              惰性: 无灰带 → classify_relation 调用 0 次；
                                 有灰带 → 1 次；anchor=False → 0 次
  G4 TestFourPathsWired          四条路径（向量快/JSON 慢/实体兜底/dedup）都走
                                 唯一决策点（打桩记录 source）
  G5 TestMergeInvariants         决策路径上的数据保全/写回不变量（全文、vector、
                                 derived_from 非自指、prev_v、decay_profile）
  G6 TestThresholdParameterized  threshold 实参: 0.85 无灰带 / 边界 0.92 /
                                 dedupe 幂等 + dry_run

运行:
  python run_tests.py p1_merge_decision
"""
import json
import math
import os
import struct
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB  # noqa: E402
from memorykit import lifecycle as lc  # noqa: E402
from memorykit.lifecycle import (  # noqa: E402
    ACTION_KEEP, ACTION_MERGE, MergeDecision, _merge_decision, read_merge_policy)

DIM = 64


# ── 向量工具 ────────────────────────────────────────────────────────────────
def at_angle(cos, dim=DIM):
    """与 [1,0,...] 余弦精确为 cos 的单位向量（2 维嵌入 64 维）。"""
    cos = max(-1.0, min(1.0, float(cos)))
    v = [cos, math.sqrt(max(0.0, 1.0 - cos * cos))] + [0.0] * (dim - 2)
    return v


def at_angle_deg(deg, dim=DIM):
    """绝对角度（度）构造单位向量（用于摆出指定两两余弦的多个向量）。"""
    r = math.radians(float(deg))
    return [math.cos(r), math.sin(r)] + [0.0] * (dim - 2)


def _hash_vec(text, dim=DIM):
    import hashlib
    h = hashlib.sha512((text or "").encode("utf-8")).digest()
    vals = [b / 255.0 for b in h]
    mean = sum(vals) / len(vals)
    centered = [x - mean for x in vals]
    n = math.sqrt(sum(x * x for x in centered)) or 1.0
    return [x / n for x in centered]


def pack(v):
    return struct.pack(f"{len(v)}f", *v)


class ScriptedEmbedder:
    """按文本精确给向量；未登记的文本走确定性 hash 向量（互不相似）。"""

    def __init__(self, mapping):
        self.mapping = {k: list(v) for k, v in mapping.items()}

    def embed(self, text):
        if text in self.mapping:
            return list(self.mapping[text])
        return _hash_vec(text)

    def is_available(self):
        return True


class OfflineEmbedder:
    def embed(self, text):
        return None

    def is_available(self):
        return True


class CountingLLM:
    """假 LLM：返回固定 relation，anchored_key 取 prompt 里第 anchor 个候选 key。"""

    model = "fake-p1-model"

    def __init__(self, relation, anchor=1):
        self.relation = relation
        self.anchor = anchor
        self.calls = 0

    def chat(self, prompt):
        self.calls += 1
        keys = []
        for line in (prompt or "").splitlines():
            if "key=" in line:
                keys.append(line.split("key=", 1)[1].split()[0])
        if isinstance(self.anchor, int):
            key = keys[self.anchor - 1] if len(keys) >= self.anchor else ""
        else:
            key = self.anchor
        return json.dumps({"relation": self.relation, "anchored_key": key,
                           "reason": "fake"}, ensure_ascii=False)


# ── 环境/夹具工具 ───────────────────────────────────────────────────────────
def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


@contextmanager
def merge_flags(merge=None, anchor=None):
    """临时设置两个 flag（None = 删除变量，回到代码默认 off）。"""
    keys = (lc.MERGE_SAFETY_ENV, lc.ANCHOR_GRAY_BAND_ENV)
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in ((lc.MERGE_SAFETY_ENV, merge), (lc.ANCHOR_GRAY_BAND_ENV, anchor)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def insert_row(db, key, content, vector, **over):
    """直接插一行（精确控制向量/decay_profile/provenance，绕开 learn 的嵌入）。"""
    fields = {"key": key, "content": content, "scope": "permanent",
              "importance": 0.5, "vector": json.dumps(vector),
              "vector_bin": pack(vector)}
    fields.update(over)
    cols = ", ".join(fields.keys())
    ph = ", ".join("?" for _ in fields)
    db._conn.execute(f"INSERT INTO memories({cols}) VALUES({ph})",
                     tuple(fields.values()))
    db._conn.commit()


def events(db):
    return [dict(r) for r in db._conn.execute(
        "SELECT * FROM merge_events ORDER BY id").fetchall()]


class EnvCase(unittest.TestCase):
    """每个用例前把两个 flag 清成默认 off；结束后恢复进程原值。"""

    def setUp(self):
        self._env_old = {k: os.environ.get(k) for k in
                         (lc.MERGE_SAFETY_ENV, lc.ANCHOR_GRAY_BAND_ENV)}
        os.environ.pop(lc.MERGE_SAFETY_ENV, None)
        os.environ.pop(lc.ANCHOR_GRAY_BAND_ENV, None)
        self.tmp = make_tmp_db()
        self.db = None

    def tearDown(self):
        if self.db is not None:
            try:
                self.db.close()
            except Exception:
                pass
        for p in (self.tmp, self.tmp + "-wal", self.tmp + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
        for k, v in self._env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── G1 决策矩阵（纯函数）────────────────────────────────────────────────────
class TestMergeDecisionMatrix(unittest.TestCase):
    """fusion × relation × mode × anchor_mode，不碰数据库。"""

    def _d(self, fusion=0.90, relation=None, anchored_key=None,
           candidate_key="general/k1", mode="shadow", anchor_mode="shadow",
           sim=None, overlap=0, threshold=0.92, floor=0.85,
           old_content="", new_content=""):
        return _merge_decision(
            source=lc.SOURCE_VECTOR, sim=fusion if sim is None else sim,
            fusion=fusion, overlap=overlap, candidate_key=candidate_key,
            relation=relation, anchored_key=anchored_key,
            old_content=old_content, new_content=new_content,
            threshold=threshold, floor=floor, mode=mode,
            anchor_mode=anchor_mode)

    def test_below_floor_always_keep(self):
        for rel in (None, "延续", "修正", "无关", "unknown"):
            for mode, anchor_mode in (("off", "off"), ("shadow", "shadow"),
                                      ("on", "veto")):
                with self.subTest(rel=rel, mode=mode):
                    d = self._d(fusion=0.84, relation=rel,
                                anchored_key="general/k1", mode=mode,
                                anchor_mode=anchor_mode)
                    self.assertEqual(d.action, ACTION_KEEP)
                    self.assertEqual(d.reason, "below_floor")
                    self.assertFalse(d.would_veto)
                    self.assertFalse(d.gray_band)

    def test_floor_boundary_is_gray(self):
        d = self._d(fusion=0.85, relation=None)
        self.assertEqual((d.action, d.reason), (ACTION_MERGE, "vector_only"))
        self.assertTrue(d.gray_band)
        self.assertFalse(d.would_veto)

    def test_gray_continuation_and_correction_merge(self):
        d1 = self._d(fusion=0.91, relation="延续", anchored_key="general/k1")
        self.assertEqual((d1.action, d1.reason), (ACTION_MERGE, "anchor_continued"))
        d2 = self._d(fusion=0.91, relation="修正", anchored_key="general/k1")
        self.assertEqual((d2.action, d2.reason), (ACTION_MERGE, "anchor_corrected"))

    def test_gray_explicit_unrelated_shadow_records_would_veto(self):
        d = self._d(fusion=0.91, relation="无关", anchored_key="general/k1",
                    mode="shadow", anchor_mode="shadow")
        self.assertEqual((d.action, d.reason), (ACTION_MERGE, "vector_only"))
        self.assertTrue(d.would_veto)
        self.assertFalse(d.veto_applied)
        self.assertFalse(d.anchor_disagrees)

    def test_gray_explicit_unrelated_veto_applies(self):
        d = self._d(fusion=0.91, relation="无关", anchored_key="general/k1",
                    mode="on", anchor_mode="veto")
        self.assertEqual((d.action, d.reason), (ACTION_KEEP, "gray_band_veto"))
        self.assertTrue(d.would_veto)
        self.assertTrue(d.veto_applied)

    def test_gray_unrelated_but_other_candidate_never_vetoes(self):
        d = self._d(fusion=0.91, relation="无关", anchored_key="general/k2",
                    mode="on", anchor_mode="veto")
        self.assertEqual(d.action, ACTION_MERGE)
        self.assertFalse(d.would_veto)

    def test_gray_unknown_never_vetoes(self):
        d = self._d(fusion=0.91, relation="unknown",
                    anchored_key="general/k1", mode="on", anchor_mode="veto")
        self.assertEqual(d.action, ACTION_MERGE)
        self.assertFalse(d.would_veto)

    def test_veto_requires_anchor_mode_veto(self):
        d = self._d(fusion=0.91, relation="无关", anchored_key="general/k1",
                    mode="on", anchor_mode="shadow")
        self.assertEqual(d.action, ACTION_MERGE)
        self.assertTrue(d.would_veto)

    def test_high_similarity_unrelated_still_merges_with_disagreement(self):
        """矩阵最后一行：fusion>=threshold 时 anchor 不否决，只记分歧。"""
        d = self._d(fusion=0.92, relation="无关", anchored_key="general/k1",
                    mode="on", anchor_mode="veto")
        self.assertEqual((d.action, d.reason), (ACTION_MERGE, "high_similarity"))
        self.assertTrue(d.anchor_disagrees)
        self.assertFalse(d.would_veto)
        self.assertFalse(d.gray_band)

    def test_high_similarity_continuation_merges(self):
        d = self._d(fusion=0.99, relation="延续", anchored_key="general/k1",
                    mode="shadow", anchor_mode="shadow")
        self.assertEqual((d.action, d.reason), (ACTION_MERGE, "high_similarity"))
        self.assertFalse(d.anchor_disagrees)

    def test_off_mode_replicates_p0(self):
        for fusion in (0.85, 0.91, 0.99):
            for rel in (None, "无关", "延续", "unknown"):
                with self.subTest(fusion=fusion, rel=rel):
                    d = self._d(fusion=fusion, relation=rel,
                                anchored_key="general/k1", mode="off",
                                anchor_mode="off")
                    self.assertEqual((d.action, d.reason),
                                     (ACTION_MERGE, "vector_only"))
                    self.assertFalse(d.would_veto)
                    self.assertFalse(d.anchor_disagrees)

    def test_critical_mismatch_vetoes_every_mode(self):
        for mode, anchor_mode in (("off", "off"), ("shadow", "shadow"),
                                  ("on", "veto")):
            with self.subTest(mode=mode):
                d = self._d(fusion=0.99, mode=mode, anchor_mode=anchor_mode,
                            old_content="显卡价格550块，显存12G",
                            new_content="显卡价格650块，显存12G")
                self.assertEqual((d.action, d.reason),
                                 (ACTION_KEEP, "critical_mismatch"))
                self.assertTrue(d.critical_mismatch)

    def test_entity_source_matrix(self):
        d3 = _merge_decision(source=lc.SOURCE_ENTITY, sim=0.0, fusion=0.0,
                             overlap=3, candidate_key="k", mode="shadow",
                             anchor_mode="shadow")
        self.assertEqual((d3.action, d3.reason), (ACTION_MERGE, "entity_only"))
        d2 = _merge_decision(source=lc.SOURCE_ENTITY, sim=0.0, fusion=0.0,
                             overlap=2, candidate_key="k", mode="shadow",
                             anchor_mode="shadow")
        self.assertEqual((d2.action, d2.reason), (ACTION_KEEP, "entity_overlap_lt3"))

    def test_dedupe_source_matrix(self):
        ok = _merge_decision(source=lc.SOURCE_DEDUPE, sim=0.92, fusion=0.92,
                             overlap=0, candidate_key="k", threshold=0.92,
                             floor=0.92, mode="shadow", anchor_mode="shadow")
        self.assertEqual((ok.action, ok.reason), (ACTION_MERGE, "dedupe"))
        below = _merge_decision(source=lc.SOURCE_DEDUPE, sim=0.919,
                                fusion=0.919, overlap=0, candidate_key="k",
                                threshold=0.92, floor=0.92, mode="shadow",
                                anchor_mode="shadow")
        self.assertEqual((below.action, below.reason),
                         (ACTION_KEEP, "below_threshold"))

    def test_decision_is_dataclass_and_auditable(self):
        d = self._d(fusion=0.88, relation="修正", anchored_key="general/k1",
                    overlap=3)
        self.assertIsInstance(d, MergeDecision)
        self.assertAlmostEqual(d.boost, 1.0)
        self.assertEqual(d.source, lc.SOURCE_VECTOR)
        self.assertEqual(d.mode, "shadow")
        self.assertEqual(d.relation, "修正")

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            _merge_decision(source="bogus", sim=0.9, fusion=0.9, overlap=0,
                            candidate_key="k")

    def test_boost_is_derived_from_fusion_over_sim(self):
        d = self._d(fusion=0.88, sim=0.80, overlap=2)
        self.assertAlmostEqual(d.boost, 1.1, places=6)


# ── G2 三档 flag 的端到端行为 ───────────────────────────────────────────────
class TestMergeSafetyModes(EnvCase):
    OLD = "项目代号 Orpheus 的部署窗口排在周五下午"
    NEW = "食堂的黄焖鸡加了双份辣"

    def setUp(self):
        super().setUp()
        self.emb = ScriptedEmbedder({self.OLD: at_angle(1.0),
                                     self.NEW: at_angle(0.90)})
        self.db = MemoryDB(self.tmp, embedder=self.emb)
        self.old_key = self.db.learn(self.OLD, source="task",
                                     auto_verify=False)["key"]

    def test_flag_parsing_and_fail_safe(self):
        with merge_flags(merge="shadow", anchor="veto"):
            p = read_merge_policy()
            self.assertEqual((p.mode, p.anchor_mode), ("shadow", "veto"))
            self.assertTrue(p.matrix)
            self.assertFalse(p.veto_applies,
                             "shadow 档下即使 anchor=veto 也只记录，不真正否决")
        with merge_flags(merge="on", anchor="veto"):
            self.assertTrue(read_merge_policy().veto_applies)
        with merge_flags(merge="ON", anchor=" VETO "):
            p = read_merge_policy()
            self.assertEqual((p.mode, p.anchor_mode), ("on", "veto"))
        with merge_flags(merge="yes", anchor="banana"):
            p = read_merge_policy()
            self.assertEqual((p.mode, p.anchor_mode), ("off", "off"),
                             "非法值必须 fail-safe 到 off，不能静默打开安全闸门")

    def test_shadow_records_would_veto_without_changing_merge(self):
        with merge_flags(merge="shadow", anchor="shadow"):
            llm = CountingLLM("无关", anchor=1)
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=llm, auto_verify=False)
        self.assertTrue(r["merged"], "shadow 阶段灰带否决只记录，不改变合并")
        self.assertEqual(llm.calls, 1)
        evs = events(self.db)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["decision"], "MERGE")
        self.assertEqual(evs[0]["reason"], "vector_only")
        self.assertEqual(evs[0]["would_veto"], 1)
        self.assertEqual(evs[0]["relation"], "无关")
        self.assertEqual(evs[0]["anchored_key"], self.old_key)
        prov = json.loads(self.db.peek(self.old_key)["provenance"])
        self.assertEqual(prov["anchor"]["relation"], "无关")

    def test_on_veto_keeps_them_apart(self):
        with merge_flags(merge="on", anchor="veto"):
            llm = CountingLLM("无关", anchor=1)
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=llm, auto_verify=False)
        self.assertFalse(r["merged"])
        self.assertEqual(events(self.db), [], "否决后不应写 merge_events")
        row = self.db.get(r["key"])
        self.assertEqual(row["derived_from"], None)
        prov = json.loads(row["provenance"])
        self.assertEqual(prov["anchor"]["relation"], "无关")
        self.assertIn(self.OLD, self.db.peek(self.old_key)["content"],
                      "旧条目内容不得被否决路径改动")

    def test_on_veto_still_merges_when_anchor_says_continued(self):
        with merge_flags(merge="on", anchor="veto"):
            llm = CountingLLM("延续", anchor=1)
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=llm, auto_verify=False)
        self.assertTrue(r["merged"])
        evs = events(self.db)
        self.assertEqual(evs[0]["reason"], "anchor_continued")
        self.assertEqual(evs[0]["would_veto"], 0)

    def test_default_off_off_is_p0(self):
        # 默认 off/off: 灰带候选直接合并，anchor 不参与（也不调 LLM）。
        with merge_flags():
            llm = CountingLLM("无关", anchor=1)
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=llm, auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertEqual(llm.calls, 0)
        evs = events(self.db)
        self.assertEqual((evs[0]["decision"], evs[0]["reason"],
                          evs[0]["would_veto"]), ("MERGE", "vector_only", 0))

    def test_off_mode_still_classifies_but_never_vetoes(self):
        """MERGE_SAFETY_V2=off 只关矩阵，不影响 ANCHOR_GRAY_BAND 的分类开关。"""
        with merge_flags(merge="off", anchor="shadow"):
            llm = CountingLLM("无关", anchor=1)
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=llm, auto_verify=False)
        self.assertTrue(r["merged"], "off 档复刻 P0：fusion>=0.85 一律合并")
        self.assertEqual(llm.calls, 1, "灰带分类由 ANCHOR_GRAY_BAND 控制")
        ev = events(self.db)[0]
        self.assertEqual((ev["reason"], ev["would_veto"]), ("vector_only", 0),
                         "off 档不得记录 would_veto / 不得走矩阵")
        self.assertEqual(
            json.loads(self.db.peek(self.old_key)["provenance"])["anchor"]["relation"],
            "无关")

    def test_critical_mismatch_blocks_merge_in_every_mode(self):
        for merge, anchor in (("off", "off"), ("shadow", "shadow"),
                              ("on", "veto")):
            with self.subTest(merge=merge):
                tmp = make_tmp_db()
                db = MemoryDB(tmp, embedder=ScriptedEmbedder({
                    "显卡价格550块，显存12G": at_angle(1.0),
                    "显卡价格650块，显存12G": at_angle(0.99)}))
                try:
                    db.learn("显卡价格550块，显存12G", source="task",
                             auto_verify=False)
                    with merge_flags(merge=merge, anchor=anchor):
                        r = db.learn("显卡价格650块，显存12G", source="task",
                                     auto_verify=False)
                    self.assertFalse(r["merged"])
                    self.assertEqual(events(db), [])
                finally:
                    db.close()
                    for p in (tmp, tmp + "-wal", tmp + "-shm"):
                        try:
                            os.remove(p)
                        except OSError:
                            pass


# ── G3 惰性锚定（打桩计数）─────────────────────────────────────────────────
class TestLazyAnchor(EnvCase):
    OLD = "项目代号 Orpheus 的部署窗口排在周五下午"
    NEW = "食堂的黄焖鸡加了双份辣"

    def _run(self, new_vec_cos, anchor=True, flags=("shadow", "shadow"),
             llm=None, seed=True):
        emb = ScriptedEmbedder({self.OLD: at_angle(1.0),
                                self.NEW: at_angle(new_vec_cos)})
        self.db = MemoryDB(self.tmp, embedder=emb)
        if seed:
            self.db.learn(self.OLD, source="task", auto_verify=False)
        # search_multi 也应该一次都不调（旧实现每次 anchor=True 都调）
        search_calls = []
        orig = self.db.search_multi
        self.db.search_multi = lambda *a, **kw: (search_calls.append(1),
                                                 orig(*a, **kw))[1]
        with mock.patch.object(lc, "classify_relation",
                               wraps=lc.classify_relation) as spy:
            with merge_flags(merge=flags[0], anchor=flags[1]):
                r = self.db.learn(self.NEW, source="task", anchor=anchor,
                                  anchor_llm=llm or CountingLLM("延续", 1),
                                  auto_verify=False)
        return r, spy.call_count, len(search_calls)

    def test_no_candidates_zero_calls(self):
        r, classify_calls, search_calls = self._run(0.90, seed=False)
        self.assertFalse(r["merged"])
        self.assertEqual(classify_calls, 0)
        self.assertEqual(search_calls, 0)

    def test_high_similarity_zero_calls(self):
        r, classify_calls, search_calls = self._run(0.99)
        self.assertTrue(r["merged"], ">=threshold 走 high_similarity 合并")
        self.assertEqual(classify_calls, 0, "高相似不是灰带，不应分类")
        self.assertEqual(search_calls, 0)

    def test_gray_band_exactly_one_call(self):
        r, classify_calls, search_calls = self._run(0.90)
        self.assertTrue(r["merged"])
        self.assertEqual(classify_calls, 1)
        self.assertEqual(search_calls, 0, "灰带候选来自向量扫描，不再二次检索")

    def test_anchor_false_zero_calls(self):
        r, classify_calls, _ = self._run(0.90, anchor=False)
        self.assertTrue(r["merged"])
        self.assertEqual(classify_calls, 0)

    def test_anchor_gray_band_off_zero_calls(self):
        r, classify_calls, _ = self._run(0.90, flags=("shadow", "off"))
        self.assertTrue(r["merged"])
        self.assertEqual(classify_calls, 0, "ANCHOR_GRAY_BAND=off 时惰性跳过")


# ── G4 四条路径都走唯一决策点 ───────────────────────────────────────────────
class TestFourPathsWired(EnvCase):
    def _spy(self):
        calls = []
        real = lc._merge_decision

        def wrapper(**kw):
            d = real(**kw)
            calls.append((kw.get("source"), d.action, d.reason))
            return d

        return calls, mock.patch.object(lc, "_merge_decision", side_effect=wrapper)

    def test_fast_vector_path(self):
        calls, patch = self._spy()
        self.db = MemoryDB(self.tmp, embedder=ScriptedEmbedder({
            "同一件事的第一次记录": at_angle(1.0),
            "同一件事的第二次记录": at_angle(0.99)}))
        self.db.learn("同一件事的第一次记录", auto_verify=False)
        with merge_flags(merge="shadow", anchor="shadow"), patch:
            r = self.db.learn("同一件事的第二次记录", auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertIn("vector", [c[0] for c in calls])

    def test_slow_json_path(self):
        self.db = MemoryDB(self.tmp, embedder=ScriptedEmbedder({
            "同一条记忆的原始版本": at_angle(1.0),
            "同一条记忆的补充版本": at_angle(0.99)}))
        self.db.learn("同一条记忆的原始版本", auto_verify=False)
        self.db._conn.execute("UPDATE memories SET vector_bin=NULL")
        self.db._conn.commit()
        calls, patch = self._spy()
        with merge_flags(merge="shadow", anchor="shadow"), patch:
            r = self.db.learn("同一条记忆的补充版本", auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertIn("vector_json", [c[0] for c in calls])

    def test_entity_fallback_path(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        content = "训练深度学习模型时显卡显存不够，预算要加"
        self.db.learn(content, auto_verify=False)
        calls, patch = self._spy()
        with merge_flags(merge="shadow", anchor="shadow"), patch:
            r = self.db.learn(content, auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertIn("entity_fallback", [c[0] for c in calls])

    def test_dedupe_path(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        insert_row(self.db, "dedupe/a", "记忆甲：显卡驱动版本 552.4", at_angle(1.0))
        insert_row(self.db, "dedupe/b", "记忆乙：桌面壁纸换成深色", at_angle(0.99))
        calls, patch = self._spy()
        with merge_flags(merge="shadow", anchor="shadow"), patch:
            r = self.db.deduplicate(threshold=0.92)
        self.assertEqual(r["merged"], 1)
        self.assertIn("dedupe", [c[0] for c in calls])


# ── G5 决策路径上的数据保全/写回不变量 ─────────────────────────────────────
class TestMergeInvariants(EnvCase):
    NEW = "补充说明：这条记录追加了新的现场细节"

    def setUp(self):
        super().setUp()
        self.a = at_angle(1.0)
        self.emb = ScriptedEmbedder({
            "旧条目：项目代号 Orpheus 的部署窗口在周五": self.a,
            # 与 NEW(25.84°) 相差 28.36° → cos=0.88；与 old/keep(0°) cos=0.585
            "另一条相邻旧条目：Orpheus 的回滚脚本已归档": at_angle_deg(54.20),
            self.NEW: at_angle(0.90),
        })
        self.db = MemoryDB(self.tmp, embedder=self.emb)
        insert_row(self.db, "old/keep",
                   "旧条目：项目代号 Orpheus 的部署窗口在周五", self.a,
                   decay_profile="slow",
                   provenance=json.dumps({"v": "verified", "vscore": 0.7,
                                          "sids": [1]}))
        insert_row(self.db, "old/other",
                   "另一条相邻旧条目：Orpheus 的回滚脚本已归档",
                   at_angle_deg(54.20))

    def test_gray_merge_preserves_full_text_and_vectors(self):
        with merge_flags(merge="shadow", anchor="shadow"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=CountingLLM("延续", 1),
                              auto_verify=False)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/keep")
        self.assertIn("旧条目：项目代号 Orpheus 的部署窗口在周五", row["content"])
        self.assertIn(self.NEW, row["content"], "合并必须保留新条目全文")
        self.assertEqual(row["vector_stale"], 0)
        refreshed = json.loads(row["vector"])
        self.assertNotEqual(refreshed, self.a, "内容变了，向量必须重嵌刷新")
        self.assertEqual(row["decay_profile"], "slow", "decay_profile 不得被清空")
        prov = json.loads(row["provenance"])
        self.assertEqual(prov["v"], "pending")
        self.assertEqual(prov["prev_v"], "verified")
        self.assertEqual(prov["vscore"], 0.7)

    def test_anchor_pointing_to_other_candidate_sets_derived_from(self):
        # 灰带两个候选：old/keep (0.90) > old/other (0.88)；锚定指向 old/other
        with merge_flags(merge="shadow", anchor="shadow"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=CountingLLM("修正", 2),
                              auto_verify=False)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/keep")
        self.assertEqual(r["key"], "old/keep")
        self.assertEqual(row["derived_from"], "old/other",
                         "anchor 指向另一条记忆时 derived_from 应写成它")
        self.assertNotEqual(row["derived_from"], row["key"])

    def test_no_anchor_keeps_derived_from_null(self):
        with merge_flags():
            r = self.db.learn(self.NEW, source="task", auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertIsNone(self.db.peek("old/keep")["derived_from"])


# ── G6 threshold 参数化 + dedupe 幂等/dry_run ──────────────────────────────
class TestThresholdParameterized(EnvCase):
    def test_threshold_le_floor_has_no_gray_band(self):
        d = _merge_decision(
            source=lc.SOURCE_VECTOR, sim=0.85, fusion=0.85, overlap=0,
            candidate_key="k", relation="无关", anchored_key="k",
            threshold=0.85, floor=0.85, mode="on", anchor_mode="veto")
        self.assertEqual((d.action, d.reason), (ACTION_MERGE, "high_similarity"))
        self.assertFalse(d.gray_band)
        self.assertFalse(d.would_veto)

    def test_custom_threshold_moves_the_band(self):
        gray = _merge_decision(
            source=lc.SOURCE_VECTOR, sim=0.88, fusion=0.88, overlap=0,
            candidate_key="k", relation=None, threshold=0.90, floor=0.85,
            mode="shadow", anchor_mode="shadow")
        self.assertTrue(gray.gray_band)
        high = _merge_decision(
            source=lc.SOURCE_VECTOR, sim=0.91, fusion=0.91, overlap=0,
            candidate_key="k", relation=None, threshold=0.90, floor=0.85,
            mode="shadow", anchor_mode="shadow")
        self.assertFalse(high.gray_band)
        self.assertEqual(high.reason, "high_similarity")

    def _seed_pair(self, cos):
        insert_row(self.db, "d/a", "记忆甲：显卡驱动版本 552.4", at_angle(1.0))
        insert_row(self.db, "d/b", "记忆乙：桌面壁纸换成深色", at_angle(cos))

    def test_dedupe_threshold_is_parameter(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        self._seed_pair(0.88)
        r92 = self.db.deduplicate(threshold=0.92)
        self.assertEqual(r92["merged"], 0, "0.88 < 0.92 不应合并")
        r85 = self.db.deduplicate(threshold=0.85)
        self.assertEqual(r85["merged"], 1, "0.85 实参下 0.88 应合并")
        self.assertEqual(events(self.db)[0]["reason"], "dedupe")

    def test_dedupe_ignores_anchor_even_with_veto_flags(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        self._seed_pair(0.99)
        with merge_flags(merge="on", anchor="veto"):
            r = self.db.deduplicate(threshold=0.92)
        self.assertEqual(r["merged"], 1, "dedupe 不走 anchor，veto 档也照常合并")
        ev = events(self.db)[0]
        self.assertEqual((ev["relation"], ev["anchored_key"], ev["would_veto"]),
                         (None, None, 0))

    def test_dedupe_is_idempotent(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        self._seed_pair(0.99)
        self.assertEqual(self.db.deduplicate(threshold=0.92)["merged"], 1)
        self.assertEqual(self.db.deduplicate(threshold=0.92)["merged"], 0)

    def test_dedupe_dry_run_writes_nothing(self):
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder())
        self._seed_pair(0.99)
        before = self.db._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0]
        r = self.db.deduplicate(threshold=0.92, dry_run=True)
        self.assertEqual(r["merged"], 1)
        self.assertEqual(self.db._conn.execute(
            "SELECT COUNT(*) FROM merge_events").fetchone()[0], before)
        still = self.db._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active')='active'"
        ).fetchone()[0]
        self.assertEqual(still, 2, "dry_run 不得把任何行标 merged")


if __name__ == "__main__":
    unittest.main()
