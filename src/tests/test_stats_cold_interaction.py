"""Statistics cold-load UX and priority, using isolated state and fake TG I/O."""
from __future__ import annotations

import threading

import pytest

from src.tests.test_tg_menu_performance import (
    Recorder, _empty_period_snapshot, _import_modules, _wait_until,
)


@pytest.fixture
def runtime(m, monkeypatch):
    cache = m["menu_cache"]
    worker = cache.StatsRefreshCoordinator()
    monkeypatch.setattr(cache, "COORDINATOR", worker)
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    monkeypatch.setattr(m["stats_menu"], "_maybe_suffix_status_banner", lambda text: text)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [], "apiKeys": {}, "oauthAccounts": [],
    }))
    snapshot = _empty_period_snapshot()
    calls = []

    def load(since):
        calls.append((since, threading.get_ident()))
        return snapshot

    monkeypatch.setattr(m["log_db"], "stats_period_snapshot", load)
    yield m, worker, recorder, snapshot, calls
    worker.stop()


def _key(m, period):
    stats = m["stats_menu"]
    return stats._period_cache_key(period, stats._since_ts(period))


@pytest.mark.parametrize("period", ["0", "3", "7", "month"])
@pytest.mark.parametrize("dim", ["all", "channel", "model", "apikey"])
def test_cold_click_renders_loading_then_result_without_second_click(runtime, period, dim):
    m, worker, recorder, snapshot, calls = runtime
    handler_thread = threading.get_ident()
    m["stats_menu"].view(42, 100, "first", period, dim)
    assert calls == []  # No SQL or additional worker launched by the handler.
    loading = recorder.edits()
    assert len(loading) == 1
    assert "完成后自动更新" in loading[0]["text"]
    assert loading[0]["reply_markup"] == m["stats_menu"]._kb(period, dim)

    worker.start()
    _wait_until(lambda: len(recorder.edits()) == 2)
    expected_text, expected_kb = m["stats_menu"]._compose_snapshot(snapshot, period, dim)
    assert recorder.edits()[-1]["text"] == expected_text
    assert recorder.edits()[-1]["reply_markup"] == expected_kb
    assert len(calls) == 1
    assert calls[0][1] == worker.thread.ident != handler_thread
    assert worker.max_active_jobs == 1


@pytest.mark.parametrize("period", ["0", "month"])
def test_cold_click_subscribes_to_already_running_preheat(runtime, monkeypatch, period):
    m, worker, recorder, snapshot, calls = runtime
    cache = m["menu_cache"].PERIOD_STATS
    key = _key(m, period)
    started, release = threading.Event(), threading.Event()

    def preheat():
        calls.append("preheat")
        started.set()
        assert release.wait(2)
        return snapshot

    worker.register_periodic("preheat", 60, lambda: cache.refresh_now(key, preheat), priority=0)
    worker.start()
    try:
        assert started.wait(1)
        m["stats_menu"].view(42, 100, "first", period, "all")
        assert len(worker._queue) == 0
        assert len(cache._waiters[key]) == 1
    finally:
        release.set()
    _wait_until(lambda: len(recorder.edits()) == 2)
    assert calls == ["preheat"]
    assert "正在加载" not in recorder.edits()[-1]["text"]


def test_repeated_clicks_share_query_and_only_latest_dimension_updates(runtime):
    m, worker, recorder, snapshot, calls = runtime
    for dim in ("all", "model", "channel", "apikey"):
        m["stats_menu"].view(42, 100, "click", "3", dim)
    assert len(worker._queue) == 1
    assert len(m["menu_cache"].PERIOD_STATS._waiters[_key(m, "3")]) == 1
    worker.start()
    _wait_until(lambda: len(recorder.edits()) == 5)
    assert len(calls) == 1
    assert recorder.edits()[-1]["reply_markup"] == m["stats_menu"]._kb("3", "apikey")


