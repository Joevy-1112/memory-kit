# MemoryKit v2 设计（合并安全 + Skill Corpus）

- 日期：2026-10-20
- 状态：草案 / **sandbox only**
- 基线：`memory-kit@ebe67fa`（2026-10-19）
- 沙箱仓库：`/home/joe/memory-kit-v2`
- 沙箱数据库：`/home/joe/memory-kit-v2/var/memory-sandbox.db`
- 关联：issue #6 / #7
- 文档规则：**只追加，不删除**；新内容写在本文件末尾的「变更记录」

> 本文件在拷贝出来的 sandbox 仓库里。  
> 生产仓库 `/home/joe/memory-kit`、生产库 `~/.hermes/memory.db`、运行中的 DSH / Hermes 均不改动。

---

## 0. 版本边界

### 本轮做

1. 合并安全：去掉 `deduplicate()` 的截断 + 物理删除，统一合并决策与写回。
2. 合并闸门：`anchor` 三态化，灰带先 shadow，不直接改变生产行为。
3. Skill Corpus：hybrid 检索 + 完整索引只出现一次 + 第一次技能调用后遮蔽。
4. 评测与消融：冻结测试集、明确指标、先 baseline 再改代码。

### 本轮不做

- 实体主表（entities）
- speaker / subject attribution（“我叫小哲”那条）
- 生产库迁移
- 在线语料自动抓取（只做 manifest + 手动/脚本快照）
- 直接替换静态技能索引

### 硬约束

```text
不改 /home/joe/memory-kit
不改 ~/.hermes/memory.db
不改运行中的 dsh / hermes profile
不 pip install 到生产 venv
所有实验只写 sandbox 库
所有文档变更只追加
```

---

## 1. 合并安全 v2

### 1.1 当前问题（已核实）

- `learn()` 里 `anchor` 先算 relation，但合并闸门只读 `fusion >= 0.85`，不读 relation。
- `无关` 是四合一标签：真无关 / 无候选 / LLM 失败 / 解析失败。
- 合并路径至少五条：快路径 `1665`、慢路径 `1677`、实体兜底 `1787+`、`deduplicate:229`、`db.set()` upsert。
- `deduplicate()` 把被合并方截断到 200 字后物理 `DELETE`。
- 合并写回不刷新 vector；`derived_from` 自指；`prev_v` 记成 `pending`；`decay_profile` 被清 `None`。
- `_fusion_score` 的实体 boost（最多 ×1.25）不打日志。

### 1.2 合并决策矩阵

`fusion = sim × entity_boost`；`sim` 为 cosine；`overlap` 为实体重叠数。

| 条件 | 决策 | 说明 |
|---|---|---|
| `fusion < 0.85` | KEEP | 不合并 |
| `0.85 <= fusion < 0.92` 且 `relation == 无关` 且 `anchored_key == candidate.key` | KEEP（shadow 阶段只记 `would_veto`） | 灰带唯一允许 anchor 干预的区间 |
| `0.85 <= fusion < 0.92` 且 `relation in (延续, 修正)` | MERGE | 锚定确认相关 |
| `0.85 <= fusion < 0.92` 且 `relation in (unknown, None)` | MERGE | 保持旧行为，标 `vector_only` |
| `fusion >= 0.92` | MERGE | **anchor 不否决**，只记 `anchor_disagrees` 供审计 |
| `source == entity_fallback` 且 `overlap >= 3` | MERGE | 保持旧行为，标 `entity_only` + `review` |
| `source == dedupe` 且 `sim >= 0.92` | MERGE | 不走 anchor；同阈值同写回 |
| `source == dedupe` 且 `sim < 0.92` | KEEP | 不合并 |

锚定三态：

```text
延续 / 修正 / 无关 / unknown
unknown = 无候选、LLM 失败、解析失败、未知标签
```

只有“显式 `无关` + `anchored_key` 正好是当前候选”才有资格 veto。  
`anchor=False` 时决策与旧版逐字节一致。

### 1.3 锚定惰性执行

当前 `anchor=True` 会在每次写入前无条件检索 + 最多 15s LLM，99% 的写入并不合并。

改为：

1. 先做向量候选。
2. 只有候选落在 `0.85–0.92` 灰带，且 `anchor=True`，才调用 `classify_relation()`。
3. 没有灰带候选不调 LLM。
4. anchor 结果只影响灰带，不改变 `>=0.92` 的向量优先。

