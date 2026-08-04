#!/usr/bin/env python3
"""memorykit 测试总入口 — 一键跑全部测试（unittest，无需 pytest）。

用法:
  python run_tests.py            # 跑全部
  python run_tests.py smoke      # 只跑冒烟测试
  python run_tests.py rings      # 只跑三环回归
  python run_tests.py <pattern>  # unittest 的 -k 子串过滤（如 'Age'）

等价 pytest 命令:
  python -m pytest tests/   (需已安装 pytest)
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1

    suite = unittest.defaultTestLoader.discover(
        os.path.join(HERE, "tests"), pattern="test_*.py")
    if args:
        # 按子串过滤：'smoke' / 'rings' / 'Age' 等
        suite = unittest.TestSuite(
            t for t in _flatten(suite) if any(a.lower() in (t.id() or "").lower() for a in args)
        )
        if suite.countTestCases() == 0:
            print(f"没有匹配 '{args}' 的测试。可用: smoke, rings, 或任意类名/方法名子串")
            sys.exit(1)

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def _flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _flatten(t)
        else:
            yield t


if __name__ == "__main__":
    main()
