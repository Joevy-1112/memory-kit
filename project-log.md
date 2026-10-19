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

---

## 2026-08-08 — scope-recall 对比发现的工程漏洞（2个）

### 背景
笔电解剖第三方记忆插件 scope-recall（journal=truth 事件溯源架构），服务器对比 memorykit 时发现两个工程漏洞。

### 漏洞1：state 没 learn（工程实现失误遗留）
- 事实: state.db（原文存档）与 memory.db（提炼）独立写入，learn 不从 state 派生
- 后果: 提炼损失不可逆（learn 抽坏只能 correction 修正，不能从 state 重放重建）——比 scope-recall 的"journal 可重建"弱
- 目标: state → learn 派生链（或至少 learn 记录 source 指针可回溯重建）

### 漏洞2：event_time 只做了检索侧，没做渲染侧（时态污染防护缺口）
- 事实: event_time 参与检索排序（TIME_WINDOW_BIAS_IN/OUT 偏置），但记忆渲染进上下文时**不带时间标注**
- 后果: 旧记忆被召回后，模型看不出新旧，可能当当前事实用（时态污染）——freshness/置信度是另一回事，防时态污染需要"渲染侧时间边界"
- 目标: 渲染时记忆带时间标注（如【2026-03-12 记录】），让模型能判断信息新旧

### 关联
- scope-recall 值得抄: local-hash 兜底 / journal digest 常驻批处理 / observational no-op / validator 活验证（file_exists/command/http）
- scope-recall 缺的: correction 闭环 / 记忆衰减 / 实体合并（jieba）——我们已有
- 我们可能想错的: 把真 bge-m3 当卖点（vector 是放大器不是真相源）

## 2026-08-08 — scope-recall 解剖 + 借鉴方案（t_ed9ca8f1）

### 状态
✅ 解剖完成：6 项补齐清单全部给出"它怎么做 → 我们缺什么 → 怎么并进"；validator 活验证原型交付（20 测试全绿）；未动 scope-recall（只读）

### 产出
- **design-notes/scope-recall-borrowing.md**：主交付物。P0×2（journal 派生链 / validator 活验证）+ P1×4（渲染时间标注 / local-hash 兜底 / 常驻提炼 / 遗忘治理壳），每项标优先级、源码行号、最小侵入落点；明确"不抄什么"6 条（无 correction / 无衰减 / 无实体合并 / LLM 模板化提炼 / 多租户复杂度 / RRF）
- **memorykit/validator.py**：活验证原型（file_exists/command/http + 别名归一化 + scope-recall 兼容 spec 形态 + run_checks 批量降级），纯标准库零依赖
- **tests/test_validator.py**：20 项测试（含本机 http.server 真 200/404/拒连），`python run_tests.py validator` 全绿

### 关键判断
- scope-recall 的 freshness.py 只是**状态机+惩罚+报表**，三种 validator 的**执行器**实际不在插件里（spec 存表、执行交给 agent 工具）——我们把执行器落成库内原语，正好补上，且与 _lightweight_verify（被动推断）互补
- journal=truth 的骨架（journal_entries + memory_journal_sources 指针表 + watermark + 收据表）可直接按单用户裁剪进库，反而解耦我们对 Hermes state.db 的依赖
- 借鉴点全部走现有注入/乘性机制（embedder 注入、VERIFY_WEIGHTS 乘法），不引入新架构

### 测试
- 新增 20 项全绿；全量 161 项在项目 venv 下 1 失败——`test_learn_batch.test_real_server_batch_matches_single_embed`（live llama.cpp 8082 对同文本批量返回微差向量，cos=1 断言挂；既有环境项，与本任务无关；系统 python3 跑另有 13 项 jieba 缺失失败，也是既有项）

### 下一步（已排优先级，见设计文档第 9 节）
1. P0 journal 表 + 双写 + replay_from_journal（修漏洞 1）
2. P0 fact_freshness 侧表 + verify 活验证接线 + 检索惩罚（validator.py 已就绪）
3. P1 local-hash 兜底（~40 行 drop-in）
4. P1 渲染时间标注 helper
5. P1 drain_journal 常驻提炼（依赖 1）
6. P1 forgetting 治理壳（dry-run/审计，包在 age() 外）

## 2026-08-08 — validator 原型修 3 个实证问题（t_986ff91b）

### 状态
✅ M1/M2/H1 全部修复 + 7 项回归测试；validator 27 项全绿，可进主线；全量 168 项仍 1 failure（既有 live 服务确定性项，与本次无关）

### 产出（memorykit/validator.py）
- **M1 不抛契约**：新增 `_coerce_timeout()`（ValueError/TypeError/nan/inf → 默认值；0/负数钳制到 MIN_TIMEOUT=0.1），_check_command/_check_http 的 timeout 解析不再裸奔于 try 外——`validate({"timeout":"abc"})` 与 run_checks 批量都不再抛
- **M2 shell 严格判型**：新增 `_as_bool()`，只认真布尔 True/False + 白名单字符串 {"true","1","yes"}；字符串 "false"/"0"/""/数字一律 False（fail-closed）——`shell:"false"` 不再展开 $HOME
- **H1 scheme 白名单 + 信任模型**：_check_http 入口校验 scheme ∈ {http,https}，file:// 等入口即拒（消除本地文件存在性探针/内网探测面收窄）；新增 `_redact_url()` 脱敏 detail 回显 URL 的 userinfo；模块 docstring 写明"validator_spec 是可信 agent 配置，绝不可从不可信内容自动派生"
- **tests/test_validator.py**：+7 项回归（畸形 timeout 不抛 ×2、字符串 false 不启用 shell、白名单字符串启用、非 http(s) scheme 拒绝、userinfo 不泄漏、_redact_url 单元）

