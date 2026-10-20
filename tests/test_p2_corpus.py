#!/usr/bin/env python3
"""P2 corpus：fence-aware 切块 + 增量索引 + 加权召回。"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memorykit.corpus import (
    CorpusIndex,
    MAX_CHUNK_CHARS,
    chunk_skill_text,
    extract_entities,
    split_frontmatter,
    tokenize,
)


class FakeEmbedder:
    """确定性 bag-of-token 向量；同词文本 cos 高，不同词接近 0。"""

    dims = 16

    def _vec(self, text):
        v = [0.0] * self.dims
        for tok in tokenize(text):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            v[h[0] % self.dims] += 1.0
        n = sum(x * x for x in v) ** 0.5
        if n == 0:
            v[0] = 1.0
            return v
        return [x / n for x in v]

    def embed(self, text):
        return self._vec(text)

    def embed_batch(self, texts):
        return [self._vec(t) for t in texts]

    def is_available(self):
        return True


def fm(name, desc="d"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n"


class TestChunker(unittest.TestCase):
    def test_frontmatter_is_separate_chunk(self):
        text = fm("demo", "耳机 电池 type-c") + "\n# Demo\n\n## A\nbody\n"
        chunks = chunk_skill_text(text, "demo")
        self.assertEqual(chunks[0].chunk_type, "frontmatter")
        self.assertEqual(chunks[0].heading, "frontmatter")
        self.assertIn("name: demo", chunks[0].content)
        self.assertTrue(any(c.heading == "A" for c in chunks))

    def test_frontmatter_only(self):
        chunks = chunk_skill_text(fm("only", "just desc"), "only")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].chunk_type, "frontmatter")
        self.assertEqual(chunks[0].content, "name: only\ndescription: just desc")

    def test_empty_file(self):
        self.assertEqual(chunk_skill_text("", "empty"), [])

    def test_heading_inside_fence_not_split(self):
        text = (
            "## Real\n"
            "before\n\n"
            "```bash\n"
            "## not a heading\n"
            "echo hi\n"
            "```\n\n"
            "after\n"
        )
        chunks = chunk_skill_text(text, "fence")
        self.assertEqual([c.heading for c in chunks], ["Real"])
        self.assertIn("## not a heading", chunks[0].content)

    def test_nested_fence_not_split(self):
        text = (
            "## Outer\n"
            "````markdown\n"
            "```bash\n"
            "## still code\n"
            "```\n"
            "````\n"
            "## Next\n"
            "body\n"
        )
        chunks = chunk_skill_text(text, "nested")
        self.assertEqual([c.heading for c in chunks], ["Outer", "Next"])
        self.assertIn("## still code", chunks[0].content)

    def test_unclosed_fence_swallows_rest(self):
        text = "## A\n```\n## B\nbody\n"
        chunks = chunk_skill_text(text, "unclosed")
        self.assertEqual([c.heading for c in chunks], ["A"])
        self.assertIn("## B", chunks[0].content)

    def test_h2_h3_mixed_and_preamble(self):
        text = "# Title\n\nintro\n\n## A\n\n### A1\n\na1\n\n### A2\n\na2\n\n## B\n\nb\n"
        chunks = chunk_skill_text(text, "mix")
        self.assertEqual(chunks[0].chunk_type, "preamble")
        self.assertIn("# Title", chunks[0].content)
        self.assertEqual([c.heading for c in chunks[1:]],
                         ["A", "A1", "A2", "B"])

    def test_heading_only_kept(self):
        chunks = chunk_skill_text("## Empty\n## Next\ntext\n", "h")
        self.assertEqual([c.heading for c in chunks], ["Empty", "Next"])
        self.assertEqual(chunks[0].content, "")

    def test_long_boundary_1200_exact(self):
        body = "x" * MAX_CHUNK_CHARS
        chunks = chunk_skill_text("## Long\n" + body, "long")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0].content), MAX_CHUNK_CHARS)

    def test_long_boundary_1201_splits(self):
        body = "x" * (MAX_CHUNK_CHARS + 1)
        chunks = chunk_skill_text("## Long\n" + body, "long")
        self.assertEqual(len(chunks), 2)
        self.assertEqual(sum(len(c.content) for c in chunks),
                         MAX_CHUNK_CHARS + 1)
        self.assertEqual(len(chunks[0].content), MAX_CHUNK_CHARS)
        self.assertEqual(len(chunks[1].content), 1)

    def test_long_split_prefers_blank_line(self):
        body = ("a" * 700) + "\n\n" + ("b" * 700)
        chunks = chunk_skill_text("## Long\n" + body, "long")
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].content.endswith("a"))
        self.assertTrue(chunks[1].content.startswith("b"))
        self.assertLessEqual(len(chunks[0].content), MAX_CHUNK_CHARS)
        self.assertLessEqual(len(chunks[1].content), MAX_CHUNK_CHARS)

    def test_crlf_normalized(self):
        text = fm("win").replace("\n", "\r\n") + "## A\r\nbody\r\n"
        chunks = chunk_skill_text(text, "win")
        self.assertEqual(chunks[0].content, "name: win\ndescription: d")
        self.assertIn("body", chunks[1].content)

    def test_frontmatter_parser_quotes_and_block(self):
        meta, body, raw = split_frontmatter(
            '---\nname: "q"\ndescription: >\n  line one\n  line two\n---\nbody\n')
        self.assertEqual(meta["name"], "q")
        self.assertEqual(meta["description"], "line one line two")
        self.assertEqual(body, "body\n")
        self.assertIn("description", raw)


class TestEntityToken(unittest.TestCase):
    def test_unknown_word_bigram_and_type_c(self):
        toks = tokenize("飞书回复被截断 type-c")
        self.assertIn("飞书", toks)
        self.assertIn("type-c", toks)

    def test_entities_supplement_nouns_and_ascii(self):
        ents = extract_entities("耳机电池不行了，想改成 type-c 充电")
        self.assertIn("耳机", ents)
        self.assertIn("电池", ents)
        self.assertIn("type-c", ents)

    def test_blacklist_lowercase(self):
        ents = extract_entities("assistant home user Hermes")
        self.assertNotIn("assistant", ents)
        self.assertNotIn("home", ents)
        self.assertNotIn("user", ents)
        self.assertNotIn("hermes", ents)


class TestCorpusIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.db = Path(self.tmp.name) / "corpus.db"
        (self.root / "alpha").mkdir(parents=True)
        (self.root / "beta").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(
            fm("alpha", "alpha quantum widget") + "\n## Alpha\n"
            "alpha quantum widget calibration\n", encoding="utf-8")
        (self.root / "beta" / "SKILL.md").write_text(
            fm("beta", "beta network") + "\n## Beta\nbeta network routing\n",
            encoding="utf-8")
        (self.root / ".archive" / "old").mkdir(parents=True)
        (self.root / ".archive" / "old" / "SKILL.md").write_text(
            fm("old", "archived") + "\n## Old\nold\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self):
        return CorpusIndex(db_path=str(self.db), root=str(self.root),
                           embedder=FakeEmbedder(), embedding_model="fake",
                           quiet=True)

    def test_index_skips_archive_and_manifest_fields(self):
        with self.make() as ci:
            r = ci.index()
            self.assertEqual(r["files_total"], 2)
            self.assertEqual(r["files_indexed"], 2)
            self.assertEqual(r["chunks_indexed"], 4)  # 每份: 1 frontmatter + 1 heading
            rows = ci.conn.execute(
                "SELECT * FROM corpus_manifest ORDER BY path").fetchall()
            self.assertEqual([x["path"] for x in rows],
                             ["alpha/SKILL.md", "beta/SKILL.md"])
            cols = {x[1] for x in ci.conn.execute(
                "PRAGMA table_info(corpus_manifest)").fetchall()}
            self.assertEqual(cols, {
                "path", "mtime", "size", "sha256", "chunk_count",
                "index_version", "embedding_model", "updated_at"})
            self.assertEqual(ci.stats()["frontmatter_chunks"], 2)

    def test_incremental_skip_and_update(self):
        with self.make() as ci:
            ci.index()
            r2 = ci.index()
            self.assertEqual(r2["files_indexed"], 0)
            self.assertEqual(r2["files_skipped"], 2)
            # 改 alpha：只重嵌 1 个文件
            p = self.root / "alpha" / "SKILL.md"
            p.write_text(p.read_text(encoding="utf-8") + "\n## More\nextra\n",
                         encoding="utf-8")
            r3 = ci.index()
            self.assertEqual(r3["files_indexed"], 1)
            self.assertEqual(r3["files_skipped"], 1)
            self.assertEqual(ci.stats()["files"], 2)
            # 删 beta：manifest + chunks 一起清
            (self.root / "beta" / "SKILL.md").unlink()
            r4 = ci.index()
            self.assertEqual(r4["files_removed"], 1)
            self.assertEqual(ci.stats()["files"], 1)

    def test_limit_does_not_delete_unscanned_manifest(self):
        with self.make() as ci:
            ci.index()
            r = ci.index(limit=1)
            self.assertEqual(r["files_removed"], 0)
            self.assertEqual(ci.stats()["files"], 2)

    def test_entity_df_threshold(self):
        with self.make() as ci:
            ci.index()
            ents = {r[0] for r in ci.conn.execute(
                "SELECT DISTINCT entity FROM corpus_entity_links").fetchall()}
            # quantum 出现在 frontmatter + 正文两处 → 保留；widget 也是
            self.assertIn("quantum", ents)
            self.assertNotIn("calibration", ents)  # 只出现一次

    def test_query_scores_and_ranking(self):
        with self.make() as ci:
            ci.index()
            out = ci.query("alpha quantum", top_k=3)
            self.assertEqual(out["results"][0]["skill_name"], "alpha")
            r = out["results"][0]
            for k in ("cos", "entity_hits", "lexical_norm", "total"):
                self.assertIn(k, r)
            expected = r["cos"] + 0.10 * r["entity_hits"] + 0.25 * r["lexical_norm"]
            self.assertAlmostEqual(r["total"], round(expected, 6), places=6)
            zero = ci.query("alpha quantum", top_k=1, w_entity=0, w_lexical=0)
            self.assertAlmostEqual(zero["results"][0]["total"],
                                   zero["results"][0]["cos"], places=6)

    def test_query_lexical_can_promote(self):
        # 词法命中但向量接近 0 的块：w_l 打开后应进入 top-1
        with self.make() as ci:
            ci.index()
            out = ci.query("calibration", top_k=1, w_entity=0, w_lexical=1.0)
            self.assertEqual(out["results"][0]["skill_name"], "alpha")
            self.assertGreater(out["results"][0]["lexical_norm"], 0)

    def test_empty_file_manifest_zero_chunks(self):
        (self.root / "empty").mkdir()
        (self.root / "empty" / "SKILL.md").write_text("", encoding="utf-8")
        with self.make() as ci:
            r = ci.index()
            self.assertEqual(r["chunks_total"], 4)
            row = ci.conn.execute(
                "SELECT chunk_count FROM corpus_manifest WHERE path='empty/SKILL.md'"
            ).fetchone()
            self.assertEqual(row[0], 0)


if __name__ == "__main__":
    unittest.main()
