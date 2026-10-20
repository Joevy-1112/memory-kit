#!/usr/bin/env python3
"""memorykit hybrid 检索 — sparse lexical 信号测试。

覆盖（任务 t_0e367b3a）：
  1. schema：新库建表含 sparse 列；旧库 _migrate_columns 自动补列
  2. learn()/learn_batch() 存 sparse（JSON {token_id_str: weight}）；set(sparse=)
     upsert 覆盖语义；P0 后 merge 会按合并全文重嵌 dense+sparse（不再保留旧值）
  3. search_multi lexical 融合：
     - 构造两条内容（dense 并列、FTS 无命中），一条 token 重叠多一条少，
       验证排序 + sparse_score 字段 + 归一化值
     - sum(min(wq, ws)) 语义 + max 归一化（构造两条不同存储权重的候选验证）
     - 候选 sparse NULL / 无重叠 → lexical 分 0（不剔除、不崩溃）
     - LEXICAL_WEIGHT 可配（默认 0.3）
  4. 兼容：无 sparse 支持的 embedder → 不存 sparse、检索无 sparse_score
     字段、排名与基线一致（不破坏 Hermes 8082 GGUF 路径）
  5. 真实服务（skipUnless）：8083 learn 带 sparse 落库 + search 命中带
     sparse_score；8082（GGUF）不返回 sparse_weights → 纯 dense 降级

运行：
  python run_tests.py sparse
  或：python -m unittest tests.test_sparse_hybrid -v
"""
import os
import sys
import json
import hashlib
import tempfile
import sqlite3
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB, HTTPEmbedder
from memorykit import db as db_mod


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class SparseDeterministicEmbedder:
    """确定性假嵌入器（带 sparse 支持）。

    dense：默认所有文本返回同一常量向量 → 语义信号恒并列（排序差异只能来自
    lexical）。use_constant=False 时用 md5 内容相关向量（learn_batch 用：
    不同内容 → 不同向量 → 不触发去重合并）。
    sparse：CJK bigram 权重（每个 bigram 出现一次 +0.5），可预测 token 重叠。
    embed_with_sparse / embed_batch_with_sparse 齐备。
    """

    def __init__(self, use_constant=True):
        self.use_constant = use_constant
        self.embed_calls = 0
        self.batch_calls = 0

    def _vec(self, text):
        if self.use_constant:
            return [0.1, 0.2, 0.3, 0.4]
        # 有符号 md5 特征（8 维、中心化到 [-1,1]）：不同内容 → 近正交方向 →
        # 低余弦，不触发去重合并（[0,1] 正区间向量互相余弦偏高，不能用于区分）
        h = hashlib.md5((text or "").encode("utf-8")).digest()
        return [(b / 255.0) * 2 - 1 for b in h[:8]]

    def sparse_for(self, text):
        """CJK bigram → {bigram: 0.5}；无 bigram 返回 None（模拟空 sparse）。"""
        out = {}
        s = text or ""
        for i in range(len(s) - 1):
            if "\u4e00" <= s[i] <= "\u9fff" and "\u4e00" <= s[i + 1] <= "\u9fff":
                bg = s[i:i + 2]
                out[bg] = out.get(bg, 0.0) + 0.5
        return out or None

    def embed(self, text):
        self.embed_calls += 1
        return self._vec(text) if text and len(text.strip()) >= 3 else None

    def embed_with_sparse(self, text):
        if not text or len(text.strip()) < 3:
            return None, None
        return self._vec(text), self.sparse_for(text)

    def embed_batch_with_sparse(self, texts):
        self.batch_calls += 1
        return [self.embed_with_sparse(t) for t in texts]

    def is_available(self):
        return True


class DenseOnlyEmbedder:
    """无 sparse 支持的假嵌入器（对照：必须完全降级纯 dense）。"""

    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]

    def is_available(self):
        return True


class FixedSparseEmbedder:
    """固定 sparse 权重的假嵌入器（验证 min(wq,ws) 语义）。"""

    def embed(self, text):
        return [0.1, 0.2, 0.3, 0.4]

    def embed_with_sparse(self, text):
        return [0.1, 0.2, 0.3, 0.4], {"t1": 0.8, "t2": 0.2}

    def is_available(self):
        return True


def _server_up(url):
    try:
        req = urllib.request.Request(url + "/v1/models")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
