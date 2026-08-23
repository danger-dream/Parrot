"""xAI / Grok OAuth provider, channel, registry and TG entry tests."""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import asyncio


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, log_db, oauth_manager, state_db
    from src.channel import registry
    from src.channel.xai_oauth_channel import XAIOAuthChannel
    from src.oauth import xai as xai_provider
    from src.providers import registry as provider_registry
    from src.protocols.matrix import capabilities_for_channel
    from src.telegram import states, ui
    from src.telegram.menus import oauth_menu
    return {
        "config": config,
        "oauth_manager": oauth_manager,
        "state_db": state_db,
        "log_db": log_db,
        "registry": registry,
        "XAIOAuthChannel": XAIOAuthChannel,
        "xai_provider": xai_provider,
        "provider_registry": provider_registry,
        "capabilities_for_channel": capabilities_for_channel,
        "states": states,
        "ui": ui,
        "oauth_menu": oauth_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {"message_id": 123}}

    def by(self, method):
        return [d for m, d in self.calls if m == method]

    def last(self, method):
        items = self.by(method)
        return items[-1] if items else None


def _setup(m):
    m["state_db"].init()
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
        c["channels"] = []
        xai_cfg = dict(m["config"].DEFAULT_CONFIG.get("xaiOAuth") or {})
        xai_cfg["defaultModels"] = ["grok-4", "grok-code-fast-1"]
        c["xaiOAuth"] = xai_cfg
    m["config"].update(_reset)
    m["states"].clear_all()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _future_expired(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_xai_pkce_login_url_and_mock_token(m):
    _setup(m)
    p = m["xai_provider"]
    verifier, challenge = p.pkce_generate()
    assert len(verifier) == 128
    assert all(ch.isalnum() or ch in "-_" for ch in verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected

    url = p.build_login_url("CHALLENGE", "STATE")
    assert url.startswith("https://auth.x.ai/oauth/authorize?")
    assert "client_id=b1a00492-073a-47ea-816f-4c329264a828" in url
    assert "grok-cli%3Aaccess" in url
    assert "api%3Aaccess" in url
    assert "code_challenge_method=S256" in url
    assert "plan=generic" in url
    assert "referrer=parrot" in url

    tok = p.exchange_code_sync("mock-code", verifier)
    assert tok["access_token"].startswith("mock-xai-access-")
    assert tok["refresh_token"].startswith("mock-xai-refresh-")
    info = p.extract_user_info(p.decode_id_token(tok["id_token"]))
    assert info["email"].startswith("mock-xai-")
    assert info["subject"].startswith("mock-xai-sub-")


def test_xai_config_overrides_oauth_defaults(m):
    _setup(m)
    p = m["xai_provider"]

    def _override(c):
        x = c.setdefault("xaiOAuth", {})
        x["clientId"] = "custom-xai-client"
        x["redirectUri"] = "http://127.0.0.1:56121/custom-callback"
        x["scope"] = ["openid", "offline_access", "api:access"]
        x["apiBaseUrl"] = "https://api.x.ai/v1/custom"

    m["config"].update(_override)
    url = p.build_login_url("CHALLENGE", "STATE", nonce="NONCE")
    assert "client_id=custom-xai-client" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A56121%2Fcustom-callback" in url
    assert "scope=openid+offline_access+api%3Aaccess" in url
    tok = p.refresh_sync("refresh-token-0123456789", email="cfg@example.test", subject="cfg-sub")
    assert tok["base_url"] == "https://api.x.ai/v1/custom"
    assert tok["redirect_uri"] == "http://127.0.0.1:56121/custom-callback"
    assert tok["scope"] == "openid offline_access api:access"


def test_xai_account_key_registry_and_usage(m):
    _setup(m)
    om = m["oauth_manager"]
    p = m["xai_provider"]
    tok = p.refresh_sync("refresh-token-0123456789", email="grok@example.test", subject="sub-1")
    om.add_account({
        "provider": "xai",
        "email": tok["email"],
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expired": _future_expired(),
        "id_token": tok["id_token"],
        "subject": tok["subject"],
        "models": ["grok-4"],
    })

    accounts = om.list_accounts()
    assert len(accounts) == 1
    ak = om._canonical_key(accounts[0]) if hasattr(om, "_canonical_key") else None
    assert ak == "xai:sub-1"
    assert om.provider_of(ak) == "xai"
    assert om.account_key_to_email(ak) == "grok@example.test"
    usage = p.empty_usage()
    assert usage["xai"]["quota_supported"] is False

    # Legacy/imported xAI account without subject upgrades in-place when a later
    # id_token exposes the stable OIDC subject.
    _setup(m)
    om.add_account({
        "provider": "xai",
        "email": "legacy@example.test",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(),
    })
    assert om._canonical_key(om.list_accounts()[0]) == "xai:legacy@example.test"
    tok2 = p.refresh_sync("refresh-token-legacy", email="legacy@example.test", subject="legacy-sub")
    om.add_account({
        "provider": "xai",
        "email": tok2["email"],
        "access_token": tok2["access_token"],
        "refresh_token": tok2["refresh_token"],
        "expired": _future_expired(),
        "id_token": tok2["id_token"],
        "subject": tok2["subject"],
    })
    accounts = om.list_accounts()
    assert len(accounts) == 1
    assert om._canonical_key(accounts[0]) == "xai:legacy-sub"

    # Restore the account used by the channel/registry checks below.
    _setup(m)
    om.add_account({
        "provider": "xai",
        "email": tok["email"],
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expired": _future_expired(),
        "id_token": tok["id_token"],
        "subject": tok["subject"],
        "models": ["grok-4"],
    })

    m["registry"].rebuild_from_config()
    ch = m["registry"].get_channel(f"oauth:{ak}")
    assert isinstance(ch, m["XAIOAuthChannel"])
    assert m["registry"].get_channel("oauth:xai:grok@example.test:sub-1") is ch
    assert ch.provider == "xai"
    assert ch.supports_model("grok-4") == "grok-4"


def test_xai_cli_billing_usage_parse_and_quota_shape(m, monkeypatch):
    _setup(m)
    p = m["xai_provider"]
    om = m["oauth_manager"]

    def _disable_mock(c):
        c.setdefault("oauth", {})["mockMode"] = False
    m["config"].update(_disable_mock)
    monkeypatch.setattr(p, "_mock_mode_enabled", lambda: False)

    class Resp:
        def __init__(self, data):
            self._data = data
        def raise_for_status(self):
            return None
        def json(self):
            return self._data

    def fake_get(url, *, headers=None, timeout=None, proxy_purpose=None):
        assert headers["Authorization"] == "Bearer at-xai"
        assert headers["x-grok-client-version"] == "0.2.93"
        assert proxy_purpose == "oauth_xai"
        if "format=auto-topup" in url:
            return Resp({"config": {
                "monthlyLimit": {"val": 15000},
                "used": {"val": 2},
                "onDemandCap": {"val": 0},
                "billingPeriodStart": "2026-07-01T00:00:00+00:00",
                "billingPeriodEnd": "2026-08-01T00:00:00+00:00",
                "history": [{
                    "billingCycle": {"year": 2026, "month": 6},
                    "includedUsed": {"val": 1},
                    "onDemandUsed": {"val": 0},
                    "totalUsed": {"val": 1},
                }],
            }})
        if "format=credits" in url:
            return Resp({"config": {
                "creditUsagePercent": 70,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-08-06T06:54:28+00:00",
                    "end": "2026-08-13T06:54:28+00:00",
                },
                "onDemandUsed": {"val": 0},
                "prepaidBalance": {"val": 0},
                "isUnifiedBillingUser": True,
            }})
        if url.endswith("/user?include=subscription"):
            return Resp({
                "userId": "sub-1",
                "principalType": "User",
                "principalId": "sub-1",
                "hasGrokCodeAccess": True,
                "subscriptionTier": "GrokPro",
                "userBlockedReason": None,
                "teamBlockedReasons": [],
            })
        if url.endswith("/settings"):
            return Resp({
                "allow_access": True,
                "subscription_tier_display": "SuperGrok",
                "default_model": "grok-4-5",
                "compaction_mode": "segments",
                "flush_soft_threshold_tokens": 4000,
            })
        raise AssertionError(url)

    monkeypatch.setattr(p.network, "get_sync", fake_get)
    usage = p.fetch_cli_billing_usage_sync("at-xai")
    billing = usage["xai"]["billing"]
    assert billing["period_type"] == "USAGE_PERIOD_TYPE_WEEKLY"
    assert billing["used_percent"] == 70
    assert billing["remaining_percent"] == 30
    assert billing["period_start"] == "2026-08-06T06:54:28+00:00"
    assert billing["period_end"] == "2026-08-13T06:54:28+00:00"
    assert billing["auto_top_up"] == {"monthly_limit": 15000, "used": 2}
    assert usage["xai"]["user"]["subscription_tier"] == "GrokPro"
    assert usage["xai"]["settings"]["default_model"] == "grok-4-5"
    flat = om.flatten_usage(usage)
    assert flat["seven_day_util"] == 70
    assert flat["seven_day_reset"] == "2026-08-13T06:54:28+00:00"
    assert flat["thirty_day_util"] is None
    assert flat["thirty_day_reset"] is None
    assert flat["five_hour_util"] is None


def test_refresh_notice_uses_xai_official_billing(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "provider": "xai",
        "email": "notice@x.ai",
        "subject": "notice-sub",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
        "enabled": True,
    })
    ak = "xai:notice-sub"
    billed = om._build_refresh_notice(
        ak,
        {"thirty_day_util": None},
        usage={
            "xai": {
                "billing": {
                    "used_percent": 70.0,
                    "period_end": "2026-08-13T06:54:28+00:00",
                }
            }
        },
    )
    assert "官方额度" in billed
    assert "70.00%" in billed
    assert "本次未拉取到" not in billed
    assert "月度额度" not in billed
    assert "5h" not in billed

    unknown = om._build_refresh_notice(
        ak,
        {"thirty_day_util": None},
        usage={"xai": {"billing": {"used_percent": None}}},
    )
    assert "官方额度" not in unknown
    assert "📊" not in unknown
    assert "本次未拉取到" not in unknown

    failed = om._build_refresh_notice(ak, None)
    assert "官方额度" not in failed
    assert "📊" not in failed
    assert "获取失败" not in failed
    assert "本次未拉取到" not in failed


