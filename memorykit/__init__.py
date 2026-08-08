#!/usr/bin/env python3
"""memorykit — 给 AI agent 用的轻量本地记忆库。

可检索、可过期、可纠正的 SQLite 记忆存储：
  - MemoryDB        主类：建库/写入/检索/置信度/纠正（correction() 降置信+置 disputed）/
                    实体关联层（继承两个 mixin）
  - MemorySearch    mixin：FTS5 + 语义 + 实体多路检索（semantic-primary 加权融合），
                    排序会参考验证状态（verified 加权、disputed 重罚）
  - MemoryLifecycle mixin：age() 遗忘状态机（active→stale→archived→物理删）+ learn() 自动学习
  - MemoryGate      写入语义闸门三分类 + 纠正信号检测
  - detect_correction / detect_correction_layer1  纠正检测（规则层零网络）

依赖：核心 API 只需要 numpy（SQLite 为标准库自带）。嵌入服务默认
http://127.0.0.1:8082（OpenAI 兼容，可用 MEMORY_EMBED_URL 覆盖），缺失时
语义检索自动降级。不依赖任何 agent 框架。

快速开始:
  from memorykit import MemoryDB
  db = MemoryDB(db_path="my_memory.db")
  db.learn("显卡价格是550块")
  hits = db.search_multi("显卡")
"""
from .db import MemoryDB, SCHEMA_SQL, DB_PATH, EMBEDDING_URL
from .search import MemorySearch
from .lifecycle import MemoryLifecycle
from .gate import (
    MemoryGate,
    gate,
    detect_correction,
    detect_correction_layer1,
)
from .embedder import Embedder, HTTPEmbedder, embed_text

__version__ = "1.2.0"

__all__ = [
    "MemoryDB",
    "MemorySearch",
    "MemoryLifecycle",
    "MemoryGate",
    "detect_correction",
    "detect_correction_layer1",
    "gate",
    "Embedder",
    "HTTPEmbedder",
    "embed_text",
    "SCHEMA_SQL",
    "DB_PATH",
    "EMBEDDING_URL",
]
