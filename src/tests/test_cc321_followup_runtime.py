"""Regression suite for CC v2.1.321 follow-up runtime capabilities (Package 2).

Covers:
1. Anthropic 7d_oi (seven_day_overage_included) passive headers, unified 429
   headers, retry-after-ms / unified-reset fallback, and Fable / Opus / Sonnet
   model-scoped cooldown isolation on Claude OAuth accounts.
2. Non-intrusive supplemental Gen-5 Claude pricing & metadata (including
   Haiku 5.5 >100k tiered pricing, Fable 5.2, dot-alias resolution, models.dev
   precedence, manual override precedence, legacy unpriced cache-write safety,
   and exclusion of internal mythos).
3. Downstream x-should-retry: false on non-retryable errors, Retry-After on
   terminal 429s, and bounded streaming Content-Encoding: gzip decompression.
"""

from __future__ import annotations

from ._isolation import isolate

isolate()

import copy
import gzip
import json
import time
import zlib
from decimal import Decimal

import httpx
import pytest

import server
from src import (
    apikey_limiter,
    config,
    cooldown,
    drain,
    failover,
    log_db,
    model_metadata,
    model_pricing,
    oauth_manager,
    request_body,
    scheduler,
    scorer,
    state_db,
    upstream,
)
from src.anthropic import rate_limit_headers as rlh
from src.channel import registry
from src.openai.channel.registration import register_factories as _openai_register_factories

AttemptResult = failover.AttemptResult


@pytest.fixture(autouse=True)
def _clean_runtime_state(monkeypatch):
    state_db.init()
    log_db.init()
    _openai_register_factories()
    for row in state_db.quota_load_all():
        state_db.quota_delete(row["account_key"])
    state_db.perf_delete()
    state_db.error_delete()
    cooldown.clear_all()
    with scorer._lock:
        scorer._stats.clear()
    failover._anthropic_snapshot_last.clear()
    failover._anthropic_snapshot_inflight.clear()
    apikey_limiter._slots.clear()
    drain.reset_for_tests()
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    yield
    cooldown.clear_all()
    with scorer._lock:
        scorer._stats.clear()
    apikey_limiter._slots.clear()
    drain.reset_for_tests()


# ─── 1. 7d_oi & Unified 429 Headers + Fable Cooldown Isolation ───────────────


def test_rate_limit_headers_7d_oi_and_unified_signal_parsing():
    patch = rlh.parse_rate_limit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.25",
        "anthropic-ratelimit-unified-7d-utilization": "0.40",
        "anthropic-ratelimit-unified-7d_oi-utilization": "0.875",
        "anthropic-ratelimit-unified-7d_oi-reset": "1794500000",
    })
    assert patch == {
        "five_hour_util": pytest.approx(25.0),
        "seven_day_util": pytest.approx(40.0),
        "fable_util": pytest.approx(87.5),
        "fable_reset": "2026-11-12T16:13:20Z",
    }

    # Milliseconds timestamp compatibility
    patch_ms = rlh.parse_rate_limit_headers({
        "Anthropic-Ratelimit-Unified-7d_oi-Reset": "1794500000000",
    })
    assert patch_ms == {"fable_reset": "2026-11-12T16:13:20Z"}

    # is_window_exceeded supports 5h, 7d, 7d_oi via surpassed, util>=1.0, and status=rejected
    assert rlh.is_window_exceeded(
        {"anthropic-ratelimit-unified-7d_oi-surpassed-threshold": "true"},
        "7d_oi",
    )
    assert rlh.is_window_exceeded(
        {"anthropic-ratelimit-unified-7d_oi-utilization": "1.0"},
        "7d_oi",
    )
    assert rlh.is_window_exceeded(
        {
            "anthropic-ratelimit-unified-7d_oi-utilization": "0.50",
            "anthropic-ratelimit-unified-7d_oi-status": "rejected",
        },
        "7d_oi",
    )
    assert not rlh.is_window_exceeded(
        {
            "anthropic-ratelimit-unified-7d_oi-utilization": "0.50",
            "anthropic-ratelimit-unified-7d_oi-status": "allowed_warning",
        },
        "7d_oi",
    )
    with pytest.raises(ValueError):
        rlh.is_window_exceeded({}, "invalid_window")

    sig = rlh.parse_unified_limit_signal({
        "anthropic-ratelimit-unified-status": "rejected",
        "anthropic-ratelimit-unified-reset": "1794500000",
        "anthropic-ratelimit-unified-representative-claim": "seven_day_overage_included",
        "anthropic-ratelimit-unified-overage-status": "rejected",
        "anthropic-ratelimit-unified-overage-disabled-reason": "out_of_credits",
        "anthropic-ratelimit-unified-overage-in-use": "false",
    })
    assert sig is not None
    assert sig["status"] == "rejected"
    assert sig["reset_epoch_s"] == 1794500000
    assert sig["reset_iso"] == "2026-11-12T16:13:20Z"
    assert sig["representative_claim"] == "seven_day_overage_included"
    assert sig["overage_status"] == "rejected"
    assert sig["overage_disabled_reason"] == "out_of_credits"
    assert sig["is_using_overage"] is False

    # Out-of-range reset timestamp is safely ignored
    sig_bad_ts = rlh.parse_unified_limit_signal({
        "anthropic-ratelimit-unified-status": "rejected",
        "anthropic-ratelimit-unified-reset": "42",
    })
    assert sig_bad_ts is not None
    assert sig_bad_ts["reset_epoch_s"] is None
    assert sig_bad_ts["reset_iso"] is None


