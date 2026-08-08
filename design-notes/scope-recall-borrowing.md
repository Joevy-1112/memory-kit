# scope-recall 借鉴方案（borrowing plan）

> 2026-08-08 · 任务 t_ed9ca8f1 · 依据 README 2026-08-08 追加2 的补齐清单 6 项
> 原则：继续自研 memorykit，scope-recall 当参考加速补齐。每个借鉴点写清
> "它怎么做 → 我们缺什么 → 怎么最小侵入并进" + 为什么适合我们的架构。
> 源码行号指向 `/home/joe/.hermes/plugins/scope-recall/`（v1.7.1，MIT，只读解剖）。

---

## 0. 总览：借鉴点 → 优先级 → 对应文件

| # | 借鉴点 | 优先级 | scope-recall 参考实现 | 我们落点 |
|---|--------|--------|----------------------|----------|
| 1 | state → learn 派生链（journal 重建） | **P0（地基）** | journal_store.py / journal_candidates.py / journal.py | ~~db.py + 新 journal 表~~ → **2026-08-08 修正为指针方案：db.py memory 加 source_session_id/source_msg_id + lifecycle.reconstruct()（✅ 已实现 t_798e9d98，见文末修正节）** |
| 2 | validator 活验证（file_exists/command/http） | **P0（地基）** | freshness.py（状态机）+ fact_freshness 表 | **memorykit/validator.py（原型已交付）** |
| 3 | 渲染侧时间标注（防时态污染） | P1 | memory_ops.py `attach_freshness_metadata` → compact_context_lines | search.py 渲染 helper |
| 4 | local-hash 兜底（embedding fail-open） | P1 | embedders.py `LocalHashEmbedder` + vector_runtime.py | embedder.py 加 LocalHashEmbedder |
| 5 | 常驻提炼（journal digest 持续节奏） | P1（依赖 #1） | journal.py `run_journal_digest` + scripts/journal-digest.py | lifecycle.py `drain_journal()` + CLI |
| 6 | 遗忘治理（soft-archive/dry-run/审计） | P1 | forgetting.py | lifecycle.py `forgetting_report()` + 审计表 |

P2（可选，本文档只列方向不展开）：freshness 覆盖率报告（fact_freshness_report）、
governance_scheduler 全自动调度、RRF 与我们的加权融合的对照实验。

---

## 1. 【P0 地基】state → learn 派生链（journal 重建）——修工程漏洞 1

> **2026-08-08 已实现（t_798e9d98）**，但**按 Joe 修正设计走指针方案**：
> 不建 journal 表（见文末「2026-08-08 修正」节），state.db 承担原始消息存档，
> memory 表加 `source_session_id`/`source_msg_id` 指针 + `reconstruct()` 回溯。
> 本节的 journal 设计保留作参考对比，落地以修正节为准。

### scope-recall 怎么做

核心是 **journal=truth 事件溯源**：原始会话消息先进 append-only journal，提炼后的
memory 只算派生视图，任何一条 memory 都能回溯到它的源 journal 行。

数据结构（journal_store.py:105-177 `ensure_journal_schema`）：

- `journal_entries`：`id / scope_id / session_id / turn_number / role / content /
  content_hash / created_at / processed_run_id / processed_at`，唯一键
  `UNIQUE(scope_id, session_id, turn_number, role, content_hash)`——同一行消息
  重复 append 是 no-op（`INSERT OR IGNORE`，journal_store.py:225），幂等。
- `memory_journal_sources`：`(memory_id, journal_entry_id, run_id, created_at)`
  主键——memory ↔ journal 的**指针表**，这就是"可回溯重建"的物理实现。
- `journal_digest_runs`：每次 digest 的收据（status/extractor/计数/metadata）。
- `journal_rejections`：被拒候选的留痕（reason + candidate 快照），低价值行
  **也会推进 watermark**——durable outcome 是"拒收回执"，不是无限重试。

