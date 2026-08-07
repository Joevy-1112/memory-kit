#!/usr/bin/env python3
"""小龙虾实测反馈 3 问题的回归测试（2026-08-07）

覆盖：
  #1 中文实体抽取污染：
     - 非技术中文句不再被 `[\u4e00-\u9fff]{2,6}` 切成碎片 tag（应返回 []）
     - 技术中文句只提取词表实词（显卡/显存/深度学习/模型…）
     - 英文高频虚词（am/buying/considering/get/used/card…）不再被捞成实体
  #2 episodic 会话索引接进 learn：
     - learn(source_session=...) 自动建 episodic 记录（幂等，同 session 不重复）
     - learn 返回前 episodic 行带 summary/tags，可被 episodic_search 检索
  #3 验证闭环 learn 侧触发：
     - learn 返回带 verify 状态（三条路径：新建/向量合并/实体合并）
     - importance>=0.6 + 有 state.db + 有源 → provenance 留 verify 痕迹（vscore/auto）
     - 高分自动验证小幅提升 confidence（不再纯出厂值）
     - 无 state.db / 无源 / 低重要度 → 保持 pending 不降级
  #4 验证闭环真正打分（vscore=0 修复，降级路径）：
     - OpenClaw 场景（无 state.db / 无源 session）→ _lightweight_verify 库内
       轻量验证：相似记忆一致性 cos>0.85（0.3-0.7）/ 时间锚定（0.30）/
       实体密度（0.25）→ provenance vscore 非 0 + method + fallback=true
     - 无证据仍 pending 不写痕迹、不虚高；verify() 源不可达不再误降 importance
     - 降级路径高分（>=0.5）→ v=verified + confidence +0.05（与 verify() 对齐）

运行：
  python run_tests.py
  或：python -m unittest tests.test_feedback_fixes -v
"""
import os
import sys
import json
import sqlite3
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


def make_state_db(session_id="sess-1", texts=None):
    """造一个 Hermes 风格 state.db（messages 表）供 _cross_check 使用。"""
    path = make_tmp_db()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "content TEXT, timestamp TEXT)"
    )
    for i, t in enumerate(texts or ["显卡价格是550块，显存12G"], start=1):
        conn.execute(
            "INSERT INTO messages (session_id, content, timestamp) VALUES (?, ?, ?)",
            (session_id, t, f"2026-08-07 10:0{i}:00"),
        )
    conn.commit()
    conn.close()
    return path


class TestEntityExtraction(unittest.TestCase):
    """#1 中文实体抽取污染 + 英文虚词过滤。"""

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

    def test_zh_non_tech_sentence_no_fragments(self):
        # 实测反馈样例：整句中文不再被切成 2-6 字碎片 tag
        ents = self.db._extract_entities("2026-08-07天气不错去公园散步买水果")
        self.assertEqual(ents, [])

    def test_zh_tech_sentence_extracts_terms(self):
        ents = self.db._extract_entities("2026-08-07训练深度学习模型时显卡显存不够，预算要加")
        for kw in ("显卡", "显存", "深度学习", "模型", "训练", "预算"):
            self.assertIn(kw, ents)
        # 整句碎片（如"训练深度学习模型"）不应作为整体实体出现
        self.assertNotIn("训练深度学习模型", ents)

    def test_zh_short_common_sentence_no_entities(self):
        # 常见生活句（无技术词）→ 无实体，tags 不被污染
        self.assertEqual(self.db._extract_entities("今天天气不错适合散步"), [])

    def test_en_high_freq_words_filtered(self):
        # 反馈样例：am/buying/considering/get/used/card 虚词不再捞上来
        ents = self.db._extract_entities(
            "I am considering buying an NVIDIA RTX 4090 with 24GB VRAM "
            "for YOLO training, using my old card to get started"
        )
        low = {e.lower() for e in ents}
        for bad in ("am", "buying", "considering", "get", "used", "card",
                    "using", "with", "for", "my", "to", "an"):
            self.assertNotIn(bad, low)
        # 技术词仍在
        for good in ("NVIDIA", "VRAM", "YOLO"):
            self.assertIn(good, ents)

    def test_zh_entities_flow_into_memory_entities_column(self):
        self.db.set("ent/test", "显卡价格是550块，显存12G", category="env", embed=False)
        row = self.db.get("ent/test")
        assert row is not None
        ents = json.loads(row["entities"]) if row["entities"] else []
        self.assertIn("显卡", ents)
        self.assertIn("显存", ents)
        # 碎片不再污染
        self.assertFalse(any(len(e) > 6 for e in ents))

    def test_zh_proper_nouns_merged(self):
        # jieba 分词: 小(a)+鸡翅(n) → 小鸡翅；银渐(nr)+层(q) → 银渐层
        ents = self.db._extract_entities("我的猫叫小鸡翅，是只银渐层")
        self.assertIn("小鸡翅", ents)
        self.assertIn("银渐层", ents)
        # 整句/长片段不应出现
        self.assertNotIn("我的猫叫小鸡翅是只银渐层", ents)

    def test_zh_server_config_entities(self):
        # 中英混合硬件配置句: 技术名词 + 英文型号都要抽出来
        ents = self.db._extract_entities(
            "服务器配置是华南X99-F8D双路主板加两个E5-2640v4处理器32G内存"
        )
        for kw in ("服务器", "主板", "处理器", "内存"):
            self.assertIn(kw, ents)
        for model in ("X99", "E5-2640v4"):
            self.assertIn(model, ents)


