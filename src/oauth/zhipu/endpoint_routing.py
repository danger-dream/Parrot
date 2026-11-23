"""ZCode 3.19.1 ProviderEndpointRoutingService: server-driven model URL routing."""
from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from . import common as c

CONFIG_PATH = "/api/v1/agent/configs"
SUCCESS_TTL_S = 300.0
FAILURE_COOLDOWN_S = 30.0
REQUEST_TIMEOUT_S = 3.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_MAPPINGS = 256
AUTH_HEADER = "X-ZCode-Authorization"
ALLOWED_ULTRA_PATHS = ("/api/v1/ultra", "/api/v1/ultra-zai")

# Matches ZCode ySa: exact 11 identity headers forwarded to /api/v1/agent/configs
# (includes X-Device-Mid; intentionally excludes X-ZCode-Agent).
SOURCE_HEADER_MAP = {
    "http-referer": "HTTP-Referer",
    "user-agent": "User-Agent",
    "x-client-language": "X-Client-Language",
    "x-client-timezone": "X-Client-Timezone",
    "x-device-mid": "X-Device-Mid",
    "x-os-category": "X-Os-Category",
    "x-os-version": "X-Os-Version",
    "x-platform": "X-Platform",
    "x-release-channel": "X-Release-Channel",
    "x-title": "X-Title",
    "x-zcode-app-version": "X-ZCode-App-Version",
}


@dataclass(frozen=True)
class RouteDecision:
    routed: bool
    url: str
    headers: dict[str, str]


def _printable(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return None
    return text


def _normalized_path(path):
    if not path or path == "/":
        return "/"
    return path.rstrip("/") or "/"


def _default_port(scheme):
    return 443 if scheme == "https" else 80


def _normalized_origin(parsed):
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    if port is None or port == _default_port(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _endpoint_key(parsed):
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or _default_port(scheme)
    return f"{scheme}://{host}:{port}{_normalized_path(parsed.path)}"


def _normalize_endpoint_url(parsed):
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    netloc = host if port is None or port == _default_port(scheme) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, _normalized_path(parsed.path), "", ""))


