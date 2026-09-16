from __future__ import annotations

import json
import os as _os
import sys as _sys
import threading
import time

import pytest

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    from src import config, log_db, oauth_manager
    from src.telegram import bot, menu_cache, ui
    from src.telegram.menus import (
        apikey_menu, channel_menu, main, oauth_menu, stats_menu,
    )
    return {
        "config": config,
        "log_db": log_db,
        "oauth_manager": oauth_manager,
        "bot": bot,
        "menu_cache": menu_cache,
        "ui": ui,
        "apikey_menu": apikey_menu,
        "channel_menu": channel_menu,
        "main": main,
        "oauth_menu": oauth_menu,
        "stats_menu": stats_menu,
    }


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, method, data=None):
        with self.lock:
            self.calls.append((method, dict(data or {})))
        return {"ok": True, "result": {"message_id": 9001}}

    def edits(self) -> list[dict]:
        with self.lock:
            return [data for method, data in self.calls if method == "editMessageText"]


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def _empty_period_snapshot() -> dict:
    overall = {
        "total": 0, "success_count": 0, "error_count": 0, "pending_count": 0,
        "total_retries": 0, "retried_requests": 0, "affinity_hits": 0,
        "success_with_cache_hit": 0, "success_with_cache_write": 0,
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_creation": 0, "total_cache_read": 0,
        "avg_connect_ms": None, "avg_first_token_ms": None,
        "avg_total_ms": None, "avg_tps": None, "max_tps": None, "min_tps": None,
        "cost_ticks": 0, "actual_cost_ticks": 0, "estimated_cost_ticks": 0,
        "actual_costed_success": 0, "estimated_costed_success": 0,
        "costed_success": 0, "unpriced_success": 0,
    }
    summary = {
        "overall": overall, "by_channel": [], "by_model": [], "by_apikey": [],
        "recent_errors": [], "recent_calls": [], "recent_cache_misses": [],
    }
    return {
        "since_ts": 0.0,
        "summary": summary,
        "families": {},
        "model_channels": {},
        "by_channel": {},
        "by_apikey": {},
    }


def _lifetime_snapshot(total: int = 1) -> dict:
    return {
        "total": total,
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_creation": 0,
        "cache_read": 0,
        "cost_ticks": 0,
        "costed_success": 0,
    }


def _patch_fast_common_loaders(m, monkeypatch, *, calls=None) -> None:
    def period(since):
        if calls is not None:
            calls.append(("period", threading.get_ident(), since))
        return _empty_period_snapshot()

    def lifetime():
        if calls is not None:
            calls.append(("lifetime", threading.get_ident(), None))
        return _lifetime_snapshot()

    def history():
        if calls is not None:
            calls.append(("history", threading.get_ident(), None))
        return {}

    monkeypatch.setattr(m["log_db"], "stats_period_snapshot", period)
    monkeypatch.setattr(m["log_db"], "stats_lifetime", lifetime)
    monkeypatch.setattr(m["log_db"], "request_totals_by_apikey", history)


def _wait_common_preheated(menu_cache) -> None:
    _wait_until(lambda: all((
        menu_cache.PERIOD_STATS.peek(
            ("period", int(menu_cache.today_start_ts()))
        ).value is not None,
        menu_cache.PERIOD_STATS.peek(
            ("period", int(menu_cache.month_start_ts()))
        ).value is not None,
        menu_cache.LIFETIME_STATS.peek("lifetime").value is not None,
        menu_cache.HISTORY_TOTALS.peek("apikey-history").value is not None,
    )))


