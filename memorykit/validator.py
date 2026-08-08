#!/usr/bin/env python3
"""memorykit.validator — 记忆活验证原语（Prototype）。

参考 scope-recall 的 freshness 体系（freshness.py 状态机 + fact_freshness
表的 validator_kind/validator_spec 契约），把"活验证执行器"落成纯标准库
的最小实现，供 memorykit 的验证闭环（verify()/age() 侧）在库内直接调用。

设计要点（为什么适合我们的架构）：
- memorykit 核心只依赖 SQLite + numpy，validator 同样零第三方依赖（urllib/
  subprocess 均为标准库）——不破坏"pip install 即用"的约束。
- 三种 validator：file_exists / command / http，对应 scope-recall 的
  VALIDATOR_KINDS 契约；unknown 值 fail-closed 到 manual（不自动判定新鲜）。
- 每个检查返回结构化结果（ok/detail/elapsed_ms/checked_at），调用方决定
  怎么用它（写 freshness 状态、排序惩罚、审计日志）——本模块只做"测"，不
  做"判"，与 freshness 状态机解耦。
- 不抛契约：任何非法 spec（畸形 timeout、类型错误、未知 scheme……）都
  结构化返回 ok=False + detail，绝不向调用方抛异常——一条坏 spec 不能
  炸掉整条验证管线（run_checks 批量同理）。
- 信任模型：validator_spec 是可信的 agent 配置（由 memorykit 调用方/库内
  接线写入），绝不可从不可信内容（如会话原文）自动派生。http 执行器只
  接受 http/https scheme（拒绝 file:// 等本地文件读取向量），detail 回显
  URL 会脱敏 userinfo 凭据；http(s) 下访问内网（127.0.0.1/169.254.169.254/
  RFC1918）由上述信任模型兜底——调用方不应把不可信来源的 URL 写进 spec。

用法：
  from memorykit.validator import validate, normalize_validator_kind
  r = validate({"kind": "file_exists", "path": "/etc/hostname"})
  r = validate({"kind": "http", "url": "http://127.0.0.1:8082/v1/models", "method": "HEAD"})
  r = validate({"kind": "command", "command": "ls /tmp/marker"})

返回：{"ok": bool, "kind": str, "checked_at": str, "elapsed_ms": float,
       "detail": str}；未知 kind / 非法 spec → ok=False + detail 说明（不抛异常）。
"""
from __future__ import annotations

import math
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

# 与 scope-recall freshness.VALIDATOR_KINDS 对齐的公共契约
VALIDATOR_KINDS = ("file_exists", "command", "http", "manual", "none")

# 别名归一化（照搬 scope-recall 的宽松入口，保证存量写法不炸）
_VALIDATOR_KIND_ALIASES = {
    "": "none",
    "none": "none",
    "no_validation": "none",
    "static": "none",
    "permanent": "none",
    "file": "file_exists",
    "path": "file_exists",
    "exists": "file_exists",
    "file_exists": "file_exists",
    "shell": "command",
    "cmd": "command",
    "command": "command",
    "http": "http",
    "https": "http",
    "url": "http",
    "http_get": "http",
    "http_head": "http",
    "manual": "manual",
    "manual_live_check": "manual",
    "human": "manual",
}

DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_COMMAND_TIMEOUT = 10.0
MIN_TIMEOUT = 0.1  # timeout 下限钳制：spec 笔误（0/负数）不静默禁用检查
MAX_COMMAND_OUTPUT_CHARS = 500
MAX_DETAIL_CHARS = 800
_HTTP_SCHEMES = ("http", "https")  # http 执行器 scheme 白名单（拒绝 file:// 等）


def normalize_validator_kind(kind: Any) -> str:
    """把各种写法归一化到 VALIDATOR_KINDS。

    与 scope-recall 语义一致：未知的非空取值 fail-closed 到 manual
    （宁可标记"需要人工复核"，也不把过期操作事实误当永恒偏好）。
    """
    normalized = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in VALIDATOR_KINDS:
        return normalized
    return _VALIDATOR_KIND_ALIASES.get(normalized, "manual" if normalized else "none")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(ok: bool, kind: str, detail: str, elapsed_ms: float) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "kind": kind,
        "checked_at": _now_iso(),
        "elapsed_ms": round(elapsed_ms, 3),
        "detail": detail[:MAX_DETAIL_CHARS],
    }


