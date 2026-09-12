"""Deterministic client generation, owner-loop and cancellation contracts."""
from __future__ import annotations

import asyncio
import threading

import pytest

from src.upstream_client import SharedClientPool


class Client:
    def __init__(self):
        self.is_closed = False
        self.closes = []
        self.closed = asyncio.Event()

    async def aclose(self):
        self.closes.append((threading.get_ident(), asyncio.get_running_loop()))
        self.is_closed = True
        self.closed.set()


def pool_and_clients():
    clients = []

    def factory():
        client = Client()
        clients.append(client)
        return client

    return SharedClientPool(factory), clients


async def test_lifespan_start_required_and_shutdown_never_lazily_reopens():
    pool, clients = pool_and_clients()
    with pytest.raises(RuntimeError, match="not_started"):
        pool.get()
    pool.invalidate()
    assert pool.snapshot() == {"state": "not_started", "ready": False}
    first = pool.start()
    assert pool.start() is first
    await pool.close()
    pool.invalidate()
    with pytest.raises(RuntimeError, match="stopped"):
        pool.get()
    assert len(clients) == 1 and first.is_closed
    assert pool.snapshot() == {"state": "stopped", "ready": False}
    assert pool.start() is not first  # Only an explicit new lifespan may restart.
    await pool.close()


async def test_many_concurrent_acquisitions_after_reload_share_one_new_client():
    pool, clients = pool_and_clients()
    old = pool.start()
    pool.invalidate()
    assert pool.snapshot() == {"state": "rebuild_pending", "ready": True}

    async def request():
        lease = pool.acquire()
        try:
            await asyncio.sleep(0)
            return lease.client
        finally:
            lease.release()

    results = await asyncio.gather(*(request() for _ in range(32)))
    assert len(clients) == 2 and all(client is clients[1] for client in results)
    assert clients[1] is not old
    await pool.close()
    assert all(len(client.closes) == 1 for client in clients)


async def test_worker_reload_defers_old_close_until_last_lease_on_owner_loop():
    owner_loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    pool, clients = pool_and_clients()
    old = pool.start()
    one, two = pool.acquire(), pool.acquire()
    worker = threading.Thread(target=pool.invalidate)
    worker.start()
    worker.join(timeout=3)
    assert not worker.is_alive()
    await asyncio.sleep(0)
    assert not old.is_closed
    new = pool.get()
    assert new is not old
    one.release()
    one.release()  # Duplicate cleanup is safe.
    await asyncio.sleep(0)
    assert not old.is_closed
    two.release()
    await asyncio.wait_for(old.closed.wait(), 1)
    assert old.closes == [(owner_thread, owner_loop)]
    assert not new.is_closed
    await pool.close()
    assert len(clients) == 2 and len(new.closes) == 1


async def test_idle_worker_reload_closes_on_original_loop_without_new_request():
    pool, _ = pool_and_clients()
    old = pool.start()
    owner = (threading.get_ident(), asyncio.get_running_loop())
    worker = threading.Thread(target=pool.invalidate)
    worker.start()
    worker.join(timeout=3)
    await asyncio.wait_for(old.closed.wait(), 1)
    assert old.closes == [owner]
    assert pool.snapshot()["state"] == "rebuild_pending"
    await pool.close()


async def test_constructor_failure_is_retryable_and_health_never_exposes_prose():
    failed = [False]
    clients = []

    def factory():
        if failed[0]:
            raise ValueError("socks5://fixture-user:private@proxy.invalid:1080")
        clients.append(Client())
        return clients[-1]

    pool = SharedClientPool(factory)
    old = pool.start()
    lease = pool.acquire()
    pool.invalidate()
    failed[0] = True
    with pytest.raises(ValueError):
        pool.get()
    assert pool.snapshot() == {"state": "construction_failed", "ready": False}
    assert not old.is_closed  # Failed replacement does not break the old stream.
    failed[0] = False
    new = pool.get()  # Same configuration can recover; no restart needed.
    assert new is not old
    assert pool.snapshot() == {"state": "ready", "ready": True}
    lease.release()
    await pool.close()
    assert all(len(client.closes) == 1 for client in clients)


async def test_config_change_during_construction_cannot_publish_stale_client():
    clients = []
    pool = None

    def factory():
        clients.append(Client())
        if len(clients) == 1:
            # A real foreign thread invalidates while the owner constructs.
            worker = threading.Thread(target=pool.invalidate)
            worker.start()
            worker.join(timeout=3)
            assert not worker.is_alive()
        return clients[-1]

    pool = SharedClientPool(factory)
    new = pool.start()
    assert new is clients[1] and len(clients) == 2
    await asyncio.wait_for(clients[0].closed.wait(), 1)
    assert not new.is_closed
    await pool.close()
    assert all(len(client.closes) == 1 for client in clients)


async def test_reentrant_config_callback_during_factory_is_safe():
    clients = []

    def factory():
        client = Client()
        clients.append(client)
        if len(clients) == 1:
            pool.invalidate()
        return client

    pool = SharedClientPool(factory)
    assert pool.start() is clients[1]
    await pool.close()
    assert all(len(client.closes) == 1 for client in clients)


async def test_repeated_reload_releases_every_retired_generation():
    pool, clients = pool_and_clients()
    pool.start()
    leases = []
    for _ in range(8):
        leases.append(pool.acquire())
        pool.invalidate()
    assert len(clients) == 8
    for lease in reversed(leases):
        assert not lease.client.is_closed
        lease.release()
    await pool.close()
    assert not pool._retired
    assert all(len(client.closes) == 1 for client in clients)


async def test_shutdown_waits_for_inflight_lease_and_drains_under_cancellation():
    pool, clients = pool_and_clients()
    old = pool.start()
    lease = pool.acquire()
    close = asyncio.create_task(pool.close())
    await asyncio.sleep(0)
    assert pool.snapshot()["state"] == "closing"
    with pytest.raises(RuntimeError, match="closing"):
        pool.get()
    with pytest.raises(RuntimeError, match="shutting down"):
        pool.start()
    pool.invalidate()
    close.cancel()
    await asyncio.sleep(0)
    close.cancel()
    assert not old.is_closed and not close.done()
    lease.release()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert old.is_closed and len(old.closes) == 1 and len(clients) == 1
    assert pool.snapshot()["state"] == "stopped"


async def test_get_on_foreign_loop_rejected_without_moving_client():
    pool, clients = pool_and_clients()
    old = pool.start()
    failures = []

    async def foreign():
        try:
            pool.get()
        except RuntimeError as exc:
            failures.append(str(exc))

    worker = threading.Thread(target=lambda: asyncio.run(foreign()))
    worker.start()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert failures == ["upstream client must be used on its owning event loop"]
    assert pool.get() is old and len(clients) == 1
    await pool.close()


async def test_closed_current_client_recovers_on_next_request():
    pool, clients = pool_and_clients()
    old = pool.start()
    old.is_closed = True
    assert pool.snapshot() == {"state": "closed", "ready": False}
    assert pool.get() is not old
    await pool.close()
    assert len(clients) == 2


async def test_retirement_error_is_observed_without_logging_sensitive_details(caplog):
    class BrokenClient(Client):
        async def aclose(self):
            raise ValueError("socks5://fixture-user:private@proxy.invalid:1080")

    pool = SharedClientPool(BrokenClient)
    pool.start()
    pool.invalidate()
    await pool.close()
    assert "retirement failed (ValueError)" in caplog.text
    assert "fixture-user" not in caplog.text and "private" not in caplog.text
