"""Proxy manager: connectors, groups, routing, failover.

Singleton accessed via module-level functions.  Config is the source of truth;
runtime state (stats, cached connectors) is rebuilt on reload.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

import httpx

from .. import config
from .connector import (
    Connector, DirectConnector, ProxyConnectError, UpstreamConnectError,
    connector_from_config, parse_proxy_url,
)

# ── State ────────────────────────────────────────────────────────

_lock = threading.RLock()
_connectors: dict[str, Connector] = {}  # name → Connector
_groups: dict[str, list[str]] = {}       # group_name → [proxy_name, ...]
_routing: dict = {}                      # routing config subtree
_initialized = False

# A special "direct" connector always available
_DIRECT = DirectConnector()


# ── Init / Reload ────────────────────────────────────────────────

def init() -> None:
    """Build connectors + groups + routing from config. Idempotent."""
    global _initialized
    _reload_from_config()
    if not _initialized:
        config.on_reload(_on_config_reload)
        _initialized = True


def _on_config_reload(_cfg=None) -> None:
    _reload_from_config()


def _reload_from_config() -> None:
    cfg = config.get()
    net = cfg.get("network") or {}

    with _lock:
        # ── proxies ──
        old_stats = {n: c.stats for n, c in _connectors.items()}
        _connectors.clear()
        raw_proxies = net.get("proxies") or {}
        for name, pcfg in raw_proxies.items():
            if name == "direct":
                continue  # reserved
            try:
                c = connector_from_config(name, pcfg)
                # Restore stats if connector was known
                if name in old_stats:
                    c.stats = old_stats[name]
                _connectors[name] = c
            except Exception as e:
                print(f"[proxy] failed to build connector '{name}': {e}")

        # ── groups ──
        _groups.clear()
        raw_groups = net.get("groups") or {}
        for gname, members in raw_groups.items():
            if isinstance(members, list):
                _groups[gname] = list(members)

        # ── routing ──
        _routing.clear()
        _routing.update(net.get("routing") or {})


# ── Lookups ──────────────────────────────────────────────────────

def get_connector(name: str) -> Optional[Connector]:
    """Get a connector by name. Returns None if not found."""
    if name == "direct":
        return _DIRECT
    with _lock:
        return _connectors.get(name)


def get_group(name: str) -> Optional[list[str]]:
    with _lock:
        return list(_groups.get(name, []))


def all_connectors() -> dict[str, Connector]:
    with _lock:
        d = dict(_connectors)
    d["direct"] = _DIRECT
    return d


def all_groups() -> dict[str, list[str]]:
    with _lock:
        return dict(_groups)


def get_routing() -> dict:
    with _lock:
        return dict(_routing)


def is_configured() -> bool:
    """Whether the new proxy subsystem has any user-visible configuration.

    DEFAULT_CONFIG always contains routing.default=direct after deep merge, so
    callers must not treat that default as an explicit opt-in. This preserves
    legacy network.socks5 behavior until a proxy/group or a non-default route is
    actually configured.
    """
    with _lock:
        if _connectors or _groups:
            return True
        r = dict(_routing)
    return bool(r and r != {"default": "direct"})


# ── Route resolution ─────────────────────────────────────────────

def resolve_proxy_target(*, channel_key: str = "", model: str = "",
                         purpose: str = "", account_key: str = "") -> str | list[str]:
    """Resolve which proxy/group to use for a given context.

    Priority: account = channel > model > purpose/family > default

    Account and channel routes are the same highest tier.  In the rare case
    both are passed and both exist, account_key wins as a deterministic tie
    breaker because it is the more specific OAuth-account identifier.

    Returns:
      - A proxy name (str)  → single proxy
      - A group name (str) that maps to a list → failover group
      - "direct"
    """
    with _lock:
        r = dict(_routing)

    # 1. Account/channel-level override (same priority; account wins ties)
    acct_routes = r.get("accounts") or {}
    acct_key = account_key or channel_key
    if acct_key and acct_key in acct_routes:
        return acct_routes[acct_key]

    # 1b. Channel-level override
    ch_routes = r.get("channels") or {}
    if channel_key and channel_key in ch_routes:
        return ch_routes[channel_key]

    # 2. Model-level override
    m_routes = r.get("models") or {}
    if model and model in m_routes:
        return m_routes[model]

    # 3. Purpose/family-level override (telegram, oauth_anthropic, oauth_openai, etc.)
    if purpose and purpose in r:
        return r[purpose]

    # Backward-compatible alias used by the first draft of the UI.
    if purpose.startswith("oauth_") and "oauth" in r:
        return r["oauth"]

    # 4. Default
    return r.get("default", "direct")


def _expand_target(target: str | list[str]) -> list[str]:
    """Expand a target (proxy name or group name) into a list of proxy names."""
    if isinstance(target, list):
        return list(target) or ["direct"]
    if not isinstance(target, str) or not target:
        return ["direct"]
    g = get_group(target)
    if g:
        return g
    # Single proxy
    return [target]


def expand_target(target: str | list[str]) -> list[str]:
    """Public wrapper for target expansion."""
    return _expand_target(target)


def resolve_proxy_chain(*, channel_key: str = "", model: str = "",
                        purpose: str = "", account_key: str = "") -> list[str]:
    """Resolve and expand a routing target into an ordered proxy chain."""
    return _expand_target(resolve_proxy_target(
        channel_key=channel_key,
        model=model,
        purpose=purpose,
        account_key=account_key,
    ))


def target_supports_sync(target: str | list[str]) -> bool:
    """Return True if a target can be used by sync httpx callers.

    Sync callers currently support direct and SOCKS5. SS2022 is implemented as
    an async httpcore backend and is only available for model/upstream requests.
    """
    for name in _expand_target(target):
        conn = get_connector(name)
        if conn is None:
            continue
        if conn.type in ("direct", "socks5"):
            return True
    return False


# ── Failover client creation ─────────────────────────────────────

async def create_client_with_failover(
    *,
    channel_key: str = "",
    model: str = "",
    purpose: str = "",
    account_key: str = "",
    timeout: httpx.Timeout | None = None,
    limits: httpx.Limits | None = None,
    http2: bool = False,
    byte_counter: Callable[[int, int], None] | None = None,
) -> tuple[httpx.AsyncClient, str]:
    """Create an httpx.AsyncClient using the resolved proxy with failover.

    Returns (client, proxy_name_used).
    Raises ProxyConnectError if all proxies fail.
    """
    target = resolve_proxy_target(
        channel_key=channel_key, model=model, purpose=purpose, account_key=account_key,
    )
    chain = _expand_target(target)

    last_err = None
    for pname in chain:
        conn = get_connector(pname)
        if conn is None:
            continue
        try:
            client = conn.create_httpx_client(
                timeout=timeout, limits=limits, http2=http2,
                byte_counter=byte_counter)
            return client, pname
        except Exception as e:
            last_err = e
            conn.stats.total_failures += 1
            conn.stats.last_error = str(e)[:200]
            print(f"[proxy] {pname} failed: {e}, trying next...")
            continue

    raise ProxyConnectError(f"all proxies failed, last error: {last_err}")


async def test_proxy(name: str, *, timeout: float = 8.0) -> dict:
    """Test a single proxy's connectivity."""
    conn = get_connector(name)
    if conn is None:
        return {"ok": False, "error": f"proxy '{name}' not found"}
    return await conn.test_connectivity(timeout=timeout)