class TestEpisodicAutoIndex(unittest.TestCase):
    """#2 learn(source_session=...) 自动接 episodic 索引。"""

    def setUp(self):
        self.tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.tmp)
        self.db._embed = lambda text: None  # 离线：走实体去重/直写路径

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.tmp)
        except Exception:
            pass

    def test_learn_with_session_creates_episode(self):
        self.db.learn("显卡价格是550块，显存12G", source="task", source_session="sess-1")
        ep = self.db.episodic_get("sess-1")
        self.assertIsNotNone(ep, "learn(source_session=...) 后 mk_episodic list 应能看到该会话")
        self.assertEqual(ep["session_id_ref"], "sess-1")
        self.assertEqual(ep["source"], "learn")
        self.assertIn("显卡价格是550块", ep["summary"])

    def test_learn_without_session_no_episode(self):
        self.db.learn("服务器GPU驱动安装配置记录", source="task")
        self.assertEqual(self.db.episodic_list(10), [])

    def test_episodic_idempotent_same_session(self):
        # 同一 session 多次 learn → 不重复建行，summary 更新为最新
        self.db.learn("显卡价格是550块，显存12G", source="task", source_session="sess-1")
        self.db.learn("显卡显存不足，需要加预算买新卡", source="task", source_session="sess-1")
        eps = self.db.episodic_list(10)
        self.assertEqual(len(eps), 1)
        self.assertIn("预算买新卡", eps[0]["summary"])

    def test_episode_searchable(self):
        self.db.learn("显卡价格是550块，显存12G", source="task", source_session="sess-1")
        hits = self.db.episodic_search("显卡价格")
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0]["session_id_ref"], "sess-1")


class TestLearnAutoVerify(unittest.TestCase):
    """#3 learn 侧自动触发验证闭环（保守策略）。"""

    def setUp(self):
        self.mem_tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.mem_tmp)
        self.db._embed = lambda text: None  # 离线

    def tearDown(self):
        for p in (getattr(self, "mem_tmp", None), getattr(self, "state_tmp", None)):
            try:
                if p:
                    os.remove(p)
            except OSError:
                pass
        try:
            self.db.close()
        except Exception:
            pass

    def test_learn_return_has_verify_status_all_paths(self):
        # 新建路径
        r1 = self.db.learn("显卡价格是550块，显存12G", source="task")
        self.assertIn("verify", r1)
        # 向量/实体合并路径（同内容再学一次 → merged=True）
        r2 = self.db.learn("显卡价格是550块，显存12G", source="task")
        self.assertTrue(r2["merged"])
        self.assertIn("verify", r2)

    def test_high_importance_with_state_db_leaves_verify_trace(self):
        # "必须/注意" 等 high_imp 关键词把 importance 抬到 >0.6 → 触发自动验证
        content = "必须注意显卡价格是550块，显存12G，不要超预算"
        self.state_tmp = make_state_db("sess-1", [content])
        self.db.state_db_path = self.state_tmp
        r = self.db.learn(content, source="task", source_session="sess-1")
        self.assertEqual(r["verify"], "verified")
        row = self.db.get(r["key"])
        assert row is not None
        self.assertGreater(row["importance"], 0.6)
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        # verify 痕迹：vscore/method/sids/auto 都写进 provenance
        self.assertEqual(prov.get("v"), "verified")
        self.assertGreaterEqual(prov.get("vscore", 0), 0.5)
        self.assertIn("auto", prov)
        self.assertTrue(prov["auto"])
        self.assertIn("method", prov)

    def test_high_importance_verified_bumps_confidence(self):
        content = "必须注意显卡价格是550块，显存12G，不要超预算"
        self.state_tmp = make_state_db("sess-1", [content])
        self.db.state_db_path = self.state_tmp
        r = self.db.learn(content, source="task", source_session="sess-1")
        row = self.db.get(r["key"])
        assert row is not None
        # 出厂值 0.55（0.45 + 有源 0.10）；验证通过后应被提升
        self.assertGreater(row["confidence"], 0.55)

    def test_no_state_db_stays_pending(self):
        # 未配置 state.db → 不写验证痕迹、不提升、保持 pending
        self.db.state_db_path = None
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session="sess-1")
        self.assertEqual(r["verify"], "pending")
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertNotIn("auto", prov)

    def test_low_importance_no_auto_verify(self):
        # importance < 0.6（普通生活句）→ 不自动验证
        self.state_tmp = make_state_db("sess-1", ["今天天气不错适合散步"])
        self.db.state_db_path = self.state_tmp
        r = self.db.learn("今天天气不错适合散步", source="task",
                          source_session="sess-1")
        self.assertEqual(r["verify"], "pending")
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertNotIn("auto", prov)

    def test_auto_verify_opt_out(self):
        content = "必须注意显卡价格是550块，显存12G，不要超预算"
        self.state_tmp = make_state_db("sess-1", [content])
        self.db.state_db_path = self.state_tmp
        r = self.db.learn(content, source="task",
                          source_session="sess-1", auto_verify=False)
        self.assertEqual(r["verify"], "pending")


