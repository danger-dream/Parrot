"""Graceful shutdown / request drain helpers.

Parrot serves long-lived streaming responses.  A plain process restart can cut
an in-flight chunked/SSE body in half, which downstream clients surface as
"peer closed connection without sending complete message body".  This module
keeps a lightweight active-request counter and a process-wide draining flag so
SIGTERM/SIGINT can stop accepting new work, then wait for existing bodies to
finish before uvicorn is allowed to shut down.
"""

from __future__ import annotations

import asyncio
import time
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import AsyncIterator

from fastapi.responses import JSONResponse

from . import config


_DRainingReason = str | None

_lock = threading.RLock()
_update_reservation: object | None = None


class AdmissionClosed(Exception):
    """New business work is fenced for update or shutdown."""


_draining: bool = False
_shutdown_requested: bool = False
_reason: _DRainingReason = None
_started_at: float | None = None
_active_requests: int = 0
_active_work: int = 0
_last_work_at: float | None = None
_condition: asyncio.Condition | None = None
_condition_loop: asyncio.AbstractEventLoop | None = None
_deadline: float | None = None
_current_lease: ContextVar[DrainLease | None] = ContextVar("parrot_request_lease", default=None)


def is_work_label(label: str) -> bool:
    """Connection-level work: responses rounds and MCP tools are counted separately."""
    text = str(label or "").strip()
    if text.startswith("ws /v1/"):
        return text != "ws /v1/responses"
    if text.startswith("http "):
        rest = text[5:].strip()
        method, _, path = rest.partition(" ")
        method_up = method.upper()
        if path in {"/images/generations", "/images/edits", "/backend-api/codex/realtime/calls"}:
            return True
        if path.startswith("/v1/"):
            if path == "/v1/models" and method_up in {"GET", "HEAD", "OPTIONS"}:
                return False
            return True
    return False


@dataclass
class DrainLease:
    """A single active HTTP/WebSocket/request lease."""

    label: str = "request"
    closed: bool = False
    log_handles: dict = field(default_factory=dict, repr=False)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.log_handles.clear()
        await _decrement_active(self.label)


async def _condition_for_current_loop() -> asyncio.Condition:
    global _condition, _condition_loop
    loop = asyncio.get_running_loop()
    if _condition is None or _condition_loop is not loop:
        _condition = asyncio.Condition()
        _condition_loop = loop
    return _condition


def is_draining() -> bool:
    return _draining


def shutdown_requested() -> bool:
    return _shutdown_requested


def reason() -> str | None:
    return _reason


def active_count() -> int:
    return _active_requests


def active_work_count() -> int:
    with _lock:
        return _active_work


def _idle_locked(quiet_seconds: float) -> bool:
    return not (_draining or _shutdown_requested or _update_reservation is not None or _active_work) and (
        _last_work_at is None or time.monotonic() - _last_work_at >= max(0.0, float(quiet_seconds)))


def is_idle_for_update(quiet_seconds: float = 180.0) -> bool:
    with _lock:
        return _idle_locked(quiet_seconds)


def try_reserve_update(quiet_seconds: float = 180.0) -> object | None:
    """Atomically check quietness and fence new work; does not request shutdown."""
    global _update_reservation
    with _lock:
        if not _idle_locked(quiet_seconds):
            return None
        _update_reservation = object()
        return _update_reservation


def release_update(token: object) -> None:
    global _update_reservation
    with _lock:
        if token is _update_reservation:
            _update_reservation = None


@dataclass
class WorkLease:
    closed: bool = False

    def close(self) -> None:
        global _active_work, _last_work_at
        with _lock:
            if not self.closed:
                self.closed = True
                _active_work -= 1
                _last_work_at = time.monotonic()


def enter_work() -> WorkLease:
    """Reserve a round/tool before enqueueing, release after owned finalizers."""
    global _active_work, _last_work_at
    with _lock:
        if _draining or _shutdown_requested or _update_reservation is not None:
            raise AdmissionClosed()
        _active_work += 1
        _last_work_at = time.monotonic()
        return WorkLease()


def started_at() -> float | None:
    return _started_at


def status_snapshot() -> dict:
    return {
        "draining": _draining,
        "shutdown_requested": _shutdown_requested,
        "reason": _reason,
        "started_at": _started_at,
        "active_requests": _active_requests,
        "drain_timeout_seconds": shutdown_timeout_seconds(),
    }


