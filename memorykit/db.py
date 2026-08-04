#!/usr/bin/env python3
"""
Hermes SQLite Memory DB — 可检索、可过期、无上限的记忆系统。

Usage:
    python memory_db.py set <key> <content> [--category CAT] [--tags TAG,TAG] [--scope PERM|SESSION|TEMP] [--importance 0-1] [--ttl SEC]
    python memory_db.py get <key>
    python memory_db.py search <query> [--category CAT] [--tags TAG] [--scope SCOPE] [--limit N]
    python memory_db.py delete <key>
    python memory_db.py stats
    python memory_db.py cleanup
    python memory_db.py export <filepath>
    python memory_db.py import <filepath>
"""

import sqlite3
import json
import os
import sys
import re
from contextlib import contextmanager
import argparse
import urllib.request
import math
import struct
import logging
import numpy as np
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# Import mixins
from .search import MemorySearch
from .lifecycle import MemoryLifecycle
from .embedder import HTTPEmbedder


EMBEDDING_URL = os.environ.get("MEMORY_EMBED_URL", "http://127.0.0.1:8082")


# 默认数据库路径：当前目录 memory.db（原 Hermes 版硬编码 ~/.hermes/memory.db）
DB_PATH = os.path.join(os.getcwd(), "memory.db")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    tags        TEXT DEFAULT '',
    scope       TEXT NOT NULL DEFAULT 'permanent',
    importance  REAL DEFAULT 0.5,
    ttl         INTEGER DEFAULT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    accessed_at TEXT DEFAULT (datetime('now')),
    vector      TEXT DEFAULT NULL,
    agent_id    TEXT DEFAULT 'hermes',
    origin      TEXT DEFAULT 'unknown',  -- 来源标注: laptop/server/unknown（只写一次，merge 不覆盖）
    source_session TEXT DEFAULT NULL,
    decay_profile TEXT DEFAULT 'medium',
    stale_since  TEXT DEFAULT NULL,     -- 环2: 进入 stale 状态的时间戳（状态机 active→stale→archived→物理删）
    provenance  TEXT DEFAULT NULL,
    entities    TEXT DEFAULT NULL,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT DEFAULT NULL,
    layer       TEXT DEFAULT 'episodic',
    water       INTEGER DEFAULT 2,      -- 0=不可删(核心画像/铁律), 1=需确认, 2=可自动淘汰
    causal_in   TEXT DEFAULT NULL,
    causal_out  TEXT DEFAULT NULL,
    derived_from TEXT DEFAULT NULL,
    compressed_to TEXT DEFAULT NULL,
    source_message_ids TEXT DEFAULT NULL,
    confidence  REAL DEFAULT 0.0,       -- 稳定性/可信度 0-1
    verification_status TEXT DEFAULT 'unverified',
    last_verified_at TEXT DEFAULT NULL,
    conflict    INTEGER DEFAULT 0,
    raw_excerpt TEXT DEFAULT NULL,
    evidence_source TEXT DEFAULT NULL,
    evidence_refs TEXT DEFAULT NULL,
    vector_bin  BLOB DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS entity_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity      TEXT NOT NULL,
    memory_id   INTEGER NOT NULL,
    memory_key  TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_links_entity ON entity_links(entity);
CREATE INDEX IF NOT EXISTS idx_entity_links_memory ON entity_links(memory_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key, content, tags,
    content='memories',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, key, content, tags)
    VALUES ('delete', old.id, old.key, old.content, old.tags);
    INSERT INTO memories_fts(rowid, key, content, tags)
    VALUES (new.id, new.key, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS episodic (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT UNIQUE NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    summary      TEXT DEFAULT '',
    tags         TEXT DEFAULT '',
    key_decisions TEXT DEFAULT '',
    outcome      TEXT DEFAULT '',
    pending_items TEXT DEFAULT '',
    started_at   TEXT NOT NULL,
    ended_at     TEXT DEFAULT NULL,
    session_id_ref TEXT DEFAULT '',
    source       TEXT DEFAULT '',
    importance   REAL DEFAULT 0.5
);

-- 知识图谱：可复用的经验精华
CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,            -- 结构化描述：工具/操作/坑
    domain      TEXT DEFAULT '',           -- 垂直领域，如 "cod-modding", "llama-server"
    tags        TEXT DEFAULT '',
    confidence  REAL DEFAULT 0.5,         -- 稳定性/可信度 0-1
    source_keys TEXT DEFAULT '[]',         -- JSON，源记忆key列表
    entity_names TEXT DEFAULT '[]',        -- JSON，关联实体名列表
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,             -- "uses" "causes" "avoids" "part-of" "prerequisite"
    weight      REAL DEFAULT 1.0,
    UNIQUE(source_id, target_id, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_src ON knowledge_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_tgt ON knowledge_edges(target_id);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title, content, tags, domain,
    content='knowledge_nodes',
    content_rowid='id',
    tokenize='unicode61'
);

-- FTS 触发器
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge_nodes BEGIN
    INSERT INTO knowledge_fts(rowid, title, content, tags, domain)
    VALUES (new.id, new.title, new.content, new.tags, new.domain);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge_nodes BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags, domain)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.domain);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge_nodes BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags, domain)
    VALUES ('delete', old.id, old.title, old.content, old.tags, old.domain);
    INSERT INTO knowledge_fts(rowid, title, content, tags, domain)
    VALUES (new.id, new.title, new.content, new.tags, new.domain);
END;

-- === work_log: agent action tracking (2026-07-13) ===
CREATE TABLE IF NOT EXISTS work_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    turn_number   INTEGER DEFAULT 0,
    action        TEXT NOT NULL,
    target        TEXT DEFAULT '',
    summary       TEXT NOT NULL,
    result        TEXT DEFAULT '',
    details       TEXT DEFAULT '',
    artifacts     TEXT DEFAULT '[]',
    parent_id     INTEGER DEFAULT NULL,
    project       TEXT DEFAULT '',
    tags          TEXT DEFAULT '',
    started_at    TEXT DEFAULT (datetime('now')),
    ended_at      TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_log_session ON work_log(session_id);