class TestLearnLightweightVerify(unittest.TestCase):
    """#4 验证闭环真正打分：state.db 缺失/无源 session → 库内轻量验证兜底。

    OpenClaw 场景：bridge.py 调 MemoryDB 时 state_db_path=None（小龙虾库没有
    state.db），旧逻辑 _maybe_auto_verify 直接 return pending、vscore 恒 0。
    新逻辑走 _lightweight_verify：相似记忆一致性（cos>0.85 → 0.3-0.7）/
    时间锚定（0.30）/实体密度（0.25）给非零 vscore + method，provenance 记
    fallback=true；无证据仍保持 pending 不写痕迹、不虚高。
    """

    def setUp(self):
        self.mem_tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.mem_tmp, state_db_path=None)
        self.db._embed = lambda text: None  # 离线

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.mem_tmp)
        except OSError:
            pass

    def test_learn_high_importance_no_state_db_nonzero_vscore(self):
        # OpenClaw 验收场景：高 importance + 有源 + 无 state.db → vscore 非 0
        content = "必须注意显卡价格是550块显存12G不要超预算"
        r = self.db.learn(content, source="task", source_session="sess-1")
        row = self.db.get(r["key"])
        assert row is not None
        self.assertGreater(row["importance"], 0.6)
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertGreater(prov.get("vscore", 0), 0, "vscore 必须非零（不再恒 0）")
        self.assertIn("method", prov)
        self.assertTrue(prov["method"])  # method 有记录
        self.assertTrue(prov.get("auto"))
        self.assertTrue(prov.get("fallback"), "降级路径应标记 fallback")
        # 保守：实体密度基础分 0.25 < 0.5 → 仍 pending，不虚报 verified
        self.assertEqual(r["verify"], "pending")

    def test_learn_no_state_db_no_source_entity_density(self):
        # 无源 session 也触发轻量验证（任务要求：无源不能直接 pending 放弃）
        content = "必须注意显卡价格是550块显存12G不要超预算"
        r = self.db.learn(content, source="task")  # 无 source_session
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertGreater(prov.get("vscore", 0), 0)
        self.assertEqual(prov.get("method"), "entity_density")
        self.assertEqual(r["verify"], "pending")

    def test_learn_no_state_db_no_evidence_stays_clean(self):
        # 无实体/无时间/无相似记忆 → 无证据：保持原样 pending、不写痕迹
        r = self.db.learn("必须注意服务器的事情很重要", source="task",
                          source_session="sess-2")
        self.assertEqual(r["verify"], "pending")
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertNotIn("auto", prov)
        self.assertEqual(prov.get("vscore", 0), 0)

    def test_low_importance_no_state_db_stays_pending(self):
        # importance=0.6（learn 基数，未抬升）→ 不触发任何验证路径
        self.db.state_db_path = None
        r = self.db.learn("显卡价格是550块，显存12G", source="task",
                          source_session="sess-1")
        self.assertEqual(r["verify"], "pending")
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertNotIn("auto", prov)

    def test_lightweight_verify_entity_density(self):
        lw = self.db._lightweight_verify("必须注意显卡价格是550块显存12G不要超预算",
                                         exclude_key="nonexistent")
        self.assertEqual(lw["score"], 0.25)
        self.assertEqual(lw["method"], "entity_density")
        self.assertEqual(lw["evidence"]["entity_density"]["entity_count"], 4)

    def test_lightweight_verify_time_anchor(self):
        lw = self.db._lightweight_verify("2026-08-07服务器宕机修复记录",
                                         exclude_key="nonexistent")
        self.assertEqual(lw["score"], 0.30)
        self.assertIn("time_anchor", lw["method"])
        self.assertEqual(lw["evidence"]["time_anchor"]["date"], "2026-08-07")

    def test_lightweight_verify_no_evidence(self):
        lw = self.db._lightweight_verify("今天天气不错适合散步", exclude_key="k")
        self.assertEqual(lw["score"], 0.0)
        self.assertEqual(lw["method"], "none")
        self.assertEqual(lw["evidence"], {})

    def test_verify_manual_no_state_db_no_spurious_degrade(self):
        # verify() 手动路径：有源 + 无 state.db → 源不可达 ≠ 矛盾，
        # 不降 importance，用库内轻量分给非零 vscore
        self.db.set("env/budget", "必须注意显卡价格是550块显存12G不要超预算",
                    category="env", embed=False, importance=0.8,
                    source_session="sess-1")
        res = self.db.verify("env/budget", auto_degrade=True)
        self.assertFalse(res["downgraded"], "源不可达不应降 importance")
        self.assertEqual(res["new_importance"], 0.8)
        self.assertGreater(res["score"], 0)
        row = self.db.get("env/budget")
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertGreater(prov.get("vscore", 0), 0)
        self.assertTrue(prov.get("fallback"))