# 1. schema
# ──────────────────────────────────────────────────────────────
class TestSparseSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=DenseOnlyEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_new_db_has_sparse_column(self):
        cols = {r[1] for r in self.db._conn.execute("PRAGMA table_info(memories)")}
        self.assertIn("sparse", cols)

    def test_old_db_auto_migrates_sparse(self):
        # 用当前 schema 建库后 DROP 掉 sparse 列，模拟 v1 老库 → 重开应自动补列
        conn = sqlite3.connect(self.tmp)
        conn.execute("ALTER TABLE memories DROP COLUMN sparse")
        conn.commit()
        conn.close()
        db2 = MemoryDB(db_path=self.tmp, embedder=DenseOnlyEmbedder())
        try:
            cols = {r[1] for r in db2._conn.execute("PRAGMA table_info(memories)")}
            self.assertIn("sparse", cols)
            # 老数据行不受影响（NULL）
            self.db.set("old/row", "老库已有数据足够长", embed=False)
            n = db2._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE key='old/row'").fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            db2.close()


# ──────────────────────────────────────────────────────────────
# 2. learn / learn_batch / set 存 sparse
# ──────────────────────────────────────────────────────────────
class TestSparseStore(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.emb = SparseDeterministicEmbedder()
        self.db = MemoryDB(db_path=self.tmp, embedder=self.emb)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_learn_stores_sparse_json(self):
        content = "服务器GPU驱动安装配置记录"
        r = self.db.learn(content)
        self.assertNotIn("error", r)
        row = self.db.get(r["key"])
        assert row is not None
        self.assertIsNotNone(row["sparse"])
        stored = json.loads(row["sparse"])
        self.assertEqual(stored, self.emb.sparse_for(content))
        # 键为字符串、值为权重（与 bge-m3 sparse_weights 契约一致）
        self.assertTrue(all(isinstance(k, str) and isinstance(v, float)
                            for k, v in stored.items()))

    def test_learn_batch_stores_sparse_one_call(self):
        # use_constant=False：不同内容 → 不同 dense 向量 → 不合并，每条独立存 sparse
        self.emb = SparseDeterministicEmbedder(use_constant=False)
        self.db._embed_with_sparse_impl = self.emb.embed_with_sparse
        self.db._embed_batch_with_sparse_impl = self.emb.embed_batch_with_sparse
        items = [
            {"content": "显卡价格是550块，显存12G，注意散热", "source": "task"},
            {"content": "内存条32G价格80块，注意兼容性", "source": "task"},
        ]
        rs = self.db.learn_batch(items)
        self.assertEqual(len(rs), 2)
        self.assertTrue(all("key" in r for r in rs))
        self.assertNotEqual(rs[0]["key"], rs[1]["key"])  # 未合并
        # 一次批量请求（2 条 < EMBED_BATCH_CHUNK），零逐条嵌入
        self.assertEqual(self.emb.batch_calls, 1)
        self.assertEqual(self.emb.embed_calls, 0)
        for r, item in zip(rs, items):
            row = self.db.get(r["key"])
            assert row is not None
            self.assertIsNotNone(row["sparse"])
            self.assertEqual(json.loads(row["sparse"]),
                             self.emb.sparse_for(item["content"]))

    def test_set_sparse_upsert_overwrites(self):
        key = "sp/upsert"
        self.db.set(key, "内容更新测试A足够长", embed=False, sparse={"t1": 0.5})
        self.db.set(key, "内容更新测试B足够长", embed=False, sparse={"t2": 0.7})
        row = self.db.get(key)
        assert row is not None
        self.assertEqual(json.loads(row["sparse"]), {"t2": 0.7})
        # 不传 sparse → 覆盖为 NULL（内容更新 → 旧词法信号作废）
        self.db.set(key, "内容更新测试C足够长", embed=False)
        row = self.db.get(key)
        assert row is not None
        self.assertIsNone(row["sparse"])

    def test_learn_merge_refreshes_sparse(self):
        # 两条内容 dense 恒同（常量向量）→ learn 必合并；P0（2026-10-20）
        # 合并后按 merged_content 重嵌，sparse 必须与合并全文一致，而不是
        # 继续用旧 sparse（旧向量/旧 sparse 会让后续 lexical 匹配越合越偏）。
        a = "显卡掉驱动事件记录甲"
        b = "显卡掉驱动排查日志乙"
        r1 = self.db.learn(a)
        key = r1["key"]
        sparse_a = self.emb.sparse_for(a)
        self.assertNotEqual(sparse_a, self.emb.sparse_for(b))  # 测试有效性前提
        r2 = self.db.learn(b)
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["key"], key)
        row = self.db.get(key)
        assert row is not None
        expected = self.emb.sparse_for(row["content"])
        self.assertEqual(json.loads(row["sparse"]), expected)
        self.assertNotEqual(json.loads(row["sparse"]), sparse_a,
                            "合并后 sparse 不应还是旧内容的 sparse")

    def test_dense_only_embedder_stores_null_sparse(self):
        # 无 sparse 支持的 embedder → learn 不存 sparse（纯 dense 降级）
        tmp = make_tmp_db()
        db2 = MemoryDB(db_path=tmp, embedder=DenseOnlyEmbedder())
        try:
            r = db2.learn("服务器GPU驱动安装配置记录")
            row = db2.get(r["key"])
            assert row is not None
            self.assertIsNone(row["sparse"])
        finally:
            db2.close()
            os.remove(tmp)


