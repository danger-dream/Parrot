"""Global IDE discovery uses the authenticated CodeBuddyIDE craft catalog."""
from __future__ import annotations

import copy

import httpx
import pytest

from src import config, oauth_manager as om, oauth_model_discovery
from src.oauth.workbuddy import catalog, common
from src.tests.test_workbuddy_lifecycle import account


def credential():
    return {"provider": "workbuddy", "realm": "global", "domain": "www.codebuddy.ai",
            "uid": "fixture-global", "access_token": "fixture-at", "refresh_token": "fixture-rt",
            "workbuddy_client_profile": "ide"}


def catalog_data():
    return {"models": [
        {"id": "default-model", "name": "Auto", "maxInputTokens": 176000, "maxOutputTokens": 24000},
        {"id": "fast-model", "name": "Fast", "modelId": "fast-wire", "supportsImages": True,
         "supportsToolCall": False, "reasoning": {"effort": "medium", "summary": "auto", "canDisableThinking": False}},
        {"id": "disabled", "disabled": True}, {"id": "completion-1.0"}, {"id": "cli-only"},
    ], "agents": [
        {"name": "craft", "models": ["default-model", "fast-model", "disabled", "missing", "default-model"]},
        {"name": "CodeCompletion", "models": ["completion-1.0"]},
        {"name": "cli", "models": ["cli-only"]},
        {"name": "ide", "models": ["cli-only"]},
    ]}


def test_global_catalog_wire_and_discovery_metadata(monkeypatch):
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setattr(config, "get", lambda: {"oauth": {"mockMode": False}})
    calls = []
    def transport(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://www.codebuddy.ai/v3/config"
        assert request.headers["authorization"] == "Bearer fixture-at"
        assert request.headers["x-user-id"] == "fixture-global"
        assert request.headers["x-domain"] == "www.codebuddy.ai"
        assert request.headers["user-agent"] == "CodeBuddyIDE/4.12.1 CodeBuddy/4.12.1"
        assert request.headers["x-ide-name"] == request.headers["x-ide-type"] == "CodeBuddyIDE"
        assert request.headers["x-ide-version"] == "4.12.1"
        assert "x-refresh-token" not in request.headers
        return httpx.Response(200, json={"code": 0, "data": catalog_data()})
    def client(**kwargs):
        assert kwargs["proxy_purpose"] == "oauth_workbuddy"
        assert kwargs["proxy_channel"] == "oauth:fixture-global-key"
        assert kwargs["timeout"] == 7 and kwargs["follow_redirects"] is False
        return httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(common.network, "sync_client", client)
    result = oauth_model_discovery.discover_workbuddy(
        credential(), timeout=7, proxy_channel="oauth:fixture-global-key")
    assert len(calls) == 1
    assert result.models == ["default-model", "fast-model"]
    records = result.catalog["models"]
    assert records[0]["name"] == "Auto" and records[0]["source"] == "workbuddy:craft"
    assert records[0]["maxInputTokens"] == 176000 and records[0]["maxOutputTokens"] == 24000
    assert "reasoningEfforts" not in records[1]
    assert records[1]["wireModel"] == "fast-wire"
    assert records[1]["requestBodyGzip"] is True
    assert records[1]["canDisableThinking"] is False and records[1]["supportsToolCall"] is False
    assert records[1]["inputModalities"] == ["text", "image"]


@pytest.mark.parametrize("data,kind", [
    ({"enterpriseId": "", "productFeatures": {}}, "invalid_catalog"),
    ({"models": [], "agents": []}, "missing_craft_catalog"),
    ({"models": [{"id": "cli-only"}], "agents": [{"name": "cli", "models": ["cli-only"]}]}, "missing_craft_catalog"),
    ({"models": [{"id": "disabled", "disabled": True}], "agents": [{"name": "craft", "models": ["disabled"]}]}, "empty_catalog"),
])
def test_global_missing_directory_never_uses_other_agents(monkeypatch, data, kind):
    calls = []
    def request(entry, path, **kwargs):
        calls.append(path)
        return data
    monkeypatch.setattr(common, "request", request)
    with pytest.raises(common.WorkBuddyError) as error:
        catalog.fetch_models_sync(credential())
    assert error.value.kind == kind and calls == ["/v3/config"]


@pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 500])
def test_global_catalog_errors_do_not_switch_hosts_or_sources(monkeypatch, status):
    calls = []
    def request(entry, path, **kwargs):
        calls.append(path)
        raise common.WorkBuddyError("request", status=status)
    monkeypatch.setattr(common, "request", request)
    with pytest.raises(common.WorkBuddyError) as error:
        catalog.fetch_models_sync(credential())
    assert error.value.status_code == status and calls == ["/v3/config"]


def test_catalog_profile_aligns_modern_identity_and_preserves_legacy():
    entry = credential()
    for kind in ("anonymous", "account", "billing", "refresh"):
        headers = common.headers(entry, kind)
        assert headers["User-Agent"] == common.GLOBAL_AUTH_USER_AGENT
        assert headers["X-IDE-Type"] == headers["X-IDE-Name"] == "CodeBuddyIDE"
        assert headers["X-IDE-Version"] == "4.12.1"
    assert common.headers(entry, "chat")["User-Agent"] == common.GLOBAL_CHAT_USER_AGENT
    for realm, domain in (("cn", "www.codebuddy.cn"), ("global", "www.workbuddy.ai")):
        legacy = dict(entry, realm=realm, domain=domain, workbuddy_client_profile="cli")
        assert common.headers(legacy, "account")["User-Agent"] == (common.LEGACY_USER_AGENT if realm == "global" else common.USER_AGENT)
        assert common.headers(legacy, "account")["X-IDE-Type"] == "CLI"


async def test_global_sync_saves_directory_and_preserves_selection_on_failure(account, monkeypatch):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    config.update(lambda c: (c.setdefault("oauth", {}).update(mockMode=False),
        c["oauthAccounts"][0].update(realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide",
            disabledModels=["fast-model"], label="保留备注")))
    key = om.get_account_key(om.list_accounts()[0])
    monkeypatch.setattr(common, "request", lambda *a, **k: catalog_data())
    result = await om._discover_account_models_once(key, timeout_s=10)
    assert result["action"] == "updated", result
    saved = copy.deepcopy(om.get_account(key))
    assert saved["models"] == ["default-model", "fast-model"]
    records = om.account_model_records(saved)
    assert records[1]["wireModel"] == "fast-wire" and records[1]["requestBodyGzip"] is True
    assert records[1]["canDisableThinking"] is False and records[1]["supportsToolCall"] is False
    assert saved["disabledModels"] == ["fast-model"] and saved["label"] == "保留备注"
    assert saved["last_model_sync"] and not saved["last_model_sync_error"]
    monkeypatch.setattr(common, "request", lambda *a, **k: {"productFeatures": {}})
    result = await om._discover_account_models_once(key, timeout_s=10)
    assert result["action"] == "error", result
    current = om.get_account(key)
    for field in ("models", "disabledModels", "account_model_catalog", "last_model_sync", "label", "access_token", "refresh_token"):
        assert current[field] == saved[field]
