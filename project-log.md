# Memory-Kit — 项目日志

> 母分支（核心库）日志。插件层（memorykit-overlay / OpenClaw 集成）记录见子分支 `memorykit-overlay/project-log.md`。
> 本日志只记 **memorykit 库本身**的演进：API、能力、性能、测试。

## 项目定位

给 AI agent 用的**轻量本地记忆库**：SQLite 存储，可检索、可过期、可纠正、可遗忘。
与框架无关，`pip install` 接入。memorykit-overlay 是它的 OpenClaw 插件子分支。

## 2026-08-07 — 库级迭代（jieba 实体 + merge 修复 + 验证闭环 + hybrid 回退）

### 状态
✅ 141 测试全绿（+27 新增）；中文专名实体抽取打通（8/10 里程碑的库侧基础）

### 迭代内容（本日志认领，原记录在 overlay 日志中）
- **jieba 用户词典**（t_921e668a）：`memorykit/data/jieba_userdict.txt`（43 行，人名/宠物/品种/地名/硬件/项目专名），`_extract_entities` 中文路径懒加载。修复两个 jieba 机制坑：①词典格式"词频在前词性在后"（写反整行变词）②posseg 的 re_han_internal 不含连字符，X99-F8D 类带 `-` 专名走直配兜底。英文/无中文场景完全跳过 jieba（首载 ~0.6s 不拖累）
- **merge 实体合并**（t_8a481992）：learn() 两处 merge 分支（向量去重 + 实体兜底）UPDATE 时新增 `_merge_entities`/`_merge_tags`——新抽实体 ∪ 旧实体（JSON 解析，去重保序）写回，旧 null 直接用新的；同步 entity_links 表。此前 merge 只拼 content 导致 entities 恒 null（中文专名丢失根因）
- **验证闭环打分**（t_bf1bb052）：`_lightweight_verify` 库内轻量验证兜底——state.db 缺失/无源 session 时不再直接 pending 放弃，按 方案A 相似记忆一致性（cos>0.85 → 0.3-0.7 按证据数）/ 方案B 时间锚定（0.30）/ 方案C 实体密度（0.25）给非零 vscore + method，provenance 记 fallback=true；高分（≥0.5）→ v=verified + confidence +0.05。顺带修复 verify() 手动路径在 state.db 不可达时把 importance 腰斩的误降级
- **hybrid 三路实验 → 回退**：08-07 上午试 bge-m3 原版三路（FlagEmbedding 1.4.0 + 原版权重，dense+sparse+colbert），sparse 接入检索（Signal 6 lexical）；实测中文 sparse 几乎不激活（0-1 token）+ 慢 6.5x → **用户拍板换回 GGUF**，hybrid 暂缓。三路代码/venv 保留（venv-flagembed + bge3_hybrid_server.py）

### 测试
- 141 测试全绿（原 114 + jieba 词典相关 + merge 5 + verify 相关）；`python run_tests.py`
- 验证用例：merge 后 entities 从 null → ["小鸡翅","银渐层","虹桥火车站"]；高 importance learn → vscore 非零 + method

### 踩坑（库侧）
- jieba 用户词典格式：`词频在前词性在后`，写反整行被当词
- posseg 不识别含连字符型号 → 直配兜底
- 系统 python3 无 jieba 时实体抽取**静默降级**（不报错）——这是"本地通、gateway 不通"的库侧根源，解决在插件层（venv python3）

## 2026-08-06 — v2 时间结构化字段（event_time）+ learn_batch 批量嵌入

### 状态
✅ 81 测试全绿（+16 新增）；v2 落地

### 迭代内容
- **event_time**：时间结构化字段（存绝对 ISO 时间），检索时软重排 bias（1.25/0.25/1.0），merge 保护，完全向后兼容。检索元数据边界，不碰 TTL、不污染文本
- **learn_batch**：`HTTPEmbedder.embed_batch()`（128 条/块）+ `learn_batch(items)`（批量嵌入后逐条复用原 learn 逻辑）+ `learn()` 加 `precomputed_vec` 哨兵参数（零侵入，无 embed_batch 自动降级）
- **性能**：端到端 1.90x（19.23s→10.10s/500条）、嵌入阶段 4.77x（19→4ms/条）、HTTP 调用 500→4 次

### 基准
- LongMemEval（20条 seed=42）：基线 60%/80%/0.692；v1（拼 content）55%/70%/0.625（失败，日期 token 污染）；v2（event_time）60%/80%/0.692（持平）
- **稳定性警告**：同一配置两次跑 recall@1 = 40% vs 55%（漂移 ±15pp）——20 条小样本单次跑不作证据，大样本（50-100 条×重复）待跑
- O(N²) 澄清：≤2000 条不成立（numpy 向量化吸收，单条恒定 26-28ms）；真实瓶颈是嵌入 HTTP 单条调用 → learn_batch 解决

## 2026-08-06 — 首版开源发布（v1.0.1 → v1.1.0）

### 状态
✅ AGPL-3.0 已发布 github.com/Joevy-1112/memory-kit

### 关键决策
- 开源只开概念模块（confidence engine / verification loop / time-anchored memory），绝带私人数据/凭据/双端同步逻辑
- 品味纪律：不吹不虚假宣传（"无上限"→"可纠正"+ 限制诚实版）、不攀附（删"从Hermes剥离"）、不过度自贬（"早期原型验证具体问题"而非"代码不算漂亮"）
- GPT-5.5 外审 → 6 项 must_fix 修 4 → 验收闭环

## 演进源流

memory-kit 由 Hermes 的 scripts/memory_db.py 概念提炼而来（开源时去除 hermes 耦合，verify_recent/search_state_* 等保留为可降级实验功能）。Hermes 自用侧仍跑 memory_db.py，库侧演进不自动同步——同步需显式操作。
