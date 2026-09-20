"""Telegram 统计快照缓存与中央刷新协调器。

所有统计 SQL 都由一个 ``tg-stats-scheduler`` 守护线程串行执行。协调器用
``time.monotonic()`` 维护周期任务的 ``next_due``，同时承接 3/7 天、模型明细
等低频按需任务；菜单线程只读取最近一次成功快照，绝不自行创建刷新线程。
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import config, startup_timing
from ..management_control.observability import DEFAULT_STATS_CONTROL, telegram_context


_STATS_CONTROL = DEFAULT_STATS_CONTROL
_CONTEXT = telegram_context()


_BJT = timezone(timedelta(hours=8))
_INITIALIZING_TEXT = "统计正在初始化，请稍后再试"


def today_start_ts() -> float:
    """返回与统计菜单既有算法一致的北京时间今日零点。"""
    return datetime.now(_BJT).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()


def month_start_ts() -> float:
    """返回与各列表菜单既有算法一致的北京时间本月一日零点。"""
    return datetime.now(_BJT).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()


@dataclass(frozen=True)
class CacheRead:
    value: Any
    fresh: bool
    refreshing: bool
    error: Exception | None = None
    restored: bool = False


class SWRCache:
    """线程安全 stale-while-revalidate 缓存。

    ``request`` 只把 loader 排入中央协调器，不创建线程。loader 异常不会覆盖
    上一次成功值。``on_ready`` 用于冷缓存完成后更新当前页面；有旧值的常用
    菜单仍只渲染一次。``interactive`` 只提升已有任务的优先级，不重复查询。
    """

    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._values: dict[Hashable, tuple[Any, float]] = {}
        self._errors: dict[Hashable, Exception] = {}
        self._inflight: set[Hashable] = set()
        self._interactive: set[Hashable] = set()
        self._restored: set[Hashable] = set()
        self._waiters: dict[
            Hashable,
            dict[Hashable, Callable[[Any, Exception | None], None]],
        ] = {}
        self._generation = 0

    def peek(self, key: Hashable) -> CacheRead:
        now = time.monotonic()
        with self._lock:
            item = self._values.get(key)
            error = self._errors.get(key)
            refreshing = key in self._inflight
            restored = key in self._restored
        if item is None:
            return CacheRead(None, False, refreshing, error)
        value, stored_at = item
        return CacheRead(
            value,
            now - stored_at < self.ttl_seconds,
            refreshing,
            error,
            restored,
        )

    def request(
        self,
        key: Hashable,
        loader: Callable[[], Any],
        *,
        subscriber: Hashable | None = None,
        on_ready: Callable[[Any, Exception | None], None] | None = None,
        force: bool = False,
        interactive: bool = False,
    ) -> CacheRead:
        """读取快照，并把必要的刷新 single-flight 排入中央串行队列。"""
        read, generation, should_enqueue = self._reserve(
            key,
            subscriber=subscriber,
            on_ready=on_ready,
            force=force,
            interactive=interactive,
        )
        if should_enqueue:
            COORDINATOR.enqueue(self, key, loader, generation)
        return read

    def _reserve(
        self,
        key: Hashable,
        *,
        subscriber: Hashable | None = None,
        on_ready: Callable[[Any, Exception | None], None] | None = None,
        force: bool = False,
        interactive: bool = False,
    ) -> tuple[CacheRead, int, bool]:
        now = time.monotonic()
        with self._lock:
            item = self._values.get(key)
            error = self._errors.get(key)
            fresh = bool(item is not None and now - item[1] < self.ttl_seconds)
            if not force and fresh:
                return CacheRead(
                    item[0], True, key in self._inflight, error,
                    key in self._restored,
                ), self._generation, False
            if interactive:
                self._interactive.add(key)
            if on_ready is not None:
                waiter_key = subscriber if subscriber is not None else object()
                self._waiters.setdefault(key, {})[waiter_key] = on_ready
            should_enqueue = key not in self._inflight
            if should_enqueue:
                self._inflight.add(key)
            value = item[0] if item is not None else None
            return CacheRead(
                value, False, True, error, key in self._restored,
            ), self._generation, should_enqueue

    def _is_interactive(self, key: Hashable, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and key in self._interactive

    def refresh_now(self, key: Hashable, loader: Callable[[], Any]) -> bool:
        """仅供协调器周期任务使用；在当前调度线程同步刷新一个 key。"""
        _read, generation, should_run = self._reserve(key, force=True)
        if not should_run:
            return True
        return self._execute_reserved(key, loader, generation)

    def _execute_reserved(
        self,
        key: Hashable,
        loader: Callable[[], Any],
        generation: int,
    ) -> bool:
        value = None
        error: Exception | None = None
        try:
            value = loader()
        except Exception as exc:  # 失败时保留 stale
            error = exc

        with self._lock:
            if generation != self._generation:
                return error is None
            if error is None:
                self._values[key] = (value, time.monotonic())
                self._errors.pop(key, None)
                self._restored.discard(key)
            else:
                self._errors[key] = error
            self._inflight.discard(key)
            self._interactive.discard(key)
            waiters = list(self._waiters.pop(key, {}).values())

        for callback in waiters:
            try:
                callback(value, error)
            except Exception as exc:
                print(f"[tg-stats-scheduler] completion callback failed: {exc}")
        if error is not None:
            print(f"[tg-stats-scheduler] refresh failed for {key!r}: {error}")
        return error is None

    def _cancel_reserved(self, key: Hashable, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._inflight.discard(key)
            self._interactive.discard(key)
            self._waiters.pop(key, None)

    def store(self, key: Hashable, value: Any, *, age_seconds: float = 0) -> None:
        """测试和已知快照注入使用。"""
        with self._lock:
            self._values[key] = (
                value,
                time.monotonic() - max(0.0, age_seconds),
            )
            self._errors.pop(key, None)
            self._restored.discard(key)

    def snapshot_entries(self) -> list[tuple[Hashable, Any]]:
        with self._lock:
            return [(key, value) for key, (value, _stored_at) in self._values.items()]

    def restore_entries(self, entries: list[tuple[Hashable, Any]]) -> int:
        restored = 0
        stale_at = time.monotonic() - self.ttl_seconds - 1.0
        with self._lock:
            for key, value in entries:
                self._values[key] = (value, stale_at)
                self._errors.pop(key, None)
                self._restored.add(key)
                restored += 1
        return restored

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._values.clear()
            self._errors.clear()
            self._inflight.clear()
            self._interactive.clear()
            self._restored.clear()
            self._waiters.clear()


@dataclass
class _PeriodicJob:
    name: str
    interval: float
    priority: int
    task: Callable[[], bool]
    next_due: float = 0.0


@dataclass(frozen=True)
class _QueuedLoad:
    cache: SWRCache
    key: Hashable
    loader: Callable[[], Any]
    generation: int
    enqueued_ns: int
    startup_detail: bool = False


class StatsRefreshCoordinator:
    """一个 timed wait 循环，串行执行周期刷新与低频按需统计。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._periodic: list[_PeriodicJob] = []
        self._queue: deque[_QueuedLoad] = deque()
        self._thread: threading.Thread | None = None
        self._running = False
        self._active_jobs = 0
        self._max_active_jobs = 0
        self._startup_started_ns = 0
        self._startup_pending: set[str] = set()
        self._startup_detail_pending = 0
        self._startup_detail_failures = 0
        self._startup_collect_details = False
        self._startup_core_logged = False
        self._startup_full_logged = False

    def register_periodic(
        self,
        name: str,
        interval: float,
        task: Callable[[], bool],
        *,
        priority: int,
    ) -> None:
        with self._condition:
            if self._running:
                raise RuntimeError("cannot register periodic job while coordinator is running")
            self._periodic.append(_PeriodicJob(name, float(interval), priority, task))

    def start(self) -> None:
        _load_stats_snapshot_once()
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            now = time.monotonic()
            startup_started_ns = startup_timing.now_ns()
            # 所有预热都立即到期，由 priority 决定业务顺序，并始终串行执行。
            for job in self._periodic:
                job.next_due = now
            self._running = True
            self._max_active_jobs = 0
            self._startup_started_ns = startup_started_ns
            self._startup_pending = {job.name for job in self._periodic}
            self._startup_detail_pending = 0
            self._startup_detail_failures = 0
            self._startup_collect_details = False
            self._startup_core_logged = False
            self._startup_full_logged = False
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="tg-stats-scheduler",
            )
            startup_timing.log(
                "stats.preheat-start",
                at_ns=startup_started_ns,
                jobs=len(self._periodic),
            )
            self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 当前 SQL 无法安全强杀；等待它结束后，循环会在下一次检查时退出。
            thread.join()
        with self._condition:
            pending = list(self._queue)
            self._queue.clear()
            self._thread = None
        for item in pending:
            item.cache._cancel_reserved(item.key, item.generation)
        _flush_stats_snapshot()

    def enqueue(
        self,
        cache: SWRCache,
        key: Hashable,
        loader: Callable[[], Any],
        generation: int,
    ) -> None:
        enqueued_ns = startup_timing.now_ns()
        with self._condition:
            startup_detail = bool(
                self._startup_collect_details
                and threading.current_thread() is self._thread
                and not self._startup_full_logged
            )
            if startup_detail:
                self._startup_detail_pending += 1
            self._queue.append(_QueuedLoad(
                cache, key, loader, generation, enqueued_ns, startup_detail,
            ))
            self._condition.notify()

    def _maybe_log_startup_ready_locked(self) -> None:
        if (
            not self._startup_core_logged
            and not ({"period-today-month", "lifetime"} & self._startup_pending)
        ):
            self._startup_core_logged = True
            startup_timing.log(
                "stats.core-ready",
                started_ns=self._startup_started_ns,
            )
        if (
            not self._startup_full_logged
            and not self._startup_pending
            and self._startup_detail_pending == 0
        ):
            self._startup_full_logged = True
            startup_timing.log(
                "stats.full-warm",
                started_ns=self._startup_started_ns,
                status="ok" if self._startup_detail_failures == 0 else "partial",
                failed_details=self._startup_detail_failures,
            )

    @property
    def thread(self) -> threading.Thread | None:
        with self._condition:
            return self._thread

    @property
    def max_active_jobs(self) -> int:
        with self._condition:
            return self._max_active_jobs

    @property
    def running(self) -> bool:
        with self._condition:
            return self._running

    def _run(self) -> None:
        while True:
            selected_job: _PeriodicJob | None = None
            selected_load: _QueuedLoad | None = None
            startup_job_name: str | None = None
            startup_detail = False
            with self._condition:
                while self._running:
                    now = time.monotonic()
                    due = [job for job in self._periodic if job.next_due <= now]
                    # 用户冷缓存请求优先于下一项后台工作，仍不抢占运行中的 SQL。
                    # 标记随 cache reservation 保存，后台已排队甚至尚未 enqueue
                    # 的同键任务都能提升优先级，且不会破坏 single-flight。
                    selected_load = next((
                        item for item in self._queue
                        if item.cache._is_interactive(item.key, item.generation)
                    ), None)
                    if selected_load is not None:
                        self._queue.remove(selected_load)
                        break
                    if due:
                        selected_job = min(due, key=lambda job: (job.priority, job.next_due))
                        # 防止任务执行期间被再次视为到期。
                        selected_job.next_due = float("inf")
                        break
                    if self._queue:
                        selected_load = self._queue.popleft()
                        break
                    next_due = min(
                        (job.next_due for job in self._periodic),
                        default=float("inf"),
                    )
                    timeout = None if next_due == float("inf") else max(0.0, next_due - now)
                    self._condition.wait(timeout=timeout)
                if not self._running:
                    return
                self._active_jobs += 1
                self._max_active_jobs = max(self._max_active_jobs, self._active_jobs)
                if selected_job is not None and selected_job.name in self._startup_pending:
                    startup_job_name = selected_job.name
                    self._startup_collect_details = startup_job_name == "model-details"
                if selected_load is not None:
                    startup_detail = selected_load.startup_detail

            task_started_ns = startup_timing.now_ns()
            if startup_job_name is not None:
                startup_timing.log(
                    "stats.job-start",
                    at_ns=task_started_ns,
                    job=startup_job_name,
                    queue_wait_ms=f"{(task_started_ns - self._startup_started_ns) / 1_000_000:.3f}",
                )
            elif startup_detail and selected_load is not None:
                key_kind = selected_load.key[0] if isinstance(selected_load.key, tuple) else "detail"
                startup_timing.log(
                    "stats.detail-start",
                    at_ns=task_started_ns,
                    kind=key_kind,
                    queue_wait_ms=f"{(task_started_ns - selected_load.enqueued_ns) / 1_000_000:.3f}",
                )

            success = True
            try:
                if selected_job is not None:
                    success = bool(selected_job.task())
                elif selected_load is not None:
                    success = selected_load.cache._execute_reserved(
                        selected_load.key,
                        selected_load.loader,
                        selected_load.generation,
                    )
            except Exception as exc:
                success = False
                label = selected_job.name if selected_job is not None else "queued-load"
                print(f"[tg-stats-scheduler] job {label!r} failed: {exc}")
            finally:
                if startup_job_name is not None:
                    startup_timing.log(
                        "stats.job-end",
                        started_ns=task_started_ns,
                        status="ok" if success else "failed",
                        job=startup_job_name,
                    )
                elif startup_detail and selected_load is not None:
                    key_kind = selected_load.key[0] if isinstance(selected_load.key, tuple) else "detail"
                    startup_timing.log(
                        "stats.detail-end",
                        started_ns=task_started_ns,
                        status="ok" if success else "failed",
                        kind=key_kind,
                    )
                with self._condition:
                    self._startup_collect_details = False
                    self._active_jobs -= 1
                    if selected_job is not None:
                        delay = selected_job.interval if success else min(15.0, selected_job.interval)
                        selected_job.next_due = time.monotonic() + delay
                    if startup_job_name is not None and success:
                        self._startup_pending.discard(startup_job_name)
                    if startup_detail:
                        self._startup_detail_pending = max(0, self._startup_detail_pending - 1)
                        if not success:
                            self._startup_detail_failures += 1
                    self._maybe_log_startup_ready_locked()
                    self._condition.notify_all()
                if success:
                    _schedule_stats_snapshot_persist()


