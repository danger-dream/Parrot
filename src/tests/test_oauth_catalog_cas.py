"""Formal OAuth catalog commits reject concurrent directory edits, not purpose edits."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from src import config, oauth_manager, oauth_model_discovery


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    before = copy.deepcopy(config.get())
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)

    async def token(_key):
        return "fake-token"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    yield
    config.update(lambda cfg: (cfg.clear(), cfg.update(before)))


def install(provider):
    account = {
        "provider": provider, "email": "catalog-cas@example.invalid", "subject": "catalog-cas",
        "workspace_id": "catalog-cas-workspace", "project_id": "catalog-cas-project",
        "access_token": "fake-token", "refresh_token": "fake-refresh",
        "generationId": "catalog-cas-generation", "models": ["lkg"],
        "enabled": True, "last_model_sync": "2026-01-01T00:00:00Z",
        "last_model_sync_attempt": "2026-01-01T00:00:00Z",
        "last_model_sync_source": f"upstream:{provider}", "last_model_sync_error": "",
    }
    catalog_key = "cursor_model_catalog" if provider == "cursor" else "account_model_catalog"
    account[catalog_key] = {"schema": 1, "models": [{"id": "lkg", "contextWindow": 12345}]}
    config.update(lambda cfg: cfg.update(oauthAccounts=[account], channels=[]))
    return oauth_manager.get_account_key(account), catalog_key


def edit_catalog(key, catalog_key, field):
    def mutate(cfg):
        account = cfg["oauthAccounts"][0]
        if field == "models":
            account["models"] = ["user-edited"]
        elif field == "catalog":
            account[catalog_key]["models"][0]["contextWindow"] = 99999
        elif field == "models_etag":
            account["models_etag"] = "newer-etag"
        else:
            account[field] = "user-newer-sync-fact"
    config.update(mutate)
    return copy.deepcopy(oauth_manager.get_account(key))


def result(provider, *, not_modified=False):
    return oauth_model_discovery.DiscoveryResult(
        ["late-model"], {"schema": 1, "models": [{"id": "late-model"}]},
        f"upstream:{provider}", not_modified=not_modified, etag="late-etag",
    )


@pytest.mark.parametrize("provider,not_modified", [
    ("claude", False), ("openai", False), ("xai", False), ("antigravity", False),
    ("openai", True),
])
@pytest.mark.parametrize("field", ["models", "catalog", "last_model_sync"])
def test_success_and_304_commit_compare_catalog_write_set(monkeypatch, provider, not_modified, field):
    key, catalog_key = install(provider)
    edited = {}

    def discover(*args, **kwargs):
        edited.update(edit_catalog(key, catalog_key, field))
        return result(provider, not_modified=not_modified)

    monkeypatch.setattr(oauth_model_discovery, "discover", discover)
    outcome = asyncio.run(oauth_manager.refresh_account_models(key))
    assert outcome["action"] == "stale"
    assert oauth_manager.get_account(key) == edited
    assert json.loads(Path(config.path()).read_text())["oauthAccounts"][0] == edited


@pytest.mark.parametrize("not_modified", [False, True])
def test_codex_etag_concurrency_rejects_late_commit(monkeypatch, not_modified):
    key, catalog_key = install("openai")

    def discover(*args, **kwargs):
        edit_catalog(key, catalog_key, "models_etag")
        return result("openai", not_modified=not_modified)

    monkeypatch.setattr(oauth_model_discovery, "discover", discover)
    outcome = asyncio.run(oauth_manager.refresh_account_models(key))
    assert outcome["action"] == "stale"
    assert oauth_manager.get_account(key)["models_etag"] == "newer-etag"
    assert oauth_manager.get_account(key)["models"] == ["lkg"]


@pytest.mark.parametrize("unified", [False, True])
@pytest.mark.parametrize("field", ["models", "catalog", "last_model_sync"])
def test_cursor_native_and_unified_commit_return_stale_without_overwrite(monkeypatch, unified, field):
    key, catalog_key = install("cursor")
    edited = {}

    def fetch(*args, **kwargs):
        edited.update(edit_catalog(key, catalog_key, field))
        return {"models": [{"id": "late-model"}], "fetched_at": "2030-01-01T00:00:00Z"}

    monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_model_catalog_sync", fetch)
    monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_profile_sync", lambda *a, **kw: {})
    outcome = (asyncio.run(oauth_manager.refresh_account_models(key)) if unified
               else oauth_manager.refresh_cursor_models_sync(key, force=True))
    assert outcome["action"] == "stale"
    assert oauth_manager.get_account(key) == edited
    assert json.loads(Path(config.path()).read_text())["oauthAccounts"][0] == edited


@pytest.mark.parametrize("provider,not_modified", [("openai", True), ("xai", False), ("cursor", False)])
def test_purpose_and_disabled_changes_do_not_cause_catalog_conflict(monkeypatch, provider, not_modified):
    key, _ = install(provider)
    disabled_field = "cursor_disabled_models" if provider == "cursor" else "disabledModels"

    def mutate_purpose():
        def mutate(cfg):
            cfg["oauthAccounts"][0].update(enabled=False, **{disabled_field: ["late-model"]})
            cfg.setdefault("images", {})["enabled"] = False
            cfg.setdefault("videos", {})["independentAccounts"] = [key]
        config.update(mutate)

    def discover(*args, **kwargs):
        mutate_purpose()
        if provider == "cursor":
            return {"models": [{"id": "late-model"}]}
        return result(provider, not_modified=not_modified)

    if provider == "cursor":
        monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_model_catalog_sync", discover)
        monkeypatch.setattr(oauth_manager.cursor_provider, "fetch_profile_sync", lambda *a, **kw: {})
    else:
        monkeypatch.setattr(oauth_model_discovery, "discover", discover)
    outcome = asyncio.run(oauth_manager.refresh_account_models(key))
    assert outcome["action"] == ("not_modified" if not_modified else "updated")
    saved = oauth_manager.get_account(key)
    assert saved["models"] == (["lkg"] if not_modified else ["late-model"])
    assert saved["enabled"] is False and saved[disabled_field] == ["late-model"]
    assert config.get()["images"]["enabled"] is False
    assert config.get()["videos"]["independentAccounts"] == [key]
    assert saved["last_model_sync_error"] == ""


def test_directory_edit_during_token_await_is_not_rebased_away(monkeypatch):
    key, catalog_key = install("openai")

    async def token(_key):
        edit_catalog(key, catalog_key, "models")
        config.update(lambda cfg: cfg["oauthAccounts"][0].update(access_token="new-token"))
        return "new-token"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    monkeypatch.setattr(oauth_model_discovery, "discover", lambda *a, **kw: pytest.fail("stale catalog must not be fetched"))
    outcome = asyncio.run(oauth_manager.refresh_account_models(key))
    assert outcome["action"] == "stale"
    assert oauth_manager.get_account(key)["models"] == ["user-edited"]
