"""Proxy manager: connectors, groups, routing, failover.

Singleton accessed via module-level functions.  Config is the source of truth;
runtime state (stats, cached connectors) is rebuilt on reload.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Optional

import httpx

from .. import config
from .routing_types import legacy_purpose, normalize_provider, provider_for_context
from .connector import (
    Connector, DirectConnector, ProxyConnectError, UpstreamConnectError,
    connector_from_config, parse_proxy_url,
)

# ── State ────────────────────────────────────────────────────────

_lock = threading.RLock()
_initialized = False
_callback_registered = False
_config_generation: object | None = None

# A special "direct" connector always available
_DIRECT = DirectConnector()


@dataclass(frozen=True)
class _ProxySnapshot:
    """One atomically published, read-only proxy configuration generation."""

    connectors: Mapping[str, Connector]
    groups: Mapping[str, tuple[Any, ...]]
    routing: Mapping[str, Any]
    api_providers: Mapping[str, str]


def _freeze(value: Any) -> Any:
    """Recursively detach JSON-like config values and make them read-only."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _mutable_copy(value: Any) -> Any:
    """Return public routing/group values with their historical dict/list shape."""
    if isinstance(value, Mapping):
        return {key: _mutable_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_copy(item) for item in value]
    return value


_EMPTY_SNAPSHOT = _ProxySnapshot(
    connectors=MappingProxyType({}),
    groups=MappingProxyType({}),
    routing=MappingProxyType({}),
    api_providers=MappingProxyType({}),
)
_snapshot = _EMPTY_SNAPSHOT


# ── Init / Reload ────────────────────────────────────────────────

def init() -> None:
    """Poll config cheaply and build at most once for each loaded generation."""
    global _callback_registered, _initialized
    with _lock:
        if not _callback_registered:
            config.on_reload(_on_config_reload)
            _callback_registered = True
        # config.get() performs the baseline mtime poll.  Its callback may install
        # the new generation re-entrantly; the identity check below makes that and
        # stable repeated init() calls no-ops without rebuilding runtime objects.
        _install_config_locked(config.get())
        _initialized = True


def _on_config_reload(_cfg=None) -> None:
    # Always re-read the current object.  A callback from an older concurrent
    # reload must not publish its stale argument after a newer generation won.
    with _lock:
        _install_config_locked(config.get())


def _install_config_locked(cfg: dict) -> None:
    global _config_generation, _snapshot
    # config retains one object for the lifetime of a loaded generation and
    # publishes a replacement object on reload/update.
    if cfg is _config_generation:
        return
    new_snapshot = _build_snapshot(cfg, _snapshot)
    # Readers use this one pointer.  No partially cleared/repopulated mappings
    # are observable, even while a reload is building the next generation.
    _snapshot = new_snapshot
    _config_generation = cfg


def _build_snapshot(cfg: dict, previous: _ProxySnapshot) -> _ProxySnapshot:
    net = cfg.get("network") or {}

    old_stats = {name: connector.stats for name, connector in previous.connectors.items()}
    connectors: dict[str, Connector] = {}
    raw_proxies = net.get("proxies") or {}
    for name, pcfg in raw_proxies.items():
        if name == "direct":
            continue  # reserved
        try:
            connector = connector_from_config(name, pcfg)
            if name in old_stats:
                connector.stats = old_stats[name]
            connectors[name] = connector
        except Exception as exc:
            print(f"[proxy] failed to build connector '{name}': {exc}")

    groups: dict[str, tuple[Any, ...]] = {}
    raw_groups = net.get("groups") or {}
    for group_name, members in raw_groups.items():
        if isinstance(members, list):
            groups[group_name] = tuple(_freeze(item) for item in members)

    raw_routing = net.get("routing") or {}
    routing = _freeze(raw_routing if isinstance(raw_routing, dict) else {})
    return _ProxySnapshot(
        connectors=MappingProxyType(connectors),
        groups=MappingProxyType(groups),
        routing=routing,
        api_providers=MappingProxyType({
            f"api:{entry['name']}": normalize_provider(entry.get("providerId"))
            for entry in cfg.get("channels") or []
            if isinstance(entry, dict) and entry.get("name")
        }),
    )


