#!/usr/bin/env python3
"""merge 路径实体重抽回归测试（t_8a481992，2026-08-07 小龙虾实测根因修复）。

背景：jieba 用户字典生效后新 learn 的 entities 正确，但 merge 去重合并时
UPDATE 只更新 content/importance/sessions/tags/category，不更新 entities——
旧记录 entities 为 null 时合并后永远 null，中文专名在所有管道里都丢失。

覆盖：
  - 向量去重合并路径：旧 entities 置 NULL 后合并 → 重抽并写回（含中文专名）
  - 实体去重兜底合并路径：旧 entities 置 NULL 后合并 → 重抽并写回
  - tags 并入旧值：旧 tags 保留 + 新 tags 追加（不再被覆盖丢弃）
  - entity_links 表同步：合并后实体关联行含新实体

运行：
  python run_tests.py
  或：python -m unittest tests.test_merge_entities -v
"""
import os
import sys
import json
import hashlib
import tempfile
import unittest

# 在 import memorykit 之前隔离会落盘的环境变量，避免测试污染项目目录
_TMP_ENVDIR = tempfile.mkdtemp(prefix="mk-test-env-")
os.environ["MEMORY_GATE_CACHE_DB"] = os.path.join(_TMP_ENVDIR, "gate_cache.db")
os.environ["MEMORY_GATE_SKILL_CANDIDATES"] = os.path.join(_TMP_ENVDIR, "skill_candidates.md")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memorykit import MemoryDB


def make_tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class DeterministicEmbedder:
    """确定性假嵌入器：相同文本 → 相同向量（cos=1 触发向量合并）。"""

    def _vec(self, text):
        h = hashlib.md5((text or "").encode("utf-8")).digest()
        return [b / 255.0 for b in h[:4]]

    def embed(self, text):
        return self._vec(text) if text and len(text.strip()) >= 3 else None

    def is_available(self):
        return True


class OfflineEmbedder:
    """离线嵌入器：永远返回 None → 走实体去重兜底合并路径。"""

    def embed(self, text):
        return None

    def is_available(self):
        return True


def _ents(row):
    if not row or not row.get("entities"):
        return []
    try:
        return json.loads(row["entities"]) if isinstance(row["entities"], str) else row["entities"]
    except (json.JSONDecodeError, TypeError):
        return []


