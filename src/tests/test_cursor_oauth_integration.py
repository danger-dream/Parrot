from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from ._isolation import isolate

isolate()

from src import config, cooldown, model_metadata, notifier, oauth_manager, state_db  # noqa: E402
from src.channel import registry  # noqa: E402
from src.channel.compatibility import apply_reasoning_effort_capability  # noqa: E402
from src.channel.cursor_oauth_channel import CursorOAuthChannel  # noqa: E402
from src.cursor_bridge import agent_pb2  # noqa: E402
from src.cursor_bridge import catalog as cursor_catalog  # noqa: E402
from src.cursor_bridge import runtime as cursor_runtime  # noqa: E402
from src.cursor_bridge.models import CursorModel  # noqa: E402
from src.cursor_bridge.request_builder import build_run_request_bytes  # noqa: E402
from src.cursor_bridge.usage import (  # noqa: E402
    CursorPlanUsage,
    CursorSpendLimit,
    CursorUsage,
)
from src.oauth import cursor as cursor_provider  # noqa: E402
from src.openai.transform.guard import GuardError  # noqa: E402
from src.telegram import states, ui  # noqa: E402
from src.telegram.menus import load_balancing_menu, logs_menu, oauth_account_models_menu, oauth_menu  # noqa: E402
from src.transform import cc_mimicry  # noqa: E402


def _catalog() -> dict:
    return cursor_catalog.build_catalog([
        CursorModel(
            id="claude-fable-5",
            name="Claude Fable 5",
            reasoning=True,
            context_window=300_000,
            context_window_max_mode=1_000_000,
            max_tokens=64_000,
            supports_images=True,
            supports_max_mode=True,
            supports_agent=True,
            legacy_slugs=(
                "claude-fable-5-low",
                "claude-fable-5-medium",
                "claude-fable-5-thinking-medium",
                "claude-fable-5-thinking-high",
                "claude-fable-5-thinking-xhigh",
                "claude-fable-5-thinking-max",
            ),
            default_on=True,
        ),
        CursorModel(
            id="composer-2.5",
            name="Composer 2.5",
            reasoning=True,
            context_window=200_000,
            max_tokens=64_000,
            supports_images=False,
            supports_max_mode=True,
            supports_agent=True,
            legacy_slugs=("composer-2.5-fast",),
            default_on=True,
        ),
        CursorModel(
            id="gpt-5.5",
            name="GPT-5.5",
            reasoning=True,
            context_window=272_000,
            max_tokens=64_000,
            supports_images=True,
            supports_max_mode=True,
            supports_agent=True,
            legacy_slugs=(
                "gpt-5.5-low",
                "gpt-5.5-medium",
                "gpt-5.5-extra-high-fast",
            ),
        ),
    ], fetched_at="2026-08-18T00:00:00Z")


def _account() -> dict:
    catalog = _catalog()
    return {
        "email": "cursor-test@local",
        "label": "Cursor Test",
        "provider": "cursor",
        "type": "cursor",
        "subject": "cursor-user-1",
        "sub": "cursor-user-1",
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "expired": "2099-01-01T00:00:00Z",
        "enabled": True,
        "disabled_reason": None,
        "models": [item["id"] for item in catalog["models"]],
        "cursor_model_catalog": catalog,
        "plan_type": "Ultra",
    }


def _install_account() -> dict:
    account = _account()
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [account],
        "oauth": {"mockMode": True},
    }))
    return account


def setup_function(_function):
    cursor_runtime.stop()
    state_db.init()
    cooldown.init()
    cooldown.clear_all()
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [],
        "oauth": {"mockMode": True},
        "modelBindings": {"defaults": {}, "scoped": {}},
    }))


def teardown_function(_function):
    cursor_runtime.stop()
    states._states.clear()


def test_cursor_variant_resolution_uses_real_native_slug_shapes():
    records = {item["id"]: item for item in _catalog()["models"]}
    assert cursor_catalog.resolve_variant(
        records["claude-fable-5"], reasoning_effort="medium", thinking=True,
    ) == "claude-fable-5-thinking-medium"
    assert cursor_catalog.resolve_variant(
        records["claude-fable-5"], reasoning_effort="low", thinking=False,
    ) == "claude-fable-5-low"
    assert cursor_catalog.resolve_variant(
        records["claude-fable-5"], reasoning_effort="xhigh", thinking=True,
    ) == "claude-fable-5-thinking-xhigh"
    assert cursor_catalog.resolve_variant(
        records["claude-fable-5"], reasoning_effort="max", thinking=True,
    ) == "claude-fable-5-thinking-max"
    assert cursor_catalog.resolve_variant(
        records["composer-2.5"], fast=True,
    ) == "composer-2.5-fast"
    assert cursor_catalog.resolve_variant(
        records["gpt-5.5"], reasoning_effort="xhigh", fast=True,
    ) == "gpt-5.5-extra-high-fast"