写入路径（journal_store.py:263-312 `append_journal_entry`）：每轮会话消息
（user/assistant/tool）清洗（去 inline data URL、拒 base64 大 blob）后按 ~2000
字符自然边界分块入库；`processed_run_id=''` 即未处理。

提炼路径（journal.py:669+ `run_journal_digest`）：
1. 取未处理 backlog（`processed_run_id=''`），按 scope 分组，动态上限
   （backlog 超阈值 4x → 上限提到 ceiling，journal.py:656-665）。
2. 分段：`_segment_session_entries`（journal_candidates.py:161-178）按
   topic signature 把同一 session 的连续轮次切成"一个话题一段"。
3. 提取：heuristic（规则组装，journal_candidates.py:231-273）或 LLM
   （llm 失败且允许 fallback → heuristic，journal.py:570-589）；LLM 异常 →
   quarantine 分类留痕（journal.py:764-792）。
4. 落库：`apply_journal_candidates`（journal.py:429-556）——对每个候选先
   `_find_match` 语义查重（dedup_key 精确 → 1.0；cos + 实体/标签加分，同
   session/topic 抬分，journal.py:272-328），≥0.88 视为已覆盖（skip+留痕），
   ≥0.55 且不冲突 → merge 更新并 `_merge_metadata`（entities/tags/importance
   max 合并，journal.py:374-394），否则 insert。**写成功后才
   `mark_entries_processed` 推进 watermark**（"store first, then attach
   provenance, then refresh vector companion"）。
5. 收尾：`journal_digest_runs` 写收据；`_prune_processed_journal` 按
   retention_days 只清已处理旧行（journal_store.py:369-388）。

### 我们缺什么

- memorykit 只有 `source_session` 一个字符串指针（db.py:79 列，learn() 参数），
  没有 journal 表、没有 watermark、没有 replay 路径。learn 抽坏了只能
  correction() 修正，不能从原文重放重建——**提炼损失不可逆**（工程漏洞 1）。
- learn() 是事件驱动（谁调谁写），没有"未处理积压"概念。

### 怎么并进（最小侵入）

> ⚠️ **已被 2026-08-08 修正设计取代**（见文末修正节）：下面的"加两张表 + 写时双写 +
> 重放通道"三步不落地；落地的是指针方案（加两列 + learn 记指针 + reconstruct 回溯）。

P0 三步，不动现有 learn() 语义：

1. **加两张表**（SCHEMA_SQL 追加，纯增量）：
   - `journal_entries`（照 scope-recall 结构，去掉 scope_id 多租户维度，加
     `source` 列标识 state.db 来源；content_hash 幂等去重）
   - `memory_journal_sources`（memory 行 ↔ journal 行指针表）
2. **写时双写**：`set()`/`learn()` 收 `source_session` 时，先
   `append_journal_entry()` 把源消息入库（失败不阻塞主写入，journal 是辅助
   真相层），再写 memory 并回填 `memory_journal_sources`。
3. **重放通道**：`replay_from_journal(key)` —— 按 memory_journal_sources
   找回源 journal 行，重跑 learn 的提取（分类/去重/锚定）生成"重建候选"，
   与现存 memory 对比供人工/校验管线裁决。correction() 后可用它重建正确版本。

**为什么适合我们**：memorykit 是库不是框架，我们不能依赖 Hermes state.db
schema（README 已标注 verify_recent 的遗留耦合问题）。journal 表把"原文存档"
收编成库内标准设施，state.db 只是其中一个写入源——反而**解耦**了框架依赖。
且与我们的护城河（correction 闭环）互补：journal 重建链给 correction 提供了
"重放正确版本"的能力，scope-recall 没有这条链，它的错信息只能一直错下去。

---

## 2. 【P0 地基】validator 活验证（file_exists / command / http）

### scope-recall 怎么做

