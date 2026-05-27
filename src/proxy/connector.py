"""Proxy connectors: abstract base + SOCKS5 / SS2022 / Direct implementations.

Each connector creates an httpx.AsyncClient that routes traffic through the
proxy.  SS2022 uses a custom httpcore network backend; SOCKS5 uses httpx's
built-in proxy support; Direct is just a plain client.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass
from typing import Optional
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
                            http2: bool = False, **kw) -> httpx.AsyncClient:
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

    def create_httpx_client(self, *, byte_counter=None, **kw) -> httpx.AsyncClient:
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

    def create_httpx_client(self, *, byte_counter=None, **kw) -> httpx.AsyncClient:
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
        self._closed = True
        await self._conn.close()

    async def start_tls(self, ssl_context: ssl.SSLContext,
                        server_hostname: str | None = None,
                        timeout: float | None = None) -> httpcore.AsyncNetworkStream:
        """Upgrade to TLS over the SS2022 tunnel (for HTTPS requests)."""
        # We need to do a real TLS handshake over the encrypted tunnel.
        # Use asyncio's built-in TLS support via a socket pair.
        loop = asyncio.get_running_loop()

        # Create a connected socket pair
        import socket
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)
        wsock.setblocking(False)

        # Pump data between SS2022 conn ↔ wsock in background
        pump_task = asyncio.ensure_future(
            self._pump(wsock, loop))

        try:
            # Do TLS handshake on rsock
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    sock=rsock,
                    ssl=ssl_context,
                    server_hostname=server_hostname,
                ),
                timeout=timeout,
            )
            return _TLSOverSS2022Stream(reader, writer, pump_task, wsock)
        except Exception:
            pump_task.cancel()
            rsock.close()
            wsock.close()
            raise

    async def _pump(self, wsock, loop):
        """Bidirectional pump between SS2022 conn and a socket."""
        try:
            await asyncio.gather(
                self._pump_read(wsock, loop),
                self._pump_write(wsock, loop),
            )
        except (asyncio.CancelledError, Exception):
            pass

    async def _pump_read(self, wsock, loop):
        """SS2022 → wsock"""
        try:
            while not self._closed:
                data = await self._conn.read(65536)
                if not data:
                    break
                await loop.sock_sendall(wsock, data)
        except Exception:
            pass
        finally:
            try:
                wsock.shutdown(0)
            except Exception:
                pass

    async def _pump_write(self, wsock, loop):
        """wsock → SS2022"""
        try:
            while not self._closed:
                data = await loop.sock_recv(wsock, 65536)
                if not data:
                    break
                await self._conn.write(data)
        except Exception:
            pass

    def get_extra_info(self, info: str) -> object:
        return None


class _TLSOverSS2022Stream(httpcore.AsyncNetworkStream):
    """TLS connection running over an SS2022 tunnel."""

    def __init__(self, reader, writer, pump_task, wsock):
        self._reader = reader
        self._writer = writer
        self._pump_task = pump_task
        self._wsock = wsock

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

    async def aclose(self) -> None:
        self._writer.close()
        self._pump_task.cancel()
        try:
            self._wsock.close()
        except Exception:
            pass

    async def start_tls(self, *args, **kwargs):
        raise NotImplementedError("already TLS")

    def get_extra_info(self, info: str) -> object:
        if info == "ssl_object":
            return self._writer.get_extra_info("ssl_object")
        return None


class _SS2022Backend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that establishes SS2022 tunnels."""

    def __init__(self, cipher: str, password: str, server: str, port: int):
        self._cipher = cipher
        self._password = password
        self._server = server
        self._port = port

    async def connect_tcp(self, host: str, port: int,
                          timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        conn = SS2022Connection(self._cipher, self._password,
                                self._server, self._port)
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

    def create_httpx_client(self, *, byte_counter=None, **kw) -> httpx.AsyncClient:
        kw.setdefault("timeout", httpx.Timeout(10))
        limits = kw.pop("limits", None)
        http2 = bool(kw.pop("http2", False))
        backend = _SS2022Backend(self.cipher, self.password,
                                 self.server, self.port)
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
