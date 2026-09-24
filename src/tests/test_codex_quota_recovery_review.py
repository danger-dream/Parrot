"""Regression cases reproduced by the independent Codex recovery review."""
import time
from datetime import datetime

import pytest

from src.tests.test_openai_responses_ws import (
    _import_modules, _setup, _make_oauth_channel_for_failover, FakeWebSocket,
)
from src.tests.test_openai_oauth_quota import _low_wham
from src.openai.recovery import capture_error_advice
from src.protocols.runtime import AttemptResult
from src import oauth_manager
from src.oauth import openai as provider
from src.scheduler import ScheduleResult


def refusal(m, ch, reset, *, code="usage_limit_reached", family="codex"):
    result = AttemptResult(outcome="http_error", http_status=429)
    capture_error_advice(result, codex=True,
        headers={"x-codex-active-limit": family} if family else {},
        payload={"error": {"code": code, "resets_at": reset}})
    assert m["failover"]._apply_codex_error_policy(ch, "test-model", result)
    return result


def test_unknown_family_is_model_local_not_an_account_disable(m):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    refusal(m, ch, int(time.time()) + 600, family=None)
    assert oauth_manager.get_account(ch.account_key).get("disabled_reason") != "quota"
    assert m["cooldown"].is_blocked(ch.key, "test-model")
    assert not (m["scorer"].get_stats(ch.key, "test-model") or {}).get("total_requests", 0)


@pytest.mark.parametrize("separation", [0, 0.005])
def test_repeated_refusals_keep_latest_deadline_and_prevent_early_resume(m, monkeypatch, separation):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    now = int(time.time())
    clock = [now + 0.001]
    monkeypatch.setattr("src.openai.recovery.time.time", lambda: clock[0])
    for reset in (now + 600, now + 3600):
        refusal(m, ch, reset)
        clock[0] += separation
    acc = oauth_manager.get_account(ch.account_key)
    assert acc["quota_observation"]["limit_error"]["reset_ms"] == (now + 3600) * 1000
    assert datetime.fromisoformat(acc["disabled_until"].replace("Z", "+00:00")).timestamp() == now + 3600
    clock[0] = now + 601
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0], tz)
    monkeypatch.setattr(oauth_manager, "datetime", ClockDateTime)
    usage = _low_wham()
    m["state_db"].quota_save(ch.account_key, {
        **oauth_manager.flatten_usage(usage), "fetched_at": int(clock[0] * 1000),
    })
    decision = oauth_manager.evaluate_and_toggle_by_usage(ch.account_key, usage, threshold=95, fresh=True)
    assert decision["action"] == "still_over_quota"
    assert oauth_manager.get_account(ch.account_key)["enabled"] is False


@pytest.mark.parametrize("code,expected", [
    ("usage_limit_reached", "resumed"),
    ("credits_exhausted", "still_over_quota"),
])
def test_new_default_low_windows_supersede_usage_refusal_not_spend_cap(m, monkeypatch, code, expected):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    now = int(time.time())
    clock = [now + 0.001]
    monkeypatch.setattr("src.openai.recovery.time.time", lambda: clock[0])
    reset = now + 1800
    refusal(m, ch, reset, code=code)
    clock[0] += 1
    snapshot = provider.parse_rate_limit_headers({
        "x-codex-primary-used-percent": "0", "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-at": str(reset + 18000),
    })
    m["state_db"].quota_save_openai_snapshot(ch.account_key, snapshot)
    usage = _low_wham()
    m["state_db"].quota_save(ch.account_key, {
        **oauth_manager.flatten_usage(usage), "fetched_at": int(clock[0] * 1000),
    })
    assert oauth_manager._cached_openai_codex_quota_hit(ch.account_key, 95, usage=usage)["any_over"]
    snapshot = provider.parse_rate_limit_headers({
        "x-codex-secondary-used-percent": "0", "x-codex-secondary-window-minutes": "10080",
        "x-codex-secondary-reset-at": str(reset + 604800),
    })
    m["state_db"].quota_save_openai_snapshot(ch.account_key, snapshot)
    decision = oauth_manager.evaluate_and_toggle_by_usage(ch.account_key, usage, threshold=95, fresh=True)
    assert decision["action"] == expected


def test_reset_only_fragment_does_not_resurrect_previous_cycle_percent():
    now = int(time.time())
    old = [{"limit_id": "codex", "primary": {"used_percent": 100, "window_minutes": 1}}]
    fragment = provider.normalize_wham_usage({"rate_limit": {"primary_window": {
        "limit_window_seconds": 60, "reset_at": now + 60,
    }}})["openai"]["rate_limits"]
    merged = provider.merge_codex_rate_limits((old, (now - 600) * 1000), (fragment, now * 1000))
    assert merged[0]["primary"].get("used_percent") is None
    assert merged[0]["primary"]["reset_at"] == now + 60


@pytest.mark.asyncio
async def test_queued_ws_candidate_uses_same_quota_policy(m, monkeypatch):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    first = {"type": "response.create", "model": "test-model", "input": "hi"}
    ws = FakeWebSocket(first)
    await ws.accept()
    await ws.receive()
    body = {"model": "test-model", "input": "hi"}
    request_id = "review-queue-limit"
    m["log_db"].insert_pending(request_id, "1.2.3.4", "ws-key", "test-model", True,
                               1, 0, {}, body, ingress_protocol="responses_ws")
    async def acquire(candidates, timeout):
        return candidates[0]
    async def attempt(*args, **kwargs):
        result = m["responses_ws"]._WsAttemptResult(outcome="upstream_error_json",
            http_status=429, error_detail="usage limit reached", error_code="usage_limit_reached")
        capture_error_advice(result, codex=True, headers={"x-codex-active-limit": "codex"},
            payload={"error": {"code": "usage_limit_reached", "resets_at": int(time.time()) + 3600}})
        return result
    monkeypatch.setattr(m["concurrency"], "acquire_from_candidates", acquire)
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", attempt)
    await m["responses_ws"]._run_ws_failover(ws, first_obj=first,
        schedule_result=ScheduleResult(candidates=[], saturated=[(ch, "test-model")],
            fp_query=None, affinity_hit=False, client_key="client:1"),
        body=body, request_id=request_id, api_key_name="ws-key", client_ip="1.2.3.4",
        start_time=time.time(), start_monotonic=time.monotonic(), fp_query=None)
    assert oauth_manager.get_account(ch.account_key)["disabled_reason"] == "quota"
    assert not (m["scorer"].get_stats(ch.key, "test-model") or {}).get("total_requests", 0)
