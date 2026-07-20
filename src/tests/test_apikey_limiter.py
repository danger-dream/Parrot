import asyncio
import gc
import weakref
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from src import apikey_limiter


def _cfg(**limits):
    return {
        "apiKeyConcurrency": {
            "enabled": True,
            "defaultMaxConcurrent": 1,
            "defaultMaxQueue": 1,
            "defaultQueueWaitSeconds": 1,
        },
        "apiKeys": {
            "k": {
                "key": "secret",
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "limits": limits,
            }
        },
    }


@pytest.fixture(autouse=True)
def _reset_slots(monkeypatch, tmp_path):
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_total = 0
    apikey_limiter._active_body_guards.clear()
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _cfg())
    monkeypatch.setattr(apikey_limiter.config, "DATA_DIR", str(tmp_path))
    yield
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    apikey_limiter._queued_body_spool_bytes_by_key.clear()
    apikey_limiter._queued_body_spool_bytes_total = 0
    apikey_limiter._active_body_guards.clear()


@pytest.mark.asyncio
async def test_default_limit_queues_and_releases_fifo():
    first = await apikey_limiter.acquire("k")
    task = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    snap = apikey_limiter.key_snapshot("k")
    assert snap["in_flight"] == 1
    assert snap["waiting"] == 1

    await first.release()
    second = await asyncio.wait_for(task, timeout=1)
    assert second.queue_wait_ms >= 0
    await second.release()
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
async def test_queue_full_returns_429_error():
    first = await apikey_limiter.acquire("k")
    queued = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    with pytest.raises(apikey_limiter.ApiKeyLimitError) as ei:
        await apikey_limiter.acquire("k")
    assert ei.value.reason == "queue_full"
    queued.cancel()
    await first.release()


@pytest.mark.asyncio
async def test_key_limits_enabled_overrides_global(monkeypatch):
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: {
            "apiKeyConcurrency": {
                "enabled": False,
                "defaultMaxConcurrent": 1,
                "defaultMaxQueue": 1,
                "defaultQueueWaitSeconds": 1,
            },
            "apiKeys": {"k": {"key": "secret", "limits": {"enabled": True, "maxConcurrent": 1}}},
        },
    )
    first = await apikey_limiter.acquire("k")
    queued = asyncio.create_task(apikey_limiter.acquire("k"))
    await asyncio.sleep(0.05)
    assert apikey_limiter.key_snapshot("k")["waiting"] == 1
    queued.cancel()
    await first.release()


@pytest.mark.asyncio
async def test_queued_disconnect_watch_preserves_unread_request_body():
    """Disconnect monitoring may read ASGI events only if it replays them."""
    first = await apikey_limiter.acquire("k")
    chunks = [
        {"type": "http.request", "body": b'{"model":"gpt-test",', "more_body": True},
        {"type": "http.request", "body": b'"input":"hello"}', "more_body": False},
    ]
    body_seen_by_watcher = asyncio.Event()
    no_more_events = asyncio.Event()

    async def receive():
        if chunks:
            message = chunks.pop(0)
            if not chunks:
                body_seen_by_watcher.set()
            return message
        await no_more_events.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/responses",
            "headers": [],
        },
        receive,
    )

    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await asyncio.wait_for(body_seen_by_watcher.wait(), timeout=1)
    await first.release()
    second = await asyncio.wait_for(queued, timeout=1)
    assert second.receive is not None

    downstream_request = Request(request.scope, second.receive)
    body = await asyncio.wait_for(downstream_request.body(), timeout=1)
    assert body == b'{"model":"gpt-test","input":"hello"}'
    assert apikey_limiter._queued_body_bytes_total == 0
    await second.release()


@pytest.mark.asyncio
async def test_queued_request_body_replay_is_bounded(monkeypatch):
    monkeypatch.setattr(apikey_limiter, "DEFAULT_MAX_REQUEST_BODY_BYTES", 8)
    first = await apikey_limiter.acquire("k")
    chunks = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    request = Request(
        {
            "type": "http", "method": "POST", "path": "/v1/responses",
            # The actual ASGI stream, not Content-Length alone, must enforce the cap.
            "headers": [(b"content-length", b"1")],
        },
        receive,
    )
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))

    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(queued, timeout=1)
    assert exc_info.value.max_bytes == 8
    assert apikey_limiter.key_snapshot("k")["waiting"] == 0
    await first.release()


