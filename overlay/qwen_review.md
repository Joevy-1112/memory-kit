# 本地模型复审记录 — qwen3.8-flash-next-iq2_xs（strata :8080，512k ctx）

调用：`python3 ask_local_model.py --file REVIEW.md --file patches.diff ...`

| 轮次 | max_tokens | 实际 completion | 结果 |
|---|---|---|---|
| 1 | 2500 | 2500（全被思考链吃光） | `content` 为空 |
| 2 | 9000 | 9000（思考链 33630 字符） | `content` **仍为空** |

结论：这是个**重度 reasoning** 模型，两次都把预算烧在思考链上、没吐出最终答案
（`ask_local_model.py` 已加 `--save-reasoning` 和空回答告警）。所以下面是我从**思考链原文**里摘出、
并逐条核实过的结论（✅ 已核实并采纳 / ❌ 核实后不成立 / ➖ 已知未修项）。

## 它提出、我核实后**采纳**的 4 条

1. ✅ **只挡 hook 不够**：`plugin.register` 被宿主重复调用时，`_origRegister(api)` 仍会把
   10 个工具注册两遍 —— 我原来只在 `registerHooks` 里放了 WeakSet。已把幂等提到 wrapper 上，
   工具 + hook 一起挡，并补了单测（`src/index.test.ts`「重复 register 同一个 api 不会双份注册」）。
2. ✅ **limit 封顶不全**：原来只 clamp 了 `search`/`search_state`，
   `episodic`/`overlay`/`knowledge`/`verify_batch` 能被传巨大 limit 把返回撑爆。
   已加 `_LIMIT_HARD` 全量封顶。
3. ✅ **`PY` 模块级是 None**：如果模块被 import 后直接调 `handle_tool`（不进 `main()`），
   会 `subprocess([None, ...])`。已加 `_ensure_py()` 惰性选择，并补测试 T11。
4. ✅ **探测超时过长**：`_has_jieba` 用 60s 超时会拖慢 MCP 启动，已收到 20s。

## 它提出、核实后**不成立**的 3 条

- ❌ 「`_ok(result=r, agent_attribution=...)` 可能 TypeError」——`_ok(**kw)` 接受任意 kwarg，
  实测 T5 返回 `agent_attribution=ok`。
- ❌ 「`verify` batch=false 且无 key 是 bug」——bridge 返回 `{"ok":false,"error":"key required"}`，
  补丁后以 `isError` 清晰上报，不是崩溃。
- ❌ 「batch=true 时 key 被忽略」——batch 优先是设计意图，工具描述里已写明。

## 它指出、我**有意没改**的（与 [PATCHES.md](PATCHES.md) 第 7 节一致）

- ➖ P1-E memorykit 工作树脏 / 版本未锁 → 要做的是 `git commit`，不是补丁
- ➖ P1-F DSH 侧没有自动 hook → 要在 DSH 插件体系里挂 hook，属另一个工程
- ➖ P2-J 自动 learn 存「用户最后那句话」→ 语义取舍，需 Joe 拍板
- ➖ P2-K bridge/MCP 原来零测试 → 本补丁已补 `tests/test_mcp_server_patched.py`（38 条断言）

## 它的第 3 问（"只能再改一处改哪里"）

> 补 MCP/bridge 测试并修 verify/limit 边界

与本补丁判断一致：limit 边界已修、测试已补。真正收益最大但**不在本补丁范围**的一项，是给
memorykit 的 `learn()` 加 `agent_id` 形参 —— 那是 P1-D 的根因，改完 `bridge._attribute()`
整段可以删掉。

---

## 附：思考链原文（33630 字符，未删改）

第一次调用（10093 字符）的思考链未保留。以下是第二轮完整原文。

We need answer in Chinese. User asks: as strict code reviewer, first conclusion list <=200 chars? Actually "先用不超过 200 字给结论清单，再逐条展开。只讲可验证的具体问题，不要客套，不要复述代码。" Need answer three things: 1) patch introduced bugs/regressions, focus mcp_memory_server TOOL_IMPL/_dispatch/error handling, bridge _attribute, src/index.ts WeakSet idempotent + timeout budget. 2) REVIEW P0/P1/P2 missing or incomplete. 3) only one change highest benefit. Need specific files/functions/behaviors. Need analyze patches.

We need inspect diff carefully. Need identify new bugs/regressions.

Let's parse patch.

