#!/usr/bin/env python3
"""memory-kit 快速上手：建库 → 写入 → 检索 → 纠正 → 验证（10 行核心逻辑）。

无需嵌入服务即可运行（语义检索自动降级为 FTS5）。正式使用请换成自己的
db_path，例如 db = MemoryDB(db_path="memory.db")。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memorykit import MemoryDB, detect_correction

db = MemoryDB(db_path="/tmp/memory_kit_basic_demo.db")   # 1. 建库
r = db.learn("显卡价格是550块，显存12G", source="task")   # 2. 写入（自动分类/锚定/去重）
hits = db.search_multi("显卡", limit=3)                  # 3. 检索
print("命中:", [(h["key"], h["content"]) for h in hits])
sig = detect_correction("不对，应该是550块")              # 4. 纠正信号（规则层，零网络）
if sig:
    db.correction(r["key"], signal=sig["signal"])        #    标记 disputed + 降置信
print("纠正后状态:", db.get(r["key"])["verification_status"])
print("验证结果:", db.verify(r["key"])["verification"])  # 5. 验证（无源 session → unverifiable）
db.close()
os.remove("/tmp/memory_kit_basic_demo.db")
