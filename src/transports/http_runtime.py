"""HTTP attempt opening runtime.

This module owns HTTP response transport work for a single upstream attempt:
proxy-chain selection, httpx stream creation, connect/header/body timeouts,
proxy attempt logging, proxy byte accounting, and small response-consumption
shims used by the failover loop.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import blacklist, log_db, network, upstream
from ..async_owned import await_owned
from ..providers import registry as provider_registry
from ..protocols import errors as protocol_errors
from ..protocols.commit_gate import SseCommitGate
from ..protocols.runtime import (
    AttemptResult,
    connection_lifecycle_outcome,
    make_stream_translator,
    toolkit_for_channel,
)
from .base import metadata_from_response
from .fingerprint import create_impersonated_client, supports_route as fingerprint_supports_route
from .http import HttpStreamRequest, open_stream
from .policy import proxy_byte_snapshot, proxy_route_kwargs
from .timing import (
    BusinessTimeoutError,
    HttpAttemptTiming,
    RoundTimeouts,
    classify_httpx_timeout,
)


@dataclass
class OpenedHttpResponse:
    ctx: Any | None = None
    response: httpx.Response | None = None
    connect_ms: int | None = None
    timing: HttpAttemptTiming | None = None
    proxy_name: str | None = None
    proxy_bytes: dict[str, int] = field(default_factory=lambda: {"up": 0, "down": 0})
    proxy_client: Any | None = None
    proxy_attempt_id: Any | None = None
    round_timeouts: RoundTimeouts | None = None
    error: AttemptResult | None = None
    terminal_persistence_scheduled: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.ctx is not None and self.response is not None


@dataclass
class HttpBodyReadResult:
    raw: bytes | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    error: AttemptResult | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.raw is not None


@dataclass
class StreamAsNonStreamResult:
    obj: dict | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body_text: str = ""
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    })
    assistant_msg: dict = field(default_factory=dict)
    first_byte_ms: int | None = None
    total_ms: int | None = None
    error: AttemptResult | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and isinstance(self.obj, dict)


@dataclass
class HttpStreamStartResult:
    aiter: Any | None = None
    tracker: Any | None = None
    builder: Any | None = None
    stream_translator: Any | None = None
    first_downstream_chunks: list[bytes] = field(default_factory=list)
    first_byte_ms: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    upstream_status: int | None = None
    error: AttemptResult | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.aiter is not None


@dataclass
class HttpStreamReadStep:
    kind: str
    downstream_chunks: list[bytes] = field(default_factory=list)
    message: str | None = None
    err_type: str | None = None
    outcome: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "chunks"


class _SharedStreamContext:
    """Keep the shared pool leased through open, body streaming and cleanup."""

    def __init__(self, request: HttpStreamRequest):
        self.lease = upstream.acquire_client()
        try:
            self.ctx = open_stream(self.lease.client, request)
        except BaseException:
            self.lease.release()
            raise

    async def __aenter__(self):
        return await self.ctx.__aenter__()

    async def __aexit__(self, *args):
        try:
            return await self.ctx.__aexit__(*args)
        finally:
            self.lease.release()


async def close_response_context(ctx) -> None:
    try:
        await ctx.__aexit__(None, None, None)
    except Exception:
        pass


async def close_proxy_client(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


async def _await_http_owned(awaitable):
    """Let an ownership transition finish before propagating cancellation."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except BaseException:
        try:
            await await_owned(task)
        except BaseException:
            pass
        raise


def _new_proxy_bytes() -> dict[str, int]:
    return {"up": 0, "down": 0}


def _count_proxy_bytes_for(bucket: dict[str, int]):
    def _cb(up: int = 0, down: int = 0):
        bucket["up"] += int(up or 0)
        bucket["down"] += int(down or 0)
    return _cb


def _attempt_result(outcome: str, detail: str, *, bucket: dict | None = None,
                    proxy_name: str | None = None) -> AttemptResult:
    up, down = proxy_byte_snapshot(bucket)
    return AttemptResult(
        outcome=outcome,
        error_detail=detail,
        proxy_name=proxy_name,
        proxy_bytes_up=up,
        proxy_bytes_down=down,
    )


def _with_timing(
    timing: HttpAttemptTiming | None,
    result: AttemptResult,
    *,
    terminal: bool = True,
) -> AttemptResult:
    if timing is None:
        return result
    if terminal:
        timing.finish(result.outcome, result.error_detail)
    return timing.apply_to(result, terminal=False)


async def _next_nonempty_http_chunk(
    aiter,
    timing: HttpAttemptTiming | None,
    round_timeouts: RoundTimeouts | None,
) -> bytes:
    """Return the next non-empty raw body chunk under this round's deadlines."""

    while True:
        awaitable = aiter.__anext__()
        if timing is not None and round_timeouts is not None:
            chunk = await timing.wait_for(awaitable, round_timeouts)
        else:
            chunk = await awaitable
        if not chunk:
            continue
        raw = bytes(chunk)
        if timing is not None:
            timing.mark_response_body_byte(raw)
        return raw


async def next_nonempty_http_chunk(
    aiter,
    timing: HttpAttemptTiming | None,
    round_timeouts: RoundTimeouts | None,
) -> bytes:
    """Public raw-body activity primitive for HTTP/SSE bridge consumers."""

    return await _next_nonempty_http_chunk(aiter, timing, round_timeouts)


async def _read_response_bytes(
    response: httpx.Response,
    timing: HttpAttemptTiming | None,
    round_timeouts: RoundTimeouts | None,
    partial_state: dict[str, Any] | None = None,
) -> bytes:
    """Read a response using only non-empty raw bytes as business activity."""

    if timing is not None:
        timing.start_response_body_wait()
    parts: list[bytes] = []
    if partial_state is not None:
        partial_state["parts"] = parts
    aiter = response.aiter_bytes()
    while True:
        try:
            parts.append(await _next_nonempty_http_chunk(aiter, timing, round_timeouts))
        except StopAsyncIteration:
            if timing is not None:
                timing.mark_io_complete()
            return b"".join(parts)


def _stream_tracker_error_result(
    tracker,
    *,
    connect_ms: int | None,
    first_byte_ms: int | None,
    response_status: int | None = None,
    translator_ctx: dict | None = None,
) -> AttemptResult | None:
    """Convert a terminal SSE error observed by a tracker into an attempt error.

    OpenAI Responses stream-only channels can return HTTP 200 and later terminate
    the SSE with ``event:error`` / ``response.failed``.  When Parrot aggregates
    that stream for a non-stream downstream (notably the local WebSearch loop),
    treating the builder output as success hides semantic errors like
    ``context_length_exceeded`` from Claude Code and prevents its compact flow.
    """

    if not bool(getattr(tracker, "saw_stream_error", False)):
        return None
    message = getattr(tracker, "stream_error_message", None) or "upstream stream error"
    detail = str(message)
    return AttemptResult(
        outcome="upstream_error_json",
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        http_status=response_status,
        error_detail=detail[:2000],
        translator_ctx=translator_ctx,
    )


