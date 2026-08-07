"""SSE translator: Anthropic Messages stream → OpenAI Responses stream.

Used by Phase 8 OpenAI Responses ingress → Anthropic upstream.  Narrow scope:
text and function tool calls.  Reasoning/thinking blocks are deliberately not
translated in this slice; request-side reasoning remains guarded.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .common import build_response_skeleton, build_response_usage
from .responses_to_anthropic import NamespaceToolMap
from ...protocols.usage import legacy_usage_from_anthropic_json


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
    try:
        obj = json.loads("\n".join(data_lines))
    except Exception:
        return event_name, None
    return event_name, obj if isinstance(obj, dict) else None


def _status_from_stop(stop_reason: Optional[str], *, has_tool: bool) -> tuple[str, Optional[dict]]:
    if stop_reason == "max_tokens":
        return "incomplete", {"reason": "max_output_tokens"}
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
class _ToolState:
    block_index: int
    output_index: int
    id: str = ""
    name: str = ""
    args: str = ""
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
    message_item_started: bool = False
    content_part_started: bool = False
    message_item_done: bool = False
    msg_item_id: str = ""
    sequence: int = 0
    next_output_index: int = 0
    text_output_index: int = -1
    text_parts: list[str] = field(default_factory=list)
    tools: dict[int, _ToolState] = field(default_factory=dict)
    stop_reason: Optional[str] = None
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
        self._store_api_key_name = api_key_name
        self._store_channel_key = channel_key
        self._store_current_input = current_input_items
        self._namespace_tool_map = namespace_tool_map

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
        yield from self._ensure_created()
        yield from self._close_message_item_if_needed()
        yield from self._close_all_tools()
        status, incomplete = _status_from_stop(
            self.state.stop_reason,
            has_tool=any(t.started for t in self.state.tools.values()),
        )
        resp = self._response_skeleton(status=status)
        resp["output"] = self._collect_output_items()
        resp["output_text"] = self._output_text()
        resp["usage"] = _responses_usage_from_anthropic(self.state.usage)
        if incomplete:
            resp["incomplete_details"] = incomplete
        yield _emit(f"response.{status if status != 'completed' else 'completed'}", {
            "type": f"response.{status if status != 'completed' else 'completed'}",
            "sequence_number": self.state.next_seq(),
            "response": resp,
        })
        self._save_to_store_if_configured()

    def _handle_event(self, event_name: str, data: dict) -> Iterator[bytes]:
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
            if btype == "text":
                yield from self._ensure_message_text_item()
            elif btype == "tool_use":
                idx = int(data.get("index", 0) or 0)
                st = self._tool(idx)
                st.id = str(block.get("id") or st.id or _gen_id("call_"))
                st.name = str(block.get("name") or st.name or "tool")
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
            if dt == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    yield from self._ensure_message_text_item()
                    self.state.text_parts.append(text)
                    yield _emit("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "sequence_number": self.state.next_seq(),
                        "item_id": self.state.msg_item_id,
                        "output_index": self.state.text_output_index,
                        "content_index": 0,
                        "delta": text,
                        "logprobs": [],
                    })
            elif dt == "input_json_delta":
                idx = int(data.get("index", 0) or 0)
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
            st = self.state.tools.get(idx)
            if st is not None and st.started and not st.done:
                yield from self._finish_tool(st)
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

    def _ensure_message_text_item(self) -> Iterator[bytes]:
        yield from self._ensure_created()
        if not self.state.message_item_started:
            self.state.message_item_started = True
            self.state.text_output_index = self.state.alloc_output_index()
            self.state.msg_item_id = f"msg_{self.state.resp_id}_0"
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self.state.next_seq(),
                "output_index": self.state.text_output_index,
                "item": {"id": self.state.msg_item_id, "type": "message", "status": "in_progress", "content": [], "role": "assistant"},
            })
        if not self.state.content_part_started:
            self.state.content_part_started = True
            yield _emit("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": self.state.msg_item_id,
                "output_index": self.state.text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
            })

    def _close_message_item_if_needed(self) -> Iterator[bytes]:
        if not self.state.message_item_started or self.state.message_item_done:
            return
        text = "".join(self.state.text_parts)
        if self.state.content_part_started:
            yield _emit("response.output_text.done", {
                "type": "response.output_text.done",
                "sequence_number": self.state.next_seq(),
                "item_id": self.state.msg_item_id,
                "output_index": self.state.text_output_index,
                "content_index": 0,
                "text": text,
                "logprobs": [],
            })
            yield _emit("response.content_part.done", {
                "type": "response.content_part.done",
                "sequence_number": self.state.next_seq(),
                "item_id": self.state.msg_item_id,
                "output_index": self.state.text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": text},
            })
        self.state.message_item_done = True
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": self.state.text_output_index,
            "item": self._message_output_item(),
        })

    def _tool(self, block_index: int) -> _ToolState:
        st = self.state.tools.get(block_index)
        if st is None:
            st = _ToolState(block_index=block_index, output_index=self.state.alloc_output_index())
            self.state.tools[block_index] = st
        return st

    def _finish_tool(self, st: _ToolState) -> Iterator[bytes]:
        st.done = True
        args = st.args or "{}"
        identity = self._tool_identity(st)
        if identity is not None and identity.kind == "custom":
            yield _emit("response.custom_tool_call_input.done", {
                "type": "response.custom_tool_call_input.done",
                "sequence_number": self.state.next_seq(),
                "item_id": f"fc_{st.id}",
                "output_index": st.output_index,
                "input": args,
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

    def _close_all_tools(self) -> Iterator[bytes]:
        for idx in sorted(self.state.tools.keys()):
            st = self.state.tools[idx]
            if st.started and not st.done:
                yield from self._finish_tool(st)

    def _message_output_item(self) -> dict:
        return {
            "id": self.state.msg_item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(self.state.text_parts), "annotations": []}],
        }

    def _output_text(self) -> str:
        return "".join(self.state.text_parts)

    def _tool_identity(self, st: _ToolState):
        if self._namespace_tool_map is None:
            return None
        return self._namespace_tool_map.identity_for_flat(st.name)

    def _tool_output_item(
        self, st: _ToolState, *, status: str = "completed", value: str | None = None,
    ) -> dict:
        identity = self._tool_identity(st)
        name = identity.child_name if identity is not None else (st.name or "tool")
        item = {
            "id": f"fc_{st.id}", "type": "function_call", "status": status,
            "arguments": st.args or "{}" if value is None else value,
            "call_id": st.id, "name": name,
        }
        if identity is not None and identity.namespace is not None:
            item["namespace"] = identity.namespace
        if identity is not None and identity.kind == "custom":
            item["type"] = "custom_tool_call"
            item["input"] = item.pop("arguments")
        return item

    def _collect_output_items(self) -> list[dict]:
        items: list[dict] = []
        pairs: list[tuple[int, dict]] = []
        if self.state.message_item_started:
            pairs.append((self.state.text_output_index, self._message_output_item()))
        for st in self.state.tools.values():
            if st.started:
                pairs.append((st.output_index, self._tool_output_item(st)))
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
