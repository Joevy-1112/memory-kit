# P1 写入侧经验锚定 — 多agent编排方案 (p1_orchestration_plan)

日期: 2026-08-29
PM: self (subagent, opendesign/deepseek-v4-flash-vision-exp)
仓库: /home/joe/memory-kit (memorykit 1.2.0, SQLite 记忆系统, 239 测试绿)

---

## 1. 阶段一产出: 可用模型盘点 (实测 2026-08-29)

| 通道 | base_url | 模型 | 实测延迟 | 能力 | 成本 | 结论 |
|---|---|---|---|---|---|---|
| 本地 llama.cpp | http://127.0.0.1:8091/v1 | Qwen3.8-27B (Q4_K_M) | ~2-3s | 35tok/s / 64K ctx | 免费 | ⚠️ 实测返回空 content，结构化 JSON 不可靠 → 不用于锚定 |
| opendesign 反代 | http://127.0.0.1:8792/v1 | deepseek-v4-flash | 2.3-3.8s | 9 个免费模型,中文强 | 免费 | ✅ **首选**：干净 JSON，本地零成本 |
| opendesign 反代 | 同上 | glm-5.1 / 5.2 / kimi-k2.x / minimax | 2.7-3.6s | 推理模型需更大max_tokens | 免费 | 可用但多为 reasoning，需额外解析 |
| zai 直连 | open.bigmodel.cn/api/paas/v4 | glm-5.3-flash | 2.4s | (key在config.yaml) | 按量 | 可用,空content(疑似reasoning),需验证 |
| deepseek 直连 | api.deepseek.com | deepseek-v4-flash | **0.9s** | 中文强,JSON准 | 按量 | ✅ 备用：最快最准 |
| orchestrator | http://127.0.0.1:8765 | — | — | 7 角色/pm/architect/ui/coder/reviewer/summarizer/researcher | — | 健康，roles=全 deepseek-v4-flash |

**锚定模型选定**: `deepseek-v4-flash`，经 opendesign 反代 @8792（本地免费、零成本、稳定）。可经 env `MEMORY_ANCHOR_LLM_URL/KEY/MODEL` 覆盖到任意 OpenAI 兼容端点。

---

## 2. 阶段二产出: P1 任务需求调研

### 提案原文 (project-log.md 2026-08-29 末条, P1 段)
> 新记忆 learn() 入库前先检索 top-k 相似旧记忆，模型显式输出"与 [[旧记忆X]] 的关系（延续/修正/无关）"再存储。改 memory-kit 写入路径，entity_links 现成是检索结构。
> 可同期实施：写入时既补 source_message_ids/derived_from（断线修复），又做 top-k 关系标注（经验锚定）。

### 08-24 断线审计数据
| 字段 | 填充率 |
|---|---|
| source_message_ids | 5/1670 |
| derived_from | **0/1670** |

