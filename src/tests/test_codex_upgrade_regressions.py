"""Upgrade compatibility for released account/config shapes (no live network)."""
from __future__ import annotations

import base64
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src import config, oauth_manager, state_db
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.openai import codex_identity as identity
from src.openai.codex_constants import codex_protocol_profile, resolve_codex_model_policy
from src.state_store import StateStore


@pytest.fixture(autouse=True)
def isolated_upgrade_state(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"),
                       manifest_path=str(tmp_path / "manifest.json"))
    store.start()
    monkeypatch.setattr(state_db, "_store", store)
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setenv("DISABLE_OAUTH_NETWORK_CALLS", "1")
    identity.clear_turn_mappings_for_tests()
    yield
    identity.clear_turn_mappings_for_tests()
    store.close()


def account(workspace="upgrade-ws", email="upgrade@example.test", **extra):
    return {
        "provider": "openai", "email": email, "chatgpt_account_id": workspace,
        "access_token": "test-access", "refresh_token": "test-refresh",
        "expired": "2999-01-01T00:00:00Z", "enabled": True,
        "models": ["gpt-5.5"],
        "account_model_catalog": {"models": [{"id": "gpt-5.5"}]},
        **extra,
    }


def jwt(workspace):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": workspace}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "e30." + payload + ".signature"


def load_legacy(accounts):
    with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"oauthAccounts": accounts, "channels": [],
                   "oauth": {"mockMode": True}}, f)
    config._cache = None
    return config.get()


def test_legacy_duplicate_workspace_converges_once_without_merging():
    first, second, third = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    accounts = [account(codexDeviceInstallationId=first),
                account(email="other@example.test", codexDeviceInstallationId=second),
                account("different-ws", codexDeviceInstallationId=third)]
    before = copy.deepcopy(accounts)
    loaded = load_legacy(accounts)
    migrated = loaded["oauthAccounts"]
    assert [a["codexDeviceInstallationId"] for a in migrated] == [first, first, third]
    assert [(a["email"], a["access_token"], a["refresh_token"]) for a in migrated] == [
        (a["email"], a["access_token"], a["refresh_token"]) for a in before]
    identity.sync_configured_identity_tombstones(migrated)
    assert config.reload()["oauthAccounts"] == migrated


def test_versioned_identity_wins_over_earlier_legacy_account():
    versioned = account(email="versioned@example.test")
    profile = codex_protocol_profile().profile_id
    identity.normalize_account_identity(versioned, protocol_profile=profile)
    legacy = account(codexDeviceInstallationId=str(uuid.uuid4()))
    accounts = [legacy, versioned]
    assert identity.normalize_account_identities(accounts, protocol_profile=profile)
    assert legacy["codexIdentity"] == versioned["codexIdentity"]
    assert accounts[0] is legacy


def test_tombstone_wins_over_conflicting_legacy_uuid():
    registered = account()
    profile = codex_protocol_profile().profile_id
    identity.normalize_account_identity(registered, protocol_profile=profile)
    identity.register_account_identity(registered)
    legacy = [account(codexDeviceInstallationId=str(uuid.uuid4())) for _ in range(2)]
    identity.normalize_account_identities(legacy, protocol_profile=profile)
    assert all(a["codexDeviceInstallationId"] == registered["codexDeviceInstallationId"] for a in legacy)


def test_converged_away_uuid_does_not_rotate_other_workspace():
    first, spare = str(uuid.uuid4()), str(uuid.uuid4())
    accounts = [account(codexDeviceInstallationId=first),
                account(email="second@example.test", codexDeviceInstallationId=spare),
                account("other-ws", codexDeviceInstallationId=spare)]
    identity.normalize_account_identities(accounts, protocol_profile=codex_protocol_profile().profile_id)
    assert [a["codexDeviceInstallationId"] for a in accounts] == [first, first, spare]


