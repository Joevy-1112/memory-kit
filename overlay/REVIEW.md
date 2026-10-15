# memorykit-overlay 代码评审

评审人：dsh（DeepSeek Harness）  日期：2026-10-15
评审对象：本目录（从 `/home/joe/memorykit-overlay` 复制，见下"副本与溯源"）
方式：只读通读 + 运行时探针（未改动任何线上文件、未写入正式 memory.db）

---

## 1. 源码在哪 / 哪份是权威

系统里同时存在 **三份** memorykit-overlay，用途不同，不是简单拷贝：

| 路径 | 角色 | 关键特征 |
|---|---|---|
| `/home/joe/memory-kit/overlay/` | **上游原码（权威）** | 在 memory-kit git 仓库内，提交 `6a6446e "add overlay/ 子库"`；`src/index.ts` 25134B（08-15 17:00），带 `dist/` 构建产物 |
| `/home/joe/memorykit-overlay/` | **活的工作副本** | 生产实际引用的那份：`bridge.py` 今天 17:15 改过（agent 身份开关）；多了 `mcp_memory_server.py`(08-17)、`dsh-memory.cordis.yml`、`README.md`(08-21 更新)；**没有 `dist/`**，只有 `dist_bak_0811/` |
| `/home/joe/.openclaw/extensions/memorykit-overlay/` | **部署副本（已停用）** | `src/index.ts` 22423B（08-07，比上游少 61 行差异）、`dist/index.js` 08-15；`openclaw.json` 里 `enabled: false` |

判定依据：
- 上游 `src/index.ts` 与工作副本 **完全一致**（`diff -q` 无输出），哈希 `7b5fad6b…`。
- 部署副本的 `src` 落后于它自己的 `dist`（src 08-07 / dist 08-15）——源码与产物不同步。
- DSH 侧走的是**工作副本**：`~/.dsh/cordis.patch.yml` → `mcp_memory_server.py`；OpenClaw 侧 TS 走 `.openclaw/extensions/` 的 `dist/index.js`。

**本次复制**：以工作副本为准（生产引用的那份），排除 `node_modules/`、`__pycache__/`、`dist_bak_0811/`、`_tmp_pr-*.txt`，并从上游补入 `dist/`（该 dist 与最新 src 同批构建）。共 21+ 文件 / 378K。

关键文件哈希（sha256，前 16 位）：

| 文件 | 行数 | sha256 |
|---|---|---|
| `src/index.ts` | 512 | `7b5fad6bcb463b4e` |
| `bridge.py` | 373 | `16f6c1b8022fa0ed` |
| `mcp_memory_server.py` | 207 | `39974d1cc3e1b518` |
| `dist/index.js` | 449 | `d4740fa327405b7a` |

---

## 2. 架构

```
┌─ OpenClaw 侧（已停用）───────────────────────────────┐
│  TS 插件 dist/index.js  (defineToolPlugin)          │
│    ├─ 10 个工具 mk_*  ──┐                            │
│    └─ 2 个自动 hook     │                            │
│       before_prompt_build → 注入相关记忆             │
│       agent_end           → 自动 learn + 心跳        │
│    解释器硬编码 /home/joe/venv-flagembed/bin/python3  │
└──────────────────────┬───────────────────────────────┘
                       │  execFile(python, bridge.py, cmd, json)
┌─ DSH 侧（在用）───────┴───────────────────────────────┐
│  mcp_memory_server.py  手写 stdio JSON-RPC MCP        │
│    └─ 7 个工具 search/learn/set/correct/reinforce/    │
│       verify/recall    （无 hook，纯被动）             │
│    解释器 sys.executable（= 裸 python3）              │
└──────────────────────┬───────────────────────────────┘
                       ▼
                 bridge.py  (命令分发，16 个子命令)
                       ▼
        memory-kit MemoryDB  (memorykit 1.2.0)
          ├─ memory.db   密集记忆层（1990 条，WAL）
          └─ state.db    原文档案（3.2GB，永不删）
```

设计意图（README）：**memory 是 state 的影子**，state 原文不可变，memory 可验证/纠正/遗忘，靠 `source_session` 指针从 memory 回溯原文，最终用"密集片段 + 指针"替代膨胀的上下文窗口。

---

## 3. 值得肯定的