mcp_memory_server.py:
- Added imports urllib.request.
- BRIDGE_TIMEOUT env.
- EMBED_URL.
- PY_CANDIDATES includes env MEMORYKIT_PY and hardcoded venv. PY initially None.
- _log stderr.
- _has_jieba runs subprocess [py, -c import jieba], timeout 60. If exception false.
- pick_python: if MEMORYKIT_SKIP_PY_PROBE: py = env or sys.executable; ok = _has_jieba(py); if not ok warn; return py, ok. Else for cand in PY_CANDIDATES: if cand and os.path.exists(cand) and _has_jieba(cand): return cand, True. fallback = env or sys.executable; warn; return fallback, False.
Potential issues:
- If MEMORYKIT_PY set but no jieba, first candidate fails, fallback env, warn. Good.
- If env set to path that doesn't exist? os.path.exists false, fallback env, warn. But call_bridge uses PY path nonexistent -> subprocess error. Could be okay but maybe should not choose nonexistent. If env set but nonexistent, fallback returns nonexistent, warn no jieba, then call_bridge fails. Could be regression? Original would use sys.executable if env not set; if env set nonexistent, original also uses nonexistent. Not new.
- _has_jieba timeout 60 at startup may delay MCP server startup; if subprocess hangs, timeout 60, then fallback. Could be new behavior. But not necessarily bug.
- check_embed_service uses urllib.request.urlopen timeout 3. If embed service unreachable, logs. Could be okay. But if server is JSON-RPC stdio, stdout must be JSON-RPC; _log stderr. Good.
- TOOLS: search schema includes mode, category, scope, max_age_days, ref_time. But _map_memory includes strict_time_window, time_window not in schema? It maps if args has them. Could be okay. But search tool schema doesn't include strict_time_window/time_window, so clients won't send. Not bug.
- learn schema source default current agent identity? But bridge cmd_learn likely uses agent from env? Need know. In patch, TOOL_IMPL learn maps content, source, source_session, event_time. But bridge cmd_learn signature? Original bridge likely cmd_learn(a) with args includes content, source, agent? Need infer. In REVIEW P1-D bridge cmd_learn uses agent variable. How is agent determined? Maybe from env or args? Patch didn't show. In mcp, no agent param. Could be using environment? If bridge expects agent? Need inspect diff only. In bridge patch, cmd_learn uses `agent` variable. It likely comes from `a.get("agent")` or env? Not shown. In mcp, learn args don't include agent. If bridge uses env AGENT_ID? Could be okay. But if bridge expects `agent` in args, mcp doesn't pass, so attribution may be default? Need not assume. But patch says source default current agent identity. Could be new behavior? Need identify verifiable from diff: mcp learn tool schema no agent; bridge _attribute uses agent variable. If agent not passed, maybe from env. Not enough.

- TOOL_IMPL:
search -> search, _map_memory, hits.
learn -> learn, passthrough content source source_session event_time, learn.
set -> set, passthrough key content category scope importance tags, raw.
get -> get, key include_vector, raw.
correct -> correct, key signal session_id, raw.
reinforce -> reinforce, key mode session_id, raw.
verify -> verify, key auto_degrade threshold, raw.
verify_stats -> verify_stats, lambda {}, raw.
recall -> episodic, _map_recall, episodes.
episodic -> episodic, passthrough action session_id summary query start end limit, raw.
overlay -> overlay, action query key limit, raw.
search_state -> search_state, query limit session_id, hits.
knowledge -> knowledge, action memory_key title domain query kid limit, raw.
stats -> stats, lambda {}, raw.