def test_quota_patch_passive_and_fable_usage_block_seven_day_overage_included():
    state_db.quota_save(
        "claude:alice@example.com",
        {
            "five_hour_util": 12.0,
            "five_hour_reset": "2026-11-11T12:00:00Z",
            "seven_day_util": 34.0,
            "seven_day_reset": "2026-11-15T00:00:00Z",
            "sonnet_util": 20.0,
            "opus_util": 30.0,
            "extra_used": 15.0,
            "extra_limit": 100.0,
            "extra_util": 15.0,
            "raw_data": {"source": "active_poll"},
        },
        email="alice@example.com",
    )
    state_db.quota_patch_passive(
        "claude:alice@example.com",
        {"fable_util": 92.5, "fable_reset": "2026-11-14T00:00:00Z"},
    )
    rows = {r["account_key"]: r for r in state_db.quota_load_all()}
    row = rows["claude:alice@example.com"]
    assert row["fable_util"] == pytest.approx(92.5)
    assert row["fable_reset"] == "2026-11-14T00:00:00Z"
    # Existing columns must remain intact
    assert row["five_hour_util"] == pytest.approx(12.0)
    assert row["seven_day_util"] == pytest.approx(34.0)
    assert row["sonnet_util"] == pytest.approx(20.0)
    assert row["opus_util"] == pytest.approx(30.0)
    assert row["extra_util"] == pytest.approx(15.0)

    # Priority: explicit limits[] Fable > seven_day_fable > seven_day_overage_included
    usage_oi = {
        "seven_day_overage_included": {
            "utilization": 78.0,
            "resets_at": "2026-11-15T00:00:00Z",
        }
    }
    assert oauth_manager.fable_usage_block(usage_oi)["utilization"] == 78.0
    assert oauth_manager.fable_display_block(usage_oi)["utilization"] == 78.0

    usage_inactive_oi = {
        "seven_day_overage_included": {
            "utilization": 78.0,
            "resets_at": "2026-11-15T00:00:00Z",
            "is_active": False,
        }
    }
    assert oauth_manager.fable_usage_block(usage_inactive_oi) == {}
    assert oauth_manager.fable_display_block(usage_inactive_oi)["utilization"] == 78.0

    usage_both = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "scope": {"model": {"display_name": "Fable"}},
                "utilization": 41.0,
                "resets_at": "2026-11-16T00:00:00Z",
            }
        ],
        "seven_day_fable": {"utilization": 55.0},
        "seven_day_overage_included": {"utilization": 88.0},
    }
    assert oauth_manager.fable_usage_block(usage_both)["utilization"] == 41.0