# 常用快照的 freshness 与主动刷新周期一致。旧值即便过期也持续可读。
PERIOD_STATS = SWRCache(60.0)
LIFETIME_STATS = SWRCache(120.0)
# 账户/渠道/Key 的按模型明细变化频率低，后台每 5 分钟统一刷新。
DETAIL_STATS = SWRCache(300.0)
# OAuth quota 窗口及账户套餐/账单周期累计随时间变化，单独按 60 秒维护，
# 不能与模型明细共用 TTL。
WINDOW_STATS = SWRCache(60.0)
HISTORY_TOTALS = SWRCache(300.0)
BACKGROUND_JOBS = SWRCache(60.0)

_STATS_SNAPSHOT_SCHEMA = "parrot-telegram-stats-cache"
_STATS_SNAPSHOT_VERSION = 1
_STATS_SNAPSHOT_MAX_AGE_SECONDS = 24 * 60 * 60
_STATS_SNAPSHOT_FILENAME = "telegram-stats-cache-v1.json"
_STATS_SNAPSHOT_WRITE_DELAY_SECONDS = 1.0
_stats_snapshot_lock = threading.Lock()
_stats_snapshot_timer: threading.Timer | None = None
_stats_snapshot_dirty = False
_stats_snapshot_loaded = False


def _stats_caches() -> dict[str, SWRCache]:
    return {
        "period": PERIOD_STATS,
        "lifetime": LIFETIME_STATS,
        "window": WINDOW_STATS,
        "history": HISTORY_TOTALS,
    }


