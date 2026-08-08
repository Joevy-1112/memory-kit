#!/usr/bin/env python3
"""hermes-verify: 聚焦验证 t_7d7026b1 改动的行为（P1 COALESCE + docstring P3-1）。
独立于测试套件，直接对 memorykit 跑 P1 复现场景。"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/home/joe/memory-kit")
from memorykit import MemoryDB

# 隔离会落盘的环境变量
envdir = tempfile.mkdtemp(prefix="hermes-verify-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(envdir, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(envdir, "skill_candidates.md")

fails = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def tmp_db(prefix):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def make_state_db():
    path = tmp_db("hermes-verify-state-")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                 "role TEXT, content TEXT, timestamp REAL)")
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                 "VALUES ('sess-abc', 'user', '显卡价格是550块，显存12G', 1785507925.0)")
    conn.commit()
    conn.close()
    return path


# ── 验证 1：P1 核心——不带指针重写同 key，指针保留且可回溯 ──
mem_db, state_db = tmp_db("hermes-verify-mem-"), make_state_db()
db = MemoryDB(db_path=mem_db, state_db_path=state_db)
try:
    rid = db.set("env/gpu", "显卡价格是550块，显存12G", category="env", embed=False,
                 source_session_id="sess-abc", source_msg_id=1)
    rid2 = db.set("env/gpu", "显卡价格涨到600块", category="env", embed=False)  # 无指针重写
    check("P1: 同 key 重写不新建行 (rid 一致)", rid == rid2, f"rid={rid} rid2={rid2}")
    row = db._conn.execute("SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
                           (rid2,)).fetchone()
    check("P1: 重写后 source_session_id 保留", row["source_session_id"] == "sess-abc",
          f"got={row['source_session_id']!r}")
    check("P1: 重写后 source_msg_id 保留", row["source_msg_id"] == "1",
          f"got={row['source_msg_id']!r}")
    rec = db.reconstruct(rid2)
    check("P1: 重写后仍可 reconstruct 回溯", rec["found"] and rec["source"]["msg_id"] == 1,
          f"found={rec['found']} reason={rec.get('reason')}")

    # ── 验证 2：显式带新指针重写 → 新指针生效（COALESCE 不误伤更新路径）──
    rid3 = db.set("env/gpu2", "显卡价格是550块", category="env", embed=False,
                  source_session_id="sess-abc", source_msg_id=1)
    db.set("env/gpu2", "显卡价格涨到600块", category="env", embed=False,
           source_session_id="sess-abc", source_msg_id=2)
    row = db._conn.execute("SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
                           (rid3,)).fetchone()
    check("P2: 带新指针重写 → source_msg_id=2", row["source_msg_id"] == "2",
          f"got={row['source_msg_id']!r}")

    # ── 验证 3：无指针基线不回归（新 key 不带指针 → NULL）──
    rid4 = db.set("env/plain", "普通写入", category="env", embed=False)
    row = db._conn.execute("SELECT source_session_id, source_msg_id FROM memories WHERE id=?",
                           (rid4,)).fetchone()
    check("基线: 新 key 不带指针 → 两列 NULL",
          row["source_session_id"] is None and row["source_msg_id"] is None)
finally:
    db.close()
    for p in (mem_db, state_db):
        try:
            os.remove(p)
        except OSError:
            pass

# ── 验证 4：lifecycle.py reconstruct docstring 含 state_db_unreadable（P3-1）──
with open("/home/joe/memory-kit/memorykit/lifecycle.py", encoding="utf-8") as f:
    src = f.read()
check("P3-1: docstring reason 枚举含 state_db_unreadable",
      "state_db_unreadable" in src)

# ── 验证 5：db.py ON CONFLICT 两列确实为 COALESCE 形式 ──
with open("/home/joe/memory-kit/memorykit/db.py", encoding="utf-8") as f:
    db_src = f.read()
check("db.py: 指针列 COALESCE 保护已落库",
      "COALESCE(excluded.source_session_id, memories.source_session_id)" in db_src
      and "COALESCE(excluded.source_msg_id, memories.source_msg_id)" in db_src)

print()
if fails:
    print(f"hermes-verify: {len(fails)} FAILED -> {fails}")
    sys.exit(1)
print("hermes-verify: 全部通过 (P1 修复 + P3-1 docstring 均验证)")
sys.exit(0)