async def read_http_error_response(
    ctx,
    response: httpx.Response,
    *,
    connect_ms: int | None,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    proxy_name: str | None = None,
    proxy_bytes: dict | None = None,
    translator_ctx: dict | None = None,
    partial_state: dict[str, Any] | None = None,
) -> AttemptResult:
    """Read and normalize a non-2xx/3xx response under round deadlines."""

    status = response.status_code
    status_outcome = "http_auth_error" if status in (401, 403) else "http_error"
    try:
        raw = await _read_response_bytes(
            response, timing, round_timeouts, partial_state,
        )
    except asyncio.CancelledError:
        # The owning HTTP attempt closes the context and terminalizes its root.
        raise
    except BusinessTimeoutError as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome=status_outcome,
            http_status=status,
            error_detail=f"HTTP {status}: {exc.outcome} reading error body",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))
    except httpx.TimeoutException as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome=status_outcome,
            http_status=status,
            error_detail=f"HTTP {status}: error body read timeout: {exc}",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))
    except Exception as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome=status_outcome,
            http_status=status,
            error_detail=f"HTTP {status}: error body read failed: {exc}",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))

    err_text = raw.decode("utf-8", errors="replace")
    await close_response_context(ctx)

    up, down = proxy_byte_snapshot(proxy_bytes)
    outcome = status_outcome
    return _with_timing(timing, AttemptResult(
        outcome=outcome,
        http_status=status,
        connect_ms=connect_ms,
        error_detail=f"HTTP {status}: {err_text[:2000]}",
        full_response_text=err_text,
        proxy_name=proxy_name,
        proxy_bytes_up=up,
        proxy_bytes_down=down,
        translator_ctx=translator_ctx,
    ))


async def read_non_stream_body(
    ctx,
    response: httpx.Response,
    *,
    connect_ms: int | None,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    partial_state: dict[str, Any] | None = None,
) -> HttpBodyReadResult:
    """Read a non-stream body; first-byte is inapplicable, idle starts on body."""

    try:
        raw = await _read_response_bytes(
            response, timing, round_timeouts, partial_state,
        )
    except asyncio.CancelledError:
        # The owning HTTP attempt closes the context and terminalizes its root.
        raise
    except BusinessTimeoutError as exc:
        await close_response_context(ctx)
        return HttpBodyReadResult(
            error=_with_timing(timing, AttemptResult(
                outcome=exc.outcome,
                error_detail=f"{exc.outcome} reading non-stream body",
            ))
        )
    except httpx.TimeoutException as exc:
        await close_response_context(ctx)
        return HttpBodyReadResult(
            error=_with_timing(timing, AttemptResult(
                outcome=classify_httpx_timeout(exc),
                error_detail=f"non-stream body timeout: {exc}",
            ))
        )
    except Exception as exc:
        await close_response_context(ctx)
        return HttpBodyReadResult(
            error=_with_timing(timing, AttemptResult(
                outcome=connection_lifecycle_outcome(
                    exc,
                    http_status=response.status_code,
                    http_phase="response_body",
                ) or "transport_error",
                http_status=response.status_code,
                error_detail=f"read non-stream body: {exc}",
            ))
        )

    response_headers = metadata_from_response(response).forward_headers()
    await close_response_context(ctx)

    if not raw:
        return HttpBodyReadResult(
            error=_with_timing(timing, AttemptResult(
                outcome="closed_before_first_byte",
                error_detail="upstream empty body",
            ))
        )
    return HttpBodyReadResult(raw=raw, response_headers=response_headers)


