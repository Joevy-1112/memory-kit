#!/usr/bin/env python3
"""memorykit — 从 Hermes 框架剥离的独立记忆系统库。

可检索、可过期、无上限的 SQLite 记忆系统：
  - MemoryDB        主类：建库/写入/检索/置信度引擎/纠正闭环/知识图谱（继承两个 mixin）
  - MemorySearch    mixin：FTS5 + 语义 + 实体三路检索 RRF 融合 + VERIFY_WEIGHTS 验证回流
  - MemoryLifecycle mixin：age() 遗忘状态机 + 艾宾浩斯衰减 + learn() 自动学习
  - MemoryGate      写入语义闸门三分类 + 纠正信号检测
  - detect_correction / detect_correction_layer1  环1 纠正检测

依赖：numpy（外部唯一依赖）。嵌入服务默认 http://127.0.0.1:8082（OpenAI 兼容，
可用 MEMORY_EMBED_URL 覆盖）。不依赖 Hermes 框架、无 ~/.hermes 路径硬编码。

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

__version__ = "1.0.0"

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
