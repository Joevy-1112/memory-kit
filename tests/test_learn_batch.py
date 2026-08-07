#!/usr/bin/env python3
"""memorykit 批量学习（learn_batch）+ 批量嵌入（embed_batch）测试。

覆盖：
  - HTTPEmbedder.embed_batch：顺序对齐 / 过短过滤 / 无 index 回退 / 整体失败 / 空列表
  - learn_batch：一次批量嵌入零二次嵌入 / 向量正确写入 / 去重合并与逐条 learn 一致 /
    event_time 透传 / 过短条目拒绝 / 空列表 / 无 embed_batch 的 embedder 逐条降级
  - learn(precomputed_vec=...) 跳过内部嵌入；不传 = 原行为（回归保护）

运行：
  python run_tests.py
  或：python -m unittest tests.test_learn_batch -v
"""
import os
import sys
import json
import hashlib
import tempfile
import unittest
import urllib.request
from unittest import mock

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB, HTTPEmbedder


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class DeterministicEmbedder:
    """确定性假嵌入器（带 embed_batch）：相同文本 → 相同向量，可复现去重行为。"""

    def __init__(self):
        self.embed_calls = 0
        self.batch_calls = 0
        self.last_batch_texts = None

    def _vec(self, text):
        h = hashlib.md5((text or "").encode("utf-8")).digest()
        return [b / 255.0 for b in h[:4]]

    def embed(self, text):
        self.embed_calls += 1
        return self._vec(text) if text and len(text.strip()) >= 3 else None

    def embed_batch(self, texts):
        self.batch_calls += 1
        self.last_batch_texts = list(texts)
        # 直接走 _vec，不经 embed() —— embed_calls 只统计真正的逐条嵌入调用
        return [self._vec(t) if t and len(t.strip()) >= 3 else None for t in texts]

    def is_available(self):
        return True


def _embed_server_up():
    try:
        req = urllib.request.Request("http://127.0.0.1:8082/v1/models")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ──────────────────────────────────────────────────────────────
# HTTPEmbedder.embed_batch
# ──────────────────────────────────────────────────────────────
class TestEmbedBatch(unittest.TestCase):
    """embed_batch：顺序对齐 / 过滤 / 失败降级（stub urlopen，离线可跑）。"""

    def _stub(self, payload_fn):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["input"] = json.loads(req.data.decode("utf-8"))["input"]
            return _FakeResp(payload_fn(captured["input"]))

        return fake_urlopen, captured

    def test_order_aligned_and_short_filtered(self):
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        fake, cap = self._stub(lambda texts: {"data": [
            {"embedding": [float(i), float(i + 1)], "index": i}
            for i in range(len(texts))
        ]})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            out = e.embed_batch(["显卡价格是550块", "x", "内存条32G", ""])
        # 过短条目（"x"、""）不发给服务端，位置占 None
        self.assertEqual(cap["input"], ["显卡价格是550块", "内存条32G"])
        self.assertEqual(len(out), 4)
        self.assertIsNone(out[1])
        self.assertIsNone(out[3])
        self.assertEqual(out[0], [0.0, 1.0])
        self.assertEqual(out[2], [1.0, 2.0])

    def test_no_index_positional_fallback(self):
        # 服务端不返回 index → 按返回顺序回填
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        fake, _ = self._stub(lambda texts: {"data": [
            {"embedding": [float(i)]} for i in range(len(texts))
        ]})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            out = e.embed_batch(["aaa", "bbb", "ccc"])
        self.assertEqual(out, [[0.0], [1.0], [2.0]])

    def test_total_failure_all_none(self):
        e = HTTPEmbedder(url="http://127.0.0.1:9")

        def boom(req, timeout=None):
            raise OSError("connection refused")

        with mock.patch("urllib.request.urlopen", side_effect=boom):
            out = e.embed_batch(["aaa", "bbb"])
        self.assertEqual(out, [None, None])

    def test_empty_and_all_short(self):
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        self.assertEqual(e.embed_batch([]), [])
        out = e.embed_batch(["", "x", "  "])
        self.assertEqual(out, [None, None, None])

    def test_batch_timeout_widened(self):
        # 批量超时下限 30s：大 batch 服务端推理耗时随条数增长
        e = HTTPEmbedder(url="http://127.0.0.1:9", timeout=5)
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return _FakeResp({"data": [{"embedding": [0.0], "index": 0}]})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            e.embed_batch(["aaaa" * 10])
        self.assertGreaterEqual(seen["timeout"], 30)

    @unittest.skipUnless(_embed_server_up(), "需要本地嵌入服务 127.0.0.1:8082")
    def test_real_server_batch_matches_single_embed(self):
        # 端到端：真实服务批量 vs 单条。llama.cpp 对 string 单条与 array 批量
        # 走不同内部路径，向量不完全相等但语义一致（cos ≈ 0.999）；批量模式
        # 自身跨调用确定（同文本同 batch 完全一致）。
        e = HTTPEmbedder()
        texts = ["显卡价格是550块", "内存条32G", "x"]
        out = e.embed_batch(texts)
        self.assertIsNone(out[2])
        single = e.embed(texts[0])
        self.assertIsNotNone(single)
        self.assertEqual(len(out[0]), len(single))
        import numpy as np
        a = np.array(out[0], dtype=np.float64)
        b = np.array(single, dtype=np.float64)
        cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        self.assertGreater(cos, 0.99)
        # 同一 batch 内相同文本必须完全一致（确定性）
        dup = e.embed_batch([texts[0], texts[0]])[1]
        self.assertEqual(out[0], dup)


