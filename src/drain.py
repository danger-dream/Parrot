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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi.responses import JSONResponse

from . import config


_DRainingReason = str | None

_draining: bool = False
_shutdown_requested: bool = False
_reason: _DRainingReason = None
_started_at: float | None = None
_active_requests: int = 0
_condition: asyncio.Condition | None = None
_condition_loop: asyncio.AbstractEventLoop | None = None


@dataclass
class DrainLease:
    """A single active HTTP/WebSocket/request lease."""

    label: str = "request"
    closed: bool = False

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
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


def begin(reason_text: str = "shutdown") -> None:
    """Enter draining mode.

    This is sync on purpose so signal handlers can call it directly.  Active
    counter mutations happen on the asyncio loop; the assignment here is atomic
    enough for Parrot's single-process/single-event-loop runtime.
    """

    global _draining, _shutdown_requested, _reason, _started_at
    first = not _draining
    _draining = True
    _shutdown_requested = True
    _reason = reason_text
    if _started_at is None:
        _started_at = time.time()
    if first:
        print(f"[drain] entering draining mode reason={reason_text} active={_active_requests}")


def reset_for_tests() -> None:
    """Reset global state for unit tests only."""

    global _draining, _shutdown_requested, _reason, _started_at, _active_requests
    _draining = False
    _shutdown_requested = False
    _reason = None
    _started_at = None
    _active_requests = 0


async def enter(label: str = "request") -> DrainLease:
    global _active_requests
    cond = await _condition_for_current_loop()
    async with cond:
        _active_requests += 1
        return DrainLease(label=label)


@asynccontextmanager
async def active(label: str = "request") -> AsyncIterator[DrainLease]:
    lease = await enter(label)
    try:
        yield lease
    finally:
        await lease.aclose()


async def _decrement_active(label: str = "request") -> None:
    global _active_requests
    cond = await _condition_for_current_loop()
    async with cond:
        _active_requests = max(0, _active_requests - 1)
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
