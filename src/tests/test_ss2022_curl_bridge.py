from __future__ import annotations

import asyncio
import base64
import os
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from src.proxy import ss2022_curl_bridge as bridge_module
from src.proxy.connector import SS2022Connector
from src.proxy.ss2022_curl_bridge import SS2022CurlProxyLease


class FakeSSConnection:
    def __init__(self, *, connect_error: BaseException | None = None, block=False):
        self.connect_error = connect_error
        self.block = block
        self.connect_started = asyncio.Event()
        self.release_connect = asyncio.Event()
        self.connected_to = None
        self.reads: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []
        self.write_called = asyncio.Event()
        self.close_count = 0

    async def connect(self, host, port, *, timeout):
        self.connected_to = (host, port, timeout)
        self.connect_started.set()
        if self.block:
            await self.release_connect.wait()
        if self.connect_error is not None:
            raise self.connect_error

    async def write(self, data):
        self.writes.append(bytes(data))
        self.write_called.set()

    async def read(self, _size=-1):
        return await self.reads.get()

    async def close(self):
        self.close_count += 1
        self.release_connect.set()


async def _start_lease(monkeypatch, connection: FakeSSConnection):
    calls = []

    def create(cipher, password, server, port, timing=None):
        calls.append((cipher, password, server, port, timing))
        return connection

    monkeypatch.setattr(bridge_module, "create_ss_connection", create)
    lease = await SS2022CurlProxyLease.create(
        cipher="2022-blake3-aes-128-gcm",
        password="not-logged",
        ss_server="ss.example.invalid",
        ss_port=8388,
        target_host="Api.Example.Test",
        target_port=443,
        connect_timeout=1.25,
        timing=SimpleNamespace(name="timing"),
    )
    return lease, calls


def _address(lease):
    parsed = urlsplit(lease.proxy_url)
    return parsed.hostname, parsed.port


def _authorization(lease) -> str:
    username, password = lease.proxy_auth
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