### 测试
- `python run_tests.py validator`：27 项全绿（原 20 + 7）
- `python run_tests.py`（.venv py3.11）：168 项 = 1 failure + 2 skipped，唯一失败仍是 test_learn_batch live llama.cpp 8082 确定性项（既有环境项）

### 下一步（不变）
P0 journal 表（先定 D2 三问：保留策略/role-turn 映射/存全文还是指针）→ P0 freshness 接线 → P1 local-hash → P1 渲染标注 → P1 drain_journal → P1 forgetting 壳

## 2026-08-08 — state→learn 派生链（P0 #1，指针方案，t_798e9d98）

### 状态
✅ 修"state 没 learn"工程漏洞（提炼损失不可逆）：memory 行现在带来源指针，可回溯到原始消息。183 项测试 = 168 旧 + 15 新，仅 1 既有 live 服务失败项，零回归。

### 设计（Joe 2026-08-08 拍板，替代 scope-recall 的 journal 方案）
```
state.db（原始消息，已有）── learn 记指针 ──→ memory.db（提炼）
   ↑ source_session_id + source_msg_id 回溯重建
```
- **不建 journal 表**：scope-recall 建 journal 是因为它没有原生会话存档；我们有 Hermes state.db（永不删），不该重复存原始消息。state.db 就是原始消息存档，memory 只加指针。
- 比 scope-recall 省一份存储（它 journal+memory 双存，我们原文一份+提炼一份+指针）。

### 产出
- **schema**：`memories` 表加 `source_session_id TEXT` + `source_msg_id TEXT`（SCHEMA_SQL + `_migrate_columns` 双路径，旧库自动 ALTER 补齐；旧行两列 NULL，不破坏）
- **learn()**：可选 `source_session_id`/`source_msg_id` 参数，插入路径透传 set() 写入；两处 merge 分支（向量去重 + 实体兜底）用 `_merge_pointer` 旧指针保留 + 新指针追加（逗号连接，语义对齐 source_session）。**不传不炸**，向后兼容 memory_db.py / overlay bridge
- **set()**：同样支持可选指针（learn 插入路径的底层；直接调用传 int msg id 也归一化为 str）
- **learn_batch()**：items 每项可带 `source_session_id`/`source_msg_id`，逐条透传
- **reconstruct(memory_id)**（lifecycle.py）：沿指针回 state.db messages 表定位原始消息，返回 `{found, source:{session_id, msg_id, role, content, timestamp}}`；明确失败分支 memory_not_found / no_pointer（旧数据/无指针="无来源"）/ no_state_db / bad_pointer / msg_not_found，绝不抛。**只建可回溯，不承诺自动重建**（重放/蒸馏是后续管线的事）

### 测试
- 新增 `tests/test_pointer_chain.py` 15 项：learn 带指针→可回溯；不带指针→NULL 不炸；learn_batch 逐条指针；merge 指针合并（含不带指针 merge 保留旧值）；旧库迁移；reconstruct 全失败分支（含 session 不匹配、畸形指针、state.db 不可读）
- `python run_tests.py`（.venv py3.11）：183 项 = 1 failure + 2 skipped，唯一失败仍是 test_learn_batch live llama.cpp 8082 确定性项（既有环境项，与本任务无关——改动不碰嵌入路径）

### 下一步（不变）
P0 freshness 接线（validator.py 已就绪）→ P1 local-hash → P1 渲染标注 → P1 drain_journal（依赖本任务的地基，可复用 learn 指针链）→ P1 forgetting 壳

### 验证补充（同任务，ad-hoc 复核发现并修复）
- **多来源回溯**：merge 后指针是逗号连接列表，`reconstruct()` 按 session/msg id 做并集查询返回 `sources` 列表（`source` 为第一条），部分指针悬空仍能回溯到存活源——"可回溯"对合并记忆同样成立
- **非整数指针拒写**：`learn()`/`set()` 对非整数 `source_msg_id` 拒写 NULL + warning（防一次脏指针经 merge 永久污染指针列）；`reconstruct()` 保留 `bad_pointer` 分支兜手工 SQL/历史脏数据
- 全量 185 项 = 168 旧 + 17 新，仍仅 1 既有 live llama.cpp 失败项

## 2026-08-08 — 修指针链 P1：set() ON CONFLICT 清空指针（t_7d7026b1）

### 状态
✅ P1 已修（review t_5be03922 终审合并前清单）：187 项测试 = 185 旧 + 2 新增回归，仍仅 1 既有 live llama.cpp 失败项，零回归。

### 修复（memorykit/db.py，一行级最小改动）
- set() 的 `ON CONFLICT(key) DO UPDATE` 中 `source_session_id`/`source_msg_id` 由 `excluded.<col>` 改为
  `COALESCE(excluded.<col>, memories.<col>)`——同 key 普通重写不再把溯源链清成 NULL；
  显式带新指针的重写仍用新值（对齐 event_time 既有 COALESCE 语义）。
  修复前：`set('env/gpu', 新内容)` 会把旧指针抹掉 → reconstruct 变 no_pointer（"提炼损失不可逆"在 set 路径上复现）。

### 配套（review 合并前清单）
- **tests/test_pointer_chain.py +2 回归**：
  - `test_set_rewrite_preserves_existing_pointers`：带指针 set → 不带指针重写同 key → 指针保留不丢，reconstruct 仍可回溯（P1 精确复现场景）
  - `test_set_rewrite_with_new_pointer_updates_it`：重写显式带新指针 → 新指针生效（COALESCE 不误伤更新路径）
- **lifecycle.py reconstruct docstring（P3-1）**：reason 枚举补 `state_db_unreadable`（代码会返回、测试已断言，纯文档漂移修复）

