"""Pure finalization policy for protocol runtime attempts.

This module decides which side effects a caller should perform at the end of an
attempt and offers small dependency-injected executors for health effects.  It
intentionally does not import application singletons, write DB rows, touch
affinity, or close transports.  Callers keep the ordering and I/O details.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Literal

from ..sqlite_errors import is_availability_error
from .runtime import should_cooldown, should_record_failure


TerminalKind = Literal["success", "error", "client_cancelled"]
FailurePolicy = Literal["runtime", "post_commit_stream", "cooldown_only"]
_logger = logging.getLogger(__name__)
_warning_lock = threading.Lock()
_last_warning_at: dict[str, float] = {}
_WARNING_INTERVAL_SECONDS = 60.0


def _warn_availability_failure(name: str, exc: sqlite3.Error) -> None:
    now = time.monotonic()
    with _warning_lock:
        last = _last_warning_at.get(name, 0.0)
        if last and now - last < _WARNING_INTERVAL_SECONDS:
            return
        _last_warning_at[name] = now
    _logger.warning("finalize health effect %s failed; response preserved: %s", name, exc)


def _run_health_effect(name: str, effect) -> None:
    """Keep SQLite health persistence failures off the primary response path."""
    try:
        effect()
    except sqlite3.Error as exc:
        if not is_availability_error(exc):
            raise
        _warn_availability_failure(name, exc)


@dataclass(frozen=True)
class FinalizePlan:
    terminal: TerminalKind
    log_success: bool = False
    log_error: bool = False
    record_success: bool = False
    record_failure: bool = False
    clear_cooldown: bool = False
    record_cooldown_error: bool = False
    write_affinity: bool = False
    cache_reasoning_replay: bool = False
    clear_reasoning_replay: bool = False


def success_plan(
    *,
    write_affinity: bool = True,
    cache_reasoning_replay: bool = False,
) -> FinalizePlan:
    return FinalizePlan(
        terminal="success",
        log_success=True,
        record_success=True,
        clear_cooldown=True,
        write_affinity=write_affinity,
        cache_reasoning_replay=cache_reasoning_replay,
    )


def error_plan(
    outcome: str,
    *,
    failure_policy: FailurePolicy = "runtime",
    clear_reasoning_replay: bool = False,
) -> FinalizePlan:
    if failure_policy == "post_commit_stream":
        # A committed partial response remains an error, but request faults and
        # connection lifecycle ends are not evidence of unhealthy credentials.
        record_failure = outcome not in {
            "request_invalid", "client_disconnected", "connection_lifecycle",
        }
    elif failure_policy == "cooldown_only":
        record_failure = should_cooldown(outcome)
    else:
        record_failure = should_record_failure(outcome)

    return FinalizePlan(
        terminal="error",
        log_error=True,
        record_failure=record_failure,
        record_cooldown_error=should_cooldown(outcome),
        clear_reasoning_replay=clear_reasoning_replay,
    )


def client_cancelled_plan(
    *,
    saw_stream_end: bool = False,
    saw_stream_error: bool = False,
) -> FinalizePlan:
    """Resolve a downstream cancel observed from a committed stream.

    A cancel after the upstream terminal success/error has been observed should
    inherit that terminal state.  Only an in-flight client disconnect is a true
    client cancellation and must not punish the channel.
    """
    if saw_stream_error:
        return error_plan("stream_upstream_error", failure_policy="post_commit_stream")
    if saw_stream_end:
        return success_plan()
    return FinalizePlan(terminal="client_cancelled", log_error=True)


def apply_success_health_effects(
    plan: FinalizePlan,
    *,
    scorer,
    cooldown,
    channel_key: str,
    model: str,
    connect_ms: int | None = None,
    first_byte_ms: int | None = None,
    total_ms: int | None = None,
) -> None:
    """Apply success scorer/cooldown effects for a precomputed plan."""
    if plan.record_success:
        _run_health_effect(
            "record_success",
            lambda: scorer.record_success(
                channel_key,
                model,
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                total_ms=total_ms,
            ),
        )
    if plan.clear_cooldown:
        _run_health_effect("clear_cooldown", lambda: cooldown.clear(channel_key, model))


def apply_error_health_effects(
    plan: FinalizePlan,
    *,
    scorer,
    cooldown,
    channel_key: str,
    model: str,
    error_detail: str | None = None,
    connect_ms: int | None = None,
    cooldown_until: int | None = None,
) -> None:
    """Apply error scorer/cooldown effects for a precomputed plan."""
    if plan.record_cooldown_error:
        _run_health_effect(
            "record_cooldown_error",
            lambda: (
                cooldown.record_error(
                    channel_key, model, error_detail, cooldown_until=cooldown_until,
                )
                if cooldown_until is not None
                else cooldown.record_error(channel_key, model, error_detail)
            ),
        )
    if plan.record_failure:
        _run_health_effect(
            "record_failure",
            lambda: scorer.record_failure(channel_key, model, connect_ms=connect_ms),
        )
