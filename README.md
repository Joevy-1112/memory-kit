# memory-kit

> **状态：Prototype / Alpha** —— 核心功能可用，API 仍可能调整。适合个人 agent 和小型原型项目。

给 AI agent 用的**轻量本地记忆库**：SQLite 存储，可检索、可过期、可纠正。
不依赖任何 agent 框架，`pip install` 即可接入。

## 它能干什么

- **存**：写入一条记忆，自动分类、抽实体、打时间锚点（`learn()`）；也可手工指定 key 写入（`set()`）。
- **找**：关键词（FTS5/LIKE）+ 语义（嵌入向量）+ 实体 多路检索，按信号加权融合排序（`search_multi()`）。
- **纠**：当用户指出某条记忆错误时，可把它标记为 `disputed` 并降低置信度，之后检索排序会重罚它（`detect_correction()` + `correction()`）。
- **排**：检索排序参考每条记忆的验证状态——`verified` 加权上浮、`disputed` 重罚下沉；久未访问的记忆权重随时间衰减。
- **忘**：久未访问的记忆按档位从 active → stale → archived 逐步衰减，可物理删除（`age()`、`cleanup()`）。
- **筛**：写入前可过一道语义闸门，判断内容值不值得作为长期记忆（`gate()`，可选）。

## 安装

需要 **Python ≥ 3.10**。运行时依赖只有一个：`numpy`（SQLite 是标准库自带）。
尚未发布到 PyPI，从源码安装：

```bash
git clone https://github.com/Joevy-1112/memory-kit.git
cd memory-kit
pip install .        # 常规安装
pip install -e .     # 开发模式（改代码即时生效）
```

## 10 行跑起来

```python
from memorykit import MemoryDB, detect_correction

db = MemoryDB(db_path="memory.db")                    # 建库
r = db.learn("显卡价格是550块，显存12G", source="task")  # 写入（自动分类/锚定/去重）
hits = db.search_multi("显卡", limit=3)               # 检索
print([(h["key"], h["content"]) for h in hits])
sig = detect_correction("不对，应该是550块")            # 用户纠正
if sig:
    db.correction(r["key"], signal=sig["signal"])     # → disputed + 降置信
print(db.get(r["key"])["verification_status"])        # disputed
print(db.verify(r["key"])["verification"])            # unverifiable（无源 session）
db.close()
```

完整可运行版本见 [examples/basic.py](examples/basic.py)（`python examples/basic.py`）。
这段代码在**没有嵌入服务**时也能跑：语义检索自动降级为 FTS5。

## 可选功能

### 语义检索（推荐，需本地嵌入服务）

语义检索和 `learn()` 的向量去重依赖一个 OpenAI 兼容的嵌入服务，默认地址
`http://127.0.0.1:8082`（可用环境变量 `MEMORY_EMBED_URL` 覆盖）。用 llama.cpp
的 llama-server 跑 bge-m3 即可：

```bash
llama-server -m /path/to/bge-m3.gguf --host 127.0.0.1 --port 8082 \
  --embedding -c 8192 --pooling mean -t 8
```

验证：

```bash
curl -s http://127.0.0.1:8082/v1/models
```

**服务未启动时一切自动降级，不抛异常**：`search_semantic()` 返回 `[]`、
`search_multi()` 退化为 FTS5/LIKE、`learn()` 走实体去重路径、
`backfill_vectors()` 返回 -1。

### 写入前语义闸门（可选）

`MemoryGate().gate(text)` 判断一条内容值不值得作为长期记忆，返回
`{"verdict": "MEMORY"|"SKILL"|"DROP", ...}`。规则优先（纯数字/无中文等直接
DROP），需要语义判断时走 DeepSeek flash（24h 缓存）；**无 API key 时按记忆放行**，
不阻塞写入。

### 过期清理与生命周期

- `set(..., ttl=...)` 给记忆设有效期，`cleanup()` 清过期项。
- `age()` 把久未访问的记忆按档位衰减（active → stale → archived），
  可在 `compact()` 里物理清除。
- `export(path)` / `import_json(path)` 搬家备份。

### CLI

```bash
python -m memorykit.db set user/显卡预算 "显卡预算550块" --category user
python -m memorykit.db search 显卡 --multi
python -m memorykit.db stats
python -m memorykit.db learn "显卡价格是550块"
```

### 接入其他 agent

库本身与框架无关——任何 agent（Hermes、OpenClaw 或自研）`pip install` 后即可
直接 import 使用。远端/无法 import Python 的 agent 可以包一层极简 CLI 包装器：

```python
#!/usr/bin/env python3
import sys
from memorykit import MemoryDB
db = MemoryDB(db_path=sys.argv[2] if len(sys.argv) > 2 else "memory.db")
cmd = sys.argv[1]
if cmd == "learn":    db.learn(sys.argv[2])
elif cmd == "search": [print(r["key"], r["content"][:50]) for r in db.search_multi(sys.argv[2])]
elif cmd == "fix":    db.correction(sys.argv[2])
```

