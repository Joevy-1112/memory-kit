#!/usr/bin/env python3
"""P0 补漏回归：deduplicate() 合并后必须重嵌（2026-10-20）。

背景：P0 把 learn 路径的合并写回收口到 `_merge_writeback`（重嵌 + vector_stale
+ merge_events），但 `deduplicate()` 是**第二条写回路径**，只写 content/importance，
向量停在旧内容上 —— 语义检索继续用旧向量匹配新内容，越合并越偏。

本文件锁三条不变量：
1. 内容变了 → vector 必须变（或 vector_stale=1）
2. 重嵌失败 → vector_stale=1 + dirty 信号，旧向量不参与检索
3. 合并仍要写 merge_events 审计行（不能被重嵌逻辑挤掉）
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from memorykit import MemoryDB
from test_p0_data_safety import HashEmbedder, OfflineEmbedder, make_tmp_db


def learn_one(db, content, source="test"):
    db.learn(content, source=source)


def duplicate_row(db_path, src_key, new_key, new_content):
    """整行复制（vector 保持相同 → 相似度必然 1.0，保证 dedupe 会合并）。"""
    c = sqlite3.connect(db_path)
    row = c.execute("SELECT * FROM memories WHERE key=?", (src_key,)).fetchone()
    cols = [d[0] for d in c.execute("SELECT * FROM memories WHERE key=?", (src_key,)).description]
    rec = dict(zip(cols, row))
    rec.pop("id", None)
    rec["key"] = new_key
    rec["content"] = new_content
    keys = list(rec)
    c.execute("INSERT INTO memories (%s) VALUES (%s)" % (
        ",".join(keys), ",".join("?" * len(keys))), [rec[k] for k in keys])
    c.commit()
    c.close()


class TestDedupeRewritesVector(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(self.tmp, state_db_path="/nonexistent",
                           embedder=HashEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_vector_changes_after_merge(self):
        """核心回归：合并改了内容，向量必须跟着变。"""
        learn_one(self.db, "服务器 V100 双卡 NVLink 关闭后 TP2 速度掉到 11.5")
        c = sqlite3.connect(self.tmp)
        keep_key = c.execute("SELECT key FROM memories LIMIT 1").fetchone()[0]
        before = c.execute(
            "SELECT content, vector FROM memories WHERE key=?", (keep_key,)).fetchone()
        duplicate_row(self.tmp, keep_key, "probe/copy-b",
                      "服务器 V100 双卡 NVLink 关掉后 TP2 速度掉到 11.5（重复）")

        result = self.db.deduplicate(threshold=0.85)
        self.assertEqual(result["merged"], 1, "前提：两条必须被合并")

        after = c.execute(
            "SELECT content, vector, vector_stale FROM memories WHERE key=?",
            (keep_key,)).fetchone()
        c.close()

        self.assertNotEqual(before[0], after[0], "内容应已合并")
        self.assertNotEqual(before[1], after[1],
                            "合并后向量必须重算，否则语义检索用旧向量匹配新内容")
        self.assertEqual(after[2], 0, "重嵌成功时 vector_stale 应为 0")

    def test_merge_events_still_written(self):
        """重嵌逻辑不能挤掉审计行。"""
        learn_one(self.db, "内存条 DDR4 3200 16G 两条组双通道")
        c = sqlite3.connect(self.tmp)
        keep_key = c.execute("SELECT key FROM memories LIMIT 1").fetchone()[0]
        duplicate_row(self.tmp, keep_key, "probe/copy-c", "内存条 DDR4 3200 16G 两条组双通道（重复）")
        self.db.deduplicate(threshold=0.85)
        n = c.execute("SELECT COUNT(*) FROM merge_events").fetchone()[0]
        c.close()
        self.assertGreaterEqual(n, 1, "合并必须留审计行")

    def test_merge_events_keep_full_text(self):
        """审计行里 merged_content 必须是全文，不是截断 200 字。"""
        long_tail = "后半段内容" * 120  # 480 字，远超 200
        learn_one(self.db, "测试条目 " + long_tail)
        c = sqlite3.connect(self.tmp)
        keep_key = c.execute("SELECT key FROM memories LIMIT 1").fetchone()[0]
        duplicate_row(self.tmp, keep_key, "probe/copy-d", "测试条目 " + long_tail + "（重复）")
        self.db.deduplicate(threshold=0.85)
        row = c.execute(
            "SELECT merged_content FROM merge_events ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        self.assertIsNotNone(row)
        self.assertIn(long_tail[-40:], row[0], "审计行必须留全文，不能截断")


class TestDedupeStaleOnReembedFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_db()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", ".dirty"):
            p = self.tmp + suffix
            if os.path.exists(p):
                os.remove(p)

    def test_stale_when_reembed_fails(self):
        """重嵌失败：vector_stale=1，旧向量标记不可信。"""
        db = MemoryDB(self.tmp, state_db_path="/nonexistent", embedder=HashEmbedder())
        learn_one(db, "网卡 Intel X550 双口万兆")
        c = sqlite3.connect(self.tmp)
        keep_key = c.execute("SELECT key FROM memories LIMIT 1").fetchone()[0]
        duplicate_row(self.tmp, keep_key, "probe/copy-e", "网卡 Intel X550 双口万兆（重复）")
        db.close()

        # 换成一个必然失败的 embedder 重新打开（模拟重嵌时嵌入服务不可用）
        db2 = MemoryDB(self.tmp, state_db_path="/nonexistent", embedder=OfflineEmbedder())
        try:
            result = db2.deduplicate(threshold=0.85)
            self.assertEqual(result["merged"], 1)
            row = c.execute(
                "SELECT vector_stale FROM memories WHERE key=?", (keep_key,)).fetchone()
            self.assertEqual(row[0], 1,
                             "重嵌失败必须标 vector_stale=1，否则旧向量继续参与检索")
        finally:
            db2.close()
            c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
