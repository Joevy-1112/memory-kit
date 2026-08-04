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
import json
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
    """OpenAI 兼容 HTTP 嵌入器（默认 127.0.0.1:8082，bge-m3）。"""

    def __init__(self, url: str = None, timeout: float = 5):
        self.url = (url or DEFAULT_EMBED_URL).rstrip("/")
        self.timeout = timeout

    def embed(self, text: str):
        """POST /v1/embeddings {"input": text, "model": "default"} → vector。"""
        if not text or len(text.strip()) < 3:
            return None
        try:
            data = json.dumps({"input": text, "model": "default"}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.url}/v1/embeddings",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            result = json.loads(resp.read())
            vec = result["data"][0]["embedding"]
            return vec if isinstance(vec, list) else None
        except Exception:
            return None

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
