#!/usr/bin/env python3
"""P0 数据安全回归（2026-10-20）。

覆盖：
- learn 向量/实体合并的四个写回不变量：vector 重嵌、derived_from 非自指、
  prev_v 保存旧状态、decay_profile 保留；source_message_ids/raw_excerpt 不丢
- 重嵌失败：vector_stale=1 + dirty_log，旧向量保留
- merge_events 审计：每个合并一行，全文留痕
- 关键事实否决：550 vs 650、否定句、单位差异、版本号不误合并；
  但"旧 550 + 新增显存 12G"这种补充仍允许合并
- merged 行不是活条目：dedupe/learn/search/search_semantic/age 全部排除
"""
import json
import math
import os
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def unit_vec(seed=1.0, dim=64):
    v = [seed] + [0.0] * (dim - 1)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def pair_vecs(cos=0.94, dim=64):
    """两个单位向量，余弦精确为 cos（>=0.92，足以越过 learn/dedupe 闸门）。"""
    v1 = [1.0] + [0.0] * (dim - 1)
    v2 = [cos, math.sqrt(max(0.0, 1.0 - cos * cos))] + [0.0] * (dim - 2)
    return v1, v2


def pack(v):
    return struct.pack(f"{len(v)}f", *v)


def _insert(db, key, content, vector=None, **over):
    fields = {"key": key, "content": content, "scope": "permanent",
              "importance": 0.5}
    if vector is not None:
        fields["vector"] = json.dumps(vector)
        fields["vector_bin"] = pack(vector)
    fields.update(over)
    cols = ", ".join(fields.keys())
    ph = ", ".join("?" for _ in fields)
    db._conn.execute(f"INSERT INTO memories({cols}) VALUES({ph})",
                     tuple(fields.values()))
    db._conn.commit()


class SequenceEmbedder:
    """按调用序号返回预设向量；fail_on 指定第几次调用返回 None。"""

    def __init__(self, vectors, fail_on=()):
        self.vectors = [list(v) for v in vectors]
        self.fail_on = set(fail_on)
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        if self.calls in self.fail_on:
            return None
        idx = min(self.calls - 1, len(self.vectors) - 1)
        return list(self.vectors[idx])

    def is_available(self):
        return True


class OfflineEmbedder:
    def embed(self, text):
        return None

    def is_available(self):
        return True


class HashEmbedder:
    """64 维去均值单位向量：同文本 cos=1，不同文本 cos≈0。"""

    def _vec(self, text):
        import hashlib
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


class MappingEmbedder:
    def __init__(self, mapping):
        self.mapping = mapping

    def embed(self, text):
        return self.mapping.get(text)

    def is_available(self):
        return True


class FakeLLM:
    model = "fake-model"

    def __init__(self, anchored_key):
        self.anchored_key = anchored_key

    def chat(self, prompt):
        return json.dumps({"relation": "修正",
                           "anchored_key": self.anchored_key,
                           "reason": "test"}, ensure_ascii=False)


