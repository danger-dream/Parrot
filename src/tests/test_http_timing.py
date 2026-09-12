"""Deterministic source tests for per-route HTTP round timing.

Execution is intentionally deferred to the import-before-src isolation runner.
All transport objects are local fakes; this module performs no network I/O.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from src.tests import conftest as test_conftest
from src.transports import http_runtime
from src.transports.timing import HttpAttemptTiming


class _Stats:
    total_attempts = 0
    total_failures = 0
    total_successes = 0
    last_attempt_ts = 0.0
    last_success_ts = 0.0
    last_latency_ms = 0
    last_error = ""


class _Connector:
    type = "socks5"

    def __init__(self) -> None:
        self.stats = _Stats()

    def create_httpx_client(self, *, timeout, byte_counter, timing):
        self.kwargs = {
            "timeout": timeout,
            "byte_counter": byte_counter,
            "timing": timing,
        }
        return SimpleNamespace(aclose=self._close)

    async def _close(self):
        return None


class _Response:
    status_code = 200
    headers = {"content-type": "application/octet-stream"}

    def __init__(self, chunks=()) -> None:
        self._chunks = list(chunks)

    async def _body(self):
        for chunk in self._chunks:
            yield chunk

    def aiter_bytes(self):
        return self._body()


class _Context:
    def __init__(self, request, *, response=None, error=None, wait=None) -> None:
        self.trace = request.extensions["trace"]
        self.response = response or _Response()
        self.error = error
        self.wait = wait
        self.exited = False

    async def __aenter__(self):
        if self.wait is not None:
            await self.wait.wait()
        if self.error is not None:
            raise self.error
        await self.trace("http11.send_request_body.started", {})
        await self.trace("http11.send_request_body.complete", {})
        await self.trace("http11.receive_response_headers.started", {})
        await self.trace("http11.receive_response_headers.complete", {})
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return None


class _DelayedHeadersContext(_Context):
    def __init__(self, request, *, header_delay: float) -> None:
        super().__init__(request)
        self.header_delay = float(header_delay)

    async def __aenter__(self):
        await self.trace("http11.send_request_body.started", {})
        await self.trace("http11.send_request_body.complete", {})
        await self.trace("http11.receive_response_headers.started", {})
        await asyncio.sleep(self.header_delay)
        await self.trace("http11.receive_response_headers.complete", {})
        return self.response


class _AfterDispatchErrorContext(_Context):
    async def __aenter__(self):
        await self.trace("http11.send_request_headers.started", {})
        raise httpx.ReadTimeout("failed after request dispatch")


def _request():
    return SimpleNamespace(
        method="POST", url="https://unit.invalid", headers={}, body=b"{}",
    )


def _proxy_update_recorder(updates):
    def update(
        handle,
        started_at=None,
        connect_ms=None,
        first_byte_ms=None,
        idle_ms=None,
        total_ms=None,
        dns_ms=None,
        tcp_ms=None,
        proxy_tcp_ms=None,
        proxy_tunnel_ms=None,
        tls_ms=None,
        target_tls_ms=None,
        ws_handshake_ms=None,
        request_upload_ms=None,
        response_headers_wait_ms=None,
        response_body_first_byte_wait_ms=None,
        ended_at=None,
        outcome=None,
        error_detail=None,
        bytes_up=None,
        bytes_down=None,
    ):
        updates.append((handle, {
            "started_at": started_at,
            "connect_ms": connect_ms,
            "first_byte_ms": first_byte_ms,
            "idle_ms": idle_ms,
            "total_ms": total_ms,
            "dns_ms": dns_ms,
            "tcp_ms": tcp_ms,
            "proxy_tcp_ms": proxy_tcp_ms,
            "proxy_tunnel_ms": proxy_tunnel_ms,
            "tls_ms": tls_ms,
            "target_tls_ms": target_tls_ms,
            "ws_handshake_ms": ws_handshake_ms,
            "request_upload_ms": request_upload_ms,
            "response_headers_wait_ms": response_headers_wait_ms,
            "response_body_first_byte_wait_ms": response_body_first_byte_wait_ms,
            "ended_at": ended_at,
            "outcome": outcome,
            "error_detail": error_detail,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
        }))
    return update


def _patch_persistence(monkeypatch):
    inserted = []
    updates = []

    def record(
        request_id,
        retry_id,
        order,
        proxy_name,
        started_at,
        *,
        round_id=None,
        transport=None,
        request_mode=None,
    ):
        handle = f"route-{order}"
        inserted.append({
            "handle": handle,
            "request_id": request_id,
            "retry_id": retry_id,
            "order": order,
            "proxy_name": proxy_name,
            "started_at": started_at,
            "round_id": round_id,
            "transport": transport,
            "request_mode": request_mode,
        })
        return handle

    monkeypatch.setattr(http_runtime.log_db, "record_proxy_attempt", record)
    monkeypatch.setattr(
        http_runtime.log_db, "update_proxy_attempt", _proxy_update_recorder(updates),
    )
    monkeypatch.setattr(http_runtime.upstream, "acquire_client", lambda: SimpleNamespace(client=object(), release=lambda: None))
    return inserted, updates


def _open_kwargs(*, response_mode="stream"):
    return {
        "channel": SimpleNamespace(),
        "resolved_model": "m",
        "upstream_req": _request(),
        "connect_timeout": 1,
        "first_byte_timeout": 1,
        "idle_timeout": 1,
        "total_timeout": 5,
        "response_mode": response_mode,
        "request_id": "r",
        "retry_attempt_id": "retry-handle",
    }


@pytest.mark.asyncio
async def test_direct_stream_round_records_direct_and_only_nonempty_raw_bytes_are_activity(monkeypatch):
    inserted, updates = _patch_persistence(monkeypatch)
    response = _Response([b"", b"first", b"", b"second"])
    contexts = []

    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("direct", None)], None),
    )

    def open_stream(client, request):
        ctx = _Context(request, response=response)
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr(http_runtime, "open_stream", open_stream)
    opened = await http_runtime.open_response_with_proxy_chain(**_open_kwargs())

    assert opened.ok
    assert inserted[0]["proxy_name"] == "direct"
    assert inserted[0]["transport"] == "http"
    assert inserted[0]["request_mode"] == "http_stream"
    assert inserted[0]["round_id"] == opened.timing.round_id
    assert updates[-1][1]["outcome"] == "open"
    assert updates[-1][1]["ended_at"] is None

    aiter = response.aiter_bytes()
    opened.timing.start_response_body_wait()
    first = await http_runtime._next_nonempty_http_chunk(
        aiter, opened.timing, opened.round_timeouts,
    )
    first_snapshot = opened.timing.snapshot()
    second = await http_runtime._next_nonempty_http_chunk(
        aiter, opened.timing, opened.round_timeouts,
    )
    with pytest.raises(StopAsyncIteration):
        await http_runtime._next_nonempty_http_chunk(
            aiter, opened.timing, opened.round_timeouts,
        )
    opened.timing.mark_io_complete()
    terminal = await http_runtime.finalize_opened_http_response(opened, "success")
    await http_runtime._drain_proxy_attempt_persistence()

    assert first == b"first"
    assert second == b"second"
    assert first_snapshot.first_byte_ms is not None
    assert terminal.first_byte_ms is not None
    assert terminal.idle_ms is not None
    assert terminal.total_ms is not None
    assert updates[-1][1]["outcome"] == "success"
    assert updates[-1][1]["ended_at"] is not None
    assert contexts[0].exited is False  # response owner closes after terminal handling


@pytest.mark.asyncio
async def test_stream_response_headers_may_exceed_connect_timeout(monkeypatch):
    _inserted, updates = _patch_persistence(monkeypatch)
    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("direct", None)], None),
    )
    monkeypatch.setattr(
        http_runtime,
        "open_stream",
        lambda client, request: _DelayedHeadersContext(request, header_delay=0.05),
    )
    kwargs = _open_kwargs()
    kwargs.update(connect_timeout=0.01, first_byte_timeout=0.2)

    opened = await http_runtime.open_response_with_proxy_chain(**kwargs)

    assert opened.ok
    assert opened.timing.connection_complete
    assert opened.connect_ms is not None
    assert updates[-1][1]["outcome"] == "open"
    await opened.ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_nonstream_connection_is_send_complete_first_is_null_and_idle_starts_on_body(monkeypatch):
    inserted, updates = _patch_persistence(monkeypatch)
    response = _Response([b"payload"])

    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("direct", None)], None),
    )
    monkeypatch.setattr(
        http_runtime, "open_stream",
        lambda client, request: _Context(request, response=response),
    )

    opened = await http_runtime.open_response_with_proxy_chain(
        **_open_kwargs(response_mode="non_stream")
    )
    assert opened.ok
    assert opened.timing.connection_complete
    assert inserted[0]["request_mode"] == "http_non_stream"

    body = await http_runtime.read_non_stream_body(
        opened.ctx,
        opened.response,
        connect_ms=opened.connect_ms,
        timing=opened.timing,
        round_timeouts=opened.round_timeouts,
    )
    assert body.error is None and body.raw == b"payload"
    terminal = await http_runtime.finalize_opened_http_response(opened, "success")
    await http_runtime._drain_proxy_attempt_persistence()
    assert terminal.connection_ms is not None
    assert terminal.first_byte_ms is None
    assert terminal.idle_ms is not None
    assert terminal.total_ms is not None
    assert updates[-1][1]["first_byte_ms"] is None


@pytest.mark.asyncio
async def test_proxy_switch_creates_fresh_round_and_terminalizes_previous_round(monkeypatch):
    inserted, updates = _patch_persistence(monkeypatch)
    connector = _Connector()
    calls = 0

    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("p1", connector), ("direct", None)], None),
    )

    def open_stream(client, request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Context(request, error=httpx.ConnectError("proxy route failed"))
        return _Context(request, response=_Response([b"ok"]))

    monkeypatch.setattr(http_runtime, "open_stream", open_stream)
    opened = await http_runtime.open_response_with_proxy_chain(**_open_kwargs())
    await http_runtime._drain_proxy_attempt_persistence()

    assert opened.ok
    assert [row["proxy_name"] for row in inserted] == ["p1", "direct"]
    assert inserted[0]["round_id"] != inserted[1]["round_id"]
    assert opened.timing.round_id == inserted[1]["round_id"]
    assert any(handle == "route-1" and data["outcome"] == "connect_error" for handle, data in updates)
    assert any(handle == "route-2" and data["outcome"] == "open" for handle, data in updates)


@pytest.mark.asyncio
async def test_proxy_route_is_not_replayed_after_request_dispatch_started(monkeypatch):
    inserted, updates = _patch_persistence(monkeypatch)
    connector = _Connector()
    calls = 0
    dispatched = []

    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("p1", connector), ("direct", None)], None),
    )
    monkeypatch.setattr(
        http_runtime.log_db,
        "mark_retry_attempt_dispatch",
        lambda retry_id, body, *, dispatch_metadata, dispatched_at: dispatched.append({
            "retry_id": retry_id,
            "body": body,
            "dispatch_metadata": dispatch_metadata,
            "dispatched_at": dispatched_at,
        }),
    )

    def open_stream(client, request):
        nonlocal calls
        calls += 1
        return _AfterDispatchErrorContext(request)

    monkeypatch.setattr(http_runtime, "open_stream", open_stream)
    opened = await http_runtime.open_response_with_proxy_chain(**_open_kwargs())
    await http_runtime._drain_proxy_attempt_persistence()

    assert opened.error is not None and opened.error.outcome == "read_timeout"
    assert calls == 1
    assert [row["proxy_name"] for row in inserted] == ["p1"]
    assert len(dispatched) == 1
    assert dispatched[0]["retry_id"] == "retry-handle"
    assert dispatched[0]["body"] == b"{}"
    assert dispatched[0]["dispatch_metadata"] is None
    assert isinstance(dispatched[0]["dispatched_at"], float)
    assert updates[-1][1]["outcome"] == "read_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (httpx.ConnectTimeout("dial"), "http_connect_timeout"),
        (httpx.PoolTimeout("pool"), "pool_timeout"),
        (httpx.WriteTimeout("write"), "write_timeout"),
        (httpx.ReadTimeout("read"), "read_timeout"),
    ],
)
async def test_httpx_transport_timeouts_remain_distinct_from_business_timeouts(
    monkeypatch, exc, outcome,
):
    _inserted, updates = _patch_persistence(monkeypatch)
    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("direct", None)], None),
    )
    monkeypatch.setattr(
        http_runtime, "open_stream",
        lambda client, request: _Context(request, error=exc),
    )

    opened = await http_runtime.open_response_with_proxy_chain(**_open_kwargs())
    await http_runtime._drain_proxy_attempt_persistence()
    assert opened.error is not None
    assert opened.error.outcome == outcome
    assert updates[-1][1]["outcome"] == outcome
    assert updates[-1][1]["total_ms"] is not None


@pytest.mark.asyncio
async def test_terminal_timing_freezes_before_real_executor_queue(monkeypatch):
    now = {"monotonic": 0.0, "wall": 1000.0}
    timing = HttpAttemptTiming(
        response_mode="stream",
        clock=lambda: now["monotonic"],
        wall_clock=lambda: now["wall"],
    )
    timing.mark_connection_complete(at=0.1)
    now["monotonic"] = 0.2
    timing.mark_response_body_byte(b"first")
    now["monotonic"] = 0.5
    opened = http_runtime.OpenedHttpResponse(
        timing=timing,
        proxy_attempt_id="route-queued",
        proxy_bytes={"up": 1, "down": 2},
    )

    persisted = []
    monkeypatch.setattr(
        http_runtime.log_db,
        "update_proxy_attempt",
        _proxy_update_recorder(persisted),
    )
    monkeypatch.setattr(asyncio, "to_thread", test_conftest._ORIG_TO_THREAD)

    worker_started = threading.Event()
    allow_worker = threading.Event()

    def occupy_only_worker():
        worker_started.set()
        assert allow_worker.wait(timeout=5)

    loop = asyncio.get_running_loop()
    previous_executor = getattr(loop, "_default_executor", None)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            loop.set_default_executor(executor)
            occupied = loop.run_in_executor(None, occupy_only_worker)
            assert worker_started.wait(timeout=5)

            finalizer = asyncio.create_task(
                http_runtime.finalize_opened_http_response(opened, "success")
            )
            await asyncio.sleep(0)
            assert timing.terminal is True
            assert finalizer.done() is True
            terminal = finalizer.result()
            assert persisted == []

            # Advancing the source clock while the real worker is queued must not
            # extend metrics or delay the caller that can proceed to failover.
            now["monotonic"] = 9.0
            now["wall"] = 1008.5
            allow_worker.set()
            await occupied
            await http_runtime._drain_proxy_attempt_persistence()
    finally:
        loop._default_executor = previous_executor

    assert terminal.total_ms == 500
    assert terminal.idle_ms == 300
    assert persisted[0][0] == "route-queued"
    assert persisted[0][1]["total_ms"] == 500
    assert persisted[0][1]["idle_ms"] == 300


@pytest.mark.asyncio
async def test_precommit_cancel_closes_owned_context_persists_cancel_and_rethrows(monkeypatch):
    _inserted, updates = _patch_persistence(monkeypatch)
    gate = asyncio.Event()
    context_created = asyncio.Event()
    contexts = []

    monkeypatch.setattr(
        http_runtime, "_resolve_http_route_chain",
        lambda channel, model: ([("direct", None)], None),
    )

    def open_stream(client, request):
        ctx = _Context(request, wait=gate)
        contexts.append(ctx)
        context_created.set()
        return ctx

    monkeypatch.setattr(http_runtime, "open_stream", open_stream)
    task = asyncio.create_task(
        http_runtime.open_response_with_proxy_chain(**_open_kwargs())
    )
    await context_created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await http_runtime._drain_proxy_attempt_persistence()

    assert contexts[0].exited is True
    assert updates[-1][1]["outcome"] == "cancelled"
    assert updates[-1][1]["ended_at"] is not None
    assert updates[-1][1]["total_ms"] is not None