def test_cursor_max_suffix_is_reasoning_effort_independent_from_max_context():
    record = next(item for item in _catalog()["models"] if item["id"] == "claude-fable-5")
    max_variant = next(item for item in record["variants"] if item["id"].endswith("-max"))
    metadata = cursor_catalog.metadata_from_record(record)

    assert max_variant["effort"] == "max"
    assert "max_mode" not in max_variant
    assert record["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert metadata["reasoningEfforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert metadata["contextWindow"] == 300_000
    assert metadata["contextWindowMaxMode"] == 1_000_000

    # Persisted catalogs are repaired from legacy_slugs without a refresh.
    stale = copy.deepcopy(record)
    stale["reasoning_efforts"] = ["low", "medium", "high", "xhigh"]
    for variant in stale["variants"]:
        if variant["id"].endswith("-max"):
            variant["effort"] = None
            variant["max_mode"] = True
    corrected = cursor_catalog.catalog_records({"schema": 1, "models": [stale]})[0]
    corrected_max = next(item for item in corrected["variants"] if item["id"].endswith("-max"))
    assert corrected["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert corrected_max["effort"] == "max"
    assert "max_mode" not in corrected_max

    # A canonical base that itself contains "max" must not invent an effort.
    codex = cursor_catalog.record_from_model(CursorModel(
        id="gpt-5.1-codex-max",
        name="GPT-5.1 Codex Max",
        reasoning=True,
        context_window=200_000,
        max_tokens=32_000,
        supports_images=False,
        supports_max_mode=True,
        legacy_slugs=("gpt-5.1-codex-max-high",),
    ))
    assert codex["reasoning_efforts"] == ["high"]

    unknown = cursor_catalog.record_from_model(CursorModel(
        id="unknown-cursor-model",
        name="Unknown Cursor model",
        reasoning=True,
        context_window=200_000,
        max_tokens=32_000,
        supports_images=False,
        supports_max_mode=True,
        legacy_slugs=("unknown-cursor-model-max",),
    ))
    payload = {"reasoning_effort": "max"}
    assert apply_reasoning_effort_capability(
        payload, unknown["reasoning_efforts"], protocol="openai-chat",
    ) is False
    assert payload["reasoning_effort"] == "max"
    assert cursor_catalog.resolve_variant(
        unknown, reasoning_effort="max", thinking=True,
    ) == "unknown-cursor-model-max"


def test_cursor_fable_5_1_exposes_and_routes_real_max_effort():
    record = cursor_catalog.record_from_model(CursorModel(
        id="claude-fable-5-1",
        name="Claude Fable 5.1",
        reasoning=True,
        context_window=300_000,
        context_window_max_mode=1_000_000,
        max_tokens=64_000,
        supports_images=True,
        supports_max_mode=True,
        legacy_slugs=(
            "claude-fable-5-1-low",
            "claude-fable-5-1-medium",
            "claude-fable-5-1-high",
            "claude-fable-5-1-xhigh",
            "claude-fable-5-1-max",
            "claude-fable-5-1-thinking-low",
            "claude-fable-5-1-thinking-medium",
            "claude-fable-5-1-thinking-high",
            "claude-fable-5-1-thinking-xhigh",
            "claude-fable-5-1-thinking-max",
        ),
    ))
    assert record["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert cursor_catalog.resolve_variant(
        record, reasoning_effort="max", thinking=False,
    ) == "claude-fable-5-1-max"
    assert cursor_catalog.resolve_variant(
        record, reasoning_effort="max", thinking=True,
    ) == "claude-fable-5-1-thinking-max"
    metadata = cursor_catalog.metadata_from_record(record)
    assert metadata["contextWindow"] == 300_000
    assert metadata["contextWindowMaxMode"] == 1_000_000


def test_cursor_models_dev_binding_keeps_cursor_transport_metadata_authoritative():
    account = _install_account()
    scope = "oauth:cursor:cursor-user-1"
    config.update(lambda cfg: cfg.update(modelBindings={
        "defaults": {},
        "scoped": {scope: {"claude-fable-5": {
            "target": "302ai/chatgpt-4o-latest", "source": "manual",
            "outboundModel": "claude-fable-5",
        }}},
    }))
    binding = model_metadata.resolve_binding(
        "claude-fable-5",
        scope_key=scope,
        outbound_model="claude-fable-5-thinking-medium",
    )
    assert binding is not None
    assert binding.authority == "models.dev" and binding.scope_key == scope
    assert binding.target == "302ai/chatgpt-4o-latest"
    assert binding.metadata["contextWindow"] == 300_000
    assert binding.metadata["contextWindowMaxMode"] == 1_000_000
    assert binding.metadata["maxOutputTokens"] == 64_000
    assert binding.metadata["vision"] is False
    assert binding.metadata["supportsImages"] is False
    assert binding.metadata["cursorUpstreamVision"] is True
    assert binding.metadata["reasoningEfforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert "claude-fable-5-thinking-medium" in binding.metadata["variants"]
    assert model_metadata.context_window(
        "claude-fable-5", scope_key=scope,
        outbound_model="claude-fable-5-thinking-medium", use_max_context=True,
    ) == 1_000_000

    result = model_metadata.auto_sync_metadata([
        model_metadata.ModelInventoryItem(
            scope, "oauth", account["label"], "claude-fable-5", "claude-fable-5",
        ),
    ])
    assert result["created"] == []
    assert result["updated"] == []
    assert result["unmatched"] == []


def test_cursor_channel_maps_chat_and_anthropic_controls(monkeypatch):
    account = _install_account()

    async def valid_token(_account_key):
        return account["access_token"]

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    channel = CursorOAuthChannel(account)

    chat_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "medium",
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="chat",
    ))
    chat_body = json.loads(chat_req.body)
    assert chat_body["model"] == "claude-fable-5-thinking-medium"
    assert chat_req.translator_ctx["cursor_client_model"] == "claude-fable-5"
    assert chat_req.translator_ctx["cursor_actual_model"] == "claude-fable-5-thinking-medium"
    assert chat_req.headers[cursor_runtime._ACCOUNT_HEADER] == "cursor:cursor-user-1"

    fast_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "service_tier": "priority",
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="chat",
    ))
    assert json.loads(fast_req.body)["model"] == "claude-fable-5-medium"

    anthropic_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "system": "You are helpful",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 1024,
            "thinking": {"type": "enabled", "budget_tokens": 8000},
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="anthropic",
    ))
    anthropic_body = json.loads(anthropic_req.body)
    assert anthropic_body["reasoning_effort"] == "medium"
    assert anthropic_body["model"] == "claude-fable-5-thinking-medium"
    assert anthropic_req.translator_ctx["response_translator"] == "anthropic_to_chat"

    explicit_max_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "maximum"}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="anthropic",
    ))
    explicit_max_body = json.loads(explicit_max_req.body)
    assert explicit_max_body["reasoning_effort"] == "max"
    assert explicit_max_body["model"] == "claude-fable-5-thinking-max"
    assert explicit_max_req.translator_ctx["cursor_actual_model"] == "claude-fable-5-thinking-max"

    explicit_xhigh_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "extra high"}],
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "xhigh"},
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="anthropic",
    ))
    explicit_xhigh_body = json.loads(explicit_xhigh_req.body)
    assert explicit_xhigh_body["reasoning_effort"] == "xhigh"
    assert explicit_xhigh_body["model"] == "claude-fable-5-thinking-xhigh"

    responses_req = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "instructions": "Be concise",
            "input": "hello",
            "reasoning": {"effort": "medium"},
            "stream": False,
        },
        "claude-fable-5",
        ingress_protocol="responses",
    ))
    responses_body = json.loads(responses_req.body)
    assert responses_body["model"] == "claude-fable-5-thinking-medium"
    assert responses_req.translator_ctx["response_translator"] == "responses_to_chat"


def test_cursor_channel_does_not_claim_image_transport():
    from src.protocols.matrix import capabilities_for_channel

    channel = CursorOAuthChannel(_install_account())
    caps = capabilities_for_channel(channel)
    assert caps.supports_images is False
    assert channel.cursor_metadata("claude-fable-5")["vision"] is False


def test_cursor_build_request_rejects_image_blocks(monkeypatch):
    account = _install_account()

    async def valid_token(_account_key):
        return account["access_token"]

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    channel = CursorOAuthChannel(account)

    with pytest.raises(GuardError) as exc:
        asyncio.run(channel.build_upstream_request(
            {
                "model": "claude-fable-5",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ]}],
            },
            "claude-fable-5",
            ingress_protocol="chat",
        ))
    assert exc.value.scope == "candidate"
    assert exc.value.status == 400
    assert "image input is not available" in exc.value.message


