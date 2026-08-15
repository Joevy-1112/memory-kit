import { describe, expect, it, beforeAll, afterAll } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import entry from "./index.js";
import { getToolPluginMetadata } from "openclaw/plugin-sdk/tool-plugin";
import {
  extractMessageText,
  lastUserMessage,
  passesQualityGate,
  lexicalOverlap,
  queryTokens,
  formatMemories,
  fnv1a,
} from "./index.js";

// ── 类型：fake api 捕获 on() 注册的 hook ──
interface CapturedHook {
  name: string;
  handler: (event: unknown, ctx: unknown) => unknown;
  opts?: { priority?: number; timeoutMs?: number };
}

function makeFakeApi(hooks: CapturedHook[]) {
  return {
    registerTool: () => {},
    registerHook: () => {},
    logger: { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} },
    on: (name: string, handler: unknown, opts?: { priority?: number; timeoutMs?: number }) => {
      hooks.push({ name, handler: handler as CapturedHook["handler"], opts });
    },
  };
}

describe("memorykit-overlay", () => {
  it("declares the 10 mk_* tool metadata", () => {
    expect(getToolPluginMetadata(entry)?.tools.map((tool) => tool.name)).toEqual([
      "mk_learn",
      "mk_search",
      "mk_get",
      "mk_correct",
      "mk_reinforce",
      "mk_verify",
      "mk_overlay",
      "mk_episodic",
      "mk_knowledge",
      "mk_stats",
    ]);
  });

  it("registers both automatic hooks (before_prompt_build + agent_end)", () => {
    const hooks: CapturedHook[] = [];
    entry.register(makeFakeApi(hooks) as never);
    const names = hooks.map((h) => h.name);
    expect(names).toContain("before_prompt_build");
    expect(names).toContain("agent_end");
    const bpb = hooks.find((h) => h.name === "before_prompt_build");
    const ae = hooks.find((h) => h.name === "agent_end");
    expect(bpb?.opts?.timeoutMs).toBeLessThanOrEqual(5000); // 每轮都跑，必须短超时
    expect(ae?.opts?.timeoutMs).toBeLessThanOrEqual(10000);
  });

  describe("message 提取", () => {
    it("extractMessageText: 字符串 / text 数组 / input_text 数组", () => {
      expect(extractMessageText("你好")).toBe("你好");
      expect(extractMessageText([{ type: "text", text: "a" }, { type: "text", text: "b" }])).toBe("a b");
      expect(extractMessageText([{ type: "input_text", text: "x" }])).toBe("x");
      expect(extractMessageText([{ type: "tool_use", name: "mk_search" }])).toBe("");
      expect(extractMessageText(123 as never)).toBe("");
      expect(extractMessageText(null as never)).toBe("");
    });

    it("lastUserMessage: 取最后一条 user，跳过 assistant/tool", () => {
      const messages = [
        { role: "assistant", content: [{ type: "text", text: "你好" }] },
        { role: "tool", content: "{}" },
        { role: "user", content: "第一条" },
        { role: "assistant", content: "回复" },
        { role: "user", content: [{ type: "text", text: "第二条" }] },
      ];
      expect(lastUserMessage(messages)).toBe("第二条");
      expect(lastUserMessage([])).toBeNull();
      expect(lastUserMessage([{ role: "assistant", content: "x" }])).toBeNull();
    });
  });

  describe("自动 learn 质量门槛（参考 hermes-local sync_turn）", () => {
    it("通过：≥20字 + 中文密度", () => {
      expect(passesQualityGate("我的猫叫小鸡翅，是只银渐层，特别粘人，最喜欢趴键盘")).toBe(true);
    });
    it("通过：≥20字 + 英文技术信号词", () => {
      expect(passesQualityGate("server ip is 192.168.5.50 running ubuntu with hermes")).toBe(true);
    });
    it("拒绝：过短", () => {
      expect(passesQualityGate("你好")).toBe(false);
      expect(passesQualityGate("我的猫叫小鸡翅")).toBe(false);
    });
    it("拒绝：纯问候重复", () => {
      expect(passesQualityGate("你好你好你好你好你好你好你好")).toBe(false);
    });
    it("拒绝：纯标点", () => {
      expect(passesQualityGate("！！！！！！！！！！！！！！！！！")).toBe(false);
    });
    it("拒绝：无中文无信号词的闲聊长句", () => {
      expect(passesQualityGate("hahaha that is really funny you know")).toBe(false);
    });
  });

  describe("词法重叠（注入噪音闸门）", () => {
    it("相关查询命中", () => {
      expect(lexicalOverlap("显卡多少钱", { content: "显卡预算550块，显存12G" })).toBe(true);
      expect(lexicalOverlap("我的猫叫什么", { content: "Joe养了只银渐层猫叫小鸡翅" })).toBe(true);
      expect(lexicalOverlap("docker compose", { content: "项目部署用Docker Compose" })).toBe(true);
    });
    it("无关查询不命中", () => {
      expect(lexicalOverlap("今天天气怎么样", { content: "显卡预算550块，显存12G" })).toBe(false);
      expect(lexicalOverlap("帮我写首诗", { content: "服务器IP是192.168.5.50" })).toBe(false);
    });
    it("queryTokens 生成英文词 + 中文 bigram", () => {
      const toks = queryTokens("显卡 docker");
      expect(toks.has("显卡")).toBe(true); // 2字中文 → 单个 bigram 就是自身
      expect(toks.has("docker")).toBe(true);
      const toks3 = queryTokens("显卡预算");
      expect(toks3.has("显卡")).toBe(true);
      expect(toks3.has("卡预")).toBe(true);
      expect(toks3.has("预算")).toBe(true);
    });
  });

  describe("注入格式", () => {
    it("前缀 + | 连接 + 截断", () => {
      const out = formatMemories([
        { content: "短内容" },
        { content: "长内容".repeat(200) },
      ]);
      expect(out.startsWith("[相关记忆] 短内容 | 长内容")).toBe(true);
      expect(out.endsWith("…")).toBe(true);
      expect(out.length).toBeLessThan(400);
    });
  });

  it("fnv1a 幂等去重哈希确定", () => {
    expect(fnv1a("abc")).toBe(fnv1a("abc"));
    expect(fnv1a("abc")).not.toBe(fnv1a("abd"));
  });
});

