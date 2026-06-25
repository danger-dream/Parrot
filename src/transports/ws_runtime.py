"""WebSocket transport runtime helpers.

Phase 9 keeps request orchestration, DB writes, scoring, and downstream close
semantics in callers while moving reusable WS transport mechanics here.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
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
from ..proxy.connector import DirectConnector, SOCKS5Connector, SS2022Connector
from .websocket import event_type as ws_event_type, frame_size as ws_frame_size


@dataclass
class WsProxyBytes:
    up: int = 0
    down: int = 0

    def count(self, up: int = 0, down: int = 0) -> None:
        self.up += int(up or 0)
        self.down += int(down or 0)


class ManagedWsConnection:
    """WebSocket connection with an attached async cleanup hook."""

    def __init__(self, ws, cleanup):
        self._ws = ws
        self._cleanup = cleanup
        self._cleanup_done = False

    def __getattr__(self, name: str):
        return getattr(self._ws, name)

    async def close(self, *args, **kwargs):
        try:
            return await self._ws.close(*args, **kwargs)
        finally:
            if not self._cleanup_done:
                self._cleanup_done = True
                await self._cleanup()


@dataclass
class ResponsesWsPreVisibleResult:
    pending: list[str | bytes] = field(default_factory=list)
    visible_frame: str | bytes | None = None
    outcome: str | None = None
    error_detail: str = ""
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
    try:
        from ..proxy import manager as pm
        pm.init()
        if pm.is_configured():
            route_chain: list[tuple[str, Any | None]] = []
            valid_seen = False
            for name in pm.resolve_proxy_chain(**ws_route_kwargs(channel, resolved_model)):
                conn = pm.get_connector(name)
                if conn is None:
                    continue
                valid_seen = True
                if getattr(conn, "type", "") == "direct":
                    route_chain.append(("direct", None))
                else:
                    route_chain.append((name, conn))
            if valid_seen:
                return route_chain
            return []
    except Exception:
        pass

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
) -> tuple[socket.socket, Callable[[], Awaitable[None]]]:
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise OSError("websocket URL missing host")
    port = p.port or (443 if p.scheme == "wss" else 80)

    from ..proxy.ss2022 import SS2022Connection

    conn = SS2022Connection(connector.cipher, connector.password, connector.server, connector.port)
    await conn.connect(host, port, timeout=timeout)

    loop = asyncio.get_running_loop()
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    cleanup_lock = asyncio.Lock()
    cleanup_done = False

    def shutdown_sock(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    async def pump_sock_to_ss() -> None:
        try:
            while not stop.is_set():
                data = await loop.sock_recv(right, 65536)
                if not data:
                    return
                proxy_bytes.count(up=len(data))
                await conn.write(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            stop.set()

    async def pump_ss_to_sock() -> None:
        try:
            while not stop.is_set():
                data = await conn.read(65536)
                if not data:
                    return
                proxy_bytes.count(down=len(data))
                await loop.sock_sendall(right, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            stop.set()
            shutdown_sock(right)

    async def cleanup() -> None:
        nonlocal cleanup_done
        async with cleanup_lock:
            if cleanup_done:
                return
            cleanup_done = True
            stop.set()
            for task in tasks:
                task.cancel()
            shutdown_sock(right)
            shutdown_sock(left)
            try:
                await conn.close()
            except Exception:
                pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    tasks.append(asyncio.create_task(pump_sock_to_ss()))
    tasks.append(asyncio.create_task(pump_ss_to_sock()))
    return left, cleanup


async def connect_upstream_ws(
    url: str,
    *,
    headers: dict[str, str],
    connector,
    proxy_bytes,
    open_timeout: float,
    open_socket_func=None,
    connect_func=None,
):
    """Open a WS connection through direct/SOCKS5/SS2022 transport."""
    connect = connect_func or websockets.connect
    kwargs = dict(
        additional_headers=headers,
        user_agent_header=None,
        open_timeout=open_timeout,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=None,
        max_queue=64,
        compression="deflate",
    )
    if connector is None or isinstance(connector, DirectConnector):
        return await connect(url, proxy=None, **kwargs)
    if isinstance(connector, SOCKS5Connector):
        return await connect(url, proxy=socks5h_url(connector.url), **kwargs)
    if isinstance(connector, SS2022Connector):
        opener = open_socket_func or open_socket_via_ss2022
        opened = await opener(url, connector, proxy_bytes, timeout=open_timeout)
        cleanup = None
        if isinstance(opened, tuple) and len(opened) == 2:
            sock, cleanup = opened
        else:
            sock = opened
        try:
            ws = await connect(url, proxy=None, sock=sock, **kwargs)
        except BaseException:
            if cleanup is not None:
                await cleanup()
            raise
        if cleanup is not None:
            return ManagedWsConnection(ws, cleanup)
        return ws
    return await connect(url, proxy=None, **kwargs)


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
    parse_wrapped_errors: bool = False,
    timeout_detail_mode: str = "event",
    timeout_label_seconds: float | int | None = None,
    use_tracker_error_detail: bool = False,
) -> ResponsesWsPreVisibleResult:
    """Read Responses WS frames until the first downstream-visible frame.

    The helper owns the pre-visible transport read loop and frame
    classification only.  Callers still decide failover, downstream close,
    logging, cooldown/scorer, affinity, and local result mapping.
    """
    result = ResponsesWsPreVisibleResult()
    wait_sec = first_wait
    timeout_label = timeout_label_seconds if timeout_label_seconds is not None else first_wait

    while True:
        try:
            data = await asyncio.wait_for(upstream_ws.recv(), timeout=wait_sec)
        except asyncio.TimeoutError:
            result.outcome = "first_byte_timeout"
            if timeout_detail_mode == "packet_or_visible":
                result.error_detail = (
                    f"first websocket packet timeout > {timeout_label}s"
                    if result.first_packet_ms is None else
                    f"first websocket visible event timeout > {timeout_label}s"
                )
            else:
                result.error_detail = f"first websocket event timeout > {timeout_label}s"
            return result

        if result.first_packet_ms is None and start_time is not None:
            result.first_packet_ms = int((time.time() - start_time) * 1000)
        if proxy_bytes is not None:
            proxy_bytes.count(down=ws_frame_size(data))

        if isinstance(data, str):
            if parse_wrapped_errors:
                maybe_error = parse_wrapped_responses_ws_error(data)
                if maybe_error:
                    result.outcome = "upstream_error_json"
                    result.http_status = maybe_error.get("status")
                    result.error_detail = maybe_error.get("message") or data[:2000]
                    if not is_retryable_responses_ws_error_before_accept(maybe_error):
                        result.pending.append(data)
                        result.closed_after_accept = True
                    return result

            event_type = ws_event_type(data)
            tracker.feed_text(data)

            if getattr(tracker, "response_failed", False):
                result.outcome = "stream_upstream_error" if event_type == "response.failed" else "upstream_error_json"
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
                    result.pending.append(data)
                    result.stream_started = True
                    result.closed_after_accept = True
                return result

            if is_responses_ws_visible_event_type(event_type):
                bl_hit = blacklist.match(data, channel_key)
                if bl_hit:
                    result.outcome = "blacklist_hit"
                    result.error_detail = f"blacklist: {bl_hit}"
                    return result
                result.pending.append(data)
                result.visible_frame = data
                return result

            result.pending.append(data)
            if getattr(tracker, "response_completed", False):
                result.ok = True
                result.outcome = "success"
                result.closed_after_accept = True
                return result
        else:
            # Binary frames are real downstream payload. They cannot be inspected
            # by the text blacklist, but they define the irreversible boundary.
            result.pending.append(data)
            result.visible_frame = data
            return result

        remaining = deadline_ts - time.time()
        if remaining <= 0:
            result.outcome = "total_timeout"
            result.error_detail = "upstream total timeout before first visible websocket event"
            return result
        wait_sec = min(float(idle_timeout), max(1.0, remaining))


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
) -> ResponsesWsReadStep:
    """Read and classify one post-accept Responses WS frame."""
    remaining = deadline_ts - time.time()
    if remaining <= 0:
        return ResponsesWsReadStep(
            outcome="total_timeout",
            error_detail="upstream total timeout",
            http_status=504,
        )

    wait_sec = min(float(idle_timeout), max(1.0, remaining))
    try:
        data = await asyncio.wait_for(upstream_ws.recv(), timeout=wait_sec)
    except asyncio.TimeoutError:
        return ResponsesWsReadStep(
            outcome="idle_timeout",
            error_detail=f"upstream idle timeout > {idle_timeout}s",
            http_status=504,
        )
    except websockets.ConnectionClosed as exc:
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
            step.outcome = "success"
            step.response_completed = True
            return step
    return step