### 关键函数定位
- `learn()` → `memorykit/lifecycle.py:1151` (MemoryLifecycle mixin)。签名已带 `source_session_id/source_msg_id/source_message_ids/raw_excerpt`(P0#1 + 遮蔽机制)。流程: 校验→_anchor_time→评分分类→tags→importance→**嵌入(dense+sparse)**→实体抽取→**向量去重(要≥0.85 fusion)>merge**,否则**实体去重(≥3重叠)>merge**,否则**set() 新建**。
- `set()` → `memorykit/db.py:563`。INSERT 含 source_session_id/source_msg_id/source_message_ids/raw_excerpt。**不写 derived_from**。
- 检索接口 → `MemorySearch.search_multi` (`memorykit/search.py:305`)，四信号融合，返回 list[dict](含 key/content/similarity)。entity_links 表在 schema 内。
- merge 两分支的 UPDATE(向量 line~1616 / 实体 line~1716) 都**不写 derived_from**。

### 需改与牵连
1. 新增 `memorykit/anchoring.py`（锚定分类）。
2. `learn()` 加 `anchor/anchor_llm/anchor_top_k` 参数；在嵌入后、去重/新建前做 top-k 检索 + 关系分类；写到 provenance + derived_from。
3. `set()` 加 `derived_from` 参数（透传写入，默认 NULL 向后兼容）。
4. learn() 两处 merge UPDATE 补 `derived_from=old_key`（**断线修复默认开启**）。
5. 调用方: `db.py:2593` (CLI), `lifecycle.py:1836/1917` (learn_batch/recurrent), `gate.py` → 全为默认参数调用，加新 kwarg 向后兼容不受影响。overlay/OpenClaw 四层更新铁律见 open_questions。

### 测试策略
- 默认行为不变 → 现有 239 测试 (skipped=2) 必须全绿。
- 新增 `tests/test_anchoring.py`：用 DeterministicEmbedder + 假 LLM。

---

## 3. 阶段三产出: 多agent编排方案

| 角色 | 模型 | 步骤 | 产物 |
|---|---|---|---|
| **PM** | opendesign/deepseek-v4-flash | 盘点模型 + 需求调研 + 定方案 + 门禁验收 | 本 plan |
| **Architect** | deepseek-v4-flash | 定设计(见 §4)：模块边界/签名/降级链 | 方案 §4 |
| **Coder** | deepseek-v4-flash | 改 anchoring.py / lifecycle.py / db.py / 新测试 | diff |
| **Reviewer** | deepseek-v4-flash | 只读审查：默认路径是否回归、降级是否安静、指针是否准确 | must_fix 清单 |
| **Summarizer** | deepseek-v4-flash | 写 project-log 记录 | log 条目 |

### ⚠️ 关键发现 (影响默认配置)
- `opendesign @8792` 反代 **需要 key**（实测 401 invalid local api key；key 见 config.yaml `providers.opendesign.api_key`）。默认 HTTPAnchorLLM key="" 会认证失败 → 自动降级到规则 fallback（这是**有意设计的安全网**，绝不阻塞写入）。
- 要真正用 LLM 锚定，必须设 `MEMORY_ANCHOR_LLM_KEY`（指向 opendesign key 或任何 OpenAI 兼容端点）。

### 执行结果 (2026-08-29 P1 已完成)
- **测试**: `run_tests.py` = **250 tests OK (skipped=2)** — 239 基线全绿 + 11 新增锚定测试，零回归（~78s）。
- **真实 LLM 端到端验证**: 配 key 后 learn(anchor=True) 走 deepseek-v4-flash，模型输出
  `{"relation":"修正","reason":"价格由550块更新为650块，显存保持不变。"}`，derived_from 正确指向旧记忆。无 key 时静默降级为 rule fallback（仍写库、仍定 derived_from）。
- **改动文件**:
  - 新增 `memorykit/anchoring.py`（锚定分类器 + HTTPAnchorLLM + 规则 fallback）
  - 新增 `tests/test_anchoring.py`（11 测试）
  - 改 `memorykit/lifecycle.py`（learn() 加 anchor/anchor_llm/anchor_top_k + 两处 merge UPDATE 补 derived_from + provenance 注入 anchor）
  - 改 `memorykit/db.py`（set() 加 derived_from 参数透传 + COALESCE）
  - 改 `memorykit/__init__.py`（导出锚定 API）
  - 本 `p1_orchestration_plan.md`
- **门禁判定**: Reviewer 自审通过（默认路径零改动 / LLM失败安静降级 / 指针准确 / 范围收敛无蔓延）。

### 流程 (串行, 2 步执行)
1. Coder 实现 → 跑 `./.venv/bin/python run_tests.py` 全绿。
2. Reviewer 只读复核(重点: 默认行为不变/LLM失败降级/derived_from正确) → 通过后 Summarizer 记 log。

### 门禁
- `run_tests.py` = OK (skipped=2) 是唯一硬门禁。
- Reviewer 只碰只读; Coder 不扩展范围(engineering-minimal-change-engineer 纪律)。

---

## 4. 设计决策 (Architect 拍板, 供 Coder 执行)

### 4.1 锚定分类器抽象 (anchoring.py)
- 受控关系词表 `{"延续","修正","无关"}`。
- `classify_relation(new_content, candidates, llm=None) -> dict`：
  - candidates = [ {key,id,content,similarity}, ... ] (top-k)。
  - llm 可调用 → 手写 prompt，要求输出 `{"relation":"延续|修正|无关","anchored_key":"<key>","reason":"..."}`。
  - llm=None/失败 → **纯规则 fallback**（实体重叠 + 相似度阈值）→ 安静降级，绝不阻塞。
  - 返回 `{relation, anchored_key, anchored_id, reason, source:"llm"|"rule", model}`。
- `HTTPAnchorLLM` openai 兼容客户端，读 `MEMORY_ANCHOR_LLM_URL/KEY/MODEL`(默认 @8792/deepseek-v4-flash)，15s 超时，temperature 0，任何异常返回 None。

### 4.2 learn() 扩展
- 新参数: `anchor=False`, `anchor_llm=None`, `anchor_top_k=3`。
- anchor=True 且 qvec 在线：`search_multi(content, limit=anchor_top_k)` → `classify_relation` → 注入 provenance `prov["anchor"]`；relation∈{"延续","修正"} 时设 `derived_from=anchored_key`。
- anchor 默认 False → 行为与旧版逐字节一致（239 测试不受影响）。
- LLM 异常被 classify_relation 吞掉 → 走规则 fallback → 仍写入。

### 4.3 断线修复 (默认开启, 非可选)
- learn() 两处 merge UPDATE 一律加 `derived_from=old_key`（合并内容派生自被折叠的旧记忆）。
- set() 加 `derived_from` 参数, COALESCE 不写空（同 source_session_id 语义）。
- 这直接提升 08-24 审计的 0/1670 → 合并/锚定记忆开始有指针。

### 4.4 不做的 (明确出圈)
- 不做存量 1670 条回填（需单独迁移脚本，P2 再议）。
- 不改 learn() 默认签名（只加 kwarg）。
- 不改 search_multi 检索算法。
- 不接 state.db 自动补 source_message_ids（上游调用层职责，见 open_questions）。

---

## 5. Open Questions (PM 标明，不瞎猜)
1. **四层更新铁律**: 库→pip→生产→overlay dist。本 P1 只改源码+测试，**不重新打包 pip / 不动生产插件层**（那超出本任务范围，且会牵连 hermes-local/overlay/OpenClaw）。已批准提案未强制本任务推全链路。
2. **source_message_ids 断线**: 这是"上游调用方不传"造成的（learn 已透传写）。本库内无法根本修复（需 state.db 派生上下文）。方案：P1 只修 derived_from + 锚定；source_message_ids 的上游补齐单独立项。
3. **锚定放 learn() 内 vs 独立 hook**: 选择放 learn() 内(opt-in)。代价是 learn() 多一个 LLM 调用(仅 anchor=True 时)；默认路径零开销。
4. **LLM 阈值**: 关系词表固定三值；"修正"时还应有边界——若搜索引擎把"无关"误判为"修正"或反之，靠评估集治（P2 读侧评估集已预注册）。P1 只保证写出标注，不断言准确率。
5. **reviewer 是否实际独立跑**: 本 subagent 单实例执行，reviewer 门禁以"我自审 + 全量测试"落地；如父 agent 可派独立 reviewer 实例，按 §3 流程补跑。