async def test_group(group_name: str, *, timeout: float = 8.0) -> list[dict]:
    """Test all proxies in a group. Returns list of results."""
    members = get_group(group_name)
    if not members:
        return [{"name": group_name, "ok": False, "error": "group not found"}]
    results = []
    for pname in members:
        r = await test_proxy(pname, timeout=timeout)
        r["name"] = pname
        results.append(r)
    return results


# ── Config mutation helpers (called from TG UI) ──────────────────

def add_proxy(name: str, proxy_cfg: dict) -> None:
    """Add or update a proxy in config."""
    def _mut(c):
        net = c.setdefault("network", {})
        proxies = net.setdefault("proxies", {})
        proxies[name] = proxy_cfg
    config.update(_mut)


def remove_proxy(name: str) -> None:
    """Remove a proxy from config (also removes from groups)."""
    def _mut(c):
        net = c.setdefault("network", {})
        proxies = net.get("proxies") or {}
        proxies.pop(name, None)
        # Remove from all groups
        for members in (net.get("groups") or {}).values():
            if isinstance(members, list):
                while name in members:
                    members.remove(name)
        # Remove from routing
        routing = net.get("routing") or {}
        for k, v in list(routing.items()):
            if v == name:
                del routing[k]
            elif isinstance(v, dict):
                for rk, rv in list(v.items()):
                    if rv == name:
                        del v[rk]
    config.update(_mut)


