#!/usr/bin/env python3
"""state→learn 派生链（P0 #1，指针方案）回归测试（2026-08-08）

设计（design-notes/scope-recall-borrowing.md §2026-08-08 修正）：
  state.db（原始消息，已有）── learn 记指针 ──→ memory.db（提炼）
     ↑ source_session_id + source_msg_id 回溯重建
  不建 journal 表；memory 表只加两个指针列；reconstruct() 沿指针回
  state.db 找回提炼来源的原始消息（只建"可回溯"，不承诺自动重建）。

覆盖：
  #1 learn 带指针 → 两列写入，reconstruct 能回溯到 source 原始消息
  #2 learn 不带指针 → 不炸且 source 为 NULL，reconstruct 明确返回 no_pointer
  #3 learn_batch 逐条带指针 → 透传写入，可回溯
  #4 merge 路径指针合并：旧指针保留 + 新指针追加（逗号连接）
  #5 旧库迁移：无新列的 memories 表建库后自动 ALTER 补齐，旧行 NULL 不破坏
  #6 reconstruct 失败分支：memory 不存在 / state.db 缺失 / 消息不存在 /
     指针畸形 / session 不匹配
  #7 set() 直接带指针（learn 的插入路径底层），向后兼容

运行：
  python run_tests.py pointer
  或：python -m unittest tests.test_pointer_chain -v
"""
import os
import sys
import sqlite3
import tempfile
import unittest

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def make_state_db(messages=None):
    """造一个 Hermes 风格 state.db（messages 表，含 role 列）供 reconstruct 使用。

    messages: list of (session_id, role, content, timestamp)；id 从 1 自增。
    """
    path = make_tmp_db()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL)"
    )
    for i, (sid, role, content, ts) in enumerate(messages or [], start=1):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, role, content, ts),
        )
    conn.commit()
    conn.close()
    return path


