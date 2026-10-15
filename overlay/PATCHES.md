# PATCHES — memorykit-overlay 修补版（dsh, 2026-10-15）

依据 [REVIEW.md](REVIEW.md) 的问题清单做的一版修补。**只改了工作区这份副本**，
线上 `/home/joe/memorykit-overlay` 与 `~/.memory-kit/overlay` 一个字节都没动。

- 原始哈希（用于回滚核对）：`src/index.ts 7b5fad6b…` / `bridge.py 16f6c1b8…` / `mcp_memory_server.py 39974d1c…`
- 完整 diff：[patches.diff](patches.diff)（604 行，+365 / −69）
- 改动量：`mcp_memory_server.py` 306 行、`src/index.ts` 49 行、`bridge.py` 33 行

---

## 1. P0-A　解释器缺 jieba → 实体抽取静默全灭

**改** `mcp_memory_server.py`

| 之前 | 之后 |
|---|---|
| `PY = os.environ.get("MEMORYKIT_PY", sys.executable)` | `pick_python()`：按 `MEMORYKIT_PY` → `/home/joe/venv-flagembed/bin/python3` 顺序**探测 jieba**，挑第一个能用的 |
| 全都没有 → 静默降级 | 全都没有 → 退回 `sys.executable` 并往 **stderr** 打 `WARN` + 一行修复配置 |
| 无自检 | 启动时 `check_embed_service()` 探嵌入服务，不可达也告警 |

`MEMORYKIT_SKIP_PY_PROBE=1` 可强制走降级路径（测试用），降级时同样告警。

**证据**（`tests/test_mcp_server_patched.py` T3–T5）：

```
T3 PASS 挑选结果写进 stderr 且 jieba=yes / 挑中的不是裸 python3
T4 PASS 降级路径打印 WARN + 修复建议 / jieba=NO
T5 PASS entities 非空（原实现是 NULL）/ 抽到中文专名 小鸡翅
```

T5 是端到端：经 MCP `learn` 写入「我的猫叫小鸡翅，是只银渐层…」，落库 `entities` 从 **NULL** 变成
`["小鸡翅","银渐层","粘人",…]`。

---

## 2. P0-B　工具面 7/16，且 `recall` 名不副实

**改** `mcp_memory_server.py`：工具 7 → **14**，覆盖 bridge 16 个子命令中的 14 个。

| 新增工具 | 对应 bridge 命令 |
|---|---|
| `get` | get |
| `verify_stats` | verify_stats |
| `recall`（修真） | **episodic**（原来转发 search） |
| `episodic` | episodic（start/end/checkin/close/window/list/search） |
| `overlay` | overlay（resolve/expand —— 同名核心能力） |
| `search_state` | search_state（memory → state.db 原文回溯） |
| `knowledge` | knowledge（promote/search/list/graph） |
| `stats` | stats |

未暴露：`cleanup` / `age`（运维命令，不该给模型）。`verify` 增加 `batch=true` 分支转发 `verify_batch`，
与 OpenClaw TS 侧 `mk_verify` 行为对齐。

`recall` 语义修正：带 `query` → `episodic action=search`；不带 → `action=list`（最近事件）。
返回结构从 `{hits:[…]}` 变成 `{episodes:[{key,title,summary,started_at,ended_at,…}]}`。

**顺带修掉**：`call_bridge` 原来在 `returncode != 0` 时**丢弃 stdout 里的 JSON**，只留 stderr
（bridge 约定退出码 2 = 业务失败但仍打印 JSON），错误原因就这么丢了。现在先尝试解析 stdout。

**一处有意的行为变化**：`learn` 不再硬编码 `source: "dsh"`，改为「不传就让 bridge 按
`MEMORYKIT_AGENT_ID` 决定」。dsh 实际接线（`AGENT_ID=dsh`）下取值不变；没设该变量的部署
会落到 bridge 的默认 `task` —— 比硬编码成 `dsh` 更准确。

---

## 3. P0-C　bridge 错误被吞成空结果

