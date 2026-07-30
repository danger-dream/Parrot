"""WebSocket transport runtime helpers.

Phase 9 keeps request orchestration, DB writes, scoring, and downstream close
semantics in callers while moving reusable WS transport mechanics here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse, urlunparse

import websockets

from .. import blacklist
from ..protocols import errors as protocol_errors
from ..protocols.runtime import (
    is_responses_ws_visible_event_type,
    is_retryable_responses_ws_error_before_accept,
    parse_wrapped_responses_ws_error,
    responses_ws_error_detail,
)
from ..proxy.connector import (
    DirectConnector,
    SOCKS5Connector,
    SS2022Connector,
    SS2022DuplexBridge,
)
from .timing import BusinessTimeoutError, RoundTimeouts, WsAttemptTiming
from .websocket import event_type as ws_event_type, frame_size as ws_frame_size


@dataclass
class WsProxyBytes:
    up: int = 0
    down: int = 0

    def count(self, up: int = 0, down: int = 0) -> None:
        self.up += int(up or 0)
        self.down += int(down or 0)


class ManagedWsConnection:
    """WebSocket transport owner followed by its SS2022 bridge owner."""

    def __init__(self, ws, bridge: SS2022DuplexBridge):
        self._ws = ws
        self._bridge = bridge
        self._close_task: asyncio.Task[None] | None = None

    def __getattr__(self, name: str):
        return getattr(self._ws, name)

    @property
    def bridge(self) -> SS2022DuplexBridge:
        return self._bridge

    def _abort_transport(self) -> BaseException | None:
        transport = getattr(self._ws, "transport", None)
        if transport is None:
            protocol = getattr(self._ws, "protocol", None)
            transport = getattr(protocol, "transport", None)
        abort = getattr(transport, "abort", None)
        if abort is None:
            return None
        try:
            abort()
        except Exception as exc:
            return exc
        return None

    async def _close_impl(self, *args, **kwargs) -> None:
        close_error: BaseException | None = None
        try:
            await self._ws.close(*args, **kwargs)
        except BaseException as exc:
            close_error = exc

        wait_closed = getattr(self._ws, "wait_closed", None)
        if wait_closed is not None:
            try:
                await wait_closed()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc

        if close_error is not None:
            # Preserve the websocket close/wait_closed cause; abort is only a
            # best-effort wake-up when the graceful close path already failed.
            self._abort_transport()

        await self._bridge.aclose(
            cause=close_error,
            direction=(
                "websocket_close_error" if close_error is not None
                else "websocket_close"
            ),
        )
        if close_error is not None:
            raise close_error

    async def close(self, *args, **kwargs):
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(*args, **kwargs),
                name="managed-websocket-close",
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            try:
                await self._close_task
            finally:
                raise


@dataclass
class ResponsesWsPreVisibleResult:
    pending: list[str | bytes] = field(default_factory=list)
    visible_frame: str | bytes | None = None
    outcome: str | None = None
    error_detail: str = ""
    error_code: Optional[str] = None
    http_status: Optional[int] = None
    first_packet_ms: Optional[int] = None
    stream_started: bool = False
    closed_after_accept: bool = False
    ok: bool = False


@dataclass
class ResponsesWsReadStep:
    data: str | bytes | None = None
    event_type: str = ""
    outcome: str | None = None
    error_detail: str = ""
    http_status: Optional[int] = None
    skip_downstream: bool = False
    response_completed: bool = False
    response_failed: bool = False
    close_code: int = 1000
    close_reason: str = ""


def socks5h_url(url: str) -> str:
    """websockets uses socks5 for local DNS and socks5h for remote DNS."""
    if url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


def http_url_to_ws(url: str) -> str:
    p = urlparse(url)
    scheme = p.scheme
    if scheme == "https":
        scheme = "wss"
    elif scheme == "http":
        scheme = "ws"
    return urlunparse((scheme, p.netloc, p.path or "", p.params, p.query, p.fragment))


def ws_route_kwargs(channel, resolved_model: str) -> dict:
    return {
        "channel_key": channel.key,
        "model": resolved_model,
        "purpose": "oauth_openai",
        "account_key": getattr(channel, "account_key", "") or "",
    }


def legacy_socks5_connector() -> SOCKS5Connector | None:
    try:
        from .. import network
        url = network.active_socks5_url()
    except Exception:
        url = None
    if not url:
        return None
    return SOCKS5Connector("legacy-socks5", url)


def resolve_ws_route_chain(channel, resolved_model: str) -> list[tuple[str, Any | None]]:
    """Resolve a WS route with the same explicit-direct policy as HTTP."""
    from ..proxy import manager as pm

    configured = False
    try:
        pm.init()
        configured = pm.is_configured()
        if configured:
            source_chain = pm.resolve_proxy_chain(**ws_route_kwargs(channel, resolved_model))
            route_chain: list[tuple[str, Any | None]] = []
            for name in source_chain:
                conn = pm.get_connector(name)
                if conn is None:
                    continue
                if getattr(conn, "type", "") == "direct":
                    route_chain.append(("direct", None))
                else:
                    route_chain.append((name, conn))
            if route_chain:
                if pm.direct_fallback_enabled() and not any(name == "direct" for name, _ in route_chain):
                    route_chain.append(("direct", None))
                return route_chain
            if pm.direct_fallback_enabled():
                print(f"[proxy] WS route has no usable target; using enabled direct fallback: {source_chain}")
                return [("direct", None)]
            return []
        if pm.has_non_direct_routing_rules():
            return []
    except Exception as exc:
        if pm.direct_fallback_enabled():
            print(f"[proxy] WS route resolution failed; using enabled direct fallback: {exc}")
            return [("direct", None)]
        if configured or pm.has_non_direct_routing_rules():
            print(f"[proxy] WS route resolution failed closed: {exc}")
            return []

    # No configured new-proxy route: preserve the normal legacy/direct path.
    legacy = legacy_socks5_connector()
    if legacy is not None:
        return [("legacy-socks5", legacy)]
    return [("direct", None)]


async def open_socket_via_ss2022(
    url: str,
    connector: SS2022Connector,
    proxy_bytes,
    *,
    timeout: float,
) -> SS2022DuplexBridge:
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise OSError("websocket URL missing host")
    port = p.port or (443 if p.scheme == "wss" else 80)

    from ..proxy.ss2022 import SS2022Connection

    conn = SS2022Connection(
        connector.cipher,
        connector.password,
        connector.server,
        connector.port,
    )
    await conn.connect(host, port, timeout=timeout)
    return await SS2022DuplexBridge.create(conn, byte_counter=proxy_bytes)


async def connect_upstream_ws(
    url: str,
    *,
    headers: dict[str, str],
    connector,
    proxy_bytes,
    open_timeout: float,
    timing: WsAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
    open_socket_func=None,
    connect_func=None,
):
    """Open one WS route; caller constructs ``timing`` immediately beforehand."""

    connect = connect_func or websockets.connect
    kwargs = dict(
        additional_headers=headers,
        user_agent_header=None,
        # Keep the library's transport timer later than the business connection
        # deadline so the unique business winner isn't relabelled by a tie.
        open_timeout=open_timeout,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=None,
        max_queue=64,
        compression="deflate",
    )

    async def _connect_once():
        if connector is None or isinstance(connector, DirectConnector):
            return await connect(url, proxy=None, **kwargs)
        if isinstance(connector, SOCKS5Connector):
            return await connect(url, proxy=socks5h_url(connector.url), **kwargs)
        if isinstance(connector, SS2022Connector):
            opener = open_socket_func or open_socket_via_ss2022
            bridge = await opener(
                url, connector, proxy_bytes, timeout=open_timeout,
            )
            sock = bridge.handoff_socket()
            try:
                ws = await connect(url, proxy=None, sock=sock, **kwargs)
            except BaseException as exc:
                # websockets received sock= and owns its selector transport even
                # when the handshake fails or is cancelled. The bridge closes
                # only its internal peer and raw SS connection.
                await bridge.aclose(
                    cause=exc,
                    direction="websocket_handshake_error",
                )
                raise
            return ManagedWsConnection(ws, bridge)
        return await connect(url, proxy=None, **kwargs)

    if timing is not None and round_timeouts is not None:
        ws = await timing.wait_for(_connect_once(), round_timeouts)
        timing.mark_handshake_complete()
        return ws
    return await _connect_once()


async def await_ws_owned(awaitable):
    """Finish one WS terminal/cleanup owner even if its caller is cancelled."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def wait_ws_round_io(
    awaitable,
    *,
    timing: WsAttemptTiming | None,
    round_timeouts: RoundTimeouts | None,
):
    """Await send/recv while the WS round's dynamic business deadlines compete."""

    if timing is not None and round_timeouts is not None:
        return await timing.wait_for(awaitable, round_timeouts)
    return await awaitable