def test_attach_retry_after_fallback_chain_for_anthropic_channels():
    from src.channel.oauth_channel import OAuthChannel

    ch = OAuthChannel({
        "provider": "claude",
        "email": "fable1@example.com",
        "account_key": "claude:fable1@example.com",
        "access_token": "tok-1",
        "models": ["claude-fable-5-2"],
    })

    # 1) retry-after-ms fallback when Retry-After is absent
    res_ms = AttemptResult(success=False, http_status=429, outcome="upstream_4xx")
    resp_ms = httpx.Response(429, headers={"retry-after-ms": "2500"})
    failover._attach_retry_after_from_response(res_ms, resp_ms, channel=ch)
    assert res_ms.retry_after_seconds == pytest.approx(2.5)
    assert res_ms.cooldown_until is not None

    # 2) anthropic-ratelimit-unified-reset fallback when both Retry-After & retry-after-ms are absent
    future_epoch = int(time.time()) + 120
    res_uni = AttemptResult(success=False, http_status=429, outcome="upstream_4xx")
    resp_uni = httpx.Response(
        429,
        headers={
            "anthropic-ratelimit-unified-status": "rejected",
            "anthropic-ratelimit-unified-reset": str(future_epoch),
            "anthropic-ratelimit-unified-representative-claim": "seven_day_overage_included",
        },
    )
    failover._attach_retry_after_from_response(res_uni, resp_uni, channel=ch)
    assert res_uni.retry_after_seconds == pytest.approx(60.0)
    assert res_uni.cooldown_until == future_epoch * 1000


def _configure_two_claude_oauth_accounts():
    def mutate(cfg):
        cfg["apiKeys"] = {"k1": {"key": "sk-test-key"}}
        cfg["channels"] = []
        cfg["oauthAccounts"] = [
            {
                "provider": "claude",
                "email": "acc1@example.com",
                "account_key": "claude:acc1@example.com",
                "access_token": "at-acc1",
                "refresh_token": "rt-acc1",
                "expires_at": int((time.time() + 7200) * 1000),
                "enabled": True,
                "weight": 100,
                "models": [
                    "claude-fable-5-2",
                    "claude-fable-5-1",
                    "claude-sonnet-4-6",
                    "claude-opus-4-7",
                ],
            },
            {
                "provider": "claude",
                "email": "acc2@example.com",
                "account_key": "claude:acc2@example.com",
                "access_token": "at-acc2",
                "refresh_token": "rt-acc2",
                "expires_at": int((time.time() + 7200) * 1000),
                "enabled": True,
                "weight": 10,
                "models": [
                    "claude-fable-5-2",
                    "claude-sonnet-4-6",
                    "claude-opus-4-7",
                ],
            },
        ]

    config.update(mutate)
    registry.rebuild_from_config()


