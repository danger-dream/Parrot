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
        # 是否已见到上游流的"收尾事件"。Anthropic: message_stop。见后判定
        # 即使 client 之后断开，服务端视角也已拿到完整响应，日志应归 success。
        self.saw_stream_end = False

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
                # 智谱等上游在 message_delta 补发 input_tokens（message_start 为 0）
                if "input_tokens" in u:
                    self.usage["input_tokens"] = int(u.get("input_tokens", 0) or 0)
                if "cache_read_input_tokens" in u:
                    v = int(u.get("cache_read_input_tokens", 0) or 0)
                    if v > self.usage["cache_read"]:
                        self.usage["cache_read"] = v
            elif t == "message_stop":
                self.saw_stream_end = True

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


# ══════════════════════════════════════════════════════════════════════
# OpenAI 家族的 SSE 工具
#
# 注意：与 Anthropic 版并列存在，不复用 / 不覆盖任一 anthropic 函数或类。
# usage 字段以 anthropic 的 4 键为准（input_tokens / output_tokens /
# cache_creation / cache_read），保证 log_db 落库无感切换。OpenAI 不区分
# cache_creation，一律置 0；cache_read 来自 cached_tokens 细节字段。
# ══════════════════════════════════════════════════════════════════════


def _iter_sse_data_lines(buf: bytes):
    """把字节流中的完整行切出来，返回 (剩余 buf, data 行列表)。

    OpenAI Chat 与 Responses 都用 `\\n\\n` 分隔 event，但同一 event 内部可能有
    多行（`event:`、`id:`、`data:`、`:`）。此函数只解析 `data:` 行，别的行
    调用方自己解析。返回的字符串已去掉 `data:` 前缀和首尾空白。
    """
    lines: list[str] = []
    while b"\n" in buf:
        line_bytes, buf = buf.split(b"\n", 1)
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line.startswith("data:"):
            lines.append(line[5:].strip())
    return buf, lines


def _iter_sse_events(buf: bytes):
    """按 `\\n\\n` 边界把 buf 切成若干完整 event 块。

    返回 (剩余 buf, [event_block_text,...])。event_block_text 原封保留行内容
    （用 \\n 拼接），便于各家族的 builder 自行拿 `event:` / `data:` 等。
    """
    events: list[str] = []
    while True:
        # SSE 规范上分隔符是 `\n\n`（也有 `\r\n\r\n`，httpx 已经归一到 \n）
        sep = b"\n\n"
        if sep not in buf:
            break
        block, buf = buf.split(sep, 1)
        events.append(block.decode("utf-8", errors="replace"))
    return buf, events


def _parse_event_block(block: str) -> tuple[Optional[str], Optional[dict]]:
    """把一个 SSE event 块解析成 (event_name, data_obj)。

    event_name 可为 None（Chat 流里只有 data: 无 event:）。
    data_obj 解析失败或是 "[DONE]" 返回 None（调用方按 event_name 判断）。
    """
    event_name: Optional[str] = None
    data_str: Optional[str] = None
    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("event:"):
            event_name = line[6:].strip() or None
        elif line.startswith("data:"):
            data_str = line[5:].strip()
    if data_str is None or data_str == "[DONE]":
        return event_name, None
    try:
        return event_name, json.loads(data_str)
    except Exception:
        return event_name, None


# ─── OpenAI Chat SSE 工具 ─────────────────────────────────────────────


def extract_usage_chat_json(obj: Any) -> dict:
    """从 /v1/chat/completions 非流式响应里抽 usage，归一到 4 键。"""
    if not isinstance(obj, dict):
        return _zero_usage()
    u = obj.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": int(u.get("prompt_tokens", 0) or 0),
        "output_tokens": int(u.get("completion_tokens", 0) or 0),
        "cache_creation": 0,
        "cache_read": int(details.get("cached_tokens", 0) or 0),
    }


