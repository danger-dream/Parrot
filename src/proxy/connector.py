"""Proxy connectors: abstract base + SOCKS5 / SS2022 / Direct implementations.

Each connector creates an httpx.AsyncClient that routes traffic through the
proxy.  SS2022 uses a custom httpcore network backend; SOCKS5 uses httpx's
built-in proxy support; Direct is just a plain client.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpcore
import httpx

from .ss2022 import SS2022Connection, SS2022Error, parse_ss_url


# ── Errors ───────────────────────────────────────────────────────

class ProxyConnectError(Exception):
    """Proxy itself is unreachable or broken → should failover."""


class UpstreamConnectError(Exception):
    """Proxy is fine but the upstream target is unreachable → no failover."""


# ── Stats ────────────────────────────────────────────────────────

@dataclass
class ProxyStats:
    total_attempts: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_attempt_ts: float = 0.0
    last_success_ts: float = 0.0
    last_error: str = ""
    last_latency_ms: float = 0.0
    total_bytes_up: int = 0
    total_bytes_down: int = 0



class CountingAsyncByteStream(httpx.AsyncByteStream):
    """Wrap request body streams and count uploaded bytes."""

    def __init__(self, stream, on_bytes):
        self._stream = stream
        self._on_bytes = on_bytes

    async def __aiter__(self):
        async for chunk in self._stream:
            if chunk:
                self._on_bytes(len(chunk), 0)
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close:
            await close()


class CountingResponseStream(httpx.AsyncByteStream):
    """Wrap response body streams and count downloaded bytes."""

    def __init__(self, stream, on_bytes):
        self._stream = stream
        self._on_bytes = on_bytes

    async def __aiter__(self):
        async for chunk in self._stream:
            if chunk:
                self._on_bytes(0, len(chunk))
            yield chunk

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close:
            await close()


class CountingAsyncTransport(httpx.AsyncBaseTransport):
    """Decorates an AsyncBaseTransport and records body bytes.

    Counts HTTP request/response body bytes (not TCP/IP/TLS overhead). This is
    the stable, protocol-neutral metric Parrot can expose without proxy-server
    cooperation.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, on_bytes):
        self._inner = inner
        self._on_bytes = on_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.stream = CountingAsyncByteStream(request.stream, self._on_bytes)
        resp = await self._inner.handle_async_request(request)
        if hasattr(resp, "_content"):
            self._on_bytes(0, len(resp._content))
        else:
            resp.stream = CountingResponseStream(resp.stream, self._on_bytes)
        return resp

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close:
            await close()


# ── Base Connector ───────────────────────────────────────────────

class Connector:
    """Base class for all proxy connectors."""

    name: str
    type: str  # "socks5" | "ss2022" | "direct"
    stats: ProxyStats

    def __init__(self, name: str, type_: str):
        self.name = name
        self.type = type_
        self.stats = ProxyStats()

    def display(self) -> str:
        return f"{self.name} ({self.type})"

    def record_bytes(self, up: int = 0, down: int = 0) -> None:
        if up:
            self.stats.total_bytes_up += int(up)
        if down:
            self.stats.total_bytes_down += int(down)

    def _counting_callback(self, extra_cb=None):
        def _cb(up: int = 0, down: int = 0):
            self.record_bytes(up, down)
            if extra_cb:
                extra_cb(int(up or 0), int(down or 0))
        return _cb

    def config_dict(self) -> dict:
        raise NotImplementedError

    def create_httpx_client(self, *, timeout: httpx.Timeout | None = None,
                            limits: httpx.Limits | None = None,
                            http2: bool = False, timing=None, **kw) -> httpx.AsyncClient:
        raise NotImplementedError

    async def test_connectivity(self, *, timeout: float = 8.0) -> dict:
        t0 = time.time()
        self.stats.total_attempts += 1
        self.stats.last_attempt_ts = t0
        try:
            to = httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout)
            async with self.create_httpx_client(timeout=to) as client:
                resp = await client.get("http://ip.sb/",
                                        headers={"User-Agent": "curl/8.0"})
                ip = resp.text.strip()
                lat = round((time.time() - t0) * 1000)
                self.stats.total_successes += 1
                self.stats.last_success_ts = time.time()
                self.stats.last_latency_ms = lat
                return {"ok": True, "ip": ip, "latency_ms": lat, "error": ""}
        except Exception as e:
            lat = round((time.time() - t0) * 1000)
            self.stats.total_failures += 1
            self.stats.last_error = str(e)[:200]
            return {"ok": False, "ip": "", "latency_ms": lat, "error": str(e)[:200]}


