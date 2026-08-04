#!/usr/bin/env python3
"""混合维度向量回归测试 — learn() 与 search_multi() 不崩溃

覆盖（2026-08-04 修复回归）：
- learn() 混合维度（1024d → 2048d）批量去重不崩（fc4259a 修复）
- search_multi() 混合维度批量语义拼接不崩（t_ae019ae4 修复）

数据源：临时自建库（不依赖任何现有环境）。

运行：
  python run_tests.py
  或：python run_tests.py dimension
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    """建全新临时库（不碰正式库）"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class TestLearnMixedDimension(unittest.TestCase):
    """learn() 混合维度向量不崩溃（fc4259a 回归）"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)
        self.db._embed = lambda text: [0.01] * 1024

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_learn_mixed_dim_no_crash(self):
        self.db.learn("这是一个旧的测试记忆，用来验证维度问题", source="task")
        # 换嵌入模型：2048d
        self.db._embed = lambda text: [0.02] * 2048
        self.db.learn("这是另一条测试记忆，不同的维度", source="task")
        # 再次用 2048d 学习——之前会在这里崩（批量拼接混合维度）
        self.db._embed = lambda text: [0.03] * 2048
        r = self.db.learn("第三条记忆，验证混合维度下的去重不崩溃", source="task")
        self.assertIsInstance(r, dict)
        self.assertIn("key", r)


class TestSearchMultiMixedDimension(unittest.TestCase):
    """search_multi() 混合维度向量不崩溃（t_ae019ae4 修复回归）"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp)
        # 先以 1024d 写入两条
        self.db._embed = lambda text: [0.01] * 1024
        self.db.set("dim/old-1024", "旧的 1024 维记忆：显卡掉驱动修复记录",
                    category="env", embed=True)
        self.db.set("dim/old-1024b", "另一条 1024 维记忆：电源选购注意事项",
                    category="env", embed=True)
        # 再以 2048d 写入一条——库里从此混有 1024d/2048d 两种向量
        self.db._embed = lambda text: [0.02] * 2048
        self.db.set("dim/new-2048", "新的 2048 维记忆：新嵌入模型产出的内容",
                    category="env", embed=True)

    def tearDown(self):
        try:
            self.db._conn.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_search_multi_1024d_query_no_crash(self):
        self.db._embed = lambda text: [0.03] * 1024
        r = self.db.search_multi("显卡", limit=5)
        self.assertIsInstance(r, list)
        for d in r:
            self.assertIn("key", d)

    def test_search_multi_2048d_query_no_crash(self):
        self.db._embed = lambda text: [0.03] * 2048
        r = self.db.search_multi("显卡", limit=5, category="env")
        self.assertIsInstance(r, list)
        for d in r:
            self.assertIn("key", d)

    def test_search_multi_with_category_filter_no_crash(self):
        # 混合维度 + category 过滤组合路径（覆盖修复 #1 与 #2 的叠加场景）
        self.db._embed = lambda text: [0.03] * 1024
        r = self.db.search_multi("显卡", limit=5, category="env")
        self.assertIsInstance(r, list)
        for d in r:
            self.assertEqual(d["category"], "env")


if __name__ == "__main__":
    unittest.main(verbosity=2)