**改** `mcp_memory_server.py`：新增 `_error()`，`handle_tool` 里任何 `resp["ok"] is False`
都以 `{isError: true, content:[错误 JSON]}` 上报，不再返回 `{"hits": []}`。

顺带把 if/elif 链改成 `TOOL_IMPL` 表驱动（`工具 → (bridge 命令, 参数映射, 输出形态)`），
新增工具只需加一行，避免再出现「bridge 有命令、MCP 没工具」的漂移。

**证据** T7：`MEMORYKIT_DB=/etc/hostname/mem.db`（sqlite 打不开）和 bridge 路径不存在两种失败，
`search` 都返回 `isError`，并带上 bridge 的原始原因。

---

## 4. P1-D　`agent_id` 归属失败不再静默

**改** `bridge.py`：`cmd_learn` 的回写从

```python
try:
    with db._tx(): db._conn.execute("UPDATE memories SET agent_id=? WHERE key=?", (agent, r["key"]))
except Exception:
    pass                      # ← 失败就留在 hermes 名下，外面完全看不出来
```

改成 `_attribute(db, key, agent)`：**回写 + 回读校验**，返回 `"ok"` 或失败原因；失败时
（a）往 stderr 打 WARN，（b）在 learn 回执里带 `agent_attribution` 字段。
MCP 侧把它透出给调用方。

**证据** T5：回执 `agent_attribution == "ok"`（`learn` 那条断言最初就是因为没接上修补版 bridge 而失败的）。

> 根因仍在 memorykit：`learn()` 没有 `agent_id` 形参、内部 `set()` 写死 `'hermes'`
> （`lifecycle.py:926`）。正解是给 `learn()` 加参数透传，那属于 `~/memory-kit` 的改动，
> **本次没动**（见第 7 节）。

---

## 5. P2-G/H/I　TS 侧三处

**改** `src/index.ts`

| 问题 | 改法 |
|---|---|
| P2-G `new URL(...).pathname` 遇空格/非 ASCII 留下 `%20` | 改用 `fileURLToPath(new URL(...))` |
| P1-F 解释器硬编码在调用点，与 MCP 侧不一致 | 提成常量 `PYTHON = process.env.MEMORYKIT_PY ?? "/home/joe/venv-flagembed/bin/python3"` |
| P2-I `agent_end` hook 超时 8s，内部 checkin(5s)+learn(8s) 最坏 13s | 提成常量并配平：checkin **2000ms** + learn **5000ms** < hook **8000ms** |
| P2-H 靠 monkey-patch 挂 hook，重复 register 会双份注册 | `registerHooks` 用 `WeakSet<api>` 幂等（同一个 api 只注册一次；不同 api 仍各注册一次，所以现有测试里的两个 fake api 不受影响） |
| P0-A 只在 MCP 侧告警 | 新增 `checkPythonCapability()`：注册时异步探一次 jieba，缺了走 `logger.warn` + 修复配置 |

---

## 6. 新增文件

| 文件 | 作用 |
|---|---|
| [preflight.py](preflight.py) | 上线自检：jieba / 嵌入服务 / bridge / memory.db / state.db / **memorykit 工作树是否脏** / 写入身份。退出码 0=全绿 1=FAIL 2=WARN，`--json` 可机读。**只读，不写库不改配置** |
| [tests/test_mcp_server_patched.py](tests/test_mcp_server_patched.py) | MCP server 回归测试（11 组、38 条断言，跑在 /tmp 临时库，不碰真库）。这是 bridge/MCP 侧**第一次有测试** |
| [ask_local_model.py](ask_local_model.py) | 把本地 qwen3.8-flash-next（strata :8080，512k ctx）当 subagent 调的小工具 |
| [patches.diff](patches.diff) | 相对原始文件的 unified diff |

`preflight.py` 实测输出：

