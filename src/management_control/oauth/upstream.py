"""Map provider/network failures of platform-specific OAuth actions to the
Management error contract (never an unhandled 500)."""
from __future__ import annotations

import functools
from contextlib import contextmanager

from src.management_control.errors import ManagementError, ManagementErrorCode


_TIMEOUT_KINDS = {"timeout", "connect_timeout", "read_timeout"}
_DISABLED_KINDS = {"disabled", "network_disabled"}


def provider_failure(exc: BaseException) -> ManagementError:
    """Classify one provider exception into a stable public error."""
    if isinstance(exc, ManagementError):
        return exc
    kind = str(getattr(exc, "kind", "") or "")
    phase = str(getattr(exc, "timeout_phase", "") or "")
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or kind in _TIMEOUT_KINDS or phase or "timeout" in name:
        return ManagementError(ManagementErrorCode.UPSTREAM_TIMEOUT, retryable=True)
    if kind in _DISABLED_KINDS:
        return ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True)
    return ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True)


@contextmanager
def mapped_upstream():
    try:
        yield
    except ManagementError:
        raise
    except Exception as exc:
        raise provider_failure(exc) from exc


def maps_upstream(method):
    """Decorator form of :func:`mapped_upstream` for control methods."""
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        with mapped_upstream():
            return method(*args, **kwargs)
    return wrapped


def call_upstream(function, /, *args, **kwargs):
    """Run one platform-specific action with the API error contract.

    Used by the HTTP adapter only: Telegram keeps receiving the provider's
    own exceptions, which its menus turn into provider-specific wording.
    """
    with mapped_upstream():
        return function(*args, **kwargs)
