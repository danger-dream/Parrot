"""Antigravity TLS/H2 impersonation transport backed by ``curl_cffi``.

The adapter deliberately keeps the httpx boundary used by the rest of Parrot.
It is only constructed for an explicitly enabled Antigravity channel and for a
route curl can represent directly (direct or SOCKS5).

curl_cffi does not expose httpcore's upload-complete trace event.  The adapter
therefore uses a conservative lifecycle bridge:

* the business connection phase ends when control is handed to curl; libcurl's
  own connect timeout remains active;
* dispatch is recorded after response headers prove that the request left the
  process, or before propagating an ambiguous timeout/cancellation/error;
* failures known to happen before any upstream dispatch (DNS/connect/profile
  setup) remain retryable by the existing route chain.

This boundary may count connect time against ``firstByte`` and cannot populate
httpcore-only DNS/TCP/TLS diagnostics, but it never treats an ambiguous send as
safe to replay.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

_IMPORT_ERROR: BaseException | None = None
try:  # Optional at import time so disabled deployments keep their old path.
    from curl_cffi.const import CurlECode as _CurlECode  # type: ignore
    from curl_cffi.requests import AsyncSession as _AsyncSession  # type: ignore
    from curl_cffi.requests import exceptions as _curl_errors  # type: ignore
except Exception as exc:  # pragma: no cover - depends on deployment packages
    _AsyncSession = None  # type: ignore[assignment]
    _curl_errors = None  # type: ignore[assignment]
    _CurlECode = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DECODED_HEADER_DROP = frozenset({"content-encoding", "content-length"})


class FingerprintBackendUnavailable(RuntimeError):
    """The configured impersonation backend cannot be constructed."""


def backend_available() -> bool:
    return _AsyncSession is not None


def backend_error() -> str | None:
    if _IMPORT_ERROR is None:
        return None
    return f"{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"


def validate_profile(profile: str) -> str:
    value = str(profile or "").strip()
    if not _PROFILE_RE.fullmatch(value):
        raise ValueError("TLS fingerprint profile must be 1-64 letters, digits, '_' or '-'")
    return value


def supports_route(route_type: str) -> bool:
    """Return whether curl can preserve this Parrot route's exit semantics."""
    return str(route_type or "direct") in {"direct", "socks5", "ss2022"}


def normalize_proxy_url(proxy: str) -> str:
    """Keep Parrot's SOCKS5 remote-DNS behavior when handing a route to curl."""
    value = str(proxy or "")
    if value.lower().startswith("socks5://"):
        return "socks5h://" + value[len("socks5://"):]
    return value