@pytest.mark.asyncio
async def test_queued_request_disconnects_cleanly_after_partial_body():
    first = await apikey_limiter.acquire("k")
    chunks = [{"type": "http.request", "body": b'{"model":', "more_body": True}]
    disconnected = asyncio.Event()

    async def receive():
        if chunks:
            return chunks.pop(0)
        await disconnected.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        receive,
    )
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await asyncio.sleep(0.05)
    disconnected.set()

    with pytest.raises(apikey_limiter.ApiKeyLimitError) as exc_info:
        await asyncio.wait_for(queued, timeout=1)
    assert exc_info.value.reason == "client_disconnected"
    assert apikey_limiter.key_snapshot("k")["waiting"] == 0
    await first.release()


@pytest.mark.asyncio
async def test_fastapi_middleware_replays_queued_chunked_body_end_to_end():
    app = FastAPI()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_body_sent = asyncio.Event()

    class LimitRequests:
        def __init__(self, downstream):
            self.downstream = downstream

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self.downstream(scope, receive, send)
                return
            request = Request(scope, receive=receive)
            lease = await apikey_limiter.acquire("k", request, receive=receive)
            try:
                await self.downstream(scope, lease.receive or receive, send)
            finally:
                await lease.release()

    app.add_middleware(LimitRequests)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        if body == b"hold":
            first_started.set()
            await release_first.wait()
        return Response(content=body)

    async def chunked_json():
        yield b'{"model":"gpt-test",'
        yield b'"input":"hello"}'
        second_body_sent.set()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/echo", content=b"hold"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = asyncio.create_task(client.post("/echo", content=chunked_json()))
        await asyncio.wait_for(second_body_sent.wait(), timeout=1)
        release_first.set()

        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.content == b"hold"
    assert second_response.content == b'{"model":"gpt-test","input":"hello"}'


async def _wait_for_waiters(key_name: str, count: int) -> None:
    async def ready():
        while apikey_limiter.key_snapshot(key_name)["waiting"] != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(ready(), timeout=1)


@pytest.mark.asyncio
async def test_disconnect_after_handoff_wakes_next_waiter(monkeypatch):
    """A disconnect that wins after its waiter was signalled must pass the slot on."""
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _cfg(maxQueue=3))
    first = await apikey_limiter.acquire("k")
    disconnected = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        receive,
    )
    original_drop = apikey_limiter._drop_waiter
    drop_started = asyncio.Event()
    allow_drop = asyncio.Event()

    async def delayed_drop(slot, waiter):
        if asyncio.current_task().get_name() == "disconnecting-waiter":
            drop_started.set()
            await allow_drop.wait()
        await original_drop(slot, waiter)

    monkeypatch.setattr(apikey_limiter, "_drop_waiter", delayed_drop)
    disconnecting = asyncio.create_task(
        apikey_limiter.acquire("k", request), name="disconnecting-waiter"
    )
    next_waiter = asyncio.create_task(apikey_limiter.acquire("k"))
    await _wait_for_waiters("k", 2)

    disconnected.set()
    await asyncio.wait_for(drop_started.wait(), timeout=1)
    await first.release()  # Pops/signals the disconnecting waiter before its abort cleanup.
    allow_drop.set()

    with pytest.raises(apikey_limiter.ApiKeyLimitError) as exc_info:
        await asyncio.wait_for(disconnecting, timeout=1)
    assert exc_info.value.reason == "client_disconnected"
    admitted = await asyncio.wait_for(next_waiter, timeout=1)
    await admitted.release()


@pytest.mark.asyncio
async def test_cancellation_after_handoff_wakes_next_waiter(monkeypatch):
    """Cancellation between wake-up and in_flight increment must repair handoff."""
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _cfg(maxQueue=3))
    original_wait = apikey_limiter._wait_for_turn_or_abort
    woke = asyncio.Event()
    hold_after_wakeup = asyncio.Event()

    async def pause_after_wakeup(*args, **kwargs):
        await original_wait(*args, **kwargs)
        if asyncio.current_task().get_name() == "cancelled-after-wakeup":
            woke.set()
            await hold_after_wakeup.wait()

    monkeypatch.setattr(apikey_limiter, "_wait_for_turn_or_abort", pause_after_wakeup)
    first = await apikey_limiter.acquire("k")
    cancelled = asyncio.create_task(
        apikey_limiter.acquire("k"), name="cancelled-after-wakeup"
    )
    next_waiter = asyncio.create_task(apikey_limiter.acquire("k"))
    await _wait_for_waiters("k", 2)

    await first.release()
    await asyncio.wait_for(woke.wait(), timeout=1)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    admitted = await asyncio.wait_for(next_waiter, timeout=1)
    await admitted.release()