freshness.py 是**状态机 + 排序惩罚 + 报表**三层，实际执行器不在插件里
（spec 存表，执行交给 agent 工具/运维），但契约完整可抄：

数据（sql_store.py:430-446 `fact_freshness` 表）：
`subject_type / subject_id / fact_key / truth_type / validator_kind /
validator_spec / ttl_days / last_checked_at / valid_until / status /
stale_reason / superseded_by`——一条 memory 可挂多条 fact，每条带自己的
验证器与有效期。

状态机（freshness.py:11-53）：
- 三族状态：CURRENT `{current,fresh,valid,verified,ok}` /
  NEEDS_CHECK `{needs_live_check,unknown,unchecked,expired}` /
  STALE `{stale,invalid,superseded,outdated}`，每族有 severity 分
  （0/1/2/3）。
- `normalize_freshness_status`：**valid_until 过期且当前是 current 族 →
  自动转 expired**（freshness.py:81-91）——时间边界是硬规则，不靠 LLM。
- `normalize_validator_kind`：别名宽松、未知非空值 **fail-closed 到 manual**
  （freshness.py:56-65，"不让过期操作事实被误当永恒偏好"）。
- `freshness_penalty`：stale 0.35 / expired 0.28 / needs_live_check 0.18
  可配置，检索时乘性降权（freshness.py:147-161）。
- `attach_freshness_metadata`：把 freshness 拼进检索 payload
  （fact_freshness_status / last_checked_at / valid_until / penalty）。
- `fact_freshness_report`：覆盖率报表（tracked/coverage_percent/by_status），
  驱动 governance_scheduler 的 `fact_freshness_live_check` 行动项。

### 我们缺什么

- memorykit 的验证是 `_lightweight_verify`（相似记忆 cos / 时间锚 / 实体密度）
  与 state.db 交叉验证——全是**被动推断**，没有**主动活验证**（去 stat 文件、
  跑命令、探 URL）。
- 没有 fact_freshness 侧表，没有 freshness 排序惩罚（VERIFY_WEIGHTS 覆盖了
  verified/disputed 但没有 freshness 维度），没有"这条事实该不该过期"的元数据。

### 怎么并进（最小侵入）

**原型已交付**：`memorykit/validator.py`（本任务第 3 节），三种执行器 +
`normalize_validator_kind` 别名归一化 + scope-recall 兼容 spec 形态
（`validator_kind` + `validator_spec`）+ `run_checks` 批量降级通道。
20 项测试全绿（`python run_tests.py validator`）。

落库三步（P0 后续）：
1. `set()`/`learn()` 加可选 `validator` 参数（`{"kind": "file_exists",
   "spec": {...}, "ttl_days": N}`），provenance 里记 validator_kind/spec。
2. 新增 `fact_freshness` 侧表 + `verify()` 扩展：valid_until 过期 →
   status=expired；有 validator spec → 调 `validator.validate()` 活验证，
   结果写回 last_checked_at/status。
3. `search_multi` 融合排序加 freshness 惩罚（照 freshness_penalty 语义，
   插在 VERIFY_WEIGHTS 之后乘性生效——我们已有这个乘性机制，零重构）。

**为什么适合我们**：validator.py 纯标准库（urllib/subprocess/os），零第三方
依赖，不破坏"核心只要 SQLite+numpy"；它是**可注入原语**，与 embedder 注入
同一模式。它补的正是我们验证闭环缺的"外部事实校验"半环：correction 闭环
管"人说的对不对"，validator 管"世界还认不认"——两者正交互补。

---

## 3. 【P1】渲染侧时间标注（防时态污染）——修工程漏洞 2

### scope-recall 怎么做

scope-recall 的渲染分两层：
- 检索结果进 prompt：`prompting.py render_current_turn_recall` 输出
  `- [target或source] summary` 行（紧凑、确定性、按预算截断）。