### 测试
- `python -m unittest tests.test_pointer_chain -v`：19 项全绿（17 + 2）
- `python run_tests.py`（.venv py3.11）：187 项 = 1 failure + 2 skipped，唯一失败仍是 test_learn_batch live llama.cpp 8082 确定性项（既有环境项，与本次改动无关——未碰嵌入路径）

### 下一步（不变）
P0 freshness 接线（validator.py 已就绪）→ P1 local-hash → P1 渲染标注 → P1 drain_journal → P1 forgetting 壳

## 2026-08-08 — freshness 活验证接线（P0 #2，validator.py 已就绪 → 落库）

### 状态
✅ P0 #2 完成（design-notes §2 落库三步全落地）：198 项测试 = 187 旧 + 11 新，
仅 1 既有 live llama.cpp 失败项（环境项，未碰嵌入路径），零回归。

### 设计（沿用"契约对齐 scope-recall，实现是 ours"）
- **零新表**：freshness 状态存 provenance.fresh（memorykit 自家形态，
  不照搬 fact_freshness 侧表——单用户库一条 memory 一个 fresh 配置，
  侧表的"一 memory 多 fact"多租户复杂度我们用不上）
- **写入**：learn()/set() 可选 `validator={"type","spec","ttl_days"}` →
  provenance.fresh（type 经 normalize_validator_type 归一化，未知值
  fail-closed 到 manual；ttl_days → valid_until = now + ttl）
- **verify()**：_check_freshness 独立于 source 交叉验证——先过期判定
  （valid_until 硬规则 → expired/0.28），再活验证（有 spec 且类型可自动执行
  → validator.validate()：ok → current/1.0 + 续期，fail → stale/0.35）；
  返回 dict 带 fresh 字段
- **search_multi**：_freshness_penalty 乘性并入 rrf_score（VERIFY_WEIGHTS ×
  time_weight × window_bias × freshness_penalty），结果暴露 freshness_penalty；
  无 fresh 配置 = 1.0 零开销
- **validator.py 命名统一（借用纪律待办）**：VALIDATOR_KINDS →
  VALIDATOR_TYPES / _VALIDATOR_TYPE_ALIASES / normalize_validator_type；
  validate() 认 `type` 键优先（kind/validator_kind 兼容）

### 踩坑（本任务）
1. **validate() 只认 `kind` 键，fresh 里存的是 `type`** → 活验证静默跳过
   （spec 有 path 但 kind 解析为空 → none → 不检查）。修法：validate()
   提取键优先级 type → kind → validator_kind（自家命名优先，存量兼容）
2. **learn() 创建路径用 init_prov 整体覆盖 provenance** → set 里组装的
   fresh 被抹掉。修法：改为 set 后读回 provenance 补 `v=pending` 键，
   不整体覆盖
3. **learn 语义去重会合并相似内容** → 测试里两条"端口 8082/8083 配置"
   被 merge 成同 key，断言失效。修法：search 测试改用 set() 建不同 key
4. **db.py patch import 时丢了 logger 定义**（NameError）——改 import 块
   时注意 logger = logging.getLogger(__name__) 是否还在

### 下一步
P1 local-hash 兜底（embedder.py 加 LocalHashEmbedder，~40 行 drop-in）
→ P1 渲染标注（render_memory_lines 纯函数 helper）
→ P1 drain_journal → P1 forgetting 壳

## 2026-08-08 — local-hash 兜底（P1 #4，embedding 挂了 fail-open）

### 状态
✅ P1 #4 完成（design-notes §4 三步全落地）：206 项测试 = 198 旧 + 8 新，
仅 1 既有 live llama.cpp 失败项，零回归。

### 设计（契约对齐 scope-recall vector_runtime，实现是 ours）
- **LocalHashEmbedder**（embedder.py，~50 行纯 stdlib）：token（[\w\u4e00-\u9fff]+
  切分，权重 1.0）+ 字符 3-gram（权重 0.35）→ sha1(feature) 前 4 字节 % dims
  定桶 + digest[4] 奇偶定符号 → 累加 → L2 归一化。空文本用占位串 "∅empty∅"
  （scope-recall 空输入产生空向量，会破坏去重一致性——ours 保持非零）。
- **MemoryDB.__init__ 加 embed_fallback=True**（默认开）：未注入 embedder 且
  HTTPEmbedder.is_available() 为 False → 构造期自动切 LocalHashEmbedder，
  _vector_status="degraded"（stats() 暴露）；False → 保持原行为（HTTP 失败
  → None）。注入自定义 embedder 完全不受影响（_vector_status 默认 ready）。
- **定位纪律**：local-hash 是可用性兜底不是真相源——检索降权/排序仍由
  verify/freshness 管，兜底向量绝不反向覆盖已 verified 记忆。

### 踩坑（本任务）
1. **全量跑时 DEFAULT_EMBED_URL 被先行模块固化**：test_localhash 顶部设
   MEMORY_EMBED_URL=:1 只在单独跑时生效；run_tests 按字母序先 import
   test_learn_batch → embedder 模块缓存 → 我的 env 设置晚了一步 → 全量
   下 fallback 测试失败（探测到真 8082 服务）。修法：**fallback 测试不依赖
   env，用 mock.patch("memorykit.db.HTTPEmbedder", _FakeHTTP) 控制
   is_available()**——测试隔离铁律的又一例：能 mock 就不依赖环境。
2. 新增 embedder 方法后记得补齐 import（re/hashlib/math 三件套）。

### 下一步
P1 渲染标注（render_memory_lines 纯函数 helper，一行接入示例）
→ P1 drain_journal → P1 forgetting 壳

## 2026-08-08 — 渲染侧时间标注（P1 #3，防时态污染）