多个 agent 共用一份 `memory.db` 时，靠 `origin` 列区分写入来源
（`db.set(..., agent_id="openclaw")`），来源只写一次、merge 不覆盖。

## 边界 / 限制

- **规模**：SQLite 单机小库设计，实测约 5,000–10,000 条开始明显变慢
  （全表扫描 + 逐 key 点查）。适合个人/小项目记忆，不适合海量知识库。
- **语言**：`gate()` 的中文规则、纠正检测以中文为主，英文语料效果打折。
- **框架耦合（Experimental）**：核心 API 不依赖任何框架，默认只要 SQLite + numpy。
  但部分高级功能（`verify_recent`、`search_state_*`）仍带原项目遗留假设——它们
  按 Hermes 的 state.db 结构读取会话消息，接入其他框架需要适配层；没有
  state.db 时这些功能自动降级，不影响核心链路。
- **融合算法**：多路检索是 semantic-primary **加权融合**（各信号打分后按档位加权
  合并），不是标准 RRF；检索结果里的 `rrf_score` 字段是历史命名，保留以兼容旧调用方。

### 2026-08-07 迭代追加（jieba 实体 / merge 修复 / 验证闭环 / hybrid 回退）

- **jieba 用户词典**：`memorykit/data/jieba_userdict.txt`（随包分发，43 行）——
  `_extract_entities` 中文路径懒加载，中文专名（人名/宠物/地名/硬件型号）整词命中；
  英文/无中文场景完全跳过 jieba（首载 ~0.6s 不拖累）。格式坑：`词频在前词性在后`。
- **merge 实体合并**：merge 时新抽实体 ∪ 旧实体合并写回（此前只拼 content，
  entities 恒 null）——中文专名不再因去重合并丢失。
- **验证闭环兜底**：无 state.db / 无源 session 时 `_lightweight_verify` 给非零
  vscore（相似记忆 cos>0.85 / 时间锚 / 实体密度），不再恒 0。
- **hybrid 三路（实验，已回退）**：bge-m3 原版三路 dense+sparse+colbert 曾接入，
  实测中文 sparse 几乎不激活 + 慢 6.5x → 换回 GGUF 纯 dense。三路代码保留在
  `venv-flagembed` + `bge3_hybrid_server.py`，要启用随时可以。
- 详细记录见 `project-log.md`（2026-08-07 条目）。

## 还在修 / Roadmap

按影响排序的已知缺口：

- **验证管线解耦**：把 `verify_recent` / `verify_batch` 从 Hermes state.db
  schema 依赖中解放出来，做成可注入的"会话源"接口。
- **英文语料**：`gate()` 与纠正检测的中文规则扩展为多语言。
- **规模**：全表扫描改为索引/分片，把实用上限从万条级往上推。
- **检索透明化**：融合权重的配置入口 + 每个信号的得分明细输出。

## 测试 / 贡献

测试全部使用临时库（tempfile），不碰正式库。共 **50 项**：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_smoke.py` | 21 | 建库/learn/search/correction/reinforce/age、无 state.db 降级、无 API key 降级、dirty_log 参数化、自定义 embedder 注入 |
| `tests/test_ring_regression.py` | 25 | 验证状态加权、纠正检测 + 置信度、状态机常量、读不写库修复 |
| `tests/test_dimension_mixed.py` | 4 | 混合维度向量（1024d/2048d）下 learn() 与 search_multi() 不崩溃 |

跑法（无需 pytest）：

```bash
python run_tests.py            # 全部 50 项
python run_tests.py smoke      # 冒烟
python run_tests.py rings      # 回归
python run_tests.py dimension  # 混合维度
```

CI 用 GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.10/3.11/3.12 上跑
`run_tests.py`。贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 架构一览（细节）

<details>
<summary>点击展开：模块结构与数据流</summary>

```
                     ┌────────────────────────────────────────────┐
                     │               memorykit                    │
                     │                                            │
  learn()/set() ───► │  MemoryDB  (db.py)                         │
                     │  ├─ SCHEMA_SQL / _migrate_columns          │
                     │  ├─ _tx 事务管理（防悬挂写锁）              │
                     │  ├─ _adjust_confidence 置信度引擎           │
                     │  ├─ correction() / reinforce() 纠错降/升权 │
                     │  ├─ verify_recent 交叉验证（需 state.db）   │
                     │  └─ knowledge_* 实体/知识关联层             │
                     │        │ 继承                               │
                     │        ▼                                   │
                     │  MemorySearch  (search.py)                 │
                     │  ├─ FTS5 + 语义 + 实体 多路加权融合         │
                     │  ├─ VERIFY_WEIGHTS 验证状态加权             │
                     │  ├─ flush_touches 延迟写                    │
                     │  └─ search_state_*（需 state.db，可降级）   │
                     │        │ 继承                               │
                     │        ▼                                   │
                     │  MemoryLifecycle  (lifecycle.py)            │
                     │  ├─ age() 遗忘状态机                        │
                     │  │    active→stale→archived→物理删          │
                     │  ├─ DECAY_TAU 分档衰减                      │
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