- profile 上下文：`memory_ops.py _profile_rows_for_target` 对每条 payload
  调 `attach_freshness_metadata`（freshness.py:164-180），把
  `fact_freshness_status / last_checked_at / valid_until / needs_live_check`
  塞进 payload，再经 `compact_context_lines` 渲染——**"这条记忆上次确认
  新鲜是什么时候"随行可见**，模型能判断信息新旧，不会被过期事实带偏。

关键点：时间边界信息在**渲染层**可见，而不只是检索层参与排序——这正是我们
工程漏洞 2 的对照（event_time 只做了检索侧）。

### 我们缺什么

- 检索结果 dict 里有 `updated_at` / `event_time`，但没有任何渲染 helper 把
  时间标注进上下文；旧记忆被召回后模型看不出新旧（时态污染）。

### 怎么并进（最小侵入）

加一个纯函数渲染 helper（放 search.py 或新 render.py，零依赖）：

```python
def render_memory_lines(items, *, max_chars=600, stamp=True):
    """items → prompt 行：'- [key] content 【YYYY-MM-DD 记录】'。
    stamp=False 关闭时间标注（调用方按场景选）。"""
```

- 标注取 `event_time or updated_at` 的日期部分；freshness 侧表落地后
  （第 2 节）追加 `(需复核)` / `(已过期)` 标记。
- `examples/basic.py` 与 README 的接入示例同步加一行用法。

**为什么适合我们**：渲染是纯函数，不碰库状态；我们的检索结果已带
event_time（v2 字段），只是没人消费——这是"零新数据、只加表现层"的最小补丁。
且与护城河一致：**标注让模型更可能触发 correction 闭环**（看出时间矛盾才会
质疑），而不是让错/旧信息悄悄通过。

---

## 4. 【P1】local-hash 兜底（embedding 挂了 fail-open）

### scope-recall 怎么做

`LocalHashEmbedder`（embedders.py:174-206），256 维，永远可用：
- 特征：token（权重 1.0）+ 字符 3-gram（权重 0.35）；空文本兜底 raw 前 64 字符。
- 哈希：`sha1(feature)` → 前 4 字节 % dims 定桶，`digest[4]` 奇偶定符号，
  累加后 L2 归一化——**确定性、无网络、无模型**，同一文本永远同一向量。
- 语义质量：弱（词袋+字符 n-gram 的 hash 桶），但**可检索**——比"直接没有
  语义通道"强一个档次，官方定位就是"degraded fallback，不是真语义模型"
  （README:349）。

配套工程（vector_runtime.py）：
- `setup_vector_layer`：embedder 主备（primary → fallback_embedder），
  都不可用 → `_vector_status="degraded"` 但**不抛、不阻塞 SQLite-only 运行**。
- 铁律（模块 docstring）：**"Vector failures should mark repair-needed state
  and never silently delete or rewrite SQLite truth"**——向量是 companion，
  丢了标 needs_repair，SQLite 真相行永不动。
- `sync_vector_index`：按 `updated_at` 增量 diff，只重嵌变化行，全量可重建。

### 我们缺什么

- HTTPEmbedder 失败 → `embed()` 返回 None → `search_semantic()` 返回 []
  → `search_multi()` 退化成纯 FTS5。**没有降级语义通道**：服务挂了，语义检索
  直接归零，learn() 去重也只剩实体兜底。
- 也没有"companion 状态机"（ready/degraded/needs_repair）——失败是静默的。

### 怎么并进（最小侵入）

1. `embedder.py` 加 `LocalHashEmbedder`（照抄特征/哈希算法，~40 行，纯
   stdlib hashlib/math）——它实现现有 `Embedder` 接口（embed/is_available），
   **注入式接入，零改动调用方**。
2. `MemoryDB.__init__`：embedder 未显式注入且 HTTPEmbedder 不可用时，
   自动换 LocalHashEmbedder（或加环境变量 `MEMORY_EMBED_FALLBACK=local-hash`
   开关）；`_vector_status` 记录 ready/degraded 供 stats() 可见。
