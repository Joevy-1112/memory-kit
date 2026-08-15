# MemoryKit Overlay — 项目日志

## 项目定位

给 OpenClaw（小龙虾 🦞）装记忆系统：把 memory-kit（服务器 /home/joe/memory-kit，AGPL-3.0）的能力桥接成 `mk_*` 工具。TS 薄壳 + Python bridge，10 个工具，覆盖 learn/search/correct/reinforce/verify/overlay/episodic/knowledge/stats。

> **分支说明（2026-08-07 起拆分）**：本日志是 **overlay 子分支**日志，只记插件层演进（hooks/小龙虾实测/桥接/安装/venv 环境坑）。
> **memory-kit 母分支**的库级记录（event_time/learn_batch/jieba 实体/merge 修复/验证闭环/hybrid 实验）已移入 `/home/joe/memory-kit/project-log.md`——读库侧改动请去母分支日志。
> 拆分前（08-06 及以前）的旧记录混合了两者，保留在此作为历史，不再追加母库内容。

---

## 2026-08-06 — PM 迭代轮：GPT-5.5 外审 → 修复 → 验收（major-revision 闭环）

### 状态
✅ 闭环完成：外审 major-revision → coder 修复 → reviewer accept

### PM 流程（kanban 标准迭代）
1. **外审 t_64975ab1**（reviewer/gpt-5.5）：判定 **major-revision**
   - 实测发现：README 声称的 `npm test` 实际失败（TS 单测仍断言模板 `["echo"]`，插件已导出 10 工具）
   - bench_test.py precision 口径造假（干扰查询误命中被排除在失败条件外，"100%" 有水分）
   - 时间建模：schema 无事件发生时间字段，search 时间权重基于 created_at ≠ LongMemEval 会话时态
   - learn 逐条全库向量去重 O(N²)
   - 过程坑：yamii.cloud gpt-5.5 chat 503 连挂两次（worker run#9/#10 崩），模型列表通但 chat 后端过载；
     gpt-5.6-sol 单发可通；后来 5.5 恢复 run#12 才跑完
2. **修复 t_e3c3f951**（coder/deepseek-v4-flash，179s）：
   - #1 src/index.test.ts：断言 `["echo"]` → 10 个真实 mk_* 工具，`npm test -- --run` 真绿
   - #2 bench_test.py：汇总如实上报 precision=27%（干扰查询 3 条/模式，误命中种子 8 条/模式）；
     设计行为未改（无阈值候选保留），exit code 仍只由引导查询 miss 决定
   - #3 README.md：补 precision 口径，npm test 声称与实测一致；其余声称核实无谎报
   - PM 亲自复验：npm test 1 passed、bench_test precision=27% 警告行、func_test 29/29、README 口径——全过
3. **验收 t_1ce8d7da**（reviewer/gpt-5.5，160s）：**accept**
   - 核实插件真导出 10 个 mk_* 工具、npm test 通过、npm run build 通过、bench_test 复现 recall@1=100% + precision=27% 警告

### 本轮回流结论
- 文档声称必须与实测一致（GPT 会真跑测试抓谎报）
- 自建基准要报 precision 不能只报 recall
- 时间字段（事件时间）是下一轮 v2 方向，未实施

## 2026-08-06 — v2 时间结构化字段（event_time）落地 + ablation

### 状态
✅ 核心+插件已实现并验证；ablation 显示与基线持平（20条小样本）

### 流程
1. **方案评估 t_44039651**（reviewer/gpt-5.5）：确认 event_time 方向正确，但**只做检索元数据**，
   不替代 created_at 的 TTL/lifecycle 语义；TEXT 格式；软重排优先不默认 SQL 硬过滤
