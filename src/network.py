"""Unified outbound networking for Parrot.

This module centralizes DNS and SOCKS5 proxy behavior for all outbound HTTP
requests. It intentionally avoids changing inbound server sockets.

Network policy:
- Configured DNS servers are used by a process-level socket.getaddrinfo patch.
- If SOCKS5 is enabled, HTTP clients use the SOCKS5 proxy. Target hostnames are
  sent to the proxy; only the proxy host itself is resolved locally (through the
  configured DNS patch when it is a hostname).
- DNS/proxy config changes take effect for new clients after reload callbacks
  clear caches and rebuild affected connection pools.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx

from . import config

try:  # optional until requirements are installed; failures are reported clearly.
    import dns.exception
    import dns.flags
    import dns.message
    import dns.query
    import dns.rcode
    import dns.resolver
    import dns.rdatatype
except Exception:  # pragma: no cover - exercised in environments without dnspython
    dns = None  # type: ignore[assignment]


_DEFAULT_TEST_URLS: tuple[tuple[str, str], ...] = (
    ("ChatGPT", "https://chatgpt.com/"),
    ("Claude/Anthropic", "https://api.anthropic.com/"),
    ("Telegram", "https://api.telegram.org/"),
    ("GitHub", "https://api.github.com/"),
)

_ORIG_GETADDRINFO = socket.getaddrinfo
_PATCHED = False
_HOOKED = False
_LOCK = threading.RLock()
_DNS_OVERRIDE = threading.local()
_CACHE: dict[tuple[str, int, int, int, int, tuple[str, ...]], tuple[float, list]] = {}
_LAST_SIGNATURE: tuple | None = None


@dataclass(frozen=True)
class NormalizedSocks5:
    url: str
    display_url: str
    host: str
    port: int


@dataclass(frozen=True)
class DnsServerSpec:
    raw: str
    kind: str          # "udp" | "dot" | "doh"
    host: str = ""
    port: int = 53
    url: str = ""
    tls_hostname: str = ""


def _network_cfg() -> dict:
    return config.get().get("network") or {}


def dns_cfg() -> dict:
    net = _network_cfg()
    return net.get("dns") or {}


def socks5_cfg() -> dict:
    net = _network_cfg()
    return net.get("socks5") or {}


def _resolve_server_host_system(host: str, *, family: int = socket.AF_UNSPEC) -> str:
    """Resolve DNS server host with the original system resolver to avoid recursion."""
    h = (host or "").strip().strip("[]")
    if _is_ip_literal(h):
        return h
    last_exc: Exception | None = None
    families = [family] if family not in (0, socket.AF_UNSPEC) else [socket.AF_INET, socket.AF_INET6]
    for fam in families:
        try:
            infos = _ORIG_GETADDRINFO(h, None, fam, socket.SOCK_STREAM, 0, 0)
            for item in infos:
                addr = item[4][0]
                if addr:
                    return addr
        except Exception as exc:
            last_exc = exc
    raise OSError(f"system resolve failed for DNS server {host}: {last_exc or 'no address'}")


def normalize_dns_server(raw: str) -> DnsServerSpec:
    """Normalize one DNS server entry.

    Supported:
    - 8.8.8.8 / 1.1.1.1 / dns.example.com[:53] → classic DNS (UDP/53)
    - dot://1.1.1.1:853?hostname=cloudflare-dns.com
    - https://dns.google/dns-query or doh://https://dns.google/dns-query

    If a DNS server host is a domain, that host itself is resolved by system DNS.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("DNS 地址不能为空")
    if s.startswith("doh://"):
        url = s[len("doh://"):]
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        p = urlparse(url)
        if p.scheme != "https" or not p.hostname:
            raise ValueError(f"非法 DoH 地址: {s}")
        return DnsServerSpec(raw=s, kind="doh", host=p.hostname, port=p.port or 443, url=url)
    p = urlparse(s if "://" in s else "")
    if p.scheme == "dot":
        if not p.hostname:
            raise ValueError(f"非法 DoT 地址: {s}")
        qs = parse_qs(p.query or "")
        hostname = (qs.get("hostname") or qs.get("sni") or [""])[0]
        return DnsServerSpec(raw=s, kind="dot", host=p.hostname, port=p.port or 853, tls_hostname=hostname or p.hostname)
    if p.scheme in ("http", "https"):
        if p.scheme != "https" or not p.hostname:
            raise ValueError(f"非法 DoH 地址: {s}")
        return DnsServerSpec(raw=s, kind="doh", host=p.hostname, port=p.port or 443, url=s)
    if "://" in s:
        raise ValueError(f"不支持的 DNS 地址格式: {s}")

    host = s
    port = 53
    if s.startswith("[") and "]" in s:
        end = s.index("]")
        host = s[1:end]
        rest = s[end + 1:]
        if rest.startswith(":"):
            port = int(rest[1:])
    elif s.count(":") == 1:
        maybe_host, maybe_port = s.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host
            port = int(maybe_port)
    # Bare IPv6 is fine; don't treat its colons as host:port.
    if port <= 0 or port > 65535:
        raise ValueError(f"DNS 端口范围应为 1-65535: {s}")
    host = host.strip()
    if not host:
        raise ValueError(f"非法 DNS 地址: {s}")
    return DnsServerSpec(raw=s, kind="udp", host=host, port=port)