def _aggregate_budget_cfg(*, per_key: int, process: int, max_events: int = 10):
    return {
        "apiKeyConcurrency": {
            "enabled": True,
            "defaultMaxConcurrent": 1,
            "defaultMaxQueue": 4,
            "defaultQueueWaitSeconds": 10,
            "defaultMaxRequestBodyBytes": 100,
            "defaultMaxRequestBodyEvents": max_events,
            "defaultMaxQueuedBodyBytesPerKey": per_key,
            "maxQueuedBodyBytesTotal": process,
        },
        "apiKeys": {
            name: {"key": "secret", "enabled": True, "limits": {}}
            for name in ("k", "k2")
        },
    }


def _spool_cfg(tmp_path, *, wait_seconds: int = 10):
    cfg = _aggregate_budget_cfg(per_key=100_000, process=100_000)
    cfg["apiKeyConcurrency"].update({
        "defaultQueueWaitSeconds": wait_seconds,
        "defaultMaxRequestBodyBytes": 100_000,
        "queuedBodySpoolThresholdBytes": 1,
        "defaultMaxQueuedBodySpoolBytesPerKey": 100_000,
        "maxQueuedBodySpoolBytes": 100_000,
    })
    return cfg


def _queued_request(messages, consumed: asyncio.Event | None = None) -> Request:
    remaining = list(messages)
    block = asyncio.Event()

    async def receive():
        if remaining:
            message = remaining.pop(0)
            if not remaining and consumed is not None:
                consumed.set()
            return message
        await block.wait()
        return {"type": "http.disconnect"}

    return Request(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        receive,
    )


@pytest.mark.asyncio
async def test_per_key_queued_body_aggregate_budget(monkeypatch):
    monkeypatch.setattr(apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False)
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: _aggregate_budget_cfg(per_key=12, process=100),
    )
    first = await apikey_limiter.acquire("k")
    first_body_consumed = asyncio.Event()
    queued = asyncio.create_task(
        apikey_limiter.acquire(
            "k",
            _queued_request(
                [{"type": "http.request", "body": b"12345678", "more_body": True}],
                first_body_consumed,
            ),
        )
    )
    await asyncio.wait_for(first_body_consumed.wait(), timeout=1)

    rejected = asyncio.create_task(
        apikey_limiter.acquire(
            "k",
            _queued_request(
                [{"type": "http.request", "body": b"abcdefgh", "more_body": True}]
            ),
        )
    )
    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(rejected, timeout=1)
    assert exc_info.value.reason == "key_aggregate"

    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    await first.release()
    assert apikey_limiter._queued_body_bytes_total == 0


@pytest.mark.asyncio
async def test_process_wide_queued_body_aggregate_budget(monkeypatch):
    monkeypatch.setattr(apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False)
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: _aggregate_budget_cfg(per_key=100, process=12),
    )
    active_k = await apikey_limiter.acquire("k")
    active_k2 = await apikey_limiter.acquire("k2")
    first_body_consumed = asyncio.Event()
    queued = asyncio.create_task(
        apikey_limiter.acquire(
            "k",
            _queued_request(
                [{"type": "http.request", "body": b"12345678", "more_body": True}],
                first_body_consumed,
            ),
        )
    )
    await asyncio.wait_for(first_body_consumed.wait(), timeout=1)

    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(
            apikey_limiter.acquire(
                "k2",
                _queued_request(
                    [{"type": "http.request", "body": b"abcdefgh", "more_body": True}]
                ),
            ),
            timeout=1,
        )
    assert exc_info.value.reason == "process_aggregate"

    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    await active_k.release()
    await active_k2.release()
    assert apikey_limiter._queued_body_bytes_total == 0


