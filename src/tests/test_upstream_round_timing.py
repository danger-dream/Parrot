"""Deterministic unit contract for the authoritative per-upstream-round clock.

Execution is intentionally deferred to the import-before-src isolation bootstrap
added later in this task.  These tests never use network or a real database.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.transports.ws_runtime import await_ws_owned
from src.transports.timing import (
    BusinessTimeoutError,
    HttpAttemptTiming,
    RoundTimeouts,
    WsAttemptTiming,
    classify_httpx_timeout,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class FakeWallClock(FakeClock):
    pass


def _timeouts(*, connection=1.0, first=2.0, idle=3.0, total=10.0) -> RoundTimeouts:
    return RoundTimeouts(
        connection=float(connection),
        first_byte=float(first),
        idle=float(idle),
        total=float(total),
    )


def test_round_identity_and_wall_clock_are_not_duration_inputs():
    mono = FakeClock(10.0)
    wall = FakeWallClock(1_000_000.0)
    first = HttpAttemptTiming(clock=mono, wall_clock=wall, response_mode="stream")
    wall.advance(86_400.0)
    mono.advance(0.25)
    first.mark_connection_complete()
    second = HttpAttemptTiming(clock=mono, wall_clock=wall, response_mode="stream")

    assert first.round_id != second.round_id
    assert first.snapshot().connection_ms == 250
    assert first.snapshot().started_at == 1_000_000.0
    assert second.snapshot().started_at == 1_086_400.0


@pytest.mark.asyncio
async def test_http_stream_final_headers_end_connection_and_raw_bytes_drive_first_idle():
    clock = FakeClock()
    timing = HttpAttemptTiming(clock=clock, wall_clock=clock, response_mode="stream")

    await timing.trace("http11.receive_response_headers.started", {})
    clock.advance(0.30)
    await timing.trace("http11.receive_response_headers.complete", {})

    assert timing.snapshot().connection_ms == 300
    assert timing.next_deadline(_timeouts(first=0.5, idle=0.2)).outcome == "idle_timeout"

    timing.start_response_body_wait()
    clock.advance(0.10)
    assert timing.mark_response_body_byte(b"") is False
    assert timing.snapshot().first_byte_ms is None
    assert timing.mark_response_body_byte(b": heartbeat\n\n") is True
    assert timing.snapshot().first_byte_ms == 100
    assert timing.next_deadline(_timeouts(first=0.01, idle=0.4)).outcome == "idle_timeout"

    clock.advance(0.25)
    timing.mark_response_body_byte(b" ")
    clock.advance(0.05)
    terminal = timing.finish("success")
    assert terminal.connection_ms == 300
    assert terminal.first_byte_ms == 100
    assert terminal.idle_ms == 50
    assert terminal.total_ms == 700
    assert terminal.response_headers_wait_ms == 300
    assert terminal.response_body_first_byte_wait_ms == 100


def test_http_stream_first_and_idle_are_parallel_and_only_earliest_wins():
    clock = FakeClock()
    timing = HttpAttemptTiming(clock=clock, wall_clock=clock, response_mode="stream")
    clock.advance(0.1)
    timing.mark_connection_complete()

    assert timing.next_deadline(_timeouts(first=0.2, idle=0.7)).outcome == "first_byte_timeout"
    assert timing.next_deadline(_timeouts(first=0.8, idle=0.3)).outcome == "idle_timeout"
    assert timing.next_deadline(_timeouts(first=20, idle=30, total=0.15)).outcome == "total_timeout"


def test_ws_handshake_utf8_frame_and_empty_frame_semantics():
    clock = FakeClock()
    timing = WsAttemptTiming(clock=clock, wall_clock=clock)
    clock.advance(0.4)
    timing.mark_handshake_complete()

    assert timing.snapshot().connection_ms == 400
    assert timing.snapshot().ws_handshake_ms == 400
    assert timing.mark_ws_frame("") is False
    assert timing.mark_ws_frame(b"") is False
    assert timing.snapshot().first_byte_ms is None

    clock.advance(0.12)
    assert timing.mark_ws_frame("é") is True
    assert timing.snapshot().first_byte_ms == 120
    clock.advance(0.07)
    timing.mark_ws_frame(b"x")
    clock.advance(0.03)
    terminal = timing.finish("success")
    assert terminal.first_byte_ms == 120
    assert terminal.idle_ms == 30
    assert terminal.total_ms == 620


@pytest.mark.asyncio
async def test_http_non_stream_connection_is_send_complete_and_first_byte_never_applies():
    clock = FakeClock()
    timing = HttpAttemptTiming(clock=clock, wall_clock=clock, response_mode="non_stream")

    await timing.trace("http11.send_request_body.started", {})
    clock.advance(0.2)
    await timing.trace("http11.send_request_body.complete", {})
    assert timing.snapshot().connection_ms == 200
    # Before the first response byte, non-stream is constrained by total only.
    assert timing.next_deadline(_timeouts(first=0.01, idle=0.01, total=4)).outcome == "total_timeout"

    clock.advance(1.0)
    assert timing.mark_response_body_byte(b"") is False
    assert timing.next_deadline(_timeouts(first=0.01, idle=0.01, total=4)).outcome == "total_timeout"
    assert timing.mark_response_body_byte(b"{") is True
    assert timing.snapshot().first_byte_ms is None
    # The first response byte only starts idle; no non-stream business first-byte
    # metric or timeout is ever exposed.
    terminal = timing.finish("success")
    assert terminal.first_byte_ms is None
    assert terminal.idle_ms == 0


def test_non_stream_snapshot_never_exposes_first_byte_even_after_activity():
    clock = FakeClock()
    timing = HttpAttemptTiming(clock=clock, wall_clock=clock, response_mode="non_stream")
    timing.mark_connection_complete()
    clock.advance(0.5)
    timing.mark_response_body_byte(b"body")
    clock.advance(0.1)
    snap = timing.finish("success")
    assert snap.first_byte_ms is None
    assert snap.response_body_first_byte_wait_ms is None
    assert snap.idle_ms == 100


@pytest.mark.asyncio
async def test_dynamic_wait_switches_same_non_stream_io_task_after_send_complete():
    clock = FakeClock()
    timing = HttpAttemptTiming(clock=clock, wall_clock=clock, response_mode="non_stream")
    gate: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(timing.wait_for(gate, _timeouts(connection=1, total=10)))
    await asyncio.sleep(0)

    clock.advance(0.8)
    timing.mark_connection_complete()
    await asyncio.sleep(0)
    # Crossing the old connection deadline must not cancel the same header task.
    clock.advance(0.5)
    gate.set_result("headers")
    assert await waiter == "headers"


@pytest.mark.asyncio
async def test_wait_for_cancellation_cancels_owned_io_and_rethrows():
    clock = FakeClock()
    timing = WsAttemptTiming(clock=clock, wall_clock=clock)
    owned_cancelled = asyncio.Event()

    async def owned_io():
        try:
            await asyncio.Future()
        finally:
            owned_cancelled.set()

    waiter = asyncio.create_task(timing.wait_for(owned_io(), _timeouts()))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert owned_cancelled.is_set()


def test_deadline_expiration_error_carries_one_selected_business_outcome():
    clock = FakeClock()
    timing = WsAttemptTiming(clock=clock, wall_clock=clock)
    timing.mark_handshake_complete()
    clock.advance(0.5)
    selected = timing.next_deadline(_timeouts(first=0.5, idle=0.5, total=5))
    err = BusinessTimeoutError(selected)
    assert err.outcome == "first_byte_timeout"
    assert str(err) == "first_byte_timeout"


def test_terminal_snapshot_is_idempotent_and_cannot_be_relabelled():
    clock = FakeClock()
    timing = WsAttemptTiming(clock=clock, wall_clock=clock)
    timing.mark_handshake_complete()
    clock.advance(1.0)
    first = timing.finish("success")
    clock.advance(99.0)
    second = timing.finish("cancelled", "late cancellation")

    assert second == first
    assert second.outcome == "success"
    assert second.error_detail is None
    assert second.total_ms == 1000
    assert second.terminal is True


@pytest.mark.asyncio
async def test_ss2022_ambiguous_tunnel_and_target_tls_are_null_not_guessed():
    clock = FakeClock()
    timing = HttpAttemptTiming(
        clock=clock,
        wall_clock=clock,
        route_type="ss2022",
        response_mode="stream",
    )
    await timing.trace("socks.setup_socks5_connection.started", {})
    clock.advance(0.2)
    await timing.trace("socks.setup_socks5_connection.complete", {})
    await timing.trace("connection.start_tls.started", {})
    clock.advance(0.3)
    await timing.trace("connection.start_tls.complete", {})
    snap = timing.snapshot()
    assert snap.proxy_tunnel_ms is None
    assert snap.tls_ms is None
    assert snap.target_tls_ms is None


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (httpx.ConnectTimeout("connect"), "http_connect_timeout"),
        (httpx.PoolTimeout("pool"), "pool_timeout"),
        (httpx.WriteTimeout("write"), "write_timeout"),
        (httpx.ReadTimeout("read"), "read_timeout"),
    ],
)
def test_httpx_timeout_taxonomy_is_distinct(exc, outcome):
    assert classify_httpx_timeout(exc) == outcome


@pytest.mark.asyncio
async def test_ws_terminal_owner_finishes_before_cancellation_is_rethrown():
    entered = asyncio.Event()
    release = asyncio.Event()
    finished: list[str] = []

    async def terminal_owner():
        entered.set()
        await release.wait()
        finished.append("terminal")

    task = asyncio.create_task(await_ws_owned(terminal_owner()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["terminal"]
