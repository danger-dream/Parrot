"""SSE 翻译器：上游 Chat Completions 流 → 下游 Responses 流。

使用场景：responses ingress（`/v1/responses` 下游）指向 openai-chat 上游。
上游只有一种事件（`data: {"choices":[{"delta":{...}}]}\\n\\n`），下游要
拆成细粒度的 responses 事件流。

状态机要点：
  - 首包到达时先 emit `response.created` + `response.in_progress`
  - `delta.content` → 打开 message item + output_text part，持续 output_text.delta；
    切换到其他 item 类型前先 close
  - `delta.reasoning_content`（非官方）→ 打开 reasoning item + summary_part，
    持续 reasoning_summary_text.delta
  - `delta.tool_calls[i]` 首次出现 → 新 function_call item，按 tc.index 索引；
    后续 arguments 累加到同一 item
  - `finish_reason` → 收尾状态（"length"→ status=incomplete）
  - `chunk.usage` → 末帧 usage（若 stream_options.include_usage=true）
  - close() 时关闭所有打开 item 并发 response.completed / response.incomplete /
    response.failed（按 finish_reason）

sequence_number 全局自增，保留官方字段占位；客户端通常不严格校验。
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
class _MessageItem:
    item_id: str
    output_index: int
    content_part_opened: bool = False
    text_buf: str = ""
    refusal_buf: str = ""
    refusal_part_opened: bool = False


@dataclass
class _ReasoningItem:
    item_id: str
    output_index: int
    summary_part_opened: bool = False
    text_buf: str = ""


@dataclass
class _FunctionCallItem:
    output_index: int
    fc_id: str
    call_id: str
    name: str = ""
    args_buf: str = ""


@dataclass
class C2RState:
    resp_id: str
    model: str
    created_ts: int
    previous_response_id: Optional[str] = None
    sequence: int = 0
    next_output_index: int = 0

    created_emitted: bool = False
    terminal_emitted: bool = False

    active_text_kind: Optional[str] = None       # "message" | "reasoning" | None
    message_item: Optional[_MessageItem] = None
    reasoning_item: Optional[_ReasoningItem] = None
    fc_by_chat_index: dict[int, _FunctionCallItem] = field(default_factory=dict)

    finish_reason: Optional[str] = None          # chat 的值：stop/length/tool_calls/content_filter/function_call
    usage: Optional[dict] = None                  # chat usage（上游 stream_options.include_usage）

    # 终止错误（上游 error chunk 或 stream 异常）
    terminal_error: Optional[dict] = None

    def next_seq(self) -> int:
        self.sequence += 1
        return self.sequence

    def allocate_output_index(self) -> int:
        i = self.next_output_index
        self.next_output_index += 1
        return i


# ─── SSE 帧构造 ──────────────────────────────────────────────────


def _emit(event: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


# ─── Translator ──────────────────────────────────────────────────


class StreamTranslator:
    """Chat SSE → Responses SSE 翻译器。"""

    def __init__(self, *, model: str, previous_response_id: Optional[str] = None,
                 created_ts: Optional[int] = None,
                 api_key_name: Optional[str] = None,
                 channel_key: Optional[str] = None,
                 current_input_items: Optional[list] = None):
        self.state = C2RState(
            resp_id=_gen_id("resp_"),
            model=model,
            created_ts=int(created_ts or time.time()),
            previous_response_id=previous_response_id,
        )
        self._buf = b""
        # Store 写入上下文：当三者齐全（+ store enabled）时，close() 把本次响应
        # 存入 openai.store 以支持下次 previous_response_id 续接
        self._store_api_key_name = api_key_name or None
        self._store_channel_key = channel_key or None
        self._store_current_input = list(current_input_items) if current_input_items else None

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
            yield from self._handle_block(block)

    def close(self) -> Iterator[bytes]:
        if self.state.terminal_emitted:
            return
        self.state.terminal_emitted = True

        # 防御：即使上游一个 chunk 都没发就关闭（空流或立即 [DONE]），
        # 也要保证下游看到合法的事件序列 response.created → in_progress → ...
        yield from self._ensure_created()

        if self.state.terminal_error is not None:
            yield from self._emit_failed(self.state.terminal_error)
            # 失败态不写 Store（不想污染后续 prev_id 链）
            return

        # 关闭所有打开的 item
        yield from self._close_text_item()
        yield from self._close_all_function_calls()

        yield from self._emit_terminal()

        # 正常结束 → 写 Store（若配齐了上下文且 Store 开启）
        self._save_to_store_if_configured()

    # --- 解析 ---

    def _handle_block(self, block: str) -> Iterator[bytes]:
        # chat SSE 块只可能有一行 `data: {json}` 或 `data: [DONE]`
        data_str: Optional[str] = None
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
        if data_str is None:
            return
        if data_str == "[DONE]":
            return  # 收尾由 close() 做
        try:
            evt = json.loads(data_str)
        except Exception:
            return

        # 首个事件前发 response.created + in_progress
        yield from self._ensure_created()

        # 上游 error chunk：标记终止，下一次 close() 会 emit failed
        if isinstance(evt, dict) and isinstance(evt.get("error"), dict):
            self.state.terminal_error = evt["error"]
            return

        choices = evt.get("choices") or []
        if choices:
            yield from self._handle_choice(choices[0])

        if isinstance(evt.get("usage"), dict):
            self.state.usage = evt["usage"]

    def _handle_choice(self, choice: dict) -> Iterator[bytes]:
        delta = choice.get("delta") or {}
        fr = choice.get("finish_reason")

        # reasoning_content 优先处理（顺序上通常 reasoning 在 message 之前）。
        # drop 模式：丢弃 reasoning 文本；不开 reasoning item（避免产生空 item）。
        rc = delta.get("reasoning_content")
        if isinstance(rc, str) and rc:
            from .common import reasoning_passthrough_enabled
            if reasoning_passthrough_enabled():
                yield from self._switch_text_kind("reasoning")
                yield from self._emit_reasoning_text_delta(rc)

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield from self._switch_text_kind("message")
            yield from self._emit_output_text_delta(content)

        refusal = delta.get("refusal")
        if isinstance(refusal, str) and refusal:
            yield from self._switch_text_kind("message")
            yield from self._emit_refusal_delta(refusal)

        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict):
                yield from self._handle_tool_call_delta(tc)

        if fr:
            self.state.finish_reason = fr

    # --- 活动 item 切换 ---

    def _switch_text_kind(self, kind: str) -> Iterator[bytes]:
        """保证当前打开的 text-ish item 是 kind（message / reasoning）。"""
        if self.state.active_text_kind == kind:
            return
        # 关掉原来的
        yield from self._close_text_item()
        # 打开新的
        if kind == "message":
            yield from self._open_message_item()
        else:
            yield from self._open_reasoning_item()
        self.state.active_text_kind = kind

    def _close_text_item(self) -> Iterator[bytes]:
        if self.state.active_text_kind == "message" and self.state.message_item:
            yield from self._close_message_item()
        elif self.state.active_text_kind == "reasoning" and self.state.reasoning_item:
            yield from self._close_reasoning_item()
        self.state.active_text_kind = None

    # --- response.created / in_progress ---

    def _ensure_created(self) -> Iterator[bytes]:
        if self.state.created_emitted:
            return
        self.state.created_emitted = True
        skeleton = self._response_skeleton(status="in_progress")
        yield _emit("response.created", {
            "type": "response.created",
            "sequence_number": self.state.next_seq(),
            "response": skeleton,
        })
        yield _emit("response.in_progress", {
            "type": "response.in_progress",
            "sequence_number": self.state.next_seq(),
            "response": skeleton,
        })

    # --- message item ---

    def _open_message_item(self) -> Iterator[bytes]:
        item = _MessageItem(item_id=_gen_id("msg_"),
                            output_index=self.state.allocate_output_index())
        self.state.message_item = item
        yield _emit("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": self.state.next_seq(),
            "output_index": item.output_index,
            "item": {
                "type": "message", "id": item.item_id, "role": "assistant",
                "status": "in_progress", "content": [],
            },
        })

    def _emit_output_text_delta(self, text: str) -> Iterator[bytes]:
        item = self.state.message_item
        assert item is not None, "message item must be opened before text delta"
        if not item.content_part_opened:
            item.content_part_opened = True
            yield _emit("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
        item.text_buf += text
        yield _emit("response.output_text.delta", {
            "type": "response.output_text.delta",
            "sequence_number": self.state.next_seq(),
            "item_id": item.item_id,
            "output_index": item.output_index,
            "content_index": 0,
            "delta": text,
        })

    def _emit_refusal_delta(self, text: str) -> Iterator[bytes]:
        item = self.state.message_item
        assert item is not None
        # refusal part 用独立的 content_index。若已存在 text part，part index=1；否则 0
        idx = 1 if item.content_part_opened else 0
        if not item.refusal_part_opened:
            item.refusal_part_opened = True
            yield _emit("response.content_part.added", {
                "type": "response.content_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": idx,
                "part": {"type": "refusal", "refusal": ""},
            })
        item.refusal_buf += text
        yield _emit("response.refusal.delta", {
            "type": "response.refusal.delta",
            "sequence_number": self.state.next_seq(),
            "item_id": item.item_id,
            "output_index": item.output_index,
            "content_index": idx,
            "delta": text,
        })

    def _close_message_item(self) -> Iterator[bytes]:
        item = self.state.message_item
        if item is None:
            return
        # 先关 text part
        if item.content_part_opened:
            yield _emit("response.output_text.done", {
                "type": "response.output_text.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "text": item.text_buf,
            })
            yield _emit("response.content_part.done", {
                "type": "response.content_part.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": item.text_buf, "annotations": []},
            })
        # refusal part
        if item.refusal_part_opened:
            ref_idx = 1 if item.content_part_opened else 0
            yield _emit("response.refusal.done", {
                "type": "response.refusal.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": ref_idx,
                "refusal": item.refusal_buf,
            })
            yield _emit("response.content_part.done", {
                "type": "response.content_part.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": ref_idx,
                "part": {"type": "refusal", "refusal": item.refusal_buf},
            })
        # output_item.done
        final_content: list[dict] = []
        if item.content_part_opened:
            final_content.append({"type": "output_text", "text": item.text_buf, "annotations": []})
        if item.refusal_part_opened:
            final_content.append({"type": "refusal", "refusal": item.refusal_buf})
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": item.output_index,
            "item": {
                "type": "message", "id": item.item_id, "role": "assistant",
                "status": "completed", "content": final_content,
            },
        })
        # 保留 item 以便 close() 收集 output items；清理 active_text_kind
        self.state.active_text_kind = None

    # --- reasoning item ---

    def _open_reasoning_item(self) -> Iterator[bytes]:
        item = _ReasoningItem(item_id=_gen_id("rs_"),
                              output_index=self.state.allocate_output_index())
        self.state.reasoning_item = item
        yield _emit("response.output_item.added", {
            "type": "response.output_item.added",
            "sequence_number": self.state.next_seq(),
            "output_index": item.output_index,
            "item": {
                "type": "reasoning", "id": item.item_id,
                "summary": [],
            },
        })

    def _emit_reasoning_text_delta(self, text: str) -> Iterator[bytes]:
        item = self.state.reasoning_item
        assert item is not None
        if not item.summary_part_opened:
            item.summary_part_opened = True
            yield _emit("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })
        item.text_buf += text
        yield _emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta",
            "sequence_number": self.state.next_seq(),
            "item_id": item.item_id,
            "output_index": item.output_index,
            "summary_index": 0,
            "delta": text,
        })

    def _close_reasoning_item(self) -> Iterator[bytes]:
        item = self.state.reasoning_item
        if item is None:
            return
        if item.summary_part_opened:
            yield _emit("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "summary_index": 0,
                "text": item.text_buf,
            })
            yield _emit("response.reasoning_summary_part.done", {
                "type": "response.reasoning_summary_part.done",
                "sequence_number": self.state.next_seq(),
                "item_id": item.item_id,
                "output_index": item.output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": item.text_buf},
            })
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": item.output_index,
            "item": {
                "type": "reasoning", "id": item.item_id,
                "summary": ([{"type": "summary_text", "text": item.text_buf}]
                            if item.summary_part_opened else []),
            },
        })

    # --- function_call items ---

    def _handle_tool_call_delta(self, tc: dict) -> Iterator[bytes]:
        idx = int(tc.get("index", 0))
        fc = self.state.fc_by_chat_index.get(idx)
        if fc is None:
            # 首包：本 tool_call 刚刚出现
            # 出现 tool_call 前先关掉 text 活跃 item（保证顺序）
            yield from self._close_text_item()

            fn = tc.get("function") or {}
            call_id = tc.get("id") or _gen_id("call_")
            fc = _FunctionCallItem(
                output_index=self.state.allocate_output_index(),
                fc_id=_gen_id("fc_"),
                call_id=call_id,
                name=fn.get("name") or "",
            )
            self.state.fc_by_chat_index[idx] = fc
            yield _emit("response.output_item.added", {
                "type": "response.output_item.added",
                "sequence_number": self.state.next_seq(),
                "output_index": fc.output_index,
                "item": {
                    "type": "function_call",
                    "id": fc.fc_id,
                    "call_id": fc.call_id,
                    "name": fc.name,
                    "arguments": "",
                    "status": "in_progress",
                },
            })
        else:
            # 后续 chunk 可能带补充的 function.name 覆盖/拼接（罕见，大多数上游只首包带 name）
            fn = tc.get("function") or {}
            if fn.get("name") and not fc.name:
                fc.name = fn["name"]

        fn = tc.get("function") or {}
        args_delta = fn.get("arguments")
        if isinstance(args_delta, str) and args_delta:
            fc.args_buf += args_delta
            yield _emit("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "sequence_number": self.state.next_seq(),
                "item_id": fc.fc_id,
                "output_index": fc.output_index,
                "delta": args_delta,
            })

    def _close_function_call(self, fc: _FunctionCallItem) -> Iterator[bytes]:
        yield _emit("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "sequence_number": self.state.next_seq(),
            "item_id": fc.fc_id,
            "output_index": fc.output_index,
            "arguments": fc.args_buf,
        })
        yield _emit("response.output_item.done", {
            "type": "response.output_item.done",
            "sequence_number": self.state.next_seq(),
            "output_index": fc.output_index,
            "item": {
                "type": "function_call",
                "id": fc.fc_id,
                "call_id": fc.call_id,
                "name": fc.name,
                "arguments": fc.args_buf,
                "status": "completed",
            },
        })

    def _close_all_function_calls(self) -> Iterator[bytes]:
        # 按 output_index 顺序关闭
        for fc in sorted(self.state.fc_by_chat_index.values(), key=lambda x: x.output_index):
            yield from self._close_function_call(fc)

    # --- 终态 ---

    def _emit_terminal(self) -> Iterator[bytes]:
        status, incomplete = _finish_reason_to_status(
            self.state.finish_reason,
            has_tool_calls=bool(self.state.fc_by_chat_index),
        )
        output_items = self._collect_output_items()
        output_text = "".join(
            (it.get("content") or [])[0].get("text", "")
            for it in output_items
            if it.get("type") == "message"
            and it.get("content")
            and (it["content"][0].get("type") == "output_text")
        )
        response = {
            "id": self.state.resp_id,
            "object": "response",
            "created_at": self.state.created_ts,
            "status": status,
            "error": None,
            "incomplete_details": incomplete,
            "model": self.state.model,
            "previous_response_id": self.state.previous_response_id,
            "output": output_items,
            "output_text": output_text,
            "usage": _usage_chat_to_resps_stream(self.state.usage or {}),
        }
        if status == "completed":
            event = "response.completed"
        elif status == "incomplete":
            event = "response.incomplete"
        else:
            event = "response.failed"
        yield _emit(event, {
            "type": event,
            "sequence_number": self.state.next_seq(),
            "response": response,
        })

    def _emit_failed(self, err: dict) -> Iterator[bytes]:
        # 已打开的 items 先关
        yield from self._close_text_item()
        yield from self._close_all_function_calls()
        output_items = self._collect_output_items()
        response = {
            "id": self.state.resp_id,
            "object": "response",
            "created_at": self.state.created_ts,
            "status": "failed",
            "error": {"message": str(err.get("message") or "upstream error"),
                      "type": err.get("type") or "server_error"},
            "incomplete_details": None,
            "model": self.state.model,
            "previous_response_id": self.state.previous_response_id,
            "output": output_items,
            "output_text": "",
            "usage": _usage_chat_to_resps_stream(self.state.usage or {}),
        }
        yield _emit("response.failed", {
            "type": "response.failed",
            "sequence_number": self.state.next_seq(),
            "response": response,
        })

    def _collect_output_items(self) -> list[dict]:
        """按 output_index 顺序收集所有 item 的"completed"快照。"""
        items: list[tuple[int, dict]] = []
        if self.state.message_item is not None:
            mi = self.state.message_item
            final_content: list[dict] = []
            if mi.content_part_opened:
                final_content.append({"type": "output_text", "text": mi.text_buf, "annotations": []})
            if mi.refusal_part_opened:
                final_content.append({"type": "refusal", "refusal": mi.refusal_buf})
            items.append((mi.output_index, {
                "type": "message", "id": mi.item_id, "role": "assistant",
                "status": "completed", "content": final_content,
            }))
        if self.state.reasoning_item is not None:
            ri = self.state.reasoning_item
            items.append((ri.output_index, {
                "type": "reasoning", "id": ri.item_id,
                "summary": ([{"type": "summary_text", "text": ri.text_buf}]
                            if ri.summary_part_opened else []),
            }))
        for fc in self.state.fc_by_chat_index.values():
            items.append((fc.output_index, {
                "type": "function_call",
                "id": fc.fc_id,
                "call_id": fc.call_id,
                "name": fc.name,
                "arguments": fc.args_buf,
                "status": "completed",
            }))
        items.sort(key=lambda x: x[0])
        return [it for _, it in items]

    def _save_to_store_if_configured(self) -> None:
        """流式 responses 响应收尾时写入 openai.store（若上下文齐全）。"""
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
            # 与非流式 translate_response 的处理一致：失败不中断已发完的流，
            # 但走节流告警让运维能看到（详见 responses_to_chat.translate_response）。
            import traceback as _tb
            _tb.print_exc()
            from ... import notifier as _notifier
            ek = _notifier.escape_html
            _notifier.throttled_notify_event_sync(
                "openai_store_save_failed",
                f"openai_store_save_failed:{self._store_api_key_name}",
                "❌ <b>OpenAI Store 写入失败</b>（流式）\n"
                f"API Key: <code>{ek(self._store_api_key_name)}</code>\n"
                f"模型: <code>{ek(self.state.model)}</code> · "
                f"渠道: <code>{ek(self._store_channel_key or '?')}</code>\n"
                f"resp_id: <code>{ek(self.state.resp_id)}</code>\n"
                f"原因: <code>{ek(str(exc))[:300]}</code>\n"
                "⚠ 下一次带该 previous_response_id 的请求会 404；"
                "请检查 state.db 读写权限与磁盘空间。",
            )

    def _response_skeleton(self, *, status: str) -> dict:
        return {
            "id": self.state.resp_id,
            "object": "response",
            "created_at": self.state.created_ts,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "model": self.state.model,
            "previous_response_id": self.state.previous_response_id,
            "output": [],
            "output_text": "",
            "usage": None,
        }


# ─── 辅助 ────────────────────────────────────────────────────────


def _finish_reason_to_status(finish_reason: Optional[str],
                              has_tool_calls: bool) -> tuple[str, Optional[dict]]:
    if finish_reason in (None, "stop"):
        return ("completed", None)
    if finish_reason in ("tool_calls", "function_call"):
        return ("completed", None)
    if finish_reason == "length":
        return ("incomplete", {"reason": "max_output_tokens"})
    if finish_reason == "content_filter":
        return ("incomplete", {"reason": "content_filter"})
    return ("completed", None)


def _usage_chat_to_resps_stream(u: dict) -> dict:
    prompt_tokens = int(u.get("prompt_tokens", 0) or 0)
    completion_tokens = int(u.get("completion_tokens", 0) or 0)
    total_tokens = int(u.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    prompt_details = u.get("prompt_tokens_details") or {}
    completion_details = u.get("completion_tokens_details") or {}
    cached = int(prompt_details.get("cached_tokens", 0) or 0)
    reasoning = int(completion_details.get("reasoning_tokens", 0) or 0)

    res: dict = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if cached:
        res["input_tokens_details"] = {"cached_tokens": cached}
    if reasoning:
        res["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return res
