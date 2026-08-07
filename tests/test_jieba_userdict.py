#!/usr/bin/env python3
"""jieba 用户词典回归测试（2026-08-07，任务 t_921e668a）

覆盖：
  - 中文专名整词命中（不再依赖 7a/7b 合并启发式）：
    宠物名（小鸡翅/小翅/鸡翅）、品种（银渐层/英短/布偶）、
    地名（虹桥火车站/华侨城创意园/北京）、人名（小明/老王/Joe/Hermes）
  - 硬件/项目专名：华南/X99-F8D（含连字符，posseg 直配兜底）/
    CX400M4/傲腾/Bonefish/memorykit
  - 英文实体（GPU/API/LLM 等）与无中文场景不受用户词典影响
  - 词典不污染日常句（今天天气不错适合散步 → []）

运行：
  python run_tests.py
  或：python -m unittest tests.test_jieba_userdict -v
"""
import os
import sys
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


class TestJiebaUserdict(unittest.TestCase):
    """用户词典加持下的中文专名实体抽取。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_cat_names_whole(self):
        # 任务验收: 我的猫叫小鸡翅，是只银渐层 → [小鸡翅, 银渐层]
        ents = self.db._extract_entities("我的猫叫小鸡翅，是只银渐层")
        self.assertIn("小鸡翅", ents)
        self.assertIn("银渐层", ents)

    def test_pet_name_variants(self):
        ents = self.db._extract_entities("小鸡翅、小翅和鸡翅都是猫")
        for w in ("小鸡翅", "小翅", "鸡翅"):
            self.assertIn(w, ents)

    def test_breeds(self):
        # 英短 整词命中；布偶+猫 合并为 布偶猫（含 布偶 语义）
        ents = self.db._extract_entities("英短蓝猫和布偶猫都很可爱")
        self.assertIn("英短", ents)
        self.assertTrue(any("布偶" in e for e in ents), f"布偶相关实体缺失: {ents}")

    def test_train_station(self):
        # 任务验收: 虹桥火车站 整词命中（此前只抽到 虹桥）
        ents = self.db._extract_entities("我明天要去虹桥火车站接人")
        self.assertIn("虹桥火车站", ents)

    def test_city_and_conference(self):
        # 任务验收: 北京 保留
        ents = self.db._extract_entities("下个月去北京参加机器学习会议")
        self.assertIn("北京", ents)

    def test_creative_park(self):
        # 华侨城创意园 整词命中（此前被切碎为 华侨城+创意+园）
        ents = self.db._extract_entities("小明和老王在华侨城创意园聊memorykit项目")
        self.assertIn("华侨城创意园", ents)
        self.assertIn("小明", ents)
        self.assertIn("老王", ents)
        self.assertIn("memorykit", ents)

    def test_hyphenated_model_direct_match(self):
        # X99-F8D 含连字符，posseg 的 re_han_internal 无法整块承载，
        # 走用户词典直配兜底（此前被拆成 X99 + F8D）
        ents = self.db._extract_entities("服务器配置是华南X99-F8D双路主板")
        self.assertIn("X99-F8D", ents)
        self.assertIn("华南", ents)
        for kw in ("服务器", "主板"):
            self.assertIn(kw, ents)

    def test_other_hardware_and_tech_names(self):
        ents = self.db._extract_entities("内存条是CX400M4，傲腾P1600X")
        self.assertIn("CX400M4", ents)
        self.assertIn("傲腾", ents)

    def test_people_and_tech_project(self):
        ents = self.db._extract_entities("Joe 和 Hermes 在聊 Bonefish 项目")
        for w in ("Joe", "Hermes", "Bonefish"):
            self.assertIn(w, ents)

    def test_english_entities_unaffected(self):
        # 用户词典与块切分不得影响纯英文实体抽取
        ents = self.db._extract_entities(
            "I am considering buying an NVIDIA RTX 4090 with 24GB VRAM "
            "for YOLO training, using my old card to get started"
        )
        low = {e.lower() for e in ents}
        for bad in ("am", "buying", "considering", "get", "used", "card",
                    "using", "with", "for", "my", "to", "an"):
            self.assertNotIn(bad, low)
        for good in ("NVIDIA", "VRAM", "YOLO"):
            self.assertIn(good, ents)

    def test_dict_does_not_pollute_common_sentence(self):
        # 词典里的专名不得让日常句变噪音
        self.assertEqual(self.db._extract_entities("今天天气不错适合散步"), [])
        self.assertEqual(
            self.db._extract_entities("2026-08-07天气不错去公园散步买水果"), [])

    def test_entities_flow_into_memory_column(self):
        self.db.set("ent/station", "我明天要去虹桥火车站接人",
                    category="env", embed=False)
        row = self.db.get("ent/station")
        assert row is not None
        ents = __import__("json").loads(row["entities"]) if row["entities"] else []
        self.assertIn("虹桥火车站", ents)

    def test_no_chinese_skips_jieba_entirely(self):
        # 懒加载约束: 纯英文文本不触发 jieba 首载。独立子进程验证，
        # 避免本测试进程已被其他用例触发过 jieba import。
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            "import sys, os, tempfile\n"
            "sys.path.insert(0, %r)\n"
            "fd, p = tempfile.mkstemp(suffix='.db'); os.close(fd); os.remove(p)\n"
            "from memorykit import MemoryDB\n"
            "db = MemoryDB(db_path=p)\n"
            "ents = db._extract_entities('GPU and API for LLM training')\n"
            "print('jieba_imported=', 'jieba' in sys.modules)\n"
            "print('ents=', ents)\n"
        ) % root
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("jieba_imported= False", out.stdout)
        self.assertIn("GPU", out.stdout)
        self.assertIn("API", out.stdout)
        self.assertIn("LLM", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