def _timeout_number(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def curl_timeout_from_extensions(extensions: dict[str, Any] | None) -> tuple[float, float]:
    """Convert httpx's timeout extension dict to curl_cffi's supported tuple."""
    raw = (extensions or {}).get("timeout")
    if isinstance(raw, dict):
        connect = _timeout_number(raw.get("connect"), 10.0)
        read = _timeout_number(raw.get("read"), 330.0)
        return connect, read
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        return _timeout_number(raw[0], 10.0), _timeout_number(raw[1], 330.0)
    if isinstance(raw, (int, float)):
        value = _timeout_number(raw, 10.0)
        return value, value
    return 10.0, 330.0


async def _emit_trace(request: httpx.Request, name: str) -> None:
    callback = (request.extensions or {}).get("trace")
    if callback is None:
        return
    result = callback(name, {})
    if inspect.isawaitable(result):
        await result


def _mark_connection_handoff(request: httpx.Request) -> None:
    lifecycle = (request.extensions or {}).get("parrot_transport_lifecycle")
    callback = lifecycle.get("mark_connection_complete") if isinstance(lifecycle, dict) else None
    if callable(callback):
        callback()


def _curl_code(exc: BaseException) -> Any:
    return getattr(exc, "code", None)


def _isinstance_backend(exc: BaseException, name: str) -> bool:
    cls = getattr(_curl_errors, name, None) if _curl_errors is not None else None
    return isinstance(cls, type) and isinstance(exc, cls)


def _safe_before_dispatch(exc: BaseException) -> bool:
    """Only classify errors that libcurl guarantees happen before HTTP dispatch."""
    if _isinstance_backend(exc, "ImpersonateError"):
        return True
    code = _curl_code(exc)
    if _CurlECode is None:
        return False
    safe_names = (
        "UNSUPPORTED_PROTOCOL",
        "URL_MALFORMAT",
        "COULDNT_RESOLVE_PROXY",
        "COULDNT_RESOLVE_HOST",
        "COULDNT_CONNECT",
        "SSL_CONNECT_ERROR",
        "SSL_CERTPROBLEM",
        "SSL_CIPHER",
        "PEER_FAILED_VERIFICATION",
        "INTERFACE_FAILED",
    )
    return any(code == getattr(_CurlECode, name, object()) for name in safe_names)


def _mapped_backend_error(
    exc: BaseException, request: httpx.Request, *, phase: str,
) -> BaseException:
    """Normalize curl_cffi errors into the existing httpx error taxonomy."""
    if isinstance(exc, (asyncio.CancelledError, httpx.HTTPError)):
        return exc
    if _isinstance_backend(exc, "Timeout"):
        return httpx.TimeoutException(str(exc), request=request)
    if phase == "connect" and any(
        _isinstance_backend(exc, name)
        for name in ("ConnectionError", "ProxyError", "DNSError", "SSLError")
    ):
        return httpx.ConnectError(str(exc), request=request)
    if _isinstance_backend(exc, "RequestException"):
        error_type = httpx.ReadError if phase == "read" else httpx.ProtocolError
        return error_type(str(exc), request=request)
    return exc


def _decoded_response_headers(response: Any) -> httpx.Headers:
    try:
        items = list(response.headers.multi_items())
    except Exception:
        items = list(dict(response.headers).items())
    encoded = any(
        str(key).lower() == "content-encoding"
        and str(value).strip().lower() not in {"", "identity"}
        for key, value in items
    )
    if encoded:
        items = [
            (key, value) for key, value in items
            if str(key).lower() not in _DECODED_HEADER_DROP
        ]
    return httpx.Headers(items)


def _http_version_extension(response: Any) -> bytes | None:
    # libcurl values: 1=1.0, 2=1.1, 3=2.0, 30=3. Values are stable curl ABI.
    return {1: b"HTTP/1.0", 2: b"HTTP/1.1", 3: b"HTTP/2", 30: b"HTTP/3"}.get(
        int(getattr(response, "http_version", 0) or 0)
    )


class _CurlResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        response: Any,
        request: httpx.Request,
        *,
        byte_counter: Callable[[int, int], None] | None,
        close_owner: Callable[[], Any],
    ) -> None:
        self._response = response
        self._request = request
        self._byte_counter = byte_counter
        self._close_owner = close_owner
        self._closed = False

    async def __aiter__(self):
        try:
            async for chunk in self._response.aiter_content():
                if chunk:
                    if self._byte_counter is not None:
                        self._byte_counter(0, len(chunk))
                    yield bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mapped = _mapped_backend_error(exc, self._request, phase="read")
            if mapped is exc:
                raise
            raise mapped from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        quit_now = getattr(self._response, "quit_now", None)
        if quit_now is not None:
            try:
                quit_now.set()
            except Exception:
                pass
        # curl_cffi 0.16.3 Response.aclose() only waits for its stream task and
        # does not signal it to stop. Closing the one-request session owns and
        # cancels the underlying handle instead of waiting for a long SSE stream.
        result = self._close_owner()
        if inspect.isawaitable(result):
            await result
        task = getattr(self._response, "astream_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class ImpersonatedTransport(httpx.AsyncBaseTransport):
    """One-attempt curl_cffi transport with an httpx-compatible response stream."""

    def __init__(
        self,
        profile: str,
        *,
        proxy: str = "",
        proxy_auth: tuple[str, str] | None = None,
        trust_env: bool = False,
        byte_counter: Callable[[int, int], None] | None = None,
        route_owner: Any | None = None,
    ) -> None:
        if _AsyncSession is None:
            raise FingerprintBackendUnavailable(
                f"curl_cffi is unavailable ({backend_error() or 'unknown import error'})"
            )
        self.profile = validate_profile(profile)
        self.proxy = normalize_proxy_url(proxy)
        self.proxy_auth = proxy_auth
        self.trust_env = bool(trust_env)
        self.byte_counter = byte_counter
        self._route_owner = route_owner
        self._session: Any | None = None
        self._close_lock = asyncio.Lock()
        self._closed = False

    def _get_session(self) -> Any:
        if self._closed:
            raise httpx.TransportError("TLS fingerprint transport is closed")
        if self._session is None:
            kwargs: dict[str, Any] = {
                "impersonate": self.profile,
                "timeout": None,
                "allow_redirects": False,
                "trust_env": self.trust_env,
                "default_headers": False,
                "retry": 0,
                "max_clients": 1,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            if self.proxy_auth is not None:
                kwargs["proxy_auth"] = self.proxy_auth
            self._session = _AsyncSession(**kwargs)
        return self._session

    async def _close_session(self) -> None:
        """Close curl first, then the route resource that carries its socket."""
        async with self._close_lock:
            session, self._session = self._session, None
            route_owner, self._route_owner = self._route_owner, None
            self.proxy_auth = None
            cancellation: asyncio.CancelledError | None = None
            try:
                if session is not None:
                    await session.close()
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception as exc:
                logger.debug("curl_cffi session close failed (%s)", type(exc).__name__)
            finally:
                close = getattr(route_owner, "aclose", None)
                if callable(close):
                    try:
                        task = asyncio.ensure_future(close())
                        await asyncio.shield(task)
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                        await asyncio.gather(task, return_exceptions=True)
                    except Exception as exc:
                        logger.debug(
                            "curl_cffi route owner close failed (%s)", type(exc).__name__,
                        )
            if cancellation is not None:
                raise cancellation

    def _route_is_safe_before_dispatch(self) -> bool:
        callback = getattr(self._route_owner, "safe_before_dispatch", None)
        if not callable(callback):
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    async def _mark_dispatch(
        self, request: httpx.Request, content_length: int, state: dict[str, bool],
    ) -> None:
        if state["dispatched"]:
            return
        state["dispatched"] = True
        if self.byte_counter is not None and content_length:
            self.byte_counter(content_length, 0)
        # Only the dispatch-start signal is synthesized. Low-level curl phases
        # are intentionally left NULL rather than fabricated.
        await _emit_trace(request, "http2.send_request_headers.started")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        timeout = curl_timeout_from_extensions(dict(request.extensions or {}))
        dispatch_state = {"dispatched": False}
        session = self._get_session()

        # The high-level curl API has no upload-complete callback. Move the
        # business state to first-byte now, while retaining an exact libcurl
        # connect timeout from the converted tuple.
        _mark_connection_handoff(request)
        try:
            response = await session.request(
                request.method,
                str(request.url),
                headers=list(request.headers.multi_items()),
                content=content if content else None,
                stream=True,
                timeout=timeout,
                allow_redirects=False,
                accept_encoding="gzip, deflate, br",
                default_headers=False,
            )
            # Response headers are proof that an application request was sent.
            await self._mark_dispatch(request, len(content), dispatch_state)
        except asyncio.CancelledError:
            # The send state is unknowable after curl took ownership. Mark it as
            # dispatched before cancellation so upper layers never replay it.
            await self._mark_dispatch(request, len(content), dispatch_state)
            await asyncio.shield(self._close_session())
            raise
        except Exception as exc:
            if not (
                _safe_before_dispatch(exc)
                or self._route_is_safe_before_dispatch()
            ):
                await self._mark_dispatch(request, len(content), dispatch_state)
            mapped = _mapped_backend_error(exc, request, phase="connect")
            if mapped is exc:
                raise
            raise mapped from exc

        headers = _decoded_response_headers(response)
        extensions: dict[str, Any] = {}
        version = _http_version_extension(response)
        if version is not None:
            extensions["http_version"] = version
        reason = str(getattr(response, "reason", "") or "")
        if reason:
            extensions["reason_phrase"] = reason.encode("latin-1", "replace")
        return httpx.Response(
            int(response.status_code),
            headers=headers,
            stream=_CurlResponseStream(
                response,
                request,
                byte_counter=self.byte_counter,
                close_owner=self._close_session,
            ),
            request=request,
            extensions=extensions,
        )

    async def aclose(self) -> None:
        self._closed = True
        await self._close_session()


def create_impersonated_client(
    profile: str,
    *,
    timeout: httpx.Timeout,
    proxy: str = "",
    proxy_auth: tuple[str, str] | None = None,
    trust_env: bool = False,
    byte_counter: Callable[[int, int], None] | None = None,
    route_owner: Any | None = None,
) -> httpx.AsyncClient:
    """Build a short-lived client or fail closed when the enabled backend is absent."""
    transport = ImpersonatedTransport(
        profile,
        proxy=proxy,
        proxy_auth=proxy_auth,
        trust_env=trust_env,
        byte_counter=byte_counter,
        route_owner=route_owner,
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout, trust_env=False)