3. 文档写明：**local-hash 是可用性兜底，不是真相源**（呼应"vector 是放大器
   不是真相源"——尤其我们没有 correction 闭环护 truth，兜底向量绝不能
   反向覆盖已 verified 的记忆）。

**为什么适合我们**：接口形状完全一致（embed/is_available），drop-in；
纯 stdlib 不破坏零依赖约束；learn() 去重与 search_semantic 共用 `_embed`
入口，一处注入全链路受益。与 scope-recall 的差异只在默认值：他们默认
local-hash，我们默认真 bge-m3、local-hash 只在服务不可用时接管——保持
我们的语义质量优势，同时堵上"挂了就归零"的可用性洞。

---

## 5. 【P1】常驻提炼（journal digest 持续节奏）

### scope-recall 怎么做

- 写侧常驻：每轮消息 append journal（见第 1 节），与提炼解耦。
- 读侧定时：`scripts/journal-digest.py`（`--interval-label 2h`）由
  governance_scheduler / cron 周期拉起 `run_journal_digest`，每次
  **有界批量**（max_entries_per_digest，backlog 大时动态提到 ceiling）：
  - 无积压 → `no_unprocessed_journal_result`（正常空跑，不算失败）；
  - 每 scope 独立处理，提取失败 → heuristic fallback 或 quarantine 留痕；
  - 收据写 `journal_digest_runs`（ok/error 都记，含 metadata 快照）；
  - `doctor_journal.py` 健康检查：连续 N 次 no-productive-writes / 连续
    error 会被报告为风险项——**"空转也要可观测"**。

### 我们缺什么

- learn() 只在 agent 决策时触发（压缩前、人工调用），没有持续节奏、没有
  积压水位、没有运行收据。会话原文躺在 state.db 里等"碰巧"被提炼。

### 怎么并进（最小侵入）

依赖第 1 节的 journal 表：
1. `lifecycle.py` 加 `drain_journal(limit=200, dry_run=False)`：取未处理
   journal → 复用 learn() 的提取路径（分类/去重/锚定/实体）→ 逐条 learn →
   成功后 mark processed；返回 `{processed, inserted, merged, skipped,
   backlog_before/after}` 收据。
2. CLI：`python -m memorykit.db drain-journal`，README 给 cron 示例
   （如 `0 * * * * cd repo && venv/bin/python -m memorykit.db drain-journal`）。
3. 无积压时返回明确的"空跑"结果（不报错）——照抄 no_unprocessed 语义。

**为什么适合我们**：库不绑框架，但"库 + CLI + cron"就是我们 README 已承诺的
接入模式（CLI 包装器一节）；drain_journal 复用 learn() 而非另起提炼管线，
保持单一提取逻辑（避免 scope-recall 里 heuristic/LLM 两套提取长期共存、
要养两套质量门槛的复杂度）。

---

## 6. 【P1】遗忘治理（soft-archive / dry-run / 审计）

### scope-recall 怎么做

forgetting.py 是"**先出报告，再执行，默认只软归档**"的治理闭环：

- `build_forgetting_report`（只读，forgetting.py:115-193）把行分类：
  - **soft-archive 候选**：assistant-prose-scratch / journal-template-
    transcript-noise / very-short-low-value（≤12 字符）/ 被拒候选 / 重复组
    非保留者；
  - **hard-delete 候选**：仅 secret-like-content / runtime-wrapper-noise
    （敏感或纯噪声才物理删）；
  - **review_debt**：candidate-review-debt / stale-review 待复核——**不删，
    挂起**。
  - 重复组：同 `(scope_id, target, dedup_key)` 按 created_at 保**最老稳定
    id**（多次运行收敛到同一结果），其余标 `superseded_by=keep_id`。