Potential new bugs:
- `verify` tool schema includes batch, limit, only_pending, auto_degrade, threshold. TOOL_IMPL verify maps key, auto_degrade, threshold only. _dispatch handles batch branch: if name == verify and args.get("batch"): return ("verify_batch", _map_passthrough("limit", "only_pending", "auto_degrade", "threshold")(args), "raw"). Good. But if batch false and key missing? bridge verify may require key? Schema no required. Could call bridge with empty args. If bridge expects key, error. But original required key. New schema allows no key; if no key and no batch, call verify with empty mapped -> bridge may fail or do something. Could be behavior regression? Need check bridge cmd_verify? Not in diff. Could be okay if bridge handles. But tool description says single key, batch. If neither, undefined. Could be bug.
- `verify` batch branch returns raw, but format? handle_tool if shape raw returns _text(resp). Good.
- `search` limit cap: handle_tool: if "limit" in mapped and cmd in ("search", "search_state"): mapped["limit"] = _cap(mapped["limit"], 5, 10). But _map_memory may include limit from args. If args limit not present, mapped doesn't include limit, so bridge default? Original had limit min(int(args.get("limit",5)),10). New if no limit, no cap, bridge default maybe 5. Good. But if args limit invalid string, _cap handles. If limit present but not in schema? okay.
- `recall` _map_recall uses _cap(args.get("limit"),5,20). Good. But TOOL_IMPL recall shape episodes, handle_tool if cmd in search/search_state cap? no. Good.
- `format_hits` changed [:8] to [:10]. Could increase token usage, but not bug. It uses h.get("created_at") or "" to avoid None. Good.
- `format_episodes` assumes resp.get("episodes") list. If bridge episodic returns different shape? Need know. Could be okay.
- `_error(resp, label)` returns {"isError": True, "content": [{"type":"text","text": json.dumps({"ok": False, "tool": label, "error": msg})}]}. MCP tool result usually content array, isError boolean. Good. But if resp is not dict? call_bridge returns dict. Good.
- call_bridge: if returncode != 0, try parse stdout JSON. If parsed dict, return parsed. If not, error. If returncode 0, return json.loads(r.stdout). If stdout empty, json.loads raises, caught. Good. But bridge convention exit code 2 business failure prints JSON. If returncode 2 and stdout JSON ok=false, returns parsed. Good. If returncode 2 and stdout JSON ok=true? Then handle_tool sees ok true, maybe wrong. But bridge likely ok false.
- Potential issue: `call_bridge` uses `PY` global. In main, `global PY, PY_HAS_JIEBA; PY, PY_HAS_JIEBA = pick_python()`. Good. But if `handle_tool` called before main? no.
- `_has_jieba` uses subprocess.run with timeout 60. If `py` is None? PY_CANDIDATES includes env maybe None; loop checks cand and os.path.exists. fallback env or sys.executable. Good.
- Startup probe may run `import jieba` in subprocess, but if `py` is venv and jieba import slow, timeout 60. Could delay. Not bug.
- `check_embed_service` uses urllib.request.Request, urlopen. If EMBED_URL has no scheme? env default http. If env set weird, exception. Good.
- stdout JSON-RPC: _log stderr. Good.

Bridge patch:
- cmd_learn now attribution = "not-needed" if agent == hermes? Actually code:
```
attribution = "not-needed"       # hermes 调用方本来就是这个归属
if agent != "hermes" and isinstance(r, dict) and r.get("key"):
    attribution = _attribute(db, r["key"], agent)
    if attribution != "ok": sys.stderr.write(...)
elif agent != "hermes":
    attribution = "skipped: learn() 未返回 key"
return _ok(result=r, agent_attribution=attribution)
```
Potential bug: If agent != hermes and r not dict or no key, attribution = skipped. Good. If agent == hermes, not-needed. If agent != hermes but r is dict no key, skipped. Good.
- `_attribute`: try with db._tx(): execute update. Then row = db._conn.execute(...).fetchone(). But after `with db._tx():` transaction committed? If `db._tx()` context manager commits at exit. Then `db._conn.execute` outside transaction maybe okay. But if `db._tx()` uses same connection and commits, read after commit okay. If `db._tx()` context manager maybe rollback on exception. If update succeeds, read row. If row None, got None, returns verify-mismatch. Good.
Potential issue: If `db._tx()` context manager commits, but `db._conn.execute` after may be in autocommit? okay. If `db._tx()` uses transaction and read inside? It reads after context, okay. If `db._tx()` is private API, not robust.
Potential issue: `_attribute` returns failure but does not raise; cmd_learn returns ok with agent_attribution failure. This is visible. But if update fails, memory remains hermes; caller may think learned ok but attribution failed. Good.
Potential regression: If `agent` is not passed to bridge, `agent` variable maybe from env? If agent != hermes but learn returns key, _attribute called. If agent is None? `agent != "hermes"` true, _attribute(db, key, None) sets agent_id=None? Could be bug. Need know how agent set. In original bridge cmd_learn likely `agent = a.get("agent") or os.environ.get("AGENT_ID") or "hermes"`? Not shown. If agent can be None, patch may write None. But original had `if agent != "hermes" and isinstance(r, dict) and r.get("key")`, so if agent None, original would attempt update to None too. Not new.
- `_ok(result=r, agent_attribution=attribution)` maybe `_ok` signature? If `_ok` only accepts result? In diff, likely `_ok` can accept kwargs? Need check. If not, new TypeError. But patch uses `_ok(result=r, agent_attribution=attribution)`. We don't see `_ok` definition. Could be existing helper accepts **kwargs? If not, bug. Need be careful. Since diff only shows call, cannot verify. But as reviewer, can flag if `_ok` signature not known. However likely `_ok` is defined as `def _ok(**kwargs): return {"ok": True, **kwargs}`? In bridge, common. Could be okay.
- `_attribute` uses `db._conn.execute` after `with db._tx()`. If `db._tx()` context manager closes connection? no.
- Potential issue: `sys.stderr.write` in bridge. Bridge stdout is JSON; stderr okay. But if bridge is invoked by execFile with capture_output, stderr captured. Good.