async def test_claude_oauth_429_7d_oi_isolates_fable_without_disabling_account(monkeypatch):
    _configure_two_claude_oauth_accounts()
    reset_epoch = int(time.time()) + 3600
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth_hdr = req.headers.get("authorization", "")
        calls.append(auth_hdr)
        if "at-acc1" in auth_hdr:
            return httpx.Response(
                429,
                headers={
                    "content-type": "application/json",
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-reset": str(reset_epoch),
                    "anthropic-ratelimit-unified-representative-claim": "seven_day_overage_included",
                    "anthropic-ratelimit-unified-5h-utilization": "0.32",
                    "anthropic-ratelimit-unified-7d-utilization": "0.48",
                    "anthropic-ratelimit-unified-7d_oi-utilization": "1.0",
                    "anthropic-ratelimit-unified-7d_oi-reset": str(reset_epoch),
                    "anthropic-ratelimit-unified-7d_oi-status": "rejected",
                    "anthropic-ratelimit-unified-overage-status": "rejected",
                    "anthropic-ratelimit-unified-overage-disabled-reason": "out_of_credits",
                },
                json={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Usage credits are required to continue using Fable after reaching your weekly limit.",
                    },
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "msg_ok_acc2",
                "type": "message",
                "role": "assistant",
                "model": "claude-fable-5-2",
                "content": [{"type": "text", "text": "hello from acc2"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream.set_client(mock_client)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"x-api-key": "sk-test-key"},
                json={
                    "model": "claude-fable-5-2",
                    "max_tokens": 64,
                    "stream": False,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == "msg_ok_acc2"
        assert len(calls) == 2

        # Account 1 MUST NOT be globally disabled!
        acc1 = oauth_manager.get_account("claude:acc1@example.com")
        assert acc1 is not None
        assert not acc1.get("disabled_reason")

        ch_key = "oauth:claude:acc1@example.com"
        # Fable models on Account 1 are paused until reset_epoch * 1000
        assert cooldown.is_blocked(ch_key, "claude-fable-5-2")
        assert cooldown.is_blocked(ch_key, "claude-fable-5-1")
        # Sonnet & Opus on Account 1 remain completely available!
        assert not cooldown.is_blocked(ch_key, "claude-sonnet-4-6")
        assert not cooldown.is_blocked(ch_key, "claude-opus-4-7")
        # No health failure penalty on Account 1
        assert (ch_key, "claude-fable-5-2") not in scorer._stats

        # Passive quota cache immediately recorded 429 7d_oi snapshot
        rows = {r["account_key"]: r for r in state_db.quota_load_all()}
        assert rows["claude:acc1@example.com"]["fable_util"] == pytest.approx(100.0)
        assert rows["claude:acc1@example.com"]["five_hour_util"] == pytest.approx(32.0)

        # Scheduling Sonnet still includes Account 1 as primary candidate
        route_sonnet = scheduler.schedule(
            {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "ping"}]},
            api_key_name="k1",
            client_ip="127.0.0.1",
        )
        cand_keys = [ch.key for ch, _m in route_sonnet.candidates]
        assert ch_key in cand_keys
    finally:
        await mock_client.aclose()


async def test_claude_oauth_429_all_accounts_fable_exhausted_returns_no_retry_and_keeps_accounts_enabled(
    monkeypatch,
):
    _configure_two_claude_oauth_accounts()
    reset_epoch = int(time.time()) + 1800

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "content-type": "application/json",
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-reset": str(reset_epoch),
                "anthropic-ratelimit-unified-representative-claim": "seven_day_overage_included",
            },
            json={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "Usage credits are required to continue using Fable.",
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream.set_client(mock_client)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"x-api-key": "sk-test-key"},
                json={
                    "model": "claude-fable-5-2",
                    "max_tokens": 64,
                    "stream": False,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.headers.get("x-should-retry") == "false"
        payload = resp.json()
        assert payload["error"]["details"]["root_cause"]["classification"] == "quota_exhausted"
        assert payload["error"]["details"]["root_cause"]["retryable"] is False

        for ak in ("claude:acc1@example.com", "claude:acc2@example.com"):
            acc = oauth_manager.get_account(ak)
            assert acc is not None and not acc.get("disabled_reason")
            assert cooldown.is_blocked(f"oauth:{ak}", "claude-fable-5-2")
            assert not cooldown.is_blocked(f"oauth:{ak}", "claude-sonnet-4-6")
    finally:
        await mock_client.aclose()


async def test_claude_oauth_429_5h_exhausted_disables_whole_account_and_opus_claim_cools_opus_only(
    monkeypatch,
):
    _configure_two_claude_oauth_accounts()
    reset_epoch = int(time.time()) + 900

    # Part A: seven_day_opus claim cools only Opus on acc1, keeps acc1 enabled for Sonnet
    def handler_opus(req: httpx.Request) -> httpx.Response:
        if "at-acc1" in req.headers.get("authorization", ""):
            return httpx.Response(
                429,
                headers={
                    "content-type": "application/json",
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-reset": str(reset_epoch),
                    "anthropic-ratelimit-unified-representative-claim": "seven_day_opus",
                },
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "Opus weekly limit reached"},
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "msg_opus_acc2",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    mock_opus = httpx.AsyncClient(transport=httpx.MockTransport(handler_opus))
    upstream.set_client(mock_opus)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            r_opus = await client.post(
                "/v1/messages",
                headers={"x-api-key": "sk-test-key"},
                json={
                    "model": "claude-opus-4-7",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
        assert r_opus.status_code == 200
        acc1 = oauth_manager.get_account("claude:acc1@example.com")
        assert acc1 is not None and not acc1.get("disabled_reason")
        assert cooldown.is_blocked("oauth:claude:acc1@example.com", "claude-opus-4-7")
        assert not cooldown.is_blocked("oauth:claude:acc1@example.com", "claude-sonnet-4-6")
    finally:
        await mock_opus.aclose()

    # Part B: five_hour exhaustion disables the whole account with disabled_reason="quota"
    def handler_5h(req: httpx.Request) -> httpx.Response:
        if "at-acc1" in req.headers.get("authorization", ""):
            return httpx.Response(
                429,
                headers={
                    "content-type": "application/json",
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-reset": str(reset_epoch),
                    "anthropic-ratelimit-unified-representative-claim": "five_hour",
                    "anthropic-ratelimit-unified-5h-utilization": "1.0",
                    "anthropic-ratelimit-unified-5h-reset": str(reset_epoch),
                    "anthropic-ratelimit-unified-5h-status": "rejected",
                },
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "5h rate limit exceeded"},
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "msg_sonnet_acc2",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    mock_5h = httpx.AsyncClient(transport=httpx.MockTransport(handler_5h))
    upstream.set_client(mock_5h)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"x-api-key": "sk-test-key"},
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "test"}],
                },
            )
        assert resp.status_code == 200
        acc1 = oauth_manager.get_account("claude:acc1@example.com")
        assert acc1 is not None
        assert acc1.get("disabled_reason") == "quota"
    finally:
        await mock_5h.aclose()


