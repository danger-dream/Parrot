"""Strict current SYS traces layered over the frozen v0.31.13 baseline."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.telegram import states
from src.telegram.menus import system_menu as sm
from src.tests.test_tg_contract_system_helpers import (
    SEGMENT, SYSTEM_IDS, cases_for, run_and_compare,
)
from src.tests.tg_contract import assert_capability_coverage

CASES = cases_for("TG-SYS-08")
ALL_CASES = cases_for(*SYSTEM_IDS)


def _concurrency_empty(e):
    e.callback("sys:show:concurrency")
    e.callback("sys:cc_toggle")


def _concurrency_active(e):
    e.callback("sys:show:concurrency")
    e.callback("sys:edit:cc_queue_wait")
    e.text("sys_cc_queue_wait", "bad")
    e.text("sys_cc_queue_wait", "-1")
    e.text("sys_cc_queue_wait", "0")
    e.callback("sys:edit:cc_default_max")
    e.text("sys_cc_default_max", "bad")
    e.text("sys_cc_default_max", "-1")
    e.text("sys_cc_default_max", "7")


def _limiter_empty(e):
    e.callback("sys:show:aklim")
    e.callback("sys:aklim_toggle")


def _limiter_active(e):
    e.callback("sys:show:aklim")
    values = {
        "max": ("bad", "-1", "0"),
        "queue": ("bad", "-1", "25"),
        "wait": ("bad", "-1m", "30m"),
    }
    for field, attempts in values.items():
        e.callback("sys:edit:aklim_" + field)
        for value in attempts:
            e.text("sys_aklim_" + field, value)
    states.set_state(42, "sys_aklim_unknown")
    e.text("sys_aklim_unknown", "9", "unknown-limiter-state")


def _limiter_durations(e):
    for value in ("1800", "30m", "1h", "45s", "0"):
        e.callback("sys:edit:aklim_wait")
        e.text("sys_aklim_wait", value)


def _ws(e):
    e.callback("sys:show:ws_mode")
    e.callback("sys:ws_mode:toggle")
    e.callback("sys:ws_mode:toggle")


RUNNERS = {
    "concurrency_empty": _concurrency_empty,
    "concurrency_active": _concurrency_active,
    "limiter_empty": _limiter_empty,
    "limiter_active": _limiter_active,
    "limiter_durations": _limiter_durations,
    "ws": _ws,
}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_system_runtime_trace(case, monkeypatch):
    run_and_compare(case, monkeypatch, RUNNERS[case["entry"]["scenario"]])


# Current callback/state families, including reviewed overlays and retained
# legacy entries. Coverage is checked in both directions against these sets.
CALLBACK_FAMILIES = {
    "menu:settings", "sys:show:retention", "sys:retention:days",
    "sys:retention:forever", "sys:retention:toggle_bodies",
    "sys:retention:cancel_input", "sys:retention:noop", "sys:retention:scan:*",
    "sys:retention:plan:*", "sys:retention:commit:*", "sys:retention:cancel:*",
    "sys:show:retry", "sys:retry:toggle_transient", "sys:retry:failover_info",
    "sys:retry:edit_attempts", "sys:retry:edit_backoff",
    "sys:retry:toggle_error:*", "sys:retry:toggle_recovery:*",
    "sys:show:timeouts", "sys:edit:timeouts", "sys:show:errwin",
    "sys:edit:errwin", "sys:edit:oauth_grace", "sys:edit:ladder_interval",
    "sys:edit:perm_min_age", "sys:show:scoring", "sys:edit:scoring:*",
    "sys:show:affinity", "sys:edit:affinity:*", "sys:show:chsel",
    "sys:chsel_set:*", "sys:show:network", "sys:net:edit_dns",
    "sys:net:sync_dns", "sys:net:dns_save", "sys:net:dns_save_force",
    "sys:net:edit_socks5", "sys:net:socks5_save", "sys:net:socks5_save_force",
    "sys:net:toggle_socks5", "sys:net:dns_cache", "sys:net:dns_cache_clear",
    "sys:mon:show", "sys:mon:toggle:*", "sys:mon:edit_interval",
    "sys:mon:run_now", "sys:mon:core", "sys:mon:core_toggle:*",
    "sys:mon:channels", "sys:mon:channels_toggle", "sys:mon:ch_toggle:*",
    "sys:show:notif", "sys:notif_toggle_main", "sys:notif_toggle:*",
    "sys:show:blacklist", "sys:bl_add_default", "sys:bl_del_default",
    "sys:bl_del_exec:*", "sys:bl_add_ch", "sys:show:concurrency",
    "sys:cc_toggle", "sys:edit:cc_queue_wait", "sys:edit:cc_default_max",
    "sys:show:aklim", "sys:aklim_toggle", "sys:edit:aklim_max",
    "sys:edit:aklim_queue", "sys:edit:aklim_wait", "sys:show:ws_mode",
    "sys:ws_mode:toggle",
    # Model-degraded alert mute buttons.
    "sys:mdg_mute:*", "sys:mdg_unmute:*", "sys:mdg_off",
    # Present UI helpers that v0.31.13's router no longer dispatches.
    "sys:show:cch", "sys:cch_set:*", "sys:show:quota", "sys:quota_toggle",
    "sys:edit:quota_interval", "sys:edit:quota_threshold",
}
UNAVAILABLE_ENTRY_CALLBACKS = {
    "sys:bl_del_ch:*",  # by-channel terms have add-only UI in this baseline
    "sys:mon:history",  # history is embedded in sys:mon:show, not a separate route
}
STATE_FAMILIES = {
    "sys_retention_days", "sys_retry_attempts", "sys_retry_backoff",
    "sys_timeouts", "sys_errwin", "sys_oauth_grace", "sys_ladder_interval",
    "sys_perm_min_age", "sys_scoring:*", "sys_affinity:*",
    "sys_bl_add_default", "sys_bl_add_ch", "sys_net_dns", "sys_net_socks5",
    "sys_mon_interval", "sys_cc_queue_wait", "sys_cc_default_max", "sys_aklim_*",
    "sys_net_dns_confirm", "sys_net_socks5_confirm",
    # Emitted by legacy quota helpers but not routed by handle_text_state.
    "sys_quota_interval", "sys_quota_threshold",
}


def _source_handler_families() -> tuple[set[str], set[str]]:
    source = Path(sm.__file__).read_text(encoding="utf-8")
    callback_block = source[source.index("def handle_callback"):source.index("def handle_text_state")]
    state_block = source[source.index("def handle_text_state"):source.index("# ─── 并发限制")]
    callbacks = set(re.findall(r'data == "([^"]+)"', callback_block))
    callbacks |= {value + "*" for value in re.findall(r'data\.startswith\("([^"]+)"\)', callback_block)}
    state_values = set(re.findall(r'action == "([^"]+)"', state_block))
    state_values |= {value + "*" for value in re.findall(r'action\.startswith\("([^"]+)"\)', state_block)}
    return callbacks, state_values


def test_system_manifest_ids_case_ids_and_family_coverage_are_bidirectional():
    assert_capability_coverage(SYSTEM_IDS, ALL_CASES)
    assert len({case["caseId"] for case in ALL_CASES}) == len(ALL_CASES)
    fixture_callbacks = {item for case in ALL_CASES for item in case["entry"].get("coveredCallbacks", [])}
    fixture_states = {item for case in ALL_CASES for item in case["entry"].get("coveredStates", [])}
    unavailable = {item for case in ALL_CASES for item in case["entry"].get("coveredUnavailableCallbacks", [])}
    assert fixture_callbacks == CALLBACK_FAMILIES
    assert fixture_states == STATE_FAMILIES
    assert unavailable == UNAVAILABLE_ENTRY_CALLBACKS

    routed_callbacks, routed_states = _source_handler_families()
    intentionally_unrouted_callbacks = {
        "sys:show:cch", "sys:cch_set:*", "sys:show:quota", "sys:quota_toggle",
        "sys:edit:quota_interval", "sys:edit:quota_threshold",
    }
    intentionally_unrouted_states = {"sys_quota_interval", "sys_quota_threshold"}
    callback_values = [step["callbackData"] for case in ALL_CASES for step in case["stateSteps"] if "callbackData" in step]
    state_values = [step["stateAction"] for case in ALL_CASES for step in case["stateSteps"] if "stateAction" in step]
    state_values += [step["state"]["action"] for case in ALL_CASES for step in case["stateSteps"] if step.get("state")]

    def covered(value, families):
        return next((family for family in families if value == family or (family.endswith("*") and value.startswith(family[:-1]))), None)

    observed_callbacks = {covered(value, CALLBACK_FAMILIES | UNAVAILABLE_ENTRY_CALLBACKS) for value in callback_values}
    observed_states = {covered(value, STATE_FAMILIES) for value in state_values}
    assert None not in observed_callbacks
    assert None not in observed_states
    assert observed_callbacks == CALLBACK_FAMILIES | UNAVAILABLE_ENTRY_CALLBACKS
    assert observed_states == STATE_FAMILIES
    assert routed_callbacks | intentionally_unrouted_callbacks == CALLBACK_FAMILIES
    assert routed_states | intentionally_unrouted_states | {"sys_net_dns_confirm", "sys_net_socks5_confirm"} == STATE_FAMILIES


def test_system_manifest_is_raw_strict_and_contains_no_management_auth_surface():
    raw = SEGMENT.read_bytes()
    assert raw
    assert b"mauth:" not in raw
    for case in ALL_CASES:
        assert "update" in case["entry"]
        assert set(case) == {
            "caseId", "capabilityId", "entry", "initialConfig", "initialState",
            "initialRuntime", "tgApi", "stateSteps", "finalBusinessState",
            "expectedException",
        }
        for call in case["tgApi"]:
            assert call["method"] in {"sendMessage", "editMessageText", "answerCallbackQuery"}
            # Round-trip proves exact UTF-8 fixture bytes are valid without normalization.
            assert json.loads(json.dumps(call["payload"], ensure_ascii=False)) == call["payload"]
