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


@dataclass
class OpenedHttpResponse:
    ctx: Any | None = None
    response: httpx.Response | None = None
    connect_ms: int = 0
    proxy_name: str | None = None
    proxy_bytes: dict[str, int] = field(default_factory=lambda: {"up": 0, "down": 0})
    proxy_client: Any | None = None
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


def _stream_tracker_error_result(
    tracker,
    *,
    connect_ms: int,
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
    deadline_ts: float,
    connect_ms: int,
    proxy_name: str | None = None,
    proxy_bytes: dict | None = None,
    translator_ctx: dict | None = None,
) -> AttemptResult:
    """Read and normalize a non-2xx/3xx HTTP response without proxy failover."""
    read_timeout = max(1.0, deadline_ts - time.time())
    try:
        raw = await asyncio.wait_for(response.aread(), timeout=read_timeout)
    except asyncio.TimeoutError:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return AttemptResult(
            outcome="total_timeout",
            connect_ms=connect_ms,
            error_detail=f"total timeout reading error body (> {int(read_timeout)}s)",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        )
    except Exception as exc:
        await close_response_context(ctx)
        up, down = proxy_byte_snapshot(proxy_bytes)
        return AttemptResult(
            outcome="transport_error",
            connect_ms=connect_ms,
            error_detail=f"read http error body: {exc}",
            proxy_name=proxy_name,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        )

    err_text = raw.decode("utf-8", errors="replace")
    status = response.status_code
    await close_response_context(ctx)

    up, down = proxy_byte_snapshot(proxy_bytes)
    outcome = "http_auth_error" if status in (401, 403) else "http_error"
    return AttemptResult(
        outcome=outcome,
        http_status=status,
        connect_ms=connect_ms,
        error_detail=f"HTTP {status}: {err_text[:2000]}",
        proxy_name=proxy_name,
        proxy_bytes_up=up,
        proxy_bytes_down=down,
        translator_ctx=translator_ctx,
    )


async def read_non_stream_body(
    ctx,
    response: httpx.Response,
    *,
    deadline_ts: float,
    connect_ms: int,
) -> HttpBodyReadResult:
    """Read a non-stream response body and close the response context."""
    read_timeout = max(1.0, deadline_ts - time.time())
    try:
        raw = await asyncio.wait_for(response.aread(), timeout=read_timeout)
    except asyncio.TimeoutError:
        await close_response_context(ctx)
        return HttpBodyReadResult(
            error=AttemptResult(
                outcome="total_timeout",
                connect_ms=connect_ms,
                error_detail=f"total timeout reading non-stream body (> {int(read_timeout)}s)",
            )
        )
    except Exception as exc:
        await close_response_context(ctx)
        return HttpBodyReadResult(
            error=AttemptResult(
                outcome="transport_error",
                connect_ms=connect_ms,
                error_detail=f"read non-stream body: {exc}",
            )
        )

    response_headers = metadata_from_response(response).forward_headers()
    await close_response_context(ctx)

    if not raw:
        return HttpBodyReadResult(
            error=AttemptResult(
                outcome="closed_before_first_byte",
                connect_ms=connect_ms,
                error_detail="upstream empty body",
            )
        )
    return HttpBodyReadResult(raw=raw, response_headers=response_headers)


