from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from src import config, cooldown, model_metadata, model_pricing, oauth_manager, oauth_model_discovery, state_db
from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
from src.channel.oauth_channel import OAuthChannel
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.telegram.menus import oauth_account_models_menu, oauth_menu


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
    def json(self): return self.payload


def test_openai_codex_request_headers_url_and_parser(monkeypatch):
    seen = {}
    def get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response({"models": [
            {
                "slug": "gpt-visible",
                "visibility": "list",
                "service_tiers": [
                    {"id": "priority", "name": "Fast", "description": "1.5x", "secret": "drop"},
                    {"id": "ultrafast", "name": "Ultrafast"},
                    {"id": "ultrafast", "name": "duplicate"},
                    {"id": "hyperspeed", "name": "Hyperspeed"},
                    {"name": "missing-id"},
                ],
                "minimal_client_version": "0.144.0",
                "additional_speed_tiers": ["fast"],
                "available_in_plans": ["plus", "pro"],  # not an entitlement signal; dropped
            },
            {"slug": "gpt-no-tiers", "visibility": "list", "service_tiers": []},
            {"slug": "gpt-hidden", "visibility": "hide"},
        ]})
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    result = oauth_model_discovery.discover_openai({
        "access_token": "tok", "workspace_id": "ws",
    })
    assert result.models == ["gpt-visible", "gpt-no-tiers"]
    assert seen["url"] == "https://chatgpt.com/backend-api/codex/models?client_version=0.144.0"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["headers"]["ChatGPT-Account-ID"] == "ws"
    assert seen["headers"]["originator"] == "codex_cli_rs"
    assert seen["headers"]["user-agent"].startswith("codex_cli_rs/0.144.0 ")
    assert result.catalog["models"] == [{
        "id": "gpt-visible",
        "serviceTiers": [
            {"id": "priority", "name": "Fast", "description": "1.5x"},
            {"id": "ultrafast", "name": "Ultrafast"},
            {"id": "hyperspeed", "name": "Hyperspeed"},
        ],
        "minimalClientVersion": "0.144.0",
        "additionalSpeedTiers": ["fast"],
    }, {
        "id": "gpt-no-tiers",
        "serviceTiers": [],
    }]


def test_openai_codex_client_version_config_drives_catalog_query_and_ua(monkeypatch):
    seen = {}
    original = copy.deepcopy(config.get().get("openaiOAuth") or {})
    try:
        config.update(lambda cfg: cfg.setdefault("openaiOAuth", {}).update({
            "codexCliVersion": "0.150.1",
        }))
        monkeypatch.setattr(
            oauth_model_discovery.network,
            "get_sync",
            lambda url, **kwargs: seen.update(url=url, **kwargs) or Response({
                "models": [{"slug": "gpt-future", "visibility": "list"}],
            }),
        )
        result = oauth_model_discovery.discover_openai({"access_token": "tok"})
        assert result.models == ["gpt-future"]
        assert seen["url"].endswith("?client_version=0.150.1")
        assert seen["headers"]["user-agent"].startswith("codex_cli_rs/0.150.1 ")
    finally:
        config.update(lambda cfg: cfg.__setitem__("openaiOAuth", original))