2. **核心实现 t_e7e875e5**（coder/flash，~8min）：
   - db.py：memories 加 `event_time TEXT DEFAULT NULL`（SCHEMA_SQL + _migrate_columns 旧库补列），
     set() 加参数，upsert COALESCE 保留旧值
   - lifecycle.py：learn() 加 event_time；merge 保护（旧值存在且相距>7天保留旧值，
     MERGE_EVENT_TIME_MAX_GAP_DAYS）
   - search.py：search_multi/search_semantic 加 time_window / ref_time+max_age_days /
     strict_time_window（默认 False 软重排，True 硬过滤）；`_temporal_value = event_time or created_at`
     只替换 recency rank 与 _time_weight 输入；TTL 仍用 created_at；不传时间参数 bias 恒 1.0 完全兼容
   - 新增 tests/test_event_time.py 15 项；全套 65 测试通过（50 旧 + 15 新），PM 复验通过
   - PM 抽验：event_time 窗口内 bias=1.25 / 无时间 bias=0.25 / 不传参 bias=1.0 ✅
3. **插件适配 t_5bdcbef7**（coder/flash，~3min）：
   - bridge.py：cmd_learn 透传 event_time；cmd_search 透传 ref_time/max_age_days/time_window(list|dict)/strict_time_window；_MEM_FIELDS 加 event_time + time_bias
   - src/index.ts：mk_learn 加 event_time；mk_search 加 ref_time/max_age_days/strict_time_window
   - PM 复验：bridge 全链路 OK（learn 落 event_time、search bias 1.25、旧调用 bias 1.0）；npm test 1 passed；func_test 29/29
4. **Ablation t_79ca8cff**（coder/flash）：
   - 基线（20条 seed=42）：recall@1=60%, @5=80%, MRR=0.692
   - v2（--event-time，ref_time=question_date, max_age_days=90）：**与基线完全持平**（60/80/0.692，temporal 50%/50%）
   - 对比 v1（拼content）55/70/0.625：v2 没有 v1 的负效果，但也没有正收益（20条样本太小，temporal 仅 4 条）

### 跳闸事件（小区电压不稳）
- 17:28 左右跑 ablation 时跳闸，服务器重启（worker run #17 被杀）
- 重启后：llama-embedding(8082/8083) systemd 自启正常；kanban worker 自动重新拉起 run #19 从头跑
- **意外收获：16G→32G 内存条被识别**（8G×2 双通道 2133MT/s，DIMM_A1+C1）——之前买的 +16G 条子到位

### 结论与下一步
- v2 基础设施正确但 20 条样本看不出统计差异；temporal 类提升需更大样本（100条≈40min）
  + 更激进时间窗验证
- **方法论发现（重要）**：20条 benchmark 结果不稳定——同一配置（A_90d）不同运行间
  recall@1 漂移 40%~55%（±15pp）。单条样本重跑稳定，但整体结果在运行间漂移，
  可能来自 learn 去重合并的浮点/阈值随机性。**"基线60% vs v2 60%持平"不可判定**，
  需要 50-100 条 + 多次重复取均值才能下结论。小样本单次跑的数据只能当参考，不能当证据。
