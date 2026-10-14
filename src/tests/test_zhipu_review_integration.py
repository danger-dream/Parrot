"""Upgrade integration regressions through real credential entry points."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src import config, network, oauth_manager as om, oauth_model_discovery, search_service as search
from src.oauth.zhipu import common, project_tokens
from src.tests.test_search_service import backend
from src.tests.test_search_zhipu import wire
from src.tests.test_zhipu_provider import account_env, credential  # noqa: F401
from src.tests.zhipu_pat_fixtures import envelope, pat


@pytest.mark.parametrize("mode,site", [("api_key", "bigmodel"), ("oauth", "bigmodel"), ("oauth", "zai")])
async def test_search_real_token_resolution_does_not_hold_generation_lock(account_env, monkeypatch, mode, site):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    account = credential(mode, site, organization_id="org", project_id="project")
    if mode == "oauth":
        account.update(model_key="", project_key_id="fixture")
    om.add_account(account)
    config.update(lambda cfg: cfg.update(search={"backends": [backend("zhipu", apiKeys=[])]}))
    issued = []
    def request(url, **kwargs):
        assert url.endswith("/access_tokens")
        issued.append(url)
        return envelope()
    monkeypatch.setattr(common, "request", request)
    calls = []
    def install(handler):
        def capture(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(network, "async_client", lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    wire(install)
    result = await asyncio.wait_for(search.search({"query": "Python"}), timeout=2)
    assert result["results"][0]["title"] == "Python"
    assert len(calls) == 4
    expected = pat() if mode == "oauth" else account["model_key"]
    assert all(row.headers["authorization"] == "Bearer " + expected for row in calls)
    assert len(issued) == (1 if mode == "oauth" else 0)


@pytest.mark.parametrize("field", ["project_key_id", "zhipu_oauth_token"])
async def test_catalog_cannot_publish_after_pat_credential_scope_changes(account_env, monkeypatch, field):
    account = credential("oauth", "zai", model_key="", project_key_id="old-key",
                         zhipu_oauth_token="old-source", organization_id="org", project_id="project",
                         models=["glm-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(common, "request", lambda *args, **kwargs: envelope("old-key"))
    def discover(snapshot, **kwargs):
        config.update(lambda cfg: cfg["oauthAccounts"][0].update({field: "replacement"}))
        return oauth_model_discovery.DiscoveryResult(
            ["glm-5.3-flash"], {"schema": 1, "models": [{"id": "glm-5.3-flash"}]}, "upstream:zhipu")
    monkeypatch.setattr(oauth_model_discovery, "discover", discover)
    result = await om.refresh_account_models(key)
    assert result["action"] == "stale"
    assert om.get_account(key)["models"] == ["glm-5.3"]


@pytest.mark.parametrize("edit_directory", [False, True])
async def test_catalog_uses_legitimate_refreshed_business_credential(account_env, monkeypatch, edit_directory):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    account = credential("oauth", "zai", model_key="", project_key_id="fixture",
                         zhipu_oauth_token="source", organization_id="org", project_id="project",
                         models=["glm-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        if url.endswith("/api/auth/z/login"):
            return {"access_token": "new-business"}
        if url.endswith("/access_tokens"):
            if kwargs["headers"]["Authorization"] == "Bearer fixture-biz":
                raise common.ZhipuError("project_token", status=401)
            if edit_directory:
                config.update(lambda cfg: cfg["oauthAccounts"][0].update(models=["glm-5.2"]))
            return envelope()
        if url.endswith("/api/anthropic/v1/models"):
            return {"data": [{"id": "glm-5.3-flash"}]}
        if "/client/configs?" in url:
            return {"configs": {}}
        pytest.fail("unexpected endpoint: " + url)
    monkeypatch.setattr(common, "request", request)
    result = await om.refresh_account_models(key)
    assert result["action"] == ("stale" if edit_directory else "updated")
    assert om.get_account(key)["access_token"] == "new-business"
    assert om.get_account(key)["models"] == (["glm-5.2"] if edit_directory else ["glm-5.3-flash"])
    assert sum(url.endswith("/api/auth/z/login") for url in calls) == 1


@pytest.mark.parametrize("change", ["generation", "token"])
async def test_search_rechecks_account_after_awaited_token(account_env, monkeypatch, change):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    account = credential("oauth", model_key="", project_key_id="fixture",
                         organization_id="org", project_id="project")
    om.add_account(account)
    key = om.get_account_key(account)
    config.update(lambda cfg: cfg.update(search={"backends": [backend("zhipu", apiKeys=[])]}))
    monkeypatch.setattr(common, "request", lambda *args, **kwargs: envelope())
    original = om.ensure_valid_token
    async def changed(*args, **kwargs):
        token = await original(*args, **kwargs)
        if change == "generation":
            config.update(lambda cfg: cfg["oauthAccounts"][0].update(generationId="replacement-generation"))
        else:
            current = om.get_account(key)
            with project_tokens._lock:
                project_tokens._entries[project_tokens.scope(current)]["value"]["token"] = pat(key_id="replacement")
        return token
    monkeypatch.setattr(om, "ensure_valid_token", changed)
    def forbidden_client(**kwargs):
        pytest.fail("retired credential reached MCP transport")
    monkeypatch.setattr(network, "async_client", forbidden_client)
    with pytest.raises(search.SearchError, match="已变更"):
        await asyncio.wait_for(search.search({"query": "Python"}), timeout=2)
