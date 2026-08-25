from __future__ import annotations

import copy as _ap_copy
import os as _ap_os
import sys as _ap_sys

import pytest

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()


@pytest.fixture(autouse=True)
def _restore_network_config_after_test():
    """Prevent monitor/proxy config mutations from leaking across test files."""
    from src import config

    before = _ap_copy.deepcopy(config.get().get("network"))
    yield

    def _restore(c):
        if before is None:
            c.pop("network", None)
        else:
            c["network"] = _ap_copy.deepcopy(before)

    config.update(_restore)


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import config, network_monitor, state_db
    return {"config": config, "network_monitor": network_monitor, "state_db": state_db}


def test_routed_channel_and_core_monitor_are_connect_close_only(m, monkeypatch):
    """Probe routing is observable and incurs no HTTP/model payload write."""
    from types import SimpleNamespace

    nm = m["network_monitor"]
    opens = []

    class RecordingStream:
        def __init__(self, close_error=None):
            self.close_error = close_error
            self.closed = 0
            self.writes = []
        def close(self):
            self.closed += 1
            if self.close_error:
                raise self.close_error
        def write(self, payload):
            self.writes.append(payload)
            pytest.fail("monitor must never write an HTTP/model payload")
        send = write

    streams = []
    def open_stream(host, port, **kwargs):
        opens.append((host, port, kwargs))
        stream = RecordingStream()
        streams.append(stream)
        return stream

    monkeypatch.setattr(nm.network, "open_sync_stream", open_stream)
    channel = SimpleNamespace(
        key="oauth:acct", display_name="acct", type="oauth",
        provider="openai", protocol="openai-responses",
    )
    channel_result = nm._channel_check(channel, 3)
    core_result = nm._core_check("openai", 4)

    assert channel_result.ok and core_result.ok
    assert opens[0][2] == {
        "timeout": 3, "proxy_purpose": "channel_monitor",
        "proxy_channel": "oauth:acct",
    }
    assert opens[1][2] == {
        "timeout": 4, "proxy_purpose": "core_monitor", "proxy_channel": "",
    }
    assert all(stream.closed == 1 and stream.writes == [] for stream in streams)


def test_monitor_open_parse_and_close_failures_are_failed_results(m, monkeypatch):
    from types import SimpleNamespace

    nm = m["network_monitor"]
    channel = SimpleNamespace(
        key="api:bad", display_name="bad", type="api", base_url="not-a-url",
        api_path=None, protocol="anthropic",
    )
    parsed = nm._channel_check(channel, 1)
    assert not parsed.ok and "host" in parsed.detail

    monkeypatch.setattr(
        nm.network, "open_sync_stream",
        lambda *_a, **_k: (_ for _ in ()).throw(ConnectionError("route failed")),
    )
    failed_open = nm._core_check("openai", 1)
    assert not failed_open.ok and failed_open.detail == "route failed"

    class CloseFails:
        def close(self):
            raise RuntimeError("close failed")
        def write(self, _payload):
            pytest.fail("monitor must never write")
        send = write

    monkeypatch.setattr(nm.network, "open_sync_stream", lambda *_a, **_k: CloseFails())
    failed_close = nm._core_check("openai", 1)
    assert not failed_close.ok and failed_close.detail == "close failed"


def test_monitor_explicit_network_fallback_remains_connect_only(m, monkeypatch):
    """A network-layer fallback may select direct, but monitor still only closes."""
    nm = m["network_monitor"]
    events = []

    class FallbackStream:
        selected_route = "direct-fallback"
        def close(self): events.append("close")
        def write(self, _payload): pytest.fail("fallback probe wrote payload")
        send = write

    def simulated_network_boundary(*_args, **kwargs):
        events.append((kwargs["proxy_purpose"], kwargs["proxy_channel"], "fallback"))
        return FallbackStream()

    monkeypatch.setattr(nm.network, "open_sync_stream", simulated_network_boundary)
    result = nm._core_check("cloudflare", 2)
    assert result.ok
    assert events == [("core_monitor", "", "fallback"), "close"]


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