def test_cursor_usage_normalization_uses_cents_not_reported_total_percent():
    raw = CursorUsage(
        plan_name="Ultra",
        subscription_status="active",
        billing_cycle_end="1789574199000",
        plan_usage=CursorPlanUsage(
            total_spend_cents=572,
            remaining_cents=39_428,
            limit_cents=40_000,
            auto_percent_used=0.281,
            api_percent_used=0.02,
            total_percent_used=0.2288,
        ),
        spend_limit=CursorSpendLimit(
            individual_limit_cents=1000,
            individual_remaining_cents=1000,
            limit_type="user",
        ),
        auto_bucket_models=("composer-2.5",),
    )
    usage = cursor_provider.normalize_usage(raw)
    cursor = usage["cursor"]
    assert round(cursor["total_utilization"], 4) == 1.43
    assert cursor["reported_total_percent_used"] == 0.2288
    assert cursor["billing_cycle_end"] == "2026-09-16T15:56:39Z"
    assert cursor["auto_bucket_models"] == ["composer-2.5"]


def test_cursor_quota_cools_only_affected_pool_and_recovers():
    account = _install_account()
    state_db.init()
    cooldown.init()
    account_key = "cursor:cursor-user-1"
    reset = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = {
        "cursor": {
            "auto_percent_used": 96.0,
            "api_percent_used": 20.0,
            "billing_cycle_end": reset,
            "auto_bucket_models": ["composer-2.5"],
        },
        "openai": {"thirty_day": {"utilization": 30.0, "resets_at": reset}},
    }
    result = oauth_manager.evaluate_and_toggle_by_usage(account_key, usage, threshold=95)
    channel_key = f"oauth:{account_key}"
    assert result["action"] == "cursor_pool_cooldown"
    assert cooldown.is_blocked(channel_key, "composer-2.5")
    assert not cooldown.is_blocked(channel_key, "claude-fable-5")
    assert oauth_manager.get_account(account_key)["enabled"] is True

    usage["cursor"]["auto_percent_used"] = 10.0
    recovered = oauth_manager.evaluate_and_toggle_by_usage(account_key, usage, threshold=95, fresh=True)
    assert recovered["action"] == "cursor_pool_recovered"
    assert not cooldown.is_blocked(channel_key, "composer-2.5")


def test_cursor_web_profile_uses_synthesized_workos_cookie(monkeypatch):
    config.update(lambda cfg: cfg.setdefault("oauth", {}).__setitem__("mockMode", False))
    token = cursor_provider._mock_jwt("auth0|user_profile", int(time.time() * 1000) + 3600_000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cursor.com/api/auth/me"
        assert request.headers["cookie"] == (
            f"WorkosCursorSessionToken=user_profile%3A%3A{token}"
        )
        return httpx.Response(200, json={
            "id": "user_profile",
            "sub": "auth0|user_profile",
            "email": "real-cursor@example.com",
            "email_verified": True,
            "name": "Cursor User",
        })

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cursor_provider,
        "_http_client",
        lambda **_kwargs: httpx.Client(transport=transport),
    )
    profile = cursor_provider.fetch_profile_sync(token, account_key="cursor:auth0|user_profile")
    assert profile == {
        "id": "user_profile",
        "sub": "auth0|user_profile",
        "email": "real-cursor@example.com",
        "name": "Cursor User",
        "email_verified": True,
    }


def test_cursor_model_refresh_updates_real_profile_without_changing_subject(monkeypatch):
    _install_account()
    monkeypatch.setattr(cursor_provider, "fetch_model_catalog_sync", lambda _token: _catalog())
    monkeypatch.setattr(cursor_provider, "fetch_profile_sync", lambda *_args, **_kwargs: {
        "id": "cursor-web-user-1",
        "sub": "cursor-user-1",
        "email": "real-cursor@example.com",
        "name": "Cursor User",
        "email_verified": True,
    })

    result = oauth_manager.refresh_cursor_models_sync(
        "cursor:cursor-user-1", force=True, min_interval_seconds=0,
    )
    account = oauth_manager.get_account("cursor:cursor-user-1")
    assert result["profile_updated"] is True
    assert account["subject"] == "cursor-user-1"
    assert account["email"] == "real-cursor@example.com"
    assert account["label"] == "real-cursor@example.com"
    assert account["cursor_profile_name"] == "Cursor User"
    assert account["cursor_profile_id"] == "cursor-web-user-1"
    assert account["cursor_email_verified"] is True


def test_cursor_profile_refresh_survives_model_catalog_timeout(monkeypatch):
    _install_account()

    def model_timeout(_token):
        raise TimeoutError("AvailableModels timed out")

    monkeypatch.setattr(cursor_provider, "fetch_model_catalog_sync", model_timeout)
    monkeypatch.setattr(cursor_provider, "fetch_profile_sync", lambda *_args, **_kwargs: {
        "id": "cursor-web-user-1",
        "sub": "cursor-user-1",
        "email": "real-cursor@example.com",
        "name": "Cursor User",
        "email_verified": True,
    })

    result = oauth_manager.refresh_cursor_models_sync(
        "cursor:cursor-user-1", force=True, min_interval_seconds=0,
    )
    account = oauth_manager.get_account("cursor:cursor-user-1")
    assert result["action"] == "profile_updated"
    assert result["model_error"] == "TimeoutError"
    assert account["models"] == ["claude-fable-5", "composer-2.5", "gpt-5.5"]
    assert account["email"] == "real-cursor@example.com"
    assert account["cursor_profile_name"] == "Cursor User"


def test_cursor_relogin_profile_failure_preserves_verified_display_identity():
    account = _account()
    account.update({
        "email": "real-cursor@example.com",
        "label": "real-cursor@example.com",
        "cursor_profile_name": "Cursor User",
        "cursor_profile_id": "cursor-web-user-1",
        "cursor_email_verified": True,
    })
    oauth_manager.add_account(account)

    fallback = _account()
    oauth_manager.add_account(fallback)
    saved = oauth_manager.get_account("cursor:cursor-user-1")
    assert saved["email"] == "real-cursor@example.com"
    assert saved["label"] == "real-cursor@example.com"
    assert saved["cursor_profile_id"] == "cursor-web-user-1"


def test_cursor_retry_log_exposes_native_outbound_variant():
    binding_json = json.dumps({
        "schema": 1,
        "dispatch": {
            "client_visible_model": "claude-fable-5",
            "outbound_model_id": "claude-fable-5-thinking-medium",
        },
    }, separators=(",", ":"), sort_keys=True)
    assert logs_menu._attempt_outbound_model({"binding_json": binding_json}) == (
        "claude-fable-5-thinking-medium"
    )