@pytest.mark.asyncio
async def test_aggregate_error_survives_watcher_cancellation_and_gather(monkeypatch):
    """Admission may win the wait race, but a watcher aggregate error must win cleanup."""
    monkeypatch.setattr(
        apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False,
    )
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: _aggregate_budget_cfg(per_key=100, process=5),
    )
    original_reserve = apikey_limiter._BodyPreservingReceive._reserve
    reserve_started = asyncio.Event()
    allow_reserve = asyncio.Event()

    async def paused_reserve(self, message):
        reserve_started.set()
        await allow_reserve.wait()
        return await original_reserve(self, message)

    monkeypatch.setattr(
        apikey_limiter._BodyPreservingReceive, "_reserve", paused_reserve,
    )
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"12345678", "more_body": True},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await asyncio.wait_for(reserve_started.wait(), timeout=1)

    # Wake/admit the FIFO waiter while its watcher has an event in hand.  The
    # wait cleanup cancels that watcher and gathers it; the pending aggregate
    # failure must not be replaced by a successful lease or CancelledError.
    await active.release()
    await asyncio.sleep(0.05)
    assert not queued.done()
    allow_reserve.set()

    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(queued, timeout=1)
    assert exc_info.value.reason == "process_aggregate"
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"x"], ids=["empty", "one-byte"])
async def test_queued_body_event_count_is_bounded(monkeypatch, body):
    monkeypatch.setattr(
        apikey_limiter.config,
        "get",
        lambda: _aggregate_budget_cfg(per_key=10_000, process=10_000, max_events=3),
    )
    first = await apikey_limiter.acquire("k")
    request = _queued_request(
        [
            {"type": "http.request", "body": body, "more_body": True}
            for _ in range(4)
        ]
    )

    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(apikey_limiter.acquire("k", request), timeout=1)
    assert exc_info.value.reason == "event_count"
    assert apikey_limiter._queued_body_bytes_total == 0
    await first.release()


@pytest.mark.asyncio
async def test_release_cancellation_during_guard_cleanup_does_not_leak_capacity():
    lease = await apikey_limiter.acquire("k")
    request = _queued_request([])
    assert lease.limits is not None
    guard = apikey_limiter._body_preserving_receive(request, "k", lease.limits)
    await guard._reserve(
        {"type": "http.request", "body": b"buffered", "more_body": True}
    )
    lease._replay_guard = guard

    await apikey_limiter._queued_body_lock.acquire()
    cleanup_task = None
    try:
        releasing = asyncio.create_task(lease.release())

        async def cleanup_started():
            while lease._release_task is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(cleanup_started(), timeout=1)
        cleanup_task = lease._release_task
        assert cleanup_task is not None
        await asyncio.sleep(0)
        assert not cleanup_task.done()

        releasing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await releasing
        assert not lease._released
        assert apikey_limiter._queued_body_bytes_total > 0
        assert apikey_limiter.key_snapshot("k")["in_flight"] == 1
    finally:
        apikey_limiter._queued_body_lock.release()

    await asyncio.wait_for(lease.release(), timeout=1)
    assert lease._release_task is cleanup_task
    assert lease._released
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_bytes_by_key == {}
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0

    replacement = await asyncio.wait_for(apikey_limiter.acquire("k"), timeout=1)
    await replacement.release()


@pytest.mark.asyncio
async def test_hot_disable_releases_all_signaled_waiters(monkeypatch):
    current = _cfg(maxQueue=4, queueWaitSeconds=10)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    first = await apikey_limiter.acquire("k")
    queued = [
        asyncio.create_task(apikey_limiter.acquire("k"))
        for _ in range(3)
    ]
    await _wait_for_waiters("k", 3)

    current["apiKeyConcurrency"]["enabled"] = False
    await first.release()
    leases = await asyncio.wait_for(asyncio.gather(*queued), timeout=1)

    assert all(lease.noop for lease in leases)
    assert apikey_limiter.key_snapshot("k")["waiting"] == 0
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
async def test_noop_response_attachment_cleans_unread_replay_buffer():
    limits = apikey_limiter._resolve_limits("k")
    request = _queued_request([])
    guard = apikey_limiter._body_preserving_receive(request, "k", limits)
    await guard._reserve(
        {"type": "http.request", "body": b"unread", "more_body": True}
    )
    lease = apikey_limiter.ApiKeyLease(
        "k", None, limits, noop=True, replay_guard=guard
    )

    response = apikey_limiter.attach_release_to_response(Response(), lease)
    assert response is not None
    assert lease.attached_to_response
    assert apikey_limiter._queued_body_bytes_total > 0

    async def cleaned_up():
        while not lease._released:
            await asyncio.sleep(0)

    await asyncio.wait_for(cleaned_up(), timeout=1)
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_bytes_by_key == {}