def parse_first_chat_sse_event(chunk: bytes) -> Optional[dict]:
    """Chat SSE 首帧解析。

    - 正常首帧：`data: {"id":..., "object":"chat.completion.chunk", ...}` → 返回 dict
    - 错误首帧：OpenAI 部分上游在首包就发 `data: {"error":{...}}`，直接
      返回 `{"error": {...}}` 让 failover 按 upstream_error_json 处理
    - 解析失败或仅 `[DONE]` → None
    """
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


class ChatSSEUsageTracker:
    """从 /v1/chat/completions SSE 中抽取 usage + 保留全量文本。

    Chat 流的 usage 只出现在末尾一帧（stream_options.include_usage=true 时）；
    若上游没开 include_usage，tracker 返回全 0。
    """

    def __init__(self):
        self.usage = _zero_usage()
        self._chunks: list[bytes] = []
        self._buf = b""
        # Chat 流的收尾标记：[DONE] 或任一 choice 带 finish_reason。
        # 两者都足以说明上游完成了本次生成；若 client 之后断开日志归 success。
        self.saw_stream_end = False

    def feed(self, chunk_bytes: bytes) -> None:
        if not chunk_bytes:
            return
        self._chunks.append(chunk_bytes)
        self._buf += chunk_bytes
        self._buf, lines = _iter_sse_data_lines(self._buf)
        for data in lines:
            if not data:
                continue
            if data == "[DONE]":
                self.saw_stream_end = True
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            if isinstance(evt, dict):
                choices = evt.get("choices")
                if isinstance(choices, list):
                    for ch in choices:
                        if isinstance(ch, dict) and ch.get("finish_reason"):
                            self.saw_stream_end = True
                            break
                u = evt.get("usage")
                if isinstance(u, dict):
                    details = u.get("prompt_tokens_details") or {}
                    self.usage["input_tokens"] = int(u.get("prompt_tokens", 0) or 0)
                    self.usage["output_tokens"] = int(u.get("completion_tokens", 0) or 0)
                    self.usage["cache_read"] = int(details.get("cached_tokens", 0) or 0)
                    # cache_creation 在 OpenAI 里没有对应概念，保持 0

    def get_full_response(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class ChatSSEAssistantBuilder:
    """累积 Chat SSE 的 delta 还原 assistant message。

    输出结构（喂给 fingerprint.fingerprint_write_chat 等）：
      {"role":"assistant","content":"...","tool_calls":[...], "refusal":...}
    """

    def __init__(self):
        self._buf = b""
        self._role = "assistant"
        self._content_parts: list[str] = []
        self._refusal_parts: list[str] = []
        # tool_calls 按 index 聚合，保留首次的 id/name，arguments 拼接
        self._tool_calls: dict[int, dict] = {}
        self._finish_reason: Optional[str] = None
        self._got_any = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        self._buf, lines = _iter_sse_data_lines(self._buf)
        for data in lines:
            if not data or data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            self._apply(evt)

    def _apply(self, evt: dict) -> None:
        choices = evt.get("choices") or []
        if not choices:
            return
        self._got_any = True
        ch0 = choices[0]
        delta = ch0.get("delta") or {}
        if delta.get("role"):
            self._role = delta["role"]
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._content_parts.append(content)
        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            self._refusal_parts.append(refusal)
        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index", 0))
            slot = self._tool_calls.setdefault(idx, {
                "id": None, "type": "function",
                "function": {"name": None, "arguments": ""},
            })
            if tc.get("id") and not slot["id"]:
                slot["id"] = tc["id"]
            if tc.get("type"):
                slot["type"] = tc["type"]
            fn = tc.get("function") or {}
            if fn.get("name") and not slot["function"]["name"]:
                slot["function"]["name"] = fn["name"]
            args_piece = fn.get("arguments")
            if isinstance(args_piece, str) and args_piece:
                slot["function"]["arguments"] += args_piece
        if ch0.get("finish_reason"):
            self._finish_reason = ch0["finish_reason"]

    def get_assistant(self) -> dict:
        msg: dict = {"role": self._role}
        msg["content"] = "".join(self._content_parts) or None
        if self._refusal_parts:
            msg["refusal"] = "".join(self._refusal_parts)
        if self._tool_calls:
            msg["tool_calls"] = [self._tool_calls[i] for i in sorted(self._tool_calls.keys())]
        return msg

    @property
    def has_any_event(self) -> bool:
        return self._got_any

    @property
    def finish_reason(self) -> Optional[str]:
        return self._finish_reason

    def to_full_json(self, *, id: str, model: str, created: int,
                     system_fingerprint: Optional[str] = None,
                     usage: Optional[dict] = None) -> dict:
        """把累积的 SSE 聚合成完整的 chat.completion 响应 JSON（非流式格式）。"""
        out: dict = {
            "id": id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": self.get_assistant(),
                "finish_reason": self._finish_reason or "stop",
                "logprobs": None,
            }],
        }
        if system_fingerprint:
            out["system_fingerprint"] = system_fingerprint
        if usage:
            out["usage"] = usage
        return out