# ──────────────────────────────────────────────────────────────
# learn_batch（MemoryDB 批量学习）
# ──────────────────────────────────────────────────────────────
class TestLearnBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.emb = DeterministicEmbedder()
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

    def test_one_batch_call_zero_per_item_embeds(self):
        items = [
            {"content": f"批量任务{i}：显卡价格是550块，显存12G，注意散热",
             "source": "task", "source_session": f"s{i}",
             "event_time": f"2026-08-0{i + 1}"}
            for i in range(4)
        ]
        rs = self.db.learn_batch(items)
        self.assertEqual(len(rs), 4)
        self.assertTrue(all("key" in r for r in rs))
        # 4 条 → 恰好一次批量嵌入，零逐条嵌入
        self.assertEqual(self.emb.batch_calls, 1)
        self.assertEqual(self.emb.embed_calls, 0)

    def test_vectors_written_correctly(self):
        content = "显卡价格是550块，显存12G"
        rs = self.db.learn_batch([{"content": content, "source": "task"}])
        row = self.db.get(rs[0]["key"])
        assert row is not None
        vec_bin = row["vector_bin"]
        assert vec_bin is not None
        import struct
        stored = list(struct.unpack(f"{len(vec_bin) // 4}f", vec_bin))
        expected = self.emb._vec(content)  # 无时态词 → 锚定不变
        self.assertEqual(len(stored), len(expected))
        self.assertTrue(all(abs(a - b) < 1e-6 for a, b in zip(stored, expected)))

    def test_dedup_merge_consistent_with_per_item_learn(self):
        items = [
            {"content": "显卡价格是550块，显存12G", "source": "task", "source_session": "s1"},
            {"content": "显卡价格是550块，显存12G", "source": "task", "source_session": "s2"},
        ]
        rs = self.db.learn_batch(items)
        self.assertFalse(rs[0]["merged"])
        self.assertTrue(rs[1]["merged"])
        rows = self.db._conn.execute("SELECT key, content FROM memories").fetchall()
        self.assertEqual(len(rows), 1)

        # 对照：逐条 learn 同样合并，最终库内容一致
        tmp2 = make_tmp_db()
        db2 = MemoryDB(db_path=tmp2, embedder=DeterministicEmbedder())
        try:
            r1 = db2.learn(items[0]["content"], source="task", source_session="s1")
            r2 = db2.learn(items[1]["content"], source="task", source_session="s2")
            self.assertFalse(r1["merged"])
            self.assertTrue(r2["merged"])
            rows2 = db2._conn.execute("SELECT key, content FROM memories").fetchall()
            self.assertEqual(len(rows2), 1)
            self.assertEqual(rows[0]["key"], rows2[0]["key"])
            self.assertEqual(rows[0]["content"], rows2[0]["content"])
        finally:
            db2.close()
            os.remove(tmp2)

    def test_event_time_passthrough(self):
        rs = self.db.learn_batch([{
            "content": "事件时间测试内容足够长",
            "source": "task",
            "event_time": "2026-07-01 10:00:00",
        }])
        row = self.db.get(rs[0]["key"])
        assert row is not None
        self.assertEqual(row["event_time"], "2026-07-01 10:00:00")

    def test_too_short_rejected_no_embed(self):
        rs = self.db.learn_batch([
            {"content": "短", "source": "task"},
            {"content": "显卡价格是550块，显存12G", "source": "task"},
        ])
        self.assertEqual(rs[0], {"error": "content too short"})
        self.assertIn("key", rs[1])
        # 只对有效条目做了一次批量嵌入
        self.assertEqual(self.emb.batch_calls, 1)
        self.assertEqual(self.emb.embed_calls, 0)

    def test_empty_and_invalid_items(self):
        self.assertEqual(self.db.learn_batch([]), [])
        rs = self.db.learn_batch([
            {"content": "显卡价格是550块，显存12G", "source": "task"},
            "not-a-dict",
        ])
        self.assertIn("key", rs[0])
        self.assertEqual(rs[1], {"error": "invalid item"})

    def test_embedder_without_embed_batch_falls_back_per_item(self):
        class PlainFake:
            def __init__(self):
                self.calls = 0

            def embed(self, text):
                self.calls += 1
                return [0.1, 0.2, 0.3, 0.4]

            def is_available(self):
                return True

        tmp = make_tmp_db()
        fake = PlainFake()
        db = MemoryDB(db_path=tmp, embedder=fake)
        try:
            rs = db.learn_batch([
                {"content": "显卡价格是550块，显存12G", "source": "task"},
                {"content": "内存条32G价格80块", "source": "task"},
            ])
            self.assertEqual(len(rs), 2)
            self.assertTrue(all("key" in r for r in rs))
            self.assertEqual(fake.calls, 2)  # 逐条降级，不崩溃
        finally:
            db.close()
            os.remove(tmp)

    def test_learn_precomputed_vec_skips_internal_embed(self):
        # 传入向量 → 不调 _embed；不传 → 原行为（调 _embed）——回归保护
        r1 = self.db.learn("显卡价格是550块，显存12G", precomputed_vec=[0.5, 0.5, 0.5, 0.5])
        self.assertIn("key", r1)
        self.assertEqual(self.emb.embed_calls, 0)
        self.db.learn("内存条32G价格80块")
        self.assertEqual(self.emb.embed_calls, 1)

    def test_learn_explicit_none_precomputed_skips_embed(self):
        # 显式 None = 批量嵌入失败 → 跳过内部嵌入，离线降级也能入库（无向量）
        r = self.db.learn("显卡价格是550块，显存12G", precomputed_vec=None)
        self.assertIn("key", r)
        self.assertEqual(self.emb.embed_calls, 0)
        row = self.db.get(r["key"])
        assert row is not None
        self.assertIsNone(row["vector_bin"])

    @unittest.skipUnless(_embed_server_up(), "需要本地嵌入服务 127.0.0.1:8082")
    def test_real_server_batch_ingest(self):
        # 端到端：默认 HTTPEmbedder 灌 20 条，向量确实写入
        db = MemoryDB(db_path=make_tmp_db())
        try:
            items = [{
                "content": f"灌入测试{i}：显卡价格是550块，显存12G，注意散热",
                "source": "task",
            } for i in range(20)]
            rs = db.learn_batch(items)
            self.assertEqual(len(rs), 20)
            self.assertTrue(all("key" in r for r in rs))
            n_vec = db._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE vector_bin IS NOT NULL"
            ).fetchone()[0]
            self.assertGreaterEqual(n_vec, 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