src/index.ts patch:
- import fileURLToPath.
- PYTHON = process.env.MEMORYKIT_PY ?? hardcoded venv. This changes TS from hardcoded venv to env override. Good. But if env MEMORYKIT_PY set to system python3 without jieba, TS will use it and warn. Could be behavior change.
- bridgePath uses fileURLToPath. Good.
- Hook timeout constants: HOOK_PROMPT_TIMEOUT_MS=3000, HOOK_AGENT_END_TIMEOUT_MS=8000, CHECKIN_TIMEOUT_MS=2000, LEARN_TIMEOUT_MS=5000. In handleAgentEnd, checkin timeout 2000, learn timeout 5000. Sum 7000 < 8000. Good. But there may be other operations? handleAgentEnd maybe first checks lastUserMessage, quality, then checkin, learn. If checkin and learn sequential, total 7000 plus overhead. Good. But `callBridge` timeout is per call, not total. If checkin takes 2000, learn takes 5000, total 7000. Hook timeout 8000. Good. However if `checkPythonCapability` in registerHooks uses execFileAsync timeout 30000, not hook timeout. It is async void, may run after hook registration. Could spawn subprocess at startup; okay.
- WeakSet idempotent: `const hooksRegisteredOn = new WeakSet<object>(); function registerHooks(api) { if (hooksRegisteredOn.has(api as object)) return; hooksRegisteredOn.add(api as object); api.on(...); void checkPythonCapability(api.logger); }`
Potential bug: If `register` monkey patch calls `registerHooks(api)` multiple times with same api, skip. Good. But if `register` is called twice with different api objects, registers twice. That's intended? If host creates new api, okay. If same api but `register` called twice, skip. However if `register` is called twice with same api but first call failed? WeakSet added before api.on? It adds then registers. If api.on throws, subsequent calls skip, leaving no hooks. Could be bug. Original no idempotence. If first registration fails, second won't retry. But likely not.
- `checkPythonCapability` uses `execFileAsync(PYTHON, ["-c", "import jieba"], { timeout: 30_000 });` If PYTHON is env variable, okay. But if `PYTHON` is not executable or path nonexistent, execFile throws, logs warn. Good. But `execFileAsync` returns promise; `void` ignores. If logger warn not available? optional. Good.
- Potential regression: `checkPythonCapability` runs at plugin registration, not per hook. If plugin loaded in environment where stdout is JSON-RPC? TS side not. okay.
- Potential issue: `process.env.MEMORYKIT_PY ?? "/home/joe/venv-flagembed/bin/python3"` uses env, but if env empty string, `??` keeps empty string? In JS, empty string is not null/undefined, so PYTHON = "" if env set to empty. Then execFile fails. Could be minor. Could use `||`.
- Potential issue: `fileURLToPath(new URL("./bridge.py", import.meta.url))` requires Node 14? okay.
- Potential issue: `registerHooks` WeakSet uses `api as object`; if api is primitive? HookApi likely object. okay.
- Potential issue: `checkPythonCapability` may be called before `registerHooks`? It is called inside registerHooks. If `register` monkey patch calls `_origRegister(api)` then `registerHooks(api)`. If `_origRegister` throws, hooks not registered. okay.
- Potential issue: `register` monkey patch still exists. If SDK wraps register, monkey patch may be lost? Patch didn't fix P2-H fully. It only adds WeakSet. If SDK calls original register not monkey-patched, hooks disappear. But patch acknowledges. Need mention incomplete.