def _stats_snapshot_path() -> str:
    return os.path.join(config.DATA_DIR, _STATS_SNAPSHOT_FILENAME)


def _encode_snapshot_key(value: Hashable) -> dict[str, Any]:
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode_snapshot_key(item) for item in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return {"type": "scalar", "value": value}
    raise TypeError(f"unsupported stats cache key type: {type(value).__name__}")


def _decode_snapshot_key(value: Any) -> Hashable:
    if not isinstance(value, dict):
        raise ValueError("stats cache key must be an object")
    kind = value.get("type")
    if kind == "tuple":
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("stats tuple key items must be a list")
        return tuple(_decode_snapshot_key(item) for item in items)
    if kind == "scalar":
        scalar = value.get("value")
        if scalar is None or isinstance(scalar, (str, int, float, bool)):
            return scalar
    raise ValueError("unsupported stats cache key")


def _stats_snapshot_payload() -> dict[str, Any]:
    caches: dict[str, list[dict[str, Any]]] = {}
    for name, cache in _stats_caches().items():
        rows: list[dict[str, Any]] = []
        for key, value in cache.snapshot_entries():
            try:
                encoded_key = _encode_snapshot_key(key)
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                continue
            rows.append({"key": encoded_key, "value": value})
        caches[name] = rows
    return {
        "schema": _STATS_SNAPSHOT_SCHEMA,
        "version": _STATS_SNAPSHOT_VERSION,
        "written_at_ms": int(time.time() * 1000),
        "caches": caches,
    }