# ── Direct connector ─────────────────────────────────────────────

class DirectConnector(Connector):
    def __init__(self):
        super().__init__("direct", "direct")

    def display(self) -> str:
        return "direct (直连)"

    def config_dict(self) -> dict:
        return {"type": "direct"}

    def create_httpx_client(self, *, byte_counter=None, timing=None, **kw) -> httpx.AsyncClient:
        kw.setdefault("timeout", httpx.Timeout(10))
        limits = kw.pop("limits", None)
        http2 = bool(kw.pop("http2", False))
        if limits is None:
            transport = httpx.AsyncHTTPTransport(http2=http2)
        else:
            transport = httpx.AsyncHTTPTransport(limits=limits, http2=http2)
        if byte_counter is not None:
            transport = CountingAsyncTransport(transport, self._counting_callback(byte_counter))
        return httpx.AsyncClient(transport=transport, trust_env=False, **kw)


# ── SOCKS5 connector ────────────────────────────────────────────

class SOCKS5Connector(Connector):
    def __init__(self, name: str, url: str):
        super().__init__(name, "socks5")
        self.url = url

    def display(self) -> str:
        return f"{self.name} (SOCKS5) {_mask_url(self.url)}"

    def config_dict(self) -> dict:
        return {"type": "socks5", "url": self.url}

    def create_httpx_client(self, *, byte_counter=None, timing=None, **kw) -> httpx.AsyncClient:
        kw.setdefault("timeout", httpx.Timeout(10))
        limits = kw.pop("limits", None)
        http2 = bool(kw.pop("http2", False))
        proxy = httpx.Proxy(self.url)
        if limits is None:
            transport = httpx.AsyncHTTPTransport(proxy=proxy, http2=http2)
        else:
            transport = httpx.AsyncHTTPTransport(proxy=proxy, limits=limits, http2=http2)
        if byte_counter is not None:
            transport = CountingAsyncTransport(transport, self._counting_callback(byte_counter))
        return httpx.AsyncClient(transport=transport, trust_env=False, **kw)


# ── SS2022 connector (httpcore network backend) ──────────────────

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SS2022BridgeTerminal:
    """First terminal condition observed by an SS2022 duplex bridge."""

    direction: str
    cause: BaseException | None = None
    graceful: bool = False