def test_central_scheduler_is_single_thread_serial_and_preheats(m, monkeypatch):
    menu_cache = m["menu_cache"]
    calls: list[tuple[str, int, object]] = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    def enter(kind, value):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            calls.append((kind, threading.get_ident(), value))
        time.sleep(0.01)
        with lock:
            active -= 1

    def period(since):
        enter("period", since)
        return _empty_period_snapshot()

    def lifetime():
        enter("lifetime", None)
        return _lifetime_snapshot()

    def history():
        enter("history", None)
        return {}

    monkeypatch.setattr(m["log_db"], "stats_period_snapshot", period)
    monkeypatch.setattr(m["log_db"], "stats_lifetime", lifetime)
    monkeypatch.setattr(m["log_db"], "request_totals_by_apikey", history)

    menu_cache.start()
    first_thread = menu_cache.COORDINATOR.thread
    menu_cache.start()  # 生命周期重复 start 不能创建第二条循环。
    assert menu_cache.COORDINATOR.thread is first_thread
    _wait_common_preheated(menu_cache)

    scheduler_threads = [
        thread for thread in threading.enumerate()
        if thread.name == "tg-stats-scheduler"
    ]
    assert scheduler_threads == [first_thread]
    assert max_active == 1
    assert menu_cache.COORDINATOR.max_active_jobs == 1
    assert len({thread_id for _kind, thread_id, _value in calls}) == 1
    period_jobs = len({
        int(menu_cache.today_start_ts()), int(menu_cache.month_start_ts()),
    })
    assert [kind for kind, _thread_id, _value in calls] == [
        *(["period"] * period_jobs), "lifetime", "history",
    ]

    menu_cache.stop()
    assert first_thread is not None and not first_thread.is_alive()


def test_scheduler_preheats_all_stats_that_old_menus_display(m, monkeypatch):
    """生产调度必须填好窗口/模型快照，不能靠测试手工塞值掩盖删行。"""
    menu_cache = m["menu_cache"]
    account = {"provider": "openai", "email": "user@example.test"}
    account_key = "openai:user@example.test"
    oauth_channel = f"oauth:{account_key}"
    api_channel = "api:channel-a"
    api_key = "key-a"
    calls: list[tuple[str, int]] = []

    def period(_since):
        snapshot = _empty_period_snapshot()
        snapshot["by_channel"] = {
            oauth_channel: {"total": 1},
            api_channel: {"total": 1},
        }
        snapshot["by_apikey"] = {api_key: {"total": 1}}
        return snapshot

    monkeypatch.setattr(m["log_db"], "stats_period_snapshot", period)
    monkeypatch.setattr(m["log_db"], "stats_lifetime", _lifetime_snapshot)
    monkeypatch.setattr(m["log_db"], "request_totals_by_apikey", lambda: {api_key: 1})
    monkeypatch.setattr(m["oauth_manager"], "list_accounts", lambda: [account])
    monkeypatch.setattr(
        m["oauth_menu"], "_oauth_window_specs",
        lambda _accounts: [(('oauth-window', account_key, '5h'), account_key, 123.0)],
    )
    monkeypatch.setattr(
        m["config"], "get",
        lambda: {
            "channels": [{"name": "channel-a"}],
            "apiKeys": {api_key: {"key": "test-key"}},
        },
    )

    def tokens_for_channel(target, since_ts):
        calls.append((f"window:{target}:{since_ts}", threading.get_ident()))
        return {"total": 1, "input": 10, "output": 2, "cache_creation": 0,
                "cache_read": 4, "cost_ticks": 10, "costed_success": 1}

    def channel_models(target, since_ts):
        calls.append((f"channel-model:{target}", threading.get_ident()))
        return [{"final_model": "m", "total": 1}]

    def apikey_models(target, since_ts):
        calls.append((f"apikey-model:{target}", threading.get_ident()))
        return [{"final_model": "m", "total": 1}]

    monkeypatch.setattr(m["log_db"], "tokens_for_channel", tokens_for_channel)
    monkeypatch.setattr(m["log_db"], "channel_model_stats", channel_models)
    monkeypatch.setattr(m["log_db"], "apikey_model_stats", apikey_models)

    menu_cache.start()
    _wait_until(lambda: all((
        menu_cache.WINDOW_STATS.peek(("oauth-window", account_key, "5h")).value is not None,
        menu_cache.DETAIL_STATS.peek(("oauth-model", account_key, int(menu_cache.month_start_ts()))).value is not None,
        menu_cache.DETAIL_STATS.peek(("channel-model", api_channel, int(menu_cache.month_start_ts()))).value is not None,
        menu_cache.DETAIL_STATS.peek(("apikey-model", api_key, int(menu_cache.month_start_ts()))).value is not None,
    )))

    scheduler_id = menu_cache.COORDINATOR.thread.ident
    assert scheduler_id is not None
    assert calls
    assert {thread_id for _label, thread_id in calls} == {scheduler_id}
    assert any(label.startswith("window:") for label, _thread_id in calls)
    assert any(label == f"channel-model:{oauth_channel}" for label, _thread_id in calls)
    assert any(label == f"channel-model:{api_channel}" for label, _thread_id in calls)
    assert any(label == f"apikey-model:{api_key}" for label, _thread_id in calls)


