# memory-kit

给 AI 用的**轻量记忆系统** Python 库：可检索、可过期、无上限的
SQLite 记忆库。最初是个人项目里随手写的记忆模块，后来整理成独立库
（2026-08-02 整理），**不依赖任何框架**、无 `~/.hermes` 路径硬编码，可 pip 安装到任何项目。

- 唯一外部依赖：`numpy`
- 嵌入服务：默认 `http://127.0.0.1:8082`（OpenAI 兼容，llama-server bge-m3）
- 类名/公开 API 与原版一致（`MemoryDB` / `MemorySearch` / `MemoryLifecycle`），
  未来可平滑回迁

---

## 架构

```
                     ┌────────────────────────────────────────────┐
                     │               memorykit                    │
                     │                                            │
  learn()/set() ───► │  MemoryDB  (db.py)                         │
                     │  ├─ SCHEMA_SQL / _migrate_columns          │
                     │  ├─ _tx 事务管理（防悬挂写锁）              │
                     │  ├─ _adjust_confidence 统一置信度引擎       │
                     │  ├─ correction() / reinforce() 纠正闭环    │
                     │  ├─ verify_recent 交叉验证（需 state.db）   │
                     │  └─ knowledge_* 知识图谱                   │
                     │        │ 继承                               │
                     │        ▼                                   │
                     │  MemorySearch  (search.py)                 │
                     │  ├─ FTS5 + 语义 + 实体 三路 RRF 融合        │
                     │  ├─ VERIFY_WEIGHTS 验证回流加权             │
                     │  ├─ flush_touches 延迟写                    │
                     │  └─ search_state_*（需 state.db，可降级）   │
                     │        │ 继承                               │
                     │        ▼                                   │
                     │  MemoryLifecycle  (lifecycle.py)            │
                     │  ├─ age() 遗忘状态机                        │
                     │  │    active→stale→archived→物理删          │
                     │  ├─ DECAY_TAU 艾宾浩斯分档衰减              │
                     │  ├─ learn() 自动学习（分类/去重/锚定）      │
                     │  └─ verify()/deduplicate()/compact()        │
                     │                                            │
                     │  MemoryGate  (gate.py)                      │
                     │  ├─ gate() 三分类 MEMORY/SKILL/DROP         │
                     │  └─ detect_correction() 纠正信号检测        │
                     │                                            │
                     │  HTTPEmbedder (embedder.py)                 │
                     │  └─ OpenAI 兼容 /v1/embeddings，可注入      │
                     └────────────────────────────────────────────┘
                                     │
                    ┌────────────────┼─────────────────┐
                    ▼                ▼                 ▼
             memory.db         state.db(可选)    HTTP :8082 (bge-m3)
             (SQLite+FTS5)   会话消息库,缺失时   嵌入服务,离线时
                               相关功能降级      语义检索降级
```

**数据流**：`learn()/set()` → 分类+锚定+嵌入 → 写库（`_tx` 包裹，dirty 信号
走 logging）→ `search_multi()` 三路召回 + RRF 融合 + VERIFY_WEIGHTS 加权 →
用户反馈触发 `correction()`（置信 -0.3、置 disputed）或 `reinforce()`（+0.05/+0.15）
→ `age()` 按衰减档把久未访问的记忆从 active 逐步推到 archived。

---

## 安装

```bash
cd memory-kit

# 常规安装
pip install .

# 开发模式（改代码即时生效）
pip install -e .

# 只装运行时依赖（numpy 缺失时）
pip install numpy
```

需要 Python ≥ 3.10。可选：启动嵌入服务（见下节），否则语义检索自动降级为
FTS5/LIKE，`learn()` 仍可用（走实体去重路径）。

---

## 快速开始

