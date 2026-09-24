"""Upstream routing, compatibility and pre-account routing (isolated, no live I/O)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest

from src import network
from src.proxy import manager as pm
from src.proxy.connector import ProxyConnectError
from src.proxy.routing_types import PROVIDER_ROUTES, provider_for_context
from src.transports import http_runtime, policy, ws_runtime


@pytest.fixture
def routing_env(monkeypatch):
    cfg = {"oauth": {"mockMode": False}, "oauthAccounts": [], "channels": [], "network": {
        "proxies": {name: {"type": "socks5", "url": "socks5://127.0.0.1:1080"}
                    for name in ("us", "asia", "legacy", "model", "channel", "account")},
        "groups": {"us-group": ["us", "asia"]},
        "routing": {"default": "asia", "oauth_openai": "legacy", "oauth_anthropic": "legacy",
                    "providers": {key: "us-group" for key, _ in PROVIDER_ROUTES}},
    }}
    monkeypatch.setattr(pm.config, "get", lambda: cfg)
    monkeypatch.setattr(pm.config, "on_reload", lambda _fn: None)
    monkeypatch.setattr(pm, "_snapshot", pm._EMPTY_SNAPSHOT)
    monkeypatch.setattr(pm, "_config_generation", None)
    monkeypatch.setattr(pm, "_callback_registered", False)
    monkeypatch.setattr(pm, "_initialized", False)

    def reload():
        pm._install_config_locked(copy.deepcopy(cfg))
    # Use real init/resolver behavior while keeping config generations detached.
    def update(mut):
        mut(cfg)
        reload()
    monkeypatch.setattr(pm.config, "update", update)
    pm.init()
    return cfg, reload


def channel(provider, protocol="openai-responses"):
    return SimpleNamespace(type="oauth", provider=provider, key=f"oauth:{provider}:user",
                           account_key=f"{provider}:user", protocol=protocol)


@pytest.mark.parametrize("provider", [key for key, _ in PROVIDER_ROUTES])
@pytest.mark.parametrize("protocol", ["anthropic", "openai-chat", "openai-responses"])
def test_all_types_override_wire_protocol_across_http_ws_and_helpers(routing_env, provider, protocol):
    ch = channel(provider, protocol)
    assert pm.resolve_proxy_target(**policy.proxy_route_kwargs(ch, "claude-not-a-provider")) == "us-group"
    assert [name for name, _ in http_runtime._resolve_http_route_chain(ch, "claude-not-a-provider")[0]] == ["us", "asia"]
    assert [name for name, _ in ws_runtime.resolve_ws_route_chain(ch, "claude-not-a-provider")] == ["us", "asia"]
    assert [name for name, _ in network._configured_proxy_chain_or_none(
        proxy_purpose="provider_usage", proxy_channel=ch.key, proxy_model="") ] == ["us", "asia"]


@pytest.mark.parametrize("purpose,provider", [
    ("oauth_openai", "openai"), ("oauth_anthropic", "claude"), ("oauth_xai", "xai"),
    ("oauth_cursor", "cursor"), ("oauth_antigravity", "antigravity"),
    ("oauth_workbuddy", "workbuddy"), ("oauth_zhipu", "zhipu"),
    ("core_openai", "openai"), ("core_claude", "claude"),
])
def test_types_exist_before_accounts(routing_env, purpose, provider):
    assert provider_for_context(purpose=purpose) == provider
    assert pm.resolve_proxy_target(purpose=purpose) == "us-group"


def test_precedence_explicit_direct_and_telegram_independence(routing_env):
    cfg, reload = routing_env
    r = cfg["network"]["routing"]
    r.update(telegram="asia", accounts={"workbuddy:user": "account"},
             channels={"oauth:workbuddy:user": "channel"}, models={"claude-x": "model"})
    r["providers"]["workbuddy"] = "direct"
    reload()
    kw = policy.proxy_route_kwargs(channel("workbuddy"), "claude-x")
    assert pm.resolve_proxy_target(**kw) == "account"
    del r["accounts"]["workbuddy:user"]; reload()
    assert pm.resolve_proxy_target(**kw) == "channel"
    r["channels"].clear(); reload()
    assert pm.resolve_proxy_target(**kw) == "model"
    r["models"].clear(); reload()
    assert pm.resolve_proxy_target(**kw) == "direct"
    del r["providers"]["workbuddy"]; reload()
    assert pm.resolve_proxy_target(**kw) == "legacy"
    del r["oauth_openai"]; reload()
    assert pm.resolve_proxy_target(**kw) == "asia"
    assert pm.resolve_proxy_target(purpose="telegram", provider="openai") == "asia"


@pytest.mark.parametrize("purpose,legacy", [
    ("oauth_workbuddy", "oauth_openai"), ("oauth_zhipu", "oauth_openai"),
    ("core_openai", "core_monitor"), ("core_claude", "core_monitor"),
    ("oauth_cursor", "oauth_cursor"), ("oauth_antigravity", "oauth_antigravity"),
    ("oauth_xai", "oauth_xai"), ("oauth_anthropic", "oauth_anthropic"),
])
def test_absent_type_preserves_original_purpose_then_oauth_then_default(routing_env, purpose, legacy):
    cfg, reload = routing_env
    r = cfg["network"]["routing"]
    r.clear(); r.update(default="asia", oauth="us", **{legacy: "legacy"})
    reload()
    assert pm.resolve_proxy_target(purpose=purpose) == "legacy"
    del r[legacy]; reload()
    assert pm.resolve_proxy_target(purpose=purpose) == ("us" if purpose.startswith("oauth_") else "asia")


def test_api_provider_metadata_only_and_reload(routing_env):
    cfg, reload = routing_env
    cfg["channels"] = [
        {"name": "zhipu", "providerId": "zhipu"},
        {"name": "claude", "providerId": "anthropic"},
        {"name": "custom"}, {"name": "other", "providerId": "deepseek"},
    ]
    reload()
    for key in ("zhipu", "claude"):
        assert pm.resolve_proxy_target(channel_key=f"api:{key}", purpose="oauth_openai") == "us-group"
    for key in ("custom", "other", "unknown"):
        assert pm.resolve_proxy_target(channel_key=f"api:{key}", model="gpt-5", purpose="oauth_openai") == "legacy"
    cfg["network"]["routing"]["providers"]["zhipu"] = "direct"
    cfg["channels"][2]["providerId"] = "zhipu"
    reload()
    assert pm.resolve_proxy_target(channel_key="api:custom", purpose="oauth_openai") == "direct"
    assert pm.resolve_proxy_target(provider="anthropic", purpose="models-discovery") == "us-group"


def test_type_only_configuration_and_invalid_route_fail_closed(routing_env):
    cfg, reload = routing_env
    cfg["network"]["proxies"] = {}
    cfg["network"]["groups"] = {}
    cfg["network"]["routing"] = {"providers": {"workbuddy": "missing"}}
    reload()
    assert pm.is_configured() and pm.has_non_direct_routing_rules()
    with pytest.raises(ProxyConnectError):
        network._configured_proxy_chain_or_none(proxy_purpose="oauth_workbuddy", proxy_channel="", proxy_model="")
    assert http_runtime._resolve_http_route_chain(channel("workbuddy"), "x")[1].outcome == "proxy_connect_error"
    assert ws_runtime.resolve_ws_route_chain(channel("workbuddy"), "x") == []
    cfg["network"]["routing"]["directFallback"] = True; reload()
    assert ws_runtime.resolve_ws_route_chain(channel("workbuddy"), "x") == [("direct", None)]
    cfg["network"]["routing"] = {"providers": {"workbuddy": "direct"}}; reload()
    assert pm.is_configured() and not pm.has_non_direct_routing_rules()
    assert pm.resolve_proxy_chain(purpose="oauth_workbuddy") == ["direct"]


def test_real_workbuddy_helper_before_and_after_account(routing_env, monkeypatch):
    from src.oauth.workbuddy import common
    cfg, reload = routing_env
    cfg["network"]["routing"]["providers"]["workbuddy"] = "direct"; reload()
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    seen = []
    def factory(**kwargs):
        seen.append(pm.resolve_proxy_target(purpose=kwargs["proxy_purpose"], channel_key=kwargs["proxy_channel"]))
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": {"ok": True}})))
    monkeypatch.setattr(network, "sync_client", factory)
    account = {"realm": "cn", "domain": "www.codebuddy.cn", "access_token": "fixture"}
    for key in ("", "workbuddy:cn:user:p"):
        common.request(account, "/fixture", account_key=key)
    assert seen == ["direct", "direct"]


@pytest.mark.asyncio
async def test_zhipu_sync_async_helpers_and_draft_model_discovery(routing_env, monkeypatch):
    from src.oauth.zhipu import common
    from src.models_discovery import discover_models
    cfg, reload = routing_env
    cfg["network"]["routing"]["providers"]["zhipu"] = "direct"; reload()
    monkeypatch.setattr(common, "require_network", lambda: None)
    seen = []
    def factory(async_=False, **kwargs):
        seen.append(pm.resolve_proxy_target(purpose=kwargs["proxy_purpose"],
                    channel_key=kwargs.get("proxy_channel", ""), provider=kwargs.get("proxy_provider", "")))
        cls = httpx.AsyncClient if async_ else httpx.Client
        return cls(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": "glm-x"}]})))
    monkeypatch.setattr(network, "sync_client", factory)
    monkeypatch.setattr(network, "async_client", lambda **kw: factory(True, **kw))
    common._request_once("https://fixture.test", headers={}, method="GET", body=None, account_key="", timeout=1, envelope=False)
    await common._read_once("https://fixture.test", headers={}, account_key="zhipu:user", timeout=1, envelope=False)
    assert await discover_models("https://fixture.test", "fixture", provider="zhipu") == ["glm-x"]
    assert seen == ["direct"] * 3


@pytest.mark.asyncio
async def test_explicit_provider_reaches_real_sync_async_and_helper_clients(routing_env, monkeypatch):
    cfg, reload = routing_env
    cfg["network"]["routing"]["providers"]["workbuddy"] = "us"; reload()
    class RecordingConnector:
        type = "ss2022"
        def __init__(self, name): self.name = name
        def create_sync_httpx_client(self, **kw):
            return httpx.Client(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"route": self.name})))
        def create_httpx_client(self, **kw):
            return httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"route": self.name})))
    monkeypatch.setattr(pm, "get_connector", lambda name: RecordingConnector(name))
    with network.sync_client(proxy_provider="workbuddy") as client:
        assert client.get("https://fixture.test").json() == {"route": "us"}
    async with network.async_client(proxy_provider="workbuddy") as client:
        assert (await client.get("https://fixture.test")).json() == {"route": "us"}
    assert network.get_sync("https://fixture.test", proxy_provider="workbuddy").json() == {"route": "us"}
    assert network.post_sync("https://fixture.test", proxy_provider="workbuddy").json() == {"route": "us"}


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_model_discovery_management_propagates_known_type_and_channel(existing):
    from src.management_control.channels.discovery import run_model_discovery
    from src.management_control.channels.models import DiscoveryCommand
    ch = SimpleNamespace(api_key="fixture", base_url="https://fixture.test", api_path=None,
                         provider_id="zhipu", provider_preset_id="coding-cn")
    control = SimpleNamespace(_authorize=lambda *a: None, _domain_channel=lambda key: ch)
    command = (DiscoveryCommand(channel_id="api:known") if existing else
               DiscoveryCommand(api_key="fixture", provider_id="zhipu", provider_preset_id="coding-cn"))
    seen = []
    async def discover(*args, **kwargs):
        seen.append(kwargs)
        return ["glm-x"]
    result = await run_model_discovery(control, None, command, discoverer=discover)
    assert result.models == ("glm-x",)
    assert seen[0]["provider"] == "zhipu"
    assert seen[0]["channel_key"] == ("api:known" if existing else "")


def test_network_summary_filters_deleted_before_top_five(monkeypatch):
    from src.telegram.menus import system_menu as menu
    stats = [{"proxy_name": "native-sg", "requests": 999}] + [
        {"proxy_name": f"p{i}", "requests": 10 - i} for i in range(5)] + [{"proxy_name": "direct"}]
    before = copy.deepcopy(stats)
    monkeypatch.setattr(menu._runtime_control, "config_snapshot", lambda: {"network": {"proxies": {f"p{i}": {} for i in range(5)}}})
    monkeypatch.setattr(menu._runtime_control, "proxy_stats", lambda **kw: stats)
    text = "\n".join(menu._network_summary()[0])
    assert "native-sg" not in text and "p4" in text
    assert stats == before
    stats[:] = [{"proxy_name": "native-sg"}, {"proxy_name": "direct"}]
    assert "<code>direct</code>" in "\n".join(menu._network_summary()[0])


def test_menu_lists_all_types_without_accounts_and_saves_clears(routing_env, monkeypatch):
    from src.telegram.menus import proxy_menu as menu
    from src.telegram import states
    captured = []
    monkeypatch.setattr(menu.ui, "answer_cb", lambda *_: None)
    monkeypatch.setattr(menu.ui, "edit", lambda *args, **kw: captured.append((args, kw)))
    monkeypatch.setattr(states, "_states", {})
    menu._show_func_routing(42, 1, "")
    buttons = [b for row in captured[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    for key, label in PROVIDER_ROUTES:
        assert any(b["callback_data"] == f"px:rt_pick:providers:{key}" for b in buttons)
    assert menu.handle_callback(42, 1, "", "px:rt_pick:providers:workbuddy")
    assert "WorkBuddy" in captured[-1][0][2]
    assert menu.handle_callback(42, 1, "", "px:rt_do:providers:workbuddy:direct")
    assert pm.resolve_proxy_target(purpose="oauth_workbuddy") == "direct"
    assert menu.handle_callback(42, 1, "", "px:rt_do:providers:workbuddy:__del__")
    assert pm.resolve_proxy_target(purpose="oauth_workbuddy") == "legacy"
    states.set_state(42, "px_rt_pending", {"context": "providers:workbuddy"})
    assert menu.handle_callback(42, 1, "", "px:rt_s:us-group")
    assert pm.resolve_proxy_chain(purpose="oauth_workbuddy") == ["us", "asia"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
