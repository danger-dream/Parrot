"""Authoritative timing state for one real upstream HTTP/WS request round.

A round starts at the monotonic point immediately before the transport API call
(``client.stream/send`` or ``websockets.connect``).  Queueing, candidate
selection, proxy-route selection and retry backoff happen before construction of
this object and are deliberately excluded.

Business fields are transport-mode specific:

* ``http_stream``: connection ends at the reliable request-body-send completion
  trace event.  First-byte then covers the complete wait for response headers
  plus the first non-empty response-body bytes chunk; idle starts at the same
  boundary.  Every later non-empty response-body bytes chunk is activity.
* ``http_non_stream``: connection also ends at request-body-send completion.
  First-byte is not applicable.  Idle starts only after the first non-empty
  response-body bytes chunk.
* ``ws``: connection ends when the WebSocket handshake API returns; first-byte
  and idle both start there.  Every non-empty bytes/text frame is activity.

All durations and deadlines use one injected monotonic clock.  Wall time is kept
only for persisted ``started_at``/``ended_at`` timestamps.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx


Clock = Callable[[], float]
WallClock = Callable[[], float]
RoundMode = Literal["http_stream", "http_non_stream", "ws"]


BUSINESS_TIMEOUT_OUTCOMES = frozenset({
    "connection_timeout",
    "first_byte_timeout",
    "idle_timeout",
    "total_timeout",
})
TRANSPORT_TIMEOUT_OUTCOMES = frozenset({
    "http_connect_timeout",
    "pool_timeout",
    "write_timeout",
    "read_timeout",
    "transport_timeout",
})


def _duration_ms(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


def classify_httpx_timeout(exc: BaseException) -> str:
    """Return an unambiguous HTTPX transport-timeout outcome.

    These are intentionally different from Parrot's business
    ``connection_timeout``.  For example, a pool slot timeout and an upload
    write timeout must never be reported as the configured business connection
    deadline.
    """

    if isinstance(exc, httpx.ConnectTimeout):
        return "http_connect_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "transport_timeout"
    raise TypeError(f"not an HTTPX timeout: {type(exc)!r}")


@dataclass(frozen=True)
class RoundTimeouts:
    """Existing config values interpreted relative to one upstream round."""

    connection: float
    first_byte: float
    idle: float
    total: float

    @classmethod
    def from_config(cls, values: dict[str, Any] | None) -> "RoundTimeouts":
        cfg = values or {}
        return cls(
            connection=max(0.001, float(cfg.get("connect", 10))),
            first_byte=max(0.001, float(cfg.get("firstByte", 30))),
            idle=max(0.001, float(cfg.get("idle", 120))),
            total=max(0.001, float(cfg.get("total", 600))),
        )


@dataclass(frozen=True)
class TimeoutDeadline:
    """The unique next business deadline for a round state."""

    outcome: str
    deadline: float
    remaining: float


class BusinessTimeoutError(asyncio.TimeoutError):
    """Raised when the unique next business deadline actually expires."""

    def __init__(self, deadline: TimeoutDeadline):
        self.outcome = deadline.outcome
        self.deadline = deadline.deadline
        super().__init__(deadline.outcome)


@dataclass(frozen=True)
class RoundTimingSnapshot:
    """Serializable state for one route round.

    ``first_byte_ms`` is relative to business connection completion, not to the
    downstream request and not to route selection.  ``idle_ms`` is the active
    idle timer value at the terminal/snapshot boundary; it is NULL before idle
    is applicable (notably non-stream HTTP before its first response byte).
    """

    round_id: str
    transport: str
    request_mode: RoundMode
    route_type: str
    started_at: float
    ended_at: float | None = None
    connection_ms: int | None = None
    first_byte_ms: int | None = None
    idle_ms: int | None = None
    total_ms: int | None = None
    dns_ms: int | None = None
    tcp_ms: int | None = None
    proxy_tcp_ms: int | None = None
    proxy_tunnel_ms: int | None = None
    tls_ms: int | None = None
    target_tls_ms: int | None = None
    ws_handshake_ms: int | None = None
    request_upload_ms: int | None = None
    response_headers_wait_ms: int | None = None
    response_body_first_byte_wait_ms: int | None = None
    outcome: str | None = None
    error_detail: str | None = None
    terminal: bool = False

    @property
    def connect_ms(self) -> int | None:
        """Compatibility alias: this is the business connection metric."""

        return self.connection_ms


class UpstreamRoundTiming:
    """Monotonic state machine for one real upstream API round."""

    # Same-deadline ties are deterministic and still produce one error.  A tie
    # between first-byte and idle is resolved to first-byte because it is the
    # more specific unmet milestone; total remains the final hard cap.
    _DEADLINE_PRIORITY = {
        "connection_timeout": 0,
        "first_byte_timeout": 1,
        "idle_timeout": 2,
        "total_timeout": 3,
    }

    def __init__(
        self,
        *,
        request_mode: RoundMode,
        route_type: str = "direct",
        transport: str | None = None,
        round_id: str | None = None,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = time.time,
    ) -> None:
        if request_mode not in ("http_stream", "http_non_stream", "ws"):
            raise ValueError(f"unsupported upstream round mode: {request_mode!r}")
        self.request_mode: RoundMode = request_mode
        self.route_type = str(route_type or "direct")
        self.transport = str(transport or ("ws" if request_mode == "ws" else "http"))
        self.round_id = str(round_id or uuid.uuid4())
        self._clock = clock
        self._wall_clock = wall_clock

        # The caller must instantiate immediately before the actual transport API
        # call.  No wall-clock value participates in duration/deadline arithmetic.
        self.started_monotonic = clock()
        self.started_at = wall_clock()
        self._finished_monotonic: float | None = None
        self._io_completed_monotonic: float | None = None
        self._io_completed_at: float | None = None
        self._ended_at: float | None = None
        self._outcome: str | None = None
        self._error_detail: str | None = None

        self._connection_completed_at: float | None = None
        self._first_response_at: float | None = None
        self._last_activity_at: float | None = None
        self._connection_event = asyncio.Event()
        self._state_changed = asyncio.Event()

        self._phase_started: dict[str, float] = {}
        self._dns_ms: int | None = None
        self._tcp_ms: int | None = None
        self._proxy_tcp_ms: int | None = None
        self._proxy_tunnel_ms: int | None = None
        self._tls_ms: int | None = None
        self._ws_handshake_ms: int | None = None
        self._request_upload_ms: int | None = None
        self._response_headers_wait_ms: int | None = None
        self._body_wait_started_at: float | None = None
        self._body_first_byte_wait_ms: int | None = None

    @property
    def connection_complete(self) -> bool:
        return self._connection_completed_at is not None

    @property
    def first_byte_seen(self) -> bool:
        return self._first_response_at is not None

    @property
    def terminal(self) -> bool:
        return self._finished_monotonic is not None

    async def wait_connection_complete(self) -> None:
        await self._connection_event.wait()

    def mark_connection_complete(self, at: float | None = None) -> None:
        if self._connection_completed_at is not None:
            return
        now = self._clock() if at is None else float(at)
        self._connection_completed_at = now
        if self.request_mode == "ws":
            self._ws_handshake_ms = _duration_ms(self.started_monotonic, now)
        self._connection_event.set()
        self._state_changed.set()

    def start_response_body_wait(self) -> None:
        """Mark the direct wait for a first response-body byte (display stage)."""

        if self._body_wait_started_at is None and self._first_response_at is None:
            self._body_wait_started_at = self._clock()

    def mark_response_body_byte(self, chunk: bytes | bytearray | memoryview | None = None) -> bool:
        """Record activity for one non-empty raw HTTP response body chunk."""

        if chunk is not None and len(chunk) == 0:
            return False
        return self._mark_response_activity()

    def mark_ws_frame(self, frame: str | bytes | bytearray | memoryview) -> bool:
        """Record activity for one non-empty upstream WebSocket frame."""

        if isinstance(frame, str):
            if len(frame.encode("utf-8")) == 0:
                return False
        elif len(frame) == 0:
            return False
        return self._mark_response_activity()

    def _mark_response_activity(self) -> bool:
        if self._connection_completed_at is None:
            raise RuntimeError("response activity observed before business connection completed")
        now = self._clock()
        first = self._first_response_at is None
        if first:
            self._first_response_at = now
            self._body_first_byte_wait_ms = _duration_ms(self._body_wait_started_at, now)
        self._last_activity_at = now
        self._state_changed.set()
        return first

    def next_deadline(
        self,
        timeouts: RoundTimeouts,
        *,
        now: float | None = None,
    ) -> TimeoutDeadline:
        """Return the actual next applicable business deadline.

        Stream/WS first-byte and idle deadlines start together at connection
        completion.  Non-stream HTTP never creates a first-byte deadline, and its
        idle deadline doesn't exist until the first non-empty body chunk.
        """

        current = self._clock() if now is None else float(now)
        candidates: list[tuple[float, int, str]] = [
            (
                self.started_monotonic + timeouts.total,
                self._DEADLINE_PRIORITY["total_timeout"],
                "total_timeout",
            )
        ]
        if self._connection_completed_at is None:
            candidates.append((
                self.started_monotonic + timeouts.connection,
                self._DEADLINE_PRIORITY["connection_timeout"],
                "connection_timeout",
            ))
        else:
            if self.request_mode in ("http_stream", "ws"):
                if self._first_response_at is None:
                    candidates.append((
                        self._connection_completed_at + timeouts.first_byte,
                        self._DEADLINE_PRIORITY["first_byte_timeout"],
                        "first_byte_timeout",
                    ))
                idle_base = (
                    self._last_activity_at
                    if self._last_activity_at is not None
                    else self._connection_completed_at
                )
                candidates.append((
                    idle_base + timeouts.idle,
                    self._DEADLINE_PRIORITY["idle_timeout"],
                    "idle_timeout",
                ))
            elif self._last_activity_at is not None:
                candidates.append((
                    self._last_activity_at + timeouts.idle,
                    self._DEADLINE_PRIORITY["idle_timeout"],
                    "idle_timeout",
                ))

        deadline, _priority, outcome = min(candidates, key=lambda item: (item[0], item[1]))
        return TimeoutDeadline(
            outcome=outcome,
            deadline=deadline,
            remaining=max(0.0, deadline - current),
        )

    async def wait_for(self, awaitable, timeouts: RoundTimeouts):
        """Await I/O while reacting to monotonic round-state deadline changes.

        This matters for HTTP: ``ctx.__aenter__`` is still waiting for response
        headers after the request body has been sent, but the business connection
        deadline must stop at that send-complete trace event.  The same task then
        continues without being cancelled and restarted: stream requests switch
        to first-byte/idle/total semantics, while non-stream requests switch to
        total-only semantics until body activity begins.
        """

        task = asyncio.ensure_future(awaitable)
        state_wait: asyncio.Task | None = None
        try:
            while True:
                if task.done():
                    return await task
                next_deadline = self.next_deadline(timeouts)
                if next_deadline.remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise BusinessTimeoutError(next_deadline)

                self._state_changed.clear()
                state_wait = asyncio.create_task(self._state_changed.wait())
                done, _pending = await asyncio.wait(
                    {task, state_wait},
                    timeout=next_deadline.remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    state_wait.cancel()
                    await asyncio.gather(state_wait, return_exceptions=True)
                    state_wait = None
                    return await task
                if state_wait in done:
                    state_wait = None
                    continue

                state_wait.cancel()
                await asyncio.gather(state_wait, return_exceptions=True)
                state_wait = None
                expired = self.next_deadline(timeouts)
                if expired.remaining <= 0:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise BusinessTimeoutError(expired)
                # A state transition won the race at the exact old boundary;
                # recompute and keep waiting instead of assigning the stale error.
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            if state_wait is not None:
                state_wait.cancel()
                await asyncio.gather(state_wait, return_exceptions=True)

    def mark_io_complete(self, at: float | None = None) -> None:
        """Freeze the authoritative upstream-I/O end before local post-processing."""

        if self._io_completed_monotonic is None:
            self._io_completed_monotonic = self._clock() if at is None else float(at)
            self._io_completed_at = self._wall_clock()
            self._state_changed.set()

    def finish(
        self,
        outcome: str,
        error_detail: str | None = None,
        *,
        at: float | None = None,
        ended_at: float | None = None,
    ) -> RoundTimingSnapshot:
        """Create the single terminal snapshot; subsequent calls are idempotent."""

        if self._finished_monotonic is None:
            if at is None:
                self._finished_monotonic = (
                    self._io_completed_monotonic
                    if self._io_completed_monotonic is not None
                    else self._clock()
                )
            else:
                self._finished_monotonic = float(at)
            if ended_at is not None:
                self._ended_at = float(ended_at)
            elif self._io_completed_at is not None:
                self._ended_at = self._io_completed_at
            else:
                self._ended_at = self._wall_clock()
            self._outcome = str(outcome)
            self._error_detail = error_detail
        return self.snapshot()

    def snapshot(self, *, terminal: bool = False) -> RoundTimingSnapshot:
        # ``terminal=True`` remains a read-only compatibility mode for older
        # callers while they are migrated: it computes durations at now but never
        # creates a second terminal outcome.
        now = self._finished_monotonic
        if now is None and terminal:
            now = (
                self._io_completed_monotonic
                if self._io_completed_monotonic is not None
                else self._clock()
            )

        upload_ms = self._request_upload_ms
        header_ms = self._response_headers_wait_ms
        body_wait_ms = self._body_first_byte_wait_ms
        if now is not None:
            if upload_ms is None:
                for event in ("http11.send_request_body", "http2.send_request_body"):
                    if event in self._phase_started:
                        upload_ms = _duration_ms(self._phase_started[event], now)
                        break
            if header_ms is None:
                for event in ("http11.receive_response_headers", "http2.receive_response_headers"):
                    if event in self._phase_started:
                        header_ms = _duration_ms(self._phase_started[event], now)
                        break
            if body_wait_ms is None and self._body_wait_started_at is not None:
                body_wait_ms = _duration_ms(self._body_wait_started_at, now)

        idle_base: float | None = None
        if self._connection_completed_at is not None:
            if self.request_mode in ("http_stream", "ws"):
                idle_base = (
                    self._last_activity_at
                    if self._last_activity_at is not None
                    else self._connection_completed_at
                )
            elif self._last_activity_at is not None:
                idle_base = self._last_activity_at
        idle_end = now if now is not None else self._clock()
        ss2022_ambiguous = self.route_type == "ss2022"
        target_tls_ms = None if ss2022_ambiguous else self._tls_ms
        proxy_tunnel_ms = None if ss2022_ambiguous else self._proxy_tunnel_ms

        return RoundTimingSnapshot(
            round_id=self.round_id,
            transport=self.transport,
            request_mode=self.request_mode,
            route_type=self.route_type,
            started_at=self.started_at,
            ended_at=self._ended_at,
            connection_ms=_duration_ms(self.started_monotonic, self._connection_completed_at),
            first_byte_ms=(
                None if self.request_mode == "http_non_stream"
                else _duration_ms(self._connection_completed_at, self._first_response_at)
            ),
            idle_ms=_duration_ms(idle_base, idle_end),
            total_ms=_duration_ms(self.started_monotonic, now),
            dns_ms=self._dns_ms,
            tcp_ms=self._tcp_ms,
            proxy_tcp_ms=self._proxy_tcp_ms,
            proxy_tunnel_ms=proxy_tunnel_ms,
            tls_ms=target_tls_ms,
            target_tls_ms=target_tls_ms,
            ws_handshake_ms=self._ws_handshake_ms,
            request_upload_ms=upload_ms,
            response_headers_wait_ms=header_ms,
            response_body_first_byte_wait_ms=body_wait_ms,
            outcome=self._outcome,
            error_detail=self._error_detail,
            terminal=self._finished_monotonic is not None,
        )

    def apply_to(self, result: Any, *, terminal: bool = True) -> Any:
        snap = self.snapshot(terminal=terminal)
        result.round_id = snap.round_id
        result.connect_ms = snap.connection_ms
        result.first_byte_ms = snap.first_byte_ms
        result.idle_ms = snap.idle_ms
        result.total_ms = snap.total_ms
        result.dns_ms = snap.dns_ms
        result.tcp_ms = snap.tcp_ms
        result.proxy_tcp_ms = snap.proxy_tcp_ms
        result.proxy_tunnel_ms = snap.proxy_tunnel_ms
        result.tls_ms = snap.tls_ms
        result.target_tls_ms = snap.target_tls_ms
        result.ws_handshake_ms = snap.ws_handshake_ms
        result.request_upload_ms = snap.request_upload_ms
        result.response_headers_wait_ms = snap.response_headers_wait_ms
        result.response_body_first_byte_wait_ms = snap.response_body_first_byte_wait_ms
        return result


class HttpAttemptTiming(UpstreamRoundTiming):
    """HTTPcore trace adapter plus the authoritative HTTP business state."""

    _UPLOAD_EVENTS = {
        "http11.send_request_body",
        "http2.send_request_body",
    }
    _RESPONSE_HEADERS_EVENTS = {
        "http11.receive_response_headers",
        "http2.receive_response_headers",
    }

    def __init__(
        self,
        *,
        route_type: str = "direct",
        response_mode: Literal["stream", "non_stream"] = "stream",
        round_id: str | None = None,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = time.time,
    ) -> None:
        request_mode: RoundMode = (
            "http_stream" if response_mode == "stream" else "http_non_stream"
        )
        super().__init__(
            request_mode=request_mode,
            route_type=route_type,
            transport="http",
            round_id=round_id,
            clock=clock,
            wall_clock=wall_clock,
        )

    @staticmethod
    def _split_event(name: str) -> tuple[str, str] | None:
        for suffix in (".started", ".complete", ".failed"):
            if name.endswith(suffix):
                return name[: -len(suffix)], suffix[1:]
        return None

    async def trace(self, name: str, info: dict[str, Any]) -> None:
        """HTTPcore async trace callback; only direct event intervals are used."""

        del info
        parsed = self._split_event(str(name))
        if parsed is None:
            return
        event, state = parsed
        now = self._clock()
        if state == "started":
            self._phase_started[event] = now
            return

        started = self._phase_started.pop(event, None)
        elapsed = _duration_ms(started, now)
        if elapsed is None:
            return

        if event == "socks.connect_tcp" and self._proxy_tcp_ms is None:
            self._proxy_tcp_ms = elapsed
        elif event == "socks.setup_socks5_connection" and self._proxy_tunnel_ms is None:
            self._proxy_tunnel_ms = elapsed
        elif event == "connection.connect_tcp" and self.route_type == "direct" and self._tcp_ms is None:
            # HTTPcore exposes this combined connect_tcp phase but no reliable
            # separate DNS interval.  dns_ms therefore stays NULL.
            self._tcp_ms = elapsed
        elif event.endswith(".start_tls") and self._tls_ms is None:
            # SS2022's lazy tunnel response occurs inside this trace interval;
            # snapshot() intentionally forces both tunnel/TLS detail to NULL.
            if self.route_type != "ss2022":
                self._tls_ms = elapsed
        elif event in self._UPLOAD_EVENTS and self._request_upload_ms is None:
            self._request_upload_ms = elapsed
        elif event in self._RESPONSE_HEADERS_EVENTS and self._response_headers_wait_ms is None:
            self._response_headers_wait_ms = elapsed

        if state == "complete" and event in self._UPLOAD_EVENTS:
            # ``connect`` is a transport/upload deadline, not a server-think-time
            # deadline.  In particular, stream requests may legitimately wait
            # longer than ``timeouts.connect`` for response headers; that wait is
            # governed by ``firstByte``.  HTTPcore emits this milestone for both
            # fresh and reused connections, unlike TCP/TLS trace events.
            self.mark_connection_complete(now)

    def record_proxy_tcp(self, started_at: float, ended_at: float) -> None:
        """Record SS2022's directly observed proxy-server TCP dial."""

        if self._proxy_tcp_ms is None:
            self._proxy_tcp_ms = _duration_ms(started_at, ended_at)


class WsAttemptTiming(UpstreamRoundTiming):
    """Business timing state for one ``websockets.connect`` round."""

    def __init__(
        self,
        *,
        route_type: str = "direct",
        round_id: str | None = None,
        clock: Clock = time.monotonic,
        wall_clock: WallClock = time.time,
    ) -> None:
        super().__init__(
            request_mode="ws",
            route_type=route_type,
            transport="ws",
            round_id=round_id,
            clock=clock,
            wall_clock=wall_clock,
        )

    def mark_handshake_complete(self, at: float | None = None) -> None:
        self.mark_connection_complete(at)
