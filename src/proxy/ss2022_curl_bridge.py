"""One-attempt loopback HTTP CONNECT bridge for curl over built-in SS routes.

libcurl cannot consume Parrot's ``httpcore.AsyncNetworkBackend``.  This module
exposes one authenticated, target-locked CONNECT endpoint on an ephemeral
loopback port.  The accepted byte stream is relayed through the existing
Shadowsocks connection implementation without terminating TLS, so curl remains
the owner of TLS, ALPN and HTTP/2 fingerprinting.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import secrets
from typing import Any

from ..async_owned import await_owned
from .ss2022 import create_ss_connection

_HEADER_LIMIT = 16 * 1024
_HEADER_TIMEOUT_SECONDS = 10.0


def _normalized_host(value: str) -> str:
    host = str(value or "").strip().rstrip(".")
    if not host or any(char in host for char in "\r\n\x00"):
        raise ValueError("invalid CONNECT target host")
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid CONNECT target host") from exc


def _parse_authority(authority: str) -> tuple[str, int]:
    value = str(authority or "").strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0 or value[end + 1:end + 2] != ":":
            raise ValueError("invalid bracketed CONNECT authority")
        host, raw_port = value[1:end], value[end + 2:]
    else:
        if value.count(":") != 1:
            raise ValueError("CONNECT authority must include one port")
        host, raw_port = value.rsplit(":", 1)
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid CONNECT target port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid CONNECT target port")
    return _normalized_host(host), port


async def _await_owned(awaitable) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except BaseException:
        try:
            await await_owned(task)
        except BaseException:
            pass
        raise


class _TunnelOwner:
    """Own one accepted socket, its SS connection and both relay pumps."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.connection: Any | None = None
        self.pumps: tuple[asyncio.Task[None], ...] = ()
        self._close_task: asyncio.Task[None] | None = None

    async def _close_impl(self) -> None:
        # The curl-side socket is closed first. Then no new bytes can enter the
        # pumps while they are cancelled and the encrypted connection is closed.
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
        current = asyncio.current_task()
        pending = [task for task in self.pumps if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.close()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(), name="ss2022-curl-bridge-tunnel-close",
            )
        await await_owned(self._close_task)


