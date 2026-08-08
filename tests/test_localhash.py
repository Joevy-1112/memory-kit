#!/usr/bin/env python3
"""LocalHashEmbedder 兜底回归测试（P1 #4，2026-08-08）

设计（design-notes/scope-recall-borrowing.md §4）：
  - LocalHashEmbedder：确定性本地哈希向量（token + 字符 3-gram → sha1 桶）
  - MemoryDB 未注入 embedder 且 HTTP 服务不可达 → 自动降级 local-hash
    （vector_status=degraded，可用性兜底不是真相源）
  - embed_fallback=False 保持原行为（HTTP 不可达 → _embed 返回 None）

覆盖：
  #1 确定性：同文本同向量
  #2 维度与归一化：256 维、L2 模≈1
  #3 空文本不崩（占位串保持非零向量）
  #4 共享 token 的文本 cos > 0（弱语义可检索）
  #5 未注入 + HTTP 不可达 → vector_status=degraded，_embed 仍返回向量
  #6 embed_fallback=False → vector_status=ready，_embed 返回 None（原行为）
  #7 stats() 暴露 vector_status
  #8 learn→search 在 local-hash 下可检索（降级可用性闭环）

运行：
  python run_tests.py localhash
  或：python -m unittest tests.test_localhash -v
"""
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

# 嵌入服务指向必然不可达端口（仅本文件单独跑时生效；全量跑时 embedder 的
# DEFAULT_EMBED_URL 可能已被其他测试模块固化——fallback 测试用 mock 不依赖 env）
os.environ["MEMORY_EMBED_URL"] = "http://127.0.0.1:1"
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB
from memorykit.embedder import LocalHashEmbedder


class _FakeHTTP:
    """替换 db.HTTPEmbedder：is_available 可控，embed 恒 None（模拟服务挂了）。"""

    def __init__(self, available=False, url="http://127.0.0.1:1"):
        self._avail = available
        self.url = url

    def is_available(self):
        return self._avail

    def embed(self, text):
        return None

    def embed_batch(self, texts):
        return [None] * len(texts)


def make_tmp_db(**kw):
    fd, path = tempfile.mkstemp(prefix="mk-lh-", suffix=".db")
    os.close(fd)
    db = MemoryDB(db_path=path, **kw)
    return db, path


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class LocalHashEmbedderTest(unittest.TestCase):
    def test_01_deterministic(self):
        e = LocalHashEmbedder()
        v1 = e.embed("服务器嵌入服务端口 8082")
        v2 = e.embed("服务器嵌入服务端口 8082")
        self.assertEqual(v1, v2)

    def test_02_dims_and_norm(self):
        e = LocalHashEmbedder()
        v = e.embed("显卡预算与显存大小的讨论记录")
        self.assertEqual(len(v), 256)
        norm = math.sqrt(sum(x * x for x in v))
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_03_empty_text(self):
        e = LocalHashEmbedder()
        v = e.embed("")
        self.assertIsNotNone(v)
        self.assertEqual(len(v), 256)
        self.assertGreater(math.sqrt(sum(x * x for x in v)), 0.0)

    def test_04_shared_token_similar(self):
        e = LocalHashEmbedder()
        a = e.embed("服务器端口 8082 配置")
        b = e.embed("服务器端口 8082 设置讨论")
        c = e.embed("今天天气很好适合散步")
        self.assertGreater(_cos(a, b), _cos(a, c))


class MemoryDBFallbackTest(unittest.TestCase):
    def test_05_degraded_fallback(self):
        # HTTP 不可达（mock）→ 自动降级 local-hash
        with mock.patch("memorykit.db.HTTPEmbedder", _FakeHTTP):
            db, path = make_tmp_db()
            try:
                self.assertEqual(db._vector_status, "degraded")
                vec = db._embed("嵌入服务不可达时本地兜底")
                self.assertIsNotNone(vec)
                self.assertEqual(len(vec), 256)
                # local-hash 恒可用（本地确定性，无网络依赖）
                self.assertTrue(db._is_embedding_available())
            finally:
                db.close()
                if os.path.exists(path):
                    os.unlink(path)

    def test_06_fallback_disabled_keeps_old_behavior(self):
        # embed_fallback=False：即使 HTTP 不可达也不降级（保持原行为）
        with mock.patch("memorykit.db.HTTPEmbedder", _FakeHTTP):
            db, path = make_tmp_db(embed_fallback=False)
            try:
                self.assertEqual(db._vector_status, "ready")
                vec = db._embed("不启用兜底时 HTTP 失败返回 None")
                self.assertIsNone(vec)
            finally:
                db.close()
                if os.path.exists(path):
                    os.unlink(path)

    def test_07_stats_exposes_vector_status(self):
        with mock.patch("memorykit.db.HTTPEmbedder", _FakeHTTP):
            db, path = make_tmp_db()
            try:
                s = db.stats()
                self.assertIn("vector_status", s)
                self.assertEqual(s["vector_status"], "degraded")
            finally:
                db.close()
                if os.path.exists(path):
                    os.unlink(path)

    def test_08_learn_search_works_degraded(self):
        # HTTP 可用（mock available=True）→ ready 状态但 embed 恒 None（服务假活）
        # 这里直接测 local-hash 注入路径的 learn→search 闭环
        with mock.patch("memorykit.db.HTTPEmbedder", lambda: _FakeHTTP(available=True)):
            db, path = make_tmp_db(embedder=LocalHashEmbedder())
            try:
                r1 = db.learn("服务器嵌入服务端口是 8082 且必须保持运行")
                r2 = db.learn("硬盘挂载参数的选择方案讨论记录")
                self.assertTrue(r1.get("key"))
                self.assertTrue(r2.get("key"))
                res = db.search_multi("服务器嵌入服务 8082 端口", limit=5)
                keys = [d["key"] for d in res]
                self.assertIn(r1["key"], keys)
            finally:
                db.close()
                if os.path.exists(path):
                    os.unlink(path)


if __name__ == "__main__":
    unittest.main()
