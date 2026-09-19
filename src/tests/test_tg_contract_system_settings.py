"""Strict v0.31.13 traces for TG-SYS-01 through TG-SYS-04."""
from __future__ import annotations

import pytest

from src.telegram import states, ui
from src.telegram.menus import system_menu as sm
from src.tests.test_tg_contract_system_helpers import cases_for, run_and_compare

CASES = cases_for("TG-SYS-01", "TG-SYS-02", "TG-SYS-03", "TG-SYS-04")


def _home_edit(e):
    e.callback("menu:settings")


def _home_send(e):
    e.direct("send_new", lambda: sm.send_new(42))


def _retry_toggles(e):
    e.callback("sys:show:retry")
    e.callback("sys:retry:failover_info")
    e.callback("sys:retry:toggle_transient")
    for key in ("openaiServerOverloaded", "openaiServerError", "claudeOverloaded", "xaiUnavailable"):
        e.callback("sys:retry:toggle_error:" + key)
    for key in ("oauthRefresh", "invalidEncryptedContent", "claudeContext1mFallback"):
        e.callback("sys:retry:toggle_recovery:" + key)
    e.callback("sys:retry:toggle_error:unknown")
    e.callback("sys:retry:toggle_recovery:unknown")


def _retry_inputs(e):
    e.callback("sys:retry:edit_attempts")
    for value in ("x", "0", "6"):
        e.text("sys_retry_attempts", value)
    e.text("sys_retry_attempts", "3")
    e.callback("sys:retry:edit_backoff")
    for value in ("", "1,2,3,4,5,6", "nan,1", "61"):
        e.text("sys_retry_backoff", value)
    e.text("sys_retry_backoff", "0.5, 1.25")


def _timeouts(e):
    e.callback("sys:show:timeouts")
    e.callback("sys:edit:timeouts")
    for value in ("10,20,30", "a,b,c,d", "-1,30,30,600"):
        e.text("sys_timeouts", value)
    e.text("sys_timeouts", "11,31,32,650")


def _timeouts_cancel(e):
    e.callback("sys:edit:timeouts")
    e.callback("sys:show:timeouts", "cancel-via-back")


def _error_ladder(e):
    e.callback("sys:show:errwin")
    e.callback("sys:edit:errwin")
    for value in ("", "a,b", "1,-1"):
        e.text("sys_errwin", value)
    e.text("sys_errwin", "2,5,10,30,0")
    e.callback("sys:edit:oauth_grace")
    for value in ("x", "-1", "101"):
        e.text("sys_oauth_grace", value)
    e.text("sys_oauth_grace", "4")
    e.callback("sys:edit:ladder_interval")
    for value in ("x", "-1", "3601"):
        e.text("sys_ladder_interval", value)
    e.text("sys_ladder_interval", "45")
    e.callback("sys:edit:perm_min_age")
    for value in ("x", "-1", "86401"):
        e.text("sys_perm_min_age", value)
    e.text("sys_perm_min_age", "301")


def _scoring(e):
    e.callback("sys:show:scoring")
    values = {
        "emaAlpha": ("bad", "1.1", "0.33"),
        "recentWindow": ("bad", "0", "42"),
        "errorPenaltyFactor": ("bad", "101", "10"),
        "explorationRate": ("bad", "-0.1", "0.1"),
    }
    for field, attempts in values.items():
        e.callback("sys:edit:scoring:" + field)
        for value in attempts:
            e.text("sys_scoring:" + field, value)
    e.callback("sys:edit:scoring:unknown")
    states.set_state(42, "sys_scoring:unknown")
    e.text("sys_scoring:unknown", "1", "unknown-scoring-state")


def _affinity(e):
    e.callback("sys:show:affinity")
    e.callback("sys:edit:affinity:ttlMinutes")
    for value in ("bad", "0", "1441"):
        e.text("sys_affinity:ttlMinutes", value)
    e.text("sys_affinity:ttlMinutes", "45")
    e.callback("sys:edit:affinity:unknown")
    states.set_state(42, "sys_affinity:unknown")
    e.text("sys_affinity:unknown", "1", "unknown-affinity-state")


def _cch(e):
    e.direct("cch-show", lambda: sm._show_cch(42, 100, "cb-system"))
    e.direct("cch-dynamic", lambda: sm._on_cch_set(42, 100, "cb-system", "dynamic"))
    e.direct("cch-disabled", lambda: sm._on_cch_set(42, 100, "cb-system", "disabled"))
    e.direct("cch-invalid", lambda: sm._on_cch_set(42, 100, "cb-system", "static"))
    e.callback("sys:show:cch", "unrouted-cch-show")
    e.callback("sys:cch_set:dynamic", "unrouted-cch-set")