class SS2022DuplexBridge:
    """Single owner for one SS2022 tunnel bridged through a socketpair.

    The application socket is owned by the bridge only until ``handoff_socket``.
    After handoff, asyncio / websockets owns that socket and this bridge never
    closes it directly.  The bridge continues to own the internal peer, both
    pump tasks, and the raw ``SS2022Connection``.
    """

    def __init__(self, conn: SS2022Connection, *, byte_counter: Any = None):
        self._conn = conn
        self._byte_counter = byte_counter
        self._loop = asyncio.get_running_loop()
        self._application_socket, self._internal_socket = socket.socketpair()
        self._application_socket.setblocking(False)
        self._internal_socket.setblocking(False)
        self._handed_off = False
        self._terminal: SS2022BridgeTerminal | None = None
        self._close_error: BaseException | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._pump_tasks: tuple[asyncio.Task[None], asyncio.Task[None]] = (
            asyncio.create_task(
                self._pump_application_to_ss(),
                name="ss2022-bridge-application-to-ss",
            ),
            asyncio.create_task(
                self._pump_ss_to_application(),
                name="ss2022-bridge-ss-to-application",
            ),
        )

    @classmethod
    async def create(
        cls,
        conn: SS2022Connection,
        *,
        byte_counter: Any = None,
    ) -> "SS2022DuplexBridge":
        try:
            return cls(conn, byte_counter=byte_counter)
        except BaseException:
            try:
                await conn.close()
            except Exception as close_exc:
                logger.debug(
                    "SS2022 connection close after bridge setup failure failed (%s)",
                    type(close_exc).__name__,
                )
            raise

    @property
    def terminal(self) -> SS2022BridgeTerminal | None:
        return self._terminal

    @property
    def close_error(self) -> BaseException | None:
        return self._close_error

    @property
    def closed(self) -> bool:
        return self._closed_event.is_set()

    @property
    def handed_off(self) -> bool:
        return self._handed_off

    @property
    def pump_tasks(self) -> tuple[asyncio.Task[None], asyncio.Task[None]]:
        return self._pump_tasks

    def handoff_socket(self) -> socket.socket:
        """Transfer the application socket to an asyncio transport exactly once."""
        if self._handed_off:
            raise RuntimeError("SS2022 bridge application socket already handed off")
        if self._close_task is not None:
            raise RuntimeError("SS2022 bridge is closing")
        self._handed_off = True
        return self._application_socket

    def _count(self, *, up: int = 0, down: int = 0) -> None:
        if self._byte_counter is not None:
            self._byte_counter.count(up=up, down=down)

    async def _pump_application_to_ss(self) -> None:
        try:
            while True:
                data = await self._loop.sock_recv(self._internal_socket, 65536)
                if not data:
                    await self._terminate_from_pump(
                        direction="application_to_ss_eof",
                        cause=None,
                        graceful=True,
                    )
                    return
                self._count(up=len(data))
                await self._conn.write(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._terminate_from_pump(
                direction="application_to_ss_error",
                cause=exc,
                graceful=False,
            )

    async def _pump_ss_to_application(self) -> None:
        try:
            while True:
                data = await self._conn.read(65536)
                if not data:
                    await self._terminate_from_pump(
                        direction="ss_to_application_eof",
                        cause=None,
                        graceful=True,
                    )
                    return
                self._count(down=len(data))
                await self._loop.sock_sendall(self._internal_socket, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._terminate_from_pump(
                direction="ss_to_application_error",
                cause=exc,
                graceful=False,
            )

    async def _terminate_from_pump(
        self,
        *,
        direction: str,
        cause: BaseException | None,
        graceful: bool,
    ) -> None:
        terminal = SS2022BridgeTerminal(
            direction=direction,
            cause=cause,
            graceful=graceful,
        )
        # A separate close supervisor owns cancellation/await of both pumps.
        # The terminating pump returns instead of awaiting a task that waits for
        # it, which avoids self-wait deadlocks while still making close complete.
        self._ensure_close_task(terminal=terminal)

    def _ensure_close_task(
        self,
        *,
        terminal: SS2022BridgeTerminal,
    ) -> asyncio.Task[None]:
        if self._terminal is None:
            self._terminal = terminal
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(graceful=self._terminal.graceful),
                name="ss2022-bridge-close",
            )
        return self._close_task

    async def _await_close_task(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    @staticmethod
    def _shutdown(sock: socket.socket, how: int) -> None:
        try:
            sock.shutdown(how)
        except OSError:
            # Already shut down or closed is the expected idempotent-close case.
            return

    async def _close_impl(self, *, graceful: bool) -> None:
        try:
            # SHUT_WR is the correct graceful EOF direction: the application
            # reads EOF from its socket. Real transport failures abort both ways.
            self._shutdown(
                self._internal_socket,
                socket.SHUT_WR if graceful else socket.SHUT_RDWR,
            )

            siblings = [task for task in self._pump_tasks if not task.done()]
            for task in siblings:
                task.cancel()
            if siblings:
                await asyncio.gather(*siblings, return_exceptions=True)

            try:
                self._internal_socket.close()
            except OSError:
                pass

            # Before handoff, nobody else can close the application socket. Once
            # handed off, closing it here would race the selector transport.
            if not self._handed_off:
                self._shutdown(self._application_socket, socket.SHUT_RDWR)
                try:
                    self._application_socket.close()
                except OSError:
                    pass

            try:
                await self._conn.close()
            except Exception as exc:
                self._close_error = exc
                logger.debug(
                    "SS2022 bridge raw close failed (%s)",
                    type(exc).__name__,
                )
        finally:
            self._closed_event.set()

    async def aclose(
        self,
        *,
        cause: BaseException | None = None,
        direction: str = "owner_close",
    ) -> None:
        task = self._ensure_close_task(
            terminal=SS2022BridgeTerminal(
                direction=direction,
                cause=cause,
                graceful=cause is None,
            ),
        )
        await self._await_close_task(task)

    async def wait_closed(self) -> None:
        await self._closed_event.wait()


class _SS2022Stream(httpcore.AsyncNetworkStream):
    """Wraps an SS2022Connection as an httpcore AsyncNetworkStream.

    httpcore / httpx use this for HTTP/1.1 protocol handling + optional TLS
    upgrade (start_tls), so we don't need to implement HTTP parsing ourselves.
    """

    def __init__(self, conn: SS2022Connection):
        self._conn = conn
        self._closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._closed:
            return b""
        try:
            data = await asyncio.wait_for(
                self._conn.read(max_bytes), timeout=timeout)
            return data
        except asyncio.TimeoutError:
            raise httpcore.ReadTimeout("SS2022 read timeout")
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if self._closed:
            raise httpcore.WriteError("connection closed")
        try:
            await asyncio.wait_for(
                self._conn.write(buffer), timeout=timeout)
        except asyncio.TimeoutError:
            raise httpcore.WriteTimeout("SS2022 write timeout")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._conn.close()

    async def start_tls(self, ssl_context: ssl.SSLContext,
                        server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        """Upgrade to TLS while transferring tunnel ownership to one bridge."""
        if self._closed:
            raise httpcore.ConnectError("SS2022 stream is closed")

        bridge = await SS2022DuplexBridge.create(self._conn)
        self._closed = True
        application_socket = bridge.handoff_socket()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    sock=application_socket,
                    ssl=ssl_context,
                    server_hostname=server_hostname,
                ),
                timeout=timeout,
            )
        except BaseException as exc:
            # Ownership transferred when open_connection received sock=. It must
            # unregister/close that socket; the bridge only closes its own peer.
            await bridge.aclose(cause=exc, direction="tls_handshake_error")
            raise
        return _TLSOverSS2022Stream(reader, writer, bridge)

    def get_extra_info(self, info: str) -> object:
        return None


class _TLSOverSS2022Stream(httpcore.AsyncNetworkStream):
    """TLS connection whose socket transport and SS bridge close in order."""

    def __init__(self, reader, writer, bridge: SS2022DuplexBridge):
        self._reader = reader
        self._writer = writer
        self._bridge = bridge
        self._close_task: asyncio.Task[None] | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await asyncio.wait_for(
                self._reader.read(max_bytes), timeout=timeout)
        except asyncio.TimeoutError:
            raise httpcore.ReadTimeout("TLS read timeout")

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._writer.write(buffer)
        try:
            await asyncio.wait_for(self._writer.drain(), timeout=timeout)
        except asyncio.TimeoutError:
            raise httpcore.WriteTimeout("TLS write timeout")

    @staticmethod
    def _abort_writer(writer) -> BaseException | None:
        transport = getattr(writer, "transport", None)
        abort = getattr(transport, "abort", None)
        if abort is None:
            return None
        try:
            abort()
        except Exception as exc:
            return exc
        return None

    async def _close_impl(self) -> None:
        close_error: BaseException | None = None
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception as exc:
            close_error = exc
            abort_error = self._abort_writer(self._writer)
            logger.debug(
                "TLS-over-SS2022 writer close failed (%s); abort=%s",
                type(exc).__name__,
                type(abort_error).__name__ if abort_error is not None else "ok",
            )
        finally:
            await self._bridge.aclose(
                cause=close_error,
                direction=(
                    "tls_stream_close_error" if close_error is not None
                    else "tls_stream_close"
                ),
            )

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(),
                name="tls-over-ss2022-close",
            )
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            try:
                await self._close_task
            finally:
                raise

    async def start_tls(self, *args, **kwargs):
        raise NotImplementedError("already TLS")

    def get_extra_info(self, info: str) -> object:
        if info == "ssl_object":
            return self._writer.get_extra_info("ssl_object")
        return None