### 1.4 唯一决策点

新增：

```python
_merge_decision(
    *,
    source: str,            # vector | vector_json | entity_fallback | dedupe
    sim: float,
    fusion: float,
    overlap: int,
    relation: str | None,
    anchored_key: str | None,
    candidate_key: str,
) -> MergeDecision
```

`MergeDecision` 至少包含：

```text
action: MERGE | KEEP
reason: gray_band_veto | anchor_continued | anchor_corrected |
        vector_only | high_similarity | entity_only | dedupe
would_veto: bool
```

所有合并路径都必须调用它，不允许在调用点自己写 `if fusion >= ...`。

### 1.5 唯一写回点

新增 `_merge_into_existing(...)`，合并现在两份复制粘贴的 UPDATE：

必须修：

- 合并后内容变化 → `vector` / `vector_bin` 标 `stale`，异步重嵌；不得继续用旧 vector 匹配。
- `derived_from` 指向**另一条**记忆，不能自指。
- `prev_v` 先保存旧 `verified/degraded`，再把 `v` 设为 `pending`。
- `decay_profile` 保留旧值，除非本次明确指定新类型。
- `entities/tags/source_session_id/source_msg_id/source_message_ids/raw_excerpt` 按并集合并。
- `event_time` 保留现有 7 天保护。
- 写 `merge_events` 审计行。

### 1.6 数据保留与归档

`deduplicate()` 不再截断、不再物理删除：

```text
keep 行：保留全文，刷新 vector，写 merged_at
remove 行：status='merged', merged_into=keep_key，全文保留
```

如果库体积压力大：

- 完整内容搬到 `memory_archive`，zstd 压缩
- `overlay expand` 仍可按 key 找回
- 检索默认排除 `status='merged'`

`merge_events` 最小字段：

```sql
id, keep_key, remove_key,
old_content, new_content, merged_content,
sim, overlap, boost,
relation, anchored_key,
decision, reason, would_veto,
created_at
```

### 1.7 写回 bug 清单

- [ ] vector / vector_bin 不刷新
- [ ] `derived_from` 自指
- [ ] `prev_v` 被记成 `pending`
- [ ] `decay_profile` 被清 `None`
- [ ] `_fusion_score` 的 sim / overlap / boost 不打日志
- [ ] `deduplicate()` 200 字截断
- [ ] `deduplicate()` 物理 DELETE
- [ ] 实体兜底路径与向量路径写回逻辑重复

### 1.8 测试

必须新增：

- 决策矩阵：`fusion=0.84/0.85/0.91/0.92` × `relation=延续/修正/无关/unknown`
- 灰带 shadow：`would_veto` 记录但不改变合并
- `fusion>=0.92` + `无关`：仍 MERGE，`anchor_disagrees=true`
- 数据保留：合并后 remove 行全文仍在，无 200 字截断
- 写回不变量：vector stale、derived_from 非自指、prev_v、decay_profile
- 实体兜底与向量路径决策一致
- dedupe 幂等：连续跑两次不再产生新合并
- dedupe `dry_run` 不写库
- 近似但不同的测试对：`显卡 550` vs `650`、否定句、单位差异、版本号

### 1.9 上线方式

```text
MERGE_SAFETY_V2=off | shadow | on
ANCHOR_GRAY_BAND=off | shadow | veto
```

默认：

- sandbox：`shadow`
- 生产：`off`
- 灰带先 shadow 100–200 个样本，人工复核 `would_veto`，再决定是否开 `veto`

---

## 2. Skill Corpus v2

### 2.1 目标

把 243 份 SKILL.md 的静态索引（约 21.5K 字符）从“每轮全量”改成：

```text
稳定 compact digest
+ 第一次技能调用前的 full index（只出现一次）
+ 每次 query 的 top-k chunks
```

### 2.2 三层上下文

| 层 | 内容 | 生命周期 | 是否可缓存 |
|---|---|---|---|
| L0 stable digest | 技能名 + 分类 + 一句话描述 | 整个 session | 是 |
| L1 full index | 完整索引表 | 会话开始到第一次 skill 调用 | 一次性 |
| L2 chunks | top-k 相关正文块 | 每次 query | 否 |

完整索引只出现一次。第一次真正调用技能后，L1 标 masked，不再恢复全文。

### 2.3 context_slot 状态机

```text
full -> masked
```

