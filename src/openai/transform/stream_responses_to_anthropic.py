"""SSE translator: OpenAI Responses stream → Anthropic Messages stream.

Used by Phase 8 Anthropic ingress → OpenAI Responses upstream.  Metadata events
(response.created/in_progress) are consumed for state but do not emit downstream
bytes; the first Anthropic bytes are emitted only when a visible text/tool event
arrives, preserving the existing Responses failover commit boundary.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ...protocols import errors as protocol_errors
from . import common
from ...protocols.usage import legacy_usage_from_openai_responses_json


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _emit(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


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
    data_str = "\n".join(data_lines)
    if data_str == "[DONE]":
        return event_name, None
    try:
        obj = json.loads(data_str)
    except Exception:
        return event_name, None
    return event_name, obj if isinstance(obj, dict) else None


def _error_type_from_code_or_message(code: Any, message: Any) -> str:
    low = f"{code or ''} {message or ''}".lower()
    if protocol_errors.is_context_length_code_or_message(code, message):
        return "invalid_request_error"
    if "rate_limit" in low or "rate limit" in low:
        return "rate_limit_error"
    if "permission" in low or "forbidden" in low:
        return "permission_error"
    if "auth" in low or "api key" in low:
        return "authentication_error"
    return "api_error"


def _normalize_error_for_anthropic(err: dict[str, Any]) -> dict[str, Any]:
    out = dict(err or {})
    message = out.get("message") or out.get("reason") or "upstream response failed"
    code = out.get("code") or out.get("error_type")
    if protocol_errors.is_context_length_code_or_message(code, message):
        out["type"] = "invalid_request_error"
        out["code"] = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
        out["message"] = protocol_errors.context_length_error_message_for_claude_code(message)
        return out
    if not out.get("message"):
        out["message"] = str(message)
    if not out.get("type"):
        out["type"] = _error_type_from_code_or_message(code, message)
    return out


def _response_from_event(data: dict) -> dict:
    resp = data.get("response")
    return resp if isinstance(resp, dict) else data


def _anthropic_usage_from_responses_usage(usage: Optional[dict]) -> dict[str, int]:
    legacy = legacy_usage_from_openai_responses_json({"usage": usage or {}})
    return {
        "input_tokens": legacy["input_tokens"],
        "output_tokens": legacy["output_tokens"],
        "cache_creation_input_tokens": legacy["cache_creation"],
        "cache_read_input_tokens": legacy["cache_read"],
    }


def _stop_reason(status: Optional[str], incomplete_reason: Optional[str], *, saw_tool: bool) -> str:
    if saw_tool:
        return "tool_use"
    if status == "incomplete" and incomplete_reason in ("max_output_tokens", "max_tokens"):
        return "max_tokens"
    return "end_turn"


@dataclass
class _ToolState:
    key: str
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
    text_parts: list[str] = field(default_factory=list)
    tools: dict[str, _ToolState] = field(default_factory=dict)
    output_index_to_key: dict[int, str] = field(default_factory=dict)
    item_id_to_key: dict[str, str] = field(default_factory=dict)
    last_tool_key: Optional[str] = None
    status: Optional[str] = None
    incomplete_reason: Optional[str] = None
    usage: Optional[dict] = None
    terminal_emitted: bool = False

    def alloc_index(self) -> int:
        idx = self.next_block_index
        self.next_block_index += 1
        return idx


class StreamTranslator:
    """OpenAI Responses SSE → Anthropic SSE."""

    def __init__(
        self,
        *,
        model: str,
        created_ts: Optional[int] = None,
        optional_empty_string_fields_by_tool: dict[str, set[str]] | None = None,
        request_body: dict[str, Any] | None = None,
    ):
        self.state = _State(
            message_id=_gen_id("msg_"),
            model=model,
            created_ts=int(created_ts or time.time()),
        )
        self.optional_empty_string_fields_by_tool = dict(optional_empty_string_fields_by_tool or {})
        if request_body is not None:
            self.optional_empty_string_fields_by_tool.update(
                common.optional_empty_string_fields_by_tool_from_anthropic_tools(
                    request_body.get("tools") if isinstance(request_body, dict) else None
                )
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
            if event_name is None and data is None:
                continue
            yield from self._handle_event(event_name or "", data or {})

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
                "stop_reason": _stop_reason(
                    self.state.status, self.state.incomplete_reason,
                    saw_tool=any(t.started for t in self.state.tools.values()),
                ),
                "stop_sequence": None,
            },
            "usage": _anthropic_usage_from_responses_usage(self.state.usage),
        })
        yield _emit("message_stop", {"type": "message_stop"})

    # ─── event handling ──────────────────────────────────────────

    def _handle_event(self, event_name: str, data: dict) -> Iterator[bytes]:
        if event_name == "error" or data.get("type") == "error" or isinstance(data.get("error"), dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else data
            yield _emit("error", {"type": "error", "error": _normalize_error_for_anthropic(err)})
            self.state.terminal_emitted = True
            return

        if event_name in ("response.created", "response.in_progress"):
            self._capture_response_metadata(_response_from_event(data))
            return

        if event_name == "keepalive" or data.get("type") == "keepalive":
            yield from self._emit_ping()
            return

        if event_name == "response.output_item.added":
            item = data.get("item") if isinstance(data.get("item"), dict) else {}
            yield from self._on_output_item_added(data, item)
            return

        if event_name == "response.output_item.done":
            item = data.get("item") if isinstance(data.get("item"), dict) else {}
            yield from self._on_output_item_done(data, item)
            return

        if event_name == "response.content_part.added":
            part = data.get("part") if isinstance(data.get("part"), dict) else {}
            if part.get("type") in ("output_text", "refusal"):
                yield from self._ensure_text_block()
            return

        if event_name in ("response.output_text.delta", "response.refusal.delta"):
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                yield from self._emit_text_delta(delta)
            return

        if event_name == "response.function_call_arguments.delta":
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                st = self._tool_for_delta_event(data)
                if not st.stopped:
                    yield from self._emit_tool_args_delta(st, delta)
            return

        if event_name in ("response.completed", "response.incomplete", "response.failed"):
            resp = _response_from_event(data)
            self._capture_response_metadata(resp)
            self.state.status = str(resp.get("status") or event_name.removeprefix("response."))
            details = resp.get("incomplete_details") if isinstance(resp.get("incomplete_details"), dict) else {}
            self.state.incomplete_reason = details.get("reason") if isinstance(details, dict) else None
            usage = resp.get("usage")
            if isinstance(usage, dict):
                self.state.usage = usage
            if protocol_errors.is_responses_max_output_incomplete(data, event_name):
                msg = protocol_errors.responses_max_output_context_error_message(self.state.incomplete_reason)
                yield _emit("error", {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                        "message": msg,
                    },
                })
                self.state.terminal_emitted = True
            elif event_name == "response.failed":
                err = (
                    resp.get("error")
                    if isinstance(resp.get("error"), dict)
                    else {"message": "upstream response failed"}
                )
                yield _emit("error", {"type": "error", "error": _normalize_error_for_anthropic(err)})
                self.state.terminal_emitted = True
            return

    def _capture_response_metadata(self, resp: dict) -> None:
        if isinstance(resp.get("id"), str) and resp.get("id"):
            self.state.message_id = resp["id"]
        if isinstance(resp.get("model"), str) and resp.get("model"):
            self.state.model = resp["model"]
        if isinstance(resp.get("usage"), dict):
            self.state.usage = resp["usage"]
        if isinstance(resp.get("status"), str):
            self.state.status = resp["status"]

    def _on_output_item_added(self, data: dict, item: dict) -> Iterator[bytes]:
        item_type = item.get("type")
        if item_type != "function_call":
            return
        key = self._key_from_item_event(data, item)
        st = self._tool_state(key)
        self._update_tool_metadata(st, data, item, key)
        yield from self._start_tool_if_needed(st)

    def _on_output_item_done(self, data: dict, item: dict) -> Iterator[bytes]:
        item_type = item.get("type")
        if item_type != "function_call":
            return
        key = self._key_from_item_event(data, item)
        st = self._tool_state(key)
        self._update_tool_metadata(st, data, item, key)
        done_args = item.get("arguments")
        if self._should_buffer_tool_args(st) and isinstance(done_args, str):
            st.args = done_args
        elif isinstance(done_args, str) and done_args != st.args:
            # Responses usually emits argument deltas before output_item.done,
            # but some providers only include the final arguments on the done
            # item.  Emit only the missing suffix when the final value extends
            # the streamed buffer; otherwise avoid duplicating/mangling JSON.
            if done_args.startswith(st.args):
                missing = done_args[len(st.args):]
                if missing:
                    yield from self._emit_tool_args_delta(st, missing)
            elif not st.args:
                yield from self._emit_tool_args_delta(st, done_args)
        if self._should_buffer_tool_args(st):
            yield from self._flush_buffered_tool_args_if_needed(st)
        else:
            yield from self._start_tool_if_needed(st)
        if not st.stopped:
            st.stopped = True
            yield _emit("content_block_stop", {"type": "content_block_stop", "index": st.block_index})

    def _update_tool_metadata(self, st: _ToolState, data: dict, item: dict, key: str) -> None:
        call_id = item.get("call_id") or item.get("id")
        if isinstance(call_id, str) and call_id:
            st.id = call_id
        name = item.get("name")
        if isinstance(name, str) and name:
            st.name = name
        self._remember_tool_key(data, item, key)

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

    def _emit_ping(self) -> Iterator[bytes]:
        # Anthropic streams may contain ping events.  Forward OpenAI keepalives
        # as Anthropic pings so Claude Code does not see a dead connection while
        # Responses spends a long time in hidden/replayed reasoning.
        if not self.state.message_started:
            yield from self._emit_message_start()
        yield _emit("ping", {"type": "ping"})

    def _stop_text_if_needed(self) -> Iterator[bytes]:
        if self.state.text_started and not self.state.text_stopped:
            self.state.text_stopped = True
            yield _emit("content_block_stop", {"type": "content_block_stop", "index": self.state.text_index})

    def _tool_state(self, key: str) -> _ToolState:
        st = self.state.tools.get(key)
        if st is None:
            st = _ToolState(key=key, block_index=self.state.alloc_index())
            self.state.tools[key] = st
        self.state.last_tool_key = key
        return st

    def _start_tool_if_needed(self, st: _ToolState) -> Iterator[bytes]:
        if st.started:
            return
        if not self.state.message_started:
            yield from self._emit_message_start()
        yield from self._stop_text_if_needed()
        st.id = st.id or _gen_id("call_")
        name = st.name or "tool"
        st.started = True
        yield _emit("content_block_start", {
            "type": "content_block_start",
            "index": st.block_index,
            "content_block": {"type": "tool_use", "id": st.id, "name": name, "input": {}},
        })

    def _should_buffer_tool_args(self, st: _ToolState) -> bool:
        return bool(self.optional_empty_string_fields_by_tool.get(st.name or ""))

    def _sanitized_tool_args_json(self, st: _ToolState) -> str:
        try:
            parsed = json.loads(st.args) if st.args else {}
        except Exception:
            return st.args
        if not isinstance(parsed, dict):
            return st.args
        normalized = common.normalize_tool_input_optional_empty_strings(
            st.name,
            parsed,
            self.optional_empty_string_fields_by_tool,
        )
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    def _emit_tool_args_delta(self, st: _ToolState, delta: str) -> Iterator[bytes]:
        if not delta:
            return
        st.args += delta
        if self._should_buffer_tool_args(st):
            return
        yield from self._start_tool_if_needed(st)
        yield _emit("content_block_delta", {
            "type": "content_block_delta",
            "index": st.block_index,
            "delta": {"type": "input_json_delta", "partial_json": delta},
        })

    def _flush_buffered_tool_args_if_needed(self, st: _ToolState) -> Iterator[bytes]:
        if not self._should_buffer_tool_args(st):
            return
        args_json = self._sanitized_tool_args_json(st)
        yield from self._start_tool_if_needed(st)
        if args_json:
            yield _emit("content_block_delta", {
                "type": "content_block_delta",
                "index": st.block_index,
                "delta": {"type": "input_json_delta", "partial_json": args_json},
            })

    def _stop_all_tools(self) -> Iterator[bytes]:
        for key in sorted(self.state.tools.keys(), key=lambda k: self.state.tools[k].block_index):
            st = self.state.tools[key]
            if not st.started:
                if self._should_buffer_tool_args(st):
                    yield from self._flush_buffered_tool_args_if_needed(st)
                else:
                    yield from self._start_tool_if_needed(st)
            if st.started and not st.stopped:
                st.stopped = True
                yield _emit("content_block_stop", {"type": "content_block_stop", "index": st.block_index})

    # ─── tool key helpers ────────────────────────────────────────

    def _key_from_item_event(self, data: dict, item: dict) -> str:
        if isinstance(data.get("output_index"), int):
            return f"oi:{data['output_index']}"
        item_id = item.get("id") or data.get("item_id")
        if isinstance(item_id, str) and item_id:
            return f"id:{item_id}"
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            return f"call:{call_id}"
        return f"fallback:{len(self.state.tools)}"

    def _remember_tool_key(self, data: dict, item: dict, key: str) -> None:
        oi = data.get("output_index")
        if isinstance(oi, int):
            self.state.output_index_to_key[oi] = key
        for raw in (item.get("id"), data.get("item_id"), item.get("call_id")):
            if isinstance(raw, str) and raw:
                self.state.item_id_to_key[raw] = key
        self.state.last_tool_key = key

    def _tool_for_delta_event(self, data: dict) -> _ToolState:
        oi = data.get("output_index")
        if isinstance(oi, int) and oi in self.state.output_index_to_key:
            return self._tool_state(self.state.output_index_to_key[oi])
        item_id = data.get("item_id")
        if isinstance(item_id, str) and item_id in self.state.item_id_to_key:
            return self._tool_state(self.state.item_id_to_key[item_id])
        if self.state.last_tool_key:
            return self._tool_state(self.state.last_tool_key)
        key = f"oi:{oi}" if isinstance(oi, int) else f"fallback:{len(self.state.tools)}"
        if isinstance(oi, int):
            self.state.output_index_to_key[oi] = key
        return self._tool_state(key)

    def get_downstream_anthropic_assistant(self) -> dict:
        blocks: list[dict[str, Any]] = []
        if self.state.text_parts:
            blocks.append({"type": "text", "text": "".join(self.state.text_parts)})
        ordered = sorted(self.state.tools.values(), key=lambda t: t.block_index)
        for st in ordered:
            if st.started:
                try:
                    parsed = json.loads(st.args) if st.args else {}
                except Exception:
                    parsed = {"_raw": st.args}
                if not isinstance(parsed, dict):
                    parsed = {"_value": parsed}
                parsed = common.normalize_tool_input_optional_empty_strings(
                    st.name,
                    parsed,
                    self.optional_empty_string_fields_by_tool,
                )
                blocks.append({"type": "tool_use", "id": st.id, "name": st.name or "tool", "input": parsed})
        return {"role": "assistant", "content": blocks}