def test_disable_and_reenable_rejects_old_generation(m, monkeypatch):
    import asyncio

    nm = m["network_monitor"]
    st = m["state_db"]
    st.init()
    st.network_check_delete_stale(set())

    def _enable(mon):
        mon.update({
            "enabled": True,
            "dns": False,
            "socks5": True,
            "channels": {"enabled": False, "byKey": {}},
            "core": {"openai": False, "claude": False, "cloudflare": False},
            "timeoutSeconds": 1,
        })

    nm.update_settings(_enable)
    started = asyncio.Event()
    release = asyncio.Event()
    notifications = []

    async def _blocked_check(_timeout):
        started.set()
        await release.wait()
        return nm.CheckResult("socks5", "SOCKS5 代理", "socks5", False, "old")

    monkeypatch.setattr(nm, "_socks5_check", _blocked_check)
    monkeypatch.setattr(
        nm.notifier,
        "notify_event",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    async def _case():
        old_round = asyncio.create_task(nm.run_once(save=True))
        await started.wait()
        nm.update_settings(lambda mon: mon.__setitem__("enabled", False))
        assert st.network_check_load("socks5") is None
        nm.update_settings(lambda mon: mon.__setitem__("enabled", True))
        release.set()
        results = await old_round
        assert len(results) == 1

    asyncio.run(_case())
    assert st.network_check_load("socks5") is None
    assert notifications == []


def test_disable_cleanup_failure_is_best_effort(m, monkeypatch, capsys):
    nm = m["network_monitor"]
    nm.update_settings(lambda mon: mon.__setitem__("enabled", True))

    def _fail_cleanup(_live_keys):
        raise RuntimeError("socks5://user:super-secret@example.test:1080")

    monkeypatch.setattr(nm.state_db, "network_check_delete_stale", _fail_cleanup)
    nm.update_settings(lambda mon: mon.__setitem__("enabled", False))

    assert nm.cfg()["enabled"] is False
    output = capsys.readouterr().out
    assert "disabled-state cleanup failed: RuntimeError" in output
    assert "super-secret" not in output
    assert "socks5://" not in output


def test_socks5_check_is_bounded_and_aggregates_safe_errors(m, monkeypatch):
    import asyncio

    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        net = c.setdefault("network", {})
        net["socks5"] = {
            "enabled": True,
            "url": "socks5://127.0.0.1:10000",
        }
        net["proxies"] = {
            **{
                f"named-{i}": {
                    "type": "socks5",
                    "url": f"socks5://127.0.0.1:{10001 + i}",
                }
                for i in range(5)
            },
            "secret-proxy": {
                "type": "socks5",
                "url": "socks5://user:super-secret@127.0.0.1:10009",
            },
        }

    cfg.update(_set)
    active = 0
    max_active = 0
    seen = []
    first_batch_full = asyncio.Event()
    release = asyncio.Event()

    class _Response:
        def __init__(self, status_code):
            self.status_code = status_code

    class _Client:
        def __init__(self, **kwargs):
            self.proxy = kwargs["proxy"]
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            seen.append(self.proxy)
            if active == nm._SOCKS5_MAX_CONCURRENCY:
                first_batch_full.set()
            try:
                await release.wait()
                if "super-secret" in self.proxy:
                    raise RuntimeError(f"failed via {self.proxy}")
                return _Response(503 if self.proxy.endswith(":10004") else 200)
            finally:
                active -= 1

    monkeypatch.setattr(nm.httpx, "AsyncClient", _Client)

    async def _case():
        check = asyncio.create_task(nm._socks5_check(1))
        await asyncio.wait_for(first_batch_full.wait(), timeout=1)
        assert active == nm._SOCKS5_MAX_CONCURRENCY
        release.set()
        result = await asyncio.wait_for(check, timeout=1)
        leftovers = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("network-monitor-socks5-")
        ]
        return result, leftovers

    result, leftovers = asyncio.run(_case())
    assert max_active == nm._SOCKS5_MAX_CONCURRENCY
    assert len(seen) == 7  # legacy global plus six named proxies
    assert result.ok is False
    assert "named-3: HTTP 503" in result.detail
    assert "secret-proxy: RuntimeError" in result.detail
    assert "super-secret" not in result.detail
    assert "socks5://" not in result.detail
    assert active == 0
    assert leftovers == []


def test_socks5_round_deadline_and_cancellation_leave_no_tasks(m, monkeypatch):
    import asyncio
    import time

    nm = m["network_monitor"]
    cfg = m["config"]

    def _set(c):
        net = c.setdefault("network", {})
        net["socks5"] = {"enabled": False, "url": ""}
        net["proxies"] = {
            f"slow-{i}": {
                "type": "socks5",
                "url": f"socks5://127.0.0.1:{11000 + i}",
            }
            for i in range(6)
        }

    cfg.update(_set)
    monkeypatch.setattr(nm, "_SOCKS5_ROUND_DEADLINE_FACTOR", 1.0)
    monkeypatch.setattr(nm, "_SOCKS5_MIN_ROUND_DEADLINE_SECONDS", 0.02)
    active = 0
    entered = asyncio.Event()
    never = asyncio.Event()

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            nonlocal active
            active += 1
            entered.set()
            try:
                await never.wait()
            finally:
                active -= 1

    monkeypatch.setattr(nm.httpx, "AsyncClient", _Client)

    async def _probe_tasks():
        return [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("network-monitor-socks5-")
        ]

    async def _deadline_case():
        started_at = time.monotonic()
        result = await nm._socks5_check(0.02)
        elapsed = time.monotonic() - started_at
        return result, elapsed, await _probe_tasks()

    result, elapsed, leftovers = asyncio.run(_deadline_case())
    assert result.ok is False
    assert "slow-0: 整轮超时" in result.detail
    assert elapsed < 0.5
    assert active == 0
    assert leftovers == []

    # Also exercise caller cancellation, which must run the same cancel+gather
    # cleanup path instead of leaving proxy tasks behind.
    entered = asyncio.Event()
    never = asyncio.Event()

    async def _cancel_case():
        check = asyncio.create_task(nm._socks5_check(5))
        await entered.wait()
        check.cancel()
        try:
            await check
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled SOCKS5 round did not propagate cancellation")
        return await _probe_tasks()

    leftovers = asyncio.run(_cancel_case())
    assert active == 0
    assert leftovers == []


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
