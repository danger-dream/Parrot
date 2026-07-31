"""Telegram 统计快照缓存与中央刷新协调器。

所有统计 SQL 都由一个 ``tg-stats-scheduler`` 守护线程串行执行。协调器用
``time.monotonic()`` 维护周期任务的 ``next_due``，同时承接 3/7 天、模型明细
等低频按需任务；菜单线程只读取最近一次成功快照，绝不自行创建刷新线程。
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


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


class SWRCache:
    """线程安全 stale-while-revalidate 缓存。

    ``request`` 只把 loader 排入中央协调器，不创建线程。loader 异常不会覆盖
    上一次成功值。``on_ready`` 仅为低频详情兼容保留；常用菜单不注册回调。
    """

    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._values: dict[Hashable, tuple[Any, float]] = {}
        self._inflight: set[Hashable] = set()
        self._waiters: dict[
            Hashable,
            dict[Hashable, Callable[[Any, Exception | None], None]],
        ] = {}
        self._generation = 0

    def peek(self, key: Hashable) -> CacheRead:
        now = time.monotonic()
        with self._lock:
            item = self._values.get(key)
            refreshing = key in self._inflight
        if item is None:
            return CacheRead(None, False, refreshing)
        value, stored_at = item
        return CacheRead(value, now - stored_at < self.ttl_seconds, refreshing)

    def request(
        self,
        key: Hashable,
        loader: Callable[[], Any],
        *,
        subscriber: Hashable | None = None,
        on_ready: Callable[[Any, Exception | None], None] | None = None,
        force: bool = False,
    ) -> CacheRead:
        """读取快照，并把必要的刷新 single-flight 排入中央串行队列。"""
        read, generation, should_enqueue = self._reserve(
            key,
            subscriber=subscriber,
            on_ready=on_ready,
            force=force,
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
    ) -> tuple[CacheRead, int, bool]:
        now = time.monotonic()
        with self._lock:
            item = self._values.get(key)
            fresh = bool(item is not None and now - item[1] < self.ttl_seconds)
            if not force and fresh:
                return CacheRead(item[0], True, key in self._inflight), self._generation, False
            if on_ready is not None:
                waiter_key = subscriber if subscriber is not None else object()
                self._waiters.setdefault(key, {})[waiter_key] = on_ready
            should_enqueue = key not in self._inflight
            if should_enqueue:
                self._inflight.add(key)
            value = item[0] if item is not None else None
            return CacheRead(value, False, True), self._generation, should_enqueue

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
            self._inflight.discard(key)
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
            self._waiters.pop(key, None)

    def store(self, key: Hashable, value: Any, *, age_seconds: float = 0) -> None:
        """测试和已知快照注入使用。"""
        with self._lock:
            self._values[key] = (
                value,
                time.monotonic() - max(0.0, age_seconds),
            )

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._values.clear()
            self._inflight.clear()
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
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            now = time.monotonic()
            # 所有预热都立即到期，由 priority 决定业务顺序，并始终串行执行。
            for job in self._periodic:
                job.next_due = now
            self._running = True
            self._max_active_jobs = 0
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="tg-stats-scheduler",
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

    def enqueue(
        self,
        cache: SWRCache,
        key: Hashable,
        loader: Callable[[], Any],
        generation: int,
    ) -> None:
        with self._condition:
            self._queue.append(_QueuedLoad(cache, key, loader, generation))
            self._condition.notify()

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
            with self._condition:
                while self._running:
                    now = time.monotonic()
                    due = [job for job in self._periodic if job.next_due <= now]
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
                with self._condition:
                    self._active_jobs -= 1
                    if selected_job is not None:
                        delay = selected_job.interval if success else min(15.0, selected_job.interval)
                        selected_job.next_due = time.monotonic() + delay
                    self._condition.notify_all()


# 常用快照的 freshness 与主动刷新周期一致。旧值即便过期也持续可读。
PERIOD_STATS = SWRCache(60.0)
LIFETIME_STATS = SWRCache(120.0)
# 账户/渠道/Key 的按模型明细变化频率低，后台每 5 分钟统一刷新。
DETAIL_STATS = SWRCache(300.0)
# OAuth 5h/7d 本地窗口随时间滚动，单独按 60 秒维护，不能与模型明细共用 TTL。
WINDOW_STATS = SWRCache(60.0)
HISTORY_TOTALS = SWRCache(300.0)
BACKGROUND_JOBS = SWRCache(60.0)

COORDINATOR = StatsRefreshCoordinator()


def _refresh_common_periods() -> bool:
    from .. import log_db

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
            lambda start=since: log_db.stats_period_snapshot(start),
        ) and ok
    return ok


def _refresh_lifetime() -> bool:
    from .. import log_db

    return LIFETIME_STATS.refresh_now("lifetime", log_db.stats_lifetime)


def _refresh_oauth_windows() -> bool:
    """主动维护 OAuth 列表/详情原有的 5h、7d 本地 Token 与金额行。"""
    from .menus import oauth_menu

    return oauth_menu.refresh_window_snapshots_now()


def _refresh_apikey_history() -> bool:
    from .. import log_db

    return HISTORY_TOTALS.refresh_now(
        "apikey-history",
        log_db.request_totals_by_apikey,
    )


def _queue_model_detail_snapshots() -> bool:
    """把所有现有账户、渠道和 Key 的按模型明细排入本调度器。

    这里只发现任务并入队，不执行 SQL、不创建线程。真正的查询仍由同一个
    ``tg-stats-scheduler`` 在后续循环中逐个串行执行。
    """
    from .. import config, log_db, oauth_manager
    from ..oauth_ids import account_key as oauth_account_key

    since = month_start_ts()
    period = PERIOD_STATS.peek(("period", int(since))).value
    if period is None:
        return False

    by_channel = period.get("by_channel") or {}
    by_apikey = period.get("by_apikey") or {}

    def _has_calls(stats: Any) -> bool:
        return isinstance(stats, dict) and int(stats.get("total") or 0) > 0

    # OAuth 账户与 API 渠道都使用 channel_model_stats；无本月调用时直接存空
    # 结果，保持旧页面“完整但无统计行”的语义，同时避免无意义查询。
    channel_keys: list[str] = []
    for account in oauth_manager.list_accounts():
        key = oauth_account_key(account)
        if key:
            channel_keys.append(f"oauth:{key}")
    for channel in config.get().get("channels") or []:
        if not isinstance(channel, dict):
            continue
        name = str(channel.get("name") or "").strip()
        if name:
            channel_keys.append(f"api:{name}")

    seen_channels: set[str] = set()
    for channel_key in channel_keys:
        if channel_key in seen_channels:
            continue
        seen_channels.add(channel_key)
        key = ("channel-model", channel_key, int(since))
        # OAuth 菜单沿用既有 oauth-model key；渠道菜单沿用 channel-model key。
        if channel_key.startswith("oauth:"):
            account_key = channel_key[len("oauth:"):]
            key = ("oauth-model", account_key, int(since))
        if not _has_calls(by_channel.get(channel_key)):
            DETAIL_STATS.store(key, [])
            continue
        DETAIL_STATS.request(
            key,
            lambda target=channel_key, start=since: log_db.channel_model_stats(
                target, since_ts=start,
            ),
        )

    api_keys = config.get().get("apiKeys") or {}
    if isinstance(api_keys, dict):
        for name in api_keys:
            key = ("apikey-model", name, int(since))
            if not _has_calls(by_apikey.get(name)):
                DETAIL_STATS.store(key, [])
                continue
            DETAIL_STATS.request(
                key,
                lambda target=name, start=since: log_db.apikey_model_stats(
                    target, since_ts=start,
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


def is_current_view(chat_id: int, message_id: int, token: int) -> bool:
    with _view_lock:
        return _view_tokens.get((int(chat_id), int(message_id))) == token


def run_if_current(
    chat_id: int,
    message_id: int,
    token: int,
    callback: Callable[[], Any],
) -> bool:
    """与 begin_view 串行执行一次消息更新，供非统计后台交互避免竞态。"""
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
