#!/usr/bin/env python3
"""drain_journal 常驻提炼回归测试（P1 #5，2026-08-08）

设计（design-notes §5 修正版）：不建 journal 表——state.db 就是原始消息
存档，drain_journal 用 watermark 记住提炼位置，复用 learn() 提炼带指针。

覆盖：
  #1 dry_run：只统计不写（watermark 不推进、memory 不新增）
  #2 正常提炼：user 消息 → inserted，记忆带指针可回溯
  #3 watermark 推进：第二次跑不重复提炼（processed=0）
  #4 tool 消息跳过（不提炼噪音）
  #5 短消息跳过（<10 字符）
  #6 无 state.db → ok=False no_state_db
  #7 收据字段齐全（inserted/merged/skipped/backlog/last_msg_id）

运行：
  python run_tests.py drain
  或：python -m unittest tests.test_drain_journal -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(prefix="mk-drain-", suffix=".db")
    os.close(fd)
    db = MemoryDB(db_path=path)
    return db, path


def make_state_db(messages):
    """造一个最小 state.db（Hermes messages 表结构子集）。"""
    fd, path = tempfile.mkstemp(prefix="mk-state-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO messages(session_id, role, content, timestamp) "
        "VALUES(?,?,?,?)",
        messages)
    conn.commit()
    conn.close()
    return path


class DrainJournalTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_tmp_db()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.path, getattr(self, "state_path", "")):
            if p and os.path.exists(p):
                os.unlink(p)

    def _set_state(self, msgs):
        self.state_path = make_state_db(msgs)
        self.db.state_db_path = self.state_path

    def test_01_dry_run_no_writes(self):
        self._set_state([
            ("s1", "user", "服务器嵌入服务端口是 8082 而且必须保持运行", "2026-08-08 10:00:00"),
        ])
        r = self.db.drain_journal(dry_run=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["processed"], 1)
        self.assertEqual(r["inserted"], 0)  # dry_run 不写
        # watermark 不推进
        row = self.db._conn.execute(
            "SELECT value FROM drain_state WHERE key='last_msg_id'").fetchone()
        self.assertIsNone(row)
        # memory 不新增
        n = self.db._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(n, 0)

    def test_02_normal_drain_inserts_with_pointer(self):
        self._set_state([
            ("s1", "user", "服务器嵌入服务端口是 8082 而且必须保持运行", "2026-08-08 10:00:00"),
        ])
        r = self.db.drain_journal(limit=50)
        self.assertTrue(r["ok"])
        self.assertEqual(r["processed"], 1)
        self.assertEqual(r["inserted"], 1)
        # 记忆带指针，可回溯
        row = self.db._conn.execute(
            "SELECT id, source_session_id, source_msg_id FROM memories "
            "WHERE source_msg_id='1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source_session_id"], "s1")
        rec = self.db.reconstruct(row["id"])
        self.assertTrue(rec["found"])
        self.assertEqual(rec["source"]["content"],
                         "服务器嵌入服务端口是 8082 而且必须保持运行")

    def test_03_watermark_advances_no_repeat(self):
        self._set_state([
            ("s1", "user", "服务器嵌入服务端口是 8082 而且必须保持运行", "2026-08-08 10:00:00"),
        ])
        r1 = self.db.drain_journal(limit=50)
        self.assertEqual(r1["processed"], 1)
        r2 = self.db.drain_journal(limit=50)
        self.assertEqual(r2["processed"], 0)
        self.assertEqual(r2["inserted"], 0)
        self.assertEqual(r2["backlog"], 0)

    def test_04_tool_messages_skipped(self):
        self._set_state([
            ("s1", "user", "这是一条正常用户消息需要被提炼出来", "2026-08-08 10:00:00"),
            ("s1", "tool", '{"result": "大量工具输出 JSON 噪音不该进记忆"}', "2026-08-08 10:00:01"),
            ("s1", "assistant", "助理回复说端口配置确认好了可以用", "2026-08-08 10:00:02"),
        ])
        r = self.db.drain_journal(limit=50)
        self.assertEqual(r["processed"], 2)  # 只有 user + assistant
        self.assertEqual(r["inserted"], 2)

    def test_05_short_content_skipped(self):
        self._set_state([
            ("s1", "user", "太短", "2026-08-08 10:00:00"),
            ("s1", "user", "这条内容长度足够所以会被提炼处理", "2026-08-08 10:00:01"),
        ])
        r = self.db.drain_journal(limit=50)
        self.assertEqual(r["processed"], 1)
        self.assertEqual(r["inserted"], 1)

    def test_06_no_state_db(self):
        self.db.state_db_path = "/nonexistent/state.db"
        r = self.db.drain_journal()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_state_db")

    def test_07_receipt_fields(self):
        self._set_state([
            ("s1", "user", "第一条待提炼的用户消息内容要够长", "2026-08-08 10:00:00"),
            ("s1", "user", "第二条待提炼的用户消息内容也要够长", "2026-08-08 10:00:01"),
        ])
        r = self.db.drain_journal(limit=1)
        for k in ("ok", "processed", "inserted", "merged", "skipped",
                  "backlog", "last_msg_id", "dry_run"):
            self.assertIn(k, r)
        self.assertEqual(r["processed"], 1)
        self.assertEqual(r["backlog"], 1)  # 还剩 1 条
        r2 = self.db.drain_journal(limit=1)
        self.assertEqual(r2["processed"], 1)
        self.assertEqual(r2["backlog"], 0)


if __name__ == "__main__":
    unittest.main()