def test_claude_headers_pagination_and_empty_error(monkeypatch):
    calls = []
    payloads = [
        {"data": [{"id": "claude-a", "display_name": "Claude A", "max_input_tokens": 200000, "max_tokens": 8192, "capabilities": {"supported": {"reasoning": True, "vision": True}, "effort_levels": ["low", "high"]}}], "has_more": True, "last_id": "p 1"},
        {"data": [{"id": "claude-b"}], "has_more": False},
    ]
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", lambda url, **kw: calls.append((url, kw)) or Response(payloads.pop(0)))
    result = oauth_model_discovery.discover_claude({"access_token": "tok"})
    assert result.models == ["claude-a", "claude-b"]
    assert "after_id=p%201" in calls[1][0]
    assert calls[0][1]["headers"]["anthropic-version"] == "2023-06-01"
    assert calls[0][1]["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert result.catalog["models"][0] == {"id": "claude-a", "name": "Claude A", "contextWindow": 200000, "maxOutputTokens": 8192, "reasoning": True, "reasoningEfforts": ["low", "high"], "supportsImages": True}
    assert result.catalog["models"][1] == {"id": "claude-b"}
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", lambda *a, **k: Response({"data": [], "has_more": False}))
    with pytest.raises(ValueError, match="empty"):
        oauth_model_discovery.discover_claude({"access_token": "tok"})


@pytest.mark.parametrize("base_url", ["https://api.x.ai", "https://api.x.ai/v1"])
def test_xai_language_catalog_is_authoritative_and_models_only_enriches(monkeypatch, base_url):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("language-models"):
            return Response({"data": [{"id": "text-a", "aliases": ["a"]}]})
        return Response({"data": [
            {"id": "text-a", "context_length": 131072},
            {"id": "must-not-be-added", "context_length": 1},
        ]})
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    result = oauth_model_discovery.discover_xai({"access_token": "tok", "base_url": base_url})
    assert calls == ["https://api.x.ai/v1/language-models", "https://api.x.ai/v1/models"]
    assert result.models == ["text-a"]
    assert result.catalog["models"] == [{"id": "text-a", "contextWindow": 131072, "aliases": ["a"]}]


def test_claude_pagination_uses_one_decreasing_total_budget(monkeypatch):
    timeouts = []
    payloads = [
        {"data": [{"id": "a"}], "has_more": True, "last_id": "a"},
        {"data": [{"id": "b"}], "has_more": False},
    ]

    def get(_url, **kwargs):
        timeouts.append(kwargs["timeout"])
        time.sleep(0.01)
        return Response(payloads.pop(0))

    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    result = oauth_model_discovery.discover_claude(
        {"access_token": "tok"}, timeout=0.1,
    )
    assert result.models == ["a", "b"]
    assert 0 < timeouts[1] < timeouts[0] <= 0.1


def test_xai_enrichment_budget_exhaustion_keeps_authoritative_success(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url.endswith("language-models"):
            time.sleep(0.02)
            return Response({"data": [{"id": "grok-text"}]})
        pytest.fail("enrichment must not start after the total budget is exhausted")

    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    result = oauth_model_discovery.discover_xai(
        {"access_token": "tok", "base_url": "https://api.x.ai"}, timeout=0.005,
    )
    assert result.models == ["grok-text"]
    assert result.catalog["models"] == [{"id": "grok-text"}]
    assert len(calls) == 1


def test_antigravity_merges_prod_and_daily_with_daily_metadata_precedence(monkeypatch):
    calls = []
    prod_url = "https://prod.example/base/v1internal:fetchAvailableModels"
    daily_url = "https://daily.example/base/v1internal:fetchAvailableModels"
    payloads = {
        prod_url: {"models": {
            "shared": {"displayName": "Prod Shared", "maxTokens": 100000},
            "prod-only": {"displayName": "Prod Only"},
            "gemini-image": {},
            "explicit-image": {"capabilities": ["image"]},
        }},
        daily_url: {"models": {
            "shared": {"displayName": "Daily Shared", "maxTokens": 200000, "supportsThinking": True},
            "gemini-3.8-flash-low": {"displayName": "Gemini 3.8 Flash Low"},
            "gemini-3.8-flash-medium": {"displayName": "Gemini 3.8 Flash Medium"},
            "gemini-3.8-flash-high": {"displayName": "Gemini 3.8 Flash High"},
            "chat_20706": {},
        }},
    }

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url == prod_url:
            time.sleep(0.01)
        return Response(payloads[url])

    monkeypatch.setattr(oauth_model_discovery.network, "post_sync", post)
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "api_base_url", lambda: "https://prod.example/base/")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "daily_api_base_url", lambda: "https://daily.example/base")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "image_models", lambda: ["gemini-image"])
    monkeypatch.setattr(
        oauth_model_discovery.antigravity_provider,
        "default_models",
        lambda: pytest.fail("account discovery must not read fallback default models"),
    )

    result = oauth_model_discovery.discover_antigravity(
        {"access_token": "tok", "project_id": "p"}, timeout=0.2, proxy_channel="oauth:test",
    )

    assert result.models == [
        "shared", "prod-only", "gemini-3.8-flash-low",
        "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
    ]
    assert "gemini-3.8-flash" not in result.models
    assert result.catalog["models"][0] == {
        "id": "shared", "name": "Daily Shared", "contextWindow": 200000,
        "reasoning": True, "supportsThinking": True,
    }
    assert [url for url, _kwargs in calls] == [prod_url, daily_url]
    assert 0 < calls[1][1]["timeout"] < calls[0][1]["timeout"] <= 0.2
    for _url, kwargs in calls:
        assert kwargs["json"] == {"project": "p"}
        assert kwargs["headers"]["authorization"] == "Bearer tok"
        assert kwargs["headers"]["accept"] == "application/json"
        assert kwargs["headers"]["content-type"] == "application/json"
        assert kwargs["headers"]["user-agent"]
        assert kwargs["headers"]["x-goog-api-client"]
        assert kwargs["proxy_purpose"] == "oauth_antigravity"
        assert kwargs["proxy_channel"] == "oauth:test"