def test_queue_singleflight_stale_and_failed_refresh_preserves_success(m, monkeypatch):
    from src.telegram.menu_cache import SWRCache

    menu_cache = m["menu_cache"]
    _patch_fast_common_loaders(m, monkeypatch)
    menu_cache.start()
    _wait_common_preheated(menu_cache)

    cache = SWRCache(0.04)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(2)
        return {"value": calls}

    first = cache.request("same", loader)
    second = cache.request("same", loader)
    assert first.value is None and first.refreshing
    assert second.value is None and second.refreshing
    assert started.wait(1)
    assert calls == 1
    release.set()
    _wait_until(lambda: cache.peek("same").value == {"value": 1})

    time.sleep(0.05)
    failed = cache.request(
        "same",
        lambda: (_ for _ in ()).throw(RuntimeError("expected refresh failure")),
    )
    assert failed.value == {"value": 1} and not failed.fresh
    _wait_until(lambda: not cache.peek("same").refreshing)
    assert cache.peek("same").value == {"value": 1}
    assert menu_cache.COORDINATOR.max_active_jobs == 1


def _store_common_snapshots(menu_cache, *, stale: bool) -> None:
    age = 1_000.0 if stale else 0.0
    period = _empty_period_snapshot()
    menu_cache.PERIOD_STATS.store(
        ("period", int(menu_cache.today_start_ts())), period, age_seconds=age,
    )
    menu_cache.PERIOD_STATS.store(
        ("period", int(menu_cache.month_start_ts())), period, age_seconds=age,
    )
    menu_cache.LIFETIME_STATS.store(
        "lifetime", _lifetime_snapshot(), age_seconds=age,
    )
    menu_cache.HISTORY_TOTALS.store(
        "apikey-history", {}, age_seconds=age,
    )


def test_five_common_callbacks_render_stale_once_without_refresh_redraw(m, monkeypatch):
    menu_cache = m["menu_cache"]
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [], "apiKeys": {}, "oauthAccounts": [],
    }))
    _store_common_snapshots(menu_cache, stale=True)
    loader_calls: list[tuple[str, int, object]] = []
    _patch_fast_common_loaders(m, monkeypatch, calls=loader_calls)

    m["main"].handle_back(42, 100, "cb-main")
    m["stats_menu"].view(42, 101, "cb-stats", "0", "all")
    m["channel_menu"].show(42, 102, "cb-channel")
    m["oauth_menu"].show(42, 103, "cb-oauth")
    m["apikey_menu"].show(42, 104, "cb-apikey")

    assert len(recorder.edits()) == 5
    assert loader_calls == []  # 点击本身不启动常用重查询。
    assert all(
        "正在加载" not in item["text"] and "自动更新" not in item["text"]
        for item in recorder.edits()
    )

    # 即使主动调度随后刷新成功，也不会注册当前页面的二次重绘。
    menu_cache.start()
    _wait_common_preheated(menu_cache)
    time.sleep(0.05)
    assert len(recorder.edits()) == 5