async def aggregate_stream_as_non_stream_response(
    ctx,
    response: httpx.Response,
    channel,
    resolved_model: str,
    *,
    dynamic_map: dict | None,
    connect_ms: int,
    start_time: float,
    deadline_ts: float,
    total_timeout: int,
    first_byte_timeout: int,
    idle_timeout: int,
    translator_ctx: dict | None = None,
) -> StreamAsNonStreamResult:
    """Aggregate an upstream SSE response into one non-stream JSON object."""
    raw_buf = bytearray()
    aiter = response.aiter_bytes()

    first_wait = min(first_byte_timeout, max(1, int(deadline_ts - time.time())))
    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=first_wait)
    except asyncio.TimeoutError:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="first_byte_timeout",
                connect_ms=connect_ms,
                error_detail=f"first byte timeout (> {first_wait}s) [stream-only→non-stream]",
            )
        )
    except StopAsyncIteration:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="closed_before_first_byte",
                connect_ms=connect_ms,
                error_detail="upstream closed stream before first byte [stream-only→non-stream]",
            )
        )
    except Exception as exc:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="transport_error",
                connect_ms=connect_ms,
                error_detail=f"first byte transport: {exc} [stream-only→non-stream]",
            )
        )

    first_byte_ms = int((time.time() - start_time) * 1000)
    if not first_chunk:
        await close_response_context(ctx)
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="closed_before_first_byte",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail="upstream sent empty first chunk [stream-only→non-stream]",
            )
        )

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
        now = time.time()
        if now >= deadline_ts:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=AttemptResult(
                    outcome="total_timeout",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=f"total timeout reading SSE (> {total_timeout}s) [stream-only→non-stream]",
                )
            )
        wait_s = max(1, min(idle_timeout, int(deadline_ts - now)))
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_s)
        except asyncio.TimeoutError:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=AttemptResult(
                    outcome="idle_timeout",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=f"idle timeout (> {idle_timeout}s) [stream-only→non-stream]",
                )
            )
        except StopAsyncIteration:
            break
        except Exception as exc:
            await close_response_context(ctx)
            return StreamAsNonStreamResult(
                error=AttemptResult(
                    outcome="transport_error",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=f"read SSE chunk: {exc} [stream-only→non-stream]",
                )
            )
        if not chunk:
            continue
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

    if not builder.has_any_event:
        return StreamAsNonStreamResult(
            error=AttemptResult(
                outcome="upstream_malformed",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail="stream ended without any SSE event [stream-only→non-stream]",
            )
        )

    obj = builder.to_full_json(fallback_model=resolved_model)
    try:
        usage_from_tracker = tracker.usage if hasattr(tracker, "usage") else None
        if usage_from_tracker:
            obj.setdefault("usage", usage_from_tracker)
    except Exception:
        pass

    total_ms = int((time.time() - start_time) * 1000)
    usage = toolkit["extract_usage_json"](obj)
    assistant_msg = {"role": "assistant", "content": obj.get("output") or []}
    response_body_text = bytes(raw_buf).decode("utf-8", errors="replace")

    return StreamAsNonStreamResult(
        obj=obj,
        response_headers=response_headers,
        response_body_text=response_body_text,
        usage=usage,
        assistant_msg=assistant_msg,
        first_byte_ms=first_byte_ms,
        total_ms=total_ms,
    )


