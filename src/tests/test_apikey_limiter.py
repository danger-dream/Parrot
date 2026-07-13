import asyncio

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

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
def _reset_slots(monkeypatch):
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0
    monkeypatch.setattr(apikey_limiter.config, "get", lambda: _cfg())
    yield
    apikey_limiter._slots.clear()
    apikey_limiter._queued_body_bytes_by_key.clear()
    apikey_limiter._queued_body_bytes_total = 0


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

    body = await asyncio.wait_for(request.body(), timeout=1)
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
        {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []},
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

    @app.middleware("http")
    async def limit_requests(request: Request, call_next):
        lease = await apikey_limiter.acquire("k", request)
        try:
            return await call_next(request)
        finally:
            await lease.release()

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
