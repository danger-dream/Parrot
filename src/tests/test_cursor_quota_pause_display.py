"""Cursor quota pauses are not failed requests; the pool guard stays enforced."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src import config, cooldown, oauth_manager, state_db
from src.telegram.menus import oauth_menu
from src.tests import test_cursor_oauth_integration as fixtures

ACCOUNT = "cursor:cursor-user-1"
CHANNEL = "oauth:" + ACCOUNT
FABLE = "claude-fable-5"
COMPOSER = "composer-2.5"
DISABLED = "gpt-5.5"


@pytest.fixture
def env():
    fixtures.setup_function(None)
    account = fixtures._install_account()
    account["cursor_disabled_models"] = [DISABLED]
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [account],
        "quotaMonitor": {**cfg.get("quotaMonitor", {}), "disableThresholdPercent": 100},
    }))
    reset = datetime.now(timezone.utc) + timedelta(days=27)
    usage = {
        "cursor": {
            "api_percent_used": 100.0, "auto_percent_used": 0.01,
            "billing_cycle_end": reset.isoformat(), "auto_bucket_models": [COMPOSER],
            "limit_cents": 40000, "total_spend_cents": 10018,
            "remaining_cents": 29982, "total_utilization": 25.045,
        },
        "openai": {"thirty_day": {"utilization": 25.045, "resets_at": reset.isoformat()}},
    }
    yield account, usage, int(reset.timestamp() * 1000)
    fixtures.teardown_function(None)


def pause(usage):
    return oauth_manager.evaluate_and_toggle_by_usage(ACCOUNT, usage, threshold=100, fresh=True)


def test_pool_pause_keeps_guard_and_does_not_count_failures(env):
    account, usage, deadline = env
    result = pause(usage)
    assert result["cooled_models"] == 2
    for model in (FABLE, DISABLED):
        assert cooldown.is_blocked(CHANNEL, model)
        saved = state_db.error_load(CHANNEL, model)
        assert saved["error_count"] == 0
        assert saved["cooldown_until"] == deadline
        assert cooldown.get_state(CHANNEL, model)["first_error_at"] is None
    assert not cooldown.is_blocked(CHANNEL, COMPOSER)
    assert oauth_manager.get_account(ACCOUNT)["enabled"] is True
    assert pause(usage)["cooled_models"] == 0
    assert state_db.error_load(CHANNEL, FABLE)["error_count"] == 0

    # The disabled model remains guarded even if subsequently enabled by the user.
    config.update(lambda cfg: cfg["oauthAccounts"][0].update({"cursor_disabled_models": []}))
    assert cooldown.is_blocked(CHANNEL, DISABLED)
    usage["cursor"]["api_percent_used"] = 20
    assert pause(usage)["recovered_models"] == 2
    assert not cooldown.is_blocked(CHANNEL, FABLE)


def test_pause_preserves_real_failure_count_and_ladder_time(env):
    _, usage, _ = env
    cooldown.record_error(CHANNEL, FABLE, "real request failure")
    old = cooldown.get_state(CHANNEL, FABLE)
    pause(usage)
    new = cooldown.get_state(CHANNEL, FABLE)
    assert old["error_count"] == new["error_count"] == 1
    assert old["first_error_at"] == new["first_error_at"]
    assert old["last_advance_at"] == new["last_advance_at"]
    assert state_db.error_load(CHANNEL, FABLE)["error_count"] == 1
    assert cooldown.is_blocked(CHANNEL, FABLE)


def test_zero_count_pause_survives_state_reload(env, monkeypatch):
    _, usage, _ = env
    pause(usage)
    monkeypatch.setattr(cooldown, "_initialized", False)
    monkeypatch.setattr(cooldown, "_entries", {})
    cooldown.init()
    assert cooldown.is_blocked(CHANNEL, FABLE)
    assert cooldown.get_state(CHANNEL, FABLE)["error_count"] == 0


def test_failed_pause_save_does_not_publish_memory_only_guard(env, monkeypatch):
    _, _, deadline = env
    old = cooldown.get_state(CHANNEL, FABLE)
    def fail(*args, **kwargs):
        raise RuntimeError("test write failure")
    monkeypatch.setattr(state_db, "error_save", fail)
    with pytest.raises(RuntimeError, match="test write failure"):
        cooldown.record_quota_pause(CHANNEL, FABLE, "quota", cooldown_until=deadline)
    assert cooldown.get_state(CHANNEL, FABLE) == old


def test_legacy_pause_is_presented_without_guessing_historical_counts(env):
    account, usage, deadline = env
    pause(usage)
    legacy = state_db.error_load(CHANNEL, FABLE)
    state_db.error_save(CHANNEL, FABLE, 4, deadline, legacy["last_error_message"])
    cooldown._initialized = False
    cooldown.init()
    faults, groups = oauth_menu._split_cursor_quota_pauses(account, cooldown.active_entries())
    assert faults == []
    assert sum(len(rows) for rows in groups.values()) == 1
    text = oauth_menu._format_cursor_quota_pauses(groups)
    assert "配额暂停（非模型故障）" in text and "Other Models / API" in text
    assert FABLE in text and DISABLED not in text
    assert "累计失败" not in text and "恢复时间" in text and "天" in text
    assert state_db.error_load(CHANNEL, FABLE)["error_count"] == 4


def test_detail_list_and_header_separate_quota_from_real_faults(env):
    account, usage, _ = env
    pause(usage)
    state_db.quota_save(ACCOUNT, oauth_manager.flatten_usage(usage), email=account["email"])
    snapshot = {"by_channel": {}, "by_apikey": {}}
    detail, _ = oauth_menu._detail_text_and_kb(
        ACCOUNT, refresh_quota=False, month_snapshot=snapshot, model_stats=[],
    )
    assert "配额暂停（非模型故障）" in detail
    assert "1 个已启用模型" in detail
    assert "2 个已启用模型 · 禁用 1" in detail
    assert "个可用模型" not in detail
    assert FABLE in detail and DISABLED not in detail
    assert "累计失败" not in detail and "冷却中的模型" not in detail
    assert "总剩余额度不代表每个模型都可用" in detail
    block = oauth_menu._format_account_block(account, month_snapshot=snapshot)
    assert "配额暂停 1 个已启用模型（非故障）" in block
    assert "🟠 冷却" not in block
    listing, _ = oauth_menu._list_text_and_kb(month_snapshot=snapshot)
    assert "⏸ 配额暂停 1" in listing and "⚠ 冷却" not in listing

    # Real model errors retain the original error count and cooldown section.
    cooldown.record_error(CHANNEL, COMPOSER, "actual 429", cooldown_until=int(time.time()*1000)+60000)
    detail, _ = oauth_menu._detail_text_and_kb(
        ACCOUNT, refresh_quota=False, month_snapshot=snapshot, model_stats=[],
    )
    assert "配额暂停（非模型故障）" in detail and "冷却中的模型" in detail
    assert "累计失败 1 次" in detail
    assert detail.index("composer-2.5</code> —") > detail.index("冷却中的模型")
    listing, _ = oauth_menu._list_text_and_kb(month_snapshot=snapshot)
    assert "⏸ 配额暂停 1" in listing and "⚠ 冷却 1" in listing


def test_other_provider_entries_are_untouched(env):
    _, _, deadline = env
    entry = {"channel_key": "oauth:claude:fixture", "model": FABLE,
             "cooldown_until": deadline, "error_count": 2, "last_error_message": "actual timeout"}
    faults, groups = oauth_menu._split_cursor_quota_pauses({"provider":"claude"}, [entry])
    assert faults == [entry] and groups == {}


def test_cursor_models_pool_pause_has_its_own_label(env):
    account, usage, _ = env
    usage["cursor"].update({"api_percent_used":20, "auto_percent_used":100})
    pause(usage)
    faults, groups = oauth_menu._split_cursor_quota_pauses(account, cooldown.active_entries())
    text = oauth_menu._format_cursor_quota_pauses(groups)
    assert faults == [] and "Cursor Models / Auto" in text and COMPOSER in text
    assert FABLE not in text
    assert not cooldown.is_blocked(CHANNEL, FABLE)