def test_xai_quota_resume_uses_fresh_billing_even_before_period_end(m):
    _setup(m)
    om = m["oauth_manager"]
    future = _future_expired(86400 * 10)
    om.add_account({
        "provider": "xai",
        "email": "grok@example.test",
        "subject": "sub-1",
        "access_token": "at-xai",
        "refresh_token": "rt-xai",
        "expired": _future_expired(),
        "enabled": False,
        "disabled_reason": "quota",
        "disabled_until": future,
    })
    usage = {
        "five_hour": {},
        "seven_day": {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "openai": {"thirty_day": {}},
        "seven_day": {"utilization": 10.0, "resets_at": future},
        "extra_usage": {"is_enabled": False},
        "xai": {"source": "cli-chat-proxy", "quota_supported": True},
    }
    result = om.evaluate_and_toggle_by_usage("xai:sub-1", usage, threshold=95, fresh=True)
    assert result["action"] == "resumed"
    acc = om.get_account("xai:sub-1")
    assert acc["enabled"] is True
    assert acc.get("disabled_reason") is None


def test_xai_weekly_quota_disables_until_official_reset(m):
    _setup(m)
    om = m["oauth_manager"]
    future = _future_expired(86400 * 5)
    om.add_account({
        "provider": "xai",
        "email": "weekly@example.test",
        "subject": "weekly-sub",
        "access_token": "at-xai",
        "refresh_token": "rt-xai",
        "expired": _future_expired(),
        "enabled": True,
    })
    usage = {
        "five_hour": {},
        "seven_day": {"utilization": 96.0, "resets_at": future},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "openai": {"thirty_day": {}},
        "extra_usage": {"is_enabled": False},
        "xai": {"source": "cli-chat-proxy", "quota_supported": True},
    }
    result = om.evaluate_and_toggle_by_usage(
        "xai:weekly-sub", usage, threshold=95, fresh=True,
    )
    assert result["action"] == "disabled"
    assert result["hit_windows"] == ["周额度"]
    assert result["disabled_until"] == future
    acc = om.get_account("xai:weekly-sub")
    assert acc["enabled"] is False
    assert acc["disabled_reason"] == "quota"
    assert acc["disabled_until"] == future


def test_xai_missing_credit_percent_does_not_fall_back_to_legacy_monthly(m, monkeypatch):
    _setup(m)
    p = m["xai_provider"]
    monkeypatch.setattr(p, "_mock_mode_enabled", lambda: False)

    def fake_json(_token, path, timeout=None):
        if "format=auto-topup" in path:
            return {"config": {"monthlyLimit": {"val": 0}, "used": {"val": 0}}}
        if "format=credits" in path:
            return {"config": {"currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-08-06T06:54:28+00:00",
                "end": "2026-08-13T06:54:28+00:00",
            }}}
        return {}

    monkeypatch.setattr(p, "_cli_get_json_sync", fake_json)
    usage = p.fetch_cli_billing_usage_sync("at-xai")
    billing = usage["xai"]["billing"]
    assert billing["used_percent"] == 0.0
    assert billing["remaining_percent"] == 100.0
    assert usage["seven_day"] == {
        "utilization": 0.0,
        "resets_at": "2026-08-13T06:54:28+00:00",
    }
    assert usage["openai"]["thirty_day"] == {}
    assert billing["auto_top_up"] == {"monthly_limit": 0, "used": 0}