def test_cursor_login_button_completes_and_saves_account_in_mock_mode(monkeypatch):
    edits: list[tuple[str, dict | None]] = []
    finished = threading.Event()
    monkeypatch.setattr(ui, "answer_cb", lambda *args, **kwargs: None)
    def edit(*args, **kwargs):
        edits.append((args[2], kwargs.get("reply_markup")))
        if "后台将静默重试" in args[2]:
            finished.set()
    monkeypatch.setattr(ui, "edit", edit)

    oauth_menu.on_login_cursor_start(321, 654, "cb-start")
    oauth_menu.on_login_cursor_done(321, 654, "cb-done")

    accounts = [
        acc for acc in oauth_manager.list_accounts()
        if oauth_manager.provider_of(acc) == "cursor"
    ]
    assert len(accounts) == 1
    assert accounts[0]["subject"] == "cursor-mock-user"
    assert accounts[0]["email"] == "cursor-mock@example.test"
    assert accounts[0]["label"] == "cursor-mock@example.test"
    assert accounts[0]["cursor_profile_name"] == "Cursor Mock"
    # mockMode prohibits every upstream model request.  The identity is saved
    # first with an empty catalog and the shared UI worker reports fallback;
    # production discovery follows the same path with mocked provider calls in
    # the dedicated model-sync tests.
    assert accounts[0]["models"] == []
    assert accounts[0]["cursor_model_catalog"] == {}
    assert states.get_state(321) is None
    assert any("正在同步模型，请稍候" in text for text, _ in edits)
    assert finished.wait(1)
    assert "后台将静默重试" in edits[-1][0]


def test_cursor_quota_updated_at_is_detail_only(monkeypatch):
    row = {
        "fetched_at": 1_787_046_392_000,
        "raw_data": json.dumps({
            "cursor": {
                "limit_cents": 40_000,
                "remaining_cents": 39_330,
                "total_spend_cents": 670,
                "total_utilization": 1.675,
                "billing_cycle_end": "2026-09-16T15:56:39Z",
            },
        }),
    }
    monkeypatch.setattr(state_db, "quota_load", lambda _account_key: row)
    list_block = oauth_menu._format_cursor_usage_block(
        "cursor:cursor-user-1", detail=False,
    )
    detail_block = oauth_menu._format_cursor_usage_block(
        "cursor:cursor-user-1", detail=True,
    )
    assert "更新于" not in list_block
    assert "更新于" in detail_block


def test_cursor_local_monthly_stats_show_unpriced_instead_of_false_zero():
    account = _install_account()
    account_key = "cursor:cursor-user-1"
    channel_key = f"oauth:{account_key}"
    metrics = {
        "total": 2,
        "success_count": 2,
        "error_count": 0,
        "input": 1_000,
        "output": 200,
        "cache_creation": 0,
        "cache_read": 500,
        "avg_tps": 20.0,
        "max_tps": 30.0,
        "min_tps": 10.0,
        "cost_ticks": 0,
        "costed_success": 0,
        "unpriced_success": 2,
    }
    snapshot = {"by_channel": {channel_key: metrics}}

    list_text = oauth_menu._format_account_block(account, month_snapshot=snapshot)
    assert "💎 本地自然月:" in list_text
    assert "⚡ TPS:" in list_text
    assert "💵 未计价（Cursor 官方账单见上方）" not in list_text
    assert "💵 $0.00" not in list_text

    detail_text = oauth_menu._format_month_stats_block(
        account_key,
        month_snapshot=snapshot,
        by_model=[{"final_model": "composer-2.5", **metrics}],
    )
    assert "累计金额：未计价（Cursor 官方账单见上方）" in detail_text
    assert "    累计金额：未计价" in detail_text
    assert "$0.00" not in detail_text

    reconciled = {
        **metrics,
        "cost_ticks": 1_234_567_890,
        "actual_cost_ticks": 1_234_567_890,
        "actual_costed_success": 2,
        "costed_success": 2,
        "unpriced_success": 0,
    }
    actual_snapshot = {"by_channel": {channel_key: reconciled}}
    actual_list = oauth_menu._format_account_block(
        account, month_snapshot=actual_snapshot,
    )
    assert "💵 自然月 $0.12" in actual_list
    assert "Cursor 官方事件" not in actual_list
    actual_detail = oauth_menu._format_month_stats_block(
        account_key,
        month_snapshot=actual_snapshot,
        by_model=[{"final_model": "composer-2.5", **reconciled}],
    )
    assert "累计金额：$0.12" in actual_detail
    assert "Cursor 官方事件" not in actual_detail

    mixed = {**reconciled, "unpriced_success": 1}
    assert "另 1 次未计价" in oauth_menu._format_cursor_local_cost(mixed)


def test_cursor_telegram_badge_uses_custom_emoji():
    emoji_id = "6062261319426390107"
    assert config.get()["telegramUi"]["providerCustomEmoji"]["cursor"] == emoji_id
    assert ui.provider_custom_emoji_id("cursor") == emoji_id
    assert f'emoji-id="{emoji_id}"' in ui.provider_tag("cursor")
    assert f'emoji-id="{emoji_id}"' in notifier.provider_tag("cursor")


def test_cursor_login_menu_has_url_done_and_cancel_buttons(monkeypatch):
    captured: dict = {}

    class Params:
        uuid = "login-uuid"
        verifier = "verifier"
        login_url = "https://cursor.com/loginDeepControl?uuid=login-uuid"

    monkeypatch.setattr(cursor_provider, "generate_login", lambda: Params())
    monkeypatch.setattr(ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui, "edit", lambda *args, **kwargs: captured.update({
        "text": args[2], "reply_markup": kwargs.get("reply_markup"),
    }))

    oauth_menu.on_login_cursor_start(123, 456, "cb")
    state = states.get_state(123)
    assert state and state["action"] == "oa_cursor_login"
    buttons = [button for row in captured["reply_markup"]["inline_keyboard"] for button in row]
    assert any(button.get("url", "").startswith("https://cursor.com/") for button in buttons)
    assert any(button.get("callback_data") == "oa:login:cursor:done" for button in buttons)
    assert any(button.get("callback_data") == "oa:add" for button in buttons)