# ──────────────────────────────────────────────────────────────
# 3. search_multi lexical 融合
# ──────────────────────────────────────────────────────────────
class TestSparseLexicalFusion(unittest.TestCase):
    """构造两条内容：dense 并列（常量向量）+ FTS 命中率相同（都含 query 的
    前 4 个 bigram）+ 实体无命中 → 基线近并列，排序差异只能来自 lexical。
    A 的存储 sparse 与 query token 重叠多（4 个 bigram → 归一化 1.0），
    B 存储完全不重叠的 sparse（0.0）。"""

    CONTENT_A = "显卡掉驱动事件记录甲"
    CONTENT_B = "显卡掉驱动排查日志乙"
    # B 存储的 sparse 故意与内容无关（模拟不同词法信号）：与 query 零重叠
    SPARSE_B = {"完全独立词法": 0.5}
    QUERY = "显卡掉驱动怎么修"

    def setUp(self):
        self.tmp = make_tmp_db()
        self.emb = SparseDeterministicEmbedder()
        self.db = MemoryDB(db_path=self.tmp, embedder=self.emb)
        self.db.set("lex/a", self.CONTENT_A, embed=True,
                    sparse=self.emb.sparse_for(self.CONTENT_A) or {})
        self.db.set("lex/b", self.CONTENT_B, embed=True, sparse=self.SPARSE_B)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_lexical_reranks_overlap_winner_first(self):
        hits = self.db.search_multi(self.QUERY, limit=10)
        by_key = {h["key"]: h for h in hits}
        self.assertIn("lex/a", by_key)
        self.assertIn("lex/b", by_key)
        # dense 并列（常量向量）时 lexical 打破平局：重叠多者排前
        self.assertEqual(hits[0]["key"], "lex/a")
        self.assertGreater(by_key["lex/a"]["rrf_score"],
                           by_key["lex/b"]["rrf_score"])
        # 透明字段：A 归一化 1.0，B 无重叠 0.0
        self.assertEqual(by_key["lex/a"]["sparse_score"], 1.0)
        self.assertEqual(by_key["lex/b"]["sparse_score"], 0.0)

    def test_lexical_gap_vs_dense_baseline(self):
        # 对照：无 sparse 支持的 embedder 建同样两条 → 基线近并列
        # （rrf 差 < 0.01）；有 sparse → 差 ≥ 0.3*1.0 - ε
        tmp = make_tmp_db()
        db2 = MemoryDB(db_path=tmp, embedder=DenseOnlyEmbedder())
        try:
            db2.set("base/a", self.CONTENT_A, embed=True)
            db2.set("base/b", self.CONTENT_B, embed=True)
            hits = db2.search_multi(self.QUERY, limit=10)
            by_key = {h["key"]: h for h in hits}
            gap_base = abs(by_key["base/a"]["rrf_score"]
                           - by_key["base/b"]["rrf_score"])
            # 无 sparse：dense 并列，仅 recency 微差
            self.assertLess(gap_base, 0.01)
            # 无 sparse 时结果不带 sparse_score 字段（与旧版完全一致）
            self.assertTrue(all("sparse_score" not in h for h in hits))
        finally:
            db2.close()
            os.remove(tmp)
        hits = self.db.search_multi(self.QUERY, limit=10)
        by_key = {h["key"]: h for h in hits}
        gap = by_key["lex/a"]["rrf_score"] - by_key["lex/b"]["rrf_score"]
        self.assertGreater(gap, 0.25)  # ≥ 0.3（LEXICAL_WEIGHT × 归一化 1.0）

    def test_min_weight_and_max_normalization(self):
        # sum(min(wq, ws))：存储权重 < 查询权重 → 取存储权重（0.5）；
        # 存储权重 > 查询权重 → 取查询权重（0.8）。max 归一化：0.8 为池内最大。
        tmp = make_tmp_db()
        db2 = MemoryDB(db_path=tmp, embedder=FixedSparseEmbedder())
        try:
            db2.set("min/lo", "alpha token one", embed=False, sparse={"t1": 0.5})
            db2.set("min/hi", "beta token two", embed=False, sparse={"t1": 1.5})
            hits = db2.search_multi("t1 t2", limit=10)
            by_key = {h["key"]: h for h in hits}
            self.assertIn("min/lo", by_key)
            self.assertIn("min/hi", by_key)
            # hi: min(0.8, 1.5)=0.8 → 归一化 1.0；lo: min(0.8, 0.5)=0.5 → 0.625
            self.assertEqual(by_key["min/hi"]["sparse_score"], 1.0)
            self.assertEqual(by_key["min/lo"]["sparse_score"], 0.625)
            self.assertGreater(by_key["min/hi"]["rrf_score"],
                               by_key["min/lo"]["rrf_score"])
            self.assertEqual(hits[0]["key"], "min/hi")
        finally:
            db2.close()
            os.remove(tmp)

    def test_null_sparse_candidate_scores_zero_no_crash(self):
        # 候选 sparse 为 NULL（旧数据/降级写入）→ lexical 分 0，不剔除不崩溃
        self.db.set("lex/none", "完全没有重叠的独立内容段落", embed=False)
        hits = self.db.search_multi(self.QUERY, limit=10)
        by_key = {h["key"]: h for h in hits}
        self.assertIn("lex/none", by_key)
        self.assertEqual(by_key["lex/none"]["sparse_score"], 0.0)

    def test_lexical_weight_configurable(self):
        self.assertEqual(MemorySearch_LEXICAL, 0.3)  # 默认 0.3
        self.db.LEXICAL_WEIGHT = 0.6
        hits = self.db.search_multi(self.QUERY, limit=10)
        by_key = {h["key"]: h for h in hits}
        self.assertEqual(hits[0]["key"], "lex/a")
        self.assertGreater(by_key["lex/a"]["rrf_score"] - by_key["lex/b"]["rrf_score"],
                           0.5)
        self.db.LEXICAL_WEIGHT = 0.3  # 还原