- `run_forgetting`（forgetting.py:237-411）：`dry_run=True` 默认。
  - soft archive：**不删行**，metadata 写 `lifecycle=archived /
    forget_reason / archived_at / archived_by / rollback_batch_id /
    superseded_by`，清 entities/relations；`governance_audit_events` 记
    **before/after 快照 + batch_id**（可回滚）；向量行同步删除。
  - hard delete：**fail-closed**——无 vector_store 直接拒绝
    （"hard delete refused: vector_store is required"），且**先删向量、
    后删 SQLite 真相**；向量删失败 → 回滚全部归档 + 删审计行（防
    split-state 泄漏）。
  - 审计事件带 `before`/`after` 快照 = 天然回滚材料。

### 我们缺什么

- memorykit `age()` 是**自动推进**的状态机（active→stale→archived→物理删，
  时间窗 + water 判定），没有 dry-run、没有审计表、没有 batch 回滚、没有
  "为什么删"的留痕；物理删是流程的一部分而不是显式治理动作。

### 怎么并进（最小侵入）

不动 age() 的状态转移逻辑（那是我们的衰减护城河，scope-recall 没有），
外面包一层治理壳：
1. `lifecycle.py` 加 `forgetting_report()`：只读扫描，复用现有字段
   （verification_status/importance/water/ttl/accessed_at）产出
   soft/hard/review_debt/duplicate_groups 分类（分类规则照 forgetting.py
   的，去掉 scope 维度）。
2. `forgetting(dry_run=True)`：dry_run 只出计划；apply 时 soft-archive
   走现有 `age()` 的 archived 路径但**记审计表**（before/after 快照 +
   batch_id），hard-delete 显式传参 + **向量未就绪时拒绝**（我们无
   companion 向量库，等价物是"vector_bin 列存在但不可重建时提示"）。
3. `compact()` 保持现有物理删入口，但文档标注"建议先 forgetting dry-run"。

**为什么适合我们**：age() 的衰减是时间驱动的、自动的（这是我们对比
scope-recall 的独特价值之一——它没有记忆衰减）；治理壳只加"可逆性 +
可审计性"，不改变衰减本身。审计表结构照抄 governance_audit_events 的
before/after 快照设计——那是通用好模式，与租户无关。

---

## 7. 不抄什么（它的缺陷，明确不学）

1. **无 correction 闭环**：journal 提炼错信息直接进 truth，没有用户纠正
   降权机制（governance.py 的 is_conflicting 是写前冲突检测，不是事后
   纠错回流）。**这是我们的护城河**——validation 再强也拦不住"当时是对的
   后来错了"的事实，只有 correction 闭环能修。不学。
2. **无记忆衰减**：它的 forgetting 是治理驱动（人工/调度审查分类），没有
   时间驱动的 active→stale→archived 衰减，也没有 TTL 生命周期。我们
   age() 的衰减 + 复活机制是差异化能力。不学它的"不衰减"。
3. **无实体合并**：它有 extract_entities，但 merge 时不保留实体并集
   （我们 08-07 修的 merge 实体合并是它没有的）。不学。
4. **LLM 依赖的启发式提炼质量**：它的 heuristic digest 产中文模板化摘要
   （"可复用记忆治理决策。主题：…。用户意图/约束：…"），低价值噪声全靠
   forgetting 事后清。我们的 learn() 直接存原话 + 分类/锚定，提炼损耗更小。
   只借鉴它"先入 journal 再提炼"的**流水线形状**，不借鉴它的摘要模板。
5. **多租户复杂度**：scope/scope_id/accessible_scope_ids 矩阵是插件
   面向 Hermes 网关多平台多用户的设计。memorykit 是单用户库，只取
   scope_id 概念中"共享库多 agent 区分来源"的最小版（我们已有 origin/agent_id）。
6. **RRF 排序**：我们用 semantic-primary 加权融合且已文档化，不回退。
   （对照实验可做，但不在补齐清单内。）

---

## 8. 原型交付（本任务第 3 节）

**`memorykit/validator.py`**（新增，~300 行，纯标准库）：