// ── 端到端：hook handler 走真实 bridge（临时库，隔离小龙虾正式数据）──
describe("hooks 端到端（真实 bridge + 临时库）", () => {
  const dbPath = path.join(os.tmpdir(), `mktest_hooks_${Date.now()}.db`);
  const statePath = path.join(os.tmpdir(), `mktest_hooks_${Date.now()}_state.db`);
  const bridgePath = path.join(process.cwd(), "bridge.py");
  let hooks: CapturedHook[];

  beforeAll(() => {
    process.env.MEMORYKIT_DB = dbPath;
    process.env.MEMORYKIT_STATE_DB = statePath;
    // vitest 下 import.meta.url 指向 src/，用环境变量让 handler 指向仓库根目录的 bridge.py
    process.env.MEMORYKIT_OVERLAY_BRIDGE = bridgePath;
    hooks = [];
    entry.register(makeFakeApi(hooks) as never);
  });

  afterAll(() => {
    for (const p of [dbPath, statePath]) {
      try { fs.rmSync(p, { force: true }); } catch { /* 忽略 */ }
    }
  });

  const runAgentEnd = async (messages: unknown[], sessionKey = "test-sess") => {
    const h = hooks.find((x) => x.name === "agent_end")!;
    await h.handler(
      { success: true, runId: "run-1", messages },
      { sessionKey, logger: { debug: () => {} } },
    );
  };

  const runPromptBuild = async (prompt: string) => {
    const h = hooks.find((x) => x.name === "before_prompt_build")!;
    return h.handler({ prompt, messages: [] }, {});
  };

  it("agent_end 正常对话后自动 learn（带 source_session）", async () => {
    await runAgentEnd([
      { role: "assistant", content: "你好，有什么可以帮你？" },
      { role: "user", content: "我的猫叫小鸡翅，是只银渐层，特别粘人，最喜欢趴键盘" },
    ]);
    // 通过 bridge 直接查库：自动 learn 落了一条
    const { execFileSync } = await import("node:child_process");
    const out = execFileSync(
      "python3",
      [bridgePath, "search", JSON.stringify({ query: "猫", limit: 5 })],
      { encoding: "utf-8", env: process.env as Record<string, string>, timeout: 60000 },
    );
    const parsed = JSON.parse(out);
    expect(parsed.ok).toBe(true);
    const hit = parsed.hits.find((h: { content?: string }) => (h.content ?? "").includes("小鸡翅"));
    expect(hit).toBeTruthy();
    expect(hit.source_session).toBe("test-sess");
  });

  it("同一内容不重复 learn（幂等）", async () => {
    // 第二次相同 user 消息 → 不再写新条目（数量不变）
    await runAgentEnd([
      { role: "user", content: "我的猫叫小鸡翅，是只银渐层，特别粘人，最喜欢趴键盘" },
    ]);
    const { execFileSync } = await import("node:child_process");
    const out = execFileSync(
      "python3",
      [bridgePath, "search", JSON.stringify({ query: "猫", limit: 5 })],
      { encoding: "utf-8", env: process.env as Record<string, string>, timeout: 60000 },
    );
    const parsed = JSON.parse(out);
    const hits = parsed.hits.filter((h: { content?: string }) => (h.content ?? "").includes("小鸡翅"));
    expect(hits.length).toBe(1);
  });

  it("before_prompt_build 相关提问自动注入相关记忆", async () => {
    const res = (await runPromptBuild("我的猫叫什么名字")) as { prependContext?: string } | undefined;
    expect(res?.prependContext).toBeTruthy();
    expect(res!.prependContext!.includes("小鸡翅")).toBe(true);
    expect(res!.prependContext!.startsWith("[相关记忆]")).toBe(true);
  });

  it("before_prompt_build 无关提问不注入（省 token）", async () => {
    const res = await runPromptBuild("今天天气怎么样");
    expect(res).toBeUndefined();
  });
});
