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

from .. import blacklist, log_db, upstream
from ..providers import registry as provider_registry
from ..protocols import errors as protocol_errors
from ..protocols.commit_gate import SseCommitGate
from ..protocols.runtime import AttemptResult, make_stream_translator, toolkit_for_channel
from .base import metadata_from_response
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
) -> bytes:
    """Read a response using only non-empty raw bytes as business activity."""

    if timing is not None:
        timing.start_response_body_wait()
    parts: list[bytes] = []
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
) -> AttemptResult:
    """Read and normalize a non-2xx/3xx response under round deadlines."""

    try:
        raw = await _read_response_bytes(response, timing, round_timeouts)
    except asyncio.CancelledError:
        await close_response_context(ctx)
        raise
    except BusinessTimeoutError as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome=exc.outcome,
            error_detail=f"{exc.outcome} reading HTTP error body",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))
    except httpx.TimeoutException as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome=classify_httpx_timeout(exc),
            error_detail=f"read HTTP error body timeout: {exc}",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))
    except Exception as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return _with_timing(timing, AttemptResult(
            outcome="transport_error",
            error_detail=f"read http error body: {exc}",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        ))

    err_text = raw.decode("utf-8", errors="replace")
    status = response.status_code
    await close_response_context(ctx)

    up, down = proxy_byte_snapshot(proxy_bytes)
    outcome = "http_auth_error" if status in (401, 403) else "http_error"
    return _with_timing(timing, AttemptResult(
        outcome=outcome,
        http_status=status,
        connect_ms=connect_ms,
        error_detail=f"HTTP {status}: {err_text[:2000]}",
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
) -> HttpBodyReadResult:
    """Read a non-stream body; first-byte is inapplicable, idle starts on body."""

    try:
        raw = await _read_response_bytes(response, timing, round_timeouts)
    except asyncio.CancelledError:
        await close_response_context(ctx)
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
                outcome="transport_error",
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
) -> StreamAsNonStreamResult:
    """Aggregate an upstream SSE response into one non-stream JSON object."""
    raw_buf = bytearray()
    aiter = response.aiter_bytes()

    if timing is not None:
        timing.start_response_body_wait()
    try:
        first_chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
    except asyncio.CancelledError:
        await close_response_context(ctx)
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
                outcome="transport_error",
                error_detail=f"first byte transport: {exc} [stream-only→non-stream]",
            ))
        )

    first_byte_ms = timing.snapshot().first_byte_ms if timing is not None else None

    first_chunk_restored = await provider_registry.restore_response_bytes(
        channel,
        first_chunk,
        dynamic_map=dynamic_map,
        translator_ctx=translator_ctx,
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
            )
        )

    builder = toolkit["stream_builder"]()
    tracker = toolkit["stream_tracker"]()
    builder.feed(first_chunk_restored)
    tracker.feed(first_chunk_restored)
    raw_buf.extend(
        first_chunk_restored
        if isinstance(first_chunk_restored, (bytes, bytearray))
        else first_chunk_restored.encode("utf-8", errors="replace")
    )
    if err := _stream_tracker_error_result(
        tracker,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        response_status=response.status_code,
        translator_ctx=translator_ctx,
    ):
        await close_response_context(ctx)
        return StreamAsNonStreamResult(error=err)

    while True:
        try:
            chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
        except asyncio.CancelledError:
            await close_response_context(ctx)
            raise
        except BusinessTimeoutError as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=_with_timing(timing, AttemptResult(
                    outcome=exc.outcome,
                    error_detail=f"{exc.outcome} reading SSE [stream-only→non-stream]",
                ))
            )
        except StopAsyncIteration:
            if timing is not None:
                timing.mark_io_complete()
            break
        except httpx.TimeoutException as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=_with_timing(timing, AttemptResult(
                    outcome=classify_httpx_timeout(exc),
                    error_detail=f"read SSE timeout: {exc} [stream-only→non-stream]",
                ))
            )
        except Exception as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=_with_timing(timing, AttemptResult(
                    outcome="transport_error",
                    error_detail=f"read SSE chunk: {exc} [stream-only→non-stream]",
                ))
            )
        restored_chunk = await provider_registry.restore_response_bytes(
            channel,
            chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
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
            return StreamAsNonStreamResult(error=err)

    response_headers = metadata_from_response(response).forward_headers()
    await close_response_context(ctx)
    response_body_text = bytes(raw_buf).decode("utf-8", errors="replace")

    if not getattr(tracker, "saw_stream_end", False):
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="upstream_malformed",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail="stream ended without a terminal SSE event [stream-only→non-stream]",
                full_response_text=response_body_text,
            )
        )

    if not builder.has_any_event:
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="upstream_malformed",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail="stream ended without any SSE event [stream-only→non-stream]",
                full_response_text=response_body_text,
            )
        )

    obj = builder.to_full_json(fallback_model=resolved_model)
    try:
        usage_from_tracker = tracker.usage if hasattr(tracker, "usage") else None
        if usage_from_tracker:
            obj.setdefault("usage", usage_from_tracker)
    except Exception:
        pass

    total_ms = timing.snapshot(terminal=True).total_ms if timing is not None else None
    usage = toolkit["extract_usage_json"](obj)
    assistant_msg = {"role": "assistant", "content": obj.get("output") or []}

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

    async def feed_restored(restored: bytes) -> tuple[list[bytes], dict | None]:
        tracker.feed(restored)
        builder.feed(restored)
        result = commit_gate.feed(restored)
        return result.downstream_chunks, result.error_event

    restored_first = await provider_registry.restore_response_bytes(
        channel,
        first_chunk,
        dynamic_map=dynamic_map,
        translator_ctx=translator_ctx,
    )
    downstream_chunks, err = await feed_restored(restored_first)
    if downstream_chunks or err is not None:
        return downstream_chunks, err

    while True:
        chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
        restored = await provider_registry.restore_response_bytes(
            channel,
            chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
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
) -> HttpStreamStartResult:
    """Read through the pre-commit SSE boundary for an HTTP stream response."""
    aiter = response.aiter_bytes()

    if timing is not None:
        timing.start_response_body_wait()

    try:
        first_chunk = await _next_nonempty_http_chunk(aiter, timing, round_timeouts)
    except asyncio.CancelledError:
        await close_response_context(ctx)
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
                outcome="transport_error",
                error_detail=f"first byte transport: {exc}",
            ))
        )

    first_byte_ms = timing.snapshot().first_byte_ms if timing is not None else None

    toolkit = toolkit_for_channel(channel)
    tracker = toolkit["stream_tracker"]()
    builder = toolkit["stream_builder"]()
    ch_proto = getattr(channel, "protocol", "anthropic")
    stream_translator = make_stream_translator(translator_ctx)

    def _attach_precommit_response(result: AttemptResult) -> AttemptResult:
        """Keep received pre-commit SSE data available to terminal error logging."""
        try:
            full_response = tracker.get_full_response()
        except Exception:
            full_response = None
        if full_response:
            result.full_response_text = full_response
        return result

    if ch_proto == "openai-responses" or stream_translator is not None:
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
                    outcome="transport_error",
                    error_detail=f"first downstream chunk transport: {exc}",
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
        first_chunk_restored = await provider_registry.restore_response_bytes(
            channel,
            first_chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
        # Record the restored bytes before classifying a first-event error so
        # pre-commit upstream error payloads follow the same persistence path.
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
                outcome="transport_error",
            )

        restored = await provider_registry.restore_response_bytes(
            channel,
            chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
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


def _resolve_http_route_chain(channel, resolved_model: str) -> tuple[list[tuple[str, Any | None]], AttemptResult | None]:
    """Resolve an HTTP route without silently bypassing configured proxies.

    Direct remains the normal default when no new-proxy route exists.  Once a
    non-direct route is configured, parser/connector failures are fail-closed
    unless the user explicitly enabled ``routing.directFallback``.
    """
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

        chain = pm.resolve_proxy_chain(**proxy_route_kwargs(channel, resolved_model))
        route_chain: list[tuple[str, Any | None]] = []
        for proxy_name in chain:
            connector = pm.get_connector(proxy_name)
            if connector is None:
                continue
            if getattr(connector, "type", "") == "direct":
                route_chain.append(("direct", None))
            else:
                route_chain.append((proxy_name, connector))

        if route_chain:
            if pm.direct_fallback_enabled() and not any(name == "direct" for name, _ in route_chain):
                route_chain.append(("direct", None))
            return route_chain, None

        if pm.direct_fallback_enabled():
            print(f"[proxy] HTTP route has no usable target; using enabled direct fallback: {chain}")
            return [("direct", None)], None
        return [], AttemptResult(
            outcome="proxy_connect_error",
            error_detail=f"proxy route has no valid target: {chain}",
        )
    except Exception as exc:
        if pm.direct_fallback_enabled():
            print(f"[proxy] HTTP route resolution failed; using enabled direct fallback: {exc}")
            return [("direct", None)], None
        if configured or pm.has_non_direct_routing_rules():
            return [], AttemptResult(
                outcome="proxy_connect_error",
                error_detail=f"proxy route resolution failed: {exc}",
            )
        # No configured network rule: direct is the intended default, not a
        # fallback downgrade.
        return [("direct", None)], None


def _persist_proxy_attempt_timing(
    proxy_attempt_id,
    timing: HttpAttemptTiming,
    *,
    outcome: str | None,
    error_detail: str | None,
    proxy_bytes: dict,
    terminal: bool,
):
    snapshot = (
        timing.finish(outcome or "transport_error", error_detail)
        if terminal else timing.snapshot()
    )
    if proxy_attempt_id is None:
        return snapshot
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
    return snapshot


def finalize_opened_http_response(
    opened: OpenedHttpResponse,
    outcome: str,
    error_detail: str | None = None,
):
    """Idempotently terminalize and persist one opened HTTP route round."""

    if opened.timing is None:
        return None
    return _persist_proxy_attempt_timing(
        opened.proxy_attempt_id,
        opened.timing,
        outcome=outcome,
        error_detail=error_detail,
        proxy_bytes=opened.proxy_bytes,
        terminal=True,
    )


async def _finish_pre_header_round(
    *,
    ctx,
    proxy_client,
    proxy_attempt_id,
    timing: HttpAttemptTiming,
    outcome: str,
    detail: str,
    proxy_name: str | None,
    proxy_bytes: dict,
) -> AttemptResult:
    if ctx is not None:
        await close_response_context(ctx)
    await close_proxy_client(proxy_client)
    result = _attempt_result(
        outcome, detail, bucket=proxy_bytes, proxy_name=proxy_name,
    )
    _persist_proxy_attempt_timing(
        proxy_attempt_id,
        timing,
        outcome=outcome,
        error_detail=detail,
        proxy_bytes=proxy_bytes,
        terminal=True,
    )
    return timing.apply_to(result, terminal=False)


class _LateRoundTiming:
    """Proxy setup holder so client construction stays outside business round time."""

    target: HttpAttemptTiming | None = None

    def record_proxy_tcp(self, started_at: float, ended_at: float) -> None:
        if self.target is not None:
            self.target.record_proxy_tcp(started_at, ended_at)


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
) -> OpenedHttpResponse:
    """Open one independent round per HTTP route, retrying pre-header failures."""
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
        proxy_name_used = str(route_name) if connector is not None else None
        route_log_name = str(route_name) if connector is not None else "direct"
        proxy_bytes = _new_proxy_bytes()
        client = upstream.get_client()
        proxy_attempt_id = None
        proxy_attempt_order += 1
        proxy_started_at = time.time()
        late_timing = _LateRoundTiming()

        if connector is not None:
            try:
                connector.stats.total_attempts += 1
                connector.stats.last_attempt_ts = proxy_started_at
                proxy_client = connector.create_httpx_client(
                    timeout=httpx.Timeout(
                        connect=round_timeouts.connection + 0.5,
                        read=max(330.0, round_timeouts.total + 1.0),
                        write=30.0,
                        pool=round_timeouts.connection + 0.5,
                    ),
                    byte_counter=_count_proxy_bytes_for(proxy_bytes),
                    timing=late_timing,
                )
                client = proxy_client
            except Exception as exc:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
                last_pre_header = _attempt_result(
                    "proxy_connect_error",
                    f"proxy client error before upstream round: {exc}",
                    bucket=proxy_bytes,
                    proxy_name=proxy_name_used,
                )
                await close_proxy_client(proxy_client)
                continue

        round_id = str(uuid.uuid4())
        try:
            proxy_attempt_id = log_db.record_proxy_attempt(
                request_id, retry_attempt_id, proxy_attempt_order,
                route_log_name, time.time(),
                round_id=round_id,
                transport="http",
                request_mode=f"http_{response_mode}",
            )
        except Exception:
            proxy_attempt_id = None

        # Authoritative round starts only now: immediately before client.stream.
        timing = HttpAttemptTiming(
            route_type=route_type,
            response_mode=response_mode,
            round_id=round_id,
        )
        late_timing.target = timing

        try:
            ctx = open_stream(
                client,
                HttpStreamRequest(
                    method=upstream_req.method,
                    url=upstream_req.url,
                    headers=upstream_req.headers,
                    content=upstream_req.body,
                    connect_timeout=round_timeouts.connection + 0.5,
                    read_timeout=max(330.0, round_timeouts.total + 1.0),
                    write_timeout=30.0,
                    pool_timeout=round_timeouts.connection + 0.5,
                    extensions={"trace": timing.trace},
                ),
            )
        except Exception as exc:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            last_pre_header = _with_timing(timing, _attempt_result(
                "transport_error",
                f"send build error: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            ))
            _persist_proxy_attempt_timing(
                proxy_attempt_id,
                timing,
                outcome=last_pre_header.outcome,
                error_detail=last_pre_header.error_detail,
                proxy_bytes=proxy_bytes,
                terminal=True,
            )
            continue

        try:
            upstream_resp = await timing.wait_for(ctx.__aenter__(), round_timeouts)
            if response_mode == "stream" and not timing.connection_complete:
                # High-level API return is the authoritative final-header boundary.
                timing.mark_connection_complete()
        except asyncio.CancelledError:
            await asyncio.shield(_finish_pre_header_round(
                ctx=ctx,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                outcome="cancelled",
                detail="upstream HTTP round cancelled before response commit",
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
            ))
            raise
        except BusinessTimeoutError as exc:
            detail = f"{exc.outcome} while opening upstream response"
            last_pre_header = await _finish_pre_header_round(
                ctx=ctx,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                outcome=exc.outcome,
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            continue
        except httpx.TimeoutException as exc:
            outcome = classify_httpx_timeout(exc)
            detail = f"{outcome}: {exc}"
            last_pre_header = await _finish_pre_header_round(
                ctx=ctx,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                outcome=outcome,
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            continue
        except httpx.ConnectError as exc:
            detail = f"connect error: {exc}"
            last_pre_header = await _finish_pre_header_round(
                ctx=ctx,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                outcome="connect_error",
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            continue
        except Exception as exc:
            detail = f"transport: {exc}"
            last_pre_header = await _finish_pre_header_round(
                ctx=ctx,
                proxy_client=proxy_client,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                outcome="transport_error",
                detail=detail,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
            )
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = detail[:200]
            continue

        connect_ms = timing.snapshot().connect_ms
        _persist_proxy_attempt_timing(
            proxy_attempt_id,
            timing,
            outcome="open",
            error_detail=None,
            proxy_bytes=proxy_bytes,
            terminal=False,
        )
        if connector is not None:
            connector.stats.total_successes += 1
            connector.stats.last_success_ts = time.time()
            if connect_ms is not None:
                connector.stats.last_latency_ms = connect_ms

        return OpenedHttpResponse(
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

    return OpenedHttpResponse(
        error=last_pre_header or AttemptResult(
            outcome="proxy_connect_error",
            error_detail="proxy route has no usable target",
        )
    )
