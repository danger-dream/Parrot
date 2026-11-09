"""LS 2.22.0 Go transport behind Parrot's existing HTTPX contract.

Only TLS/HTTP I/O lives here. Routing, SSE parsing, business deadlines and retries
remain owned by Parrot. No implicit replay: every native request owns one h2
connection. The LS sample's business-host/dynamic equivalence is unconfirmed.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import ipaddress
import math
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import config, network
from ..async_owned import await_owned
from .http import before_native_handoff

PROFILE = "antigravity-ls-2.22.0"


def _default_native_client_type():
    from parrot_go_tls_client import Client
    return Client


def native_client_type():
    # Import lazily: disabled/legacy curl deployments retain their old path.
    return _default_native_client_type()


async def _owned(awaitable):
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except BaseException:
        try:
            await await_owned(task)
        except BaseException:
            pass
        raise


async def _trace(request: httpx.Request, name: str):
    callback = request.extensions.get("trace")
    if callback is not None:
        result = callback(name, {})
        if inspect.isawaitable(result):
            await result


def _error(event: dict, request: httpx.Request) -> BaseException:
    code = event.get("code", "protocol")
    dispatched = bool(event.get("dispatched"))
    if code == "cancelled":
        return asyncio.CancelledError()
    cls = {
        "connect_timeout": httpx.ConnectTimeout,
        "read_timeout": httpx.ReadTimeout,
        "write_timeout": httpx.WriteTimeout,
        "total_timeout": httpx.TimeoutException,
        "certificate": httpx.ConnectError,
        "connect": httpx.ConnectError,
        "read": httpx.ReadError,
    }.get(code, httpx.ProtocolError)
    # Even an unexpected native connect error may not advertise safe replay if
    # the physical-send boundary has already been crossed.
    if dispatched and cls is httpx.ConnectError:
        cls = httpx.ProtocolError
    exc = cls(event.get("message") or code, request=request)
    exc.parrot_dispatched = dispatched
    exc.parrot_native_code = code
    return exc


def _environment_proxy(url: httpx.URL) -> str:
    # Match HTTPX's NO_PROXY/ALL_PROXY precedence, not Go's process DNS/proxy defaults.
    from httpx._utils import URLPattern, get_environment_proxies
    for pattern, proxy in sorted(
        ((URLPattern(k), v) for k, v in get_environment_proxies().items()),
        key=lambda item: item[0],
    ):
        if pattern.matches(url):
            return str(proxy or "")
    return ""


async def _resolve_peer(host: str, timeout: float | None) -> list[str]:
    """Resolve only the socket peer; SOCKS/SS targets keep remote DNS semantics."""
    try:
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    if network._should_custom_resolve(host):
        # Same configured servers/cache/IPv6 policy, but cancel the actual DNS
        # socket owner, not an executor future whose thread outlives the request.
        return await asyncio.wait_for(network.resolve_host_async(
            host, timeout=network.dns_timeout() if timeout is None else min(timeout, network.dns_timeout()),
        ), timeout)
    return await asyncio.wait_for(network._system_getaddrinfo_async(host), timeout)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, transport, client, native, request, content_length):
        self.transport, self.client, self.native = transport, client, native
        self.request, self.content_length = request, content_length
        self.dispatched = False
        self.clean_eof = False
        self.aborted = False
        self.closed = False
        self._close_task = None

    async def mark_dispatch(self):
        if self.dispatched:
            return
        self.dispatched = True
        if self.transport.byte_counter is not None and self.content_length:
            self.transport.byte_counter(self.content_length, 0)
        await _trace(self.request, "http2.send_request_headers.started")
        await _trace(self.request, "http2.send_request_body.started")

    def mark_connection_complete(self):
        lifecycle = self.request.extensions.get("parrot_transport_lifecycle")
        complete = lifecycle.get("mark_connection_complete") if isinstance(lifecycle, dict) else None
        if callable(complete):
            complete()

    async def receive(self):
        while True:
            event = self.native.poll(0)  # Nonblocking FFI; never waits for body EOF.
            kind = event["kind"]
            if kind == "dispatch":
                await self.mark_dispatch()
            elif kind == "uploaded":
                await self.mark_dispatch()
                await _trace(self.request, "http2.send_request_body.complete")
                self.mark_connection_complete()
            elif kind == "pending":
                await asyncio.sleep(0.002)
            else:
                if event.get("dispatched"):
                    await self.mark_dispatch()
                if kind == "error":
                    self.aborted = True
                    raise _error(event, self.request)
                if kind == "eof":
                    self.clean_eof = True
                return event

    async def __aiter__(self):
        try:
            while True:
                event = await self.receive()
                if event["kind"] == "eof":
                    return
                if event["kind"] != "data":
                    self.aborted = True
                    raise httpx.ProtocolError("unexpected native stream event", request=self.request)
                chunk = base64.b64decode(event["data"])
                if chunk:
                    if self.transport.byte_counter is not None:
                        self.transport.byte_counter(0, len(chunk))
                    yield chunk
        except asyncio.CancelledError:
            self.aborted = True
            raise
        finally:
            await self.aclose()

    async def _close(self):
        if not self.clean_eof and not self.aborted and self.dispatched and not self.transport._closed:
            event = await asyncio.to_thread(self.native.poll, 250)
            if event.get("kind") == "eof":
                self.clean_eof = True
        self.native.cancel()  # Stop native IO immediately, before executor queueing.
        try:
            terminal = await asyncio.to_thread(self.native.close)
            if terminal.get("dispatched"):
                await self.mark_dispatch()
            if terminal.get("kind") == "eof" and not self.aborted:
                self.clean_eof = True
        finally:
            self.transport._streams.discard(self)
            if not self.clean_eof or self.transport._closed:
                await self.transport._evict_native_client(self.client)
                await self.transport._close_route()
            else:
                self.transport._last_stream_clean_eof = True
            self.closed = True

    async def aclose(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _owned(asyncio.shield(self._close_task))


class GoTLSTransport(httpx.AsyncBaseTransport):
    """Explicit one-attempt owner; per-request sockets, bounded native body queue."""
    def __init__(self, *, proxy: str = "", proxy_auth=None, trust_env: bool = False,
                 byte_counter=None, route_owner=None, resolver=None,
                 insecure: bool = False, ca_pem: str = "", lib_path=None):
        from .fingerprint import FingerprintBackendUnavailable
        try:
            self._client_type = native_client_type()
        except (ImportError, OSError) as exc:
            raise FingerprintBackendUnavailable("parrot-go-tls-client is not installed") from exc
        self.proxy, self.proxy_auth, self.trust_env = proxy, proxy_auth, trust_env
        self.byte_counter, self.route_owner = byte_counter, route_owner
        self.resolver = resolver or _resolve_peer
        self.insecure, self.ca_pem, self.lib_path = insecure, ca_pem, lib_path
        self._streams: set[_ResponseStream] = set()
        self._starts: set[asyncio.Task] = set()
        self._resolutions: set[asyncio.Task] = set()
        self._native_client = None
        self._native_client_lock = threading.Lock()
        self._last_stream_clean_eof = False
        self._close_task = None
        self._closed = False
        self._route_lock = asyncio.Lock()

    async def _evict_native_client(self, expected=None):
        with self._native_client_lock:
            if expected is not None and self._native_client is not expected:
                client = expected
            else:
                client, self._native_client = self._native_client, None
        if client is not None:
            await asyncio.to_thread(client.close)

    async def _close_route(self):
        async with self._route_lock:
            owner, self.route_owner = self.route_owner, None
            self.proxy_auth = None
            if owner is not None:
                await _owned(owner.aclose())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed:
            raise httpx.TransportError("Go TLS transport is closed", request=request)
        if request.url.scheme != "https":
            raise httpx.UnsupportedProtocol("LS transport requires HTTPS/h2", request=request)
        self._last_stream_clean_eof = False
        content = await request.aread()
        values = request.extensions.get("timeout", {})
        connect = values.get("connect", 15.0)
        if connect is not None:
            connect = float(connect)
            if connect <= 0:
                raise httpx.ConnectTimeout("connect timeout exhausted before send", request=request)
        proxy = self.proxy or (_environment_proxy(request.url) if self.trust_env else "")
        host = urlsplit(proxy).hostname if proxy else request.url.host
        started = time.monotonic()
        resolution = asyncio.create_task(self.resolver(str(host or ""), connect))
        self._resolutions.add(resolution)
        try:
            try:
                addresses = await asyncio.shield(resolution)
            except BaseException:
                if not resolution.cancelling():
                    resolution.cancel()
                try:
                    await _owned(resolution)
                except BaseException:
                    pass
                raise
        except asyncio.TimeoutError as exc:
            raise httpx.ConnectTimeout("configured DNS resolution timed out", request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        finally:
            self._resolutions.discard(resolution)
        if self._closed:
            raise httpx.TransportError("Go TLS transport closed during DNS", request=request)
        if not addresses:
            raise httpx.ConnectError("configured resolver returned no peer addresses", request=request)
        # Native start serializes/copies the whole body. Own the worker result:
        # cancelling its asyncio waiter must not abandon a newly created socket.
        # Transport.close also drains these starts before releasing the route.
        holder = []
        abandoned = threading.Event()
        proxy_auth = self.proxy_auth

        def check_start_allowed():
            if abandoned.is_set() or self._closed:
                raise httpx.TransportError("Go TLS request cancelled before native start", request=request)

        def start_native():
            check_start_allowed()
            with self._native_client_lock:
                client = self._native_client
                if client is None:
                    client = self._client_type(
                        lib_path=self.lib_path, proxy=proxy,
                        proxy_auth=proxy_auth, insecure=self.insecure, ca_pem=self.ca_pem,
                    )
                    self._native_client = client
            try:
                check_start_allowed()
                # Last synchronous gate after DNS, worker queueing and client
                # construction. Native poll/close events are retrospective.
                before_native_handoff(request)
                native = client.start(
                    request.method, str(request.url), headers=request.headers.raw, body=content,
                    connect_timeout=None if connect is None else max(1e-9, connect - (time.monotonic() - started)),
                    read_timeout=None if values.get("read") is None else max(1e-9, values["read"]),
                    write_timeout=None if values.get("write") is None else max(1e-9, values["write"]),
                    dial_ips=addresses, require_resolved=True,
                )
            except BaseException:
                with self._native_client_lock:
                    if self._native_client is client:
                        self._native_client = None
                client.close()
                raise
            return client, native

        async def start_stream():
            client, native = await asyncio.to_thread(start_native)
            stream = _ResponseStream(self, client, native, request, len(content))
            self._streams.add(stream)
            holder.append(stream)
            return stream

        start_task = asyncio.create_task(start_stream())
        self._starts.add(start_task)
        try:
            stream = await asyncio.shield(start_task)
        except BaseException:
            abandoned.set()
            try:
                await _owned(start_task)
            except BaseException:
                pass
            if holder:
                await holder[0].aclose()
            raise
        finally:
            self._starts.discard(start_task)
        try:
            if self._closed:
                raise httpx.TransportError("Go TLS transport closed during start", request=request)
            event = await stream.receive()
            if event["kind"] != "headers":
                raise httpx.ProtocolError("native response ended before headers", request=request)
            # A final response may reject a flow-controlled upload before it
            # completes. End the business connection phase without inventing a
            # successful upload trace, including for non-stream/image consumers.
            stream.mark_connection_complete()
            return httpx.Response(event["status"], headers=event["headers_raw"], stream=stream,
                                  request=request, extensions={"http_version": b"HTTP/2"})
        except BaseException:
            await stream.aclose()  # Freeze final send state before error/cancel can reach failover.
            raise

    async def _close(self):
        try:
            pending = tuple(self._resolutions | self._starts)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for stream in tuple(self._streams):
                await stream.aclose()
        finally:
            try:
                await self._evict_native_client()
            finally:
                await self._close_route()

    async def aclose(self):
        self._closed = True
        for resolution in tuple(self._resolutions):
            if not resolution.cancelling():
                resolution.cancel()
        for stream in tuple(self._streams):
            stream.native.cancel()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _owned(asyncio.shield(self._close_task))


def create_client(*, timeout: httpx.Timeout, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=GoTLSTransport(**kwargs), timeout=timeout,
                             trust_env=False, follow_redirects=False)


_DEFAULT_CREATE_CLIENT = create_client
_POOL_IDLE_TTL_SECONDS = 90.0
_MAX_IDLE_CLIENTS_PER_KEY = 4
_POOL_LOCK = threading.Lock()
_POOL_GENERATION = 0
_POOL_HOOKED = False
_IDLE_POOL: dict[tuple, list["_PooledClientEntry"]] = {}


class _PooledClientEntry:
    def __init__(self, *, key: tuple, loop: asyncio.AbstractEventLoop,
                 generation: int, client: httpx.AsyncClient,
                 transport: GoTLSTransport, ss_lease: Any = None):
        self.key = key
        self.loop = loop
        self.generation = generation
        self.client = client
        self.transport = transport
        self.ss_lease = ss_lease
        self.last_used = time.monotonic()

    def is_healthy(self, now: float) -> bool:
        if now - self.last_used > _POOL_IDLE_TTL_SECONDS:
            return False
        if self.transport._closed or self.transport._native_client is None:
            return False
        if self.transport._streams or self.transport._starts or self.transport._resolutions:
            return False
        if self.ss_lease is not None:
            if getattr(self.ss_lease, "closed", False) or getattr(self.ss_lease, "connect_failed", False):
                return False
            if not getattr(self.ss_lease, "tunnel_active", False):
                return False
        return True


class _PooledClientLease:
    """Per-request checkout wrapper around a pooled GoTLSTransport AsyncClient."""

    def __init__(self, entry: _PooledClientEntry):
        self._entry: _PooledClientEntry | None = entry
        self._closed = False

    @property
    def _transport(self):
        return self._entry.transport if self._entry is not None else None

    def stream(self, *args, **kwargs):
        if self._entry is None:
            raise httpx.TransportError("pooled Go TLS client lease is closed")
        return self._entry.client.stream(*args, **kwargs)

    async def request(self, *args, **kwargs):
        if self._entry is None:
            raise httpx.TransportError("pooled Go TLS client lease is closed")
        return await self._entry.client.request(*args, **kwargs)

    async def get(self, *args, **kwargs):
        if self._entry is None:
            raise httpx.TransportError("pooled Go TLS client lease is closed")
        return await self._entry.client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        if self._entry is None:
            raise httpx.TransportError("pooled Go TLS client lease is closed")
        return await self._entry.client.post(*args, **kwargs)

    async def aclose(self) -> None:
        if self._closed or self._entry is None:
            return
        self._closed = True
        entry, self._entry = self._entry, None
        entry.transport.byte_counter = None
        if entry.ss_lease is not None and hasattr(entry.ss_lease, "rebind_timing"):
            entry.ss_lease.rebind_timing(None)
        if not _return_pooled_entry(entry):
            await _owned(entry.client.aclose())


def _schedule_entry_close(entry: _PooledClientEntry) -> None:
    try:
        if entry.loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is entry.loop:
            asyncio.create_task(entry.client.aclose())
        else:
            entry.loop.call_soon_threadsafe(lambda: asyncio.create_task(entry.client.aclose()))
    except Exception:
        pass


def invalidate_client_pool(_cfg: dict | None = None) -> None:
    global _POOL_GENERATION
    with _POOL_LOCK:
        _POOL_GENERATION += 1
        evicted = [item for bucket in _IDLE_POOL.values() for item in bucket]
        _IDLE_POOL.clear()
    for entry in evicted:
        _schedule_entry_close(entry)


def _ensure_pool_hook() -> None:
    global _POOL_HOOKED
    with _POOL_LOCK:
        if _POOL_HOOKED:
            return
        _POOL_HOOKED = True
    config.on_reload(invalidate_client_pool)


def _take_pooled_entry(key: tuple, loop: asyncio.AbstractEventLoop) -> _PooledClientEntry | None:
    now = time.monotonic()
    stale: list[_PooledClientEntry] = []
    chosen: _PooledClientEntry | None = None
    with _POOL_LOCK:
        for k in list(_IDLE_POOL.keys()):
            bucket = _IDLE_POOL.get(k) or []
            kept: list[_PooledClientEntry] = []
            for item in bucket:
                if item.generation != _POOL_GENERATION or item.loop is not loop or (now - item.last_used > _POOL_IDLE_TTL_SECONDS):
                    stale.append(item)
                else:
                    kept.append(item)
            if kept:
                _IDLE_POOL[k] = kept
            else:
                _IDLE_POOL.pop(k, None)
        bucket = _IDLE_POOL.get(key) or []
        while bucket:
            candidate = bucket.pop()
            if candidate.is_healthy(now):
                chosen = candidate
                break
            stale.append(candidate)
        if not bucket:
            _IDLE_POOL.pop(key, None)
    for item in stale:
        _schedule_entry_close(item)
    return chosen


def _return_pooled_entry(entry: _PooledClientEntry) -> bool:
    now = time.monotonic()
    with _POOL_LOCK:
        if entry.generation != _POOL_GENERATION or entry.loop.is_closed():
            return False
        t = entry.transport
        if t._closed or not t._last_stream_clean_eof or t._native_client is None:
            return False
        if t._streams or t._starts or t._resolutions:
            return False
        if entry.ss_lease is not None and not getattr(entry.ss_lease, "tunnel_active", False):
            return False
        bucket = _IDLE_POOL.setdefault(entry.key, [])
        if len(bucket) >= _MAX_IDLE_CLIENTS_PER_KEY:
            return False
        entry.last_used = now
        bucket.append(entry)
        return True


def can_use_client_pool(profile: str) -> bool:
    if str(profile or "").strip() != PROFILE or create_client is not _DEFAULT_CREATE_CLIENT:
        return False
    try:
        return native_client_type() is _default_native_client_type()
    except Exception:
        return False


async def acquire_pooled_client(
    *,
    route_type: str,
    connector: Any,
    target_url: httpx.URL,
    timeout: httpx.Timeout,
    connect_timeout: float,
    byte_counter: Any,
    late_timing: Any,
) -> _PooledClientLease:
    _ensure_pool_hook()
    loop = asyncio.get_running_loop()
    target_host = str(target_url.host or "").lower()
    target_port = int(target_url.port or 443)
    if connector is not None and str(route_type) == "ss2022":
        route_id = (
            "ss2022",
            str(getattr(connector, "name", "")),
            str(getattr(connector, "server", "")),
            int(getattr(connector, "port", 0) or 0),
            str(getattr(connector, "cipher", "")),
            str(getattr(connector, "password", "")),
        )
    elif connector is not None and str(route_type) == "socks5":
        route_id = ("socks5", str(getattr(connector, "url", "") or ""))
    elif connector is None:
        legacy_proxy = str(network.active_socks5_url() or "")
        route_id = ("legacy", legacy_proxy, bool(not legacy_proxy))
    else:
        route_id = ("direct", "", False)

    key = (id(loop), network._signature(), route_id, (target_host, target_port))
    entry = _take_pooled_entry(key, loop)
    if entry is not None:
        entry.transport.byte_counter = byte_counter
        entry.transport._last_stream_clean_eof = False
        entry.client.timeout = timeout
        if entry.ss_lease is not None and hasattr(entry.ss_lease, "rebind_timing"):
            entry.ss_lease.rebind_timing(late_timing, connect_timeout=connect_timeout)
        return _PooledClientLease(entry)

    ss_lease = None
    proxy = ""
    auth = None
    trust_env = False
    if connector is not None and str(route_type) == "socks5":
        proxy = str(getattr(connector, "url", "") or "")
    elif connector is not None and str(route_type) == "ss2022":
        if target_url.scheme != "https" or not target_url.host:
            raise ValueError("SS2022 fingerprint bridge requires an HTTPS target")
        ss_lease = await connector.acquire_curl_proxy(
            str(target_url.host),
            target_port,
            timeout=connect_timeout,
            timing=late_timing,
        )
        if hasattr(ss_lease, "enable_reconnect"):
            ss_lease.enable_reconnect()
        proxy = ss_lease.proxy_url
        auth = ss_lease.proxy_auth
    elif connector is None:
        proxy = str(network.active_socks5_url() or "")
        trust_env = not proxy

    try:
        with _POOL_LOCK:
            gen = _POOL_GENERATION
        transport = GoTLSTransport(
            proxy=proxy,
            proxy_auth=auth,
            trust_env=trust_env,
            byte_counter=byte_counter,
            route_owner=ss_lease,
        )
        client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )
        entry = _PooledClientEntry(
            key=key,
            loop=loop,
            generation=gen,
            client=client,
            transport=transport,
            ss_lease=ss_lease,
        )
        return _PooledClientLease(entry)
    except BaseException:
        if ss_lease is not None:
            await _owned(ss_lease.aclose())
        raise


def _route_connect_timeout() -> float:
    try:
        value = float((config.get().get("timeouts") or {}).get("connect", 10))
    except (TypeError, ValueError):
        return 10.0
    return value if math.isfinite(value) and value > 0 else 10.0


async def _routed_request(method, url, *, headers, json, data, timeout,
                          proxy_purpose, proxy_channel, proxy_model, proxy_provider):
    chain = network._configured_proxy_chain_or_none(
        proxy_purpose=proxy_purpose, proxy_channel=proxy_channel,
        proxy_model=proxy_model, proxy_provider=proxy_provider,
    )
    routes = chain if chain is not None else [("legacy", None)]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    route_connect = _route_connect_timeout()
    last = None
    for index, (_name, connector) in enumerate(routes):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        # An unreachable route may only spend its connect budget, leaving the
        # rest of the shared deadline to later routes; the last keeps it all.
        connect = remaining if index == len(routes) - 1 else min(route_connect, remaining)
        owner = None
        proxy = ""
        auth = None
        if connector is None:
            proxy = network.active_socks5_url() or ""
        elif connector.type == "socks5":
            proxy = connector.url
        elif connector.type == "ss2022":
            target = httpx.URL(url)
            if target.scheme != "https":
                raise httpx.UnsupportedProtocol("SS2022 LS bridge requires HTTPS")
            try:
                owner = await connector.acquire_curl_proxy(target.host, target.port or 443, timeout=connect)
            except Exception as exc:
                # Lease construction owns its partial cleanup and cannot have
                # sent an upstream request. Match runtime's setup failover.
                last = exc
                continue
            proxy, auth = owner.proxy_url, owner.proxy_auth
        elif connector.type != "direct":
            raise httpx.ProxyError("route is unsupported by LS transport")
        try:
            async with create_client(timeout=httpx.Timeout(remaining, connect=connect), proxy=proxy, proxy_auth=auth,
                                     trust_env=connector is None and not proxy, route_owner=owner) as client:
                return await client.request(method, url, headers=headers, json=json, data=data)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if getattr(exc, "parrot_dispatched", False):
                raise
            last = exc
        finally:
            # Construction failure must release a bridge even before transport ownership.
            if owner is not None:
                await _owned(owner.aclose())
    if last is not None:
        raise last
    raise httpx.ProxyError("configured route has no usable LS transport target")


def request_sync(method: str, url: str, *, headers=None, json=None, data=None,
                 timeout: float = 30.0, proxy_purpose: str = "", proxy_channel: str = "",
                 proxy_model: str = "", proxy_provider: str = "antigravity") -> httpx.Response:
    """Provider-scoped sync helper; caller selects enabled/profile, never global network replacement.

    Uses the exact existing network proxy-chain selector (including oauth channel
    account routing), remote DNS for proxied targets, and the SS2022 CONNECT lease.
    Call sync code from a worker, not from an already running asyncio event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("request_sync must run outside the event loop; use asyncio.to_thread")
    try:
        return asyncio.run(asyncio.wait_for(_routed_request(
            method, url, headers=headers, json=json, data=data, timeout=float(timeout),
            proxy_purpose=proxy_purpose, proxy_channel=proxy_channel,
            proxy_model=proxy_model, proxy_provider=proxy_provider,
        ), timeout=float(timeout)))
    except TimeoutError as exc:
        failure = httpx.TimeoutException("LS control-plane request total timeout")
        # The outer deadline can interrupt any phase. Without an exported final
        # state at this sync boundary it must never be advertised as safe replay.
        failure.parrot_dispatched = True
        failure.parrot_native_code = "total_timeout"
        raise failure from exc