def test_cursor_max_context_default_persists_and_survives_relogin():
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    # Every account model with a distinct Max Context tier defaults on.
    assert oauth_manager.cursor_max_context_default(account_key, "claude-fable-5")

    assert oauth_manager.set_cursor_max_context_default(
        account_key, "claude-fable-5", False,
    ) is False
    assert not oauth_manager.cursor_max_context_default(account_key, "claude-fable-5")
    assert oauth_manager.cursor_max_context_disabled_models(account_key) == {
        "claude-fable-5",
    }
    with pytest.raises(ValueError, match="no separate Max Context"):
        oauth_manager.set_cursor_max_context_default(account_key, "composer-2.5", True)

    # Re-login payload omits local UI preferences; the disabled exception survives.
    oauth_manager.add_account(_account())
    assert not oauth_manager.cursor_max_context_default(account_key, "claude-fable-5")
    assert oauth_manager.set_cursor_max_context_default(
        account_key, "claude-fable-5", True,
    ) is True
    assert oauth_manager.cursor_max_context_default(account_key, "claude-fable-5")
    assert oauth_manager.cursor_max_context_disabled_models(account_key) == set()


def test_cursor_max_context_tri_state_applies_to_all_three_ingress(monkeypatch):
    account = _account()
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [account],
        "oauth": {"mockMode": True},
    }))

    async def valid_token(_account_key):
        return account["access_token"]

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    channel = CursorOAuthChannel(account)
    cases = (
        ("chat", {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        }),
        ("responses", {
            "model": "claude-fable-5",
            "input": "hello",
            "stream": True,
        }),
        ("anthropic", {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "stream": True,
        }),
    )
    for ingress, body in cases:
        request = asyncio.run(channel.build_upstream_request(
            body, "claude-fable-5", ingress_protocol=ingress,
        ))
        assert json.loads(request.body)["cursor_long_context"] is True
        assert request.translator_ctx["cursor_long_context"] is True

    explicit_off = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY: False,
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="chat",
    ))
    assert "cursor_long_context" not in json.loads(explicit_off.body)
    assert explicit_off.translator_ctx["cursor_long_context"] is False

    explicit_on = asyncio.run(channel.build_upstream_request(
        {
            "model": "claude-fable-5",
            "input": "hello",
            cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY: True,
            "stream": True,
        },
        "claude-fable-5",
        ingress_protocol="responses",
    ))
    assert json.loads(explicit_on.body)["cursor_long_context"] is True

    with pytest.raises(GuardError, match="does not expose a separate Max Context tier"):
        asyncio.run(channel.build_upstream_request(
            {
                "model": "composer-2.5",
                "messages": [{"role": "user", "content": "hello"}],
                cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY: True,
                "stream": True,
            },
            "composer-2.5",
            ingress_protocol="chat",
        ))


def test_cursor_long_context_reaches_requested_model_protobuf():
    raw = build_run_request_bytes(
        model_id="claude-fable-5-thinking-high",
        system_prompt="",
        user_text="hello",
        turns=[],
        conversation_id="conv-max-context",
        checkpoint=None,
        mcp_tools=[],
        long_context=True,
    )
    message = agent_pb2.AgentClientMessage()
    message.ParseFromString(raw)
    requested = message.run_request.requested_model
    assert requested.model_id == "claude-fable-5-thinking-high"
    assert requested.max_mode is True
    assert message.run_request.model_details.max_mode is True
    assert [(item.id, item.value) for item in requested.parameters] == [
        ("long_context", "true"),
    ]


def test_cursor_builder_keeps_max_context_independent_from_max_effort():
    effort_only = build_run_request_bytes(
        model_id="claude-fable-5-thinking-max",
        system_prompt="",
        user_text="hello",
        turns=[],
        conversation_id="conv-max-effort",
        checkpoint=None,
        mcp_tools=[],
        long_context=False,
    )
    message = agent_pb2.AgentClientMessage()
    message.ParseFromString(effort_only)
    run = message.run_request
    assert run.requested_model.model_id == "claude-fable-5-thinking-max"
    assert run.requested_model.max_mode is False
    assert run.model_details.max_mode is False
    assert list(run.requested_model.parameters) == []

    effort_and_context = build_run_request_bytes(
        model_id="claude-fable-5-thinking-max",
        system_prompt="",
        user_text="hello",
        turns=[],
        conversation_id="conv-max-effort-and-context",
        checkpoint=None,
        mcp_tools=[],
        long_context=True,
    )
    message.ParseFromString(effort_and_context)
    run = message.run_request
    assert run.requested_model.model_id == "claude-fable-5-thinking-max"
    assert run.requested_model.max_mode is True
    assert run.model_details.max_mode is True
    assert [(item.id, item.value) for item in run.requested_model.parameters] == [
        ("long_context", "true"),
    ]


def test_cursor_context_markers_and_openai_ingress_normalization():
    from src.openai import handler as openai_handler

    assert cc_mimicry.strip_context_1m_model_marker(
        "claude-fable-5~1000000"
    ) == "claude-fable-5"
    assert cc_mimicry.request_context_1m_override(
        {"model": "claude-fable-5~1000000"},
        original_model="claude-fable-5~1000000",
    ) is True
    assert cc_mimicry.request_context_1m_override(
        {"model": "claude-fable-5", "long_context": False},
        original_model="claude-fable-5",
    ) is False
    assert cc_mimicry.request_context_1m_override(
        {"model": "claude-fable-5"},
        original_model="claude-fable-5",
    ) is None

    for ingress_line in ("openai-chat", "openai-responses"):
        body = {"model": "claude-fable-5~1000000"}
        assert openai_handler._normalize_model_context_controls(
            body, ingress_line,
        ) == "claude-fable-5"
        assert body["model"] == "claude-fable-5"
        assert body[cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY] is True

        disabled = {"model": "claude-fable-5", "long_context": False}
        openai_handler._normalize_model_context_controls(disabled, ingress_line)
        assert disabled[cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY] is False


def test_cursor_max_context_metadata_and_preflight_use_one_million(monkeypatch):
    import server

    account = _install_account()
    scope = "oauth:cursor:cursor-user-1"
    normal_limit = model_metadata.safe_prompt_limit(
        "claude-fable-5", scope_key=scope, outbound_model="claude-fable-5",
    )
    max_limit = model_metadata.safe_prompt_limit(
        "claude-fable-5",
        scope_key=scope,
        outbound_model="claude-fable-5",
        use_max_context=True,
    )
    assert normal_limit == 188_800
    assert max_limit == 748_800
    assert model_metadata.context_window(
        "claude-fable-5",
        scope_key=scope,
        outbound_model="claude-fable-5",
        use_max_context=True,
    ) == 1_000_000

    channel = CursorOAuthChannel(account)
    route = SimpleNamespace(
        candidates=[(channel, "claude-fable-5")], saturated=[],
    )
    monkeypatch.setattr(
        server.token_counter, "count_request_tokens", lambda *_args, **_kwargs: 200_000,
    )
    base = {
        "model": "claude-fable-5",
        "_client_visible_model": "claude-fable-5",
        "messages": [{"role": "user", "content": "large prompt"}],
    }
    assert server._anthropic_to_openai_context_preflight(
        {**base, cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY: False}, route,
    ) is not None
    assert server._anthropic_to_openai_context_preflight(
        {**base, cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY: True}, route,
    ) is None