# ── Lookups ──────────────────────────────────────────────────────

def get_connector(name: str) -> Optional[Connector]:
    """Get a connector by name. Returns None if not found."""
    if name == "direct":
        return _DIRECT
    with _lock:
        return _snapshot.connectors.get(name)


def get_group(name: str) -> Optional[list[str]]:
    with _lock:
        return _mutable_copy(_snapshot.groups.get(name, ()))


def all_connectors() -> dict[str, Connector]:
    with _lock:
        values = dict(_snapshot.connectors)
    values["direct"] = _DIRECT
    return values


def all_groups() -> dict[str, list[str]]:
    with _lock:
        return _mutable_copy(_snapshot.groups)


def get_routing() -> dict:
    with _lock:
        return _mutable_copy(_snapshot.routing)


def _network_config_snapshot() -> dict:
    """Read the source-of-truth network subtree without trusting runtime state."""
    try:
        net = config.get().get("network") or {}
        return net if isinstance(net, dict) else {}
    except Exception:
        return {}


def _has_explicit_routing_rule(routing: dict) -> bool:
    """Whether routing contains anything beyond the implicit default direct."""
    for key, value in routing.items():
        if key == "directFallback":
            continue
        if key == "default":
            if value != "direct":
                return True
            continue
        if key in ("accounts", "channels", "models", "providers"):
            if isinstance(value, dict) and value:
                return True
            continue
        # An explicit family/purpose route matters even when it selects direct:
        # it intentionally overrides legacy SOCKS5 for that purpose.
        if value not in (None, "", {}, []):
            return True
    return False


def _has_non_direct_target(value) -> bool:
    if isinstance(value, list):
        return any(_has_non_direct_target(item) for item in value)
    return isinstance(value, str) and bool(value) and value != "direct"


def has_non_direct_routing_rules() -> bool:
    """Whether config asks any traffic to use a non-direct new-proxy target.

    This deliberately ignores a mere proxy definition.  A user may add proxies
    before assigning a route; until a non-direct rule exists, normal direct
    behavior remains available.
    """
    routing = _network_config_snapshot().get("routing") or {}
    if not isinstance(routing, dict):
        return False
    for key, value in routing.items():
        if key == "directFallback":
            continue
        if key in ("accounts", "channels", "models", "providers"):
            if isinstance(value, dict) and any(_has_non_direct_target(v) for v in value.values()):
                return True
            continue
        if _has_non_direct_target(value):
            return True
    return False


def direct_fallback_enabled() -> bool:
    """Whether a broken configured non-direct route may explicitly use direct."""
    routing = _network_config_snapshot().get("routing") or {}
    return bool(routing.get("directFallback", False)) if isinstance(routing, dict) else False


def is_configured() -> bool:
    """Whether the new proxy subsystem has user-provided configuration.

    Invalid proxy definitions count as configured too.  Otherwise a broken
    definition selected by a route could disappear from runtime state and cause
    a silent downgrade to direct traffic.
    """
    net = _network_config_snapshot()
    raw_proxies = net.get("proxies") or {}
    raw_groups = net.get("groups") or {}
    routing = net.get("routing") or {}
    return bool(raw_proxies or raw_groups or (
        isinstance(routing, dict) and _has_explicit_routing_rule(routing)
    ))


# ── Route resolution ─────────────────────────────────────────────