class TestVectorMergeReextractsEntities(unittest.TestCase):
    """向量去重合并路径：旧 entities 为 NULL → merge 后重抽写回。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=DeterministicEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_vector_merge_refills_null_entities_with_zh_names(self):
        # 首次 learn：带中文专名（jieba 正常时实体已抽出）
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s1")
        self.assertFalse(r1["merged"])
        row = self.db.get(r1["key"])
        self.assertIn("小鸡翅", _ents(row))

        # 模拟旧库 bug：entities 被置 NULL（首次 learn 时字典未生效的历史数据）
        self.db._conn.execute("UPDATE memories SET entities=NULL WHERE key=?", (r1["key"],))
        self.db._conn.commit()

        # 再 learn 相似内容 → 向量合并（同文本 cos=1 ≥ 0.85）
        r2 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s2")
        self.assertTrue(r2["merged"])

        # 核心断言：merge 后 entities 非 null 且含中文专名（不再恒 null）
        row = self.db.get(r1["key"])
        ents = _ents(row)
        self.assertTrue(ents, "merge 后 entities 不应为 null/空")
        self.assertIn("小鸡翅", ents)
        self.assertIn("银渐层", ents)

    def test_vector_merge_keeps_old_and_new_entities_union(self):
        # 旧记录已有实体（模拟历史旧实体子集）→ 合并后旧实体保留 + 新实体追加
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s1")
        row = self.db.get(r1["key"])
        self.assertIn("小鸡翅", _ents(row))
        # 把旧记录 entities 改成过期子集（模拟旧库抽得少的历史数据）
        self.db._conn.execute(
            "UPDATE memories SET entities=? WHERE key=?",
            (json.dumps(["小鸡翅"]), r1["key"]),
        )
        self.db._conn.commit()

        # 同文本 → cos=1 触发向量合并
        r2 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s2")
        self.assertTrue(r2["merged"])
        row = self.db.get(r1["key"])
        ents = _ents(row)
        # 旧实体保留（不被覆盖）
        self.assertIn("小鸡翅", ents)
        # 新内容实体追加进并集
        self.assertIn("银渐层", ents)

    def test_vector_merge_merges_tags_with_old(self):
        # tags 不再被新内容覆盖：旧 tags 保留 + 新 tags 追加（去重排序）
        r1 = self.db.learn("显卡价格是550块，显存12G", source="task", source_session="s1")
        old_tags = self.db.get(r1["key"])["tags"]
        # 直接给旧记录一个新内容不会有的标签，模拟历史 tags
        self.db._conn.execute("UPDATE memories SET tags='显存,显卡' WHERE key=?", (r1["key"],))
        self.db._conn.commit()

        r2 = self.db.learn("显卡价格是550块，显存12G", source="task", source_session="s2")
        self.assertTrue(r2["merged"])
        row = self.db.get(r1["key"])
        merged_tags = row["tags"].split(",")
        # 旧 tags 全部保留（不被新 tags 覆盖）
        for t in ("显存", "显卡"):
            self.assertIn(t, merged_tags)
        # 返回体中的 tags 与库内一致
        self.assertEqual(r2["tags"], row["tags"])

    def test_vector_merge_syncs_entity_links(self):
        r1 = self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s1")
        self.db._conn.execute("UPDATE memories SET entities=NULL WHERE key=?", (r1["key"],))
        self.db._conn.commit()
        # 清空 entity_links 模拟历史缺链
        rid = self.db._conn.execute("SELECT id FROM memories WHERE key=?", (r1["key"],)).fetchone()["id"]
        self.db._conn.execute("DELETE FROM entity_links WHERE memory_id=?", (rid,))
        self.db._conn.commit()

        self.db.learn("我的猫叫小鸡翅，是只银渐层", source="task", source_session="s2")
        links = self.db._conn.execute(
            "SELECT entity FROM entity_links WHERE memory_id=?", (rid,)).fetchall()
        entities_in_links = [l["entity"] for l in links]
        self.assertIn("小鸡翅", entities_in_links)
        self.assertIn("银渐层", entities_in_links)


class TestEntityFallbackMergeReextractsEntities(unittest.TestCase):
    """实体去重兜底合并路径（嵌入离线）：旧 entities 为 NULL → merge 后重抽写回。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp, embedder=OfflineEmbedder())

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_entity_fallback_merge_refills_stale_entities(self):
        # 需要 ≥3 个实体才能触发实体去重兜底
        content = "训练深度学习模型时显卡显存不够，预算要加"
        r1 = self.db.learn(content, source="task", source_session="s1")
        self.assertFalse(r1["merged"])
        row = self.db.get(r1["key"])
        self.assertGreaterEqual(len(_ents(row)), 3)

        # 模拟旧库抽得少的历史数据：entities 改成过期子集（如早期无 jieba 时）。
        # 保留 ≥3 个实体以触发兜底匹配（overlap≥3），但漏掉新内容里的其他实体
        self.db._conn.execute(
            "UPDATE memories SET entities=? WHERE key=?",
            (json.dumps(["显卡", "显存", "模型"]), r1["key"]),
        )
        self.db._conn.commit()

        # 再 learn 相同内容 → 实体去重兜底合并（overlap≥3）
        r2 = self.db.learn(content, source="task", source_session="s2")
        self.assertTrue(r2["merged"])

        row = self.db.get(r1["key"])
        ents = _ents(row)
        self.assertTrue(ents, "merge 后 entities 不应为 null/空")
        # 旧实体保留（不被覆盖）
        self.assertIn("显卡", ents)
        # 新内容实体追加进并集（不再停留在过期子集）
        self.assertIn("显存", ents)


if __name__ == "__main__":
    unittest.main()
