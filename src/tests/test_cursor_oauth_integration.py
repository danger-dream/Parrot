from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from ._isolation import isolate

isolate()

from src import config, cooldown, model_metadata, notifier, oauth_manager, state_db  # noqa: E402
from src.channel.cursor_oauth_channel import CursorOAuthChannel  # noqa: E402
from src.cursor_bridge import catalog as cursor_catalog  # noqa: E402
from src.cursor_bridge import runtime as cursor_runtime  # noqa: E402
from src.cursor_bridge.models import CursorModel  # noqa: E402
from src.cursor_bridge.usage import (  # noqa: E402
    CursorPlanUsage,
    CursorSpendLimit,
    CursorUsage,
)
from src.oauth import cursor as cursor_provider  # noqa: E402
from src.telegram import states, ui  # noqa: E402
from src.telegram.menus import logs_menu, oauth_menu  # noqa: E402


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
        records["composer-2.5"], fast=True,
    ) == "composer-2.5-fast"
    assert cursor_catalog.resolve_variant(
        records["gpt-5.5"], reasoning_effort="xhigh", fast=True,
    ) == "gpt-5.5-extra-high-fast"


def test_cursor_native_metadata_precedes_models_dev_and_uses_account_limits():
    account = _install_account()
    scope = "oauth:cursor:cursor-user-1"
    binding = model_metadata.resolve_binding(
        "claude-fable-5",
        scope_key=scope,
        outbound_model="claude-fable-5-thinking-medium",
    )
    assert binding is not None
    assert binding.source == "cursor.AvailableModels"
    assert binding.target == "cursor/claude-fable-5"
    assert binding.metadata["contextWindow"] == 300_000
    assert binding.metadata["contextWindowMaxMode"] == 1_000_000
    assert binding.metadata["maxOutputTokens"] == 64_000
    assert binding.metadata["vision"] is False
    assert binding.metadata["cursorUpstreamVision"] is True
    assert "claude-fable-5-thinking-medium" in binding.metadata["variants"]

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
    monkeypatch.setattr(ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui, "edit", lambda *args, **kwargs: edits.append((
        args[2], kwargs.get("reply_markup"),
    )))

    oauth_menu.on_login_cursor_start(321, 654, "cb-start")
    oauth_menu.on_login_cursor_done(321, 654, "cb-done")

    accounts = [
        acc for acc in oauth_manager.list_accounts()
        if oauth_manager.provider_of(acc) == "cursor"
    ]
    assert len(accounts) == 1
    assert accounts[0]["subject"] == "cursor-mock-user"
    assert accounts[0]["models"] == ["claude-fable-5", "composer-2.5"]
    assert accounts[0]["cursor_model_catalog"]["models"]
    assert states.get_state(321) is None
    assert "Cursor OAuth 账户已添加" in edits[-1][0]


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