def normalize_dns_servers(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw or []:
        s = str(item or "").strip()
        if not s:
            continue
        normalize_dns_server(s)
        if s not in out:
            out.append(s)
    if not out:
        raise ValueError("至少需要一个 DNS 地址")
    return out


def dns_servers() -> list[str]:
    override = getattr(_DNS_OVERRIDE, "servers", None)
    raw = override if override else (dns_cfg().get("servers") or ["8.8.8.8"])
    try:
        return normalize_dns_servers(raw)
    except Exception:
        return ["8.8.8.8"]


def dns_timeout() -> float:
    try:
        return max(0.2, float(dns_cfg().get("timeoutSeconds", 3) or 3))
    except (TypeError, ValueError):
        return 3.0


def dns_cache_ttl() -> int:
    try:
        return max(0, int(dns_cfg().get("cacheTtlSeconds", 300) or 0))
    except (TypeError, ValueError):
        return 300


def _signature() -> tuple:
    s5 = socks5_cfg()
    return (
        tuple(dns_servers()),
        dns_timeout(),
        dns_cache_ttl(),
        bool(s5.get("enabled", False)),
        str(s5.get("url") or ""),
    )


def clear_dns_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def dns_cache_entries() -> list[dict]:
    """Snapshot of current DNS cache. Returns list of:
    {host, family, servers, ips, expires_at_epoch, ttl_remaining_seconds}
    Expired entries are skipped (clean-on-read).
    """
    now = time.time()
    out: list[dict] = []
    with _LOCK:
        expired: list[tuple] = []
        for key, val in _CACHE.items():
            expires_at, ips = val
            if expires_at <= now:
                expired.append(key)
                continue
            host, family, _a, _b, _c, servers = key
            out.append({
                "host": host,
                "family": int(family or 0),
                "servers": list(servers),
                "ips": list(ips),
                "expires_at_epoch": float(expires_at),
                "ttl_remaining_seconds": max(0, int(expires_at - now)),
            })
        for k in expired:
            _CACHE.pop(k, None)
    out.sort(key=lambda x: x["host"])
    return out


def on_config_reload(_cfg: dict | None = None) -> None:
    global _LAST_SIGNATURE
    sig = _signature()
    with _LOCK:
        changed = sig != _LAST_SIGNATURE
        _LAST_SIGNATURE = sig
        if changed:
            _CACHE.clear()
    if not changed:
        return
    # Rebuild long-lived clients lazily/best-effort. Import lazily to avoid cycles.
    try:
        from .telegram import ui as tg_ui
        tg_ui.rebuild_session()
    except Exception as exc:
        print(f"[network] telegram session rebuild failed: {exc}")
    try:
        from . import upstream
        upstream.reset_client_sync()
    except Exception as exc:
        print(f"[network] upstream client reset failed: {exc}")


def init() -> None:
    """Install DNS patch and config reload hook. Idempotent."""
    global _PATCHED, _HOOKED, _LAST_SIGNATURE
    with _LOCK:
        if not _PATCHED:
            socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]
            _PATCHED = True
        _LAST_SIGNATURE = _signature()
        if not _HOOKED:
            config.on_reload(on_config_reload)
            _HOOKED = True