async def aggregate_stream_as_non_stream_response(
    ctx,
    response: httpx.Response,
    channel,
    resolved_model: str,
    *,
    dynamic_map: dict | None,
    connect_ms: int | None,
    start_time: float,
    deadline_ts: float,
    total_timeout: int,
    first_byte_timeout: int,
    idle_timeout: int,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    translator_ctx: dict | None = None,
    partial_state: dict[str, Any] | None = None,
) -> StreamAsNonStreamResult:
    """Aggregate an upstream SSE response into one non-stream JSON object."""
    raw_buf = bytearray()
    if partial_state is not None:
        partial_state["raw_buf"] = raw_buf
    aiter = response.aiter_bytes()

    if timing is not None:
        timing.start_response_body_wait()
    try:
        first_chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
    except asyncio.CancelledError:
        # The owning HTTP attempt closes the context and terminalizes its root.
        raise
    except BusinessTimeoutError as exc:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=_with_timing(timing, AttemptResult(
                outcome=exc.outcome,
                error_detail=f"{exc.outcome} [stream-only→non-stream]",
            ))
        )
    except StopAsyncIteration:
        if timing is not None:
            timing.mark_io_complete()
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=_with_timing(timing, AttemptResult(
                outcome="closed_before_first_byte",
                error_detail="upstream closed stream before first byte [stream-only→non-stream]",
            ))
        )
    except httpx.TimeoutException as exc:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=_with_timing(timing, AttemptResult(
                outcome=classify_httpx_timeout(exc),
                error_detail=f"first byte transport timeout: {exc} [stream-only→non-stream]",
            ))
        )
    except Exception as exc:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=_with_timing(timing, AttemptResult(
                outcome=connection_lifecycle_outcome(
                    exc,
                    http_status=response.status_code,
                    http_phase="response_body",
                ) or "transport_error",
                http_status=response.status_code,
                error_detail=f"first byte transport: {exc} [stream-only→non-stream]",
            ))
        )

    first_byte_ms = timing.snapshot().first_byte_ms if timing is not None else None

    try:
        first_chunk_restored = await provider_registry.restore_response_bytes(
            channel,
            first_chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
    except Exception as exc:
        await close_response_context(ctx)
        raw_text = bytes(first_chunk).decode("utf-8", errors="replace")
        return StreamAsNonStreamResult(
            error=_with_timing(timing, AttemptResult(
                outcome="transform_error",
                error_detail=f"response restoration failed: {exc}"[:2000],
                full_response_text=raw_text,
                translator_ctx=translator_ctx,
            ))
        )
    toolkit = toolkit_for_channel(channel)

    first_event = toolkit["first_event_parser"](first_chunk_restored)
    if first_event and (
        first_event.get("type") == "error"
        or isinstance(first_event.get("error"), dict)
        or first_event.get("_event_name") == "error"
    ):
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="upstream_error_json",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail=json.dumps(first_event.get("error", first_event), ensure_ascii=False)[:2000],
                translator_ctx=translator_ctx,
                full_response_text=(
                    first_chunk_restored.decode("utf-8", errors="replace")
                    if isinstance(first_chunk_restored, (bytes, bytearray))
                    else str(first_chunk_restored)
                ),
            )
        )

    bl_hit = blacklist.match(first_chunk_restored, getattr(channel, "key", ""))
    if bl_hit:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="blacklist_hit",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail=f"blacklist: {bl_hit}",
                full_response_text=(
                    first_chunk_restored.decode("utf-8", errors="replace")
                    if isinstance(first_chunk_restored, (bytes, bytearray))
                    else str(first_chunk_restored)
                ),
            )
        )

    chat_upstream = getattr(channel, "protocol", "") == "openai-chat"
    if chat_upstream:
        from .chat_aggregate import ChatAggregateBuilder
        builder = ChatAggregateBuilder()
    else:
        builder = toolkit["stream_builder"]()
    tracker = toolkit["stream_tracker"]()
    builder.feed(first_chunk_restored)
    tracker.feed(first_chunk_restored)
    raw_buf.extend(
        first_chunk_restored
        if isinstance(first_chunk_restored, (bytes, bytearray))
        else first_chunk_restored.encode("utf-8", errors="replace")
    )

    def with_partial_billing(err: AttemptResult) -> AttemptResult:
        err.full_response_text = bytes(raw_buf).decode("utf-8", errors="replace")
        usage = getattr(tracker, "usage", None)
        if isinstance(usage, dict):
            err.usage = dict(usage)
        err.usage_observed = bool(getattr(tracker, "usage_observed", False))
        return err
    if err := _stream_tracker_error_result(
        tracker,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        response_status=response.status_code,
        translator_ctx=translator_ctx,
    ):
        await close_response_context(ctx)
        return StreamAsNonStreamResult(error=with_partial_billing(err))

    while not (chat_upstream and builder.done_received):
        try:
            chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
        except asyncio.CancelledError:
            # The owning HTTP attempt closes the context and terminalizes its root.
            raise
        except BusinessTimeoutError as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=with_partial_billing(_with_timing(timing, AttemptResult(
                    outcome=exc.outcome,
                    error_detail=f"{exc.outcome} reading SSE [stream-only→non-stream]",
                )))
            )
        except StopAsyncIteration:
            if timing is not None:
                timing.mark_io_complete()
            break
        except httpx.TimeoutException as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=with_partial_billing(_with_timing(timing, AttemptResult(
                    outcome=classify_httpx_timeout(exc),
                    error_detail=f"read SSE timeout: {exc} [stream-only→non-stream]",
                )))
            )
        except Exception as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=with_partial_billing(_with_timing(timing, AttemptResult(
                    outcome=connection_lifecycle_outcome(
                        exc,
                        http_status=response.status_code,
                        http_phase="response_body",
                    ) or "transport_error",
                    http_status=response.status_code,
                    error_detail=f"read SSE chunk: {exc} [stream-only→non-stream]",
                )))
            )
        try:
            restored_chunk = await provider_registry.restore_response_bytes(
                channel,
                chunk,
                dynamic_map=dynamic_map,
                translator_ctx=translator_ctx,
            )
        except Exception as exc:
            raw_buf.extend(bytes(chunk))
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=with_partial_billing(_with_timing(timing, AttemptResult(
                    outcome="transform_error",
                    error_detail=f"response restoration failed: {exc}"[:2000],
                    translator_ctx=translator_ctx,
                )))
            )
        builder.feed(restored_chunk)
        tracker.feed(restored_chunk)
        raw_buf.extend(
            restored_chunk
            if isinstance(restored_chunk, (bytes, bytearray))
            else restored_chunk.encode("utf-8", errors="replace")
        )
        if err := _stream_tracker_error_result(
            tracker,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            response_status=response.status_code,
            translator_ctx=translator_ctx,
        ):
            await close_response_context(ctx)
            return StreamAsNonStreamResult(error=with_partial_billing(err))

    response_headers = metadata_from_response(response).forward_headers()
    await close_response_context(ctx)
    response_body_text = bytes(raw_buf).decode("utf-8", errors="replace")

    if not getattr(tracker, "saw_stream_end", False):
        return StreamAsNonStreamResult(
            error=with_partial_billing(_with_timing(timing, AttemptResult(
                outcome="upstream_malformed",
                error_detail="stream ended without a terminal SSE event [stream-only→non-stream]",
                full_response_text=response_body_text,
            )))
        )

    if not builder.has_any_event:
        return StreamAsNonStreamResult(
            error=with_partial_billing(_with_timing(timing, AttemptResult(
                outcome="upstream_malformed",
                error_detail="stream ended without any SSE event [stream-only→non-stream]",
                full_response_text=response_body_text,
            )))
        )

    obj = builder.to_full_json(fallback_model=resolved_model)
    try:
        usage_from_tracker = tracker.usage if hasattr(tracker, "usage") else None
        if usage_from_tracker and not chat_upstream:
            obj.setdefault("usage", usage_from_tracker)
    except Exception:
        pass

    total_ms = timing.snapshot(terminal=True).total_ms if timing is not None else None
    usage = toolkit["extract_usage_json"](obj)
    assistant_msg = builder.get_assistant() if chat_upstream else {"role": "assistant", "content": obj.get("output") or []}

    return StreamAsNonStreamResult(
        obj=obj,
        response_headers=response_headers,
        response_body_text=response_body_text,
        usage=usage,
        assistant_msg=assistant_msg,
        first_byte_ms=first_byte_ms,
        total_ms=total_ms,
    )


def _downstream_stream_protocol(ingress_protocol: str) -> str:
    if ingress_protocol == "responses":
        return "openai-responses"
    if ingress_protocol == "chat":
        return "openai-chat"
    return "anthropic"


async def _read_until_first_downstream_chunk(
    aiter,
    channel,
    dynamic_map: dict | None,
    tracker,
    builder,
    deadline_ts: float,
    idle_timeout: int,
    *,
    protocol: str,
    first_chunk: bytes,
    stream_translator=None,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    translator_ctx: dict | None = None,
) -> tuple[list[bytes], dict | None]:
    commit_gate = SseCommitGate(protocol=protocol, stream_translator=stream_translator)

    async def restore(raw: bytes) -> bytes:
        try:
            return await provider_registry.restore_response_bytes(
                channel,
                raw,
                dynamic_map=dynamic_map,
                translator_ctx=translator_ctx,
            )
        except Exception as exc:
            # The un-restored upstream frame is still useful billing evidence.
            # Trackers parse protocol fields, not confused tool names, so feed a
            # best-effort copy before surfacing the presentation failure.
            try:
                tracker.feed(raw)
                builder.feed(raw)
            except Exception:
                pass
            raise RuntimeError(f"response restoration failed: {exc}") from exc

    async def feed_restored(restored: bytes) -> tuple[list[bytes], dict | None]:
        relay_filter = getattr(tracker, "filter_relay_chunk", None)
        if relay_filter is not None:
            restored = relay_filter(restored)
            if not restored:
                return [], None
        tracker.feed(restored)
        builder.feed(restored)
        result = commit_gate.feed(restored)
        return result.downstream_chunks, result.error_event

    restored_first = await restore(first_chunk)
    downstream_chunks, err = await feed_restored(restored_first)
    if downstream_chunks or err is not None:
        return downstream_chunks, err

    while True:
        chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
        restored = await restore(chunk)
        downstream_chunks, err = await feed_restored(restored)
        if downstream_chunks or err is not None:
            return downstream_chunks, err


