"""In-process last-run telemetry for background maintenance loops.

Loops call :func:`started` / :func:`finished` around one iteration. Only
monotonic-free wall-clock facts are kept (process-local, reset on restart):
last start/finish, last result and the delay the loop will sleep next, from
which ``next_run_at`` is derived. Recording is O(1), lock-protected and never
raises, so it cannot affect the loops it observes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _safe(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None
    return wrapped


@_safe
def started(name: str) -> None:
    with _lock:
        row = _jobs.setdefault(name, {})
        row["running"] = True
        row["last_started_at"] = time.time()


@_safe
def finished(name: str, *, ok: bool = True, error: str | None = None,
             next_delay_seconds: float | None = None) -> None:
    now = time.time()
    with _lock:
        row = _jobs.setdefault(name, {})
        row["running"] = False
        row["last_run_at"] = now
        row["last_result"] = "succeeded" if ok else "failed"
        row["last_error"] = (str(error)[:200] if error else None)
        row["next_run_at"] = now + float(next_delay_seconds) if next_delay_seconds is not None else None


@_safe
def scheduled(name: str, delay_seconds: float) -> None:
    """Record the next planned run without a completed iteration (e.g. startup delay)."""
    with _lock:
        row = _jobs.setdefault(name, {})
        row["next_run_at"] = time.time() + float(delay_seconds)


def snapshot() -> dict[str, dict[str, Any]]:
    with _lock:
        return {name: dict(row) for name, row in _jobs.items()}


def _reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