def test_xai_weekly_official_block_and_local_monthly_label(m):
    _setup(m)
    account_key = "xai:render-sub"
    raw = {
        "xai": {
            "source": "cli-chat-proxy",
            "quota_supported": True,
            "billing": {
                "period_type": "USAGE_PERIOD_TYPE_WEEKLY",
                "used_percent": 70,
                "remaining_percent": 30,
                "period_start": "2026-08-06T06:54:28+00:00",
                "period_end": "2026-08-13T06:54:28+00:00",
            },
            "settings": {},
        }
    }
    m["state_db"].quota_save(account_key, {
        "fetched_at": m["state_db"].now_ms(),
        "seven_day_util": 70,
        "seven_day_reset": "2026-08-13T06:54:28+00:00",
        "raw_data": json.dumps(raw),
    }, email="render@example.test")

    summary = m["oauth_menu"]._format_xai_official_block(account_key, detail=False)
    detail = m["oauth_menu"]._format_xai_official_block(account_key, detail=True)
    local = m["oauth_menu"]._format_xai_spend_block(
        account_key, detail=False,
        month_stats={"input": 10, "output": 2, "cache_creation": 0, "cache_read": 0},
    )
    assert "周额度: 剩余 30.00% · 已用 70.00%" in summary
    assert "周额度: 剩余 <code>30.00%</code> · 已用 <code>70.00%</code>" in detail
    assert "0 / 0" not in summary + detail
    assert "月额度" not in summary + detail
    assert "本地月度" in local
    assert "本月经 Parrot" not in local