def _is_persistable_system_dns_server(raw: str) -> bool:
    """Return whether a system resolver is safe to persist as an upstream DNS.

    Loopback resolvers such as Docker's 127.0.0.11 or systemd-resolved's
    127.0.0.53 are local namespace entry points, not portable upstream DNS
    servers.  Unspecified addresses are invalid upstream targets as well.
    Private/LAN addresses remain valid because they may be intentional DNS
    servers supplied by the deployment network.
    """
    try:
        spec = normalize_dns_server(raw)
    except ValueError:
        return False
    try:
        addr = ipaddress.ip_address(spec.host.strip("[]"))
    except ValueError:
        # Preserve the existing leniency for hostname-based resolver entries.
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not (addr.is_loopback or addr.is_unspecified)


def _parse_resolv_conf(path: str = "/etc/resolv.conf") -> list[str]:
    out: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2 or parts[0] != "nameserver":
                    continue
                server = parts[1].strip()
                if not _is_persistable_system_dns_server(server):
                    continue
                if server not in out:
                    out.append(server)
    except OSError:
        pass
    return out


def bootstrap_system_dns_once() -> None:
    """Sync usable system DNS to config once without persisting local stubs."""
    dcfg = dns_cfg()
    if not bool(dcfg.get("bootstrapFromSystem", True)):
        return
    if bool(dcfg.get("bootstrapped", False)):
        return
    servers = _parse_resolv_conf()

    if not servers:
        def _mark_attempted(c: dict) -> None:
            dns_obj = c.setdefault("network", {}).setdefault("dns", {})
            dns_obj["bootstrapped"] = True

        config.update(_mark_attempted, skip_if_unchanged=True)
        print("[network] skipped DNS bootstrap: no persistable system DNS server")
        return

    def _mut(c: dict) -> None:
        dns_obj = c.setdefault("network", {}).setdefault("dns", {})
        dns_obj["servers"] = servers
        dns_obj["bootstrapped"] = True

    config.update(_mut)
    print(f"[network] bootstrapped DNS: {', '.join(servers)}")


def sync_system_dns_now() -> list[str]:
    """Manually sync usable system DNS and mark bootstrapped."""
    servers = _parse_resolv_conf()
    if not servers:
        raise ValueError("系统 DNS 中没有可同步的上游地址，当前 DNS 未修改")

    def _mut(c: dict) -> None:
        dns_obj = c.setdefault("network", {}).setdefault("dns", {})
        dns_obj["servers"] = servers
        dns_obj["bootstrapped"] = True

    config.update(_mut)
    return servers


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _should_custom_resolve(host: Any) -> bool:
    if not isinstance(host, str):
        return False
    h = host.strip("[]")
    if not h or h == "localhost" or _is_ip_literal(h):
        return False
    return True