@pytest.mark.parametrize("failed_index", [0, 1])
@pytest.mark.parametrize("failure_kind", ["http_error", "no_text_models"])
def test_antigravity_one_unavailable_endpoint_still_uses_the_other(
    monkeypatch, failed_index, failure_kind,
):
    calls = []

    def post(url, **kwargs):
        call_index = len(calls)
        calls.append((url, kwargs))
        if call_index == failed_index:
            if failure_kind == "http_error":
                return Response({}, 503)
            return Response({"models": {
                "gemini-image": {},
                "explicit-image": {"type": "image"},
            }})
        return Response({"models": {
            "gemini-text": {"supportedGenerationMethods": ["generateContent"]},
            # Real upstream records commonly omit generic modality metadata.
            "new-upstream-text": {"displayName": "New Upstream Text", "maxTokens": 1000000, "maxOutputTokens": 64000, "supportsThinking": True, "supportsImages": True, "tag": "preview"},
        }})

    monkeypatch.setattr(oauth_model_discovery.network, "post_sync", post)
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "api_base_url", lambda: "https://prod.example")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "daily_api_base_url", lambda: "https://daily.example")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "image_models", lambda: ["gemini-image"])

    result = oauth_model_discovery.discover_antigravity({"access_token": "tok", "project_id": "p"})

    assert result.models == ["gemini-text", "new-upstream-text"]
    assert result.catalog["models"][1] == {"id": "new-upstream-text", "name": "New Upstream Text", "tagline": "preview", "contextWindow": 1000000, "maxOutputTokens": 64000, "reasoning": True, "supportsThinking": True, "supportsImages": True}
    assert [url for url, _kwargs in calls] == [
        "https://prod.example/v1internal:fetchAvailableModels",
        "https://daily.example/v1internal:fetchAvailableModels",
    ]


@pytest.mark.parametrize("failure_kind", ["http_error", "no_text_models"])
def test_antigravity_rejects_when_both_endpoints_are_unavailable(monkeypatch, failure_kind):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if failure_kind == "http_error":
            return Response({}, 503)
        return Response({"models": {
            "gemini-image": {},
            "explicit-image": {"capabilities": ["image"]},
            "chat_20706": {},
        }})

    monkeypatch.setattr(oauth_model_discovery.network, "post_sync", post)
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "api_base_url", lambda: "https://prod.example")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "daily_api_base_url", lambda: "https://daily.example")
    monkeypatch.setattr(oauth_model_discovery.antigravity_provider, "image_models", lambda: ["gemini-image"])

    expected = "HTTP 503" if failure_kind == "http_error" else "no verified text models"
    with pytest.raises((RuntimeError, ValueError), match=expected):
        oauth_model_discovery.discover_antigravity({"access_token": "tok", "project_id": "p"})
    assert len(calls) == 2


def test_cursor_adapter_delegates_existing_catalog(monkeypatch):
    monkeypatch.setattr(oauth_model_discovery.cursor_provider, "fetch_model_catalog_sync", lambda token: {"models": [{"id": "cursor-a"}]})
    result = oauth_model_discovery.discover_cursor({"access_token": "tok"})
    assert result.models == ["cursor-a"]


@pytest.mark.parametrize("provider", ["openai", "claude", "xai", "antigravity", "cursor"])
@pytest.mark.parametrize("failure", ["empty", "http_error", "timeout"])
def test_all_discovery_adapters_reject_empty_error_and_timeout(monkeypatch, provider, failure):
    if failure == "timeout":
        def response(*args, **kwargs): raise TimeoutError("mock timeout")
    elif failure == "http_error":
        def response(*args, **kwargs): return Response({}, 503)
    else:
        payload = {
            "openai": {"models": []},
            "claude": {"data": [], "has_more": False},
            "xai": {"data": []},
            "antigravity": {"models": {}},
            "cursor": {"models": []},
        }[provider]
        def response(*args, **kwargs): return Response(payload)
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", response)
    monkeypatch.setattr(oauth_model_discovery.network, "post_sync", response)
    if provider == "cursor":
        if failure == "timeout":
            monkeypatch.setattr(oauth_model_discovery.cursor_provider, "fetch_model_catalog_sync", lambda token: (_ for _ in ()).throw(TimeoutError("mock timeout")))
        elif failure == "http_error":
            monkeypatch.setattr(oauth_model_discovery.cursor_provider, "fetch_model_catalog_sync", lambda token: (_ for _ in ()).throw(RuntimeError("HTTP 503")))
        else:
            monkeypatch.setattr(oauth_model_discovery.cursor_provider, "fetch_model_catalog_sync", lambda token: {"models": []})
    account = {"provider": provider, "access_token": "tok", "project_id": "p", "base_url": "https://example.invalid"}
    with pytest.raises(Exception):
        oauth_model_discovery.ADAPTERS[provider](account)


@pytest.fixture
def account_config():
    state_db.init()
    before = copy.deepcopy(config.get())
    def setup(cfg):
        cfg["oauthDefaultModels"] = ["claude-default"]
        cfg.setdefault("openaiOAuth", {})["defaultModels"] = ["gpt-default"]
        cfg["oauthAccounts"] = [
            {"provider": "openai", "type": "openai", "email": "a@x", "workspace_id": "ws-a", "access_token": "ta", "refresh_token": "ra", "expired": "2999-01-01T00:00:00Z", "models": ["old"], "disabledModels": ["old"]},
            {"provider": "openai", "type": "openai", "email": "b@x", "workspace_id": "ws-b", "access_token": "tb", "refresh_token": "rb", "expired": "2999-01-01T00:00:00Z", "models": ["old"], "disabledModels": []},
        ]
    config.update(setup)
    yield
    config.update(lambda cfg: (cfg.clear(), cfg.update(copy.deepcopy(before))))


