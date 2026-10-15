# MemoryKit Overlay

给 OpenClaw（小龙虾 🦞）的记忆系统 Overlay 插件——把 memory-kit 的全部能力暴露为 `mk_*` 工具。

**设计核心**：`state.db` 原文永不删，memory 是上层影子。写入时自动分类/抽实体/打时间锚点，检索时多路融合，纠正/强化/验证构成反馈闭环。

**自动接线（hooks，v0.1.0 起）**：记忆系统不再需要手动召唤——`before_prompt_build` 自动把相关记忆注入上下文（等效 hermes 的 prefetch），`agent_end` 自动把高价值对话沉淀进记忆库（等效 hermes 的 sync_turn）。详见下方"自动接线"一节。

## 架构

```
OpenClaw agent
   │  mk_learn / mk_search / mk_get / mk_correct / mk_reinforce
   │  mk_verify / mk_overlay / mk_episodic / mk_knowledge / mk_stats
   ▼
src/index.ts  (TS 薄壳, defineToolPlugin, 10 个工具)
   │  调 bridge.py <cmd> <json-args> (execFile, 30s 超时)
   ▼
bridge.py    (Python 桥, 15 个命令, JSON 输出, 检索结果裁剪 vector 肥字段)
   │  sys.path → /home/joe/memory-kit
   ▼
memorykit    (MemoryDB: learn/search/correct/reinforce/verify/overlay/episodic/knowledge/stats)
   │
   ├── memorykit-overlay.db   (默认: ~/.openclaw/state/memorykit-overlay.db, 可 MEMORYKIT_DB 覆盖)
   └── state.db               (回溯原文: ~/.hermes/state.db, 可 MEMORYKIT_STATE_DB 覆盖)
```

- **TS 薄壳**：只做参数校验 + 转发，零业务逻辑
- **Python bridge**：单文件、零依赖（除 memory-kit），所有输出单行 JSON
- **检索裁剪**：命中只留 LLM 有用的字段（id/key/content/category/importance/confidence/verification_status/source_session...），vector 等肥字段不返回

## 工具清单

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `mk_learn` | 写入记忆（自动分类/实体/时间锚点/去重） | content, source(task/session/correction/discovery) |
| `mk_search` | 多路检索：FTS5+语义+实体加权融合 | query, limit, mode(multi/semantic/two_stage) |
| `mk_get` | 按 key 精确取一条 | key |
| `mk_correct` | 纠正记忆：confidence -0.3 + disputed（唯一外部锚点） | key, signal |
| `mk_reinforce` | 正向强化：explicit +0.15 / implicit +0.05，disputed→verified | key, mode |
| `mk_verify` | 验证单条：交叉检查来源 session → 评分，低分可自动降级 | key, auto_degrade |
| `mk_overlay` | 遮蔽管理：resolve 取密集片段 / expand 回溯 state 原文 | action, query/key |
| `mk_episodic` | 时间索引：会话 start/end/list/search | action, session_id |
| `mk_knowledge` | 知识层：promote/search/list/graph | action, memory_key |
| `mk_stats` | 记忆库统计 | — |

## 自动接线（hooks）

插件激活时注册两个 OpenClaw 钩子（manifest `activation.onCapabilities: ["hook"]`，`src/index.ts` 的 `registerHooks`），让记忆系统"自己出现"而不是被手动召唤：

### 1. `before_prompt_build` → 自动注入相关记忆（prefetch 等效）

- 每轮构建 prompt 前，用当前 prompt 调 bridge `search`（复用 `mk_search` 同一检索管线），取 top-4 候选
- 相关度门槛：`rrf_score >= 0.5` 且 top-1 与查询有词法重叠（`lexicalOverlap`，中文 bigram / 英文词）——无关查询（"今天天气怎么样"等）不注入，省 token
- 命中注入为 `prependContext`：`[相关记忆] 内容1 | 内容2 | 内容3`（最多 3 条，每条截断 140 字符）
- 节流：相同 prompt 30s 内不重复检索（`SEARCH_CACHE_TTL_MS`，缓存 8 条）；bridge 失败静默跳过，绝不让 hook 拖垮 agent（超时 3s）
- 自动 learn 沉淀新记忆后清空检索缓存，下一轮立即生效

### 2. `agent_end` → 自动 learn 沉淀（sync_turn 等效）

- 每轮对话结束，取最后一条 user 消息，质量门槛（`passesQualityGate`，参考 hermes-local sync_turn）：
  - 长度 ≥ 20 字符；非纯标点；非纯问候/寒暄（首词命中跳过，含"你好你好…"重复串）
  - 信息密度：中文字数 ≥ 4 **或** 信号词命中（配置/错误/修复/安装/测试/服务器/显卡… 硬件软件与个人偏好词表）
