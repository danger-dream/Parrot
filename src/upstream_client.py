"""Lifecycle of the shared HTTP client, independent of request/routing policy.

Only the owning event loop constructs/uses clients. Config callbacks may retire
one from any thread; leases keep in-flight responses alive until they close.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .async_owned import await_owned

_logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _Entry:
    client: Any
    loop: asyncio.AbstractEventLoop | None
    users: int = 0
    retired: bool = False
    closing: bool = False
    closed: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)


class ClientLease:
    def __init__(self, pool: SharedClientPool, entry: _Entry):
        self.client = entry.client
        self._pool = pool
        self._entry: _Entry | None = entry

    def release(self) -> None:
        # No await between releasing ownership and scheduling retirement; even
        # repeated cancellation cannot leak an in-flight reference.
        with self._pool._lock:
            entry, self._entry = self._entry, None
            if entry is not None:
                entry.users -= 1
                self._pool._queue_close(entry)


class SharedClientPool:
    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state = "not_started"
        self._current: _Entry | None = None
        self._retired: set[_Entry] = set()
        self._tasks: set[asyncio.Task] = set()
        self._revision = 0
        self._construction_failed = False

    def start(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._state == "closing":
                raise RuntimeError("upstream client runtime is shutting down")
            if self._state != "running":
                self._state = "running"
                self._loop = loop
                self._construction_failed = False
        return self.get()

    def install_for_tests(self, client) -> None:
        """Install a fixture before its first I/O; fixture owns final closure."""
        with self._lock:
            self._state = "running"
            self._current = _Entry(client, None)

    def _get_entry(self) -> _Entry:
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._state != "running":
                    raise RuntimeError("upstream client runtime is " + self._state)
                if self._loop is None:
                    self._loop = loop
                if loop is not self._loop:
                    raise RuntimeError("upstream client must be used on its owning event loop")
                entry = self._current
                if entry is not None and not getattr(entry.client, "is_closed", False):
                    entry.loop = loop
                    return entry
                if entry is not None:
                    self._retire_current()
                revision = self._revision
            # Do not hold the lifecycle lock through config.get()/proxy setup:
            # they can fire reentrant callbacks or take other modules' locks.
            # Creation has no await, so only this owning loop can construct;
            # another thread can only invalidate it, checked before publication.
            try:
                client = self._factory()
            except Exception:
                with self._lock:
                    if revision != self._revision and self._state == "running":
                        continue
                    self._construction_failed = True
                raise
            candidate = _Entry(client, loop)
            with self._lock:
                if self._state == "running" and revision == self._revision:
                    self._current = candidate
                    self._construction_failed = False
                    return candidate
                # Never publish a candidate built against an invalidated config.
                candidate.retired = True
                self._retired.add(candidate)
                self._queue_close(candidate)

    def get(self):
        """Observe/create the current client; production I/O must acquire a lease."""
        return self._get_entry().client

    def acquire(self) -> ClientLease:
        while True:
            entry = self._get_entry()
            with self._lock:
                if self._state == "running" and entry is self._current and not entry.retired:
                    entry.users += 1
                    return ClientLease(self, entry)

    def _retire_current(self) -> None:
        entry, self._current = self._current, None
        if entry is not None:
            entry.retired = True
            self._retired.add(entry)
            self._queue_close(entry)

    def invalidate(self) -> None:
        """Thread-safe, no network/asyncio.run and no synchronous pool closure."""
        with self._lock:
            self._revision += 1
            self._construction_failed = False
            self._retire_current()

    def _queue_close(self, entry: _Entry) -> None:
        # Caller holds _lock. Lease release and invalidation share this decision.
        if not entry.retired or entry.users or entry.closing:
            return
        loop = entry.loop or self._loop
        if loop is None:
            return  # Unused injected fixture; bind it if close() is requested.
        entry.closing = True
        try:
            loop.call_soon_threadsafe(self._spawn_close, entry)
        except RuntimeError:
            # Never run an already-used AsyncClient on a foreign event loop.
            _logger.error("upstream client owner loop closed before retirement")
            self._retired.discard(entry)
            entry.closed.set_result(None)

    def _spawn_close(self, entry: _Entry) -> None:
        task = asyncio.create_task(self._close_entry(entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _close_entry(self, entry: _Entry) -> None:
        try:
            await entry.client.aclose()
        except Exception as exc:
            # Proxy exceptions can contain credentials; log only the class.
            _logger.warning("upstream client retirement failed (%s)", type(exc).__name__)
        finally:
            with self._lock:
                self._retired.discard(entry)
                entry.closed.set_result(None)

    async def close(self) -> None:
        with self._lock:
            if self._state == "closing":
                entries = tuple(self._retired)
            else:
                self._state = "closing"
                self._revision += 1
                self._retire_current()
                entries = tuple(self._retired)
            for entry in entries:
                if entry.loop is None:
                    entry.loop = self._loop or asyncio.get_running_loop()
                self._queue_close(entry)

        async def finish():
            try:
                # In-flight scopes release normally; lifespan already drains and
                # cancels its background users before calling close().
                await asyncio.gather(*(asyncio.wrap_future(entry.closed) for entry in entries))
            finally:
                with self._lock:
                    # A second close waiter must not stop a subsequent explicit
                    # lifespan start after the first waiter already finished.
                    if self._state == "closing":
                        self._state = "stopped"

        await await_owned(finish())

    def snapshot(self) -> dict[str, Any]:
        """Local readiness only: no client construction, DNS or paid probe."""
        with self._lock:
            state = self._state
            if state == "running":
                if self._construction_failed:
                    state = "construction_failed"
                elif self._current is None:
                    state = "rebuild_pending"
                elif getattr(self._current.client, "is_closed", False):
                    state = "closed"
                else:
                    state = "ready"
            return {"state": state, "ready": state in {"ready", "rebuild_pending"}}