@pytest.mark.asyncio
async def test_fresh_replaces_lkg_failure_and_empty_preserve(monkeypatch, account_config):
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", lambda key: _async_value("ta"))
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", lambda account, timeout: oauth_model_discovery.DiscoveryResult(["new"], {}, "upstream:codex"))
    out = await oauth_manager.refresh_account_models("openai:a@x:ws-a")
    assert out["action"] == "updated"
    assert oauth_manager.get_account("openai:a@x:ws-a")["models"] == ["new"]
    assert oauth_manager.get_account("openai:a@x:ws-a")["disabledModels"] == ["old"]
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("late")))
    await oauth_manager.refresh_account_models("openai:a@x:ws-a")
    assert oauth_manager.get_account("openai:a@x:ws-a")["models"] == ["new"]
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", lambda *a, **k: oauth_model_discovery.DiscoveryResult([], {}, "upstream:codex"))
    await oauth_manager.refresh_account_models("openai:a@x:ws-a")
    assert oauth_manager.get_account("openai:a@x:ws-a")["models"] == ["new"]


async def _async_value(value): return value


def test_fallback_stateless_disabled_isolation_and_four_channels(account_config):
    def mutate(cfg):
        cfg["oauthAccounts"][0]["models"] = []
        cfg["oauthAccounts"][0]["disabledModels"] = ["gpt-default"]
    config.update(mutate)
    selection = oauth_manager.account_model_selection("openai:a@x:ws-a")
    assert selection["fallback"] and selection["models"] == ["gpt-default"]
    assert selection["effective_models"] == []
    # Default list remains a plain list and is not changed by account state.
    assert config.get()["openaiOAuth"]["defaultModels"] == ["gpt-default"]
    assert oauth_manager.account_disabled_models("openai:b@x:ws-b") == set()

    common = {"email": "x", "access_token": "t", "refresh_token": "r", "models": ["m1", "m2"], "disabledModels": ["m2"]}
    channels = [
        OAuthChannel({**common, "provider": "claude"}, ["fallback"]),
        OpenAIOAuthChannel({**common, "provider": "openai"}),
        XAIOAuthChannel({**common, "provider": "xai", "subject": "s"}),
        AntigravityOAuthChannel({**common, "provider": "antigravity", "project_id": "p"}),
    ]
    for channel in channels:
        assert channel.list_client_models() == ["m1"]
        assert channel.supports_model("m1") == "m1"
        assert channel.supports_model("m2") is None


def test_batch_disabled_models_preserves_hidden_snapshot_scope(account_config):
    account_key = "openai:a@x:ws-a"
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        models=["m1", "m2", "new-after-open"],
        disabledModels=["m1", "hidden-old", "new-after-open"],
    ))
    saved = oauth_manager.set_account_disabled_models(
        account_key, ["m2"], visible_models=["m1", "m2"],
    )
    assert saved == {"m2", "hidden-old", "new-after-open"}
    assert oauth_manager.account_disabled_models(account_key) == saved
    assert oauth_manager.account_disabled_models("openai:b@x:ws-b") == set()
    assert config.get()["openaiOAuth"]["defaultModels"] == ["gpt-default"]
    with pytest.raises(ValueError, match="not in editor snapshot"):
        oauth_manager.set_account_disabled_models(
            account_key, ["not-visible"], visible_models=["m1", "m2"],
        )


def test_status_priority_pagination_numeric_keyboard_and_banner(monkeypatch, account_config):
    account_key = "openai:a@x:ws-a"
    models = [f"model-{i}" for i in range(1, 14)]
    def mutate(cfg):
        cfg["oauthAccounts"][0]["models"] = models
        cfg["oauthAccounts"][0]["disabledModels"] = ["model-1"]
        cfg["oauthAccounts"][0]["last_model_sync_source"] = "upstream:codex"
    config.update(mutate)
    monkeypatch.setattr(cooldown, "get_state", lambda ck, model: {"cooldown_until": -1} if model == "model-1" else ({"cooldown_until": 9999999999999} if model == "model-2" else None))

    entries = oauth_account_models_menu._sorted_status_entries(
        account_key, models, {"model-1"},
    )
    assert [model for model, _status in entries][-2:] == ["model-2", "model-1"]
    assert [status[1] for _model, status in entries][-2:] == ["冷却中", "已停用"]
    assert oauth_account_models_menu._status(account_key, "model-1", {"model-1"})[1] == "已停用"

    text, kb = oauth_account_models_menu.render(account_key, model_page=1, account_page=3, filter_key="openai")
    assert "1. ✅ <code>model-10</code> - 可用" in text
    assert text.count("\n") >= 12 and "13. " not in text
    numeric = [b["text"] for b in kb["inline_keyboard"][0]]
    assert numeric == [str(index) for index in range(1, 7)]
    assert len(kb["inline_keyboard"][0]) == 6
    bulk_callback = next(
        button["callback_data"]
        for row in kb["inline_keyboard"] for button in row
        if button["text"] == "🚫 批量禁用"
    )
    assert bulk_callback.startswith("oam:bulk:")
    assert len(bulk_callback.encode("utf-8")) <= 64
    assert "oa:view:" in kb["inline_keyboard"][-1][0]["callback_data"]
    page2, _ = oauth_account_models_menu.render(account_key, model_page=2)
    assert "12. 🟠 <code>model-2</code> - 冷却中" in page2
    page3, _ = oauth_account_models_menu.render(account_key, model_page=3)
    assert "13. 🚫 <code>model-1</code> - 已停用" in page3

    config.update(lambda cfg: (cfg["oauthAccounts"][0].update(models=[], disabledModels=[], last_model_sync_error="timeout")))
    fallback, _ = oauth_account_models_menu.render(account_key)
    assert "上游同步失败/尚无账户目录，正在使用默认模型" in fallback


