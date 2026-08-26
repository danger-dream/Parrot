from __future__ import annotations

from types import SimpleNamespace

import pytest

from src import network, network_monitor, provider_usage
from src.cursor_bridge import h2stream
from src.oauth import antigravity, openai, xai
from src.proxy.connector import ProxyConnectError, SOCKS5Connector
from src.transports import ws_runtime


class _Raw:
    def __init__(self, alpn="h2"):
        self.alpn = alpn
        self.closed = False
        self.sent = []

    def start_tls(self, _context, *, server_hostname):
        self.server_hostname = server_hostname
        return self

    def selected_alpn_protocol(self):
        return self.alpn

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        return b""

    def close(self):
        self.closed = True


class _H2:
    def __init__(self, config=None):
        pass

    def initiate_connection(self):
        pass

    def get_next_available_stream_id(self):
        return 1

    def send_headers(self, *args, **kwargs):
        pass

    def data_to_send(self):
        return b"preface"

    def close_connection(self):
        pass


def test_cursor_h2_forwards_account_channel_model_route(monkeypatch):
    raw = _Raw()
    seen = {}

    def routed(host, port, **kwargs):
        seen.update(host=host, port=port, **kwargs)
        return raw

    monkeypatch.setattr(network, "open_sync_stream", routed)
    monkeypatch.setattr(h2stream, "H2Connection", _H2)
    stream = h2stream.CursorH2Stream(
        account_key="openai:user@example.com",
        channel_key="oauth:openai:user@example.com",
        model="cursor-model",
    )
    stream.open([])
    stream.close()
    assert seen["proxy_purpose"] == "oauth_cursor"
    assert seen["proxy_channel"] == "oauth:openai:user@example.com"
    assert seen["proxy_model"] == "cursor-model"
    assert raw.server_hostname == stream.host
    assert raw.closed


def test_routed_stream_fails_closed_without_direct(monkeypatch):
    class Broken:
        def open_sync_stream(self, *args, **kwargs):
            raise OSError("proxy unavailable")

    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **kwargs: [("ss2022", Broken())])
    monkeypatch.setattr(
        "src.proxy.connector.DirectConnector.open_sync_stream",
        lambda *args, **kwargs: pytest.fail("must not use an implicit direct socket"),
    )
    with pytest.raises(ProxyConnectError, match="ss2022"):
        network.open_sync_stream("upstream.invalid", 443, timeout=1)


def test_routed_stream_timeout_keeps_proxy_failover_contract_and_cause(monkeypatch):
    timeout = TimeoutError("route timed out")

    class TimedOut:
        def open_sync_stream(self, *args, **kwargs):
            raise timeout

    monkeypatch.setattr(
        network,
        "_configured_proxy_chain_or_none",
        lambda **kwargs: [("socks5", TimedOut()), ("ss2022", TimedOut())],
    )
    with pytest.raises(ProxyConnectError, match="configured proxy route") as caught:
        network.open_sync_stream("upstream.invalid", 443, timeout=1)

    assert caught.value.__cause__ is timeout


def test_routed_stream_continues_explicit_chain(monkeypatch):
    calls = []
    winner = object()

    class Connector:
        def __init__(self, name, fail):
            self.name, self.fail = name, fail

        def open_sync_stream(self, *args, **kwargs):
            calls.append(self.name)
            if self.fail:
                raise OSError("unavailable")
            return winner

    monkeypatch.setattr(
        network,
        "_configured_proxy_chain_or_none",
        lambda **kwargs: [
            ("socks5", Connector("socks5", True)),
            ("ss2022", Connector("ss2022", False)),
        ],
    )
    assert network.open_sync_stream("upstream.invalid", 443, timeout=1) is winner
    assert calls == ["socks5", "ss2022"]


