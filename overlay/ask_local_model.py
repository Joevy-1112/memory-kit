#!/usr/bin/env python3
"""把本地 qwen3.8-flash-next 当 subagent 用（strata 服务, :8080, 512k 上下文）。

用法:
  python3 ask_local_model.py --file A.md --file B.diff --prompt "审一下这份补丁" -o out.md
  python3 ask_local_model.py --prompt "只回答两个字：收到"

说明:
  - 服务是 OpenAI 兼容的 /v1/chat/completions（strata --serve, 本机 8080）。
  - 默认关掉思考链输出（reasoning_content 仍会打印到 stderr 供排查）。
  - 大文件直接塞进 prompt：512k 上下文，几万 token 无所谓，但**prefill 慢**
    （实测 ~44 tok/s），所以长任务建议后台跑。
"""
import argparse
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "qwen3.8-flash-next-iq2_xs"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="任务描述")
    ap.add_argument("--file", action="append", default=[], help="附带的文件（可多次）")
    ap.add_argument("--system", default="你是一个严格的代码评审者，只给可验证的具体结论，不要客套。")
    ap.add_argument("--max-tokens", type=int, default=6000)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("-o", "--out", help="把回答写到文件")
    ap.add_argument("--save-reasoning", help="把思考链(reasoning_content)另存一份")
    args = ap.parse_args()

    parts = [args.prompt]
    for path in args.file:
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
        except OSError as e:
            print(f"跳过 {path}: {e}", file=sys.stderr)
            continue
        parts.append(f"\n\n===== FILE: {path} =====\n{body}")
    user = "\n".join(parts)

    req_body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": args.system},
                     {"role": "user", "content": user}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "stream": False,
    }).encode("utf-8")

    t0 = time.time()
    req = urllib.request.Request(URL, data=req_body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    dt = time.time() - t0

    msg = (data.get("choices") or [{}])[0].get("message", {})
    content = msg.get("content") or ""
    reason = msg.get("reasoning_content") or ""
    usage = data.get("usage", {})
    print(f"[用时 {dt:.1f}s  prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}]",
          file=sys.stderr)
    if reason:
        print(f"[thinking {len(reason)} chars] {reason[:400]}…", file=sys.stderr)
    if args.save_reasoning and reason:
        with open(args.save_reasoning, "w", encoding="utf-8") as fh:
            fh.write(reason)
        print(f"-> {args.save_reasoning} ({len(reason)} chars)", file=sys.stderr)
    if not content:
        print("[WARN] content 为空——很可能是 max_tokens 被思考链吃光了，调大 --max-tokens 再试",
              file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"-> {args.out} ({len(content)} chars)")
    else:
        print(content)


if __name__ == "__main__":
    main()