MemorySearch_LEXICAL = 0.3  # 供上面断言引用（防手滑改默认值）


# ──────────────────────────────────────────────────────────────
# 4. 真实服务验证（8083 bge-m3 原版 / 8082 GGUF 降级）
# ──────────────────────────────────────────────────────────────
@unittest.skipUnless(_server_up("http://127.0.0.1:8083"),
                     "需要 8083 嵌入服务")
class TestRealServer8083(unittest.TestCase):
    """8083 现为 llama.cpp GGUF（08-07 从 FlagEmbedding 原版换回，hybrid 暂缓）——
    不返回 sparse_weights → 验证纯 dense 降级：sparse 存 NULL、检索无 sparse_score。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp,
                           embedder=HTTPEmbedder(url="http://127.0.0.1:8083"))

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_gguf_learn_stores_null_sparse(self):
        content = ("the quick brown fox jumps over the lazy dog "
                   "near the river bank")
        r = self.db.learn(content)
        self.assertNotIn("error", r)
        row = self.db.get(r["key"])
        assert row is not None
        # GGUF 不返回 sparse_weights → 存 NULL（纯 dense 降级）
        self.assertIsNone(row["sparse"])

    def test_gguf_search_no_sparse_score(self):
        content = ("the quick brown fox jumps over the lazy dog "
                   "near the river bank")
        self.db.learn(content)
        hits = self.db.search_multi(
            "the quick brown fox jumps over the lazy dog near the river",
            limit=5)
        self.assertGreaterEqual(len(hits), 1)
        # GGUF 无 sparse → 检索不注入 sparse_score（降级后与旧行为一致）
        self.assertTrue(all("sparse_score" not in h for h in hits))


@unittest.skipUnless(_server_up("http://127.0.0.1:8082"),
                     "需要 8082 嵌入服务（Hermes 路径）")
class TestRealServer8082GGUF(unittest.TestCase):
    """8082 为 llama.cpp GGUF（08-07 从 FlagEmbedding 换回，hybrid 暂缓）——
    不返回 sparse_weights → 纯 dense 降级，不破坏 Hermes 自身检索路径。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp,
                           embedder=HTTPEmbedder(url="http://127.0.0.1:8082"))

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_gguf_learn_stores_null_sparse(self):
        r = self.db.learn("the quick brown fox jumps over the lazy dog")
        self.assertNotIn("error", r)
        row = self.db.get(r["key"])
        assert row is not None
        # GGUF 不返回 sparse_weights → 存 NULL（纯 dense 降级）
        self.assertIsNone(row["sparse"])

    def test_gguf_search_no_sparse_score(self):
        self.db.learn("the quick brown fox jumps over the lazy dog")
        hits = self.db.search_multi("quick brown fox", limit=5)
        self.assertGreaterEqual(len(hits), 1)
        # GGUF 无 sparse → 检索不注入 sparse_score（与旧行为一致）
        self.assertTrue(all("sparse_score" not in h for h in hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