def _persist_stats_snapshot() -> None:
    global _stats_snapshot_dirty, _stats_snapshot_timer
    with _stats_snapshot_lock:
        _stats_snapshot_timer = None
        if not _stats_snapshot_dirty:
            return
        path = _stats_snapshot_path()
        parent = os.path.dirname(os.path.abspath(path)) or "."
        temp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            payload = _stats_snapshot_payload()
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temp, path)
            try:
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except Exception as exc:
            try:
                os.unlink(temp)
            except OSError:
                pass
            print(f"[tg-stats-cache] persist failed: {type(exc).__name__}")
            return
        _stats_snapshot_dirty = False


def _schedule_stats_snapshot_persist() -> None:
    global _stats_snapshot_dirty, _stats_snapshot_timer
    with _stats_snapshot_lock:
        _stats_snapshot_dirty = True
        previous = _stats_snapshot_timer
        if previous is not None:
            previous.cancel()
        timer = threading.Timer(
            _STATS_SNAPSHOT_WRITE_DELAY_SECONDS,
            _persist_stats_snapshot,
        )
        timer.daemon = True
        timer.name = "tg-stats-cache-save"
        _stats_snapshot_timer = timer
        timer.start()


def _flush_stats_snapshot() -> None:
    global _stats_snapshot_timer
    with _stats_snapshot_lock:
        timer = _stats_snapshot_timer
        _stats_snapshot_timer = None
    if timer is not None:
        timer.cancel()
    _persist_stats_snapshot()


