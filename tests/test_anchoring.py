#!/usr/bin/env python3
"""P1 写入侧经验锚定测试 (2026-08-29, t_p1_anchor)。

背景: project-log 2026-08-29 "记忆-attention 提案" P1 段 —— 新记忆 learn()
入库前先检索 top-k 相似旧记忆, 模型显式输出"与旧记忆X的关系(延续/修正/无关)"
再存储。同期修复 08-24 断线审计中发现 learn 写入不补 derived_from 指针(0/1670)。

覆盖:
  - anchor=True + 假LLM: new 记忆 provenance 注入 anchor 标注(relation/source=llm)
    relation=修正/延续 → derived_from 指向被锚定旧记忆
    relation=无关   → derived_from 保持 NULL
  - anchor=True + 无LLM(规则 fallback): 安静降级, source=rule, 仍写入不崩溃
  - anchor=False(默认): provenance 无 anchor 字段, derived_from 为 NULL(向后兼容)
  - 断线修复(P0 修正): 向量/实体 merge 路径 → derived_from 不得自指；
    无 anchor 时保留旧值（NULL），有 anchor 且指向别的 key 时才写 anchor
  - set(derived_from=): 透传写入; 未传 → NULL

运行:
  python run_tests.py
  ./.venv/bin/python -m unittest tests.test_anchoring -v
"""
import os
import sys
import json
import re
import tempfile
import unittest

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB, classify_relation  # noqa: E402


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class DeterministicEmbedder:
    """确定性假嵌入器：相同文本 → 相同向量（cos=1 触发向量去重合并）。"""

    def _vec(self, text):
        # 64 维去均值单位向量: 相同文本 cos=1(触发向量去重), 不同文本 cos≈0。
        # 旧实现取 md5 前 4 字节 → 只有 4 维, 随机文本的 cos 噪声可达 0.93,
        # 会越过 0.92 合并闸门把无关记忆合并掉(CI 红: test_llm_unrelated_*)。
        import hashlib
        import math
        h = hashlib.sha512((text or "").encode("utf-8")).digest()
        vals = [b / 255.0 for b in h]
        mean = sum(vals) / len(vals)
        centered = [x - mean for x in vals]
        norm = math.sqrt(sum(x * x for x in centered)) or 1.0
        return [x / norm for x in centered]

    def embed(self, text):
        return self._vec(text) if text and len(text.strip()) >= 3 else None

    def is_available(self):
        return True


def at_angle(cos, dim=64):
    """与 [1,0,...] 余弦精确为 cos 的单位向量（构灰带夹具）。"""
    import math
    cos = max(-1.0, min(1.0, float(cos)))
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))] + [0.0] * (dim - 2)


class ScriptedEmbedder:
    """按文本精确给向量；未登记文本走确定性 hash（与登记文本≈不相似）。"""

    def __init__(self, mapping):
        self.mapping = {k: list(v) for k, v in mapping.items()}

    def embed(self, text):
        if text in self.mapping:
            return list(self.mapping[text])
        return DeterministicEmbedder()._vec(text)

    def is_available(self):
        return True


class _Flags:
    """临时设置 P1 两个 flag（退出时恢复）。"""

    def __init__(self, merge="shadow", anchor="shadow"):
        self.values = {"MERGE_SAFETY_V2": merge, "ANCHOR_GRAY_BAND": anchor}

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.values}
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class OfflineEmbedder:
    """离线嵌入器：永远返回 None → 走实体去重兜底合并路径。"""

    def embed(self, text):
        return None

    def is_available(self):
        return True


class FakeLLM:
    """假 LLM：从 prompt 里取第一个候选 key 作 anchored_key, 返回固定 relation。"""

    model = "fake-model"

    def __init__(self, relation):
        self.relation = relation

    def chat(self, prompt):
        m = re.search(r"key=([^\s]+)", prompt or "")
        key = m.group(1) if m else ""
        return json.dumps({"relation": self.relation, "anchored_key": key,
                           "reason": "fake"}, ensure_ascii=False)