def test_xai_missing_weekly_percent_means_unused(m):
    _setup(m)
    account_key = "xai:unknown-sub"
    raw = {"xai": {"source": "cli-chat-proxy", "billing": {
        "period_type": "USAGE_PERIOD_TYPE_WEEKLY",
        "used_percent": None,
        "remaining_percent": None,
        "period_start": "2026-08-06T06:54:28+00:00",
        "period_end": "2026-08-13T06:54:28+00:00",
    }, "settings": {}}}
    m["state_db"].quota_save(account_key, {
        "fetched_at": m["state_db"].now_ms(),
        "raw_data": json.dumps(raw),
    }, email="unknown@example.test")
    summary = m["oauth_menu"]._format_xai_official_block(account_key, detail=False)
    text = m["oauth_menu"]._format_xai_official_block(account_key, detail=True)
    assert "周额度: 剩余 100.00% · 已用 0.00%" in summary
    assert "周额度: 剩余 <code>100.00%</code> · 已用 <code>0.00%</code>" in text
    assert "上游未返回额度百分比" not in summary + text
    assert "0 / 0" not in text
    assert "月额度" not in text


def test_xai_credits_fetch_error_stays_unknown(m, monkeypatch):
    _setup(m)
    p = m["xai_provider"]
    monkeypatch.setattr(p, "_mock_mode_enabled", lambda: False)

    def fake_json(_token, path, timeout=None):
        if "format=auto-topup" in path:
            return {"config": {"monthlyLimit": {"val": 0}, "used": {"val": 0}}}
        if "format=credits" in path:
            raise RuntimeError("credits down")
        return {}

    monkeypatch.setattr(p, "_cli_get_json_sync", fake_json)
    usage = p.fetch_cli_billing_usage_sync("at-xai")
    billing = usage["xai"]["billing"]
    assert billing["used_percent"] is None
    assert billing["remaining_percent"] is None
    assert usage["seven_day"] == {}
    assert "credits" in (usage["xai"].get("errors") or {})

    account_key = "xai:credits-error"
    m["state_db"].quota_save(account_key, {
        "fetched_at": m["state_db"].now_ms(),
        "raw_data": json.dumps(usage),
    }, email="error@example.test")
    text = m["oauth_menu"]._format_xai_official_block(account_key, detail=True)
    assert "上游未返回额度百分比" in text
    assert "已用 0.00%" not in text