def _load_stats_snapshot_once() -> None:
    global _stats_snapshot_loaded
    started_ns = startup_timing.now_ns()
    with _stats_snapshot_lock:
        if _stats_snapshot_loaded:
            return
        _stats_snapshot_loaded = True
    path = _stats_snapshot_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("snapshot root must be an object")
        if (
            payload.get("schema") != _STATS_SNAPSHOT_SCHEMA
            or payload.get("version") != _STATS_SNAPSHOT_VERSION
        ):
            raise ValueError("snapshot schema/version mismatch")
        written_at_ms = int(payload.get("written_at_ms") or 0)
        age_ms = int(time.time() * 1000) - written_at_ms
        if age_ms < -300_000 or age_ms > _STATS_SNAPSHOT_MAX_AGE_SECONDS * 1000:
            startup_timing.log(
                "stats.snapshot-restore",
                started_ns=started_ns,
                status="expired",
                age_ms=age_ms,
            )
            return
        cache_payloads = payload.get("caches")
        if not isinstance(cache_payloads, dict):
            raise ValueError("snapshot caches must be an object")
        restored = 0
        for name, cache in _stats_caches().items():
            raw_entries = cache_payloads.get(name) or []
            if not isinstance(raw_entries, list):
                continue
            entries: list[tuple[Hashable, Any]] = []
            for row in raw_entries:
                if not isinstance(row, dict) or "key" not in row or "value" not in row:
                    continue
                try:
                    key = _decode_snapshot_key(row["key"])
                    hash(key)
                except (TypeError, ValueError):
                    continue
                entries.append((key, row["value"]))
            restored += cache.restore_entries(entries)
    except FileNotFoundError:
        startup_timing.log(
            "stats.snapshot-restore", started_ns=started_ns, status="missing",
        )
        return
    except Exception as exc:
        startup_timing.log(
            "stats.snapshot-restore",
            started_ns=started_ns,
            status="invalid",
            error=type(exc).__name__,
        )
        print(f"[tg-stats-cache] restore ignored: {type(exc).__name__}")
        return
    startup_timing.log(
        "stats.snapshot-restore",
        started_ns=started_ns,
        status="restored",
        entries=restored,
        age_ms=max(0, age_ms),
    )