Now REVIEW P0/P1/P2 missing/incomplete:
P0-A: patch fixes by env override and startup probe in mcp and TS. But not fix `~/.dsh/cordis.patch.yml` config. It auto picks venv if env not set. Good. But if `MEMORYKIT_PY` set to no jieba, warns but still uses it. Could be okay. It doesn't ensure memorykit uses venv? It chooses. But if memorykit itself imports jieba in subprocess? It uses chosen PY. Good. However if `MEMORYKIT_PY` env is set to system python3, TS uses it and warns; no automatic fallback to venv. Could be incomplete. Also `mcp_memory_server.py` startup probe runs subprocess for each candidate; if `MEMORYKIT_PY` set but no jieba, it warns but doesn't change to venv. If user intended env override, okay. But P0-A says fix config; patch does not modify config. It adds runtime fallback if env absent. If env present wrong, still bug. Could be considered not fully fixed.
P0-B: patch expands tools 7->14, covers 14 of 16, intentionally omits cleanup/age. But missing `verify_batch`? It includes verify batch branch but no tool named verify_batch. It covers `verify_batch` via verify batch. Missing `cleanup`, `age` intentionally. But `recall` fixed to episodic. However `episodic` tool schema action default list? It says default list but no default in schema. Could be okay. `overlay` tool added. `search_state`, knowledge, stats, get, verify_stats. Good. But `recall` description says with query event search, without recent list; _map_recall does that. Good. Potential missing: `search` tool schema includes mode etc but bridge `cmd_search` may not support all? Need not.
P0-C: patch fixes error reporting via `_error`. But `format_hits` still returns empty if `resp.ok` true but hits missing? If bridge returns ok true but no hits, okay. If bridge returns ok false, error. Good. But `handle_tool` for raw tools returns `_text(resp)` even if resp.ok false? It checks `if not resp.get("ok"): return _error`. Good. However `call_bridge` if returncode !=0 and stdout JSON parsed dict, returns parsed. If parsed dict lacks `ok` but is result? Then `not resp.get("ok")` true -> error. Good. If bridge returns JSON with `ok: false`, error. Good.
Potential new bug: `handle_tool` for `learn` shape learn returns `learned: True` regardless of resp.ok? It checks not ok before, so okay. But if bridge returns ok true but result null, learned true. okay.
Potential issue: `_error` label includes `name→cmd`; for batch verify label `verify→verify_batch`. Good.
P1-D: patch adds explicit attribution and readback. But root cause memorykit learn lacks agent_id param remains. It still uses private API and writes after learn. It doesn't add agent_id param to learn. So not fully fixed. Also `_attribute` readback may be race? If learn writes in transaction, then update, read. okay. But if `agent` is not passed from MCP, attribution may not work. Need mention.
P1-E: patch does not address dirty worktree, sys.path hardcoded, version lock. Missing.
P1-F: patch aligns TS interpreter via env and startup warning, but DSH MCP still no hooks. It doesn't add hooks to DSH. It doesn't address OpenClaw disabled. Incomplete.
P2-G: patch fixes fileURLToPath. Good.
P2-H: patch adds WeakSet idempotence but monkey-patch remains; if SDK wraps register, hooks still disappear. Incomplete.
P2-I: patch fixes timeout budgets. Good. But `checkPythonCapability` timeout 30s outside hook; okay. `agent_end` total 7s < 8s. Good.
P2-J: patch does not change auto learn content; still lastUserMessage slice? It didn't modify handleAgentEnd content extraction. Missing.
P2-K: patch mentions regression tests `tests/test_mcp_server_patched.py` but no test file in diff. It claims tests but not included. Bridge/MCP zero tests remains. TS test may not cover new. Missing.