def _prov(row):
    if not row or not row.get("provenance"):
        return {}
    try:
        return json.loads(row["provenance"]) if isinstance(row["provenance"], str) else row["provenance"]
    except (json.JSONDecodeError, TypeError):
        return {}


def _ents(row):
    if not row or not row.get("entities"):
        return []
    try:
        return json.loads(row["entities"]) if isinstance(row["entities"], str) else row["entities"]
    except (json.JSONDecodeError, TypeError):
        return []


class TestAnchorLLMEnabled(unittest.TestCase):
    """anchor=True + 假 LLM，且候选落在灰带（P1 惰性后唯一会分类的区间）。

    P1（2026-10-20）语义变化:
      - 锚定只在 0.85<=fusion<0.92 的灰带候选上执行；
      - 灰带 + 修正/延续 → MERGE（anchor 不否决），provenance 记 anchor；
      - 灰带 + 显式无关 → shadow 记 would_veto 仍合并 / veto 档保持两条独立；
      - derived_from 只在 anchor 指向"另一条"记忆时才写（指向 merge 目标本身
        就是自指，由 merged_into 表达）。
    """

    OLD = "旧条目：项目代号 Orpheus 的部署窗口在周五"
    NEW = "新条目：食堂的黄焖鸡加了双份辣"

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=ScriptedEmbedder({
            self.OLD: at_angle(1.0), self.NEW: at_angle(0.90)}))
        self.old_key = self.db.learn(self.OLD, source="task", auto_verify=False)["key"]

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_llm_correction_gray_band_records_anchor(self):
        with _Flags("shadow", "shadow"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=FakeLLM("修正"), anchor_top_k=3,
                              auto_verify=False)
        self.assertTrue(r["merged"], "灰带 + 修正 → 合并")
        row = self.db.get(self.old_key)
        prov = _prov(row)
        self.assertEqual(prov.get("v"), "pending")
        anchor = prov.get("anchor")
        self.assertIsNotNone(anchor, "灰带分类后应写入 provenance.anchor")
        self.assertEqual(anchor["relation"], "修正")
        self.assertEqual(anchor["source"], "llm")
        self.assertEqual(anchor["model"], "fake-model")
        self.assertIsNone(row["derived_from"],
                          "anchor 指向的正是 merge 目标 → derived_from 保持 NULL（不自指）")

    def test_llm_continuation_gray_band_records_anchor(self):
        with _Flags("shadow", "shadow"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=FakeLLM("延续"), anchor_top_k=3,
                              auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertEqual(_prov(self.db.get(self.old_key))["anchor"]["relation"], "延续")

    def test_llm_unrelated_shadow_merges_but_records_would_veto(self):
        with _Flags("shadow", "shadow"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=FakeLLM("无关"), anchor_top_k=3,
                              auto_verify=False)
        self.assertTrue(r["merged"], "shadow 阶段只记 would_veto，不改变合并")
        ev = self.db._conn.execute(
            "SELECT decision, reason, would_veto FROM merge_events").fetchone()
        self.assertEqual((ev["decision"], ev["reason"], ev["would_veto"]),
                         ("MERGE", "vector_only", 1))
        self.assertEqual(_prov(self.db.get(self.old_key))["anchor"]["relation"], "无关")

    def test_llm_unrelated_veto_keeps_new_entry(self):
        with _Flags("on", "veto"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=FakeLLM("无关"), anchor_top_k=3,
                              auto_verify=False)
        self.assertFalse(r["merged"], "on+veto 下显式无关应否决灰带合并")
        row = self.db.get(r["key"])
        self.assertEqual(_prov(row)["anchor"]["relation"], "无关")
        self.assertIsNone(row["derived_from"], "无关 → 不写 derived_from")


class TestAnchorRuleFallback(unittest.TestCase):
    """anchor=True + 无 LLM: 纯规则 fallback, 安静降级不崩溃。

    P1 三态化: 规则兜底只信肯定信号（延续/修正）；规则判"无关"是启发式
    否定，一律降级为 unknown，不参与否决（避免模型/规则失败时静默杀合并）。
    """

    OLD = "旧条目：项目代号 Orpheus 的部署窗口在周五"
    NEW = "新条目：食堂的黄焖鸡加了双份辣"

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=ScriptedEmbedder({
            self.OLD: at_angle(1.0), self.NEW: at_angle(0.90)}))

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_rule_negative_degrades_to_unknown_and_still_merges(self):
        old_key = self.db.learn(self.OLD, source="task", auto_verify=False)["key"]
        with _Flags("on", "veto"):
            r = self.db.learn(self.NEW, source="task", anchor=True,
                              anchor_llm=None, anchor_top_k=3,
                              auto_verify=False)
        self.assertTrue(r["merged"], "unknown 不得否决合并（哪怕 on+veto）")
        anchor = _prov(self.db.get(old_key)).get("anchor")
        self.assertIsNotNone(anchor, "规则 fallback 也应写入 anchor 标注")
        self.assertEqual(anchor["source"], "rule")
        self.assertEqual(anchor["relation"], "unknown")
        self.assertTrue(anchor["degraded"])

    def test_rule_positive_survives_fallback(self):
        # 规则看到"上涨"→修正（肯定信号，可用），即使没有 LLM
        from memorykit.anchoring import classify_relation
        cands = [{"key": "k1", "id": 1, "content": "显卡价格550块",
                  "similarity": 0.90, "entity_overlap": 2}]
        rel = classify_relation("显卡价格上涨到650块", cands, llm=None)
        self.assertEqual(rel["relation"], "修正")
        self.assertEqual(rel["source"], "rule")
        self.assertTrue(rel["degraded"], "规则结果仍标记为降级（非模型显式判定）")
        self.assertEqual(rel["anchored_key"], "k1")


