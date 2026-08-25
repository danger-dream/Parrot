from __future__ import annotations

import asyncio
import contextlib
import http.server
import os
import socket
import ssl
import threading
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from src import network
from src.proxy.connector import (
    DirectConnector,
    ProxyConnectError,
    SOCKS5Connector,
    SyncRoutedStream,
    SS2022DuplexBridge,
    _SS2022LoopWorker,
    _SS2022Stream,
    _SyncSS2022Stream,
)
from src.transports import ws_runtime


class ScriptedSocksStream:
    """Scripted byte stream; responses are deliberately returned one byte at a time."""

    def __init__(self, *, auth=False, auth_ok=True, method=0, method_reply=None,
                 reply=None, eof_stage=None, timeout_stage=None):
        self.auth = auth
        self.auth_ok = auth_ok
        self.method = 2 if auth else method
        self.method_reply = method_reply
        self.reply = reply or b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb"
        self.eof_stage = eof_stage
        self.timeout_stage = timeout_stage
        self.phase = 0
        self.pending = bytearray()
        self.writes: list[bytes] = []
        self.closed = False
        self.close_calls = 0

    def write(self, data, timeout=None):
        payload = bytes(data)
        self.writes.append(payload)
        self.phase += 1
        if self.phase == 1:
            self.pending.extend(
                self.method_reply
                if self.method_reply is not None
                else bytes((5, self.method))
            )
        elif self.auth and self.phase == 2:
            self.pending.extend(bytes((1, 0 if self.auth_ok else 1)))
        else:
            self.pending.extend(self.reply)

    def read(self, size, timeout=None):
        if self.timeout_stage == self.phase:
            raise httpcore.ReadTimeout("script timeout")
        if self.eof_stage == self.phase:
            return b""
        if not self.pending:
            return b""
        out = bytes(self.pending[:1])
        del self.pending[:1]
        return out

    def close(self):
        self.close_calls += 1
        self.closed = True


def _open_socks(monkeypatch, core, *, url="socks5://proxy.invalid:1080", host="example.test"):
    monkeypatch.setattr(
        "src.proxy.connector.httpcore.SyncBackend.connect_tcp",
        lambda *_args, **_kwargs: core,
    )
    return SOCKS5Connector("socks", url).open_sync_stream(host, 443, timeout=0.1)


@pytest.mark.parametrize("reply", [
    b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb",
    b"\x05\x00\x00\x04" + (b"\x00" * 15) + b"\x01\x01\xbb",
    b"\x05\x00\x00\x03\x03foo\x01\xbb",
])
def test_socks5_no_auth_segmented_success_reply_matrix(monkeypatch, reply):
    core = ScriptedSocksStream(reply=reply)
    stream = _open_socks(monkeypatch, core)
    stream.close()
    stream.close()
    assert core.close_calls == 1


def test_socks5_auth_percent_credentials_and_unicode_idna(monkeypatch):
    core = ScriptedSocksStream(auth=True)
    stream = _open_socks(
        monkeypatch, core,
        url="socks5://u%40ser:p%20ass@proxy.invalid:1080",
        host="bücher.example",
    )
    assert core.writes[1] == b"\x01\x05u@ser\x05p ass"
    assert b"xn--bcher-kva.example" in core.writes[2]
    stream.close()


@pytest.mark.parametrize("core,match", [
    (ScriptedSocksStream(method=255), "rejected authentication"),
    (ScriptedSocksStream(auth=True, auth_ok=False), "authentication failed"),
    (ScriptedSocksStream(reply=b"\x05\x05\x00\x01" + b"\x00" * 6), "CONNECT failed"),
    (ScriptedSocksStream(reply=b"\x05\x00\x00\x09"), "address type"),
])
def test_socks5_rejection_matrix_closes_once(monkeypatch, core, match):
    with pytest.raises((ProxyConnectError, Exception), match=match):
        _open_socks(monkeypatch, core)
    assert core.close_calls == 1


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_socks5_eof_each_handshake_stage_closes_once(monkeypatch, stage):
    core = ScriptedSocksStream(auth=True, eof_stage=stage)
    with pytest.raises(ProxyConnectError, match="closed during handshake"):
        _open_socks(monkeypatch, core, url="socks5://u:p@proxy.invalid:1080")
    assert core.close_calls == 1


def test_socks5_timeout_closes_once(monkeypatch):
    core = ScriptedSocksStream(timeout_stage=1)
    with pytest.raises(httpcore.ReadTimeout):
        _open_socks(monkeypatch, core)
    assert core.close_calls == 1


