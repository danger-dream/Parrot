"""Draft probes and Zhipu MCP preserve provider/account proxy routing."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest

from src import network, oauth_manager, probe, search_service as search
from src.management_control.channels.models import ChannelProtocol, DraftProbeCommand
from src.management_control.channels.service import ChannelControl
from src.oauth.zhipu import project_tokens
from src.oauth_ids import account_key
from src.proxy import manager as pm
from src.proxy.connector import ProxyStats
from src.proxy.routing_types import PROVIDER_ROUTES
from src.tests.test_proxy_upstream_types import routing_env  # noqa: F401
from src.tests.test_search_service import setup, backend  # noqa: F401
from src.tests.test_search_zhipu import wire
from src.tests.test_zhipu_provider import credential
from src.tests.zhipu_pat_fixtures import pat


@pytest.fixture
def draft_probe(routing_env, monkeypatch):
    cfg, reload = routing_env
    requests = []

    class WireConnector:
        type = "ss2022"

        def __init__(self, name):
            self.name = name
            self.stats = ProxyStats()

        def create_httpx_client(self, **kwargs):
            kwargs.pop("byte_counter", None)

            def handle(request):
                requests.append((self.name, request))
                return httpx.Response(200, json={
                    "id": "chatcmpl-fixture", "object": "chat.completion",
                    "model": "fixture-model", "choices": [{
                        "index": 0, "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }],
                })

            return httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False, **kwargs)

    monkeypatch.setattr(pm, "get_connector", lambda name: WireConnector(name))

    async def run(provider):
        ch = ChannelControl._draft_channel(DraftProbeCommand(
            name="Draft", base_url="https://fixture.invalid", api_key="fixture-key",
            protocol=ChannelProtocol.OPENAI_CHAT, model="fixture-model", provider_id=provider,
        ))
        assert ch.key not in pm._snapshot.api_providers

        async def build(body, model, **kwargs):
            assert body["model"] == model == "fixture-model"
            return SimpleNamespace(url="https://fixture.invalid/v1/chat/completions", headers={}, body=b"{}")

        monkeypatch.setattr(ch, "build_upstream_request", build)
        ok, _elapsed, reason = await probe.probe_channel_model(ch, "fixture-model", timeout_s=1)
        assert ok, reason
        assert len(requests) == 1
        return requests[0][0]

    return cfg, reload, run


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [key for key, _ in PROVIDER_ROUTES] + ["anthropic", None])
async def test_draft_probe_uses_known_provider_even_before_save(draft_probe, provider):
    _cfg, _reload, run = draft_probe
    assert await run(provider) == ("asia" if provider is None else "us")


@pytest.mark.asyncio
@pytest.mark.parametrize("section,key,target", [
    ("channels", "api:Draft__wiz", "channel"),
    ("models", "fixture-model", "model"),
])
async def test_draft_probe_keeps_channel_and_model_override_precedence(draft_probe, section, key, target):
    cfg, reload, run = draft_probe
    cfg["network"]["routing"][section] = {key: target}
    reload()
    assert await run("claude") == target


@pytest.mark.asyncio
@pytest.mark.parametrize("use_account", [False, True])
@pytest.mark.parametrize("operation", ["search", "extract"])
async def test_zhipu_mcp_uses_account_route_only_for_account_credentials(setup, monkeypatch, use_account, operation):
    cfg, requests, install = setup
    account = credential("oauth", "zai", generationId="proxy-route-generation",
                         model_key="", organization_id="fixture-org", project_id="fixture-project",
                         project_key_id="fixture-project-key")
    # OAuth stores only the Key location; its MCP credential is a process-local PAT.
    token = pat(account["project_key_id"])
    token_calls = []

    async def ensure(key, *, expected_state_key=None):
        assert key == account_key(account)
        assert expected_state_key == oauth_manager.account_state_key(account)
        token_calls.append(key)
        return token

    def cached_token(current):
        assert current == account
        return token

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", ensure)
    monkeypatch.setattr(project_tokens, "cached_token", cached_token)
    cfg["oauthAccounts"] = [account]
    cfg["channels"] = []
    cfg["search"]["backends"] = [backend("zhipu", apiKeys=[])] if use_account else [backend("zhipu")]
    cfg["network"] = {
        "proxies": {name: {} for name in ("default-route", "provider-route", "account-route")},
        "routing": {
            "default": "default-route", "directFallback": False,
            "providers": {"zhipu": "provider-route"},
            "accounts": {f"oauth:{account_key(account)}": "account-route"},
        },
    }
    before = copy.deepcopy(cfg)
    real_client = network.async_client
    if operation == "extract":
        wire(install, tool_name="webReader", result={"structuredContent": {
            "url": "https://example.com/", "content": "Example body",
        }})
    else:
        wire(install)
    wire_client = network.async_client
    selected = []

    class WireConnector:
        type = "ss2022"

        def __init__(self, name):
            self.name = name
            self.stats = ProxyStats()

        def create_httpx_client(self, **kwargs):
            selected.append(self.name)
            return wire_client(**kwargs)

    monkeypatch.setattr(pm.config, "on_reload", lambda _fn: None)
    monkeypatch.setattr(pm, "connector_from_config", lambda name, _cfg: WireConnector(name))
    monkeypatch.setattr(pm, "_snapshot", pm._EMPTY_SNAPSHOT)
    monkeypatch.setattr(pm, "_config_generation", None)
    monkeypatch.setattr(pm, "_callback_registered", False)
    monkeypatch.setattr(pm, "_initialized", False)
    monkeypatch.setattr(network, "async_client", real_client)
    if operation == "extract":
        result = await search.extract({"url": "https://example.com/"})
        assert result["content"] == "Example body"
    else:
        result = await search.search({"query": "Python"})
        assert result["results"][0]["title"] == "Python"
    assert selected == ["account-route" if use_account else "provider-route"]
    assert len(requests) == 4
    assert token_calls == ([account_key(account)] if use_account else [])
    expected_token = token if use_account else "private-test-key"
    assert all(request.headers["authorization"] == "Bearer " + expected_token for request in requests)
    assert cfg == before
