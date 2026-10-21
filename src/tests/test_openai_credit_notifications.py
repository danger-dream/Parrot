from __future__ import annotations

import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import notifier
from src.openai import credit_notifications as alerts
from src.tests.test_openai_credit_opt_in import _allow, _payload
from src.tests.test_openai_oauth_quota import _import_modules, _MockResp, _UiRecorder
from src.tests.test_codex_quota_fragments import _account


@pytest.fixture
def account(m, monkeypatch):
    key, channel = _account(m, "credit-alert")
    _allow(m, key)
    m["config"].update(lambda cfg: cfg.update(notifications={"enabled": True, "events": {}}))
    sent = []
    capture(monkeypatch, lambda kind, text, record: sent.append(text) or True)
    alerts._states.clear()
    yield key, channel, sent
    alerts._states.clear()


def usage(m, *, used=100, observed=None, **kwargs):
    payload = _payload(**kwargs)
    if not kwargs.get("no_plan"):
        payload["rate_limit"]["secondary_window"]["used_percent"] = used
        payload["rate_limit"].update(allowed=used < 100, limit_reached=used >= 100)
    value = m["openai_provider"].normalize_wham_usage(payload)
    value["openai"]["quota_observed_at"] = observed or int(time.time() * 1000)
    return value


def headers(m, channel, used=100):
    m["failover"]._maybe_record_codex_snapshot(channel, _MockResp({
        "x-codex-secondary-used-percent": str(used),
        "x-codex-secondary-window-minutes": "10080",
        "x-codex-secondary-reset-after-seconds": "86400",
        "x-codex-credits-has-credits": "true",
        "x-codex-credits-balance": "62500",
    }))


@pytest.mark.parametrize("entry", ["headers", "ws", "monitor"])
def test_credit_notification_is_shared_by_live_and_monitor_paths(m, account, monkeypatch, entry):
    key, channel, sent = account
    if entry == "headers":
        headers(m, channel)
    elif entry == "ws":
        snap = {"secondary_used_pct": 100, "secondary_window_min": 10080,
                "secondary_reset_sec": 86400, "fetched_at": int(time.time() * 1000),
                "credits": {"has_credits": True, "balance": "62500"}}
        m["failover"]._maybe_record_codex_snapshot(channel, None, snapshot=snap)
    else:
        monkeypatch.setattr(m["openai_provider"], "_mock_wham_payload", _payload)
        asyncio.run(m["oauth_manager"].quota_monitor_once())
    assert len(sent) == 1
    assert "套餐额度耗尽，将使用积分" in sent[0]
    assert "credit-alert@quota.test" in sent[0]
    assert "暂停积分续用" in sent[0]
    assert m["oauth_manager"].get_account(key)["enabled"] is True
    headers(m, channel)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m))
    assert len(sent) == 1


def test_sent_episode_stops_repeat_events_at_the_source(m, monkeypatch):
    # Real durable store: a duplicate is suppressed there, so emit reports no
    # new push even though the episode was already delivered.
    key, channel = _account(m, "credit-repeat")
    _allow(m, key)
    m["config"].update(lambda cfg: cfg.update(notifications={"enabled": True, "events": {}},
                                              telegram={**(cfg.get("telegram") or {}), "adminIds": [11]}))
    queued = []
    monkeypatch.setattr(notifier, "enqueue_delivery", lambda delivery_id: queued.append(delivery_id) or True)
    alerts._states.clear()
    headers(m, channel)
    assert len(queued) == 1
    calls = []
    real = alerts.notifications.account_event
    monkeypatch.setattr(alerts.notifications, "account_event", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    alerts._states.clear()  # a restart loses memory; the durable episode still stops repeats
    for _ in range(3):
        headers(m, channel)
    assert calls == ["account.credits.started"] and len(queued) == 1
    alerts._states.clear()


@pytest.mark.parametrize("used", [0, 95, 99.9])
def test_disable_threshold_is_not_exhaustion(m, account, used):
    key, channel, sent = account
    headers(m, channel, used)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m, used=used))
    assert not sent
    assert m["oauth_manager"].get_account(key)["enabled"] is True


@pytest.mark.parametrize("case", ["no-opt-in", "no-credits", "spend", "overage", "user", "auth_error"])
def test_unusable_or_disabled_account_does_not_claim_credit_continuation(m, account, case):
    key, _channel, sent = account
    kwargs = {}
    if case == "no-opt-in":
        _allow(m, key, False)
    elif case == "no-credits":
        kwargs["credits"] = {"has_credits": False, "balance": "0"}
    elif case in {"spend", "overage"}:
        kwargs["hard"] = case
    else:
        m["oauth_manager"].set_enabled(key, False, reason=case)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m, **kwargs))
    assert not any("将使用积分继续" in text for text in sent)


@pytest.mark.parametrize("switch", ["main", "event"])
def test_notification_switches_do_not_consume_unsent_episode(m, account, switch):
    key, channel, sent = account
    disabled = {"enabled": False} if switch == "main" else {
        "enabled": True, "events": {"openai_credits_started": False}}
    m["config"].update(lambda cfg: cfg.__setitem__("notifications", disabled))
    headers(m, channel)
    assert not sent
    m["config"].update(lambda cfg: cfg.__setitem__("notifications", {"enabled": True, "events": {}}))
    headers(m, channel)
    assert len(sent) == 1
    assert m["oauth_manager"].get_account(key)["enabled"]


def test_recovery_rearms_and_old_snapshot_cannot_restart_episode(m, account):
    key, _channel, sent = account
    now = int(time.time() * 1000)
    high = usage(m, observed=now)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 1
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m, used=10, observed=now + 10))
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 1
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m, observed=now + 20))
    assert len(sent) == 2