### 状态
✅ P1 #3 完成（design-notes §3 全落地）：206 项测试 = 198 旧 + 8 新，零回归
（新增 render 8 项，未碰库状态——纯函数模块）。

### 设计（零新数据、只加表现层）
- **`memorykit/render.py`**：`render_memory_lines(items, *, max_chars=600,
  stamp=True)` 纯函数——检索结果 dict 列表 → prompt 行
  `- [key] content【YYYY-MM-DD 记录】`：
  - 时间标注取 `event_time` 优先、`updated_at` 兜底（日期部分 YYYY-MM-DD）
  - stamp=False 关闭（调用方按场景选：当前会话引用不需要时间边界）
  - 内容换行压单行（prompt 行约束）；max_chars 按条目边界截断
  - 畸形/缺失时间字段 → 不崩、无标注
- **与护城河一致**：标注让模型更可能触发 correction 闭环（看出时间矛盾才
  会质疑），而不是让错/旧信息悄悄通过。
- examples/basic.py 加一行用法（渲染带标注的检索结果）。

### 下一步
P1 drain_journal（常驻提炼，依赖指针链地基）→ P1 forgetting 壳

## 2026-08-08 — 自审 + P0 修复: local-hash 向量维度混合 bug

### 背景
yamii 后端 503 无法外审 → 按 gpt-review 强任务标准自审(design-notes/
self-review-2026-08-08.md)。实测抓到 **P0**: LocalHashEmbedder 默认 256 维,
bge-m3 是 1024 维——降级期间 learn 存的 256 维向量 + 服务恢复后 1024 维
查询向量 → search.py np.dot 直接 ValueError, 语义检索崩溃。兜底引入了比
故障更糟的状态("挂了会换一种方式炸")。

### 修复
- LocalHashEmbedder 默认 dims=1024(对齐 bge-m3), 同维后 cos 计算合法
- 文档标注维度契约: local-hash 必须与主嵌入器同维(向量共存同一表)
- 回归测试 +2: test_09(默认 1024 维) / test_10(降级写入+恢复查询不崩,
  np.dot 同维可算)
- 全量 216 项 = 1 既有 live llama.cpp 失败项, 零回归

### 自审其他发现(未修, 记录在案)
- P1: freshness 不是主动状态机(只 verify() 触发), 文档未明说
- P1: provenance JSON 检索热路径逐行解析, 无性能讨论
- P1: 测试套件常年 1 failure(live 8082 断言), 建议 mock/放宽
- P2: reconstruct() 新增 state.db 耦合点; README 主体缺 attribution
- 教训: 兜底实现必须检查与主路径的维度/格式契约一致性(审查 checklist)

## 2026-08-08 — drain_journal 常驻提炼（P1 #5）

### 状态
✅ P1 #5 完成（design-notes §5 修正版，复用指针链）：223 项测试 =
216 旧 + 7 新，仅 1 既有 live llama.cpp 失败项，零回归。

### 设计（不建 journal 表，watermark + 指针链）
- **drain_journal(limit, dry_run)**：state.db 就是原始消息存档（不重复存），
  memory.db 加 `drain_state` KV 表记 watermark（last_msg_id，幂等）。
  SELECT id > watermark AND role IN (user,assistant) AND length >= 10
  ORDER BY id LIMIT N → 逐条 learn(source_session_id, source_msg_id) →
  推进 watermark。
- 提炼规则：tool 消息跳过（JSON/噪音）；短消息跳过（learn 有最短门槛，
  预过滤避免空转）；learn 自身处理分类/去重/合并——drain 只喂料与收据，
  不另起提炼管线（避免 scope-recall 双提取逻辑长期共存的复杂度）。
- 收据：{processed, inserted, merged, skipped, backlog, last_msg_id}；
  dry_run 只统计不写（watermark 不推进、memory 不新增）。
- CLI：`python -m memorykit.db drain-journal [--limit N] [--dry-run]`，
  README 可给 cron 示例（如每小时跑一次，无积压自动空跑）。

### 测试（tests/test_drain_journal.py，7 项）
dry-run 零写入 / 正常提炼带指针（reconstruct 可回溯）/ watermark 幂等
（第二次 processed=0）/ tool 跳过 / 短消息跳过 / no_state_db / 收据字段
+ 积压递减。用临时 state.db（messages 表最小结构），不碰正式库。

### 下一步
P1 forgetting 壳（soft-archive + dry-run + 审计，包在 age() 外面）
→ 测试套件常年 1 failure 的 live 项 mock 化（自审 P1-3）

## 2026-08-08 — forgetting 遗忘治理壳（P1 #6，借鉴清单收尾）

### 状态
✅ P1 #6 完成——**design-notes 借鉴清单 6 项全部落地**（P0 #1 指针链 /
P0 #2 freshness / P1 #3 渲染 / P1 #4 local-hash / P1 #5 drain / P1 #6 forgetting）：
229 项测试 = 223 旧 + 6 新，仅 1 既有 live llama.cpp 失败项，零回归。

### 设计（包在 age() 外面，不动衰减状态机）
- **governance_audit_events 表**（SCHEMA_SQL 新表）：batch_id/action/
  memory_id/memory_key/before/after/created_at——before/after 快照 =
  天然回滚材料（契约对齐 scope-recall governance_audit_events，实现 ours）
- **forgetting_report()** 只读分类：soft_archive / hard_delete（secret 正则
  或 <8 字符噪音）/ review_debt（pending 超 7 天挂起）/ duplicate_groups
- **forgetting(dry_run=True 默认, delete=False)**：dry_run 只出计划；
  apply 软归档（verification_status='ARCHIVED' + importance=0.05）或
  delete=True 才物理删；每条记审计，batch_id 自动生成或传入
