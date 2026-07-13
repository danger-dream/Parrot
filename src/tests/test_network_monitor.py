from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import config, network_monitor, state_db
    return {"config": config, "network_monitor": network_monitor, "state_db": state_db}


def test_monitor_interval_minimum(m):
    nm = m["network_monitor"]
    nm.update_settings(lambda mon: mon.__setitem__("intervalSeconds", 1))
    assert nm.cfg()["intervalSeconds"] == 5


def test_channel_toggle(m):
    nm = m["network_monitor"]
    nm.set_channel_enabled("api:foo", True)
    assert nm.channel_enabled("api:foo") is True
    nm.update_settings(lambda mon: mon.setdefault("channels", {}).__setitem__("enabled", True))
    assert "api:foo" in nm.enabled_channel_keys()
    nm.set_channel_enabled("api:foo", False)
    assert nm.channel_enabled("api:foo") is False


def test_state_and_summary(m):
    st = m["state_db"]
    nm = m["network_monitor"]
    st.init()
    st.network_check_save({
        "key": "dns",
        "label": "DNS 解析",
        "category": "dns",
        "ok": False,
        "detail": "boom",
        "latency_ms": 12,
        "checked_at": 1,
    })
    row = st.network_check_load("dns")
    assert row is not None
    assert row["ok"] == 0
    assert "DNS" in (nm.active_summary() or "")
    st.network_check_save({
        "key": "dns",
        "label": "DNS 解析",
        "category": "dns",
        "ok": True,
        "detail": "",
        "latency_ms": 10,
        "checked_at": 2,
    })
    assert nm.active_summary() is None


def test_disabling_monitor_clears_persisted_banner(m):
    st = m["state_db"]
    nm = m["network_monitor"]
    st.init()
    st.network_check_save({
        "key": "socks5",
        "label": "SOCKS5 代理",
        "category": "socks5",
        "ok": False,
        "detail": "old failure",
        "latency_ms": None,
        "checked_at": 1,
    })
    assert nm.active_summary() is not None
    nm.update_settings(lambda mon: mon.__setitem__("enabled", False))
    assert nm.active_summary() is None


def test_socks5_check_recognizes_named_proxy(m, monkeypatch):
    import asyncio

    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        net = c.setdefault("network", {})
        net["socks5"] = {"enabled": False, "url": ""}
        net["proxies"] = {
            "ipv6-1": {"type": "socks5", "url": "socks5://127.0.0.1:52081"},
        }
    cfg.update(_set)

    class _Response:
        status_code = 200

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["proxy"] == "socks5://127.0.0.1:52081"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(nm.httpx, "AsyncClient", _Client)
    result = asyncio.run(nm._socks5_check(5))
    assert result.ok is True
    assert result.detail == ""


# ─── 新增：core 检测家族过滤 + 孤儿渠道清理 ──────────────────

def _import_modules_for_family():
    """为家族过滤测试单独导入：也需要 oauth_manager."""
    import importlib
    from src import config, network_monitor
    return {"config": config, "network_monitor": network_monitor}


def test_has_family_account_empty(m):
    """空 config：所有 family 都没有账号；只有 cloudflare 返回 True。"""
    nm = m["network_monitor"]
    cfg = m["config"]

    def _wipe(c):
        c["oauthAccounts"] = []
        c["channels"] = []
    cfg.update(_wipe)

    assert nm._has_family_account("openai") is False
    assert nm._has_family_account("anthropic") is False
    assert nm._has_family_account("cloudflare") is True


def test_has_family_account_anthropic_oauth(m):
    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        c["oauthAccounts"] = [{"provider": "anthropic", "email": "x@y.io"}]
        c["channels"] = []
    cfg.update(_set)
    assert nm._has_family_account("anthropic") is True
    assert nm._has_family_account("openai") is False


def test_has_family_account_openai_oauth(m):
    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        c["oauthAccounts"] = [{"provider": "openai", "email": "x@y.io"}]
        c["channels"] = []
    cfg.update(_set)
    assert nm._has_family_account("openai") is True
    assert nm._has_family_account("anthropic") is False


def test_has_family_account_api_channel_only(m):
    """没有 OAuth 账号，但有 API 渠道 → 也算这个家族有账号。"""
    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        c["oauthAccounts"] = []
        c["channels"] = [
            {"name": "ds", "baseUrl": "https://x", "protocol": "anthropic"},
        ]
    cfg.update(_set)
    assert nm._has_family_account("anthropic") is True
    assert nm._has_family_account("openai") is False

    def _set2(c):
        c["channels"] = [
            {"name": "gpt", "baseUrl": "https://x",
             "protocol": "openai-chat"},
        ]
    cfg.update(_set2)
    assert nm._has_family_account("openai") is True
    assert nm._has_family_account("anthropic") is False


