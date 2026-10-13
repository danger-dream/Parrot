"""SSE translator: Anthropic Messages stream → OpenAI Responses stream.

Used by OpenAI Responses ingress → Anthropic upstream. Readable thinking is
exposed as Responses reasoning summaries under the existing reasoning bridge
policy. Native signatures/redacted payloads use tagged opaque replay carriers.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .common import build_response_skeleton, build_response_usage, reasoning_passthrough_enabled
from .responses_to_anthropic import NamespaceToolMap, _CUSTOM_RAW_KEY
from .. import anthropic_reasoning_replay as native_replay
from ...protocols.usage import legacy_usage_from_anthropic_json
from ...protocols.sse import split_sse_events


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def _emit(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def _parse_event_block(block: str) -> tuple[Optional[str], Optional[dict]]:
    event_name: Optional[str] = None
    data_lines: list[str] = []
    for line in block.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
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


def _status_from_stop(stop_reason: Optional[str], *, has_tool: bool) -> tuple[str, Optional[dict]]:
    if stop_reason in {"max_tokens", "model_context_window_exceeded"}:
        return "incomplete", {"reason": "max_output_tokens"}
    if stop_reason == "pause_turn":
        return "incomplete", {"reason": "pause_turn"}
    return "completed", None


def _responses_usage_from_anthropic(usage: Optional[dict]) -> dict:
    legacy = legacy_usage_from_anthropic_json({"usage": usage or {}})
    prompt_tokens = legacy["input_tokens"] + legacy["cache_creation"] + legacy["cache_read"]
    return build_response_usage(
        input_tokens=prompt_tokens,
        output_tokens=legacy["output_tokens"],
        cached_tokens=legacy["cache_read"],
        reasoning_tokens=0,
        total_tokens=prompt_tokens + legacy["output_tokens"],
    )


def _merge_anthropic_usage(existing: Optional[dict], update: dict) -> dict:
    """Merge Anthropic stream usage without dropping message_start tokens."""
    merged = dict(existing or {})
    for key, value in (update or {}).items():
        if key in {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        }:
            try:
                incoming = int(value or 0)
            except Exception:
                incoming = 0
            try:
                current = int(merged.get(key) or 0)
            except Exception:
                current = 0
            # Keep the largest cumulative value seen so output-only or
            # zero-filled message_delta usage cannot erase message_start
            # prompt/cache accounting.
            merged[key] = max(current, incoming)
            continue
        merged[key] = value
    return merged


@dataclass
class _ReasoningState:
    item_id: str
    output_index: int
    text_parts: list[str] = field(default_factory=list)
    native_block: dict = field(default_factory=dict)
    native_complete: bool = False
    summary_started: bool = False
    done: bool = False


@dataclass
class _TextState:
    item_id: str
    output_index: int
    text_parts: list[str] = field(default_factory=list)
    done: bool = False


@dataclass
class _ToolState:
    block_index: int
    output_index: int
    id: str = ""
    name: str = ""
    args: str = ""
    initial_input: dict = field(default_factory=dict)
    started: bool = False
    done: bool = False


@dataclass
class _State:
    resp_id: str
    model: str
    created_ts: int
    previous_response_id: Optional[str] = None
    request_body: Optional[dict] = None
    created_emitted: bool = False
    terminal_emitted: bool = False
    sequence: int = 0
    next_output_index: int = 0
    messages: dict[int, _TextState] = field(default_factory=dict)
    tools: dict[int, _ToolState] = field(default_factory=dict)
    reasoning: dict[int, _ReasoningState] = field(default_factory=dict)
    stop_reason: Optional[str] = None
    message_stopped: bool = False
    usage: Optional[dict] = None

    def next_seq(self) -> int:
        self.sequence += 1
        return self.sequence

    def alloc_output_index(self) -> int:
        idx = self.next_output_index
        self.next_output_index += 1
        return idx


class StreamTranslator:
    """Anthropic SSE → Responses SSE."""

    def __init__(
        self,
        *,
        model: str,
        previous_response_id: Optional[str] = None,
        api_key_name: Optional[str] = None,
        channel_key: Optional[str] = None,
        current_input_items: Optional[list] = None,
        request_body: Optional[dict] = None,
        namespace_tool_map: NamespaceToolMap | None = None,
        allow_unsigned_thinking: bool = False,
        created_ts: Optional[int] = None,
    ):
        self.state = _State(
            resp_id=_gen_id("resp_"),
            model=model,
            created_ts=int(created_ts or time.time()),
            previous_response_id=previous_response_id,
            request_body=request_body,
        )
        self._buf = b""
        self._hosted_blocks: dict[int, dict] = {}
        self._hosted_args: dict[int, str] = {}
        self._hosted_items: dict[str, tuple[int, dict]] = {}
        self._store_api_key_name = api_key_name
        self._store_channel_key = channel_key
        self._store_current_input = current_input_items
        self._namespace_tool_map = namespace_tool_map
        self._reasoning_enabled = reasoning_passthrough_enabled()
        self._preserve_native = self._reasoning_enabled and not allow_unsigned_thinking
        self._invalid_tool = False

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        if not chunk:
            return
        self._buf, blocks = split_sse_events(self._buf + chunk)
        for block_bytes in blocks:
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
        yield from self._ensure_created()
        pending = [
            (st.output_index, self._finish_reasoning(st))
            for st in self.state.reasoning.values() if not st.done
        ]
        pending.extend(
            (st.output_index, self._close_message_item_if_needed(st))
            for st in self.state.messages.values() if not st.done
        )
        pending.extend(
            (st.output_index, self._finish_tool(st))
            for st in self.state.tools.values() if st.started and not st.done
        )
        for _, events in sorted(pending, key=lambda pair: pair[0]):
            yield from events
        if self.state.terminal_emitted and self._invalid_tool:
            return
        status, incomplete = _status_from_stop(
            self.state.stop_reason,
            has_tool=any(t.started for t in self.state.tools.values()),
        )
        if self.state.stop_reason is None or not self.state.message_stopped or self._buf.strip():
            status, incomplete = "failed", None
        resp = self._response_skeleton(status=status)
        resp["output"] = self._collect_output_items()
        resp["output_text"] = self._output_text()
        resp["usage"] = _responses_usage_from_anthropic(self.state.usage)
        if incomplete:
            resp["incomplete_details"] = incomplete
        if status == "failed":
            resp["error"] = {"type": "upstream_error", "code": "incomplete_stream", "message": "Anthropic stream ended without a complete message_stop"}
        yield _emit(f"response.{status}", {
            "type": f"response.{status}",
            "sequence_number": self.state.next_seq(),
            "response": resp,
        })
        if status != "failed":
            self._save_to_store_if_configured()

    def _handle_event(self, event_name: str, data: dict) -> Iterator[bytes]:
        if self.state.terminal_emitted:
            return
        typ = str(data.get("type") or event_name or "")
        if typ == "error" or isinstance(data.get("error"), dict):
            yield from self._ensure_created()
            err = data.get("error") if isinstance(data.get("error"), dict) else data
            yield _emit("response.failed", {
                "type": "response.failed",
                "sequence_number": self.state.next_seq(),
                "response": {**self._response_skeleton(status="failed"), "error": err, "output": self._collect_output_items()},
            })
            self.state.terminal_emitted = True
            return

        if typ == "message_start":
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            if isinstance(msg.get("model"), str) and msg.get("model"):
                self.state.model = msg["model"]
            if isinstance(msg.get("usage"), dict):
                self.state.usage = _merge_anthropic_usage(self.state.usage, msg["usage"])
            yield from self._ensure_created()
            return

        if typ == "content_block_start":
            block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
            btype = block.get("type")
            from ... import search_hosted_codec
            if search_hosted_codec.is_anthropic_search(block):
                idx = int(data.get("index", 0) or 0)
                self._hosted_blocks[idx] = dict(block)
                yield from self._ensure_created()
                for item in search_hosted_codec.anthropic_to_responses(list(self._hosted_blocks.values())):
                    key = str(item.get("id") or "")
                    previous = self._hosted_items.get(key)
                    output_index = previous[0] if previous else self.state.alloc_output_index()
                    self._hosted_items[key] = (output_index, item)
                    if not previous:
                        yield _emit("response.output_item.added", {"type": "response.output_item.added", "sequence_number": self.state.next_seq(), "output_index": output_index, "item": item})
                    if btype == "web_search_tool_result" and item.get("id") == block.get("tool_use_id"):
                        yield _emit("response.output_item.done", {"type": "response.output_item.done", "sequence_number": self.state.next_seq(), "output_index": output_index, "item": item})
                return
            if btype in ("thinking", "redacted_thinking") and self._preserve_native:
                yield from self._start_reasoning(int(data.get("index", 0) or 0), block)
            if btype == "thinking":
                yield from self._emit_reasoning(int(data.get("index", 0) or 0), block.get("thinking"))
            elif btype == "text":
                yield from self._emit_text(int(data.get("index", 0) or 0), block.get("text"))
            elif btype == "tool_use":
                idx = int(data.get("index", 0) or 0)
                st = self._tool(idx)
                st.id = str(block.get("id") or st.id or _gen_id("call_"))
                st.name = str(block.get("name") or st.name or "tool")
                st.initial_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                st.started = True
                yield from self._ensure_created()
                yield _emit("response.output_item.added", {
                    "type": "response.output_item.added",
                    "sequence_number": self.state.next_seq(),
                    "output_index": st.output_index,
                    "item": self._tool_output_item(st, status="in_progress", value=""),
                })
            return

        if typ == "content_block_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            dt = delta.get("type")
            if dt == "thinking_delta":
                yield from self._emit_reasoning(int(data.get("index", 0) or 0), delta.get("thinking"))
            elif dt == "signature_delta":
                st = self.state.reasoning.get(int(data.get("index", 0) or 0))
                if self._preserve_native and st is not None and not st.done and isinstance(delta.get("signature"), str):
                    st.native_block["signature"] = st.native_block.get("signature", "") + delta["signature"]
            elif dt == "text_delta":
                yield from self._emit_text(int(data.get("index", 0) or 0), delta.get("text"))
            elif dt == "input_json_delta":
                idx = int(data.get("index", 0) or 0)
                if idx in self._hosted_blocks:
                    self._hosted_args[idx] = self._hosted_args.get(idx, "") + str(delta.get("partial_json") or "")
                    try:
                        self._hosted_blocks[idx]["input"] = json.loads(self._hosted_args[idx])
                    except ValueError:
                        pass
                    return
                st = self._tool(idx)
                part = delta.get("partial_json")
                if isinstance(part, str) and part:
                    st.args += part
                    identity = self._tool_identity(st)
                    event = (
                        "response.custom_tool_call_input.delta"
                        if identity is not None and identity.kind == "custom"
                        else "response.function_call_arguments.delta"
                    )
                    if identity is not None and identity.kind == "custom" and self._is_raw_custom(st):
                        return  # JSON string escaping is not a raw-text delta; emit decoded text at block end.
                    yield _emit(event, {
                        "type": event,
                        "sequence_number": self.state.next_seq(),
                        "item_id": f"fc_{st.id or 'call'}",
                        "output_index": st.output_index,
                        "delta": part,
                    })
            return

        if typ == "content_block_stop":
            idx = int(data.get("index", 0) or 0)
            message = self.state.messages.get(idx)
            if message is not None:
                yield from self._close_message_item_if_needed(message)
            reasoning = self.state.reasoning.get(idx)
            if reasoning is not None:
                reasoning.native_complete = True
                yield from self._finish_reasoning(reasoning)
            st = self.state.tools.get(idx)
            if st is not None and st.started and not st.done:
                yield from self._finish_tool(st)
            return

        if typ == "message_stop":
            self.state.message_stopped = True
            return

        if typ == "message_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if isinstance(delta.get("stop_reason"), str):
                self.state.stop_reason = delta["stop_reason"]
            if isinstance(data.get("usage"), dict):
                self.state.usage = _merge_anthropic_usage(self.state.usage, data["usage"])
            return

    def _ensure_created(self) -> Iterator[bytes]:
        if self.state.created_emitted:
            return
        self.state.created_emitted = True
        created = self._response_skeleton(status="in_progress")
        yield _emit("response.created", {"type": "response.created", "sequence_number": self.state.next_seq(), "response": created})
        yield _emit("response.in_progress", {"type": "response.in_progress", "sequence_number": self.state.next_seq(), "response": created})

    def _emit_text(self, block_index: int, text: Any) -> Iterator[bytes]:
        yield from self._ensure_created()
        st = self.state.messages.get(block_index)
        if st is None:
            # Each Anthropic text block owns one Responses message. A later text
            # block must not be appended across an intervening tool/reasoning item.
            st = _TextState(
                item_id=f"msg_{self.state.resp_id}_{block_index}",
                output_index=self.state.alloc_output_index(),
            )
            self.state.messages[block_index] = st
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self.state.next_seq(),
                "output_index": st.output_index,
                "item": {"id": st.item_id, "type": "message", "status": "in_progress", "content": [], "role": "assistant"},
            })
            yield _emit("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": st.item_id,
                "output_index": st.output_index,
                "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
            })
        if isinstance(text, str) and text and not st.done:
            st.text_parts.append(text)
            yield _emit("response.output_text.delta", {
                "type": "response.output_text.delta",
                "sequence_number": self.state.next_seq(),
                "item_id": st.item_id,
                "output_index": st.output_index,
                "content_index": 0,
                "delta": text,
                "logprobs": [],
            })

    def _close_message_item_if_needed(self, st: _TextState) -> Iterator[bytes]:
        if st.done:
            return
        st.done = True
        text = "".join(st.text_parts)
        yield _emit("response.output_text.done", {
            "type": "response.output_text.done",
            "sequence_number": self.state.next_seq(),
            "item_id": st.item_id,
            "output_index": st.output_index,
            "content_index": 0,
            "text": text,
            "logprobs": [],
        })
        yield _emit("response.content_part.done", {
            "type": "response.content_part.done",
            "sequence_number": self.state.next_seq(),
            "item_id": st.item_id,
            "output_index": st.output_index,
            "content_index": 0,
            "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": text},
        })
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": st.output_index,
            "item": self._message_output_item(st),
        })

    def _start_reasoning(self, block_index: int, block: dict) -> Iterator[bytes]:
        if block_index in self.state.reasoning:
            return
        yield from self._ensure_created()
        native = copy.deepcopy(block)
        if native.get("type") == "thinking":
            native["thinking"] = ""
        st = _ReasoningState(item_id=_gen_id("rs_"), output_index=self.state.alloc_output_index(), native_block=native)
        self.state.reasoning[block_index] = st
        yield _emit("response.output_item.added", {
            "type": "response.output_item.added", "sequence_number": self.state.next_seq(),
            "output_index": st.output_index, "item": {"type": "reasoning", "id": st.item_id, "summary": []},
        })

    def _emit_reasoning(self, block_index: int, text: Any) -> Iterator[bytes]:
        if (not self._reasoning_enabled and not self._preserve_native) or not isinstance(text, str) or not text:
            return
        yield from self._start_reasoning(block_index, {"type": "thinking", "thinking": ""})
        st = self.state.reasoning[block_index]
        if st.done:
            return
        st.native_block["thinking"] = st.native_block.get("thinking", "") + text
        if not self._reasoning_enabled:
            return
        if not st.summary_started:
            st.summary_started = True
            yield _emit("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": st.item_id,
                "output_index": st.output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })
        st.text_parts.append(text)
        yield _emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta",
            "sequence_number": self.state.next_seq(),
            "item_id": st.item_id,
            "output_index": st.output_index,
            "summary_index": 0,
            "delta": text,
        })

    def _reasoning_output_item(self, st: _ReasoningState) -> dict:
        item = {
            "type": "reasoning", "id": st.item_id,
            "status": "completed" if st.done else "in_progress",
            "summary": [{"type": "summary_text", "text": "".join(st.text_parts)}] if st.summary_started else [],
        }
        carrier = native_replay.encode(st.native_block) if self._preserve_native and st.native_complete else None
        if carrier:
            item["encrypted_content"] = carrier
        return item

    def _finish_reasoning(self, st: _ReasoningState) -> Iterator[bytes]:
        if st.done:
            return
        st.done = True
        if st.summary_started:
            yield from self._finish_reasoning_summary(st)
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done", "sequence_number": self.state.next_seq(),
            "output_index": st.output_index, "item": self._reasoning_output_item(st),
        })

    def _finish_reasoning_summary(self, st: _ReasoningState) -> Iterator[bytes]:
        text = "".join(st.text_parts)
        yield _emit("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done",
            "sequence_number": self.state.next_seq(),
            "item_id": st.item_id,
            "output_index": st.output_index,
            "summary_index": 0,
            "text": text,
        })
        yield _emit("response.reasoning_summary_part.done", {
            "type": "response.reasoning_summary_part.done",
            "sequence_number": self.state.next_seq(),
            "item_id": st.item_id,
            "output_index": st.output_index,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": text},
        })

    def _tool(self, block_index: int) -> _ToolState:
        st = self.state.tools.get(block_index)
        if st is None:
            st = _ToolState(block_index=block_index, output_index=self.state.alloc_output_index())
            self.state.tools[block_index] = st
        return st

    def _finish_tool(self, st: _ToolState) -> Iterator[bytes]:
        st.done = True
        args = st.args or json.dumps(st.initial_input, ensure_ascii=False, separators=(",", ":"))
        identity = self._tool_identity(st)
        if identity is not None and identity.kind == "custom" and self._is_raw_custom(st):
            try:
                wrapped = json.loads(args)
                if not isinstance(wrapped, dict) or not isinstance(wrapped.get(_CUSTOM_RAW_KEY), str):
                    raise ValueError("missing raw input")
                raw = wrapped[_CUSTOM_RAW_KEY]
            except (ValueError, TypeError):
                st.done = False
                self._invalid_tool = True
                self.state.terminal_emitted = True
                yield _emit("response.failed", {"type": "response.failed", "sequence_number": self.state.next_seq(),
                    "response": {**self._response_skeleton(status="failed"), "output": self._collect_output_items(),
                    "error": {"code": "invalid_tool_input", "message": "Claude custom tool input was not a raw-text wrapper"}}})
                return
            yield _emit("response.custom_tool_call_input.delta", {
                "type": "response.custom_tool_call_input.delta", "sequence_number": self.state.next_seq(),
                "item_id": f"fc_{st.id}", "output_index": st.output_index, "delta": raw,
            })
            yield _emit("response.custom_tool_call_input.done", {
                "type": "response.custom_tool_call_input.done",
                "sequence_number": self.state.next_seq(),
                "item_id": f"fc_{st.id}",
                "output_index": st.output_index,
                "input": raw,
            })
        elif identity is not None and identity.kind == "custom":
            yield _emit("response.custom_tool_call_input.done", {
                "type": "response.custom_tool_call_input.done", "sequence_number": self.state.next_seq(),
                "item_id": f"fc_{st.id}", "output_index": st.output_index, "input": args,
            })
        else:
            yield _emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "sequence_number": self.state.next_seq(),
                "item_id": f"fc_{st.id}",
                "output_index": st.output_index,
                "arguments": args,
                "name": identity.child_name if identity is not None else (st.name or "tool"),
            })
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": st.output_index,
            "item": self._tool_output_item(st),
        })

    def _message_output_item(self, st: _TextState) -> dict:
        return {
            "id": st.item_id,
            "type": "message",
            "status": "completed" if st.done else "in_progress",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(st.text_parts), "annotations": []}],
        }

    def _output_text(self) -> str:
        return "".join(
            "".join(st.text_parts)
            for st in sorted(self.state.messages.values(), key=lambda value: value.output_index)
        )

    def _tool_identity(self, st: _ToolState):
        if self._namespace_tool_map is None:
            return None
        return self._namespace_tool_map.identity_for_flat(st.name)

    def _is_raw_custom(self, st: _ToolState) -> bool:
        return self._namespace_tool_map is not None and st.name in self._namespace_tool_map.raw_custom_flats

    def _tool_output_item(
        self, st: _ToolState, *, status: str = "completed", value: str | None = None,
    ) -> dict:
        identity = self._tool_identity(st)
        name = identity.child_name if identity is not None else (st.name or "tool")
        item = {
            "id": f"fc_{st.id}", "type": "function_call", "status": status,
            "arguments": (st.args or json.dumps(st.initial_input, ensure_ascii=False, separators=(",", ":"))) if value is None else value,
            "call_id": st.id, "name": name,
        }
        if identity is not None and identity.namespace is not None:
            item["namespace"] = identity.namespace
        if identity is not None and identity.kind == "custom":
            item["type"] = "custom_tool_call"
            wrapped = item.pop("arguments")
            if not self._is_raw_custom(st):
                item["input"] = wrapped
            elif status == "in_progress":
                item["input"] = ""
            else:
                try:
                    decoded = json.loads(wrapped)
                    item["input"] = decoded[_CUSTOM_RAW_KEY] if isinstance(decoded, dict) and isinstance(decoded.get(_CUSTOM_RAW_KEY), str) else ""
                except (ValueError, TypeError):
                    item["input"] = ""
        return item

    def _collect_output_items(self) -> list[dict]:
        items: list[dict] = []
        pairs: list[tuple[int, dict]] = list(self._hosted_items.values())
        pairs.extend((st.output_index, self._reasoning_output_item(st)) for st in self.state.reasoning.values())
        pairs.extend((st.output_index, self._message_output_item(st)) for st in self.state.messages.values())
        for st in self.state.tools.values():
            if st.started:
                pairs.append((st.output_index, self._tool_output_item(
                    st, status="completed" if st.done else "in_progress")))
        for _, item in sorted(pairs, key=lambda x: x[0]):
            items.append(item)
        return items

    def _response_skeleton(self, *, status: str) -> dict:
        return build_response_skeleton(
            resp_id=self.state.resp_id,
            model=self.state.model,
            created_at=self.state.created_ts,
            status=status,
            previous_response_id=self.state.previous_response_id,
            request_body=self.state.request_body,
        )

    def _save_to_store_if_configured(self) -> None:
        if not self._store_api_key_name or self._store_current_input is None:
            return
        try:
            from .. import store as _store
            if not _store.is_enabled():
                return
            _store.save(
                response_id=self.state.resp_id,
                parent_id=self.state.previous_response_id,
                api_key_name=self._store_api_key_name,
                model=self.state.model,
                channel_key=self._store_channel_key,
                input_items=self._store_current_input,
                output_items=self._collect_output_items(),
            )
        except Exception as exc:
            import traceback as _tb
            _tb.print_exc()
            from ... import notifier as _notifier
            ek = _notifier.escape_html
            _notifier.throttled_notify_event_sync(
                "openai_store_save_failed",
                f"openai_store_save_failed:{self._store_api_key_name}",
                f"❌ {_notifier.provider_custom_emoji_html('openai')} <b>OpenAI Store 写入失败</b>（流式 Anthropic→Responses）\n"
                f"API Key: <code>{ek(self._store_api_key_name)}</code>\n"
                f"模型: <code>{ek(self.state.model)}</code> · 渠道: <code>{ek(self._store_channel_key or '?')}</code>\n"
                f"resp_id: <code>{ek(self.state.resp_id)}</code>\n"
                f"原因: <code>{ek(str(exc))[:300]}</code>",
            )

    def get_downstream_responses_output(self) -> list[dict]:
        return self._collect_output_items()
