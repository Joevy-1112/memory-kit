import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { Type } from "typebox";
import {
  defineToolPlugin,
  type DefinedToolPluginEntry,
} from "openclaw/plugin-sdk/tool-plugin";

const execFileAsync = promisify(execFile);

// 解释器（2026-10-15 修补, REVIEW.md P0-A/P1-F）：
// 实体抽取依赖 jieba，系统 python3 没装 → memorykit 会**静默降级**（中文专名全灭）。
// 变量名与 mcp_memory_server.py 的 MEMORYKIT_PY 保持一致，两侧统一配置。
const PYTHON =
  process.env.MEMORYKIT_PY ?? "/home/joe/venv-flagembed/bin/python3";

// memorykit-overlay bridge 路径（与插件同目录；可用环境变量覆盖——测试时指向源目录）
function bridgePath(): string {
  return (
    process.env.MEMORYKIT_OVERLAY_BRIDGE ??
    // 用 fileURLToPath 而不是 .pathname：路径含空格/非 ASCII 时 pathname 会留下 %20
    // （2026-10-15 修补, REVIEW.md P2-G）
    fileURLToPath(new URL("./bridge.py", import.meta.url))
  );
}

// ════════════════════════════════════════════════════════════════════
// 自动接线（hooks）参数 —— memorykit-overlay 自动 prefetch / 自动 learn
// 2026-08-07：小龙虾反馈"没有触发它的地方" → 挂 before_prompt_build
// （注入相关记忆，等效 hermes 的 prefetch）+ agent_end（自动沉淀，
// 等效 hermes 的 sync_turn）。全部阈值经 src/index.test.ts 单元 +
// 端到端用例验证（质量门槛/相关度闸门/幂等去重各有正反断言）。
//
// 2026-08-07 修复（t_2c164107）：hook 注册 API 用 api.on（typed hook，
// types-DaHgOqFX.d.ts:12298 `on: <K extends PluginHookName>(...)`，运行时
// 走 registerTypedHook → registry.typedHooks —— 这是 openclaw 插件钩子
// 唯一的派发通道）。不要改成 api.registerHook：它注册的是 internal hook
// （registry.hooks + registerInternalHook），只服务 command/session/
// gateway/message 内部事件，before_prompt_build / agent_end 永远不会经
// 它触发，且它强制要求 opts.name 否则抛错、opts 里也没有 timeoutMs。
// agent_end 属于 conversation hook，非 bundled 插件必须在 openclaw.json
// 里显式开 plugins.entries.memorykit-overlay.hooks.allowConversationAccess
// = true，否则 registerTypedHook 直接拒绝注册（静默不生效的根因）。
// ════════════════════════════════════════════════════════════════════
const AUTO_INJECT_MIN_SCORE = 0.5; // rrf_score 绝对下限（无关查询顶多 ~0.48）
const AUTO_INJECT_MAX = 3; // 最多注入条数
const AUTO_INJECT_CHAR_CAP = 140; // 单条截断长度（省 token）
const SEARCH_CACHE_TTL_MS = 30_000; // 相同 prompt 检索缓存窗口
const SEARCH_CACHE_MAX = 8; // 检索缓存条数上限
const LEARNED_DEDUPE_MAX = 100; // 自动 learn 幂等窗口（条数）
const MIN_LEARN_CHARS = 20; // 质量门槛：最短长度（参考 hermes-local sync_turn）
const MIN_CJK_CHARS = 4; // 质量门槛：最少中文字数（信息密度，闲聊通常 <4）
const LEARN_MAX_CHARS = 500; // 自动 learn 内容上限
// hook 超时预算（2026-10-15 修补, REVIEW.md P2-I）：agent_end 内部
// checkin + learn 的预算之和必须 < hook 自身超时，否则 learn 会在提交前被杀。
const HOOK_PROMPT_TIMEOUT_MS = 3000; // before_prompt_build：每轮都跑，必须短
const HOOK_AGENT_END_TIMEOUT_MS = 8000;
const CHECKIN_TIMEOUT_MS = 2000;
const LEARN_TIMEOUT_MS = 5000;

