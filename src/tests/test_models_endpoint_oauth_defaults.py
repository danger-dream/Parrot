"""Public discovery uses Bot defaults without narrowing internal routing."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from ._isolation import isolate

isolate()

import server as parrot_server
from src import config, drain, model_mapping, scheduler, state_db
from src.channel import registry
from src.cursor_bridge import catalog as cursor_catalog
from src.cursor_bridge import runtime as cursor_runtime
from src.cursor_bridge.models import CursorModel
from src.openai.channel.registration import register_factories
from src.telegram.menus import oauth_defaults_menu

PROVIDERS = ("claude", "openai", "xai", "antigravity")
SECTIONS = {
    "openai": "openaiOAuth",
    "xai": "xaiOAuth",
    "antigravity": "antigravityOAuth",
}


def _set_defaults(cfg, provider, value):
    if provider == "claude":
        cfg["oauthDefaultModels"] = value
    else:
        cfg.setdefault(SECTIONS[provider], {})["defaultModels"] = value


def _defaults(provider):
    cfg = config.get()
    if provider == "claude":
        return cfg["oauthDefaultModels"]
    return cfg[SECTIONS[provider]]["defaultModels"]


@pytest.fixture(autouse=True)
def discovery_config(monkeypatch, tmp_path):
    """Scope config/cache/hooks/registry to this test, not the shared suite.

    Only isolate storage and singleton ownership: auth, channel construction,
    config updates/reloads, and the server ASGI endpoint all run real code.
    """
    drain.reset_for_tests()
    cfg = copy.deepcopy(config.get())
    cfg.update({
        "oauthAccounts": [],
        "channels": [],
        "apiKeys": {"discovery": {
            "key": "test-discovery-key", "enabled": True, "allowedModels": [],
        }},
        "modelMapping": {"global": {}},
        "modelBindings": {"defaults": {}, "scoped": {}},
    })
    for provider in PROVIDERS:
        section = SECTIONS.get(provider)
        if section:
            cfg[section] = copy.deepcopy(config.DEFAULT_CONFIG[section])
        _set_defaults(cfg, provider, [])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0.0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(registry, "_channels", {})
    monkeypatch.setattr(registry, "_channel_factories", {})
    config.reload()
    state_db.init()
    register_factories()
    registry.install_config_reload_hook()
    registry.rebuild_from_config()
    try:
        yield
    finally:
        cursor_runtime.stop()
        drain.reset_for_tests()


def _account(provider, models, **patch):
    account = {
        "provider": provider,
        "email": f"discovery-{provider}@example.test",
        "enabled": True,
        "disabled_reason": None,
        "models": list(models),
        "account_model_catalog": {"models": [
            {"id": model, "contextWindow": 128_000} for model in models
        ]},
    }
    if provider == "antigravity":
        account["project_id"] = "discovery-project"
    if provider == "cursor":
        account["subject"] = "discovery-cursor"
        account["cursor_model_catalog"] = cursor_catalog.build_catalog([
            CursorModel(
                id=model, name=model, reasoning=False, context_window=128_000,
                max_tokens=16_000, supports_images=False, supports_max_mode=False,
                supports_agent=True,
            ) for model in models
        ])
    account.update(patch)
    return account


def _install(provider, models, defaults, **patch):
    account = _account(provider, models, **patch)

    def mutate(cfg):
        _set_defaults(cfg, provider, defaults)
        cfg["oauthAccounts"] = [account]

    config.update(mutate)
    channels = registry.all_channels()
    assert len(channels) == 1, "fixture must construct a real OAuth channel"
    return channels[0]


async def _get(headers=None):
    transport = httpx.ASGITransport(app=parrot_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(
            "/v1/models",
            headers={"x-api-key": "test-discovery-key"} if headers is None else headers,
        )


async def _ids():
    response = await _get()
    assert response.status_code == 200
    return [item["id"] for item in response.json()["data"]]


async def test_openai_eight_account_models_exposes_four_bot_defaults():
    models = [f"gpt-discovery-{index}" for index in range(8)]
    defaults = [models[index] for index in (6, 0, 4, 2)]
    ch = _install("openai", models, defaults)
    accounts_before = copy.deepcopy(config.get()["oauthAccounts"])

    assert await _ids() == sorted(defaults)
    assert ch.models == models
    assert config.get()["oauthAccounts"] == accounts_before
    assert registry.available_models() == sorted(models)
    assert registry.available_models_for_families({"openai"}) == sorted(models)
    assert model_mapping.list_available_models_for("openai-responses") == sorted(models)
    # Discovery is not an authorization or scheduling allowlist.
    explicit = models[1]
    assert ch.supports_model(explicit) == explicit
    available, _, _, _ = scheduler._filter_candidates(
        explicit, "responses", {"model": explicit, "input": "hello"},
    )
    assert (ch, explicit) in available


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_provider_defaults_intersect_only_its_supported_ids(provider):
    selected, extra = f"{provider}-selected", f"{provider}-extra"
    ch = _install(provider, [extra, selected], [selected, "missing", selected])

    assert await _ids() == [selected]
    assert ch.supports_model(extra) == extra
    family = "anthropic" if provider == "claude" else "openai"
    assert registry.available_models_for_families({family}) == sorted([extra, selected])
    family = "anthropic" if provider == "claude" else provider
    assert oauth_defaults_menu._read_list(family) == [selected, "missing", selected]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("account_models", [[], ["account-only"]], ids=["fallback", "lkg"])
async def test_explicit_empty_defaults_never_fall_back_for_discovery(provider, account_models):
    _install(provider, account_models, [])
    assert await _ids() == []
    assert _defaults(provider) == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_defaults_without_a_routable_match_are_not_advertised(provider):
    _install(provider, ["account-only"], ["not-on-this-account"])
    assert await _ids() == []


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("state", ["absent", "off", "reason"])
async def test_default_requires_enabled_channel_without_disabled_reason(provider, state):
    patch = {"enabled": False} if state == "off" else {"disabled_reason": "auth_error"}
    _install(provider, ["selected"], ["selected"], **patch)
    if state == "absent":
        config.update(lambda cfg: cfg.update(oauthAccounts=[]))
    assert await _ids() == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_disabled_models_remain_excluded(provider):
    ch = _install(provider, ["enabled", "disabled", "extra"], ["enabled", "disabled"],
                  disabledModels=["disabled"])
    assert await _ids() == ["enabled"]
    assert ch.supports_model("disabled") is None


async def test_openai_plan_unsupported_default_is_not_advertised():
    ch = _install("openai", ["gpt-5.2-codex", "gpt-5.4"],
                  ["gpt-5.2-codex", "gpt-5.4"], plan_type="plus")
    assert ch.supports_model("gpt-5.2-codex") is None
    assert await _ids() == ["gpt-5.4"]
    # The old internal catalog contract must not silently change.
    assert registry.available_models() == ["gpt-5.2-codex", "gpt-5.4"]


@pytest.mark.parametrize("owner", PROVIDERS)
async def test_another_oauth_provider_cannot_prove_a_default_supported(owner):
    def mutate(cfg):
        _set_defaults(cfg, owner, ["cross-provider"])
        cfg["oauthAccounts"] = [
            _account(provider, ["own-only" if provider == owner else "cross-provider"])
            for provider in PROVIDERS
        ]

    config.update(mutate)
    assert len(registry.all_channels()) == len(PROVIDERS)
    assert await _ids() == []


async def test_same_provider_channels_union_support_without_disabled_route_leaks():
    def mutate(cfg):
        _set_defaults(cfg, "openai", ["one", "two", "disabled-only", "absent"])
        cfg["oauthAccounts"] = [
            _account("openai", ["one", "extra"], email="one@example.test"),
            _account("openai", ["two", "extra"], email="two@example.test"),
            _account("openai", ["disabled-only"], email="off@example.test", enabled=False),
        ]

    config.update(mutate)
    assert await _ids() == ["one", "two"]


@pytest.mark.parametrize("protocol", ["anthropic", "openai-chat", "openai-responses"])
async def test_api_and_cursor_preserve_catalog_and_oauth_hidden_same_ids(protocol):
    def mutate(cfg):
        _set_defaults(cfg, "openai", ["selected"])
        cfg["oauthAccounts"] = [
            _account("openai", ["shared-api", "shared-cursor", "selected", "oauth-only"]),
            _account("cursor", ["cursor-only", "shared-cursor"]),
        ]
        cfg["channels"] = [{
            "name": "discovery-api", "protocol": protocol,
            "baseUrl": "http://127.0.0.1:1", "apiKey": "test-upstream",
            "enabled": True, "disabled_reason": None,
            "models": [
                {"real": "upstream-only", "alias": "api-alias"},
                {"real": "shared-api", "alias": "shared-api"},
            ],
        }]

    config.update(mutate)
    assert len(registry.all_channels()) == 3
    assert await _ids() == ["api-alias", "cursor-only", "selected", "shared-api", "shared-cursor"]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_bot_edit_and_disk_hot_reload_immediately_change_discovery(provider):
    _install(provider, ["first", "second", "third"], ["first"])
    family = "anthropic" if provider == "claude" else provider
    assert await _ids() == ["first"]
    oauth_defaults_menu._write_list(family, ["second"])
    assert await _ids() == ["second"]
    assert oauth_defaults_menu._read_list(family) == ["second"]

    # Publish via config's atomic file writer; the next GET notices the mtime.
    path = Path(config.path())
    cfg = json.loads(path.read_text(encoding="utf-8"))
    _set_defaults(cfg, provider, ["third"])
    config._write_atomic(cfg)
    # Fast temp-file replacements can share the filesystem's timestamp tick.
    # Exercise the existing mtime reload contract deterministically, not sleep.
    changed_mtime = config._mtime + 1
    os.utime(path, (changed_mtime, changed_mtime))
    assert await _ids() == ["third"]
    oauth_defaults_menu._write_list(family, [])
    assert await _ids() == []
    assert oauth_defaults_menu._read_list(family) == []


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("raw", [None, "selected", {"selected": True}, 42])
async def test_invalid_default_list_is_not_iterated_as_ids(provider, raw):
    _install(provider, ["selected", "s", "e"], ["selected"])
    # A malformed Claude default may leave the last published registry intact
    # (existing rebuild behavior); discovery must still fail closed, not use it.
    config.update(lambda cfg: _set_defaults(cfg, provider, raw))
    assert await _ids() == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_default_list_ignores_non_strings_and_blank_ids(provider):
    _install(provider, ["selected", "extra"], [None, 1, {}, [], "", "  ", "selected"])
    assert await _ids() == ["selected"]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_missing_default_uses_normal_config_backfill(provider):
    if provider == "claude":
        built_in = config.DEFAULT_CONFIG["oauthDefaultModels"]
    else:
        built_in = config.DEFAULT_CONFIG[SECTIONS[provider]]["defaultModels"]
    selected = built_in[0]
    _install(provider, [selected, "account-extra"], [])
    cfg = copy.deepcopy(config.get())
    if provider == "claude":
        cfg.pop("oauthDefaultModels")
    else:
        cfg[SECTIONS[provider]].pop("defaultModels")
    config._write_atomic(cfg)
    config.reload()
    assert await _ids() == [selected]


@pytest.mark.parametrize(("allowed", "expected"), [
    (["not-present"], []),
    (["extra"], []),
    (["selected"], ["selected"]),
    (["alias"], []),
    (["selected", "alias"], ["alias", "selected"]),
    (["extra", "hidden-alias"], []),
    ([], ["alias", "selected"]),
])
async def test_allowlist_and_global_alias_require_visible_target_and_alias_permission(allowed, expected):
    _install("openai", ["selected", "extra"], ["selected"])

    def mutate(cfg):
        cfg["apiKeys"]["discovery"]["allowedModels"] = allowed
        cfg["modelMapping"] = {
            "global": {"alias": "selected", "hidden-alias": "extra", "stale": "absent"},
            "anthropic": {"legacy-anthropic": "selected"},
            "openai-chat": {"legacy-chat": "selected"},
            "openai-responses": {"legacy-responses": "selected"},
        }

    config.update(mutate)
    assert await _ids() == expected


async def test_metadata_and_bindings_cannot_add_discovery_ids_or_response_fields():
    _install("openai", ["selected", "extra"], ["selected", "catalog-only", "binding-only"])

    def mutate(cfg):
        cfg["oauthAccounts"][0]["account_model_catalog"]["models"].append({
            "id": "catalog-only", "contextWindow": 872_000,
        })
        cfg["modelBindings"]["defaults"] = {"binding-only": {"target": "openai/gpt-5.4"}}

    config.update(mutate)
    response = await _get({"authorization": "Bearer test-discovery-key"})
    assert response.status_code == 200
    assert response.json() == {
        "data": [{"type": "model", "id": "selected", "display_name": "selected",
                  "created_at": "2025-01-01T00:00:00Z"}],
        "first_id": "selected", "last_id": "selected", "has_more": False,
    }


async def test_empty_response_schema_is_unchanged():
    _install("openai", ["extra"], [])
    response = await _get()
    assert response.status_code == 200
    assert response.json() == {"data": [], "first_id": None, "last_id": None, "has_more": False}


@pytest.mark.parametrize(("headers", "disabled", "message"), [
    ({}, False, "Missing API key"),
    ({"x-api-key": "invalid"}, False, "Invalid API key"),
    ({"x-api-key": "test-discovery-key"}, True, "API key is disabled"),
])
async def test_auth_failure_remains_401(headers, disabled, message):
    _install("openai", ["selected", "extra"], ["selected"])
    if disabled:
        config.update(lambda cfg: cfg["apiKeys"]["discovery"].update(enabled=False))
    response = await _get(headers)
    assert response.status_code == 401
    assert response.json() == {
        "type": "error", "error": {"type": "authentication_error", "message": message},
    }


def test_bot_overview_documents_discovery_and_existing_fallback():
    text = oauth_defaults_menu._overview_text()
    assert "/v1/models" in text
    assert "兜底" in text
    assert "显式" in text
