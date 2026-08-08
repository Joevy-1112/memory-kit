#!/usr/bin/env python3
"""memorykit.embedder — 嵌入向量接口（可注入）。

默认实现 HTTPEmbedder：调用本地 OpenAI 兼容的 /v1/embeddings 服务
（llama.cpp 的 llama-server 或任何 OpenAI 兼容嵌入端点）。

环境变量：
  MEMORY_EMBED_URL  嵌入服务地址，默认 http://127.0.0.1:8082

用法：
  from memorykit.embedder import HTTPEmbedder
  e = HTTPEmbedder()                      # 默认 127.0.0.1:8082
  vec = e.embed("显卡价格")                # list[float] 或 None
  ok = e.is_available()

  # 注入自定义 embedder（任何实现 embed()/is_available() 的对象均可）:
  db = MemoryDB(db_path=..., embedder=MyEmbedder())
"""
import os
import re
import json
import math
import hashlib
import urllib.request

DEFAULT_EMBED_URL = os.environ.get("MEMORY_EMBED_URL", "http://127.0.0.1:8082")


class Embedder:
    """嵌入器接口：子类实现 embed() 与 is_available()。"""

    def embed(self, text: str):
        """文本 → 向量 (list[float])。失败返回 None（调用方降级）。"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """嵌入服务是否可用。"""
        raise NotImplementedError


class HTTPEmbedder(Embedder):
    """OpenAI 兼容 HTTP 嵌入器（默认 127.0.0.1:8082，bge-m3）。

    hybrid 检索支持：bge-m3 原版三路服务（如 8083）在 /v1/embeddings 响应里
    带 `sparse_weights` 字段（{token_id_str: weight}）。embed_with_sparse /
    embed_batch_with_sparse 解析该字段；老服务（llama.cpp GGUF 8082）不返回
    → sparse=None，调用方降级纯 dense。embed()/embed_batch() 保持纯 dense
    契约不变（兼容既有调用方与注入式 embedder）。
    """

    def __init__(self, url: str = None, timeout: float = 5):
        self.url = (url or DEFAULT_EMBED_URL).rstrip("/")
        self.timeout = timeout

    def embed(self, text: str):
        """POST /v1/embeddings {"input": text, "model": "default"} → vector。"""
        dense, _ = self.embed_with_sparse(text)
        return dense

    def embed_with_sparse(self, text: str):
        """嵌入并解析 sparse_weights → (dense_vec, sparse_dict)。

        dense_vec:  list[float] 或 None（失败/过短）
        sparse_dict: {token_id_str: weight} 或 None
          —— 服务端返回 sparse_weights 字段时解析；不返回（如 llama.cpp
          GGUF 8082）→ None，调用方降级纯 dense。空 dict {} 视为 None
          （无词法信号，跳过 lexical 与不解析等价）。
        """
        if not text or len(text.strip()) < 3:
            return None, None
        try:
            data = json.dumps({"input": text, "model": "default"}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.url}/v1/embeddings",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            result = json.loads(resp.read())
            item = result["data"][0]
            vec = item.get("embedding")
            dense = vec if isinstance(vec, list) else None
            sw = item.get("sparse_weights")
            sparse = None
            if isinstance(sw, dict) and sw:
                sparse = {str(k): float(v) for k, v in sw.items()}
            return dense, sparse
        except Exception:
            return None, None

    def embed_batch(self, texts: list) -> list:
        """批量嵌入：一次 POST /v1/embeddings {"input": texts} 返回全部向量。

        返回按输入顺序对齐的列表（与 texts 等长）；单条失败/过短（len(strip)<3）
        对应位置为 None。服务端按 OpenAI 兼容协议返回 data[].index 时按 index 回填；
        不返回 index 时按返回顺序回填（默认与提交顺序一致）。整体请求失败（服务
        不可达/超时）→ 全部 None（与 embed() 失败返回 None 的降级语义一致）。

        批量超时按条数放宽（大 batch 服务端推理耗时随条数增长），下限 30s。
        """
        pairs = self.embed_batch_with_sparse(texts)
        return [p[0] for p in pairs]

    def embed_batch_with_sparse(self, texts: list) -> list:
        """批量嵌入 + sparse → 与 texts 等长、按输入顺序对齐的 [(dense, sparse)]。

        dense/sparse 语义与 embed_with_sparse 一致（服务端无 sparse_weights →
        sparse=None；单条失败/过短 → (None, None)；整体请求失败 → 全 (None, None)）。
        """
        if not texts:
            return []
        # 过短条目与 embed() 同规则过滤：不发给服务端，位置占 (None, None)
        valid_idx = [i for i, t in enumerate(texts) if t and len(str(t).strip()) >= 3]
        clean = [texts[i] for i in valid_idx]
        out: list = [(None, None)] * len(texts)
        if not clean:
            return out
        try:
            data = json.dumps({"input": clean, "model": "default"}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.url}/v1/embeddings",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=max(self.timeout, 30))
            result = json.loads(resp.read())
            data_list = result.get("data") or []
            by_index = {}
            has_index = False
            for item in data_list:
                if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                    if item.get("index") is not None:
                        has_index = True
                        by_index[int(item["index"])] = item
            for j, idx in enumerate(valid_idx):
                if has_index:
                    item = by_index.get(j)
                elif j < len(data_list) and isinstance(data_list[j], dict):
                    item = data_list[j]
                else:
                    item = None
                if item is None:
                    out[idx] = (None, None)
                    continue
                emb = item.get("embedding")
                dense = emb if isinstance(emb, list) else None
                sw = item.get("sparse_weights")
                sparse = None
                if isinstance(sw, dict) and sw:
                    sparse = {str(k): float(v) for k, v in sw.items()}
                out[idx] = (dense, sparse)
            return out
        except Exception:
            return [(None, None)] * len(texts)

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.url}/v1/models")
            urllib.request.urlopen(req, timeout=self.timeout)
            return True
        except Exception:
            return False


def embed_text(text: str, url: str = None) -> list:
    """便捷函数：单次嵌入调用，返回向量或 None。"""
    return HTTPEmbedder(url=url).embed(text)


class LocalHashEmbedder(Embedder):
    """确定性本地哈希嵌入器（可用性兜底，**不是真相源**）。

    契约对齐 scope-recall 的 LocalHashEmbedder 语义（embedding 挂了 fail-open，
    degraded fallback 而不是语义通道归零），实现是 memorykit 自家：

    - 特征：token（按 [\\w\\u4e00-\\u9fff]+ 切分，权重 1.0）+ 字符 3-gram（权重 0.35）
    - 空文本：占位串 "∅empty∅"（保持非零向量；scope-recall 用 raw 前 64 字符，
      空输入产生空向量会破坏去重/检索的一致性）
    - 哈希：sha1(feature) 前 4 字节 % dims 定桶，digest[4] 奇偶定符号，
      按权重累加后 L2 归一化
    - 确定性：同一文本永远同一向量；无网络、无模型、无第三方依赖
    - 语义质量弱（词袋+字符 n-gram 的 hash 桶）但**可检索**——比"直接没有
      语义通道"强一个档次。向量是放大器不是真相源，local-hash 绝不能反向
      覆盖已 verified 的记忆（检索降权/排序仍由 verify/freshness 管）
    """

    def __init__(self, dims: int = 256):
        self.dims = max(64, int(dims))
        self._tok_re = re.compile(r"[\w\u4e00-\u9fff]+")

    def _features(self, text: str):
        text = (text or "").strip().lower()
        if not text:
            text = "∅empty∅"
        feats: dict = {}
        for tok in self._tok_re.findall(text):
            feats[tok] = feats.get(tok, 0.0) + 1.0
        chars = re.sub(r"\s+", "", text)
        for i in range(len(chars) - 2):
            g = chars[i:i + 3]
            feats[g] = feats.get(g, 0.0) + 0.35
        return feats

    def embed(self, text: str):
        vec = [0.0] * self.dims
        for feat, w in self._features(text).items():
            h = hashlib.sha1(feat.encode("utf-8")).digest()
            bucket = int.from_bytes(h[:4], "little") % self.dims
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[bucket] += sign * w
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def is_available(self) -> bool:
        return True  # 本地确定性，永远可用
