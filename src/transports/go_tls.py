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
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import network
from ..async_owned import await_owned

PROFILE = "antigravity-ls-2.22.0"


def native_client_type():
    # Import lazily: disabled/legacy curl deployments retain their old path.
    from parrot_go_tls_client import Client
    return Client


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
                    raise _error(event, self.request)
                return event

    async def __aiter__(self):
        try:
            while True:
                event = await self.receive()
                if event["kind"] == "eof":
                    return
                if event["kind"] != "data":
                    raise httpx.ProtocolError("unexpected native stream event", request=self.request)
                chunk = base64.b64decode(event["data"])
                if chunk:
                    if self.transport.byte_counter is not None:
                        self.transport.byte_counter(0, len(chunk))
                    yield chunk
        finally:
            await self.aclose()

    async def _close(self):
        self.native.cancel()  # Stop native IO immediately, before executor queueing.
        try:
            terminal = await asyncio.to_thread(self.native.close)
            if terminal.get("dispatched"):
                await self.mark_dispatch()
        finally:
            await asyncio.to_thread(self.client.close)
            self.transport._streams.discard(self)
            await self.transport._close_route()
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
        self._close_task = None
        self._closed = False
        self._route_lock = asyncio.Lock()

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
            client = self._client_type(lib_path=self.lib_path, proxy=proxy,
                                       proxy_auth=proxy_auth, insecure=self.insecure, ca_pem=self.ca_pem)
            try:
                check_start_allowed()
                native = client.start(
                    request.method, str(request.url), headers=request.headers.raw, body=content,
                    connect_timeout=None if connect is None else max(1e-9, connect - (time.monotonic() - started)),
                    read_timeout=None if values.get("read") is None else max(1e-9, values["read"]),
                    write_timeout=None if values.get("write") is None else max(1e-9, values["write"]),
                    dial_ips=addresses, require_resolved=True,
                )
            except BaseException:
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


async def _routed_request(method, url, *, headers, json, data, timeout,
                          proxy_purpose, proxy_channel, proxy_model, proxy_provider):
    chain = network._configured_proxy_chain_or_none(
        proxy_purpose=proxy_purpose, proxy_channel=proxy_channel,
        proxy_model=proxy_model, proxy_provider=proxy_provider,
    )
    routes = chain if chain is not None else [("legacy", None)]
    last = None
    for _name, connector in routes:
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
                owner = await connector.acquire_curl_proxy(target.host, target.port or 443, timeout=timeout)
            except Exception as exc:
                # Lease construction owns its partial cleanup and cannot have
                # sent an upstream request. Match runtime's setup failover.
                last = exc
                continue
            proxy, auth = owner.proxy_url, owner.proxy_auth
        elif connector.type != "direct":
            raise httpx.ProxyError("route is unsupported by LS transport")
        try:
            async with create_client(timeout=httpx.Timeout(timeout), proxy=proxy, proxy_auth=auth,
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