```python
from memorykit import MemoryDB, MemoryGate, detect_correction

# 1. 初始化（临时库示例；正式用 db_path="memory.db"）
db = MemoryDB(db_path="/tmp/my_memory.db")

# 2. learn — 自动分类/锚定时间/语义去重/写入（一行完成）
r = db.learn("显卡价格是550块，显存12G", source="task")
print(r)   # {"key": "env/0710_显卡价格是550块", "category": "env", ...}

# 3. search — 三路检索（FTS5 + 语义 + 实体，RRF 融合）
hits = db.search_multi("显卡", limit=5)
for h in hits:
    print(h["key"], h["content"], "rrf=", h.get("rrf_score"))

# 4. correction — 用户说"不对"时降置信 + 置 disputed（环1）
from memorykit import detect_correction
sig = detect_correction("不对，应该是550块")       # Layer1 规则命中
if sig:
    db.correction(r["key"], session_id="s1", signal=sig["signal"])

# 5. reinforce — 用户确认正确时升置信（disputed → verified）
db.reinforce(r["key"], mode="explicit")

# 6. age — 遗忘状态机：active→stale→archived→物理删（按衰减档）
report = db.age()
print(report["decayed"], "条进入衰减", report["profiles"])

# 7. 写入前语义闸门（可选）：MEMORY / SKILL / DROP 三分类
mg = MemoryGate()
print(mg.gate("这条内容值不值得作为长期记忆……"))   # {"verdict": "MEMORY", ...}

db.close()
```

### 手工写入 / 检索 / 删除

```python
db.set("user/显卡预算", "显卡预算550块", category="user",
       importance=0.8, embed=True)          # embed=True 会调嵌入服务
row = db.get("user/显卡预算")                 # → dict 或 None
db.delete("user/显卡预算")                    # water 保护，water=0 需 force
db.stats()                                   # 总量/分类/过期统计
db.cleanup()                                 # 清过期项
```

---

## 嵌入服务部署（可选但推荐）

语义检索与 `learn()` 的向量去重依赖嵌入服务。默认地址
`http://127.0.0.1:8082`，OpenAI 兼容（`/v1/embeddings`）。用 llama.cpp 的
llama-server 跑 bge-m3 即可：

```bash
# 用 llama.cpp 提供 OpenAI 兼容嵌入端点（bge-m3 中文/多语言效果好）
llama-server \
  -m /path/to/bge-m3.gguf \
  --host 127.0.0.1 \
  --port 8082 \
  --embedding \
  -c 8192 \
  --pooling mean \
  -t 8
```

验证：

```bash
curl -s http://127.0.0.1:8082/v1/models | head
curl -s http://127.0.0.1:8082/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input": "显卡价格", "model": "default"}' | head -c 200
```

服务未启动时一切自动降级：`search_semantic` 返回 `[]`，`learn()` 走实体去重，
`backfill_vectors` 返回 -1。**不会抛异常。**

---

## API 参考

### `MemoryDB(db_path=None, state_db_path=None, dirty_log=None, embedder=None)`

主类（继承 `MemorySearch` + `MemoryLifecycle`）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `db_path` | 当前目录 `memory.db`（或 `MEMORY_DB_PATH`） | 记忆库 sqlite 路径 |
| `state_db_path` | `None`（或 `MEMORY_STATE_DB_PATH`） | 会话消息库；缺失时 `verify_recent`/`search_state_*`/`_cross_check` 自动降级 |
| `dirty_log` | `None` | dirty 信号文件路径；`None` 时走 Python logging（logger.debug） |
| `embedder` | `HTTPEmbedder(MEMORY_EMBED_URL)` | 自定义嵌入器（实现 `embed()`/`is_available()`） |

主要方法（继承关系见架构图）：

```python
# 写入
db.set(key, content, category="general", tags="", scope="permanent",
       importance=0.5, ttl=None, embed=True, agent_id="hermes",
       source_session=None, decay_profile=None, provenance=None,
       vector=None, water=2) -> int                    # 行 id
db.learn(content, source="task", source_session=None)  # 自动分类+锚定+去重+写入

# 检索
db.search(query, category=None, tags=None, scope=None, layer=None,
          limit=20, offset=0) -> list[dict]            # FTS5/LIKE
db.search_multi(query, limit=10, category=None, scope=None)  # 三路 RRF 融合
db.search_semantic(query, limit=10, category=None, scope=None)  # 纯语义
db.search_state_fast(query, limit=5, session_id="")    # 会话消息库检索（需 state.db）
db.flush_touches() -> int                              # 延迟写落库

# 置信度引擎（环1）
db._adjust_confidence(key, delta, min_conf=0.0, max_conf=0.95, _in_tx=False)
db.correction(key, session_id="", signal="", delta=-0.3) -> dict   # 降权+disputed
db.reinforce(key, session_id="", mode="implicit") -> dict          # +0.05/+0.15
db.verify_recent(limit=50) -> dict                    # 交叉验证（需 state.db）

# 生命周期（环2）
db.age() -> dict                          # 状态机：active→stale→archived→删
db.deduplicate(threshold=0.92) -> dict
db.compact() -> dict
db.verify(key, auto_degrade=True, degrade_threshold=0.3) -> dict
db.verify_batch(limit=100, auto_degrade=True) -> dict

# 其他
db.stats() / db.cleanup() / db.export(path) / db.import_json(path, merge=True)
db.episodic_*()     # 会话时间线
db.knowledge_*()    # 知识图谱层
db.close()
```