def test_cursor_new_entry_and_unified_rich_detail_keep_max_context():
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    _account_text, account_kb = oauth_menu._detail_text_and_kb(account_key, refresh_quota=False)
    manage = next(button for row in account_kb["inline_keyboard"] for button in row if button["text"] == "🧬 管理模型")
    assert manage["callback_data"].startswith("oam:open:")

    text, kb = oauth_account_models_menu.render(account_key)
    assert "Cursor 模型目录" not in text and "刷新额度与模型" not in text
    assert "<code>claude-fable-5</code>" in text
    assert "上下文：1.0M（Max Context 默认） · 🧠" in text
    assert "思考档位：low、medium、high、xhigh、max" in text
    detail, detail_kb = oauth_account_models_menu._detail_render(
        account_key, "claude-fable-5", model_page=1, account_page=1, filter_key="all",
    )
    assert "完整 ID: <code>claude-fable-5</code>" in detail
    assert "Max Context: <code>1.0M</code>" in detail
    labels = [button["text"] for row in detail_kb["inline_keyboard"] for button in row]
    assert "🚫 停用模型" in labels and "关闭 Max Context" in labels
    oauth_manager.set_account_model_disabled(account_key, "claude-fable-5", True)
    assert oauth_manager.cursor_disabled_models(account_key) == {"claude-fable-5"}
    oauth_manager.set_cursor_max_context_default(account_key, "claude-fable-5", False)
    assert not oauth_manager.cursor_max_context_default(account_key, "claude-fable-5")


def test_cursor_binding_cannot_override_native_context_or_max_context_button(monkeypatch):
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    model = "claude-fable-5"
    scope = f"oauth:{account_key}"
    config.update(lambda cfg: cfg.update(modelBindings={
        "defaults": {},
        "scoped": {scope: {model: {
            "target": "demo/effective-max",
            "source": "manual",
            "outboundModel": model,
        }}},
    }))
    monkeypatch.setattr(
        model_metadata.model_pricing, "catalog_metadata",
        lambda target: {"contextWindow": 1_000_000} if target == "demo/effective-max" else None,
    )

    detail, detail_kb = oauth_account_models_menu._detail_render(
        account_key, model, model_page=1, account_page=1, filter_key="all",
    )
    buttons = [button for row in detail_kb["inline_keyboard"] for button in row]
    max_button = next(button for button in buttons if button["text"] == "关闭 Max Context")
    assert "上下文: <code>300K</code>" in detail
    assert "Max Context: <code>1.0M</code>" in detail
    assert "Cursor 账户能力 + models.dev 计价/描述" in detail
    assert max_button["callback_data"].startswith("oam:maxctx:")

    answers = []
    monkeypatch.setattr(oauth_account_models_menu.ui, "answer_cb", lambda _cb, text="": answers.append(text))
    monkeypatch.setattr(oauth_account_models_menu.ui, "edit", lambda *args, **kwargs: None)
    assert oauth_account_models_menu.handle_callback(
        1, 2, "cb", max_button["callback_data"],
    )
    assert not oauth_manager.cursor_max_context_default(account_key, model)
    assert answers == ["Max Context 已关闭"]