def test_xai_channel_request_shape_and_provider_capabilities(m):
    _setup(m)
    account = {
        "provider": "xai",
        "email": "grok@example.test",
        "subject": "sub-1",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(),
        "models": ["grok-4"],
    }
    ch = m["XAIOAuthChannel"](account)

    adapter = m["provider_registry"].adapter_for_channel(ch)
    assert adapter.name == "xai-oauth"
    assert "tool_search" not in adapter.capabilities.native_state
    assert "namespace" not in adapter.capabilities.native_state
    assert "web_search" in adapter.capabilities.native_state
    assert "ws" not in adapter.capabilities.transports

    matrix_caps = m["capabilities_for_channel"](ch)
    assert "prompt_cache_key" in matrix_caps.native_state
    assert "tool_search" not in matrix_caps.native_state
    assert "namespace" not in matrix_caps.native_state
    assert "web_search" in matrix_caps.native_state
    assert "ws" not in matrix_caps.transports

    old_ensure = m["oauth_manager"].ensure_valid_token
    async def fake_ensure(account_key):
        assert account_key == "xai:sub-1"
        return "at-fresh"
    m["oauth_manager"].ensure_valid_token = fake_ensure
    try:
        req = asyncio_run(async_build(ch, {
            "model": "grok-4",
            "input": "hi",
            "stream": False,
            "prompt_cache_key": "session-1",
            "_api_key_name": "client-a",
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "previous_response_id": "",
            "stream_options": {"include_usage": False, "ignored": True},
            "metadata": {"probe_id": "should-be-dropped"},
            "service_tier": "auto",
        }))
    finally:
        m["oauth_manager"].ensure_valid_token = old_ensure

    assert req.url == "https://api.x.ai/v1/responses"
    assert req.headers["authorization"] == "Bearer at-fresh"
    assert req.headers["accept"] == "text/event-stream"
    assert req.headers["x-grok-conv-id"] != "session-1"
    payload = json.loads(req.body.decode("utf-8"))
    assert payload["model"] == "grok-4"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "previous_response_id" not in payload
    assert "metadata" not in payload
    assert "service_tier" not in payload
    assert payload["prompt_cache_key"] == "session-1"
    assert "tool_choice" not in payload
    assert "parallel_tool_calls" not in payload


