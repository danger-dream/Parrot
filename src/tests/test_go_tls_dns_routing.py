"""Cancellation-owned DNS and AG-only account routing regressions."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import dns.message
import dns.rrset
import httpx
import pytest

from src import network
from src.proxy import manager as pm
from src.transports import go_tls, http_runtime


@pytest.fixture(autouse=True)
def isolated_dns(monkeypatch):
    monkeypatch.setattr(network, "_CACHE", {})
    monkeypatch.setattr(network, "dns_cache_ttl", lambda: 300)


def test_sync_total_cancels_dns_socket_owner_without_executor_shutdown_delay(monkeypatch):
    calls, closed = [], []

    async def query(spec, host, qtype, timeout):
        calls.append(qtype)
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(qtype)

    monkeypatch.setattr(network, "dns_servers", lambda: ["127.0.0.1", "127.0.0.2", "127.0.0.3"])
    monkeypatch.setattr(network, "_query_with_server_async", query)
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **kw: [("direct", SimpleNamespace(type="direct"))])
    monkeypatch.setattr(go_tls, "native_client_type", lambda: lambda **kw: pytest.fail("DNS timeout must not start native I/O"))
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException) as failure:
        go_tls.request_sync("POST", "https://cancel-dns.invalid", json={}, timeout=.05)
    assert time.monotonic() - started < .25
    assert calls == closed == ["A"]
    assert failure.value.parrot_dispatched is True
    assert failure.value.parrot_native_code == "total_timeout"
    assert network._CACHE == {}


@pytest.mark.asyncio
async def test_cancel_dns_does_not_continue_fallback_or_publish_cache(monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    calls = []

    async def query(spec, host, qtype, timeout):
        calls.append((spec.raw, qtype))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(network, "dns_servers", lambda: ["127.0.0.1", "127.0.0.2"])
    monkeypatch.setattr(network, "_query_with_server_async", query)
    task = asyncio.create_task(go_tls._resolve_peer("cancel-dns.invalid", 10))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and len(calls) == 1 and network._CACHE == {}


@pytest.mark.asyncio
async def test_async_dns_preserves_family_fallback_and_shared_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(network, "dns_servers", lambda: ["127.0.0.1", "127.0.0.2"])
    monkeypatch.setattr(network, "_is_address_usable", lambda ip: ip != "2001:db8::dead")

    async def query(spec, host, qtype, timeout):
        calls.append((spec.raw, qtype))
        if spec.raw == "127.0.0.1" and qtype == "A":
            raise OSError("first resolver A failed")
        message = dns.message.make_response(dns.message.make_query(host, qtype))
        ips = ["127.0.0.9"] if qtype == "A" else ["::1", "2001:db8::dead"]
        message.answer.append(dns.rrset.from_text(host, 60, "IN", qtype, *ips))
        return message

    monkeypatch.setattr(network, "_query_with_server_async", query)
    assert await network.resolve_host_async("cache-fixture.invalid") == ["127.0.0.9", "::1"]
    assert calls == [("127.0.0.1", "A"), ("127.0.0.1", "AAAA"), ("127.0.0.2", "A"), ("127.0.0.2", "AAAA")]
    monkeypatch.setattr(network, "_query_with_server", lambda *a, **kw: pytest.fail("must reuse same DNS cache"))
    assert network.resolve_host("cache-fixture.invalid") == ["127.0.0.9", "::1"]


@pytest.mark.asyncio
async def test_system_bootstrap_uses_local_nss_without_importing_parrot():
    ips = await network._system_getaddrinfo_async("localhost")
    assert ips and all(ip in {"127.0.0.1", "::1"} for ip in ips)


@pytest.mark.asyncio
async def test_cancel_system_bootstrap_kills_and_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []
    ready = asyncio.Event()

    async def stalled_child(*args, **kwargs):
        # Only replace lookup with a local wait; retain real process ownership.
        proc = await original(args[0], "-I", "-S", "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(proc)
        ready.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled_child)
    task = asyncio.create_task(network._system_getaddrinfo_async("localhost"))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["udp", "dot", "doh"])
async def test_dns_protocols_preserve_server_bootstrap_sni_and_timeout(monkeypatch, kind):
    from dns import asyncquery
    calls = []

    async def bootstrap(host):
        calls.append(("bootstrap", host))
        return ["::1", "127.0.0.1"]

    async def answer(query, **kwargs):
        calls.append(("query", kwargs))
        return dns.message.make_response(query)

    monkeypatch.setattr(network, "_system_getaddrinfo_async", bootstrap)
    monkeypatch.setattr(asyncquery, {"udp": "udp", "dot": "tls", "doh": "https"}[kind], answer)
    raw = {"udp": "resolver.invalid:5300", "dot": "dot://resolver.invalid:853?hostname=verify.invalid", "doh": "https://resolver.invalid/dns-query"}[kind]
    await network._query_with_server_async(network.normalize_dns_server(raw), "target.invalid", "A", .5)
    assert calls[0] == ("bootstrap", "resolver.invalid")
    options = calls[1][1]
    assert options["timeout"] == .5
    if kind == "doh":
        assert options["where"] == raw and options["bootstrap_address"] == "127.0.0.1"
        assert options["client"].is_closed
    else:
        assert options["where"] == "127.0.0.1"
        if kind == "dot":
            assert options["server_hostname"] == "verify.invalid"


def routes(monkeypatch, routing):
    cfg = {"network": {"proxies": {"private": {"type": "socks5", "url": "socks5://127.0.0.1:9"}}, "routing": routing}}
    monkeypatch.setattr(pm, "init", lambda: None)
    monkeypatch.setattr(pm, "_snapshot", pm._build_snapshot(cfg, pm._EMPTY_SNAPSHOT))
    monkeypatch.setattr(pm, "_network_config_snapshot", lambda: cfg["network"])


@pytest.mark.parametrize("provider,purpose", [("antigravity", "oauth_antigravity"), ("", "oauth_antigravity")])
def test_ag_control_obeys_account_key_before_channel_provider_and_default(monkeypatch, provider, purpose):
    routes(monkeypatch, {"accounts": {"acct": "private", "oauth:acct": "direct"}, "providers": {"antigravity": "direct"}, "default": "direct"})
    chain = network._configured_proxy_chain_or_none(proxy_channel="oauth:acct", proxy_provider=provider, proxy_purpose=purpose, proxy_model="")
    assert [name for name, _ in chain] == ["private"]
    channel = SimpleNamespace(key="oauth:acct", account_key="acct", provider="antigravity", type="oauth", protocol="openai-responses")
    runtime, error = http_runtime._resolve_http_route_chain(channel, "model")
    assert error is None and [name for name, _ in runtime] == ["private"]


def test_other_provider_routing_does_not_gain_ag_account_inference(monkeypatch):
    routes(monkeypatch, {"accounts": {"acct": "private"}, "default": "direct"})
    chain = network._configured_proxy_chain_or_none(proxy_channel="oauth:acct", proxy_provider="openai", proxy_purpose="oauth_openai", proxy_model="")
    assert [name for name, _ in chain] == ["direct"]


def test_ag_keeps_purpose_and_model_context_when_no_account_override(monkeypatch):
    routes(monkeypatch, {"models": {"actual-model": "private"}, "oauth_antigravity": "direct", "default": "private"})
    common = dict(proxy_channel="oauth:acct", proxy_provider="antigravity", proxy_purpose="oauth_antigravity")
    control = network._configured_proxy_chain_or_none(**common, proxy_model="")
    model = network._configured_proxy_chain_or_none(**common, proxy_model="actual-model")
    assert [name for name, _ in control] == ["direct"]
    assert [name for name, _ in model] == ["private"]

@pytest.mark.asyncio
async def test_real_udp_dns_total_closes_query_without_next_server(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    received, closed = [], []

    class SilentDNS(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            received.append(dns.message.from_wire(data))

    listener, _ = await asyncio.get_running_loop().create_datagram_endpoint(SilentDNS, local_addr=("127.0.0.1", 0))
    port = listener.get_extra_info("sockname")[1]
    query = network._query_with_server_async

    async def observed_query(*args):
        try:
            return await query(*args)
        finally:
            closed.append(True)

    monkeypatch.setattr(network, "dns_servers", lambda: [f"127.0.0.1:{port}", f"127.0.0.2:{port}"])
    monkeypatch.setattr(network, "_query_with_server_async", observed_query)
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **kw: [("direct", SimpleNamespace(type="direct"))])
    monkeypatch.setattr(go_tls, "native_client_type", lambda: lambda **kw: pytest.fail("native must not start before DNS succeeds"))
    try:
        start = time.monotonic()
        with pytest.raises(httpx.TimeoutException) as failure:
            await _ORIG_TO_THREAD(go_tls.request_sync, "GET", "https://dns-fixture.invalid", timeout=.1)
        assert time.monotonic() - start < .5
        assert failure.value.parrot_dispatched
        assert len(received) == len(closed) == 1
        assert network._CACHE == {}
    finally:
        listener.close()


def test_environment_proxy_keeps_httpx_precedence_and_no_proxy(monkeypatch):
    for key in tuple(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9911")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9912")
    monkeypatch.setenv("NO_PROXY", "localhost,.example.invalid")
    assert go_tls._environment_proxy(httpx.URL("https://localhost")) == ""
    assert go_tls._environment_proxy(httpx.URL("https://a.example.invalid")) == ""
    assert go_tls._environment_proxy(httpx.URL("https://outside.invalid")) == "http://127.0.0.1:9911"