def test_socks5_proxy_tcp_connect_failure_has_no_stream_to_leak(monkeypatch):
    monkeypatch.setattr(
        "src.proxy.connector.httpcore.SyncBackend.connect_tcp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(httpcore.ConnectError("down")),
    )
    with pytest.raises(httpcore.ConnectError):
        SOCKS5Connector("socks", "socks5://127.0.0.1:1").open_sync_stream(
            "example.test", 443, timeout=0.1,
        )


class _TCPServer:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn, _ = self.listener.accept()
        with conn:
            data = conn.recv(16)
            conn.sendall(data.upper())
        self.done.set()

    def close(self):
        self.listener.close()
        self.thread.join(1)


def test_direct_raw_tcp_success_and_double_close():
    server = _TCPServer()
    try:
        stream = DirectConnector().open_sync_stream("127.0.0.1", server.port, timeout=1)
        stream.sendall(b"ping")
        assert stream.recv(4) == b"PING"
        stream.close()
        stream.close()
        assert server.done.wait(1)
    finally:
        server.close()


def test_direct_raw_connect_failure():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(httpcore.ConnectError):
        DirectConnector().open_sync_stream("127.0.0.1", port, timeout=0.1)


@pytest.mark.asyncio
async def test_direct_raw_tls_success_and_alpn(tmp_path):
    from src.tests.test_ss_aead import _run_in_test_thread, _start_h2_tls_origin
    import ssl

    origin, port = await _start_h2_tls_origin(tmp_path)
    context = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
    context.set_alpn_protocols(["h2"])

    def connect():
        stream = DirectConnector().open_sync_stream("127.0.0.1", port, timeout=2)
        try:
            stream.start_tls(context, server_hostname="127.0.0.1")
            assert stream.selected_alpn_protocol() == "h2"
        finally:
            stream.close()
            stream.close()

    try:
        await _run_in_test_thread(connect)
    finally:
        origin.close()
        await origin.wait_closed()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = b"route-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass


@contextlib.contextmanager
def _http_origin():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_sync_request_connect_failover_uses_only_explicit_chain(monkeypatch):
    bad = SOCKS5Connector("bad", f"socks5://127.0.0.1:{_closed_port()}")
    direct = DirectConnector()
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **_kw: [("bad", bad), ("direct", direct)])
    with _http_origin() as port, network.sync_client(timeout=1) as client:
        response = client.get(f"http://127.0.0.1:{port}/")
        assert response.text == "route-ok"


@pytest.mark.asyncio
async def test_async_request_connect_failover_uses_only_explicit_chain(monkeypatch):
    bad = SOCKS5Connector("bad", f"socks5://127.0.0.1:{_closed_port()}")
    direct = DirectConnector()
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **_kw: [("bad", bad), ("direct", direct)])
    with _http_origin() as port:
        async with network.async_client(timeout=1) as client:
            response = await client.get(f"http://127.0.0.1:{port}/")
            assert response.text == "route-ok"