CREATE INDEX IF NOT EXISTS idx_work_log_action ON work_log(action);
CREATE INDEX IF NOT EXISTS idx_work_log_started ON work_log(started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS work_log_fts USING fts5(
    action, target, summary, result, details, project, tags,
    content='work_log', content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS work_log_ai AFTER INSERT ON work_log BEGIN
    INSERT INTO work_log_fts(rowid, action, target, summary, result, details, project, tags)
    VALUES (new.id, new.action, new.target, new.summary, new.result, new.details, new.project, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS work_log_ad AFTER DELETE ON work_log BEGIN
    INSERT INTO work_log_fts(work_log_fts, rowid, action, target, summary, result, details, project, tags)
    VALUES ('delete', old.id, old.action, old.target, old.summary, old.result, old.details, old.project, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS work_log_au AFTER UPDATE ON work_log BEGIN
    INSERT INTO work_log_fts(work_log_fts, rowid, action, target, summary, result, details, project, tags)
    VALUES ('delete', old.id, old.action, old.target, old.summary, old.result, old.details, old.project, old.tags);
    INSERT INTO work_log_fts(rowid, action, target, summary, result, details, project, tags)
    VALUES (new.id, new.action, new.target, new.summary, new.result, new.details, new.project, new.tags);
END;

-- === correction_log: 环1 应用反馈审计表 (memory-v2-plan.md 环1, 2026-08-02) ===
-- 记录每次纠正信号 / 正向强化对记忆 confidence 的调整。
-- 只加进 SCHEMA_SQL（新库自动建）；不 ALTER 正式库（FTS 陷阱防护）。
-- 同步: merge_memories.py 需纳入本表。
CREATE TABLE IF NOT EXISTS correction_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT DEFAULT '',
    memory_key       TEXT NOT NULL,
    signal           TEXT NOT NULL,      -- 纠正信号: 不对/错了/不是/你记错了/flash-否定词兜底
    action           TEXT NOT NULL,      -- correction | reinforce_implicit | reinforce_explicit
    confidence_delta REAL DEFAULT 0.0,   -- 本次调整量 (-0.3 / +0.05 / +0.15)
    created_at       TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_correction_log_key ON correction_log(memory_key);
CREATE INDEX IF NOT EXISTS idx_correction_log_created ON correction_log(created_at);
"""


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class MemoryDB(MemorySearch, MemoryLifecycle):
    """SQLite-backed memory store with FTS5 + multi-signal search + lifecycle."""
    def __init__(self, db_path: str = None, state_db_path: str = None,
                 dirty_log: str = None, embedder=None):
        """Initialize memory DB. Also handles schema migration for vector column.

        参数（去 Hermes 耦合，全部可注入）:
          db_path:        记忆库 sqlite 路径。默认当前目录 memory.db（或环境变量 MEMORY_DB_PATH）。
          state_db_path:  state.db（会话消息库）路径。None/文件不存在时，依赖 state.db 的
                          功能（verify_recent 交叉验证、search_state_*、_cross_check）自动
                          降级跳过，不影响核心 learn/search。
          dirty_log:      dirty 信号日志文件路径。None 时改用 Python logging 输出（logger.debug）。
          embedder:       自定义嵌入器对象（实现 embed(text)->list|None 与
                          is_available()->bool）。None 时使用 HTTPEmbedder（默认
                          http://127.0.0.1:8082，可用 MEMORY_EMBED_URL 覆盖）。
        """
        if db_path is None:
            db_path = os.environ.get("MEMORY_DB_PATH", DB_PATH)
        self.db_path = os.path.expanduser(db_path)
        if state_db_path is None:
            state_db_path = os.environ.get("MEMORY_STATE_DB_PATH")
        self.state_db_path = os.path.expanduser(state_db_path) if state_db_path else None
        self.dirty_log = dirty_log
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15)
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        # 可注入 embedder：替换默认 HTTP 嵌入实现（_embed/_is_embedding_available）
        if embedder is not None:
            self._embed = embedder.embed
            self._is_embedding_available = embedder.is_available
        # Migration: add columns for existing databases — only run ALTER for
        # columns that are actually missing (checked via PRAGMA table_info),
        # so repeated MemoryDB() instantiations don't replay 20+ write ops
        # (avoids 'database is locked' contention + wasted IO).
        self._migrate_columns("memories", [
            "ALTER TABLE memories ADD COLUMN vector TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN agent_id TEXT DEFAULT 'hermes'",
            "ALTER TABLE memories ADD COLUMN origin TEXT DEFAULT 'unknown'",
            "ALTER TABLE memories ADD COLUMN source_session TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN decay_profile TEXT DEFAULT 'medium'",
            "ALTER TABLE memories ADD COLUMN stale_since TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN provenance TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN entities TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN water INTEGER DEFAULT 2",
            "ALTER TABLE memories ADD COLUMN vector_bin BLOB DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0",
            "ALTER TABLE memories ADD COLUMN last_accessed TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'episodic'",
            "ALTER TABLE memories ADD COLUMN causal_in TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN causal_out TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN derived_from TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN compressed_to TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN source_message_ids TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN confidence REAL DEFAULT 0.0",
            "ALTER TABLE memories ADD COLUMN verification_status TEXT DEFAULT 'unverified'",
            "ALTER TABLE memories ADD COLUMN last_verified_at TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN conflict INTEGER DEFAULT 0",
            "ALTER TABLE memories ADD COLUMN raw_excerpt TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN evidence_source TEXT DEFAULT NULL",
            "ALTER TABLE memories ADD COLUMN evidence_refs TEXT DEFAULT NULL",
        ])
        # Knowledge nodes extensions
        self._migrate_columns("knowledge_nodes", [
            "ALTER TABLE knowledge_nodes ADD COLUMN trigger_patterns TEXT DEFAULT '[]'",
            "ALTER TABLE knowledge_nodes ADD COLUMN access_count INTEGER DEFAULT 0",
            "ALTER TABLE knowledge_nodes ADD COLUMN last_accessed TEXT DEFAULT NULL",
        ])
        # Create entity_links table (idempotent via IF NOT EXISTS)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entity_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL,
                memory_id INTEGER NOT NULL,
                memory_key TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_entity_links_entity ON entity_links(entity);
            CREATE INDEX IF NOT EXISTS idx_entity_links_memory ON entity_links(memory_id);
        """)
        self._conn.commit()

    def _migrate_columns(self, table: str, col_sqls: list):
        """Run ALTER TABLE ADD COLUMN only for columns that don't exist yet.

        Reads PRAGMA table_info(table) once (read-only, no write lock) and
        skips ALTERs for columns already present. try/except OperationalError
        stays as a safety net for concurrent migrations (duplicate-column
        errors from racing processes are harmless).
        """
        try:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {r[1] for r in rows} if rows else set()
        except sqlite3.Error:
            existing = set()
        for col_sql in col_sqls:
            parts = col_sql.split("ADD COLUMN ", 1)
            col_name = parts[1].split(" ", 1)[0] if len(parts) == 2 else None
            if col_name and col_name in existing:
                continue
            try:
                self._conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass

    def close(self):
        self._conn.close()

    @contextmanager
    def _tx(self):
        """事务安全包装：成功自动 commit，任何异常自动 rollback。

        2026-08-02 新增。Python sqlite3 在 DML 前隐式 BEGIN，若「第一次写成功
        之后、commit 之前」抛异常，写事务会悬挂在连接上、长期持有 WAL 写锁，
        外部进程全部报 database is locked。所有多语句写操作必须走这里。
        """
        try:
            yield
        except BaseException:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
        else:
            self._conn.commit()

    # ── Work Log ───────────────────────────────────────────────────────────

    def log_work(self, session_id: str, action: str, target: str = "",
                 summary: str = "", result: str = "unknown",
                 details: str = "", artifacts: list = None,
                 parent_id: int = None, turn_number: int = 0,
                 project: str = "", tags: str = "") -> int:
        """Insert a work log entry. Returns row id."""
        import json as _json
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        artifacts_json = _json.dumps(artifacts or [])
        self._conn.execute("""
            INSERT INTO work_log(session_id, turn_number, action, target,
                summary, result, details, artifacts, parent_id, started_at,
                project, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, turn_number, action, target,
              summary, result, details, artifacts_json, parent_id, now,
              project, tags))
        self._conn.commit()
        row = self._conn.execute("SELECT last_insert_rowid()").fetchone()
        return row[0] if row else -1

    def search_work_log(self, query: str = "", action: str = None,
                        session_id: str = None, project: str = None,
                        limit: int = 10) -> list[dict]:
        """Search work log by FTS5 or filters. Returns recent entries first."""
        import re as _re
        parts = ["1=1"]
        params = []
        if query and query.strip():
            safe = _re.sub(r'[!@#$%^&*()\\[\\]{}|":;<>?,./\\\\]', ' ', query.strip())
            safe = ' '.join(safe.split())
            if safe:
                try:
                    self._conn.execute(
                        "SELECT 1 FROM work_log_fts WHERE work_log_fts MATCH ?",
                        (safe,))
                    parts.append("w.id IN (SELECT rowid FROM work_log_fts "
                                 "WHERE work_log_fts MATCH ?)")
                    params.append(safe)
                except Exception:
                    for token in safe.split()[:3]:
                        parts.append(
                            "(w.summary LIKE ? OR w.target LIKE ? OR w.details LIKE ?)")
                        params.extend([f"%{token}%"] * 3)
        if action:
            parts.append("w.action = ?")
            params.append(action)
        if session_id:
            parts.append("w.session_id = ?")
            params.append(session_id)
        if project:
            parts.append("w.project = ?")
            params.append(project)
        where = " AND ".join(parts)
        params.append(limit)
        rows = self._conn.execute(f"""
            SELECT w.* FROM work_log w WHERE {where}
            ORDER BY w.started_at DESC LIMIT ?
        """, params).fetchall()
        return [dict(r) for r in rows]

    def recent_work(self, session_id: str = None, limit: int = 5) -> list[dict]:
        """Get most recent work log entries. Optionally filter by session."""
        if session_id:
            rows = self._conn.execute("""
                SELECT * FROM work_log WHERE session_id = ?
                ORDER BY started_at DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute("""
                SELECT * FROM work_log
                ORDER BY started_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── CRUD ──────────────────────────────────────────────────────────────

    def set(self, key: str, content: str, category: str = "general",
            tags: str = "", scope: str = "permanent",
            importance: float = 0.5, ttl: int = None,
            embed: bool = True,
            agent_id: str = "hermes", source_session: str = None,
            decay_profile: str = None, provenance: str = None,
            vector: list = None, water: int = 2) -> int:
        """Insert or replace a memory entry. Optionally computes embedding. Returns row id.
        
        If `vector` is provided (pre-computed), skip embedding call entirely."""
        # Auto-detect decay_profile from category if not specified
        if decay_profile is None:
            if category in ("lesson", "protocol"):
                decay_profile = "slow"
            elif category in ("env", "user"):
                decay_profile = "medium"
            else:
                decay_profile = "medium"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Use pre-computed vector or compute new one
        vec = vector if vector is not None else None
        if vec is None and embed and content and len(content.strip()) >= 10:
            vec = self._embed(content[:5000])  # cap at 5K chars for perf
        vec_json = json.dumps(vec) if vec else None
        vec_bin = struct.pack(f'{len(vec)}f', *vec) if vec else None

        # Extract entities
        extracted = self._extract_entities(content)
        entities_json = json.dumps(extracted) if extracted else None

        # ── 自动置信度估算（轻量启发式）──
        # 基于内容长度、来源session、分类计算初始置信度
        base_conf = 0.45  # 中性起点
        if source_session:
            base_conf += 0.10  # 有源可查 +0.10
        if category in ("lesson", "protocol"):
            base_conf += 0.05  # 结构化类别更可信
        if len(content) > 200:
            base_conf += 0.05  # 长内容通常包含更多上下文
        if len(content) < 20:
            base_conf -= 0.15  # 太短的信息可能碎片化
        confidence = round(min(max(base_conf, 0.15), 0.80), 2)

        # ── provenance: 记录来源信息用于后续验证 ──
        prov_data = {"v": "pending", "src": source_session or "",
                     "len": len(content), "cat": category}
        provenance_final = json.dumps(prov_data) if not provenance else provenance

        self._conn.execute("""
            INSERT INTO memories(key, content, category, tags, scope, importance, ttl,
                created_at, updated_at, accessed_at,
                vector, vector_bin, agent_id, source_session, decay_profile,
                provenance, entities, water, confidence, verification_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                content=excluded.content,
                category=excluded.category,
                tags=excluded.tags,
                scope=excluded.scope,
                importance=excluded.importance,
                ttl=excluded.ttl,
                vector=excluded.vector,
                vector_bin=excluded.vector_bin,
                entities=excluded.entities,
                agent_id=excluded.agent_id,
                source_session=excluded.source_session,
                decay_profile=excluded.decay_profile,
                provenance=excluded.provenance,
                water=excluded.water,
                confidence=excluded.confidence,
                verification_status=excluded.verification_status,
                updated_at=excluded.updated_at,
                accessed_at=excluded.accessed_at
        """, (key, content, category, tags, scope, importance, ttl, now, now, now,
              vec_json, vec_bin, agent_id, source_session, decay_profile,
              provenance_final, entities_json, water, confidence, "pending"))
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM memories WHERE key=?", (key,)).fetchone()
        rid = row["id"] if row else -1
        if rid > 0 and extracted:
            self._update_entity_links(rid, key, extracted)
        # Signal knowledge refresh needed — dirty 信号走 Python logging；
        # 配置了 dirty_log 路径时同时追加写该文件（兼容旧管线的文件轮询方式）
        if self.dirty_log:
            try:
                with open(self.dirty_log, "a", encoding="utf-8") as f:
                    f.write(f"{key}|{rid}|{now}\n")
            except Exception:
                pass
        else:
            logger.debug("dirty: %s|%s|%s", key, rid, now)
        return rid

    def get(self, key: str) -> dict | None:
        """Get a single entry by key. Updates accessed_at."""
        self._conn.execute("UPDATE memories SET accessed_at=datetime('now') WHERE key=?", (key,))
        self._conn.commit()
        row = self._conn.execute("SELECT * FROM memories WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def search(self, query: str = "", category: str = None,
               tags: str = None, scope: str = None, layer: str = None,
               limit: int = 20, offset: int = 0) -> list[dict]:
        """Multi-condition search. Uses FTS5 for English, LIKE for Chinese."""
        conditions = []
        params = []

        if query and query.strip():
            q_raw = query.strip()
            # Prefix search (e.g. "lesson/") — use LIKE only, FTS5 chokes on "/"
            if q_raw.endswith("/"):
                like_conditions = ["m.key LIKE ?"]
                params.append(f"{q_raw}%")
                conditions.append("(" + " OR ".join(like_conditions) + ")")
            else:
                # Try FTS5 first, with graceful fallback on syntax error
                try:
                    fts_condition = "m.rowid IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?)"
                    q = q_raw
                    if " " in q and '"' not in q:
                        q = f'"{q}"'
                    # Test if FTS5 can handle this query
                    test_params = [q]
                    test_sql = "SELECT 1 FROM memories_fts WHERE memories_fts MATCH ?"
                    self._conn.execute(test_sql, test_params)
                    fts_usable = True
                except Exception:
                    fts_usable = False

                # Also prepare LIKE fallback for CJK / special chars
                like_conditions = []
                like_params = []
                for token in q_raw.split():
                    like_conditions.append("m.content LIKE ?")
                    like_params.append(f"%{token}%")
                    # CJK 长查询: FTS5/整句 LIKE 会把连续中文当整体 token，
                    # 而记忆内容常只有部分子串（如查询"显卡掉驱动怎么修" vs 内容"…就会掉驱动"）
                    # → 追加 2-gram 相邻双字 LIKE OR 兜底
                    if re.search(r'[\u4e00-\u9fff]', token) and len(token) >= 3:
                        for i in range(len(token) - 1):
                            like_conditions.append("m.content LIKE ?")
                            like_params.append(f"%{token[i:i+2]}%")

                # Use FTS5 + LIKE as OR (graceful: LIKE only if FTS5 can't handle query)
                if fts_usable:
                    conditions.append(f"({fts_condition} OR {' OR '.join(like_conditions)})")
                    params.append(q)
                else:
                    conditions.append(f"({' OR '.join(like_conditions)})")
                params.extend(like_params)

        if category:
            conditions.append("m.category = ?")
            params.append(category)

        if tags:
            # comma-separated: match any of the tags via FTS5
            tag_conditions = []
            for t in tags.split(","):
                t = t.strip()
                if t:
                    tag_conditions.append("m.rowid IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?)")
                    params.append(f'"{t}"')
            if tag_conditions:
                conditions.append("(" + " OR ".join(tag_conditions) + ")")

        if scope:
            conditions.append("m.scope = ?")
            params.append(scope)

        if layer:
            conditions.append("m.layer = ?")
            params.append(layer)

        # Build query
        conditions.append("(m.ttl IS NULL OR (julianday('now') - julianday(m.created_at)) * 86400 < m.ttl)")

        where = " AND ".join(conditions) if conditions else "1"
        sql = f"""
            SELECT m.* FROM memories m
            WHERE {where}
            ORDER BY m.importance DESC, m.accessed_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        # 2026-08-02 修复（审查 P1-2）：检索是读操作，不在此处写库。
        # 原逻辑每轮 search() 都 UPDATE access_count + commit——search_multi
        # 内部调 search() 导致双重计数 + 抢写锁。改为延迟缓冲 _touch_pending，
        # 与 search_multi 共用同一缓冲，flush_touches() 批量落库。
        # archived/deleted 不记 touch（防止 age() 复活，环2 机制不被架空）。
        if results and query and query.strip():
            buf = getattr(self, "_touch_pending", None)
            if buf is None:
                buf = self._touch_pending = {}
            for r in results:
                if r.get("verification_status") in ("ARCHIVED", "deleted"):
                    continue
                buf.setdefault(r["key"], 0)
                buf[r["key"]] += 1
            if len(buf) >= 50:
                self.flush_touches()
        return results

    def delete(self, key: str, force: bool = False) -> dict:
        """Delete by key. Returns {'ok': True, 'water': level} or error dict.
        Water protection: water=0 → refuse unless force=True.
                         water=1 → warn but proceed.
                         water=2 → allow without question."""
        row = self._conn.execute("SELECT water, content FROM memories WHERE key=?", (key,)).fetchone()
        if not row:
            return {"ok": False, "error": "NOT FOUND"}
        water = row["water"] if row["water"] is not None else 2
        if water == 0 and not force:
            return {"ok": False, "error": f"Water=0 (core data), use --force to delete", "water": 0}
        cur = self._conn.execute("DELETE FROM memories WHERE key=?", (key,))
        self._conn.commit()
        return {"ok": True, "water": water, "deleted": cur.rowcount > 0}

    # ── Maintenance ───────────────────────────────────────────────────────

    def cleanup(self) -> int:
        """Remove expired scope=temp and scope=session entries. Returns count."""
        cur = self._conn.execute("""
            DELETE FROM memories WHERE
                scope IN ('temp', 'session')
                AND ttl IS NOT NULL
                AND (julianday('now') - julianday(created_at)) * 86400 >= ttl
        """)
        self._conn.commit()
        return cur.rowcount

    # ── 统一 confidence engine（环1 应用反馈 + 环3 验证回流共用）───────────
    # 评审修正 #2：所有 confidence 写入必须走这里，同一记忆的 correction
    # 与 verify 用同一套 clamp/落库语义，避免竞态互相打架。

    def _adjust_confidence(self, key: str, delta: float,
                           min_conf: float = 0.0, max_conf: float = 0.95,
                           _in_tx: bool = False):
        """统一 confidence 调整引擎。

        delta: 调整量（correction -0.3 / implicit +0.05 / explicit +0.15 /
               verify -0.10 / -0.15 / +0.10）
        返回 (old_conf, new_conf) 或 None（key 不存在）。
        默认自带 commit；_in_tx=True 时交由调用方事务管理（批量循环用）。
        """
        row = self._conn.execute(
            "SELECT confidence FROM memories WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        old = float(row["confidence"] or 0.0)
        new = round(min(max(old + delta, min_conf), max_conf), 4)
        self._conn.execute(
            "UPDATE memories SET confidence=?, updated_at=datetime('now') WHERE key=?",
            (new, key))
        if not _in_tx:
            self._conn.commit()
        return (old, new)

    def correction(self, key: str, session_id: str = "", signal: str = "",
                   delta: float = -0.3) -> dict:
        """环1 纠正信号落地：confidence -0.3 + verification_status='disputed' + 写 correction_log。

        signal: 触发信号（'不对'/'错了'/'不是'/'你记错了'/'flash-否定词兜底'）
        返回 {"ok": True, "key", "old_conf", "new_conf", "delta", "verification_status"}
        """
        with self._tx():
            adj = self._adjust_confidence(key, delta, _in_tx=True)
            if adj is None:
                return {"ok": False, "error": f"记忆不存在: {key}"}
            self._conn.execute(
                "UPDATE memories SET verification_status='disputed', "
                "last_verified_at=datetime('now'), updated_at=datetime('now') WHERE key=?",
                (key,))
            try:
                self._conn.execute(
                    "INSERT INTO correction_log(session_id, memory_key, signal, "
                    "action, confidence_delta) VALUES (?,?,?,?,?)",
                    (session_id, key, signal or "correction", "correction",
                     round(adj[1] - adj[0], 4)))
            except sqlite3.OperationalError:
                pass  # 旧库无 correction_log 表 → 跳过审计，不阻塞
        return {"ok": True, "key": key, "old_conf": adj[0], "new_conf": adj[1],
                "delta": round(adj[1] - adj[0], 4),
                "verification_status": "disputed"}

    def reinforce(self, key: str, session_id: str = "",
                  mode: str = "implicit") -> dict:
        """正向强化：implicit +0.05（引用+无反驳+继续推进） / explicit +0.15（显式说对）。

        mode: 'implicit' 或 'explicit'
        """
        delta = 0.15 if mode == "explicit" else 0.05
        action = f"reinforce_{mode}"
        with self._tx():
            adj = self._adjust_confidence(key, delta, _in_tx=True)
            if adj is None:
                return {"ok": False, "error": f"记忆不存在: {key}"}
            # 正向强化 = 争议解决：disputed → verified（用户确认了），bump updated_at
            self._conn.execute(
                "UPDATE memories SET verification_status='verified', "
                "last_verified_at=datetime('now'), updated_at=datetime('now') "
                "WHERE key=? AND verification_status='disputed'",
                (key,))
            try:
                self._conn.execute(
                    "INSERT INTO correction_log(session_id, memory_key, signal, "
                    "action, confidence_delta) VALUES (?,?,?,?,?)",
                    (session_id, key, action, action,
                     round(adj[1] - adj[0], 4)))
            except sqlite3.OperationalError:
                pass  # 旧库无 correction_log 表 → 跳过审计，不阻塞
        return {"ok": True, "key": key, "old_conf": adj[0], "new_conf": adj[1],
                "delta": round(adj[1] - adj[0], 4), "mode": mode}

    def verify_recent(self, limit: int = 50) -> dict:
        """交叉验证最近未验证的记忆。
        对每条 pending/unverified 记忆，检查其 source_session 在 state.db
        中是否曾经出现——有源可查 → 升置信 + 标记 verified，源不可达 → 降级。

        返回 {"verified": N, "degraded": N, "skipped": N}"""
        import sqlite3 as _sql
        import os as _os
        import json as _json

        state_path = self.state_db_path or ""
        verified = degraded = skipped = 0

        # 取最近 N 条未验证记忆
        rows = self._conn.execute("""
            SELECT id, key, content, confidence, source_session, provenance
            FROM memories
            WHERE verification_status IN ('pending', 'unverified')
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        if not rows:
            return {"verified": 0, "degraded": 0, "skipped": 0}

        try:
            sconn = _sql.connect(state_path)
            sconn.row_factory = _sql.Row
        except Exception:
            # state.db 不在——全部标记 unverifiable
            with self._tx():
                for r in rows:
                    # 统一 confidence engine（-0.10）
                    self._adjust_confidence(r["key"], -0.10, _in_tx=True)
                    self._conn.execute("""
                        UPDATE memories SET verification_status='unverifiable',
                        last_verified_at=datetime('now')
                        WHERE id=?
                    """, (r["id"],))
                    degraded += 1
            return {"verified": 0, "degraded": degraded, "skipped": 0}

        # 2026-08-02 修复：所有 UPDATE 包在 _tx 里——任何异常（含下面 state.db
        # 查询失败）都会 rollback，绝不把写事务/写锁悬挂在连接上（否则外部进程
        # 全部 database is locked）。state.db 查询本身加保护：读失败按"源不可达"
        # 降级处理，不再让整个循环抛异常。
        try:
            with self._tx():
                for r in rows:
                    sid = r["source_session"]
                    if not sid:
                        # 无来源 → 标记 unverifiable，降置信（统一 engine -0.10）
                        self._adjust_confidence(r["key"], -0.10, _in_tx=True)
                        self._conn.execute("""
                            UPDATE memories SET verification_status='unverifiable',
                            last_verified_at=datetime('now')
                            WHERE id=?
                        """, (r["id"],))
                        degraded += 1
                        continue

                    # 检查 source_session 在 state.db 中是否存在
                    try:
                        exists = sconn.execute(
                            "SELECT 1 FROM messages WHERE session_id=? LIMIT 1", (sid,)
                        ).fetchone()
                    except Exception:
                        exists = None  # state.db 暂时不可读 → 按源不可达降级

                    if exists:
                        # 源存在 → 验证通过（统一 engine +0.10）
                        self._adjust_confidence(r["key"], +0.10, _in_tx=True)
                        prov = r["provenance"]
                        try:
                            pdata = _json.loads(prov) if prov else {}
                        except Exception:
                            pdata = {}
                        pdata["v"] = "verified"
                        pdata["verified_at"] = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                        self._conn.execute("""
                            UPDATE memories SET verification_status='verified',
                            provenance=?, last_verified_at=datetime('now')
                            WHERE id=?
                        """, (_json.dumps(pdata), r["id"]))
                        verified += 1
                    else:
                        # 源丢失或错误 → 降级（统一 engine -0.15）
                        self._adjust_confidence(r["key"], -0.15, _in_tx=True)
                        self._conn.execute("""
                            UPDATE memories SET verification_status='degraded',
                            last_verified_at=datetime('now')
                            WHERE id=?
                        """, (r["id"],))
                        degraded += 1
            return {"verified": verified, "degraded": degraded, "skipped": skipped}
        finally:
            try:
                sconn.close()
            except Exception:
                pass

    def stats(self) -> dict:
        """Return memory statistics."""
        total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_category = {
            r["category"]: r["cnt"]
            for r in self._conn.execute("SELECT category, COUNT(*) as cnt FROM memories GROUP BY category").fetchall()
        }
        by_scope = {
            r["scope"]: r["cnt"]
            for r in self._conn.execute("SELECT scope, COUNT(*) as cnt FROM memories GROUP BY scope").fetchall()
        }
        avg_imp = self._conn.execute("SELECT AVG(importance) FROM memories").fetchone()[0] or 0.0
        expired = self._conn.execute("""
            SELECT COUNT(*) FROM memories
            WHERE ttl IS NOT NULL AND (julianday('now') - julianday(created_at)) * 86400 >= ttl
        """).fetchone()[0]
        return {
            "total": total,
            "by_category": by_category,
            "by_scope": by_scope,
            "avg_importance": round(avg_imp, 2),
            "expired_count": expired,
        }

    # ── Embedding ──────────────────────────────────────────────────────────

    def _is_embedding_available(self) -> bool:
        """Check if the embedding server is reachable."""
        try:
            req = urllib.request.Request(f"{EMBEDDING_URL}/v1/models")
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception:
            return False

    def _embed(self, text: str) -> list[float] | None:
        """Call the local embedding server. Returns a vector or None on failure."""
        if not text or len(text.strip()) < 3:
            return None
        try:
            data = json.dumps({"input": text, "model": "default"}).encode("utf-8")
            req = urllib.request.Request(
                f"{EMBEDDING_URL}/v1/embeddings",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req, timeout=5)
            result = json.loads(resp.read())
            vec = result["data"][0]["embedding"]
            return vec if isinstance(vec, list) else None
        except Exception:
            return None

    @staticmethod
    def _vector_dim_from_blob(blob) -> int:
        """Return float32 vector dimension stored in a BLOB, or 0 if invalid."""
        try:
            size = len(blob)
            return size // 4 if size and size % 4 == 0 else 0
        except Exception:
            return 0

    def _cosine_similarity(self, a, b) -> float:
        """Cosine similarity — numpy accelerated. Supports list or ndarray."""
        if a is None or b is None or len(a) != len(b):
            return 0.0
        a_arr = np.asarray(a, dtype=np.float32)
        b_arr = np.asarray(b, dtype=np.float32)
        if a_arr.size == 0 or b_arr.size == 0:
            return 0.0
        dot = np.dot(a_arr, b_arr)
        na = np.linalg.norm(a_arr)
        nb = np.linalg.norm(b_arr)
        if na == 0 or nb == 0:
            return 0.0
        return float(dot / (na * nb))

    def search_semantic(self, query: str, limit: int = 10,
                        category: str = None, scope: str = None) -> list[dict]:
        """Semantic search: embed query, cosine similarity against stored vectors."""
        qvec = self._embed(query)
        if not qvec:
            return []  # embedding server unavailable, return empty

        # Build SQL filters
        conditions = ["m.vector IS NOT NULL"]  # only entries with vectors
        params = []
        if category:
            conditions.append("m.category = ?"); params.append(category)
        if scope:
            conditions.append("m.scope = ?"); params.append(scope)
        conditions.append("(m.ttl IS NULL OR (julianday('now') - julianday(m.created_at)) * 86400 < m.ttl)")
        where = " AND ".join(conditions)

        rows = self._conn.execute(
            f"SELECT m.* FROM memories m WHERE {where}", params
        ).fetchall()

        if not rows:
            return []

        qvec_np = np.array(qvec, dtype=np.float32)
        qdim = int(qvec_np.size)

        # Try binary blobs first (fast path), fall back to JSON.
        # Important: old memory vectors may be 1024d while the currently running
        # embedding service can return 2048d (Qwen3-VL).  Do not let an online
        # but dimension-incompatible service crash search/prefetch; skip
        # incompatible vectors so callers can fall back to FTS/RRF.
        blobs = []
        indices = []
        for idx, r in enumerate(rows):
            blob = r["vector_bin"]
            if blob is not None and self._vector_dim_from_blob(blob) == qdim:
                blobs.append(blob)
                indices.append(idx)

        if blobs:
            # Fast path: numpy batch cosine over binary blobs
            vecs_np = np.array([np.frombuffer(b, dtype=np.float32) for b in blobs])
            dots = np.dot(vecs_np, qvec_np)
            norms_v = np.linalg.norm(vecs_np, axis=1)
            norm_q = np.linalg.norm(qvec_np)
            if norm_q > 0:
                scores = dots / np.maximum(norms_v * norm_q, 1e-10)
            else:
                scores = np.zeros(len(blobs))
            scored = []
            for i, idx in enumerate(indices):
                d = dict(rows[idx])
                d["similarity"] = round(float(scores[i]), 4)
                scored.append(d)
        else:
            # Slow path: JSON parse fallback, also dimension-safe.
            scored = []
            for r in rows:
                d = dict(r)
                vec = json.loads(d["vector"]) if d.get("vector") else None
                if vec and len(vec) == qdim:
                    score = self._cosine_similarity(qvec, vec)
                    d["similarity"] = round(score, 4)
                    scored.append(d)

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]

    def backfill_vectors(self, limit: int = 500) -> int:
        """Compute embeddings for existing entries that lack them. Returns count.
        Also auto-migrates json vectors to binary format."""
        if not self._is_embedding_available():
            return -1  # server down
        
        # First: migrate existing json vectors to binary
        json_rows = self._conn.execute(
            "SELECT id, vector FROM memories WHERE vector IS NOT NULL AND vector_bin IS NULL"
        ).fetchall()
        migrated = 0
        with self._tx():
            for r in json_rows:
                try:
                    vec = json.loads(r["vector"])
                    if vec:
                        self._conn.execute(
                            "UPDATE memories SET vector_bin=? WHERE id=?",
                            (struct.pack(f'{len(vec)}f', *vec), r["id"])
                        )
                        migrated += 1
                except Exception:
                    pass
        if migrated > 0:
            logger.info("[backfill] migrated %d json vectors to binary", migrated)
        
        # Then: backfill missing vectors
        sql = "SELECT id, key, content FROM memories WHERE vector IS NULL"
        params = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        count = 0
        with self._tx():
            for r in rows:
                vec = self._embed(str(r["content"])[:5000])
                if vec:
                    self._conn.execute(
                        "UPDATE memories SET vector=?, vector_bin=? WHERE id=?",
                        (json.dumps(vec), struct.pack(f'{len(vec)}f', *vec), r["id"])
                    )
                    count += 1
                if count % 10 == 0:
                    self._conn.commit()
        return count

    # ── Import / Export ───────────────────────────────────────────────────

    def export(self, filepath: str):
        """Export all memories to a JSON file."""
        rows = self._conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
        data = [dict(r) for r in rows]
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_json(self, filepath: str, merge: bool = True):
        """Import memories from a JSON file. merge=True: update on key conflict."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        with self._tx():
            for item in data:
                key = item.get("key")
                if not key:
                    continue
                if merge:
                    existing = self._conn.execute("SELECT id FROM memories WHERE key=?", (key,)).fetchone()
                    if existing:
                        self._conn.execute("""
                            UPDATE memories SET content=?, category=?, tags=?, scope=?,
                                importance=?, ttl=?, updated_at=datetime('now')
                            WHERE key=?
                        """, (item.get("content", ""), item.get("category", "general"),
                              item.get("tags", ""), item.get("scope", "permanent"),
                              item.get("importance", 0.5), item.get("ttl"), key))
                        count += 1
                        continue
                # Single path: set() handles both insert and upsert
                self.set(key, item.get("content", ""), item.get("category", "general"),
                         item.get("tags", ""), item.get("scope", "permanent"),
                         item.get("importance", 0.5), item.get("ttl"))
                count += 1
        return count

    # ── 情节时间线 (Episodic Timeline) ─────────────────────────────────────

    def episodic_start(self, session_id: str, source: str = "tui",
                       title: str = None) -> str:
        """Start a new episode. Returns the key."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name = title or f"Session {session_id[:8]}"
        key = f"episodic/{session_id[:16]}"
        try:
            self._conn.execute("""
                INSERT OR IGNORE INTO episodic(key, title, started_at, session_id_ref, source)
                VALUES (?, ?, ?, ?, ?)
            """, (key, name, now, session_id, source))
            self._conn.commit()
        except Exception:
            pass
        return key

    def episodic_end(self, session_id: str, summary: str = "",
                     tags: str = "", key_decisions: str = "",
                     outcome: str = "", pending_items: str = "") -> bool:
        """End an episode. Returns True if updated."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        key = f"episodic/{session_id[:16]}"
        cur = self._conn.execute("""
            UPDATE episodic SET
                ended_at=?, summary=?, tags=?, key_decisions=?,
                outcome=?, pending_items=?, importance=0.7
            WHERE key=?
        """, (now, summary, tags, key_decisions, outcome, pending_items, key))
        self._conn.commit()
        return cur.rowcount > 0

    def episodic_list(self, limit: int = 10) -> list[dict]:
        """Return recent episodes sorted by started_at desc."""
        rows = self._conn.execute("""
            SELECT * FROM episodic
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def episodic_get(self, session_id: str) -> dict | None:
        """Get a specific episode by session_id."""
        key = f"episodic/{session_id[:16]}"
        row = self._conn.execute(
            "SELECT * FROM episodic WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def episodic_search(self, query: str, limit: int = 5) -> list[dict]:
        """Search episodic table by summary/title/decisions."""
        if not query or not query.strip():
            return self.episodic_list(limit)
        q = f"%{query.strip()}%"
        rows = self._conn.execute("""
            SELECT * FROM episodic WHERE
                summary LIKE ? OR title LIKE ? OR tags LIKE ? OR key_decisions LIKE ?
            ORDER BY started_at DESC
            LIMIT ?
        """, (q, q, q, q, limit)).fetchall()
        return [dict(r) for r in rows]

    def episodic_delete(self, session_id: str) -> bool:
        """Delete an episode by session_id."""
        key = f"episodic/{session_id[:16]}"
        cur = self._conn.execute("DELETE FROM episodic WHERE key=?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

    # ── 实体提取 ──────────────────────────────────────────────────────────

    ENTITY_PATTERNS = {
        # 技术/产品名: 大写字母+数字组合
        "tech": r'\b[A-Z][a-z]*[-]?[A-Z]?[a-z]*\d*(?:[-/]\w+)*\b',
        # 数字版本号
        "version": r'\bv?\d+\.\d+\.\d+\b',
        # 中英文混合术语 (GPU, API, LLM等)
        "acronym": r'\b[A-Z]{2,10}\b',
        # 文件路径
        "path": r'[A-Z]?:\\[\\\w\-. ]+',
    }

    def _extract_entities(self, text: str) -> list[str]:
        """从文本中提取关键实体。评分排序+噪音过滤。

        注意: Python 3 re 的 \\w 包含 CJK，含中文的文本中 \\b 边界不生效。
        因此所有英文模式用 re.ASCII 确保 \\b/\\w 行为正确。
        """
        import re
        if not text or len(text) < 10:
            return []

        candidates = {}  # entity -> score

        # 通用停用词（大小写不敏感）
        common_words = {"the","this","that","with","from","have","been","were","what",
                        "when","where","which","their","there","about","would","could",
                        "should","after","before","between","other","than","into","more",
                        "some","such","these","those","also","very","just","than","then",
                        "each","both","over","still","while","because","is","or","to",
                        "in","at","by","ok","it","on","as","be","an","not","no","of",
                        "and","for","can","may","all","are","was","has","had","its"}

        # 时间/地点的过滤模式
        time_loc_patterns = [
            r'Asia/\w+', r'\bUTC[+-]\d+\b', r'\bGMT[+-]\d+\b',
            r'\b\d{4}-\d{2}-\d{2}\b', r'\b\d{2}:\d{2}:\d{2}\b',
        ]

        skip_these = set()
        for pat in time_loc_patterns:
            for m in re.finditer(pat, text):
                skip_these.add(m.group())

        def is_skip(e):
            if e in skip_these:
                return True
            for s in skip_these:
                if e in s:
                    return True
            return False

        # ── 英文技术术语 (带re.ASCII确保\b在CJK旁仍生效) ──

        # 1. 大写缩写: GPU, API, LLM (3-10个大写字母)
        for m in re.finditer(r'\b[A-Z]{3,10}\b', text, re.ASCII):
            e = m.group().strip()
            if len(e) >= 3 and not is_skip(e):
                candidates[e] = candidates.get(e, 0) + 1.5

        # 2. 大驼峰/混合大小写的技术名 (CamelCase, Qwen3_5)
        for m in re.finditer(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+\d*)*\b', text, re.ASCII):
            e = m.group().strip()
            if len(e) >= 3 and not is_skip(e) and e.lower() not in common_words:
                score = 1.5 if any(c.isdigit() for c in e) else 1.0
                candidates[e] = candidates.get(e, 0) + score

        # 3. 带数字的技术术语: RX6600, Qwen3.5-2B, llama.cpp
        for m in re.finditer(r'\b[A-Za-z][A-Za-z0-9]*[-.]?\d+[A-Za-z0-9]*\b', text, re.ASCII):
            e = m.group()
            if len(e) >= 3 and not is_skip(e):
                candidates[e] = candidates.get(e, 0) + 2.5

        # 4. 小写技术词: llama, docker, python, linux等(≥4字母,不含空停用词)
        for m in re.finditer(r'\b[a-z]{4,}\b', text, re.ASCII):
            e = m.group()
            if e not in common_words and not is_skip(e):
                candidates[e] = candidates.get(e, 0) + 1.0

        # 5. 版本号
        for m in re.finditer(r'\bv?\d+\.\d+(?:\.\d+)?\b', text, re.ASCII):
            e = m.group()
            candidates[e] = candidates.get(e, 0) + 1.5

        # ── 中文实体 ──

        # 6. 中文技术/关键名词 (2-6字, 含GPU/CPU/模型/驱动等关键词)
        stopwords = {"因为","所以","但是","而且","如果","虽然","然后","可以",
                     "这个","那个","什么","怎么","不是","就是","没有","一个",
                     "已经","知道","需要","能够","合并自","默认","之前",
                     "之后","这样","那样","这里","那里","如何","为何",
                     "就是","而是","还是","或者","并且"}
        for m in re.finditer(r'[\u4e00-\u9fff]{2,6}', text):
            e = m.group().strip()
            if e not in stopwords:
                # 含关键词的实体加分
                score = 1.0
                if any(kw in e for kw in ("GPU","CPU","AI","API","模型","驱动",
                                          "配置","版本","内存","系统","网络",
                                          "训练","学习","数据","服务")):
                    score = 2.0
                candidates[e] = candidates.get(e, 0) + score

        # 过滤低频噪音
        sorted_entities = sorted(candidates.items(), key=lambda x: -x[1])
        result = []
        for e, s in sorted_entities:
            if s < 1.0:
                continue
            # 跳过纯数字实体
            if re.match(r'^-?\d+(?:\.\d+)?$', e):
                continue
            # 跳过纯符号/空格
            if not re.search(r'[a-zA-Z\u4e00-\u9fff]', e):
                continue
            result.append(e)
            if len(result) >= 15:
                break

        return result

    def _update_entity_links(self, memory_id: int, memory_key: str, entities: list[str]):
        """更新实体关联表。先删旧链，再插新链。

        2026-08-02 修复：原实现 DELETE+INSERT 后从不 commit——后续任何语句
        抛异常都会让写事务悬挂在连接上，长期持有 memory.db 的 WAL 写锁
        （外部进程表现为 database is locked）。改用 _tx 保证事务闭合。
        """
        with self._tx():
            self._conn.execute("DELETE FROM entity_links WHERE memory_id=?", (memory_id,))
            for e in entities:
                self._conn.execute(
                    "INSERT INTO entity_links(entity, memory_id, memory_key, weight) VALUES (?, ?, ?, ?)",
                    (e, memory_id, memory_key, 1.0)
                )

    # ══════════════════════════════════════════════════════════════
    # 知识图谱 (knowledge_nodes + knowledge_edges)
    # ══════════════════════════════════════════════════════════════

    def knowledge_promote(self, memory_key: str, title: str, domain: str = "",
                          tags: str = "", confidence: float = 0.5) -> dict:
        """Promote a memory entry to a knowledge node."""
        mem = self.get(memory_key)
        if not mem:
            return {"error": f"Memory '{memory_key}' not found"}
        source_keys = json.dumps([memory_key])
        # Get entity names from the memory if present
        entity_names = "[]"
        if mem.get("entities"):
            try:
                ents = json.loads(mem["entities"]) if isinstance(mem["entities"], str) else mem["entities"]
                entity_names = json.dumps(list(set(ents)))
            except (json.JSONDecodeError, TypeError):
                pass
        c = self._conn.execute(
            "INSERT INTO knowledge_nodes(title, content, domain, tags, confidence, source_keys, entity_names) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, mem["content"], domain, tags, confidence, source_keys, entity_names)
        )
        self._conn.commit()
        return {"id": c.lastrowid, "title": title, "source": memory_key}

    def knowledge_set(self, title: str, content: str, domain: str = "",
                      tags: str = "", confidence: float = 0.5,
                      source_keys: list = None, entity_names: list = None) -> int:
        """Create a knowledge node directly (no source memory needed)."""
        sk = json.dumps(source_keys or [])
        en = json.dumps(entity_names or [])
        c = self._conn.execute(
            "INSERT INTO knowledge_nodes(title, content, domain, tags, confidence, source_keys, entity_names) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, content, domain, tags, confidence, sk, en)
        )
        self._conn.commit()
        return c.lastrowid

    def knowledge_get(self, kid: int) -> dict:
        row = self._conn.execute(
            "SELECT * FROM knowledge_nodes WHERE id=?", (kid,)
        ).fetchone()
        return dict(row) if row else None

    def knowledge_search(self, query: str, limit: int = 10) -> list[dict]:
        """Search knowledge layer via FTS5, with LIKE fallback for CJK."""
        if not query:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_nodes ORDER BY confidence DESC LIMIT ?",
                (limit,)
            ).fetchall()
        else:
            # FTS5 first
            rows = self._conn.execute(
                "SELECT kn.* FROM knowledge_fts kfts "
                "JOIN knowledge_nodes kn ON kn.id = kfts.rowid "
                "WHERE knowledge_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit)
            ).fetchall()
            # If FTS5 returns nothing (CJK issue), fall back to LIKE
            if not rows:
                like_q = f"%{query}%"
                rows = self._conn.execute(
                    "SELECT * FROM knowledge_nodes "
                    "WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? OR domain LIKE ? "
                    "ORDER BY confidence DESC LIMIT ?",
                    (like_q, like_q, like_q, like_q, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    def knowledge_search_deep(self, query: str, limit: int = 10,
                              confidence_threshold: float = 0.4) -> dict:
        """Search knowledge layer first, fall through to memory layer if confidence insufficient."""
        knew = self.knowledge_search(query, limit)
        result = {"knowledge": [], "memories": [], "fallback": False}
        # Check if any knowledge node has confidence >= threshold and match quality
        good_knowledge = [k for k in knew if k["confidence"] >= confidence_threshold]
        if good_knowledge:
            result["knowledge"] = good_knowledge[:limit]
            # Still attach linked source memories for context
            source_keys = set()
            for k in good_knowledge:
                try:
                    sk = json.loads(k["source_keys"]) if isinstance(k["source_keys"], str) else k["source_keys"]
                    source_keys.update(sk)
                except (json.JSONDecodeError, TypeError):
                    pass
            if source_keys:
                placeholders = ",".join("?" for _ in source_keys)
                mems = self._conn.execute(
                    f"SELECT key, content, category, tags, importance FROM memories "
                    f"WHERE key IN ({placeholders})", list(source_keys)
                ).fetchall()
                result["memories"] = [dict(m) for m in mems]
            return result
        # Fall through: confidence insufficient, search memory layer
        result["fallback"] = True
        mems = self.search_multi(query, limit) if hasattr(self, 'search_multi') else \
               self.search(query, limit=limit)
        result["memories"] = mems
        return result

    def knowledge_link(self, source_id: int, target_id: int,
                       rel_type: str = "related", weight: float = 1.0) -> dict:
        """Add a directed edge between two knowledge nodes."""
        try:
            self._conn.execute(
                "INSERT INTO knowledge_edges(source_id, target_id, rel_type, weight) "
                "VALUES (?, ?, ?, ?)",
                (source_id, target_id, rel_type, weight)
            )
            self._conn.commit()
            return {"ok": True}
        except sqlite3.IntegrityError as e:
            return {"error": str(e)}

    def knowledge_graph(self, kid: int, max_hops: int = 2) -> dict:
        """BFS traversal from a knowledge node. Returns subgraph."""
        nodes = {}; edges = []
        visited = {kid}
        frontier = {kid}
        for hop in range(max_hops):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = self._conn.execute(
                f"SELECT ke.*, kns.title as src_title, knt.title as tgt_title "
                f"FROM knowledge_edges ke "
                f"JOIN knowledge_nodes kns ON kns.id = ke.source_id "
                f"JOIN knowledge_nodes knt ON knt.id = ke.target_id "
                f"WHERE ke.source_id IN ({placeholders}) OR ke.target_id IN ({placeholders})",
                list(frontier) + list(frontier)
            ).fetchall()
            new_frontier = set()
            for r in rows:
                edges.append({"source": r["source_id"], "target": r["target_id"],
                              "type": r["rel_type"], "weight": r["weight"]})
                if r["source_id"] not in visited:
                    visited.add(r["source_id"]); new_frontier.add(r["source_id"])
                if r["target_id"] not in visited:
                    visited.add(r["target_id"]); new_frontier.add(r["target_id"])
            frontier = new_frontier
        # Fetch all node details
        if visited:
            placeholders = ",".join("?" for _ in visited)
            for row in self._conn.execute(
                f"SELECT * FROM knowledge_nodes WHERE id IN ({placeholders})", list(visited)
            ).fetchall():
                nodes[row["id"]] = {"title": row["title"], "domain": row["domain"],
                                     "confidence": row["confidence"], "tags": row["tags"]}
        return {"nodes": nodes, "edges": edges}

    def knowledge_list(self, domain: str = "", limit: int = 50) -> list[dict]:
        if domain:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_nodes WHERE domain=? ORDER BY confidence DESC LIMIT ?",
                (domain, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_nodes ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def knowledge_delete(self, kid: int) -> bool:
        self._conn.execute("DELETE FROM knowledge_edges WHERE source_id=? OR target_id=?", (kid, kid))
        c = self._conn.execute("DELETE FROM knowledge_nodes WHERE id=?", (kid,))
        self._conn.commit()
        return c.rowcount > 0

    def knowledge_stats(self) -> dict:
        n = self._conn.execute("SELECT COUNT(*) as c FROM knowledge_nodes").fetchone()["c"]
        e = self._conn.execute("SELECT COUNT(*) as c FROM knowledge_edges").fetchone()["c"]
        domains = {}
        for r in self._conn.execute("SELECT domain, COUNT(*) as c FROM knowledge_nodes GROUP BY domain"):
            domains[r["domain"]] = r["c"]
        return {"nodes": n, "edges": e, "domains": domains}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="Hermes SQLite Memory DB")
    sub = parser.add_subparsers(dest="cmd")

    p_set = sub.add_parser("set", help="Write a memory entry")
    p_set.add_argument("key")
    p_set.add_argument("content")
    p_set.add_argument("--category", default="general")
    p_set.add_argument("--tags", default="")
    p_set.add_argument("--scope", default="permanent", choices=["permanent", "session", "temp"])
    p_set.add_argument("--importance", type=float, default=0.5)
    p_set.add_argument("--ttl", type=int, default=None)

    p_get = sub.add_parser("get", help="Get a memory by key")
    p_get.add_argument("key")

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--category")
    p_search.add_argument("--tags")
    p_search.add_argument("--scope")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--semantic", action="store_true",
                          help="Use embedding-based semantic search instead of FTS5")
    p_search.add_argument("--multi", action="store_true",
                          help="Multi-signal: FTS5 + semantic + tags fused via RRF")

    p_del = sub.add_parser("delete", help="Delete a memory by key")
    p_del.add_argument("key")
    p_del.add_argument("--force", action="store_true",
                       help="Bypass water protection (water=0 core data)")

    sub.add_parser("stats", help="Memory statistics")
    sub.add_parser("cleanup", help="Remove expired entries")

    p_backfill = sub.add_parser("backfill", help="Compute embeddings for entries missing them")
    p_backfill.add_argument("--limit", type=int, default=500)

    sub.add_parser("age", help="Age all memories: decay importance by last access time")
    sub.add_parser("deduplicate", help="Find and merge semantically duplicate entries")
    sub.add_parser("compact", help="Purge expired + low-importance entries")
    sub.add_parser("conflicts", help="Detect cross-agent key conflicts")
    sub.add_parser("entity-stats", help="Show entity graph statistics")
    sub.add_parser("backfill-entities", help="Extract entities for entries missing them")

    p_entity = sub.add_parser("entity-graph", help="Query entity connections")
    p_entity.add_argument("entity", help="Entity name to search")

    p_low = sub.add_parser("low-importance", help="Count entries below importance threshold")
    p_low.add_argument("--threshold", type=float, default=0.2)

    p_learn = sub.add_parser("learn", help="Auto-learn: classify + store + embed in one step")
    p_learn.add_argument("content", help="What you learned")
    p_learn.add_argument("--source", default="task",
                         choices=["task", "session", "correction", "discovery"])

    # ── 验证管线 CLI ──
    p_verify = sub.add_parser("verify", help="验证单条记忆：交叉检查源session → 评分")
    p_verify.add_argument("key", help="记忆 key")
    p_verify.add_argument("--no-degrade", action="store_true",
                          help="不自动降级低分记忆")
    p_verify.add_argument("--threshold", type=float, default=0.3,
                          help="降级阈值 (default: 0.3)")

    p_verify_batch = sub.add_parser("verify-batch", help="批量验证记忆")
    p_verify_batch.add_argument("--limit", type=int, default=100,
                                help="单批最大数量 (default: 100)")
    p_verify_batch.add_argument("--no-degrade", action="store_true",
                                help="不自动降级")
    p_verify_batch.add_argument("--threshold", type=float, default=0.3,
                                help="降级阈值 (default: 0.3)")
    p_verify_batch.add_argument("--all", dest="verify_all", action="store_true",
                                help="验证全部记忆（不限 pending）")

    sub.add_parser("verify-stats", help="验证管线统计：按状态分组计数")

    p_export = sub.add_parser("export", help="Export to JSON")
    p_export.add_argument("filepath")

    p_import = sub.add_parser("import", help="Import from JSON")
    p_import.add_argument("filepath")
    p_import.add_argument("--no-merge", action="store_true", help="Overwrite on key conflict")

    # Episodic timeline commands
    p_es = sub.add_parser("episodic-start", help="Start a new episode")
    p_es.add_argument("session_id")
    p_es.add_argument("--source", default="tui")
    p_es.add_argument("--title")

    p_ee = sub.add_parser("episodic-end", help="End an episode")
    p_ee.add_argument("session_id")
    p_ee.add_argument("--summary", default="")
    p_ee.add_argument("--tags", default="")
    p_ee.add_argument("--decisions", dest="key_decisions", default="")
    p_ee.add_argument("--outcome", default="")
    p_ee.add_argument("--pending", dest="pending_items", default="")

    p_el = sub.add_parser("episodic-list", help="List recent episodes")
    p_el.add_argument("--limit", type=int, default=10)

    p_eg = sub.add_parser("episodic-get", help="Get an episode by session_id")
    p_eg.add_argument("session_id")

    p_esearch = sub.add_parser("episodic-search", help="Search episodes")
    p_esearch.add_argument("query")
    p_esearch.add_argument("--limit", type=int, default=5)

    # ── 知识图谱 CLI ──
    p_kp = sub.add_parser("knowledge-promote", help="Promote a memory to knowledge node")
    p_kp.add_argument("memory_key")
    p_kp.add_argument("--title", required=True)
    p_kp.add_argument("--domain", default="")
    p_kp.add_argument("--tags", default="")
    p_kp.add_argument("--confidence", type=float, default=0.5)

    p_ks = sub.add_parser("knowledge-set", help="Create a knowledge node directly")
    p_ks.add_argument("--title", required=True)
    p_ks.add_argument("--content", required=True)
    p_ks.add_argument("--domain", default="")
    p_ks.add_argument("--tags", default="")
    p_ks.add_argument("--confidence", type=float, default=0.5)
    p_ks.add_argument("--source-keys", default="")
    p_ks.add_argument("--entity-names", default="")

    p_ksr = sub.add_parser("knowledge-search", help="Search knowledge layer")
    p_ksr.add_argument("query", nargs="?", default="")
    p_ksr.add_argument("--limit", type=int, default=10)
    p_ksr.add_argument("--deep", action="store_true",
                       help="Fall through to memory layer if knowledge confidence insufficient")

    p_kl = sub.add_parser("knowledge-list", help="List knowledge nodes")
    p_kl.add_argument("--domain", default="")
    p_kl.add_argument("--limit", type=int, default=50)

    p_kg = sub.add_parser("knowledge-get", help="Get a knowledge node by id")
    p_kg.add_argument("id", type=int)

    pk_link = sub.add_parser("knowledge-link", help="Add edge between knowledge nodes")
    pk_link.add_argument("source_id", type=int)
    pk_link.add_argument("target_id", type=int)
    pk_link.add_argument("--type", dest="rel_type", default="related")
    pk_link.add_argument("--weight", type=float, default=1.0)

    pk_graph = sub.add_parser("knowledge-graph", help="BFS traversal from a knowledge node")
    pk_graph.add_argument("id", type=int)
    pk_graph.add_argument("--hops", type=int, default=2)

    pk_del = sub.add_parser("knowledge-delete", help="Delete a knowledge node")
    pk_del.add_argument("id", type=int)

    sub.add_parser("knowledge-stats", help="Knowledge graph statistics")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    db = MemoryDB()

    if args.cmd == "set":
        rid = db.set(args.key, args.content, args.category, args.tags, args.scope, args.importance, args.ttl)
        print(f"id={rid}  key={args.key}")

    elif args.cmd == "get":
        row = db.get(args.key)
        if row:
            print(f"key:        {row['key']}")
            print(f"content:    {row['content']}")
            print(f"category:   {row['category']}")
            print(f"tags:       {row['tags']}")
            print(f"scope:      {row['scope']}")
            print(f"importance: {row['importance']}")
            print(f"ttl:        {row['ttl']}")
            print(f"created:    {row['created_at']}")
            print(f"updated:    {row['updated_at']}")
            print(f"accessed:   {row['accessed_at']}")
        else:
            print(f"NOT FOUND: {args.key}")
            sys.exit(1)

    elif args.cmd == "search":
        if args.multi:
            results = db.search_multi(args.query, args.limit, args.category, args.scope)
        elif args.semantic:
            results = db.search_semantic(args.query, args.limit, args.category, args.scope)
        else:
            results = db.search(args.query, args.category, args.tags, args.scope, args.limit)
        if results:
            for r in results:
                tags = f" [{r['tags']}]" if r['tags'] else ""
                imp = f" imp={r['importance']}" if r['importance'] != 0.5 else ""
                sim = f" sim={r['similarity']}" if r.get('similarity') else ""
                rrf = f" rrf={r['rrf_score']}" if r.get('rrf_score') else ""
                print(f"  [{r['key']}]{tags}{imp}{sim}{rrf}")
                print(f"    {r['content'][:120]}{'...' if len(r['content']) > 120 else ''}")
                print()
            print(f"--- {len(results)} result(s) ---")
        else:
            print("No results.")

    elif args.cmd == "delete":
        ok = db.delete(args.key, force=args.force)
        if ok.get("ok"):
            print(f"Deleted. (water={ok['water']})")
        else:
            print(f"ERROR: {ok.get('error', 'unknown')}")
            sys.exit(1)

    elif args.cmd == "stats":
        s = db.stats()
        print(f"Total:        {s['total']}")
        print(f"By category:  {s['by_category']}")
        print(f"By scope:     {s['by_scope']}")
        print(f"Avg importance: {s['avg_importance']}")
        print(f"Expired:      {s['expired_count']} (run 'cleanup' to remove)")

    elif args.cmd == "cleanup":
        n = db.cleanup()
        print(f"Cleaned up {n} expired entries.")

    elif args.cmd == "backfill":
        n = db.backfill_vectors(args.limit)
        if n == -1:
            print("Embedding server not available. Start it first (port 8082).")
        else:
            print(f"Computed embeddings for {n} entries.")

    elif args.cmd == "age":
        r = db.age()
        print("Decayed %d entries. Profiles: %s" % (r["decayed"], r["profiles"]))

    elif args.cmd == "deduplicate":
        r = db.deduplicate()
        print(f"Merged {r['merged']} pairs, removed {r['removed']} entries.")
        for keep, remove in r["pairs"]:
            print(f"  Kept: {keep}  |  Removed: {remove}")

    elif args.cmd == "compact":
        r = db.compact()
        print(f"Cleaned up: {r['deleted_low_imp']} low-importance, {r['deleted_expired']} expired.")

    elif args.cmd == "conflicts":
        r = db.detect_conflicts()
        if not r:
            print("No cross-agent conflicts detected ✅")
        else:
            print(f"Found {len(r)} conflicts:")
            for c in r:
                print(f"  key={c['key']} agents={c['agents']} versions={c['versions']}")
                for v in c["versions"]:
                    print(f"    [{v['agent_id']}] @ {v['updated_at']}: {v['content'][:60]}")

    elif args.cmd == "low-importance":
        n = db.low_importance_count(args.threshold)
        print(f"Entries with importance < {args.threshold}: {n}")

    elif args.cmd == "entity-stats":
        s = db.entity_stats()
        print(f"Total entities: {s['total_entities']}")
        print("Top entities:")
        for e in s["top_entities"]:
            print(f"  {e['entity']}: {e['cnt']} memories")

    elif args.cmd == "backfill-entities":
        n = db.backfill_entities()
        print(f"Backfilled entities for {n} entries.")

    elif args.cmd == "entity-graph":
        r = db.entity_graph(args.entity)
        if not r:
            print(f"No entities found matching '{args.entity}'")
        else:
            print(f"Entities matching '{args.entity}':")
            seen = set()
            for row in r:
                if row['entity'] not in seen:
                    print(f"  [{row['entity']}] cat={row['category']}")
                    seen.add(row['entity'])
                print(f"    → {row['memory_key']}: {row['content'][:80]}")

    elif args.cmd == "learn":
        r = db.learn(args.content, source=args.source)
        if "error" in r:
            print("Error: " + r["error"])
        else:
            print("Learned! key=%s  category=%s  importance=%.2f  tags=%s  verify=%s" %
                  (r["key"], r["category"], r["importance"], r["tags"],
                   r.get("verify", "pending")))

    # ── 验证管线 dispatch ──
    elif args.cmd == "verify":
        r = db.verify(args.key, auto_degrade=not args.no_degrade,
                      degrade_threshold=args.threshold)
        if not r.get("ok"):
            print(f"Error: {r.get('error', 'unknown')}")
        else:
            status_icon = {"verified": "✅", "degraded": "⚠️", "unverifiable": "❓", "conflict": "🔴"}
            icon = status_icon.get(r["verification"], "❔")
            print(f"{icon} {r['verification']}  key={args.key}")
            print(f"   验证分: {r['score']:.4f}  (keyword={r['keyword_score']:.4f}, entity={r['entity_score']:.4f})")
            print(f"   方法: {r.get('method', 'none')}")
            if r.get("downgraded"):
                print(f"   ⚠ 已降级: importance {r['new_importance']:.2f} (原 {r.get('new_importance', 0) + r.get('downgrade_amount', 0):.2f})")
            elif r.get("boosted"):
                print(f"   ↑ 高验证分提升: importance → {r['new_importance']:.2f}")
            if r.get("matched_ids"):
                print(f"   匹配消息: {r['matched_ids'][:5]}")

    elif args.cmd == "verify-batch":
        auto_deg = not args.no_degrade
        r = db.verify_batch(limit=args.limit, auto_degrade=auto_deg,
                            degrade_threshold=args.threshold,
                            only_pending=not args.verify_all)
        print(f"📊 批量验证完成:")
        print(f"   总数: {r['total']}")
        print(f"   ✅ 已验证: {r['verified']}")
        print(f"   ⚠️ 已降级: {r['degraded']}")
        print(f"   ❓ 不可验证: {r['unverifiable']}")
        print(f"   ❌ 错误: {r['errors']}")
        if r.get("avg_score", 0) > 0:
            print(f"   📈 平均验证分: {r['avg_score']:.4f}")

    elif args.cmd == "verify-stats":
        s = db.verify_stats()
        print(f"📊 验证管线统计 (总记忆: {s['total']})")
        print(f"")
        print(f"  验证状态分布:")
        for status, cnt in sorted(s["by_status"].items()):
            pct = round(cnt / max(s["total"], 1) * 100, 1)
            bar = "█" * int(pct / 2)
            print(f"    {status:16s} {cnt:5d} ({pct:5.1f}%) {bar}")
        print(f"")
        print(f"  置信度分布:")
        for band, cnt in s.get("confidence_distribution", {}).items():
            print(f"    {band}: {cnt}")
        print(f"")
        print(f"  平均验证分: {s.get('avg_verification_score', 0):.4f}")
        print(f"  未验证率: {s.get('unverified_pct', 0):.1f}%")
        print(f"  待验证率: {s.get('pending_pct', 0):.1f}%")

    elif args.cmd == "export":
        db.export(args.filepath)
        print(f"Exported to {args.filepath}")

    elif args.cmd == "import":
        n = db.import_json(args.filepath, merge=not args.no_merge)
        print(f"Imported {n} entries.")

    elif args.cmd == "episodic-start":
        key = db.episodic_start(args.session_id, args.source, args.title)
        print(f"Episode started: {key}")

    elif args.cmd == "episodic-end":
        ok = db.episodic_end(args.session_id, args.summary, args.tags,
                              args.key_decisions, args.outcome, args.pending_items)
        print("Episode ended." if ok else "Episode not found.")

    elif args.cmd == "episodic-list":
        eps = db.episodic_list(args.limit)
        if eps:
            for e in eps:
                dur = ""
                if e["started_at"] and e["ended_at"]:
                    dur = f" ({e['started_at'][:16]} → {e['ended_at'][:16]})"
                print(f"  [{e['key']}]{dur}")
                if e["title"]:
                    print(f"    {e['title']}")
                if e["summary"]:
                    print(f"    {e['summary'][:80]}")
                print()
        else:
            print("No episodes.")

    elif args.cmd == "episodic-get":
        ep = db.episodic_get(args.session_id)
        if ep:
            for k, v in ep.items():
                print(f"  {k}: {v}")
        else:
            print("Episode not found.")

    elif args.cmd == "episodic-search":
        eps = db.episodic_search(args.query, args.limit)
        if eps:
            for e in eps:
                print(f"  [{e['key']}] {e.get('title','')} ({e['started_at'][:16]})")
                if e["summary"]:
                    print(f"    {e['summary'][:100]}")
                print()
        else:
            print("No matching episodes.")

    # ── 知识图谱 dispatch ──

    elif args.cmd == "knowledge-promote":
        r = db.knowledge_promote(args.memory_key, args.title, args.domain, args.tags, args.confidence)
        if "error" in r:
            print(f"Error: {r['error']}")
        else:
            print(f"✅ Promoted → knowledge id={r['id']}  title={r['title']}  source={r['source']}")

    elif args.cmd == "knowledge-set":
        sk = [s.strip() for s in args.source_keys.split(",") if s.strip()] if args.source_keys else None
        en = [e.strip() for e in args.entity_names.split(",") if e.strip()] if args.entity_names else None
        kid = db.knowledge_set(args.title, args.content, args.domain, args.tags, args.confidence, sk, en)
        print(f"✅ Created knowledge id={kid}  title={args.title}")

    elif args.cmd == "knowledge-search":
        if args.deep:
            r = db.knowledge_search_deep(args.query, args.limit)
            if r["knowledge"]:
                print(f"🔵 知识层 ({len(r['knowledge'])}条):")
                for k in r["knowledge"]:
                    tags = f" [{k['tags']}]" if k['tags'] else ""
                    print(f"  [{k['id']}] {k['title']}{tags}  confidence={k['confidence']}  domain={k['domain']}")
                if r["memories"]:
                    print(f"\n📎 关联源记忆 ({len(r['memories'])}条):")
                    for m in r["memories"]:
                        print(f"  [{m['key']}] {m['content'][:80]}")
            elif r["fallback"]:
                print(f"⬇️ 知识层置信度不足，下钻记忆层 ({len(r['memories'])}条):")
                for m in r["memories"]:
                    key = m.get("key", "?")
                    cat = m.get("category", "")
                    c = m.get("content", "")
                    print(f"  [{key}] ({cat}) {c[:80]}")
            else:
                print("No results.")
        else:
            results = db.knowledge_search(args.query, args.limit)
            if results:
                for k in results:
                    tags = f" [{k['tags']}]" if k['tags'] else ""
                    print(f"  [{k['id']}] {k['title']}{tags}  confidence={k['confidence']}  domain={k['domain']}")
                    print(f"    {k['content'][:100]}")
                    print()
                print(f"--- {len(results)} result(s) ---")
            else:
                print("No results.")

    elif args.cmd == "knowledge-list":
        nodes = db.knowledge_list(args.domain, args.limit)
        if nodes:
            for n in nodes:
                src_count = len(json.loads(n["source_keys"])) if isinstance(n["source_keys"], str) else len(n["source_keys"] or [])
                print(f"  [{n['id']}] {n['title']}  domain={n['domain']}  conf={n['confidence']}  sources={src_count}")
            print(f"--- {len(nodes)} nodes ---")
        else:
            print("Empty.")

    elif args.cmd == "knowledge-get":
        n = db.knowledge_get(args.id)
        if n:
            for k, v in n.items():
                print(f"  {k}: {v}")
        else:
            print(f"Not found: id={args.id}")

    elif args.cmd == "knowledge-link":
        r = db.knowledge_link(args.source_id, args.target_id, args.rel_type, args.weight)
        if "ok" in r:
            print(f"✅ Linked {args.source_id} --[{args.rel_type}]--> {args.target_id}")
        else:
            print(f"Error: {r['error']}")

    elif args.cmd == "knowledge-graph":
        r = db.knowledge_graph(args.id, args.hops)
        if r["nodes"]:
            print(f"🔵 知识子图 ({len(r['nodes'])}节点, {len(r['edges'])}边):")
            for nid, ndata in r["nodes"].items():
                print(f"  [{nid}] {ndata['title']}  domain={ndata['domain']}  conf={ndata['confidence']}")
            for e in r["edges"]:
                src = r["nodes"][e["source"]]["title"] if e["source"] in r["nodes"] else f"#{e['source']}"
                tgt = r["nodes"][e["target"]]["title"] if e["target"] in r["nodes"] else f"#{e['target']}"
                print(f"    {src} --[{e['type']}]--> {tgt}")
        else:
            print(f"No graph: id={args.id} not found")

    elif args.cmd == "knowledge-delete":
        ok = db.knowledge_delete(args.id)
        print("Deleted." if ok else "Not found.")

    elif args.cmd == "knowledge-stats":
        s = db.knowledge_stats()
        print(f"知识节点: {s['nodes']}")
        print(f"知识关系: {s['edges']}")
        print(f"领域分布: {s['domains']}")


if __name__ == "__main__":
    _cli()