def test_cursor_unified_bulk_button_opens_complete_legacy_editor(monkeypatch, account_config):
    models = [f"cursor-model-{index:02d}" for index in range(1, 36)]
    disabled = models[:18]
    account = {
        "provider": "cursor",
        "type": "cursor",
        "subject": "cursor-bulk-test",
        "label": "Cursor Bulk Test",
        "access_token": "tok",
        "models": models,
        "cursor_disabled_models": disabled,
        "cursor_model_catalog": {
            "models": [{"id": model, "name": model} for model in models],
        },
    }
    config.update(lambda cfg: cfg["oauthAccounts"].append(account))
    account_key = "cursor:cursor-bulk-test"

    _text, kb = oauth_account_models_menu.render(
        account_key, model_page=4, account_page=3, filter_key="cursor",
    )
    bulk_callback = next(
        button["callback_data"]
        for row in kb["inline_keyboard"] for button in row
        if button["text"] == "🚫 批量禁用"
    )
    assert bulk_callback.startswith("oa:cursor_disable:")
    assert not bulk_callback.startswith("oam:bulk:")
    assert bulk_callback.endswith(":4")
    assert len(bulk_callback.encode("utf-8")) <= 64

    captured = {}
    chat_id = 910024
    monkeypatch.setattr(oauth_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        oauth_menu.ui, "edit",
        lambda chat, message, text, reply_markup=None: captured.update(
            text=text, reply_markup=reply_markup,
        ),
    )
    try:
        assert oauth_menu.handle_callback(chat_id, 2, "cb", bulk_callback)
        state = oauth_menu._cursor_disable_state(chat_id)
        assert state and len(state["selected"]) == 18
        assert state["page"] == 4
        assert "共 <b>35</b> 个模型 · 将禁用 <b>18</b> 个" in captured["text"]
        select_callbacks = [
            button["callback_data"]
            for row in captured["reply_markup"]["inline_keyboard"]
            for button in row
            if button["callback_data"].startswith("oa:cursor_dis_sel:")
        ]
        assert len(select_callbacks) == 35
        assert [int(callback.rsplit(":", 1)[1]) for callback in select_callbacks] == list(range(1, 36))
        assert not any(
            "page" in button["callback_data"]
            for row in captured["reply_markup"]["inline_keyboard"]
            for button in row
        )
        assert all(len(callback.encode("utf-8")) <= 64 for callback in select_callbacks)
    finally:
        oauth_account_models_menu.states.pop_state(chat_id)