Need identify new bugs/regressions specifically:
1. `mcp_memory_server.py` startup `pick_python()` may choose hardcoded venv even when `MEMORYKIT_PY` intentionally set to a different interpreter, causing behavior change? It checks env first, so if env set, uses env if jieba. If env set no jieba, warns but still uses env. If env not set, chooses venv. Good. But if env set to path that exists but no jieba, it doesn't fallback to venv; could be regression relative to original? Original used env; patch uses env too. Not regression.
2. `_has_jieba` timeout 60 can block MCP initialization; if `MEMORYKIT_PY` is a wrapper that hangs, server startup delayed. Could be new operational bug.
3. `check_embed_service` uses `urllib.request.urlopen` to `/v1/models`; if embed service is not HTTP or requires auth, logs warn. Could be okay. But if `EMBED_URL` is `http://127.0.0.1:8082` and service is down, startup warning. Could be new side effect. If stdout must be JSON-RPC, stderr okay. But if MCP client expects no stderr? okay.
4. `call_bridge` now parses stdout on nonzero exit. If bridge prints JSON to stdout with `ok:false`, returns it. If bridge prints JSON but `ok` missing, handle_tool error. Good. But if bridge returns JSON with `ok: true` on nonzero exit, handle_tool treats as success. Could be inconsistent. But bridge convention exit 2 business failure prints JSON likely ok false.
5. `handle_tool` caps limit only for search/search_state, not for `episodic`, `overlay`, `knowledge`, `verify_batch`, `recall`? recall caps in mapper. episodic limit not capped; could pass huge limit to bridge. Original had limit min 10 for search/recall. New tools may allow arbitrary limit. Could be behavior regression: `episodic`/`overlay`/`knowledge` limit not capped, potential large output/token. But not necessarily bug. Could mention.
6. `format_hits` limit changed from 8 to 10; if MCP tool description says max 10, okay. But `search` schema max 10, format_hits [:10]. Good.
7. `verify` tool schema no required key; if neither key nor batch, `mapped` empty, bridge may error or do nothing. Could be new behavior. Need flag.
8. `verify` batch branch maps `limit`, `only_pending`, `auto_degrade`, `threshold`; but `verify` tool schema includes `key` and batch. If batch true and key present, key ignored. Could be okay. If batch false and key missing, error. Could be bug.
9. `_map_memory` includes `strict_time_window`, `time_window` not in schema; if clients send, passed. okay.
10. `TOOL_IMPL` for `search` shape hits, but bridge `search` may return `ok` and `hits`. Good.
11. `TOOL_IMPL` for `search_state` shape hits, but bridge `search_state` may return `hits`? likely. Good.
12. `TOOL_IMPL` for `episodic` shape raw, but `recall` shape episodes. If `episodic` action list returns `episodes`, raw returns full resp. okay.
13. `handle_tool` for `learn` returns `agent_attribution` from bridge. But bridge `_ok(result=r, agent_attribution=attribution)`. If `_ok` returns `{"ok": True, "result": r, "agent_attribution": attribution}`. Good.
14. `bridge._attribute` readback after transaction: If `db._tx()` context manager uses `with db._tx():` and then `db._conn.execute` outside, but if `db._tx()` commits, okay. If `db._tx()` is a context manager that yields connection and commits, but `db._conn` may be closed? no.
15. `bridge._attribute` catches Exception and returns failed. But if update fails due to readonly database, returns failed. Good. However `cmd_learn` writes WARN to stderr, but returns ok. If caller only checks ok, may ignore attribution. But MCP learn shape includes agent_attribution. Good.
16. `bridge` uses `sys.stderr.write` but if stdout is JSON-RPC? bridge called by execFile, stdout captured. okay.
17. `src/index.ts` `checkPythonCapability` uses `execFileAsync` with timeout 30_000. If plugin register is called many times, WeakSet prevents repeated hook registration but `checkPythonCapability` only called once per api. Good. But if `register` monkey patch calls `registerHooks` after `_origRegister`, and `_origRegister` calls `registerHooks`? no.
18. `WeakSet` idempotence: If `api` is reused but `register` called twice, skip. But if `register` called twice with same api and first call did not register because `hooksRegisteredOn` already had api from previous plugin instance? WeakSet global across module. If same api object passed to two different plugin instances? Could skip incorrectly. But likely one plugin.
19. `checkPythonCapability` is async but not awaited; if it throws, unhandled? It catches. Good.
20. `PYTHON` env override: If `MEMORYKIT_PY` is set to `python3` without jieba, TS warns but still uses it; could cause silent degradation if logger warn not observed. But patch aims explicit. okay.
21. `fileURLToPath` import from node:url; if runtime is older Node? Could be issue. But likely.

Need answer three things. Need first conclusion list <=200 characters? Chinese "不超过 200 字" likely 200 Chinese characters. Need concise conclusion list. Then detailed. Need ensure first list <=200 characters. Could be bullet list. Need count? We can make short. Maybe:
"结论：补丁有可验证回归：MCP 启动探针可能阻塞/选错解释器；verify 无 key/batch 行为未定义；limit 未全量封顶；bridge 回读依赖私有 API；TS WeakSet 首次失败不重试。P0-A/B/C 基本修，P1-D/E/F/J/K 未修或半修。最大收益：补 MCP/bridge 测试并修 verify/limit 边界。"
Count maybe >200? Need <=200 Chinese characters. Let's count roughly: 结论：补丁有可验证回归：MCP启动探针可能阻塞/选错解释器；verify无key/batch行为未定义；limit未全量封顶；bridge回读依赖私有API；TS WeakSet首次失败不重试。P0-A/B/C基本修，P1-D/E/F/J/K未修或半修。最大收益：补MCP/bridge测试并修verify/limit边界。
Count: 结论(2)：(1) 补丁有可验证回归(8?) Let's count characters: 结1论2：3补4丁5有6可7验8证9回10归11：12M13C14P15启16动17探18针19可20能21阻22塞23/24选25错26解27释28器29；30v31e32r33i34f35y36无37k38e39y40/41b42a43t44c45h46行47为48未49定50义51；52l53i54m55i56t57未58全59量60封61顶62；63b64r65i66d67g68e69回70读71依72赖73私74有75A76P77I78；79T80S81 W82e83a84k85S86e87t88首89次90失91败92不93重94试95。96P97O98-99A100/101B102/103C104基105本106修107，108P109-110D111/112E113/114F115/116J117/118K119未120修121或122半123修124。125最126大127收128益129：130补131M132C133P134/135b136r137i138d139g140e141测142试143并144修145v146e147r148i149f150y151/152l153i154m155i156t157边158界159。
159 chars. Good. But maybe need include "最大收益" maybe one change. Could be okay. Need maybe first list only conclusion, then expand. Ensure <=200. Could make even shorter.