def test_main_navigation_is_operable_on_cold_cache_without_sync_stats_and_keeps_permissions(
    m, monkeypatch,
):
    from src.telegram import states

    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["ui"].configure("fake-main-cold-token", [42])
    m["config"].update(lambda cfg: cfg.update({
        "channels": [{"name": "cold", "enabled": True}],
        "apiKeys": {"key": {"key": "fake"}},
        "oauthAccounts": [],
        "concurrency": {"enabled": False},
    }))
    loader_calls = []
    monkeypatch.setattr(
        m["log_db"], "stats_lifetime",
        lambda: loader_calls.append(threading.get_ident()) or _lifetime_snapshot(9),
    )

    # /start never waits for statistics and keeps every established destination.
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/start"})
    # /menu renders the complete navigation plus an honest loading row.  Merely
    # handling the command cannot execute the lifetime query on the polling thread.
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/menu"})
    sends = [data for method, data in recorder.calls if method == "sendMessage"]
    assert "欢迎使用" in sends[0]["text"]
    assert "统计正在初始化，菜单功能仍可使用" in sends[1]["text"]
    assert "总调用 <code>0</code>" not in sends[1]["text"]
    assert sends[1]["reply_markup"] == m["main"]._kb()
    assert loader_calls == []

    # A menu:main button from a pre-restart page is just normal navigation; a
    # pre-existing text state neither blocks it nor gets silently destroyed.
    states.set_state(42, "old-page-input", {"preserve": True})
    m["bot"]._handle_callback({
        "id": "cb-old-main",
        "from": {"id": 42},
        "message": {"message_id": 777, "chat": {"id": 42}},
        "data": "menu:main",
    })
    edit = recorder.edits()[-1]
    assert edit["message_id"] == 777
    assert "统计正在初始化，菜单功能仍可使用" in edit["text"]
    assert edit["reply_markup"] == m["main"]._kb()
    assert states.get_state(42)["action"] == "old-page-input"
    assert loader_calls == []
    answer = [
        data for method, data in recorder.calls
        if method == "answerCallbackQuery" and data["callback_query_id"] == "cb-old-main"
    ]
    assert answer == [{"callback_query_id": "cb-old-main"}]

    edit_count = len(recorder.edits())
    m["bot"]._handle_callback({
        "id": "cb-denied-main",
        "from": {"id": 43},
        "message": {"message_id": 778, "chat": {"id": 43}},
        "data": "menu:main",
    })
    assert len(recorder.edits()) == edit_count
    assert recorder.calls[-1] == (
        "answerCallbackQuery",
        {"callback_query_id": "cb-denied-main", "text": "⛔ 无权限"},
    )


def test_main_cold_command_returns_before_slow_stats_and_auto_restores_snapshot(m, monkeypatch):
    menu_cache = m["menu_cache"]
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [{"name": "slow", "enabled": True}],
        "apiKeys": {}, "oauthAccounts": [], "concurrency": {"enabled": False},
    }))
    # Isolate this test to the interactive lifetime load; other periodic jobs are
    # independently covered above and must not obscure whether main itself blocks.
    monkeypatch.setattr(menu_cache.COORDINATOR, "_periodic", [])
    entered = threading.Event()
    release = threading.Event()
    loader_threads = []

    def slow_lifetime():
        loader_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return _lifetime_snapshot(73)

    monkeypatch.setattr(m["log_db"], "stats_lifetime", slow_lifetime)
    caller_thread = threading.get_ident()
    m["main"].show(42)
    sends = [data for method, data in recorder.calls if method == "sendMessage"]
    assert len(sends) == 1
    assert "统计正在初始化，菜单功能仍可使用" in sends[0]["text"]
    assert sends[0]["reply_markup"] == m["main"]._kb()
    assert recorder.edits() == []
    assert not entered.is_set()

    menu_cache.start()
    assert entered.wait(1)
    assert recorder.edits() == []
    release.set()
    _wait_until(lambda: len(recorder.edits()) == 1)
    assert recorder.edits()[-1]["message_id"] == 9001
    assert "总调用 <code>73</code> 次" in recorder.edits()[-1]["text"]
    assert "统计正在初始化" not in recorder.edits()[-1]["text"]
    assert loader_threads and loader_threads[0] != caller_thread
    menu_cache.stop()