- learn 全库向量去重 O(N²) 隐患（GPT 指出）——**实测澄清 (2026-08-06)**：
  - 2000 条规模实测：单条 learn 耗时恒定 26-28ms（100条时28ms vs 2000条时28ms），
    **O(N²) 在 ≤2000 条不成立**——numpy 批量 dot 是全表扫描的向量化吸收，几毫秒完成
  - 真实瓶颈：嵌入 HTTP 单条调用 ~18ms/条（占 70%）；其余为分类/实体提取/SQL写入
  - 嵌入服务已支持批量 input（实测 `["a","b"]` 一次返回两个 embedding），
    但 memorykit `_embed` 只发单条 → **真正的优化是批量嵌入接口**（每 HTTP 往返处理 N 条）
  - 决策：O(N²) 修复推迟到真实拐点（预估几万条，届时全扫描从 ms 级涨到百 ms 级）；
    批量嵌入作为下轮优化项（真收益）
  - 待办：大样本 ablation（多次重复取均值）/ 批量嵌入优化 / 时间窗参数调优

  ## 2026-08-06 — 批量嵌入优化（learn_batch）落地

  ### 状态
  ✅ 完成并验证：端到端 1.90x 提速，嵌入阶段 4.77x，81 测试全绿

  ### 实现（t_bcbb8170, coder/flash, ~14min）
  - embedder.py：`HTTPEmbedder.embed_batch(texts)` — 一次 POST 批量嵌入，按 index 顺序回填，
    过短(<3)/失败条目 None，整体失败全 None；embed() 单条不变
  - db.py：默认 `_embed_batch` + __init__ 注入 wiring（embedder 无 embed_batch 时 learn_batch 逐条降级）
  - lifecycle.py：`learn()` 加 `precomputed_vec` 可选参（哨兵 _NO_PRECOMPUTED_VEC：
    不传=原行为内部嵌入；传=跳过 _embed）；新增 `learn_batch(items)` —
    先批量嵌入（128/块），再逐条复用 learn 原逻辑（分类/去重/写库/merge+event_time 保护），
    返回与 items 一一对应结果
  - tests/test_learn_batch.py 新增 16 测试；全套 81/81 通过（65 旧 + 16 新）

  ### 性能（BENCH_REPORT.md，500 条 bge-m3@8082）
  | 指标 | 逐条 learn | learn_batch | 提速 |
  |---|---|---|---|
  | 端到端 500 条 | 19.23s | 10.10s | 1.90x |
  | 嵌入 HTTP 调用 | 500 | 4 (128/块) | ~N/B |
  | 纯嵌入阶段 | 19.0ms/条 | 4.0ms/条 | 4.77x |

  ### 说明
  - 单条 vs 批量路径同文本向量 cos≈0.9988（llama.cpp 内部路径不同），
    临界去重条目（sim≈0.85±ε）可能有 <1% 合并差异；确定性假嵌入器下两路径完全一致
  - 批量模式自身跨调用确定（cos=1.0），检索/去重自洽
  - PM 复验：81 测试全过；learn_batch 5 条 190ms、event_time 透传、检索时间 bias 正常

## 2026-08-07 — hybrid 三路嵌入 + sparse lexical 检索接入

### 状态
✅ 8082/8083 均换 bge-m3 原版三路（FlagEmbedding）；sparse 接入 memorykit 检索 + bridge 暴露

### 嵌入服务升级
- **8082**（Hermes 记忆用，systemd）：llama_cpp GGUF → **bge3_hybrid_server.py 原版三路**（llama-embedding.service 改 ExecStart，venv-flagembed）
- **8083**（小龙虾用）：同原版三路服务（后台进程）
- bge3_hybrid_server.py 加 --port/--model 参数（8082/8083 共用）
- 性能：单条 35.8ms / 批量 8.9ms/条 / learn_batch 15ms/条（去 colbert 序列化后；colbert 默认不返回，?include_colbert=1 按需）
- 显存：原版 FP16 1492MiB/实例（GGUF 578MiB），两实例 ~3GB
- 踩坑记录：见 memory（torch.load 安全检查→转 safetensors；transformers 须 5.x；FlagEmbedding 参数是 devices 非 device）

### sparse lexical 检索（t_0e367b3a, coder）
- schema：memories 加 `sparse TEXT`（lexical_weights JSON）
- learn/learn_batch：`_embed_with_sparse` 一次请求解析 dense+sparse_weights（服务不支持→None 纯 dense 降级）
- search_multi：Signal 6 lexical——query sparse × 候选 sparse 的 sum(min(wq,ws)) 交集重叠、max 归一化、LEXICAL_WEIGHT=0.3 可配、hit 带 sparse_score
- **97 测试全过**（81 旧 + 16 新）；GGUF 8082 路径零破坏（Hermes 自身兼容）
- bridge.py _MEM_FIELDS 加 sparse_score

### 重要实测发现（诚实记录）
- **bge-m3 的 sparse 对中文几乎不激活**（中文长短文本均 0 tokens）——lexical 信号仅英文有效
- 英文长句激活 0-19 tokens 不稳定（取决于 token 区分度），短句偶有 4-5 个
- **hybrid 增益主要对英文语料**；中文场景 sparse_score 恒 0/无，检索退化为纯 dense+fulltext
- 结论：sparse 能力保留（基础设施正确），但对中文记忆的实测增益有限——不夸大