def _chsel(e, monkeypatch):
    from src.telegram.menus import load_balancing_menu
    monkeypatch.setattr(load_balancing_menu, "show", lambda chat_id, message_id: e.events.append({
        "event": "load-balancing.show", "chatId": chat_id, "messageId": message_id,
    }))
    def set_mode(mode):
        e.events.append({"event": "load-balancing.set", "mode": mode})
        if e.fake.get("chselFailure"):
            raise RuntimeError(e.fake["chselFailure"])
        e.cfg["channelSelection"] = mode
    monkeypatch.setattr(sm.load_balancing, "set_mode", set_mode)
    e.direct("chsel-show-page", lambda: sm._show_chsel(42, 100, "cb-system"))
    e.callback("sys:show:chsel", "chsel-legacy-redirect")
    e.callback("sys:chsel_set:order", "chsel-legacy-set-redirect")
    e.direct("chsel-set-order", lambda: sm._on_chsel_set(42, 100, "cb-system", "order"))
    e.direct("chsel-set-invalid", lambda: sm._on_chsel_set(42, 100, "cb-system", "bogus"))


def _quota(e):
    e.direct("quota-show", lambda: sm._show_quota(42, 100, "cb-system"))
    e.direct("quota-toggle", lambda: sm._on_quota_toggle(42, 100, "cb-system"))
    e.direct("quota-edit-interval", lambda: sm._edit_quota_interval(42, 100, "cb-system"))
    for value in ("bad", "9", "86401"):
        e.direct("quota-interval:" + value, lambda v=value: sm._on_quota_interval_input(42, v))
    e.direct("quota-interval:90", lambda: sm._on_quota_interval_input(42, "90"))
    e.direct("quota-edit-threshold", lambda: sm._edit_quota_threshold(42, 100, "cb-system"))
    for value in ("bad", "0", "101"):
        e.direct("quota-threshold:" + value, lambda v=value: sm._on_quota_threshold_input(42, v))
    e.direct("quota-threshold:97%", lambda: sm._on_quota_threshold_input(42, "97%"))
    e.callback("sys:show:quota", "unrouted-quota-show")
    e.callback("sys:quota_toggle", "unrouted-quota-toggle")
    e.callback("sys:edit:quota_interval", "unrouted-quota-interval-edit")
    e.callback("sys:edit:quota_threshold", "unrouted-quota-threshold-edit")
    e.text("sys_quota_interval", "60", "unrouted-quota-state")


def _notifications(e):
    e.callback("sys:show:notif")
    e.callback("sys:notif_toggle_main")
    for key, _label in sm._NOTIF_EVENTS:
        e.callback("sys:notif_toggle:" + key)
    e.callback("sys:notif_toggle:unknown")
    # 模型降级通知的三个静音按钮：按渠道静音 → 取消静音 → 永久禁用。
    e.callback("sys:mdg_mute:1:deadbeef")
    e.callback("sys:mdg_unmute:deadbeef")
    e.callback("sys:mdg_off")


def _blacklist(e):
    e.callback("sys:show:blacklist")
    e.callback("sys:bl_add_default")
    e.text("sys_bl_add_default", "   ")
    e.text("sys_bl_add_default", "x" * 201)
    e.text("sys_bl_add_default", "policy<&>")
    e.callback("sys:bl_add_default")
    e.text("sys_bl_add_default", "policy<&>", "duplicate-default")
    e.callback("sys:bl_del_default")
    delete_cb = e.capture.calls[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    e.callback(delete_cb, "delete-default-short-code")
    e.callback(delete_cb, "repeat-delete-is-idempotent")
    e.callback("sys:bl_del_exec:deadbeef", "expired-delete-short-code")
    e.callback("sys:bl_del_ch:deadbeef", "unavailable-by-channel-delete")
    e.callback("sys:bl_add_ch")
    for value in ("no-equals", "=term", "channel="):
        e.text("sys_bl_add_ch", value)
    e.text("sys_bl_add_ch", "API <One>=danger&term")
    e.callback("sys:bl_add_ch")
    e.text("sys_bl_add_ch", "API <One>=danger&term", "duplicate-channel-term")


def _blacklist_empty_delete(e):
    e.callback("sys:bl_del_default")


RUNNERS = {
    "home_edit": _home_edit,
    "home_send": _home_send,
    "retry_toggles": _retry_toggles,
    "retry_inputs": _retry_inputs,
    "timeouts": _timeouts,
    "timeouts_cancel": _timeouts_cancel,
    "error_ladder": _error_ladder,
    "scoring": _scoring,
    "affinity": _affinity,
    "cch": _cch,
    "quota": _quota,
    "notifications": _notifications,
    "blacklist": _blacklist,
    "blacklist_empty_delete": _blacklist_empty_delete,
}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_system_settings_trace(case, monkeypatch):
    scenario = case["entry"]["scenario"]
    if scenario == "chsel":
        run_and_compare(case, monkeypatch, lambda e: _chsel(e, monkeypatch))
    else:
        run_and_compare(case, monkeypatch, RUNNERS[scenario])