def test_main_slow_stats_result_does_not_overwrite_a_newer_page(m, monkeypatch):
    menu_cache = m["menu_cache"]
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [{"name": "stale-view", "enabled": True}],
        "apiKeys": {}, "oauthAccounts": [], "concurrency": {"enabled": False},
    }))
    monkeypatch.setattr(menu_cache.COORDINATOR, "_periodic", [])
    entered = threading.Event()
    release = threading.Event()

    def slow_lifetime():
        entered.set()
        assert release.wait(2)
        return _lifetime_snapshot(81)

    monkeypatch.setattr(m["log_db"], "stats_lifetime", slow_lifetime)
    m["main"].handle_back(42, 102, "cb-main-old-view")
    menu_cache.start()
    assert entered.wait(1)
    assert len(recorder.edits()) == 1

    # Any later callback claims the message generation before rendering its page.
    # The old lifetime completion may fill the cache but must not repaint it.
    menu_cache.begin_view(42, 102)
    release.set()
    _wait_until(lambda: menu_cache.LIFETIME_STATS.peek("lifetime").value is not None)
    time.sleep(0.03)
    assert len(recorder.edits()) == 1
    menu_cache.stop()


def test_main_failed_initial_stats_stays_operable_then_retry_restores_snapshot(m, monkeypatch):
    menu_cache = m["menu_cache"]
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [{"name": "retry", "enabled": True}],
        "apiKeys": {}, "oauthAccounts": [], "concurrency": {"enabled": False},
    }))
    monkeypatch.setattr(menu_cache.COORDINATOR, "_periodic", [])
    attempts = 0

    def flaky_lifetime():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture lifetime unavailable")
        return _lifetime_snapshot(91)

    monkeypatch.setattr(m["log_db"], "stats_lifetime", flaky_lifetime)
    m["main"].handle_back(42, 101, "cb-main-fail")
    menu_cache.start()
    _wait_until(lambda: any("统计暂不可用" in edit["text"] for edit in recorder.edits()))
    failed = recorder.edits()[-1]
    assert failed["reply_markup"] == m["main"]._kb()
    assert "总调用 <code>0</code>" not in failed["text"]
    assert menu_cache.LIFETIME_STATS.peek("lifetime").error is not None

    # Retrying navigation remains immediate, queues one background attempt, and
    # replaces the unavailable row with the original statistics once successful.
    m["main"].handle_back(42, 101, "cb-main-retry")
    _wait_until(lambda: menu_cache.LIFETIME_STATS.peek("lifetime").value is not None)
    _wait_until(lambda: "总调用 <code>91</code> 次" in recorder.edits()[-1]["text"])
    assert attempts == 2
    assert recorder.edits()[-1]["reply_markup"] == m["main"]._kb()
    menu_cache.stop()


