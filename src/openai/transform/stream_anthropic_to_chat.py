"""SSE translator: Anthropic Messages stream → OpenAI Chat stream.

Used by Phase 8 OpenAI Chat ingress → Anthropic upstream.  Narrow scope:
text and function tool calls.  Anthropic thinking/redacted_thinking are not
exposed here; request-side reasoning remains guarded before routing.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ...protocols.usage import legacy_usage_from_anthropic_json


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _chat_chunk(state: "_State", *, delta: Optional[dict] = None,
                finish_reason: Optional[str] = None, usage: Optional[dict] = None,
                include_choice: bool = True, include_usage_null: bool = False) -> bytes:
    obj: dict[str, Any] = {
        "id": state.chat_id,
        "object": "chat.completion.chunk",
        "created": state.created_ts,
        "model": state.model,
        "choices": [],
    }
    if include_choice:
        obj["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}]
    if usage is not None:
        obj["usage"] = usage
    elif include_usage_null:
        obj["usage"] = None
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _error_chunk(err: dict) -> bytes:
    return b"data: " + json.dumps({"error": err}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _parse_event_block(block: str) -> tuple[Optional[str], Optional[dict]]:
    event_name: Optional[str] = None
    data_lines: list[str] = []
    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("event:"):
            event_name = line[6:].strip() or None
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return event_name, None
    try:
        obj = json.loads("\n".join(data_lines))
    except Exception:
        return event_name, None
    return event_name, obj if isinstance(obj, dict) else None


def _finish_reason(stop_reason: Optional[str], *, saw_tool: bool) -> str:
    if saw_tool or stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason in ("stop_sequence", "end_turn"):
        return "stop"
    return "stop"


def _chat_usage_from_anthropic(usage: Optional[dict]) -> dict:
    legacy = legacy_usage_from_anthropic_json({"usage": usage or {}})
    prompt_tokens = legacy["input_tokens"] + legacy["cache_creation"] + legacy["cache_read"]
    completion_tokens = legacy["output_tokens"]
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": legacy["cache_read"]},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def _merge_anthropic_usage(existing: Optional[dict], update: dict) -> dict:
    """Merge Anthropic stream usage without dropping message_start tokens.

    Anthropic streams normally send input/cache counts on message_start and
    output counts on message_delta.  A plain assignment at message_delta would
    erase input/cache tokens and break Parrot's accounting/UI totals.
    """
    merged = dict(existing or {})
    token_keys = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    for key, value in (update or {}).items():
        if key in token_keys:
            try:
                incoming = int(value or 0)
            except Exception:
                incoming = 0
            try:
                current = int(merged.get(key) or 0)
            except Exception:
                current = 0
            # Anthropic-compatible streams may send input/cache on
            # message_start and output-only (or zero-filled) usage on
            # message_delta.  Preserve the largest observed cumulative value
            # instead of letting a later zero erase prompt/cache accounting.
            merged[key] = max(current, incoming)
            continue
        merged[key] = value
    return merged


@dataclass
class _ToolState:
    block_index: int
    chat_index: int
    id: str = ""
    name: str = ""
    args: str = ""
    started: bool = False
    emitted: bool = False


@dataclass
class _State:
    chat_id: str
    model: str
    created_ts: int
    include_usage: bool = False
    role_sent: bool = False
    terminal_emitted: bool = False
    stop_reason: Optional[str] = None
    usage: Optional[dict] = None
    tools: dict[int, _ToolState] = field(default_factory=dict)
    next_tool_index: int = 0
    text_parts: list[str] = field(default_factory=list)


class StreamTranslator:
    """Anthropic SSE → Chat SSE."""

    def __init__(self, *, model: str, include_usage: bool = False, created_ts: Optional[int] = None):
        self.state = _State(
            chat_id=_gen_id("chatcmpl-"),
            model=model,
            created_ts=int(created_ts or time.time()),
            include_usage=include_usage,
        )
        self._buf = b""

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        if not chunk:
            return
        self._buf += chunk
        while b"\n\n" in self._buf:
            block_bytes, self._buf = self._buf.split(b"\n\n", 1)
            block = block_bytes.decode("utf-8", errors="replace")
            if not block.strip():
                continue
            event_name, data = _parse_event_block(block)
            if data is None:
                continue
            yield from self._handle_event(event_name or str(data.get("type") or ""), data)

    def close(self) -> Iterator[bytes]:
        if self.state.terminal_emitted:
            return
        self.state.terminal_emitted = True
        if not self.state.role_sent:
            yield from self._emit_role()
        yield from self._emit_pending_tools()
        usage = _chat_usage_from_anthropic(self.state.usage)
        finish = _finish_reason(self.state.stop_reason, saw_tool=any(t.emitted for t in self.state.tools.values()))
        yield _chat_chunk(
            self.state,
            delta={},
            finish_reason=finish,
            usage=usage if not self.state.include_usage else None,
            include_usage_null=self.state.include_usage,
        )
        if self.state.include_usage:
            yield _chat_chunk(self.state, include_choice=False, usage=usage)
        yield b"data: [DONE]\n\n"

    def _handle_event(self, event_name: str, data: dict) -> Iterator[bytes]:
        typ = str(data.get("type") or event_name or "")
        if typ == "error" or isinstance(data.get("error"), dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else data
            yield _error_chunk(err)
            self.state.terminal_emitted = True
            return

        if typ == "message_start":
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            if isinstance(msg.get("model"), str) and msg.get("model"):
                self.state.model = msg["model"]
            if isinstance(msg.get("usage"), dict):
                self.state.usage = _merge_anthropic_usage(self.state.usage, msg["usage"])
            yield from self._emit_role()
            return

        if typ == "content_block_start":
            block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
            if block.get("type") == "tool_use":
                idx = int(data.get("index", 0) or 0)
                st = self._tool(idx)
                st.id = str(block.get("id") or st.id or _gen_id("call_"))
                st.name = str(block.get("name") or st.name or "tool")
                st.started = True
            return

        if typ == "content_block_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            dt = delta.get("type")
            if dt == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    yield from self._emit_role()
                    self.state.text_parts.append(text)
                    yield _chat_chunk(self.state, delta={"content": text}, include_usage_null=self.state.include_usage)
            elif dt == "input_json_delta":
                idx = int(data.get("index", 0) or 0)
                st = self._tool(idx)
                part = delta.get("partial_json")
                if isinstance(part, str) and part:
                    st.args += part
            return

        if typ == "content_block_stop":
            idx = int(data.get("index", 0) or 0)
            st = self.state.tools.get(idx)
            if st is not None and st.started and not st.emitted:
                yield from self._emit_tool(st)
            return

        if typ == "message_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if isinstance(delta.get("stop_reason"), str):
                self.state.stop_reason = delta["stop_reason"]
            if isinstance(data.get("usage"), dict):
                self.state.usage = _merge_anthropic_usage(self.state.usage, data["usage"])
            return

        if typ == "message_stop":
            return

    def _emit_role(self) -> Iterator[bytes]:
        if self.state.role_sent:
            return
        self.state.role_sent = True
        yield _chat_chunk(self.state, delta={"role": "assistant"}, include_usage_null=self.state.include_usage)

    def _tool(self, block_index: int) -> _ToolState:
        st = self.state.tools.get(block_index)
        if st is None:
            st = _ToolState(block_index=block_index, chat_index=self.state.next_tool_index)
            self.state.next_tool_index += 1
            self.state.tools[block_index] = st
        return st

    def _emit_tool(self, st: _ToolState) -> Iterator[bytes]:
        yield from self._emit_role()
        st.emitted = True
        yield _chat_chunk(self.state, delta={
            "tool_calls": [{
                "index": st.chat_index,
                "id": st.id or _gen_id("call_"),
                "type": "function",
                "function": {"name": st.name or "tool", "arguments": st.args or "{}"},
            }]
        }, include_usage_null=self.state.include_usage)

    def _emit_pending_tools(self) -> Iterator[bytes]:
        for idx in sorted(self.state.tools.keys()):
            st = self.state.tools[idx]
            if st.started and not st.emitted:
                yield from self._emit_tool(st)

    def get_downstream_chat_assistant(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": "".join(self.state.text_parts) or None}
        calls = []
        for idx in sorted(self.state.tools.keys()):
            st = self.state.tools[idx]
            if st.started:
                calls.append({
                    "id": st.id,
                    "type": "function",
                    "function": {"name": st.name or "tool", "arguments": st.args or "{}"},
                })
        if calls:
            msg["tool_calls"] = calls
        return msg