# ─── OpenAI Responses SSE 工具 ────────────────────────────────────────


def extract_usage_responses_json(obj: Any) -> dict:
    """从 /v1/responses 非流式响应里抽 usage。"""
    if not isinstance(obj, dict):
        return _zero_usage()
    u = obj.get("usage") or {}
    in_details = u.get("input_tokens_details") or {}
    return {
        "input_tokens": int(u.get("input_tokens", 0) or 0),
        "output_tokens": int(u.get("output_tokens", 0) or 0),
        "cache_creation": 0,
        "cache_read": int(in_details.get("cached_tokens", 0) or 0),
    }


def parse_first_responses_sse_event(chunk: bytes) -> Optional[dict]:
    """Responses SSE 首个 event 解析。

    返回一个带 `_event_name` 的 dict 以便 failover 区分：
    - 正常首帧：`event: response.created\\ndata: {response:{...}}` → 返回
      `{"_event_name": "response.created", **data}`
    - 错误首帧：`event: error\\ndata: {...}` → 返回
      `{"_event_name": "error", "error": {...}}` （兼容 failover 的 error 识别）

    解析失败返回 None。
    """
    if not chunk:
        return None
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None
    # 只看第一个以 `\n\n` 结束的 event block（chunk 可能没含完整 event，尽力而为）
    blocks = text.split("\n\n")
    for block in blocks:
        if not block.strip():
            continue
        event_name, data = _parse_event_block(block)
        if data is None and event_name is None:
            continue
        if event_name == "error":
            # data 可能形如 {"type":"error","message":"...","code":...}
            err_body = data if isinstance(data, dict) else {"message": "unknown error"}
            return {"_event_name": "error", "error": err_body}
        if data is None:
            # 只有 event name 没 data；跳过继续找
            continue
        out = dict(data)
        out["_event_name"] = event_name or ""
        return out
    return None