def _parse_mapping_url(raw, label, *, allow_insecure=False):
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"agent_configs_invalid_{label}_url")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        raise ValueError(f"agent_configs_invalid_{label}_url") from None
    scheme = parsed.scheme.lower()
    allowed_schemes = ("https", "http") if allow_insecure else ("https",)
    if (scheme not in allowed_schemes or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError(f"agent_configs_unsafe_{label}_url")
    return parsed


def _is_allowed_identity_target(parsed, endpoint_origin):
    if _normalized_origin(parsed) != endpoint_origin:
        return False
    norm = _normalized_path(parsed.path)
    return any(norm == prefix or norm.startswith(prefix + "/") for prefix in ALLOWED_ULTRA_PATHS)


def _reject_json_constant(_value):
    raise ValueError("agent_configs_invalid_json")


def build_config_headers(app_version, source_headers=None, api_key_id=None):
    version = _printable(app_version)
    headers = {"User-Agent": f"ZCode/{version or 'unknown'}"}
    if version:
        headers["X-ZCode-App-Version"] = version
    for key, value in (source_headers or {}).items():
        if not isinstance(key, str):
            continue
        target = SOURCE_HEADER_MAP.get(key.lower())
        clean = _printable(value)
        if target and clean:
            headers[target] = clean
    clean_key = _printable(api_key_id)
    if clean_key:
        headers["x-api-key-id"] = clean_key
    headers["Accept"] = "application/json"
    return headers


def parse_routing_mapping(raw_bytes, endpoint_origin, *, allow_insecure=False):
    try:
        payload = json.loads(raw_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError):
        raise ValueError("agent_configs_invalid_json") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid_schema")
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        raise ValueError("invalid_schema")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("invalid_schema")
    if code != 0:
        raise ValueError("agent_configs_nonzero_code")
    if "proxyEndpoint" not in data:
        return {}
    proxy_endpoint = data["proxyEndpoint"]
    if not isinstance(proxy_endpoint, dict) or set(proxy_endpoint.keys()) != {"mapping"}:
        raise ValueError("invalid_schema")
    items = proxy_endpoint["mapping"]
    if not isinstance(items, list) or len(items) > MAX_MAPPINGS:
        raise ValueError("invalid_schema")

    normalized_origin = _normalized_origin(urlsplit(endpoint_origin))
    mapping = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("invalid_schema")
        keys = set(entry.keys())
        if not {"from", "to"}.issubset(keys) or not keys.issubset({"from", "to", "zcodeAuthorizationHeader"}):
            raise ValueError("invalid_schema")
        if not isinstance(entry["from"], str) or not isinstance(entry["to"], str):
            raise ValueError("invalid_schema")
        auth_header = entry.get("zcodeAuthorizationHeader")
        if auth_header is not None and not isinstance(auth_header, str):
            raise ValueError("invalid_schema")
        from_url = _parse_mapping_url(entry["from"], "from", allow_insecure=allow_insecure)
        to_url = _parse_mapping_url(entry["to"], "to", allow_insecure=allow_insecure)
        trimmed_auth = auth_header.strip() if auth_header is not None else ""
        include_auth = bool(trimmed_auth)
        if include_auth and (trimmed_auth.lower() != AUTH_HEADER.lower()
                             or not _is_allowed_identity_target(to_url, normalized_origin)):
            raise ValueError("agent_configs_unsafe_zcode_authorization_target")
        key = _endpoint_key(from_url)
        if key in mapping:
            raise ValueError("agent_configs_duplicate_from")
        mapping[key] = (include_auth, _normalize_endpoint_url(to_url))
    return mapping


def _format_zcode_authorization(token):
    clean = _printable(token)
    if not clean:
        return None
    if len(clean) >= 7 and clean[:6].lower() == "bearer" and clean[6].isspace():
        return clean
    return "Bearer " + clean


def apply_routed_headers(headers, routed_headers):
    """Apply Mpa semantics: drop host and overwrite any routed headers."""
    out = {k: v for k, v in headers.items() if k.lower() != "host"}
    for name, value in (routed_headers or {}).items():
        lower = name.lower()
        for existing in list(out):
            if existing.lower() == lower:
                out.pop(existing, None)
        out[name] = value
    return out


class EndpointRouter:
    def __init__(self, *, account_key, platform_origin, app_version, device_id="", api_key_id=None,
                 allow_insecure=False, success_ttl_s=SUCCESS_TTL_S, failure_cooldown_s=FAILURE_COOLDOWN_S,
                 request_timeout_s=REQUEST_TIMEOUT_S, max_response_bytes=MAX_RESPONSE_BYTES):
        parsed_origin = urlsplit(platform_origin)
        if parsed_origin.scheme.lower() not in ("https", "http") or not parsed_origin.hostname:
            raise ValueError("Provider endpoint routing config URL must use HTTP(S).")
        self.account_key = account_key
        self.endpoint_origin = _normalized_origin(parsed_origin)
        self.config_url = self.endpoint_origin + CONFIG_PATH
        self.app_version = _printable(app_version)
        self.device_id = _printable(device_id) or ""
        self.api_key_id = _printable(api_key_id)
        self.allow_insecure = allow_insecure
        self.success_ttl_s = success_ttl_s
        self.failure_cooldown_s = failure_cooldown_s
        self.request_timeout_s = request_timeout_s
        self.max_response_bytes = max_response_bytes
        self.lock = threading.RLock()
        self.snapshot = None  # (expires_at_monotonic, mapping_dict)
        self.retry_after = 0.0
        self.flight = None

    def _source_headers(self):
        headers = c.identity_headers()
        if self.device_id:
            headers["X-Device-Mid"] = self.device_id
        return headers

    async def _fetch_raw(self):
        from ... import network
        c.require_network()
        headers = build_config_headers(self.app_version, self._source_headers(), self.api_key_id)
        async with network.async_client(
            timeout=self.request_timeout_s,
            follow_redirects=False,
            proxy_purpose="oauth_zhipu",
            proxy_channel="oauth:" + self.account_key if self.account_key else "oauth:zhipu:login",
        ) as client:
            async with client.stream("GET", self.config_url, headers=headers) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"agent_configs_http_{response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.max_response_bytes:
                        raise ValueError("agent_configs_response_too_large")
                return bytes(raw)

    async def _refresh(self):
        try:
            raw = await self._fetch_raw()
            mapping = parse_routing_mapping(raw, self.endpoint_origin, allow_insecure=self.allow_insecure)
            with self.lock:
                self.snapshot = (time.monotonic() + self.success_ttl_s, mapping)
                self.retry_after = 0.0
        except Exception:
            with self.lock:
                self.retry_after = time.monotonic() + self.failure_cooldown_s

    async def ensure_fresh(self):
        with self.lock:
            now = time.monotonic()
            if (self.snapshot is not None and self.snapshot[0] > now) or self.retry_after > now:
                return
            future = self.flight
            leader = future is None
            if leader:
                future = concurrent.futures.Future()
                self.flight = future
        if not leader:
            await asyncio.shield(asyncio.wrap_future(future))
            return
        try:
            await self._refresh()
        finally:
            with self.lock:
                if self.flight is future:
                    self.flight = None
            if not future.done():
                future.set_result(None)

    async def resolve(self, url, *, zcode_token=""):
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError:
            return RouteDecision(False, url, {})
        allowed_schemes = ("https", "http") if self.allow_insecure else ("https",)
        if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
            return RouteDecision(False, url, {})
        await self.ensure_fresh()
        with self.lock:
            mapping = self.snapshot[1] if self.snapshot is not None else None
        entry = mapping.get(_endpoint_key(parsed)) if mapping else None
        if entry is None:
            return RouteDecision(False, url, {})
        include_auth, target_base = entry
        target_url = f"{target_base}?{parsed.query}" if parsed.query else target_base
        headers = {}
        if include_auth:
            auth_value = _format_zcode_authorization(zcode_token)
            if auth_value:
                headers[AUTH_HEADER] = auth_value
        return RouteDecision(True, target_url, headers)


_cache = {}
_cache_lock = threading.RLock()


def for_account(account_key, *, api_key_id=None, device_id="", network_revision=""):
    key = (c.PLATFORM_ORIGIN, c.VERSION, _printable(api_key_id) or "", _printable(device_id) or "", network_revision)
    with _cache_lock:
        cached = _cache.get(account_key)
        if cached and cached[0] == key:
            return cached[1]
        router = EndpointRouter(
            account_key=account_key,
            platform_origin=c.PLATFORM_ORIGIN,
            app_version=c.VERSION,
            device_id=device_id,
            api_key_id=api_key_id,
        )
        _cache[account_key] = (key, router)
        return router


def forget(account_key):
    with _cache_lock:
        _cache.pop(account_key, None)


def clear_all():
    with _cache_lock:
        _cache.clear()