1. **进程边界干净，无注入面**。TS 侧用 `execFile`（非 shell）传 argv，bridge 用 `argv[2]` 收 JSON；`cmd_*` 只做参数取值。没有拼接 shell 字符串。
2. **故障绝不拖垮 agent**。两个 hook 全包 try/catch，失败静默返回；`before_prompt_build` 超时收到 3s、检索 2.5s；还加了 30s/8 条的 prompt 检索缓存 + FNV-1a 幂等窗口。
3. **检索结果裁剪有实测依据**。`_MEM_FIELDS` 白名单 + `cmd_get` 丢掉 `vector/vector_bin/sparse`，注释写明"全量 dump 一条 20KB+ 纯烧 token（小龙虾实测 08-07）"。
4. **并发考虑到了**。`db.py:314-316` 设了 `PRAGMA busy_timeout=15000` + `journal_mode=WAL`，多进程 execFile 并发不会轻易 `database is locked`。
5. **重计算外置**。嵌入走本地 HTTP 服务（`127.0.0.1:8082`，bge-m3 GGUF，实测 `/v1/embeddings` 200）；所以每次 spawn Python 的固定开销只有 ~0.28s（`import memorykit` 0.22s），"每调用一进程"这个看似昂贵的架构实际可接受。
6. **注释和文档是资产**。TS 头部长注释记录了 hook 注册 API 的踩坑（`api.on` vs `api.registerHook`、`allowConversationAccess`）、事故号（`t_2c164107`）、校准结论；README 有 benchmark 表格 + "已知问题/待办"清单，自省文化明显。
7. **TS 测试质量不错**。`src/index.test.ts` 229 行：纯函数单测（质量门槛/词法重叠/去重哈希正反用例）+ 真 bridge + 临时库的端到端（自动 learn 落库、幂等、注入、不注入）。

---

## 4. 问题清单

### P0-A　DSH 路径实体抽取全灭（实测，一行可修）

`mcp_memory_server.py:15` `PY = os.environ.get("MEMORYKIT_PY", sys.executable)`，而 dsh 用裸 `python3` 拉起它 → `sys.executable = /usr/bin/python3`（**无 jieba**）。
memorykit 的 `_extract_entities` 在 jieba import 失败时**静默降级**（`db.py:1982`）。

端到端实测（同一句中文，同一份 `mcp_memory_server.py`，只换解释器）：

| 解释器 | 落库 `entities` |
|---|---|
| `/usr/bin/python3`（当前 DSH 实际路径） | **`None`** |
| `/home/joe/venv-flagembed/bin/python3` | `["小鸡翅","银渐层","粘人"]` |

`entities=None` 意味着 `_update_entity_links` 无输入 → **实体图 / 实体加权检索对 dsh 写入完全失效**，且不报错。

讽刺点：README 第 171 行早已把这条列为"**关键坑（重要）**"，但当年的解药只打在 OpenClaw TS 上；08-17 新增的 DSH MCP server 用 `sys.executable` **原样又踩了一遍**。

修复（无需改代码）——`~/.dsh/cordis.patch.yml` 的 `memory-memorykit.env` 加一行：

```yaml
          MEMORYKIT_PY: /home/joe/venv-flagembed/bin/python3
```

### P0-B　MCP 面能力缺 9 项，`recall` 名不副实

bridge 有 **16** 个子命令，MCP 只暴露 **7** 个工具。缺的包括**同名核心 `overlay`**（resolve 遮蔽解析 / expand 回溯 state 原文）、`episodic`（时间索引）、`knowledge`（知识层）、`stats`、`get`、`search_state`、`verify_stats`、`verify_batch`、`cleanup`、`age`。

更糟的是 `mcp_memory_server.py:138-139`：

```python
elif name == "recall":
    r = call_bridge("search", {"query": q, "limit": limit})
```

工具自述"检索最近发生的事件/时间线(episodic)"，实现却是普通语义检索；bridge 里真正的 `episodic` 命令**从未被任何 MCP 工具调用**。调用方（我）会据此产生错误预期。

### P0-C　MCP 侧把 bridge 错误吞成空结果

`handle_tool` 对 search/recall 走 `format_hits(r, …)`；bridge 返回 `{"ok": false, "error": …}` 时 `r.get("hits")` 为 None → 返回 `{"hits": []}`。调用方看到的是"**没搜到**"，而非"**桥挂了**"。
（实测依据：我在沙箱里直调 `bridge.py` 时，写路径报 `attempt to write a readonly database`，`overlay`/`episodic` 正是这么被吞掉的。）

### P1-D　`agent_id` 归属靠事后补丁 + 裸 except

bridge `cmd_learn`（`bridge.py:82-88`）：

```python
if agent != "hermes" and isinstance(r, dict) and r.get("key"):
    try:
        with db._tx():
            db._conn.execute("UPDATE memories SET agent_id=? WHERE key=?", (agent, r["key"]))
    except Exception:
        pass          # ← 归属失败被静默吞掉，条目就留在 hermes
```

这是拿私有 API（`db._tx` / `db._conn`）从外部打补丁。根因在 memorykit：`lifecycle.py:1153` 的 `learn()` **没有 agent_id 形参**，内部 `set()` 写死 `agent_id='hermes'`（bridge 自己的注释也承认 `lifecycle.py L926`）。

正确修法：给 `learn()` 加 `agent_id` 形参并透传，overlay 只需传参——补丁和裸 except 一起消失。这属于 memorykit 上游改动（AGPL 自有项目，可改）。

### P1-E　跑的是未提交的脏工作树，不可复现