def _query_with_server(spec: DnsServerSpec, host: str, qtype: str, timeout: float):
    if dns is None:  # type: ignore[name-defined]
        raise RuntimeError("dnspython is required for custom DNS; install dnspython")
    query = dns.message.make_query(host, qtype)  # type: ignore[union-attr]
    if spec.kind == "udp":
        where = _resolve_server_host_system(spec.host)
        return dns.query.udp(query, where=where, port=spec.port, timeout=timeout)  # type: ignore[union-attr]
    if spec.kind == "dot":
        where = _resolve_server_host_system(spec.host)
        return dns.query.tls(query, where=where, port=spec.port, timeout=timeout, server_hostname=spec.tls_hostname or None)  # type: ignore[union-attr]
    if spec.kind == "doh":
        return dns.query.https(query, where=spec.url, timeout=timeout)  # type: ignore[union-attr]
    raise ValueError(f"unsupported DNS server kind: {spec.kind}")


def resolve_host(host: str, *, family: int = socket.AF_UNSPEC,
                 servers: Optional[list[str]] = None,
                 timeout: Optional[float] = None,
                 use_cache: bool = True) -> list[str]:
    """Resolve host through configured DNS servers. Returns IP strings."""
    if _is_ip_literal(host):
        return [host.strip("[]")]
    servers = servers or dns_servers()
    specs = [normalize_dns_server(s) for s in servers]
    timeout = dns_timeout() if timeout is None else timeout
    cache_ttl = dns_cache_ttl()
    qfamilies: list[tuple[int, str]] = []
    if family in (socket.AF_UNSPEC, 0):
        qfamilies = [(socket.AF_INET, "A"), (socket.AF_INET6, "AAAA")]
    elif family == socket.AF_INET:
        qfamilies = [(socket.AF_INET, "A")]
    elif family == socket.AF_INET6:
        qfamilies = [(socket.AF_INET6, "AAAA")]
    else:
        qfamilies = [(socket.AF_INET, "A"), (socket.AF_INET6, "AAAA")]

    key = (host.lower().strip("[]"), int(family or 0), 0, 0, 0, tuple(servers))
    now = time.time()
    if use_cache and cache_ttl > 0:
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and cached[0] > now:
                return list(cached[1])

    ips: list[str] = []
    errors: list[str] = []
    for spec in specs:
        for _fam, qtype in qfamilies:
            try:
                response = _query_with_server(spec, host, qtype, timeout)
                rcode = response.rcode()
                if rcode != dns.rcode.NOERROR:  # type: ignore[union-attr]
                    errors.append(f"{spec.raw} {qtype}: rcode={dns.rcode.to_text(rcode)}")  # type: ignore[union-attr]
                    continue
                rdtype = dns.rdatatype.from_text(qtype)  # type: ignore[union-attr]
                for rrset in response.answer:
                    if rrset.rdtype != rdtype:
                        continue
                    for ans in rrset:
                        ip = ans.to_text().strip()
                        if ip and ip not in ips:
                            ips.append(ip)
            except Exception as exc:
                errors.append(f"{spec.raw} {qtype}: {exc}")
        if ips:
            break
    if not ips:
        raise OSError(f"DNS resolve failed for {host}: {'; '.join(errors) or 'no answers'}")
    if use_cache and cache_ttl > 0:
        with _LOCK:
            _CACHE[key] = (now + cache_ttl, list(ips))
    return ips


def _patched_getaddrinfo(host: Any, port: Any, family: int = 0, type: int = 0,
                         proto: int = 0, flags: int = 0):
    if not _should_custom_resolve(host):
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    try:
        ips = resolve_host(str(host), family=family or socket.AF_UNSPEC)
    except Exception as exc:
        raise socket.gaierror(str(exc)) from exc

    results = []
    seen = set()
    for ip in ips:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC, fam):
            continue
        for item in _ORIG_GETADDRINFO(ip, port, fam, type, proto, flags):
            k = (item[0], item[1], item[2], item[4])
            if k in seen:
                continue
            seen.add(k)
            results.append(item)
    if results:
        return results
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