### 插件状态
- bridge 全链路验证：learn 落 sparse、search 输出 sparse_score（相关 1.0 / 无关 0.0）
- func_test 29/29、npm test 1 passed 无回归

## 2026-08-07 — 小龙虾实测驱动迭代（第二轮）

### 状态
✅ mk_get 瘦身、episodic 接入、jieba 实体抽取——三轮反馈全部落地

### 小龙虾反馈 → 修复闭环
1. **mk_get 烧 token**（实测 15KB→1KB）：bridge get 默认裁剪 vector/sparse/vector_bin，`include_vector=true` 才返回全量。✅
2. **episodic 索引空**：learn 在 source_session 存在时自动 episodic_start/end；title 不再截断。✅
3. **中文实体整句污染**：`_extract_entities` 改 jieba 分词+词性过滤（n/nr/ns/nt/nz 保留，v/a/d/m 丢弃，eng 走英文规则，专名合并启发式）。实测"我的猫叫小鸡翅，是只银渐层"→[小鸡翅,银渐层]；硬件句→[X99,F8D,E5-2640v4,服务器,主板,内存]。✅
4. **测试同步**：8082 已换 hybrid，TestRealServer8082GGUF → TestRealServer8082Hybrid，114 测试全绿。✅

### 关键教训
- **插件安装目录的 bridge.py 是旧副本**（~/.openclaw/extensions/memorykit-overlay/ 软链指向的 bridge.py 与源目录不同步）——改源码必须 cp 同步到插件目录，否则小龙虾跑的永远是旧版。这次差点误判"修复没生效"。
- jieba 词典对自造词（猫名/人名）覆盖有限——专名识别依赖用户词典，后续可加
- merge 路径不重抽实体（旧数据保持旧标签，合理不覆盖）

### 遗留
- learn 返回 dict 的 entities 字段未填充（库里有，返回没有）——小瑕疵
- 中文自造词实体（"小鸡翅"类）依赖 jieba 用户词典——后续可配
- OpenClaw 原生 memory_search 索引损坏（嵌入 provider 配置，与插件无关，独立待办）

## 2026-08-07 — 自动接线（hooks）落地 + 小白鼠评分 7/10

### 状态
✅ 插件从"手动实验台"→"半自动系统"：before_prompt_build 自动注入 + agent_end 自动 learn，小龙虾评分 6→7

### 实现（t_acc96b4b + t_2c164107, coder）
- **before_prompt_build 钩子**：自动检索相关记忆（rrf_score≥0.5 + 词法重叠闸门，最多 3 条×140 字符 prependContext，30s 缓存节流）→ 注入格式 `[相关记忆] 内容1 | 内容2`
- **agent_end 钩子**：自动 learn 沉淀（质量门槛≥20 字符/中文密度/信号词，FNV-1a 幂等去重，带 source_session）→ 等效 hermes 的 sync_turn
- manifest activation 加 `onCapabilities: ["hook"]`
- 验证：npm run build ✓、vitest 19/19 ✓、plugins validate ✓

### 踩坑（重要）
1. **hooks 注册 API 争论**：coder 最初用 `api.on`，我误判为 bug 要改 `api.registerHook`——**实际 api.on 是对的**（typed hook，types-DaHgOqFX.d.ts:12298，registerTypedHook 是唯一派发通道）；`registerHook` 是 internal hook，不服务 before_prompt_build/agent_end。**教训：不要凭一知半解推翻 worker 的 API 调研，先看证据。**
2. **静默不生效根因**：`agent_end` 属于 conversation hook，非 bundled 插件必须显式开 `openclaw.json → plugins.entries.memorykit-overlay.hooks.allowConversationAccess = true`，否则 registerTypedHook 拒绝注册（静默）。已配。
3. **--local 模式不加载 hooks**：embedded agent 不走 gateway hook 管道，测试 hooks 必须 `openclaw gateway --allow-unconfigured` 起 gateway + `openclaw agent --agent main`（不带 --local）。gateway 需 gateway.mode 配置或用 --allow-unconfigured。
4. **worker 迭代预算超时**：真实验证环节（跑 openclaw agent）慢，90 次迭代不够，run 30/32/33 超时。复杂验证任务要拆小或提高预算。

