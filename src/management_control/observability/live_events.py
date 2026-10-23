"""In-process live event bus behind the Management ``/events`` WebSocket.

Request-path producers (``log_db`` through :mod:`src.request_events`) only hand
over a few already-known scalars. This bus re-posts them onto its own event
loop with ``loop.call_soon_threadsafe`` (O(1), non-blocking) and performs any
database enrichment in its own tasks via ``asyncio.to_thread``. When nobody is
connected, the bus is not subscribed to the producer hook at all, so the hook
is a single truthiness check on the request path.

Each subscriber owns one bounded queue of already-encoded messages. A full
queue is never allowed to block or grow: its queued events are discarded and a
single ``resync`` message tells the client to reload its views.

The bus is transport-neutral: the adapter supplies ``encode(type, data)``
which turns event data into a wire message once per event (not per client).
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from src import request_events as request_events_module
from src.management_auth import AuthMethod, Capability, ManagementPrincipal
from src.management_control.context import ManagementContext


Encoder = Callable[[str, dict[str, Any]], str]


async def _offload(function, /, *args, **kwargs):
    """Run enrichment reads on anyio's worker threads, not on the asyncio
    default executor that the inference path uses for request logging."""
    import functools

    import anyio.to_thread
    return await anyio.to_thread.run_sync(functools.partial(function, *args, **kwargs))

_CLOSE = object()


def _reader_context() -> ManagementContext:
    """Read-only internal actor for enrichment reads (never audited)."""
    return ManagementContext(
        request_id="live-events",
        actor=ManagementPrincipal.with_capabilities(
            subject_id="live-events",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            capabilities=(Capability.READ,),
            issued_at=datetime.now(timezone.utc),
        ),
    )


def _utc(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class LiveEventSubscription:
    """One client's bounded queue; consumed by exactly one sender task."""

    def __init__(self, bus: "LiveEventBus", maxsize: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.closed = False
        self.dropped = 0

    def offer(self, message: str) -> None:
        """Loop-thread only. Never blocks; overflow collapses into ``resync``."""
        if self.closed:
            return
        try:
            self._queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        while True:
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                break
        self.dropped += 1
        resync = self._bus.encode_safe("resync", {"reason": "overflow"})
        if resync is not None:
            self._queue.put_nowait(resync)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(_CLOSE)

    async def get(self) -> str | None:
        """Next encoded message, or None once the bus/subscription closed."""
        item = await self._queue.get()
        if item is _CLOSE:
            return None
        return item


class LiveEventBus:
    def __init__(
        self,
        *,
        logs,
        status,
        encode: Encoder,
        source=request_events_module,
        max_clients: int = 20,
        queue_size: int = 256,
        snapshot_interval: float = 2.0,
        ping_interval: float = 20.0,
        finished_delay: float = 0.05,
    ) -> None:
        self.logs = logs
        self.status = status
        self._encode = encode
        self._source = source
        self.max_clients = max_clients
        self.queue_size = queue_size
        self.snapshot_interval = snapshot_interval
        self.ping_interval = ping_interval
        self.finished_delay = finished_delay
        self._context = _reader_context()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: list[LiveEventSubscription] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._ticker: asyncio.Task | None = None
        self._flusher: asyncio.Task | None = None
        self._pending_finished: dict[str, str | None] = {}
        self._closed = False

    # ── wire encoding ────────────────────────────────────────────────
    def encode_safe(self, event_type: str, data: dict[str, Any]) -> str | None:
        try:
            return self._encode(event_type, data)
        except Exception:
            return None

    # ── subscriber lifecycle (event-loop thread) ─────────────────────
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def connect(self) -> LiveEventSubscription | None:
        """Register a client; None when closed or at capacity."""
        loop = asyncio.get_running_loop()
        if self._closed or len(self._subscribers) >= self.max_clients:
            return None
        if self._loop is not loop:
            # A different loop (e.g. a new test client) cannot reuse tasks.
            self._reset_loop_state()
            self._loop = loop
        subscription = LiveEventSubscription(self, self.queue_size)
        self._subscribers.append(subscription)
        with self._lock:
            if self._unsubscribe is None:
                self._unsubscribe = self._source.subscribe(self._on_source_event)
        if self._ticker is None or self._ticker.done():
            self._ticker = loop.create_task(self._tick())
        return subscription

    def disconnect(self, subscription: LiveEventSubscription) -> None:
        subscription.close()
        try:
            self._subscribers.remove(subscription)
        except ValueError:
            pass
        if not self._subscribers:
            self._reset_loop_state()

    def _reset_loop_state(self) -> None:
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        for task in (self._ticker, self._flusher):
            if task is not None and not task.done():
                task.cancel()
        self._ticker = None
        self._flusher = None
        self._pending_finished.clear()
        for subscription in self._subscribers:
            subscription.close()
        self._subscribers = []

    def close(self) -> None:
        """Thread-safe shutdown: unhook the producer and end every client."""
        self._closed = True
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._reset_loop_state()
        else:
            try:
                loop.call_soon_threadsafe(self._reset_loop_state)
            except RuntimeError:
                pass

    # ── producer side (any thread; must stay O(1) and never raise) ───
    def _on_source_event(self, kind: str, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or self._closed:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, kind, payload)
        except RuntimeError:
            # Loop already closed; the producer must never observe it.
            pass

    # ── loop side ────────────────────────────────────────────────────
    def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        message = self.encode_safe(event_type, data)
        if message is None:
            return
        for subscription in list(self._subscribers):
            subscription.offer(message)

    def _dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        if kind == "started":
            self.broadcast("request.started", {
                "id": str(payload.get("id") or ""),
                "createdAt": _utc(payload.get("createdAt")),
                "apiKeyName": payload.get("apiKeyName"),
                "requestedModel": payload.get("requestedModel"),
                "protocol": payload.get("protocol"),
                "isStream": bool(payload.get("isStream")),
                "clientIp": payload.get("clientIp"),
                "reasoningEffort": payload.get("reasoningEffort"),
                "fastMode": payload.get("fastMode"),
            })
        elif kind == "updated":
            data = {"id": str(payload.get("id") or "")}
            for key in ("channelId", "model", "retryCount", "proxyName"):
                if payload.get(key) is not None:
                    data[key] = payload[key]
            self.broadcast("request.updated", data)
        elif kind == "finished":
            request_id = str(payload.get("id") or "")
            if not request_id:
                return
            self._pending_finished[request_id] = payload.get("month")
            if self._flusher is None or self._flusher.done():
                loop = self._loop
                if loop is not None:
                    self._flusher = loop.create_task(self._flush_finished())

    async def _flush_finished(self) -> None:
        while self._pending_finished and self._subscribers:
            if self.finished_delay > 0:
                # A short window batches bursts into one read per month.
                await asyncio.sleep(self.finished_delay)
            pending, self._pending_finished = self._pending_finished, {}
            by_month: dict[str | None, list[str]] = {}
            for request_id, month in pending.items():
                by_month.setdefault(month, []).append(request_id)
            for month, ids in by_month.items():
                try:
                    records = await _offload(
                        self.logs.list_records, self._context, ids, month=month,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Enrichment failed: tell clients to re-read instead of
                    # silently missing a finished request.
                    self.broadcast("resync", {"reason": "enrichmentFailed"})
                    continue
                for record in records:
                    self.broadcast("request.finished", {"log": record})

    async def _tick(self) -> None:
        elapsed_since_ping = 0.0
        while self._subscribers:
            await asyncio.sleep(self.snapshot_interval)
            if not self._subscribers:
                return
            try:
                snapshot = await _offload(
                    self.status.api_concurrency_snapshot, self._context,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                snapshot = None
            if snapshot is not None:
                self.broadcast("runtime.snapshot", snapshot)
            elapsed_since_ping += self.snapshot_interval
            if elapsed_since_ping >= self.ping_interval:
                elapsed_since_ping = 0.0
                self.broadcast("ping", {})