def test_xai_channel_maps_anthropic_fast_to_priority(m):
    _setup(m)
    ch = m["XAIOAuthChannel"]({
        "provider": "xai",
        "email": "grok@example.test",
        "subject": "sub-1",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(),
        "models": ["grok-4"],
    })
    old_ensure = m["oauth_manager"].ensure_valid_token
    async def fake_ensure(account_key):
        return "at-fresh"
    m["oauth_manager"].ensure_valid_token = fake_ensure
    try:
        req = asyncio_run(async_build(ch, {
            "model": "grok-4",
            "messages": [{"role": "user", "content": "hi"}],
            "speed": "fast",
        }, ingress_protocol="anthropic"))
    finally:
        m["oauth_manager"].ensure_valid_token = old_ensure
    payload = json.loads(req.body.decode("utf-8"))
    assert payload["service_tier"] == "priority"


def test_xai_channel_keeps_native_web_search_and_normalizes_aliases(m):
    _setup(m)
    ch = m["XAIOAuthChannel"]({
        "provider": "xai",
        "email": "grok@example.test",
        "subject": "sub-1",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(),
        "models": ["grok-4"],
    })
    old_ensure = m["oauth_manager"].ensure_valid_token
    async def fake_ensure(account_key):
        return "at-fresh"
    m["oauth_manager"].ensure_valid_token = fake_ensure
    try:
        req = asyncio_run(async_build(ch, {
            "model": "grok-4",
            "input": "search latest xAI docs",
            "tools": [{
                "type": "web_search_preview",
                "blocked_domains": ["example.com"],
                "enable_image_search": True,
                "context_size": "high",
            }],
            "tool_choice": {"type": "web_search_preview"},
            "parallel_tool_calls": True,
        }))
    finally:
        m["oauth_manager"].ensure_valid_token = old_ensure
    payload = json.loads(req.body.decode("utf-8"))
    assert payload["tools"] == [{
        "type": "web_search",
        "excluded_domains": ["example.com"],
        "enable_image_search": True,
    }]
    assert payload["tool_choice"] == {"type": "web_search"}
    assert payload["parallel_tool_calls"] is True