```
✅ 解释器 jieba              /home/joe/venv-flagembed/bin/python3
⚠️  MEMORYKIT_PY 未设置       dsh 侧默认用 sys.executable=/usr/bin/python3; 建议显式指向 …
✅ 嵌入服务                   http://127.0.0.1:8082 model=/home/joe/llam/bge-m3-Q4_K_M.gguf
✅ bridge.py stats            total=1990 categories=16
✅ memory.db                  rows=1990 agents=[hermes,hermes-desktop,hermes-server,dsh]
✅ state.db                   3.29 GB (overlay expand 依赖)
⚠️  memorykit 工作树脏         HEAD=6a6446e 有 7 处未提交改动 → 线上行为 ≠ 任何已提交状态
ℹ️  写入身份                   MEMORYKIT_AGENT_ID=hermes(默认)
8 项检查: 0 FAIL / 2 WARN
```

---

## 7. 有意**没**改的

| 项 | 原因 |
|---|---|
| P2-J 自动 learn 存的是「用户最后那句话」（流水账） | 是设计取舍，不是 bug。要改成「学助手提炼的结论」会改变语义，得你拍板 |
| P1-D 根因：给 `memorykit.learn()` 加 `agent_id` 形参 | 属于 `~/memory-kit`（上游），本次只保证 bridge 侧不静默；上游改完 `_attribute()` 就能删掉 |
| P1-E memorykit 工作树脏 | 要做的是 `git commit`（运维动作），不是补丁能解决的 |
| P0-B 的 `cleanup`/`age` 不暴露 | 运维命令，故意不给模型 |
| TS 插件的 `enabled: false` | 那在 `~/.openclaw/openclaw.json`，属于配置决策 |

---

## 8. 怎么用（可选）

**最小止血**（不动任何代码，只给线上 dsh 打补丁）——在 `~/.dsh/cordis.patch.yml` 的
`memory-memorykit.env` 里加一行，重启 dsh web：

```yaml
          MEMORYKIT_PY: /home/joe/venv-flagembed/bin/python3
```

**用这一版**：把 `bridge.py` / `mcp_memory_server.py` 覆盖到 `/home/joe/memorykit-overlay/`
（建议先 `git commit` memory-kit 的脏改动，留回滚点），TS 侧 `npm run build` 后再交给 openclaw。

**先体检**：`python3 preflight.py`。

**回归**：`TMPDIR=/tmp python3 tests/test_mcp_server_patched.py` 和
`TMPDIR=/tmp ./node_modules/.bin/vitest run --config ./vitest.config.ts`。

> ⚠️ 本机 `TMPDIR=/home/joe/.hermes/cache/scratch` 对沙箱/受限用户不可写，vite/vitest 会
> `EACCES`。跑 node 侧测试请显式 `TMPDIR=/tmp`。

---

## 9. 验证记录

| 检查 | 结果 |
|---|---|
| `python3 -m py_compile` ×3 | 通过 |
| `tsc -p tsconfig.json --noEmit` | 通过（改前基线也通过） |
| `vitest run`（原 19 个用例 + 新增 1 个幂等用例） | **20 passed**，无回归 |
| `tests/test_mcp_server_patched.py` | **38/38 PASS** |
| `preflight.py` | 0 FAIL / 2 WARN（两条 WARN 都是真实待办：MEMORYKIT_PY 未设、memorykit 工作树脏） |

> ⚠️ 一次**负载下的抖动**被撞见了：并发跑 python 回归（也在打嵌入服务）时，
> `before_prompt_build 相关提问自动注入相关记忆` 这个用例失败了一次 ——
> 注入路径本身只要 1.6s，但 hook 里 `callBridge("search", …, 2500)` 的超时是 2.5s，
> 嵌入服务被抢时就会超时 → `catch { hits = [] }` → **静默不注入**。单独跑 20/20 全绿。
> 这正是 REVIEW.md P1-F/P2-I 说的"静默降级"在生产里的样子：不报错、只是没注入。

---

## 10. 复审回路：本地模型又抓出 4 处

用本地 `qwen3.8-flash-next-iq2_xs`（512k ctx）当独立评审者审了这一版补丁，它的思考链指出了
4 个我确实漏掉的问题，已全部修掉（详见 [qwen_review.md](qwen_review.md)）：

