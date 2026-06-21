"""SSE translator: OpenAI Chat stream → Anthropic Messages stream.

Used by Phase 8 Anthropic ingress → OpenAI Chat upstream when the downstream
request is streaming.  This intentionally supports only text/refusal and
function tool calls.  Reasoning deltas are not surfaced as Anthropic thinking;
request-side thinking/reasoning remains guarded before routing.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ...protocols.usage import legacy_usage_from_openai_chat_json


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _emit(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def _parse_chat_block(block: str) -> Optional[dict]:
    data_str: Optional[str] = None
    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[5:].strip()
    if data_str is None:
        return None
    if data_str == "[DONE]":
        return {"_done": True}
    try:
        obj = json.loads(data_str)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _stop_reason(finish_reason: str | None, *, saw_tool: bool) -> str:
    if saw_tool or finish_reason in ("tool_calls", "function_call"):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def _anthropic_usage_from_chat_usage(usage: Optional[dict]) -> dict[str, int]:
    legacy = legacy_usage_from_openai_chat_json({"usage": usage or {}})
    return {
        "input_tokens": legacy["input_tokens"],
        "output_tokens": legacy["output_tokens"],
        "cache_creation_input_tokens": legacy["cache_creation"],
        "cache_read_input_tokens": legacy["cache_read"],
    }


@dataclass
class _ToolState:
    chat_index: int
    block_index: int
    id: str = ""
    name: str = ""
    args: str = ""
    started: bool = False
    stopped: bool = False


@dataclass
class _State:
    message_id: str
    model: str
    created_ts: int
    message_started: bool = False
    text_started: bool = False
    text_stopped: bool = False
    text_index: int = -1
    next_block_index: int = 0
    tools: dict[int, _ToolState] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    usage: Optional[dict] = None
    done_seen: bool = False
    terminal_emitted: bool = False
    text_parts: list[str] = field(default_factory=list)

    def alloc_index(self) -> int:
        idx = self.next_block_index
        self.next_block_index += 1
        return idx


class StreamTranslator:
    """OpenAI Chat SSE → Anthropic SSE."""

    def __init__(self, *, model: str, created_ts: Optional[int] = None):
        self.state = _State(
            message_id=_gen_id("msg_"),
            model=model,
            created_ts=int(created_ts or time.time()),
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
            evt = _parse_chat_block(block)
            if evt is None:
                continue
            yield from self._handle_event(evt)

    def close(self) -> Iterator[bytes]:
        if self.state.terminal_emitted:
            return
        self.state.terminal_emitted = True
        if not self.state.message_started:
            yield from self._emit_message_start()
        yield from self._stop_text_if_needed()
        yield from self._stop_all_tools()
        yield _emit("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": _stop_reason(self.state.finish_reason, saw_tool=any(t.started for t in self.state.tools.values())),
                "stop_sequence": None,
            },
            "usage": _anthropic_usage_from_chat_usage(self.state.usage),
        })
        yield _emit("message_stop", {"type": "message_stop"})

    # ─── event handling ──────────────────────────────────────────

    def _handle_event(self, evt: dict) -> Iterator[bytes]:
        if evt.get("_done"):
            self.state.done_seen = True
            return
        if isinstance(evt.get("error"), dict):
            yield _emit("error", {"type": "error", "error": evt.get("error") or {}})
            self.state.terminal_emitted = True
            return

        if isinstance(evt.get("id"), str) and evt.get("id"):
            self.state.message_id = evt["id"]
        if isinstance(evt.get("model"), str) and evt.get("model"):
            self.state.model = evt["model"]
        if isinstance(evt.get("created"), int):
            self.state.created_ts = int(evt["created"])

        usage = evt.get("usage")
        if isinstance(usage, dict):
            self.state.usage = usage

        choices = evt.get("choices") or []
        if not choices:
            return
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        if delta or choice.get("finish_reason"):
            if not self.state.message_started:
                yield from self._emit_message_start()

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield from self._emit_text_delta(content)

        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            yield from self._emit_text_delta(refusal)

        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict):
                yield from self._handle_tool_call_delta(tc)

        legacy_fn = delta.get("function_call")
        if isinstance(legacy_fn, dict):
            yield from self._handle_tool_call_delta({"index": 0, "type": "function", "function": legacy_fn})

        fr = choice.get("finish_reason")
        if isinstance(fr, str) and fr:
            self.state.finish_reason = fr

    # ─── Anthropic emit helpers ──────────────────────────────────

    def _emit_message_start(self) -> Iterator[bytes]:
        if self.state.message_started:
            return
        self.state.message_started = True
        yield _emit("message_start", {
            "type": "message_start",
            "message": {
                "id": self.state.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.state.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _ensure_text_block(self) -> Iterator[bytes]:
        if not self.state.message_started:
            yield from self._emit_message_start()
        if self.state.text_started and not self.state.text_stopped:
            return
        self.state.text_index = self.state.alloc_index() if self.state.text_index < 0 else self.state.text_index
        self.state.text_started = True
        self.state.text_stopped = False
        yield _emit("content_block_start", {
            "type": "content_block_start",
            "index": self.state.text_index,
            "content_block": {"type": "text", "text": ""},
        })

    def _emit_text_delta(self, text: str) -> Iterator[bytes]:
        yield from self._ensure_text_block()
        self.state.text_parts.append(text)
        yield _emit("content_block_delta", {
            "type": "content_block_delta",
            "index": self.state.text_index,
            "delta": {"type": "text_delta", "text": text},
        })

    def _stop_text_if_needed(self) -> Iterator[bytes]:
        if self.state.text_started and not self.state.text_stopped:
            self.state.text_stopped = True
            yield _emit("content_block_stop", {"type": "content_block_stop", "index": self.state.text_index})

    def _tool_state(self, chat_index: int) -> _ToolState:
        st = self.state.tools.get(chat_index)
        if st is None:
            st = _ToolState(chat_index=chat_index, block_index=self.state.alloc_index())
            self.state.tools[chat_index] = st
        return st

    def _start_tool_if_ready(self, st: _ToolState) -> Iterator[bytes]:
        if st.started:
            return
        if not self.state.message_started:
            yield from self._emit_message_start()
        # Anthropic tool_use requires a name.  If the stream never supplies one,
        # use a conservative placeholder rather than emitting an invalid block.
        name = st.name or "tool"
        st.id = st.id or _gen_id("call_")
        yield from self._stop_text_if_needed()
        st.started = True
        yield _emit("content_block_start", {
            "type": "content_block_start",
            "index": st.block_index,
            "content_block": {"type": "tool_use", "id": st.id, "name": name, "input": {}},
        })

    def _handle_tool_call_delta(self, tc: dict) -> Iterator[bytes]:
        try:
            idx = int(tc.get("index", 0))
        except Exception:
            idx = 0
        st = self._tool_state(idx)
        if isinstance(tc.get("id"), str) and tc.get("id"):
            st.id = tc["id"]
        fn = tc.get("function") or {}
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name and not st.started:
                st.name = name
            args = fn.get("arguments")
            if isinstance(args, str) and args:
                if not st.started:
                    yield from self._start_tool_if_ready(st)
                st.args += args
                yield _emit("content_block_delta", {
                    "type": "content_block_delta",
                    "index": st.block_index,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                })
        if not st.started and (st.name or st.id):
            yield from self._start_tool_if_ready(st)

    def _stop_all_tools(self) -> Iterator[bytes]:
        for idx in sorted(self.state.tools.keys()):
            st = self.state.tools[idx]
            if not st.started:
                yield from self._start_tool_if_ready(st)
            if st.started and not st.stopped:
                st.stopped = True
                yield _emit("content_block_stop", {"type": "content_block_stop", "index": st.block_index})

    def get_downstream_anthropic_assistant(self) -> dict:
        blocks: list[dict[str, Any]] = []
        if self.state.text_parts:
            blocks.append({"type": "text", "text": "".join(self.state.text_parts)})
        for idx in sorted(self.state.tools.keys()):
            st = self.state.tools[idx]
            if st.started:
                try:
                    parsed = json.loads(st.args) if st.args else {}
                except Exception:
                    parsed = {"_raw": st.args}
                if not isinstance(parsed, dict):
                    parsed = {"_value": parsed}
                blocks.append({"type": "tool_use", "id": st.id, "name": st.name or "tool", "input": parsed})
        return {"role": "assistant", "content": blocks}