def test_bulk_disable_draft_no_pagination_controls_save_and_cancel(monkeypatch, account_config):
    account_key = "openai:a@x:ws-a"
    models = [f"model-{i}" for i in range(1, 9)]
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        models=models,
        disabledModels=["model-2", "hidden-old"],
        last_model_sync_source="upstream:codex",
    ))
    chat_id = 910023
    edits = []
    answers = []
    monkeypatch.setattr(
        oauth_account_models_menu.ui, "edit",
        lambda chat, message, text, reply_markup=None: edits.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        oauth_account_models_menu.ui, "answer_cb",
        lambda cb, text=None, **kwargs: answers.append(text),
    )
    short = oauth_account_models_menu.ui.register_code(account_key)
    try:
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bulk:{short}:1:3:openai",
        )
        text, kb = edits[-1]
        assert "· 批量禁用模型</b>" in text
        assert "第 1/2 页" not in text
        assert "另有 <b>1</b> 个目录外停用记录将继续保留" in text
        assert "1. ✅ <code>model-1</code> - 保持启用" in text
        assert "2. ✅ <code>model-3</code> - 保持启用" in text
        assert "8. 🚫 <code>model-2</code> - 将停用" in text
        numeric = [
            button["text"] for row in kb["inline_keyboard"] for button in row
            if str(button.get("callback_data") or "").startswith("oam:bsel:")
        ]
        assert numeric == [str(index) for index in range(1, 9)]
        assert not any(
            str(button.get("callback_data") or "").startswith("oam:bpage:")
            for row in kb["inline_keyboard"] for button in row
        )

        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:ball:{short}:1:3:openai",
        )
        assert len(oauth_account_models_menu._bulk_state(chat_id)["selected"]) == 8
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bclear:{short}:1:3:openai",
        )
        assert oauth_account_models_menu._bulk_state(chat_id)["selected"] == []
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:binv:{short}:1:3:openai",
        )
        assert len(oauth_account_models_menu._bulk_state(chat_id)["selected"]) == 8
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bclear:{short}:1:3:openai",
        )
        # The draft order stays fixed even after "全部启用" clears the selection.
        assert "8. ✅ <code>model-2</code> - 保持启用" in edits[-1][0]
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bsel:{short}:8:1:3:openai",
        )
        assert oauth_account_models_menu._bulk_state(chat_id)["selected"] == ["model-2"]

        # A background sync changes the catalog while this draft is open. IDs
        # outside the editor snapshot must not be enabled by this save.
        config.update(lambda cfg: cfg["oauthAccounts"][0].update(
            models=models + ["model-9"],
            disabledModels=["model-2", "hidden-old", "model-9"],
        ))
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bsave:{short}:1:3:openai",
        )
        assert oauth_manager.account_disabled_models(account_key) == {
            "model-2", "hidden-old", "model-9",
        }
        assert oauth_account_models_menu._bulk_state(chat_id) is None

        # Cancel discards a second draft without touching persisted settings.
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bulk:{short}:1:3:openai",
        )
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:ball:{short}:1:3:openai",
        )
        assert oauth_account_models_menu.handle_callback(
            chat_id, 2, "cb", f"oam:bcancel:{short}:1:3:openai",
        )
        assert oauth_manager.account_disabled_models(account_key) == {
            "model-2", "hidden-old", "model-9",
        }
        assert "已取消，未保存修改" in answers
    finally:
        oauth_account_models_menu.states.pop_state(chat_id)


def test_oauth_pagination_over_ten_and_fault_clear_keeps_user_disable(monkeypatch, account_config):
    row = oauth_account_models_menu._pagination(6, 14, "acct", 2, "xai")
    assert row[0]["text"] == "1" and row[-1]["text"] == "14"
    assert sum(button["text"] == "…" for button in row) == 2
    assert "[6]" in [button["text"] for button in row]

    account_key = "openai:a@x:ws-a"
    assert "old" in oauth_manager.account_disabled_models(account_key)
    cleared = []
    monkeypatch.setattr(oauth_account_models_menu.cooldown, "clear", lambda ck, model=None: cleared.append((ck, model)))
    short, model_ref = oauth_account_models_menu.ui.register_code(account_key), oauth_account_models_menu.ui.register_code("old")
    monkeypatch.setattr(oauth_account_models_menu.ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(oauth_account_models_menu.ui, "edit", lambda *a, **k: None)
    assert oauth_account_models_menu.handle_callback(1, 2, "cb", f"oam:clear:{short}:{model_ref}:1:1:all")
    assert cleared == [(f"oauth:{account_key}", "old")]
    assert "old" in oauth_manager.account_disabled_models(account_key)


def test_detail_fixed_rows_and_openai_reset_wording(account_config):
    _text, kb = oauth_menu._detail_text_and_kb("openai:a@x:ws-a", refresh_quota=False)
    labels = [[button["text"] for button in row] for row in kb["inline_keyboard"]]
    assert labels == [
        ["🔄 刷新 Token", "📊 刷新额度"],
        ["🧬 管理模型", "🚦 并发上限"],
        ["🧹 清模型故障", "🔗 清亲和绑定"],
        ["⏸ 停用账户", "🗑 删除账户"],
        ["♻️ 重置额度"],
        ["🏠 主菜单", "◀ 返回列表"],
    ]
    assert "官方重置次数" in _text


def test_relogin_preserves_models_and_disabled(account_config):
    existing = oauth_manager.get_account("openai:a@x:ws-a")
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(account_model_catalog={"schema": 1, "models": [{"id": "old", "contextWindow": 1000}]}))
    existing = oauth_manager.get_account("openai:a@x:ws-a")
    incoming = {**existing, "access_token": "fresh"}
    incoming.pop("models", None); incoming.pop("disabledModels", None); incoming.pop("account_model_catalog", None)
    oauth_manager.add_account(incoming)
    saved = oauth_manager.get_account("openai:a@x:ws-a")
    assert saved["models"] == ["old"]
    assert saved["disabledModels"] == ["old"]
    assert saved["account_model_catalog"]["models"][0]["contextWindow"] == 1000