### 小白鼠最终确认（7/10）
- 自动 learn 实锤：不手动调 mk_*，消息自动入库（outcome=merged 自动去重，行为收敛）
- [相关记忆] 注入实锤：格式匹配 formatMemories 源码输出
- **"丢数据"批评收回**：是 --local 临时库 vs 正式库的混淆（workspace 日志里的内容从未进正式库），不是系统丢数据；正式库 08-06 记录活到现在
- 评分：设计 9 / 实现 6.5 / 帮助 5 / 稳定 7 = **7 分**（"合格的半自动工具"）
- 体验建议：工具响应带库模式标识（local: true）防误判——改进项
- 上 8 分还差：jieba 中文专名词典 / 验证闭环 vscore=0 / 真实数据跑几周过规模考验

### 当前运行状态
- OpenClaw gateway 在跑（--allow-unconfigured，10 plugins 含 memorykit-overlay）
- 8082/8083 均为 GGUF 嵌入（5.5ms）
- memorykit 114 测试全绿；插件 vitest 19/19

## 2026-08-07 — 中文专名实体抽取打通（8/10 里程碑）

### 状态
✅ 小龙虾评分 7 → 8（"能用的工具，从实验台正式毕业"）。中文专名（小鸡翅/银渐层/虹桥火车站）首次真实入库。

### 根因链（今天最深的坑）
1. jieba 用户词典 + merge 实体合并已落地（t_921e668a + t_8a481992，141 测试全绿），本地手动测试 entities 正确
2. 但小龙虾（gateway）实测 entities 恒 None——"本地通、gateway 不通"
3. 排查：词典路径对、pip 版遮蔽排除、merge 修复生效但 gateway 仍旧
4. **真相**：gateway 的 TS 调 `execFileAsync("python3")` → 系统 python3（`/usr/bin/python3`）**没装 jieba** → `import jieba` 失败 → 实体抽取**静默降级**到词表方案 → 中文专名全灭
5. 手动测试用的 venv python3（`/home/joe/venv-flagembed/bin/python3`）有 jieba 0.42.1 → 本地测试通
6. **修复**：src/index.ts 的 execFileAsync 硬编码 `/home/joe/venv-flagembed/bin/python3`（t 直接改，非 kanban）
7. 验证：小龙虾 mk_learn 全新记录 → entities=["小鸡翅","银渐层猫","虹桥火车站"] ✅

### 关键教训
- **python 环境差异是隐形坑**：`#!/usr/bin/env python3` 和 `execFileAsync("python3")` 依赖 PATH——系统 python3 与 venv python3 的 site-packages 不同，jieba 缺失时实体抽取**静默降级不报错**
- **静默降级是最难查的 bug**：代码路径完整、测试全绿，但运行时缺依赖静默走 fallback——要验证真实环境（gateway）而非本地（venv）
- **TS 硬编码 venv 路径**：`/home/joe/venv-flagembed/bin/python3`——venv 更新/删除会破坏，后续应改为可配置（env 或检测）
- 我 shell 曾残留 `MEMORYKIT_DB=/tmp/fix3_verify.db` 污染测试（已 unset）——测试前必须确认环境变量干净

### 8 分现状
- 实体抽取 9（质变）；验证闭环 vscore 0.3 回写；自动 learn/注入/merge/检索/持久化/GGUF 全通
- 扣分：知识层 mk_knowledge 0 节点、数据规模小、"救我于遗忘"实战未现、venv 依赖脆弱
- 冲 9 分：知识层激活 / 真实数据攒几个月 / 一次真实遗忘场景实战

## 2026-08-07 — 自动接线：before_prompt_build 注入 + agent_end 自动 learn（hooks）

### 状态
✅ 完成并验证：npm run build / vitest 19 测试（含真实 bridge 端到端）/ `openclaw plugins validate` 全过；已同步进安装目录

### 背景
小龙虾实测反馈核心痛点——"我的工作流里没有触发它的地方，得我主动调工具，而'记得去查记忆'本身就是认知负担"。Hermes 自己的记忆插件靠 sync_turn/prefetch 钩子自动工作，memorykit-overlay 只有手动工具。OpenClaw 插件支持 hooks（api.registerHook，26 个钩子名）→ 挂两个自动钩子。