def test_different_messages_share_query_but_each_gets_result(runtime):
    m, worker, recorder, snapshot, calls = runtime
    m["stats_menu"].view(42, 100, "first", "7", "all")
    m["stats_menu"].view(43, 101, "second", "7", "model")
    assert len(worker._queue) == 1
    worker.start()
    _wait_until(lambda: len(recorder.edits()) == 4)
    assert len(calls) == 1
    assert [(r["chat_id"], r["message_id"]) for r in recorder.edits()[2:]] == [(42, 100), (43, 101)]


def test_switching_period_drops_old_result(runtime):
    m, worker, recorder, snapshot, calls = runtime
    m["stats_menu"].view(42, 100, "first", "3", "all")
    m["stats_menu"].view(42, 100, "second", "7", "model")
    worker.start()
    _wait_until(lambda: len(recorder.edits()) == 3)
    assert len(calls) == 2
    assert recorder.edits()[-1]["reply_markup"] == m["stats_menu"]._kb("7", "model")


@pytest.mark.parametrize("fail", [False, True])
def test_leaving_stats_drops_late_success_or_error(runtime, monkeypatch, fail):
    m, worker, recorder, snapshot, calls = runtime
    if fail:
        def load(_since):
            raise RuntimeError("expected failure")
        monkeypatch.setattr(m["log_db"], "stats_period_snapshot", load)
    m["stats_menu"].view(42, 100, "first", "3", "all")
    # The bot invalidates the same message before dispatching every new callback.
    m["menu_cache"].begin_view(42, 100)
    m["ui"].edit(42, 100, "another menu")
    worker.start()
    _wait_until(lambda: not m["menu_cache"].PERIOD_STATS.peek(_key(m, "3")).refreshing)
    worker.stop()  # Include completion callbacks, not just the cache store.
    assert [r["text"] for r in recorder.edits()][-1] == "another menu"
    assert len(recorder.edits()) == 2


def test_navigation_during_result_compose_is_rechecked_before_edit(runtime, monkeypatch):
    m, worker, recorder, snapshot, calls = runtime
    started, release = threading.Event(), threading.Event()
    original = m["stats_menu"]._compose_snapshot

    def compose(*args):
        started.set()
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(m["stats_menu"], "_compose_snapshot", compose)
    m["stats_menu"].view(42, 100, "first", "3", "all")
    worker.start()
    try:
        assert started.wait(1)
        m["menu_cache"].begin_view(42, 100)
        m["ui"].edit(42, 100, "another menu")
    finally:
        release.set()
    worker.stop()
    assert len(recorder.edits()) == 2
    assert recorder.edits()[-1]["text"] == "another menu"


def test_cache_completed_while_loading_message_was_sent_is_not_missed(runtime, monkeypatch):
    m, worker, recorder, snapshot, calls = runtime
    key = _key(m, "month")

    def api(method, data=None):
        result = recorder(method, data)
        if method == "editMessageText" and "正在加载" in data["text"]:
            m["menu_cache"].PERIOD_STATS.store(key, snapshot)
        return result

    monkeypatch.setattr(m["ui"], "api", api)
    m["stats_menu"].view(42, 100, "first", "month", "all")
    assert len(recorder.edits()) == 2
    assert "正在加载" not in recorder.edits()[-1]["text"]
    assert len(worker._queue) == 0
    assert not m["menu_cache"].PERIOD_STATS._waiters
    assert calls == []


