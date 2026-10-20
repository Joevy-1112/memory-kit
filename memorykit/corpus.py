#!/usr/bin/env python3
"""memorykit.corpus — 第二种语料（skill / 文档）的切块、索引与召回。

设计见 `design-notes/skill-corpus-2026-10.md`。本模块只做三件事：

1. **切块**：frontmatter 单独成块；正文按 `^##` / `^###` 切；围栏代码块里的
   `##` 不切；单块 > 1200 字符再按空行二切（无空行时硬切）。
   索引范围 = `SKILL.md` + `references/**/*.md`（`--no-include-references`
   可退回只索引正文）；references 块统一 `chunk_type='reference'`。
2. **索引**：`corpus_chunks` + `corpus_entity_links` + `corpus_manifest`，
   向量用 bge-m3（1024 维，`np.float32` blob），实体复用
   `MemoryDB._extract_entities` 并做小写归一 / 黑名单 / 全库 df>=2 过滤。
   增量按文件 sha256 跳过未变源（SKILL.md 不重嵌）；`index --dry-run`
   只扫描/切块报预计新增块数，不嵌入不写库。
3. **召回**：`score = max(cos) + w_e * 命中实体数(封顶3) + w_l * lexical_norm`。
   两个权重经 P4 E1 的 4 组网格检验；默认 0.10/0.25 保留（29 条样本不足以下调）。

CLI：
    python -m memorykit.corpus --db var/memory-sandbox.db --include-references index --dry-run
    python -m memorykit.corpus --db var/memory-sandbox.db index
    python -m memorykit.corpus --db var/memory-sandbox.db stats
    python -m memorykit.corpus --db var/memory-sandbox.db query "飞书回复被截断"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from .db import MemoryDB
from .embedder import HTTPEmbedder

# ── 版本与参数 ────────────────────────────────────────────────────────────────
INDEX_VERSION = "p2-corpus-v1"
MAX_CHUNK_CHARS = 1200
DEFAULT_BATCH_SIZE = 8
FRONTMATTER_HEADING = "frontmatter"
# 来自 `references/**/*.md` 的块统一用这个 chunk_type，便于和 SKILL.md 的
# frontmatter/heading/preamble 区分（分来源归因、`--include-references` 统计）。
REFERENCE_CHUNK_TYPE = "reference"

# 召回权重（P4 E1 网格：0/0、0/0.25、0.05/0.10、0.10/0.25）。
# E1 上 (0,0.25) 略优但差异约 1-2 条 query；保留默认，等更大评测集再动。
W_ENTITY = 0.10
W_LEXICAL = 0.25
ENTITY_CAP = 3

DEFAULT_SKILLS_ROOT = os.environ.get(
    "MEMORY_SKILLS_ROOT", os.path.expanduser("~/.hermes/skills"))
# 不提供 cwd/memory.db 兜底：语料索引必须显式落在 MEMORY_DB_PATH（沙箱库），
# 避免误建到仓库目录或生产库。
DEFAULT_DB_PATH = os.environ.get("MEMORY_DB_PATH")


CORPUS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS corpus_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type   TEXT NOT NULL DEFAULT 'skill',   -- 'skill' | 'doc' ...
    source_id     TEXT NOT NULL,                   -- 相对路径（唯一）
    skill_name    TEXT NOT NULL,                   -- frontmatter name / 目录名
    chunk_no      INTEGER NOT NULL,
    chunk_type    TEXT NOT NULL,                   -- frontmatter | heading | preamble
    heading       TEXT DEFAULT '',
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    char_len      INTEGER NOT NULL,
    entities_json TEXT DEFAULT '[]',               -- 清洗后的实体（全量，供 df 重算）
    tokens_json   TEXT DEFAULT '[]',               -- lexical 用的去重 token
    vector_bin    BLOB,
    updated_at    TEXT NOT NULL,
    UNIQUE(source_type, source_id, chunk_no)
);
CREATE INDEX IF NOT EXISTS idx_corpus_chunks_source
    ON corpus_chunks(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_corpus_chunks_skill
    ON corpus_chunks(skill_name);

CREATE TABLE IF NOT EXISTS corpus_entity_links (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    entity   TEXT NOT NULL,                        -- 已小写归一
    chunk_id INTEGER NOT NULL REFERENCES corpus_chunks(id) ON DELETE CASCADE,
    weight   REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_corpus_entity
    ON corpus_entity_links(entity);
CREATE INDEX IF NOT EXISTS idx_corpus_entity_chunk
    ON corpus_entity_links(chunk_id);

CREATE TABLE IF NOT EXISTS corpus_manifest (
    path            TEXT PRIMARY KEY,               -- 相对 skills root
    mtime           REAL NOT NULL,                  -- epoch seconds
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    chunk_count     INTEGER NOT NULL,
    index_version   TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


# ── 基础工具 ──────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _normalize_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


# ── frontmatter ───────────────────────────────────────────────────────────────
def split_frontmatter(text: str):
    """返回 (meta, body, raw_frontmatter)。

    只认文件开头的 `---` … `---` / `...`。没有 frontmatter 时 meta={}，
    body 为原文，raw_frontmatter=''。raw 保留定界符之间的原始文本。
    """
    text = _normalize_newlines(text)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text, ""
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            return _parse_frontmatter(raw), body, raw
    # 没有闭合定界符：按无 frontmatter 处理，避免把正文整段当元数据
    return {}, text, ""


def _parse_frontmatter(raw: str) -> dict:
    """极简 frontmatter 解析：顶层 `key: value`，支持 | / > 块标量。

    不引入 PyYAML；只保留名字/描述等标量，嵌套 metadata 忽略。
    """
    meta: dict = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2)
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            style = value[0]
            buf = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "":
                    buf.append("")
                    i += 1
                    continue
                if not nxt.startswith((" ", "\t")):
                    break
                buf.append(nxt.strip())
                i += 1
            if style == ">":
                meta[key] = " ".join(x for x in buf if x).strip()
            else:
                meta[key] = "\n".join(buf).strip()
            continue
        meta[key] = _strip_quotes(value)
        i += 1
    return meta


# ── 切块 ──────────────────────────────────────────────────────────────────────
@dataclass
class Chunk:
    chunk_no: int
    chunk_type: str          # frontmatter | heading | preamble
    heading: str
    content: str

    @property
    def char_len(self) -> int:
        return len(self.content)

    def as_dict(self) -> dict:
        return {
            "chunk_no": self.chunk_no,
            "chunk_type": self.chunk_type,
            "heading": self.heading,
            "content": self.content,
            "char_len": self.char_len,
        }


_HEADING_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")


def _clean_block(text: str) -> str:
    return text.strip("\n").strip()


def _split_headings(body: str):
    """按围栏外的 `##` / `###` 切段，返回 [(heading, content), ...]。

    heading='' 表示第一个标题之前的 preamble。围栏用 CommonMark 规则：
    开围栏 N 个反引号/波浪号，只有同字符且 >=N 个、后面仅空白的行才闭合。
    这样 ```` 里出现 ``` 不会误闭合，围栏里的 `##` 也不会被当标题。
    """
    sections = []
    heading = ""
    buf = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush():
        nonlocal buf
        sections.append((heading, "".join(buf)))
        buf = []

    for line in body.splitlines(keepends=True):
        if not in_fence:
            hm = _HEADING_RE.match(line)
            if hm:
                flush()
                heading = hm.group(2).strip()
                continue
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            ch = marker[0]
            if not in_fence:
                in_fence = True
                fence_char = ch
                fence_len = len(marker)
            elif ch == fence_char and len(marker) >= fence_len \
                    and fm.group(2).strip() == "":
                in_fence = False
        buf.append(line)
    flush()
    return sections


def _best_cut(text: str, limit: int) -> int:
    """在 <=limit 处找二切点：优先围栏外空行，其次围栏外换行，最后硬切。"""
    best_blank = -1
    best_newline = -1
    pos = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        if pos > limit:
            break
        if not in_fence and not line.strip():
            if pos > 0:
                best_blank = pos
        if not in_fence and line.endswith("\n"):
            end = pos + len(line)
            if end <= limit:
                best_newline = end
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            ch = marker[0]
            if not in_fence:
                in_fence = True
                fence_char = ch
                fence_len = len(marker)
            elif ch == fence_char and len(marker) >= fence_len \
                    and fm.group(2).strip() == "":
                in_fence = False
        pos += len(line)
    floor = max(1, int(limit * 0.35))
    if best_blank >= floor:
        return best_blank
    if best_newline >= floor:
        return best_newline
    return limit


def _split_long(text: str, limit: int) -> list:
    """单块 >limit 时二切；保证每块 <=limit（除非单行本身超长）。"""
    if len(text) <= limit:
        return [text]
    parts = []
    rest = text
    while len(rest) > limit:
        cut = _best_cut(rest, limit)
        if cut <= 0 or cut >= len(rest):
            cut = limit
        part = rest[:cut].strip("\n")
        rest = rest[cut:].lstrip("\n")
        if part:
            parts.append(part)
        else:
            # 防御：cut 落在纯空白处时强制推进
            parts.append(rest[:limit])
            rest = rest[limit:]
    if rest.strip():
        parts.append(rest.strip("\n"))
    return parts


def chunk_skill_text(text: str, skill_name: str = "",
                     max_chars: int = MAX_CHUNK_CHARS) -> list:
    """把一份 SKILL.md 切成 Chunk 列表。

    顺序：frontmatter（若有）→ preamble（第一个 ##/### 之前）→ 各 heading。
    frontmatter 块内容保留定界符之间的原文；embedding 时再拼 skill 名。
    """
    text = _normalize_newlines(text)
    meta, body, raw_fm = split_frontmatter(text)
    if not skill_name:
        skill_name = str(meta.get("name") or "").strip()

    chunks = []
    if raw_fm.strip():
        # 只保留 name + description：Hermes 静态索引里真正被模型看到的就是
        # 这两个字段；license/author/metadata 会稀释描述信号（有 4.7K 字符的
        # frontmatter 特例）。缺失时才退回原始块。
        fm_text = ""
        if meta.get("name"):
            fm_text += f"name: {meta['name']}\n"
        if meta.get("description"):
            fm_text += f"description: {meta['description']}"
        if not fm_text.strip():
            fm_text = raw_fm.strip()
        chunks.append(Chunk(len(chunks), "frontmatter", FRONTMATTER_HEADING,
                            fm_text.strip()))

    for heading, content in _split_headings(body):
        content = _clean_block(content)
        if not heading and not content:
            continue
        # 标题本身也算语义：空正文的标题块保留（embedding 会拼 heading）
        sub_parts = _split_long(content, max_chars) if content else [""]
        if len(sub_parts) > 1:
            for i, part in enumerate(sub_parts, 1):
                chunks.append(Chunk(
                    len(chunks),
                    "heading" if heading else "preamble",
                    f"{heading} (part {i}/{len(sub_parts)})" if heading else "",
                    part,
                ))
        else:
            chunks.append(Chunk(
                len(chunks),
                "heading" if heading else "preamble",
                heading,
                content,
            ))
    return chunks


def chunk_reference_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list:
    """把一份 references 文档切成 Chunk 列表。

    复用 SKILL.md 的 fence-aware 切块规则（frontmatter/preamble/heading 的
    内部结构照旧），但**所有块的 chunk_type 统一定为 `reference`**：来源
    归属（正文 vs references）必须一眼可判，结构细节保留在 heading 里。
    """
    chunks = chunk_skill_text(text, "reference", max_chars)
    for c in chunks:
        c.chunk_type = REFERENCE_CHUNK_TYPE
    return chunks


def is_reference_path(rel: str) -> bool:
    """相对 skills root 的路径是否属于某 skill 的 references/ 目录。"""
    parts = Path(rel).parts
    return len(parts) >= 3 and "references" in parts[:-1]


def skill_name_for(path: Path, text: str) -> str:
    meta, _, _ = split_frontmatter(text)
    name = str(meta.get("name") or "").strip()
    return name or path.parent.name


# ── 实体 / token ──────────────────────────────────────────────────────────────
# MemoryDB._extract_entities 不读实例状态；这里直接复用，避免复制 7 步规则。
_RAW_EXTRACT = MemoryDB._extract_entities

ENTITY_BLACKLIST = {
    "assistant", "home", "user", "users", "skill", "skills", "name",
    "description", "version", "author", "license", "metadata", "hermes",
    "true", "false", "null", "none", "http", "https", "www", "com",
    "the", "and", "for", "with", "this", "that", "from", "into",
    # 文档结构 / 高频虚词：df>=2 也救不了，留着只会抬高任意 query 的命中数
    "part", "parts", "frontmatter", "references", "reference",
    "use", "uses", "using", "used", "when", "only", "not", "step", "steps",
    "before", "after", "via", "you", "all", "what", "full", "file", "files",
    "text", "path", "content", "output", "command", "url", "key", "value",
    "data", "code", "type", "types", "typescript", "ts", "用户", "文件",
    "系统", "内容", "方法", "步骤", "情况",
    "问题", "功能", "支持", "需要", "可以", "进行", "使用", "参数", "命令",
    "目录", "路径", "部分", "时候", "一个", "我们", "他们",
}


def sanitize_entity(item) -> str:
    """单实体归一化 + 黑名单；空串表示丢弃。"""
    e = str(item).strip().lower()
    e = e.strip("`'\"“”‘’.,;:!?()[]{}<>《》")
    if len(e) < 2 or len(e) > 40:
        return ""
    if e in ENTITY_BLACKLIST:
        return ""
    if not re.search(r"[a-z\u4e00-\u9fff]", e):
        return ""
    return e


_NOUN_POS = {"n", "nr", "ns", "nt", "nz", "vn", "eng"}


def _noun_supplement(text: str) -> list:
    """补 `_extract_entities` 漏掉的高频普通名词与相邻单字专名。

    `_extract_entities` 会把 jieba 词频 >=150 的日常名词（耳机/电池）滤掉，
    也会把未登录词"飞书"切成单字。语料实体只用来数命中数，不写回记忆库，
    所以这里按词性补名词：相邻两个单字名词合并（飞+书→飞书）。df>=2 会
    在写 links 时再过滤一次长尾噪音。
    """
    try:
        import jieba.posseg as pseg
    except Exception:
        return []
    out = []
    blocks = re.findall(r"[\u4e00-\u9fffa-zA-Z0-9+#&._%\-]+", text)
    for block in blocks:
        if not re.search(r"[\u4e00-\u9fff]", block):
            continue
        prev_single = ""
        for word, flag in pseg.cut(block):
            word = word.strip()
            if not word:
                continue
            if flag in _NOUN_POS and len(word) >= 2:
                out.append(word)
                prev_single = ""
            elif flag in _NOUN_POS and len(word) == 1:
                if prev_single:
                    out.append(prev_single + word)
                    prev_single = ""
                else:
                    prev_single = word
            else:
                prev_single = ""
    return out


def extract_entities(text: str) -> list:
    """规则实体 + 名词补充 + ASCII 技术词；小写归一 + 黑名单。

    df>=2 在写 links 时统一过滤。补充项只服务于语料召回打分，不写回
    memories/entity_links。
    """
    try:
        raw = _RAW_EXTRACT(None, text)  # type: ignore[arg-type]
    except Exception:
        raw = []
    candidates = list(raw) + _noun_supplement(text)
    # type-c / vllm / v100 这类 token 不在 _extract_entities 的规则里
    for tok in tokenize(text):
        if re.search(r"[a-z]", tok) and (re.search(r"[0-9.+#/-]", tok)
                                         or len(tok) >= 3):
            candidates.append(tok)
    out = []
    for item in candidates:
        e = sanitize_entity(item)
        if e and e not in out:
            out.append(e)
    return out[:40]


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.+#/-]*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "怎么",
    "什么", "如何", "可以", "需要", "一个", "我们", "你们", "他们", "问题",
    "时候", "现在", "已经", "还是", "就是", "因为", "所以", "如果", "没有",
}


def tokenize(text: str) -> list:
    """query / chunk 共用的词法 token（去重、保序）。

    中文优先 jieba 搜索模式（词粒度更细，短 query 更容易命中）；jieba 不可用
    时退化为 2-gram。英文/型号按 `[a-z0-9][a-z0-9_.+#/-]*` 切。
    """
    text = (text or "").lower()
    if not text.strip():
        return []
    tokens = []
    try:
        import jieba
        use_jieba = True
    except Exception:
        use_jieba = False
    for piece in re.split(r"([\u4e00-\u9fff]+)", text):
        if not piece:
            continue
        if _CJK_RE.fullmatch(piece):
            if use_jieba:
                words = jieba.lcut_for_search(piece)
            else:
                words = [piece[i:i + 2] for i in range(max(1, len(piece) - 1))]
        else:
            words = _TOKEN_RE.findall(piece)
        # 未登录中文词常被 jieba 切成相邻单字（飞/书）——合并后再收
        merged = []
        i = 0
        while i < len(words):
            w = words[i]
            if (len(w) == 1 and _CJK_RE.fullmatch(w)
                    and i + 1 < len(words)
                    and len(words[i + 1]) == 1
                    and _CJK_RE.fullmatch(words[i + 1])):
                merged.append(w + words[i + 1])
                i += 2
                continue
            merged.append(w)
            i += 1
        for w in merged:
            w = w.strip("._-+/#")
            if len(w) < 2 or w in _STOPWORDS:
                continue
            # 纯中文单字丢弃；英文单字母也丢
            if len(w) == 1:
                continue
            variants = [w]
            # 连字符/下划线/斜杠复合词再拆子词：1Cat-vLLM → 1cat, vllm;
            # type-c → type。整词保留，避免丢掉型号原貌。
            if re.search(r"[-_/]", w):
                for part in re.split(r"[-_/]", w):
                    if len(part) >= 2 and re.search(r"[a-z]", part):
                        variants.append(part)
            for v in variants:
                if v not in tokens:
                    tokens.append(v)
    return tokens


# ── 索引 ──────────────────────────────────────────────────────────────────────
def detect_embedding_model(embedder=None) -> str:
    """尽力拿服务端模型名；失败时用环境变量/unknown。"""
    env = os.environ.get("MEMORY_EMBED_MODEL")
    if env:
        return env
    try:
        import urllib.request
        url = getattr(embedder, "url", None) or os.environ.get(
            "MEMORY_EMBED_URL", "http://127.0.0.1:8082")
        with urllib.request.urlopen(f"{url.rstrip('/')}/v1/models", timeout=2) as r:
            data = json.loads(r.read())
        item = (data.get("data") or [{}])[0]
        name = item.get("id") or ""
        return os.path.basename(name) if name else "unknown"
    except Exception:
        return "unknown"


class CorpusIndex:
    """skill/doc 语料的索引器与召回器。

    db_path 默认 `MEMORY_DB_PATH`。与 memories 共用同一个 SQLite 文件，但只
    读写 `corpus_*` 三张表，不碰 memories / entity_links。
    """

    def __init__(self, db_path: str = None, root: str = None,
                 embedder=None, source_type: str = "skill",
                 max_chars: int = MAX_CHUNK_CHARS,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 embedding_model: str = None, quiet: bool = False,
                 include_references: bool = True):
        chosen = db_path or os.environ.get("MEMORY_DB_PATH") or DEFAULT_DB_PATH
        if not chosen:
            raise ValueError(
                "CorpusIndex 需要显式 db_path 或 MEMORY_DB_PATH（不默认 cwd/memory.db）")
        self.db_path = os.path.expanduser(chosen)
        self.root = Path(os.path.expanduser(
            root or os.environ.get("MEMORY_SKILLS_ROOT", DEFAULT_SKILLS_ROOT)))
        self.source_type = source_type
        self.max_chars = max_chars
        self.batch_size = max(1, int(batch_size))
        self.include_references = bool(include_references)
        self.quiet = quiet
        self.embedder = embedder or HTTPEmbedder(timeout=120)
        self.embedding_model = embedding_model or detect_embedding_model(
            self.embedder)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(CORPUS_SCHEMA_SQL)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── 扫描 ────────────────────────────────────────────────────────────────
    def discover(self) -> list:
        """返回 [(relative_path, Path), ...]，跳过任意层级的 .archive。

        默认包含每个 skill 的 `SKILL.md` 与 `references/**/*.md`
        （`include_references=False` 时只收 SKILL.md）。references 文件先按
        所属 skill 目录聚合，保证不会把孤立目录当 skill。
        """
        root = self.root
        if not root.exists():
            raise FileNotFoundError(f"skills root 不存在: {root}")
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != ".archive")
            if "SKILL.md" not in filenames:
                continue
            p = Path(dirpath) / "SKILL.md"
            found.append((str(p.relative_to(root)), p))
            if not self.include_references:
                continue
            ref_dir = Path(dirpath) / "references"
            if not ref_dir.is_dir():
                continue
            for ref in sorted(ref_dir.rglob("*.md")):
                if ref.is_file():
                    found.append((str(ref.relative_to(root)), ref))
        found.sort(key=lambda x: x[0])
        return found

    def _manifest(self) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM corpus_manifest WHERE path LIKE ?",
            (self._path_prefix(),)).fetchall()
        return {r["path"]: dict(r) for r in rows}

    def _path_prefix(self) -> str:
        # source_type='skill' 目前只对应一个 root；manifest 的 path 已相对 root。
        return "%"

    # ── 嵌入 ────────────────────────────────────────────────────────────────
    def _embedding_text(self, skill: str, chunk: Chunk) -> str:
        if chunk.chunk_type == "frontmatter":
            return f"{skill}\n{chunk.content}"
        if chunk.heading:
            return f"{skill} / {chunk.heading}\n{chunk.content}"
        return f"{skill}\n{chunk.content}"

    def _embed_many(self, texts: list, progress=None) -> list:
        """批量嵌入；整批失败退避重试，单条失败逐条重试，仍失败则抛错。

        P4：`embed_batch` 已在内部按 ≤16 切批并做批间失败隔离，这里再包一层
        退避重试（最多 3 次，2s/4s）。**整批全 None 视为服务级失败**，重试后
        直接抛错而不是逐条打 8 个单请求——共享 8082 卡住时逐条重试只会雪上加
        霜。只有"批里个别位置为 None"才逐条补，定位坏文本。
        """
        out = [None] * len(texts)
        bs = self.batch_size
        for start in range(0, len(texts), bs):
            batch = texts[start:start + bs]
            got = None
            for attempt in range(3):
                got = None
                if hasattr(self.embedder, "embed_batch"):
                    try:
                        got = self.embedder.embed_batch(batch)
                    except Exception:
                        got = None
                if (isinstance(got, list) and len(got) == len(batch)
                        and any(v is not None for v in got)):
                    break
                if attempt < 2:
                    wait = 2 * (attempt + 1)
                    print(f"[index] 批次 {start // bs} 失败，{wait}s 后重试 "
                          f"({attempt + 1}/3)", flush=True)
                    time.sleep(wait)
            if not isinstance(got, list) or len(got) != len(batch) \
                    or all(v is None for v in got):
                raise RuntimeError(
                    f"嵌入整批失败（batch {start // bs}，{len(batch)} 条），"
                    f"已重试 3 次；首个文本 {batch[0][:80]!r}")
            for j, vec in enumerate(got):
                if vec is None:
                    try:
                        vec = self.embedder.embed(batch[j])
                    except Exception:
                        vec = None
                if vec is None:
                    raise RuntimeError(
                        f"嵌入失败（batch {start // bs}，item {j}）："
                        f"{batch[j][:80]!r}")
                out[start + j] = vec
            if progress:
                progress(start + len(batch), len(texts))
        return out

    # ── 主流程 ──────────────────────────────────────────────────────────────
    def _skill_name_for_reference(self, rel: str) -> str:
        """references 文件 → 所属 skill 的 frontmatter.name（无则目录名）。"""
        parts = Path(rel).parts
        try:
            idx = parts.index("references")
        except ValueError:
            return Path(rel).parent.name
        skill_dir = self.root.joinpath(*parts[:idx])
        cache = getattr(self, "_skill_name_cache", None)
        if cache is None:
            cache = self._skill_name_cache = {}
        key = str(skill_dir)
        if key in cache:
            return cache[key]
        skill_md = skill_dir / "SKILL.md"
        name = skill_dir.name
        try:
            if skill_md.is_file():
                name = skill_name_for(
                    skill_md,
                    skill_md.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
        cache[key] = name
        return name

    def _chunks_for(self, rel: str, path: Path):
        """按来源类型切块 → (skill_name, chunks, kind)。"""
        text = _normalize_newlines(
            path.read_text(encoding="utf-8", errors="replace"))
        if is_reference_path(rel):
            return (self._skill_name_for_reference(rel),
                    chunk_reference_text(text, self.max_chars), "reference")
        skill = skill_name_for(path, text)
        return skill, chunk_skill_text(text, skill, self.max_chars), "skill"

    def index(self, force: bool = False, limit: int = None,
              prune_entities: bool = True, dry_run: bool = False) -> dict:
        t0 = time.time()
        all_files = self.discover()
        files = all_files[:limit] if limit else all_files
        old = self._manifest()
        current_paths = {rel for rel, _ in files}

        # 已删除/移出范围的源。`--limit` 只是抽样，不能把未扫描到的旧源
        # 当成已删除（否则一次 limit 索引会清掉其余文件）。dry-run 不落任何写。
        removed = ([] if limit else [p for p in old if p not in current_paths])
        if removed and not dry_run:
            for rel in removed:
                self._delete_source(rel)
                self.conn.execute(
                    "DELETE FROM corpus_manifest WHERE path=?", (rel,))
            self.conn.commit()

        changed = []
        skipped = 0
        for rel, path in files:
            st = path.stat()
            digest = _sha256_file(path)
            prev = old.get(rel)
            if (not force and prev and prev["sha256"] == digest
                    and prev["index_version"] == INDEX_VERSION
                    and prev["embedding_model"] == self.embedding_model):
                skipped += 1
                continue
            changed.append((rel, path, st, digest))

        # 先切块，得到准确的 total（空文件也进 manifest，chunk_count=0）
        jobs = []
        total_chunks = 0
        seen = {}
        for rel, path, st, digest in changed:
            skill, chunks, kind = self._chunks_for(rel, path)
            jobs.append({
                "rel": rel, "path": path, "stat": st, "sha256": digest,
                "skill": skill, "chunks": chunks, "kind": kind,
            })
            total_chunks += len(chunks)
            slot = seen.setdefault(kind, {"files": 0, "chunks": 0})
            slot["files"] += 1
            slot["chunks"] += len(chunks)

        scope = {"skill": {"files": 0, "chunks": 0},
                 "reference": {"files": 0, "chunks": 0}}
        for rel, _ in files:
            scope["reference" if is_reference_path(rel) else "skill"]["files"] += 1

        if dry_run:
            existing_chunks = self.conn.execute(
                "SELECT COUNT(*) FROM corpus_chunks WHERE source_type=?",
                (self.source_type,)).fetchone()[0]
            removed_chunks = sum(int(old[p]["chunk_count"]) for p in removed)
            # 变更源是"整文件替换"：先删旧块再插新块，所以建后总数要减去
            # 这些源在旧库里的块数（files_removed 只覆盖已删除的路径）。
            replaced_chunks = sum(int(old[rel]["chunk_count"])
                                  for rel, _, _, _ in changed if rel in old)
            result = {
                "dry_run": True,
                "files_total": len(all_files),
                "files_skill": scope["skill"]["files"],
                "files_reference": scope["reference"]["files"],
                "files_changed": len(jobs),
                "files_skipped": skipped,
                "files_removed": len(removed),
                "chunks_new": total_chunks,
                "chunks_existing": existing_chunks,
                "chunks_removed": removed_chunks,
                "chunks_replaced": replaced_chunks,
                "chunks_total_after": (existing_chunks - replaced_chunks
                                       + total_chunks - removed_chunks),
                "changed_by_kind": seen,
                "include_references": self.include_references,
                "embedding_model": self.embedding_model,
                "index_version": INDEX_VERSION,
                "elapsed_s": round(time.time() - t0, 2),
            }
            if not self.quiet:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

        all_texts = []
        for job in jobs:
            for c in job["chunks"]:
                all_texts.append(self._embedding_text(job["skill"], c))

        done = 0
        last_report = 0
        start_ts = time.time()

        def progress(n, total):
            nonlocal done, last_report
            done = n
            if n - last_report >= 500 or n == total:
                elapsed = max(1e-6, time.time() - start_ts)
                rate = n / elapsed
                print(f"[index] {n}/{total} chunks  {rate:.1f} chunks/s  "
                      f"elapsed={elapsed:.1f}s", flush=True)
                last_report = n

        if all_texts:
            vectors = self._embed_many(all_texts, progress=progress)
        else:
            vectors = []

        # 嵌入全部成功后才写库；按文件事务替换
        vi = 0
        inserted_chunks = 0
        try:
            with self.conn:
                for job in jobs:
                    self._delete_source(job["rel"])
                    for c in job["chunks"]:
                        vec = np.asarray(vectors[vi], dtype=np.float32)
                        vi += 1
                        entities = extract_entities(
                            f"{job['skill']} {c.heading}\n{c.content}")
                        tokens = tokenize(f"{job['skill']} {c.heading} {c.content}")
                        content_hash = _sha256_text(
                            f"{job['skill']} / {c.heading}\n{c.content}")
                        self.conn.execute(
                            "INSERT INTO corpus_chunks("
                            "source_type, source_id, skill_name, chunk_no, chunk_type,"
                            " heading, content, content_hash, char_len, entities_json,"
                            " tokens_json, vector_bin, updated_at"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (self.source_type, job["rel"], job["skill"],
                             c.chunk_no, c.chunk_type, c.heading, c.content,
                             content_hash, c.char_len,
                             json.dumps(entities, ensure_ascii=False),
                             json.dumps(tokens, ensure_ascii=False),
                             vec.tobytes(), _now_iso()))
                        inserted_chunks += 1
                    self.conn.execute(
                        "INSERT INTO corpus_manifest("
                        "path, mtime, size, sha256, chunk_count, index_version,"
                        " embedding_model, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET "
                        "mtime=excluded.mtime, size=excluded.size, "
                        "sha256=excluded.sha256, chunk_count=excluded.chunk_count, "
                        "index_version=excluded.index_version, "
                        "embedding_model=excluded.embedding_model, "
                        "updated_at=excluded.updated_at",
                        (job["rel"], job["stat"].st_mtime, job["stat"].st_size,
                         job["sha256"], len(job["chunks"]), INDEX_VERSION,
                         self.embedding_model, _now_iso()))
        except Exception:
            self.conn.rollback()
            raise

        links = self.rebuild_entity_links() if prune_entities else 0
        total = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks WHERE source_type=?",
            (self.source_type,)).fetchone()[0]
        result = {
            "files_total": len(all_files),
            "files_skill": scope["skill"]["files"],
            "files_reference": scope["reference"]["files"],
            "files_indexed": len(jobs),
            "files_skipped": skipped,
            "files_removed": len(removed),
            "chunks_indexed": inserted_chunks,
            "chunks_new_by_kind": seen,
            "chunks_total": total,
            "entity_links": links,
            "include_references": self.include_references,
            "embedding_model": self.embedding_model,
            "index_version": INDEX_VERSION,
            "elapsed_s": round(time.time() - t0, 2),
        }
        if not self.quiet:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    def _delete_source(self, rel: str):
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM corpus_chunks WHERE source_type=? AND source_id=?",
            (self.source_type, rel)).fetchall()]
        if ids:
            q = ",".join("?" for _ in ids)
            self.conn.execute(
                f"DELETE FROM corpus_entity_links WHERE chunk_id IN ({q})", ids)
            self.conn.execute(
                f"DELETE FROM corpus_chunks WHERE id IN ({q})", ids)

    def rebuild_entity_links(self) -> int:
        """从 chunks.entities_json 重建 links，只保留全库 df>=2 的实体。

        保留每块的全量实体 JSON，因此增量索引后可以无损重算 df；不会出现
        "先被过滤掉、后来变常用却加不回来"的问题。
        """
        rows = self.conn.execute(
            "SELECT id, entities_json FROM corpus_chunks WHERE source_type=?",
            (self.source_type,)).fetchall()
        parsed = []
        df: dict = {}
        for r in rows:
            try:
                ents = json.loads(r["entities_json"] or "[]")
            except Exception:
                ents = []
            ents = [e for e in ents if isinstance(e, str)]
            parsed.append((r["id"], ents))
            for e in set(ents):
                df[e] = df.get(e, 0) + 1
        keep = {e for e, n in df.items() if n >= 2}
        with self.conn:
            self.conn.execute("DELETE FROM corpus_entity_links")
            n = 0
            for cid, ents in parsed:
                for e in ents:
                    if e in keep:
                        self.conn.execute(
                            "INSERT INTO corpus_entity_links(entity, chunk_id, weight)"
                            " VALUES (?,?,1.0)", (e, cid))
                        n += 1
        return n

    # ── 统计 / 召回 ─────────────────────────────────────────────────────────
    def stats(self) -> dict:
        out = {}
        out["db_path"] = self.db_path
        out["root"] = str(self.root)
        out["files"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_manifest").fetchone()[0]
        out["chunks"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks WHERE source_type=?",
            (self.source_type,)).fetchone()[0]
        out["reference_chunks"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks WHERE source_type=? "
            "AND chunk_type=?", (self.source_type, REFERENCE_CHUNK_TYPE)).fetchone()[0]
        out["reference_files"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_manifest WHERE path LIKE '%references/%'"
        ).fetchone()[0]
        out["skill_files"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_manifest WHERE path LIKE '%SKILL.md'"
        ).fetchone()[0]
        out["frontmatter_chunks"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks WHERE source_type=? "
            "AND chunk_type='frontmatter'", (self.source_type,)).fetchone()[0]
        out["entities"] = self.conn.execute(
            "SELECT COUNT(DISTINCT entity) FROM corpus_entity_links").fetchone()[0]
        out["entity_links"] = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_entity_links").fetchone()[0]
        out["models"] = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT embedding_model FROM corpus_manifest").fetchall()]
        out["index_versions"] = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT index_version FROM corpus_manifest").fetchall()]
        out["chunk_types"] = {
            r[0]: r[1] for r in self.conn.execute(
                "SELECT chunk_type, COUNT(*) FROM corpus_chunks "
                "WHERE source_type=? GROUP BY chunk_type",
                (self.source_type,)).fetchall()}
        return out

    def _frequent_entities(self) -> set:
        return {r[0] for r in self.conn.execute(
            "SELECT entity FROM corpus_entity_links "
            "GROUP BY entity HAVING COUNT(*) >= 2").fetchall()}

    def query(self, text: str, top_k: int = 3,
              w_entity: float = W_ENTITY,
              w_lexical: float = W_LEXICAL) -> dict:
        rows = self.conn.execute(
            "SELECT id, source_id, skill_name, chunk_no, chunk_type, heading,"
            " content, entities_json, tokens_json, vector_bin "
            "FROM corpus_chunks WHERE source_type=? ORDER BY id",
            (self.source_type,)).fetchall()
        qvec = self.embedder.embed(text)
        if qvec is None:
            raise RuntimeError(f"query 嵌入失败: {text!r}")
        q = np.asarray(qvec, dtype=np.float32)
        qn = float(np.linalg.norm(q))
        if qn == 0:
            raise RuntimeError("query 向量为零")
        q = q / qn

        usable = []
        vectors = []
        for r in rows:
            if r["vector_bin"] is None:
                continue
            v = np.frombuffer(r["vector_bin"], dtype=np.float32)
            if v.shape != q.shape:
                continue
            n = float(np.linalg.norm(v))
            if n == 0:
                continue
            usable.append(r)
            vectors.append(v / n)
        q_entities = extract_entities(text)
        q_tokens = tokenize(text)
        if not usable:
            if rows:
                raise RuntimeError(
                    f"没有可用向量：{len(rows)} 个 chunk 的维度与 query 不一致")
            return {"query": text, "weights": {"entity": w_entity,
                    "lexical": w_lexical}, "entities": q_entities,
                    "tokens": q_tokens, "candidates": 0, "results": []}
        mat = np.vstack(vectors)
        cos = mat @ q

        frequent = self._frequent_entities()
        q_entities = [e for e in q_entities if e in frequent]
        qset = set(q_tokens)

        scored = []
        for i, r in enumerate(usable):
            try:
                ents = set(json.loads(r["entities_json"] or "[]"))
            except Exception:
                ents = set()
            hits = len(set(q_entities) & ents)
            hits = min(hits, ENTITY_CAP)
            try:
                toks = set(json.loads(r["tokens_json"] or "[]"))
            except Exception:
                toks = set()
            lexical = (len(qset & toks) / len(qset)) if qset else 0.0
            c = float(cos[i])
            total = c + w_entity * hits + w_lexical * lexical  # 待 P4 消融
            scored.append({
                "id": r["id"],
                "source_id": r["source_id"],
                "skill_name": r["skill_name"],
                "chunk_no": r["chunk_no"],
                "chunk_type": r["chunk_type"],
                "heading": r["heading"],
                "cos": round(c, 6),
                "entity_hits": hits,
                "lexical_norm": round(lexical, 6),
                "total": round(total, 6),
                "content": r["content"],
            })
        scored.sort(key=lambda x: (-x["total"], -x["cos"],
                                   x["source_id"], x["chunk_no"]))
        return {
            "query": text,
            "weights": {"entity": w_entity, "lexical": w_lexical},
            "entities": q_entities,
            "tokens": q_tokens,
            "candidates": len(scored),
            "results": scored[:max(0, top_k)],
        }


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cmd_index(args):
    with CorpusIndex(args.db, args.root, source_type=args.source_type,
                     batch_size=args.batch_size, quiet=args.quiet,
                     include_references=args.include_references) as ci:
        result = ci.index(force=args.force, limit=args.limit,
                          dry_run=args.dry_run)
    return 0


def _cmd_stats(args):
    with CorpusIndex(args.db, args.root, source_type=args.source_type,
                     include_references=args.include_references) as ci:
        print(json.dumps(ci.stats(), ensure_ascii=False, indent=2))
    return 0


def _cmd_query(args):
    with CorpusIndex(args.db, args.root, source_type=args.source_type) as ci:
        result = ci.query(args.text, top_k=args.top, w_entity=args.we,
                          w_lexical=args.wl)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"query: {result['query']}")
        print(f"weights: entity={result['weights']['entity']} "
              f"lexical={result['weights']['lexical']}")
        print(f"entities: {result.get('entities')}")
        for i, r in enumerate(result["results"], 1):
            print(f"{i}. {r['skill_name']} | {r['heading'] or '(no heading)'}"
                  f" | cos={r['cos']:.4f} ent={r['entity_hits']} "
                  f"lex={r['lexical_norm']:.4f} total={r['total']:.4f}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m memorykit.corpus",
                                description="skill/doc 语料索引与召回")
    p.add_argument("--db", default=None,
                   help="SQLite 路径（默认 MEMORY_DB_PATH）")
    p.add_argument("--root", default=None,
                   help="skills 根目录（默认 ~/.hermes/skills）")
    p.add_argument("--source-type", default="skill")
    p.add_argument("--include-references", dest="include_references",
                   action="store_true", default=True,
                   help="索引 references/**/*.md（默认 on）")
    p.add_argument("--no-include-references", dest="include_references",
                   action="store_false",
                   help="只索引 SKILL.md（关闭 references）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="增量建库")
    pi.add_argument("--force", action="store_true")
    pi.add_argument("--limit", type=int, default=None, help="只索引前 N 个文件")
    pi.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    pi.add_argument("--dry-run", action="store_true",
                    help="只扫描/切块并报告预计新增块数，不嵌入、不写库")
    pi.add_argument("--quiet", action="store_true")
    pi.set_defaults(func=_cmd_index)

    ps = sub.add_parser("stats", help="统计")
    ps.set_defaults(func=_cmd_stats)

    pq = sub.add_parser("query", help="召回")
    pq.add_argument("text")
    pq.add_argument("--top", type=int, default=3)
    pq.add_argument("--we", type=float, default=W_ENTITY)
    pq.add_argument("--wl", type=float, default=W_LEXICAL)
    pq.add_argument("--json", action="store_true")
    pq.set_defaults(func=_cmd_query)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