class TestLearnVectorWriteback(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.A = unit_vec(1.0)
        self.B = unit_vec(2.0)
        self.dirty = self.tmp + ".dirty"
        self.emb = SequenceEmbedder([self.A, self.B])
        self.db = MemoryDB(self.tmp, embedder=self.emb, dirty_log=self.dirty)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.tmp, self.dirty):
            try:
                os.remove(p)
            except OSError:
                pass

    def _seed(self, **over):
        fields = dict(
            key="old/key",
            content="旧内容：显卡价格550块，显存12G",
            category="env",
            importance=0.5,
            vector=self.A,
            decay_profile="slow",
            provenance=json.dumps({"v": "verified", "vscore": 0.77,
                                   "sids": [1, 2], "method": "keyword"}),
            derived_from="src/other",
            source_message_ids=json.dumps(["10", "11"]),
            raw_excerpt="raw old excerpt",
            verification_status="verified",
            entities=json.dumps(["显卡", "显存"]),
            tags="gpu",
        )
        fields.update(over)
        vec = fields.pop("vector", None)
        _insert(self.db, fields.pop("key"), fields.pop("content"),
                vector=vec, **fields)

    def test_vector_refresh_and_field_preservation(self):
        self._seed()
        r = self.db.learn("补充说明：显卡显存需要注意散热", source="task",
                          source_session="s2", auto_verify=False)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/key")
        # vector/vector_bin 刷新为第二次嵌入结果，stale 清零
        self.assertEqual(json.loads(row["vector"]), self.B)
        self.assertEqual(row["vector_bin"], pack(self.B))
        self.assertEqual(row["vector_stale"], 0)
        # decay_profile 不写死 None
        self.assertEqual(row["decay_profile"], "slow")
        # derived_from 保留旧值，绝不自指
        self.assertNotEqual(row["derived_from"], row["key"])
        self.assertEqual(row["derived_from"], "src/other")
        # prev_v 先存旧值，再置 pending；vscore/sids 不丢
        prov = json.loads(row["provenance"])
        self.assertEqual(prov["v"], "pending")
        self.assertEqual(prov["prev_v"], "verified")
        self.assertEqual(prov["vscore"], 0.77)
        self.assertEqual(prov["sids"], [1, 2])
        # 原文指针/摘录不因新条目未带而丢
        self.assertEqual(json.loads(row["source_message_ids"]), ["10", "11"])
        self.assertEqual(row["raw_excerpt"], "raw old excerpt")
        # 审计行：全文 + 路径原因
        ev = self.db._conn.execute(
            "SELECT keep_key, remove_key, old_content, new_content, "
            "merged_content, reason FROM merge_events").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["keep_key"], "old/key")
        self.assertEqual(ev[0]["reason"], "vector_only")
        self.assertIn("旧内容", ev[0]["old_content"])
        self.assertIn("补充说明", ev[0]["new_content"])
        self.assertIn("旧内容", ev[0]["merged_content"])
        self.assertIn("补充说明", ev[0]["merged_content"])

    def test_prev_v_degraded_is_not_pending(self):
        self._seed(provenance=json.dumps(
            {"v": "degraded", "vscore": 0.2, "sids": [7]}))
        self.db.learn("补充说明：显卡显存需要注意散热", source="task",
                      auto_verify=False)
        prov = json.loads(self.db.peek("old/key")["provenance"])
        self.assertEqual(prov["v"], "pending")
        self.assertEqual(prov["prev_v"], "degraded",
                         "prev_v 必须保存旧值 degraded，而不是 pending")
        self.assertEqual(prov["vscore"], 0.2)
        self.assertEqual(prov["sids"], [7])

    def test_reembed_failure_sets_stale_and_dirty(self):
        self.emb.fail_on = {2}
        self._seed()
        r = self.db.learn("补充说明：显卡显存需要注意散热", source="task",
                          auto_verify=False)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/key")
        self.assertEqual(row["vector_stale"], 1)
        # 失败时保留旧向量（等 dirty 重嵌），不清空
        self.assertEqual(json.loads(row["vector"]), self.A)
        self.assertEqual(row["vector_bin"], pack(self.A))
        self.assertTrue(os.path.exists(self.dirty))
        with open(self.dirty, encoding="utf-8") as f:
            self.assertIn("old/key", f.read())

    def test_anchor_derived_from_points_to_other_key(self):
        """P1（2026-10-20）后锚定改为灰带惰性：本用例改成灰带夹具 + shadow 开。

        原夹具用 cos=1.0（>=0.92 高相似区），P1 起该区间不触发分类、anchor
        不参与决策，因此 derived_from 不会再被 anchor 改写。断言本身
        （derived_from 指向别的 key、绝不自指）不变，只在灰带路径上验证。
        """
        self._seed(derived_from=None)
        # 与旧条目共享 显卡/显存 两实体 → boost 1.10；cos 0.80*1.10 = 0.88 落灰带
        gray = pair_vecs(0.80)[1]
        self.emb.vectors = [gray, gray, gray]
        os.environ["MERGE_SAFETY_V2"] = "shadow"
        os.environ["ANCHOR_GRAY_BAND"] = "shadow"
        try:
            r = self.db.learn("补充说明：显卡显存需要注意散热", source="task",
                              anchor=True, anchor_llm=FakeLLM("anchor/target"),
                              anchor_top_k=3, auto_verify=False)
        finally:
            os.environ.pop("MERGE_SAFETY_V2", None)
            os.environ.pop("ANCHOR_GRAY_BAND", None)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/key")
        self.assertEqual(row["derived_from"], "anchor/target")
        self.assertNotEqual(row["derived_from"], row["key"])


class TestLearnEntityWriteback(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.dirty = self.tmp + ".dirty"
        self.db = MemoryDB(self.tmp, embedder=OfflineEmbedder(),
                           dirty_log=self.dirty)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.tmp, self.dirty):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_entity_merge_writeback(self):
        ents = ["训练", "深度学习", "模型", "显卡", "显存", "预算"]
        _insert(self.db, "old/entity",
                "训练深度学习模型时显卡显存不够，预算要加",
                entities=json.dumps(ents),
                decay_profile="medium",
                provenance=json.dumps({"v": "verified", "vscore": 0.6,
                                       "sids": [3]}),
                derived_from="src/entity-other",
                source_message_ids=json.dumps(["21"]),
                raw_excerpt="entity raw")
        r = self.db.learn("训练深度学习模型时显卡显存不够，预算要加，另外注意散热",
                          source="task", source_session="s2",
                          auto_verify=False)
        self.assertTrue(r["merged"])
        row = self.db.peek("old/entity")
        self.assertEqual(row["derived_from"], "src/entity-other")
        self.assertNotEqual(row["derived_from"], row["key"])
        prov = json.loads(row["provenance"])
        self.assertEqual(prov["v"], "pending")
        self.assertEqual(prov["prev_v"], "verified")
        self.assertEqual(prov["vscore"], 0.6)
        self.assertEqual(prov["sids"], [3])
        self.assertEqual(row["decay_profile"], "medium")
        self.assertEqual(json.loads(row["source_message_ids"]), ["21"])
        self.assertEqual(row["raw_excerpt"], "entity raw")
        self.assertEqual(row["vector_stale"], 1, "离线重嵌失败应标 stale")
        ev = self.db._conn.execute(
            "SELECT reason, overlap FROM merge_events").fetchone()
        self.assertEqual(ev["reason"], "entity_only")
        self.assertGreaterEqual(ev["overlap"], 3)


