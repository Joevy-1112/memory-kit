#!/usr/bin/env python3
"""memorykit.render — 检索结果渲染 helper（防时态污染）。

工程漏洞 2 的修复（design-notes/scope-recall-borrowing.md §3）：
event_time 此前只做了检索侧偏置（排序），渲染侧不带时间标注——旧记忆被
召回后模型看不出新旧，过期信息可能被当新鲜事实用。本模块补"零新数据、
只加表现层"的最小补丁：把 event_time/updated_at 的日期随行渲染进 prompt。

用法：
  from memorykit.render import render_memory_lines
  hits = db.search_multi("显卡", limit=5)
  print(render_memory_lines(hits))          # 默认带时间标注
  print(render_memory_lines(hits, stamp=False))  # 关闭标注

设计：
- 纯函数，零依赖库状态；items 是 search_multi 返回的 dict 列表
- 标注取 event_time 或 updated_at 的日期部分（YYYY-MM-DD）
- max_chars 总输出上限（按条目顺序累积，超出截断）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

_DATE_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _date_part(value: Any) -> str:
    """把时间字段裁成 YYYY-MM-DD；解析失败返回空串。"""
    if not value:
        return ""
    s = str(value).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # 已经是 YYYY-MM-DD 或裸日期前缀
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def render_memory_lines(items: Iterable[Mapping], *,
                        max_chars: int = 600, stamp: bool = True) -> str:
    """检索结果 → prompt 行。

    每行形态：`- [key] content【YYYY-MM-DD 记录】`
    - stamp=True（默认）带时间标注，取 event_time 优先、updated_at 兜底
    - stamp=False 关闭标注（调用方按场景选：当前会话内引用不需要时间边界）
    - max_chars 控制总输出，超出后截断（保留靠前的条目）
    - 内容里的换行压成空格（prompt 行必须单行）

    防时态污染语义：**标注让模型能判断信息新旧**——看出时间矛盾才会触发
    correction 闭环质疑，而不是让过期信息悄悄通过。
    """
    out: list[str] = []
    used = 0
    for it in items:
        key = str(it.get("key", "")).strip()
        content = str(it.get("content", "") or "").replace("\n", " ").strip()
        line = f"- [{key}] {content}"
        if stamp:
            ts = _date_part(it.get("event_time") or it.get("updated_at"))
            if ts:
                line += f"【{ts} 记录】"
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)