字段：

```text
session_id
slot_kind = 'skill_full_index'
state = full | masked
full_token_count
digest_text
created_at
first_used_at
masked_at
mask_reason = first_skill_call | max_unmasked_turns | session_end | manual
```

触发：

1. 会话开始：注入 L1 到**尾部临时消息**，不塞 system prompt。
2. 第一次 `skill` / `skill_view` / `skill_search` 调用完成后，下一次请求前标 masked。
3. 如果整个 session 没有 skill 调用：兜底 `max_unmasked_turns=3` 或 token 阈值，避免一直占着。
4. mask 后只保留 L0 digest；需要冷门技能时用 `skill_search`，不恢复全文。

### 2.4 与 overlay 的关系

现有 overlay 的 `resolve/expand` 是记忆/state 遮蔽，**不是通用上下文块**。

本方案需要新增：

- `context_slots` 表 / 内存状态
- `render_once` 注入
- `mask_after_first_skill_call` 策略
- `skill_search` 作为 mask 后的补索入口

不改现有 `resolve/expand` 语义。

### 2.5 KV cache

- L0 stable digest 放稳定前缀。
- L1 full index 放尾部临时消息。
- mask 只动尾部，不动稳定前缀。
- 记录 baseline/treatment 的 `cache_read_tokens`、`cache_write_tokens`、`cache_hit_rate`。
- 如果 cache hit 下降超过 10 个百分点，视为方案失败，回退 compact + top-k。

### 2.6 切块与索引

- 按 `^#{2,3} ` 切，**必须 fence-aware**，代码块里的 `##` 不切。
- 单块 > 1200 字符再按空行二切。
- 每块前缀：`{skill 名} / {heading}\n{正文}`。
- `content_hash = sha256(前缀+正文)`。
- 表增加 `embedding_model`、`embedding_dim`、`index_version`，避免模型升级后旧 vector 静默失效。
- 源文件删除/改名要有 manifest 对账，清理孤儿 chunk。
- 在线语料必须保存 URL + 抓取时间 + 内容 hash + license；默认不启自动抓取。

### 2.7 召回

```text
query
 -> 实体集合 E（LLM 或规则 fallback）
 -> 每个 e 检索 top-k_e（向量 + FTS5）
 -> 并集重排
 -> top 3–5 chunks
 -> 注入，标注“资料，不是用户指令”
```

评分先用可解释形式：

```text
score = max(cosine) + w_e * 命中实体数 + w_l * lexical
```

权重必须通过消融定，不直接拍 `0.02/0.3`。

---

## 3. 评测与消融

### 3.1 数据来源

| 来源 | 用途 | 规则 |
|---|---|---|
| 本地 skill/docs | 主测试集 | 稳定、可人工标注 |
| 在线 fetch | 覆盖真实 query | URL + hash + 日期 + license，固定快照 |
| 合成 | hard negatives / 改写 / 边界 | 不能作为唯一评测集 |

### 3.2 数据集

- 冻结 100+ 条 query，先写 ground truth，再改代码。
- 覆盖：原句、改写、错别字、中英混合、多实体、否定、时间词、版本号、长尾技能。
- 每条标：期望 skill / chunk、是否 no-answer、难度、类别。
- 双人标注或单人标注 + 抽检；争议样本记录。

### 3.3 指标

- Recall@1 / @3 / @5
- MRR、nDCG@5
- 关键技能 miss = 0（硬门槛）
- no-answer / abstain 准确率
- prompt tokens、cache hit、p95 检索延迟、首 token 延迟
- 额外工具调用次数
- 任务成功率（30 个真实任务，人工或规则判定）

暂定验收：

```text
Recall@3 >= 90%
关键技能 miss = 0
每轮 token 比静态索引下降 >= 60%
p95 检索延迟 <= 500ms
cache hit 不比 baseline 低超过 10 个百分点
任务成功率不下降
```

不达标 → 只做补充，不替换静态索引。

### 3.4 消融

| 变量 | 水平 |
|---|---|
| 索引粒度 | 技能摘要 vs 正文 chunk |
| 召回 | 纯向量 vs 纯 FTS vs hybrid |
| 实体 | 整句 vs 多实体拆开并集 |
| 实体清洗 | 开 / 关 |
| 上下文 | 静态全量 vs digest vs digest+top-k vs full-once+mask |
| mask 时机 | 首次技能调用 / 第一轮结束 / token 阈值 |
| top-k | 3 / 5 / 8 |
| 来源 | 本地 / 本地+在线 / 加合成 hard negatives |
| KV cache | 尾部注入 vs 前缀注入 |