class TestCriticalFactVeto(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def _run_pair(self, a, b, cos=0.94):
        v1, v2 = pair_vecs(cos)
        emb = MappingEmbedder({a: v1, b: v2})
        self.db = MemoryDB(self.tmp, embedder=emb)
        r1 = self.db.learn(a, source="task", auto_verify=False)
        r2 = self.db.learn(b, source="task", auto_verify=False)
        self.assertFalse(r1["merged"])
        active = self.db._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active') != 'merged'"
        ).fetchone()[0]
        return r2, active

    def test_number_conflict_550_vs_650(self):
        r2, active = self._run_pair("显卡价格是550块，显存12G",
                                    "显卡价格是650块，显存12G")
        self.assertFalse(r2["merged"], "550 vs 650 高余弦也不许合并")
        self.assertEqual(active, 2)

    def test_negation_conflict(self):
        r2, active = self._run_pair("这个方案不能上线，风险太高",
                                    "这个方案可以上线，风险可控")
        self.assertFalse(r2["merged"], "否定句 vs 肯定句不许合并")
        self.assertEqual(active, 2)

    def test_unit_conflict(self):
        r2, active = self._run_pair("文件大小是2GB，需要清理",
                                    "文件大小是2048MB，需要清理")
        self.assertFalse(r2["merged"], "2GB vs 2048MB 不许合并")
        self.assertEqual(active, 2)

    def test_version_conflict(self):
        r2, active = self._run_pair("Python 3.10 兼容这个库",
                                    "Python 3.12 兼容这个库")
        self.assertFalse(r2["merged"], "3.10 vs 3.12 不许合并")
        self.assertEqual(active, 2)

    def test_subset_update_still_merges(self):
        """旧 550 + 新增显存 12G 是补充，不是冲突，必须仍允许合并。"""
        v = unit_vec(1.0)
        a = "显卡价格是550块的报价"
        b = "显卡价格是550块的报价，显存12G"
        self.db = MemoryDB(self.tmp, embedder=MappingEmbedder({a: v, b: v}))
        r1 = self.db.learn(a, source="task", auto_verify=False)
        r2 = self.db.learn(b, source="task", auto_verify=False)
        self.assertFalse(r1["merged"])
        self.assertTrue(r2["merged"], "数字子集（补充细节）不应被否决")
        row = self.db.peek(r1["key"])
        self.assertIn(a, row["content"])
        self.assertIn(b, row["content"])