class TestLearnPointerChain(unittest.TestCase):
    """#1/#2：learn 带/不带指针的写入与回溯。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.state = make_state_db([
            ("sess-abc", "user", "显卡价格是550块，显存12G", 1785507925.0),
            ("sess-abc", "assistant", "可以加预算买新卡", 1785507926.0),
        ])
        self.db = MemoryDB(db_path=self.tmp, state_db_path=self.state)

    def tearDown(self):
        for obj in (self.db,):
            try:
                obj.close()
            except Exception:
                pass
        for p in (self.tmp, self.state):
            try:
                os.remove(p)
            except Exception:
                pass

    def test_learn_with_pointer_writes_columns(self):
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session="sess-abc",
                          source_session_id="sess-abc", source_msg_id=1)
        row = self.db._conn.execute(
            "SELECT id, key, source_session_id, source_msg_id "
            "FROM memories WHERE key=?", (r["key"],)).fetchone()
        self.assertEqual(row["source_session_id"], "sess-abc")
        self.assertEqual(row["source_msg_id"], "1")

    def test_reconstruct_roundtrip(self):
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session="sess-abc",
                          source_session_id="sess-abc", source_msg_id=1)
        row = self.db._conn.execute(
            "SELECT id, key FROM memories WHERE key=?", (r["key"],)).fetchone()
        rec = self.db.reconstruct(row["id"])
        self.assertTrue(rec["found"])
        self.assertEqual(rec["source"]["session_id"], "sess-abc")
        self.assertEqual(rec["source"]["msg_id"], 1)
        self.assertEqual(rec["source"]["role"], "user")
        self.assertEqual(rec["source"]["content"], "显卡价格是550块，显存12G")

    def test_learn_without_pointer_no_crash_null_source(self):
        r = self.db.learn("memorykit 是纯 SQLite 库，不依赖 agent 框架")
        row = self.db._conn.execute(
            "SELECT id, key, source_session_id, source_msg_id "
            "FROM memories WHERE key=?", (r["key"],)).fetchone()
        self.assertIsNone(row["source_session_id"])
        self.assertIsNone(row["source_msg_id"])
        rec = self.db.reconstruct(row["id"])
        self.assertFalse(rec["found"])
        self.assertEqual(rec["reason"], "no_pointer")

    def test_set_with_pointers_direct(self):
        # learn 插入路径的底层 set() 也支持指针（向后兼容：不传 = NULL）
        rid = self.db.set("env/pointer_direct", "显卡价格是550块，显存12G",
                          category="env", embed=False,
                          source_session_id="sess-abc", source_msg_id=1)
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
            (rid,)).fetchone()
        self.assertEqual(row["source_session_id"], "sess-abc")
        self.assertEqual(row["source_msg_id"], "1")
        self.assertTrue(self.db.reconstruct(rid)["found"])

    def test_set_without_pointers_backward_compat(self):
        rid = self.db.set("env/plain", "普通写入不带指针", category="env", embed=False)
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
            (rid,)).fetchone()
        self.assertIsNone(row["source_session_id"])
        self.assertIsNone(row["source_msg_id"])

    def test_set_rewrite_preserves_existing_pointers(self):
        # P1 回归（review t_5be03922）：同 key 重写走 ON CONFLICT 路径，
        # 指针列必须 COALESCE 保留旧值——普通内容更新不能抹掉溯源链。
        rid = self.db.set("env/gpu", "显卡价格是550块，显存12G", category="env",
                          embed=False, source_session_id="sess-abc", source_msg_id=1)
        self.assertEqual(
            self.db._conn.execute(
                "SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
                (rid,)).fetchone()["source_msg_id"],
            "1")
        # 不带指针的普通重写 → 指针保留不丢，reconstruct 仍可回溯
        rid2 = self.db.set("env/gpu", "显卡价格涨到600块", category="env", embed=False)
        self.assertEqual(rid2, rid)  # 同 key 覆盖，不新建行
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
            (rid2,)).fetchone()
        self.assertEqual(row["source_session_id"], "sess-abc")
        self.assertEqual(row["source_msg_id"], "1")
        rec = self.db.reconstruct(rid2)
        self.assertTrue(rec["found"])
        self.assertEqual(rec["source"]["msg_id"], 1)

    def test_set_rewrite_with_new_pointer_updates_it(self):
        # 重写时显式带新指针 → 用新指针（COALESCE 只在本次为 NULL 时保留旧值）
        rid = self.db.set("env/gpu2", "显卡价格是550块，显存12G", category="env",
                          embed=False, source_session_id="sess-abc", source_msg_id=1)
        self.db.set("env/gpu2", "显卡价格涨到600块", category="env", embed=False,
                    source_session_id="sess-abc", source_msg_id=2)
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
            (rid,)).fetchone()
        self.assertEqual(row["source_session_id"], "sess-abc")
        self.assertEqual(row["source_msg_id"], "2")


class TestReconstructFailures(unittest.TestCase):
    """#6：reconstruct 的明确失败分支。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.state = make_state_db([
            ("sess-abc", "user", "显卡价格是550块，显存12G", 1785507925.0),
        ])
        self.db = MemoryDB(db_path=self.tmp, state_db_path=self.state)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.tmp, self.state):
            try:
                os.remove(p)
            except Exception:
                pass

    def _learn_with_pointer(self, msg_id=1, sid="sess-abc"):
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session=sid, source_session_id=sid,
                          source_msg_id=msg_id)
        return self.db._conn.execute(
            "SELECT id FROM memories WHERE key=?", (r["key"],)).fetchone()["id"]

    def test_reconstruct_memory_not_found(self):
        rec = self.db.reconstruct(99999)
        self.assertFalse(rec["found"])
        self.assertEqual(rec["reason"], "memory_not_found")

    def test_reconstruct_no_state_db(self):
        mid = self._learn_with_pointer()
        db2 = MemoryDB(db_path=self.tmp)  # 不注入 state_db_path
        try:
            rec = db2.reconstruct(mid)
            self.assertFalse(rec["found"])
            self.assertEqual(rec["reason"], "no_state_db")
        finally:
            db2.close()

    def test_reconstruct_msg_not_found(self):
        mid = self._learn_with_pointer(msg_id=42)  # state.db 无 id=42
        rec = self.db.reconstruct(mid)
        self.assertFalse(rec["found"])
        self.assertEqual(rec["reason"], "msg_not_found")

    def test_reconstruct_session_mismatch(self):
        # msg id 存在但 session 不匹配 → 视为找不到（指针不一致）
        mid = self._learn_with_pointer(msg_id=1, sid="other-session")
        rec = self.db.reconstruct(mid)
        self.assertFalse(rec["found"])
        self.assertEqual(rec["reason"], "msg_not_found")

    def test_reconstruct_bad_pointer(self):
        # learn() 会拒写非整数指针（防 merge 污染），bad_pointer 分支只可能来自
        # 手工 SQL / 历史脏数据——直接注入验证该分支
        mid = self._learn_with_pointer()
        self.db._conn.execute(
            "UPDATE memories SET source_msg_id='abc' WHERE id=?", (mid,))
        self.db._conn.commit()
        rec = self.db.reconstruct(mid)
        self.assertFalse(rec["found"])
        self.assertEqual(rec["reason"], "bad_pointer")

    def test_learn_drops_non_integer_msg_id(self):
        # 非整数 msg id 不写库（拒写 NULL），不抛，不污染
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session_id="sess-abc", source_msg_id="abc")
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id FROM memories WHERE key=?",
            (r["key"],)).fetchone()
        self.assertEqual(row["source_session_id"], "sess-abc")
        self.assertIsNone(row["source_msg_id"])

    def test_reconstruct_state_db_unreadable(self):
        # state 路径存在但不是合法 sqlite 文件 → 不抛，明确失败
        bad = make_tmp_db()
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not a sqlite file")
        mid = self._learn_with_pointer()
        db2 = MemoryDB(db_path=self.tmp, state_db_path=bad)
        try:
            rec = db2.reconstruct(mid)
            self.assertFalse(rec["found"])
            self.assertIn(rec["reason"], ("state_db_unreadable", "msg_not_found"))
        finally:
            db2.close()
            os.remove(bad)