def test_only_stats_cold_callback_loads_automatically_other_menus_and_commands_keep_hint(
    m, monkeypatch,
):
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    m["config"].update(lambda cfg: cfg.update({
        "channels": [], "apiKeys": {}, "oauthAccounts": [],
    }))

    m["main"].handle_back(42, 100, "cb-main")
    m["stats_menu"].view(42, 101, "cb-stats", "0", "all")
    m["channel_menu"].show(42, 102, "cb-channel")
    m["oauth_menu"].show(42, 103, "cb-oauth")
    m["apikey_menu"].show(42, 104, "cb-apikey")

    assert len(recorder.edits()) == 2
    assert {edit["message_id"] for edit in recorder.edits()} == {100, 101}
    main_edit = next(edit for edit in recorder.edits() if edit["message_id"] == 100)
    stats_edit = next(edit for edit in recorder.edits() if edit["message_id"] == 101)
    assert "首次使用检测" in main_edit["text"]
    assert "完成后自动更新" in stats_edit["text"]
    answers = [
        data for method, data in recorder.calls if method == "answerCallbackQuery"
    ]
    assert len(answers) == 5
    assert next(data for data in answers if data["callback_query_id"] == "cb-main") == {
        "callback_query_id": "cb-main",
    }
    assert all(
        "初始化" in data.get("text", "")
        for data in answers
        if data["callback_query_id"] in {"cb-channel", "cb-oauth", "cb-apikey"}
    )
    assert "自动更新" in next(
        data["text"] for data in answers if data["callback_query_id"] == "cb-stats"
    )

    m["main"].show(42)
    m["stats_menu"].send_new(42)
    m["channel_menu"].send_new(42)
    m["oauth_menu"].send_new(42)
    m["apikey_menu"].send_new(42)
    sends = [data for method, data in recorder.calls if method == "sendMessage"]
    assert len(sends) == 5
    assert "首次使用检测" in sends[0]["text"]
    assert all("初始化" in data["text"] for data in sends[1:])
    assert len(recorder.edits()) == 2


def test_rolling_stats_uses_same_queue_and_auto_edits_cold_page(m, monkeypatch):
    menu_cache = m["menu_cache"]
    recorder = Recorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    _patch_fast_common_loaders(m, monkeypatch)

    m["stats_menu"].view(42, 100, "cb-first", "3", "all")
    assert len(recorder.edits()) == 1
    assert "正在加载" in recorder.edits()[0]["text"]
    first_answer = [
        data for method, data in recorder.calls if method == "answerCallbackQuery"
    ][-1]
    assert "完成后自动更新" in first_answer["text"]

    menu_cache.start()
    _wait_until(
        lambda: menu_cache.PERIOD_STATS.peek(("rolling-period", "3")).value
        is not None
    )
    _wait_until(lambda: len(recorder.edits()) == 2)
    assert "正在加载" not in recorder.edits()[-1]["text"]

    m["stats_menu"].view(42, 100, "cb-retry", "3", "all")
    assert len(recorder.edits()) == 3
    assert "正在加载" not in recorder.edits()[-1]["text"]


def test_bot_lifecycle_starts_and_stops_scheduler(m, monkeypatch):
    bot = m["bot"]
    menu_cache = m["menu_cache"]
    _patch_fast_common_loaders(m, monkeypatch)
    monkeypatch.setattr(bot, "is_configured", lambda: True)
    monkeypatch.setattr(bot, "_drop_pending_updates", lambda: None)
    monkeypatch.setattr(bot, "_poll_loop", lambda *_args: None)
    monkeypatch.setattr(m["ui"], "delete_my_commands", lambda: None)
    monkeypatch.setattr(m["ui"], "set_my_commands", lambda _commands: None)
    monkeypatch.setattr(m["ui"], "install_notify_handler", lambda: None)
    monkeypatch.setattr(m["ui"], "close_session", lambda: None)
    bot._running = False
    bot._thread = None

    bot.start()
    scheduler_thread = menu_cache.COORDINATOR.thread
    assert scheduler_thread is not None and scheduler_thread.is_alive()
    _wait_common_preheated(menu_cache)

    bot.stop()
    assert not scheduler_thread.is_alive()
    assert menu_cache.COORDINATOR.thread is None