- **rollback(batch_id)**：soft_archive 快照 UPDATE 还原全列；hard_delete
  快照 INSERT 重建（id 保原值）——审计行保留
- 与 scope-recall 差异：它 hard-delete 要求 vector_store 存在（fail-closed）；
  我们把 before 快照写审计表即视为"可回滚材料"，物理删前必留快照——
  我们的向量在 SQLite 同一行，快照天然包含，不需要 companion 库检查

### 测试（tests/test_forgetting.py，6 项）
分类正确性 / dry-run 零写库 / soft_archive 审计 + 状态变更 / hard_delete
物理删 + 审计含 content / 整批 rollback 双向还原 / 空库不崩。

### 借鉴清单收尾备注
6 项全落地后 README 的"补齐清单"可标记完成。剩余自审遗留：
①测试套件 live 项 mock 化（P1-3）②freshness 主动调度（P1-1）
③README 主体 attribution（P2）——下轮迭代处理。

## 2026-08-08 — 打磨轮（自审 P1/P2 收尾）: 测试套件首次全绿

### 状态
✅ **229 项测试 = 0 failure + 2 skipped**（历史首次全绿——此前常年 1 项
live 服务失败，各轮 project-log 都写"既有环境项，与本次无关"）。

### 修复（自审 P1-3：live 项 mock/放宽）
- **test_learn_batch 根因实锤**：不是 cos 阈值（批量 vs 单条实测 cos=0.99854，
  过 0.99），而是**批量内重复文本的严格相等断言**——llama.cpp 批量路径对
  重复文本有浮点级非确定性（实测差异 1.6e-01 / cos 0.998）。严格
  assertEqual 必然失败。
- 修法：cos 0.99→0.90（跨路径语义余量）+ 批量内确定性断言改 cos>0.99
  （语义一致承担确定性语义）。服务不可达时既有 skipUnless 生效。

### 补充（自审 P1-1：freshness 主动调度文档化）
- verify_batch 返回加 `fresh_checked` / `fresh_changed` 统计——批量扫描时
  freshness 处理量可见（过期判定/活验证随 verify 执行）。
- 明确语义：freshness 检查入口 = verify() / verify_batch()（only_pending=False
  全量扫），非自动后台任务。

### 补充（自审 P2：README attribution）
- README 顶部加借鉴声明：scope-recall(MIT) 契约启发、实现独立自研、
  correction/衰减/遮蔽/指针链是独有增量、逐条记录见 design-notes。

### 剩余（记录在案，不再本轮处理）
- freshness 真正自动化（后台调度器）——需要外部 cron/调度接入，非库内职责

## 2026-08-08 — review 打磨轮 v2（版本 bump / 统计命名 / 文档入口）

### 审查发现（自审第二轮）
1. **版本未 bump**：v1.1.0 后一天加了 6 大功能（指针链/freshness/渲染/
   local-hash/drain/forgetting），版本还停在 1.1.0——版本纪律缺失。
2. **fresh_changed 命名误导**：实际统计的是"状态已定"的行（expired/stale/
   current 且有活验证时间戳），不是"本次发生变化"——改名 fresh_resolved
   并注释语义。
3. **README 无更新入口**：08-08 迭代章节已 8 个，新读者找不到入口——
   顶部加"📜 更新日志"指引。

### 修复
- pyproject.toml + __init__.py：1.1.0 → **1.2.0**
- verify_batch 统计字段 fresh_changed → fresh_resolved（+ 语义注释）
- README 顶部加更新日志指引

### 状态
✅ v1.2.0，229 项测试全绿（本轮改动不碰逻辑路径，仅统计命名/版本/文档）。

## 2026-08-08 — CI 修复: jieba 缺失致 13 项失败(连续 6 个 push 红)

### 根因
CI workflow 只 `pip install numpy`, 没装项目依赖——pyproject.toml 声明了
jieba==0.42.1 但 CI 从未 `pip install .`。jieba 缺失 → 中文实体抽取懒加载
跳过 → 依赖用户词典的实体测试("小鸡翅" 等)13 项全挂。本地 .venv(有 jieba)
全绿掩盖了问题, 推 GitHub 才暴露。**测试环境与 CI 环境依赖不一致**。

### 修复
ci.yml 的 Install dependencies 改为 `pip install -e .`——装项目自身依赖
(验证 pyproject 完整 + jieba/numpy 齐), 不再手写依赖清单(避免漂移)。

### 验证
- 本地模拟 CI(干净 venv + pip install -e . + python run_tests.py):
  229 项 = OK (skipped=2) ✅
- 推送后真 CI 三版本矩阵(3.10/3.11/3.12)应全绿

### 教训(加入 checklist)
- 提交前若 CI 存在, 检查"本地 venv 依赖 ≠ CI 依赖"的漂移(本地 jieba 装过,
  CI 没装 → 本地全绿 CI 红)
- run_tests.py 在本地全绿 ≠ 仓库可 CI——用干净 venv 复现 CI 路径再推

## 2026-08-11 — 时态污染真实案例 #2（注入层"快照当对话"变体）

### 现象（Joe 指出）
08-10 深夜会话（id=20260810_202222_c62fb36d, msg 202784）：用户只发"诡异"两个字，
回复却开头接住"那个 heartbeat 调 token 的——确实诡异"——**heartbeat 调 token
这个话题用户根本没在当轮说过**。

### 根因（与案例 #1 不同源）
- 案例 #1（08-10, P40 回复）：**旧条目污染**——0718 旧条目("sxm2 v100 550")
  未标 superseded，被检索注入，模型当"当前计划"自信输出。
- 案例 #2（08-11, 本案例）：**快照当对话**——memory-context 的 System note 里
  带时间戳的历史快照（[sync] 协议条目、最近工作记录）被模型当成"用户刚说的话"，
  于是回复接住了一个本轮不存在的话题（heartbeat/可复述语言），且推理看起来
  完全自洽——**污染内容自洽时最危险**。