def normalize_socks5_url(raw: str) -> NormalizedSocks5:
    s = (raw or "").strip()
    if not s:
        raise ValueError("SOCKS5 地址不能为空")
    if "://" not in s:
        s = "socks5://" + s
    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    if scheme == "tcp":
        scheme = "socks5"
    if scheme == "socks5h":
        # User intent in Parrot: enabling SOCKS5 means target domains go through
        # the proxy. HTTPX's socks5 path already sends remote hostnames to the
        # SOCKS server; keep one canonical scheme for simpler config.
        scheme = "socks5"
    if scheme != "socks5":
        raise ValueError("仅支持 SOCKS5 地址（socks5://、tcp:// 或 host:port）")
    if not parsed.hostname:
        raise ValueError("SOCKS5 地址缺少主机")
    if parsed.port is None:
        raise ValueError("SOCKS5 地址缺少端口")
    if parsed.port <= 0 or parsed.port > 65535:
        raise ValueError("SOCKS5 端口范围应为 1-65535")
    normalized = urlunparse((scheme, parsed.netloc, parsed.path or "", "", parsed.query or "", ""))
    if parsed.params or parsed.fragment:
        normalized = urlunparse((scheme, parsed.netloc, parsed.path or "", "", parsed.query or "", ""))
    return NormalizedSocks5(
        url=normalized,
        display_url=mask_url(normalized),
        host=parsed.hostname,
        port=int(parsed.port),
    )


