"""Shared browser Origin decision for HTTP, cookie CSRF checks and WebSocket.

An Origin is allowed when it is in ``management.allowedOrigins`` or when its
``host[:port]`` equals the request's ``Host`` header (or the first value of
``X-Forwarded-Host``). Default ports are normalized using the Origin's scheme,
so ``http://example.test`` matches ``Host: example.test:80``.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _host_port(value: str, default_port: int | None) -> tuple[str, int | None] | None:
    value = (value or "").strip()
    if not value or any(ch in value for ch in "/@ \t"):
        return None
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return host.lower(), port if port is not None else default_port


def _origin_host_port(origin: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit(origin)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host or parsed.path not in {"", "/"}:
        return None
    return host.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def same_origin(origin: str, *, host: str | None, forwarded_host: str | None) -> bool:
    target = _origin_host_port(origin)
    if target is None:
        return False
    scheme = urlsplit(origin).scheme.lower()
    default_port = _DEFAULT_PORTS.get(scheme)
    candidates = [host]
    if forwarded_host:
        candidates.append(forwarded_host.split(",", 1)[0])
    for candidate in candidates:
        if candidate and _host_port(candidate, default_port) == target:
            return True
    return False


def origin_allowed(
    origin: str,
    *,
    allowed: Iterable[str],
    host: str | None,
    forwarded_host: str | None,
) -> bool:
    if origin in frozenset(allowed):
        return True
    return same_origin(origin, host=host, forwarded_host=forwarded_host)


def https_request(scheme: str | None, forwarded_proto: str | None) -> bool:
    if (scheme or "").lower() in {"https", "wss"}:
        return True
    first = (forwarded_proto or "").split(",", 1)[0].strip().lower()
    return first == "https"
