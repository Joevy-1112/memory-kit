#!/usr/bin/env python3
"""search_multi JSON 回退路径回归测试。

背景（2026-10-16）：`search_multi` 的语义信号有两条路径——
  1. 快路径：`vector_bin` BLOB + numpy 批量余弦；
  2. 回退路径：批量路径无结果时，逐行 `json.loads(vector)` 计算余弦。
回退路径里 `all_rows` 是 `sqlite3.Row`，而代码写了 `r.get("vector")`；
`sqlite3.Row` 没有 `.get()`，于是回退一触发就抛
`AttributeError: 'sqlite3.Row' object has no attribute 'get'`，
导致「有 JSON 向量但尚未回填 vector_bin」的记忆整条检索链失败。

本测试锁定两点：
  1. 只有 JSON 向量（vector_bin IS NULL）时 `search_multi` 不抛异常；
  2. 语义不变性：同一批数据，二进制快路径与 JSON 回退路径的
     最终融合结果（顺序 + rrf_score）一致。

运行：python run_tests.py json_fallback  （或 python -m unittest tests.test_search_multi_json_fallback -v）
"""
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def _vec_for(text, dim=4):
    """确定性 4 维假向量：不同文本 → 不同向量，保证 learn 不触发向量去重合并。"""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [((h[i] / 255.0) * 2.0 - 1.0) for i in range(dim)]


class FakeEmbedder:
    """离线假嵌入器：不依赖 8082 服务。"""

    def embed(self, text):
        return _vec_for(text)

    def is_available(self):
        return True


class TestSearchMultiJsonFallback(unittest.TestCase):
    DOCS = [
        "GPU 显存 24GB 的显卡跑推理",
        "服务器 CPU 型号与内存条",
        "网络代理端口与隧道配置",
        "记忆库向量检索的去重策略",
    ]
    QUERY = "显卡 推理"

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="mk-jsonfb-", suffix=".db")
        os.close(fd)
        os.remove(self.db_path)
        self.db = MemoryDB(db_path=self.db_path, embedder=FakeEmbedder())
        for c in self.DOCS:
            self.db.learn(c, source="test")

    def tearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.remove(p)

    @staticmethod
    def _snapshot(results):
        return [
            (d.get("key"), round(float(d.get("rrf_score", 0.0)), 6))
            for d in results
        ]

    def _drop_all_vector_bin(self):
        self.db._conn.execute("UPDATE memories SET vector_bin=NULL")
        self.db._conn.commit()

    def test_json_only_rows_do_not_raise(self):
        """核心回归：vector_bin 全为 NULL → 命中 JSON 回退，不得抛 AttributeError。"""
        self._drop_all_vector_bin()
        no_bin = self.db._conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE vector_bin IS NULL"
        ).fetchone()["c"]
        with_json = self.db._conn.execute(
            "SELECT COUNT(*) AS c FROM memories WHERE vector IS NOT NULL"
        ).fetchone()["c"]
        self.assertGreater(no_bin, 0)
        self.assertEqual(no_bin, with_json)

        try:
            hits = self.db.search_multi(self.QUERY, limit=10)
        except AttributeError as exc:  # 修复前这里就是失败点
            self.fail(f"search_multi JSON 回退抛 AttributeError: {exc}")
        self.assertEqual(len(hits), len(self.DOCS))

    def test_binary_and_json_paths_are_equivalent(self):
        """语义不变性：快路径（vector_bin）与回退路径（JSON）结果应逐条一致。"""
        binary = self._snapshot(self.db.search_multi(self.QUERY, limit=10))
        self._drop_all_vector_bin()
        json_fb = self._snapshot(self.db.search_multi(self.QUERY, limit=10))

        self.assertEqual(
            [k for k, _ in binary], [k for k, _ in json_fb],
            "JSON 回退的命中/排序与二进制快路径不一致",
        )
        for (kb, sb), (kj, sj) in zip(binary, json_fb):
            self.assertAlmostEqual(
                sb, sj, places=6,
                msg=f"{kb!r} 的 rrf_score 在两条路径下不一致：{sb} vs {sj}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