class TestDedupeCriticalVeto(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def test_number_conflict_not_merged(self):
        v1, v2 = pair_vecs(0.94)
        _insert(self.db, "a", "显卡价格是550块", vector=v1, importance=0.9)
        _insert(self.db, "b", "显卡价格是650块", vector=v2, importance=0.5)
        r = self.db.deduplicate(threshold=0.92, max_seconds=None)
        self.assertEqual(r["merged"], 0)
        self.assertGreaterEqual(r["vetoed"], 1,
                                "550 vs 650 应记录为被关键事实否决")
        self.assertEqual(self.db.peek("a")["content"], "显卡价格是550块")
        self.assertEqual(self.db.peek("b")["content"], "显卡价格是650块")

    def test_same_fact_still_merged(self):
        v1, v2 = pair_vecs(0.99)
        _insert(self.db, "a", "显卡价格是550块，显存12G", vector=v1,
                importance=0.9)
        _insert(self.db, "b", "显卡价格是550块，显存12G", vector=v2,
                importance=0.5)
        r = self.db.deduplicate(threshold=0.92, max_seconds=None)
        self.assertEqual(r["merged"], 1)


class TestMergedRowsAreNotActive(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.emb = HashEmbedder()
        self.db = MemoryDB(self.tmp, embedder=self.emb)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def test_search_excludes_merged(self):
        qv = self.emb.embed("显卡价格")
        _insert(self.db, "active/1", "活跃记忆：显卡价格550",
                vector=qv, status=None)
        _insert(self.db, "merged/1", "归档记忆：显卡价格650",
                vector=qv, status="merged", merged_into="active/1")
        self.assertEqual([r["key"] for r in self.db.search("显卡价格")], ["active/1"])
        self.assertEqual([r["key"] for r in self.db.search_multi("显卡价格")], ["active/1"])
        self.assertEqual([r["key"] for r in self.db.search_semantic("显卡价格")],
                         ["active/1"])

    def test_age_does_not_physically_delete_merged(self):
        old = "2000-01-01 00:00:00"
        _insert(self.db, "merged/old", "归档内容",
                vector=unit_vec(), status="merged",
                verification_status="ARCHIVED", stale_since=old,
                accessed_at="1999-01-01 00:00:00",
                water=2, category="general", importance=0.05)
        _insert(self.db, "active/old", "待删内容",
                vector=unit_vec(2.0),
                verification_status="ARCHIVED", stale_since=old,
                accessed_at="1999-01-01 00:00:00",
                water=2, category="general", importance=0.05)
        r = self.db.age()
        self.assertEqual(r["deleted"], 1)
        self.assertIsNotNone(self.db.peek("merged/old"),
                             "age() 不得物理删 merged 行")
        self.assertIsNone(self.db.peek("active/old"),
                          "对照：活跃 archived 行仍应被 age() 物理删")

    def test_dedupe_ignores_merged(self):
        v = unit_vec()
        _insert(self.db, "active/a", "活跃内容 A", vector=v, importance=0.5)
        _insert(self.db, "merged/b", "归档内容 B", vector=v,
                status="merged", merged_into="active/a", importance=0.99)
        r = self.db.deduplicate(threshold=0.92, max_seconds=None)
        self.assertEqual(r["merged"], 0)
        self.assertEqual(self.db.peek("active/a")["content"], "活跃内容 A")
        self.assertEqual(self.db.peek("merged/b")["content"], "归档内容 B")

    def test_learn_ignores_merged_candidate(self):
        new = "新内容 550 显卡补充"
        v = self.emb.embed(new)
        _insert(self.db, "active/a", "活跃内容 550", vector=v, importance=0.5)
        _insert(self.db, "merged/b", "归档内容 550", vector=v,
                status="merged", merged_into="active/a", importance=0.99)
        r = self.db.learn(new, source="task", auto_verify=False)
        self.assertTrue(r["merged"])
        self.assertEqual(r["key"], "active/a",
                         "learn 不能选 merged 行当合并目标")
        self.assertEqual(self.db.peek("merged/b")["content"], "归档内容 550")
        self.assertIn(new, self.db.peek("active/a")["content"])


class TestMergeEventsTimezone(unittest.TestCase):
    """merge_events.created_at 必须跟随本地时区（本机 CST +08:00），不能写 UTC。

    背景（2026-10-20）：建表默认值与 INSERT 曾用 datetime('now')（UTC），
    与 memories 里 Python 写入的本地时间差 8 小时，审计时对不上。
    修法：统一 datetime('now','localtime')。
    """

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def _latest_created_at(self):
        row = self.db._conn.execute(
            "SELECT created_at FROM merge_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "应该写入一条 merge_events")
        return datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")

    def _merge_once(self):
        v1, v2 = pair_vecs(0.99)
        _insert(self.db, "tz/a", "时区测试同一事实", vector=v1, importance=0.9)
        _insert(self.db, "tz/b", "时区测试同一事实", vector=v2, importance=0.5)
        r = self.db.deduplicate(threshold=0.92, max_seconds=None)
        self.assertEqual(r["merged"], 1, "需要真的合并一条才能测 created_at")

    def test_created_at_matches_local_clock(self):
        self._merge_once()
        ts = self._latest_created_at()
        drift = abs((datetime.now() - ts).total_seconds())
        self.assertLess(drift, 300,
                        f"created_at={ts} 与本地时间偏差 {drift:.0f}s，应 <300s")

    def test_created_at_is_not_utc(self):
        """本机是 CST(+8)：写入值必须比 UTC 快约 8 小时，写 UTC 会差 8 小时整。"""
        offset = datetime.now(timezone.utc).astimezone().utcoffset()
        if offset is None or offset.total_seconds() != 8 * 3600:
            self.skipTest(f"本机时区偏移 {offset}，不是 CST(+8)，本断言不适用")
        self._merge_once()
        ts = self._latest_created_at()
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        ahead_hours = (ts - utc_now).total_seconds() / 3600.0
        self.assertAlmostEqual(
            ahead_hours, 8.0, delta=0.2,
            msg=f"created_at={ts} 比 UTC 快 {ahead_hours:.2f}h，应为 ~8h（写 UTC 会是 ~0h）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