class TestLearnBatchPointers(unittest.TestCase):
    """#3：learn_batch 逐条带指针。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.state = make_state_db([
            ("s1", "user", "显卡价格是550块，显存12G", 1.0),
            ("s2", "user", "我的猫叫小鸡翅，是只银渐层", 2.0),
        ])
        self.db = MemoryDB(db_path=self.tmp, state_db_path=self.state)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.tmp, self.state):
            try:
                os.remove(p)
            except Exception:
                pass

    def test_learn_batch_per_item_pointers(self):
        items = [
            {"content": "显卡价格是550块，显存12G", "source_session_id": "s1",
             "source_msg_id": 1},
            {"content": "我的猫叫小鸡翅，是只银渐层", "source_session_id": "s2",
             "source_msg_id": 2},
            {"content": "memorykit 不依赖 agent 框架"},  # 不带指针
        ]
        results = self.db.learn_batch(items)
        self.assertEqual(len(results), 3)
        # 逐条回溯
        for res in results:
            self.assertNotIn("error", res)
        for i, res in enumerate(results):
            row = self.db._conn.execute(
                "SELECT id FROM memories WHERE key=?", (res["key"],)).fetchone()
            rec = self.db.reconstruct(row["id"])
            if i < 2:
                self.assertTrue(rec["found"], f"item {i} 应可回溯")
                self.assertEqual(rec["source"]["session_id"], f"s{i+1}")
            else:
                self.assertFalse(rec["found"])
                self.assertEqual(rec["reason"], "no_pointer")


class TestMergePointerAccumulation(unittest.TestCase):
    """#4：merge 路径指针合并（旧保留 + 新追加）。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.state = make_state_db([
            ("s1", "user", "我的猫叫小鸡翅，是只银渐层", 1.0),
            ("s2", "user", "我的猫叫小鸡翅，是只银渐层", 2.0),
        ])
        self.db = MemoryDB(db_path=self.tmp, state_db_path=self.state)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        for p in (self.tmp, self.state):
            try:
                os.remove(p)
            except Exception:
                pass

    def test_merge_appends_pointers(self):
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层",
                           source_session="s1",
                           source_session_id="s1", source_msg_id=1)
        r2 = self.db.learn("我的猫叫小鸡翅，是只银渐层",
                           source_session="s2",
                           source_session_id="s2", source_msg_id=2)
        self.assertTrue(r2["merged"])
        row = self.db._conn.execute(
            "SELECT source_session, source_session_id, source_msg_id "
            "FROM memories WHERE key=?", (r1["key"],)).fetchone()
        self.assertEqual(row["source_session"], "s1,s2")
        self.assertEqual(row["source_session_id"], "s1,s2")
        self.assertEqual(row["source_msg_id"], "1,2")

    def test_merge_without_pointer_preserves_old(self):
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层",
                           source_session_id="s1", source_msg_id=1)
        r2 = self.db.learn("我的猫叫小鸡翅，是只银渐层")  # 不带指针
        self.assertTrue(r2["merged"])
        row = self.db._conn.execute(
            "SELECT source_session_id, source_msg_id "
            "FROM memories WHERE key=?", (r1["key"],)).fetchone()
        self.assertEqual(row["source_session_id"], "s1")
        self.assertEqual(row["source_msg_id"], "1")

    def test_merge_reconstruct_multi_source(self):
        # merge 后指针是逗号连接的多来源，reconstruct 应返回全部源
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层",
                           source_session_id="s1", source_msg_id=1)
        self.db.learn("我的猫叫小鸡翅，是只银渐层",
                      source_session_id="s2", source_msg_id=2)
        mid = self.db._conn.execute(
            "SELECT id FROM memories WHERE key=?", (r1["key"],)).fetchone()["id"]
        rec = self.db.reconstruct(mid)
        self.assertTrue(rec["found"])
        self.assertEqual({s["msg_id"] for s in rec["sources"]}, {1, 2})
        self.assertEqual(rec["source"]["msg_id"], 1)


