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
  - 断线修复: 向量/实体 merge 路径 → derived_from == 被折叠的 old_key
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
    """anchor=True + 假 LLM: 写入前做 top-k 检索 + 关系标注。"""

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

    def _seed_related(self):
        # 种子一条相关旧记忆(与后续新记忆共享"显卡"实体但文本不同 → 不触发向量合并)
        r = self.db.learn("显卡当前价格是550块，显存12G", source="task", source_session="s1")
        return r["key"]

    def test_llm_correction_sets_derived_from(self):
        old_key = self._seed_related()
        r = self.db.learn("显卡价格上涨到650块，显存不变", source="task",
                          source_session="s2", anchor=True,
                          anchor_llm=FakeLLM("修正"), anchor_top_k=3)
        self.assertFalse(r["merged"])
        row = self.db.get(r["key"])
        prov = _prov(row)
        self.assertEqual(prov.get("v"), "pending")
        anchor = prov.get("anchor")
        self.assertIsNotNone(anchor, "anchor=True 时应写入 provenance.anchor")
        self.assertEqual(anchor["relation"], "修正")
        self.assertEqual(anchor["source"], "llm")
        self.assertEqual(anchor["model"], "fake-model")
        # relation ∈ {延续,修正} → derived_from 指向被锚定的旧记忆
        self.assertEqual(row["derived_from"], old_key,
                         "修正锚定 → derived_from 应指向被修正的旧记忆")

    def test_llm_continuation_sets_derived_from(self):
        old_key = self._seed_related()
        # 与 seed 不同文本(md5 cos≈0.79, 实体重叠=1 不 boost) → 不触发向量合并, 走新建路径
        r = self.db.learn("显卡驱动报错 Xid 43，需要降频", source="task",
                          source_session="s2", anchor=True,
                          anchor_llm=FakeLLM("延续"), anchor_top_k=3)
        self.assertFalse(r["merged"])
        row = self.db.get(r["key"])
        prov = _prov(row)
        self.assertEqual(prov.get("anchor", {}).get("relation"), "延续")
        self.assertEqual(row["derived_from"], old_key,
                         "延续锚定 → derived_from 指向被延续的旧记忆")

    def test_llm_unrelated_keeps_derived_from_null(self):
        self._seed_related()
        r = self.db.learn("今天中午吃了黄焖鸡米饭", source="task",
                          source_session="s2", anchor=True,
                          anchor_llm=FakeLLM("无关"), anchor_top_k=3)
        self.assertFalse(r["merged"])
        row = self.db.get(r["key"])
        prov = _prov(row)
        self.assertEqual(prov.get("anchor", {}).get("relation"), "无关")
        # 无关 → 不设 derived_from
        self.assertIsNone(row["derived_from"])


class TestAnchorRuleFallback(unittest.TestCase):
    """anchor=True + 无 LLM: 纯规则 fallback, 安静降级不崩溃。"""

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

    def test_rule_fallback_degrades_gracefully(self):
        old_key = self.db.learn("显卡当前价格是550块，显存12G", source="task",
                                source_session="s1")["key"]
        # anchor=True, anchor_llm=None → classify_relation 走规则 fallback
        r = self.db.learn("显卡价格上涨到650块，显存不变", source="task",
                          source_session="s2", anchor=True,
                          anchor_llm=None, anchor_top_k=3)
        self.assertFalse(r["merged"])
        row = self.db.get(r["key"])
        prov = _prov(row)
        anchor = prov.get("anchor")
        self.assertIsNotNone(anchor, "规则 fallback 也应写入 anchor 标注")
        self.assertEqual(anchor["source"], "rule")
        self.assertIn(anchor["relation"], ("延续", "修正", "无关"))
        # "上涨"是修正线索 → 规则判定修正 → derived_from 指向旧记忆
        if anchor["relation"] == "修正":
            self.assertEqual(row["derived_from"], old_key)


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
    """断线修复(08-24 审计): merge 路径补 derived_from 指针。"""

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
        self.assertEqual(row["derived_from"], r1["key"],
                         "向量 merge 后 derived_from 应指向被折叠的旧记忆")

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
            self.assertEqual(row["derived_from"], r1["key"],
                             "实体 merge 后 derived_from 应指向被折叠的旧记忆")
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
    """classify_relation 纯函数面: 空候选 / llm输出解析 / 词表归一。"""

    def test_no_candidates_returns_unrelated(self):
        r = classify_relation("随便内容", [], llm=None)
        self.assertEqual(r["relation"], "无关")
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

    def test_out_of_vocab_relation_forced_to_unrelated_or_match(self):
        cands = [{"key": "k", "id": 1, "content": "c", "similarity": 0.5}]
        # 输出未知词 → 归一化兜底(要么匹配受控词表, 要么回退"无关")
        fake = FakeLLM("zzz")
        r = classify_relation("xxx", cands, llm=fake)
        self.assertIn(r["relation"], ("延续", "修正", "无关"))


if __name__ == "__main__":
    unittest.main()