- 通过门槛才自动 `learn`（`source: "session"` + `source_session` 关联，便于回溯 state.db 原文）；内容上限 500 字符
- 幂等：同一内容 24h 窗口内不重复 learn（FNV-1a 哈希 + LRU 去重窗口 100 条）
- 失败轮（`success: false`）不沉淀；bridge 失败静默跳过（超时 8s）

### 配置

无配置项（`configSchema` 为空）。阈值常量在 `src/index.ts` 顶部，均经 `src/index.test.ts` 单元 + 端到端用例（真实 bridge + 临时库）验证。

## 构建 / 安装

```bash
# 依赖 OpenClaw >= 2026.5.17
npm install
npm run plugin:build
npm run plugin:validate
npm test                 # vitest 单元测试

# 安装进小龙虾
openclaw plugins install /home/joe/memorykit-overlay
openclaw plugins list    # 应显示 MemoryKit Overlay · enabled
```

## 测试

### 1. 功能测试 — func_test.py

learn→search→correct→reinforce→verify→overlay 全链路（临时库，29 断言）。

```bash
python3 func_test.py   # 期望: 29 通过 / 0 失败
```

### 2. 自建基准 — bench_test.py

6 查询 × 3 检索模式（multi/semantic/two_stage），引导 3 + 干扰 3，量化 recall@1 和 precision（误命中种子）。

```bash
python3 bench_test.py
```

### 3. LongMemEval 标准基准 — bench_longmemeval.py

**数据**：LongMemEval（Letta/MemGPT 团队，HF `xiaowu0162/longmemeval-cleaned`，277MB / 500条）。
已在 `benchdata/` 下。每条样本 = 1 问题 + 1 答案 + 53 个会话（只有 1-2 个藏着答案，其余全是干扰闲聊）。
问题分 6 类：single-session-user / multi-session / temporal-reasoning / knowledge-update / single-session-preference / single-session-assistant。

**跑法**：
```bash
# 基线（无时态处理）
python3 bench_longmemeval.py --limit 20 --mode multi

# v1 时态实验（时间戳拼 content，已证失败，仅复现用）
python3 bench_longmemeval.py --limit 20 --mode multi --temporal-anchor

# v2 时态（event_time 结构化字段 + ref_time 软重排）
python3 bench_longmemeval.py --limit 20 --mode multi --event-time

# 换检索模式 / 全量
python3 bench_longmemeval.py --limit 500 --mode semantic
```

**机制**（每条样本独立临时库，模拟"AI 只经历过这 53 个会话"）：
```
① 建独立临时库 /tmp/mktest_longmem_<qid>.db
② 把 53 个会话的对话逐条 learn 进库（真实管线：嵌入+去重）
③ 用 question 检索 top-5
④ 答案关键词（去停用词）在结果中 ≥2 个命中 = 命中
⑤ 按问题类型分组汇总 recall@1 / recall@5 / MRR
```
- 嵌入走 GPU：`MEMORY_EMBED_URL=http://127.0.0.1:8083`（bge-m3 on V100，与 Hermes 的 8082 隔离）
- 进程内直接 import memorykit（不经 bridge 子进程，否则每消息一次 python 启动，太慢）
- 耗时：20 条约 7 分钟（learn 是大头，~23ms/条消息）；500 条约 3 小时

### 当前结果 (2026-08-06)

- **功能测试: 29/29 通过**
  learn/search/correct/reinforce/overlay/episodic/knowledge/stats 全链路
- **自建基准: 引导查询 recall@1 = 100% (3/3) × 3 模式；干扰查询误命中种子内容 8 条/3 次查询 × 3 模式 (precision=27%)**
  multi / semantic / two_stage 引导查询全部首位命中，平均 ~245ms/查询。
  干扰查询无阈值候选是设计行为（容错）——命中种子内容计入 false-positive 统计并如实上报，相关度过滤由 LLM 层负责