async def prepare_stream_response_start(
    ctx,
    response: httpx.Response,
    channel,
    *,
    dynamic_map: dict | None,
    connect_ms: int | None,
    deadline_ts: float,
    first_byte_timeout: int,
    idle_timeout: int,
    ingress_protocol: str,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    translator_ctx: dict | None = None,
    partial_state: dict[str, Any] | None = None,
) -> HttpStreamStartResult:
    """Read through the pre-commit SSE boundary for an HTTP stream response."""
    aiter = response.aiter_bytes()

    if timing is not None:
        timing.start_response_body_wait()

    try:
        first_chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
    except asyncio.CancelledError:
        # The owning HTTP attempt closes the context and terminalizes its root.
        raise
    except BusinessTimeoutError as exc:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=_with_timing(timing, AttemptResult(
                outcome=exc.outcome,
                error_detail=f"{exc.outcome} during first raw body byte wait",
            ))
        )
    except StopAsyncIteration:
        if timing is not None:
            timing.mark_io_complete()
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=_with_timing(timing, AttemptResult(
                outcome="closed_before_first_byte",
                http_status=response.status_code,
                error_detail="upstream closed stream before first byte",
            ))
        )
    except httpx.TimeoutException as exc:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=_with_timing(timing, AttemptResult(
                outcome=classify_httpx_timeout(exc),
                error_detail=f"first byte transport timeout: {exc}",
            ))
        )
    except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=_with_timing(timing, AttemptResult(
                outcome=connection_lifecycle_outcome(
                    exc,
                    http_status=response.status_code,
                    http_phase="response_body",
                ) or "transport_error",
                http_status=response.status_code,
                error_detail=f"first byte transport: {exc}",
            ))
        )

    first_byte_ms = timing.snapshot().first_byte_ms if timing is not None else None

    toolkit = toolkit_for_channel(channel)
    tracker = toolkit["stream_tracker"]()
    builder = toolkit["stream_builder"]()
    if partial_state is not None:
        partial_state["tracker"] = tracker
    ch_proto = getattr(channel, "protocol", "anthropic")
    stream_translator = make_stream_translator(translator_ctx)

    def _attach_precommit_response(result: AttemptResult) -> AttemptResult:
        """Keep pre-commit SSE bytes and strict tracker billing facts together."""
        try:
            full_response = tracker.get_full_response()
        except Exception:
            full_response = None
        if full_response:
            result.full_response_text = full_response
        usage = getattr(tracker, "usage", None)
        if isinstance(usage, dict):
            result.usage = dict(usage)
        result.usage_observed = bool(getattr(tracker, "usage_observed", False))
        return result

    if ch_proto in ("openai-chat", "openai-responses") or stream_translator is not None:
        try:
            first_downstream_chunks, pre_visible_error = await _read_until_first_downstream_chunk(
                aiter,
                channel,
                dynamic_map,
                tracker,
                builder,
                deadline_ts,
                idle_timeout,
                protocol=_downstream_stream_protocol(ingress_protocol),
                first_chunk=first_chunk,
                stream_translator=stream_translator,
                timing=timing,
                round_timeouts=round_timeouts,
                translator_ctx=translator_ctx,
            )
        except BusinessTimeoutError as exc:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome=exc.outcome,
                    error_detail=f"{exc.outcome} before first downstream-visible chunk",
                )))
            )
        except StopAsyncIteration:
            if timing is not None:
                timing.mark_io_complete()
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome="closed_before_first_byte",
                    http_status=response.status_code,
                    error_detail="upstream closed stream before first downstream chunk",
                )))
            )
        except httpx.TimeoutException as exc:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome=classify_httpx_timeout(exc),
                    error_detail=f"first downstream chunk timeout: {exc}",
                )))
            )
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome=connection_lifecycle_outcome(
                        exc,
                        http_status=response.status_code,
                        http_phase="response_body",
                    ) or "transport_error",
                    http_status=response.status_code,
                    error_detail=f"first downstream chunk transport: {exc}",
                )))
            )
        except Exception as exc:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome="transform_error",
                    error_detail=f"first downstream chunk transform: {exc}"[:2000],
                    translator_ctx=translator_ctx,
                )))
            )
        if pre_visible_error:
            await close_response_context(ctx)
            # A Responses ``response.incomplete`` before any downstream-visible
            # bytes can be surfaced by the commit gate as a pre-commit error.
            # ``max_output_tokens`` is request/context-budget scoped, not an
            # unhealthy upstream: preserve its normalized 400 semantics so the
            # failover loop does not cool down the only eligible channel.
            if protocol_errors.is_responses_max_output_incomplete(pre_visible_error):
                message = protocol_errors.responses_max_output_context_error_message(
                    protocol_errors.responses_incomplete_reason(pre_visible_error)
                )
                return HttpStreamStartResult(
                    error=_attach_precommit_response(AttemptResult(
                        outcome="request_invalid",
                        connect_ms=connect_ms,
                        first_byte_ms=first_byte_ms,
                        http_status=400,
                        error_code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                        error_detail=message,
                        translator_ctx=translator_ctx,
                    ))
                )
            return HttpStreamStartResult(
                error=_attach_precommit_response(AttemptResult(
                    outcome="upstream_error_json",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=json.dumps(pre_visible_error.get("error", pre_visible_error), ensure_ascii=False)[:2000],
                    translator_ctx=translator_ctx,
                ))
            )
    else:
        try:
            first_chunk_restored = await provider_registry.restore_response_bytes(
                channel,
                first_chunk,
                dynamic_map=dynamic_map,
                translator_ctx=translator_ctx,
            )
        except Exception as exc:
            try:
                tracker.feed(first_chunk)
                builder.feed(first_chunk)
            except Exception:
                pass
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_with_timing(timing, _attach_precommit_response(AttemptResult(
                    outcome="transform_error",
                    error_detail=f"response restoration failed: {exc}"[:2000],
                    translator_ctx=translator_ctx,
                )))
            )
        # Record only protocol-visible bytes.  Chat's DONE relay filter also
        # drops any same-batch events after the terminal marker.
        relay_filter = getattr(tracker, "filter_relay_chunk", None)
        if relay_filter is not None:
            first_chunk_restored = relay_filter(first_chunk_restored)
        tracker.feed(first_chunk_restored)
        builder.feed(first_chunk_restored)
        first_event = toolkit["first_event_parser"](first_chunk_restored)
        if first_event and (
            first_event.get("type") == "error"
            or isinstance(first_event.get("error"), dict)
            or first_event.get("_event_name") == "error"
        ):
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=_attach_precommit_response(AttemptResult(
                    outcome="upstream_error_json",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=json.dumps(first_event.get("error", first_event), ensure_ascii=False)[:2000],
                    translator_ctx=translator_ctx,
                ))
            )
        if stream_translator is not None:
            first_downstream_chunks = list(stream_translator.feed(first_chunk_restored))
        else:
            first_downstream_chunks = [first_chunk_restored]

    bl_target = b"".join(first_downstream_chunks)
    bl_hit = blacklist.match(bl_target, getattr(channel, "key", ""))
    if bl_hit:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=_attach_precommit_response(AttemptResult(
                outcome="blacklist_hit",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail=f"blacklist: {bl_hit}",
            ))
        )

    return HttpStreamStartResult(
        aiter=aiter,
        tracker=tracker,
        builder=builder,
        stream_translator=stream_translator,
        first_downstream_chunks=first_downstream_chunks,
        first_byte_ms=first_byte_ms,
        response_headers=metadata_from_response(response).forward_headers(),
        upstream_status=response.status_code,
    )


