"""SSE 翻译器：上游 Responses 流 → 下游 Chat 流。

使用场景：chat ingress（`/v1/chat/completions` 下游）指向 openai-responses 上游。
上游输出形如 `event: response.output_text.delta\\ndata: {...}\\n\\n` 的细粒度事件，
需要还原成下游期望的 `data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}`。

状态机要点：
  - 首个 delta（文本或 tool_call）之前发一个"role chunk"（delta.role="assistant"）
  - output_item.added/done 里的 function_call：记录 output_index → chat tool_calls 的
    index 映射；首次 emit 时带 id/name/arguments=""，arguments delta/done 或
    output_item.done 的完整快照只补发尚未转发的尾部
  - response.output_text.delta；若仅有 output_text.done/content_part.done，则补发完整文本
  - response.refusal.delta；若仅有 refusal.done/content_part.done，则补发完整拒绝文本
  - response.reasoning_summary_text.delta / response.reasoning_text.delta →
    delta.reasoning_content（非官方字段；客户端不识别会忽略）
  - response.completed：收尾发 finish_reason chunk + 可选 usage chunk + [DONE]
  - response.failed / error：立即发 error 帧 + [DONE]
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from ...protocols import errors as protocol_errors
from ...protocols.sse import split_sse_events


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


# ─── 状态 ─────────────────────────────────────────────────────────


@dataclass
class R2CState:
    chunk_id: str
    model: str
    created_ts: int
    include_usage: bool = False
    role_sent: bool = False
    # function_call: responses output_index → chat tool_calls.index
    fc_output_index_to_tc_index: dict[int, int] = field(default_factory=dict)
    fc_name_by_tc_index: dict[int, str] = field(default_factory=dict)
    fc_call_id_by_tc_index: dict[int, str] = field(default_factory=dict)
    next_tc_index: int = 0
    # 下游 chat assistant 累积（供 MS-7 亲和 fingerprint_write_chat 使用）
    chat_text_parts: list = field(default_factory=list)
    # 每个 Responses output/content part 已转发的文本。终态 done 事件携带完整
    # 文本时用它做去重，并补齐上游未发 delta 的尾部。
    chat_text_by_part: dict[tuple[int, int], str] = field(default_factory=dict)
    chat_refusal_parts: list = field(default_factory=list)
    # 与文本同理：refusal.done 可能是拒绝文本唯一的载体。
    chat_refusal_by_part: dict[tuple[int, int], str] = field(default_factory=dict)
    fc_args_by_tc_index: dict[int, str] = field(default_factory=dict)
    # 02-bug-findings #35: 累积 annotation.added 事件
    annotations: list = field(default_factory=list)
    # 累积
    usage: Optional[dict] = None
    finish_reason: Optional[str] = None
    # 收尾状态（防止重复 emit）
    terminal_emitted: bool = False
    # 观察到的收尾结果（normal/error）
    terminal_status: Optional[str] = None     # completed / incomplete / failed / error
    terminal_error: Optional[dict] = None     # 若 failed / error


# ─── SSE 解析辅助 ────────────────────────────────────────────────


def _parse_event_block(block: str) -> tuple[Optional[str], Optional[dict]]:
    """把一个 `event:/data:` 块解析成 (event_name, payload_dict)。"""
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


# ─── chat chunk 构造 ────────────────────────────────────────────


def _mk_chunk(state: R2CState, *, delta: Optional[dict] = None,
              finish_reason: Optional[str] = None,
              usage: Optional[dict] = None,
              include_choice: bool = True,
              is_final_usage_chunk: bool = False,
              logprobs: Optional[dict] = None) -> bytes:
    obj: dict[str, Any] = {
        "id": state.chunk_id,
        "object": "chat.completion.chunk",
        "created": state.created_ts,
        "model": state.model,
        "choices": [],
    }
    if include_choice:
        obj["choices"] = [{
            "index": 0,
            "delta": delta or {},
            "finish_reason": finish_reason,
            "logprobs": logprobs,
        }]
    # 02-bug-findings #43: include_usage=true 时 OpenAI chat 协议要求每个 chunk
    # 都带 usage 字段，中间 chunk 为 null，最后一帧才是真值。某些 SDK
    # （如 LangChain）会基于此做存在性判断。
    if usage is not None:
        obj["usage"] = usage
    elif state.include_usage and not is_final_usage_chunk:
        obj["usage"] = None
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


_DONE = b"data: [DONE]\n\n"


def _mk_error_chunk(state: R2CState, *, message: str, err_type: str = "server_error",
                    code: Optional[str] = None, param: Optional[str] = None) -> bytes:
    """Chat 流内错误：一条裸的 error 帧（非 chat.completion.chunk）。

    02-bug-findings #7: code/param/type 全部透传，OpenAI 客户端会按 code 做分支处理。
    """
    obj = {"error": {"message": message, "type": err_type, "code": code, "param": param}}
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


# ─── Translator ──────────────────────────────────────────────────


class StreamTranslator:
    """Responses SSE → Chat SSE 翻译器。"""

    def __init__(self, *, model: str, include_usage: bool = False,
                 created_ts: Optional[int] = None):
        self.state = R2CState(
            chunk_id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            model=model,
            created_ts=int(created_ts or time.time()),
            include_usage=include_usage,
        )
        self._buf = b""

    # --- 公开接口 ---

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        if not chunk:
            return
        self._buf += chunk
        self._buf, blocks = split_sse_events(self._buf)
        for block_bytes in blocks:
            block = block_bytes.decode("utf-8", errors="replace")
            if not block.strip():
                continue
            yield from self._handle_event_block(block)

    def close(self) -> Iterator[bytes]:
        """流结束：emit 终态 chunk + [DONE]。"""
        if self.state.terminal_emitted:
            return
        self.state.terminal_emitted = True

        if self.state.terminal_status in ("failed", "error"):
            # 已在 feed 过程中发了 error + [DONE]，这里不重复；兜底：若未发则补一次
            err_msg = "upstream failure"
            err_detail: dict = {}
            if isinstance(self.state.terminal_error, dict):
                err_msg = str(self.state.terminal_error.get("message") or err_msg)
                err_detail = self.state.terminal_error.get("detail") or {}
            yield _mk_error_chunk(
                self.state, message=err_msg,
                err_type=(err_detail.get("type") if isinstance(err_detail, dict) else None) or "server_error",
                code=err_detail.get("code") if isinstance(err_detail, dict) else None,
                param=err_detail.get("param") if isinstance(err_detail, dict) else None,
            )
            yield _DONE
            return

        if self.state.terminal_status is None:
            # EOF is not a normal Responses completion.  Returning stop here
            # would make a truncated response indistinguishable from success.
            yield _mk_error_chunk(
                self.state,
                message="upstream stream ended without a terminal response event",
                err_type="server_error",
            )
            yield _DONE
            return

        # 正常收尾：finish_reason chunk（delta 为空）
        finish_reason = self.state.finish_reason or "stop"
        yield _mk_chunk(self.state, delta={}, finish_reason=finish_reason)

        # 可选 usage chunk
        if self.state.include_usage and self.state.usage is not None:
            yield _mk_chunk(
                self.state,
                include_choice=False,
                usage=_usage_resps_to_chat_stream(self.state.usage),
            )

        yield _DONE

    # --- 事件处理 ---

    def _handle_event_block(self, block: str) -> Iterator[bytes]:
        event_name, data = _parse_event_block(block)
        if event_name is None and data is None:
            return
        # 02-bug-findings #20: 一旦观察到终态（completed/incomplete/failed/error），
        # 后续任何事件（合规模型不会发，但兜底防御）都直接短路，避免改写已收尾的 state
        # 也避免在 close() 之后通过 feed() 注入新 chunk（terminal_emitted 仅由 close 设）
        if self.state.terminal_status is not None:
            return
        # responses 事件在 MS-4 首版只处理关键子集；未识别的 event 静默丢弃
        if event_name == "response.output_item.added":
            yield from self._on_output_item_added(data or {})
        elif event_name == "response.output_item.done":
            yield from self._on_output_item_done(data or {})
        elif event_name == "response.output_text.delta":
            yield from self._on_output_text_delta(data or {})
        elif event_name == "response.output_text.done":
            yield from self._on_output_text_done(data or {})
        elif event_name == "response.content_part.done":
            yield from self._on_content_part_done(data or {})
        elif event_name == "response.refusal.delta":
            yield from self._on_refusal_delta(data or {})
        elif event_name == "response.refusal.done":
            yield from self._on_refusal_done(data or {})
        elif event_name in ("response.reasoning_summary_text.delta",
                             "response.reasoning_text.delta"):
            yield from self._on_reasoning_delta(data or {})
        elif event_name == "response.function_call_arguments.delta":
            yield from self._on_fc_args_delta(data or {})
        elif event_name == "response.function_call_arguments.done":
            yield from self._on_fc_args_done(data or {})
        elif event_name == "response.output_text.annotation.added":
            yield from self._on_annotation_added(data or {})
        elif event_name == "response.completed":
            yield from self._on_completed(data or {})
        elif event_name == "response.incomplete":
            yield from self._on_incomplete(data or {})
        elif event_name in ("response.failed", "error"):
            yield from self._on_error(event_name, data or {})
        # 其他事件（response.created、response.in_progress、content_part.added、
        # reasoning_summary_part.*、reasoning_summary_text.done、web_search_call.* 等）
        # 对 chat 下游无用，忽略

    def _ensure_role_sent(self) -> Iterator[bytes]:
        if self.state.role_sent:
            return
        self.state.role_sent = True
        yield _mk_chunk(self.state, delta={"role": "assistant"})

    def _on_output_item_added(self, data: dict) -> Iterator[bytes]:
        item = data.get("item") or {}
        item_type = item.get("type")
        # 02-bug-findings #33: 上游连续 emit 多个 message item 时，
        # 下游 chat 流应每个 message 一个 role chunk 来分段；
        # 否则所有 text 会被合并到同一个 message 里、丢段落。
        if item_type == "message":
            # 第二次及以后看到 message item.added，强制再发 role chunk 让下游开新段
            if self.state.role_sent:
                # 重置标志让 _ensure_role_sent 再发一次 role chunk
                self.state.role_sent = False
                yield from self._ensure_role_sent()
            return
        if item_type == "function_call":
            yield from self._ensure_function_call_started(data, item)

    def _ensure_function_call_started(self, data: dict, item: dict) -> Iterator[bytes]:
        """Register and emit a Chat tool-call start exactly once.

        A normal Responses stream supplies this through ``output_item.added``.
        ``output_item.done`` also carries the complete item, so it can repair a
        stream that omitted the earlier item event without duplicating a normal
        tool call.
        """
        output_index = int(data.get("output_index", 0))
        if output_index in self.state.fc_output_index_to_tc_index:
            return
        tc_index = self.state.next_tc_index
        self.state.next_tc_index += 1
        self.state.fc_output_index_to_tc_index[output_index] = tc_index
        call_id = item.get("call_id") or _gen_id("call_")
        name = item.get("name") or ""
        self.state.fc_call_id_by_tc_index[tc_index] = call_id
        self.state.fc_name_by_tc_index[tc_index] = name

        # emit role chunk 在首 tool_call 之前
        yield from self._ensure_role_sent()
        yield _mk_chunk(self.state, delta={
            "tool_calls": [{
                "index": tc_index,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": ""},
            }],
        })

    def _on_output_item_done(self, data: dict) -> Iterator[bytes]:
        """Use the complete terminal item as a last-resort content snapshot."""
        item = data.get("item") or {}
        item_type = item.get("type")
        if item_type == "function_call":
            yield from self._ensure_function_call_started(data, item)
            yield from self._emit_terminal_fc_args_tail(data, item.get("arguments"))
            return
        if item_type != "message":
            return
        content = item.get("content")
        if not isinstance(content, list):
            return
        for content_index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            part_data = dict(data)
            part_data["content_index"] = content_index
            if part.get("type") == "output_text":
                yield from self._emit_terminal_text_tail(part_data, part.get("text"))
            elif part.get("type") == "refusal":
                yield from self._emit_terminal_refusal_tail(part_data, part.get("refusal"))

    @staticmethod
    def _content_key(data: dict) -> tuple[int, int]:
        """Stable key for a Responses message content part."""
        try:
            output_index = int(data.get("output_index", 0) or 0)
        except (TypeError, ValueError):
            output_index = 0
        try:
            content_index = int(data.get("content_index", 0) or 0)
        except (TypeError, ValueError):
            content_index = 0
        return output_index, content_index

    def _emit_terminal_text_tail(self, data: dict, full_text: Any) -> Iterator[bytes]:
        """Emit only the text not already delivered through delta events.

        Some valid Responses streams provide text only in ``output_text.done``
        (or its enclosing ``content_part.done``) rather than in text deltas.
        The done payload is a full snapshot, so it also safely repairs a stream
        that supplied only an initial subset of deltas.
        """
        if not isinstance(full_text, str) or not full_text:
            return
        key = self._content_key(data)
        emitted = self.state.chat_text_by_part.get(key, "")
        if not emitted:
            tail = full_text
            self.state.chat_text_by_part[key] = full_text
        elif full_text.startswith(emitted):
            tail = full_text[len(emitted):]
            self.state.chat_text_by_part[key] = full_text
        else:
            # A contradictory terminal snapshot cannot be safely merged without
            # risking duplicated or reordered client-visible text.
            return
        if not tail:
            return
        self.state.chat_text_parts.append(tail)
        yield from self._ensure_role_sent()
        yield _mk_chunk(self.state, delta={"content": tail})

    def _on_output_text_delta(self, data: dict) -> Iterator[bytes]:
        text = data.get("delta")
        if not isinstance(text, str) or not text:
            return
        key = self._content_key(data)
        self.state.chat_text_by_part[key] = self.state.chat_text_by_part.get(key, "") + text
        self.state.chat_text_parts.append(text)
        yield from self._ensure_role_sent()
        yield _mk_chunk(
            self.state,
            delta={"content": text},
            logprobs=_responses_delta_logprobs_to_chat(data.get("logprobs"), "content"),
        )

    def _on_output_text_done(self, data: dict) -> Iterator[bytes]:
        yield from self._emit_terminal_text_tail(data, data.get("text"))

    def _on_content_part_done(self, data: dict) -> Iterator[bytes]:
        part = data.get("part")
        if not isinstance(part, dict):
            return
        if part.get("type") == "output_text":
            yield from self._emit_terminal_text_tail(data, part.get("text"))
        elif part.get("type") == "refusal":
            yield from self._emit_terminal_refusal_tail(data, part.get("refusal"))

    def _emit_terminal_refusal_tail(self, data: dict, full_text: Any) -> Iterator[bytes]:
        """Emit a refusal.done/content_part.done suffix exactly once."""
        if not isinstance(full_text, str) or not full_text:
            return
        key = self._content_key(data)
        emitted = self.state.chat_refusal_by_part.get(key, "")
        if not emitted:
            tail = full_text
            self.state.chat_refusal_by_part[key] = full_text
        elif full_text.startswith(emitted):
            tail = full_text[len(emitted):]
            self.state.chat_refusal_by_part[key] = full_text
        else:
            return
        if not tail:
            return
        self.state.chat_refusal_parts.append(tail)
        yield from self._ensure_role_sent()
        yield _mk_chunk(self.state, delta={"refusal": tail})

    def _on_refusal_delta(self, data: dict) -> Iterator[bytes]:
        text = data.get("delta")
        if not isinstance(text, str) or not text:
            return
        key = self._content_key(data)
        self.state.chat_refusal_by_part[key] = self.state.chat_refusal_by_part.get(key, "") + text
        self.state.chat_refusal_parts.append(text)
        yield from self._ensure_role_sent()
        yield _mk_chunk(
            self.state,
            delta={"refusal": text},
            logprobs=_responses_delta_logprobs_to_chat(data.get("logprobs"), "refusal"),
        )

    def _on_refusal_done(self, data: dict) -> Iterator[bytes]:
        yield from self._emit_terminal_refusal_tail(data, data.get("refusal"))

    def _on_reasoning_delta(self, data: dict) -> Iterator[bytes]:
        # drop 模式：丢弃 reasoning 文本（usage.reasoning_tokens 不受影响）
        from .common import reasoning_passthrough_enabled
        if not reasoning_passthrough_enabled():
            return
        text = data.get("delta")
        if not isinstance(text, str) or not text:
            return
        yield from self._ensure_role_sent()
        # 非官方字段：兼容客户端会忽略；DeepSeek 系列客户端能拾取
        yield _mk_chunk(self.state, delta={"reasoning_content": text})

    def _on_fc_args_delta(self, data: dict) -> Iterator[bytes]:
        output_index = int(data.get("output_index", 0))
        tc_index = self.state.fc_output_index_to_tc_index.get(output_index)
        if tc_index is None:
            return  # 在 output_item.added 之前出现的孤儿 delta，丢弃
        text = data.get("delta")
        if not isinstance(text, str):
            return
        self.state.fc_args_by_tc_index[tc_index] = (
            self.state.fc_args_by_tc_index.get(tc_index, "") + text
        )
        yield _mk_chunk(self.state, delta={
            "tool_calls": [{
                "index": tc_index,
                "function": {"arguments": text},
            }],
        })

    def _emit_terminal_fc_args_tail(self, data: dict, full_args: Any) -> Iterator[bytes]:
        """Emit only function-call arguments not already delivered by deltas."""
        if not isinstance(full_args, str):
            return
        output_index = int(data.get("output_index", 0))
        tc_index = self.state.fc_output_index_to_tc_index.get(output_index)
        if tc_index is None:
            return
        emitted = self.state.fc_args_by_tc_index.get(tc_index, "")
        if not full_args.startswith(emitted):
            # A contradictory final snapshot cannot be appended without
            # corrupting the JSON argument stream sent to the client.
            return
        tail = full_args[len(emitted):]
        self.state.fc_args_by_tc_index[tc_index] = full_args
        if not tail:
            return
        yield _mk_chunk(self.state, delta={
            "tool_calls": [{
                "index": tc_index,
                "function": {"arguments": tail},
            }],
        })

    def _on_fc_args_done(self, data: dict) -> Iterator[bytes]:
        yield from self._emit_terminal_fc_args_tail(data, data.get("arguments"))

    def _on_annotation_added(self, data: dict) -> Iterator[bytes]:
        """02-bug-findings #35: 累积 annotation 到 state；chat 流没有
        annotation 增量事件，annotation 在 close() 之前由 get_downstream_chat_assistant
        汇总到 message.annotations（供下游 buf 用，例如 failover fingerprint_write_chat）。
        chat SSE 协议无对应增量事件，此处不主动 yield。
        """
        ann = data.get("annotation")
        if isinstance(ann, dict):
            self.state.annotations.append(ann)
        return
        yield  # noqa: keep generator

    def _on_completed(self, data: dict) -> Iterator[bytes]:
        resp = data.get("response") or {}
        self.state.terminal_status = "completed"
        self.state.usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
        fallback = "tool_calls" if self.state.fc_output_index_to_tc_index else "stop"
        self.state.finish_reason = _finish_reason_for_responses(resp, fallback=fallback)
        # An empty but completed Responses result is valid.  Emit a normal Chat
        # role chunk so the commit gate does not turn it into a fake pre-first-
        # chunk 503; close() then supplies stop + [DONE].
        if not self.state.role_sent:
            yield from self._ensure_role_sent()

    def _on_incomplete(self, data: dict) -> Iterator[bytes]:
        resp = data.get("response") or {}
        self.state.terminal_status = "incomplete"
        self.state.usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
        incomplete = resp.get("incomplete_details") or {}
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        if protocol_errors.is_responses_max_output_incomplete(data, "response.incomplete"):
            msg = protocol_errors.responses_max_output_context_error_message(reason)
            self.state.terminal_status = "error"
            self.state.terminal_error = {
                "message": msg,
                "detail": {
                    "type": "invalid_request_error",
                    "code": protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                    "param": None,
                },
            }
            self.state.terminal_emitted = True
            yield _mk_error_chunk(
                self.state,
                message=msg,
                err_type="invalid_request_error",
                code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                param=None,
            )
            yield _DONE
        elif reason == "content_filter":
            self.state.finish_reason = "content_filter"
            if not self.state.role_sent:
                yield from self._ensure_role_sent()
        else:
            self.state.finish_reason = "stop"
            if not self.state.role_sent:
                # A non-error incomplete terminal event may legitimately carry
                # no content.  It still represents a normal Chat stream result.
                yield from self._ensure_role_sent()

    def get_downstream_chat_assistant(self) -> dict:
        """累积至今的下游 chat `assistant` message 快照。

        形状与 `upstream.ChatSSEAssistantBuilder.get_assistant` 对齐，供
        failover 里的 fingerprint_write_chat 做亲和写入使用。本身不带状态
        副作用；流未结束时调用给出的是"到目前为止"的快照。
        """
        msg: dict = {"role": "assistant"}
        content = "".join(self.state.chat_text_parts)
        msg["content"] = content if content else None
        if self.state.chat_refusal_parts:
            msg["refusal"] = "".join(self.state.chat_refusal_parts)
        if self.state.annotations:
            msg["annotations"] = list(self.state.annotations)
        if self.state.fc_output_index_to_tc_index:
            tcs: list[dict] = []
            for tc_index in sorted(set(self.state.fc_output_index_to_tc_index.values())):
                tcs.append({
                    "id": self.state.fc_call_id_by_tc_index.get(tc_index, ""),
                    "type": "function",
                    "function": {
                        "name": self.state.fc_name_by_tc_index.get(tc_index, ""),
                        "arguments": self.state.fc_args_by_tc_index.get(tc_index, ""),
                    },
                })
            if tcs:
                msg["tool_calls"] = tcs
        return msg

    def _on_error(self, event_name: str, data: dict) -> Iterator[bytes]:
        # response.failed 的 payload 里 response.error.{message,code,...}
        # error 事件的 payload 直接 {type:"error", message, code, ...}
        msg = "upstream error"
        err_body: dict = {}
        if event_name == "response.failed":
            resp = data.get("response") or {}
            err_body = resp.get("error") or {}
            msg = str(err_body.get("message") or msg)
        else:  # "error"
            err_body = data
            msg = str(data.get("message") or msg)
        self.state.terminal_status = "failed" if event_name == "response.failed" else "error"
        self.state.terminal_error = {"message": msg, "detail": err_body}

        # 02-bug-findings #7: 透传 code/param/type
        err_type_raw = err_body.get("type") if isinstance(err_body, dict) else None
        err_code = err_body.get("code") if isinstance(err_body, dict) else None
        err_param = err_body.get("param") if isinstance(err_body, dict) else None
        # 立即 emit error + [DONE]，并锁 terminal_emitted 防止 close() 重复
        self.state.terminal_emitted = True
        yield _mk_error_chunk(
            self.state, message=msg,
            err_type=err_type_raw or "server_error",
            code=err_code, param=err_param,
        )
        yield _DONE


# ─── 辅助 ────────────────────────────────────────────────────────


def _finish_reason_for_responses(resp: dict, *, fallback: str) -> str:
    status = resp.get("status")
    if status == "incomplete":
        incomplete = resp.get("incomplete_details") or {}
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return fallback
    if status == "completed":
        output = resp.get("output") or []
        if isinstance(output, list):
            for it in output:
                if isinstance(it, dict) and it.get("type") == "function_call":
                    return "tool_calls"
        # Detailed item events may contain a function call while the completed
        # snapshot omits output.  Keep the caller's state-derived fallback
        # rather than incorrectly labelling that tool turn as stop.
        return fallback
    if status in ("failed", "cancelled"):
        return fallback
    return fallback


def _usage_resps_to_chat_stream(u: dict) -> dict:
    # 02-bug-findings #9: details fields must always be written.
    from .common import build_chat_usage
    input_tokens = int(u.get("input_tokens", 0) or 0)
    output_tokens = int(u.get("output_tokens", 0) or 0)
    total = int(u.get("total_tokens", input_tokens + output_tokens) or 0)
    in_details = u.get("input_tokens_details") or {}
    out_details = u.get("output_tokens_details") or {}
    cached = int(in_details.get("cached_tokens", 0) or 0)
    reasoning = int(out_details.get("reasoning_tokens", 0) or 0)
    return build_chat_usage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )


def _responses_delta_logprobs_to_chat(raw: Any, key: str) -> Optional[dict[str, Any]]:
    if not isinstance(raw, list):
        return None
    clean = [item for item in raw if isinstance(item, dict)]
    if not clean:
        return None
    if key == "refusal":
        return {"content": None, "refusal": clean}
    return {"content": clean, "refusal": None}