@pytest.mark.asyncio
async def test_streaming_response_wrapper_propagates_aclose_to_inner_iterator():
    class InnerStream:
        def __init__(self):
            self.sent = False
            self.closed = False
            self.release = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.sent:
                self.sent = True
                return b"chunk"
            await self.release.wait()
            raise StopAsyncIteration

        async def aclose(self):
            self.closed = True
            self.release.set()

    lease = await apikey_limiter.acquire("k")
    inner = InnerStream()
    response = apikey_limiter.attach_release_to_response(
        StreamingResponse(inner), lease
    )
    wrapped = response.body_iterator

    assert await anext(wrapped) == b"chunk"
    await wrapped.aclose()

    assert inner.closed
    assert lease._released
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
async def test_lowered_body_budget_applies_to_next_queued_event(monkeypatch):
    monkeypatch.setattr(
        apikey_limiter, "QUEUED_BODY_EVENT_OVERHEAD_BYTES", 0, raising=False
    )
    current = _aggregate_budget_cfg(per_key=100, process=100)
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 20
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    first = await apikey_limiter.acquire("k")
    first_event_received = asyncio.Event()
    allow_second_event = asyncio.Event()
    block = asyncio.Event()
    event_number = 0

    async def receive():
        nonlocal event_number
        event_number += 1
        if event_number == 1:
            first_event_received.set()
            return {"type": "http.request", "body": b"12345", "more_body": True}
        if event_number == 2:
            await allow_second_event.wait()
            return {"type": "http.request", "body": b"67890", "more_body": True}
        await block.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        receive,
    )
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await asyncio.wait_for(first_event_received.wait(), timeout=1)

    async def first_event_accounted():
        while apikey_limiter._queued_body_bytes_total != 5:
            await asyncio.sleep(0)

    await asyncio.wait_for(first_event_accounted(), timeout=1)
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 8
    allow_second_event.set()

    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await asyncio.wait_for(queued, timeout=1)
    assert exc_info.value.max_bytes == 8
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter.key_snapshot("k")["waiting"] == 0
    await first.release()


@pytest.mark.asyncio
async def test_handoff_reserves_capacity_against_new_arrivals(monkeypatch):
    current = _cfg(maxQueue=4, queueWaitSeconds=10)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    original_wait = apikey_limiter._wait_for_turn_or_abort
    old_woke = asyncio.Event()
    allow_old = asyncio.Event()

    async def pause_old_after_wakeup(*args, **kwargs):
        await original_wait(*args, **kwargs)
        current_task = asyncio.current_task()
        if current_task is not None and current_task.get_name() == "old-waiter":
            old_woke.set()
            await allow_old.wait()

    monkeypatch.setattr(
        apikey_limiter,
        "_wait_for_turn_or_abort",
        pause_old_after_wakeup,
    )
    active = await apikey_limiter.acquire("k")
    old = asyncio.create_task(apikey_limiter.acquire("k"), name="old-waiter")
    await _wait_for_waiters("k", 1)

    await active.release()
    await asyncio.wait_for(old_woke.wait(), timeout=1)
    newcomer = asyncio.create_task(apikey_limiter.acquire("k"), name="newcomer")
    await asyncio.sleep(0.05)

    assert not newcomer.done()
    assert apikey_limiter.key_snapshot("k")["waiting"] == 1
    allow_old.set()
    old_lease = await asyncio.wait_for(old, timeout=1)
    assert not newcomer.done()
    await old_lease.release()
    new_lease = await asyncio.wait_for(newcomer, timeout=1)
    await new_lease.release()


@pytest.mark.asyncio
async def test_hot_disable_wakes_waiters_without_active_release(monkeypatch):
    current = _cfg(maxQueue=4, queueWaitSeconds=10)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    active = await apikey_limiter.acquire("k")
    queued = [
        asyncio.create_task(apikey_limiter.acquire("k"))
        for _ in range(2)
    ]
    await _wait_for_waiters("k", 2)

    current["apiKeyConcurrency"]["enabled"] = False
    leases = await asyncio.wait_for(asyncio.gather(*queued), timeout=2)

    assert all(lease.noop for lease in leases)
    assert apikey_limiter.key_snapshot("k")["waiting"] == 0
    await active.release()


