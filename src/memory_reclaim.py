"""Prompt release of request-sized memory.

Every proxied response leaves small reference cycles behind (httpx binds each
Response to its stream, anyio keeps a cancelled task's traceback), and those
cycles still reach the complete upstream request body.  Reference counting
never frees them and CPython's full collection runs rarely in a heap this large,
so several multi-MiB requests stay resident long after they finished.  glibc
then keeps the freed heap mapped unless it is trimmed explicitly.

The server process freezes its startup objects (so a full collection only scans
request-time objects), collects once enough request payload has completed, and
trims the freed heap back to the OS.  Only ``server.py`` run as a program enables
this; importing the app (tests, tooling) keeps the interpreter defaults.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import os
import time

# glibc mallopt() parameters.
_M_MMAP_THRESHOLD = -3
_M_ARENA_MAX = -8

_MMAP_THRESHOLD_BYTES = 1024 * 1024
_ARENA_MAX = 2

# Collect under load once this much payload completed, but not more often than
# the interval; collect leftovers once traffic pauses.
_COLLECT_BYTES = 32 * 1024 * 1024
_QUIET_COLLECT_BYTES = 1024 * 1024
_MIN_INTERVAL_S = 2.0
_QUIET_S = 3.0
_TICK_S = 1.0
# Startup objects stay frozen; re-scan them occasionally (when idle) so a cycle
# that became garbage after a config reload is not kept forever.
_REFREEZE_IDLE_S = 3600.0
_REFREEZE_MAX_S = 6 * 3600.0

_enabled = False
_libc: ctypes.CDLL | None = None
_pending_bytes = 0
_last_activity = 0.0
_stats = {"collections": 0, "trims": 0, "refreezes": 0, "last_collect_ms": 0.0, "max_collect_ms": 0.0}


def configure() -> None:
    """Enable reclamation for this process and fix glibc's malloc thresholds.

    Call before worker threads start.  glibc raises its mmap threshold to the
    largest block freed so far (up to 32 MiB); afterwards multi-MiB request
    strings are carved from the heap and never returned.  Explicit MALLOC_*
    environment settings win over these defaults.
    """
    global _enabled, _libc
    if os.environ.get("PARROT_MEMORY_RECLAIM", "1") == "0":
        return
    _enabled = True
    try:
        if not (os.confstr("CS_GNU_LIBC_VERSION") or "").startswith("glibc"):
            return
    except (ValueError, OSError, AttributeError):
        return  # musl, macOS, Windows: default allocator settings
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
        libc.malloc_trim.argtypes = (ctypes.c_size_t,)
    except (OSError, AttributeError):
        return
    _libc = libc
    if "MALLOC_ARENA_MAX" not in os.environ:
        libc.mallopt(_M_ARENA_MAX, _ARENA_MAX)
    if "MALLOC_MMAP_THRESHOLD_" not in os.environ:
        libc.mallopt(_M_MMAP_THRESHOLD, _MMAP_THRESHOLD_BYTES)


def is_enabled() -> bool:
    return _enabled


def note(nbytes: int) -> None:
    """Record request/response payload that becomes garbage when it completes."""
    global _pending_bytes, _last_activity
    if nbytes > 0:
        _pending_bytes += nbytes
        _last_activity = time.monotonic()


def freeze_startup() -> None:
    gc.collect()
    gc.freeze()


def stats() -> dict:
    return {**_stats, "pending_bytes": _pending_bytes, "frozen": gc.get_freeze_count()}


def _collect(*, refreeze: bool) -> None:
    started = time.perf_counter()
    if refreeze:
        gc.unfreeze()
    gc.collect()
    if refreeze:
        gc.freeze()
        _stats["refreezes"] += 1
    elapsed = (time.perf_counter() - started) * 1000
    _stats["collections"] += 1
    _stats["last_collect_ms"] = round(elapsed, 2)
    _stats["max_collect_ms"] = max(_stats["max_collect_ms"], round(elapsed, 2))


async def reclaim_loop() -> None:
    global _pending_bytes
    from . import drain

    last_collect = last_refreeze = time.monotonic()
    while True:
        await asyncio.sleep(_TICK_S)
        now = time.monotonic()
        pending = _pending_bytes
        quiet = now - _last_activity >= _QUIET_S
        due = (
            (pending >= _COLLECT_BYTES and now - last_collect >= _MIN_INTERVAL_S)
            or (quiet and pending >= _QUIET_COLLECT_BYTES)
        )
        refreeze = (
            (quiet and drain.active_count() == 0 and now - last_refreeze >= _REFREEZE_IDLE_S)
            or now - last_refreeze >= _REFREEZE_MAX_S
        )
        if not (due or refreeze):
            continue
        _pending_bytes = 0
        last_collect = now
        if refreeze:
            last_refreeze = now
        try:
            _collect(refreeze=refreeze)
            if _libc is not None:
                # ctypes releases the GIL; the trim never blocks the event loop.
                await asyncio.to_thread(_libc.malloc_trim, 0)
                _stats["trims"] += 1
        except Exception as exc:
            print(f"[memory] reclaim failed: {type(exc).__name__}: {exc}")