async def read_next_stream_step(
    *,
    aiter,
    channel,
    dynamic_map: dict | None,
    tracker,
    builder,
    stream_translator,
    deadline_ts: float,
    start_time: float,
    idle_timeout: int,
    timing: HttpAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    translator_ctx: dict | None = None,
    upstream_status: int | None = None,
) -> HttpStreamReadStep:
    """Read and normalize one post-commit HTTP SSE stream step."""
    while True:
        try:
            chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
        except BusinessTimeoutError as exc:
            return HttpStreamReadStep(
                kind="error",
                err_type="timeout_error",
                message=exc.outcome,
                outcome=exc.outcome,
            )
        except StopAsyncIteration:
            if timing is not None:
                timing.mark_io_complete()
            return HttpStreamReadStep(kind="end")
        except httpx.TimeoutException as exc:
            outcome = classify_httpx_timeout(exc)
            return HttpStreamReadStep(
                kind="error",
                err_type="api_error",
                message=f"stream transport timeout: {exc}",
                outcome=outcome,
            )
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            return HttpStreamReadStep(
                kind="error",
                err_type="api_error",
                message=f"stream transport error: {exc}",
                outcome=connection_lifecycle_outcome(
                    exc,
                    http_status=upstream_status,
                    http_phase="response_body",
                ) or "transport_error",
            )

        try:
            restored = await provider_registry.restore_response_bytes(
                channel,
                chunk,
                dynamic_map=dynamic_map,
                translator_ctx=translator_ctx,
            )
        except Exception as exc:
            try:
                tracker.feed(chunk)
                builder.feed(chunk)
            except Exception:
                pass
            return HttpStreamReadStep(
                kind="error",
                err_type="api_error",
                message=f"response restoration failed: {exc}"[:2000],
                outcome="transform_error",
            )
        relay_filter = getattr(tracker, "filter_relay_chunk", None)
        if relay_filter is not None:
            restored = relay_filter(restored)
            if not restored:
                continue
        tracker.feed(restored)
        builder.feed(restored)
        if stream_translator is not None:
            downstream_chunks = list(stream_translator.feed(restored))
        else:
            downstream_chunks = [restored]

        if (
            getattr(tracker, "saw_stream_error", False)
            and getattr(tracker, "stream_error_code", None) == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
        ):
            return HttpStreamReadStep(
                kind="error",
                err_type="invalid_request_error",
                message=(
                    getattr(tracker, "stream_error_message", None)
                    or protocol_errors.responses_max_output_context_error_message()
                ),
                outcome="request_invalid",
            )

        if downstream_chunks:
            bl_hit = blacklist.match(b"".join(downstream_chunks), getattr(channel, "key", ""))
            if bl_hit:
                return HttpStreamReadStep(
                    kind="blacklist",
                    err_type="api_error",
                    message=f"blacklist: {bl_hit}",
                    outcome="blacklist_hit",
                )

        return HttpStreamReadStep(kind="chunks", downstream_chunks=downstream_chunks)


def _resolve_http_route_chain(
    channel,
    resolved_model: str,
    *,
    proxy_purpose: str | None = None,
) -> tuple[list[tuple[str, Any | None]], AttemptResult | None]:
    """Resolve an HTTP route without silently bypassing configured proxies.

    Direct remains the normal default when no new-proxy route exists.  Once a
    non-direct route is configured, parser/connector failures are fail-closed
    unless the user explicitly enabled ``routing.directFallback``.

    Process-private loopback bridges are never sent through an external model
    proxy. Their own provider transport owns any upstream routing policy.
    """
    if bool(getattr(channel, "internal_loopback", False)):
        return [("direct", None)], None

    from ..proxy import manager as pm

    configured = False
    try:
        pm.init()
        configured = pm.is_configured()
        if not configured:
            if pm.has_non_direct_routing_rules():
                return [], AttemptResult(
                    outcome="proxy_connect_error",
                    error_detail="configured non-direct proxy route could not be initialized",
                )
            return [("direct", None)], None

        route_kwargs = proxy_route_kwargs(channel, resolved_model)
        if proxy_purpose:
            route_kwargs["purpose"] = str(proxy_purpose)
        chain = pm.resolve_proxy_chain(**route_kwargs)
        route_chain: list[tuple[str, Any | None]] = []
        for proxy_name in chain:
            connector = pm.get_connector(proxy_name)
            if connector is None:
                continue
            if getattr(connector, "type", "") == "direct":
                # An explicit direct decision is not the legacy shared route:
                # the shared client may already be bound to the default proxy.
                route_chain.append(("direct", connector))
            else:
                route_chain.append((proxy_name, connector))

        if route_chain:
            if pm.direct_fallback_enabled() and not any(name == "direct" for name, _ in route_chain):
                route_chain.append(("direct", pm.get_connector("direct")))
            return route_chain, None

        if pm.direct_fallback_enabled():
            print(f"[proxy] HTTP route has no usable target; using enabled direct fallback: {chain}")
            return [("direct", pm.get_connector("direct"))], None
        return [], AttemptResult(
            outcome="proxy_connect_error",
            error_detail=f"proxy route has no valid target: {chain}",
        )
    except Exception as exc:
        if pm.direct_fallback_enabled():
            print(f"[proxy] HTTP route resolution failed; using enabled direct fallback: {exc}")
            return [("direct", pm.get_connector("direct"))], None
        if configured or pm.has_non_direct_routing_rules():
            return [], AttemptResult(
                outcome="proxy_connect_error",
                error_detail=f"proxy route resolution failed: {exc}",
            )
        # No configured network rule: direct is the intended default, not a
        # fallback downgrade.
        return [("direct", None)], None


def _persist_proxy_attempt_snapshot(
    proxy_attempt_id,
    snapshot,
    *,
    outcome: str | None,
    error_detail: str | None,
    proxy_bytes: dict,
) -> None:
    """Persist an already-frozen event-loop snapshot from a worker thread."""
    if proxy_attempt_id is None:
        return
    try:
        log_db.update_proxy_attempt(
            proxy_attempt_id,
            started_at=snapshot.started_at,
            connect_ms=snapshot.connect_ms,
            first_byte_ms=snapshot.first_byte_ms,
            idle_ms=snapshot.idle_ms,
            total_ms=snapshot.total_ms,
            dns_ms=snapshot.dns_ms,
            tcp_ms=snapshot.tcp_ms,
            proxy_tcp_ms=snapshot.proxy_tcp_ms,
            proxy_tunnel_ms=snapshot.proxy_tunnel_ms,
            tls_ms=snapshot.tls_ms,
            target_tls_ms=snapshot.target_tls_ms,
            ws_handshake_ms=snapshot.ws_handshake_ms,
            request_upload_ms=snapshot.request_upload_ms,
            response_headers_wait_ms=snapshot.response_headers_wait_ms,
            response_body_first_byte_wait_ms=snapshot.response_body_first_byte_wait_ms,
            ended_at=snapshot.ended_at,
            outcome=outcome,
            error_detail=(error_detail or "")[:4000] if error_detail else None,
            bytes_up=proxy_bytes.get("up"),
            bytes_down=proxy_bytes.get("down"),
        )
    except Exception:
        pass


