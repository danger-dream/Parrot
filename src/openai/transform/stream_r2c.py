"""SSE 翻译器：上游 Responses 流 → 下游 Chat 流。

使用场景：chat ingress（`/v1/chat/completions` 下游）指向 openai-responses 上游。
上游输出形如 `event: response.output_text.delta\\ndata: {...}\\n\\n` 的细粒度事件，
需要还原成下游期望的 `data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}`。

状态机要点：
  - 首个 delta（文本或 tool_call）之前发一个"role chunk"（delta.role="assistant"）
  - output_item.added 里的 function_call：记录 output_index → chat tool_calls 的
    index 映射；首次 emit 时带 id/name/arguments=""，后续 arguments delta 只带
    index + function.arguments
  - response.output_text.delta → delta.content
  - response.reasoning_summary_text.delta / response.reasoning_text.delta →
    delta.reasoning_content（非官方字段；客户端不识别会忽略）
  - response.refusal.delta → delta.refusal
  - response.completed：收尾发 finish_reason chunk + 可选 usage chunk + [DONE]
  - response.failed / error：立即发 error 帧 + [DONE]
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


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
              include_choice: bool = True) -> bytes:
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
            "logprobs": None,
        }]
    if usage is not None:
        obj["usage"] = usage
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


_DONE = b"data: [DONE]\n\n"


def _mk_error_chunk(state: R2CState, *, message: str, err_type: str = "server_error") -> bytes:
    """Chat 流内错误：一条裸的 error 帧（非 chat.completion.chunk）。"""
    obj = {"error": {"message": message, "type": err_type, "code": None, "param": None}}
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
        while b"\n\n" in self._buf:
            block_bytes, self._buf = self._buf.split(b"\n\n", 1)
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
            if isinstance(self.state.terminal_error, dict):
                err_msg = str(self.state.terminal_error.get("message") or err_msg)
            yield _mk_error_chunk(self.state, message=err_msg)
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
        # responses 事件在 MS-4 首版只处理关键子集；未识别的 event 静默丢弃
        if event_name == "response.output_item.added":
            yield from self._on_output_item_added(data or {})
        elif event_name == "response.output_text.delta":
            yield from self._on_output_text_delta(data or {})
        elif event_name == "response.refusal.delta":
            yield from self._on_refusal_delta(data or {})
        elif event_name in ("response.reasoning_summary_text.delta",
                             "response.reasoning_text.delta"):
            yield from self._on_reasoning_delta(data or {})
        elif event_name == "response.function_call_arguments.delta":
            yield from self._on_fc_args_delta(data or {})
        elif event_name == "response.completed":
            yield from self._on_completed(data or {})
        elif event_name == "response.incomplete":
            yield from self._on_incomplete(data or {})
        elif event_name in ("response.failed", "error"):
            yield from self._on_error(event_name, data or {})
        # 其他事件（response.created、response.in_progress、output_item.done、
        # content_part.added/done、output_text.done、reasoning_summary_part.*、
        # reasoning_summary_text.done、function_call_arguments.done、
        # web_search_call.* 等）对 chat 下游无用，忽略

    def _ensure_role_sent(self) -> Iterator[bytes]:
        if self.state.role_sent:
            return
        self.state.role_sent = True
        yield _mk_chunk(self.state, delta={"role": "assistant"})

    def _on_output_item_added(self, data: dict) -> Iterator[bytes]:
        item = data.get("item") or {}
        if item.get("type") != "function_call":
            return
        output_index = int(data.get("output_index", 0))
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

    def _on_output_text_delta(self, data: dict) -> Iterator[bytes]:
        text = data.get("delta")
        if not isinstance(text, str) or not text:
            return
        yield from self._ensure_role_sent()
        yield _mk_chunk(self.state, delta={"content": text})

    def _on_refusal_delta(self, data: dict) -> Iterator[bytes]:
        text = data.get("delta")
        if not isinstance(text, str) or not text:
            return
        yield from self._ensure_role_sent()
        yield _mk_chunk(self.state, delta={"refusal": text})

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
        yield _mk_chunk(self.state, delta={
            "tool_calls": [{
                "index": tc_index,
                "function": {"arguments": text},
            }],
        })

    def _on_completed(self, data: dict) -> Iterator[bytes]:
        resp = data.get("response") or {}
        self.state.terminal_status = "completed"
        self.state.usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
        self.state.finish_reason = _finish_reason_for_responses(resp, fallback="stop")
        # 不主动 emit 帧：由 close() 统一发
        return
        yield  # noqa: make this a generator

    def _on_incomplete(self, data: dict) -> Iterator[bytes]:
        resp = data.get("response") or {}
        self.state.terminal_status = "incomplete"
        self.state.usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
        incomplete = resp.get("incomplete_details") or {}
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        if reason == "max_output_tokens":
            self.state.finish_reason = "length"
        elif reason == "content_filter":
            self.state.finish_reason = "content_filter"
        else:
            self.state.finish_reason = "stop"
        return
        yield

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

        # 立即 emit error + [DONE]，并锁 terminal_emitted 防止 close() 重复
        self.state.terminal_emitted = True
        yield _mk_error_chunk(self.state, message=msg, err_type="server_error")
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
        return "stop"
    if status in ("failed", "cancelled"):
        return fallback
    return fallback


def _usage_resps_to_chat_stream(u: dict) -> dict:
    """同 chat_to_responses._usage_resps_to_chat，但独立一份避免跨文件 import。"""
    input_tokens = int(u.get("input_tokens", 0) or 0)
    output_tokens = int(u.get("output_tokens", 0) or 0)
    total = int(u.get("total_tokens", input_tokens + output_tokens) or 0)
    in_details = u.get("input_tokens_details") or {}
    out_details = u.get("output_tokens_details") or {}
    cached = int(in_details.get("cached_tokens", 0) or 0)
    reasoning = int(out_details.get("reasoning_tokens", 0) or 0)
    res: dict = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total,
    }
    if cached:
        res["prompt_tokens_details"] = {"cached_tokens": cached}
    if reasoning:
        res["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return res