### `VERIFY_WEIGHTS`（环3 验证回流）

`MemorySearch.VERIFY_WEIGHTS`：检索融合得分乘以的系数，`verification_status`
越可信权重越高，disputed 重罚（可能错了，比 STALE 罚更重）：

| status | 权重 | 含义 |
|---|---|---|
| `verified` | **1.15** | 交叉验证通过 |
| `unverified` / `pending` / `unverifiable` | 1.00 | 基线 |
| `STALE` | 0.90 | 久未访问 |
| `degraded` | 0.85 | 验证降级 |
| `disputed` | **0.50** | 被纠正过，重罚 |
| `ARCHIVED` / `deleted` | 0.10 | 沉底 |

### `gate()` / `detect_correction()`

```python
gate(text) -> {"verdict": "MEMORY"|"SKILL"|"DROP", "reason": "..."}
detect_correction(text, layer2=False) -> {"signal", "layer", "matched", "tail"} | None
detect_correction_layer1(text) -> dict | None     # 纯规则，零网络
```

`gate()`：长度 <20 直接 DROP；纯数字/无中文/纯符号直接 DROP；其余走
DeepSeek flash 语义判断（24h 缓存）。**无 API key 时优雅降级**：按记忆放行
（`no-key-fallback`），不阻塞写入。`detect_correction` Layer1 纯规则命中
无需 key；Layer2 flash 兜底需 key，无 key 返回 `None`。

---

## 双 Agent 集成示例（Hermes 🐱 × OpenClaw 🦞）

memory-kit 被设计为**与框架无关**——任何 agent（Hermes、OpenClaw、或自研 agent）
都能 `pip install` 后直接用。以下是在真实双 agent 环境验证过的两种接入方式。

### 场景 A：Hermes（本机 agent）直接当库用

Hermes 的插件钩子（prefetch/sync_turn）可以调用 memory-kit 作为记忆后端：

```python
# Hermes 插件内（示意）
import memorykit
from memorykit import MemoryDB

db = MemoryDB(db_path="~/.hermes/memory.db")  # 或独立库路径

# prefetch 钩子：检索相关记忆注入上下文
def prefetch(query: str) -> list[dict]:
    return db.search_multi(query, limit=5, scope="permanent")

# sync_turn 钩子：对话后沉淀
def sync_turn(user: str, asst: str) -> None:
    db.learn(f"{user} → {asst}", source="hermes-session")

# 用户纠正时：置信度降权 + disputed（环1）
def on_user_message(text: str) -> None:
    corr = memorykit.detect_correction(text)
    if corr:
        for hit in db.search_multi(text, limit=1, scope="permanent"):
            db.correction(hit["key"], session_id="hermes", signal=corr["signal"])
```

### 场景 B：OpenClaw（远程/独立 agent）通过 CLI 或 HTTP 调用

OpenClaw 不 import Python 库，但可以**用 CLI 包装器或 HTTP 服务**暴露 memory-kit：

```bash
# 1. 写一个极简 CLI 包装器（存为 memctl）
#!/usr/bin/env python3
import sys, memorykit
from memorykit import MemoryDB
db = MemoryDB(db_path=sys.argv[2] if len(sys.argv) > 2 else "memory.db")
cmd = sys.argv[1]
if cmd == "learn":   db.learn(sys.argv[2])
elif cmd == "search": [print(r["key"], r["content"][:50]) for r in db.search_multi(sys.argv[2])]
elif cmd == "fix":   db.correction(sys.argv[2])
```

```bash
# OpenClaw 侧调用（exec 工具）
memctl learn "OpenClaw 记住：V100 显卡 550 块"
memctl search "显卡"
memctl fix "显卡"
```

### 实测记录（2026-08-02，OpenClaw 2026.7.1 + DeepSeek flash）