/** 纯问候/寒暄（首词命中即跳过 learn）——参考 hermes-local _greetings */
const GREETINGS = new Set([
  "hi", "hello", "hey", "yo", "你好", "您好", "在吗", "哈喽", "嗨",
  "咋样", "睡了", "嗯", "好", "ok", "谢谢", "感谢", "再见", "拜拜",
  "早", "早安", "早上好", "晚安", "晚上好", "中午好", "哈哈", "在不在",
]);

/** 信号词（hermes-local sync_turn fallback 同款 + 硬件/服务器补充） */
const SIGNAL_RE =
  /配置|版本|路径|错误|失败|修复|安装|删除|测试|发现|问题|解决|环境|硬件|软件|内存|驱动|模型|训练|学习|改了|换了|用了|装|报错|崩溃|卡顿|GPU|CPU|端口|API|IP|key|token|向量|嵌入|服务器|显卡|主板|硬盘|预算|喜欢|讨厌|觉得|想|打算|我的|买了|换了|升级/i;

/** 纯问候重复串（"你好你好你好…"）整体跳过 */
const GREETING_ONLY_RE = /^(你好|您好|在吗|哈喽|嗨|hi|hello|hey|嗯|好|ok|谢谢|再见|拜拜|哈哈|早上好|晚上好|中午好)+$/i;

/** 纯标点 */
const PUNCT_ONLY_RE = /^[\s。，！？!?.,~～、；;:：""''()（）\-—·…]+$/;

// ── 检索命中裁剪后的字段（与 bridge.py _MEM_FIELDS 对齐的子集）──
interface MemHit {
  key?: string;
  content?: string;
  tags?: string;
  rrf_score?: number;
}

type HookApi = Parameters<DefinedToolPluginEntry["register"]>[0];

async function callBridge(
  cmd: string,
  args: Record<string, unknown> = {},
  timeoutMs = 30000,
) {
  const { stdout } = await execFileAsync(PYTHON, [bridgePath(), cmd, JSON.stringify(args)], {
    timeout: timeoutMs,
    maxBuffer: 1024 * 1024 * 8,
  });
  const parsed = JSON.parse(stdout);
  if (!parsed.ok) {
    throw new Error(parsed.error ?? "bridge error");
  }
  return parsed;
}

// ════════════════════════════════════════════════════════════════════
// 纯逻辑辅助（导出供单测）
// ════════════════════════════════════════════════════════════════════

/** OpenAI 风格 content 归一化：string | {type:"text"|"input_text",text}[] → 纯文本 */
export function extractMessageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part && typeof part === "object") {
          const p = part as { type?: string; text?: string };
          if (p.type === "text" || p.type === "input_text") return p.text ?? "";
          return "";
        }
        return "";
      })
      .join(" ")
      .trim();
  }
  return "";
}

/** 取 messages 里最后一条 user 消息文本（agent_end 事件形状） */
export function lastUserMessage(messages: unknown[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i] as { role?: string; content?: unknown } | null | undefined;
    if (m && typeof m === "object" && m.role === "user") {
      const text = extractMessageText(m.content).trim();
      if (text) return text;
    }
  }
  return null;
}

export function cjkCount(text: string): number {
  let n = 0;
  for (const ch of text) {
    const c = ch.codePointAt(0) ?? 0;
    if (c >= 0x4e00 && c <= 0x9fff) n++;
  }
  return n;
}

/** 查询词法 token：英文/数字词（≥2 字符）+ 中文 2-gram */
export function queryTokens(query: string): Set<string> {
  const toks = new Set<string>();
  for (const m of query.matchAll(/[A-Za-z0-9_.:/@\-]+/g)) {
    if (m[0].length >= 2) toks.add(m[0].toLowerCase());
  }
  for (const m of query.matchAll(/[\u4e00-\u9fff]{2,}/g)) {
    const run = m[0];
    for (let i = 0; i < run.length - 1; i++) toks.add(run.slice(i, i + 2));
  }
  return toks;
}

