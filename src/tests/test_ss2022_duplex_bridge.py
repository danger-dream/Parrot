"""Offline lifecycle tests for the shared SS2022 HTTP/WS duplex bridge."""

from __future__ import annotations

import asyncio
import gc
import os
import socket
import ssl
from typing import Any

import httpcore
import pytest
import websockets

from src.proxy.connector import (
    SS2022Connector,
    SS2022DuplexBridge,
    _SS2022Stream,
)
from src.transports.ws_runtime import (
    ManagedWsConnection,
    WsProxyBytes,
    connect_upstream_ws,
)


class FakeSSConnection:
    """Scriptable SS connection whose read side blocks until explicitly fed."""

    def __init__(self, *, write_error: BaseException | None = None):
        self.reads: asyncio.Queue[bytes | BaseException] = asyncio.Queue()
        self.write_error = write_error
        self.writes: list[bytes] = []
        self.write_called = asyncio.Event()
        self.close_count = 0

    def feed(self, item: bytes | BaseException) -> None:
        self.reads.put_nowait(item)

    async def read(self, _n: int = -1) -> bytes:
        item = await self.reads.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def write(self, data: bytes) -> None:
        self.write_called.set()
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)

    async def close(self) -> None:
        self.close_count += 1


class StreamTunnelConnection:
    """Raw loopback stream used as a fake already-connected SS tunnel."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.close_count = 0

    async def read(self, n: int = -1) -> bytes:
        return await self.reader.read(n)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.close_count += 1
        self.writer.close()
        await self.writer.wait_closed()


def _connector() -> SS2022Connector:
    return SS2022Connector(
        "offline-ss2022",
        "127.0.0.1",
        1,
        "2022-blake3-aes-128-gcm",
        "AAAAAAAAAAAAAAAAAAAAAA",
    )


def _bridge_tasks_pending() -> list[asyncio.Task[Any]]:
    return [
        task for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("ss2022-bridge-")
    ]


@pytest.mark.asyncio
async def test_selector_fd_can_be_reused_immediately_after_transport_wait_closed():
    loop = asyncio.get_running_loop()
    reused = False

    for _ in range(16):
        conn = FakeSSConnection()
        bridge = await SS2022DuplexBridge.create(conn)
        application_socket = bridge.handoff_socket()
        owned_fd = application_socket.fileno()

        reader, writer = await asyncio.open_connection(sock=application_socket)
        await bridge.aclose()
        assert application_socket.fileno() == owned_fd
        assert await asyncio.wait_for(reader.read(1), timeout=1) == b""

        writer.close()
        await writer.wait_closed()
        assert application_socket.fileno() == -1

        candidate, peer = socket.socketpair()
        if peer.fileno() == owned_fd:
            candidate, peer = peer, candidate
        reused = reused or candidate.fileno() == owned_fd
        candidate.setblocking(False)
        peer.setblocking(False)
        reader2, writer2 = await asyncio.open_connection(sock=candidate)
        await loop.sock_sendall(peer, b"x")
        assert await asyncio.wait_for(reader2.readexactly(1), timeout=1) == b"x"
        writer2.close()
        await writer2.wait_closed()
        peer.close()

    assert reused, "the closed selector fd wasn't exercised by immediate reuse"
    assert not _bridge_tasks_pending()


@pytest.mark.asyncio
async def test_application_to_ss_write_failure_cancels_blocked_reverse_pump():
    loop = asyncio.get_running_loop()
    cause = BrokenPipeError("injected SS write failure")
    conn = FakeSSConnection(write_error=cause)
    bridge = await SS2022DuplexBridge.create(conn)
    application_socket = bridge.handoff_socket()

    await loop.sock_sendall(application_socket, b"request")
    await asyncio.wait_for(bridge.wait_closed(), timeout=1)

    assert bridge.terminal is not None
    assert bridge.terminal.direction == "application_to_ss_error"
    assert bridge.terminal.cause is cause
    assert bridge.terminal.graceful is False
    assert conn.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)
    assert await asyncio.wait_for(loop.sock_recv(application_socket, 1), timeout=1) == b""
    application_socket.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_result", "direction", "cause_type", "graceful"),
    [
        (b"", "ss_to_application_eof", type(None), True),
        (ConnectionResetError("injected SS read failure"),
         "ss_to_application_error", ConnectionResetError, False),
    ],
)
async def test_ss_to_application_eof_and_error_propagate_immediately(
    read_result,
    direction,
    cause_type,
    graceful,
):
    loop = asyncio.get_running_loop()
    conn = FakeSSConnection()
    bridge = await SS2022DuplexBridge.create(conn)
    application_socket = bridge.handoff_socket()
    conn.feed(read_result)

    assert await asyncio.wait_for(loop.sock_recv(application_socket, 1), timeout=1) == b""
    await asyncio.wait_for(bridge.wait_closed(), timeout=1)

    assert bridge.terminal is not None
    assert bridge.terminal.direction == direction
    assert isinstance(bridge.terminal.cause, cause_type)
    assert bridge.terminal.graceful is graceful
    assert conn.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)
    application_socket.close()


@pytest.mark.asyncio
async def test_bridge_close_is_idempotent_before_and_after_handoff():
    conn = FakeSSConnection()
    bridge = await SS2022DuplexBridge.create(conn)
    owned_socket = bridge._application_socket
    await asyncio.gather(bridge.aclose(), bridge.aclose(), bridge.aclose())
    assert bridge.closed
    assert conn.close_count == 1
    assert owned_socket.fileno() == -1
    assert all(task.done() for task in bridge.pump_tasks)

    conn2 = FakeSSConnection()
    bridge2 = await SS2022DuplexBridge.create(conn2)
    handed_socket = bridge2.handoff_socket()
    handed_fd = handed_socket.fileno()
    await asyncio.gather(bridge2.aclose(), bridge2.aclose())
    assert bridge2.closed
    assert conn2.close_count == 1
    assert handed_socket.fileno() == handed_fd
    assert all(task.done() for task in bridge2.pump_tasks)
    handed_socket.close()


@pytest.mark.asyncio
async def test_http_tls_handshake_timeout_closes_writer_bridge_and_raw_conn(monkeypatch):
    captured: list[SS2022DuplexBridge] = []
    original_create = SS2022DuplexBridge.create.__func__

    async def capture_create(cls, conn, *, byte_counter=None):
        bridge = await original_create(cls, conn, byte_counter=byte_counter)
        captured.append(bridge)
        return bridge

    monkeypatch.setattr(
        SS2022DuplexBridge,
        "create",
        classmethod(capture_create),
    )

    conn = FakeSSConnection()
    stream = _SS2022Stream(conn)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with pytest.raises(httpcore.ConnectTimeout, match="TLS handshake timeout"):
        await stream.start_tls(
            context,
            server_hostname="example.invalid",
            timeout=0.05,
        )

    assert len(captured) == 1
    bridge = captured[0]
    await asyncio.wait_for(bridge.wait_closed(), timeout=1)
    assert bridge.terminal is not None
    assert bridge.terminal.direction == "tls_handshake_error"
    assert bridge.terminal.cause is not None
    assert bridge.handed_off and bridge.closed
    assert bridge._application_socket.fileno() == -1
    assert conn.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)


async def _loopback_bridge_opener(
    port: int,
    owners: list[tuple[SS2022DuplexBridge, StreamTunnelConnection]],
):
    async def open_bridge(_url, _connector, proxy_bytes, *, timeout):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=timeout,
        )
        raw = StreamTunnelConnection(reader, writer)
        bridge = await SS2022DuplexBridge.create(raw, byte_counter=proxy_bytes)
        owners.append((bridge, raw))
        return bridge

    return open_bridge


@pytest.mark.asyncio
async def test_real_websocket_normal_close_waits_then_closes_bridge():
    received: list[str] = []

    async def handler(ws):
        received.append(await ws.recv())
        await ws.send("pong")
        await ws.wait_closed()

    owners: list[tuple[SS2022DuplexBridge, StreamTunnelConnection]] = []
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        opener = await _loopback_bridge_opener(port, owners)
        managed = await connect_upstream_ws(
            f"ws://127.0.0.1:{port}/responses",
            headers={},
            connector=_connector(),
            proxy_bytes=WsProxyBytes(),
            open_timeout=1,
            open_socket_func=opener,
            connect_func=websockets.connect,
        )
        assert isinstance(managed, ManagedWsConnection)
        await managed.send("ping")
        assert await managed.recv() == "pong"
        await managed.close()
        await managed.close()

    assert received == ["ping"]
    assert len(owners) == 1
    bridge, raw = owners[0]
    assert bridge.closed and raw.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)
    assert bridge._application_socket.fileno() == -1


@pytest.mark.asyncio
async def test_real_websocket_handshake_failure_reuses_fds_without_task_or_fd_growth():
    async def reject(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    owners: list[tuple[SS2022DuplexBridge, StreamTunnelConnection]] = []
    opener = await _loopback_bridge_opener(port, owners)
    fd_before = len(os.listdir("/proc/self/fd"))
    try:
        for _ in range(12):
            with pytest.raises(websockets.exceptions.InvalidStatus):
                await connect_upstream_ws(
                    f"ws://127.0.0.1:{port}/responses",
                    headers={},
                    connector=_connector(),
                    proxy_bytes=WsProxyBytes(),
                    open_timeout=1,
                    open_socket_func=opener,
                    connect_func=websockets.connect,
                )
            bridge, raw = owners[-1]
            assert bridge.closed and raw.close_count == 1
            assert bridge.terminal is not None
            # The rejecting peer may deliver raw EOF before websockets surfaces
            # InvalidStatus; the bridge intentionally preserves that first cause.
            assert bridge.terminal.direction in {
                "ss_to_application_eof",
                "websocket_handshake_error",
            }
            assert bridge._application_socket.fileno() == -1
            assert all(task.done() for task in bridge.pump_tasks)
    finally:
        server.close()
        await server.wait_closed()

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gc.collect()
    fd_after = len(os.listdir("/proc/self/fd"))
    assert fd_after <= fd_before + 1
    assert not _bridge_tasks_pending()


@pytest.mark.asyncio
async def test_real_websocket_handshake_cancellation_closes_all_owned_resources():
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def hang(reader, writer):
        accepted.set()
        try:
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(hang, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    owners: list[tuple[SS2022DuplexBridge, StreamTunnelConnection]] = []
    opener = await _loopback_bridge_opener(port, owners)
    task = asyncio.create_task(connect_upstream_ws(
        f"ws://127.0.0.1:{port}/responses",
        headers={},
        connector=_connector(),
        proxy_bytes=WsProxyBytes(),
        open_timeout=5,
        open_socket_func=opener,
        connect_func=websockets.connect,
    ))
    try:
        await asyncio.wait_for(accepted.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        server.close()
        await server.wait_closed()

    assert len(owners) == 1
    bridge, raw = owners[0]
    assert bridge.closed and raw.close_count == 1
    assert bridge.terminal is not None
    assert bridge.terminal.direction == "websocket_handshake_error"
    assert isinstance(bridge.terminal.cause, asyncio.CancelledError)
    assert bridge._application_socket.fileno() == -1
    assert all(task.done() for task in bridge.pump_tasks)
    assert not _bridge_tasks_pending()


@pytest.mark.asyncio
async def test_ss2022_failure_resources_close_before_direct_fallback():
    conn = FakeSSConnection(write_error=BrokenPipeError("injected route failure"))
    bridge = await SS2022DuplexBridge.create(conn)

    async def open_ss(*args, **kwargs):
        return bridge

    async def claim_then_fail(*args, **kwargs):
        reader, writer = await asyncio.open_connection(sock=kwargs["sock"])
        del reader
        writer.write(b"trigger")
        await writer.drain()
        await conn.write_called.wait()
        writer.close()
        await writer.wait_closed()
        raise ConnectionError("SS2022 websocket route failed")

    with pytest.raises(ConnectionError):
        await connect_upstream_ws(
            "ws://example.invalid/responses",
            headers={},
            connector=_connector(),
            proxy_bytes=WsProxyBytes(),
            open_timeout=1,
            open_socket_func=open_ss,
            connect_func=claim_then_fail,
        )

    assert bridge.closed and conn.close_count == 1
    assert all(task.done() for task in bridge.pump_tasks)

    direct_result = object()

    async def direct_connect(*args, **kwargs):
        assert kwargs["proxy"] is None
        assert "sock" not in kwargs
        return direct_result

    result = await connect_upstream_ws(
        "ws://example.invalid/responses",
        headers={},
        connector=None,
        proxy_bytes=WsProxyBytes(),
        open_timeout=1,
        connect_func=direct_connect,
    )
    assert result is direct_result
    assert not _bridge_tasks_pending()