class _SS2022Backend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that establishes SS2022 tunnels."""

    def __init__(self, cipher: str, password: str, server: str, port: int,
                 timing=None):
        self._cipher = cipher
        self._password = password
        self._server = server
        self._port = port
        self._timing = timing

    async def connect_tcp(self, host: str, port: int,
                          timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        conn = SS2022Connection(
            self._cipher, self._password, self._server, self._port,
            timing=self._timing,
        )
        try:
            await conn.connect(host, port, timeout=timeout or 8.0)
        except (ConnectionError, TimeoutError, OSError, SS2022Error) as e:
            raise httpcore.ConnectError(
                f"SS2022 {self._server}:{self._port}: {e}") from e
        return _SS2022Stream(conn)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.UnsupportedProtocol("unix sockets not supported via SS2022")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SS2022Connector(Connector):
    def __init__(self, name: str, server: str, port: int,
                 cipher: str, password: str):
        super().__init__(name, "ss2022")
        self.server = server
        self.port = port
        self.cipher = cipher
        self.password = password

    def display(self) -> str:
        return f"{self.name} (SS2022) {self.server}:{self.port}"

    def config_dict(self) -> dict:
        return {
            "type": "ss2022",
            "server": self.server,
            "port": self.port,
            "cipher": self.cipher,
            "password": self.password,
        }

    def create_httpx_client(self, *, byte_counter=None, timing=None, **kw) -> httpx.AsyncClient:
        kw.setdefault("timeout", httpx.Timeout(10))
        limits = kw.pop("limits", None)
        http2 = bool(kw.pop("http2", False))
        backend = _SS2022Backend(
            self.cipher, self.password, self.server, self.port, timing=timing,
        )
        # Inject our network backend into httpx's transport pool.
        if limits is None:
            base_transport = httpx.AsyncHTTPTransport(http2=http2)
        else:
            base_transport = httpx.AsyncHTTPTransport(limits=limits, http2=http2)
        old_pool = base_transport._pool
        base_transport._pool = httpcore.AsyncConnectionPool(
            ssl_context=old_pool._ssl_context,
            network_backend=backend,
            max_connections=old_pool._max_connections,
            max_keepalive_connections=old_pool._max_keepalive_connections,
            keepalive_expiry=old_pool._keepalive_expiry,
            http1=old_pool._http1,
            http2=http2,
            retries=old_pool._retries,
            local_address=old_pool._local_address,
            socket_options=old_pool._socket_options,
        )
        transport = base_transport
        if byte_counter is not None:
            transport = CountingAsyncTransport(transport, self._counting_callback(byte_counter))
        return httpx.AsyncClient(transport=transport, trust_env=False, **kw)


# ── Factory ──────────────────────────────────────────────────────

def connector_from_config(name: str, cfg: dict) -> Connector:
    t = cfg.get("type", "")
    if t == "direct":
        return DirectConnector()
    if t == "socks5":
        return SOCKS5Connector(name, cfg["url"])
    if t == "ss2022":
        return SS2022Connector(name, cfg["server"], cfg["port"],
                               cfg["cipher"], cfg["password"])
    raise ValueError(f"unknown proxy type: {t}")


def parse_proxy_url(text: str) -> dict:
    """Parse a proxy URL (ss:// or socks5://) into a config dict + optional name."""
    s = text.strip()
    if s.lower().startswith("ss://"):
        info = parse_ss_url(s)
        return {
            "type": "ss2022",
            "server": info["server"],
            "port": info["port"],
            "cipher": info["cipher"],
            "password": info["password"],
            "name": info.get("name", ""),
        }
    low = s.lower()
    if low.startswith(("socks5://", "socks5h://", "socks://")) or "://" not in s:
        if "://" not in s:
            s = "socks5://" + s
        elif low.startswith("socks://"):
            s = "socks5://" + s[8:]
        elif low.startswith("socks5h://"):
            s = "socks5://" + s[10:]
        p = urlparse(s)
        if not p.hostname or not p.port:
            raise ValueError("SOCKS5 URL must include host and port")
        name = p.fragment.strip() if p.fragment else ""
        clean = "socks5://"
        if p.username:
            clean += p.username
            if p.password:
                clean += f":{p.password}"
            clean += "@"
        clean += f"{p.hostname}:{p.port}"
        return {"type": "socks5", "url": clean, "name": name}
    raise ValueError(f"unsupported proxy URL: {s}")


def _mask_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.password:
            return url.replace(f":{p.password}@", ":***@", 1)
        return url
    except Exception:
        return url