/** 词法重叠：查询 token 是否出现在命中条目的 content/key/tags 里。
 *  校准结论（10 相关 + 11 无关查询）：相关查询 lex=1 全覆盖，无关查询 lex=0 ——
 *  与 rrf_score≥0.5 组合可把"今天天气怎么样"类噪音完全挡在注入门外。 */
export function lexicalOverlap(query: string, hit: MemHit): boolean {
  const toks = queryTokens(query);
  if (toks.size === 0) return false;
  const blob = `${hit.content ?? ""} ${hit.key ?? ""} ${hit.tags ?? ""}`.toLowerCase();
  for (const t of toks) {
    if (blob.includes(t)) return true;
  }
  return false;
}

/** 注入格式：[相关记忆] 内容1 | 内容2 ...（每条截断省 token） */
export function formatMemories(hits: MemHit[]): string {
  const parts = hits.map((h) => {
    const c = (h.content ?? "").replace(/\s+/g, " ").trim();
    return c.length > AUTO_INJECT_CHAR_CAP ? `${c.slice(0, AUTO_INJECT_CHAR_CAP)}…` : c;
  });
  return `[相关记忆] ${parts.join(" | ")}`;
}

/** 自动 learn 质量门槛（参考 hermes-local sync_turn）：
 *  ① 长度 ≥20；② 非纯标点；③ 非纯问候；④ 中文信息密度（CJK≥4）或信号词命中。 */
export function passesQualityGate(text: string): boolean {
  const t = text.trim();
  if (t.length < MIN_LEARN_CHARS) return false;
  if (PUNCT_ONLY_RE.test(t)) return false;
  if (GREETING_ONLY_RE.test(t)) return false;
  const first = t.toLowerCase().split(/\s+/)[0] ?? "";
  if (GREETINGS.has(first) || GREETINGS.has(t.toLowerCase())) return false;
  if (cjkCount(t) >= MIN_CJK_CHARS) return true;
  if (SIGNAL_RE.test(t)) return true;
  return false;
}

/** FNV-1a 32bit（幂等去重哈希，无需 node:crypto） */
export function fnv1a(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16);
}

// ════════════════════════════════════════════════════════════════════
// hooks 状态（模块级单例）
// ════════════════════════════════════════════════════════════════════
const searchCache = new Map<string, { at: number; hits: MemHit[] }>();
const learnedHashes = new Map<string, number>(); // hash → ts（LRU 去重窗口）

function cacheSearch(prompt: string, hits: MemHit[]) {
  searchCache.set(prompt, { at: Date.now(), hits });
  if (searchCache.size > SEARCH_CACHE_MAX) {
    const oldest = searchCache.keys().next().value;
    if (oldest !== undefined) searchCache.delete(oldest);
  }
}

function markLearned(hash: string) {
  const now = Date.now();
  learnedHashes.set(hash, now);
  // 窗口内只保留最近的 N 条（超窗的旧条目直接清掉）
  if (learnedHashes.size > LEARNED_DEDUPE_MAX) {
    for (const [k, ts] of learnedHashes) {
      if (now - ts > 86_400_000) learnedHashes.delete(k);
    }
  }
  while (learnedHashes.size > LEARNED_DEDUPE_MAX) {
    const oldestKey = learnedHashes.keys().next().value;
    if (oldestKey === undefined) break;
    learnedHashes.delete(oldestKey);
  }
}