def test_prune_orphan_channel_toggles_removes_dead_keys(m):
    """byKey 里有 registry 不存在的 key → prune 应该清掉它。"""
    nm = m["network_monitor"]
    cfg = m["config"]
    from src.channel import registry

    # 注入一些 byKey，包含一个不存在的孤儿
    def _seed(c):
        c.setdefault("network", {}).setdefault("monitor", {}).setdefault(
            "channels", {"enabled": True, "byKey": {}}
        )["byKey"] = {
            "api:ghost": True,        # registry 里不存在
            "api:also_ghost": False,  # registry 里不存在
        }
    cfg.update(_seed)
    # 此时 registry 是空的（isolated config 里 channels=[]，rebuild 后无 channel）
    registry.rebuild_from_config()

    removed = nm.prune_orphan_channel_toggles()
    assert removed == 2

    by_key = (cfg.get().get("network") or {}).get("monitor", {}).get(
        "channels", {}).get("byKey", {})
    assert by_key == {}


def test_prune_orphan_channel_toggles_keeps_live_keys(m):
    """byKey 里有 registry 里存在的 key → 保留。"""
    nm = m["network_monitor"]
    cfg = m["config"]
    from src.channel import registry

    def _seed(c):
        c["channels"] = [
            {"name": "live", "baseUrl": "https://x",
             "protocol": "anthropic"},
        ]
        c.setdefault("network", {}).setdefault("monitor", {}).setdefault(
            "channels", {"enabled": True, "byKey": {}}
        )["byKey"] = {
            "api:live": True,
            "api:dead": True,
        }
    cfg.update(_seed)
    registry.rebuild_from_config()

    removed = nm.prune_orphan_channel_toggles()
    assert removed == 1

    by_key = (cfg.get().get("network") or {}).get("monitor", {}).get(
        "channels", {}).get("byKey", {})
    assert "api:live" in by_key
    assert "api:dead" not in by_key


def test_prune_orphan_channel_toggles_no_op_when_clean(m):
    """没有孤儿 → 返回 0，不写盘。"""
    nm = m["network_monitor"]
    cfg = m["config"]
    from src.channel import registry

    def _seed(c):
        c["channels"] = [
            {"name": "a", "baseUrl": "https://x",
             "protocol": "anthropic"},
        ]
        c.setdefault("network", {}).setdefault("monitor", {}).setdefault(
            "channels", {"enabled": True, "byKey": {}}
        )["byKey"] = {"api:a": True}
    cfg.update(_seed)
    registry.rebuild_from_config()

    assert nm.prune_orphan_channel_toggles() == 0


def test_run_once_skips_core_when_no_account(m, monkeypatch):
    """core.openai/claude 开关都开，但 config 里没有任何账号 → run_once 不应调用 _core_check。"""
    nm = m["network_monitor"]
    cfg = m["config"]
    import asyncio

    # 清空账号
    def _wipe(c):
        c["oauthAccounts"] = []
        c["channels"] = []
        mon = c.setdefault("network", {}).setdefault("monitor", {})
        mon["enabled"] = True
        mon["dns"] = False
        mon["socks5"] = False
        mon["channels"] = {"enabled": False, "byKey": {}}
        mon["core"] = {"openai": True, "claude": True, "cloudflare": False}
    cfg.update(_wipe)

    called = []
    monkeypatch.setattr(nm, "_core_check",
                        lambda name, timeout: called.append(name) or
                        nm.CheckResult(f"core:{name}", name, "core", True, "ok"))

    results = asyncio.run(nm.run_once(save=False))
    # openai/claude 都没账号 → 不应执行 _core_check
    assert called == [], f"core checks should be skipped, but got: {called}"


def test_run_once_does_check_core_when_account_present(m, monkeypatch):
    """有 OpenAI 账号 → openai core 应被检测；claude 没账号 → 跳过。"""
    nm = m["network_monitor"]
    cfg = m["config"]
    import asyncio

    def _set(c):
        c["oauthAccounts"] = [{"provider": "openai", "email": "x@y.io"}]
        c["channels"] = []
        mon = c.setdefault("network", {}).setdefault("monitor", {})
        mon["enabled"] = True
        mon["dns"] = False
        mon["socks5"] = False
        mon["channels"] = {"enabled": False, "byKey": {}}
        mon["core"] = {"openai": True, "claude": True, "cloudflare": True}
    cfg.update(_set)

    called = []
    monkeypatch.setattr(nm, "_core_check",
                        lambda name, timeout: called.append(name) or
                        nm.CheckResult(f"core:{name}", name, "core", True, "ok"))

    asyncio.run(nm.run_once(save=False))
    # openai 有账号 → 应被检测；claude 没账号 → 跳过；cloudflare 没家族概念 → 总是被检测
    assert "openai" in called
    assert "claude" not in called
    assert "cloudflare" in called
