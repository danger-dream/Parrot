"""Real config callbacks and route selection, with only wire transports faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from src import config, network, upstream
from src.proxy import manager as pm
from src.tests import test_network_client_reload as reload_tests
from src.tests import test_protocol_fake_upstreams as fake
from src.transports import http_runtime
from src.telegram import ui

reload_env = reload_tests.reload_env
_tg_rebuild_session = ui.rebuild_session


class MarkerConnector:
    def __init__(self, name, marker, *, kind="socks5", fail=False):
        self.name, self.marker, self.type, self.fail = name, marker, kind, fail
        self.url = marker
        self.stats = SimpleNamespace(
            total_attempts=0, total_failures=0, total_successes=0,
            last_attempt_ts=0, last_error="", last_success_ts=0, last_latency_ms=0,
        )

    def create_httpx_client(self, **kwargs):
        # Fail HTTP route setup before dispatch, not in MockTransport.handle:
        # MockTransport is marked dispatched and must not replay a POST.
        if self.fail and "timing" in kwargs:
            raise httpx.ConnectError("fixture proxy setup unavailable")
        kwargs.pop("byte_counter", None)
        kwargs.pop("timing", None)

        def wire(request):
            if self.fail:
                raise httpx.ConnectError("fixture proxy unavailable")
            return httpx.Response(200, json={"via": self.marker})

        return httpx.AsyncClient(transport=httpx.MockTransport(wire), **kwargs)


@pytest.fixture
async def proxy_env(reload_env, monkeypatch):
    monkeypatch.setattr(pm, "_initialized", False)
    monkeypatch.setattr(pm, "_callback_registered", False)
    monkeypatch.setattr(pm, "_config_generation", None)
    monkeypatch.setattr(pm, "_snapshot", pm._EMPTY_SNAPSHOT)
    # Stub connector I/O, not signature/config/manager/HTTP route decisions.
    monkeypatch.setattr(pm, "connector_from_config", lambda name, cfg: MarkerConnector(name, cfg["url"], fail=cfg.get("fixture_fail", False)))
    monkeypatch.setattr(pm, "_DIRECT", MarkerConnector("direct", "direct", kind="direct"))
    config.update(lambda cfg: cfg["network"].update(
        proxies={
            "p1": {"type": "socks5", "url": "socks5://127.0.0.1:11001"},
            "p2": {"type": "socks5", "url": "socks5://127.0.0.1:11002"},
        }, groups={"pool": ["p1", "p2"]}, routing={"default": "pool"},
    ))
    pm.init()
    fake._setup(fake._import_modules())
    yield


@pytest.mark.parametrize("kind,expected", [
    ("routing", "socks5://127.0.0.1:11002"),
    ("group", "socks5://127.0.0.1:11002"),
    ("proxy_url", "socks5://127.0.0.1:11003"),
])
async def test_modern_proxy_updates_refresh_long_lived_client(proxy_env, kind, expected):
    old = upstream.create_client()
    before_signature = network._signature()
    assert (await old.get("https://fixture.invalid/")).json()["via"] == "socks5://127.0.0.1:11001"
    if kind == "routing":
        pm.set_routing("default", "p2")
    elif kind == "group":
        pm.update_group_members("pool", ["p2", "p1"])
    else:
        pm.add_proxy("p1", {"type": "socks5", "url": expected})
    assert network._signature() != before_signature
    new = upstream.get_client()
    assert new is not old
    assert (await new.get("https://fixture.invalid/")).json()["via"] == expected
    async with network.async_client() as fresh:
        assert (await fresh.get("https://fixture.invalid/")).json()["via"] == expected


async def _open_for(channel, model="fixture-model"):
    return await http_runtime.open_response_with_proxy_chain(
        channel=channel, resolved_model=model,
        upstream_req=SimpleNamespace(method="POST", url="https://fixture.invalid/v1/chat/completions", headers={}, body=b"{}"),
        connect_timeout=2, first_byte_timeout=2, idle_timeout=2, total_timeout=5,
        response_mode="non_stream", request_id="direct-route-fixture",
    )


@pytest.mark.parametrize("section,key", [("channels", "api:target"), ("models", "fixture-model"), ("accounts", "api:target")])
async def test_explicit_direct_override_does_not_inherit_default_proxy(proxy_env, section, key):
    pm.set_routing(key, "direct", section=section)
    shared = upstream.create_client()
    assert (await shared.get("https://fixture.invalid/")).json()["via"] == "socks5://127.0.0.1:11001"
    channel = SimpleNamespace(key="api:target", type="api", protocol="openai-chat", provider="")
    opened = await _open_for(channel)
    assert opened.ok
    try:
        assert json.loads(await opened.response.aread())["via"] == "direct"
        assert opened.proxy_name is None  # Preserve existing direct metadata.
    finally:
        await http_runtime.close_response_context(opened.ctx)
        await http_runtime.close_proxy_client(opened.proxy_client)
    assert not shared.is_closed


async def test_unrelated_or_identical_update_keeps_shared_client(proxy_env):
    client = upstream.create_client()
    signature = network._signature()
    pm.set_routing("default", "pool")
    config.update(lambda cfg: cfg.update(channelSelection=cfg.get("channelSelection", "smart")))
    # Dict order isn't route order; changing it must not invalidate the pool.
    config.update(lambda cfg: cfg["network"].update(proxies=dict(reversed(list(cfg["network"]["proxies"].items())))))
    assert network._signature() == signature
    assert upstream.get_client() is client


@pytest.mark.parametrize("allow_direct", [False, True])
async def test_direct_fallback_uses_actual_direct_only_when_enabled(proxy_env, allow_direct):
    for name, port in [("p1", 11001), ("p2", 11002)]:
        pm.add_proxy(name, {"type": "socks5", "url": f"socks5://127.0.0.1:{port}", "fixture_fail": True})
    pm.set_direct_fallback(allow_direct)
    upstream.create_client()
    channel = SimpleNamespace(key="api:target", type="api", protocol="openai-chat", provider="")
    opened = await _open_for(channel)
    if not allow_direct:
        assert not opened.ok
        assert pm._DIRECT.stats.total_attempts == 0
        return
    assert opened.ok
    try:
        assert json.loads(await opened.response.aread())["via"] == "direct"
        assert opened.proxy_name is None
    finally:
        await http_runtime.close_response_context(opened.ctx)
        await http_runtime.close_proxy_client(opened.proxy_client)
    assert pm._DIRECT.stats.total_attempts == 1


async def test_direct_fallback_toggle_rebuilds_default_client(proxy_env):
    old = upstream.create_client()
    signature = network._signature()
    pm.set_direct_fallback(True)
    assert network._signature() != signature
    assert upstream.get_client() is not old


async def test_signature_uses_one_config_snapshot_and_ignores_dns_probe_override(proxy_env, monkeypatch):
    original = config.get()
    calls = []

    def get():
        calls.append(1)
        return original

    monkeypatch.setattr(network.config, "get", get)
    before = network._signature()
    assert len(calls) == 1
    monkeypatch.setattr(network._DNS_OVERRIDE, "servers", ["9.9.9.9"], raising=False)
    assert network._signature() == before
    assert len(calls) == 2
    # The explicit snapshot variant must never consult live config.
    assert network._signature(original) == before
    assert len(calls) == 2


async def test_modern_proxy_invalidation_preserves_old_inflight_lease(proxy_env):
    old = upstream.create_client()
    with upstream.client_scope() as active:
        assert active is old
        pm.set_routing("default", "p2")
        new = upstream.get_client()
        assert not old.is_closed
        assert (await active.get("https://fixture.invalid/")).json()["via"] == "socks5://127.0.0.1:11001"
        assert (await new.get("https://fixture.invalid/")).json()["via"] == "socks5://127.0.0.1:11002"
    await upstream.close_client()
    assert old.is_closed and new.is_closed


async def test_legacy_socks_path_still_uses_shared_client(reload_env, monkeypatch):
    fake._setup(fake._import_modules())
    config.update(lambda cfg: cfg["network"].update(
        proxies={}, groups={}, routing={"default": "direct"},
        socks5={"enabled": True, "url": "socks5://127.0.0.1:11001"},
    ))
    built = []

    def factory(**kwargs):
        built.append(network.active_socks5_url())
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"via": "legacy"})), **kwargs)

    monkeypatch.setattr(network, "async_client", factory)
    upstream.create_client()
    channel = SimpleNamespace(key="api:legacy", type="api", protocol="openai-chat", provider="")
    routes, error = http_runtime._resolve_http_route_chain(channel, "fixture-model")
    assert routes == [("direct", None)] and error is None
    opened = await _open_for(channel)
    assert opened.ok
    try:
        assert json.loads(await opened.response.aread())["via"] == "legacy"
    finally:
        await http_runtime.close_response_context(opened.ctx)
    assert built == ["socks5://127.0.0.1:11001"]


async def test_real_tcp_direct_override_never_contacts_default_socks_proxy(reload_env):
    import asyncio

    fake._setup(fake._import_modules())
    proxy_contacts = []
    http_contacts = []

    async def proxy(reader, writer):
        proxy_contacts.append(1)
        writer.close()
        await writer.wait_closed()

    async def target(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            http_contacts.append(1)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\ndirect")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as proxy_server, await asyncio.start_server(target, "127.0.0.1", 0) as target_server:
        proxy_port = proxy_server.sockets[0].getsockname()[1]
        target_port = target_server.sockets[0].getsockname()[1]
        config.update(lambda cfg: cfg["network"].update(
            proxies={"default-proxy": {"type": "socks5", "url": f"socks5://127.0.0.1:{proxy_port}"}},
            groups={}, routing={"default": "default-proxy", "channels": {"api:direct": "direct"}},
        ))
        upstream.create_client()  # Real default pool contains the SOCKS route.
        opened = await http_runtime.open_response_with_proxy_chain(
            channel=SimpleNamespace(key="api:direct", type="api", protocol="openai-chat", provider=""),
            resolved_model="fixture-model",
            upstream_req=SimpleNamespace(method="GET", url=f"http://127.0.0.1:{target_port}/", headers={}, body=b""),
            connect_timeout=2, first_byte_timeout=2, idle_timeout=2, total_timeout=5,
            response_mode="non_stream", request_id="real-direct-route",
        )
        assert opened.ok, opened.error
        try:
            assert await opened.response.aread() == b"direct"
        finally:
            await http_runtime.close_response_context(opened.ctx)
            await http_runtime.close_proxy_client(opened.proxy_client)
    assert http_contacts == [1] and proxy_contacts == []


@pytest.mark.parametrize("kind,expected", [
    ("telegram_route", "socks5://127.0.0.1:11002"),
    ("group", "socks5://127.0.0.1:11002"),
    ("proxy_url", "socks5://127.0.0.1:11003"),
    ("direct", "direct"),
])
async def test_modern_proxy_save_changes_next_telegram_request(proxy_env, monkeypatch, kind, expected):
    # Integration: retain config.update -> network callback -> TG invalidation
    # -> network.sync_client -> proxy manager. Only the socket wire is replaced.
    real_client = httpx.Client
    monkeypatch.setattr(ui, "rebuild_session", _tg_rebuild_session)
    sent = []

    def client_factory(**kwargs):
        via = kwargs.pop("proxy", None) or "direct"

        def wire(request):
            sent.append((request.url.path, via))
            return httpx.Response(200, json={"ok": True, "result": via})

        return real_client(transport=httpx.MockTransport(wire), **kwargs)

    monkeypatch.setattr(network.httpx, "Client", client_factory)
    ui.configure("fixture-token", [1])
    try:
        assert ui.api("getMe")["result"] == "socks5://127.0.0.1:11001"
        old = ui._session
        if kind == "telegram_route":
            pm.set_routing("telegram", "p2")
        elif kind == "group":
            pm.update_group_members("pool", ["p2", "p1"])
        elif kind == "proxy_url":
            pm.add_proxy("p1", {"type": "socks5", "url": expected})
        else:
            pm.set_routing("telegram", "direct")
        assert ui.api("getMe")["result"] == expected
        assert ui._session is not old
        assert [via for _, via in sent] == ["socks5://127.0.0.1:11001", expected]
    finally:
        ui.close_session()
        assert ui.wait_session_idle(2)