async def _next_nonempty_ws_frame(
    upstream_ws,
    *,
    timing: WsAttemptTiming | None,
    round_timeouts: RoundTimeouts | None,
):
    """Receive the next non-empty frame; only it is response activity."""

    while True:
        data = await wait_ws_round_io(
            upstream_ws.recv(), timing=timing, round_timeouts=round_timeouts,
        )
        if isinstance(data, str):
            if not data.encode("utf-8"):
                continue
        elif not data:
            continue
        if timing is not None:
            timing.mark_ws_frame(data)
        return data


async def read_until_first_responses_ws_visible_event(
    upstream_ws,
    tracker,
    *,
    channel_key: str,
    deadline_ts: float,
    first_wait: float,
    idle_timeout: int,
    proxy_bytes: WsProxyBytes | None = None,
    start_time: float | None = None,
    start_monotonic: float | None = None,
    parse_wrapped_errors: bool = False,
    commit_retryable_errors: bool = False,
    timeout_detail_mode: str = "event",
    timeout_label_seconds: float | int | None = None,
    use_tracker_error_detail: bool = False,
    timing: WsAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
) -> ResponsesWsPreVisibleResult:
    """Read Responses WS frames until the first downstream-visible frame.

    The helper owns the pre-visible transport read loop and frame
    classification only.  Callers still decide failover, downstream close,
    logging, cooldown/scorer, affinity, and local result mapping.
    """
    result = ResponsesWsPreVisibleResult()
    pending_bytes = 0

    def append_pending(data: str | bytes) -> bool:
        """Bound metadata buffered before the irreversible output boundary."""

        nonlocal pending_bytes
        size = ws_frame_size(data)
        if len(result.pending) >= 1_024 or pending_bytes + size > 8 * 1024 * 1024:
            result.pending.clear()
            result.outcome = "transport_error"
            result.http_status = 502
            result.error_detail = "pre-visible websocket buffer limit exceeded"
            return False
        result.pending.append(data)
        pending_bytes += size
        return True

    while True:
        try:
            data = await _next_nonempty_ws_frame(
                upstream_ws, timing=timing, round_timeouts=round_timeouts,
            )
        except BusinessTimeoutError as exc:
            result.outcome = exc.outcome
            result.error_detail = exc.outcome
            return result
        except asyncio.TimeoutError:
            # Compatibility fallback for callers not yet supplying a round.
            result.outcome = "transport_timeout"
            result.error_detail = "websocket transport timeout before first frame"
            return result

        if result.first_packet_ms is None and timing is not None:
            result.first_packet_ms = timing.snapshot().first_byte_ms
        if proxy_bytes is not None:
            proxy_bytes.count(down=ws_frame_size(data))

        if isinstance(data, str):
            # Every upstream text frame must reach the tracker before a terminal
            # classification returns. Attempt settlement relies on the exact
            # response body for usage/service-tier/actual-cost extraction.
            event_type = ws_event_type(data)
            tracker.feed_text(data)

            if parse_wrapped_errors:
                maybe_error = parse_wrapped_responses_ws_error(data)
                if maybe_error:
                    result.outcome = "upstream_error_json"
                    result.http_status = maybe_error.get("status")
                    result.error_code = str(maybe_error.get("code") or "") or None
                    result.error_detail = maybe_error.get("message") or data[:2000]
                    if (
                        commit_retryable_errors
                        or not is_retryable_responses_ws_error_before_accept(maybe_error)
                    ):
                        if not append_pending(data):
                            return result
                        result.closed_after_accept = True
                    return result

            if getattr(tracker, "response_failed", False):
                if timing is not None:
                    timing.mark_io_complete()
                result.outcome = "stream_upstream_error" if event_type == "response.failed" else "upstream_error_json"
                result.error_code = getattr(tracker, "stream_error_code", None)
                if use_tracker_error_detail:
                    result.error_detail = getattr(tracker, "stream_error_message", None) or data[:2000]
                else:
                    status, detail = responses_ws_error_detail(data)
                    result.http_status = status
                    result.error_detail = detail
                if getattr(tracker, "stream_error_code", None) == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE:
                    result.outcome = "request_invalid"
                    result.http_status = 400
                    result.error_detail = (
                        getattr(tracker, "stream_error_message", None)
                        or protocol_errors.responses_max_output_context_error_message()
                    )
                if event_type == "response.failed":
                    if not append_pending(data):
                        return result
                    result.stream_started = True
                    result.closed_after_accept = True
                return result

            if is_responses_ws_visible_event_type(event_type):
                bl_hit = blacklist.match(data, channel_key)
                if bl_hit:
                    result.outcome = "blacklist_hit"
                    result.error_detail = f"blacklist: {bl_hit}"
                    return result
                if not append_pending(data):
                    return result
                result.visible_frame = data
                return result

            if not append_pending(data):
                return result
            if getattr(tracker, "response_completed", False):
                if timing is not None:
                    timing.mark_io_complete()
                result.ok = True
                result.outcome = "success"
                result.closed_after_accept = True
                return result
        else:
            # Binary frames are real downstream payload. They cannot be inspected
            # by the text blacklist, but they define the irreversible boundary.
            if not append_pending(data):
                return result
            result.visible_frame = data
            return result