def test_failed_cold_load_shows_error_and_can_retry(runtime, monkeypatch):
    m, worker, recorder, snapshot, calls = runtime

    def load(since):
        calls.append(since)
        if len(calls) == 1:
            raise RuntimeError("expected <failure>")
        return snapshot

    monkeypatch.setattr(m["log_db"], "stats_period_snapshot", load)
    m["stats_menu"].view(42, 100, "first", "month", "channel")
    worker.start()
    _wait_until(lambda: len(recorder.edits()) == 2)
    assert "&lt;failure&gt;" in recorder.edits()[-1]["text"]
    assert recorder.edits()[-1]["reply_markup"] == m["stats_menu"]._kb("month", "channel")
    m["stats_menu"].view(42, 100, "retry", "month", "channel")
    _wait_until(lambda: len(recorder.edits()) == 4)
    assert len(calls) == 2
    assert "查询失败" not in recorder.edits()[-1]["text"]
    assert not m["menu_cache"].PERIOD_STATS._interactive


@pytest.mark.parametrize("period", ["0", "3", "7", "month"])
@pytest.mark.parametrize("stale", [False, True])
def test_existing_snapshots_still_render_once_without_loading(runtime, period, stale):
    m, worker, recorder, snapshot, calls = runtime
    m["menu_cache"].PERIOD_STATS.store(_key(m, period), snapshot, age_seconds=1000 if stale else 0)
    m["stats_menu"].view(42, 100, "first", period, "all")
    assert len(recorder.edits()) == 1
    assert "正在加载" not in recorder.edits()[0]["text"]
    assert not m["menu_cache"].PERIOD_STATS._waiters
    assert not m["menu_cache"].PERIOD_STATS._interactive
    worker.start()
    if stale and period in ("3", "7"):
        _wait_until(lambda: len(calls) == 1)
    worker.stop()
    assert len(recorder.edits()) == 1


def test_interactive_loads_precede_due_background_jobs_and_promote_singleflight(runtime):
    m, worker, recorder, snapshot, calls = runtime
    cache = m["menu_cache"].SWRCache(60)
    order, threads = [], []

    def run(label):
        order.append(label)
        threads.append(threading.get_ident())
        return True

    worker.register_periodic("p0", 60, lambda: run("p0"), priority=0)
    worker.register_periodic("p1", 60, lambda: run("p1"), priority=1)
    cache.request("background", lambda: run("background"))
    cache.request("promoted", lambda: run("promoted"))
    cache.request("promoted", lambda: run("duplicate"), interactive=True)
    cache.request("foreground", lambda: run("foreground"), interactive=True)
    assert len(worker._queue) == 3
    worker.start()
    _wait_until(lambda: len(order) == 5)
    assert order == ["promoted", "foreground", "p0", "p1", "background"]
    assert set(threads) == {worker.thread.ident}
    assert worker.max_active_jobs == 1
    assert not cache._interactive


def test_priority_survives_background_reservation_before_enqueue(runtime, monkeypatch):
    m, worker, recorder, snapshot, calls = runtime
    cache = m["menu_cache"].SWRCache(60)
    order = []
    cache.request("background", lambda: order.append("background") or True)
    entered, release = threading.Event(), threading.Event()
    original = worker.enqueue

    def delayed_enqueue(cache, key, loader, generation):
        entered.set()
        assert release.wait(2)
        original(cache, key, loader, generation)

    monkeypatch.setattr(worker, "enqueue", delayed_enqueue)
    submitter = threading.Thread(target=lambda: cache.request("target", lambda: order.append("target") or True))
    submitter.start()
    try:
        assert entered.wait(1)
        cache.request("target", lambda: order.append("duplicate"), interactive=True)
    finally:
        release.set()
        submitter.join(2)
    assert not submitter.is_alive()
    assert len(worker._queue) == 2
    worker.start()
    _wait_until(lambda: len(order) == 2)
    assert order == ["target", "background"]


def test_stop_cancels_pending_priority_and_subscribers(runtime):
    m, worker, recorder, snapshot, calls = runtime
    cache = m["menu_cache"].SWRCache(60)
    cache.request("pending", lambda: True, interactive=True, on_ready=lambda *_: calls.append("late"))
    worker.stop()
    assert not cache._interactive
    assert not cache._inflight
    assert not cache._waiters
    assert calls == []
