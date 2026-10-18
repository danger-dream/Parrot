"""Profile-specific authenticated model catalogs and final static headers."""
from __future__ import annotations

import copy

import httpx
import pytest

from src import config
from src.oauth.workbuddy import catalog, common


def account(ide=False, **values):
    return {"realm": "global" if ide else "cn", "workbuddy_client_profile": "ide" if ide else "cli",
            "domain": "www.codebuddy.ai" if ide else "www.codebuddy.cn", "uid": "fixture-user",
            "access_token": "fixture-at", "refresh_token": "fixture-rt", **values}


def data(ide=False):
    return {"models": [{"id": "first", "name": "First", "aliases": ["first-alias"]},
                       {"id": "second", "modelId": "second-wire", "supportsImages": True,
                        "supportsToolCall": False, "reasoning": {"supportedEfforts": ["none", "off", "low", "high"],
                        "defaultEffort": "high", "canDisableThinking": False, "summary": "auto"}},
                       {"id": "disabled", "disabled": True}, {"id": "completion"}],
            "agents": [{"name": "craft" if ide else "cli", "models": ["first", "second", "disabled"]}],
            "productFeatures": {"RequestBodyGzip": True}}


@pytest.mark.parametrize("ide", [False, True])
def test_modern_static_headers_and_login_stages(ide):
    entry = account(ide, enterprise_id="team")
    chat = common.headers(entry, "chat")
    assert chat["X-Private-Data"] == "true"
    for key in ("Origin", "Referer", "X-Requested-With", "x-codebuddy-request", "X-Device-Token", "X-Device-Token-Error"):
        assert key not in chat
    assert chat["X-Tenant-Id"] == chat["X-Enterprise-Id"] == "team"
    assert ("X-Product-Version" in chat) is ide
    assert ("X-Env-ID" in chat) is ide
    state = common.headers(entry, "login_state")
    token = common.headers(entry, "login_token")
    assert state["X-Domain"] == ("www.codebuddy.ai" if ide else "copilot.tencent.com")
    assert "X-Domain" not in token
    for headers in (state, token):
        assert "Authorization" not in headers and "X-User-Id" not in headers and "X-Enterprise-Id" not in headers
        assert all(headers[key] == "true" for key in ("X-No-Authorization", "X-No-User-Id", "X-No-Enterprise-Id", "X-No-Department-Info"))
    identity = common.headers(entry, "login_account")
    assert identity["Authorization"] == "Bearer fixture-at" and "X-No-Authorization" not in identity
    assert "X-User-Id" not in identity and "X-Enterprise-Id" not in identity
    refresh = common.headers(entry, "refresh")
    assert refresh["X-Auth-Refresh-Source"] == "plugin" and "Authorization" not in refresh
    assert refresh["X-Refresh-Token"] == "fixture-rt" and refresh["X-Domain"] == entry["domain"]
    billing = common.headers(entry, "billing")
    assert billing["Origin"] == common.billing_base_url(entry)
    assert billing["X-Requested-With"] == "XMLHttpRequest"


@pytest.mark.parametrize("ide", [False, True])
@pytest.mark.parametrize("whitelist", [None, [], ["second"]])
def test_catalog_whitelist_metadata_and_wire_model(monkeypatch, ide, whitelist):
    payload = data(ide)
    if whitelist is not None:
        payload["availableModels"] = whitelist
    def request(entry, path, **kwargs):
        assert path == "/v3/config" and kwargs["kind"] == "models"
        return copy.deepcopy(payload)
    monkeypatch.setattr(common, "request", request)
    records = catalog.fetch_models_sync(account(ide))
    assert [record["id"] for record in records] == (["second"] if whitelist else ["first", "second"])
    record = records[-1]
    assert record["wireModel"] == ("second-wire" if ide else "second")
    assert record["inputModalities"] == ["text", "image"] and record["supportsToolCall"] is False
    assert record["reasoningEfforts"] == ["low", "high"] and record["canDisableThinking"] is False
    assert record["defaultReasoningEffort"] == "high" and record["requestBodyGzip"] is True