def shutdown_timeout_seconds() -> int:
    """Configured max seconds to wait for active work during process stop.

    Default is intentionally below systemd's default TimeoutStopSec=90 so a
    normal `systemctl restart parrot.service` gets a chance to exit cleanly
    before systemd escalates to SIGKILL.
    """

    try:
        cfg = config.get()
        shutdown_cfg = cfg.get("shutdown") or {}
        value = int(shutdown_cfg.get("drainTimeoutSeconds", 80))
    except Exception:
        value = 80
    return max(0, value)


def retain_log_handle(handle) -> None:
    """to_thread copies this request context; keep string-only log callers live."""
    lease = _current_lease.get()
    if lease is not None and not lease.closed:
        lease.log_handles[handle.request_id] = handle


def release_log_handle(request_id: str) -> None:
    lease = _current_lease.get()
    if lease is not None:
        lease.log_handles.pop(request_id, None)


def remaining_seconds() -> float:
    """One monotonic admission/drain deadline, never restarted by lifespan."""
    return max(0.0, _deadline - time.monotonic()) if _deadline is not None else float(shutdown_timeout_seconds())


def expedite() -> None:
    global _deadline
    _deadline = time.monotonic()


def begin(reason_text: str = "shutdown") -> None:
    """Enter draining mode.

    This is sync so signal handlers can call it directly. The admission lock
    also serializes updates from the background updater thread.
    """

    global _draining, _shutdown_requested, _reason, _started_at, _deadline
    with _lock:
        first = not _draining
        _draining = True
        _shutdown_requested = True
        _reason = reason_text
        if _started_at is None:
            _started_at = time.time()
        if first:
            _deadline = time.monotonic() + shutdown_timeout_seconds()
            print(f"[drain] entering draining mode reason={reason_text} active={_active_requests}")


def reset_for_tests() -> None:
    """Reset global state for unit tests only."""

    global _draining, _shutdown_requested, _reason, _started_at, _active_requests, _active_work, _last_work_at, _deadline
    global _update_reservation
    _update_reservation = None
    _draining = False
    _shutdown_requested = False
    _reason = None
    _started_at = None
    _active_requests = 0
    _active_work = 0
    _last_work_at = None
    _deadline = None
    _current_lease.set(None)


async def enter(label: str = "request") -> DrainLease:
    global _active_requests, _active_work, _last_work_at
    cond = await _condition_for_current_loop()
    async with cond:
        with _lock:
            work = is_work_label(label)
            if _draining or _shutdown_requested or (work and _update_reservation is not None):
                raise AdmissionClosed()
            _active_requests += 1
            if work:
                _active_work += 1
                _last_work_at = time.monotonic()
            lease = DrainLease(label=label)
            _current_lease.set(lease)
            return lease


@asynccontextmanager
async def active(label: str = "request") -> AsyncIterator[DrainLease]:
    lease = await enter(label)
    try:
        yield lease
    finally:
        await lease.aclose()


async def _decrement_active(label: str = "request") -> None:
    global _active_requests, _active_work, _last_work_at
    cond = await _condition_for_current_loop()
    async with cond:
        with _lock:
            _active_requests = max(0, _active_requests - 1)
            if is_work_label(label):
                _active_work = max(0, _active_work - 1)
                _last_work_at = time.monotonic()
            if _draining:
                print(f"[drain] active request finished label={label} remaining={_active_requests}")
        cond.notify_all()


async def wait_for_zero(timeout: float | int | None = None) -> bool:
    """Wait until active request count reaches zero.

    Returns True if drained cleanly, False if timeout elapsed first.
    """

    cond = await _condition_for_current_loop()
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + max(0.0, float(timeout))
    async with cond:
        while _active_requests > 0:
            if deadline is None:
                await cond.wait()
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return _active_requests == 0
        return True


def reject_response() -> JSONResponse:
    """HTTP response for new requests after draining started."""

    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "server_error",
                "message": "Parrot is draining for graceful restart; please retry shortly.",
            },
            "draining": True,
            "active_requests": _active_requests,
        },
        headers={"Retry-After": "5"},
    )


def allow_path_during_drain(path: str) -> bool:
    # Keep health visible for systemd/scripts/load balancers while all model
    # traffic is rejected.
    return path == "/health"