def _reset_log_fixture(m) -> None:
    ld = m["log_db"]
    ld.init()
    conn = ld._get_conn()
    for table in ("upstream_attempt_usage", "retry_chain", "request_detail", "request_log"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    def configure(cfg):
        cfg.setdefault("pricing", {})["enabled"] = True
        cfg["pricing"].setdefault("channelProviders", {})["api:priced"] = "openai"
        cfg["modelBindings"] = {
            "defaults": {
                "gpt-5.6-sol": {"target": "openai/gpt-5.6-sol", "source": "test"},
                "grok-4.5": {"target": "xai/grok-4.5", "source": "test"},
            },
            "scoped": {},
        }
    m["config"].update(configure)


def _insert_success(ld, request_id: str, api_key: str, channel: str,
                    model: str, protocol: str, response_body: str) -> None:
    ld.insert_pending(
        request_id, "127.0.0.1", api_key, model, True, 1, 0, {}, {},
        ingress_protocol="responses",
    )
    ld.finish_success(
        request_id, channel, "oauth" if channel.startswith("oauth:") else "api", model,
        input_tokens=100, output_tokens=20,
        cache_creation_tokens=10, cache_read_tokens=30,
        connect_ms=10, first_token_ms=20, total_ms=1000,
        response_body=response_body, http_status=200,
        upstream_protocol=protocol,
    )


def test_period_batch_matches_old_per_object_and_family_queries(m, monkeypatch):
    _reset_log_fixture(m)
    ld = m["log_db"]
    _insert_success(
        ld, "batch-openai", "key-a", "api:Priced", "gpt-5.6-sol",
        "openai-responses", json.dumps({"id": "normal"}),
    )
    actual_ticks = 123_456_789
    _insert_success(
        ld, "batch-xai", "key-b", "oauth:xai:acct", "grok-4.5",
        "openai-responses",
        json.dumps({
            "service_tier": "priority",
            "usage": {"cost_in_usd_ticks": actual_ticks},
        }),
    )
    since = time.time() - 3600

    # 新批量入口不能回退到逐对象 _aggregate_by_filter。
    old_channel = ld.tokens_for_channel("api:Priced", since)
    old_xai = ld.tokens_for_channel("oauth:xai:acct", since)
    old_key_a = ld.tokens_for_apikey("key-a", since)
    old_key_b = ld.tokens_for_apikey("key-b", since)
    monkeypatch.setattr(
        ld, "_aggregate_by_filter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("N+1 fallback")),
    )
    snapshot = ld.stats_period_snapshot(since)

    fields = (
        "total", "success_count", "error_count", "input", "output",
        "cache_creation", "cache_read", "avg_tps", "max_tps", "min_tps",
        "cost_ticks", "actual_cost_ticks", "estimated_cost_ticks",
        "actual_costed_success", "estimated_costed_success",
        "costed_success", "unpriced_success",
    )
    for current, old in (
        (snapshot["by_channel"]["api:Priced"], old_channel),
        (snapshot["by_channel"]["oauth:xai:acct"], old_xai),
        (snapshot["by_apikey"]["key-a"], old_key_a),
        (snapshot["by_apikey"]["key-b"], old_key_b),
    ):
        assert {field: current[field] for field in fields} == {
            field: old[field] for field in fields
        }
    assert snapshot["by_channel"]["oauth:xai:acct"]["actual_cost_ticks"] == actual_ticks
    assert snapshot["by_channel"]["oauth:xai:acct"]["service_tier_counts"] == {"priority": 1}

    # all/openai family 由同一 snapshot 给出，结果与旧 family 查询口径一致。
    old_family = ld.stats_summary(since, family="openai", summary_top_limit=100)
    assert snapshot["families"]["openai"]["overall"] == old_family["overall"]
    for dimension in ("by_channel", "by_model", "by_apikey"):
        assert snapshot["families"]["openai"][dimension] == old_family[dimension]


def test_menu_renderers_do_not_call_per_object_full_stats(m, monkeypatch):
    snapshot = _empty_period_snapshot()
    snapshot["by_channel"] = {}
    snapshot["by_apikey"] = {}
    monkeypatch.setattr(
        m["log_db"], "_aggregate_by_filter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync N+1")),
    )
    # 渲染函数只消费快照；配置为空或有对象均不会触发旧逐对象聚合。
    m["channel_menu"]._list_text_and_kb(snapshot=snapshot)
    m["apikey_menu"]._render_list(snapshot=snapshot, history_totals={})
    m["oauth_menu"]._list_text_and_kb(month_snapshot=snapshot)