def _remaining_ms(deadline_ts: float) -> int:
    return max(0, int((deadline_ts - time.time()) * 1000))


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
        remaining = _remaining_ms(deadline_ts)
        if remaining <= 0:
            raise asyncio.TimeoutError("upstream total timeout before first downstream chunk")
        wait_sec = min(idle_timeout, max(1, remaining / 1000))
        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_sec)
        if not chunk:
            continue
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
    connect_ms: int,
    deadline_ts: float,
    first_byte_timeout: int,
    idle_timeout: int,
    ingress_protocol: str,
    translator_ctx: dict | None = None,
) -> HttpStreamStartResult:
    """Read through the pre-commit SSE boundary for an HTTP stream response."""
    aiter = response.aiter_bytes()

    t_first_start = time.time()
    remaining_ms = _remaining_ms(deadline_ts)
    first_wait = min(first_byte_timeout, max(1, remaining_ms / 1000))

    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=first_wait)
    except asyncio.TimeoutError:
        await close_response_context(ctx)
        if _remaining_ms(deadline_ts) <= 0:
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="total_timeout",
                    connect_ms=connect_ms,
                    error_detail="total timeout during first byte wait",
                )
            )
        return HttpStreamStartResult(
            error=AttemptResult(
                outcome="first_byte_timeout",
                connect_ms=connect_ms,
                error_detail=f"first byte timeout > {first_byte_timeout}s",
            )
        )
    except StopAsyncIteration:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=AttemptResult(
                outcome="closed_before_first_byte",
                connect_ms=connect_ms,
                error_detail="upstream closed stream before first byte",
            )
        )
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=AttemptResult(
                outcome="transport_error",
                connect_ms=connect_ms,
                error_detail=f"first byte transport: {exc}",
            )
        )

    first_byte_ms = int((time.time() - t_first_start) * 1000 + connect_ms)
    if not first_chunk:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=AttemptResult(
                outcome="closed_before_first_byte",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail="upstream sent empty first chunk",
            )
        )

    toolkit = toolkit_for_channel(channel)
    tracker = toolkit["stream_tracker"]()
    builder = toolkit["stream_builder"]()
    ch_proto = getattr(channel, "protocol", "anthropic")
    stream_translator = make_stream_translator(translator_ctx)

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
                translator_ctx=translator_ctx,
            )
        except asyncio.TimeoutError:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="first_byte_timeout",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=f"first downstream chunk timeout > {idle_timeout}s",
                )
            )
        except StopAsyncIteration:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="closed_before_first_byte",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail="upstream closed stream before first downstream chunk",
                )
            )
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="transport_error",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=f"first downstream chunk transport: {exc}",
                )
            )
        if pre_visible_error:
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="upstream_error_json",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=json.dumps(pre_visible_error.get("error", pre_visible_error), ensure_ascii=False)[:2000],
                    translator_ctx=translator_ctx,
                )
            )
    else:
        first_chunk_restored = await provider_registry.restore_response_bytes(
            channel,
            first_chunk,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
        first_event = toolkit["first_event_parser"](first_chunk_restored)
        if first_event and (
            first_event.get("type") == "error"
            or isinstance(first_event.get("error"), dict)
            or first_event.get("_event_name") == "error"
        ):
            await close_response_context(ctx)
            return HttpStreamStartResult(
                error=AttemptResult(
                    outcome="upstream_error_json",
                    connect_ms=connect_ms,
                    first_byte_ms=first_byte_ms,
                    error_detail=json.dumps(first_event.get("error", first_event), ensure_ascii=False)[:2000],
                    translator_ctx=translator_ctx,
                )
            )
        tracker.feed(first_chunk_restored)
        builder.feed(first_chunk_restored)
        if stream_translator is not None:
            first_downstream_chunks = list(stream_translator.feed(first_chunk_restored))
        else:
            first_downstream_chunks = [first_chunk_restored]

    bl_target = b"".join(first_downstream_chunks)
    bl_hit = blacklist.match(bl_target, getattr(channel, "key", ""))
    if bl_hit:
        await close_response_context(ctx)
        return HttpStreamStartResult(
            error=AttemptResult(
                outcome="blacklist_hit",
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                error_detail=f"blacklist: {bl_hit}",
            )
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
    translator_ctx: dict | None = None,
) -> HttpStreamReadStep:
    """Read and normalize one post-commit HTTP SSE stream step."""
    while True:
        remaining = _remaining_ms(deadline_ts)
        if remaining <= 0:
            return HttpStreamReadStep(
                kind="error",
                err_type="timeout_error",
                message=f"upstream total timeout > {int((deadline_ts - start_time))}s",
                outcome="total_timeout",
            )

        wait_sec = min(idle_timeout, max(1, remaining / 1000))
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_sec)
        except asyncio.TimeoutError:
            if _remaining_ms(deadline_ts) <= 0:
                return HttpStreamReadStep(
                    kind="error",
                    err_type="timeout_error",
                    message="upstream total timeout",
                    outcome="total_timeout",
                )
            return HttpStreamReadStep(
                kind="error",
                err_type="timeout_error",
                message=f"upstream idle timeout > {idle_timeout}s",
                outcome="idle_timeout",
            )
        except StopAsyncIteration:
            return HttpStreamReadStep(kind="end")
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
            return HttpStreamReadStep(
                kind="error",
                err_type="api_error",
                message=f"stream transport error: {exc}",
                outcome="transport_error",
            )

        if not chunk:
            continue

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
    route_chain: list[tuple[str, Any | None]] = []
    try:
        from ..proxy import manager as pm
        pm.init()
        if pm.is_configured():
            chain = pm.resolve_proxy_chain(**proxy_route_kwargs(channel, resolved_model))
            valid_seen = False
            for proxy_name in chain:
                connector = pm.get_connector(proxy_name)
                if connector is None:
                    continue
                valid_seen = True
                if getattr(connector, "type", "") == "direct":
                    route_chain.append(("direct", None))
                else:
                    route_chain.append((proxy_name, connector))
            if not valid_seen:
                return [], AttemptResult(
                    outcome="proxy_connect_error",
                    error_detail=f"proxy route has no valid target: {chain}",
                )
    except Exception:
        route_chain = []
    if not route_chain:
        route_chain = [("direct", None)]
    return route_chain, None


