#!/usr/bin/env python3
"""render_memory_lines 渲染 helper 回归测试（P1 #3，2026-08-08）

设计（design-notes/scope-recall-borrowing.md §3）：
  渲染侧时间标注——检索结果进 prompt 时随行带【YYYY-MM-DD 记录】，
  让模型能判断信息新旧（防时态污染）；纯函数零依赖。

覆盖：
  #1 默认带时间标注（event_time 优先）
  #2 updated_at 兜底（无 event_time）
  #3 stamp=False 关闭标注
  #4 内容换行压成空格（单行输出）
  #5 max_chars 截断（保留靠前条目）
  #6 无时间字段 → 不带标注（不崩）
  #7 畸形时间值 → 不崩、无标注
  #8 多条目换行拼接

运行：
  python run_tests.py render
  或：python -m unittest tests.test_render -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit.render import render_memory_lines


class RenderTest(unittest.TestCase):
    def test_01_stamp_with_event_time(self):
        items = [{"key": "env/port", "content": "服务器端口 8082",
                  "event_time": "2026-08-01 10:00:00",
                  "updated_at": "2026-08-08 10:00:00"}]
        out = render_memory_lines(items)
        self.assertIn("- [env/port] 服务器端口 8082【2026-08-01 记录】", out)

    def test_02_updated_at_fallback(self):
        items = [{"key": "env/port", "content": "服务器端口 8082",
                  "updated_at": "2026-08-08 10:00:00"}]
        out = render_memory_lines(items)
        self.assertIn("【2026-08-08 记录】", out)

    def test_03_stamp_false(self):
        items = [{"key": "env/port", "content": "服务器端口 8082",
                  "event_time": "2026-08-01 10:00:00"}]
        out = render_memory_lines(items, stamp=False)
        self.assertNotIn("【", out)
        self.assertIn("- [env/port] 服务器端口 8082", out)

    def test_04_newlines_collapsed(self):
        items = [{"key": "k", "content": "第一行\n第二行\n第三行",
                  "updated_at": "2026-08-08"}]
        out = render_memory_lines(items)
        self.assertNotIn("\n第二行", out)
        self.assertIn("第一行 第二行 第三行", out)

    def test_05_max_chars_truncate(self):
        items = [
            {"key": "k1", "content": "A" * 100, "updated_at": "2026-08-08"},
            {"key": "k2", "content": "B" * 100, "updated_at": "2026-08-08"},
            {"key": "k3", "content": "C" * 100, "updated_at": "2026-08-08"},
        ]
        out = render_memory_lines(items, max_chars=150)
        self.assertIn("k1", out)
        self.assertNotIn("k3", out)
        self.assertLessEqual(len(out), 150 + 60)  # 截断在条目边界，略超上限可接受

    def test_06_no_time_no_stamp(self):
        items = [{"key": "k", "content": "没有时间字段的记录"}]
        out = render_memory_lines(items)
        self.assertNotIn("【", out)
        self.assertIn("- [k] 没有时间字段的记录", out)

    def test_07_bad_time_no_crash(self):
        items = [{"key": "k", "content": "畸形时间值",
                  "event_time": "not-a-date"}]
        out = render_memory_lines(items)
        self.assertNotIn("【", out)
        self.assertIn("- [k] 畸形时间值", out)

    def test_08_multi_items_joined(self):
        items = [
            {"key": "a", "content": "AAA", "updated_at": "2026-08-01"},
            {"key": "b", "content": "BBB", "updated_at": "2026-08-02"},
        ]
        out = render_memory_lines(items)
        lines = out.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("- [a] AAA", lines[0])
        self.assertIn("- [b] BBB", lines[1])


if __name__ == "__main__":
    unittest.main()