def mask_url(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.username and not p.password:
            return url
        host = p.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if p.port:
            netloc += f":{p.port}"
        if p.username:
            netloc = f"{p.username}:***@{netloc}"
        return urlunparse((p.scheme, netloc, p.path or "", "", p.query or "", ""))
    except Exception:
        return url


def active_socks5_url() -> Optional[str]:
    s5 = socks5_cfg()
    if not bool(s5.get("enabled", False)):
        return None
    raw = str(s5.get("url") or "").strip()
    if not raw:
        return None
    return normalize_socks5_url(raw).url


def _configured_proxy_chain_or_none(*, proxy_purpose: str, proxy_channel: str, proxy_model: str):
    """Return an explicit new-proxy chain, or ``None`` for the legacy/default path.

    A configured non-direct rule must never disappear into a direct/legacy client
    simply because its connector failed to parse.  ``directFallback`` is the sole
    opt-in for adding direct to such a chain.
    """
    from .proxy import manager as pm
    from .proxy.connector import ProxyConnectError

    configured = False
    try:
        pm.init()
        configured = pm.is_configured()
        if not configured:
            if pm.has_non_direct_routing_rules():
                raise ProxyConnectError("configured non-direct proxy route could not be initialized")
            return None

        source_chain = pm.resolve_proxy_chain(
            channel_key=proxy_channel, model=proxy_model, purpose=proxy_purpose,
        )
        chain = []
        for name in source_chain:
            connector = pm.get_connector(name)
            if connector is not None:
                chain.append((name, connector))
        if chain:
            if pm.direct_fallback_enabled() and not any(name == "direct" for name, _ in chain):
                direct = pm.get_connector("direct")
                if direct is not None:
                    chain.append(("direct", direct))
            return chain
        if pm.direct_fallback_enabled():
            direct = pm.get_connector("direct")
            if direct is not None:
                print(f"[proxy] network route has no usable target; using enabled direct fallback: {source_chain}")
                return [("direct", direct)]
        raise ProxyConnectError(f"proxy route has no valid target: {source_chain}")
    except ProxyConnectError:
        raise
    except Exception as exc:
        if pm.direct_fallback_enabled():
            direct = pm.get_connector("direct")
            if direct is not None:
                print(f"[proxy] network route resolution failed; using enabled direct fallback: {exc}")
                return [("direct", direct)]
        if configured or pm.has_non_direct_routing_rules():
            raise ProxyConnectError(f"proxy route resolution failed: {exc}") from exc
        return None


def async_client(*, timeout: Any = None, limits: httpx.Limits | None = None,
                 http2: bool = False,
                 proxy_purpose: str = "",
                 proxy_channel: str = "",
                 proxy_model: str = "",
                 byte_counter=None,
                 **kwargs) -> httpx.AsyncClient:
    """Create an async HTTP client, optionally routing through a proxy.

    If the new proxy subsystem is configured, ``proxy_purpose`` /
    ``proxy_channel`` / ``proxy_model`` are used to resolve a proxy via
    ``proxy.manager``.  Falls back to legacy ``socks5`` config.
    """
    chain = _configured_proxy_chain_or_none(
        proxy_purpose=proxy_purpose,
        proxy_channel=proxy_channel,
        proxy_model=proxy_model,
    )
    if chain is not None:
        from .proxy.connector import ProxyConnectError
        last_exc: Exception | None = None
        for pname, conn in chain:
            try:
                return conn.create_httpx_client(
                    timeout=timeout, limits=limits, http2=http2,
                    byte_counter=byte_counter, **kwargs,
                )
            except Exception as exc:
                last_exc = exc
                print(f"[proxy] async client construction failed for {pname}: {exc}")
        raise ProxyConnectError(
            f"configured proxy route has no usable async client: {last_exc or 'unknown error'}"
        )

    # No configured new-proxy route: preserve legacy SOCKS5/default behavior.
    opts = dict(kwargs)
    if timeout is not None:
        opts["timeout"] = timeout
    if limits is not None:
        opts["limits"] = limits
    opts.setdefault("http2", http2)
    proxy = active_socks5_url()
    if proxy:
        opts["proxy"] = proxy
        opts["trust_env"] = False
    return httpx.AsyncClient(**opts)


def sync_client(*, timeout: Any = None, limits: httpx.Limits | None = None,
                http2: bool = False,
                proxy_purpose: str = "",
                proxy_channel: str = "",
                proxy_model: str = "",
                **kwargs) -> httpx.Client:
    """Create a sync HTTP client.

    Sync callers support direct and SOCKS5. SS2022 is async-only; if a route
    resolves to SS2022 we keep scanning the target chain for a sync-capable
    fallback, then fall back to legacy socks5.
    """
    opts = dict(kwargs)
    if timeout is not None:
        opts["timeout"] = timeout
    if limits is not None:
        opts["limits"] = limits
    opts.setdefault("http2", http2)

    chain = _configured_proxy_chain_or_none(
        proxy_purpose=proxy_purpose,
        proxy_channel=proxy_channel,
        proxy_model=proxy_model,
    )
    if chain is not None:
        from .proxy.connector import ProxyConnectError
        unsupported: list[str] = []
        for pname, conn in chain:
            if conn.type == "direct":
                opts.setdefault("trust_env", False)
                return httpx.Client(**opts)
            if conn.type == "socks5":
                opts["proxy"] = conn.url
                opts["trust_env"] = False
                return httpx.Client(**opts)
            unsupported.append(str(pname))
        raise ProxyConnectError(
            "configured proxy route has no sync-compatible target"
            + (f": {', '.join(unsupported)}" if unsupported else "")
        )

    proxy = active_socks5_url()
    if proxy:
        opts["proxy"] = proxy
        opts["trust_env"] = False
    return httpx.Client(**opts)


def get_sync(url: str, **kwargs) -> httpx.Response:
    timeout = kwargs.pop("timeout", None)
    proxy_purpose = kwargs.pop("proxy_purpose", "")
    proxy_channel = kwargs.pop("proxy_channel", "")
    proxy_model = kwargs.pop("proxy_model", "")
    with sync_client(timeout=timeout, proxy_purpose=proxy_purpose,
                     proxy_channel=proxy_channel, proxy_model=proxy_model) as client:
        return client.get(url, **kwargs)


def post_sync(url: str, **kwargs) -> httpx.Response:
    timeout = kwargs.pop("timeout", None)
    proxy_purpose = kwargs.pop("proxy_purpose", "")
    proxy_channel = kwargs.pop("proxy_channel", "")
    proxy_model = kwargs.pop("proxy_model", "")
    with sync_client(timeout=timeout, proxy_purpose=proxy_purpose,
                     proxy_channel=proxy_channel, proxy_model=proxy_model) as client:
        return client.post(url, **kwargs)


def get_json_sync(url: str, *, timeout: float = 15, headers: Optional[dict] = None) -> Any:
    resp = get_sync(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.json()


def save_dns_servers(servers: list[str]) -> None:
    clean = normalize_dns_servers(servers)

    def _mut(c: dict) -> None:
        dns_obj = c.setdefault("network", {}).setdefault("dns", {})
        dns_obj["servers"] = clean
        dns_obj["bootstrapped"] = True

    config.update(_mut)


def save_socks5(url: str, *, enabled: bool = True) -> str:
    norm = normalize_socks5_url(url)

    def _mut(c: dict) -> None:
        s5 = c.setdefault("network", {}).setdefault("socks5", {})
        s5["url"] = norm.url
        s5["enabled"] = bool(enabled)

    config.update(_mut)
    return norm.url


def set_socks5_enabled(enabled: bool) -> None:
    def _mut(c: dict) -> None:
        c.setdefault("network", {}).setdefault("socks5", {})["enabled"] = bool(enabled)
    config.update(_mut)


def parse_dns_input(text: str) -> list[str]:
    raw = (text or "").replace("，", ",").replace(";", ",").replace("；", ",")
    parts: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Also allow whitespace-separated input.
        parts.extend([p.strip() for p in chunk.split() if p.strip()])
    return normalize_dns_servers(parts)


def _host_from_url(url: str) -> str:
    p = urlparse(url)
    if not p.hostname:
        raise ValueError(f"URL 缺少 host: {url}")
    return p.hostname


def _status_line(ok: bool, label: str, detail: str = "") -> str:
    if ok:
        return f"{label} ... ✅"
    return f"{label} ... ❌ {detail}".rstrip()


def test_dns_servers(servers: list[str], *, timeout: float | None = None) -> dict:
    """Test DNS resolve and direct HTTPS reachability with candidate DNS.

    Returns a JSON-serializable dict for storing in TG state.
    """
    servers = normalize_dns_servers(servers)
    timeout = dns_timeout() if timeout is None else timeout
    results: list[dict] = []
    for label, url in _DEFAULT_TEST_URLS:
        host = _host_from_url(url)
        item = {"label": label, "url": url, "host": host, "resolve": False, "http": False, "error": ""}
        try:
            ips = resolve_host(host, family=socket.AF_UNSPEC, servers=servers, timeout=timeout, use_cache=False)
            item["resolve"] = True
            item["ips"] = ips[:4]
        except Exception as exc:
            item["error"] = f"DNS: {str(exc)[:160]}"
            results.append(item)
            continue
        # Direct HTTP test under temporary DNS patch behavior. Since getaddrinfo
        # reads config, use a one-off monkey inside resolve path is overkill here;
        # verifying resolution is the critical DNS step. For reachability, use the
        # current stack with candidate result already validated by DNS.
        try:
            # HEAD is not universally allowed; GET with tiny read timeout is safer.
            _DNS_OVERRIDE.servers = list(servers)
            with httpx.Client(timeout=httpx.Timeout(connect=timeout, read=5.0, write=5.0, pool=timeout), http2=False, follow_redirects=False, trust_env=False) as client:
                resp = client.get(url, headers={"User-Agent": "parrot-network-test/0.1"})
                item["http"] = resp.status_code < 500 or resp.status_code in (401, 403, 404)
                if not item["http"]:
                    item["error"] = f"HTTP {resp.status_code}"
        except Exception as exc:
            item["error"] = f"HTTP: {str(exc)[:160]}"
        finally:
            try:
                delattr(_DNS_OVERRIDE, "servers")
            except AttributeError:
                pass
        results.append(item)
    ok = all(bool(r.get("resolve")) for r in results)
    return {"ok": ok, "servers": servers, "results": results}


async def test_socks5(url: str, *, timeout: float | None = None) -> dict:
    norm = normalize_socks5_url(url)
    timeout = dns_timeout() if timeout is None else timeout
    results: list[dict] = []
    proxy_resolve = {"label": "SOCKS5 地址解析", "host": norm.host, "ok": True, "error": ""}
    if not _is_ip_literal(norm.host) and norm.host != "localhost":
        try:
            proxy_resolve["ips"] = resolve_host(norm.host, timeout=timeout, use_cache=False)[:4]
        except Exception as exc:
            proxy_resolve["ok"] = False
            proxy_resolve["error"] = str(exc)[:160]
    results.append(proxy_resolve)

    if proxy_resolve["ok"]:
        for label, url2 in _DEFAULT_TEST_URLS:
            item = {"label": label, "url": url2, "ok": False, "error": ""}
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=timeout, read=8.0, write=5.0, pool=timeout),
                    proxy=norm.url,
                    trust_env=False,
                    http2=False,
                    follow_redirects=False,
                ) as client:
                    resp = await client.get(url2, headers={"User-Agent": "parrot-network-test/0.1"})
                    item["ok"] = resp.status_code < 500 or resp.status_code in (401, 403, 404)
                    if not item["ok"]:
                        item["error"] = f"HTTP {resp.status_code}"
            except Exception as exc:
                item["error"] = str(exc)[:180]
            results.append(item)
    ok = all(bool(r.get("ok")) for r in results)
    return {"ok": ok, "url": norm.url, "display_url": norm.display_url, "results": results}


