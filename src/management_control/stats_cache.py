"""Stale-while-revalidate cache for expensive Management statistics reads.

Statistics are computed with the unchanged ``log_db`` aggregation code; this
module only decides *when* to recompute:

* a value younger than ``fresh_seconds`` is returned as is;
* an older value whose last computation was quick (under ``slow_seconds``) is
  recomputed on the spot, so cheap live windows (today, recent outcomes) are
  never served stale and pages agree with one another;
* an older value of a slow computation (up to ``max_stale_seconds``) is
  returned immediately while one background thread recomputes it (single
  flight per key);
* without a usable value the caller computes it, and concurrent callers for
  the same key wait for that single computation instead of repeating it.

Sealed (past) months are already cached by ``log_db`` keyed on their file
signature; the live month is therefore what this short-lived cache absorbs.
At most ``max_concurrent`` computations run at once so a burst of different
statistics pages cannot saturate the SQLite/IO capacity shared with
inference logging. Computation never happens on an event-loop thread: every
caller is a threadpool worker or the cache's own daemon thread.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Hashable


@dataclass(slots=True)
class _Entry:
    value: Any
    stored_at: float
    cost: float = 0.0


class _Flight:
    __slots__ = ("done", "value", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


_POOL: ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _refresh_pool() -> ThreadPoolExecutor:
    """Long-lived workers: each thread keeps its own log-DB connection, so refreshes must not spawn new threads."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="management-stats-refresh")
        return _POOL


class StatsCache:
    def __init__(
        self,
        *,
        fresh_seconds: float = 15.0,
        max_stale_seconds: float = 600.0,
        slow_seconds: float = 1.0,
        max_entries: int = 256,
        max_concurrent: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fresh_seconds = float(fresh_seconds)
        self.max_stale_seconds = float(max_stale_seconds)
        self.slow_seconds = float(slow_seconds)
        self.max_entries = int(max_entries)
        # PARROT_STATS_CACHE=0 disables caching (operations knob; the test
        # suite sets it because tests read statistics right after writing).
        self.enabled = os.environ.get("PARROT_STATS_CACHE", "1") != "0"
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._flights: dict[Hashable, _Flight] = {}
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))

    # ── internals ───────────────────────────────────────────────────
    def _store(self, key: Hashable, value: Any, cost: float = 0.0) -> None:
        with self._lock:
            self._entries[key] = _Entry(value, self._clock(), cost)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def _run(self, key: Hashable, loader: Callable[[], Any], flight: _Flight) -> None:
        try:
            with self._slots:
                started = time.monotonic()
                value = loader()
                cost = time.monotonic() - started
            self._store(key, value, cost)
            flight.value = value
        except BaseException as exc:  # delivered to waiters; background errors keep the stale value
            flight.error = exc
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.done.set()

    def _start_background(self, key: Hashable, loader: Callable[[], Any]) -> None:
        with self._lock:
            if key in self._flights:
                return
            flight = self._flights[key] = _Flight()
        _refresh_pool().submit(self._run, key, loader, flight)

    # ── public API ──────────────────────────────────────────────────
    def get(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        """Return a deep copy of the cached or freshly computed value."""
        if not self.enabled:
            return loader()
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            age = None if entry is None else now - entry.stored_at
        if entry is not None and age is not None and age < self.fresh_seconds:
            return copy.deepcopy(entry.value)
        if (entry is not None and age is not None and age < self.max_stale_seconds
                and entry.cost >= self.slow_seconds):
            self._start_background(key, loader)
            return copy.deepcopy(entry.value)
        with self._lock:
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = self._flights[key] = _Flight()
        if owner:
            self._run(key, loader, flight)
        else:
            flight.done.wait()
        if flight.error is not None:
            raise flight.error
        return copy.deepcopy(flight.value)

    def peek_or_refresh(self, key: Hashable, loader: Callable[[], Any], *,
                        max_stale_seconds: float | None = None) -> tuple[Any, bool]:
        """Non-blocking read: ``(value, loading)``.

        Returns any usable value (fresh or stale) and schedules a background
        refresh when it is stale or missing; ``loading`` is True while no value
        exists yet (TG's 「统计初始化中」). Never computes on the caller thread.
        ``max_stale_seconds`` overrides how long a stale value stays usable.
        """
        if not self.enabled:
            return loader(), False
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            age = None if entry is None else now - entry.stored_at
        if entry is not None and age is not None and age < self.fresh_seconds:
            return copy.deepcopy(entry.value), False
        self._start_background(key, loader)
        limit = self.max_stale_seconds if max_stale_seconds is None else float(max_stale_seconds)
        if entry is not None and age is not None and age < limit:
            return copy.deepcopy(entry.value), False
        return None, True

    def invalidate(self, prefix: Hashable | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._entries.clear()
                return
            for key in list(self._entries):
                if isinstance(key, tuple) and key[:1] == (prefix,):
                    self._entries.pop(key, None)


#: Process-wide cache shared by Stats, API-key, overview and OAuth reads.
STATS_CACHE = StatsCache()


def cached(key: Hashable, loader: Callable[[], Any], *, source: Any = None) -> Any:
    """Cache only reads of the real ``log_db`` module (injected fakes bypass)."""
    if source is not None:
        from src import log_db
        if source is not log_db:
            return loader()
    return STATS_CACHE.get(key, loader)


def snapshot_key(period: str, since_ts: float) -> tuple:
    """Cache key for a stats_period_snapshot window.

    Calendar windows (today / month / lifetime) include their start so a day
    or month rollover never serves the previous window; rolling windows
    (3d / 7d) are keyed by name only so stale-while-revalidate keeps working.
    """
    fixed = period in {"today", "month", "lifetime"}
    return ("snapshot", period, float(since_ts) if fixed else None)


class CachingStatsSource:
    """Read-through proxy over a ``log_db``-like statistics source.

    Only the listed read functions are cached (keyed by name and arguments);
    a month snapshot shares :func:`snapshot_key` with StatsControl so one
    computation serves both pages. Anything else passes straight through.
    """

    _CACHED = frozenset({"stats_period_snapshot", "tokens_for_apikey", "apikey_model_stats",
                         "tokens_for_channel"})

    def __init__(self, source: Any) -> None:
        self._source = source

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._source, name)
        if name not in self._CACHED or not callable(value):
            return value

        def call(*args, **kwargs):
            if name == "stats_period_snapshot" and len(args) == 1 and not kwargs:
                key = snapshot_key("month", args[0])
            else:
                key = ("source", name, args, tuple(sorted(kwargs.items())))
            return cached(key, lambda: value(*args, **kwargs), source=self._source)
        return call