每个变体重复跑，记录 bootstrap 95% 置信区间，不只报单点。

---

## 4. 隔离与操作

### 4.1 沙箱

```text
repo : /home/joe/memory-kit-v2
db   : /home/joe/memory-kit-v2/var/memory-sandbox.db
```

- 从生产库用 SQLite `.backup` 复制，不直接 `cp` 正在写的 WAL 库。
- 所有测试/脚本显式 `MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db`。
- 不在 sandbox 里指向 `~/.hermes/memory.db`。
- 每次实验前记录 sandbox DB 的 sha256；实验后核对生产库 mtime/sha256 未变。

### 4.2 生产隔离

- 不改 `/home/joe/memory-kit`。
- 不 pip install 到生产 venv。
- 不改 DSH / Hermes profile。
- feature flags 默认 `off`；只在 sandbox 打开。
- 需要给 Hermes 试用时，用独立 profile / 独立端口 / 独立 `DSH_HOME`，不碰当前 8010。

### 4.3 文档规则

- 新文档只新增，不改旧文档正文。
- 旧文档需要修正时，在文末追加「更正」段，不删除原文。
- 本文件后续变更只追加到 §7。

---

## 5. 实施顺序

### P0：数据安全

1. `deduplicate()` 去截断、去物理删、加 archive/soft delete。
2. `merge_events` 表 + 审计。
3. 合并写回 bug 修复（vector/derived_from/prev_v/decay_profile）。
4. 测试：数据保留、幂等、dry-run。

### P1：统一决策

1. `_merge_decision()`。
2. `_merge_into_existing()`。
3. 四条路径改调单点。
4. 三态化 + 惰性 anchor。
5. 灰带 shadow + `would_veto` 统计。

### P2：Corpus 索引

1. `corpus_chunks` / `corpus_entity_links` + `embedding_model` 元数据。
2. fence-aware 切块 + manifest + 增量更新。
3. 索引构建 CLI / 测试。
4. 先 local-only，线上源后置。

### P3：上下文

1. `context_slots` + overlay 渲染。
2. L0 digest + L1 full-once + L2 top-k。
3. 第一次 skill 调用后 mask。
4. `skill_search` 补索。
5. KV cache 观测。

### P4：评测

1. 冻结 100+ query。
2. baseline vs treatment。
3. 消融矩阵。
4. go/no-go：不达标只做补充。

---

## 6. 待定

- 灰带是否最终开 `veto`：先 shadow，数据说话。
- 无 skill 调用 session 的兜底 mask 阈值（默认 3 turns）。
- token 降幅、p95 延迟的最终验收线。
- 实体主表（entities）：本轮不做。
- speaker / subject attribution：本轮不做，单独 issue。

---

## 7. 变更记录（只追加）

### 2026-10-20 初版

- 基线：`ebe67fa`
- 建立 sandbox：`/home/joe/memory-kit-v2` + `var/memory-sandbox.db`
- 固化决策：
  - dedupe 不截断、不物理删
  - `fusion>=0.92` anchor 不否决
  - 灰带先 shadow
  - skill corpus 用 hybrid
  - full index 只在第一次 skill 调用前出现，调用后 mask
  - 评测 100+ query + 消融 + 相对 baseline 验收

---

## 8. 附录：设计对话打包（2026-10-20）

> 只追加。用于交接：把“为什么这么定”与“哪些还没定”从聊天记录里捞出来。
> 下面是压缩后的设计对话摘要，不是逐字 transcript；关键原话保留原样。

### 8.1 对话背景

- MemoryKit 新增了三份设计文件：
  - `design-notes/README.md`
  - `design-notes/handoff-2026-10-19.md`
  - `design-notes/skill-corpus-2026-10.md`
- 外部 review 已推翻 issue #7 的“灰带加一个 if”方案，并发现 `deduplicate()` 的 200 字截断 + 物理删除是 P0 数据丢失。
- 本次对话的目标不是立即写代码，而是把 v2 的范围、合并语义、Skill Corpus 形态、评测与隔离方式对齐。

### 8.2 Joe 的关键决策（原话）