def _reset_stats_snapshot_for_tests() -> None:
    global _stats_snapshot_dirty, _stats_snapshot_loaded, _stats_snapshot_timer
    with _stats_snapshot_lock:
        timer = _stats_snapshot_timer
        _stats_snapshot_timer = None
        _stats_snapshot_dirty = False
        _stats_snapshot_loaded = False
    if timer is not None:
        timer.cancel()
    try:
        os.unlink(_stats_snapshot_path())
    except FileNotFoundError:
        pass


COORDINATOR = StatsRefreshCoordinator()


def _refresh_common_periods() -> bool:
    ok = True
    seen: set[int] = set()
    # 默认“今日”优先；月初两者边界相同，只查询一次。
    for since in (today_start_ts(), month_start_ts()):
        if not COORDINATOR.running:
            break
        since_int = int(since)
        if since_int in seen:
            continue
        seen.add(since_int)
        ok = PERIOD_STATS.refresh_now(
            ("period", since_int),
            lambda start=since: _STATS_CONTROL.period_snapshot_since(_CONTEXT, start),
        ) and ok
    return ok


def request_period_snapshot(
    since: float,
    *,
    subscriber: Hashable | None = None,
    on_ready: Callable[[Any, Exception | None], None] | None = None,
    interactive: bool = False,
) -> CacheRead:
    """读取或排队一个精确起点的周期统计，不阻塞菜单线程。"""
    start = float(since)
    return PERIOD_STATS.request(
        ("period", int(start)),
        lambda: _STATS_CONTROL.period_snapshot_since(_CONTEXT, start),
        subscriber=subscriber,
        on_ready=on_ready,
        interactive=interactive,
    )


def request_apikey_history(
    *,
    subscriber: Hashable | None = None,
    on_ready: Callable[[Any, Exception | None], None] | None = None,
    interactive: bool = False,
) -> CacheRead:
    """读取或排队 API Key 历史调用量，不阻塞菜单线程。"""
    return HISTORY_TOTALS.request(
        "apikey-history",
        lambda: _STATS_CONTROL.request_totals_by_apikey(_CONTEXT),
        subscriber=subscriber,
        on_ready=on_ready,
        interactive=interactive,
    )