Need be strict, verifiable. Need not recode. Need cite file/function/behavior.

Let's think deeper to ensure no missed new bugs.

Potential issue in `mcp_memory_server.py`: `PY_CANDIDATES = (os.environ.get("MEMORYKIT_PY"), "/home/joe/venv-flagembed/bin/python3")`. If `MEMORYKIT_PY` is set to a path that exists but lacks jieba, `pick_python` will not choose venv because env candidate fails, then fallback env. So if env wrong, no fallback. Could be intentional. But if env absent, chooses venv. Good.

Potential issue: `_has_jieba` uses `subprocess.run([py, "-c", "import jieba"], capture_output=True, timeout=60)`. If `py` is a symlink to python that imports memorykit? no. If `py` is not executable, exception false. Good. But if `py` is `sys.executable` and `sys.executable` is empty? In some embedded Python, sys.executable may be empty string. `os.path.exists("")` false, fallback `sys.executable` empty, `_warn_no_jieba("")`, then call_bridge subprocess with empty path -> error. Original would use empty too. Not new.

Potential issue: `check_embed_service` at startup may make network request, violating MCP server startup expectations? It logs to stderr. Could be okay. But if MCP client expects server to be responsive immediately, 3s delay. Could be new. If embed service unreachable, warn. Good.

Potential issue: `handle_tool` `_dispatch` for `verify` batch: if `args.get("batch")` truthy, returns `verify_batch`. But `TOOL_IMPL` for verify maps key, auto_degrade, threshold. If batch true and key present, key ignored. If bridge `verify_batch` expects `key`? no. Good.

Potential issue: `handle_tool` caps limit for `search` and `search_state` only. But `search` schema max 10, `search_state` schema default 5 no max. If client sends limit 1000, `_cap` caps to 10. Good. `episodic` limit not capped; schema says return count but no max. Could output huge. `knowledge` limit not capped. `overlay` limit not capped. `verify_batch` limit not capped. Could be regression from original where all tool limit capped? Original only search/recall capped. New tools no cap. Could be issue.

Potential issue: `format_hits` now returns up to 10, but `search` schema max 10. Good. `search_state` schema limit default 5 but no max; handle caps to 10. Good.

Potential issue: `format_episodes` returns up to 20. `recall` schema max 20. Good. `episodic` raw may return more.

Potential issue: `call_bridge` timeout `BRIDGE_TIMEOUT` default 30. Original 30. Good. But `check_embed_service` timeout 3, `_has_jieba` timeout 60. Startup can be 60+3. If MCP client has startup timeout, could fail. New.

Potential issue: `mcp_memory_server.py` `main` prints `_log` before reading stdin. If stdout is JSON-RPC, stderr okay. But if MCP client captures stderr as error? okay.

Potential issue: `handle_tool` returns `{"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}`. Good.

Potential issue: `TOOLS` includes `verify` no required, but `handle_tool` if `name == "verify" and args.get("batch")` else `TOOL_IMPL`. If `batch` false and `key` missing, `mapped` empty. If bridge `cmd_verify` expects key, maybe returns error. But tool schema no required, so clients may call empty. Could be new undefined behavior. Need flag.

Potential issue: `bridge._attribute` readback: If `db._tx()` context manager commits, then `db._conn.execute` may start new transaction? okay. But if `db._tx()` uses `with db._tx():` and inside `db._conn.execute`, after context, connection may be in transaction? If `db._tx()` is a context manager that yields and commits, okay. If it uses `db._conn` and `db._tx` is private, not stable. Could be bug if `db._tx` returns a transaction object not connection? But original used same.

Potential issue: `bridge._attribute` returns `verify-mismatch` if row None. But if update affected 0 rows because key doesn't exist, row None. Good. If key exists but agent_id already agent? update no effect, read returns agent. Good.

Potential issue: `cmd_learn` attribution = "not-needed" for hermes. If agent != hermes but `r` is not dict or no key, attribution skipped. Good. But if `r` is dict with key but `agent` is None, _attribute sets None. Could be bug. Need maybe not.

Potential issue: `src/index.ts` `checkPythonCapability` uses `execFileAsync(PYTHON, ["-c", "import jieba"], { timeout: 30_000 });` If `PYTHON` is env variable but not executable, it logs warn. Good. But if `PYTHON` is `python3` and `execFileAsync` resolves, no warn. If `PYTHON` is empty string, execFile throws, warn. Good.

