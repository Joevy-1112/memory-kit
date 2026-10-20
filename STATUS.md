# STATUS — 记忆系统 当前状态

> 这份文件只回答四个问题：**代码在哪、什么在跑、现在多准、哪些路试过了不行**。
> 设计意图看 `overlay/README.md`；历史过程看 `history/`（待建）。
> 最后更新：2026-10-20 14:05

---

## 1. 目录地图（谁是谁）

| 路径 | 是什么 | git | 版本 | 状态 |
|---|---|---|---|---|
| `/home/joe/memory-kit` | **memorykit 库**（Python，AGPL） | `ebe67fa` | 10-16 | ⚠️ **落后**：3 个未提交改动；被生产 overlay 依赖 |
| `/home/joe/memory-kit-v2` | memorykit 沙箱（今天 P0-P4 都在这） | `e07d116` | 10-20 | 领先生产 **943 行**（仅 lifecycle.py） |
| `/home/joe/memorykit-overlay` | **overlay 生产部署目录** | 非 git | 10-19 | ⚠️ **未修补版**：8 个 MCP 工具、错误吞成空结果、归属失败裸 except |

两个 memory-kit 是**同一个 git remote**（`Joevy-1112/memory-kit`），但不共享工作树。

**关键事实**：生产 overlay（`/home/joe/memorykit-overlay/bridge.py:17`）硬编码
`sys.path.insert(0, "/home/joe/memory-kit")` → **跑的是 P0 之前的老库**。今天修的合并丢数据 bug，生产没吃到。

---

## 2. 在跑的服务

| 进程 | PID | 端口 | 读哪个库 | 配置 |
|---|---|---|---|---|
| Hermes gateway | 2130 | — | `~/.hermes/memory.db` | `~/.hermes/config.yaml` |
| dsh web（飞书 bot） | 234710 | 8010 | `~/.hermes/memory.db` | `~/.dsh/cordis.patch.yml` |
| └ MCP 记忆桥 | 234741 | stdio | `~/.hermes/memory.db` | 同上（`memory-memorykit`） |
| llama-embedding (bge-m3) | 273253 | 8082 | — | `/etc/systemd/system/llama-embedding.service` |

- dsh 启动于 **08:27**，早于 `memory-context` 插件挂载 → **web profile 尚未加载该插件**
- headless profile 每次新起 → 已生效（实测见 §4）
- `memory-context` 插件：`/home/joe/.dsh/memory-context/`（149 行，9/9 单测）

---

## 3. 数据库

| 库 | 大小 | memories | merge_events 表 | 用途 |
|---|---|---|---|---|
| `~/.hermes/memory.db` | 118M | 2,141 | ❌ **无** | **生产**（Hermes + dsh 共用） |
| `var/memory-sandbox.db` | 253M | 2,100 | ✅ 有 | 沙箱（P0-P4 实验） |
| `~/.openclaw/state/memorykit-overlay.db` | 964K | 29 | ❌ 无 | overlay 默认库（旧，几乎没用） |
| 沙箱 `corpus_chunks` | — | 18,361 块 | — | 技能索引（248 SKILL + 1,222 references） |

**生产库没有 `merge_events` 表** → P0 的合并审计在生产不存在。

---

## 4. 现在多准（三个口径，任务不同别混着比）

| 任务 | 方案 | 指标 | 数字 | 日期 |
|---|---|---|---|---|
| 自建基准（6 题引导） | 多路融合检索 | recall@1 | **100%** | 08-06 |
| 同上（干扰查询） | — | precision | 27%（**设计行为**：无阈值候选，过滤交给 LLM） | 08-06 |
| LongMemEval（20 条抽样） | 基线 | recall@1 / @5 / MRR | **60% / 80% / 0.692** | 08-06 |
| 技能检索（29 query） | B 纯检索 top-3 | R@1 / R@3 | 44.8% / 65.5% | 10-20 |
| 技能检索（29 query） | B 端到端 top-10 | R@1 / R@3 | 65.5% / 75.9% | 10-20 |
| 技能检索（29 query） | **A 静态全量（现状）** | R@1 / R@3 / MRR | **79.3% / 93.1% / 0.851** | 10-20 |
| 记忆合并 | P0 修复后 | 310 字条目是否全文保留 | 通过 | 10-20 |

**读法**：LongMemEval 60-80% 是这类方法的物理上限（外部标准集）；技能检索 93% 是因为任务简单（248 选 1，且有 description）。

---

## 5. 已做过的实验及结论（含负结果 — 别重做）

| # | 实验 | 结论 | 日期 |
|---|---|---|---|
| 1 | LongMemEval v1：时间戳拼进 content | ❌ **变差**（recall@1 60→55），日期 token 污染向量/FTS | 08-06 |
| 2 | LongMemEval v2：event_time 结构化字段 | ＝ 与基线持平（样本太小看不出差异） | 08-06 |
| 3 | bge-m3 原版三路（dense+sparse+colbert） | ❌ 中文 sparse 几乎不激活（0-1 token）且慢 6.5× → 退回 GGUF | 08-07 |
| 4 | P1 灰带否决（198 个真实灰带候选） | ❌ `would_veto = 0`：117/198 先被数字冲突拦下，8 个「无关」全在其中 → **不开** | 10-20 |
| 5 | P2 实体加权（w_e=0.1） | ❌ **负作用**：w_e 归零后 MRR 0.479 → 0.531 | 10-20 |
| 6 | P2 chunk 级 vs skill 级聚合 | ✅ skill 级更好（R@10 66.7% → 83.3%），同 skill 多块会挤满 top-k | 10-20 |
| 7 | P4 B 方案（digest + top-k）vs A（静态全量） | ❌ **B 全面落后**：R@3 65.5% vs 93.1%；成本按 cache 计费只差 1.5× → **P3 不做** | 10-20 |
| 8 | P2 references 纳入索引 | ✅ R@1 37.9→44.8、R@10 69.0→82.8（但没解决根本问题） | 10-20 |
| 9 | 「耳机电池改 type-C」query | 覆盖空档（活跃语料无对应正文），不是召回错 | 10-20 |

---

## 6. 风险与待办（按严重度）

| 风险 | 后果 | 状态 |
|---|---|---|
| **生产 overlay 跑 10-19 老版** | 8 工具、bridge 报错显示成「没搜到」、agent_id 归属静默失败 | 未修（修补版在 `memory-kit-v2/overlay`，未部署） |
| **生产 memorykit 停在 ebe67fa** | 合并路径仍会 `[:200]` 截断 + 物理 DELETE | 未修 |
| **生产库无 merge_events** | 合并不可审计、不可回溯 | 未修 |
| 自动 learn 存用户原话（P2-J） | 库里 1,892 条 confidence<0.3、60% pending | 语义未拍板 |
| knowledge 层 0 节点 | 知识层功能闲置 | 未用起来 |
| 无限上下文三大机制 | 未实现（P4 已证明当前规模不需要） | 搁置 |

---

## 7. 下一步建议

1. **部署链修复**（最高优先）：overlay 换修补版 → memorykit 升到 e07d116 → 生产库迁移验证 → 改掉 bridge 的硬编码路径
2. **蒸馏质量闸门设计**：这是唯一能把 60-80% 往上推、同时让上下文不随历史增长的路
3. 检索侧不要再调了（P4 已证明不是瓶颈）
