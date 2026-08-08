# memory-kit 自审报告 (2026-08-08, Hermes 内部审查)

> 审查对象: README.md 全量 + project-log.md 08-08 + design-notes/scope-recall-borrowing.md
> 审查者: Hermes(服务器侧 agent, 兼实现者——所以这篇对自己下手不留情)
> 方法: gpt-review skill 的强任务标准(AI 味/最弱环节/工程严谨/借鉴诚实/accept 判断)

---

## P0 — 必须修: 向量维度混合导致 search 崩溃

**问题**: `LocalHashEmbedder` 默认 **256 维**, bge-m3 是 **1024 维**。
场景: 嵌入服务挂掉期间 `learn()` 用 local-hash 存了 256 维向量 → 服务恢复后
`search()` 用 bge-m3 1024 维查询向量 → `search.py:415 np.dot(vecs_np, qvec_np)`
直接 `ValueError: shapes (2,256) and (1024,) not aligned` → **语义检索崩溃**。

已实测复现(np.dot 维度不匹配异常)。本地哈希兜底的本意是"挂了也还能检索",
但维度不一致让"恢复后"变成"检索必崩"——兜底引入了比故障更糟的状态。

**修复**: LocalHashEmbedder 默认维度对齐 bge-m3(1024)。同维后 cos 计算合法,
语义弱但可用, 兜底才成立。同时文档标注"local-hash 必须与主嵌入器同维"。

**教训**: 测试只验证了"降级时能检索"(test_08), 没验证"降级→恢复"的混合场景。
审查 checklist 应该加: *兜底实现与主路径的维度/格式契约是否一致?*

---

## P1 — 应该修, 影响可信度

1. **freshness 不是主动状态机**。design-notes 写的是"状态机 + 排序惩罚 + 报表"
   三层, 实际只落了"verify() 调用时检查"——没有自动过期扫描/调度入口。
   文档里"过期 → expired"的语义需要明确: 只有有人调 verify()/verify_batch()
   才发生。README 没写这个限制, 读者会以为过期是自动的。

2. **provenance JSON 高频解析开销**。search_multi 每条结果调 `_freshness_penalty`
   → 每次 `json.loads(provenance)`。数据库行数多了之后(万级), 这个解析在
   检索热路径上不是免费的。设计时选了"零新表", 代价是每行解析——文档没
   讨论这个权衡, 也没给未来优化方向(如冗余列/缓存)。

3. **测试套件常年 1 failure**。test_learn_batch 的 live llama.cpp 确定性断言
   挂了很久(至少 3 个提交周期), 每个 project-log 都写"既有环境项, 与本次
   无关"。这个说法技术上对, 但**常年红色的测试套件会让后续的"全绿"失去
   说服力**。应该: mock 服务端返回固定向量, 或者放宽断言(同文本批量 vs
   单条的 cos 容差), 把套件做回全绿。

4. **"闭环"和"护城河"用词泛滥**。README/design-notes 里"correction 闭环"
   "验证闭环""护城河"出现 8+ 次。第一次出现是概念, 第八次是自我说服。
   文档应该第一次定义清楚, 之后用具体行为词(如"correction 会降权并留痕")
   代替。

---

## P2 — 加分项/观察

5. **reconstruct() 新增了 state.db 耦合点**。README 自己标注过 verify_recent
   "遗留耦合", 指针链又加一个(虽然"不承诺自动重建"缓解了)。长期看应该有
   注入式 state 访问接口, 而不是硬读路径。

6. **借鉴标注诚实性: design-notes 满分, README 主体不及格**。design-notes
   把每个借鉴点写清了(它怎么做/我们缺什么/怎么并进), 借用纪律也明确;
   但 README 主体(快速开始/核心设计)完全没有 attribution——外部读者不知道
   哪些是自研、哪些借鉴了 scope-recall。README 顶部应加一行明确的
   attribution("设计受 scope-recall(MIT) 的 freshness/journal 契约启发, 实现独立")。

7. **版本节奏**: 一天 4 个提交、每个都带独立测试+文档, 节奏是好的; 但
   README 的 08-08 追加章节已经 3 个(还在涨), 顶部索引/更新日志列表没有——
   新读者进来找不到"最近发生了什么"的入口。

---

## 一句话总结

架构思考是真实的(指针方案替代 journal、零新表 freshness、correction 闭环
护城河都站得住), 但**"实现细节的契约一致性"(P0)和"文档的克制"是短板**——
P0 修掉之前, 这个库不能算"挂了能兜底", 只能算"挂了会换一种方式炸"。

## 自审 vs GPT 外审

yamii 后端 503, 改为内部自审。差异: GPT 没有"我写的所以我偏爱"的偏差, 但
也没有执行验证能力(P0 是实测抓出来的)。建议 yamii 恢复后仍跑一轮外审,
对照本报告看哪些是共同点(共同点 = 真问题)。