def resolve_proxy_target(*, channel_key: str = "", model: str = "",
                         purpose: str = "", account_key: str = "",
                         provider: str = "") -> str | list[str]:
    """Resolve which proxy/group to use for a given context.

    Priority: account = channel > model > upstream type > legacy purpose > default

    Account and channel routes are the same highest tier.  In the rare case
    both are passed and both exist, account_key wins as a deterministic tie
    breaker because it is the more specific OAuth-account identifier.

    Returns:
      - A proxy name (str)  → single proxy
      - A group name (str) that maps to a list → failover group
      - "direct"
    """
    with _lock:
        snapshot = _snapshot
        routing = snapshot.routing

    def selected(value):
        return _mutable_copy(value)

    # 1. Account/channel-level override (same priority; account wins ties)
    acct_routes = routing.get("accounts") or {}
    if account_key and account_key in acct_routes:
        return selected(acct_routes[account_key])
    # The Telegram account-routing UI stores OAuth channel keys (``oauth:...``),
    # while transports also pass the provider account key (without that prefix).
    # Account keys still win when both routes exist, but a missing account-key
    # route must fall back to the saved channel key instead of silently going
    # direct.
    if channel_key and channel_key in acct_routes:
        return selected(acct_routes[channel_key])

    # 1b. Channel-level override
    ch_routes = routing.get("channels") or {}
    if channel_key and channel_key in ch_routes:
        return selected(ch_routes[channel_key])

    # 2. Model-level override
    model_routes = routing.get("models") or {}
    if model and model in model_routes:
        return selected(model_routes[model])

    # 3. Stable upstream type, available even before any OAuth account exists.
    # Telegram is an independent function, never a model/provider route.
    route_provider = "" if purpose == "telegram" else provider_for_context(
        provider=provider, channel_key=channel_key, account_key=account_key,
        purpose=purpose, api_providers=snapshot.api_providers,
    )
    provider_routes = routing.get("providers") or {}
    if route_provider and route_provider in provider_routes:
        return selected(provider_routes[route_provider])

    # 4. Preserve the exact previous purpose/family path if no type override.
    purpose = legacy_purpose(purpose)
    if purpose and purpose in routing:
        return selected(routing[purpose])

    # Backward-compatible alias used by the first draft of the UI.
    if purpose.startswith("oauth_") and "oauth" in routing:
        return selected(routing["oauth"])

    # 4. Default
    return selected(routing.get("default", "direct"))


def _expand_target(target: str | list[str]) -> list[str]:
    """Expand a target (proxy name or group name) into a list of proxy names.

    An empty/invalid explicitly configured target is not equivalent to direct:
    callers must fail closed unless the user enabled ``directFallback``.
    """
    if isinstance(target, list):
        return list(target)
    if not isinstance(target, str) or not target:
        return []
    g = get_group(target)
    if g:
        return g
    # Single proxy
    return [target]


def expand_target(target: str | list[str]) -> list[str]:
    """Public wrapper for target expansion."""
    return _expand_target(target)


def resolve_proxy_chain(*, channel_key: str = "", model: str = "",
                        purpose: str = "", account_key: str = "",
                        provider: str = "") -> list[str]:
    """Resolve and expand a routing target into an ordered proxy chain."""
    return _expand_target(resolve_proxy_target(
        channel_key=channel_key,
        model=model,
        purpose=purpose,
        account_key=account_key,
        provider=provider,
    ))


def target_supports_sync(target: str | list[str]) -> bool:
    """Return True if a target can be used by sync httpx callers."""
    for name in _expand_target(target):
        conn = get_connector(name)
        if conn is None:
            continue
        if conn.type in ("direct", "socks5", "ss2022"):
            return True
    return False


# ── Failover client creation ─────────────────────────────────────

async def create_client_with_failover(
    *,
    channel_key: str = "",
    model: str = "",
    purpose: str = "",
    account_key: str = "",
    provider: str = "",
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
        channel_key=channel_key, model=model, purpose=purpose, account_key=account_key, provider=provider,
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
    section="models", "channels", "accounts" or "providers" → nested dict
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


def set_direct_fallback(enabled: bool) -> None:
    """Persist the user-controlled implicit direct fallback switch."""
    def _mut(c):
        routing = c.setdefault("network", {}).setdefault("routing", {})
        routing["directFallback"] = bool(enabled)
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