def _extract(mapping: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """同时兼容 {'path': ...} 与 {'spec': {'path': ...}} 两种 spec 形态。"""
    spec = mapping.get("spec")
    if isinstance(spec, Mapping):
        return spec.get(key, mapping.get(key, default))
    return mapping.get(key, default)


def _coerce_timeout(value: Any, default: float) -> float:
    """把 spec 里的 timeout 安全转成 float，畸形值回退默认（绝不抛）。

    ValueError/TypeError（字符串 "abc"、列表、None 等）、非有限值（nan/inf）
    一律回退默认；0/负数钳制到 MIN_TIMEOUT——既守住"不抛"契约，也不让
    spec 笔误把每条检查静默变成"秒失败"。
    """
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(parsed, MIN_TIMEOUT)


def _as_bool(value: Any) -> bool:
    """严格布尔解析：只有真布尔 True 或白名单字符串才返回 True。

    安全要点：bool("false") == True 是经典脚枪——spec 大概率由 agent/LLM
    手写 JSON，字符串布尔（"false"/"0"/""）是常见错误形态，一旦误启用
    shell 就静默击穿默认的无 shell 安全保证。这里 fail-closed：只认真布尔
    （True/False）与白名单 {"true", "1", "yes"}（大小写不敏感），其余
    （含数字、任意其他字符串）一律 False。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _redact_url(url: str) -> str:
    """detail 回显 URL 前脱敏 userinfo（http://user:pass@host → http://host）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):  # IPv6 字面量加回方括号
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # 非数字端口（host:abc）不炸，只丢端口
        port = ""
    netloc = f"{host}{port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _check_file_exists(spec: Mapping[str, Any]) -> dict[str, Any]:
    import os

    path = str(_extract(spec, "path") or _extract(spec, "file") or "").strip()
    if not path:
        return _result(False, "file_exists", "missing 'path' in spec", 0.0)
    try:
        ok = os.path.exists(path)
        detail = f"path={'exists' if ok else 'missing'}: {path}"
    except Exception as exc:  # 防御：异常路径也算验证失败，不抛
        ok, detail = False, f"check error: {type(exc).__name__}: {exc}"
    return _result(ok, "file_exists", detail, 0.0)


def _check_command(spec: Mapping[str, Any]) -> dict[str, Any]:
    command = str(_extract(spec, "command") or _extract(spec, "cmd") or "").strip()
    if not command:
        return _result(False, "command", "missing 'command' in spec", 0.0)
    timeout = _coerce_timeout(_extract(spec, "timeout", DEFAULT_COMMAND_TIMEOUT),
                              DEFAULT_COMMAND_TIMEOUT)
    started = time.monotonic()
    # 默认 shell=False + shlex 拆分（避免裸 shell 注入）；仅当 spec 显式给出
    # 真布尔 True（或白名单字符串 "true"/"1"/"yes"）才走 shell——字符串
    # "false"/"0"/"" 绝不启用 shell（fail-closed，见 _as_bool）
    use_shell = _as_bool(_extract(spec, "shell", False))
    try:
        if use_shell:
            proc = subprocess.run(command, shell=True, capture_output=True,
                                  text=True, timeout=timeout)
        else:
            argv = shlex.split(command)
            if not argv:
                return _result(False, "command", "empty command after shlex split", 0.0)
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        elapsed = (time.monotonic() - started) * 1000.0
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        snippet = " ".join(output.split())[:MAX_COMMAND_OUTPUT_CHARS]
        detail = f"exit={proc.returncode}"
        if snippet:
            detail += f" output={snippet!r}"
        return _result(proc.returncode == 0, "command", detail, elapsed)
    except subprocess.TimeoutExpired:
        elapsed = (time.monotonic() - started) * 1000.0
        return _result(False, "command", f"timeout after {timeout}s", elapsed)
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000.0
        return _result(False, "command", f"check error: {type(exc).__name__}: {exc}", elapsed)


def _check_http(spec: Mapping[str, Any]) -> dict[str, Any]:
    url = str(_extract(spec, "url") or "").strip()
    if not url:
        return _result(False, "http", "missing 'url' in spec", 0.0)
    # scheme 白名单：urllib 默认 opener 带 FileHandler，file:// 会真读本地
    # 文件（可读/不可读可区分 = 本地文件存在性探针）；入口即拒，任何非
    # http(s) scheme 都不触达 IO 层（stdlib 已拦截 redirect 到非 http(s)）。
    try:
        parsed_url = urllib.parse.urlparse(url)
    except ValueError:
        return _result(False, "http", "invalid url in spec", 0.0)
    if parsed_url.scheme.lower() not in _HTTP_SCHEMES:
        return _result(False, "http",
                       f"unsupported URL scheme {parsed_url.scheme!r}: only http/https allowed",
                       0.0)
    # detail 回显用脱敏 URL（userinfo 凭据不进日志）
    display_url = _redact_url(url)
    timeout = _coerce_timeout(_extract(spec, "timeout", DEFAULT_HTTP_TIMEOUT),
                              DEFAULT_HTTP_TIMEOUT)
    method = str(_extract(spec, "method", "GET") or "GET").strip().upper()
    if method not in ("GET", "HEAD"):
        method = "GET"
    started = time.monotonic()
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "memorykit-validator/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = (time.monotonic() - started) * 1000.0
            status = getattr(resp, "status", 200)
            ok = 200 <= int(status) < 400
            detail = f"http {status} {display_url}"
            if not ok:
                detail += f" (status outside 2xx/3xx)"
            return _result(ok, "http", detail, elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = (time.monotonic() - started) * 1000.0
        # 4xx/5xx 是"可验证的失败"——HTTPError 本身证明服务在响应
        ok = 200 <= int(exc.code) < 400
        return _result(ok, "http", f"http {exc.code} {display_url}", elapsed)
    except urllib.error.URLError as exc:
        elapsed = (time.monotonic() - started) * 1000.0
        reason = getattr(exc, "reason", exc)
        return _result(False, "http", f"connection error: {reason}", elapsed)
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000.0
        return _result(False, "http", f"check error: {type(exc).__name__}: {exc}", elapsed)


def validate(spec: Any) -> dict[str, Any]:
    """执行一次活验证。spec 支持两种形态：

    - 字符串/简单 dict：{"kind": "file_exists", "path": "..."}
    - scope-recall 兼容形态：{"validator_kind": "http", "validator_spec": {...}}

    返回结构化结果（ok/kind/checked_at/elapsed_ms/detail）。
    manual / none 不做自动检查（manual 需要人来确认，none 不追踪）。
    """
    if isinstance(spec, str):
        # 裸字符串当作 file_exists 的路径（最常用的快查形态）
        return _check_file_exists({"path": spec})
    if not isinstance(spec, Mapping):
        return _result(False, "manual", f"unsupported spec type: {type(spec).__name__}", 0.0)

    kind = normalize_validator_kind(_extract(spec, "kind", _extract(spec, "validator_kind", "")))
    raw_inner = spec.get("validator_spec")
    inner: Mapping[str, Any] = raw_inner if isinstance(raw_inner, Mapping) else spec

    if kind == "file_exists":
        return _check_file_exists(inner)
    if kind == "command":
        return _check_command(inner)
    if kind == "http":
        return _check_http(inner)
    if kind == "manual":
        return _result(False, "manual", "manual validation required (no automatic check)", 0.0)
    # none / 未知 → 不验证，ok=False + 明确原因（调用方据此给 unverifiable 而非 fresh）
    return _result(False, kind, f"validator kind {kind!r} performs no live check", 0.0)


def run_checks(checks: list[Any]) -> list[dict[str, Any]]:
    """批量执行（顺序），全部失败也不抛——供验证管线当降级通道用。"""
    return [validate(check) for check in checks]


if __name__ == "__main__":
    import json
    import sys

    demo = [
        {"kind": "file_exists", "path": sys.argv[1] if len(sys.argv) > 1 else __file__},
        {"kind": "command", "command": "python3 -c 'print(1)'"},
        {"kind": "http", "url": "http://127.0.0.1:8082/v1/models", "method": "HEAD"},
    ]
    for result in run_checks(demo):
        print(json.dumps(result, ensure_ascii=False))
