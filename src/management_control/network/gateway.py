"""Narrow production/fake seam for network side effects."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Callable, Protocol

from src import config, network, network_monitor, state_db
from src.channel import registry


class NetworkGateway(Protocol):
    def config_get(self) -> dict[str, Any]: ...
    def serialized_updates(self): ...
    def normalize_dns(self, servers: list[str]) -> list[str]: ...
    def test_dns(self, servers: list[str]) -> dict[str, Any]: ...
    def save_dns(self, servers: list[str]) -> None: ...
    def sync_system_dns(self) -> list[str]: ...
    def dns_cache(self) -> list[dict[str, Any]]: ...
    def clear_dns_cache(self) -> None: ...
    def normalize_socks5(self, url: str): ...
    async def test_socks5(self, url: str) -> dict[str, Any]: ...
    def save_socks5(self, url: str, *, enabled: bool) -> str: ...
    def set_socks5_enabled(self, enabled: bool) -> None: ...
    def channels(self) -> list[Any]: ...
    def monitor_config(self) -> dict[str, Any]: ...
    def update_monitor(self, mutator: Callable[[dict[str, Any]], None]) -> None: ...
    def set_monitor_channel(self, channel_id: str, enabled: bool) -> None: ...
    def monitor_transaction(self): ...
    def checks(self) -> list[dict[str, Any]]: ...
    async def run_monitor(self) -> list[Any]: ...


class ModuleNetworkGateway:
    def config_get(self) -> dict[str, Any]:
        return config.get()

    def serialized_updates(self):
        return config.serialized_updates()

    def normalize_dns(self, servers: list[str]) -> list[str]:
        return network.normalize_dns_servers(servers)

    def parse_dns_text(self, value: str) -> list[str]:
        return network.parse_dns_input(value)

    def test_dns(self, servers: list[str]) -> dict[str, Any]:
        return network.test_dns_servers(servers)

    def save_dns(self, servers: list[str]) -> None:
        network.save_dns_servers(servers)

    def sync_system_dns(self) -> list[str]:
        return network.sync_system_dns_now()

    def dns_cache(self) -> list[dict[str, Any]]:
        return network.dns_cache_entries()

    def clear_dns_cache(self) -> None:
        network.clear_dns_cache()

    def normalize_socks5(self, url: str):
        return network.normalize_socks5_url(url)

    async def test_socks5(self, url: str) -> dict[str, Any]:
        return await network.test_socks5(url)

    def save_socks5(self, url: str, *, enabled: bool) -> str:
        return network.save_socks5(url, enabled=enabled)

    def set_socks5_enabled(self, enabled: bool) -> None:
        network.set_socks5_enabled(enabled)

    def channels(self) -> list[Any]:
        return list(registry.all_channels())

    def monitor_config(self) -> dict[str, Any]:
        return network_monitor.cfg()

    def update_monitor(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        network_monitor.update_settings(mutator)

    def set_monitor_channel(self, channel_id: str, enabled: bool) -> None:
        network_monitor.set_channel_enabled(channel_id, enabled)

    @contextmanager
    def monitor_transaction(self):
        # Keep the same lock order as network_monitor.update_settings/run_once:
        # monitor generation lock, then serialized config lifecycle lock.
        with network_monitor._monitor_lifecycle_lock:  # noqa: SLF001 - gateway owns legacy adaptation
            with config.serialized_updates():
                yield

    def checks(self) -> list[dict[str, Any]]:
        return list(state_db.network_check_load_all())

    async def run_monitor(self) -> list[Any]:
        # Manual run: executes even when the monitor master switch is off.
        return await network_monitor.run_once(save=True, manual=True)

    def format_monitor_result(self, item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return dict(item)
        return {
            "key": str(getattr(item, "key", "")),
            "label": str(getattr(item, "label", "")),
            "category": str(getattr(item, "category", "")),
            "ok": bool(getattr(item, "ok", False)),
            "detail": str(getattr(item, "detail", "") or ""),
            "latencyMilliseconds": getattr(item, "latency_ms", None),
        }


DEFAULT_NETWORK_GATEWAY = ModuleNetworkGateway()
