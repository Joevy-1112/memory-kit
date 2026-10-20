#!/usr/bin/env python3
"""P4：references 纳入语料索引 —— 切块类型/来源、dry-run、增量不重嵌。"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from memorykit.corpus import (
    CorpusIndex,
    REFERENCE_CHUNK_TYPE,
    chunk_reference_text,
    is_reference_path,
)


class CountingEmbedder:
    """确定性向量 + 记录 embed/embed_batch 调用条数（验证增量不重嵌）。"""

    dims = 16

    def __init__(self):
        self.single_calls = 0
        self.batch_items = 0
        self.batch_calls = 0

    def _vec(self, text):
        v = [0.0] * self.dims
        for tok in (text or "").lower().split():
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            v[h[0] % self.dims] += 1.0
        n = sum(x * x for x in v) ** 0.5
        if n == 0:
            v[0] = 1.0
            return v
        return [x / n for x in v]

    def embed(self, text):
        self.single_calls += 1
        return self._vec(text)

    def embed_batch(self, texts):
        self.batch_calls += 1
        self.batch_items += len(texts)
        return [self._vec(t) for t in texts]

    def is_available(self):
        return True


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_tree(root: Path):
    write(root / "demo" / "SKILL.md",
          "---\nname: demo-skill\ndescription: demo desc\n---\n\n## Usage\nbody\n")
    write(root / "demo" / "references" / "alpha.md",
          "## Alpha Ref\nalpha body content\n\n## Alpha Two\nmore alpha\n")
    write(root / "demo" / "references" / "nested" / "beta.md",
          "# Beta\n\nno h2 here\n")
    write(root / "other" / "SKILL.md",
          "---\nname: other-skill\ndescription: other desc\n---\n\n## B\nb\n")
    # 没有 SKILL.md 的目录：整棵不索引
    write(root / "orphan" / "references" / "lost.md", "## Lost\nx\n")


class TestReferencePaths(unittest.TestCase):
    def test_is_reference_path(self):
        self.assertTrue(is_reference_path("a/references/x.md"))
        self.assertTrue(is_reference_path("a/references/sub/x.md"))
        self.assertFalse(is_reference_path("a/SKILL.md"))
        self.assertFalse(is_reference_path("references/x.md"))

    def test_chunk_type_is_reference(self):
        chunks = chunk_reference_text("## A\na\n\n## B\nb\n")
        self.assertTrue(chunks)
        self.assertEqual({c.chunk_type for c in chunks}, {REFERENCE_CHUNK_TYPE})
        self.assertEqual([c.heading for c in chunks], ["A", "B"])


class TestReferenceIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(suffix="-p4refs")
        self.root = Path(self.tmp) / "skills"
        make_tree(self.root)
        self.db = str(Path(self.tmp) / "corpus.db")
        self.emb = CountingEmbedder()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discover_includes_references_only_under_skills(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True) as ci:
            rels = [rel for rel, _ in ci.discover()]
        self.assertIn("demo/SKILL.md", rels)
        self.assertIn("demo/references/alpha.md", rels)
        self.assertIn("demo/references/nested/beta.md", rels)
        self.assertNotIn("orphan/references/lost.md", rels)
        self.assertEqual(len(rels), 4)

    def test_discover_can_exclude_references(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True, include_references=False) as ci:
            rels = [rel for rel, _ in ci.discover()]
        self.assertEqual(rels, ["demo/SKILL.md", "other/SKILL.md"])

    def test_index_writes_reference_chunks_with_skill_name(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True) as ci:
            result = ci.index()
            self.assertEqual(result["files_indexed"], 4)
            rows = [dict(r) for r in ci.conn.execute(
                "SELECT source_id, skill_name, chunk_type FROM corpus_chunks "
                "ORDER BY source_id, chunk_no")]
            refs = [r for r in rows if r["chunk_type"] == REFERENCE_CHUNK_TYPE]
            self.assertTrue(refs)
            self.assertTrue(all(r["skill_name"] == "demo-skill" for r in refs))
            self.assertIn("demo/references/alpha.md",
                          {r["source_id"] for r in refs})
            # SKILL.md 的结构类型保持不变
            self.assertIn("frontmatter", {r["chunk_type"] for r in rows})
            self.assertIn("heading", {r["chunk_type"] for r in rows})

    def test_dry_run_writes_nothing_and_reports_new_chunks(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True) as ci:
            dry = ci.index(dry_run=True)
            self.assertTrue(dry["dry_run"])
            self.assertGreater(dry["chunks_new"], 0)
            self.assertEqual(ci.conn.execute(
                "SELECT COUNT(*) FROM corpus_chunks").fetchone()[0], 0)
            self.assertEqual(ci.conn.execute(
                "SELECT COUNT(*) FROM corpus_manifest").fetchone()[0], 0)
            self.assertEqual(self.emb.batch_items + self.emb.single_calls, 0)
            real = ci.index()
            self.assertEqual(dry["chunks_new"], real["chunks_indexed"])
            self.assertEqual(dry["files_changed"], real["files_indexed"])

    def test_incremental_skips_unchanged_skill_md(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True) as ci:
            first = ci.index()
            embedded_first = self.emb.batch_items + self.emb.single_calls
            self.assertEqual(embedded_first, first["chunks_indexed"])
            self.emb.batch_items = 0
            self.emb.single_calls = 0
            second = ci.index()
            self.assertEqual(second["files_indexed"], 0)
            self.assertEqual(second["files_skipped"], 4)
            self.assertEqual(self.emb.batch_items + self.emb.single_calls, 0)

    def test_enabling_references_only_embeds_reference_chunks(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True, include_references=False) as ci:
            first = ci.index()
            self.assertEqual(first["files_indexed"], 2)
            self.emb.batch_items = 0
            self.emb.single_calls = 0
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True, include_references=True) as ci:
            second = ci.index()
            # 2 个 SKILL.md 的 hash 未变 → 跳过；只嵌 references
            self.assertEqual(second["files_indexed"], 2)
            self.assertEqual(second["files_skipped"], 2)
            self.assertEqual(
                second["chunks_new_by_kind"].get("skill", {"files": 0})["files"], 0)
            self.assertEqual(
                self.emb.batch_items + self.emb.single_calls,
                second["chunks_indexed"])

    def test_toggle_off_removes_reference_sources(self):
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True, include_references=True) as ci:
            ci.index()
        with CorpusIndex(self.db, str(self.root), embedder=self.emb,
                         quiet=True, include_references=False) as ci:
            result = ci.index()
            self.assertEqual(result["files_removed"], 2)
            self.assertEqual(ci.conn.execute(
                "SELECT COUNT(*) FROM corpus_chunks WHERE chunk_type=?",
                (REFERENCE_CHUNK_TYPE,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