def add_group(name: str, members: list[str]) -> None:
    def _mut(c):
        net = c.setdefault("network", {})
        groups = net.setdefault("groups", {})
        groups[name] = members
    config.update(_mut)


def remove_group(name: str) -> None:
    def _mut(c):
        net = c.setdefault("network", {})
        groups = net.get("groups") or {}
        groups.pop(name, None)
        # Remove references in routing
        routing = net.get("routing") or {}
        for k, v in list(routing.items()):
            if v == name:
                del routing[k]
            elif isinstance(v, dict):
                for rk, rv in list(v.items()):
                    if rv == name:
                        del v[rk]
    config.update(_mut)


def update_group_members(name: str, members: list[str]) -> None:
    def _mut(c):
        net = c.setdefault("network", {})
        groups = net.setdefault("groups", {})
        groups[name] = members
    config.update(_mut)


def set_routing(key: str, value: str, *, section: str = "") -> None:
    """Set a routing rule.

    section="" → top-level (default, telegram, oauth)
    section="models" or "channels" → nested dict
    """
    def _mut(c):
        net = c.setdefault("network", {})
        routing = net.setdefault("routing", {})
        if section:
            sub = routing.setdefault(section, {})
            sub[key] = value
        else:
            routing[key] = value
    config.update(_mut)


def remove_routing(key: str, *, section: str = "") -> None:
    def _mut(c):
        net = c.setdefault("network", {})
        routing = net.get("routing") or {}
        if section:
            sub = routing.get(section)
            if isinstance(sub, dict):
                sub.pop(key, None)
        else:
            routing.pop(key, None)
    config.update(_mut)


# ── Migration: old socks5 config → new proxy system ─────────────

def migrate_legacy_socks5() -> bool:
    """If old-style network.socks5 is configured, migrate to new proxy system.

    Returns True if migration happened.
    """
    cfg = config.get()
    net = cfg.get("network") or {}
    s5 = net.get("socks5") or {}

    # Already migrated?
    if net.get("proxies"):
        return False

    url = str(s5.get("url") or "").strip()
    enabled = bool(s5.get("enabled")) and bool(url)
    if not url:
        return False

    def _mut(c):
        net_c = c.setdefault("network", {})
        proxies = net_c.setdefault("proxies", {})
        proxies["socks5"] = {"type": "socks5", "url": url}
        groups = net_c.setdefault("groups", {})
        groups["default"] = ["socks5", "direct"]
        routing = net_c.setdefault("routing", {})
        if enabled:
            routing["default"] = "default"
        else:
            routing["default"] = "direct"
        # Keep old config for backward compat but mark migrated
        net_c.setdefault("_socks5_migrated", True)

    config.update(_mut)
    print("[proxy] migrated legacy socks5 config to new proxy system")
    return True