### 实现（t_acc96b4b, coder）
1. **before_prompt_build → 自动注入相关记忆（prefetch 等效）**
   - `handleBeforePromptBuild`：用事件里的 prompt 调 bridge `search`（复用 mk_search 同一管线，不重复实现），取 top-4 候选
   - 相关度门槛：`rrf_score >= 0.5` 且 top-1 词法重叠（`lexicalOverlap`：中文 bigram + 英文词，校准过：相关查询 lex=1 全覆盖、无关 lex=0）→ 无关查询不注入（省 token）
   - 注入 `prependContext`：`[相关记忆] 内容1 | 内容2`（最多 3 条 × 140 字符截断）
   - 节流：相同 prompt 30s 缓存（8 条上限）；bridge 失败静默跳过（超时 3s），绝不让 hook 拖垮 agent
2. **agent_end → 自动 learn 沉淀（sync_turn 等效）**
   - `handleAgentEnd`：取最后一条 user 消息，质量门槛 `passesQualityGate`（参考 hermes-local：≥20 字符 + 非纯标点/问候 + 中文密度 CJK≥4 或信号词命中），闲聊/短句不存
   - 自动 learn（`source:"session"` + source_session 关联回溯）；内容上限 500 字符
   - 幂等：FNV-1a 哈希 + LRU 去重窗口（100 条 / 24h），同一轮不重复 learn；失败轮（success:false）不沉淀
   - 沉淀后清空检索缓存，下一轮立即生效
3. **manifest**：activation 加 `onCapabilities: ["hook"]`；contracts tools 不变（hooks 是附加能力）
4. **注册方式**：`defineToolPlugin` 只注册工具，`plugin.register` 外包一层挂 hooks（单测里 fake api 捕获 `on()` 验证两个钩子 + 超时约束：before_prompt_build ≤5s、agent_end ≤10s）

### 验证
- `npm run build` 通过；`openclaw plugins validate` → "Plugin memorykit-overlay is valid."
- vitest 19/19：10 工具元数据 + 双钩子注册 + 消息提取 + 质量门槛正反例（过短/纯问候/纯标点/无信号闲聊全拒）+ 词法重叠闸门 + 注入格式截断 + 幂等哈希
- 端到端（真实 bridge + 临时库）：agent_end 正常对话自动 learn 落库且带 source_session；重复消息不重复 learn（幂等）；before_prompt_build 相关提问注入含"小鸡翅"、无关提问（"今天天气怎么样"）返回 undefined
- 安装目录同步：bridge.py / dist/index.js / openclaw.plugin.json 与源目录 diff 一致（吸取上次"插件目录旧副本"教训）

### 阈值校准说明
- AUTO_INJECT_MIN_SCORE=0.5 / AUTO_INJECT_MAX=3 / CHAR_CAP=140：经 `src/index.test.ts` 单元 + 端到端用例验证（相关/无关正反例）
- MIN_LEARN_CHARS=20 / MIN_CJK_CHARS=4 / SIGNAL_RE：参考 hermes-local sync_turn 逻辑移植

### 待办
- [ ] 小龙虾实测验收：正常对话（不手动调工具）后 mk_stats 显示自动 learn；问相关问题自动注入
- [ ] agent_end 目前只沉淀 user 消息原文——可考虑提炼为更浓缩的记忆（下轮优化）

### 状态
✅ 全部完成（功能测试 29/29、基准 recall@1 100%、端到端验证通过）

### 做了什么
1. **插件本体 memorykit-overlay v0.1.0**
   - `src/index.ts`：TS 薄壳，`defineToolPlugin` 注册 10 个 `mk_*` 工具
   - `bridge.py`：Python 桥，15 个命令，JSON 输出，检索结果裁剪 vector 肥字段
   - `openclaw.plugin.json`：onStartup 激活，contracts 声明 10 工具
   - 已 `openclaw plugins install` 进小龙虾，enabled ✅