class SS2022CurlProxyLease:
    """Single-use authenticated CONNECT listener owned by one curl attempt."""

    def __init__(
        self,
        *,
        cipher: str,
        password: str,
        ss_server: str,
        ss_port: int,
        target_host: str,
        target_port: int,
        connect_timeout: float,
        timing: Any = None,
    ) -> None:
        self._cipher = cipher
        self._password = password
        self._ss_server = ss_server
        self._ss_port = int(ss_port)
        self._target_host = _normalized_host(target_host)
        self._target_port = int(target_port)
        if not 1 <= self._target_port <= 65535:
            raise ValueError("invalid CONNECT target port")
        self._connect_timeout = max(0.001, float(connect_timeout))
        self._timing = timing
        self._username = secrets.token_urlsafe(18)
        self._auth_password = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._owners: set[_TunnelOwner] = set()
        self._listener_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._claimed = False
        self._closing = False
        self._closed = False
        self._tunnel_established = False
        self._connect_failed = False

    @classmethod
    async def create(cls, **kwargs) -> "SS2022CurlProxyLease":
        lease = cls(**kwargs)
        try:
            lease._server = await asyncio.start_server(
                lease._accept,
                host="127.0.0.1",
                port=0,
                limit=_HEADER_LIMIT,
            )
            sockets = lease._server.sockets or ()
            if len(sockets) != 1:
                raise RuntimeError("SS2022 curl bridge failed to bind one loopback socket")
            bound_host = str(sockets[0].getsockname()[0])
            if bound_host != "127.0.0.1":
                raise RuntimeError("SS2022 curl bridge escaped IPv4 loopback")
            return lease
        except BaseException:
            await _await_owned(lease.aclose())
            raise

    @property
    def proxy_url(self) -> str:
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("SS2022 curl bridge listener is closed")
        port = int(server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    @property
    def proxy_auth(self) -> tuple[str, str]:
        # Kept separate from proxy_url so exceptions and logs never contain it.
        return self._username, self._auth_password

    @property
    def tunnel_established(self) -> bool:
        return self._tunnel_established

    @property
    def connect_failed(self) -> bool:
        return self._connect_failed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def listening(self) -> bool:
        return self._server is not None and bool(self._server.sockets)

    @property
    def active_handler_count(self) -> int:
        return sum(not task.done() for task in self._handlers)

    def safe_before_dispatch(self) -> bool:
        """No application bytes can leave before the CONNECT tunnel exists."""
        return not self._tunnel_established

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(
            self._handle_client(reader, writer),
            name="ss2022-curl-bridge-client",
        )
        self._handlers.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._handlers.discard(completed)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.exception()

        task.add_done_callback(done)

    async def _stop_accepting(self) -> None:
        """Close the listening sockets without waiting on this active client."""
        async with self._listener_lock:
            if self._server is not None:
                self._server.close()

    async def _close_listener(self) -> None:
        async with self._listener_lock:
            server, self._server = self._server, None
            if server is None:
                return
            server.close()
            await server.wait_closed()

    def _authorized(self, header_value: str) -> bool:
        scheme, separator, encoded = str(header_value or "").partition(" ")
        if not separator or scheme.lower() != "basic":
            return False
        try:
            supplied = base64.b64decode(encoded.strip(), validate=True)
        except Exception:
            return False
        expected = f"{self._username}:{self._auth_password}".encode("utf-8")
        return hmac.compare_digest(supplied, expected)

    async def _send_status(
        self, writer: asyncio.StreamWriter, status: int, reason: str, *, auth: bool = False,
    ) -> None:
        headers = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Length: 0",
            "Connection: close",
        ]
        if auth:
            headers.append('Proxy-Authenticate: Basic realm="Parrot"')
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _read_connect_request(
        self, reader: asyncio.StreamReader,
    ) -> tuple[str, int, dict[str, str]]:
        raw = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=_HEADER_TIMEOUT_SECONDS,
        )
        if len(raw) > _HEADER_LIMIT:
            raise ValueError("CONNECT headers exceed limit")
        text = raw.decode("latin-1")
        lines = text[:-4].split("\r\n")
        if not lines:
            raise ValueError("empty CONNECT request")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].startswith("HTTP/1."):
            raise ValueError("only HTTP CONNECT is supported")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            if not separator:
                raise ValueError("malformed CONNECT header")
            lowered = key.strip().lower()
            if not lowered or lowered in headers:
                raise ValueError("duplicate or empty CONNECT header")
            headers[lowered] = value.strip()
        host, port = _parse_authority(parts[1])
        return host, port, headers

    async def _pump_client_to_ss(
        self, reader: asyncio.StreamReader, connection: Any,
    ) -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await connection.write(data)

    async def _pump_ss_to_client(
        self, connection: Any, writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            data = await connection.read(65536)
            if not data:
                return
            writer.write(data)
            await writer.drain()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        owner = _TunnelOwner(writer)
        self._owners.add(owner)
        try:
            try:
                host, port, headers = await self._read_connect_request(reader)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
                await self._send_status(writer, 400, "Bad Request")
                return

            if not self._authorized(headers.get("proxy-authorization", "")):
                await self._send_status(writer, 407, "Proxy Authentication Required", auth=True)
                return
            if host != self._target_host or port != self._target_port:
                await self._send_status(writer, 403, "Forbidden")
                return
            if self._closing or self._claimed:
                await self._send_status(writer, 503, "Service Unavailable")
                return

            # No await between the check and claim: event-loop atomic and exactly once.
            self._claimed = True
            # Python 3.13 Server.wait_closed() also waits for accepted clients;
            # awaiting it from this client would self-deadlock. Stop accepting
            # now and let lease.aclose() await final listener ownership later.
            await self._stop_accepting()
            connection = create_ss_connection(
                self._cipher,
                self._password,
                self._ss_server,
                self._ss_port,
                timing=self._timing,
            )
            owner.connection = connection
            try:
                # The SS protocol receives the domain itself; there is no local DNS lookup.
                await connection.connect(
                    self._target_host,
                    self._target_port,
                    timeout=self._connect_timeout,
                )
            except BaseException:
                self._connect_failed = True
                await self._send_status(writer, 502, "Bad Gateway")
                raise

            self._tunnel_established = True
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            owner.pumps = (
                asyncio.create_task(
                    self._pump_client_to_ss(reader, connection),
                    name="ss2022-curl-bridge-client-to-ss",
                ),
                asyncio.create_task(
                    self._pump_ss_to_client(connection, writer),
                    name="ss2022-curl-bridge-ss-to-client",
                ),
            )
            done, pending = await asyncio.wait(
                owner.pumps, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Curl receives only bounded proxy status/EOF; credentials and SS
            # endpoint details are deliberately not reflected into the wire.
            return
        finally:
            await _await_owned(owner.aclose())
            self._owners.discard(owner)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            current = asyncio.current_task()
            handlers = [
                task for task in self._handlers
                if task is not current and not task.done()
            ]
            for task in handlers:
                task.cancel()
            if handlers:
                await asyncio.gather(*handlers, return_exceptions=True)
            if self._owners:
                await asyncio.gather(
                    *(owner.aclose() for owner in tuple(self._owners)),
                    return_exceptions=True,
                )
            await self._close_listener()
            # Drop per-attempt credentials after every socket/task owner is gone.
            self._username = ""
            self._auth_password = ""
            self._password = ""
            self._closed = True