def test_partial_unrelated_window_cannot_rearm(m, account):
    key, _channel, sent = account
    now = int(time.time() * 1000)
    value = usage(m, observed=now)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, value)
    alerts.observe(key, snapshot={"primary_used_pct": 0, "fetched_at": now + 10})
    alerts.observe(key, snapshot={"credits": {"balance": "60000"}, "fetched_at": now + 20})
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m, observed=now + 30))
    assert len(sent) == 1


def test_expired_window_does_not_alert_and_new_cycle_rearms(m, account, monkeypatch):
    key, _channel, sent = account
    clock = [time.time()]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    snap = {"secondary_used_pct": 100, "secondary_reset_at": clock[0] - 1,
            "fetched_at": int(clock[0] * 1000), "credits": {"has_credits": True}}
    alerts.observe(key, snapshot=snap)
    assert not sent
    snap["secondary_reset_at"] = clock[0] + 60
    alerts.observe(key, snapshot=snap)
    assert len(sent) == 1
    clock[0] += 61
    alerts.observe(key, snapshot=snap)
    assert len(sent) == 1
    snap.update(secondary_reset_at=clock[0] + 60, fetched_at=int(clock[0] * 1000))
    alerts.observe(key, snapshot=snap)
    assert len(sent) == 2


def test_windowless_explicit_exhaustion_and_recovery(m, account):
    key, _channel, sent = account
    high = usage(m, no_plan=True)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 1
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 1
    alerts.observe(key, snapshot={"primary_used_pct": 0,
                                 "fetched_at": high["openai"]["quota_observed_at"] + 1})
    high["openai"]["quota_observed_at"] += 2
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 1
    low = copy.deepcopy(high)
    low["openai"].update(allowed=True, limit_reached=False,
                         quota_observed_at=high["openai"]["quota_observed_at"] + 1)
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, low)
    high["openai"]["quota_observed_at"] += 2
    m["oauth_manager"].evaluate_and_toggle_by_usage(key, high)
    assert len(sent) == 2


def test_concurrent_observations_only_enqueue_once(m, account):
    key, _channel, sent = account
    value = usage(m)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: alerts.observe(key, usage=value), range(20)))
    assert len(sent) == 1


def test_enqueue_failure_retries_without_affecting_account(m, account, monkeypatch):
    key, channel, sent = account
    capture(monkeypatch, lambda *a: False)
    headers(m, channel)
    assert not alerts._states[key].sent
    capture(monkeypatch, lambda kind, text, record: sent.append(text) or True)
    headers(m, channel)
    assert len(sent) == 1 and m["oauth_manager"].get_account(key)["enabled"]


def test_distinct_workspaces_have_independent_alerts_and_delete_clears_state(m, account):
    key, _channel, sent = account
    manager = m["oauth_manager"]
    first = manager.get_account(key)
    manager.add_account({"email": first["email"], "provider": "openai",
                         "access_token": "at-other", "refresh_token": "rt-other",
                         "chatgpt_account_id": "other-workspace"})
    other = next(a for a in manager.list_accounts() if a.get("chatgpt_account_id") == "other-workspace")
    other_key = manager._account_key(other)
    _allow(m, other_key)
    for target in (key, other_key):
        manager.evaluate_and_toggle_by_usage(target, usage(m))
    assert len(sent) == 2
    manager.delete_account(key)
    assert key not in alerts._states and other_key in alerts._states


@pytest.mark.parametrize("status", [403, 429, 500])
def test_error_response_does_not_announce_credit_continuation(m, account, status):
    _key, channel, sent = account
    response = _MockResp({
        "x-codex-secondary-used-percent": "100",
        "x-codex-secondary-reset-after-seconds": "86400",
        "x-codex-credits-has-credits": "true",
    })
    response.status_code = status
    m["failover"]._maybe_record_codex_snapshot(channel, response)
    assert not sent


def test_notification_failure_does_not_break_quota_or_response_path(m, account, monkeypatch):
    key, channel, _sent = account
    def fail(*args, **kwargs):
        raise RuntimeError("queue unavailable")
    capture(monkeypatch, lambda kind, text, record: fail(kind, text))
    headers(m, channel)
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(key, usage(m))
    assert result["action"] == "kept_enabled"
    assert m["oauth_manager"].get_account(key)["enabled"]
    assert not alerts._states[key].sent


def test_tg_notification_has_independent_toggle(m, account, monkeypatch):
    from src.telegram.menus import system_menu
    recorder = _UiRecorder()
    monkeypatch.setattr(m["ui"], "api", recorder)
    system_menu._show_notif(42, 100, "show")
    page = recorder.last("editMessageText")
    buttons = [button for row in page["reply_markup"]["inline_keyboard"] for button in row]
    assert [button["callback_data"] for button in buttons if "套餐额度耗尽，将使用积分" in button["text"]] == ["sys:notif_toggle:account.credits.started"]
    toggles = [button["text"][2:] for button in buttons if button["callback_data"].startswith("sys:notif_toggle:")]
    assert len(toggles) == len(set(toggles))
    assert not any(button["callback_data"] == "sys:notif_toggle:openai_credits_started" for button in buttons)
    system_menu._on_notif_toggle_event(42, 100, "toggle", "account.credits.started")
    events = m["config"].get()["notifications"]["events"]
    assert events["account.credits.started"] is False
    assert events.get("openai_credits_started", True) is True
    assert events.get("quota_disabled", True) is True

from src.tests.notification_helpers import capture