def _request(path: str, *, content_type: str = "application/json", content_length: int | None = None):
    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": path, "headers": headers},
        receive,
    )


def test_public_process_aggregate_key_is_primary_with_legacy_fallback(monkeypatch):
    current = _aggregate_budget_cfg(per_key=100, process=111)
    current["apiKeyConcurrency"]["maxQueuedBodyBytes"] = 222
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    assert apikey_limiter._resolve_limits("k").max_queued_body_bytes_total == 222

    del current["apiKeyConcurrency"]["maxQueuedBodyBytes"]
    assert apikey_limiter._resolve_limits("k").max_queued_body_bytes_total == 111


@pytest.mark.asyncio
async def test_image_total_body_contract_accepts_legal_20mib_single_images(monkeypatch):
    current = _cfg()
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 8 * 1024 * 1024
    current["images"] = {"maxInputImageBytes": 20 * 1024 * 1024}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)

    raw_size = 20 * 1024 * 1024
    multipart = _request(
        "/v1/images/edit",
        content_type="multipart/form-data; boundary=x",
        content_length=raw_size + 4096,
    )
    multipart_lease = await apikey_limiter.acquire("k", multipart)
    await multipart_lease.release()

    data_url_wire = 4 * ((raw_size + 2) // 3) + 512
    json_request = _request(
        "/v1/images/edit", content_length=data_url_wire,
    )
    json_lease = await apikey_limiter.acquire("k", json_request)
    await json_lease.release()


def test_compat_image_contract_covers_multi_image_and_mask(monkeypatch):
    current = _cfg()
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 100
    current["images"] = {"maxInputImageBytes": 300}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    limits = apikey_limiter._resolve_limits("k")

    multipart_max = apikey_limiter._request_body_max_bytes(
        _request("/v1/images/edits", content_type="multipart/form-data; boundary=x"),
        limits,
    )
    json_max = apikey_limiter._request_body_max_bytes(
        _request("/v1/images/edits"), limits,
    )
    # 16 legal images plus one mask, with a bounded form/JSON envelope.
    assert multipart_max >= 17 * 300
    assert json_max >= 17 * (4 * ((300 + 2) // 3))
    assert json_max > multipart_max


@pytest.mark.asyncio
async def test_nonqueued_core_chunked_body_is_not_replay_limited(monkeypatch):
    current = _cfg()
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 5
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    messages = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    request = Request(
        {
            "type": "http", "method": "POST", "path": "/v1/responses",
            # Deliberately false-low: queue replay limits must not become a
            # nonqueued core protocol cap.
            "headers": [(b"content-length", b"1")],
        },
        receive,
    )
    lease = await apikey_limiter.acquire("k", request)
    assert lease.receive is None
    assert await request.body() == b"123456"
    await lease.release()


@pytest.mark.asyncio
async def test_nonqueued_core_event_count_is_not_replay_limited(monkeypatch):
    current = _cfg()
    current["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 100
    current["apiKeyConcurrency"]["defaultMaxRequestBodyEvents"] = 1
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    messages = [
        {"type": "http.request", "body": b"a", "more_body": True},
        {"type": "http.request", "body": b"b", "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []},
        receive,
    )
    lease = await apikey_limiter.acquire("k", request)
    assert lease.receive is None
    assert await request.body() == b"ab"
    await lease.release()


async def _wait_for_spool_bytes(expected: int) -> None:
    async def ready():
        while apikey_limiter._queued_body_spool_bytes_total != expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(ready(), timeout=2)


def _assert_spool_clean(tmp_path, request: Request | None = None) -> None:
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_bytes_by_key == {}
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_by_key == {}
    spool_dir = Path(tmp_path) / "queued-body-spool"
    assert not spool_dir.exists() or list(spool_dir.iterdir()) == []
    # Receive ownership is no longer stored on a Starlette Request. A complete
    # cleanup therefore has no retained guard in the global shutdown registry.
    assert len(apikey_limiter._active_body_guards) == 0


@pytest.mark.asyncio
async def test_spooled_body_cancellation_closes_file_and_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"cancel-me", "more_body": True},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await _wait_for_spool_bytes(len(b"cancel-me"))

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    _assert_spool_clean(tmp_path, request)
    await active.release()


@pytest.mark.asyncio
async def test_spool_disk_budget_stays_reserved_until_file_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    active = await apikey_limiter.acquire("k")
    chunks = (b"first-chunk", b"second-chunk")
    request = _queued_request([
        {"type": "http.request", "body": chunks[0], "more_body": True},
        {"type": "http.request", "body": chunks[1], "more_body": False},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await _wait_for_spool_bytes(sum(map(len, chunks)))
    await active.release()
    lease = await asyncio.wait_for(queued, timeout=1)
    assert lease.receive is not None

    first = await lease.receive()
    assert first["body"] == chunks[0]
    # The backing file still contains both events, so its whole allocation must
    # continue to count even though the first event has been handed downstream.
    assert apikey_limiter._queued_body_spool_bytes_total == sum(map(len, chunks))
    second = await lease.receive()
    assert second["body"] == chunks[1]
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    await lease.release()
    _assert_spool_clean(tmp_path, request)


@pytest.mark.asyncio
async def test_spooled_body_timeout_closes_file_and_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path, wait_seconds=1),
    )
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"timeout-body", "more_body": True},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await _wait_for_spool_bytes(len(b"timeout-body"))

    with pytest.raises(apikey_limiter.ApiKeyLimitError) as exc_info:
        await asyncio.wait_for(queued, timeout=2)
    assert exc_info.value.reason == "queue_timeout"
    _assert_spool_clean(tmp_path, request)
    await active.release()


@pytest.mark.asyncio
async def test_spooled_body_disconnect_closes_file_and_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    active = await apikey_limiter.acquire("k")
    messages = [
        {"type": "http.request", "body": b"partial-body", "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
        receive,
    )
    with pytest.raises(apikey_limiter.ApiKeyLimitError) as exc_info:
        await apikey_limiter.acquire("k", request)
    assert exc_info.value.reason == "client_disconnected"
    _assert_spool_clean(tmp_path, request)
    await active.release()


@pytest.mark.asyncio
async def test_spooled_body_hot_disable_noop_release_cleans(monkeypatch, tmp_path):
    current = _spool_cfg(tmp_path)
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: current)
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"hot-disable", "more_body": True},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await _wait_for_spool_bytes(len(b"hot-disable"))

    current["apiKeyConcurrency"]["enabled"] = False
    lease = await asyncio.wait_for(queued, timeout=2)
    assert lease.noop
    # A noop lease still owns the queued replay resource until response cleanup.
    assert apikey_limiter._queued_body_spool_bytes_total == len(b"hot-disable")
    await lease.release()
    _assert_spool_clean(tmp_path, request)
    await active.release()


@pytest.mark.asyncio
async def test_spool_write_exception_closes_file_and_accounting(monkeypatch, tmp_path):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"io-failure", "more_body": True},
    ])

    async def fail_write(self, *args, **kwargs):
        # Open the real temporary object first so cleanup proves fd/file closure.
        if self._spool is None:
            self._spool = self._open_spool(self._limits)
            self._spool.rollover()
            self._spooled_to_disk = True
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(
        apikey_limiter._BodyPreservingReceive, "_write_spooled_body", fail_write,
    )
    with pytest.raises(apikey_limiter.QueuedBodySpoolError):
        await apikey_limiter.acquire("k", request)
    _assert_spool_clean(tmp_path, request)
    await active.release()