@pytest.mark.parametrize("field", ["access_token", "id_token"])
def test_missing_workspace_backfilled_from_saved_token_without_refresh(field, monkeypatch):
    legacy = account("", **{field: jwt("claims-workspace")})
    loaded = load_legacy([legacy])["oauthAccounts"][0]
    assert loaded["workspace_id"] == "claims-workspace"
    assert loaded[field] == legacy[field]
    assert identity.account_identity_from_account(loaded) is not None
    assert config.reload()["oauthAccounts"][0] == loaded


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["responses", "realtime"])
async def test_valid_token_missing_workspace_refreshes_once_before_dispatch(monkeypatch, transport):
    old = load_legacy([account("")])["oauthAccounts"][0]
    calls = []

    def refresh(token, **kwargs):
        calls.append(token)
        return {"access_token": "renewed-access", "refresh_token": "renewed-refresh",
                "expires_in": 3600, "id_token": jwt("refreshed-ws")}

    monkeypatch.setattr(oauth_manager.openai_provider, "refresh_sync", refresh)
    ch = OpenAIOAuthChannel(old)
    if transport == "responses":
        request = await ch.build_upstream_request({"model": "gpt-5.5", "input": "hi"},
                                                  "gpt-5.5", ingress_protocol="responses")
        headers = request.headers
    else:
        headers = await ch.build_realtime_headers()
    assert headers["chatgpt-account-id"] == "refreshed-ws"
    current_key = "openai:upgrade@example.test:refreshed-ws"
    current = oauth_manager.get_account(current_key)
    assert current["access_token"] == "renewed-access"
    assert current["refresh_token"] == "renewed-refresh"
    assert identity.account_identity_from_account(current) is not None
    assert await oauth_manager.ensure_valid_token(current_key) == "renewed-access"
    assert calls == ["test-refresh"]


@pytest.mark.asyncio
async def test_missing_workspace_does_not_bypass_no_refresh_guard(monkeypatch):
    load_legacy([account("")])
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    def forbidden(*args, **kwargs):
        pytest.fail("refresh guard bypassed")
    monkeypatch.setattr(oauth_manager.openai_provider, "refresh_sync", forbidden)
    assert await oauth_manager.ensure_valid_token("openai:upgrade@example.test") == "test-access"
    with pytest.raises(ValueError, match="unknown|requires|unavailable"):
        await OpenAIOAuthChannel(oauth_manager.get_account("openai:upgrade@example.test")).build_realtime_headers()


def test_legacy_catalog_policy_preserves_old_models_and_current_lite():
    models = ["gpt-5.3-codex-spark", "gpt-6-astra", "gpt-5.5"]
    records = [{"id": model} for model in models]
    records[-1]["useResponsesLite"] = True  # explicit catalog policy always wins
    old = account(models=models, account_model_catalog={"models": records},
                  last_model_sync_error="TimeoutError")
    migrated = load_legacy([old])["oauthAccounts"][0]
    result = migrated["account_model_catalog"]["models"]
    assert [r["useResponsesLite"] for r in result] == [False, True, True]
    assert not resolve_codex_model_policy(models[0], result[0]).use_responses_lite
    assert migrated["models"] == models
    assert config.reload()["oauthAccounts"][0] == migrated


def test_new_catalog_unknown_model_still_fails_closed():
    current = account(models=["future-unknown"],
                      account_model_catalog={"models": [{"id": "future-unknown"}]},
                      last_model_sync_profile=codex_protocol_profile().profile_id)
    loaded = load_legacy([current])["oauthAccounts"][0]
    record = loaded["account_model_catalog"]["models"][0]
    assert "useResponsesLite" not in record
    with pytest.raises(ValueError, match="No explicit Responses Lite policy"):
        resolve_codex_model_policy("future-unknown", record)


def test_old_sync_failure_bypasses_backoff_once_then_current_failure_waits():
    now = datetime.now(timezone.utc)
    old = account(last_model_sync=now.isoformat(), last_model_sync_attempt=now.isoformat(),
                  last_model_sync_error="TimeoutError", last_model_sync_client_version="old",
                  last_model_sync_profile="old-profile")
    loaded = load_legacy([old])["oauthAccounts"][0]
    assert oauth_manager._model_sync_due(loaded, now=now)
    key = oauth_manager.get_account_key(loaded)
    assert oauth_manager._persist_model_discovery_failure(key, oauth_manager._discovery_generation(loaded), "timeout")
    failed = oauth_manager.get_account(key)
    assert failed["last_model_sync_client_version"] == "old"
    assert not oauth_manager._model_sync_due(failed, now=now)
    assert oauth_manager._model_sync_due(failed, now=now + timedelta(minutes=16))
    assert not oauth_manager._model_sync_due(config.reload()["oauthAccounts"][0], now=now)


def test_first_install_persists_selected_profile_immediately():
    first = config.get()
    profile = codex_protocol_profile(first["openaiOAuth"])
    saved = json.loads(open(config.CONFIG_PATH, encoding="utf-8").read())
    assert saved["openaiOAuth"]["codexCliVersion"] == profile.client_version
    assert saved["openaiOAuth"]["codexProtocolProfile"] == profile.profile_id
    assert config.reload() == first