async def _connect_request(lease, authority, authorization=""):
    host, port = _address(lease)
    reader, writer = await asyncio.open_connection(host, port)
    headers = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
    ]
    if authorization:
        headers.append(f"Proxy-Authorization: {authorization}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
    return reader, writer, response


def _bridge_tasks():
    return [
        task for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("ss2022-curl-bridge-")
    ]


@pytest.mark.asyncio
async def test_connect_requires_one_time_auth_and_exact_target_then_relays_both_directions(monkeypatch):
    connection = FakeSSConnection()
    lease, calls = await _start_lease(monkeypatch, connection)
    proxy_url = lease.proxy_url
    username, password = lease.proxy_auth
    assert username not in proxy_url and password not in proxy_url

    reader, writer, response = await _connect_request(
        lease, "api.example.test:443", "Basic Zm9vOmJhcg==",
    )
    assert response.startswith(b"HTTP/1.1 407")
    writer.close()
    await writer.wait_closed()

    reader, writer, response = await _connect_request(
        lease, "other.example.test:443", _authorization(lease),
    )
    assert response.startswith(b"HTTP/1.1 403")
    writer.close()
    await writer.wait_closed()

    reader, writer, response = await _connect_request(
        lease, "API.EXAMPLE.TEST:443", _authorization(lease),
    )
    assert response.startswith(b"HTTP/1.1 200")
    assert not lease.listening
    assert lease.tunnel_established
    assert connection.connected_to == ("api.example.test", 443, 1.25)
    assert len(calls) == 1 and calls[0][:4] == (
        "2022-blake3-aes-128-gcm", "not-logged", "ss.example.invalid", 8388,
    )

    writer.write(b"client-bytes")
    await writer.drain()
    await asyncio.wait_for(connection.write_called.wait(), timeout=1)
    assert connection.writes == [b"client-bytes"]

    connection.reads.put_nowait(b"server-bytes")
    assert await asyncio.wait_for(reader.readexactly(12), timeout=1) == b"server-bytes"
    writer.close()
    await writer.wait_closed()
    await lease.aclose()
    assert lease.closed and connection.close_count == 1
    assert lease.active_handler_count == 0
    await asyncio.sleep(0)
    assert not _bridge_tasks()


@pytest.mark.asyncio
async def test_ss_connect_failure_returns_502_is_pre_dispatch_and_never_relays(monkeypatch):
    connection = FakeSSConnection(connect_error=ConnectionRefusedError("private detail"))
    lease, _calls = await _start_lease(monkeypatch, connection)
    _reader, writer, response = await _connect_request(
        lease, "api.example.test:443", _authorization(lease),
    )
    assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"private detail" not in response
    assert lease.connect_failed
    assert lease.safe_before_dispatch()
    assert not lease.tunnel_established
    assert connection.writes == []
    writer.close()
    await writer.wait_closed()
    await lease.aclose()
    assert connection.close_count == 1
    assert not _bridge_tasks()


@pytest.mark.asyncio
async def test_close_cancels_blocked_connect_and_releases_listener_socket_and_tasks(monkeypatch):
    connection = FakeSSConnection(block=True)
    lease, _calls = await _start_lease(monkeypatch, connection)
    host, port = _address(lease)
    reader, writer = await asyncio.open_connection(host, port)
    authority = "api.example.test:443"
    writer.write(
        (
            f"CONNECT {authority} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Proxy-Authorization: {_authorization(lease)}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    await asyncio.wait_for(connection.connect_started.wait(), timeout=1)

    await asyncio.wait_for(lease.aclose(), timeout=1)
    assert lease.closed and not lease.listening
    assert connection.close_count == 1
    assert lease.active_handler_count == 0
    assert await asyncio.wait_for(reader.read(), timeout=1) in {
        b"", b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    }
    writer.close()
    await writer.wait_closed()
    with pytest.raises(OSError):
        await asyncio.open_connection(host, port)
    await asyncio.sleep(0)
    assert not _bridge_tasks()


@pytest.mark.asyncio
async def test_real_curl_streams_http2_over_real_local_ss2022_protocol(tmp_path):
    from curl_cffi.requests import AsyncSession
    from src.tests.test_ss_aead import (
        _handle_ss2022_client,
        _start_h2_tls_origin,
        _start_ss_proxy,
    )

    origin, origin_port = await _start_h2_tls_origin(tmp_path)
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    ss_server, ss_port = await _start_ss_proxy(
        lambda reader, writer: _handle_ss2022_client(
            reader, writer, cipher, password,
        )
    )
    connector = SS2022Connector(
        "local-real-ss2022", "127.0.0.1", ss_port, cipher, password,
    )
    baseline_fds = len(os.listdir("/proc/self/fd"))
    lease = await connector.acquire_curl_proxy(
        "127.0.0.1", origin_port, timeout=2,
    )
    bridge_host, bridge_port = _address(lease)
    session = AsyncSession(
        impersonate="chrome131",
        proxy=lease.proxy_url,
        proxy_auth=lease.proxy_auth,
        verify=False,
        allow_redirects=False,
        retry=0,
        max_clients=1,
        default_headers=False,
        trust_env=False,
    )
    try:
        response = await session.get(
            f"https://127.0.0.1:{origin_port}/",
            stream=True,
            timeout=(2, 5),
            allow_redirects=False,
        )
        body = b"".join([chunk async for chunk in response.aiter_content()])
        assert response.status_code == 200
        assert response.http_version == 3  # libcurl CURL_HTTP_VERSION_2_0
        assert body == b"ok-h2"
    finally:
        # Production ownership order: curl handle first, bridge second.
        await session.close()
        await lease.aclose()
        ss_server.close()
        origin.close()
        await ss_server.wait_closed()
        await origin.wait_closed()

    assert lease.closed and lease.active_handler_count == 0
    with pytest.raises(OSError):
        await asyncio.open_connection(bridge_host, bridge_port)
    await asyncio.sleep(0)
    assert not _bridge_tasks()
    assert len(os.listdir("/proc/self/fd")) <= baseline_fds