**环境**：独立 Linux 服务器，无 Hermes 框架，OpenClaw agent 通过 `exec` 工具调用。

| 步骤 | 结果 |
|:---|:---|
| `pip install memory-kit` | ✅ 1.0.0，numpy 自动依赖，未污染系统环境 |
| `learn("小龙虾试装 memory-kit 成功 V100 显卡")` | ✅ 自动分类 general、抽 tags、importance 0.6 |
| `search("显卡")` | ✅ FTS5+语义+实体三路 RRF 融合命中 |
| `correction(signal="你记错了")` | ✅ confidence 0.45→0.15（-0.3），状态→disputed，写 correction_log |
| 再次 search | ✅ disputed 记录按 VERIFY_WEIGHTS 自动降权（环3 生效）|
| 安全性 | ✅ 全程 /tmp 临时库，正式库零接触 |

**agent 侧评价（原话）**：
> "挺扎实的库——'会遗忘、会自我怀疑'这套设计（置信度引擎 + 纠正闭环 +
> 状态机）正是记忆系统该有的样子，三路检索融合在中文关键词上也识别得很顺。"

**结论**：库对第三方 agent 完全开放——任何框架 `pip install` 即用，三环
（置信度/遗忘/验证回流）在无 Hermes 依赖的环境下完整工作。

### 双 Agent 共享同一个库（推荐架构）

两个 agent 共用一份 memory.db，靠 `origin` 列区分写入来源：

```
┌────────────┐   ┌────────────┐
│  Hermes 🐱 │   │ OpenClaw 🦞 │
│  本地后端   │   │  独立环境   │
└─────┬──────┘   └─────┬──────┘
      │   pip install  │
      └───────┬────────┘
              ▼
      ┌──────────────┐
      │  memory.db   │   origin 列: hermes / openclaw / unknown
      │  (共享文件)   │   谁写谁标记，检索加权一视同仁
      └──────────────┘
```

- 各 agent 用自己的 `origin` 写记忆（`db.learn(..., agent_id="openclaw")`）
- `origin` 只写一次，merge 不覆盖——来源永远可追溯
- 三环（置信度/遗忘/验证回流）对双 agent 写入的记忆同等生效

---

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_DB_PATH` | 当前目录 `memory.db` | 记忆库路径 |
| `MEMORY_STATE_DB_PATH` | 未设置 | state.db（会话消息库）路径；未设置相关功能降级 |
| `MEMORY_EMBED_URL` | `http://127.0.0.1:8082` | 嵌入服务地址（OpenAI 兼容） |
| `MEMORY_GATE_API_KEY` | 未设置 | 语义闸门 API key（优先） |
| `DEEPSEEK_API_KEY` / `YAMII_GPT_KEY` | 未设置 | 兼容回退 key |
| `MEMORY_GATE_CACHE_DB` | 当前目录 `.memory_gate_cache.db` | 闸门缓存库路径 |
| `MEMORY_GATE_SKILL_CANDIDATES` | 缓存目录下 `skill_candidates.md` | SKILL 候选清单路径 |

---

## 测试

```bash
cd memory-kit

# 方式一：内置 unittest 运行器（无需 pytest）
~/.hermes/hermes-agent/venv/bin/python run_tests.py          # 全部 41 项
~/.hermes/hermes-agent/venv/bin/python run_tests.py smoke    # 冒烟
~/.hermes/hermes-agent/venv/bin/python run_tests.py rings    # 三环回归

# 方式二：pytest（需自行安装）
pip install pytest && python -m pytest tests/ -v
```

测试全部使用临时库（`tempfile`），**不触碰正式库**。覆盖：
- `test_ring_regression.py`：环3 验证加权、环1 纠正检测 + confidence engine 闭环、
  环2 状态机常量、读不写库修复（20 项，迁移自 Hermes 回归套件）
- `test_smoke.py`：建库/learn/search/correction/reinforce/age、无 state.db 降级、
  无 API key 降级、dirty_log 参数化、自定义 embedder 注入（21 项）

---

## 许可证

AGPL-3.0-only（SPDX）。官方全文：
<https://www.gnu.org/licenses/agpl-3.0.txt>

要点：可自由使用/修改/分发；通过网络提供服务时须向用户提供对应源代码
（Affero 条款）；衍生作品须以相同许可证发布。本库按"原样"提供，无担保。