class TestLearnFallbackSimilarityBoost(unittest.TestCase):
    """#4 confidence 联动：降级路径高分（>=0.5，相似记忆证据 >=3）→
    v=verified + confidence +0.05（与 verify() 高分 boost 对齐）。"""

    @staticmethod
    def _fake_embed(text):
        # 确定性伪向量：关键词哈希，相似文本 → 相似向量（cos 可复现）
        import hashlib
        words = ["显卡", "显存", "价格", "预算", "训练", "模型"]
        vec = [0.0] * 16
        for w in words:
            if w in text:
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                vec[h % 16] += 1.0
        if not any(vec):
            vec[0] = 1.0
        n = sum(x * x for x in vec) ** 0.5
        return [x / n for x in vec]

    def setUp(self):
        self.mem_tmp = make_tmp_db()
        self.db = MemoryDB(db_path=self.mem_tmp, state_db_path=None)
        self.db._embed = self._fake_embed

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.remove(self.mem_tmp)
        except OSError:
            pass

    def test_learn_fallback_similarity_verified_boosts_confidence(self):
        # 造 3 条与目标相似（cos>0.85）的记忆 → 轻量验证证据数=3 → vscore 0.6
        for k, c in (("env/a", "显卡价格是550块显存12G"),
                     ("env/b", "显卡显存价格预算550块"),
                     ("env/d", "显存显卡预算价格很高"),
                     ("env/e", "显卡显存价格预算配置")):
            self.db.set(k, c, category="env", importance=0.7,
                        source_session="sess-x", embed=True)
        _a = self.db.get("env/a")
        assert _a is not None
        old_conf = _a["confidence"]

        # learn 高 importance 同主题内容 → 向量合并进 env/a（fusion>0.85），
        # 随后 _maybe_auto_verify 走降级路径：3 条相似证据 → verified + boost
        r = self.db.learn("必须注意显卡价格是550块显存12G不要超预算",
                          source="task", source_session="sess-1")
        self.assertTrue(r["merged"])
        self.assertEqual(r["verify"], "verified")
        row = self.db.get(r["key"])
        assert row is not None
        prov = json.loads(row["provenance"]) if row["provenance"] else {}
        self.assertEqual(prov.get("v"), "verified")
        self.assertGreaterEqual(prov.get("vscore", 0), 0.5)
        self.assertIn("similarity", prov.get("method", ""))
        self.assertTrue(prov.get("fallback"))
        # confidence 联动：+0.05（与 verify() 高分 boost 对齐）
        self.assertAlmostEqual(row["confidence"], round(old_conf + 0.05, 2),
                               places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