def test_sync_route_without_explicit_direct_fails_closed_at_connect(monkeypatch):
    bad = SOCKS5Connector("bad", f"socks5://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **_kw: [("bad", bad)])
    with network.sync_client(timeout=0.2) as client:
        with pytest.raises(httpx.ConnectError):
            client.get("http://127.0.0.1:9/")


@pytest.mark.asyncio
async def test_async_route_without_explicit_direct_fails_closed_at_connect(monkeypatch):
    bad = SOCKS5Connector("bad", f"socks5://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **_kw: [("bad", bad)])
    async with network.async_client(timeout=0.2) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://127.0.0.1:9/")


def test_sync_failover_never_replays_after_write_error():
    calls = []
    class First(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append("first")
            raise httpx.WriteError("body may have started")
    class Second(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append("second")
            raise AssertionError("must not replay")
    transport = network._RouteFailoverSyncTransport([
        ("first", lambda _url: First(), []),
        ("second", lambda _url: Second(), []),
    ])
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.WriteError):
            client.post("http://example.invalid/", content=b"non-idempotent")
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_async_failover_never_replays_after_read_error():
    calls = []
    class First(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append("first")
            raise httpx.ReadError("response may have started")
    class Second(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append("second")
            raise AssertionError("must not replay")
    transport = network._RouteFailoverAsyncTransport([
        ("first", lambda _url: First(), []),
        ("second", lambda _url: Second(), []),
    ])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("http://example.invalid/")
    assert calls == ["first"]


def test_ss_worker_submit_after_close_and_idempotent_close():
    worker = _SS2022LoopWorker()
    worker.close()
    worker.close()
    async def value():
        return 1
    coro = value()
    with pytest.raises(RuntimeError, match="closed"):
        worker.submit(coro)
    assert coro.cr_frame is None
    assert not worker._thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "socks5", "ss2022", "unknown"])
async def test_ws_connector_parameter_matrix(monkeypatch, kind):
    calls = []
    class FakeWs:
        async def close(self, *args, **kwargs): pass
        async def wait_closed(self): pass
    async def connect(url, **kwargs):
        calls.append(kwargs)
        return FakeWs()
    connector = None
    if kind == "socks5":
        connector = SOCKS5Connector("s", "socks5://u:p@proxy.invalid:1080")
    elif kind == "ss2022":
        connector = SimpleNamespace()  # replaced below with real type check fixture
    elif kind == "unknown":
        connector = object()
    if kind == "ss2022":
        from src.proxy.connector import SS2022Connector
        connector = SS2022Connector("s", "127.0.0.1", 1, "2022-blake3-aes-128-gcm", "x")
        class Bridge:
            def handoff_socket(self): return object()
            async def aclose(self, **kwargs): self.closed = kwargs
        bridge = Bridge()
        result = await ws_runtime.connect_upstream_ws(
            "wss://example.invalid/ws", headers={"x": "y"}, connector=connector,
            proxy_bytes=None, open_timeout=1, connect_func=connect,
            open_socket_func=lambda *a, **k: asyncio.sleep(0, result=bridge),
        )
        assert calls[0]["proxy"] is None and "sock" in calls[0]
        await result.close()
        return
    if kind == "unknown":
        with pytest.raises(ProxyConnectError):
            await ws_runtime.connect_upstream_ws("wss://example.invalid/ws", headers={}, connector=connector, proxy_bytes=None, open_timeout=1, connect_func=connect)
        assert not calls
        return
    await ws_runtime.connect_upstream_ws("wss://example.invalid/ws", headers={}, connector=connector, proxy_bytes=None, open_timeout=1, connect_func=connect)
    expected = None if kind == "direct" else "socks5h://u:p@proxy.invalid:1080"
    assert calls[0]["proxy"] == expected


@pytest.mark.asyncio
async def test_ws_socks_connect_failure_is_not_retried_direct():
    calls = []
    async def fail(*args, **kwargs):
        calls.append(kwargs.get("proxy"))
        raise OSError("proxy unavailable")
    connector = SOCKS5Connector("s", "socks5://u:p@proxy.invalid:1080")
    with pytest.raises(OSError, match="proxy unavailable"):
        await ws_runtime.connect_upstream_ws(
            "wss://example.invalid/ws", headers={}, connector=connector,
            proxy_bytes=None, open_timeout=1, connect_func=fail,
        )
    assert calls == ["socks5h://u:p@proxy.invalid:1080"]


@pytest.mark.parametrize("core", [
    ScriptedSocksStream(method_reply=b"\x04\x00"),
    ScriptedSocksStream(reply=b"\x04\x00\x00\x01" + b"\x00" * 6),
    ScriptedSocksStream(reply=b"\x05\x00\x01\x01" + b"\x00" * 6),
])
def test_socks5_malformed_version_or_reserved_byte_closes_once(monkeypatch, core):
    with pytest.raises(Exception):
        _open_socks(monkeypatch, core)
    assert core.close_calls == 1


@pytest.mark.parametrize("reply", [
    b"\x05\x00\x00",
    b"\x05\x00\x00\x01\x7f",
    b"\x05\x00\x00\x04" + b"\x00" * 8,
    b"\x05\x00\x00\x03\x05ab",
    b"\x05\x00\x00\x03\x03foo\x01",
])
def test_socks5_representative_truncated_replies_close_once(monkeypatch, reply):
    core = ScriptedSocksStream(reply=reply)
    with pytest.raises(ProxyConnectError, match="closed during handshake"):
        _open_socks(monkeypatch, core)
    assert core.close_calls == 1


def test_socks5_oversized_target_domain_fails_and_closes_once(monkeypatch):
    core = ScriptedSocksStream()
    oversized = ".".join(["a" * 63] * 5)
    with pytest.raises(Exception):
        _open_socks(monkeypatch, core, host=oversized)
    assert core.close_calls == 1


class _BlockingRawSSStream:
    def __init__(self):
        self.write_started = threading.Event()
        self.read_started = threading.Event()
        self.closed = threading.Event()

    async def write(self, _data):
        self.write_started.set()
        await asyncio.Event().wait()

    async def read(self, _size):
        self.read_started.set()
        await asyncio.Event().wait()

    async def close(self):
        self.closed.set()


def _worker_count():
    return sum(
        thread.name == "ss2022-sync-worker" and thread.is_alive()
        for thread in threading.enumerate()
    )


def _close_worker_bounded(worker):
    closer = threading.Thread(target=worker.close, name="ss2022-worker-closer", daemon=True)
    closer.start()
    closer.join(1)
    assert not closer.is_alive(), "SS2022 worker close deadlocked"


def test_ss2022_sync_write_timeout_cleans_worker_task_and_fds():
    baseline_workers = _worker_count()
    baseline_fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    raw = _BlockingRawSSStream()
    worker = _SS2022LoopWorker()
    stream = _SyncSS2022Stream(worker, _SS2022Stream(raw))
    try:
        with pytest.raises(httpcore.WriteTimeout, match="SS2022 write timeout"):
            stream.write(b"blocked", timeout=0.02)
        assert raw.write_started.wait(1)
        async def task_count():
            return len(asyncio.all_tasks())
        assert worker.submit(task_count()) == 1
    finally:
        stream.close()
        _close_worker_bounded(worker)
    assert raw.closed.wait(1)
    assert worker._loop.is_closed()
    assert not worker._thread.is_alive()
    assert _worker_count() == baseline_workers
    if baseline_fds is not None:
        assert len(os.listdir("/proc/self/fd")) <= baseline_fds


def test_ss2022_worker_close_cancels_running_submit_without_deadlock():
    baseline_workers = _worker_count()
    worker = _SS2022LoopWorker()
    started = threading.Event()
    finalized = threading.Event()
    outcome = []

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    def submitter():
        try:
            worker.submit(blocked())
        except BaseException as exc:
            outcome.append(exc)

    caller = threading.Thread(target=submitter, name="ss2022-submit-caller")
    caller.start()
    assert started.wait(1)
    _close_worker_bounded(worker)
    caller.join(1)
    assert not caller.is_alive()
    assert finalized.wait(1)
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert "cancelled" in str(outcome[0])
    assert worker._loop.is_closed()
    assert not worker._thread.is_alive()
    assert _worker_count() == baseline_workers


@pytest.mark.asyncio
async def test_ss2022_sync_tls_handshake_timeout_closes_bridge_raw_worker_and_socket(monkeypatch):
    baseline_workers = _worker_count()
    baseline_fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    raw = _BlockingRawSSStream()
    handshake_started = threading.Event()
    bridges = []
    original_create = SS2022DuplexBridge.create

    async def tracking_create(cls, conn, *, byte_counter=None):
        bridge = await original_create(conn, byte_counter=byte_counter)
        bridges.append(bridge)
        return bridge

    async def blocked_open_connection(*, sock, **_kwargs):
        handshake_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sock.close()

    monkeypatch.setattr(SS2022DuplexBridge, "create", classmethod(tracking_create))
    monkeypatch.setattr(asyncio, "open_connection", blocked_open_connection)
    worker = _SS2022LoopWorker()
    stream = _SyncSS2022Stream(worker, _SS2022Stream(raw))

    def handshake():
        context = ssl.create_default_context()
        with pytest.raises(httpcore.ConnectTimeout, match="TLS handshake timeout"):
            stream.start_tls(context, server_hostname="example.test", timeout=0.02)

    try:
        await asyncio.wait_for(asyncio.to_thread(handshake), timeout=1)
        assert handshake_started.wait(1)
    finally:
        stream.close()
        await asyncio.wait_for(asyncio.to_thread(_close_worker_bounded, worker), timeout=2)
    assert len(bridges) == 1
    bridge = bridges[0]
    assert bridge.closed
    assert bridge._internal_socket.fileno() == -1
    assert bridge._application_socket.fileno() == -1
    assert all(task.done() for task in bridge.pump_tasks)
    assert raw.closed.wait(1)
    assert worker._loop.is_closed()
    assert not worker._thread.is_alive()
    assert _worker_count() == baseline_workers
    if baseline_fds is not None:
        assert len(os.listdir("/proc/self/fd")) <= baseline_fds


@pytest.mark.asyncio
async def test_managed_ws_cancel_during_close_finishes_socket_bridge_and_tasks():
    raw = _BlockingRawSSStream()
    bridge = await SS2022DuplexBridge.create(raw)
    handed_off = bridge.handoff_socket()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class Transport:
        def abort(self):
            handed_off.close()

    class Ws:
        transport = Transport()
        async def close(self):
            close_started.set()
            await release_close.wait()
            handed_off.close()
        async def wait_closed(self):
            return None

    managed = ws_runtime.ManagedWsConnection(Ws(), bridge)
    caller = asyncio.create_task(managed.close())
    await asyncio.wait_for(close_started.wait(), timeout=1)
    caller.cancel()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, timeout=1)
    await asyncio.wait_for(bridge.wait_closed(), timeout=1)
    assert handed_off.fileno() == -1
    assert bridge.closed
    assert bridge._internal_socket.fileno() == -1
    assert bridge._application_socket.fileno() == -1
    assert all(task.done() for task in bridge.pump_tasks)
    assert raw.closed.is_set()
    assert managed._close_task.done()