class ResponsesSSEUsageTracker:
    """从 /v1/responses SSE 中抽取 usage，usage 出现在 `response.completed` / `.failed` / `.incomplete` 事件里。"""

    def __init__(self):
        self.usage = _zero_usage()
        self._chunks: list[bytes] = []
        self._buf = b""
        # Responses 流的收尾事件：completed / failed / incomplete 之一。
        # 收到即视为上游已完成本次生成，client 后续断开不影响日志归 success。
        self.saw_stream_end = False

    def feed(self, chunk_bytes: bytes) -> None:
        if not chunk_bytes:
            return
        self._chunks.append(chunk_bytes)
        self._buf += chunk_bytes
        self._buf, events = _iter_sse_events(self._buf)
        for block in events:
            event_name, data = _parse_event_block(block)
            if data is None:
                continue
            if event_name in ("response.completed", "response.failed", "response.incomplete"):
                self.saw_stream_end = True
                resp = data.get("response") if isinstance(data, dict) else None
                if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
                    u = resp["usage"]
                    in_details = u.get("input_tokens_details") or {}
                    self.usage["input_tokens"] = int(u.get("input_tokens", 0) or 0)
                    self.usage["output_tokens"] = int(u.get("output_tokens", 0) or 0)
                    self.usage["cache_read"] = int(in_details.get("cached_tokens", 0) or 0)

    def get_full_response(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class ResponsesSSEAssistantBuilder:
    """从 /v1/responses SSE 中还原 output_items，供 fingerprint_write_responses 使用。

    聚合规则（简化版；完整翻译器在 MS-3/MS-4 做）：
      - `response.output_item.added / done`：把 item 按 output_index 记录
      - `response.output_text.delta`：按 (output_index, content_index) 拼 text
      - `response.function_call_arguments.delta`：按 output_index 拼 arguments
    """

    def __init__(self):
        self._buf = b""
        self._items: dict[int, dict] = {}             # output_index → item
        self._fc_args: dict[int, str] = {}            # output_index → arguments buf
        self._msg_text: dict[tuple, str] = {}         # (output_index, content_index) → text
        self._got_any = False
        # response 顶层 metadata（由 response.created / response.completed 事件携带）
        self._response_obj: Optional[dict] = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        self._buf, events = _iter_sse_events(self._buf)
        for block in events:
            event_name, data = _parse_event_block(block)
            if event_name is None or data is None:
                continue
            self._got_any = True
            # response.created / response.completed 里的 response 对象是顶层 metadata 来源
            if event_name in ("response.created", "response.in_progress",
                              "response.completed", "response.failed"):
                resp = data.get("response")
                if isinstance(resp, dict):
                    self._response_obj = resp
            if event_name == "response.output_item.added":
                idx = int(data.get("output_index", 0))
                item = dict(data.get("item") or {})
                self._items[idx] = item
            elif event_name == "response.output_item.done":
                idx = int(data.get("output_index", 0))
                item = dict(data.get("item") or {})
                self._items[idx] = item
            elif event_name == "response.output_text.delta":
                key = (int(data.get("output_index", 0)), int(data.get("content_index", 0)))
                self._msg_text[key] = self._msg_text.get(key, "") + (data.get("delta") or "")
            elif event_name == "response.function_call_arguments.delta":
                idx = int(data.get("output_index", 0))
                self._fc_args[idx] = self._fc_args.get(idx, "") + (data.get("delta") or "")

    def get_output_items(self) -> list[dict]:
        """按 output_index 顺序返回 items（用于 fingerprint_write_responses 等）。"""
        out: list[dict] = []
        for idx in sorted(self._items.keys()):
            item = dict(self._items[idx])
            t = item.get("type")
            if t == "message":
                # 合并 output_text deltas 到 content 中
                content = list(item.get("content") or [])
                merged = {}
                for (oi, ci), text in self._msg_text.items():
                    if oi != idx:
                        continue
                    merged.setdefault(ci, text)
                for ci in sorted(merged.keys()):
                    # 如果 done 事件已带完整 text 就保留原样；否则补回
                    if ci < len(content) and isinstance(content[ci], dict):
                        if not content[ci].get("text"):
                            content[ci]["text"] = merged[ci]
                    else:
                        content.append({"type": "output_text", "text": merged[ci], "annotations": []})
                item["content"] = content
            elif t == "function_call":
                args_buf = self._fc_args.get(idx)
                if args_buf and not item.get("arguments"):
                    item["arguments"] = args_buf
            out.append(item)
        return out

    def get_assistant(self) -> dict:
        """返回 OpenAI Responses 风格的 assistant 对象，供 fingerprint_write_responses 用。

        首版只包含 items 列表；MS-7 才真正接入 fingerprint，暂以结构占位。
        """
        return {"role": "assistant", "output": self.get_output_items()}

    @property
    def has_any_event(self) -> bool:
        return self._got_any

    def to_full_json(self, *, fallback_model: str = "") -> dict:
        """把累积的 SSE 聚合成完整的 /v1/responses 响应 JSON。

        优先使用 response.completed 事件里的 response 对象做骨架；
        若骨架缺失，用 fallback_model 兜底并现场组装 output。
        """
        base: dict = {}
        if isinstance(self._response_obj, dict):
            base = dict(self._response_obj)
        # 无论 base 有没有 output，都用 builder 内部聚合的 items 覆盖（更可靠）
        base["output"] = self.get_output_items()
        base.setdefault("object", "response")
        base.setdefault("status", "completed")
        if not base.get("model"):
            base["model"] = fallback_model
        return base