| 它指出的 | 我的修法 |
|---|---|
| 只挡 hook 不够：`_origRegister(api)` 重复调用会让 **10 个工具**也注册两遍 | 幂等提到 wrapper：`registeredApis` WeakSet 一次挡住工具 + hook；补单测 |
| limit 只 clamp 了 `search`/`search_state`，`episodic`/`overlay`/`knowledge`/`verify_batch` 可被传巨大 limit | 新增 `_LIMIT_HARD` 全量封顶 |
| `PY` 模块级为 `None`，import 后直接调 `handle_tool` 会 `subprocess([None, ...])` | 新增 `_ensure_py()` 惰性选择；补测试 T11 |
| `_has_jieba` 探测超时 60s 会拖慢 MCP 启动 | 收到 20s |

它另有 3 条经核实**不成立**（`_ok(**kw)` 接受 kwarg / `verify` 缺 key 是清晰报错不是崩溃 /
batch 优先是设计意图），也记在 `qwen_review.md` 里。
它的"只能再改一处"回答是「补 MCP/bridge 测试并修 verify/limit 边界」——
与本补丁判断一致，两条都已落地。

---

## 11. 第二轮：rebase 到当前生产（learn_batch 事故，2026-10-15）

### 发生了什么
上面第 1-10 节那份补丁的文件 mtime 是 18:23-18:45，而生产侧 `bridge.py` /
`mcp_memory_server.py` / `func_test.py` 是 **21:26-21:27** 写的 —— 快照早于
`learn_batch` 落地。于是那份补丁**把 learn_batch 整条删掉了**：

| 文件 | 生产 | 旧补丁快照 | 后果 |
|---|---|---|---|
| `bridge.py` | 426 行，有 `cmd_learn_batch` | 395 行，无 | 批量写入消失 |
| `mcp_memory_server.py` | 245 行，有 `learn_batch` 工具 | 无 | dsh 侧工具消失 |
| `func_test.py` | 168 行（39 绿） | 138 行（08-11） | 8b 节 5 条测试消失 |
| `README.md` | 231 行 | 230 行（08-21） | 进度记录回退 |

**若按旧版上线 = 静默回退当天刚验收的批量能力**，且"工具数 8→14"这类计数断言抓不到。

### 怎么改的
以生产 21:26-21:27 快照为基线重落补丁（本文档其余结论不变）：

1. `bridge.py`：`_attribute()` 同时服务于 `cmd_learn` 和 `cmd_learn_batch`；`agent_id` /
   `agent_id_fixed` 字段名保留（func_test 8b 依赖），另加 `agent_attribution` /
   `agent_attribution_failures`。
2. `mcp_memory_server.py`：补上 `learn_batch` 工具（`TOOL_IMPL` 一行 + `"batch"` 形态），
   工具数 14 → **15**；新增 `_TOOL_TIMEOUT["learn_batch"]=600s`（默认 30s 会砍掉批量写入）。
3. `func_test.py` / `README.md` / `verify_mcp_tools.py` 对齐生产版；`verify_mcp_tools.py`
   的 `REQUIRED` 扩到 15 个工具。
4. `tests/test_mcp_server_patched.py` 新增 **T12**：端到端批量写入 + 断言 `bridge.py` 仍导出
   `learn_batch`、`TOOL_IMPL` 仍含它、超时 ≥300s —— 这次的防线。
5. `patches.diff` 已按新基线重新生成（819 行 / 6 个文件）。

### 验证（新基线）
| 检查 | 结果 |
|---|---|
| `py_compile` ×2 | 通过 |
| `tests/test_mcp_server_patched.py` | **全绿**（T1-T12） |
| `func_test.py` | **39 通过 / 0 失败** |
| `verify_mcp_tools.py` | 15 工具齐全 → `OK` |
| `preflight.py` | 0 FAIL / 1 WARN（只剩 `MEMORYKIT_PY`；`memorykit 工作树脏` 已随 e93607c 消失） |
| `tsc` + `vitest run` | 构建通过 / **20 passed**；`dist/` 已重建（含 `fileURLToPath`） |