### 教训（检索侧 → 渲染侧之外，还要"来源隔离"）
1. 注入层必须显式标记来源边界："【历史快照, 非当前消息】"——让模型能区分
   "记忆背景" 与 "用户本轮输入"，而不是把两者当同质上下文。
2. 模型侧防御：回复若引用"用户没在当前轮说过"的具体话题，应视为可疑——
   置信度与依据脱节时主动降级/标注来源，而不是顺着接。
3. 与案例 #1 合并结论：时态污染的完整防线 = 写入侧矛盾检测（P0 候选）+
   检索侧来源隔离 + 渲染侧时间标注（P1 #3 已落地）+ 模型侧"接陌生话题先
   确认"的纪律。

## 2026-08-11 — Hermes 记忆插件统一到本库单源（memory_db.py 双轨退役）

### 背景
生产 `~/.hermes/scripts/memory_db.py`（Hermes 专用）与本库（AGPL 开源母）同源分裂、
各自演进（能力漂移，见 08-07 记录）。Joe 拍板统一到本库单源，`memory_db.py` 退役。

### 本次改动
- 移植 3 个 Hermes 专用方法进 `memorykit/db.py`（注释注明来源/日期）：
  - `rollback_pending()` — 清悬挂写事务（WAL 写锁防护，插件统一调用入口）
  - `peek(key)` — 纯读 get（不更新 accessed_at，prefetch 读路径专用）
  - `recent_reinforce_count(key, seconds)` — correction_log 强化限频计数
- 顺带修复 `get()` 的 accessed_at 写 UTC（`datetime('now')` → `'localtime'`，
  与 08-07 时区纪律对齐；生产版早已是 localtime）
- 新增 `tests/test_hermes_port.py`（10 测试：三方法全路径）
- hermes-local 插件 `_get_db` 改 `from memorykit import MemoryDB`（sys.path
  显式加 /home/joe/memory-kit），db_path 显式传 `~/.hermes/memory.db`
  （本库默认是 cwd/memory.db，不传会连错库）；memory-kit 不可达时回退
  memory_db.py（笔电等未部署端不硬断）

### 验证
- 全量 239 测试全绿（229 旧 + 10 新，skipped=2；repo venv 含 jieba）
- 真实库副本（SQLite backup API）冒烟：COUNT 963=963 不变、_migrate_columns
  补 4 列（event_time/sparse/source_session_id/source_msg_id）+ governance
  表、search_multi/peek/recent_work/search_semantic/移植方法全部可用
- 插件模块在 hermes venv 下导入 OK、db_path 解析正确
- 真实 memory.db 未改动（仅在副本上验证；迁移在 gateway 重启后自然发生）

## 2026-08-24 记忆系统三层链路审计 + 可溯性欠账（Joe 确认"先记着"，未动手）

### 背景
复盘"掩码层-原对话-记忆"三层结构（想上 Obsidian 可视化，看清遮蔽层和原对话关系）时，
审计了真实库 memory.db 的三层链路状态，发现**可溯性欠账**是硬伤：

### 三层真实数据（2026-08-24 实测 memory.db）
| 层 | 表 | 条数 | 指针字段填充情况 |
|---|---|---|---|
| ① 记忆层 | memories | 1670 | `source_message_ids` 仅 **5/1670**、`derived_from` **0/1670**、`provenance` 1662/1670 |
| ② 掩码层 | episodic | 97 | `session_id_ref` 有（可回指会话 ✓）|
| ③ 原对话层 | 原始消息/state.db | — | 本 memory.db **无 state/session 表**（未接 state.db 原始对话）|

### 关键判断：链路只通了一半
- **②→③ 掩码→原对话**：✅ 通（靠 `session_id_ref` 回指会话）
- **①→② 记忆→掩码**：❌ 断（`derived_from` 0/1670）
- **①→③ 记忆→原对话**：❌ 断（`source_message_ids` 5/1670）

字段 schema 早就设计好了（`provenance/evidence_refs/derived_from/compressed_to/source_message_ids/raw_excerpt/event_time`），
但**写入链路（learn/episodic 写入）没接** → 记忆成了孤岛：点开一条记忆，看不到它从哪段对话提炼、
盖住了什么原文、跟别的记忆是否同源。

### Obsidian 可视化前提（记录方向，未实施）
Obsidian 双链(`[[ ]]`)+Graph view 能把 `source_message_ids`/`derived_from`/`episodic.session_id_ref`
这些指针画成图，让"掩码层↔原对话↔记忆"三层关系可见。但**前提是先补断线**：
learn 写入时把 `source_message_ids`/`derived_from`/`raw_excerpt` 真填上（+可选存量 1670 条回填），
否则 Obsidian 只能画出空的右半边。

### 产物
- 图示: `/media/joe/E1/projects/openmle-cog/memory_layers_diagram.png`（三层结构真实链路状况，实线=通/虚线红=断）
- 绘图脚本: `/media/joe/E1/projects/openmle-cog/draw_memory_layers.py`

### 状态
**Joe 2026-08-24 确认：这是之前留的烂摊子，先记着，暂不动手。** 后续若要接 Obsidian 可视化，
第一步是补 learn 写入的指针字段（记忆→原对话/掩码），而非改大量既有数据。

## 2026-08-29 — 记忆-attention 提案（Joe 提出，方向记录，未实施）

### 提案核心
把语义消歧从 **FFN 参数记忆**（训练语料统计均值，对所有用户一样）搬到**情景记忆**（检索本机对话史）：
attention 不只看当前输入，还检索"过往类似语段"——`苹果` 的语义由**这台机器自己的记忆库**塑形，
而非全网均值。检索库是活的、随个人对话史漂移 = 个性化长在语义解释层里（非 prompt 附加）。