def test_cursor_model_catalog_uses_six_per_page_and_six_columns(monkeypatch):
    account = _account()
    records = account["cursor_model_catalog"]["models"]
    template = copy.deepcopy(records[-1])
    for index in range(10):
        extra = copy.deepcopy(template)
        extra.update({
            "id": f"test-model-{index}",
            "name": f"Test Model {index}",
            "context_window": 200_000,
            "context_window_max_mode": None,
            "legacy_slugs": [],
            "variants": [],
            "reasoning_efforts": [],
        })
        records.append(extra)
    account["models"] = [str(item["id"]) for item in records]
    oauth_manager.add_account(account)

    captured: dict = {}
    answers: list[str] = []
    monkeypatch.setattr(
        ui,
        "answer_cb",
        lambda *_args, **_kwargs: answers.append(
            str(_args[1] if len(_args) > 1 else "")
        ),
    )
    monkeypatch.setattr(ui, "edit", lambda *_args, **kwargs: captured.update({
        "text": _args[2], "reply_markup": kwargs.get("reply_markup"),
    }))

    account_short = ui.register_code("cursor:cursor-user-1")
    oauth_menu.on_cursor_models(1, 2, "cb-list", f"{account_short}:1")
    assert "共 <b>13</b> 个模型 · 已禁用 <b>0</b> 个 · 第 <b>1/3</b> 页" in captured["text"]
    assert "13. " not in captured["text"]
    assert "1. Claude Fable 5" in captured["text"]
    assert "上下文：<code>1.0M</code>（Max Context 默认）" in captured["text"]
    assert "支持思考档位：low、medium、high、xhigh、max" in captured["text"]
    assert "Cursor 原生变体" not in captured["text"]
    assert "claude-fable-5-thinking-medium" not in captured["text"]

    buttons = [
        button for row in captured["reply_markup"]["inline_keyboard"] for button in row
    ]
    details = [
        button for button in buttons
        if str(button.get("callback_data") or "").startswith("oa:cursor_model:")
    ]
    assert [button["text"] for button in details] == [
        str(index) for index in range(1, 7)
    ]
    detail_rows = [
        row for row in captured["reply_markup"]["inline_keyboard"]
        if row and str(row[0].get("callback_data") or "").startswith("oa:cursor_model:")
    ]
    assert [len(row) for row in detail_rows] == [6]
    refresh_row = next(
        row for row in captured["reply_markup"]["inline_keyboard"]
        if row and row[0].get("text") == "🔄 刷新额度与模型"
    )
    assert [button.get("text") for button in refresh_row] == [
        "🔄 刷新额度与模型", "🚫 批量禁用",
    ]

    detail_payload = details[0]["callback_data"].split(":", 2)[2]
    oauth_menu.on_cursor_model_detail(1, 2, "cb-detail", detail_payload)
    assert "默认上下文：<code>1.0M</code>（Max Context 已开启）" in captured["text"]
    assert "普通上下文：<code>300.0K</code>" in captured["text"]
    assert "Max Context：<code>1.0M</code>" in captured["text"]
    assert "Max Context 默认：<b>已开启</b>" in captured["text"]
    assert "Cursor 原生变体" not in captured["text"]
    detail_buttons = [
        button for row in captured["reply_markup"]["inline_keyboard"] for button in row
    ]
    toggle = next(
        button for button in detail_buttons
        if str(button.get("callback_data") or "").startswith("oa:cursor_maxctx:")
    )
    assert toggle["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("cursor")

    toggle_payload = toggle["callback_data"].split(":", 2)[2]
    oauth_menu.on_cursor_max_context_toggle(1, 2, "cb-toggle", toggle_payload)
    assert not oauth_manager.cursor_max_context_default(
        "cursor:cursor-user-1", "claude-fable-5",
    )
    assert oauth_manager.cursor_max_context_disabled_models(
        "cursor:cursor-user-1",
    ) == {"claude-fable-5"}
    assert "默认上下文：<code>300.0K</code>（Max Context 已关闭）" in captured["text"]
    assert "Max Context 默认：<b>已关闭</b>" in captured["text"]
    assert any("Max Context 默认已关闭" in answer for answer in answers)

    oauth_menu.on_cursor_models(1, 2, "cb-page-2", f"{account_short}:2")
    assert "第 <b>2/3</b> 页" in captured["text"]
    page2_details = [
        button for row in captured["reply_markup"]["inline_keyboard"] for button in row
        if str(button.get("callback_data") or "").startswith("oa:cursor_model:")
    ]
    assert [button["text"] for button in page2_details] == ["7", "8", "9", "10", "11", "12"]
    oauth_menu.on_cursor_models(1, 2, "cb-page-3", f"{account_short}:3")
    assert "第 <b>3/3</b> 页" in captured["text"]


def test_cursor_disabled_models_filter_channel_and_survive_refresh_and_relogin(
    monkeypatch,
):
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"

    saved = oauth_manager.set_cursor_disabled_models(account_key, {"gpt-5.5"})
    assert saved == {"gpt-5.5"}
    assert oauth_manager.cursor_disabled_models(account_key) == {"gpt-5.5"}

    account = oauth_manager.get_account(account_key)
    channel = CursorOAuthChannel(account)
    assert "gpt-5.5" not in channel.list_client_models()
    assert channel.supports_model("gpt-5.5") is None
    assert channel.supports_model("claude-fable-5") == "claude-fable-5"
    with pytest.raises(GuardError, match="disabled or unavailable"):
        asyncio.run(channel.build_upstream_request(
            {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
            "gpt-5.5",
            ingress_protocol="chat",
        ))

    # Normal model refresh and re-login payloads omit this local preference.
    monkeypatch.setattr(cursor_provider, "fetch_model_catalog_sync", lambda _token: _catalog())
    monkeypatch.setattr(cursor_provider, "fetch_profile_sync", lambda *_args, **_kwargs: {})
    refreshed = oauth_manager.refresh_cursor_models_sync(
        account_key, force=True, min_interval_seconds=0,
    )
    assert refreshed["action"] == "updated"
    assert oauth_manager.cursor_disabled_models(account_key) == {"gpt-5.5"}
    oauth_manager.add_account(_account())
    assert oauth_manager.cursor_disabled_models(account_key) == {"gpt-5.5"}
    with pytest.raises(ValueError, match="Cursor model not found"):
        oauth_manager.set_cursor_disabled_models(account_key, {"not-in-catalog"})

    all_models = set(oauth_manager.get_account(account_key)["models"])
    oauth_manager.set_cursor_disabled_models(account_key, all_models)
    assert CursorOAuthChannel(oauth_manager.get_account(account_key)).list_client_models() == []

    assert oauth_manager.set_cursor_disabled_models(account_key, set()) == set()
    assert CursorOAuthChannel(oauth_manager.get_account(account_key)).supports_model(
        "gpt-5.5"
    ) == "gpt-5.5"


def test_cursor_model_records_sort_name_then_disabled_last():
    account = _account()
    account["cursor_model_catalog"]["models"] = list(reversed(
        account["cursor_model_catalog"]["models"]
    ))
    account["cursor_disabled_models"] = ["composer-2.5"]
    assert [item["id"] for item in oauth_menu._cursor_model_records(account)] == [
        "claude-fable-5", "gpt-5.5", "composer-2.5",
    ]


def test_cursor_batch_disable_menu_persists_and_can_reenable(monkeypatch):
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    account_short = ui.register_code(account_key)
    captured: dict = {}
    answers: list[str] = []
    monkeypatch.setattr(
        ui,
        "answer_cb",
        lambda *_args, **_kwargs: answers.append(
            str(_args[1] if len(_args) > 1 else "")
        ),
    )
    monkeypatch.setattr(ui, "edit", lambda *_args, **kwargs: captured.update({
        "text": _args[2], "reply_markup": kwargs.get("reply_markup"),
    }))

    assert oauth_menu.handle_callback(
        10, 20, "cb-start", f"oa:cursor_disable:{account_short}:1"
    ) is True
    state = oauth_menu._cursor_disable_state(10)
    assert state and state["selected"] == []
    records = oauth_menu._cursor_model_records(oauth_manager.get_account(account_key))
    gpt_index = next(
        index for index, item in enumerate(records, start=1)
        if item["id"] == "gpt-5.5"
    )
    assert oauth_menu.handle_callback(
        10, 20, "cb-select", f"oa:cursor_dis_sel:{account_short}:{gpt_index}"
    ) is True
    assert oauth_menu._cursor_disable_state(10)["selected"] == ["gpt-5.5"]
    assert "将禁用 <b>1</b> 个" in captured["text"]
    button_data = [
        button["callback_data"]
        for row in ((captured.get("reply_markup") or {}).get("inline_keyboard") or [])
        for button in row
    ]
    assert f"oa:cursor_dis_sel:{account_short}:{gpt_index}" in button_data
    assert f"oa:cursor_dis_save:{account_short}" in button_data

    assert oauth_menu.handle_callback(
        10, 20, "cb-save", f"oa:cursor_dis_save:{account_short}"
    ) is True
    assert states.get_state(10) is None
    assert oauth_manager.cursor_disabled_models(account_key) == {"gpt-5.5"}
    assert "已禁用 <b>1</b> 个" in captured["text"]
    assert "GPT-5.5</b> · <code>gpt-5.5</code> · 🚫 <b>已禁用</b>" in captured["text"]
    assert any("已禁用 1 个 Cursor 模型" in answer for answer in answers)
    gpt_ref = oauth_menu._cursor_model_ref(account_key, "gpt-5.5")
    oauth_menu.on_cursor_model_detail(10, 20, "cb-detail", f"{gpt_ref}:1")
    assert "使用状态：🚫 已禁用" in captured["text"]

    # Reopening starts from the persisted set; deselect + save restores use.
    oauth_menu.on_cursor_disable_start(10, 20, "cb-reopen", f"{account_short}:1")
    assert oauth_menu._cursor_disable_state(10)["selected"] == ["gpt-5.5"]
    oauth_menu.on_cursor_disable_set_all(10, 20, "cb-all", selected_all=True)
    assert set(oauth_menu._cursor_disable_state(10)["selected"]) == {
        item["id"] for item in records
    }
    oauth_menu.on_cursor_disable_set_all(10, 20, "cb-clear", selected_all=False)
    assert oauth_menu._cursor_disable_state(10)["selected"] == []
    oauth_menu.on_cursor_disable_select(10, 20, "cb-reselect", str(gpt_index))
    oauth_menu.on_cursor_disable_select(10, 20, "cb-enable", str(gpt_index))
    oauth_menu.on_cursor_disable_save(10, 20, "cb-save-enable")
    assert oauth_manager.cursor_disabled_models(account_key) == set()


def test_cursor_batch_disable_survives_process_restart(monkeypatch):
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    account_short = ui.register_code(account_key)
    oauth_manager.set_cursor_disabled_models(account_key, {"gpt-5.5"})
    captured: dict = {}
    answers: list[str] = []
    monkeypatch.setattr(
        ui,
        "answer_cb",
        lambda *_args, **_kwargs: answers.append(
            str(_args[1] if len(_args) > 1 else "")
        ),
    )
    monkeypatch.setattr(ui, "edit", lambda *_args, **kwargs: captured.update({
        "text": _args[2], "reply_markup": kwargs.get("reply_markup"),
    }))

    states.clear_all()
    with ui._code_lock:
        ui._code_to_name.clear()

    records = oauth_menu._cursor_model_records(oauth_manager.get_account(account_key))
    composer_index = next(
        index for index, item in enumerate(records, start=1)
        if item["id"] == "composer-2.5"
    )
    assert oauth_menu.handle_callback(
        10, 20, "cb-after-restart",
        f"oa:cursor_dis_sel:{account_short}:{composer_index}",
    ) is True
    assert "会话已失效" not in "".join(answers)
    assert set(oauth_menu._cursor_disable_state(10)["selected"]) == {
        "gpt-5.5", "composer-2.5",
    }
    assert oauth_menu.handle_callback(
        10, 20, "cb-save-after-restart", f"oa:cursor_dis_save:{account_short}",
    ) is True
    assert oauth_manager.cursor_disabled_models(account_key) == {
        "gpt-5.5", "composer-2.5",
    }


def test_cursor_disabled_model_disappears_from_registry_and_load_balancing():
    oauth_manager.add_account(_account())
    account_key = "cursor:cursor-user-1"
    oauth_manager.set_cursor_disabled_models(account_key, {"gpt-5.5"})
    try:
        registry.rebuild_from_config()
        channel = registry.get_channel("oauth:cursor:cursor-user-1")
        assert channel is not None
        assert channel.supports_model("gpt-5.5") is None
        assert channel not in load_balancing_menu._channels_for_model("gpt-5.5")
        assert "gpt-5.5" not in load_balancing_menu._client_models()
        assert channel in load_balancing_menu._channels_for_model("claude-fable-5")
    finally:
        config.update(lambda cfg: cfg.__setitem__("oauthAccounts", []))
        registry.rebuild_from_config()


def test_cursor_model_disable_is_scoped_to_one_account():
    first = _account()
    second = _account()
    second.update({
        "email": "cursor-second@local",
        "label": "Cursor Second",
        "subject": "cursor-user-2",
        "sub": "cursor-user-2",
        "access_token": "test-access-2",
        "refresh_token": "test-refresh-2",
    })
    oauth_manager.add_account(first)
    oauth_manager.add_account(second)
    oauth_manager.set_cursor_disabled_models("cursor:cursor-user-1", {"gpt-5.5"})
    try:
        registry.rebuild_from_config()
        candidates = load_balancing_menu._channels_for_model("gpt-5.5")
        keys = {channel.key for channel in candidates}
        assert "oauth:cursor:cursor-user-1" not in keys
        assert "oauth:cursor:cursor-user-2" in keys
        assert "gpt-5.5" in load_balancing_menu._client_models()
    finally:
        config.update(lambda cfg: cfg.__setitem__("oauthAccounts", []))
        registry.rebuild_from_config()


def test_cursor_account_and_model_codes_survive_process_restart():
    account = _account()
    oauth_manager.add_account(account)
    account_key = "cursor:cursor-user-1"
    account_short = ui.register_code(account_key)
    model_ref = oauth_menu._cursor_model_ref(account_key, "claude-fable-5")

    # Simulate a Parrot restart: Telegram keeps old buttons, reverse map is gone.
    with ui._code_lock:
        ui._code_to_name.clear()

    assert oauth_menu._account_key_from_short(account_short) == account_key
    resolved = oauth_menu._resolve_cursor_model_ref(model_ref)
    assert resolved is not None
    assert resolved[0] == account_key
    assert resolved[2]["id"] == "claude-fable-5"


def test_cursor_bulk_editor_freezes_order_and_visible_snapshot(monkeypatch):
    account = _account()
    account["cursor_disabled_models"] = ["hidden-old"]
    oauth_manager.add_account(account)
    account_key = "cursor:cursor-user-1"
    original = [str(item["id"]) for item in oauth_menu._cursor_model_records(account)]
    assert len(original) >= 2
    chat_id = 8675309
    oauth_menu._set_cursor_disable_state(
        chat_id, account_key, page=1, selected=set(), models=original,
    )

    # A background sync adds and reorders the live catalog while the old
    # Telegram editor remains open. Number 2 must still mean snapshot ID 2.
    def reorder(cfg):
        item = cfg["oauthAccounts"][0]
        records = list(reversed(item["cursor_model_catalog"]["models"]))
        records.insert(0, {"id": "new-after-open", "name": "New"})
        item["cursor_model_catalog"]["models"] = records
        item["cursor_disabled_models"].append("new-after-open")
    config.update(reorder)

    monkeypatch.setattr(oauth_menu.ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(oauth_menu.ui, "edit", lambda *a, **k: None)
    oauth_menu.on_cursor_disable_select(chat_id, 1, "cb", "2")
    data = oauth_menu._cursor_disable_state(chat_id)
    assert data["models"] == original
    assert data["selected"] == [original[1]]
    rendered, _kb = oauth_menu._cursor_disable_text_and_kb(data)
    assert f"2. 🚫 <code>{original[1]}</code>" in rendered

    monkeypatch.setattr(oauth_menu, "on_cursor_models", lambda *a, **k: None)
    oauth_menu.on_cursor_disable_save(chat_id, 1, "cb")
    saved = oauth_manager.cursor_disabled_models(account_key)
    assert saved == {"hidden-old", "new-after-open", original[1]}
