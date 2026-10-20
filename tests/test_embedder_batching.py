#!/usr/bin/env python3
"""P4 事故回归：HTTPEmbedder.embed_batch 必须内部分批（≤16）+ 批间失败隔离。

事故：探针一次提交 1103 条给共享 llama.cpp 8082，服务端 decode 死锁
（`decode: cannot decode batches with this context`），半小时无响应。
根因是 embed_batch 把整个列表一次 POST。这里用桩服务断言：

  1. 100 条 → 7 批（16×6 + 4），没有任何一次请求超过 16 条
  2. 返回顺序与输入严格对齐（跨批边界也按批内 index 回填）
  3. 某一批抛异常 → 只有该批位置为 None，其余批正常
  4. 过滤过短条目后仍按批切；空列表 / 全过短不产生请求
  5. embed_batch_with_sparse 的 sparse 字段与 dense 一起按批对齐
"""
import json
import unittest
from unittest import mock

from memorykit.embedder import EMBED_BATCH_LIMIT, HTTPEmbedder


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    """记录每次 POST 的 input 长度与内容；可指定第 N 次请求抛异常。"""

    def __init__(self, fail_call=None, with_index=True, with_sparse=False):
        self.calls = []
        self.fail_call = fail_call
        self.with_index = with_index
        self.with_sparse = with_sparse

    def __call__(self, req, timeout=None):
        call_no = len(self.calls)
        texts = json.loads(req.data.decode("utf-8"))["input"]
        self.calls.append({"texts": list(texts), "timeout": timeout})
        if self.fail_call is not None and call_no == self.fail_call:
            raise OSError("simulated batch failure")
        data = []
        for i, _ in enumerate(texts):
            item = {"embedding": [float(i), float(i + 1)]}
            if self.with_index:
                item["index"] = i
            if self.with_sparse:
                item["sparse_weights"] = {str(i): 1.0}
            data.append(item)
        return _FakeResp({"data": data})


class TestEmbedBatchBatching(unittest.TestCase):
    def test_batch_limit_is_at_most_16(self):
        self.assertLessEqual(EMBED_BATCH_LIMIT, 16)
        self.assertEqual(HTTPEmbedder().batch_limit, EMBED_BATCH_LIMIT)

    def test_100_texts_split_into_7_batches(self):
        rec = Recorder()
        texts = [f"text-{i:03d}" for i in range(100)]
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            out = e.embed_batch(texts)
        self.assertEqual(len(rec.calls), 7)
        self.assertEqual([len(c["texts"]) for c in rec.calls],
                         [16, 16, 16, 16, 16, 16, 4])
        self.assertTrue(all(len(c["texts"]) <= 16 for c in rec.calls))
        # 请求内容按输入顺序切片，不重不漏
        self.assertEqual([t for c in rec.calls for t in c["texts"]], texts)
        # 返回值与输入等长且按批内 index 回填（每批从 0 重新编号）
        self.assertEqual(len(out), 100)
        for i, vec in enumerate(out):
            self.assertEqual(vec, [float(i % 16), float(i % 16 + 1)])

    def test_one_batch_failure_isolated(self):
        rec = Recorder(fail_call=1)  # 第 2 批（index 16..31）挂
        texts = [f"text-{i:03d}" for i in range(40)]  # 16 + 16 + 8
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            out = e.embed_batch(texts)
        self.assertEqual([len(c["texts"]) for c in rec.calls], [16, 16, 8])
        self.assertTrue(all(v is not None for v in out[:16]))
        self.assertTrue(all(v is None for v in out[16:32]))
        self.assertTrue(all(v is not None for v in out[32:]))

    def test_no_index_positional_fallback_per_batch(self):
        rec = Recorder(with_index=False)
        texts = [f"text-{i:03d}" for i in range(20)]  # 16 + 4
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            out = e.embed_batch(texts)
        self.assertEqual([len(c["texts"]) for c in rec.calls], [16, 4])
        self.assertEqual(out[0], [0.0, 1.0])
        self.assertEqual(out[15], [15.0, 16.0])
        self.assertEqual(out[16], [0.0, 1.0])  # 新批从 0 开始
        self.assertEqual(out[19], [3.0, 4.0])

    def test_short_entries_filtered_before_batching(self):
        rec = Recorder()
        texts = ["x", ""] + [f"text-{i:03d}" for i in range(32)]
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            out = e.embed_batch(texts)
        self.assertEqual([len(c["texts"]) for c in rec.calls], [16, 16])
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertTrue(all(v is not None for v in out[2:]))

    def test_empty_and_all_short_make_no_request(self):
        rec = Recorder()
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            self.assertEqual(e.embed_batch([]), [])
            self.assertEqual(e.embed_batch(["", "x"]), [None, None])
        self.assertEqual(rec.calls, [])

    def test_all_batches_fail_still_all_none(self):
        rec = Recorder()
        texts = ["aaa", "bbb"]
        e = HTTPEmbedder(url="http://127.0.0.1:9")

        def boom(req, timeout=None):
            raise OSError("connection refused")

        with mock.patch("urllib.request.urlopen", side_effect=boom):
            self.assertEqual(e.embed_batch(texts), [None, None])

    def test_sparse_batch_alignment(self):
        rec = Recorder(with_sparse=True)
        texts = [f"text-{i:03d}" for i in range(18)]  # 16 + 2
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            pairs = e.embed_batch_with_sparse(texts)
        self.assertEqual(len(pairs), 18)
        self.assertEqual(pairs[0], ([0.0, 1.0], {"0": 1.0}))
        self.assertEqual(pairs[16], ([0.0, 1.0], {"0": 1.0}))
        self.assertEqual(pairs[17], ([1.0, 2.0], {"1": 1.0}))

    def test_batch_failure_keeps_sparse_none(self):
        rec = Recorder(fail_call=0, with_sparse=True)
        e = HTTPEmbedder(url="http://127.0.0.1:9")
        with mock.patch("urllib.request.urlopen", side_effect=rec):
            pairs = e.embed_batch_with_sparse(["text-000", "text-001"])
        self.assertEqual(pairs, [(None, None), (None, None)])


if __name__ == "__main__":
    unittest.main()