- **LongMemEval (20条, seed=42, 8083 GPU 嵌入) — 三组对比**

  | 指标 | 基线 | v1(拼content) | v2(event_time) |
  |---|---|---|---|
  | TOTAL recall@1 | 60% | 55% | 60% |
  | TOTAL recall@5 | 80% | 70% | 80% |
  | MRR | 0.692 | 0.625 | 0.692 |
  | temporal-reasoning @1/@5 | 50%/50% | 25%/50% | 50%/50% |

  - multi-session 最强 (@1=80%, MRR=0.867)；single-session-user 弱 (@1=43%)
  - temporal-reasoning 50%——答案多为跨会话推算，检索只能捞相关会话，最终推理要下游做
  - **v1（时间戳拼 content）失败**：日期 token 污染向量/FTS，整体变差
  - **v2（event_time 结构化字段）与基线持平**：基础设施正确（不污染文本、兼容旧调用），
    但 20 条样本太小看不出统计差异；temporal 类提升需更大样本 + 更激进时间窗验证（未做）
- **端到端实锤**：小龙虾真调用 `mk_learn` 存下"服务器IP是192.168.5.50"，SQLite 落库，source_session 指向 `agent:main:mktest-001` 会话（回溯指针）

### 追加记录 (2026-08-07) — 小龙虾实测驱动迭代 + 8/10 毕业

08-06 之后的新变化（旧记录保留在上方，此为追加，非覆盖）：

- **自动接线（hooks）落地**：`before_prompt_build` 自动注入相关记忆（rrf≥0.5 + 词法重叠闸门，`[相关记忆]` 前缀，3 条×140 字符）+ `agent_end` 自动 learn 沉淀（质量门槛 + FNV-1a 幂等去重）。插件从"手动实验台"→"半自动系统"
- **jieba 用户词典**：`memorykit/data/jieba_userdict.txt`（43 行，人名/宠物/品种/地名/硬件/项目专名），中文专名（小鸡翅/银渐层/虹桥火车站/老王）整词命中
- **merge 实体合并**：merge 时不再丢实体——新抽实体 ∪ 旧实体合并写回（此前 merge 只拼 content 导致 entities 恒 null）
- **验证闭环打分**：`_lightweight_verify` 兜底（相似记忆 cos>0.85 / 时间锚 / 实体密度），vscore 非零 + method 记录（此前恒 0）
- **GCUF 回归**：8082/8083 换回 GGUF（bge-m3 Q4，5.5ms）；hybrid 三路暂缓（中文 sparse 不激活）
- **小龙虾评分：6 → 8/10**（"能用的工具，从实验台正式毕业"）——实体抽取 9、验证闭环实锤、自动 learn/注入/merge/检索/持久化/GGUF 全通
- **测试规模**：memorykit 141 测试全绿（+27 新增）；插件 vitest 19/19
- **关键坑（重要）**：gateway 的 TS 必须用 **venv python3**（`/home/joe/venv-flagembed/bin/python3`，有 jieba）——系统 python3 无 jieba 会导致实体抽取**静默降级**（全灭但不报错），这是"本地通、gateway 不通"的根因。已硬编码进 `src/index.ts`，venv 变更需同步
- **冲 9 分待办**：知识层 mk_knowledge 激活（当前 0 节点）、真实数据攒几周过规模考验、"我忘了它帮我想起"实战场景

## 演进方向：无限上下文架构（Joe 定，2026-08-07）

**一句话**：用相对密集的 memory 覆盖 state——上下文只装"当前时间段记忆（时间聚集）+ 语义召回的历史相关片段"，原文沉底 state.db 备查（指针可展开），实现类似无限上下文。

```
state.db（原文档案，永不删，高分辨率）
   ↑ 提炼/遮蔽（带指针）        ↑ 展开（回溯原文）
memory层（密集片段，低分辨率+指针）
   ├── 当前时间段记忆（按时间聚集）→ 维持当前对话
   └── 历史相关记忆（语义召回）→ 按需注入
   ▼
模型上下文（≈当前密集 memory + 召回片段）→ 窗口不随历史增长 = 无限上下文
```

**三大机制**：
1. **覆盖**：上下文只放密集 memory 片段（相对 state 的"主动且密集"压缩），state 原文沉底
2. **时间聚集**：当前对话用当前时间段 memory 维持（context-calendar 按日分块思路）
3. **语义召回**：历史相关靠语义检索拉入（memorykit search），带指针可展开

**蒸馏管线（核心难点，待设计）**：
- L1 结构事实：规则抽取（memorykit learn 已有：实体/时间锚定/质量门槛），零成本
- L2 语义沉淀：轻模型异步批处理（会话结束/空闲窗口），主力
- L3 高价值：重模型低频（架构决策/跨会话主题）
- 关键纪律：**异步批处理 + 质量闸门**（源头=原文+完整上下文 / 产物=vscore 验证 / 修正=correction 闭环），绝不边聊边蒸馏（ext_ 污染前科）