// ════════════════════════════════════════════════════════════════════
// hook 1: before_prompt_build → 自动注入相关记忆（prefetch 等效）
// ════════════════════════════════════════════════════════════════════
async function handleBeforePromptBuild(event: { prompt: string; messages: unknown[] }) {
  const prompt = (event.prompt ?? "").trim();
  if (!prompt) return;

  // 节流：相同 prompt 短时间内不重复检索
  const cached = searchCache.get(prompt);
  let hits: MemHit[] | null = null;
  if (cached && Date.now() - cached.at < SEARCH_CACHE_TTL_MS) {
    hits = cached.hits;
  } else {
    try {
      const res = await callBridge("search", { query: prompt, limit: AUTO_INJECT_MAX + 1 }, 2500);
      hits = Array.isArray(res.hits) ? (res.hits as MemHit[]) : [];
    } catch {
      hits = []; // bridge 失败（嵌入服务挂了等）→ 本轮不注入，绝不让 hook 拖垮 agent
    }
    cacheSearch(prompt, hits);
  }

  if (!hits || hits.length === 0) return;

  // 相关度门槛：分数低于阈值的不注入；top1 必须有词法重叠（防"天气/写诗"类噪音）
  const ranked = hits.filter((h) => (h.rrf_score ?? 0) >= AUTO_INJECT_MIN_SCORE);
  if (ranked.length === 0) return;
  if (!lexicalOverlap(prompt, ranked[0])) return;

  return { prependContext: formatMemories(ranked.slice(0, AUTO_INJECT_MAX)) };
}

// ════════════════════════════════════════════════════════════════════
// hook 2: agent_end → 自动 learn 沉淀（sync_turn 等效）
// ════════════════════════════════════════════════════════════════════
async function handleAgentEnd(
  event: { success?: boolean; messages?: unknown[]; runId?: string },
  ctx: { sessionKey?: string; sessionId?: string; runId?: string; logger?: HookApi["logger"] },
) {
  if (event.success === false) return; // 失败轮不沉淀
  // Episodic 心跳: 每轮 agent_end 都 checkin(开窗/续窗/超 30min 旋转),
  // 独立于 learn 质量门槛——窗口是纯时间语义, 不能因问候/短句被跳过
  // (2026-08-15 小龙虾实测: 旧窗 id20 永开 = checkin 从未被自动调用)
  const sessionRef = ctx.sessionKey ?? ctx.sessionId ?? event.runId ?? "";
  if (sessionRef) {
    try {
      await callBridge("episodic", { action: "checkin", session_id: sessionRef }, CHECKIN_TIMEOUT_MS);
    } catch (err) {
      ctx.logger?.debug?.(`[memorykit-overlay] episodic checkin skipped: ${err}`);
    }
  }

  const userMsg = lastUserMessage(event.messages ?? []);
  if (!userMsg) return;

  const content = userMsg.slice(0, LEARN_MAX_CHARS);
  if (!passesQualityGate(content)) return;

  // 幂等：同一内容（同窗口内）不重复 learn
  const hash = fnv1a(content);
  if (learnedHashes.has(hash)) return;

  try {
    await callBridge(
      "learn",
      { content, source: "session", source_session: sessionRef },
      LEARN_TIMEOUT_MS,
    );
  } catch (err) {
    ctx.logger?.debug?.(`[memorykit-overlay] agent_end auto-learn skipped: ${err}`);
    return;
  }
  markLearned(hash);
  // 学了新东西 → 下一轮检索重新查（清空 prompt 缓存）
  searchCache.clear();
  ctx.logger?.debug?.("[memorykit-overlay] agent_end auto-learned a memory");
}

/** 启动自检：解释器到底有没有 jieba。
 *  缺了 memorykit 会静默降级（中文专名全灭、entities=NULL），必须显式告警
 *  —— 2026-10-15 修补, REVIEW.md P0-A（这条坑 README 记录过一次，MCP 侧又踩了一次）。 */
async function checkPythonCapability(logger?: HookApi["logger"]) {
  try {
    await execFileAsync(PYTHON, ["-c", "import jieba"], { timeout: 30_000 });
  } catch {
    logger?.warn?.(
      `[memorykit-overlay] ${PYTHON} 没有 jieba → 实体抽取会静默降级（中文专名全灭）。` +
        " 修复：设 MEMORYKIT_PY=/home/joe/venv-flagembed/bin/python3",
    );
  }
}