- `bridge.py:16` 硬编码 `sys.path.insert(0, "/home/joe/memory-kit")` → 运行时实际 import 的是 `memory-kit/memorykit/__init__.py`（**1.2.0**），不是 site-packages 里那份（**1.0.0**）。
- 而该 checkout 处于脏状态：`M memorykit/__init__.py`、`M db.py`、`M lifecycle.py`、`?? memorykit/anchoring.py`、`?? tests/test_anchoring.py`。
- overlay 子库的提交 `6a6446e` 也**没有锁定**它依赖的 memorykit 版本/提交。

结论：线上行为 ≠ 任何已提交状态。换台机器或 `git stash` 一下，行为就变。

### P1-F　两个前端分配不一致，且"自动接线"在 DSH 侧根本不存在

| | 解释器 | hook |
|---|---|---|
| OpenClaw TS | 硬编码 venv-flagembed（有 jieba） | 有（自动注入 + 自动 learn） |
| DSH MCP | `sys.executable` 裸 python3（无 jieba） | **一个都没有** |

同一个插件两套行为，正好是 P0-A 的来源。另外 `~/.openclaw/openclaw.json` 里 `memorykit-overlay.enabled = false`（08-15 起）——**两侧现在都没有自动接线**，全靠调用方显式调工具。README 第 223-224 行对此是诚实的，但"自动 prefetch / 自动沉淀"这套设计目前处于纯离线状态。

### P2-G　`new URL(...).pathname` 的路径编码坑

`src/index.ts:15`：`new URL("./bridge.py", import.meta.url).pathname`。路径含空格或非 ASCII 会留下 `%20`/百分号编码，execFile 直接找不到文件。应使用 `fileURLToPath(new URL(...))`。（当前路径 `/home/joe/memorykit-overlay/` 侥幸没踩到。）

### P2-H　靠 monkey-patch 挂 hook

`src/index.ts:506-510`：

```ts
const _origRegister = plugin.register;
plugin.register = (api) => { _origRegister(api); registerHooks(api); };
```

能跑，但（a）`register` 若被宿主调用两次，hook 会重复注册、每轮注入两次；（b）SDK 若改成绑定/包装 `register`，hook 会**静默消失**且测试看不出来（测试用的是自建 fake api 直接调 `entry.register`）。

### P2-I　hook 超时预算不自洽

`agent_end` 的 hook 超时 8000ms，但 handler 内部先 `episodic checkin`（5000ms 预算）再 `learn`（8000ms 预算）——最坏 13s 明显超预算，learn 可能在提交前被杀。要么拆成两个 hook，要么统一预算。

### P2-J　"自动 learn"学的是用户原话（流水账）

`handleAgentEnd` 只取 `lastUserMessage` 并 `slice(0, 500)`——存的是用户最后那句话，不是助手提炼出的结论。这跟 `mk_learn` 自己的工具描述"要记住的内容（一句话干货，不要流水账）"直接矛盾。
库里现状与之吻合：**1990 条中 60.6% 仍 pending 验证，平均验证分 0.2531，1892 条 confidence < 0.3**。自动 learn 把闲聊原话灌进了库里（虽然质量门槛拦掉了短句/问候，但拦不住"长而无信息量"）。

### P2-K　bridge / MCP server 零测试

TS 有 229 行测试，`bridge.py`(373 行) 和 `mcp_memory_server.py`(207 行) **一行测试都没有**——而 P0-A/B/C 三个问题全部落在这两个文件里。
另外 TS 的 e2e 测试（`index.test.ts:191`）用 `execFileSync("python3", …)` 即**系统 python3**，而生产 TS 用 venv python3 —— 测试解释器与生产解释器不一致，恰好把 P0-A 这类洞漏过去。

---

## 5. 结论

| 维度 | 评价 |
|---|---|
| 架构选型 | **B+**　"memory 影子 + state 原文 + 指针回溯"的思路清晰；进程边界与重计算外置的选择是对的 |
| 代码质量 | **B**　TS 侧结构清楚、注释即设计文档；bridge/MCP 侧偏脚本化，存在私有 API 补丁与静默吞错 |
| 健壮性/可观测 | **C**　三处静默降级（实体抽取、bridge 错误、归属失败），都表现为"看起来正常" |
| 可复现性 | **C-**　跑在脏工作树上，依赖版本未锁 |
| 文档/自省 | **A-**　README + project-log 有 benchmark、已知问题与实测结论，罕见地诚实 |

**一句话**：设计是站得住的，工程质量中等偏上，但"**静默降级**"是这个项目最大的系统性风险——同一个 jieba 坑在 README 里被记录过一次，四个月后换了个前端又踩了一次，而且两次都不报错。

**最高性价比的三步**（都不动插件代码）：
1. `~/.dsh/cordis.patch.yml` 加 `MEMORYKIT_PY: /home/joe/venv-flagembed/bin/python3`（修 P0-A）。
2. 给 `mcp_memory_server.py` 的 `handle_tool` 加 `if not r.get("ok"): return {isError…}`（修 P0-C）。
3. 让两个执行入口在启动时做一次 **jieba/嵌入服务自检**并在缺失时 `logger.warn`，把静默降级变成显式告警（治本）。