class TestLegacyDbMigration(unittest.TestCase):
    """#5：旧库（无新列）自动迁移，旧行 NULL 不破坏。"""

    def test_migration_adds_columns_and_old_rows_null(self):
        tmp = make_tmp_db()
        conn = sqlite3.connect(tmp)
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                tags TEXT DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'permanent',
                importance REAL DEFAULT 0.5,
                ttl INTEGER DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                source_session TEXT DEFAULT NULL,
                decay_profile TEXT DEFAULT 'medium')
        """)
        conn.execute("INSERT INTO memories (key, content) VALUES ('old/1', '旧数据行')")
        conn.commit()
        conn.close()
        try:
            db = MemoryDB(db_path=tmp)
            try:
                cols = [r[1] for r in db._conn.execute(
                    "PRAGMA table_info(memories)").fetchall()]
                self.assertIn("source_session_id", cols)
                self.assertIn("source_msg_id", cols)
                row = db._conn.execute(
                    "SELECT id, key, source_session_id, source_msg_id "
                    "FROM memories").fetchone()
                self.assertEqual(row["key"], "old/1")
                self.assertIsNone(row["source_session_id"])
                self.assertIsNone(row["source_msg_id"])
                rec = db.reconstruct(row["id"])
                self.assertFalse(rec["found"])
                self.assertEqual(rec["reason"], "no_pointer")
            finally:
                db.close()
        finally:
            os.remove(tmp)


if __name__ == "__main__":
    unittest.main()