def test_openai_catalog_is_allowlisted_and_raw_payload_is_not_retained(monkeypatch):
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", lambda *a, **k: Response({"models": [{
        "slug": "gpt-rich", "visibility": "list", "display_name": "GPT Rich",
        "description": "Useful", "context_window": 200000, "max_context_window": 1000000,
        "max_output_tokens": 32000, "input_modalities": ["text", "image"],
        "reasoning": True, "reasoning_levels": ["low", "high"], "supports_images": True,
        "base_instructions": "MUST NEVER PERSIST", "model_messages": {"huge": "secret-ish"},
    }] }))
    result = oauth_model_discovery.discover_openai({"access_token": "tok"})
    assert result.models == ["gpt-rich"]
    assert result.catalog == {"schema": 1, "models": [{
        "id": "gpt-rich", "name": "GPT Rich", "description": "Useful",
        "contextWindow": 200000, "contextWindowMaxMode": 1000000,
        "maxOutputTokens": 32000, "inputModalities": ["text", "image"],
        "reasoning": True, "reasoningEfforts": ["low", "high"], "supportsImages": True,
    }]}
    assert "MUST NEVER PERSIST" not in repr(result.catalog)


def test_xai_enrichment_failure_is_nonfatal(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/models"): raise TimeoutError("optional endpoint down")
        return Response({"data": [{"id": "grok-a", "aliases": ["stable"]}]})
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    result = oauth_model_discovery.discover_xai({"access_token": "tok", "base_url": "https://api.x.ai/v1"})
    assert result.models == ["grok-a"]
    assert result.catalog["models"] == [{"id": "grok-a", "aliases": ["stable"]}]
    assert calls == ["https://api.x.ai/v1/language-models", "https://api.x.ai/v1/models"]


@pytest.mark.asyncio
async def test_metadata_atomic_persistence_and_failed_sync_preserves_lkg(monkeypatch, account_config):
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", lambda key: _async_value("ta"))
    rich = {"schema": 1, "models": [{
        "id": "rich-id", "name": "Pretty Alias", "contextWindow": 1000000,
        "reasoning": True, "supportsImages": True,
        "reasoningEfforts": ["low", "medium", "high"],
        "serviceTiers": [
            {"id": "priority", "name": "Fast"},
            {"id": "ultrafast", "name": "Ultrafast"},
            {"id": "hyperspeed", "name": "Hyperspeed"},
        ],
        "minimalClientVersion": "0.150.0",
    }]}
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", lambda *a, **k: oauth_model_discovery.DiscoveryResult(["rich-id"], rich, "upstream:test"))
    await oauth_manager.refresh_account_models("openai:a@x:ws-a")
    saved = oauth_manager.get_account("openai:a@x:ws-a")
    assert saved["models"] == ["rich-id"] and saved["account_model_catalog"] == rich
    text, kb = oauth_account_models_menu.render("openai:a@x:ws-a")
    assert "<code>rich-id</code>" in text and "Pretty Alias</code> -" not in text
    assert "上下文：1.0M · 🧠 · 🖼 · ⚡ Ultra" in text and "思考档位：low、medium、high" in text
    detail, _ = oauth_account_models_menu._detail_render(
        "openai:a@x:ws-a", "rich-id", model_page=1, account_page=1, filter_key="all",
    )
    assert (
        "服务档位（账户目录）: "
        "<code>Fast (priority)、Ultrafast、Hyperspeed</code>"
    ) in detail
    assert "最低 Codex CLI: <code>0.150.0</code>" in detail
    assert all(len(str(b.get("callback_data") or "").encode()) <= 64 for row in kb["inline_keyboard"] for b in row)
    monkeypatch.setattr(oauth_manager.oauth_model_discovery, "discover", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("down")))
    await oauth_manager.refresh_account_models("openai:a@x:ws-a")
    failed = oauth_manager.get_account("openai:a@x:ws-a")
    assert failed["models"] == ["rich-id"] and failed["account_model_catalog"] == rich


def test_fallback_ids_have_no_fake_metadata(account_config):
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(models=[], account_model_catalog={"schema": 1, "models": [{"id": "gpt-default", "contextWindow": 999}]}))
    selection = oauth_manager.account_model_selection("openai:a@x:ws-a")
    assert selection["fallback"] and selection["records"] == []
    text, _ = oauth_account_models_menu.render("openai:a@x:ws-a")
    assert "上下文：" not in text


@pytest.mark.parametrize("provider", ["openai", "claude", "xai", "antigravity", "cursor"])
def test_five_provider_rich_list_uses_complete_id_six_per_page(monkeypatch, provider):
    models = [f"{provider}-id-{n}" for n in range(7)]
    account = {"provider": provider, "label": provider, "models": models}
    records = [{"id": model, "name": f"Display {n}", "contextWindow": 1000000, "reasoning": True, "supportsImages": True, "reasoningEfforts": ["low", "high"]} for n, model in enumerate(models)]
    selection = {"models": models, "effective_models": models, "disabled_models": set(), "source": f"upstream:{provider}", "fallback": False, "synced_at": "now", "error": "", "records": records}
    monkeypatch.setattr(oauth_manager, "get_account", lambda key: account)
    monkeypatch.setattr(oauth_manager, "account_model_selection", lambda value: selection)
    monkeypatch.setattr(cooldown, "get_state", lambda *a, **k: None)
    effective = {
        item["id"]: model_metadata.MetadataBinding(
            item["id"], f"{provider}/{item['id']}", provider, item["id"],
            f"oauth:{provider}:account", item["id"], "account_model_catalog", item,
            "account-upstream",
        ) for item in records
    }
    monkeypatch.setattr(
        model_metadata, "resolve_binding",
        lambda model, **kwargs: effective.get(model),
    )
    text, kb = oauth_account_models_menu.render(f"{provider}:account")
    assert text.count(" - 可用") == 6 and f"<code>{provider}-id-0</code>" in text
    assert "Display 0</code> -" not in text and "上下文：1.0M · 🧠 · 🖼" in text
    numeric = [b["text"] for b in kb["inline_keyboard"][0]]
    assert numeric == ["1", "2", "3", "4", "5", "6"]


