"""Process-local request lifecycle hook for live observers (e.g. WebUI events).

``log_db`` calls :func:`publish` right after its durable SQLite writes. The
contract on the request path is strict:

* with no listener, :func:`active` is a single tuple truthiness check and the
  caller skips building any payload, so the inference path is unchanged;
* listeners must be O(1), non-blocking and must never perform IO; any listener
  failure is swallowed here so the request path can never observe it;
* payloads are small dicts of already-known scalar facts. Anything that needs a
  database read is the listener owner's job, off the request thread.

This module is a business-layer primitive: it imports nothing from
``src.management_*`` (the management event bus subscribes to it instead).
"""

from __future__ import annotations

import threading
from typing import Any, Callable


Listener = Callable[[str, dict[str, Any]], None]

_lock = threading.Lock()
# Copy-on-write tuple: readers never lock, writers replace the whole tuple.
_listeners: tuple[Listener, ...] = ()


def active() -> bool:
    """True when at least one listener is subscribed."""
    return bool(_listeners)


def subscribe(listener: Listener) -> Callable[[], None]:
    """Register ``listener``; returns an idempotent unsubscribe callable."""
    global _listeners
    with _lock:
        _listeners = (*_listeners, listener)

    def unsubscribe() -> None:
        global _listeners
        with _lock:
            _listeners = tuple(item for item in _listeners if item is not listener)

    return unsubscribe


def publish(kind: str, payload: dict[str, Any]) -> None:
    """Deliver one event to every listener; never raises."""
    listeners = _listeners
    if not listeners:
        return
    for listener in listeners:
        try:
            listener(kind, payload)
        except Exception:
            pass


def _reset_for_tests() -> None:
    global _listeners
    with _lock:
        _listeners = ()