async def _persist_frozen_proxy_attempt(
    proxy_attempt_id,
    snapshot,
    *,
    outcome: str | None,
    error_detail: str | None,
    proxy_bytes: dict,
) -> None:
    if proxy_attempt_id is None:
        return
    await asyncio.to_thread(
        _persist_proxy_attempt_snapshot,
        proxy_attempt_id,
        snapshot,
        outcome=outcome,
        error_detail=error_detail,
        proxy_bytes=proxy_bytes,
    )


_proxy_persistence_tasks: set[asyncio.Task] = set()


def _schedule_frozen_proxy_attempt_persistence(
    proxy_attempt_id,
    snapshot,
    *,
    outcome: str | None,
    error_detail: str | None,
    proxy_bytes: dict,
) -> None:
    """Own a DB-only worker without holding up proxy or candidate failover."""
    if proxy_attempt_id is None:
        return
    task = asyncio.create_task(_persist_frozen_proxy_attempt(
        proxy_attempt_id,
        snapshot,
        outcome=outcome,
        error_detail=error_detail,
        proxy_bytes=dict(proxy_bytes),
    ))
    _proxy_persistence_tasks.add(task)

    def completed(done: asyncio.Task) -> None:
        _proxy_persistence_tasks.discard(done)
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(completed)


async def _drain_proxy_attempt_persistence() -> None:
    """Wait for currently owned proxy DB workers in deterministic tests."""
    while _proxy_persistence_tasks:
        await asyncio.gather(*tuple(_proxy_persistence_tasks), return_exceptions=True)


@dataclass
class _HttpResponseOpenOwner:
    """Own a route from client creation until ``OpenedHttpResponse`` handoff."""

    route_type: str
    response_mode: str
    proxy_name: str | None
    proxy_bytes: dict[str, int]
    round_id: str | None = None
    proxy_attempt_id: Any | None = None
    proxy_client: Any | None = None
    ctx: Any | None = None
    timing: HttpAttemptTiming | None = None
    transferred: bool = False
    closed: bool = False
    terminal_persistence_scheduled: bool = False

    async def abort(
        self,
        outcome: str,
        error_detail: str,
        *,
        wait_for_persistence: bool = False,
    ) -> None:
        if self.transferred:
            return

        snapshot = (
            self.timing.finish(outcome, error_detail)
            if self.timing is not None
            else None
        )
        if not self.closed:
            self.closed = True
            ctx, self.ctx = self.ctx, None
            client, self.proxy_client = self.proxy_client, None
            try:
                if ctx is not None:
                    await close_response_context(ctx)
            except BaseException:
                pass
            try:
                await close_proxy_client(client)
            except BaseException:
                pass

        if self.proxy_attempt_id is None or self.terminal_persistence_scheduled:
            return
        self.terminal_persistence_scheduled = True
        if snapshot is not None:
            if wait_for_persistence:
                await _persist_frozen_proxy_attempt(
                    self.proxy_attempt_id,
                    snapshot,
                    outcome=outcome,
                    error_detail=error_detail,
                    proxy_bytes=dict(self.proxy_bytes),
                )
            else:
                _schedule_frozen_proxy_attempt_persistence(
                    self.proxy_attempt_id,
                    snapshot,
                    outcome=outcome,
                    error_detail=error_detail,
                    proxy_bytes=self.proxy_bytes,
                )
            return

        # Cancellation can arrive while record_proxy_attempt's worker is in
        # flight, before the authoritative HTTP round timing is constructed.
        # Keep its original started_at and terminalize only the fields known at
        # this pre-round boundary.
        try:
            up, down = proxy_byte_snapshot(self.proxy_bytes)
            await asyncio.to_thread(
                log_db.update_proxy_attempt,
                self.proxy_attempt_id,
                ended_at=time.time(),
                outcome=outcome,
                error_detail=error_detail[:4000],
                bytes_up=up,
                bytes_down=down,
            )
        except Exception:
            pass

    def transfer(self) -> None:
        if self.closed:
            raise RuntimeError("cannot transfer an aborted HTTP response owner")
        self.transferred = True


async def finalize_opened_http_response(
    opened: OpenedHttpResponse,
    outcome: str,
    error_detail: str | None = None,
):
    """Freeze a route round on the event loop, then offload only persistence."""

    if opened.timing is None:
        return None
    snapshot = opened.timing.finish(outcome, error_detail)
    if not opened.terminal_persistence_scheduled:
        opened.terminal_persistence_scheduled = True
        _schedule_frozen_proxy_attempt_persistence(
            opened.proxy_attempt_id,
            snapshot,
            outcome=outcome,
            error_detail=error_detail,
            proxy_bytes=opened.proxy_bytes,
        )
    return snapshot


async def _finish_pre_header_round(
    *,
    owner: _HttpResponseOpenOwner,
    timing: HttpAttemptTiming,
    outcome: str,
    detail: str,
    proxy_name: str | None,
    proxy_bytes: dict,
    persist_dispatch=None,
) -> AttemptResult:
    # The raised I/O result is the terminal boundary.  Freeze before dispatch
    # bookkeeping, socket cleanup, or an executor queue can extend the round.
    timing.finish(outcome, detail)
    result = _attempt_result(
        outcome, detail, bucket=proxy_bytes, proxy_name=proxy_name,
    )
    try:
        if persist_dispatch is not None:
            await persist_dispatch()
    finally:
        await _await_http_owned(owner.abort(outcome, detail))
    return timing.apply_to(result, terminal=False)


class _LateRoundTiming:
    """Proxy setup holder so client construction stays outside business round time."""

    target: HttpAttemptTiming | None = None

    def record_proxy_tcp(self, started_at: float, ended_at: float) -> None:
        if self.target is not None:
            self.target.record_proxy_tcp(started_at, ended_at)


def _headers_for_physical_dispatch(headers) -> dict[str, str]:
    """Copy headers and refresh attempt-scoped CC values without shared mutation."""
    out = dict(headers or {})
    for key in list(out):
        lowered = str(key).lower()
        if lowered == "x-client-request-id":
            out[key] = str(uuid.uuid4())
        elif lowered == "x-stainless-retry-count":
            out[key] = "0"
    return out