def test_xai_cost_aggregation_from_sse_usage(m):
    _setup(m)
    log_db = m["log_db"]
    log_db.init()
    channel = "oauth:xai:cost-sub"
    request_id = "xai-cost-test-1"
    body = (
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"service_tier":"priority","usage":'
        '{"input_tokens":226,"input_tokens_details":{"cached_tokens":128},'
        '"output_tokens":49,"cost_in_usd_ticks":5540000}}}\n\n'
    )
    conn = log_db._get_conn()
    conn.execute(
        """INSERT INTO request_log
           (request_id, created_at, final_channel_key, final_channel_type,
            requested_model, final_model, status, http_status, is_stream,
            input_tokens, output_tokens, cache_read_tokens, total_time_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (request_id, datetime.now(timezone.utc).timestamp(), channel, "oauth", "grok-4.5", "grok-4.5",
         "success", 200, 1, 98, 49, 128, 1573),
    )
    conn.execute(
        "INSERT INTO request_detail (request_id, response_body) VALUES (?,?)",
        (request_id, body),
    )
    conn.commit()

    s = log_db.xai_cost_for_channel(channel, since_ts=0)
    assert s["cost_ticks"] == 5540000
    assert s["cost_usd"] == pytest.approx(0.000554)
    assert s["input"] == 98
    assert s["cache_read"] == 128
    assert s["output"] == 49
    assert s["service_tier_counts"] == {"priority": 1}

    text = m["oauth_menu"]._format_xai_spend_block(
        "xai:cost-sub", detail=True, month_stats=s,
    )
    assert "💵 本地计费: $0.00" in text
    assert "实际" not in text and "估算" not in text and "未计价" not in text
    assert "缓存 128 (56.6%)" in text
    assert "≈" not in text


def test_xai_cost_disabled_does_not_read_response_body(m):
    _setup(m)
    log_db = m["log_db"]
    log_db.init()
    channel = "oauth:xai:cost-disabled"
    conn = log_db._get_conn()
    conn.execute(
        """INSERT INTO request_log
           (request_id, created_at, final_channel_key, final_channel_type,
            requested_model, final_model, status, input_tokens, output_tokens)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "xai-cost-disabled",
            datetime.now(timezone.utc).timestamp(),
            channel,
            "oauth",
            "grok-4.5",
            "grok-4.5",
            "success",
            10,
            2,
        ),
    )
    conn.execute(
        "INSERT INTO request_detail (request_id, response_body) VALUES (?,?)",
        ("xai-cost-disabled", '{"usage":{"cost_in_usd_ticks":123}}'),
    )
    conn.commit()
    original_enabled = bool(
        (m["config"].get().get("pricing") or {}).get("enabled", True)
    )
    statements = []
    try:
        m["config"].update(
            lambda c: c.setdefault("pricing", {}).__setitem__("enabled", False)
        )
        conn.set_trace_callback(statements.append)
        stats = log_db.xai_cost_for_channel(channel, since_ts=0)
        conn.set_trace_callback(None)
        assert stats["cost_rows"] == 0
        assert not any("request_detail" in sql.lower() for sql in statements)
        text = m["oauth_menu"]._format_xai_spend_block(
            "xai:cost-disabled", detail=True
        )
        assert "本地计费: 已关闭" in text
    finally:
        conn.set_trace_callback(None)
        m["config"].update(
            lambda c: c.setdefault("pricing", {}).__setitem__(
                "enabled", original_enabled
            )
        )


def asyncio_run(coro):
    return asyncio.run(coro)


async def async_build(ch, body, *, ingress_protocol="responses"):
    return await ch.build_upstream_request(body, "grok-4", ingress_protocol=ingress_protocol)


def test_xai_channel_rejects_previous_response_id(m):
    _setup(m)
    ch = m["XAIOAuthChannel"]({
        "provider": "xai",
        "email": "grok@example.test",
        "subject": "sub-1",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(),
        "models": ["grok-4"],
    })
    with pytest.raises(ValueError) as exc:
        asyncio_run(async_build(ch, {"model": "grok-4", "input": "hi", "previous_response_id": "resp_1"}))
    assert "previous_response_id is not supported" in str(exc.value)


def test_xai_tg_login_and_refresh_token_entries(m):
    _setup(m)
    rec = _install_recorder(m)
    menu = m["oauth_menu"]
    states = m["states"]
    om = m["oauth_manager"]

    menu.on_add_menu(1, 10, "cb")
    rendered = rec.last("editMessageText") or {}
    assert "Grok 登录获取 Token" in rendered.get("text", "") or "Grok" in json.dumps(rendered, ensure_ascii=False)

    menu.on_login_xai_start(1, 10, "cb")
    st = states.get_state(1)
    assert st and st["action"] == "oa_xai_code"
    url_text = (rec.last("editMessageText") or {}).get("text", "")
    assert "auth.x.ai" in url_text
    state = (st.get("data") or {}).get("state")
    menu.on_login_xai_code_input(1, f"http://127.0.0.1:56121/callback?code=abc&state={state}")
    assert any(om.provider_of(acc) == "xai" for acc in om.list_accounts())
    assert "Grok OAuth 账户已" in (rec.last("sendMessage") or {}).get("text", "")

    rec.calls.clear()
    menu.on_set_rt_xai_start(1, 10, "cb")
    assert (states.get_state(1) or {}).get("action") == "oa_xai_rt"
    menu.on_set_rt_xai_input(1, "refresh_token: rt_xai_abcdefghijklmnopqrstuvwxyz")
    assert any(om.provider_of(acc) == "xai" for acc in om.list_accounts())
    assert "Grok OAuth 账户已" in (rec.last("sendMessage") or {}).get("text", "")