/** 注册两个自动 hook（manifest activation.onCapabilities: ["hook"] 配套） */
const hooksRegisteredOn = new WeakSet<object>();

function registerHooks(api: HookApi) {
  // 同一个 api 只注册一次：宿主若重复调 register，hook 会双份触发（每轮注入两次）
  // —— 2026-10-15 修补, REVIEW.md P2-H
  if (hooksRegisteredOn.has(api as object)) return;
  hooksRegisteredOn.add(api as object);
  // 默认 before_prompt_build 超时 15s 太慢——每轮都跑，收短到 3s，失败静默
  api.on("before_prompt_build", handleBeforePromptBuild, { timeoutMs: HOOK_PROMPT_TIMEOUT_MS });
  api.on("agent_end", handleAgentEnd, { timeoutMs: HOOK_AGENT_END_TIMEOUT_MS });
  void checkPythonCapability(api.logger);
}

// ════════════════════════════════════════════════════════════════════
// 插件本体
// ════════════════════════════════════════════════════════════════════
const plugin = defineToolPlugin({
  id: "memorykit-overlay",
  name: "MemoryKit Overlay",
  description:
    "Memory-overlay 记忆系统：learn 写入 / search 多路检索 / correct 纠正锚定 / overlay 遮蔽回溯 / episodic 时间索引 / knowledge 知识层。state.db 原文永不删，memory 是上层影子。自动接线：before_prompt_build 注入相关记忆 + agent_end 自动 learn。",
  // 声明 hook 能力（openclaw plugins build 会把它写进 openclaw.plugin.json 的 activation）
  activation: { onStartup: true, onCapabilities: ["hook"] },
  tools: (tool) => [
    tool({
      name: "mk_learn",
      label: "Memory Learn",
      description:
        "写入一条记忆。自动分类、抽取实体、打时间锚点、去重。source 建议: task(任务结论)/session(对话提炼)/correction(纠正后重记)/discovery(新发现)。",
      parameters: Type.Object({
        content: Type.String({ description: "要记住的内容（一句话干货，不要流水账）" }),
        source: Type.Optional(
          Type.Union([
            Type.Literal("task"),
            Type.Literal("session"),
            Type.Literal("correction"),
            Type.Literal("discovery"),
          ], { description: "记忆来源分类" }),
        ),
        source_session: Type.Optional(Type.String({ description: "来源会话 id（用于后续回溯 state 原文）" })),
        event_time: Type.Optional(Type.String({ description: "事件发生时间 YYYY-MM-DD HH:MM:SS" })),
      }),
      execute: async ({ content, source, source_session, event_time }) =>
        callBridge("learn", { content, source, source_session, event_time }),
    }),

    tool({
      name: "mk_search",
      label: "Memory Search",
      description:
        "多路检索记忆：FTS5 关键词 + 语义向量 + 实体加权融合。排序会参考验证状态（verified 上浮、disputed 重罚）。mode: multi(默认融合)/semantic(纯语义)/two_stage(粗排+精排)。",
      parameters: Type.Object({
        query: Type.String({ description: "检索内容描述" }),
        limit: Type.Optional(Type.Number({ description: "返回条数, 默认10" })),
        mode: Type.Optional(
          Type.Union([
            Type.Literal("multi"),
            Type.Literal("semantic"),
            Type.Literal("two_stage"),
          ], { description: "检索模式" }),
        ),
        category: Type.Optional(Type.String({ description: "按类别过滤" })),
        ref_time: Type.Optional(Type.String({ description: "时间检索基准时间 YYYY-MM-DD[ HH:MM:SS]，与 max_age_days 成对生效（年龄窗口 [ref_time - max_age_days, ref_time]）" })),
        max_age_days: Type.Optional(Type.Number({ description: "时间检索年龄窗口天数，与 ref_time 成对生效" })),
        strict_time_window: Type.Optional(Type.Boolean({ description: "时间窗口硬过滤（默认 false 仅软重排不剔除）" })),
      }),
      execute: async ({ query, limit, mode, category, ref_time, max_age_days, strict_time_window }) =>
        callBridge("search", { query, limit, mode, category, ref_time, max_age_days, strict_time_window }),
    }),

    tool({
      name: "mk_get",
      label: "Memory Get",
      description: "按 key 精确取一条记忆。key 形如 env/0806_xxx 或 user/xxx。",
      parameters: Type.Object({
        key: Type.String({ description: "记忆 key" }),
      }),
      execute: async ({ key }) => callBridge("get", { key }),
    }),

    tool({
      name: "mk_correct",
      label: "Memory Correct",
      description:
        "用户纠正记忆：confidence -0.3 + 标记 disputed。correction 是唯一来自真实世界的外部锚点——用户显式说'不对'时才调用。之后检索排序会重罚该条。",
      parameters: Type.Object({
        key: Type.String({ description: "要纠正的记忆 key" }),
        signal: Type.Optional(Type.String({ description: "纠正信号，如'不对'/'错了'" })),
        session_id: Type.Optional(Type.String({ description: "当前会话 id" })),
      }),
      execute: async ({ key, signal, session_id }) =>
        callBridge("correct", { key, signal, session_id }),
    }),

    tool({
      name: "mk_reinforce",
      label: "Memory Reinforce",
      description:
        "正向强化：explicit(+0.15, 用户显式确认'对')/implicit(+0.05, 引用且无反驳)。disputed 的记忆被强化后恢复为 verified。",
      parameters: Type.Object({
        key: Type.String({ description: "记忆 key" }),
        mode: Type.Optional(
          Type.Union([
            Type.Literal("explicit"),
            Type.Literal("implicit"),
          ], { description: "强化模式" }),
        ),
        session_id: Type.Optional(Type.String({ description: "当前会话 id" })),
      }),
      execute: async ({ key, mode, session_id }) =>
        callBridge("reinforce", { key, mode, session_id }),
    }),

    tool({
      name: "mk_verify",
      label: "Memory Verify",
      description:
        "验证记忆管线。单条: 交叉检查来源 session → 评分 → 低分自动降级(可关)。batch: 批量验证 pending 记忆(limit/only_pending 可调), 返回含 freshness 统计 fresh_checked/fresh_resolved(memorykit v1.2.0)。",
      parameters: Type.Object({
        key: Type.Optional(Type.String({ description: "单条模式: 记忆 key" })),
        auto_degrade: Type.Optional(Type.Boolean({ description: "低分自动降级, 默认true" })),
        threshold: Type.Optional(Type.Number({ description: "降级阈值, 默认0.3" })),
        batch: Type.Optional(Type.Boolean({ description: "批量验证模式: 整库扫描验证, 默认false(单条)" })),
        limit: Type.Optional(Type.Number({ description: "batch 模式: 单批最大数量, 默认100" })),
        only_pending: Type.Optional(Type.Boolean({ description: "batch 模式: 只验证 pending 状态(默认true), false=全部" })),
      }),
      execute: async ({ key, auto_degrade, threshold, batch, limit, only_pending }) =>
        batch
          ? callBridge("verify_batch", { auto_degrade, threshold, limit, only_pending })
          : callBridge("verify", { key, auto_degrade, threshold }),
    }),

    tool({
      name: "mk_overlay",
      label: "Memory Overlay",
      description:
        "memory-overlay 核心：遮蔽管理。action=resolve 检索高置信记忆（密集片段）；action=expand 沿指针回溯 state.db 原文（深究时用，memory→state 方向）。",
      parameters: Type.Object({
        action: Type.Union([
          Type.Literal("resolve"),
          Type.Literal("expand"),
        ], { description: "resolve=取密集记忆片段 / expand=展开原文" }),
        query: Type.Optional(Type.String({ description: "resolve 时: 检索描述" })),
        key: Type.Optional(Type.String({ description: "expand 时: 要展开的记忆 key" })),
        limit: Type.Optional(Type.Number({ description: "resolve 时返回条数, 默认5" })),
      }),
      execute: async ({ action, query, key, limit }) =>
        callBridge("overlay", { action, query, key, limit }),
    }),

    tool({
      name: "mk_episodic",
      label: "Memory Episodic",
      description:
        "时间索引：会话开始/结束/按时间检索。主索引是时间——记录'什么时候发生了什么'。action: start/end/checkin/close/window/list/search。checkin=遮蔽窗心跳(开窗/续窗/30min超窗旋转), close=关窗+摘要, window=按时间窗查。",
      parameters: Type.Object({
        action: Type.Union([
          Type.Literal("start"),
          Type.Literal("end"),
          Type.Literal("checkin"),
          Type.Literal("close"),
          Type.Literal("window"),
          Type.Literal("list"),
          Type.Literal("search"),
        ], { description: "操作类型" }),
        session_id: Type.Optional(Type.String({ description: "start/end/checkin/close 时必填: 会话 id" })),
        summary: Type.Optional(Type.String({ description: "end/close 时: 会话摘要" })),
        query: Type.Optional(Type.String({ description: "search 时: 检索描述" })),
        start: Type.Optional(Type.String({ description: "window 时: 起始时间(ISO)" })),
        end: Type.Optional(Type.String({ description: "window 时: 结束时间(ISO)" })),
        limit: Type.Optional(Type.Number({ description: "list/search/window 返回条数" })),
      }),
      execute: async ({ action, session_id, summary, query, start, end, limit }) =>
        callBridge("episodic", { action, session_id, summary, query, start, end, limit }),
    }),

    tool({
      name: "mk_knowledge",
      label: "Memory Knowledge",
      description:
        "知识层（话题/关键词辅索引）：把高价值记忆提升为知识节点、按话题检索、查关联图。action: promote/search/list/graph。",
      parameters: Type.Object({
        action: Type.Union([
          Type.Literal("promote"),
          Type.Literal("search"),
          Type.Literal("list"),
          Type.Literal("graph"),
        ], { description: "操作类型" }),
        memory_key: Type.Optional(Type.String({ description: "promote 时: 要提升的记忆 key" })),
        title: Type.Optional(Type.String({ description: "promote 时: 知识节点标题" })),
        domain: Type.Optional(Type.String({ description: "promote/list 时: 领域分类" })),
        query: Type.Optional(Type.String({ description: "search 时: 检索描述" })),
        kid: Type.Optional(Type.Number({ description: "graph 时: 知识节点 id" })),
        limit: Type.Optional(Type.Number({ description: "返回条数" })),
      }),
      execute: async ({ action, memory_key, title, domain, query, kid, limit }) =>
        callBridge("knowledge", { action, memory_key, title, domain, query, kid, limit }),
    }),

    tool({
      name: "mk_stats",
      label: "Memory Stats",
      description: "记忆库统计：总量、分类分布、置信度、验证状态分布。",
      parameters: Type.Object({}),
      execute: async () => callBridge("stats"),
    }),
  ],
});

// 附加自动 hook：defineToolPlugin 只注册工具，register 外面再包一层挂 hooks。
// （SDK 没给 hooks 入口时的绕法；registerHooks 内部用 WeakSet 保证幂等，
//   即使宿主重复调用 register 也不会双份注册 —— 2026-10-15, REVIEW.md P2-H）
const registeredApis = new WeakSet<object>();
const _origRegister = plugin.register;
plugin.register = (api) => {
  // 工具 + hook **一起**幂等：宿主重复调 register 时，只挡 hook 不够——
  // _origRegister 会再跑一遍，10 个工具也注册两遍。（2026-10-15 补，本地模型复审提出）
  if (registeredApis.has(api as object)) return;
  registeredApis.add(api as object);
  _origRegister(api);
  registerHooks(api);
};

export default plugin;