async def open_response_with_proxy_chain(
    *,
    channel,
    resolved_model: str,
    upstream_req,
    connect_timeout: int,
    first_byte_timeout: int,
    idle_timeout: int,
    total_timeout: int,
    response_mode: str,
    request_id,
    retry_attempt_id=None,
    proxy_purpose: str | None = None,
) -> OpenedHttpResponse:
    """Open one independent round per HTTP route, retrying pre-header failures."""
    if proxy_purpose:
        route_chain, route_error = _resolve_http_route_chain(
            channel, resolved_model, proxy_purpose=proxy_purpose,
        )
    else:
        # Preserve the long-standing two-argument seam used by transport tests
        # and internal extensions when no purpose override is required.
        route_chain, route_error = _resolve_http_route_chain(channel, resolved_model)
    if route_error is not None:
        return OpenedHttpResponse(error=route_error)
    if response_mode not in ("stream", "non_stream"):
        raise ValueError(f"invalid HTTP response mode: {response_mode!r}")
    round_timeouts = RoundTimeouts.from_config({
        "connect": connect_timeout,
        "firstByte": first_byte_timeout,
        "idle": idle_timeout,
        "total": total_timeout,
    })

    last_pre_header: AttemptResult | None = None
    proxy_attempt_order = 0

    for route_name, connector in route_chain:
        route_type = getattr(connector, "type", "direct") if connector is not None else "direct"
        proxy_client = None
        proxy_name_used = str(route_name) if connector is not None and route_type != "direct" else None
        route_log_name = str(route_name) if connector is not None else "direct"
        proxy_bytes = _new_proxy_bytes()
        owner = _HttpResponseOpenOwner(
            route_type=str(route_type),
            response_mode=response_mode,
            proxy_name=proxy_name_used,
            proxy_bytes=proxy_bytes,
        )
        # Explicit proxy/loopback routes own independent clients. Only the
        # shared route acquires a pool lease, immediately before stream setup.
        client = None
        if bool(getattr(channel, "internal_loopback", False)):
            # The shared client may carry legacy SOCKS settings. A process-local
            # bridge must always use a short-lived trust_env=False direct client.
            proxy_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=round_timeouts.connection + 0.5,
                    read=max(330.0, round_timeouts.total + 1.0),
                    write=30.0,
                    pool=round_timeouts.connection + 0.5,
                ),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                trust_env=False,
                http2=False,
            )
            client = proxy_client
            owner.proxy_client = proxy_client
        proxy_attempt_id = None
        proxy_attempt_order += 1
        proxy_started_at = time.time()
        late_timing = _LateRoundTiming()

        fingerprint_profile = str(getattr(channel, "tls_fingerprint", None) or "").strip()
        fingerprint_route = bool(
            fingerprint_profile
            and not bool(getattr(channel, "internal_loopback", False))
            and fingerprint_supports_route(str(route_type))
        )
        if connector is not None or fingerprint_route:
            try:
                if connector is not None:
                    connector.stats.total_attempts += 1
                    connector.stats.last_attempt_ts = proxy_started_at

                client_timeout = httpx.Timeout(
                    connect=round_timeouts.connection + 0.5,
                    read=max(330.0, round_timeouts.total + 1.0),
                    write=30.0,
                    pool=round_timeouts.connection + 0.5,
                )
                route_counter = _count_proxy_bytes_for(proxy_bytes)
                fingerprint_counter = route_counter
                if fingerprint_route and connector is not None:
                    def fingerprint_counter(up: int = 0, down: int = 0) -> None:
                        connector.record_bytes(up, down)
                        route_counter(up, down)

                if fingerprint_route:
                    fingerprint_auth = None
                    fingerprint_owner = None
                    if connector is not None and str(route_type) == "socks5":
                        fingerprint_proxy = str(getattr(connector, "url", "") or "")
                    elif connector is not None and str(route_type) == "ss2022":
                        target_url = httpx.URL(str(upstream_req.url))
                        if target_url.scheme != "https" or not target_url.host:
                            raise ValueError(
                                "SS2022 fingerprint bridge requires an HTTPS target"
                            )
                        target_port = int(target_url.port or 443)
                        fingerprint_owner = await connector.acquire_curl_proxy(
                            str(target_url.host),
                            target_port,
                            timeout=round_timeouts.connection + 0.5,
                            timing=late_timing,
                        )
                        fingerprint_proxy = fingerprint_owner.proxy_url
                        fingerprint_auth = fingerprint_owner.proxy_auth
                    elif connector is None:
                        # Preserve the legacy SOCKS setting used by the shared
                        # client; an explicit direct connector never inherits it.
                        fingerprint_proxy = str(network.active_socks5_url() or "")
                    else:
                        fingerprint_proxy = ""
                    try:
                        proxy_client = create_impersonated_client(
                            fingerprint_profile,
                            timeout=client_timeout,
                            proxy=fingerprint_proxy,
                            proxy_auth=fingerprint_auth,
                            trust_env=bool(connector is None and not fingerprint_proxy),
                            byte_counter=fingerprint_counter,
                            route_owner=fingerprint_owner,
                        )
                    except BaseException:
                        if fingerprint_owner is not None:
                            await _await_http_owned(fingerprint_owner.aclose())
                        raise
                else:
                    # Disabled fingerprinting preserves the original custom
                    # httpcore backend for every SS2022 route.
                    proxy_client = connector.create_httpx_client(
                        timeout=client_timeout,
                        byte_counter=route_counter,
                        timing=late_timing,
                    )
                client = proxy_client
                owner.proxy_client = proxy_client
            except Exception as exc:
                if connector is not None:
                    connector.stats.total_failures += 1
                    connector.stats.last_error = str(exc)[:200]
                last_pre_header = _attempt_result(
                    "proxy_connect_error",
                    f"proxy client error before upstream round: {exc}",
                    bucket=proxy_bytes,
                    proxy_name=proxy_name_used,
                )
                await _await_http_owned(owner.abort(
                    last_pre_header.outcome,
                    last_pre_header.error_detail or "proxy client error",
                ))
                continue
            except BaseException as exc:
                await _await_http_owned(owner.abort(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "transport_error",
                    f"proxy client setup aborted: {exc}",
                    wait_for_persistence=True,
                ))
                raise

        round_id = str(uuid.uuid4())
        owner.round_id = round_id

        async def record_route_attempt() -> None:
            owner.proxy_attempt_id = await asyncio.to_thread(
                log_db.record_proxy_attempt,
                request_id, retry_attempt_id, proxy_attempt_order,
                route_log_name, time.time(),
                round_id=round_id,
                transport="http",
                request_mode=f"http_{response_mode}",
            )

        if request_id is not None:
            try:
                await _await_http_owned(record_route_attempt())
            except asyncio.CancelledError:
                await _await_http_owned(owner.abort(
                    "cancelled",
                    "upstream HTTP route cancelled while recording proxy attempt",
                    wait_for_persistence=True,
                ))
                raise
            except Exception:
                owner.proxy_attempt_id = None
            except BaseException as exc:
                await _await_http_owned(owner.abort(
                    "transport_error",
                    f"upstream HTTP route aborted while recording proxy attempt: {exc}",
                    wait_for_persistence=True,
                ))
                raise
        proxy_attempt_id = owner.proxy_attempt_id

        # The trace callback itself must remain non-blocking. It captures the
        # authoritative physical-send timestamp; the surrounding coroutine then
        # awaits the SQLite offload before exposing the next lifecycle state.
        dispatch_state: dict[str, Any] = {"at": None, "persisted": False}

        def note_dispatch() -> None:
            if dispatch_state["at"] is None:
                dispatch_state["at"] = time.time()

        async def persist_dispatch() -> None:
            if (
                retry_attempt_id is None
                or dispatch_state["at"] is None
                or dispatch_state["persisted"]
            ):
                return
            dispatch_state["persisted"] = True
            try:
                await _await_http_owned(asyncio.to_thread(
                    log_db.mark_retry_attempt_dispatch,
                    retry_attempt_id,
                    upstream_req.body,
                    dispatch_metadata=getattr(upstream_req, "dispatch_metadata", None),
                    dispatched_at=dispatch_state["at"],
                ))
            except Exception:
                # Billing diagnostics must never break the proxy path.
                pass

        # Authoritative round starts only now: immediately before client.stream.
        timing = HttpAttemptTiming(
            route_type=route_type,
            response_mode=response_mode,
            round_id=round_id,
            on_dispatch=note_dispatch,
        )
        late_timing.target = timing
        owner.timing = timing

        async def trace_with_dispatch(name: str, info: dict[str, Any]) -> None:
            was_dispatched = timing.dispatch_started
            await timing.trace(name, info)
            if not was_dispatched and timing.dispatch_started:
                # The trace callback is async, so the lock/commit offload can be
                # awaited at the exact physical-send boundary without a detached
                # task or delaying visibility until response headers arrive.
                await persist_dispatch()

        try:
            dispatch_headers = _headers_for_physical_dispatch(upstream_req.headers)
            stream_request = HttpStreamRequest(
                method=upstream_req.method,
                url=upstream_req.url,
                headers=dispatch_headers,
                content=upstream_req.body,
                connect_timeout=round_timeouts.connection + 0.5,
                read_timeout=max(330.0, round_timeouts.total + 1.0),
                write_timeout=30.0,
                pool_timeout=round_timeouts.connection + 0.5,
                extensions={
                    "trace": trace_with_dispatch,
                    **({
                        # End the business connection phase without fabricating
                        # low-level httpcore trace intervals.
                        "parrot_transport_lifecycle": {
                            "mark_connection_complete": timing.mark_connection_complete,
                        },
                    } if fingerprint_route else {}),
                },
            )
            hook = getattr(upstream_req, "stream_context_hook", None)
            if hook is None:
                ctx = (
                    _SharedStreamContext(stream_request)
                    if client is None else open_stream(client, stream_request)
                )
            else:
                def physical_context(request):
                    return _SharedStreamContext(request) if client is None else open_stream(client, request)
                ctx = hook(stream_request, physical_context)
            owner.ctx = ctx
        except upstream.ClientUnavailableError:
            # Keep a local client construction failure outside channel scoring
            # and cooldown, as the former get_client() failure path did. A later
            # request can rebuild rather than being filtered out by cooldown.
            await _await_http_owned(owner.abort(
                "guard_error", "shared upstream HTTP client unavailable",
            ))
            raise
        except Exception as exc:
            last_pre_header = _with_timing(timing, _attempt_result(
                connection_lifecycle_outcome(
                    exc, http_phase="pre_headers",
                ) or "transport_error",
                f"send build error: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            ))
            await _await_http_owned(owner.abort(
                last_pre_header.outcome,
                last_pre_header.error_detail or "send build error",
            ))
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            continue
        except BaseException as exc:
            await _await_http_owned(owner.abort(
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "transport_error",
                f"upstream HTTP send build aborted: {exc}",
                wait_for_persistence=True,
            ))
            raise

        try:
            upstream_resp = await timing.wait_for(ctx.__aenter__(), round_timeouts)
            await persist_dispatch()
            if response_mode == "stream" and not timing.connection_complete:
                # High-level API return is the authoritative final-header boundary.
                timing.mark_connection_complete()
        except asyncio.CancelledError:
            await _await_http_owned(_finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome="cancelled",
                detail="upstream HTTP round cancelled before response commit",
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            ))
            raise
        except BusinessTimeoutError as exc:
            detail = f"{exc.outcome} while opening upstream response"
            last_pre_header = await _finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome=exc.outcome,
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            if timing.dispatch_started:
                return OpenedHttpResponse(error=last_pre_header)
            continue
        except httpx.TimeoutException as exc:
            outcome = classify_httpx_timeout(exc)
            detail = f"{outcome}: {exc}"
            last_pre_header = await _finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome=outcome,
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            if timing.dispatch_started:
                return OpenedHttpResponse(error=last_pre_header)
            continue
        except httpx.ConnectError as exc:
            detail = f"connect error: {exc}"
            last_pre_header = await _finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome="connect_error",
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            if timing.dispatch_started:
                return OpenedHttpResponse(error=last_pre_header)
            continue
        except Exception as exc:
            detail = f"transport: {exc}"
            last_pre_header = await _finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome=connection_lifecycle_outcome(
                    exc, http_phase="pre_headers",
                ) or "transport_error",
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            if timing.dispatch_started:
                return OpenedHttpResponse(error=last_pre_header)
            continue
        except BaseException as exc:
            await _await_http_owned(_finish_pre_header_round(
                owner=owner,
                timing=timing,
                outcome=(
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "transport_error"
                ),
                detail=f"upstream HTTP round aborted before response commit: {exc}",
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                persist_dispatch=persist_dispatch,
            ))
            raise

        open_snapshot = timing.snapshot()
        connect_ms = open_snapshot.connect_ms
        try:
            await _await_http_owned(_persist_frozen_proxy_attempt(
                proxy_attempt_id,
                open_snapshot,
                outcome="open",
                error_detail=None,
                proxy_bytes=proxy_bytes,
            ))
            if connector is not None:
                connector.stats.total_successes += 1
                connector.stats.last_success_ts = time.time()
                if connect_ms is not None:
                    connector.stats.last_latency_ms = connect_ms

            opened = OpenedHttpResponse(
                ctx=ctx,
                response=upstream_resp,
                connect_ms=connect_ms,
                timing=timing,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                round_timeouts=round_timeouts,
            )
            owner.transfer()
            return opened
        except asyncio.CancelledError:
            await _await_http_owned(owner.abort(
                "cancelled",
                "upstream HTTP round cancelled before response handoff",
                wait_for_persistence=True,
            ))
            raise
        except BaseException as exc:
            await _await_http_owned(owner.abort(
                "transport_error",
                f"upstream HTTP round aborted before response handoff: {exc}",
                wait_for_persistence=True,
            ))
            raise

    return OpenedHttpResponse(
        error=last_pre_header or AttemptResult(
            outcome="proxy_connect_error",
            error_detail="proxy route has no usable target",
        )
    )