```text
0 同意
1 ... 我拿不准
2 第一次调用后藏
3 无明确偏好，强于当前即可
4 hybrid，配合 overlay 让完整索引表全文出现唯一，每次用完自动遮蔽掉
5 语料从网上 fetch 或者沿用本地又或自己造都行，指标确定且鲁棒就行，记得做消融
6 待定
```

补充约束：

```text
文档内容只追加不删除
不影响在用版本
不污染库
拷贝再用
```

### 8.3 逐项对齐结果

| # | 议题 | 结论 | 理由 / 备注 |
|---|---|---|---|
| 0 | issue #7 正文过期 | 已加 SUPERSEDED banner | 不改正文历史；下一任先看最新评论 |
| 1/2 | 合并机制 + 数据保留 | 单点决策 + 单点写回；被合并行全文归档/soft delete；不截断、不物理删 | 误合并比重复更难恢复；数据丢失是 P0 |
| 3 | 灰带 anchor | `>=0.92` anchor 不否决；灰带先 **shadow**，只记 `would_veto` | Joe 拿不准；先样本复核，再决定是否开 `veto` |
| 4 | Skill Corpus 形态 | hybrid；L0 compact digest 常驻；L1 full index 只出现一次；**第一次 skill 调用后 mask**；L2 top-k chunks | 完整索引放尾部，保护 KV cache；mask 后靠 digest + `skill_search` |
| 5 | 语料与评测 | 本地 / 在线 / 合成都可用；100+ query 冻结集；指标确定且鲁棒；必须做消融 | 在线语料要 URL/时间/hash/license；合成只做 hard negatives |
| 6 | 实体主表 | 本轮不做 | 与 speaker/subject attribution 一起后置 |

### 8.4 被否掉或暂缓的方案

- **直接给 `>=0.92` 的 anchor 一票否决权**：否。高相似度以向量为准，anchor 只记录分歧。
- **直接全量替换静态技能索引**：暂缓。先 hybrid；不达标只做补充。
- **`deduplicate()` 保留 200 字截断 + 物理 DELETE**：否。P0 数据丢失。
- **在生产仓库 / 生产库上直接改**：否。全部在 sandbox 拷贝里做。
- **实体主表马上开工**：否。本轮只做合并安全 + Skill Corpus。
- **把 speaker/subject attribution 塞进 entities**：否。两个问题不同层，单独 issue。

### 8.5 关键数据（交接时别丢）

- 技能索引：21,486 字符，占 Hermes 系统提示 46.6%。
- 243 个 SKILL.md → 6,762 块，均 483 字符；全量嵌入约 20 分钟。
- 预实验 8 题：整句 embedding 6/14，多实体并集 9/14。
- 合并实测：真实 bge-m3 随机 100 对 max sim 0.662；hash 兜底 max 0.271；`fusion>=0.85` 均 0/100。
- 实体现状：`entity_links` 9,648 条 / 5,280 实体 / 2,086 条记忆；`Hermes` 与 `hermes` 并存。
- dedupe 旧实现 24.7 分钟，已由 `18b4246` 降到约 1s；但截断 + 删除仍在。

### 8.6 尚未对齐 / 需要数据决定

1. 灰带 `0.85–0.92` 最终是否开 `veto`：先 shadow 100–200 个样本。
2. 无 skill 调用 session 的兜底 mask：默认 `max_unmasked_turns=3`，可调。
3. token 降幅和 p95 延迟最终阈值：暂定 `-60%` / `500ms`，先跑 baseline。
4. 实体主表、speaker/subject attribution：本轮不做。

### 8.7 交接下一步

1. 先读本文件 §1–§5，再看 `SANDBOX.md`。
2. 在 `/home/joe/memory-kit-v2` 内按 P0 → P1 → P2 → P3 实施。
3. 不改 `/home/joe/memory-kit`、`~/.hermes/memory.db`、运行中的 DSH/Hermes。
4. 任何实验前显式设置：

```bash
export MEMORY_DB_PATH=/home/joe/memory-kit-v2/var/memory-sandbox.db
```

5. 每次只追加文档；旧结论要修正就在文末追加“更正”，不覆盖原文。

---

## 9. 变更记录（续，只追加）

### 2026-10-20 附录

- 追加 §8「设计对话打包」：背景、Joe 原话、逐项对齐、被否方案、关键数据、未决项、交接下一步。
- 本次只改 sandbox 文档，不改代码 / 生产库 / 运行中服务。
