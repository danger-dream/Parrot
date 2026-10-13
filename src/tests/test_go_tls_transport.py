"""Native loopback contract tests; run with the installed vendor wheel on PYTHONPATH."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import gzip
import ipaddress
import os
import ssl

import h2.config
import h2.connection
import h2.events
import h2.errors
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.transports import go_tls, fingerprint

if os.environ.get("PARROT_REQUIRE_GO_TLS") == "1":
    # Release/CI gates must fail collection, not silently skip a missing wheel
    # or shared library. Construction is local-only (no request/socket).
    from parrot_go_tls_client import Client as _RequiredNativeClient
    with _RequiredNativeClient():
        pass
else:
    pytest.importorskip("parrot_go_tls_client")


@pytest.fixture(autouse=True)
def real_native_workers(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)


@pytest.fixture
def tls_files(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), False).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / "cert").write_text(pem)
    (tmp_path / "key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "cert", tmp_path / "key")
    ctx.set_alpn_protocols(["h2"])
    return ctx, pem


@asynccontextmanager
async def h2_server(tls_files):
    ctx, pem = tls_files
    state = {"calls": [], "active": set(), "closed": asyncio.Event(), "request": asyncio.Event()}

    async def accept(reader, writer):
        task = asyncio.current_task()
        state["active"].add(task)
        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
        requests = {}
        try:
            conn.initiate_connection()
            writer.write(conn.data_to_send())
            await writer.drain()
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for event in conn.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        requests[event.stream_id] = dict(event.headers)
                    elif isinstance(event, h2.events.DataReceived):
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        sid = event.stream_id
                        req = requests[sid]
                        path = req[":path"]
                        state["calls"].append(req)
                        state["request"].set()
                        if path == "/headers-hang":
                            continue
                        if path == "/refused":
                            conn.reset_stream(sid, h2.errors.ErrorCodes.REFUSED_STREAM)
                            continue
                        if path == "/redirect":
                            conn.send_headers(sid, [(":status", "302"), ("location", "/final")], end_stream=True)
                            continue
                        headers = [(":status", "200")]
                        body = b"local-body"
                        if path == "/gzip":
                            body = gzip.compress(body)
                            headers += [("content-encoding", "gzip"), ("content-length", str(len(body)))]
                        if path == "/brotli":
                            import brotli
                            body = brotli.compress(body)
                            headers += [("content-encoding", "br"), ("content-length", str(len(body)))]
                        if path == "/stream":
                            headers += [("content-type", "text/event-stream")]
                            body = b"data: first\n\n"
                        conn.send_headers(sid, headers)
                        if path != "/read-hang":
                            conn.send_data(sid, body, end_stream=path != "/stream")
                writer.write(conn.data_to_send())
                await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass
            state["active"].discard(task)
            state["closed"].set()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=ctx)
    state["url"] = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    state["pem"] = pem
    try:
        yield state
    finally:
        server.close()
        await server.wait_closed()
        tasks = tuple(state["active"])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def client_for(state, **kwargs):
    return go_tls.create_client(timeout=httpx.Timeout(2), ca_pem=state["pem"], **kwargs)


@pytest.mark.asyncio
async def test_first_sse_chunk_before_eof_and_cancel_reclaims_native_socket(tls_files):
    async with h2_server(tls_files) as state:
        transport = go_tls.GoTLSTransport(ca_pem=state["pem"])
        async with httpx.AsyncClient(transport=transport, timeout=2) as client:
            async with client.stream("POST", state["url"] + "/stream", content=b"{}") as response:
                chunk = await asyncio.wait_for(response.aiter_bytes().__anext__(), 1)
                assert chunk == b"data: first\n\n"
                assert state["active"]  # Server has not sent EOF.
            await asyncio.wait_for(state["closed"].wait(), 1)
        assert not transport._streams


@pytest.mark.asyncio
async def test_gzip_decoded_once_no_redirect_and_no_refused_stream_replay(tls_files):
    async with h2_server(tls_files) as state:
        async with client_for(state) as client:
            response = await client.get(state["url"] + "/gzip", headers={"accept-encoding": "gzip"})
            assert response.content == b"local-body"
            assert response.http_version == "HTTP/2"
            response = await client.post(state["url"] + "/redirect", content=b"{}")
            assert response.status_code == 302
            with pytest.raises(httpx.HTTPError) as failure:
                await client.post(state["url"] + "/refused", content=b"{}")
            assert failure.value.parrot_dispatched
        assert [c[":path"] for c in state["calls"]] == ["/gzip", "/redirect", "/refused"]


@pytest.mark.asyncio
async def test_brotli_and_explicit_timeout_values(tls_files):
    pytest.importorskip("brotli")
    async with h2_server(tls_files) as state:
        async with client_for(state) as client:
            assert (await client.get(state["url"] + "/brotli", timeout=None)).content == b"local-body"
            with pytest.raises(httpx.ConnectTimeout):
                await client.post(state["url"], timeout=0)
            with pytest.raises(httpx.ReadTimeout):
                await client.get(state["url"] + "/read-hang", timeout=httpx.Timeout(2, read=0))
        assert len(state["calls"]) <= 2  # zero connect never dispatched


@pytest.mark.asyncio
async def test_cert_rejection_and_read_timeout_send_state(tls_files):
    async with h2_server(tls_files) as state:
        async with go_tls.create_client(timeout=httpx.Timeout(2)) as client:
            with pytest.raises(httpx.ConnectError) as failure:
                await client.get(state["url"])
            assert not failure.value.parrot_dispatched
            assert failure.value.parrot_native_code == "certificate"
        async with client_for(state) as client:
            with pytest.raises(httpx.ReadTimeout) as failure:
                await client.get(state["url"] + "/read-hang", timeout=.15)
            assert failure.value.parrot_dispatched


@pytest.mark.asyncio
async def test_cancel_pre_headers_marks_dispatch_before_propagating(tls_files):
    events = []
    async def trace(name, _info):
        events.append(name)
    async with h2_server(tls_files) as state:
        transport = go_tls.GoTLSTransport(ca_pem=state["pem"])
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            task = asyncio.create_task(client.get(state["url"] + "/headers-hang", extensions={"trace": trace}))
            await asyncio.wait_for(state["request"].wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert "http2.send_request_headers.started" in events
            await asyncio.wait_for(state["closed"].wait(), 1)
            assert not transport._streams


@pytest.mark.asyncio
async def test_cancel_stalled_tls_is_not_dispatched():
    accepted, closed = asyncio.Event(), asyncio.Event()
    async def hold(reader, writer):
        accepted.set()
        try:
            while await reader.read(65536):
                pass
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()
    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    events = []
    try:
        async with go_tls.create_client(timeout=httpx.Timeout(10)) as client:
            task = asyncio.create_task(client.get(
                f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                extensions={"trace": lambda name, info: events.append(name)},
            ))
            await asyncio.wait_for(accepted.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await asyncio.wait_for(closed.wait(), 1)
            assert events == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ss2022_authenticated_target_locked_bridge_and_close_order(tls_files, monkeypatch):
    from src.proxy import ss2022_curl_bridge as bridge
    async with h2_server(tls_files) as state:
        target = httpx.URL(state["url"])
        connections = []
        class LocalSS:
            def __init__(self):
                self.closed = False
                connections.append(self)
            async def connect(self, host, port, timeout):
                assert (host, port) == (target.host, target.port)
                self.reader, self.writer = await asyncio.open_connection(host, port)
            async def read(self, size):
                return await self.reader.read(size)
            async def write(self, data):
                self.writer.write(data)
                await self.writer.drain()
            async def close(self):
                self.closed = True
                self.writer.close()
                await self.writer.wait_closed()
        monkeypatch.setattr(bridge, "create_ss_connection", lambda *args, **kwargs: LocalSS())
        lease = await bridge.SS2022CurlProxyLease.create(
            cipher="local-fixture", password="local-fixture", ss_server="127.0.0.1", ss_port=1,
            target_host=target.host, target_port=target.port, connect_timeout=2,
        )
        async with client_for(state, proxy=lease.proxy_url, proxy_auth=lease.proxy_auth, route_owner=lease) as client:
            response = await client.get(state["url"])
            assert response.content == b"local-body"
        assert lease.closed and not lease.listening and lease.active_handler_count == 0
        assert connections and all(conn.closed for conn in connections)


@pytest.mark.asyncio
async def test_connect_auth_failure_is_safe_and_never_tries_direct(tls_files):
    from src.proxy.ss2022_curl_bridge import SS2022CurlProxyLease
    async with h2_server(tls_files) as state:
        target = httpx.URL(state["url"])
        lease = await SS2022CurlProxyLease.create(
            cipher="unused", password="unused", ss_server="127.0.0.1", ss_port=1,
            target_host=target.host, target_port=target.port, connect_timeout=2,
        )
        async with client_for(state, proxy=lease.proxy_url, proxy_auth=("wrong", "wrong"), route_owner=lease) as client:
            with pytest.raises(httpx.ConnectError, match="407") as failure:
                await client.get(state["url"])
            assert not failure.value.parrot_dispatched
        assert lease.closed and state["calls"] == []


@pytest.mark.asyncio
async def test_dns_policy_resolves_peer_not_proxy_target(tls_files, monkeypatch):
    observed = []
    async def resolver(host, timeout):
        observed.append(host)
        return ["127.0.0.1"]
    async with h2_server(tls_files) as state:
        url = state["url"].replace("127.0.0.1", "localhost")
        async with client_for(state, resolver=resolver) as client:
            assert (await client.get(url)).status_code == 200
        assert observed == ["localhost"]
    async def failing(host, timeout):
        raise OSError("DNS policy failure")
    async with go_tls.create_client(timeout=httpx.Timeout(1), resolver=failing) as client:
        with pytest.raises(httpx.ConnectError, match="DNS policy failure"):
            await client.get("https://no-fallback.invalid/")


def test_profile_selection_keeps_old_curl(monkeypatch):
    selected = []
    monkeypatch.setattr(go_tls, "create_client", lambda **kwargs: selected.append(kwargs) or "go")
    assert fingerprint.validate_profile(go_tls.PROFILE) == go_tls.PROFILE
    assert fingerprint.create_impersonated_client(go_tls.PROFILE, timeout=httpx.Timeout(2)) == "go"
    assert len(selected) == 1


@pytest.mark.asyncio
async def test_go_over_real_local_ss2022_encryption(tls_files):
    import os
    from src.proxy.connector import SS2022Connector
    from src.tests.test_ss_aead import _handle_ss2022_client, _start_ss_proxy
    async with h2_server(tls_files) as state:
        cipher = "2022-blake3-aes-128-gcm"
        password = base64.urlsafe_b64encode(os.urandom(16)).decode()
        server, port = await _start_ss_proxy(
            lambda r, w: _handle_ss2022_client(r, w, cipher, password),
        )
        connector = SS2022Connector("fixture", "127.0.0.1", port, cipher, password)
        target = httpx.URL(state["url"])
        lease = await connector.acquire_curl_proxy(target.host, target.port, timeout=2)
        observed = []
        async def resolver(host, timeout):
            observed.append(host)
            return ["127.0.0.1"]
        try:
            async with client_for(state, proxy=lease.proxy_url, proxy_auth=lease.proxy_auth,
                                  route_owner=lease, resolver=resolver) as client:
                async with client.stream("GET", state["url"] + "/stream") as response:
                    assert await asyncio.wait_for(response.aiter_bytes().__anext__(), 1) == b"data: first\n\n"
            assert lease.closed and lease.active_handler_count == 0
            assert observed == ["127.0.0.1"]  # only the CONNECT listener, never SS target DNS
        finally:
            await lease.aclose()
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_sync_helper_reuses_network_route_and_serializes_json(tls_files, monkeypatch):
    from types import SimpleNamespace
    calls = []
    def route(**kwargs):
        calls.append(kwargs)
        return [("direct", SimpleNamespace(type="direct"))]
    monkeypatch.setattr(go_tls.network, "_configured_proxy_chain_or_none", route)
    async with h2_server(tls_files) as state:
        original = go_tls.create_client
        monkeypatch.setattr(go_tls, "create_client", lambda **kw: original(ca_pem=state["pem"], **kw))
        response = await asyncio.to_thread(
            go_tls.request_sync, "POST", state["url"], json={"local": True},
            headers={"x-fixture": "ok"}, timeout=2, proxy_channel="oauth:fixture", proxy_purpose="oauth_antigravity",
        )
        assert response.content == b"local-body"
        assert calls == [{"proxy_purpose": "oauth_antigravity", "proxy_channel": "oauth:fixture",
                          "proxy_model": "", "proxy_provider": "antigravity"}]
        assert state["calls"][0]["content-type"] == "application/json"
        assert state["calls"][0]["x-fixture"] == "ok"
        assert not state["active"]


@pytest.mark.asyncio
async def test_sync_helper_does_not_replay_ambiguous_send(tls_files, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(go_tls.network, "_configured_proxy_chain_or_none",
                        lambda **kw: [("a", SimpleNamespace(type="direct")), ("b", SimpleNamespace(type="direct"))])
    async with h2_server(tls_files) as state:
        original = go_tls.create_client
        monkeypatch.setattr(go_tls, "create_client", lambda **kw: original(ca_pem=state["pem"], **kw))
        with pytest.raises(httpx.HTTPError) as failure:
            await asyncio.to_thread(go_tls.request_sync, "POST", state["url"] + "/refused", json={}, timeout=2)
        assert failure.value.parrot_dispatched
        assert len(state["calls"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["https", "socks5"])
async def test_authenticated_proxy_keeps_remote_target_dns(tls_files, scheme):
    async with h2_server(tls_files) as state:
        target = httpx.URL(state["url"])
        seen = []
        async def proxy(reader, writer):
            upstream = None
            try:
                if scheme == "https":
                    request = await reader.readuntil(b"\r\n\r\n")
                    assert b"Proxy-Authorization: Basic dXNlcjpwYXNz" in request
                    authority = request.split(b" ", 2)[1].decode()
                    seen.append(authority)
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    version, count = await reader.readexactly(2)
                    assert version == 5 and 2 in await reader.readexactly(count)
                    writer.write(b"\x05\x02")
                    await writer.drain()
                    version, size = await reader.readexactly(2)
                    assert version == 1 and await reader.readexactly(size) == b"user"
                    size = (await reader.readexactly(1))[0]
                    assert await reader.readexactly(size) == b"pass"
                    writer.write(b"\x01\x00")
                    await writer.drain()
                    assert await reader.readexactly(4) == b"\x05\x01\x00\x03"
                    size = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(size)).decode()
                    port = int.from_bytes(await reader.readexactly(2), "big")
                    seen.append(f"{host}:{port}")
                    writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x01")
                await writer.drain()
                remote, upstream = await asyncio.open_connection("127.0.0.1", target.port)
                async def pump(source, sink):
                    while data := await source.read(65536):
                        sink.write(data)
                        await sink.drain()
                tasks = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(remote, writer))]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if upstream is not None:
                    upstream.close()
                    await upstream.wait_closed()
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(proxy, "127.0.0.1", 0,
                                            ssl=tls_files[0] if scheme == "https" else None)
        resolved = []
        async def resolver(host, timeout):
            resolved.append(host)
            return ["127.0.0.1"]
        try:
            async with client_for(state, proxy=f"{scheme}://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                  proxy_auth=("user", "pass"), resolver=resolver) as client:
                response = await client.get(f"https://localhost:{target.port}/")
                assert response.content == b"local-body"
            assert seen == [f"localhost:{target.port}"]
            assert resolved == ["127.0.0.1"]
        finally:
            server.close()
            await server.wait_closed()
