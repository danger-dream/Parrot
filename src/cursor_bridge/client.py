"""Public CursorClient: OpenAI-shaped chat with real tool_calls."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .auth import CursorTokens, refresh_cursor_token, token_expiry_ms
from .constants import REQUEST_TIMEOUT_S
from .errors import (
    CursorAuthError,
    CursorError,
    CursorTimeoutError,
    CursorToolActivityError,
    classify_cursor_failure,
)
from .models import CursorModel, list_cursor_models
from .openai_messages import conversation_fingerprint, parse_messages, select_tools_for_choice
from .request_builder import build_mcp_tools, build_run_request_bytes, enabled_tool_names
from .retry import clamp_max_retries, retry_delay_s, should_retry
from .session import CursorSession, SessionEvent, new_conversation_id
from .thinking import ThinkingTagFilter
from .usage import CursorUsage, fetch_cursor_usage

SessionFactory = Callable[..., CursorSession]


def _final_usage(usage: dict[str, int] | None, output_parts: list[str]) -> dict[str, int] | None:
    if usage is None and not output_parts:
        return None
    estimated_output = 0
    output_text = "".join(output_parts)
    if output_text:
        estimated_output = max(1, int(math.ceil(len(output_text.encode("utf-8", errors="replace")) / 3)))
    current = usage or {}
    completion = max(int(current.get("completion_tokens") or 0), estimated_output)
    total = max(int(current.get("total_tokens") or 0), completion)
    prompt = max(0, total - completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _split_context_tier(model: str) -> tuple[str, bool]:
    idx = model.rfind("~")
    if idx > 0:
        try:
            tokens = int(model[idx + 1 :])
        except ValueError:
            return model, False
        if tokens > 0:
            return model[:idx], True
    return model, False


@dataclass
class ConversationState:
    conversation_id: str = field(default_factory=new_conversation_id)
    checkpoint: bytes | None = None
    blob_store: dict[str, bytes] = field(default_factory=dict)
    live: CursorSession | None = None


class CursorClient:
    """Standalone Cursor Agent client.

    Native Cursor filesystem/shell tools are rejected. Client `tools` are
    advertised as MCP and returned as OpenAI `tool_calls`.
    """

    def __init__(
        self,
        access_token: str,
        *,
        refresh_token: str | None = None,
        max_mode: bool = False,
        max_retries: int = 2,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        session_factory: SessionFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at_ms = token_expiry_ms(access_token)
        self._token_lock = threading.RLock()
        self.max_mode = max_mode
        self.max_retries = clamp_max_retries(max_retries)
        self.request_timeout_s = request_timeout_s
        self._session_factory = session_factory or CursorSession
        self._sleep = sleeper
        self._conversations: dict[str, ConversationState] = {}

    @property
    def access_token(self) -> str:
        with self._token_lock:
            self._refresh_if_needed()
            return self._access_token

    def update_access_token(self, access_token: str) -> None:
        """Replace the account token supplied by Parrot's OAuth manager.

        CursorClient never owns refresh persistence inside Parrot.  Existing H2
        streams keep their authenticated connection; future streams use this
        token.
        """
        if not access_token:
            return
        with self._token_lock:
            if access_token != self._access_token:
                self._access_token = access_token
                self._expires_at_ms = token_expiry_ms(access_token)

    def discard_conversation(self, session_id: str, *, cancel: bool = False) -> None:
        state = self._conversations.pop(str(session_id or ""), None)
        if state is None or state.live is None:
            return
        if cancel:
            state.live.cancel()
        else:
            state.live.close()
        state.live = None

    def close(self) -> None:
        for state in self._conversations.values():
            if state.live is not None:
                state.live.close()
                state.live = None

    def list_models(self) -> list[CursorModel]:
        return list_cursor_models(self.access_token)

    def usage(self) -> CursorUsage:
        return fetch_cursor_usage(self.access_token)

    def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = True,
        session_id: str | None = None,
        long_context: bool = False,
    ) -> Iterator[dict[str, Any]] | dict[str, Any]:
        if not model:
            raise CursorError("model is required", code="invalid_request", status=400)
        if not isinstance(messages, list) or not messages:
            raise CursorError("messages is required", code="invalid_request", status=400)

        model_id, long_ctx = _split_context_tier(model)
        long_ctx = long_context or long_ctx
        key = session_id or conversation_fingerprint(messages, model_id)
        parsed = parse_messages(messages)
        selected = select_tools_for_choice(tools or [], tool_choice)
        mcp_tools = build_mcp_tools(selected)
        enabled = enabled_tool_names(mcp_tools)
        state = self._conversations.setdefault(key, ConversationState())

        resumed = bool(parsed.tool_results and state.live is not None and state.live.alive)
        if resumed:
            assert state.live is not None
            state.live.send_tool_results(
                [
                    {"tool_call_id": item.tool_call_id, "content": item.content, "is_error": False}
                    for item in parsed.tool_results
                ]
            )
            session = state.live
        else:
            session = self._open_session(
                state,
                model_id=model_id,
                parsed=parsed,
                mcp_tools=mcp_tools,
                enabled=enabled,
                long_ctx=long_ctx,
            )

        if stream:
            return self._stream(
                session,
                state,
                model,
                resumed=resumed,
                open_session=lambda: self._open_session(
                    state,
                    model_id=model_id,
                    parsed=parsed,
                    mcp_tools=mcp_tools,
                    enabled=enabled,
                    long_ctx=long_ctx,
                ),
            )
        return self._collect(
            session,
            state,
            model,
            resumed=resumed,
            open_session=lambda: self._open_session(
                state,
                model_id=model_id,
                parsed=parsed,
                mcp_tools=mcp_tools,
                enabled=enabled,
                long_ctx=long_ctx,
            ),
        )

    def _open_session(
        self,
        state: ConversationState,
        *,
        model_id: str,
        parsed: Any,
        mcp_tools: list[Any],
        enabled: set[str],
        long_ctx: bool,
    ) -> CursorSession:
        if state.live is not None:
            state.live.close()
            state.live = None
        user_text = parsed.user_text
        if not user_text and parsed.tool_results:
            user_text = "\n".join(item.content for item in parsed.tool_results)
        request_bytes = build_run_request_bytes(
            model_id=model_id,
            system_prompt=parsed.system_prompt,
            user_text=user_text,
            turns=parsed.turns,
            conversation_id=state.conversation_id,
            checkpoint=state.checkpoint,
            mcp_tools=mcp_tools,
            long_context=long_ctx,
            max_mode=self.max_mode,
        )
        session = self._session_factory(
            access_token=self.access_token,
            request_bytes=request_bytes,
            blob_store=state.blob_store,
            mcp_tools=mcp_tools,
            enabled_tools=enabled,
            cloud_rule=parsed.system_prompt or None,
            on_checkpoint=lambda blob, target=state: setattr(target, "checkpoint", blob),
        )
        state.live = session
        return session

    def _reset_conversation(self, state: ConversationState) -> None:
        if state.live is not None:
            state.live.close()
            state.live = None
        state.conversation_id = new_conversation_id()
        state.checkpoint = None
        state.blob_store.clear()

    def _retry_or_raise(
        self,
        event: SessionEvent,
        *,
        state: ConversationState,
        attempt: int,
        resumed: bool,
        emitted: bool,
        auth_retried: bool,
    ) -> tuple[bool, int, bool]:
        if not event.error:
            return False, attempt, auth_retried
        error = classify_cursor_failure(event.error, http_status=event.http_status)
        if isinstance(error, CursorAuthError) and self._refresh_token and not auth_retried and not emitted:
            self._refresh_if_needed(force=True)
            if not resumed:
                self._close_live(state)
            return True, attempt, True
        if emitted:
            return False, attempt, auth_retried
        if resumed or not should_retry(
            error,
            attempt=attempt,
            max_retries=self.max_retries,
            emitted_output=False,
        ):
            raise error
        if error.retry_hint == "blob_not_found":
            self._reset_conversation(state)
        else:
            self._close_live(state)
        self._sleep(retry_delay_s(error.retry_hint or "timeout"))
        return True, attempt + 1, auth_retried

    def _close_live(self, state: ConversationState) -> None:
        if state.live is not None:
            state.live.close()
            state.live = None

    def _next_event(self, session: CursorSession, deadline: float) -> SessionEvent:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            session.close()
            raise CursorTimeoutError(f"request exceeded {self.request_timeout_s:.0f}s")
        try:
            return session.next(timeout=remaining)
        except TimeoutError as exc:
            session.close()
            raise CursorTimeoutError(str(exc) or "session event timeout") from exc

    def _stream(
        self,
        session: CursorSession,
        state: ConversationState,
        model: str,
        *,
        resumed: bool,
        open_session: Callable[[], CursorSession],
    ) -> Iterator[dict[str, Any]]:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:28]}"
        created = int(time.time())
        tag_filter = ThinkingTagFilter()
        has_native_thinking = False
        tool_index = 0
        emitted = False
        attempt = 0
        auth_retried = False
        best_usage: dict[str, int] | None = None
        output_parts: list[str] = []
        deadline = time.monotonic() + self.request_timeout_s

        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        current = session
        while True:
            event = self._next_event(current, deadline)
            if event.type == "text":
                emitted = True
                output_parts.append(event.text)
                if event.is_thinking:
                    has_native_thinking = True
                    yield chunk({"reasoning_content": event.text})
                elif has_native_thinking:
                    yield chunk({"content": event.text})
                else:
                    filtered = tag_filter.process(event.text)
                    if filtered.reasoning:
                        yield chunk({"reasoning_content": filtered.reasoning})
                    if filtered.content:
                        yield chunk({"content": filtered.content})
            elif event.type == "toolCall" and event.exec is not None:
                emitted = True
                flushed = tag_filter.flush()
                if flushed.reasoning:
                    yield chunk({"reasoning_content": flushed.reasoning})
                if flushed.content:
                    yield chunk({"content": flushed.content})
                output_parts.extend([
                    event.exec.tool_name,
                    event.exec.decoded_args,
                ])
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": event.exec.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": event.exec.tool_name,
                                    "arguments": event.exec.decoded_args,
                                },
                            }
                        ]
                    }
                )
                tool_index += 1
            elif event.type == "usage":
                best_usage = {
                    "prompt_tokens": max(0, event.total_tokens - event.output_tokens),
                    "completion_tokens": event.output_tokens,
                    "total_tokens": event.total_tokens,
                }
            elif event.type == "batchReady":
                flushed = tag_filter.flush()
                if flushed.reasoning:
                    yield chunk({"reasoning_content": flushed.reasoning})
                if flushed.content:
                    yield chunk({"content": flushed.content})
                final_usage = _final_usage(best_usage, output_parts)
                if final_usage is not None:
                    yield {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": final_usage,
                    }
                yield chunk({}, "tool_calls")
                return
            elif event.type == "done":
                if event.error:
                    retry, attempt, auth_retried = self._retry_or_raise(
                        event,
                        state=state,
                        attempt=attempt,
                        resumed=resumed,
                        emitted=emitted,
                        auth_retried=auth_retried,
                    )
                    if retry:
                        current = open_session()
                        continue
                if event.error:
                    raise classify_cursor_failure(
                        event.error, http_status=event.http_status,
                    )
                flushed = tag_filter.flush()
                if flushed.reasoning:
                    yield chunk({"reasoning_content": flushed.reasoning})
                if flushed.content:
                    yield chunk({"content": flushed.content})
                final_usage = _final_usage(best_usage, output_parts)
                if final_usage is not None:
                    yield {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": final_usage,
                    }
                yield chunk({}, "stop")
                state.live = None
                current.close()
                return

    def _collect(
        self,
        session: CursorSession,
        state: ConversationState,
        model: str,
        *,
        resumed: bool,
        open_session: Callable[[], CursorSession],
    ) -> dict[str, Any]:
        """Collect one Cursor turn without destroying a pending tool session."""
        attempt = 0
        auth_retried = False
        current = session
        deadline = time.monotonic() + self.request_timeout_s

        def payload_for(
            *,
            text: list[str],
            reasoning: list[str],
            tool_calls: list[dict[str, Any]],
            usage: dict[str, int] | None,
            finish_reason: str,
        ) -> dict[str, Any]:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text) if text else (None if tool_calls else ""),
            }
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            if tool_calls:
                message["tool_calls"] = tool_calls
            usage_parts = list(text) + list(reasoning)
            for call in tool_calls:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                usage_parts.extend([
                    str(function.get("name") or ""),
                    str(function.get("arguments") or ""),
                ])
            usage = _final_usage(usage, usage_parts)
            payload: dict[str, Any] = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            }
            if usage:
                payload["usage"] = usage
            return payload

        while True:
            text: list[str] = []
            reasoning: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            usage: dict[str, int] | None = None
            while True:
                event = self._next_event(current, deadline)
                if event.type == "text":
                    (reasoning if event.is_thinking else text).append(event.text)
                elif event.type == "usage":
                    usage = {
                        "prompt_tokens": max(0, event.total_tokens - event.output_tokens),
                        "completion_tokens": event.output_tokens,
                        "total_tokens": event.total_tokens,
                    }
                elif event.type == "toolCall" and event.exec is not None:
                    tool_calls.append({
                        "id": event.exec.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": event.exec.tool_name,
                            "arguments": event.exec.decoded_args,
                        },
                    })
                elif event.type == "batchReady":
                    # CursorSession remains alive and paused.  A later HTTP turn
                    # carrying role=tool resumes this exact H2 stream.
                    return payload_for(
                        text=text,
                        reasoning=reasoning,
                        tool_calls=tool_calls,
                        usage=usage,
                        finish_reason="tool_calls",
                    )
                elif event.type == "done":
                    if event.error:
                        retry, attempt, auth_retried = self._retry_or_raise(
                            event,
                            state=state,
                            attempt=attempt,
                            resumed=resumed,
                            emitted=False,
                            auth_retried=auth_retried,
                        )
                        if retry:
                            current = open_session()
                            break
                    current.close()
                    state.live = None
                    if event.error:
                        raise classify_cursor_failure(event.error, http_status=event.http_status)
                    return payload_for(
                        text=text,
                        reasoning=reasoning,
                        tool_calls=tool_calls,
                        usage=usage,
                        finish_reason="tool_calls" if tool_calls else "stop",
                    )

    def _refresh_if_needed(self, *, force: bool = False) -> None:
        # Standalone callers may still opt into local refresh.  Parrot creates
        # clients without a refresh token so oauth_manager remains the sole
        # owner of rotation and durable persistence.
        if not self._refresh_token:
            if force:
                raise CursorAuthError("access token rejected and no refresh token is configured")
            return
        if not force and int(time.time() * 1000) < self._expires_at_ms:
            return
        try:
            tokens: CursorTokens = refresh_cursor_token(self._refresh_token)
        except Exception as exc:  # noqa: BLE001
            raise CursorAuthError(f"token refresh failed: {exc}") from exc
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        self._expires_at_ms = tokens.expires_at_ms