async def read_next_responses_ws_step(
    upstream_ws,
    tracker,
    *,
    channel_key: str,
    deadline_ts: float,
    idle_timeout: int,
    proxy_bytes: WsProxyBytes | None = None,
    frame_transform: Callable[[str | bytes], str | bytes] | None = None,
    skip_event_types: tuple[str, ...] = ("codex.rate_limits",),
    closed_error_detail: str | None = None,
    blacklist_before_error: bool = False,
    check_blacklist: bool = True,
    timing: WsAttemptTiming | None = None,
    round_timeouts: RoundTimeouts | None = None,
) -> ResponsesWsReadStep:
    """Read and classify one post-accept Responses WS frame."""
    try:
        data = await _next_nonempty_ws_frame(
            upstream_ws, timing=timing, round_timeouts=round_timeouts,
        )
    except BusinessTimeoutError as exc:
        return ResponsesWsReadStep(
            outcome=exc.outcome,
            error_detail=exc.outcome,
            http_status=504,
        )
    except asyncio.TimeoutError:
        return ResponsesWsReadStep(
            outcome="transport_timeout",
            error_detail="websocket transport timeout",
            http_status=504,
        )
    except websockets.ConnectionClosed as exc:
        if timing is not None:
            timing.mark_io_complete()
        close_code = int(exc.rcvd.code if exc.rcvd else 1000)
        close_reason = str(exc.rcvd.reason if exc.rcvd else "")
        if getattr(tracker, "response_completed", False):
            return ResponsesWsReadStep(
                outcome="success",
                response_completed=True,
                close_code=close_code,
                close_reason=close_reason,
            )
        if getattr(tracker, "response_failed", False):
            is_context_error = (
                getattr(tracker, "stream_error_code", None)
                == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
            )
            error_detail = getattr(tracker, "stream_error_message", None)
            if not error_detail:
                error_detail = (
                    protocol_errors.responses_max_output_context_error_message()
                    if is_context_error else "upstream stream error"
                )
            return ResponsesWsReadStep(
                outcome="request_invalid" if is_context_error else "stream_upstream_error",
                error_detail=error_detail,
                http_status=400 if is_context_error else 503,
                response_failed=True,
                close_code=close_code,
                close_reason=close_reason,
            )
        detail = closed_error_detail if closed_error_detail is not None else f"upstream websocket closed: {exc}"
        return ResponsesWsReadStep(
            outcome="upstream_closed",
            error_detail=detail,
            http_status=502,
            close_code=close_code,
            close_reason=close_reason,
        )

    if proxy_bytes is not None:
        proxy_bytes.count(down=ws_frame_size(data))
    if frame_transform is not None:
        data = frame_transform(data)

    step = ResponsesWsReadStep(data=data)
    if isinstance(data, str):
        step.event_type = ws_event_type(data)
        tracker.feed_text(data)
        if step.event_type in skip_event_types:
            step.skip_downstream = True
            return step
        if check_blacklist and blacklist_before_error:
            bl_hit = blacklist.match(data, channel_key)
            if bl_hit:
                step.outcome = "blacklist_hit"
                step.error_detail = f"blacklist: {bl_hit}"
                step.http_status = 503
                return step
        if getattr(tracker, "response_failed", False):
            if timing is not None:
                timing.mark_io_complete()
            if getattr(tracker, "stream_error_code", None) == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE:
                step.outcome = "request_invalid"
                step.error_detail = (
                    getattr(tracker, "stream_error_message", None)
                    or protocol_errors.responses_max_output_context_error_message()
                )
                step.http_status = 400
                step.skip_downstream = True
            else:
                step.outcome = "stream_upstream_error"
                step.error_detail = getattr(tracker, "stream_error_message", None) or "upstream stream error"
                step.http_status = 503
            step.response_failed = True
            return step
        if check_blacklist and not blacklist_before_error:
            bl_hit = blacklist.match(data, channel_key)
            if bl_hit:
                step.outcome = "blacklist_hit"
                step.error_detail = f"blacklist: {bl_hit}"
                step.http_status = 503
                return step
        if getattr(tracker, "response_completed", False):
            if timing is not None:
                timing.mark_io_complete()
            step.outcome = "success"
            step.response_completed = True
            return step
    return step
