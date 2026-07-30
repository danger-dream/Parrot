"""上游 httpx 客户端与 SSE 工具。

提供：
  - 全局共享的 `httpx.AsyncClient`（生命周期由 server.py 管理）
  - `SSEUsageTracker`：从 SSE 流实时抽取 usage（不存全量）
  - `SSEAssistantBuilder`：累积 content_block_* 事件还原完整 assistant 消息
  - `parse_first_sse_event`：解析首个 SSE event（用于首包安全检查）
  - `extract_usage_from_json`：非流式响应的 usage 抽取
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

import httpx

from . import network
from .protocols import errors as protocol_errors
from .protocols.sse import split_sse_events as _split_sse_events_bytes
from .protocols.usage import (
    UsageAccumulator,
    legacy_usage_from_anthropic_json,
    legacy_usage_from_openai_chat_json,
    legacy_usage_from_openai_responses_json,
    zero_legacy_usage,
)


_client: Optional[httpx.AsyncClient] = None


def create_client() -> httpx.AsyncClient:
    """构造共享 AsyncClient。由 server.py lifespan 调用。"""
    global _client
    if _client is not None:
        return _client
    _client = network.async_client(
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


def reset_client_sync() -> None:
    """Drop shared upstream client after network config changes.

    If called while an event loop is running, close in the background; otherwise
    close synchronously with asyncio.run. New requests lazily create a fresh
    client with the latest network settings.
    """
    global _client
    old = _client
    _client = None
    if old is None:
        return
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        import asyncio
        asyncio.run(old.aclose())
    else:
        loop.create_task(old.aclose())


def set_client(client: httpx.AsyncClient) -> None:
    """用于测试注入（例如 MockTransport 的 client）。"""
    global _client
    _client = client


# ─── Usage 抽取 ──────────────────────────────────────────────────

def extract_usage_from_json(obj: Any) -> dict:
    """非流式响应对象中的 usage 抽取为统一结构。"""
    return legacy_usage_from_anthropic_json(obj)


def _zero_usage() -> dict:
    return zero_legacy_usage()


def _format_stream_error_info(payload: Any, fallback: str = "upstream stream error") -> tuple[Optional[str], str]:
    """Return (error_type_or_code, readable_message) for terminal SSE errors."""
    return protocol_errors.extract_error_info(payload, fallback=fallback)


def _incomplete_stream_error_info(data: dict) -> tuple[str, str]:
    """Readable error info for OpenAI Responses response.incomplete events."""
    if protocol_errors.is_responses_max_output_incomplete(data):
        return (
            protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
            protocol_errors.responses_max_output_context_error_message(
                protocol_errors.responses_incomplete_reason(data)
            ),
        )
    resp = data.get("response") if isinstance(data, dict) else None
    details = resp.get("incomplete_details") if isinstance(resp, dict) else None
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason is None and isinstance(resp, dict):
        reason = resp.get("status")
    return "response_incomplete", f"response incomplete: {reason or 'unknown reason'}"


# Events that carry actual assistant/tool output. Metadata-only events such as
# response.created / response.in_progress / keepalive must not be considered the
# first downstream byte boundary; upstream errors before these events should still
# be catchable/retryable.
RESPONSES_VISIBLE_EVENTS = frozenset({
    "response.output_item.added",
    "response.output_text.delta",
    "response.refusal.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.function_call_arguments.delta",
    "response.output_text.annotation.added",
    "response.web_search_call.in_progress",
    "response.web_search_call.searching",
    "response.web_search_call.completed",
    "response.code_interpreter_call.in_progress",
    "response.code_interpreter_call_code.delta",
    "response.code_interpreter_call.completed",
    "response.mcp_call.in_progress",
    "response.mcp_call.completed",
    "response.file_search_call.in_progress",
    "response.file_search_call.completed",
})


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
        self._usage_acc = UsageAccumulator()
        self.usage = self._usage_acc.legacy_dict()
        self._chunks: list[bytes] = []
        self._buf = b""
        # 是否已见到上游流的"收尾事件"。Anthropic: message_stop。见后判定
        # 即使 client 之后断开，服务端视角也已拿到完整响应，日志应归 success。
        self.saw_stream_end = False
        self.saw_stream_error = False
        self.stream_error_message: Optional[str] = None
        self.stream_error_code: Optional[str] = None

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
            if t == "error" or isinstance(evt.get("error"), dict):
                self.saw_stream_error = True
                self.stream_error_code, self.stream_error_message = _format_stream_error_info(evt)
                continue
            if t == "message_start":
                message = evt.get("message")
                usage_obj = message.get("usage") if isinstance(message, dict) else None
                self._usage_acc.update_from_anthropic_message_start(usage_obj)
                self.usage = self._usage_acc.legacy_dict()
            elif t == "message_delta":
                self._usage_acc.update_from_anthropic_message_delta(evt.get("usage"))
                self.usage = self._usage_acc.legacy_dict()
            elif t == "message_stop":
                self.saw_stream_end = True

    @property
    def usage_observed(self) -> bool:
        return bool(self._usage_acc.usage_observed)

    @property
    def usage_invalid(self) -> bool:
        return bool(self._usage_acc.usage_invalid)

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
#
# ⚠️ 语义对齐（v0.8.0 修复）：
# OpenAI 上游的 `prompt_tokens` 是含缓存命中的**总 prompt**，而 Anthropic
# 的 `input_tokens` 指的是**未命中缓存的新 token**。为了让 DB 里 4 键的语
# 义统一（Anthropic 风格），此处在归一时做一次扣减：
#     input_tokens = max(0, prompt_tokens - cached_tokens)
# 这样 DB 里的 `input_tokens + cache_read_tokens` 始终等于完整 prompt 总量，
# 展示层的 `↑ = input + cache_creation + cache_read` 公式对 OpenAI / Anthropic
# 两套协议都能得到正确的总 prompt 大小，缓存命中率 `cache_read / ↑` 也不会
# 因为重复计数而掉到一半。
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
    """Split complete LF- or CRLF-framed SSE events from *buf*.

    HTTPX preserves wire framing, so CRLF must be handled explicitly instead of
    relying on a transport-level newline normalization that does not occur.
    """
    buf, blocks = _split_sse_events_bytes(buf)
    return buf, [block.decode("utf-8", errors="replace") for block in blocks]


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


def split_sse_events(buf: bytes) -> tuple[bytes, list[bytes]]:
    """Backward-compatible export of the shared LF/CRLF SSE splitter."""
    return _split_sse_events_bytes(buf)


def parse_sse_event_bytes(block: bytes) -> tuple[Optional[str], Optional[dict]]:
    return _parse_event_block(block.decode("utf-8", errors="replace"))


def is_stream_error_event(event_name: Optional[str], data: Optional[dict]) -> bool:
    if not isinstance(data, dict):
        return False
    if protocol_errors.is_responses_max_output_incomplete(data, event_name):
        return True
    if event_name == "error" or data.get("type") == "error":
        return True
    if event_name == "response.failed":
        return True
    if isinstance(data.get("error"), dict):
        return True
    resp = data.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
        return True
    return False


def is_downstream_visible_event(event_name: Optional[str], data: Optional[dict], protocol: str) -> bool:
    """Whether an upstream SSE event should cross the first-byte boundary.

    Metadata/control events are intentionally excluded. For OpenAI Responses,
    response.created / response.in_progress / keepalive / response.completed /
    response.failed do not constitute visible content.
    """
    if protocol == "openai-responses":
        return event_name in RESPONSES_VISIBLE_EVENTS
    return data is not None and not is_stream_error_event(event_name, data)


# ─── OpenAI Chat SSE 工具 ─────────────────────────────────────────────


def extract_usage_chat_json(obj: Any) -> dict:
    """从 /v1/chat/completions 非流式响应里抽 usage，归一到 4 键。"""
    return legacy_usage_from_openai_chat_json(obj)


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
        self._usage_acc = UsageAccumulator()
        self.usage = self._usage_acc.legacy_dict()
        self._chunks: list[bytes] = []
        self._buf = b""
        # Chat 流的收尾标记：[DONE] 或任一 choice 带 finish_reason。
        # 两者都足以说明上游完成了本次生成；若 client 之后断开日志归 success。
        self.saw_stream_end = False
        self.saw_stream_error = False
        self.stream_error_message: Optional[str] = None
        self.stream_error_code: Optional[str] = None

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
                if isinstance(evt.get("error"), dict):
                    self.saw_stream_error = True
                    self.stream_error_code, self.stream_error_message = _format_stream_error_info(evt)
                    continue
                choices = evt.get("choices")
                if isinstance(choices, list):
                    for ch in choices:
                        if isinstance(ch, dict) and ch.get("finish_reason"):
                            self.saw_stream_end = True
                            break
                u = evt.get("usage")
                if isinstance(u, dict):
                    # 见文件顶部「语义对齐」说明：OpenAI 的 prompt_tokens 含
                    # cache，此处扣除后落库，保持与 Anthropic 语义一致。
                    self._usage_acc.set_from_openai_chat_usage(u)
                    self.usage = self._usage_acc.legacy_dict()

    @property
    def usage_observed(self) -> bool:
        return bool(self._usage_acc.usage_observed)

    @property
    def usage_invalid(self) -> bool:
        return bool(self._usage_acc.usage_invalid)

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
        fr = self._finish_reason
        # Guard: upstream claims tool_calls but none were actually returned.
        if fr in ("tool_calls", "function_call") and not self._tool_calls:
            return "stop"
        return fr

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
                "finish_reason": self.finish_reason or "stop",
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
    return legacy_usage_from_openai_responses_json(obj)


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
    # 优先看完整的 LF/CRLF event；首个网络 chunk 若尚未带完整分隔符，
    # 仍保留旧行为，对当前已收到的部分尽力解析。
    remaining, raw_blocks = _split_sse_events_bytes(chunk)
    blocks = [raw.decode("utf-8", errors="replace") for raw in raw_blocks]
    if remaining.strip():
        blocks.append(remaining.decode("utf-8", errors="replace"))
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
        self._usage_acc = UsageAccumulator()
        self.usage = self._usage_acc.legacy_dict()
        self._chunks: list[bytes] = []
        self._buf = b""
        # Responses 流的收尾事件：completed / failed / incomplete 之一。
        # 收到即视为上游已完成本次生成，client 后续断开不影响日志归 success。
        self.saw_stream_end = False
        self.saw_stream_error = False
        self.stream_error_message: Optional[str] = None
        self.stream_error_code: Optional[str] = None

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
            if event_name == "error" or (isinstance(data, dict) and data.get("type") == "error"):
                self.saw_stream_error = True
                self.stream_error_code, self.stream_error_message = _format_stream_error_info(data)
                continue
            if event_name == "response.failed":
                self.saw_stream_end = True
                self.saw_stream_error = True
                self.stream_error_code, self.stream_error_message = _format_stream_error_info(data)
            elif event_name == "response.incomplete":
                self.saw_stream_end = True
                if protocol_errors.is_responses_max_output_incomplete(data, event_name):
                    # Downstream clients commonly miss the Responses-specific
                    # terminal incomplete event.  Normalize the explicit
                    # max_output_tokens reason into a context-length style error
                    # so existing retry/compact paths can recognize it.
                    self.saw_stream_error = True
                    self.stream_error_code, self.stream_error_message = _incomplete_stream_error_info(data)
            elif event_name == "response.completed":
                self.saw_stream_end = True
            if event_name in ("response.completed", "response.failed", "response.incomplete"):
                resp = data.get("response") if isinstance(data, dict) else None
                if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
                    # 见文件顶部「语义对齐」说明：扣掉缓存命中部分后落库。
                    self._usage_acc.set_from_openai_responses_usage(resp["usage"])
                    self.usage = self._usage_acc.legacy_dict()

    @property
    def usage_observed(self) -> bool:
        return bool(self._usage_acc.usage_observed)

    @property
    def usage_invalid(self) -> bool:
        return bool(self._usage_acc.usage_invalid)

    def get_full_response(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class ResponsesSSEAssistantBuilder:
    """Rebuild final Responses output items from an incremental SSE stream.

    Detailed SSE events are useful when an upstream omits the final response
    snapshot; conversely, ``response.completed.response.output`` is authoritative
    when it is present.  The builder merges both instead of letting a sparse
    ``output_item.added`` event overwrite a complete terminal response.
    """

    def __init__(self):
        self._buf = b""
        self._items: dict[int, dict] = {}
        self._fc_args: dict[int, str] = {}
        self._msg_text: dict[tuple[int, int], str] = {}
        self._msg_refusal: dict[tuple[int, int], str] = {}
        self._got_any = False
        self._response_obj: Optional[dict] = None
        self._completed_response_obj: Optional[dict] = None

    @staticmethod
    def _index(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _merge_text_snapshot(current: str, snapshot: Any) -> str:
        """Merge a full terminal snapshot without duplicating delta text."""
        if not isinstance(snapshot, str):
            return current
        if not current:
            return snapshot
        if snapshot.startswith(current):
            return snapshot
        if current.startswith(snapshot):
            return current
        # A terminal event is more authoritative than a conflicting intermediate
        # snapshot; response.completed can still supersede it later.
        return snapshot

    @staticmethod
    def _merge_preferred_text(preferred: Any, supplemental: Any) -> Any:
        """Keep the authoritative value unless the supplement safely extends it."""
        if not isinstance(preferred, str) or not preferred:
            return supplemental if isinstance(supplemental, str) else preferred
        if not isinstance(supplemental, str) or not supplemental:
            return preferred
        if supplemental.startswith(preferred):
            return supplemental
        return preferred

    def _append_part_delta(self, store: dict[tuple[int, int], str], data: dict) -> None:
        delta = data.get("delta")
        if not isinstance(delta, str):
            return
        key = (self._index(data.get("output_index")), self._index(data.get("content_index")))
        store[key] = store.get(key, "") + delta

    def _set_part_snapshot(self, store: dict[tuple[int, int], str], data: dict, value: Any) -> None:
        if not isinstance(value, str):
            return
        key = (self._index(data.get("output_index")), self._index(data.get("content_index")))
        store[key] = self._merge_text_snapshot(store.get(key, ""), value)

    def _capture_item_snapshot(self, output_index: int, item: dict) -> None:
        """Record content/arguments embedded in item snapshots as terminal data."""
        item_type = item.get("type")
        if item_type == "function_call":
            args = item.get("arguments")
            if isinstance(args, str):
                self._fc_args[output_index] = self._merge_text_snapshot(
                    self._fc_args.get(output_index, ""), args,
                )
            return
        if item_type != "message":
            return
        content = item.get("content")
        if not isinstance(content, list):
            return
        for content_index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            data = {"output_index": output_index, "content_index": content_index}
            if part.get("type") == "output_text":
                self._set_part_snapshot(self._msg_text, data, part.get("text"))
            elif part.get("type") == "refusal":
                self._set_part_snapshot(self._msg_refusal, data, part.get("refusal"))

    def _record_item(self, output_index: int, item: Any) -> None:
        if not isinstance(item, dict):
            return
        snapshot = copy.deepcopy(item)
        self._items[output_index] = snapshot
        self._capture_item_snapshot(output_index, snapshot)

    @staticmethod
    def _merge_item_snapshots(preferred: dict, supplemental: dict) -> dict:
        """Fill sparse fields from *supplemental* without replacing final data."""
        merged = copy.deepcopy(preferred)
        for key, value in supplemental.items():
            if key == "content":
                continue
            if merged.get(key) in (None, "", [], {}):
                merged[key] = copy.deepcopy(value)

        pref_content = merged.get("content")
        supp_content = supplemental.get("content")
        if not isinstance(pref_content, list):
            if isinstance(supp_content, list):
                merged["content"] = copy.deepcopy(supp_content)
            return merged
        if not isinstance(supp_content, list):
            return merged

        content = copy.deepcopy(pref_content)
        for index, part in enumerate(supp_content):
            if index >= len(content):
                content.append(copy.deepcopy(part))
                continue
            existing = content[index]
            if not isinstance(existing, dict) or not isinstance(part, dict):
                if existing in (None, "", [], {}):
                    content[index] = copy.deepcopy(part)
                continue
            for key, value in part.items():
                if existing.get(key) in (None, "", [], {}):
                    existing[key] = copy.deepcopy(value)
        merged["content"] = content
        return merged

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        self._buf, events = _iter_sse_events(self._buf)
        for block in events:
            event_name, data = _parse_event_block(block)
            if event_name is None or not isinstance(data, dict):
                continue
            self._got_any = True

            if event_name in (
                "response.created", "response.in_progress", "response.completed",
                "response.incomplete", "response.failed",
            ):
                response = data.get("response")
                if isinstance(response, dict):
                    self._response_obj = copy.deepcopy(response)
                    if event_name == "response.completed":
                        self._completed_response_obj = copy.deepcopy(response)
                    # The terminal response can be the only place an upstream
                    # exposes output items, so keep it as a source as well.
                    if event_name in ("response.completed", "response.incomplete"):
                        output = response.get("output")
                        if isinstance(output, list):
                            for output_index, item in enumerate(output):
                                self._record_item(output_index, item)

            if event_name in ("response.output_item.added", "response.output_item.done"):
                self._record_item(self._index(data.get("output_index")), data.get("item"))
            elif event_name == "response.output_text.delta":
                self._append_part_delta(self._msg_text, data)
            elif event_name == "response.output_text.done":
                self._set_part_snapshot(self._msg_text, data, data.get("text"))
            elif event_name == "response.refusal.delta":
                self._append_part_delta(self._msg_refusal, data)
            elif event_name == "response.refusal.done":
                self._set_part_snapshot(self._msg_refusal, data, data.get("refusal"))
            elif event_name == "response.content_part.done":
                part = data.get("part")
                if isinstance(part, dict):
                    if part.get("type") == "output_text":
                        self._set_part_snapshot(self._msg_text, data, part.get("text"))
                    elif part.get("type") == "refusal":
                        self._set_part_snapshot(self._msg_refusal, data, part.get("refusal"))
            elif event_name == "response.function_call_arguments.delta":
                output_index = self._index(data.get("output_index"))
                delta = data.get("delta")
                if isinstance(delta, str):
                    self._fc_args[output_index] = self._fc_args.get(output_index, "") + delta
            elif event_name == "response.function_call_arguments.done":
                output_index = self._index(data.get("output_index"))
                self._fc_args[output_index] = self._merge_text_snapshot(
                    self._fc_args.get(output_index, ""), data.get("arguments"),
                )

    def _base_items_by_index(self) -> dict[int, dict]:
        """Start from response.completed output, then fill only missing snapshots."""
        out: dict[int, dict] = {}
        response = self._completed_response_obj or self._response_obj
        if isinstance(response, dict) and isinstance(response.get("output"), list):
            for output_index, item in enumerate(response["output"]):
                if isinstance(item, dict):
                    out[output_index] = copy.deepcopy(item)
        for output_index, item in self._items.items():
            existing = out.get(output_index)
            out[output_index] = (
                self._merge_item_snapshots(existing, item)
                if isinstance(existing, dict) else copy.deepcopy(item)
            )
        return out

    def _apply_message_buffers(self, output_index: int, item: dict) -> None:
        content = item.get("content")
        if not isinstance(content, list):
            content = []
        else:
            content = copy.deepcopy(content)

        for store, part_type, value_key in (
            (self._msg_text, "output_text", "text"),
            (self._msg_refusal, "refusal", "refusal"),
        ):
            for (item_index, content_index), value in store.items():
                if item_index != output_index or not value:
                    continue
                while len(content) <= content_index:
                    content.append({})
                part = content[content_index]
                if not isinstance(part, dict):
                    part = {}
                    content[content_index] = part
                part.setdefault("type", part_type)
                part[value_key] = self._merge_preferred_text(part.get(value_key), value)
                if part_type == "output_text":
                    part.setdefault("annotations", [])
        item["content"] = content

    def get_output_items(self) -> list[dict]:
        """Return final output items ordered by Responses ``output_index``."""
        out: list[dict] = []
        for output_index, source_item in sorted(self._base_items_by_index().items()):
            item = copy.deepcopy(source_item)
            item_type = item.get("type")
            if item_type == "message":
                self._apply_message_buffers(output_index, item)
            elif item_type == "function_call":
                item["arguments"] = self._merge_preferred_text(
                    item.get("arguments"), self._fc_args.get(output_index, ""),
                )
            out.append(item)
        return out

    def get_assistant(self) -> dict:
        return {"role": "assistant", "output": self.get_output_items()}

    @property
    def has_any_event(self) -> bool:
        return self._got_any

    def to_full_json(self, *, fallback_model: str = "") -> dict:
        """Build one complete Responses object without losing terminal output."""
        source = self._completed_response_obj or self._response_obj
        base: dict = copy.deepcopy(source) if isinstance(source, dict) else {}
        base["output"] = self.get_output_items()
        base.setdefault("object", "response")
        base.setdefault("status", "completed")
        if not base.get("model"):
            base["model"] = fallback_model
        return base
