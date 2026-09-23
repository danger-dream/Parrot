"""Independent-review regressions: prove usable recovery, not just enabled=True.
All credentials, requests and state are the existing isolated fake fixtures.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime

import httpx
import pytest

from src import config, cooldown, failover, oauth_manager as om, scheduler, state_db
from src.channel import registry
from src.oauth import claude_reset as cr
from src.tests.test_claude_reset import env, execute
from src.tests.test_codex_review_state_paths import env as openai_env


@pytest.fixture(autouse=True)
def isolated_cooldowns(monkeypatch):
    monkeypatch.setattr(cooldown, "_entries", {})


def quota_error(env, program="cedar_ember", *, model="claude-opus-5", message=None):
    window = "7d" if program == "cedar_ember" else "5h"
    field = "seven_day" if window == "7d" else "five_hour"
    stamp = int(datetime.fromisoformat(env.usage[field]["resets_at"].replace("Z", "+00:00")).timestamp())
    failover._maybe_auto_disable_by_headers(env.key, env.entry["email"], {
        f"anthropic-ratelimit-unified-{window}-utilization": "1.0",
        f"anthropic-ratelimit-unified-{window}-reset": str(stamp),
    }, provider="claude")
    detail = message or ('HTTP 429: ' + json.dumps({"error": {
        "type": "rate_limit_error", "message": f"Your {window} usage limit has been reached"}}))
    cooldown.record_error(f"oauth:{env.key}", model, detail, cooldown_until=stamp * 1000)
    return stamp * 1000


@pytest.mark.parametrize("program", cr.PROGRAMS)
def test_reset_restores_real_scheduler_and_removes_persisted_old_quota_error(env, monkeypatch, program):
    monkeypatch.setattr(config, "_reload_callbacks", [])
    registry.install_config_reload_hook()
    registry.rebuild_from_config()
    quota_error(env, program)
    assert not scheduler._filter_candidates("claude-opus-5")[0]
    result = execute(env, program)
    assert result["quota_action"]["action"] == "resumed"
    assert om.get_account(env.key)["enabled"] is True
    assert not cooldown.is_blocked(f"oauth:{env.key}", "claude-opus-5")
    assert state_db.error_load(f"oauth:{env.key}", "claude-opus-5") is None
    available = scheduler._filter_candidates("claude-opus-5")[0]
    assert [(ch.key, model) for ch, model in available] == [(f"oauth:{env.key}", "claude-opus-5")]
    assert state_db.get_store().close()
    state_db.init()
    assert state_db.error_load(f"oauth:{env.key}", "claude-opus-5") is None


@pytest.mark.parametrize("restriction", ["transport", "auth", "different_deadline", "fable"])
def test_reset_preserves_unrelated_or_still_unknown_restrictions(env, restriction):
    until = quota_error(env)
    detail = 'HTTP 429: {"error":{"type":"rate_limit_error","message":"limited"}}'
    model = "claude-fable-5.1" if restriction == "fable" else "other-model"
    if restriction == "transport":
        detail = "read_timeout"
    elif restriction == "auth":
        detail = 'HTTP 401: {"error":{"type":"authentication_error"}}'
    elif restriction == "different_deadline":
        until += 60000
    cooldown.record_error(f"oauth:{env.key}", model, detail, cooldown_until=until)
    result = execute(env)
    assert result["quota_action"]["action"] == "resumed"
    assert cooldown.is_blocked(f"oauth:{env.key}", model)
    assert not cooldown.is_blocked(f"oauth:{env.key}", "claude-opus-5")


def test_cooldown_delete_failure_cannot_claim_account_recovery(env, monkeypatch):
    quota_error(env)
    original = state_db.error_delete
    def fail(key, model=None):
        if model == "claude-opus-5":
            raise OSError("isolated write failure")
        return original(key, model)
    monkeypatch.setattr(state_db, "error_delete", fail)
    result = execute(env)
    assert result["quota_action"]["error_code"] == "runtime_state_clear_failed"
    assert om.get_account(env.key)["enabled"] is False
    assert cooldown.is_blocked(f"oauth:{env.key}", "claude-opus-5")


@pytest.mark.parametrize("partial", ["five_hour_only", "spend_control_only", "empty"])
def test_openai_partial_active_usage_cannot_erase_weekly_blocker(openai_env, monkeypatch, partial):
    m, key, _ = openai_env
    manager, upstream = m["oauth_manager"], m["openai_provider"]
    high = upstream.normalize_wham_usage({"rate_limit": {
        "primary_window": {"used_percent": 1, "limit_window_seconds": 18000, "reset_after_seconds": 3600},
        "secondary_window": {"used_percent": 100, "limit_window_seconds": 604800, "reset_after_seconds": 86400}}})
    state_db.quota_save(key, manager.flatten_usage(high))
    assert manager.evaluate_and_toggle_by_usage(key, high)["action"] == "disabled"
    raw = {"rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 18000}}}
    if partial == "spend_control_only":
        raw = {"spend_control": {"reached": False}}
    elif partial == "empty":
        raw = {}
    current = upstream.normalize_wham_usage(raw)
    async def fetch(*args, **kwargs):
        return copy.deepcopy(current)
    monkeypatch.setattr(manager, "fetch_usage_snapshot", fetch)
    monkeypatch.setattr(manager.notifier, "throttled_notify_event_sync", lambda *a, **k: None)
    asyncio.run(manager.quota_monitor_once())
    assert manager.get_account(key)["enabled"] is False
    assert state_db.quota_load(key)["seven_day_util"] == 100
    assert json.loads(state_db.quota_load(key)["raw_data"]) == current
    # No permanent lock: fresh evidence covering the original cap resumes it.
    current = copy.deepcopy(high)
    current["seven_day"]["utilization"] = 0
    out = asyncio.run(manager.quota_monitor_once())
    assert out[manager.account_key_to_email(key)] == "resumed"
    assert manager.get_account(key)["enabled"] is True


@pytest.mark.parametrize("partial", [{}, {"five_hour": {"utilization": 1}}])
def test_claude_background_and_direct_refresh_share_missing_window_protection(env, monkeypatch, partial):
    old = copy.deepcopy(env.usage)
    old["seven_day"]["utilization"] = 100
    state_db.quota_save(env.key, om.flatten_usage(old))
    om.set_disabled_by_quota(env.key, old["seven_day"]["resets_at"])
    current = partial
    async def fetch(*args, **kwargs):
        return copy.deepcopy(current)
    monkeypatch.setattr(om, "fetch_usage_snapshot", fetch)
    monkeypatch.setattr(om.notifier, "throttled_notify_event_sync", lambda *a, **k: None)
    asyncio.run(om.quota_monitor_once())
    assert om.get_account(env.key)["enabled"] is False
    assert state_db.quota_load(env.key)["seven_day_util"] == 100
    assert om.evaluate_and_toggle_by_usage(env.key, partial)["action"] == "quota_unknown_keep_disabled"
    current = env.usage
    asyncio.run(om.quota_monitor_once())
    assert om.get_account(env.key)["enabled"] is True


def test_claude_empty_without_cache_and_stale_low_cannot_resume(env):
    om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    assert om.evaluate_and_toggle_by_usage(env.key, {})["action"] == "quota_unknown_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(env.key, env.usage, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.get_account(env.key)["enabled"] is False


@pytest.mark.parametrize("age", [30, 601])
def test_new_confirmed_cedar_grant_is_not_blocked_by_old_unknown(env, age):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout("lost response"))
    first = execute(env)
    assert first["result"] == "unconfirmed"
    journal = cr._journal_key(om.account_state_key(om.get_account(env.key)), "cedar_ember", env.org)
    record = state_db.claude_reset_operation_load(journal)
    record["created_at"] = time.time() - age
    state_db.claude_reset_operation_save(journal, record)
    env.post_effect = None
    env.usage["cedar_ember"]["grants"][0]["id"] = "g_new"
    env.usage["cedar_ember"]["next_grant_id"] = "g_new"
    out = execute(env)
    assert out["result"] == "reset"
    assert len(env.posts) == 2
    assert env.posts[1][1]["json"]["grant_id"] == "g_new"
    assert env.posts[1][1]["json"]["request_id"] != first["request_id"]


def test_expired_same_cedar_grant_remains_uncertain_not_blindly_replayed(env):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout("lost response"))
    execute(env)
    journal = cr._journal_key(om.account_state_key(om.get_account(env.key)), "cedar_ember", env.org)
    record = state_db.claude_reset_operation_load(journal)
    record["created_at"] = time.time() - 601
    state_db.claude_reset_operation_save(journal, record)
    env.post_effect = None
    assert execute(env)["reason"] == "pending_requires_reconciliation"
    assert len(env.posts) == 1


@pytest.mark.parametrize("program", cr.PROGRAMS)
@pytest.mark.parametrize("concurrent", [False, True])
def test_reset_401_recovers_once_same_body_and_generation(env, monkeypatch, program, concurrent):
    refreshes, calls = [], []
    async def refresh(*args, **kwargs):
        assert kwargs["expected_state_key"] == om.account_state_key(om.get_account(env.key))
        refreshes.append(True)
        return "new-token"
    def post(url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            if concurrent:
                config.update(lambda c: c["oauthAccounts"][0].update(access_token="new-token"))
            return httpx.Response(401, json={"reason": "expired"}, request=httpx.Request("POST", url))
        return httpx.Response(200, json=env.response, request=httpx.Request("POST", url))
    monkeypatch.setattr(om, "force_refresh", refresh)
    monkeypatch.setattr(cr.network, "post_sync", post)
    result = execute(env, program)
    assert result["result"] == "reset"
    assert len(calls) == 2 and len(refreshes) == int(not concurrent)
    assert calls[0]["json"] == calls[1]["json"]
    assert calls[1]["headers"]["Authorization"] == "Bearer new-token"
    if program == "juniper_tide":
        assert calls[1]["json"] == {"program": program}


@pytest.mark.parametrize("code,count", [(401, 2), (403, 1), (429, 1), (500, 1)])
def test_reset_retry_is_bounded_and_not_used_for_other_errors(env, monkeypatch, code, count):
    calls, refreshes = [], []
    async def refresh(*args, **kwargs):
        refreshes.append(True)
        return "new-token"
    def post(url, **kwargs):
        calls.append(True)
        return httpx.Response(code, json={}, request=httpx.Request("POST", url))
    monkeypatch.setattr(om, "force_refresh", refresh)
    monkeypatch.setattr(cr.network, "post_sync", post)
    execute(env)
    assert len(calls) == count
    assert len(refreshes) == int(code == 401)


def test_account_replacement_during_401_refresh_never_receives_retry(env, monkeypatch):
    calls = []
    async def refresh(*args, **kwargs):
        om.delete_account(env.key)
        om.add_account(env.entry)
        return "replacement-token"
    def post(url, **kwargs):
        calls.append(True)
        return httpx.Response(401, json={}, request=httpx.Request("POST", url))
    monkeypatch.setattr(om, "force_refresh", refresh)
    monkeypatch.setattr(cr.network, "post_sync", post)
    execute(env)
    assert len(calls) == 1