class TestAnchorDefaultOff(unittest.TestCase):
    """anchor=False(默认): 行为与旧版一致, provenance 无 anchor, derived_from 为 NULL。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=DeterministicEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_default_learn_no_anchor_no_derived(self):
        r = self.db.learn("显卡价格是550块，显存12G", source="task", source_session="s1")
        self.assertFalse(r["merged"])
        row = self.db.get(r["key"])
        prov = _prov(row)
        self.assertNotIn("anchor", prov, "默认 anchor=False 不应写入 anchor 标注")
        self.assertIsNone(row["derived_from"], "默认 learn 不应写 derived_from")
        # provenance 常规字段仍在
        self.assertEqual(prov.get("v"), "pending")


class TestDerivedFromPointer(unittest.TestCase):
    """P0 修正(2026-10-20): merge 路径 derived_from 不得自指（旧实现写 best["key"]）。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=DeterministicEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_vector_merge_sets_derived_from(self):
        # 同文本 → cos=1 触发向量去重合并 → merge 后 derived_from == old_key
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s1")
        self.assertFalse(r1["merged"])
        self.assertIsNone(self.db.get(r1["key"])["derived_from"])
        r2 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s2")
        self.assertTrue(r2["merged"])
        row = self.db.get(r1["key"])
        self.assertNotEqual(row["derived_from"], row["key"],
                            "derived_from 不得指向自身 key（自指成环）")
        self.assertIsNone(row["derived_from"],
                          "无 anchor 的向量 merge 应保留旧值 NULL，而不是写 best['key']")

    def test_entity_fallback_merge_sets_derived_from(self):
        # 切到离线嵌入 → 实体去重兜底合并(需要 ≥3 实体重叠)
        OffDb = MemoryDB(db_path=make_tmp_db(), embedder=OfflineEmbedder())
        try:
            content = "训练深度学习模型时显卡显存不够，预算要加"
            r1 = OffDb.learn(content, source="task", source_session="s1")
            self.assertFalse(r1["merged"])
            # 确保 ≥3 实体以触发兜底
            self.assertGreaterEqual(len(_ents(OffDb.get(r1["key"]))), 3)
            r2 = OffDb.learn(content, source="task", source_session="s2")
            self.assertTrue(r2["merged"])
            row = OffDb.get(r1["key"])
            self.assertNotEqual(row["derived_from"], row["key"],
                                "derived_from 不得指向自身 key（自指成环）")
            self.assertIsNone(row["derived_from"],
                              "无 anchor 的实体 merge 应保留旧值 NULL，而不是写 best['key']")
        finally:
            OffDb.close()

    def test_set_derived_from_passthrough(self):
        key = "general/0801_setp1"
        rid = self.db.set(key, "一条带派生指针的记忆", derived_from="general/0801_src")
        self.assertGreater(rid, 0)
        row = self.db.get(key)
        self.assertEqual(row["derived_from"], "general/0801_src")
        # 未传 derived_from → NULL
        key2 = "general/0801_setp2"
        self.db.set(key2, "一条不带派生指针的记忆")
        self.assertIsNone(self.db.get(key2)["derived_from"])


