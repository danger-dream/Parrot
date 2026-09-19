"""Antigravity TLS/H2 指纹伪装：transport 工厂、async_client 接线与渠道属性。"""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys

import pytest
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import warnings

import httpx


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import config, network
    from src.transports import fingerprint
    from src.channel import base as channel_base
    from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
    return {
        "config": config,
        "network": network,
        "fingerprint": fingerprint,
        "channel_base": channel_base,
        "AntigravityOAuthChannel": AntigravityOAuthChannel,
    }


def test_channel_base_default_is_none(m):
    """未声明指纹的渠道保持共享 httpx 池行为。"""
    assert m["channel_base"].Channel.tls_fingerprint is None


def test_factory_falls_back_without_backend(m, monkeypatch):
    """backend 缺失时工厂返回 None 并告警，调用方 fail-open。"""
    fp = m["fingerprint"]
    monkeypatch.setattr(fp, "AsyncSession", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert fp.impersonation_transport("chrome131") is None
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_factory_rejects_empty_profile(m):
    fp = m["fingerprint"]
    assert fp.impersonation_transport("") is None


@pytest.mark.asyncio
async def test_async_client_uses_impersonated_transport(m):
    """declared profile → curl_cffi transport；backend 缺失 → 默认 transport。"""
    fp, net = m["fingerprint"], m["network"]
    client = net.async_client(impersonate="chrome131", http2=False)
    try:
        if fp.backend_available():
            assert isinstance(client._transport, fp.ImpersonatedTransport)
        else:
            assert client._transport.__class__.__name__ == "AsyncHTTPTransport"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_client_without_impersonate_keeps_default_transport(m):
    net = m["network"]
    client = net.async_client(http2=False)
    try:
        assert client._transport.__class__.__name__ == "AsyncHTTPTransport"
    finally:
        await client.aclose()


def test_antigravity_channel_reads_tls_fingerprint(m, monkeypatch):
    cfg = m["config"]
    monkeypatch.setattr(cfg, "get", lambda: {"antigravityOAuth": {"tlsFingerprint": "chrome131"}})
    channel = m["AntigravityOAuthChannel"]({"email": "fp@example.com", "project_id": "p"})
    assert channel.tls_fingerprint == "chrome131"


def test_antigravity_channel_empty_fingerprint_disables(m, monkeypatch):
    cfg = m["config"]
    monkeypatch.setattr(cfg, "get", lambda: {"antigravityOAuth": {"tlsFingerprint": ""}})
    channel = m["AntigravityOAuthChannel"]({"email": "fp-off@example.com", "project_id": "p"})
    assert channel.tls_fingerprint is None


def test_antigravity_default_config_ships_fingerprint(m):
    """默认配置即启用 chrome131 画像（可用空串显式关闭）。"""
    cfg = m["config"]
    assert cfg.DEFAULT_CONFIG["antigravityOAuth"].get("tlsFingerprint") == "chrome131"


@pytest.mark.asyncio
async def test_impersonated_transport_honors_proxy(m):
    """显式代理必须由 transport 承担：httpx 传自定义 transport 时会忽略 client.proxy，
    静默绕过会改变出口 IP。用不可达代理验证——请求必须失败而不是直连成功。"""
    fp = m["fingerprint"]
    if not fp.backend_available():
        pytest.skip("curl_cffi backend not installed")
    transport = fp.impersonation_transport("chrome131", proxy="socks5h://127.0.0.1:1")
    assert transport is not None
    client = httpx.AsyncClient(transport=transport, timeout=10)
    try:
        with pytest.raises(Exception):
            await client.get("https://example.com")
    finally:
        await client.aclose()