def _load_lifetime() -> dict[str, Any]:
    return _STATS_CONTROL.lifetime_snapshot(_CONTEXT)


def request_lifetime(
    *,
    subscriber: Hashable | None = None,
    on_ready: Callable[[Any, Exception | None], None] | None = None,
) -> CacheRead:
    """按需提升累计统计预热优先级；调用线程绝不执行统计查询。"""
    return LIFETIME_STATS.request(
        "lifetime",
        _load_lifetime,
        subscriber=subscriber,
        on_ready=on_ready,
        interactive=True,
    )


def _refresh_lifetime() -> bool:
    return LIFETIME_STATS.refresh_now("lifetime", _load_lifetime)


def _refresh_oauth_windows() -> bool:
    """主动维护 OAuth quota 窗口与账户周期 Token/金额快照。"""
    from .menus import oauth_menu

    return oauth_menu.refresh_window_snapshots_now()


def _refresh_apikey_history() -> bool:
    return HISTORY_TOTALS.refresh_now(
        "apikey-history",
        lambda: _STATS_CONTROL.request_totals_by_apikey(_CONTEXT),
    )


def _queue_model_detail_snapshots() -> bool:
    """把所有现有账户、渠道和 Key 的按模型明细排入本调度器。

    这里只发现任务并入队，不执行 SQL、不创建线程。真正的查询仍由同一个
    ``tg-stats-scheduler`` 在后续循环中逐个串行执行。
    """
    from ..oauth_ids import account_key as oauth_account_key
    from .menus import oauth_menu

    since = month_start_ts()
    period = PERIOD_STATS.peek(("period", int(since))).value
    if period is None:
        return False

    by_channel = period.get("by_channel") or {}
    by_apikey = period.get("by_apikey") or {}

    def _has_calls(stats: Any) -> bool:
        return isinstance(stats, dict) and int(stats.get("total") or 0) > 0

    # OAuth 详情必须按各账号自己的 provider/billing 周期预热；不能继续
    # 沿用自然月 key，否则首次进入详情会显示另一统计口径或等待二次点击。
    for account in _STATS_CONTROL.oauth_accounts(_CONTEXT):
        account_key = oauth_account_key(account)
        if not account_key:
            continue
        row = _STATS_CONTROL.quota_row(_CONTEXT, account_key)
        local_period = oauth_menu._oauth_local_period(account, row=row)
        account_since = float(local_period["since"])
        key = ("oauth-model", account_key, int(account_since))
        account_stats = oauth_menu._account_period_stats(
            account_key, local_period, month_snapshot=period,
        )
        if account_stats is None and local_period.get("stats_window"):
            # The higher-priority OAuth-window job will populate the aggregate;
            # do not cache a false empty model result before that happens.
            continue
        if not _has_calls(account_stats):
            DETAIL_STATS.store(key, [])
            continue
        cached_models = DETAIL_STATS.peek(key).value
        DETAIL_STATS.request(
            key,
            lambda target=account_key, start=account_since: _STATS_CONTROL.channel_model_stats(
                _CONTEXT, f"oauth:{target}", since_ts=start,
            ),
            # 总体已有调用时，空模型列表只能是冷启动竞态留下的无效快照。
            force=isinstance(cached_models, list) and not cached_models,
        )

    # Ordinary API channels remain natural-month reports.
    channel_keys = _STATS_CONTROL.configured_channel_keys(_CONTEXT)

    seen_channels: set[str] = set()
    for channel_key in channel_keys:
        if channel_key in seen_channels:
            continue
        seen_channels.add(channel_key)
        key = ("channel-model", channel_key, int(since))
        if not _has_calls(by_channel.get(channel_key)):
            DETAIL_STATS.store(key, [])
            continue
        DETAIL_STATS.request(
            key,
            lambda target=channel_key, start=since: _STATS_CONTROL.channel_model_stats(
                _CONTEXT, target, since_ts=start,
            ),
        )

    for name in _STATS_CONTROL.configured_api_key_names(_CONTEXT):
        key = ("apikey-model", name, int(since))
        if not _has_calls(by_apikey.get(name)):
            DETAIL_STATS.store(key, [])
            continue
        DETAIL_STATS.request(
            key,
            lambda target=name, start=since: _STATS_CONTROL.apikey_model_stats(
                _CONTEXT, target, since_ts=start,
            ),
        )
    return True