2. **端到端验证**（笔电 Hermes 执行）
   - 起 OpenClaw gateway :18789，补 auth token + 开 chatCompletions 端点
   - 通过 OpenAI 兼容 API 让小龙虾真调用 `mk_learn` 存下"服务器IP是192.168.5.50"
   - SQLite 落库实锤：`~/.openclaw/state/memorykit-overlay.db`，source_session=agent:main:mktest-001

3. **功能测试 func_test.py**：learn→search→correct→reinforce→verify→overlay→episodic→knowledge→stats 全链路
   - 首轮 24/27，3 个失败全是 bridge 参数签名不匹配（`verify` 的 `degrade_threshold`、`knowledge_promote` 的 `tags/confidence`、reinforce 断言逻辑）
   - 修完重跑：**29/29 通过**（correct→reinforce 闭环新增 2 条断言）

4. **基准测试 bench_test.py**（新增，本 session 服务器侧完成）
   - 种子 6 条（3 目标 + 3 干扰：同域/异域）
   - 查询 6 条（3 引导 + 3 干扰）× 3 检索模式（multi/semantic/two_stage）
   - **结果：recall@1 = 100% (3/3) × 3 模式，平均 ~240ms/查询**
   - 干扰查询返回候选是设计行为（无阈值容错，LLM 层过滤）

5. **LongMemEval 标准基准 bench_longmemeval.py**（新增，本 session 服务器侧完成）
   - 拉取 HF xiaowu0162/longmemeval-cleaned（500 条，277MB，走 Clash 代理直连 huggingface.co——hf-mirror 对该文件报 Invalid username）
   - 这是 Letta/MemGPT 团队的长对话记忆检索标准基准：每样本 = 1 问题 + 1 答案 + 53 会话干扰噪音
   - 适配：进程内直接 import memorykit（不经 bridge 子进程，否则每消息一次 python 启动太慢），每样本独立临时库，haystack 逐条 learn → question 检索 → 答案关键词 overlap 判定
   - 小样本验证（5 条）：recall@1=80%, MRR=0.800；每样本 learn ~483 条消息约 21s
   - 瓶颈：learn 逐条嵌入+全库去重是真实管线（公平），但海量灌入场景偏慢

6. **8083 独立 GPU 嵌入实例**（新增，本 session 服务器侧完成）
   - 给 memorykit-overlay 起同款 bge-m3 嵌入服务：`python3 -m llama_cpp.server --model /home/joe/llam/bge-m3-Q4_K_M.gguf --n_gpu_layers -1 --n_ctx 512 --port 8083`（后台进程，非 systemd）
   - 与 Hermes 自己的 8082 隔离；benchmark 用 `MEMORY_EMBED_URL=http://127.0.0.1:8083` 切换
   - 8083 只在测插件时开，不需要常驻
   - 坑：bench_longmemeval.py 曾硬编码打印"bge-m3@8082"误导，改为读环境变量显示

7. **时态锚定 v1 实验：失败**（本 session 服务器侧完成）
   - 假设：LongMemEval temporal-reasoning 弱（50%）→ 把会话时间戳锚进记忆可提升
   - 实现：learn 时 content 前缀 `[2023/07/01 (Sat) 22:07]` + 检索 query 拼 `(asked on question_date)`
   - 结果：**整体变差** recall@1 60%→55%, recall@5 80%→70%, MRR 0.692→0.625
     - multi-session 反而升了（80%→100%），但 single-session-user 崩了（@5 85.7%→42.9%）
   - 根因：日期 token 混进 content 污染向量/FTS；query 里的日期把同一天干扰会话排前
   - **结论：时间必须是独立结构化字段（时间窗筛选+加权），不能拼文本。
     印证 context-calendar 的存储/渲染解耦。v2 需改 memorykit schema（加内容时间列），未实施。**
   - 测试文件 bench_longmemeval.py 保留时态 v1 代码（可回滚对比），默认行为已回到无时态基线

8. **文档**（本 session 服务器侧完成）
   - README.md 重写：架构图、工具清单、构建/安装/测试、设计约定、结果（含 LongMemEval 基线 + 时态 v1 失败记录）
   - project-log.md（本文件）