async def open_response_with_proxy_chain(
    *,
    channel,
    resolved_model: str,
    upstream_req,
    deadline_ts: float,
    connect_timeout: int,
    request_id: str,
    retry_attempt_id: int | None,
) -> OpenedHttpResponse:
    """Open upstream HTTP response headers, retrying only pre-header proxy failures."""
    route_chain, route_error = _resolve_http_route_chain(channel, resolved_model)
    if route_error is not None:
        return OpenedHttpResponse(error=route_error)

    last_pre_header: AttemptResult | None = None
    proxy_attempt_order = 0

    for route_name, connector in route_chain:
        proxy_client = None
        proxy_name_used = None
        proxy_bytes = _new_proxy_bytes()
        client = upstream.get_client()
        proxy_attempt_id: int | None = None
        proxy_attempt_order += 1
        proxy_started_at = time.time()

        if connector is not None:
            proxy_name_used = str(route_name)
            try:
                proxy_attempt_id = log_db.record_proxy_attempt(
                    request_id, retry_attempt_id, proxy_attempt_order,
                    proxy_name_used, proxy_started_at,
                )
            except Exception:
                proxy_attempt_id = None
            try:
                connector.stats.total_attempts += 1
                connector.stats.last_attempt_ts = proxy_started_at
                proxy_client = connector.create_httpx_client(
                    timeout=httpx.Timeout(
                        connect=connect_timeout,
                        read=330,
                        write=30,
                        pool=connect_timeout,
                    ),
                    byte_counter=_count_proxy_bytes_for(proxy_bytes),
                )
                client = proxy_client
            except Exception as exc:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
                last_pre_header = _attempt_result(
                    "proxy_connect_error",
                    f"proxy client error: {exc}",
                    bucket=proxy_bytes,
                    proxy_name=proxy_name_used,
                )
                if proxy_attempt_id is not None:
                    try:
                        log_db.update_proxy_attempt(
                            proxy_attempt_id,
                            ended_at=time.time(),
                            outcome=last_pre_header.outcome,
                            error_detail=(last_pre_header.error_detail or "")[:4000],
                            bytes_up=proxy_bytes.get("up"),
                            bytes_down=proxy_bytes.get("down"),
                        )
                    except Exception:
                        pass
                await close_proxy_client(proxy_client)
                continue

        t_send = time.time()
        remaining = max(1.0, deadline_ts - t_send)

        try:
            ctx = open_stream(
                client,
                HttpStreamRequest(
                    method=upstream_req.method,
                    url=upstream_req.url,
                    headers=upstream_req.headers,
                    content=upstream_req.body,
                    connect_timeout=connect_timeout,
                    read_timeout=remaining,
                    write_timeout=30.0,
                    pool_timeout=connect_timeout,
                ),
            )
        except Exception as exc:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            last_pre_header = _attempt_result(
                "transport_error",
                f"send build error: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            continue

        enter_timeout = max(1.0, deadline_ts - time.time())
        try:
            upstream_resp = await asyncio.wait_for(ctx.__aenter__(), timeout=enter_timeout)
        except asyncio.TimeoutError:
            await close_proxy_client(proxy_client)
            last_pre_header = _attempt_result(
                "total_timeout",
                f"total timeout during connect/headers (> {int(enter_timeout)}s)",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id,
                        connect_ms=int((time.time() - t_send) * 1000),
                        ended_at=time.time(),
                        outcome=last_pre_header.outcome,
                        error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=proxy_bytes.get("up"),
                        bytes_down=proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            return OpenedHttpResponse(error=last_pre_header)
        except httpx.ConnectTimeout:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = f"connect timeout > {connect_timeout}s"
            last_pre_header = _attempt_result(
                "connect_timeout",
                f"connect timeout > {connect_timeout}s",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id,
                        connect_ms=int((time.time() - t_send) * 1000),
                        ended_at=time.time(),
                        outcome=last_pre_header.outcome,
                        error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=proxy_bytes.get("up"),
                        bytes_down=proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except httpx.ConnectError as exc:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            last_pre_header = _attempt_result(
                "connect_error",
                f"connect error: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id,
                        connect_ms=int((time.time() - t_send) * 1000),
                        ended_at=time.time(),
                        outcome=last_pre_header.outcome,
                        error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=proxy_bytes.get("up"),
                        bytes_down=proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except httpx.TimeoutException as exc:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            last_pre_header = _attempt_result(
                "connect_timeout",
                f"timeout: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id,
                        connect_ms=int((time.time() - t_send) * 1000),
                        ended_at=time.time(),
                        outcome=last_pre_header.outcome,
                        error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=proxy_bytes.get("up"),
                        bytes_down=proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except Exception as exc:
            await close_proxy_client(proxy_client)
            if connector is not None:
                connector.stats.total_failures += 1
                connector.stats.last_error = str(exc)[:200]
            last_pre_header = _attempt_result(
                "transport_error",
                f"transport: {exc}",
                bucket=proxy_bytes,
                proxy_name=proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id,
                        connect_ms=int((time.time() - t_send) * 1000),
                        ended_at=time.time(),
                        outcome=last_pre_header.outcome,
                        error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=proxy_bytes.get("up"),
                        bytes_down=proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue

        connect_ms = int((time.time() - t_send) * 1000)
        if proxy_attempt_id is not None:
            try:
                log_db.update_proxy_attempt(
                    proxy_attempt_id,
                    connect_ms=connect_ms,
                    ended_at=time.time(),
                    outcome="connected",
                    bytes_up=proxy_bytes.get("up"),
                    bytes_down=proxy_bytes.get("down"),
                )
            except Exception:
                pass
        if connector is not None:
            connector.stats.total_successes += 1
            connector.stats.last_success_ts = time.time()
            connector.stats.last_latency_ms = connect_ms

        return OpenedHttpResponse(
            ctx=ctx,
            response=upstream_resp,
            connect_ms=connect_ms,
            proxy_name=proxy_name_used,
            proxy_bytes=proxy_bytes,
            proxy_client=proxy_client,
        )

    return OpenedHttpResponse(
        error=last_pre_header or AttemptResult(
            outcome="proxy_connect_error",
            error_detail="proxy route has no usable target",
        )
    )