COORDINATOR.register_periodic(
    "period-today-month", 60.0, _refresh_common_periods, priority=0,
)
COORDINATOR.register_periodic(
    "oauth-windows", 60.0, _refresh_oauth_windows, priority=1,
)
COORDINATOR.register_periodic(
    "lifetime", 120.0, _refresh_lifetime, priority=2,
)
COORDINATOR.register_periodic(
    "apikey-history", 300.0, _refresh_apikey_history, priority=3,
)
COORDINATOR.register_periodic(
    "model-details", 300.0, _queue_model_detail_snapshots, priority=4,
)


def start() -> None:
    COORDINATOR.start()


def stop() -> None:
    COORDINATOR.stop()


def initialization_text() -> str:
    return _INITIALIZING_TEXT


def with_refreshing_notice(text: str, *reads: CacheRead) -> str:
    restored = [read for read in reads if read.restored]
    if not restored:
        return text
    if any(read.error is not None for read in restored):
        return text + "\n\n⚠️ <i>统计更新失败，当前仍显示上次成功快照。</i>"
    return text + "\n\n⏳ <i>数据来自上次成功快照，正在更新…</i>"


_view_lock = threading.Lock()
_view_counter = itertools.count(1)
_view_tokens: dict[tuple[int, int], int] = {}
_message_locks: dict[tuple[int, int], threading.RLock] = {}


def _message_lock(chat_id: int, message_id: int) -> threading.RLock:
    key = (int(chat_id), int(message_id))
    with _view_lock:
        return _message_locks.setdefault(key, threading.RLock())


def begin_view(chat_id: int, message_id: int) -> int:
    token = next(_view_counter)
    key = (int(chat_id), int(message_id))
    lock = _message_lock(*key)
    with lock, _view_lock:
        _view_tokens[key] = token
    return token


def current_view_token(chat_id: int, message_id: int) -> int | None:
    """返回消息当前页面令牌；供同一次管理回调复用，避免重复 begin_view。"""
    with _view_lock:
        return _view_tokens.get((int(chat_id), int(message_id)))


def is_current_view(chat_id: int, message_id: int, token: int) -> bool:
    with _view_lock:
        return _view_tokens.get((int(chat_id), int(message_id))) == token


def run_if_current(
    chat_id: int,
    message_id: int,
    token: int,
    callback: Callable[[], Any],
) -> bool:
    """与 begin_view 串行执行一次消息更新，防止后台结果覆盖新页面。"""
    key = (int(chat_id), int(message_id))
    lock = _message_lock(*key)
    with lock:
        with _view_lock:
            if _view_tokens.get(key) != token:
                return False
        callback()
        return True


def subscriber(chat_id: int, message_id: int, token: int) -> tuple[int, int, int]:
    return int(chat_id), int(message_id), int(token)


def reset_for_tests() -> None:
    """仅供测试隔离进程级调度器与缓存。"""
    stop()
    _reset_stats_snapshot_for_tests()
    for cache in (
        PERIOD_STATS,
        LIFETIME_STATS,
        DETAIL_STATS,
        WINDOW_STATS,
        HISTORY_TOTALS,
        BACKGROUND_JOBS,
    ):
        cache.clear()
    with _view_lock:
        _view_tokens.clear()
        _message_locks.clear()