def dns_test_text(test: dict) -> str:
    lines = ["正在检测 DNS 网络访问情况：", ""]
    for r in test.get("results") or []:
        label = f"{r.get('host') or r.get('label')}"
        ok = bool(r.get("resolve"))
        detail = "" if ok else str(r.get("error") or "解析失败")
        lines.append(_status_line(ok, label, detail))
    failed = [r for r in test.get("results") or [] if not r.get("resolve")]
    if failed:
        lines += ["", "⚠️ 检测到部分域名解析失败，保存后相关功能可能异常。"]
    else:
        lines += ["", "✅ DNS 解析检测通过。"]
    return "\n".join(lines)


def socks5_test_text(test: dict) -> str:
    lines = ["正在检测 SOCKS5 网络访问情况：", ""]
    for r in test.get("results") or []:
        label = str(r.get("label") or r.get("url") or r.get("host") or "test")
        ok = bool(r.get("ok"))
        detail = "" if ok else str(r.get("error") or "失败")
        lines.append(_status_line(ok, label, detail))
    failed = [r for r in test.get("results") or [] if not r.get("ok")]
    if failed:
        lines += ["", "⚠️ 检测到部分网络经 SOCKS5 访问失败，保存后相关功能可能异常。"]
    else:
        lines += ["", "✅ SOCKS5 检测通过。"]
    return "\n".join(lines)


def failure_warning(kind: str, test: dict) -> str:
    if kind == "dns":
        failed = [r for r in test.get("results") or [] if not r.get("resolve")]
        if not failed:
            return ""
        lines = ["⚠️ DNS 已强制保存，但以下域名解析失败，相关功能可能不可用：", ""]
        for r in failed:
            lines.append(f"• {r.get('host')}: {r.get('error') or '解析失败'}")
        return "\n".join(lines)
    failed = [r for r in test.get("results") or [] if not r.get("ok")]
    if not failed:
        return ""
    lines = ["⚠️ SOCKS5 已强制保存/启用，但以下检测失败，相关功能可能不可用：", ""]
    for r in failed:
        lines.append(f"• {r.get('label') or r.get('url') or r.get('host')}: {r.get('error') or '访问失败'}")
    return "\n".join(lines)


def dumps_state(obj: dict) -> dict:
    """Keep TG state compact and JSON-safe."""
    return json.loads(json.dumps(obj, ensure_ascii=False))
