"""Quota/refusal lifecycle regressions through real parsing and StateStore writes."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import time

import pytest

from src.tests.test_openai_oauth_quota import _import_modules, _MockResp
from src.tests.test_codex_quota_fragments import _account, _save_headers
from src.tests.test_openai_stale_refusal import _store_wham, _OLD_REFUSAL, _MISSING
from src.tests.test_openai_cached_wham_gate import _wham, _LOW_HEADERS


def _usage(m, key):
    return m["oauth_manager"].usage_from_quota_row(m["state_db"].quota_load(key))


def _reopen(m):
    assert m["state_db"].flush(strict=True)
    assert m["state_db"].close()
    m["state_db"].init()


def _clock(m, monkeypatch):
    clock = [int(time.time()) * 1000]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz=tz)

    monkeypatch.setattr(m["oauth_manager"], "datetime", ClockDateTime)
    monkeypatch.setattr(time, "time", lambda: clock[0] / 1000)
    return clock


@pytest.mark.parametrize("later", ["missing", "invalid"])
@pytest.mark.parametrize("reopen_store", [False, True])
def test_cleared_refusal_stays_cleared_after_sparse_reads(m, later, reopen_store):
    key, _ = _account(m, "cleared-lifecycle")
    manager = m["oauth_manager"]
    now = int(time.time() * 1000)
    _save_headers(m, key, {"x-codex-rate-limit-reached-type": _OLD_REFUSAL}, now - 3000)
    _store_wham(m, key, now - 2000, None)
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] is None
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    if reopen_store:
        _reopen(m)
    fresh = _store_wham(m, key, now - 1000, _MISSING if later == "missing" else "")
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] is None
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    assert manager.evaluate_and_toggle_by_usage(key, fresh, fresh=True)["action"] == "kept_enabled"
    assert manager.get_account(key)["enabled"] is True
    row = m["state_db"].quota_load(key)
    assert row["codex_wham_reached_at"] == now - 2000
    assert row["codex_rate_limit_reached_type"] == _OLD_REFUSAL  # source facts retained


@pytest.mark.parametrize("allow_credits", [False, True])
def test_slow_detail_cannot_retimestamp_old_null_past_new_refusal(m, monkeypatch, allow_credits):
    key, channel = _account(m, "slow-detail")
    manager = m["oauth_manager"]
    manager.get_account(key)["allowCredits"] = allow_credits
    clock = _clock(m, monkeypatch)
    start = clock[0]
    m["config"].get().setdefault("quotaMonitor", {}).update(enabled=False, accessRefreshThrottleSeconds=0)

    async def token(_key, *, expected_state_key=None):
        assert expected_state_key == manager.account_state_key(manager.get_account(_key))
        return manager.get_account(_key)["access_token"]

    async def wham(*args, **kwargs):
        payload = m["openai_provider"]._mock_wham_payload()
        payload["rate_limit_reached_type"] = None
        payload["credits"] = {"has_credits": True, "balance": "9"}
        return m["openai_provider"].normalize_wham_usage(payload)

    async def details(*args, **kwargs):
        clock[0] = start + 1000
        m["failover"]._maybe_record_codex_snapshot(channel, _MockResp({
            "x-codex-rate-limit-reached-type": _OLD_REFUSAL,
        }))
        clock[0] = start + 2000
        return {"available_count": 2, "data": []}

    monkeypatch.setattr(manager, "ensure_valid_token", token)
    monkeypatch.setattr(m["openai_provider"], "fetch_wham_usage", wham)
    monkeypatch.setattr(manager, "fetch_openai_rate_limit_reset_credits", details)
    assert asyncio.run(manager.ensure_quota_fresh(key)) is True
    row = m["state_db"].quota_load(key)
    assert row["codex_active_observed_at"] == start
    assert row["fetched_at"] == start + 2000
    assert row["codex_rate_limit_reached_at"] == start + 1000
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] == _OLD_REFUSAL
    assert manager.get_account(key)["enabled"] is False  # fresh evaluation is protected too
    assert not manager.openai_credits_usable(key)
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "wham_limit_keep_disabled"


@pytest.mark.parametrize("reopen_store", [False, True])
def test_recovered_gate_does_not_revive_after_low_sample_expiry(m, monkeypatch, reopen_store):
    key, channel = _account(m, "expiry-lifecycle")
    manager = m["oauth_manager"]
    clock = _clock(m, monkeypatch)
    _wham(m, key, clock[0] - 1000)
    headers = {**_LOW_HEADERS,
               "x-codex-primary-reset-after-seconds": "10",
               "x-codex-secondary-reset-after-seconds": "100"}
    m["failover"]._maybe_record_codex_snapshot(channel, _MockResp(headers))
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    assert m["state_db"].quota_load(key)["codex_wham_gate_retired"] is True
    if reopen_store:
        _reopen(m)
    clock[0] += 11000
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    assert manager.get_account(key)["enabled"] is True
    assert json.loads(m["state_db"].quota_load(key)["raw_data"])["openai"]["allowed"] is False


@pytest.mark.parametrize("first_minutes", [300, 10080])
def test_complete_monthly_pair_does_not_require_nonexistent_window(m, first_minutes):
    key, _ = _account(m, "monthly-complete")
    now = int(time.time() * 1000)
    payload = m["openai_provider"]._mock_wham_payload()
    payload["rate_limit"].update(allowed=False, limit_reached=True)
    payload["rate_limit"]["primary_window"]["limit_window_seconds"] = first_minutes * 60
    payload["rate_limit"]["secondary_window"].update(used_percent=100, limit_window_seconds=2592000)
    usage = m["openai_provider"].normalize_wham_usage(payload)
    m["state_db"].quota_save(key, {**m["oauth_manager"].flatten_usage(usage), "fetched_at": now - 1000})
    headers = {**_LOW_HEADERS, "x-codex-primary-window-minutes": str(first_minutes),
               "x-codex-secondary-window-minutes": "43200"}
    _save_headers(m, key, headers, now)
    assert m["oauth_manager"].evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    assert m["oauth_manager"].get_account(key)["enabled"] is True


@pytest.mark.parametrize("new_refusal_delta", [0, 1000])
def test_new_refusal_beats_persisted_clear_and_late_old_null(m, new_refusal_delta):
    key, _ = _account(m, "ordered-refusals")
    now = int(time.time() * 1000)
    _store_wham(m, key, now, None)
    _store_wham(m, key, now + 2000, _MISSING)
    _save_headers(m, key, {"x-codex-rate-limit-reached-type": _OLD_REFUSAL}, now + new_refusal_delta)
    _store_wham(m, key, now - 1000, None)
    _reopen(m)
    assert m["state_db"].quota_load(key)["codex_active_observed_at"] == now + 2000
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] == _OLD_REFUSAL
    assert m["oauth_manager"].evaluate_and_toggle_by_cached_quota(key)["action"] == "wham_limit_disabled"


@pytest.mark.parametrize("late_refusal", [None, _OLD_REFUSAL])
def test_out_of_order_active_reads_preserve_newer_explicit_field(m, late_refusal):
    key, _ = _account(m, "ordered-active")
    now = int(time.time() * 1000)
    _store_wham(m, key, now, _OLD_REFUSAL if late_refusal is None else None)
    _store_wham(m, key, now + 2000, _MISSING)
    _store_wham(m, key, now - 1000, late_refusal)
    expected = _OLD_REFUSAL if late_refusal is None else None
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] == expected
    row = m["state_db"].quota_load(key)
    assert row["codex_active_observed_at"] == now + 2000
    assert row["codex_wham_reached_at"] == now


def test_late_explicit_clear_can_fill_newer_missing_field(m):
    key, _ = _account(m, "late-field")
    now = int(time.time() * 1000)
    _store_wham(m, key, now, _OLD_REFUSAL)
    _store_wham(m, key, now + 2000, _MISSING)
    _store_wham(m, key, now + 1000, None)
    row = m["state_db"].quota_load(key)
    assert row["codex_active_observed_at"] == now + 2000
    assert row["codex_wham_reached_at"] == now + 1000
    assert _usage(m, key)["openai"]["rate_limit_reached_type"] is None


@pytest.mark.parametrize("later", ["active-gate", "hard-refusal", "high-window"])
def test_retirement_never_masks_new_limits(m, later):
    key, _ = _account(m, "retirement-new-limit")
    manager = m["oauth_manager"]
    now = int(time.time() * 1000)
    _wham(m, key, now - 1000)
    _save_headers(m, key, _LOW_HEADERS, now)
    assert manager.evaluate_and_toggle_by_cached_quota(key)["action"] == "cached_below_threshold"
    if later == "active-gate":
        _wham(m, key, now + 1000)
        assert m["state_db"].quota_load(key)["codex_wham_gate_retired"] is False
    elif later == "hard-refusal":
        _save_headers(m, key, {"x-codex-rate-limit-reached-type": _OLD_REFUSAL}, now + 1000)
    else:
        _save_headers(m, key, {**_LOW_HEADERS, "x-codex-primary-used-percent": "100"}, now + 1000)
    result = manager.evaluate_and_toggle_by_cached_quota(key)
    assert result["any_over"] is True
    assert manager.get_account(key)["enabled"] is False


def test_retirement_cas_cannot_mark_newer_wham_gate(m):
    key, _ = _account(m, "retirement-cas")
    now = int(time.time() * 1000)
    _wham(m, key, now - 1000)
    _save_headers(m, key, _LOW_HEADERS, now)
    stale = m["state_db"].quota_load(key)
    _wham(m, key, now + 1000)
    assert not m["state_db"].quota_retire_openai_wham_gate(key, stale)
    assert m["state_db"].quota_load(key)["codex_wham_gate_retired"] is False


@pytest.mark.parametrize("reason", ["user", "auth_error"])
def test_retiring_cached_gate_does_not_enable_manual_or_auth_disabled_account(m, reason):
    key, _ = _account(m, "retirement-manual")
    manager = m["oauth_manager"]
    now = int(time.time() * 1000)
    _wham(m, key, now - 1000)
    _save_headers(m, key, _LOW_HEADERS, now)
    account = manager.get_account(key)
    account.update(enabled=False, disabled_reason=reason)
    manager.evaluate_and_toggle_by_cached_quota(key)
    assert account["enabled"] is False
    assert account["disabled_reason"] == reason