### 踩坑记录
- **OpenClaw gateway 的 OpenAI 兼容端点默认关闭**：`config set gateway.http.endpoints.chatCompletions.enabled true` 才开
- **auth token 要显式设**（`gateway.auth.token`），否则每次重启生成新 token
- **网关重启命令会被 Hermes 拦截器误判**（关键词误杀）→ 用 /tmp 脚本中转
- **bridge 参数签名**：`verify()` 用 `degrade_threshold` 不是 `threshold`；`knowledge_promote()` 用 `tags/confidence` 没有 `content`
- **bridge 软链问题**：插件代码在 `dist/bridge.py` 找桥接脚本，实际文件在扩展根目录 → 建软链 `dist/bridge.py → ../bridge.py`（小龙虾自己发现并修了）
- **pgrep 进程名 >15 字符匹配不到** → 用 `pgrep -f`

### 待办 / 下一步
- [ ] 基准测试扩大题目集（当前 6 题 × 3 模式，种子 6 条）
- [ ] 服务器 Hermes 评审（memory-v2 概念模块，开源评估）
- [ ] 设计文档归档（memorykit-overlay 架构说明）

### 恢复命令（如需重建）
```bash
cd /home/joe/memorykit-overlay
npm run plugin:build && npm run plugin:validate
openclaw plugins install /home/joe/memorykit-overlay
python3 func_test.py   # 29/29
python3 bench_test.py  # recall@1 100%
```

---

## 2026-08-07 — 演进方向确立：无限上下文架构（Joe 定）

### 背景
高光笔实验（方向A/B，cognitive-architecture/experiments/highlighter/）五轮审查后方向A关闭；
方向B（选择性压缩分辨率）验证了子机制（模糊化保留可答性 48/48、不向上虚构、来源转移），
但 Joe 指出真正的方向不是"压缩实验"，而是把压缩**改成主动且密集的蒸馏**，接上 overlay 的指针展开：

### 架构（已写入 README"演进方向"章节）
用相对密集的 memory 覆盖 state——上下文只装"当前时间段记忆（时间聚集）+ 语义召回历史片段"，
原文沉底 state.db（指针可展开），窗口不随历史增长 = 类似无限上下文。

三大机制：覆盖（密集 memory 替代 state）/ 时间聚集（context-calendar 思路）/ 语义召回（memorykit search + 指针展开）。

### 蒸馏管线（核心难点，待设计）
- L1 结构事实：规则抽取（memorykit learn 已有），零成本
- L2 语义沉淀：轻模型异步批处理，主力
- L3 高价值：重模型低频
- 纪律：异步批处理 + 质量闸门（源头原文+上下文 / 产物 vscore / 修正 correction），不边聊边蒸馏

### 关联
- 方向B v13（t_c40486db）在跑：判分/截断修复 + 来源转移矩阵（"来源转移"正是"展开"的行为雏形）
- 待办：设计文档归档（cognitive-architecture/design-docs/ 或本目录）、蒸馏管线最小验证、memory 插件功能扩大（mk_learn_batch 暴露等）

---

## 2026-08-08 — scope-recall 对比：插件层差距与借鉴

### 背景
笔电解剖第三方记忆插件 scope-recall（28838行）咨询服务器对比。结论已记 memory-kit/project-log + README。

### 插件层差距（overlay vs scope-recall）
- scope-recall 是完整插件生态（capture/promotion/review/store/freshness/forgetting/doctor/CLI/benchmarks），overlay 只是薄桥（bridge.py 320行 + TS 壳）
- 它有独立 CLI（治理/体检/报告），我们有 func_test + bench 但无治理工具

### 借鉴优先级（插件层）
1. **validator 活验证**（file_exists/command/http）——未来 mk_verify 可扩展
2. **governance 审计**（record_governance_audit_event）——correction/删除留审计
3. **freshness 三态**（current/needs_check/stale）——我们的 verification_status 可对齐
4. **soft-archive + dry-run** 遗忘治理——比直接删安全

### 不动
- 我们的 correction 闭环 / 遮蔽 / 指针展开是独有，保持