class TestClassifyRelationSurface(unittest.TestCase):
    """classify_relation 纯函数面: 三态化（无关 vs unknown）/ 解析 / 归一。"""

    def test_no_candidates_is_unknown_degraded(self):
        r = classify_relation("随便内容", [], llm=None)
        self.assertEqual(r["relation"], "unknown", "无候选 ≠ 无关")
        self.assertTrue(r["degraded"])
        self.assertIsNone(r["anchored_key"])

    def test_llm_fenced_json_parsed(self):
        cands = [{"key": "general/0801_a", "id": 1, "content": "旧", "similarity": 0.9}]
        fake = FakeLLM("修正")
        # 劫持返回带 markdown 围栏的 JSON
        orig = fake.chat
        fake.chat = lambda p: "```json\n" + orig(p) + "\n```"
        r = classify_relation("新", cands, llm=fake)
        self.assertEqual(r["relation"], "修正")
        self.assertEqual(r["source"], "llm")
        self.assertEqual(r["anchored_key"], "general/0801_a")
        self.assertFalse(r["degraded"])

    def test_llm_explicit_unrelated_is_not_degraded(self):
        cands = [{"key": "general/0801_a", "id": 1, "content": "旧",
                  "similarity": 0.9}]
        r = classify_relation("新", cands, llm=FakeLLM("无关"))
        self.assertEqual(r["relation"], "无关")
        self.assertFalse(r["degraded"], "显式无关是可信否定，不是降级态")
        self.assertEqual(r["anchored_key"], "general/0801_a")

    def test_out_of_vocab_relation_is_unknown(self):
        cands = [{"key": "k", "id": 1, "content": "c", "similarity": 0.5}]
        r = classify_relation("xxx", cands, llm=FakeLLM("zzz"))
        self.assertEqual(r["relation"], "unknown")
        self.assertTrue(r["degraded"])

    def test_llm_failure_falls_back_to_rule(self):
        cands = [{"key": "k1", "id": 1, "content": "显卡价格550块",
                  "similarity": 0.88, "entity_overlap": 2}]

        class Boom:
            model = "boom"

            def chat(self, prompt):
                raise RuntimeError("network down")

        r = classify_relation("显卡价格上涨到650块", cands, llm=Boom())
        self.assertEqual(r["source"], "rule")
        self.assertEqual(r["relation"], "修正")
        self.assertTrue(r["degraded"])

    def test_rule_negative_is_degraded_unknown(self):
        cands = [{"key": "k1", "id": 1, "content": "黄焖鸡",
                  "similarity": 0.90, "entity_overlap": 0}]
        r = classify_relation("显卡价格550块", cands, llm=None)
        self.assertEqual(r["relation"], "unknown")
        self.assertTrue(r["degraded"])
        self.assertIsNone(r["anchored_key"], "降级态不得给 anchored_key（防误否决）")

    def test_unrelated_english_synonym_accepted(self):
        cands = [{"key": "k1", "id": 1, "content": "c", "similarity": 0.9}]

        class Eng:
            model = "eng"

            def chat(self, prompt):
                return '{"relation":"unrelated","anchored_key":"k1","reason":"x"}'

        r = classify_relation("新", cands, llm=Eng())
        self.assertEqual(r["relation"], "无关")
        self.assertFalse(r["degraded"])


if __name__ == "__main__":
    unittest.main()
