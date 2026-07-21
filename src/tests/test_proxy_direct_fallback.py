from __future__ import annotations

import pytest

from src.network import _configured_proxy_chain_or_none
from src.proxy.connector import ProxyConnectError
from src.proxy import manager as pm
from src.transports.http_runtime import _resolve_http_route_chain
from src.transports.ws_runtime import resolve_ws_route_chain


class _Channel:
    key = "oauth:openai:test"
    protocol = "openai-responses"
    account_key = "openai:test"


class _Connector:
    def __init__(self, type_: str):
        self.type = type_


def _patch_proxy_manager(monkeypatch, *, chain: list[str], connectors: dict[str, object],
                         enabled: bool, configured: bool = True, non_direct_rules: bool = True):
    monkeypatch.setattr(pm, "init", lambda: None)
    monkeypatch.setattr(pm, "is_configured", lambda: configured)
    monkeypatch.setattr(pm, "has_non_direct_routing_rules", lambda: non_direct_rules)
    monkeypatch.setattr(pm, "direct_fallback_enabled", lambda: enabled)
    monkeypatch.setattr(pm, "resolve_proxy_chain", lambda **_kwargs: list(chain))
    monkeypatch.setattr(pm, "get_connector", lambda name: connectors.get(name))


def test_configured_broken_http_route_fails_closed_by_default(monkeypatch):
    _patch_proxy_manager(monkeypatch, chain=["broken"], connectors={}, enabled=False)

    routes, error = _resolve_http_route_chain(_Channel(), "gpt-test")

    assert routes == []
    assert error is not None
    assert error.outcome == "proxy_connect_error"
    assert "no valid target" in (error.error_detail or "")


def test_enabled_direct_fallback_is_appended_after_configured_proxy(monkeypatch):
    proxy = _Connector("socks5")
    direct = _Connector("direct")
    _patch_proxy_manager(
        monkeypatch,
        chain=["proxy-a"],
        connectors={"proxy-a": proxy, "direct": direct},
        enabled=True,
    )

    http_routes, error = _resolve_http_route_chain(_Channel(), "gpt-test")
    ws_routes = resolve_ws_route_chain(_Channel(), "gpt-test")
    network_chain = _configured_proxy_chain_or_none(
        proxy_purpose="oauth_openai",
        proxy_channel=_Channel.key,
        proxy_model="gpt-test",
    )

    assert error is None
    assert [name for name, _ in http_routes] == ["proxy-a", "direct"]
    assert [name for name, _ in ws_routes] == ["proxy-a", "direct"]
    assert [name for name, _ in network_chain] == ["proxy-a", "direct"]


def test_enabled_direct_fallback_recovers_unresolvable_route(monkeypatch):
    direct = _Connector("direct")
    _patch_proxy_manager(
        monkeypatch,
        chain=["broken"],
        connectors={"direct": direct},
        enabled=True,
    )

    http_routes, error = _resolve_http_route_chain(_Channel(), "gpt-test")
    ws_routes = resolve_ws_route_chain(_Channel(), "gpt-test")
    network_chain = _configured_proxy_chain_or_none(
        proxy_purpose="oauth_openai",
        proxy_channel=_Channel.key,
        proxy_model="gpt-test",
    )

    assert error is None
    assert http_routes == [("direct", None)]
    assert ws_routes == [("direct", None)]
    assert [name for name, _ in network_chain] == ["direct"]


def test_no_network_rule_keeps_normal_direct_path(monkeypatch):
    _patch_proxy_manager(
        monkeypatch,
        chain=[],
        connectors={},
        enabled=False,
        configured=False,
        non_direct_rules=False,
    )

    routes, error = _resolve_http_route_chain(_Channel(), "gpt-test")

    assert error is None
    assert routes == [("direct", None)]
    assert _configured_proxy_chain_or_none(
        proxy_purpose="oauth_openai",
        proxy_channel=_Channel.key,
        proxy_model="gpt-test",
    ) is None


def test_network_helper_does_not_silently_fall_back_when_disabled(monkeypatch):
    _patch_proxy_manager(monkeypatch, chain=["broken"], connectors={}, enabled=False)

    with pytest.raises(ProxyConnectError, match="no valid target"):
        _configured_proxy_chain_or_none(
            proxy_purpose="oauth_openai",
            proxy_channel=_Channel.key,
            proxy_model="gpt-test",
        )
