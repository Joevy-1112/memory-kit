#!/usr/bin/env python3
"""memorykit validator 原型测试 — 三种活验证 + 归一化 + 降级路径。

运行：
  python run_tests.py validator
  或：python -m unittest tests.test_validator -v

覆盖：
- normalize_validator_type 别名归一化 / 未知值 fail-closed 到 manual
- file_exists：真实存在 / 缺失 / 裸字符串路径形态
- command：exit=0 / exit!=0 / 超时 / shlex 拆分（无 shell）/ 畸形 timeout 不抛（M1 回归）
- shell 旗标：字符串 "false"/"0"/"" 不启用 shell，白名单字符串启用（M2 回归）
- http：本机起 http.server 测 200 / 404（HTTPError 分支）/ 连接拒绝（URLError 分支）
- http scheme 白名单：file:// 等非 http(s) 入口即拒（H1 回归）+ URL userinfo 脱敏
- manual / none：不做自动检查
- scope-recall 兼容形态：{"validator_kind": ..., "validator_spec": {...}}
- run_checks 批量不抛异常
"""
import http.server
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit.validator import (  # noqa: E402
    _redact_url,  # 私有 helper，仅回归测试直接调用
    normalize_validator_type,
    run_checks,
    validate,
)


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    """最小 200 处理器：/ok → 200，其余 → 404。不写日志。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def log_message(self, format: str, *args):  # silence
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ValidatorKindNormalizationTest(unittest.TestCase):
    def test_alias_normalization(self):
        self.assertEqual(normalize_validator_type("path"), "file_exists")
        self.assertEqual(normalize_validator_type("file"), "file_exists")
        self.assertEqual(normalize_validator_type("shell"), "command")
        self.assertEqual(normalize_validator_type("cmd"), "command")
        self.assertEqual(normalize_validator_type("https"), "http")
        self.assertEqual(normalize_validator_type("url"), "http")
        self.assertEqual(normalize_validator_type("manual_live_check"), "manual")
        self.assertEqual(normalize_validator_type(""), "none")
        self.assertEqual(normalize_validator_type("permanent"), "none")

    def test_unknown_kind_fails_closed_to_manual(self):
        # 未知非空取值 → manual（宁可人工复核，不当永恒偏好）
        self.assertEqual(normalize_validator_type("quantum-check"), "manual")


class FileExistsValidatorTest(unittest.TestCase):
    def test_existing_file_ok(self):
        with tempfile.NamedTemporaryFile() as f:
            result = validate({"kind": "file_exists", "path": f.name})
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "file_exists")
        self.assertIn("exists", result["detail"])

    def test_missing_file_fails(self):
        result = validate({"kind": "file_exists", "path": "/nonexistent/definitely-missing-xyz"})
        self.assertFalse(result["ok"])
        self.assertIn("missing", result["detail"])

    def test_bare_string_treated_as_path(self):
        with tempfile.NamedTemporaryFile() as f:
            result = validate(f.name)
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "file_exists")

    def test_missing_path_spec_fails_cleanly(self):
        result = validate({"kind": "file_exists"})
        self.assertFalse(result["ok"])
        self.assertIn("missing 'path'", result["detail"])


class CommandValidatorTest(unittest.TestCase):
    def test_zero_exit_ok(self):
        result = validate({"kind": "command", "command": "python3 -c 'import sys; sys.exit(0)'"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "command")
        self.assertIn("exit=0", result["detail"])

    def test_nonzero_exit_fails(self):
        result = validate({"kind": "command", "command": "python3 -c 'import sys; sys.exit(3)'"})
        self.assertFalse(result["ok"])
        self.assertIn("exit=3", result["detail"])

    def test_missing_command_fails_cleanly(self):
        result = validate({"kind": "command"})
        self.assertFalse(result["ok"])
        self.assertIn("missing 'command'", result["detail"])

    def test_timeout_fails_cleanly(self):
        result = validate({"kind": "command", "command": "sleep 5", "timeout": 0.5})
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["detail"])

    def test_shell_true_path(self):
        result = validate({"kind": "command", "command": "echo hello | tr a-z A-Z", "shell": True})
        self.assertTrue(result["ok"])
        self.assertIn("HELLO", result["detail"])

    def test_malformed_timeout_does_not_raise(self):
        # M1 回归：畸形 timeout（字符串/列表/dict/None/nan/inf/0/负数）一律不抛，
        # 回退默认值或钳制到 MIN_TIMEOUT，命令照常执行
        for bad in ("abc", [1, 2], {"x": 1}, None, "nan", "inf", -3, 0):
            with self.subTest(timeout=bad):
                result = validate({"kind": "command", "command": "python3 -c 'pass'", "timeout": bad})
                self.assertTrue(result["ok"], f"timeout={bad!r} -> {result}")

    def test_shell_string_false_does_not_enable_shell(self):
        # M2 回归：字符串 "false"/"0"/""/任意非白名单值（含数字）绝不启用 shell——
        # 无 shell 时 shlex 拆分，$HOME 原样传给 echo，不做变量展开
        for bad in ("false", "0", "", "no", "off", "FALSE", 0, 1):
            with self.subTest(shell=bad):
                result = validate({"kind": "command", "command": "echo $HOME", "shell": bad})
                self.assertTrue(result["ok"], f"shell={bad!r} -> {result}")
                self.assertIn("$HOME", result["detail"],
                              f"shell={bad!r} 误展开 $HOME: {result}")

    def test_shell_whitelist_string_enables_shell(self):
        # 白名单字符串 "true"/"1"/"yes"（大小写不敏感）显式启用 shell
        for ok_val in ("true", "TRUE", "1", "yes"):
            with self.subTest(shell=ok_val):
                result = validate({"kind": "command", "command": "echo hello | tr a-z A-Z",
                                   "shell": ok_val})
                self.assertTrue(result["ok"], f"shell={ok_val!r} -> {result}")
                self.assertIn("HELLO", result["detail"])


class HttpValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), _QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_200_ok(self):
        result = validate({"kind": "http", "url": f"{self.base}/ok"})
        self.assertTrue(result["ok"])
        self.assertIn("200", result["detail"])

    def test_404_fails(self):
        # HTTPError 分支：服务在响应，但 404 不是"新鲜"
        result = validate({"kind": "http", "url": f"{self.base}/missing"})
        self.assertFalse(result["ok"])
        self.assertIn("404", result["detail"])

    def test_connection_refused_fails_cleanly(self):
        # URLError 分支：连不上（端口已释放）
        port = _free_port()
        result = validate({"kind": "http", "url": f"http://127.0.0.1:{port}/", "timeout": 2.0})
        self.assertFalse(result["ok"])
        self.assertIn("connection error", result["detail"])

    def test_missing_url_fails_cleanly(self):
        result = validate({"kind": "http"})
        self.assertFalse(result["ok"])
        self.assertIn("missing 'url'", result["detail"])

    def test_non_http_scheme_rejected(self):
        # H1 回归：非 http(s) scheme 入口即拒，不触达 IO 层（file:// 曾是真读
        # 本地文件的存在性探针）
        for url in ("file:///etc/hostname", "file:///nonexistent-xyz-123",
                    "ftp://example.com/x", "gopher://example.com/1"):
            with self.subTest(url=url):
                result = validate({"kind": "http", "url": url})
                self.assertFalse(result["ok"], f"{url} -> {result}")
                self.assertIn("scheme", result["detail"].lower(), f"{url} -> {result}")

    def test_malformed_http_timeout_does_not_raise(self):
        # M1 回归（http 侧）：畸形 timeout 不抛，回退默认值后检查照常执行
        for bad in ("abc", [1, 2], None, "nan", -1, 0):
            with self.subTest(timeout=bad):
                result = validate({"kind": "http", "url": f"{self.base}/ok", "timeout": bad})
                self.assertTrue(result["ok"], f"timeout={bad!r} -> {result}")

    def test_userinfo_never_leaks_into_detail(self):
        # H1 小项：URL 带 userinfo 时凭据不进 detail（urllib 把 userinfo 当
        # 主机名 → DNS 失败走 URLError 分支，此处断言整个结果对象无凭据残留）
        result = validate({"kind": "http", "url": "http://alice:s3cr3t@127.0.0.1:1/"})
        self.assertFalse(result["ok"])
        for secret in ("s3cr3t", "alice"):
            self.assertNotIn(secret, result["detail"], f"凭据泄漏: {result}")
            self.assertNotIn(secret, str(result), f"凭据泄漏: {result}")


class NoOpAndCompatTest(unittest.TestCase):
    def test_manual_performs_no_automatic_check(self):
        result = validate({"kind": "manual", "path": "/whatever"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "manual")
        self.assertIn("manual", result["detail"])

    def test_none_performs_no_live_check(self):
        result = validate({"kind": "none"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "none")

    def test_scope_recall_compat_shape(self):
        # scope-recall 契约形态：validator_kind + validator_spec
        with tempfile.NamedTemporaryFile() as f:
            result = validate({"validator_kind": "file_exists", "validator_spec": {"path": f.name}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "file_exists")

    def test_unsupported_spec_type_fails_cleanly(self):
        result = validate(12345)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "manual")

    def test_run_checks_batch_never_raises(self):
        results = run_checks([
            {"kind": "file_exists", "path": "/nonexistent/xyz"},
            {"kind": "command", "command": "python3 -c 'pass'"},
            {"kind": "command", "command": "python3 -c 'pass'", "timeout": "abc"},
            {"kind": "http", "url": "http://127.0.0.1:1/", "timeout": 1.0},
            {"kind": "http", "url": "http://127.0.0.1:1/", "timeout": "abc"},
            {"kind": "manual"},
            "also-a-raw-path",
        ])
        self.assertEqual(len(results), 7)
        for result in results:
            self.assertIn("ok", result)
            self.assertIn("checked_at", result)
            self.assertIn("elapsed_ms", result)

    def test_redact_url_strips_userinfo(self):
        # H1 小项（helper 级）：_redact_url 脱敏 userinfo、保留 host:port/路径/查询
        cases = {
            "http://user:pass@host:8080/a?b=c#d": "http://host:8080/a?b=c#d",
            "http://user@host/x": "http://host/x",
            "http://host/x": "http://host/x",  # 无 userinfo → 原样
            "http://user:pw@host:abc/x": "http://host/x",  # 非数字端口不炸
            "https://u:p@[::1]:8443/v": "https://[::1]:8443/v",  # IPv6
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(_redact_url(raw), expected)


if __name__ == "__main__":
    unittest.main()