@pytest.mark.asyncio
async def test_spool_error_wins_concurrent_admission_handoff(monkeypatch, tmp_path):
    """A watcher spool failure must not be hidden by a simultaneous FIFO wake."""
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    write_started = asyncio.Event()
    allow_failure = asyncio.Event()

    async def paused_failure(self, *args, **kwargs):
        write_started.set()
        await allow_failure.wait()
        raise OSError("handoff disk failure")

    monkeypatch.setattr(
        apikey_limiter._BodyPreservingReceive,
        "_write_spooled_body",
        paused_failure,
    )
    active = await apikey_limiter.acquire("k")
    request = _queued_request([
        {"type": "http.request", "body": b"handoff-body", "more_body": True},
    ])
    queued = asyncio.create_task(apikey_limiter.acquire("k", request))
    await asyncio.wait_for(write_started.wait(), timeout=1)

    await active.release()
    await asyncio.sleep(0)
    allow_failure.set()
    with pytest.raises(apikey_limiter.QueuedBodySpoolError):
        await asyncio.wait_for(queued, timeout=1)
    _assert_spool_clean(tmp_path, request)
    assert apikey_limiter.key_snapshot("k")["in_flight"] == 0


@pytest.mark.asyncio
async def test_default_spool_capacity_tracks_effective_request_limit_and_stays_bounded(
    monkeypatch, tmp_path,
):
    """Defaults admit one legal body, while concurrent retained bodies remain capped."""
    monkeypatch.setattr(
        apikey_limiter, "DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_PER_KEY", 10,
    )
    monkeypatch.setattr(
        apikey_limiter, "DEFAULT_MAX_QUEUED_BODY_SPOOL_BYTES_TOTAL", 20,
    )
    cfg = _aggregate_budget_cfg(per_key=100_000, process=100_000)
    cfg["apiKeyConcurrency"].update({
        "defaultMaxRequestBodyBytes": 100,
        "queuedBodySpoolThresholdBytes": 1,
        "defaultMaxQueuedBodySpoolBytesPerKey": 10,
        "maxQueuedBodySpoolBytes": 20,
    })
    cfg["apiKeys"]["k2"] = {"key": "secret", "enabled": True, "limits": {}}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)

    first = _queued_request([])
    first_guard = apikey_limiter._body_preserving_receive(
        first, "k", apikey_limiter._resolve_limits("k"),
    )
    await first_guard._reserve(
        {"type": "http.request", "body": b"a" * 60, "more_body": True}
    )
    assert apikey_limiter._queued_body_spool_bytes_total == 60

    second = _queued_request([])
    second_guard = apikey_limiter._body_preserving_receive(
        second, "k2", apikey_limiter._resolve_limits("k2"),
    )
    with pytest.raises(apikey_limiter.RequestBodyTooLarge) as exc_info:
        await second_guard._reserve(
            {"type": "http.request", "body": b"b" * 50, "more_body": True}
        )
    assert exc_info.value.reason == "process_aggregate"
    assert exc_info.value.max_bytes == 100

    await second_guard.abandon()
    await first_guard.abandon()
    _assert_spool_clean(tmp_path)