Potential issue: `registerHooks` WeakSet: If `api` is same object but `register` called twice, skip. But `checkPythonCapability` only called once. If first call skipped due to WeakSet, no warn. okay.

Potential issue: `register` monkey patch: `plugin.register = (api) => { _origRegister(api); registerHooks(api); };` If `_origRegister` throws, `registerHooks` not called. If `register` called twice, `_origRegister` called twice, `registerHooks` skip. Could duplicate tool registration? `_origRegister` may register tools twice. WeakSet only hooks, not tools. If host calls register twice, tools may duplicate. Original issue P2-H says hook duplicate; patch fixes hook duplicate but not tool duplicate. Could be incomplete. Need mention.

Potential issue: `checkPythonCapability` is called inside `registerHooks`, but if `registerHooks` returns early due to WeakSet, no capability check. If first call had no logger? okay.

Potential issue: `fileURLToPath` may not be available in some Node versions or bundlers. But likely.

Potential issue: `src/index.ts` `PYTHON` env override may make TS use system python3 if env set, contrary to original hardcoded venv. If env set to no jieba, warn but still use. Could be behavior regression if env accidentally set. But patch intended.

Potential issue: `mcp_memory_server.py` `PY` global initially None; `call_bridge` could be called before `main` sets PY? In normal main, no. But tests may import and call handle_tool without main, then PY None -> subprocess [None, ...] error. New tests? Not in diff. Could be bug for test harness. But not runtime.

Potential issue: `mcp_memory_server.py` `TOOLS` `search` schema includes `max_age_days`, `ref_time`, but `_map_memory` includes `strict_time_window`, `time_window` not in schema. If bridge expects `max_age_days` and `ref_time`, okay. But `search` tool description maybe not. Not bug.

Potential issue: `mcp_memory_server.py` `recall` tool schema no required, but `_map_recall` if no query returns action list. Good. But `episodic` tool action default list? It says default list but schema no default. If client sends no action, `_map_passthrough` doesn't include action, bridge may default list. okay.

Potential issue: `mcp_memory_server.py` `verify` tool schema includes `batch`, but `TOOL_IMPL` verify maps `key`, `auto_degrade`, `threshold`; `_dispatch` batch branch maps `limit`, `only_pending`, `auto_degrade`, `threshold`. If `batch` false but `key` missing, no `key`. Could be bug.

Potential issue: `mcp_memory_server.py` `handle_tool` for `learn` returns `learned: True` but bridge `cmd_learn` returns `_ok(result=r, agent_attribution=attribution)`. If `r` is not dict? `result` maybe. Good.

Potential issue: `mcp_memory_server.py` `_error` returns JSON text with `ok:false`, but MCP clients may expect `isError` and content. Good.

Potential issue: `mcp_memory_server.py` `call_bridge` if `r.returncode != 0` and stdout JSON parsed but not dict? It passes through? It checks isinstance dict. If parsed list, falls to error. Good.

Potential issue: `mcp_memory_server.py` `check_embed_service` uses `urllib.request.urlopen(req, timeout=3)`. If `EMBED_URL` is `http://127.0.0.1:8082` and server returns 200 but body not JSON, okay. If server requires POST, GET /v1/models may fail. Could be false warning. But not bug.

Potential issue: `bridge.py` `_attribute` writes WARN to stderr, but if bridge is used in MCP, stderr captured by call_bridge. If returncode 0, call_bridge returns stdout JSON, stderr ignored. The WARN won't reach MCP caller except via `agent_attribution` in result. Good. If `_attribute` returns failed, `agent_attribution` included. Good.

Potential issue: `bridge.py` `_ok(result=r, agent_attribution=attribution)` if `_ok` helper doesn't accept extra kwargs, TypeError. Need verify? In diff, original `return _ok(result=r)`. If `_ok` defined as `def _ok(result=None, **kwargs):` likely. But if not, new bug. We can flag as potential if `_ok` signature not updated. But user wants verifiable specific problems. We can say "若 `_ok` 未接受 kwargs，则调用会 TypeError；从 diff 无法确认，但这是可验证风险." But maybe too speculative. Need avoid speculation? Could mention as risk.

Potential issue: `bridge.py` `_attribute` uses `db._conn.execute` after `with db._tx():`. If `db._tx()` context manager commits, but `db._conn` may be in autocommit? okay. If `db._tx()` uses `db._conn` and `db._tx` is private, not stable. But original already used private API. Patch adds readback, still private.

Potential issue: `src/index.ts` `checkPythonCapability` uses `execFileAsync` but `execFileAsync` is promisified `execFile`. It returns `{stdout, stderr}`. If timeout, throws.