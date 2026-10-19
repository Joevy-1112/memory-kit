# 第二种语料：skill / 文档库（设计草案）

- 日期：2026-10-19
- 状态：草案（待评审，未实现）
- 关联：`memorykit/db.py`（`_extract_entities`、`entity_links`）、`memorykit/search.py`（`RRF_K`、entity 检索）

## 一、为什么做

Hermes 侧的实测（2026-10-19，`state.db.system_prompts` 最新一条）：

| 项 | 数值 |
|---|---|
| 系统提示总长 | 46,128 字符 ≈ 26.6K tok |
| **其中技能索引（name + description 全量列出）** | **21,486 字符 = 46.6%** |
| MEMORY | 8,150 字符 = 17.7% |
| 基础规则 | 12,664 字符 = 27.5% |
| USER PROFILE | 3,330 字符 = 7.2% |

技能正文体量：243 个 `SKILL.md`，合计 3,188K 字符；单份最大 94K 字符（`h3-video-generation` 一份 ≈ 55K tok，载入一次就是系统提示的两倍）。

**结论：技能索引是主上下文里最大的一块静态开销，而它 90% 的时间是无用的**（一次会话真正用到的技能通常 ≤5 个）。

## 二、可复用的现成能力（memory-kit 已有）

| 能力 | 位置 | 现状 |
|---|---|---|
| 嵌入 | 服务器常驻 `llama_cpp bge-m3-Q4_K_M`（8082），**1024 维** | memories 已有 2,081/2,085 条 `vector_bin`（4,096 B/条） |
| 实体抽取 | `db.py::_extract_entities`（7 步打分：缩写/驼峰/带数字型号/小写技术词/版本号/中文词表/jieba） | 规则成熟，带停用词与时间戳过滤 |
| 实体关联 | `entity_links`（entity, memory_id, memory_key, weight） | 9,648 条 / 5,280 实体（碎片化，见第五节） |
| 融合检索 | `search.py::RRF_K`（fts5=40 / entity=50 / semantic=60 / tag=70 / recency=80），`LEXICAL_WEIGHT=0.3` | 加权融合（非标准 RRF，字段名历史遗留） |

也就是说：**嵌入 + 实体 + 融合这套管线已经跑在记忆上，缺的只是"第二种语料"的接入点。**

## 三、提议（不碰 memories 表）

新建平行表，不动核心表结构，避免迁移风险：

```sql
CREATE TABLE IF NOT EXISTS corpus_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type  TEXT NOT NULL,             -- 'skill' | 'doc' | 'issue' ...
    source_id    TEXT NOT NULL,             -- skill 名 / 文件相对路径
    chunk_no     INTEGER NOT NULL,
    heading      TEXT,                      -- 所属 ## / ### 标题
    content      TEXT NOT NULL,
    vector_bin   BLOB,                      -- 1024 × float32
    sparse       TEXT,                      -- bge-m3 sparse_weights JSON（可选）
    content_hash TEXT,                      -- 增量更新判定
    updated_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id, chunk_no)
);

CREATE TABLE IF NOT EXISTS corpus_entity_links (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    entity   TEXT NOT NULL,
    chunk_id INTEGER NOT NULL REFERENCES corpus_chunks(id) ON DELETE CASCADE,
    weight   REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_corpus_entity ON corpus_entity_links(entity);
CREATE INDEX IF NOT EXISTS idx_corpus_source ON corpus_chunks(source_type, source_id);
```

**为什么不直接泛化 `entity_links`**：那张表有 9,648 条在用、被检索路径依赖，改结构要迁移 + 回归；先平行、后合并更稳。等 corpus 这套验证完，再谈合并成 `(entity, target_type, target_id)`。

### 切块策略

- 按 `^#{2,3} ` 标题切，实测 243 个文件 → **6,762 块**，平均 483 字符
- 单块 > 1,200 字符再按空行二切
- 每块入库前拼上下文前缀：`{skill 名} / {heading}\n{正文}` —— 单块脱离标题会失去语义
- `content_hash = sha256(前缀+正文)`，增量只重嵌变化的块

### 实体清洗（复用 `_extract_entities` 后必须做的三步）

1. **归一化**：统一小写（现在 `Hermes` / `hermes` 是两个实体）
2. **黑名单**：`assistant` / `home` / `user` 一类正则误捞词
3. **频次门槛**：只保留全库出现 ≥2 次的实体（现在 5,280 个实体对 2,086 条记忆，长尾全是单例）

### 召回（本次预实验已验证的形态）

1. 从 query 抽实体集合 E（生产建议：LLM 抽一次，规则做兜底）
2. 每个 e ∈ E 各自检索 top-k_e（向量 + FTS5 混合）
3. 合并候选集，排序：`score = max(cosine) + 0.02 × 命中实体数 + 0.3 × lexical`
4. 取 top 3–5 块注入上下文（约 1–2K tok），替代 21.5K 静态索引

## 四、预实验（2026-10-19）

- 243 个技能建"技能级摘要"迷你索引：55.6 秒（含嵌入）
- 8 个真实问题，对比两种召回：

| 方式 | 命中（满分 14） |
|---|---|
| A：整句 embedding → top5 | 6 |
| B：多实体拆开 → 各自 top3 → 并集重排 | **9** |

- 典型对照：
  - 「耳机电池不行了 想改 type-c」：A 全错；B（实体=耳机/电池/type-c）命中 `windows-peripherals`
  - 「strata 解码速度慢」：A 认成网络问题；B（实体=strata）命中 `local-gguf-serving`
- 失败案例：「无穷小的阶 泰勒展开」两种全错 —— **该技能的 description 里没有正文主题词**，证明索引必须是正文 chunk 级，摘要级不够。

## 五、已知问题（现状数据）

- `entity_links` 9,648 条 / **5,280 个不同实体**，平均每实体连 1.8 条记忆 —— 碎片化严重
- 大小写未归一：`Hermes`(61) 与 `hermes`(60) 并存
- 误捞词：`assistant`(132)、`home`(42)、`user`(40)

## 六、不做的事

- **不替换技能正文的加载**：本文只解决"该载哪个"，正文是否进主上下文是另一个问题（执行型技能应交给子 agent）
- **不改 Hermes 的 skill 调用行为**：消费端在 Hermes，本库只提供检索 API
- **不动 `memories` 表结构**

## 七、开放问题

1. 实体抽取用 LLM（一次 flash 调用）还是纯规则？规则快但会把 `gen`/`web` 这类技能名词根当实体
2. 注入形态：给技能名列表，还是直接给命中段落？
3. 是否把 corpus 实体与 memory 实体合并成统一词表（归一化后）
4. 全量重建成本：6,762 块 × 5.6 块/s ≈ 20 分钟；是否需要多进程并发压到 5 分钟内

## 八、版本与迁移

- 目标版本：**1.3.0**（新增能力，非补丁）
- 旧数据：`corpus_*` 独立，`entity_links` 原样保留
- 回滚：删两张新表即可，不影响记忆检索