**数据流**：`learn()/set()` → 分类+锚定+嵌入 → 写库（`_tx` 包裹，dirty 信号走
logging）→ `search_multi()` 多路召回 + 加权融合 + 验证状态加权 → 用户反馈触发
`correction()`（置信 -0.3、置 disputed）或 `reinforce()`（+0.05/+0.15）→ `age()`
按衰减档把久未访问的记忆从 active 逐步推到 archived。

</details>

## API 速览

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
db.search_multi(query, limit=10, category=None, scope=None)  # 多路加权融合
db.search_semantic(query, limit=10, category=None, scope=None)  # 纯语义
db.search_state_fast(query, limit=5, session_id="")    # 会话消息检索（Experimental，需 state.db）
db.flush_touches() -> int                              # 延迟写落库

# 纠错 / 置信度
db.correction(key, session_id="", signal="", delta=-0.3) -> dict   # 降权+disputed
db.reinforce(key, session_id="", mode="implicit") -> dict          # +0.05/+0.15
db.verify_recent(limit=50) -> dict                    # 交叉验证（Experimental，需 state.db）

# 生命周期
db.age() -> dict                          # 状态机：active→stale→archived→删
db.deduplicate(threshold=0.92) -> dict
db.compact() -> dict
db.verify(key, auto_degrade=True, degrade_threshold=0.3) -> dict
db.verify_batch(limit=100, auto_degrade=True) -> dict

# 其他
db.stats() / db.cleanup() / db.export(path) / db.import_json(path, merge=True)
db.episodic_*()     # 会话时间线
db.knowledge_*()    # 实体/知识关联层
db.close()
```

检索排序的验证状态加权（`MemorySearch.VERIFY_WEIGHTS`，乘性系数）：

| 状态 | 权重 | 含义 |
|---|---|---|
| `verified` | **1.15** | 交叉验证通过 |
| `unverified` / `pending` / `unverifiable` | 1.00 | 基线 |
| `STALE` | 0.90 | 久未访问 |
| `degraded` | 0.85 | 验证降级 |
| `disputed` | **0.50** | 被纠正过，重罚 |
| `ARCHIVED` / `deleted` | 0.10 | 沉底 |

完整签名以 docstring 为准（`help(memorykit.MemoryDB)`）。

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

## 更新日志

### v1.1.0（2026-08-04）

- **文档重写**（按 GPT-5.5 审查结论）：结构改为「能干什么 → 安装/10 行上手 →
  可选功能 → 边界/限制 → Roadmap → 测试/贡献」；巨型架构图从第一屏折叠到文末；
  概念降噪——不再用「知识图谱/纠正闭环/验证回流/艾宾浩斯/RRF」等大词，改为可观察
  行为描述；明确「核心 API 不依赖框架，默认只要 SQLite+numpy；verify_recent /
  search_state_* 带原项目遗留假设，标记 Experimental」；版本状态标注 Prototype/Alpha。
- **新增**：`examples/basic.py`（10 行可运行 demo）、`CONTRIBUTING.md`、
  GitHub Actions CI（`.github/workflows/ci.yml`，Python 3.10/3.11/3.12）。
- **清理遗留**：文档与代码 docstring 统一「加权融合」口径（不再称 RRF；`rrf_score`
  字段名保留以兼容旧调用方）；删除「无上限」表述；CLI 描述改为 memory-kit；
  pyproject urls 补真实仓库地址；补上缺失的 `py.typed`；`__version__` 同步 1.1.0。
- **GitHub 仓库**：更新 About 介绍。

### v1.0.1（2026-08-04）

- **修复**：`learn()` 去重路径在混合维度向量（如换过嵌入模型 1024d→2048d）时崩溃（`ValueError: inhomogeneous shape`）——批量余弦前按维度过滤，与 `search_semantic` 行为一致（发现者：OpenClaw 小龙虾审查）
- **修复**：`learn()` 慢路径 `r.get("vector")` 对 `sqlite3.Row` 无该方法报错——改为 `r["vector"]` 并加维度检查
- **新增**：`tests/test_dimension_mixed.py` 回归测试（模拟 1024d/2048d 混合库）

### v1.0.0（2026-08-02）

- 初始独立库：多路检索（FTS5/语义/实体）+ 置信度引擎 + 纠错降权 + 遗忘状态机 + 语义闸门
- 双 Agent 集成示例（Hermes × OpenClaw）+ 实测记录

## 许可证

AGPL-3.0-only（SPDX）。官方全文：
<https://www.gnu.org/licenses/agpl-3.0.txt>

要点：可自由使用/修改/分发；通过网络提供服务时须向用户提供对应源代码
（Affero 条款）；衍生作品须以相同许可证发布。本库按"原样"提供，无担保。