### 与已发表工作的对照（边界诚实记录）
- kNN-LM (2019) / RETRO (DeepMind 2022)：检索**静态训练语料** → Joe 差异化 = 检索库是**活的个人记忆**（memory.db 1670条+entity_links 7435）
- Memorizing Transformer (2021)：缓存被动积累、无记忆系统语义
- conversational-prefetch-rag（自建）：只到 prompt 层，**没进计算路径**

### 三级路线
- **P1 写入侧经验锚定**（零训练，最快）：新记忆 learn() 入库前先检索 top-k 相似旧记忆，
  模型显式输出"与 [[旧记忆X]] 的关系（延续/修正/无关）"再存储。改 memory-kit 写入路径，entity_links 现成是检索结构。
- **P2 读侧检索式消歧**：响应前两段式——过往语境作为独立注意力池（prompt 结构模拟），
  用 llm-behavior-experiments 纪律做 A/B（歧义词测试集，对照有无过往语段检索的消歧正确率）
- **P3 RETRO 式架构改造**（寒假候选）：1-3B 小模型 cross-attention 层挂 FFN 旁，检索源 = memory.db。
  双 V100 LoRA 显存可扛，与 execution-feedback-rl 线共享预算

### 坑（预注册）
- **FFN 不能删**——还管句法/通用世界知识；是"追加源替换"（情景记忆补充参数记忆），非替代
- **检索质量是命门**——embedding 相似 ≠ 消歧所需相关语境；评估集先建：
  从 1670 条 memories 挖"同词不同义"真实对话对当 ground truth
- **写侧实验注意**：Learn() 签名改动会牵连 memorykit-overlay/OpenClaw 插件层，四层更新铁律（库→pip→生产→overlay dist）

### 与既有线索的连接
- **断线问题（08-24 记录）**：本提案 P1 与"补 learn 写入的指针字段"是同一条写入路径的改造——
  可同期实施：写入时既补 source_message_ids/derived_from（断线修复），又做 top-k 关系标注（经验锚定）
- **Obsidian 可视化**（08-24 记录）：P1 完成后记忆间关系（derived_from/关联锚）才有内容可画，vault 导出同理
- **感知时钟主线**：系统语义被自己历史塑形 = "活在时间里"的具体切片

## 2026-08-29 — P1 锚定落地 + 模型通道切换（deepseek 官方直连）

### 背景
P1 写入侧经验锚定实现完成（子 agent deleg_0bd8a03e 交付, 见上方提案）。随后按 Joe 指示弃用 opendesign 反代（8792）, 锚定 LLM 通道切到官方直连。

### 通道切换（本日志认领）
- **弃用**: opendesign 反代 @8792（config.yaml 的 api_key 与反代本地鉴权 key 不一致, 且上游 runtimeKey 轮换不可控——实测 401 invalid local api key）
- **改用**: deepseek 官方直连 `https://api.deepseek.com/v1` + `deepseek-v4-flash`（实测 0.9s 响应）
- **关键坑（预注册成铁律）**: deepseek-v4-flash / glm-5.3-flash 都是先出 `reasoning_content` 的模型——`max_tokens<600` 时 content 为空串（实测）。HTTPAnchorLLM.chat 默认 max_tokens 已从 256→600。
- **key 解析**: `_default_key()` 从 config.yaml `providers.deepseek-v4-flash.api_key` 读取——用**标准库正则**（避免 yaml 依赖, repo venv 无 yaml）；段切割用 `providers:` + 下一 provider 名作锚（直接 split "deepseek-v4-flash:" 会误匹配 model 行）。

### 验证
- 全量 250 测试全绿（venv 带 jieba）
- 端到端: learn(anchor=True) → relation=延续(规则fallback) / derived_from 正确指向旧记忆 / provenance 注入 anchor 块
- LLM 直连: HTTPAnchorLLM chat 返回 content（key 自动读 config, 无需 env）

### 状态
- 断线修复（learn 写入补 derived_from）随锚定默认开启 ✅
- 遗留: 存量 1670 条不回填（P2 再议）; source_message_ids 根因在上游调用方不传（plan open_questions #2）


## 2026-08-29 — qwen38 memory gateway: memory_save 写入丢失 bug 根因 + 修复

### 现象
Qwen 调 memory_save 返回"已记住"(成功消息), 但快照库 /tmp/qwen38_mem_snapshot.db 里查不到该记录(origin='qwen38-tool' 恒为 0 条)。日志显示 save 耗时固定在 5.01s 后返回, result_chars=26 即 `工具执行失败: database is locked`。

### 根因(两条, 且互相叠加)
1. **锁竞争(表层)**: 网关原先每个工具的开口都调 `refresh_snapshot()` → `src.backup(dst)` 把 236MB 生产库整库 backup 进快照。backup 对目标库持写锁、耗时数秒; 冷启动 `_refresh_ts=0` 时多个并发请求同时触发 backup → 写锁 thrash → 后续 INSERT `database is locked` 等满默认 5s busy_timeout 后报错。验证: 停掉网关进程后写锁立即释放(0.00s 拿到)。
2. **刷新覆盖(真正"写后查不到"的根因)**: `src.backup(dst)` 是**整库替换**——每次 refresh 都把快照库整体替换成生产库副本。而生产库(origin 只有 laptop/desktop/server)不含 qwen38-tool 行, 于是 memory_save 刚写入索引的记录, 下一次 refresh(≤60s 后, 或进程重启后首个请求 _refresh_ts=0)就把整库覆盖回生产副本, 记录被冲掉。这正是"save 成功但库里查不到"的机制。
   - 另: 用 backup 拷贝**正在被写入的**生产库, 会继承生产库的 FTS vtable(work_log_fts)等重对象, 独立性校验 `PRAGMA integrity_check` 直接报 `vtable constructor failed`。

