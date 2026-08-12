"""M5 后台任务集成测试（全部 mock / mockMode，0 真实网络调用）。

覆盖：
  - probe_channel_model：API 渠道成功 / 失败 / OAuth 拒绝
  - recovery_run_once：cooldown 恢复（OAuth 被跳过）
  - proactive_refresh_once：近过期刷新、健康跳过、auth_error 处理、临时网络失败不误禁用
  - quota_monitor_once：高利用率禁用、恢复、跳过 user/auth_error

运行：./venv/bin/python -m src.tests.test_m5
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        config, cooldown, notifier, oauth_manager, probe, scorer, state_db,
    )
    from src.channel import api_channel, oauth_channel, registry
    return {
        "config": config, "cooldown": cooldown, "notifier": notifier,
        "oauth_manager": oauth_manager, "probe": probe, "scorer": scorer,
        "state_db": state_db, "api_channel": api_channel,
        "oauth_channel": oauth_channel, "registry": registry,
    }


def _reset(m):
    m["state_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["state_db"].client_affinity_delete()
    for mod_name in ("cooldown", "scorer"):
        m[mod_name]._initialized = False
    m["cooldown"].init()
    m["scorer"].init()


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


def _make_api_channel(m, name, base_url):
    return m["api_channel"].ApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": "glm-5", "alias": "glm-5"}],
        "cc_mimicry": False, "enabled": True,
    })


# ─── 通知收集器 ──────────────────────────────────────────────────

class NotifyCollector:
    def __init__(self):
        self.messages: list[str] = []
    def __call__(self, text: str):
        self.messages.append(text)


# ─── Probe 测试 ──────────────────────────────────────────────────

async def test_probe_api_success_and_failure(m):
    _reset(m)
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    # 用 MockTransport 模拟一个 API 渠道
    # 把 probe.probe_channel_model 里的 httpx.AsyncClient(...) 路径劫持到 mock 不容易，
    # 所以我们用一个真的本地 mock endpoint：注入一个 MockTransport AsyncClient 替换 probe 内部的 client。
    # 改用猴补 httpx.AsyncClient
    import httpx as _httpx

    class FakeClient:
        def __init__(self, transport_fn, timeout=None, **kw):
            self._fn = transport_fn
            self._timeout = timeout
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, content=None, **kw):
            return self._fn(url, headers, content)

    # ─── 成功场景 ──
    def success_fn(url, headers, content):
        return _httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "2"}],
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    orig_AsyncClient = _httpx.AsyncClient
    _httpx.AsyncClient = lambda **kw: FakeClient(success_fn, **kw)
    try:
        ch = _make_api_channel(m, "chA", "https://cha")
        ok, elapsed, reason = await m["probe"].probe_channel_model(ch, "glm-5", timeout_s=5)
        assert ok is True, f"expected success: reason={reason}"
        print(f"  [PASS] probe API success ({elapsed}ms)")
    finally:
        _httpx.AsyncClient = orig_AsyncClient

    # ─── HTTP 500 失败 ──
    def fail_fn(url, headers, content):
        return _httpx.Response(500, text="boom")
    _httpx.AsyncClient = lambda **kw: FakeClient(fail_fn, **kw)
    try:
        ch = _make_api_channel(m, "chA", "https://cha")
        ok, elapsed, reason = await m["probe"].probe_channel_model(ch, "glm-5", timeout_s=5)
        assert ok is False
        assert "HTTP 500" in reason, reason
        print(f"  [PASS] probe API HTTP 500 → reason: {reason[:60]}")
    finally:
        _httpx.AsyncClient = orig_AsyncClient

    # ─── 上游 200 + error JSON 失败 ──
    def err_json(url, headers, content):
        return _httpx.Response(200, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    _httpx.AsyncClient = lambda **kw: FakeClient(err_json, **kw)
    try:
        ok, elapsed, reason = await m["probe"].probe_channel_model(
            _make_api_channel(m, "chA", "https://cha"), "glm-5", timeout_s=5)
        assert ok is False
        assert "upstream error" in reason, reason
        print(f"  [PASS] probe API error JSON → reason: {reason[:60]}")
    finally:
        _httpx.AsyncClient = orig_AsyncClient


async def test_probe_oauth_skipped(m):
    _reset(m)
    # 伪造一个 OAuth 渠道（不会真发请求）
    ch = m["oauth_channel"].OAuthChannel(
        {"email": "skip@test.com", "access_token": "x", "refresh_token": "x",
         "expired": "2099-01-01T00:00:00Z", "enabled": True},
        default_models=["claude-opus-4-7"],
    )
    ok, elapsed, reason = await m["probe"].probe_channel_model(ch, "claude-opus-4-7")
    assert ok is False
    assert "oauth" in reason.lower()
    print(f"  [PASS] probe skips OAuth channel (reason: {reason})")


async def test_recovery_run_once(m):
    """让 chA 处于 cooldown，mock probe 返回成功 → 应清除。
    chB (OAuth) 处于 cooldown → 不应动它（probe 跳过）。"""
    _reset(m)
    cd = m["cooldown"]
    cfg = m["config"]

    # 确保 cooldownRecovery 开
    def _enable(c):
        c.setdefault("cooldownRecovery", {})["enabled"] = True
    cfg.update(_enable)

    chA = _make_api_channel(m, "chA", "https://cha")
    chO = m["oauth_channel"].OAuthChannel(
        {"email": "o@test.com", "access_token": "x", "refresh_token": "x",
         "expired": "2099-01-01T00:00:00Z", "enabled": True},
        default_models=["claude-opus-4-7"],
    )
    _install_channels(m, [chA, chO])

    # 测试本身验证 probe.recovery 的"清冷却"行为；为简化，关掉 OAuth 宽容次数
    # 让单次 record_error 即进入冷却（生产默认 3，本测试场景不验证 grace）。
    cfg.update(lambda c: c.__setitem__("oauthGraceCount", 0))

    # 注入 cooldown
    cd.record_error("api:chA", "glm-5", "initial failure")
    cd.record_error("oauth:o@test.com", "claude-opus-4-7", "fake oauth cooldown")
    assert cd.is_blocked("api:chA", "glm-5")
    assert cd.is_blocked("oauth:o@test.com", "claude-opus-4-7")

    # 猴补 AsyncClient 使 probe 成功
    import httpx as _httpx
    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            return _httpx.Response(200, json={"id":"x","type":"message","role":"assistant",
                                              "content":[{"type":"text","text":"ok"}],
                                              "usage":{"input_tokens":1,"output_tokens":1}})
    orig = _httpx.AsyncClient
    _httpx.AsyncClient = FakeClient
    try:
        cleared = await m["probe"].recovery_run_once()
    finally:
        _httpx.AsyncClient = orig

    assert cleared == 1, f"cleared={cleared}"
    assert not cd.is_blocked("api:chA", "glm-5"), "chA should be cleared"
    assert cd.is_blocked("oauth:o@test.com", "claude-opus-4-7"), "OAuth should still be blocked (not probed)"
    print("  [PASS] recovery_run_once cleared API, skipped OAuth")


async def test_recovery_skips_active_long_lived_quota_cooldown(m):
    """Known 1310 reset windows must stay quiet until their cooldown expires."""
    _reset(m)
    m["config"].update(
        lambda c: c.setdefault("cooldownRecovery", {}).__setitem__("enabled", True)
    )
    ch = _make_api_channel(m, "chA", "https://open.bigmodel.cn/api/paas/v4")
    _install_channels(m, [ch])
    reset_ms = int((time.time() + 3600) * 1000)
    detail = (
        'HTTP 429: {"error":{"code":"1310","message":'
        '"[1310][每周/每月使用上限，限额将在 2099-01-01 00:00:00 重置]"}}'
    )
    m["cooldown"].record_error(ch.key, "glm-5", detail, cooldown_until=reset_ms)

    calls = []
    original = m["probe"].probe_channel_model

    async def must_not_probe(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("active quota cooldown must not be probed")

    m["probe"].probe_channel_model = must_not_probe
    try:
        cleared = await m["probe"].recovery_run_once()
    finally:
        m["probe"].probe_channel_model = original

    assert cleared == 0
    assert calls == []
    assert m["cooldown"].is_blocked(ch.key, "glm-5")
    print("  [PASS] recovery skips active long-lived quota cooldown")


# ─── OAuth 主动刷新 ──────────────────────────────────────────────

async def test_proactive_refresh_triggers_near_expiry(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    # 开 mock 模式避免真实 HTTP
    def _setup(c):
        c.setdefault("oauth", {})["mockMode"] = True
        # 两个账户：一个快到期，一个健康
        c["oauthAccounts"] = [
            {
                "email": "near@test.com",
                "access_token": "old-token-1",
                "refresh_token": "r1",
                "expired": (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
            },
            {
                "email": "ok@test.com",
                "access_token": "old-token-2",
                "refresh_token": "r2",
                "expired": (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
            },
            {
                "email": "off@test.com",
                "access_token": "old-token-3",
                "refresh_token": "r3",
                "expired": (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "enabled": False, "disabled_reason": "user", "disabled_until": None, "models": [],
            },
        ]
    cfg.update(_setup)

    outcomes = await m["oauth_manager"].proactive_refresh_once(refresh_threshold_seconds=600)
    assert outcomes["near@test.com"] == "refreshed", outcomes
    assert outcomes["ok@test.com"].startswith("skipped:healthy"), outcomes
    assert outcomes["off@test.com"].startswith("skipped:"), outcomes

    # near 的 access_token 应被替换
    near = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "near@test.com")
    assert near["access_token"] != "old-token-1"
    assert near["access_token"].startswith("mock-access-")

    # 通知应至少 1 条（refresh 成功）
    m["notifier"].wait_drain(2.0)
    assert any(("refreshed" in msg.lower() or "已刷新" in msg)
               for msg in collector.messages), collector.messages
    print("  [PASS] proactive_refresh: near refreshed, ok skipped, off skipped")


async def test_proactive_refresh_network_failure_does_not_mark_auth_error(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    def _setup(c):
        c.setdefault("oauth", {})["mockMode"] = False  # 关 mock 走真实路径，但让 httpx 报错
        c["oauthAccounts"] = [{
            "email": "bad@test.com",
            "access_token": "old", "refresh_token": "r",
            "expired": (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
        }]
    cfg.update(_setup)

    # 猴补统一网络层，让 token 刷新路径抛网络异常
    import httpx as _httpx
    orig_post = m["oauth_manager"].network.post_sync
    def raising_post(*a, **kw):
        raise _httpx.ConnectError("simulated network down")
    m["oauth_manager"].network.post_sync = raising_post
    try:
        outcomes = await m["oauth_manager"].proactive_refresh_once(refresh_threshold_seconds=600)
    finally:
        m["oauth_manager"].network.post_sync = orig_post

    assert outcomes["bad@test.com"].startswith("failed:claude_oauth_network_error"), outcomes
    bad = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "bad@test.com")
    assert bad["enabled"] is True
    assert bad["disabled_reason"] is None
    m["notifier"].wait_drain(2.0)
    assert any(("刷新失败" in msg and "账号未自动禁用" in msg)
               for msg in collector.messages), collector.messages
    print("  [PASS] transient refresh failure → not marked auth_error + notified")


async def test_proactive_refresh_401_marks_auth_error(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    def _setup(c):
        c.setdefault("oauth", {})["mockMode"] = False
        c["oauthAccounts"] = [{
            "email": "expired@test.com",
            "access_token": "old", "refresh_token": "r",
            "expired": (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
        }]
    cfg.update(_setup)

    import httpx as _httpx
    orig_post = m["oauth_manager"].network.post_sync
    def response_401(*a, **kw):
        req = _httpx.Request("POST", "https://api.anthropic.com/v1/oauth/token")
        return _httpx.Response(401, request=req, json={"error": "invalid_grant"})
    m["oauth_manager"].network.post_sync = response_401
    try:
        outcomes = await m["oauth_manager"].proactive_refresh_once(refresh_threshold_seconds=600)
    finally:
        m["oauth_manager"].network.post_sync = orig_post

    assert outcomes["expired@test.com"].startswith("failed:claude_token_401"), outcomes
    bad = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "expired@test.com")
    assert bad["enabled"] is False
    assert bad["disabled_reason"] == "auth_error"
    m["notifier"].wait_drain(2.0)
    joined = "\n".join(collector.messages)
    assert "Claude 授权已失效" in joined
    assert "developer.mozilla" not in joined
    assert "Client error" not in joined
    print("  [PASS] auth refresh 401 → marked auth_error + friendly notification")


async def test_claude_refresh_compat_falls_back_legacy_no_scope(m):
    _reset(m)
    cfg = m["config"]

    def _setup(c):
        c.setdefault("oauth", {})["mockMode"] = False
        c["oauthAccounts"] = [{
            "email": "legacy@test.com",
            "provider": "claude",
            "access_token": "old", "refresh_token": "r",
            "expired": (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
        }]
    cfg.update(_setup)

    calls = []
    import httpx as _httpx
    orig_post = m["oauth_manager"].network.post_sync
    def fake_post(url, json=None, **kw):
        calls.append((url, dict(json or {})))
        if url == m["oauth_manager"].OAUTH_TOKEN_URL_LEGACY and "scope" not in (json or {}):
            req = _httpx.Request("POST", url)
            return _httpx.Response(200, request=req, json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
            })
        req = _httpx.Request("POST", url)
        return _httpx.Response(400, request=req, json={"error": "invalid_request"})
    m["oauth_manager"].network.post_sync = fake_post
    try:
        outcomes = await m["oauth_manager"].proactive_refresh_once(refresh_threshold_seconds=600)
    finally:
        m["oauth_manager"].network.post_sync = orig_post

    assert outcomes["legacy@test.com"] == "refreshed", outcomes
    acc = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "legacy@test.com")
    assert acc["enabled"] is True
    assert acc["disabled_reason"] is None
    assert acc["access_token"] == "new-access"
    assert acc["refresh_token"] == "new-refresh"
    assert calls == [(m["oauth_manager"].OAUTH_TOKEN_URL_LEGACY, {
        "grant_type": "refresh_token",
        "refresh_token": "r",
        "client_id": m["oauth_manager"].OAUTH_CLIENT_ID,
    })]
    print("  [PASS] Claude refresh compat: legacy no-scope account uses legacy endpoint")


async def test_claude_refresh_400_invalid_request_does_not_disable(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    def _setup(c):
        c.setdefault("oauth", {})["mockMode"] = False
        c["oauthAccounts"] = [{
            "email": "compat-fail@test.com",
            "provider": "claude",
            "access_token": "old", "refresh_token": "r",
            "scopes": "user:profile",
            "expired": (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
        }]
    cfg.update(_setup)

    import httpx as _httpx
    orig_post = m["oauth_manager"].network.post_sync
    def response_400_invalid_request(url, json=None, **kw):
        req = _httpx.Request("POST", url)
        return _httpx.Response(400, request=req, json={"error": "invalid_request"})
    m["oauth_manager"].network.post_sync = response_400_invalid_request
    try:
        outcomes = await m["oauth_manager"].proactive_refresh_once(refresh_threshold_seconds=600)
    finally:
        m["oauth_manager"].network.post_sync = orig_post

    assert outcomes["compat-fail@test.com"].startswith("failed:claude_refresh_token_failed"), outcomes
    acc = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "compat-fail@test.com")
    assert acc["enabled"] is True
    assert acc["disabled_reason"] is None
    m["notifier"].wait_drain(2.0)
    assert any("账号未自动禁用" in msg for msg in collector.messages), collector.messages
    print("  [PASS] Claude refresh compat: 400 invalid_request does not mark auth_error")


# ─── OAuth 配额监控 ──────────────────────────────────────────────

def _fake_usage(util_percent: float, resets_at_future_seconds: int = 3600):
    """构造一个 Anthropic /oauth/usage 响应，所有窗口利用率相同。

    2026-04-20: Anthropic /api/oauth/usage JSON body 的 utilization 就是 0..100
    百分比，直接传入 util_percent 即可，不再除以 100。
    """
    reset = (datetime.now(timezone.utc) + timedelta(seconds=resets_at_future_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    util = float(util_percent)  # 直接透传 0..100 百分比
    return {
        "five_hour": {"utilization": util, "resets_at": reset},
        "seven_day": {"utilization": util, "resets_at": reset},
        "seven_day_sonnet": {"utilization": util, "resets_at": reset},
        "seven_day_opus": {"utilization": util, "resets_at": reset},
        "extra_usage": {"is_enabled": False},
    }


async def test_quota_monitor_disables_high_util(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    def _setup(c):
        c["oauthAccounts"] = [{
            "email": "high@test.com",
            "access_token": "t", "refresh_token": "r",
            "expired": "2099-01-01T00:00:00Z",
            "enabled": True, "disabled_reason": None, "disabled_until": None, "models": [],
        }]
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 95
    cfg.update(_setup)

    # 猴补 fetch_usage 返回高利用率
    orig_fetch = m["oauth_manager"].fetch_usage
    async def fake_fetch(email):
        return _fake_usage(97)
    m["oauth_manager"].fetch_usage = fake_fetch
    try:
        outcomes = await m["oauth_manager"].quota_monitor_once()
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch

    assert outcomes["high@test.com"].startswith("disabled_quota:"), outcomes
    acc = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "high@test.com")
    assert acc["disabled_reason"] == "quota"
    assert acc["disabled_until"]
    m["notifier"].wait_drain(2.0)
    assert any(("quota disabled" in msg.lower() or "配额已用尽" in msg)
               for msg in collector.messages), collector.messages
    print("  [PASS] quota_monitor disables account with util=97%")


async def test_quota_monitor_resumes_after_reset(m):
    _reset(m)
    cfg = m["config"]
    collector = NotifyCollector()
    m["notifier"].set_handler(collector)

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def _setup(c):
        c["oauthAccounts"] = [{
            "email": "resume@test.com",
            "access_token": "t", "refresh_token": "r",
            "expired": "2099-01-01T00:00:00Z",
            "enabled": False, "disabled_reason": "quota", "disabled_until": past,
            "models": [],
        }]
    cfg.update(_setup)

    orig_fetch = m["oauth_manager"].fetch_usage
    async def fake_fetch(email):
        return _fake_usage(50)   # 低利用率
    m["oauth_manager"].fetch_usage = fake_fetch
    try:
        outcomes = await m["oauth_manager"].quota_monitor_once()
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch

    assert outcomes["resume@test.com"] == "resumed", outcomes
    acc = next(a for a in m["config"].get()["oauthAccounts"] if a["email"] == "resume@test.com")
    assert acc["enabled"] is True
    assert acc["disabled_reason"] is None
    m["notifier"].wait_drain(2.0)
    assert any(("resumed" in msg.lower() or "已恢复" in msg)
               for msg in collector.messages), collector.messages
    print("  [PASS] quota_monitor resumes account after reset")


async def test_quota_monitor_skips_user_and_auth_error(m):
    _reset(m)
    cfg = m["config"]

    def _setup(c):
        c["oauthAccounts"] = [
            {
                "email": "user@test.com",
                "access_token": "t", "refresh_token": "r",
                "expired": "2099-01-01T00:00:00Z",
                "enabled": False, "disabled_reason": "user", "disabled_until": None,
                "models": [],
            },
            {
                "email": "autherr@test.com",
                "access_token": "t", "refresh_token": "r",
                "expired": "2099-01-01T00:00:00Z",
                "enabled": False, "disabled_reason": "auth_error", "disabled_until": None,
                "models": [],
            },
        ]
    cfg.update(_setup)

    async def must_not_call(email):
        raise AssertionError("fetch_usage should not be called for user/auth_error accounts")

    orig = m["oauth_manager"].fetch_usage
    m["oauth_manager"].fetch_usage = must_not_call
    try:
        outcomes = await m["oauth_manager"].quota_monitor_once()
    finally:
        m["oauth_manager"].fetch_usage = orig

    assert outcomes["user@test.com"].startswith("skipped:user"), outcomes
    assert outcomes["autherr@test.com"].startswith("skipped:auth_error"), outcomes
    print("  [PASS] quota_monitor skips user/auth_error accounts")


# ─── main ────────────────────────────────────────────────────────

async def amain():
    m = _import_modules()
    m["state_db"].init()
    orig = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_probe_api_success_and_failure,
        test_probe_oauth_skipped,
        test_recovery_run_once,
        test_recovery_skips_active_long_lived_quota_cooldown,
        test_proactive_refresh_triggers_near_expiry,
        test_proactive_refresh_network_failure_does_not_mark_auth_error,
        test_proactive_refresh_401_marks_auth_error,
        test_claude_refresh_compat_falls_back_legacy_no_scope,
        test_claude_refresh_400_invalid_request_does_not_disable,
        test_quota_monitor_disables_high_util,
        test_quota_monitor_resumes_after_reset,
        test_quota_monitor_skips_user_and_auth_error,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                await t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig)
        m["config"].update(_restore)
        m["notifier"].set_handler(None)
        _reset(m)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
