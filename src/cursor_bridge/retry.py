"""Retry policy copied from pi-cursor request-lifecycle, plus unavailable."""

from __future__ import annotations

import random

from .errors import CursorError, RetryHint

DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_CAP = 10

RETRY_BASE_S: dict[RetryHint, float] = {
    "blob_not_found": 0.2,
    "resource_exhausted": 2.0,
    "timeout": 1.0,
    "unavailable": 1.5,
}


def clamp_max_retries(value: int) -> int:
    return max(0, min(int(value), MAX_RETRIES_CAP))


def retry_delay_s(hint: RetryHint, *, jitter: float | None = None) -> float:
    """Base delay plus 0-50% jitter, matching pi-cursor retryDelayMs()."""

    base = RETRY_BASE_S.get(hint, 1.0)
    if jitter is None:
        factor = random.random() * 0.5
    else:
        factor = max(0.0, min(0.5, float(jitter)))
    return base * (1.0 + factor)


def should_retry(
    error: CursorError,
    *,
    attempt: int,
    max_retries: int,
    emitted_output: bool,
) -> bool:
    if emitted_output or attempt >= max_retries:
        return False
    return bool(error.retryable and error.retry_hint)
