"""Low-overhead monotonic timing for one Parrot process startup."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

_PROCESS_START_NS = time.monotonic_ns()
_PRINT_LOCK = threading.Lock()


def set_process_start_ns(value: int) -> None:
    """Set the process baseline before importing the rest of Parrot."""
    global _PROCESS_START_NS
    _PROCESS_START_NS = int(value)


def now_ns() -> int:
    return time.monotonic_ns()


def elapsed_ms(at_ns: int | None = None) -> float:
    end = now_ns() if at_ns is None else int(at_ns)
    return (end - _PROCESS_START_NS) / 1_000_000


def log(
    phase: str,
    *,
    started_ns: int | None = None,
    status: str = "ok",
    at_ns: int | None = None,
    **fields: Any,
) -> None:
    end = now_ns() if at_ns is None else int(at_ns)
    parts = [
        "[startup-timing]",
        f"phase={phase}",
        f"status={status}",
    ]
    if started_ns is not None:
        parts.append(f"duration_ms={(end - int(started_ns)) / 1_000_000:.3f}")
    parts.append(f"elapsed_ms={elapsed_ms(end):.3f}")
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace(" ", "_").replace("\n", "_")
        parts.append(f"{key}={text}")
    with _PRINT_LOCK:
        print(" ".join(parts), flush=True)


@contextmanager
def phase(name: str, **fields: Any) -> Iterator[None]:
    started = now_ns()
    try:
        yield
    except BaseException as exc:
        log(
            name,
            started_ns=started,
            status="error",
            error=type(exc).__name__,
            **fields,
        )
        raise
    else:
        log(name, started_ns=started, **fields)