**与方向B实验的关系**：方向B（选择性压缩分辨率）的 v10-v13 验证了子机制（模糊化保留可答性/不向上虚构/来源转移）；本架构是完整形态——压缩改主动且密集，加指针展开，扩 memory 插件功能。

## 设计约定

- **correction 是唯一外部锚点**：只有用户显式说"不对"才调用 `mk_correct`，之后检索排序重罚该条
- **无阈值候选**：search 对无关查询也返回 top-k（容错），相关度过滤由 LLM 层负责
- **记忆是影子，原文是根**：`mk_overlay expand` 沿 source_session 指针回溯 state.db 原文，记忆可被验证/纠正/遗忘，原文不可变
- **检索结果裁剪**：省 token——LLM 只需要内容+置信+验证状态，不需要向量

## 已知问题 / 待办

- [x] ~~O(N²) 去重优化~~ → **实测澄清 (08-06)**：≤2000 条不成立（numpy 向量化吸收，单条恒定 26-28ms）；真实瓶颈是嵌入 HTTP 单条调用 → 已做批量嵌入优化
- [x] ~~批量嵌入~~ → **learn_batch 落地 (08-06)**：端到端 1.90x、嵌入阶段 4.77x、HTTP 调用 500→4 次、81 测试全绿（memorykit 核心，overlay bridge 尚未暴露）
- [x] ~~hybrid 嵌入~~ → **8082/8083 均换 bge-m3 原版三路 (08-07 上午)**：dense+sparse+colbert，sparse 接入检索（Signal 6 lexical，LEXICAL_WEIGHT=0.3），97 测试全绿
- [x] ~~hybrid 回退 (08-07 下午)~~ → **实测结论追加**：bge-m3 原版三路对中文 sparse 几乎不激活（0-1 token）+ 慢 6.5x（35.8ms vs GGUF 5.5ms）→ 用户拍板换回 GGUF，hybrid 暂缓。原版三路代码/venv 保留（venv-flagembed + bge3_hybrid_server.py），要启用随时可以
- [x] ~~服务器 Hermes 评审 / 开源评估~~ → **取消 (08-21 Joe 拍板)**：不开源评审流程，本地 README 即文档
- [x] ~~设计文档归档~~ → **本地 README 已含架构/工具/测试/当前状态**（08-21 补）
- [ ] sparse 对中文语料增益有限（实测几乎不激活）——hybrid 价值主要在英文；中文场景退化纯 dense+fulltext，不夸大
- [x] ~~overlay bridge/TS 暴露 learn_batch~~ → **bridge + MCP 落地 (10-15)**：`bridge.py learn_batch`（≤200 条/批、一次批量嵌入 + 逐条复用 learn 去重、结果与输入顺序对齐、单条失败不中断、agent_id 按调用方身份回写）+ MCP 工具 `learn_batch`（单次 timeout 600s）；func_test 33→39 全绿。OpenClaw TS 侧未做（插件 08-15 起 disabled）
- [x] ~~MCP `recall` 实际转发 `search`（bridge 已有 `episodic` 未接）~~ → **修补版修真 (10-15)**：带 `query` 走 `episodic action=search`、不带走 `action=list`，返回结构由 `{hits}` 改为 `{episodes}`。语义变更待 Joe 拍板确认
- [ ] 基准测试扩大题目集（当前 6 题 × 3 模式，种子 6 条）
- [ ] 大样本 ablation（50-100 条 × 多次重复取均值）——20 条样本结果不稳定（±15pp 漂移），"基线 vs v2 持平"不可判定

## 当前状态（2026-08-21 更新）

- **OpenClaw 插件已 disable（08-15）**：为避免与 Hermes 的 memory-kit 依赖混用，`src/index.ts` 插件从小龙虾停用；代码/测试/桥保留，随时可重新 enable
- **dsh MCP 记忆桥（08-17，10-15 扩工具；修补版 15 工具）**：`mcp_memory_server.py`（stdio MCP server，15 工具：search/learn/learn_batch/set/get/correct/reinforce/verify/verify_stats/recall/episodic/overlay/search_state/knowledge/stats；`cleanup`/`age` 为运维命令故意不暴露）+ `dsh-memory.cordis.yml`——指向 `~/.hermes/memory.db` 真库，供 DeepSeek Harness 直接读写 Hermes 记忆
- **memorykit 本体独立演进**：`~/memory-kit`（AGPL-3.0）已解耦，改动只影响 Hermes 侧

## 相关

- memory-kit: `/home/joe/memory-kit`（AGPL-3.0，github.com/Joevy-1112/memory-kit）
- 项目日志: `project-log.md`