def test_effective_metadata_ui_matches_runtime_and_reports_authority(monkeypatch, account_config):
    account_key = "openai:a@x:ws-a"
    scope = f"oauth:{account_key}"
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        models=["rich-model"],
        account_model_catalog={"models": [{
            "id": "rich-model", "name": "Upstream Name",
            "description": "Upstream description", "contextWindow": 872_000,
            "contextWindowMaxMode": 1_000_000,
            "supportsImages": True, "reasoningEfforts": ["high"],
        }]},
        last_model_sync_source="upstream:codex",
        last_model_sync="2026-08-24T01:02:03Z",
        last_model_sync_error="",
    ))
    config.update(lambda cfg: cfg.update(modelBindings={
        "defaults": {"rich-model": {"target": "demo/default", "source": "manual"}},
        "scoped": {scope: {"rich-model": {
            "target": "demo/scoped", "source": "manual",
            "outboundModel": "rich-model",
        }}},
    }))
    metadata = {
        "demo/default": {"contextWindow": 400_000, "reasoningEfforts": ["medium"]},
        "demo/scoped": {
            "contextWindow": 200_000, "reasoningEfforts": ["low"], "vision": False,
        },
    }
    monkeypatch.setattr(model_pricing, "catalog_metadata", lambda target: metadata.get(target))

    effective = model_metadata.get_metadata(
        "rich-model", scope_key=scope, outbound_model="rich-model",
    )
    listing, _ = oauth_account_models_menu.render(account_key)
    detail, detail_kb = oauth_account_models_menu._detail_render(
        account_key, "rich-model", model_page=1, account_page=1, filter_key="all",
    )
    assert effective["contextWindow"] == 200_000
    assert "上下文：200K · 🧠" in listing and "🖼" not in listing
    assert "思考档位：low" in listing
    assert "上下文: <code>200K</code>" in detail
    assert "元数据来源: <code>models.dev · 账户绑定</code>" in detail
    assert not any(
        str(button.get("callback_data") or "").startswith("oam:maxctx:")
        for row in detail_kb["inline_keyboard"] for button in row
    )

    config.update(lambda cfg: cfg["modelBindings"]["scoped"].clear())
    default_detail, _ = oauth_account_models_menu._detail_render(
        account_key, "rich-model", model_page=1, account_page=1, filter_key="all",
    )
    assert "上下文: <code>400K</code>" in default_detail
    assert "元数据来源: <code>models.dev · 默认绑定</code>" in default_detail

    config.update(lambda cfg: cfg["modelBindings"]["defaults"].clear())
    upstream_detail, _ = oauth_account_models_menu._detail_render(
        account_key, "rich-model", model_page=1, account_page=1, filter_key="all",
    )
    assert "上下文: <code>872K</code>" in upstream_detail
    assert "元数据来源: <code>账户上游目录</code>" in upstream_detail


def test_three_sync_failure_visibility_states(account_config):
    account_key = "openai:a@x:ws-a"
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        models=["lkg-model"],
        account_model_catalog={"models": [{"id": "lkg-model"}]},
        last_model_sync="2026-08-24T01:02:03Z",
        last_model_sync_error="timeout <unsafe>",
    ))
    lkg, _ = oauth_account_models_menu.render(account_key)
    assert "本次同步失败，正在使用上次成功目录" in lkg
    assert "timeout &lt;unsafe&gt;" in lkg
    assert "上次成功时间: <code>2026-08-24T01:02:03Z</code>" in lkg

    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        models=[], account_model_catalog={"models": [{"id": "gpt-default"}]},
        last_model_sync="", last_model_sync_error="first failure",
    ))
    first_default, _ = oauth_account_models_menu.render(account_key)
    assert "正在使用默认模型" in first_default
    assert "<code>gpt-default</code>" in first_default

    config.update(lambda cfg: cfg.update(oauthAccounts=[{
        "provider": "cursor", "subject": "new-cursor", "label": "Cursor New",
        "models": [], "cursor_model_catalog": {"models": [{"id": "stale"}]},
        "last_model_sync_error": "first failure",
    }]))
    cursor_zero, _ = oauth_account_models_menu.render("cursor:new-cursor")
    assert "共 0 个" in cursor_zero
    assert "没有可用账户目录，后台会重试" in cursor_zero
    assert "<code>stale</code>" not in cursor_zero
