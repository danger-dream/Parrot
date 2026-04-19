"""上游 httpx 客户端与 SSE 工具。

提供：
  - 全局共享的 `httpx.AsyncClient`（生命周期由 server.py 管理）
  - `SSEUsageTracker`：从 SSE 流实时抽取 usage（不存全量）
  - `SSEAssistantBuilder`：累积 content_block_* 事件还原完整 assistant 消息
  - `parse_first_sse_event`：解析首个 SSE event（用于首包安全检查）
  - `extract_usage_from_json`：非流式响应的 usage 抽取
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx


_client: Optional[httpx.AsyncClient] = None


def create_client() -> httpx.AsyncClient:
    """构造共享 AsyncClient。由 server.py lifespan 调用。"""
    global _client
    if _client is not None:
        return _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=330.0, write=30.0, pool=15.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        http2=False,
    )
    return _client


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("upstream.create_client() not called yet")
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def set_client(client: httpx.AsyncClient) -> None:
    """用于测试注入（例如 MockTransport 的 client）。"""
    global _client
    _client = client


# ─── Usage 抽取 ──────────────────────────────────────────────────

def extract_usage_from_json(obj: Any) -> dict:
    """非流式响应对象中的 usage 抽取为统一结构。"""
    if not isinstance(obj, dict):
        return _zero_usage()
    u = obj.get("usage") or {}
    return {
        "input_tokens": int(u.get("input_tokens", 0) or 0),
        "output_tokens": int(u.get("output_tokens", 0) or 0),
        "cache_creation": int(u.get("cache_creation_input_tokens", 0) or 0),
        "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
    }


def _zero_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}


# ─── SSE 解析 ────────────────────────────────────────────────────

def parse_first_sse_event(chunk: bytes) -> Optional[dict]:
    """从字节流中解析第一个 `data: {...}` JSON。解析不到返回 None。"""
    if not chunk:
        return None
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            return json.loads(data)
        except Exception:
            continue
    return None


class SSEUsageTracker:
    """从 SSE 流中实时抽取 usage，同时收集完整响应文本用于落库。

    使用行缓冲处理跨 chunk 的 JSON 事件。
    """

    def __init__(self):
        self.usage = _zero_usage()
        self._chunks: list[bytes] = []
        self._buf = b""

    def feed(self, chunk_bytes: bytes) -> None:
        if not chunk_bytes:
            return
        self._chunks.append(chunk_bytes)
        self._buf += chunk_bytes
        while b"\n" in self._buf:
            line_bytes, self._buf = self._buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            t = evt.get("type", "")
            if t == "message_start":
                u = (evt.get("message") or {}).get("usage") or {}
                self.usage["input_tokens"] = int(u.get("input_tokens", 0) or 0)
                self.usage["cache_creation"] = int(u.get("cache_creation_input_tokens", 0) or 0)
                self.usage["cache_read"] = int(u.get("cache_read_input_tokens", 0) or 0)
            elif t == "message_delta":
                u = evt.get("usage") or {}
                if "output_tokens" in u:
                    self.usage["output_tokens"] = int(u.get("output_tokens", 0) or 0)

    def get_full_response(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class SSEAssistantBuilder:
    """累积 content_block_* 事件还原完整 assistant 消息对象（供亲和指纹写入）。"""

    def __init__(self):
        self._buf = b""
        self._blocks: dict[int, dict] = {}      # index -> dict
        self._partial_jsons: dict[int, str] = {}  # index -> partial_json string
        self._role = "assistant"
        self._stop_reason: Optional[str] = None
        self._got_any = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line_bytes, self._buf = self._buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            self._apply_event(evt)

    def _apply_event(self, evt: dict) -> None:
        t = evt.get("type", "")
        if t == "message_start":
            self._got_any = True
            msg = evt.get("message") or {}
            self._role = msg.get("role", "assistant")
        elif t == "content_block_start":
            self._got_any = True
            idx = int(evt.get("index", 0))
            block = dict(evt.get("content_block") or {})
            self._blocks[idx] = block
        elif t == "content_block_delta":
            self._got_any = True
            idx = int(evt.get("index", 0))
            delta = evt.get("delta") or {}
            dt = delta.get("type", "")
            block = self._blocks.setdefault(idx, {})
            if dt == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif dt == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
            elif dt == "input_json_delta":
                self._partial_jsons[idx] = (self._partial_jsons.get(idx) or "") + (delta.get("partial_json") or "")
            elif dt == "signature_delta":
                block["signature"] = (block.get("signature") or "") + (delta.get("signature") or "")
        elif t == "content_block_stop":
            idx = int(evt.get("index", 0))
            # tool_use / server_tool_use / mcp_tool_use 等：把累积的 partial_json 解析为 input
            if idx in self._partial_jsons:
                block = self._blocks.get(idx) or {}
                raw = self._partial_jsons.pop(idx)
                try:
                    block["input"] = json.loads(raw) if raw else {}
                except Exception:
                    block["input"] = {"_raw": raw}
                self._blocks[idx] = block
        elif t == "message_delta":
            delta = evt.get("delta") or {}
            if "stop_reason" in delta:
                self._stop_reason = delta.get("stop_reason")

    def get_assistant(self) -> dict:
        """返回 `{"role": "assistant", "content": [...]}`，可用于亲和 fingerprint_write。"""
        blocks = [dict(self._blocks[i]) for i in sorted(self._blocks.keys())]
        # tool_use 的 input 字段应是 dict；若没 partial_json 则保留原本（可能为空 dict）
        for b in blocks:
            b.pop("_raw", None)
        return {"role": self._role, "content": blocks}

    @property
    def has_any_event(self) -> bool:
        return self._got_any