- `normalize_validator_kind(kind)` —— 别名归一化，未知值 fail-closed 到
  manual（照 scope-recall freshness.py:56-65 语义）。
- `validate(spec)` —— 分发器，兼容三种形态：
  - 裸字符串 = file_exists 路径（最常用快查）；
  - `{"kind": "http", "url": ...}`；
  - scope-recall 契约 `{"validator_kind": ..., "validator_spec": {...}}`。
- 三种执行器：
  - `file_exists`：os.path.exists，spec 缺 path → ok=False + 明确原因；
  - `command`：默认 shlex 拆分 + shell=False（防裸注入），spec 显式
    `shell=True` 才走 shell；超时/异常全部结构化返回，不抛；
  - `http`：urllib GET/HEAD，200-399 算 ok；HTTPError（4xx/5xx）算
    "可验证的失败"（服务在响应但事实不新鲜）；URLError/超时 → 连接失败。
- `manual` / `none`：不做自动检查（manual 需要人来，none 不追踪）。
- `run_checks(checks)`：批量顺序执行，全部失败不抛——验证管线的降级通道。

**`tests/test_validator.py`**（新增，20 项，`python run_tests.py validator` 全绿）：

- 别名归一化 / 未知值 fail-closed；
- file_exists：存在/缺失/裸字符串/缺 path；
- command：exit 0 / exit≠0 / 超时 / shell=True 管道 / 缺 command；
- http：本机起 ThreadingHTTPServer 测 200 / 404（HTTPError 分支）/
  连接拒绝（URLError 分支）/ 缺 url；
- manual/none 不自动检查；scope-recall 兼容形态；run_checks 批量不抛。

验证结果（项目 venv，python3.11）：

```
$ python run_tests.py validator
Ran 20 tests in 1.088s — OK
$ python run_tests.py            # 全量 161 项
Ran 161 tests — 1 failure（test_learn_batch 的 live 嵌入服务测试，
llama.cpp 8082 服务对同文本批量返回微差向量，cos<1 断言失败；
与本任务改动无关，服务端确定性问题的既有环境项）
```

---

## 9. 落地顺序建议

1. **P0 #1（journal 表 + 双写 + replay）**——修工程漏洞 1，也是 #5 的地基。
2. **P0 #2（fact_freshness 侧表 + verify 活验证接入）**——validator.py
   原型已就绪，接线即可；同时把 freshness 惩罚并入 search_multi。
3. **P1 #4（local-hash 兜底）**——40 行 drop-in，先堵可用性洞。
4. **P1 #3（渲染时间标注）**——纯函数 helper，一行接入示例。
5. **P1 #5（drain_journal 常驻提炼）**——依赖 #1。
6. **P1 #6（forgetting 治理壳）**——包在 age() 外面，不动状态机。

每步都是独立可合并的小改动，互不阻塞；#1/#2 完成后 README 的
"工程漏洞 1/2"即可标记修复。

---

## 借用纪律（2026-08-08 Joe 定，补充）

**"参考"不"照抄"——尤其命名**：
1. 借鉴的是**契约语义**（file_exists/command/http 验证种类、fail-closed、宽松别名入口），不是**命名/结构照搬**
2. 所有并入 memorykit 的代码用**我们自己的命名体系**（verify/vscore/校验器等已有语言），不用 scope-recall 的函数名/常量名/别名表
3. 借鉴点在代码注释里**标注契约来源**（"契约对齐 scope-recall freshness"），但实现是 ours
4. 原因：①保持 memorykit 设计语言统一 ②避免维护混淆（分不清自己的和抄的）③MIT 合法但要有自己的工程身份

### validator.py 现状修正（待办）
- [ ] 重命名 `VALIDATOR_KINDS`/`_VALIDATOR_KIND_ALIASES`/`normalize_validator_kind` 为 memorykit 自家命名
- [ ] 保留契约语义（三种验证+fail-closed），实现保持 ours