# ─── 2. x-should-retry & Bounded Streaming Gzip Request Decoding ─────────────


def _configure_api_channel(*, models=("claude-sonnet-4-6", "gpt-5.4")):
    model_entries = [{"alias": m, "real": m} for m in models]

    def mutate(cfg):
        cfg["apiKeys"] = {
            "full": {"key": "sk-full"},
            "restricted": {"key": "sk-restricted", "allowedModels": ["claude-sonnet-4-6"]},
        }
        cfg["oauthAccounts"] = []
        cfg["channels"] = [
            {
                "name": "anth-up",
                "type": "api",
                "protocol": "anthropic",
                "baseUrl": "https://api.example.test",
                "apiKey": "up-key",
                "enabled": True,
                "models": copy.deepcopy(model_entries),
            },
            {
                "name": "oai-up",
                "type": "api",
                "protocol": "openai-chat",
                "baseUrl": "https://oai.example.test/v1",
                "apiKey": "up-oai",
                "enabled": True,
                "models": copy.deepcopy(model_entries),
            },
        ]

    config.update(mutate)
    registry.rebuild_from_config()


async def test_ingress_x_should_retry_headers_across_endpoints():
    _configure_api_channel()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        # 401 auth error -> x-should-retry: false
        r401_anth = await client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "messages": []})
        assert r401_anth.status_code == 401
        assert r401_anth.headers.get("x-should-retry") == "false"

        r401_chat = await client.post("/v1/chat/completions", json={"model": "gpt-5.4", "messages": []})
        assert r401_chat.status_code == 401
        assert r401_chat.headers.get("x-should-retry") == "false"

        # 400 invalid JSON -> x-should-retry: false
        r400 = await client.post(
            "/v1/messages",
            headers={"x-api-key": "sk-full", "content-type": "application/json"},
            content=b"{not-json",
        )
        assert r400.status_code == 400
        assert r400.headers.get("x-should-retry") == "false"

        # 403 disallowed model -> x-should-retry: false
        r403 = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-restricted"},
            json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r403.status_code == 403
        assert r403.headers.get("x-should-retry") == "false"

        # 404 model never supported -> x-should-retry: false
        r404 = await client.post(
            "/v1/messages",
            headers={"x-api-key": "sk-full"},
            json={"model": "never-configured-model", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r404.status_code == 404
        assert r404.headers.get("x-should-retry") == "false"

        # 503 all channels cooled down -> retryable! Must NOT set x-should-retry: false
        cooldown.record_quota_pause("api:anth-up", "claude-sonnet-4-6", "temp", cooldown_until=int(time.time() * 1000) + 60_000)
        cooldown.record_quota_pause("api:oai-up", "claude-sonnet-4-6", "temp", cooldown_until=int(time.time() * 1000) + 60_000)
        r503 = await client.post(
            "/v1/messages",
            headers={"x-api-key": "sk-full"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r503.status_code == 503
        assert "x-should-retry" not in r503.headers


@pytest.mark.parametrize(
    ("upstream_status", "upstream_headers", "upstream_body", "expect_status", "expect_no_retry", "expect_retry_after"),
    [
        (
            400,
            {},
            {"type": "error", "error": {"type": "invalid_request_error", "message": "messages.0.content: invalid"}},
            400,
            True,
            None,
        ),
        (
            401,
            {},
            {"type": "error", "error": {"type": "authentication_error", "message": "bad upstream key"}},
            401,
            True,
            None,
        ),
        (
            429,
            {"Retry-After": "17"},
            {"type": "error", "error": {"type": "rate_limit_error", "message": "too many requests"}},
            429,
            False,
            "17",
        ),
        (
            529,
            {},
            {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
            503,
            False,
            None,
        ),
    ],
)
async def test_failover_terminal_retry_headers_precision(
    monkeypatch,
    upstream_status,
    upstream_headers,
    upstream_body,
    expect_status,
    expect_no_retry,
    expect_retry_after,
):
    def mutate(cfg):
        cfg["apiKeys"] = {"full": {"key": "sk-full"}}
        cfg["oauthAccounts"] = []
        cfg["retryPolicy"] = {"transientRetries": 0}
        cfg["channels"] = [
            {
                "name": "only-anth",
                "type": "api",
                "protocol": "anthropic",
                "baseUrl": "https://api.example.test",
                "apiKey": "up-key",
                "enabled": True,
                "models": [{"alias": "claude-sonnet-4-6", "real": "claude-sonnet-4-6"}],
            }
        ]

    config.update(mutate)
    registry.rebuild_from_config()

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                upstream_status,
                headers={"content-type": "application/json", **upstream_headers},
                json=upstream_body,
            )
        )
    )
    upstream.set_client(mock_client)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/v1/messages",
                headers={"x-api-key": "sk-full"},
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "check"}],
                },
            )
        assert resp.status_code == expect_status
        if expect_no_retry:
            assert resp.headers.get("x-should-retry") == "false"
        else:
            assert "x-should-retry" not in resp.headers
        if expect_retry_after is not None:
            assert resp.headers.get("retry-after") == expect_retry_after
    finally:
        await mock_client.aclose()