@pytest.mark.parametrize("ide", [False, True])
def test_enterprise_overlay_order_whitelist_and_scoped_request(monkeypatch, ide):
    payload = data(ide)
    payload["availableModels"] = ["first"]
    calls = []
    def request(entry, path, **kwargs):
        calls.append(path)
        assert entry["domain"] == account(ide)["domain"]
        if path == "/v3/config":
            return payload
        assert path == "/console/enterprises/team/config/models"
        assert entry["enterprise_id"] == "team" and kwargs["allow_list"] is True
        assert 0 < kwargs["timeout"] <= 20
        return [{"id": "second", "modelId": "enterprise-wire", "tags": ["chat"]},
                {"id": "custom:team", "tags": ["chat"]}]
    monkeypatch.setattr(common, "request", request)
    records = catalog.fetch_models_sync(account(ide, enterprise_id="team"))
    assert len(calls) == 2
    assert [r["id"] for r in records] == (["second", "custom:team"] if ide else ["second", "custom:team", "first"])
    assert records[0]["wireModel"] == ("enterprise-wire" if ide else "second")


def test_cli_alias_and_chat_tag_resolution_never_adds_unknown_ids(monkeypatch):
    payload = data()
    payload["agents"][0]["models"] = ["first-alias", "First", "unknown"]
    payload["models"].append({"id": "chat-extra", "tags": ["chat"]})
    monkeypatch.setattr(common, "request", lambda *a, **k: payload)
    assert [r["id"] for r in catalog.fetch_models_sync(account())] == ["first", "chat-extra"]
    payload["models"].append({"id": "conflict", "aliases": ["first-alias"]})
    with pytest.raises(common.WorkBuddyError, match="ambiguous_alias"):
        catalog.fetch_models_sync(account())


def test_no_direct_connect_credentials_or_cloud_endpoint_are_adopted(monkeypatch):
    payload = data(True)
    payload.update(endpoint="https://other.example", enterpriseId="team")
    payload["models"].append({"id": "local", "vendor": "user", "url": "https://other.example", "apiKey": "not-for-proxy"})
    payload["agents"][0]["models"].append("local")
    calls = []
    def request(entry, path, **kwargs):
        calls.append(path)
        assert common.api_base_url(entry) == "https://www.codebuddy.ai"
        return payload if path == "/v3/config" else []
    monkeypatch.setattr(common, "request", request)
    assert [r["id"] for r in catalog.fetch_models_sync(account(True))] == ["first", "second"]
    assert calls[-1] == "/console/enterprises/team/config/models"


@pytest.mark.parametrize("ide", [False, True])
@pytest.mark.parametrize("features,expected", [(None, True), ({}, True), ({"RequestBodyGzip": False}, False)])
def test_pinned_compression_default_respects_cloud_override(monkeypatch, ide, features, expected):
    payload = data(ide)
    if features is None:
        payload.pop("productFeatures")
    else:
        payload["productFeatures"] = features
    monkeypatch.setattr(common, "request", lambda *a, **k: payload)
    assert all(r["requestBodyGzip"] is expected for r in catalog.fetch_models_sync(account(ide)))


def test_enterprise_failure_does_not_publish_partial_directory(monkeypatch):
    def request(entry, path, **kwargs):
        if path == "/v3/config":
            return data(True)
        raise common.WorkBuddyError("request", status=403)
    monkeypatch.setattr(common, "request", request)
    with pytest.raises(common.WorkBuddyError) as error:
        catalog.fetch_models_sync(account(True, enterprise_id="team"))
    assert error.value.status_code == 403


def test_list_response_requires_explicit_request_contract(monkeypatch):
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setattr(config, "get", lambda: {"oauth": {"mockMode": False}})
    monkeypatch.setattr(common.network, "sync_client", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": [{"id": "team-model"}]}))))
    with pytest.raises(common.WorkBuddyError, match="invalid_data"):
        common.request(account(), "/fixture", kind="models")
    assert common.request(account(), "/fixture", kind="models", allow_list=True) == [{"id": "team-model"}]