def test_socks5_sync_stream_completes_authenticated_domain_connect(monkeypatch):
    class FakeCoreStream:
        def __init__(self):
            self.pending = bytearray()
            self.writes = []
            self.closed = False

        def write(self, data, timeout=None):
            payload = bytes(data)
            self.writes.append(payload)
            if payload.startswith(b"\x05") and len(self.writes) == 1:
                self.pending.extend(b"\x05\x02")
            elif payload.startswith(b"\x01"):
                self.pending.extend(b"\x01\x00")
            else:
                assert payload[:4] == b"\x05\x01\x00\x03"
                assert payload[5:5 + payload[4]] == b"api2.cursor.sh"
                self.pending.extend(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

        def read(self, size, timeout=None):
            out = bytes(self.pending[:size])
            del self.pending[:size]
            return out

        def close(self):
            self.closed = True

    core = FakeCoreStream()
    monkeypatch.setattr(
        "src.proxy.connector.httpcore.SyncBackend.connect_tcp",
        lambda _self, host, port, **kwargs: core,
    )
    stream = SOCKS5Connector(
        "test-socks5", "socks5://user:pass@proxy.invalid:1080",
    ).open_sync_stream("api2.cursor.sh", 443, timeout=1)
    assert len(core.writes) == 3
    stream.close()
    assert core.closed


@pytest.mark.asyncio
async def test_provider_usage_uses_channel_route(monkeypatch):
    seen = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"limit": 10, "usage": 1}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, headers=None):
            return Response()

    def client(**kwargs):
        seen.append(kwargs)
        return Client()

    monkeypatch.setattr(network, "async_client", client)
    spec = provider_usage.SPECS[("openrouter", "standard")]
    await provider_usage.fetch(spec, "key", channel_key="channel-a")
    await provider_usage.fetch(spec, "key", channel_key="channel-b")
    assert [item["proxy_channel"] for item in seen] == ["channel-a", "channel-b"]
    assert all(item["proxy_purpose"] == "provider_usage" for item in seen)


def test_channel_and_core_monitor_use_routed_stream(monkeypatch):
    calls = []

    class Stream:
        def close(self):
            calls.append("closed")

    def routed(host, port, **kwargs):
        calls.append((host, port, kwargs))
        return Stream()

    monkeypatch.setattr(network, "open_sync_stream", routed)
    ch = SimpleNamespace(key="channel-key", display_name="A", type="api", base_url="https://api.example", protocol="openai-chat", api_path=None)
    assert network_monitor._channel_check(ch, 1).ok
    assert network_monitor._core_check("openai", 1).ok
    assert calls[0][2]["proxy_channel"] == "channel-key"
    assert calls[0][2]["proxy_purpose"] == "channel_monitor"
    assert calls[2][2]["proxy_purpose"] == "core_monitor"


@pytest.mark.asyncio
async def test_unknown_ws_connector_fails_closed():
    with pytest.raises(ProxyConnectError, match="unsupported WebSocket connector"):
        await ws_runtime.connect_upstream_ws(
            "wss://example.invalid/socket",
            headers={}, connector=object(), proxy_bytes=None, open_timeout=1,
            connect_func=lambda *args, **kwargs: pytest.fail("must not connect direct"),
        )


def test_saved_oauth_refresh_adapters_forward_account_route(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "token", "expires_in": 3600}

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr(xai, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(antigravity, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(openai, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(xai.network, "post_sync", post)
    xai.refresh_sync("refresh", account_key="xai:user@example.com")
    monkeypatch.setattr(antigravity.network, "post_sync", post)
    antigravity.refresh_sync("refresh", account_key="antigravity:user@example.com")
    assert calls[0]["proxy_channel"] == "oauth:xai:user@example.com"
    assert calls[1]["proxy_channel"] == "oauth:antigravity:user@example.com"

    captured = {}
    monkeypatch.setattr(openai, "_post_token_json", lambda data, **kwargs: captured.update(kwargs) or {"access_token": "token"})
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", lambda *args, **kwargs: None)
    openai.refresh_sync("refresh", account_key="openai:user@example.com")
    assert captured["proxy_channel"] == "oauth:openai:user@example.com"