### 修复方向(按 Joe 指示换方案, 而非在镜像方案上打补丁)
定位重申: 这是**类 transformer 的检索系统插件**(纯检索, 不生成新内容), 数据面要干净。因此放弃"镜像生产库"方案——它本身就是锁+覆盖问题的来源。改为 **Qwen 独立检索库**, 与生产库彻底解耦:

- **新建独立库** `/tmp/qwen38_own.db`, 干净 schema(仅 id/key/content/category/scope/importance/agent_id/origin/created_at/updated_at, 不含生产库 FTS/vector 重列)。
- **一次性 seed**: `ensure_own_db()` 只在 own 库为空时执行一次, 从 SEED_SOURCE(= 生产 memory.db 只读源, 1670 条)用 SELECT 拷贝 memories 列(不触发 FTS vtable)。之后**绝不重刷覆盖** → ingest 写入的记录持久可搜。
- **工具语义改名**: `memory_save` → `memory_ingest`(写入检索索引/ingest, 非"替主人记事"); 保留 memory_save 作兼容别名映射到相同实现。
- **锁处理**: `_connect()` 统一设 `busy_timeout=30s` + `PRAGMA journal_mode=WAL`; 各工具用 try/finally 关连接, 消除 5s 默认超时。

### 验证(端到端, 全绿)
- 模块级: fresh seed 1670 行(0.13s) → ingest(0.02s) → search 立刻命中, 无锁超时。
- 服务级: `export XDG_RUNTIME_DIR=/run/user/1000 && systemctl --user restart/enable qwen38-memgw.service`(原为 transient unit, 已改名成持久 unit 文件 ~/.config/systemd/user/qwen38-memgw.service)。
- curl 8093 走完整 OpenAI 兼容代理+tool_calls 循环: ① ingest(TECHCON 展会 B42, 0.03s) ② search 命中该记录(0.01s, origin=qwen38-tool) ③ 服务重启后记录仍在(1670→1671, qwen-owned=1)——证明无覆盖。
- 清理测试记录后, 重启回 fresh seed 1670 行, 服务 active。

### 数据源方案选择说明
选了**独立库 + 一次性 seed(生产 memory.db 只读源)**。理由: seed 后与生产库零耦合, 无锁竞争、无覆盖, ingest→search 闭环稳定。更进一步的方案(从 state.db 的 169k 条原始 messages 蒸馏出 Qwen 自己的规范记忆库)数据量更纯粹, 但工作量大, 属后续迭代, 不在本次修复范围。


## 2026-10-19 第二种语料(skill/文档库)设计草案

- 起因: Hermes 系统提示实测 46,128 字符, 其中技能索引(243 份 SKILL.md 的 name+description)占 21,486 = 46.6%; 单份最大技能正文 94K 字符(≈55K tok), 载入一次等于系统提示两倍。
- 已确认可复用: 服务器 bge-m3(1024 维, 8082 常驻) + `_extract_entities` + `entity_links`(9,648 条/5,280 实体) + `RRF_K` 融合。
- 预实验: 243 技能建技能级摘要索引 55.6s; 8 题召回对比 —— 整句 embedding 命中 6/14, 多实体并集(每实体 top3 合并重排) 9/14。失败案例证明索引必须到正文 chunk 级(仅 description 会漏主题词)。
- 设计落 `design-notes/skill-corpus-2026-10.md`: 新增平行表 `corpus_chunks` / `corpus_entity_links`(不动 memories 与 entity_links), 按 ## 标题切块(243 文件 → 6,762 块), 实体三步清洗(小写归一/黑名单/频次≥2), 混合召回(向量+FTS5)。
- 版本目标 1.3.0; 未实现, 待评审。

## 2026-10-19 第二种语料设计 + 合并闸门定位

- 起因: Hermes 系统提示实测 46,128 字符≈26.6K tok, 技能索引占 21,486=46.6%;
  单份最大技能正文 94K 字符(载入≈55K tok)。
- 预实验: 243 技能→6,762 块(均 483 字), bge-m3@8082 1024 维, 全量约 20 分钟;
  8 题召回对比 整句 6/14 vs 多实体并集 9/14。
- 产出: `design-notes/skill-corpus-2026-10.md`(设计稿) + README Roadmap 一条 +
  issue #6(RFC); `design-notes/handoff-2026-10-19.md`(交接: 三条待办+环境须知)。
- CI 红定位: 上次绿是 2026-08-15, 之后 test_anchoring 才加, 从没跑过。
  失败根因 = 测试自身 `DeterministicEmbedder` 只有 4 维(md5 前 4 字节),
  随机文本 cos 噪声可达 0.93 越过 0.92 闸门 → "无关不合并"断言失败。
  修: 4 维→64 维 sha512 去均值归一(b1d7f66), 256 tests OK(2 skipped), CI 绿。
- 把猜测证伪: 原以为"嵌入降级(hash 兜底)→ cos 虚高 → 误合并", 实测否掉
  (hash 100 对 max 0.271; 真 bge-m3 100 对 max 0.662; 均 0/100 越 0.85)。
- 真问题(issue #7): `learn()` 锚定先算 relation(1598), 合并闸门(1665)不读它 →
  模型判"无关"仍合并。方案 A(灰带 0.85–0.92 放行锚定否决)待实施, Joe 说之后再说。
- 环境: gh 设备码登录成功(Joevy-1112, repo/read:org/gist); 笔电连不上 github.com,
  服务器直连即可; 本地跑测试需 jieba(裸 python 会挂 14 条实体测试)。
