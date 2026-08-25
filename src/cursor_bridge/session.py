"""One Cursor AgentService/Run stream, paused across tool_calls."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Empty, Queue
from collections.abc import Callable
from typing import Literal

from . import agent_pb2
from .connect import ConnectFrameParser, frame_connect_message, parse_connect_end_stream
from .constants import (
    AGENT_RUN_PATH,
    CONNECT_TIMEOUT_S,
    HEARTBEAT_INTERVAL_S,
    INACTIVITY_FLUSHED_S,
    INACTIVITY_STREAMING_S,
    INACTIVITY_THINKING_S,
)
from .errors import RetryHint, classify_cursor_failure
from .h2stream import CursorH2Stream, cursor_headers
from .tool_dispatch import (
    PendingExec,
    handle_exec_message,
    handle_interaction_query,
    handle_kv_message,
    send_mcp_result_for_pending,
)

@dataclass
class SessionEvent:
    type: Literal["text", "toolCall", "batchReady", "usage", "done"]
    text: str = ""
    is_thinking: bool = False
    exec: PendingExec | None = None
    output_tokens: int = 0
    total_tokens: int = 0
    error: str | None = None
    retry_hint: RetryHint | None = None
    http_status: int | None = None


@dataclass
class StreamState:
    pending_execs: list[PendingExec] = field(default_factory=list)
    output_tokens: int = 0
    total_tokens: int = 0
    end_stream_seen: bool = False
    checkpoint_after_exec: bool = False


def classify_connect_error(message: str, *, http_status: int | None = None) -> RetryHint | None:
    return classify_cursor_failure(message, http_status=http_status).retry_hint


class CursorSession:
    def __init__(
        self,
        *,
        access_token: str,
        request_bytes: bytes,
        blob_store: dict[str, bytes],
        mcp_tools: list[agent_pb2.McpToolDefinition],
        enabled_tools: set[str],
        cloud_rule: str | None,
        on_checkpoint: Callable[[bytes], None] | None = None,
        account_key: str = "",
        channel_key: str = "",
        model: str = "",
    ) -> None:
        self.access_token = access_token
        self.request_bytes = request_bytes
        self.blob_store = blob_store
        self.mcp_tools = mcp_tools
        self.enabled_tools = enabled_tools
        self.cloud_rule = cloud_rule
        self.on_checkpoint = on_checkpoint
        self.state = StreamState()
        self.events: Queue[SessionEvent] = Queue()
        self.alive = True
        self._stop = threading.Event()
        self.done_sent = False
        self.batch_state: Literal["streaming", "collecting", "flushed"] = "streaming"
        self.pending_execs: list[PendingExec] = []
        self._flushed: list[PendingExec] = []
        self._timer_phase: Literal["thinking", "streaming"] = "thinking"
        self._inactivity_deadline = time.monotonic() + INACTIVITY_THINKING_S
        self._stream = CursorH2Stream(
            timeout_s=CONNECT_TIMEOUT_S,
            account_key=account_key,
            channel_key=channel_key,
            model=model,
            purpose="oauth_cursor",
        )
        self._parser = ConnectFrameParser(self._on_frame, self._on_end_stream)
        self._worker = threading.Thread(target=self._run, name="cursor-session", daemon=True)
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="cursor-heartbeat", daemon=True)
        self._worker.start()
        self._heartbeat.start()

    def next(self, timeout: float | None = None) -> SessionEvent:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
            if deadline is not None and remaining == 0:
                raise TimeoutError("session event timeout")
            try:
                return self.events.get(timeout=remaining or 0.25)
            except Empty:
                if not self.alive and not self.done_sent:
                    return self._done("session closed")
                if time.monotonic() > self._inactivity_deadline and self.alive:
                    self._fail("inactivity timeout")
                    self.close()
                    return SessionEvent(type="done", error="inactivity timeout", retry_hint="timeout")

    def send_tool_results(self, results: list[dict[str, str | bool]]) -> None:
        remaining: list[PendingExec] = []
        by_id = {str(item["tool_call_id"]): item for item in results}

        def send(data: bytes) -> None:
            self._stream.write(data)

        for exec_item in self.pending_execs:
            match = by_id.get(exec_item.tool_call_id)
            if match is None:
                remaining.append(exec_item)
                continue
            send_mcp_result_for_pending(
                exec_item,
                send,
                str(match.get("content") or ""),
                is_error=bool(match.get("is_error")),
            )
        self.pending_execs = remaining
        if remaining:
            for item in remaining:
                self.events.put(SessionEvent(type="toolCall", exec=item))
            self.batch_state = "flushed"
            self._flushed = list(remaining)
            self.events.put(SessionEvent(type="batchReady"))
        else:
            self.batch_state = "streaming"
            self._flushed = []
            self._reset_inactivity()

    def cancel(self) -> None:
        if not self.alive:
            return
        try:
            action = agent_pb2.ConversationAction(cancel_action=agent_pb2.CancelAction())
            payload = agent_pb2.AgentClientMessage(conversation_action=action).SerializeToString()
            self._stream.write(frame_connect_message(payload))
        except Exception:
            pass
        self.on_checkpoint = None
        self.close()

    def close(self) -> None:
        was_alive = self.alive
        self.alive = False
        self._stop.set()
        if was_alive and not self.done_sent:
            # Publish the intentional close before transport EOF can race the worker's
            # fallback "bridge connection lost" result.
            self._push_done(SessionEvent(type="done", error="session closed"))
        self._stream.close()
        current = threading.current_thread()
        for thread in (self._worker, self._heartbeat):
            if thread is not current and thread.is_alive():
                thread.join(timeout=1.0)

    def _run(self) -> None:
        try:
            self._stream.open(
                cursor_headers(
                    path=AGENT_RUN_PATH,
                    access_token=self.access_token,
                    content_type="application/connect+proto",
                ),
                frame_connect_message(self.request_bytes),
                end_stream=False,
            )
            while self.alive:
                item = self._stream.read(timeout=0.5)
                if time.monotonic() > self._inactivity_deadline:
                    self._fail("inactivity timeout")
                    break
                if item is None:
                    continue
                kind, payload = item
                if kind == "data":
                    self._parser.feed(payload)
                    self._after_parse()
                elif kind == "error":
                    text = payload.decode("utf-8", errors="replace") or "h2 error"
                    self._fail(text, http_status=self._stream.status)
                    break
                elif kind in {"end", "closed"}:
                    break
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc), http_status=self._stream.status)
        finally:
            self.alive = False
            self._stop.set()
            self._stream.close()
            if not self.done_sent:
                if self.pending_execs:
                    self._fail("session closed with pending tool calls")
                elif self._stream.status not in (None, 200):
                    self._fail(f"HTTP {self._stream.status}", http_status=self._stream.status)
                elif not self.state.end_stream_seen:
                    self._fail("bridge connection lost")
                else:
                    self._push_done(SessionEvent(type="done"))

    def _heartbeat_loop(self) -> None:
        while self.alive:
            if self._stop.wait(HEARTBEAT_INTERVAL_S):
                return
            if not self.alive:
                return
            payload = agent_pb2.AgentClientMessage(client_heartbeat=agent_pb2.ClientHeartbeat())
            self._stream.write(frame_connect_message(payload.SerializeToString()))

    def _on_frame(self, raw: bytes) -> None:
        msg = agent_pb2.AgentServerMessage()
        msg.ParseFromString(raw)
        case = msg.WhichOneof("message")

        def send(data: bytes) -> None:
            self._stream.write(data)

        if case == "exec_server_message":
            handle_exec_message(
                msg.exec_server_message,
                mcp_tools=self.mcp_tools,
                enabled=self.enabled_tools,
                cloud_rule=self.cloud_rule,
                send=send,
                on_mcp_exec=self._on_mcp_exec,
            )
            self._reset_inactivity()
            return
        if case == "exec_server_control_message":
            self._reset_inactivity()
            return
        if case == "interaction_query":
            handle_interaction_query(msg.interaction_query, send)
            self._reset_inactivity()
            return
        if case == "interaction_update":
            self._handle_update(msg.interaction_update)
            self._reset_inactivity()
            return
        if case == "kv_server_message":
            handle_kv_message(msg.kv_server_message, self.blob_store, send)
            self._reset_inactivity()
            return
        if case == "conversation_checkpoint_update":
            state = msg.conversation_checkpoint_update
            if state.HasField("token_details"):
                self.state.total_tokens = state.token_details.used_tokens
                self.events.put(
                    SessionEvent(
                        type="usage",
                        output_tokens=self.state.output_tokens,
                        total_tokens=self.state.total_tokens,
                    )
                )
            self.state.checkpoint_after_exec = True
            if self.on_checkpoint:
                self.on_checkpoint(state.SerializeToString())
            self._reset_inactivity()

    def _handle_update(self, update: agent_pb2.InteractionUpdate) -> None:
        case = update.WhichOneof("message")
        if case == "text_delta" and update.text_delta.text:
            self._timer_phase = "streaming"
            self.events.put(SessionEvent(type="text", text=update.text_delta.text, is_thinking=False))
        elif case == "thinking_delta" and update.thinking_delta.text:
            self.events.put(SessionEvent(type="text", text=update.thinking_delta.text, is_thinking=True))
        elif case == "token_delta":
            self.state.output_tokens += update.token_delta.tokens
            self.events.put(
                SessionEvent(
                    type="usage",
                    output_tokens=self.state.output_tokens,
                    total_tokens=self.state.total_tokens,
                )
            )
        elif case in {"turn_ended", "step_completed"} and self.pending_execs:
            self.state.checkpoint_after_exec = True

    def _on_mcp_exec(self, exec_item: PendingExec) -> None:
        self.pending_execs.append(exec_item)
        if self.batch_state == "streaming":
            self.batch_state = "collecting"
        self.events.put(SessionEvent(type="toolCall", exec=exec_item))

    def _on_end_stream(self, raw: bytes) -> None:
        self.state.end_stream_seen = True
        error = parse_connect_end_stream(raw)
        if error:
            self._fail(error, http_status=self._stream.status)
            self.alive = False
            return
        if self.pending_execs and self.batch_state == "collecting":
            self.state.checkpoint_after_exec = True

    def _after_parse(self) -> None:
        if (
            self.batch_state == "collecting"
            and self.pending_execs
            and not self._flushed
            and self.state.checkpoint_after_exec
        ):
            self.batch_state = "flushed"
            self.state.checkpoint_after_exec = False
            self._flushed = list(self.pending_execs)
            self.events.put(SessionEvent(type="batchReady"))
        if (
            self.state.end_stream_seen
            and self.batch_state != "collecting"
            and not self.pending_execs
            and not self.done_sent
        ):
            self._push_done(SessionEvent(type="done"))

    def _reset_inactivity(self) -> None:
        if self.batch_state == "flushed":
            timeout = INACTIVITY_FLUSHED_S
        elif self._timer_phase == "thinking":
            timeout = INACTIVITY_THINKING_S
        else:
            timeout = INACTIVITY_STREAMING_S
        self._inactivity_deadline = time.monotonic() + timeout

    def _fail(self, message: str, *, http_status: int | None = None) -> None:
        classified = classify_cursor_failure(message, http_status=http_status)
        self._push_done(
            SessionEvent(
                type="done",
                error=str(classified),
                retry_hint=classified.retry_hint,
                http_status=http_status,
            )
        )

    def _push_done(self, event: SessionEvent) -> None:
        if self.done_sent:
            return
        self.done_sent = True
        self.events.put(event)

    def _done(self, error: str) -> SessionEvent:
        event = SessionEvent(type="done", error=error)
        self._push_done(event)
        return event


def new_conversation_id() -> str:
    return str(uuid.uuid4())