---

## 2026-08-08 修正：journal 项设计变更（Joe 拍板）

**原设计**（照 scope-recall）：建 journal_entries 表存原始消息 + memory_journal_sources 指针表
**修正**（Joe：原始消息不是有 state 了吗）：**不建 journal 表**——state.db 已是原始消息（Hermes 原生会话存档，永不删），journal 角色由它承担。

### 新设计（更省）
```
state.db（原始消息，已有）── learn 记指针 ──→ memory.db（提炼）
   ↑ source_session_id + source_msg_id 回溯重建
```
- memory 表加指针字段：`source_session_id` + `source_msg_id`（复用已有 source_session 可扩展）
- 保留策略：state.db 管（已有，永不删）
- 字段映射：learn 时从 source 消息提取（已有实体/时间提取逻辑）
- **比 scope-recall 省一份存储**（它 journal+memory 双存，我们原文一份+提炼一份+指针）

### 为什么这才是对的
- scope-recall 建 journal 是因为它没有原生会话存档（OpenClaw 插件环境）
- 我们有 Hermes state.db——**不该重复存原始消息**
- 体现"借鉴契约语义（可回溯重建），不照搬结构（不重复存原文）"

---

## ✅ 已实现（t_798e9d98，2026-08-08）

指针方案落地，零 journal 表、零 learn 语义改动：

- **schema**：`memories` 加 `source_session_id TEXT` + `source_msg_id TEXT`（SCHEMA_SQL +
  `_migrate_columns` 双路径，旧库自动 ALTER 补齐；旧行两列 NULL 不破坏）
- **learn()**：可选 `source_session_id`/`source_msg_id` 参数——插入路径透传 `set()` 写入；
  两处 merge 分支（向量去重 + 实体兜底）旧指针保留 + 新指针追加（`_merge_pointer`，
  逗号连接，语义对齐 source_session）。不传不炸，向后兼容
- **set()**：同支持可选指针（learn 插入路径底层；int msg id 归一化为 str）
- **learn_batch()**：items 每项可带 `source_session_id`/`source_msg_id`，逐条透传
- **reconstruct(memory_id)**（lifecycle.py）：沿指针回 state.db messages 定位原始消息，
  返回 `{found, source:{session_id, msg_id, role, content, timestamp}, sources:[...]}`；
  **多来源**（merge 逗号连接指针）做并集查询返回全部源，部分指针悬空仍回溯到存活源；
  失败分支明确：memory_not_found / **no_pointer（旧数据/无指针="无来源"）** / no_state_db /
  bad_pointer / msg_not_found，绝不抛。**只建可回溯，不承诺自动重建**（重放/蒸馏是后续管线的事）
- **非整数指针拒写**：learn()/set() 对非整数 source_msg_id 拒写 NULL + warning（防脏指针
  经 merge 污染指针列），reconstruct 的 bad_pointer 分支兜手工 SQL/历史脏数据
- **测试**：`tests/test_pointer_chain.py` 17 项全绿；全量 185 项 = 168 旧 + 17 新，
  仅 1 既有 live llama.cpp 服务确定性失败项（与本任务无关）
- **文档**：project-log.md 2026-08-08 节已追加

### 与 scope-recall journal 方案的差异（留档）
| 维度 | scope-recall | 我们（指针方案） |
|------|--------------|------------------|
| 原始消息存储 | journal_entries 表（双存） | state.db 一份（不重复存） |
| 指针 | memory_journal_sources 指针表 | memory 行两列（source_session_id/source_msg_id） |
| 提炼触发 | journal digest（watermark + 收据） | learn/learn_batch 事件驱动（原样保留） |
| 回溯 | 沿指针表查 journal 行 | `reconstruct()` 沿指针查 state.db |
| 后续 | drain_journal 常驻提炼（P1 #5）可复用本指针链 | 同左，落点改为按指针从 state.db 取未提炼消息 |