@pytest.mark.asyncio
async def test_shutdown_spooling_closes_all_live_guards(monkeypatch, tmp_path):
    cfg = _spool_cfg(tmp_path)
    cfg["apiKeys"]["k2"] = {"key": "secret", "enabled": True, "limits": {}}
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: cfg)
    guards = []
    for key, body in (("k", b"first"), ("k2", b"second")):
        request = _queued_request([])
        guard = apikey_limiter._body_preserving_receive(
            request, key, apikey_limiter._resolve_limits(key),
        )
        await guard._reserve(
            {"type": "http.request", "body": body, "more_body": True}
        )
        guards.append(guard)

    assert apikey_limiter._queued_body_spool_bytes_total == 11
    assert len(apikey_limiter._active_body_guards) == 2
    await apikey_limiter.shutdown_spooling()
    await apikey_limiter.shutdown_spooling()  # repeated shutdown is harmless

    assert all(guard._spool is None for guard in guards)
    assert len(apikey_limiter._active_body_guards) == 0
    _assert_spool_clean(tmp_path)


@pytest.mark.asyncio
async def test_repeated_spool_cleanup_releases_fds_files_and_guard_memory(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _spool_cfg(tmp_path))
    fd_dir = Path("/proc/self/fd")
    before_fds = len(list(fd_dir.iterdir())) if fd_dir.exists() else None
    guard_refs = []

    for _ in range(40):
        request = _queued_request([])
        guard = apikey_limiter._body_preserving_receive(
            request, "k", apikey_limiter._resolve_limits("k"),
        )
        guard_refs.append(weakref.ref(guard))
        await guard._reserve(
            {"type": "http.request", "body": b"retained", "more_body": True}
        )
        await guard.abandon()
        await guard.abandon()

    del guard, request
    gc.collect()
    after_fds = len(list(fd_dir.iterdir())) if fd_dir.exists() else None
    if before_fds is not None and after_fds is not None:
        assert after_fds <= before_fds + 1
    assert all(ref() is None for ref in guard_refs)
    assert len(apikey_limiter._active_body_guards) == 0
    _assert_spool_clean(tmp_path)
