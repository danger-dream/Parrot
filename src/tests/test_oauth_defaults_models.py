"""OAuth fallback retirement: no static routes, no old writes, real LKG survives."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from src import config, oauth_manager, oauth_model_discovery, state_db
from src.channel import registry
from src.channel.oauth_channel import OAuthChannel
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
from src.management_control.oauth import OAuthBackend, OAuthControl
from src.openai.codex_constants import current_codex_protocol_profile
from src.telegram import bot, states, ui
from src.telegram.menus import oauth_account_models_menu, oauth_menu

RETIRED_OPERATIONS = {
    "getOAuthDefaultModels", "replaceOAuthDefaultModels", "discoverOAuthDefaultModels",
}
CHANNELS = {
    "claude": OAuthChannel, "openai": OpenAIOAuthChannel,
    "xai": XAIOAuthChannel, "antigravity": AntigravityOAuthChannel,
}


def legacy_fields(cfg):
    cfg["oauthDefaultModels"] = ["retired-static"]
    for section in ("openaiOAuth", "xaiOAuth", "antigravityOAuth"):
        cfg.setdefault(section, {})["defaultModels"] = ["retired-static"]
    cfg.setdefault("oauth", {}).setdefault("providers", {}).setdefault("openai", {})["defaultModels"] = ["retired-static"]


def assert_retired(cfg):
    assert "oauthDefaultModels" not in cfg
    for section in ("openaiOAuth", "xaiOAuth", "antigravityOAuth"):
        assert "defaultModels" not in cfg.get(section, {})
    assert "defaultModels" not in cfg.get("oauth", {}).get("providers", {}).get("openai", {})


@pytest.fixture
def domain(monkeypatch):
    state_db.init()
    before = copy.deepcopy(config.get())
    channels_before = registry._channels.copy()
    states.clear_all()
    monkeypatch.setattr(registry, "_sync_state_db_with_channels", lambda: None)
    yield
    config.update(lambda cfg: (cfg.clear(), cfg.update(before)))
    registry._channels = channels_before
    states.clear_all()


@pytest.mark.parametrize("provider", CHANNELS)
@pytest.mark.asyncio
async def test_catalog_success_routes_failure_and_empty_keep_lkg(provider, domain, monkeypatch):
    account = {
        "provider": provider, "email": f"retirement-{provider}@example.invalid",
        "subject": "subject", "workspace_id": "workspace", "project_id": "project",
        "models": [], "access_token": "fake", "refresh_token": "fake",
        "expired": "2999-01-01T00:00:00Z", "enabled": True,
    }
    config.update(lambda cfg: cfg.update(oauthAccounts=[account], channels=[], modelMapping={}))
    key = oauth_manager.get_account_key(account)

    async def token(_key):
        return "fake"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    empty = oauth_model_discovery.DiscoveryResult([], {}, f"upstream:{provider}")
    monkeypatch.setattr(oauth_model_discovery, "discover", lambda *a, **kw: empty)
    assert (await oauth_manager.refresh_account_models(key))["action"] == "empty"
    assert oauth_manager.account_model_selection(key)["models"] == []
    assert CHANNELS[provider](oauth_manager.get_account(key)).list_client_models() == []
    registry.rebuild_from_config()
    assert registry.available_models() == []
    text, kb = oauth_account_models_menu.render(key)
    assert "当前无可路由模型" in text and "同步上游" in text
    assert "正在使用默认模型" not in text
    assert not any(b.get("callback_data", "").startswith("odm:") for row in kb["inline_keyboard"] for b in row)

    catalog = {"schema": 1, "models": [{"id": "live-model", "contextWindow": 12345}]}
    success = oauth_model_discovery.DiscoveryResult(["live-model"], catalog, f"upstream:{provider}")
    monkeypatch.setattr(oauth_model_discovery, "discover", lambda *a, **kw: success)
    assert (await oauth_manager.refresh_account_models(key))["action"] == "updated"
    good = oauth_manager.get_account(key)
    lkg = copy.deepcopy(good["account_model_catalog"])
    synced_at = good["last_model_sync"]
    registry.rebuild_from_config()
    assert registry.available_models() == ["live-model"]
    assert registry.get_channel("oauth:" + key).supports_model("live-model") == "live-model"

    def timeout(*a, **kw):
        raise TimeoutError("fake timeout")

    def error(*a, **kw):
        raise RuntimeError("fake unavailable")

    for discover, action in ((timeout, "timeout"), (lambda *a, **kw: empty, "empty"), (error, "error")):
        monkeypatch.setattr(oauth_model_discovery, "discover", discover)
        assert (await oauth_manager.refresh_account_models(key))["action"] == action
        saved = oauth_manager.get_account(key)
        assert saved["models"] == ["live-model"]
        assert saved["account_model_catalog"] == lkg
        assert saved["last_model_sync"] == synced_at
        assert saved["last_model_sync_error"]
        registry.rebuild_from_config()
        assert registry.available_models() == ["live-model"]
        assert CHANNELS[provider](saved).supports_model("live-model") == "live-model"
        text, _ = oauth_account_models_menu.render(key)
        assert "正在使用上次成功目录" in text


@pytest.mark.parametrize("provider", CHANNELS)
def test_runtime_ignores_retired_fields_even_without_migration(provider, monkeypatch):
    cfg = copy.deepcopy(config.get())
    legacy_fields(cfg)
    monkeypatch.setattr(config, "get", lambda: cfg)
    account = {"provider": provider, "email": "retired@example.invalid", "project_id": "project", "models": []}
    selected = oauth_manager.account_model_selection(account)
    assert selected["models"] == selected["effective_models"] == selected["records"] == []
    assert selected["source"].endswith(":awaiting-account-catalog")
    channel = CHANNELS[provider](account)
    assert channel.list_client_models() == []
    assert channel.supports_model("retired-static") is None
    profile = current_codex_protocol_profile()
    assert profile.models  # packaged protocol metadata is not retired
    for model in profile.models:
        assert channel.supports_model(model) is None
    account["models"] = ["live-model", "disabled-model"]
    account["disabledModels"] = ["disabled-model"]
    assert CHANNELS[provider](account).list_client_models() == ["live-model"]


def test_load_save_migrates_only_retired_fields(tmp_path, monkeypatch):
    original = copy.deepcopy(config.get())
    legacy_fields(original)
    original.update(
        image_models={"openai": ["image-custom"], "xai": ["grok-image-custom"]},
        video_models={"xai": ["video-custom"]},
        oauthAccounts=[],
        channels=[{"name": "api", "models": [{"alias": "client", "real": "upstream"}]}],
    )
    original["xaiOAuth"]["imageModels"] = ["legacy-image"]
    original["xaiOAuth"]["videoModels"] = ["legacy-video"]
    original["antigravityOAuth"]["imageModels"] = ["ag-image"]
    keep = {key: copy.deepcopy(original[key]) for key in (
        "video_models", "channels", "cursorOAuth", "workbuddyOAuth",
    ) if key in original}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    loaded = config.get()
    assert_retired(loaded)
    assert_retired(json.loads(path.read_text()))
    for key, value in keep.items():
        assert loaded[key] == value
    assert loaded["openaiOAuth"]["codexIdentity"] == original["openaiOAuth"]["codexIdentity"]
    assert loaded["openaiOAuth"]["codexProtocolProfile"] == original["openaiOAuth"]["codexProtocolProfile"]
    assert loaded["openaiOAuth"]["codexCliVersion"] == original["openaiOAuth"]["codexCliVersion"]
    assert loaded["xaiOAuth"]["imageModels"] == ["legacy-image"]
    assert loaded["xaiOAuth"]["videoModels"] == ["legacy-video"]
    assert "imageModels" not in loaded["antigravityOAuth"]
    assert loaded["image_models"] == {
        "openai": ["image-custom"],
        "xai": ["grok-image-custom"],
        "antigravity": ["ag-image"],
    }
    assert config._migrate_antigravity_image_models(loaded) is False
    assert config._retire_oauth_default_models(loaded) is False
    legacy_fields(loaded)
    config.save()
    assert_retired(loaded)
    assert_retired(json.loads(path.read_text()))
    config.update(legacy_fields)
    assert_retired(config.get())
    assert_retired(json.loads(path.read_text()))
    assert_retired(config.DEFAULT_CONFIG)
    assert_retired(json.loads(Path("config.example.json").read_text()))


def test_antigravity_empty_legacy_image_list_stays_disabled_after_migration():
    legacy = {
        "antigravityOAuth": {"imageModels": []},
        "xaiOAuth": {"imageModels": ["xai-custom"]},
        "images": {"toolModel": "openai-custom"},
    }
    assert config._migrate_antigravity_image_models(legacy) is True
    assert legacy["image_models"]["antigravity"] == []
    assert legacy["image_models"]["xai"] == ["xai-custom"]
    assert "openai-custom" in legacy["image_models"]["openai"]
    assert "imageModels" not in legacy["antigravityOAuth"]
    assert config._migrate_antigravity_image_models(legacy) is False


def test_removed_control_and_schema_surface():
    for name in ("get_default_models", "replace_default_models", "replace_default_models_raw",
                 "discover_default_models", "default_models_snapshot", "static_default_models_snapshot",
                 "scan_default_model_references"):
        assert not hasattr(OAuthControl, name)
    for name in ("default_models", "static_default_models", "replace_default_models",
                 "replace_default_models_conditional", "default_models_state"):
        assert not hasattr(OAuthBackend, name)
    assert importlib.util.find_spec("src.telegram.menus.oauth_defaults_menu") is None
    assert importlib.util.find_spec("src.management_control.oauth.default_models") is None
    assert not hasattr(bot, "oauth_defaults_menu")


def test_retired_api_routes_cannot_write_or_create_operations(tmp_path):
    from src.tests.test_management_oauth_api import auth_client, request
    client, headers, runtime, control, backend = auth_client(tmp_path)
    before = copy.deepcopy(backend.accounts)
    operations = len(runtime.operations._items)
    try:
        for family in ("anthropic", "openai", "xai", "antigravity"):
            for method, suffix, body in (
                ("GET", "", None), ("PUT", "", {"models": ["fake"], "cleanupReferences": True}),
                ("POST", "/actions/discover", None),
            ):
                for auth in ({}, headers):
                    response = request(client, method, f"/oauth/default-models/{family}{suffix}", body, auth)
                    assert response.status_code == 404, response.text
        schema = client.app.openapi()
        assert not any("/oauth/default-models" in path for path in schema["paths"])
        assert not any("DefaultModel" in name for name in schema["components"]["schemas"])
        assert backend.accounts == before
        assert len(runtime.operations._items) == operations
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("callback", ["odm:show", "odm:edit:openai", "odm:ok", "odm:commit:old:clean", "odm:retry"])
def test_old_tg_buttons_are_read_only(callback, monkeypatch):
    messages = []
    states.clear_all()
    ui.configure("fake", [42])
    monkeypatch.setattr(ui, "answer_cb", lambda _cb, text="", **kw: messages.append(text))
    monkeypatch.setattr(config, "update", lambda *a, **kw: pytest.fail("retired TG callback wrote config"))
    states.set_state(42, "odm_edit:openai", {"existing_models": ["old"]})
    bot._handle_callback({"id": "cb", "data": callback, "message": {"message_id": 1, "chat": {"id": 42}}})
    assert states.get_state(42) is None
    assert any("已退役" in text and "同步上游模型" in text for text in messages)


@pytest.mark.parametrize("text,action", [("/oauth_defaults", None), ("new-model", "odm_edit:openai"), ("new-model", "odm_model_select")])
def test_old_tg_command_and_text_cannot_write(text, action, monkeypatch):
    messages = []
    states.clear_all()
    ui.configure("fake", [42])
    monkeypatch.setattr(ui, "send", lambda _chat, message, **kw: messages.append(message))
    monkeypatch.setattr(config, "update", lambda *a, **kw: pytest.fail("retired TG text wrote config"))
    if action:
        states.set_state(42, action, {})
    bot._handle_message({"chat": {"id": 42}, "text": text})
    assert states.get_state(42) is None
    assert any("已退役" in message for message in messages)
    settings, kb = oauth_menu._settings_text_and_kb()
    assert "备用模型" not in settings
    assert not any(b.get("callback_data", "").startswith("odm:") for row in kb["inline_keyboard"] for b in row)