async def test_bounded_gzip_request_body_across_messages_chat_and_responses(monkeypatch):
    def mutate(cfg):
        cfg["apiKeys"] = {"full": {"key": "sk-full"}}
        cfg["oauthAccounts"] = []
        cfg["channels"] = [
            {
                "name": "anth-up",
                "type": "api",
                "protocol": "anthropic",
                "baseUrl": "https://api.example.test",
                "apiKey": "up-key",
                "enabled": True,
                "models": [{"alias": "claude-sonnet-4-6", "real": "claude-sonnet-4-6"}],
            },
            {
                "name": "chat-up",
                "type": "api",
                "protocol": "openai-chat",
                "baseUrl": "https://oai.example.test/v1",
                "apiKey": "up-oai",
                "enabled": True,
                "models": [{"alias": "gpt-5.4-chat", "real": "gpt-5.4"}],
            },
            {
                "name": "resp-up",
                "type": "api",
                "protocol": "openai-responses",
                "baseUrl": "https://oai.example.test/v1",
                "apiKey": "up-oai",
                "enabled": True,
                "models": [{"alias": "gpt-5.4-resp", "real": "gpt-5.4"}],
            },
        ]

    config.update(mutate)
    registry.rebuild_from_config()

    def handler(req: httpx.Request) -> httpx.Response:
        # Upstream always receives cleanly decoded & re-encoded UTF-8 JSON (no client Content-Encoding leak)
        assert "content-encoding" not in {k.lower() for k in req.headers.keys()}
        sent = json.loads(req.content.decode("utf-8"))
        url = str(req.url)
        if url.endswith("/v1/messages"):
            return httpx.Response(
                200,
                json={
                    "id": "msg_gzip_ok",
                    "type": "message",
                    "role": "assistant",
                    "model": sent["model"],
                    "content": [{"type": "text", "text": "decoded-anth"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            )
        if url.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_gzip_ok",
                    "object": "chat.completion",
                    "model": sent["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "decoded-chat"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_gzip_ok",
                "object": "response",
                "status": "completed",
                "model": sent["model"],
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "decoded-resp"}]}],
                "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream.set_client(mock_client)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            # 1) Valid gzip on /v1/messages
            anth_gz = gzip.compress(
                json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "compressed hello"}],
                }).encode("utf-8")
            )
            r_anth = await client.post(
                "/v1/messages",
                headers={
                    "x-api-key": "sk-full",
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                content=anth_gz,
            )
            assert r_anth.status_code == 200
            assert r_anth.json()["id"] == "msg_gzip_ok"

            # 2) Valid x-gzip on /v1/chat/completions
            chat_gz = gzip.compress(
                json.dumps({
                    "model": "gpt-5.4-chat",
                    "stream": False,
                    "messages": [{"role": "user", "content": "compressed chat"}],
                }).encode("utf-8")
            )
            r_chat = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-full",
                    "content-type": "application/json",
                    "content-encoding": "x-gzip",
                },
                content=chat_gz,
            )
            assert r_chat.status_code == 200
            assert r_chat.json()["id"] == "chatcmpl_gzip_ok"

            # 3) Valid gzip on /v1/responses
            resp_gz = gzip.compress(
                json.dumps({
                    "model": "gpt-5.4-resp",
                    "stream": False,
                    "input": "compressed responses",
                }).encode("utf-8")
            )
            r_resp = await client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer sk-full",
                    "content-type": "application/json",
                    "content-encoding": "GZIP",
                },
                content=resp_gz,
            )
            assert r_resp.status_code == 200
            assert r_resp.json()["id"] == "resp_gzip_ok"

            # 4) Corrupt & truncated gzip -> 400 invalid_request_error + x-should-retry: false
            for bad_payload in (b"not-a-gzip-stream", anth_gz[:-6], b""):
                r_bad = await client.post(
                    "/v1/messages",
                    headers={
                        "x-api-key": "sk-full",
                        "content-type": "application/json",
                        "content-encoding": "gzip",
                    },
                    content=bad_payload,
                )
                assert r_bad.status_code == 400
                assert r_bad.headers.get("x-should-retry") == "false"
                assert r_bad.json()["error"]["type"] == "invalid_request_error"

            # 5) Unsupported encoding (br / gzip,gzip) -> 415 + x-should-retry: false
            for unsupported in ("br", "gzip, gzip"):
                r_br = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer sk-full",
                        "content-type": "application/json",
                        "content-encoding": unsupported,
                    },
                    content=b"\x1b\x00\x00",
                )
                assert r_br.status_code == 415
                assert r_br.headers.get("x-should-retry") == "false"
                assert "unsupported Content-Encoding" in r_br.json()["error"]["message"]

            # 6) Real >32 MiB decompressed gzip bomb (~34 KB compressed!) -> 413 immediately!
            comp = zlib.compressobj(level=1, method=zlib.DEFLATED, wbits=16 + zlib.MAX_WBITS)
            million_zeros = b"0" * (1024 * 1024)
            bomb_parts = [comp.compress(b'{"a":"')]
            for _ in range(33):  # 33 MiB > 32 MiB cap
                bomb_parts.append(comp.compress(million_zeros))
            bomb_parts.append(comp.compress(b'"}'))
            bomb_parts.append(comp.flush())
            bomb_wire = b"".join(bomb_parts)
            assert len(bomb_wire) < 200_000  # Tiny wire footprint, huge decompressed size

            r_bomb_anth = await client.post(
                "/v1/messages",
                headers={
                    "x-api-key": "sk-full",
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                content=bomb_wire,
            )
            assert r_bomb_anth.status_code == 413
            assert r_bomb_anth.headers.get("x-should-retry") == "false"
            assert "exceeds limit" in r_bomb_anth.json()["error"]["message"]

            r_bomb_oai = await client.post(
                "/v1/responses",
                headers={
                    "Authorization": "Bearer sk-full",
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                content=bomb_wire,
            )
            assert r_bomb_oai.status_code == 413
            assert r_bomb_oai.headers.get("x-should-retry") == "false"
            assert "exceeds limit" in r_bomb_oai.json()["error"]["message"]
    finally:
        await mock_client.aclose()


def test_bounded_gzip_decoder_stops_within_single_compressed_chunk():
    # Even a single compressed chunk that would expand to 10 MiB stops at max_bytes + 1
    big_compressed = gzip.compress(b"A" * (10 * 1024 * 1024), compresslevel=1)
    with pytest.raises(request_body.RequestBodyDecodeError) as exc_info:
        request_body.decompress_gzip_chunks([big_compressed], max_decompressed_bytes=4096)
    assert exc_info.value.status == 413
    assert exc_info.value.code == "request_too_large"
