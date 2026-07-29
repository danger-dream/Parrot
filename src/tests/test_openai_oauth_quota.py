"""OpenAI Codex 限额响应头路径（Commit 3）测试。

覆盖：
  - state_db.quota_save_openai_snapshot 写入字段齐全（原始 codex_* + 归一化
    five_hour_* / seven_day_* + reset_at ISO）
  - failover._maybe_record_codex_snapshot：
      * 非 OpenAIOAuthChannel 直接跳过
      * 有 x-codex-* 头时触发一次写入
      * 30s 节流窗口内重复调用不再写
      * 响应头无 codex 字段时不写
  - oauth_menu 详情页对 provider=openai 账户的展示
      （provider 行 / 5h/7d 归一化展示 / refresh_usage 友好提示）
  - status_menu._quota_warnings 对 openai 账户追加 🅾 标记

用 HTTPX Response 的 mock 对象代替真实网络。
"""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sys
import time


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = "1"
    from src import config, oauth_manager, state_db, failover
    from src.channel import registry
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    from src.channel.oauth_channel import OAuthChannel
    from src.oauth import openai as openai_provider
    from src.openai.channel.registration import register_factories
    from src.telegram import states, ui
    from src.telegram.menus import oauth_menu, status_menu
    register_factories()
    return {
        "config": config, "oauth_manager": oauth_manager, "state_db": state_db,
        "failover": failover,
        "registry": registry,
        "OpenAIOAuthChannel": OpenAIOAuthChannel,
        "OAuthChannel": OAuthChannel,
        "openai_provider": openai_provider,
        "states": states, "ui": ui,
        "oauth_menu": oauth_menu, "status_menu": status_menu,
    }


def _setup(m):
    m["state_db"].init()
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
    m["config"].update(_reset)
    for row in m["state_db"].quota_load_all():
        m["state_db"].quota_delete(row.get("account_key") or row["email"])
    # 清 failover / UI 的跨 test 状态。
    m["failover"]._codex_snapshot_last.clear()
    m["states"].clear_all()
    # UI list/detail helpers schedule daemon background refreshes in production. In
    # this standalone integration test they race with the next case's isolated
    # config/state, so no-op the schedulers and clear in-flight buckets.
    if "oauth_menu" in m:
        m["oauth_menu"]._schedule_oauth_cache_refresh_for_ui = lambda *a, **kw: None
        m["oauth_menu"]._schedule_openai_metadata_for_ui = lambda *a, **kw: None
        m["oauth_menu"]._BACKGROUND_REFRESH_INFLIGHT.clear()
        m["oauth_menu"]._METADATA_REFRESH_INFLIGHT.clear()


def _add_openai(m, email="q@openai.test"):
    m["oauth_manager"].add_account({
        "email": email,
        "provider": "openai",
        "access_token": "at-x", "refresh_token": "rt-x",
        "id_token": "h.p.s", "chatgpt_account_id": f"acct-{email}",
        "plan_type": "plus",
    })


class _MockResp:
    def __init__(self, headers: dict):
        self.headers = headers


# ─── state_db write ───────────────────────────────────────────────

def test_quota_save_openai_snapshot_writes_all_columns(m):
    _setup(m)
    _add_openai(m, "q1@openai.test")
    email = "q1@openai.test"
    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "42.5",
        "x-codex-primary-reset-after-seconds": "3600",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "17",
        "x-codex-secondary-window-minutes": "300",
        "x-codex-secondary-reset-after-seconds": "180",
        "x-codex-primary-over-secondary-limit-percent": "5.5",
    })
    assert snap is not None
    norm = m["openai_provider"].normalize_codex_snapshot(snap)
    m["state_db"].quota_save_openai_snapshot(email, snap, norm)
    row = m["state_db"].quota_load(email)
    assert row is not None, "row not persisted"
    # 原始列
    assert row["codex_primary_used_pct"] == 42.5
    assert row["codex_primary_window_min"] == 10080
    assert row["codex_secondary_used_pct"] == 17.0
    assert row["codex_secondary_window_min"] == 300
    assert row["codex_primary_over_secondary_pct"] == 5.5
    # 归一化列：primary window 大 → 7d；secondary window 小 → 5h
    assert row["seven_day_util"] == 42.5
    assert row["five_hour_util"] == 17.0
    # reset_at ISO：五小时重置=180s 后，七日=3600s 后
    assert row["five_hour_reset"] and row["five_hour_reset"].endswith("Z")
    assert row["seven_day_reset"] and row["seven_day_reset"].endswith("Z")
    # Claude 专属字段应为 None
    assert row["sonnet_util"] is None
    assert row["opus_util"] is None
    assert row["extra_used"] is None
    print("  [PASS] quota_save_openai_snapshot writes all columns correctly")


def test_quota_save_auto_normalize(m):
    _setup(m)
    _add_openai(m, "q2@openai.test")
    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "10",
        "x-codex-primary-window-minutes": "300",
    })
    # 不传 normalized，由 quota_save_openai_snapshot 自动 normalize
    m["state_db"].quota_save_openai_snapshot("q2@openai.test", snap)
    row = m["state_db"].quota_load("q2@openai.test")
    assert row["five_hour_util"] == 10.0
    print("  [PASS] quota_save_openai_snapshot auto-normalizes when arg omitted")


# ─── failover hook ────────────────────────────────────────────────

def test_record_codex_snapshot_happy_path(m):
    _setup(m)
    _add_openai(m, "hook@openai.test")
    acc = m["oauth_manager"].get_account("openai:hook@openai.test:acct-hook@openai.test")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _MockResp({
        "x-codex-primary-used-percent": "35",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "12",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    row = m["state_db"].quota_load("openai:hook@openai.test:acct-hook@openai.test")
    assert row is not None and row["seven_day_util"] == 35.0
    print("  [PASS] _maybe_record_codex_snapshot writes on first call")


def test_record_codex_snapshot_throttle(m):
    _setup(m)
    _add_openai(m, "throttle@openai.test")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:throttle@openai.test:acct-throttle@openai.test"))
    resp1 = _MockResp({
        "x-codex-primary-used-percent": "10",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp1)
    row1 = m["state_db"].quota_load("openai:throttle@openai.test:acct-throttle@openai.test")
    assert row1["seven_day_util"] == 10.0
    # 30s 内第二次调用：即使头里值变了也不应覆盖
    resp2 = _MockResp({
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp2)
    row2 = m["state_db"].quota_load("openai:throttle@openai.test:acct-throttle@openai.test")
    assert row2["seven_day_util"] == 10.0, f"throttle failed, got {row2['seven_day_util']}"
    # 手动穿过节流窗口：回退上次写时间到 30s 之前
    m["failover"]._codex_snapshot_last["openai:throttle@openai.test:acct-throttle@openai.test"] = time.time() - 31
    m["failover"]._maybe_record_codex_snapshot(ch, resp2)
    row3 = m["state_db"].quota_load("openai:throttle@openai.test:acct-throttle@openai.test")
    assert row3["seven_day_util"] == 99.0, "expected write after throttle window"
    print("  [PASS] _maybe_record_codex_snapshot throttles within 30s")


def test_record_skip_non_openai_channel(m):
    _setup(m)
    m["oauth_manager"].add_account({
        "email": "c@claude.test", "provider": "claude",
        "access_token": "x", "refresh_token": "x",
    })
    acc = m["oauth_manager"].get_account("c@claude.test")
    ch = m["OAuthChannel"](acc, [])
    resp = _MockResp({"x-codex-primary-used-percent": "50"})
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    # 不应为 claude 账户写 codex 数据
    row = m["state_db"].quota_load("c@claude.test")
    if row:
        assert row.get("codex_primary_used_pct") is None
    print("  [PASS] _maybe_record_codex_snapshot skips non-OpenAI channels")


def test_record_skip_no_codex_headers(m):
    _setup(m)
    _add_openai(m, "noh@openai.test")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:noh@openai.test:acct-noh@openai.test"))
    resp = _MockResp({"content-type": "text/event-stream"})  # 无任何 x-codex-*
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    row = m["state_db"].quota_load("openai:noh@openai.test:acct-noh@openai.test")
    assert row is None, "should not write when headers carry no codex fields"
    print("  [PASS] _maybe_record_codex_snapshot skips when headers lack x-codex-*")


# ─── TG UI 展示 ──────────────────────────────────────────────────

class _UiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def last(self, method):
        matches = [d for mth, d in self.calls if mth == method]
        return matches[-1] if matches else None

    def clear(self):
        self.calls.clear()


def test_oauth_menu_detail_openai_shows_provider_and_codex_usage(m):
    _setup(m)
    _add_openai(m, "ui@openai.test")
    # 写一条 codex snapshot
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:ui@openai.test:acct-ui@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "77",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "22",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    rec = _UiRecorder()
    m["ui"].api = rec
    short = m["ui"].register_code("openai:ui@openai.test:acct-ui@openai.test")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last, "no editMessageText captured"
    text = last["text"]
    # provider 行：正文使用 Telegram custom emoji HTML + 明文 provider label。
    assert "tg-emoji" in text and "5861557411784957025" in text and "OpenAI" in text, text[:500]
    # plan 行
    assert "plus" in text
    # 归一化 5h / 7d
    assert "5h" in text and "77" in text
    # 不再展示低价值的 Codex 原始窗口块
    assert "Codex 原始窗口" not in text
    assert "primary (10080min)" not in text
    print("  [PASS] oauth_menu detail: provider tag + plan + normalized codex usage")


def test_oauth_menu_list_cached_openai_over_dynamic_threshold_auto_disables(m):
    """OAuth 列表按动态 disableThresholdPercent 收敛，不写死 95。"""
    _setup(m)
    def _set_threshold(c):
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 80
    m["config"].update(_set_threshold)
    _add_openai(m, "cached@openai.test")
    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-primary-reset-after-seconds": "3600",
        "x-codex-secondary-used-percent": "0",
        "x-codex-secondary-window-minutes": "300",
    })
    norm = m["openai_provider"].normalize_codex_snapshot(snap)
    m["state_db"].quota_save_openai_snapshot(
        "openai:cached@openai.test:acct-cached@openai.test", snap, norm, email="cached@openai.test",
    )

    rec = _UiRecorder()
    m["ui"].api = rec
    m["oauth_menu"].show(42, 100, "cb")

    acc = m["oauth_manager"].get_account("openai:cached@openai.test:acct-cached@openai.test")
    assert acc.get("disabled_reason") == "quota", acc
    text = rec.last("editMessageText")["text"]
    assert "🔒" in text
    assert "cached@openai.test" in text
    assert "85" in text
    print("  [PASS] oauth list: cached OpenAI value over dynamic threshold auto disables immediately")


def test_oauth_menu_list_cached_openai_below_dynamic_threshold_kept_enabled(m):
    """阈值调高后，缓存值低于当前阈值时不能误禁用。"""
    _setup(m)
    def _set_threshold(c):
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 90
    m["config"].update(_set_threshold)
    _add_openai(m, "below@openai.test")
    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-primary-reset-after-seconds": "3600",
        "x-codex-secondary-used-percent": "0",
        "x-codex-secondary-window-minutes": "300",
    })
    norm = m["openai_provider"].normalize_codex_snapshot(snap)
    m["state_db"].quota_save_openai_snapshot(
        "openai:below@openai.test:acct-below@openai.test", snap, norm, email="below@openai.test",
    )

    rec = _UiRecorder()
    m["ui"].api = rec
    m["oauth_menu"].show(42, 100, "cb")

    acc = m["oauth_manager"].get_account("openai:below@openai.test:acct-below@openai.test")
    assert acc.get("disabled_reason") is None, acc
    text = rec.last("editMessageText")["text"]
    assert "✅" in text
    assert "below@openai.test" in text
    assert "85" in text
    print("  [PASS] oauth list: cached OpenAI value below dynamic threshold stays enabled")


def test_oauth_menu_refresh_usage_openai_wham(m):
    """OpenAI 账户点“刷新用量” → 有效 access_token 直接 wham/usage，且不发 probe/强刷 token。"""
    _setup(m)
    _add_openai(m, "ru@openai.test")
    m["registry"].rebuild_from_config()

    def _stamp(c):
        for a in c["oauthAccounts"]:
            if a["email"] == "ru@openai.test":
                a["access_token"] = "OLD-AT"
                a["expired"] = "2099-01-01T00:00:00Z"
                a["last_refresh"] = "2026-01-01T00:00:00Z"
    m["config"].update(_stamp)
    m["registry"].rebuild_from_config()

    called = {"probe": 0}
    orig_probe = m["OpenAIOAuthChannel"].probe_usage
    async def _counting_probe(self, *args, **kwargs):
        called["probe"] += 1
        return await orig_probe(self, *args, **kwargs)

    rec = _UiRecorder()
    m["ui"].api = rec
    try:
        m["OpenAIOAuthChannel"].probe_usage = _counting_probe
        short = m["ui"].register_code("openai:ru@openai.test:acct-ru@openai.test")
        m["oauth_menu"].on_refresh_usage(42, 100, "cb", short)
    finally:
        m["OpenAIOAuthChannel"].probe_usage = orig_probe

    acc = m["oauth_manager"].get_account("openai:ru@openai.test:acct-ru@openai.test")
    assert acc["access_token"] == "OLD-AT"
    assert acc["expired"] == "2099-01-01T00:00:00Z"
    assert acc["last_refresh"] == "2026-01-01T00:00:00Z"
    assert called["probe"] == 0
    row = m["state_db"].quota_load("openai:ru@openai.test:acct-ru@openai.test")
    assert row is not None, "wham should have written quota cache"
    assert row["five_hour_util"] == 1.0
    assert row["seven_day_util"] == 3.0
    last = rec.last("editMessageText")
    assert last and "wham/usage" in last["text"], last.get("text", "")[:200]
    print("  [PASS] oauth_menu refresh_usage: openai → wham without force_refresh + re-render")


def test_oauth_menu_refresh_usage_openai_auto_disables_over_quota(m):
    """OpenAI 单账号刷新用量后，若 7d/5h >= 阈值，应立刻标 quota 禁用。"""
    _setup(m)
    _add_openai(m, "limit@openai.test")
    m["registry"].rebuild_from_config()

    orig_fetch = m["openai_provider"].fetch_wham_usage_sync
    def _usage_100(access_token: str, *, account_id: str | None = None):
        return {
            "five_hour": {"utilization": 0.0, "resets_at": "2026-01-01T00:01:00Z"},
            "seven_day": {"utilization": 100.0, "resets_at": "2026-01-01T01:00:00Z"},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "extra_usage": {"is_enabled": False},
            "openai": {"source": "wham_usage"},
        }

    rec = _UiRecorder()
    m["ui"].api = rec
    try:
        m["openai_provider"].fetch_wham_usage_sync = _usage_100
        short = m["ui"].register_code("openai:limit@openai.test:acct-limit@openai.test")
        m["oauth_menu"].on_refresh_usage(42, 100, "cb", short)
    finally:
        m["openai_provider"].fetch_wham_usage_sync = orig_fetch

    acc = m["oauth_manager"].get_account("openai:limit@openai.test:acct-limit@openai.test")
    assert acc.get("disabled_reason") == "quota", acc
    assert acc.get("enabled") is False
    assert acc.get("disabled_until"), acc
    last = rec.last("editMessageText")
    assert last and "配额禁用" in last["text"]
    assert "7d" in last["text"] and "100" in last["text"]
    print("  [PASS] oauth_menu refresh_usage(openai): wham over-quota auto disables")


def test_openai_quota_resume_respects_active_codex_snapshot(m):
    """WHAM 低于阈值时，仍要尊重未过期的 Codex 响应头超限快照。"""
    _setup(m)
    email = "edge@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")

    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "96",
        "x-codex-primary-reset-after-seconds": "3600",
        "x-codex-primary-window-minutes": "300",
        "x-codex-secondary-used-percent": "10",
        "x-codex-secondary-window-minutes": "10080",
    })
    assert snap is not None
    m["state_db"].quota_save_openai_snapshot(
        key, snap, m["openai_provider"].normalize_codex_snapshot(snap), email=email,
    )

    wham_below_threshold = {
        "five_hour": {"utilization": 1.0, "resets_at": "2099-01-01T00:01:00Z"},
        "seven_day": {"utilization": 1.0, "resets_at": "2099-01-01T01:00:00Z"},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "openai": {"source": "wham_usage"},
    }
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, wham_below_threshold, threshold=95, fresh=True,
    )
    acc = m["oauth_manager"].get_account(key)
    assert result["action"] == "still_over_quota", result
    assert result["any_over"] is True, result
    assert "codex primary 96%" in result["hit_windows"], result
    assert acc.get("disabled_reason") == "quota", acc
    assert acc.get("enabled") is False, acc
    print("  [PASS] OpenAI quota resume respects active Codex over-threshold snapshot")


def test_openai_quota_ignores_expired_codex_snapshot_missing_reset(m):
    """Codex over-threshold snapshots without reset headers must expire by short TTL."""
    _setup(m)
    email = "stale-codex@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2000-01-01T00:00:00Z")

    snap = m["openai_provider"].parse_rate_limit_headers({
        "x-codex-primary-used-percent": "96",
        "x-codex-primary-window-minutes": "300",
    })
    assert snap is not None
    m["state_db"].quota_save_openai_snapshot(
        key, snap, m["openai_provider"].normalize_codex_snapshot(snap), email=email,
    )
    old_ms = m["state_db"].now_ms() - 11 * 60 * 1000
    conn = m["state_db"]._get_conn()
    conn.execute(
        "UPDATE oauth_quota_cache SET fetched_at=?, last_passive_update_at=? WHERE account_key=?",
        (old_ms, old_ms, key),
    )
    conn.commit()

    wham_below_threshold = {
        "five_hour": {"utilization": 1.0, "resets_at": "2099-01-01T00:01:00Z"},
        "seven_day": {"utilization": 1.0, "resets_at": "2099-01-01T01:00:00Z"},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "openai": {"source": "wham_usage"},
    }
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, wham_below_threshold, threshold=95, fresh=True,
    )
    acc = m["oauth_manager"].get_account(key)
    assert result["action"] == "resumed", result
    assert acc.get("enabled") is True, acc
    assert acc.get("disabled_reason") is None, acc
    print("  [PASS] OpenAI quota ignores stale Codex over-threshold snapshot without reset")


def _save_codex_over_threshold_snapshot(m, key, email, *, reset_after_seconds="604800"):
    """Persist a Codex response-header snapshot that is over threshold."""
    headers = {
        "x-codex-primary-used-percent": "95",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "0",
        "x-codex-secondary-window-minutes": "300",
    }
    if reset_after_seconds is not None:
        headers["x-codex-primary-reset-after-seconds"] = reset_after_seconds
    snap = m["openai_provider"].parse_rate_limit_headers(headers)
    assert snap is not None
    m["state_db"].quota_save_openai_snapshot(
        key, snap, m["openai_provider"].normalize_codex_snapshot(snap), email=email,
    )


def _set_quota_timestamps(m, key, *, passive_ms, usage_ms):
    conn = m["state_db"]._get_conn()
    conn.execute(
        "UPDATE oauth_quota_cache SET last_passive_update_at=?, fetched_at=? "
        "WHERE account_key=?",
        (passive_ms, usage_ms, key),
    )
    conn.commit()


def test_openai_quota_resumes_when_fresh_wham_supersedes_codex_snapshot(m):
    """An early upstream reset must not stay blocked by an older header snapshot.

    Regression: the cached snapshot predicted its reset as
    ``last_passive_update_at + reset_after_seconds``, i.e. a full 7d window. When
    OpenAI resets the window early, WHAM reports 0% while that prediction is
    still in the future, so the account stayed quota-disabled for days.
    """
    _setup(m)
    email = "early-reset@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    _save_codex_over_threshold_snapshot(m, key, email)

    now = m["state_db"].now_ms()
    # Header snapshot sampled a day ago; WHAM refreshed just now.
    _set_quota_timestamps(m, key, passive_ms=now - 24 * 3600 * 1000, usage_ms=now)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, _low_wham(), threshold=95, fresh=True,
    )
    acc = m["oauth_manager"].get_account(key)
    assert result["action"] == "resumed", result
    assert result["any_over"] is False, result
    assert result["hit_windows"] == [], result
    assert acc.get("enabled") is True, acc
    assert acc.get("disabled_reason") is None, acc
    assert acc.get("disabled_until") is None, acc
    print("  [PASS] OpenAI quota resumes when fresh WHAM supersedes Codex snapshot")


def test_openai_quota_keeps_boundary_codex_snapshot_authoritative(m):
    """Near-simultaneous WHAM/Codex data must still let the snapshot win.

    This is the original guard: WHAM can briefly report low usage right after a
    Codex response header said the window is exhausted. Only clearly older
    snapshots may be discarded.
    """
    _setup(m)
    email = "boundary@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    _save_codex_over_threshold_snapshot(m, key, email)

    now = m["state_db"].now_ms()
    # WHAM is newer, but only by a minute: inside the boundary margin.
    _set_quota_timestamps(m, key, passive_ms=now - 60 * 1000, usage_ms=now)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, _low_wham(), threshold=95, fresh=True,
    )
    acc = m["oauth_manager"].get_account(key)
    assert result["action"] == "still_over_quota", result
    assert result["any_over"] is True, result
    assert "codex primary 95%" in result["hit_windows"], result
    assert acc.get("enabled") is False, acc
    assert acc.get("disabled_reason") == "quota", acc
    print("  [PASS] OpenAI quota keeps boundary Codex snapshot authoritative")


def test_codex_snapshot_superseded_helper_requires_both_timestamps(m):
    """Missing/sentinel timestamps must never discard the snapshot."""
    om = m["oauth_manager"]
    margin = om._CODEX_SNAPSHOT_SUPERSEDED_BY_USAGE_MS
    now = m["state_db"].now_ms()

    assert om._codex_snapshot_superseded_by_usage(
        {"last_passive_update_at": now - margin, "fetched_at": now}) is True
    assert om._codex_snapshot_superseded_by_usage(
        {"last_passive_update_at": now - margin + 1000, "fetched_at": now}) is False
    # fetched_at=0 is the "never actively synced" sentinel written by
    # quota_patch_passive; it must not look like fresh WHAM usage.
    assert om._codex_snapshot_superseded_by_usage(
        {"last_passive_update_at": now, "fetched_at": 0}) is False
    assert om._codex_snapshot_superseded_by_usage(
        {"last_passive_update_at": None, "fetched_at": now}) is False
    assert om._codex_snapshot_superseded_by_usage({}) is False
    # WHAM older than the header snapshot keeps the snapshot authoritative.
    assert om._codex_snapshot_superseded_by_usage(
        {"last_passive_update_at": now, "fetched_at": now - 10 * margin}) is False
    print("  [PASS] Codex snapshot supersede helper needs both fresh timestamps")


def test_quota_monitor_resumes_openai_after_early_upstream_reset(m):
    """End-to-end monitor tick: save fresh WHAM, then resume the account."""
    import asyncio

    _setup(m)
    email = "monitor-early-reset@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    _save_codex_over_threshold_snapshot(m, key, email)

    now = m["state_db"].now_ms()
    _set_quota_timestamps(m, key, passive_ms=now - 24 * 3600 * 1000, usage_ms=now - 24 * 3600 * 1000)

    async def _fake_fetch_usage(account_key):
        assert account_key == key, account_key
        return _low_wham()

    original = m["oauth_manager"].fetch_usage
    m["oauth_manager"].fetch_usage = _fake_fetch_usage
    try:
        out = asyncio.run(m["oauth_manager"].quota_monitor_once())
    finally:
        m["oauth_manager"].fetch_usage = original

    acc = m["oauth_manager"].get_account(key)
    assert out.get(email) == "resumed", out
    assert acc.get("enabled") is True, acc
    assert acc.get("disabled_reason") is None, acc
    row = m["state_db"].quota_load(key)
    # The monitor writes fresh WHAM usage; the old header snapshot columns stay
    # but no longer veto recovery.
    assert row["fetched_at"] > row["last_passive_update_at"], row
    print("  [PASS] quota_monitor resumes OpenAI account after early upstream reset")


def test_openai_quota_resume_uses_fresh_usage_over_future_disabled_until(m):
    """Fresh low usage must override an obsolete predicted disabled_until."""
    _setup(m)
    email = "cooldown@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")

    wham_below_threshold = {
        "five_hour": {"utilization": 1.0, "resets_at": "2099-01-01T00:01:00Z"},
        "seven_day": {"utilization": 1.0, "resets_at": "2099-01-01T01:00:00Z"},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "openai": {"source": "wham_usage"},
    }
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, wham_below_threshold, threshold=95, fresh=True,
    )
    acc = m["oauth_manager"].get_account(key)
    assert result["action"] == "resumed", result
    assert acc.get("disabled_reason") is None, acc
    assert acc.get("enabled") is True, acc
    assert acc.get("disabled_until") is None, acc
    print("  [PASS] OpenAI quota resume trusts fresh low usage over old disabled_until")


def test_quota_monitor_notifies_when_openai_quota_really_resumes(m):
    """Once disabled_until has expired and fresh WHAM usage is low, resume and notify."""
    _setup(m)
    email = "notify-resume@openai.test"
    key = f"openai:{email}:acct-{email}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2000-01-01T00:00:00Z")

    wham_below_threshold = {
        "five_hour": {"utilization": 1.0, "resets_at": "2000-01-01T00:01:00Z"},
        "seven_day": {"utilization": 1.0, "resets_at": "2000-01-01T01:00:00Z"},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "openai": {"source": "wham_usage"},
    }

    calls = []
    orig_fetch = m["oauth_manager"].fetch_usage
    orig_notify = m["oauth_manager"].notifier.throttled_notify_event_sync

    async def _fake_fetch_usage(account_key):
        assert account_key == key
        return wham_below_threshold

    def _fake_notify(event, throttle_key, message, **kwargs):
        calls.append((event, throttle_key, message, kwargs))

    import asyncio
    try:
        m["oauth_manager"].fetch_usage = _fake_fetch_usage
        m["oauth_manager"].notifier.throttled_notify_event_sync = _fake_notify
        out = asyncio.run(m["oauth_manager"].quota_monitor_once())
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch
        m["oauth_manager"].notifier.throttled_notify_event_sync = orig_notify

    acc = m["oauth_manager"].get_account(key)
    assert out[email] == "resumed", out
    assert acc.get("enabled") is True, acc
    assert acc.get("disabled_reason") is None, acc
    assert len(calls) == 1, calls
    assert calls[0][0] == "quota_resumed", calls
    assert calls[0][1] == f"quota_resumed:{key}", calls
    assert "OAuth 配额已恢复" in calls[0][2]
    assert "OpenAI · Plus" in calls[0][2]
    print("  [PASS] quota_monitor resumes and sends notification after cooldown expires")


def test_openai_plan_workspace_label_disambiguates_same_email(m):
    _setup(m)
    label = m["oauth_manager"].openai_plan_workspace_label({
        "plan_type": "team",
        "workspace_name": "us",
        "workspace_id": "d5611c34-1909-44f5-ac0a-9ef630e41c85",
    })
    assert label == "OpenAI · Team（us）"
    assert "d5611c34" not in label
    plus_label = m["oauth_manager"].openai_plan_workspace_label({
        "plan_type": "plus",
        "workspace_name": "Personal",
    })
    assert plus_label == "OpenAI · Plus"
    print("  [PASS] OpenAI notification label shows readable plan/workspace without raw id")


def test_oauth_menu_refresh_all_uses_wham_for_openai(m):
    """refresh_all 对 openai：有效 access_token 直接 wham/usage，不发 probe/强刷 token。"""
    _setup(m)
    m["oauth_manager"].add_account({
        "email": "c@claude.test", "provider": "claude",
        "access_token": "x", "refresh_token": "x",
    })
    _add_openai(m, "o@openai.test")
    m["registry"].rebuild_from_config()

    def _stamp(c):
        for a in c["oauthAccounts"]:
            if a["email"] == "o@openai.test":
                a["access_token"] = "OLD-OPENAI-AT"
                a["expired"] = "2099-01-01T00:00:00Z"
    m["config"].update(_stamp)
    m["registry"].rebuild_from_config()

    called = {"probe": 0}
    orig_probe = m["OpenAIOAuthChannel"].probe_usage
    async def _counting_probe(self, *args, **kwargs):
        called["probe"] += 1
        return await orig_probe(self, *args, **kwargs)

    rec = _UiRecorder()
    m["ui"].api = rec
    try:
        m["OpenAIOAuthChannel"].probe_usage = _counting_probe
        m["oauth_menu"].on_refresh_all(42, 100, "cb")
    finally:
        m["OpenAIOAuthChannel"].probe_usage = orig_probe

    sends = [d for mth, d in rec.calls if mth == "sendMessage"]
    assert sends, "expected progress messages"
    final_text = sends[-1]["text"]
    assert "c@claude.test" in final_text, final_text[:500]
    assert "o@openai.test" in final_text, final_text[:500]
    assert "刷新成功" in final_text, final_text[:500]
    assert "重置次数" in final_text, final_text[:500]
    assert "用量刷新完成" in final_text
    assert called["probe"] == 0
    acc = m["oauth_manager"].get_account("openai:o@openai.test:acct-o@openai.test")
    assert acc["access_token"] == "OLD-OPENAI-AT"
    row = m["state_db"].quota_load("openai:o@openai.test:acct-o@openai.test")
    assert row is not None and row["seven_day_util"] == 3.0
    print("  [PASS] oauth_menu refresh_all: openai wham without force_refresh")


def test_probe_usage_writes_snapshot_in_mock_mode(m):
    """OpenAIOAuthChannel.probe_usage mockMode 下合成 snapshot 写库，不发 HTTP。"""
    _setup(m)
    _add_openai(m, "probe@openai.test")
    m["registry"].rebuild_from_config()
    ch = m["registry"].get_channel("oauth:openai:probe@openai.test:acct-probe@openai.test")
    import asyncio
    res = asyncio.run(ch.probe_usage())
    assert res["ok"] is True, res
    assert res.get("reason") == "mock"
    row = m["state_db"].quota_load("openai:probe@openai.test:acct-probe@openai.test")
    assert row is not None
    assert row["codex_primary_used_pct"] == 3.0
    assert row["codex_secondary_used_pct"] == 1.0
    assert row["seven_day_util"] == 3.0     # primary window=10080 → 7d
    assert row["five_hour_util"] == 1.0     # secondary window=300 → 5h
    print("  [PASS] probe_usage(mockMode): synthesized snapshot, no real HTTP")


def test_delete_account_clears_codex_snapshot_throttle(m):
    """Commit 5 ⑥：account 删除时同步清 failover._codex_snapshot_last。"""
    _setup(m)
    _add_openai(m, "del@openai.test")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:del@openai.test:acct-del@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "5",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    assert "openai:del@openai.test:acct-del@openai.test" in m["failover"]._codex_snapshot_last
    m["oauth_manager"].delete_account("openai:del@openai.test:acct-del@openai.test")
    assert "openai:del@openai.test:acct-del@openai.test" not in m["failover"]._codex_snapshot_last
    print("  [PASS] delete_account: forget_codex_snapshot clears throttle bucket")


def test_on_refresh_token_openai_updates_usage_via_wham(m):
    """刷新 Token 后，OpenAI 也顺手用 wham/usage 更新 quota，且不发 probe。"""
    _setup(m)
    _add_openai(m, "rt@openai.test")
    m["registry"].rebuild_from_config()

    rec = _UiRecorder()
    m["ui"].api = rec

    called = {"fetch_usage": 0, "probe_usage": 0}
    orig_fetch = m["oauth_manager"].fetch_usage
    async def _counting_fetch(ak):
        called["fetch_usage"] += 1
        return await orig_fetch(ak)

    orig_probe = m["OpenAIOAuthChannel"].probe_usage
    async def _counting_probe(self, *args, **kwargs):
        called["probe_usage"] += 1
        return await orig_probe(self, *args, **kwargs)

    try:
        m["oauth_manager"].fetch_usage = _counting_fetch
        m["OpenAIOAuthChannel"].probe_usage = _counting_probe
        short = m["ui"].register_code("openai:rt@openai.test:acct-rt@openai.test")
        m["oauth_menu"].on_refresh_token(42, 100, "cb", short)
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch
        m["OpenAIOAuthChannel"].probe_usage = orig_probe

    assert called["fetch_usage"] >= 1
    assert called["probe_usage"] == 0
    row = m["state_db"].quota_load("openai:rt@openai.test:acct-rt@openai.test")
    assert row is not None and row["five_hour_util"] == 1.0
    last = rec.last("editMessageText")
    assert last and ("已刷新" in last["text"] or "Token" in last["text"])
    print("  [PASS] on_refresh_token(openai): wham usage updated, no probe")


def test_status_menu_quota_warnings_tags_openai(m):
    _setup(m)
    _add_openai(m, "warn@openai.test")
    # 写 warn 级别用量（85%）：高于 warnings 阈值 80%，低于 disable 阈值 95%
    # → 应被预警，但不被自动禁用（2026-04-20 响应头自动禁用接入后的正确边界）
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:warn@openai.test:acct-warn@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    # 确认没被禁用
    acc = m["oauth_manager"].get_account("openai:warn@openai.test:acct-warn@openai.test")
    assert acc.get("disabled_reason") is None, f"should not be auto-disabled at 85% (threshold 95%): {acc}"

    warnings = m["status_menu"]._quota_warnings(threshold_pct=80.0)
    assert warnings, "expected at least one warning"
    joined = "\n".join(warnings)
    assert "warn@openai.test" in joined
    # custom emoji + OpenAI 明文标记 openai 账户
    assert "tg-emoji" in joined and "5861557411784957025" in joined and "OpenAI" in joined, joined
    print("  [PASS] status_menu _quota_warnings: openai accounts get custom emoji tag")


def _low_wham(**openai_overrides):
    openai = {"source": "wham_usage", "allowed": True, "limit_reached": False}
    openai.update(openai_overrides)
    return {
        "five_hour": {"utilization": 1.0, "resets_at": "2099-01-01T00:01:00Z"},
        "seven_day": {"utilization": 2.0, "resets_at": "2099-01-01T01:00:00Z"},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
        "openai": openai,
    }


def _disk_account(config, email):
    with open(config.path(), "r", encoding="utf-8") as f:
        raw = json.load(f)
    return next(acc for acc in raw.get("oauthAccounts", []) if acc.get("email") == email)


def test_fresh_openai_recovery_clears_runtime_but_preserves_fresh_quota(m):
    """A real recovery must unblock routing without deleting the new WHAM row."""
    from src import cooldown

    _setup(m)
    email = "runtime-recovery@openai.test"
    key = f"openai:{email}:acct-{email}"
    channel_key = f"oauth:{key}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    usage = _low_wham()
    m["state_db"].quota_save(
        key, m["oauth_manager"].flatten_usage(usage), email=email,
    )
    cooldown.record_error(
        channel_key, "gpt-test", "429", cooldown_until=m["state_db"].now_ms() + 60_000,
    )
    assert cooldown.is_blocked(channel_key, "gpt-test")

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        key, usage, threshold=95, fresh=True,
    )

    assert result["action"] == "resumed", result
    assert result["runtime_state"]["cooldown_cleared"] is True, result
    assert result["runtime_state"]["required_state_cleared"] is True, result
    assert not cooldown.is_blocked(channel_key, "gpt-test")
    assert m["state_db"].error_load(channel_key, "gpt-test") is None
    assert m["state_db"].quota_load(key) is not None
    assert result["runtime_state"]["quota_cache_cleared"] is False

    # Simulated process reload must not resurrect the pre-recovery cooldown.
    cooldown._entries.clear()
    cooldown._initialized = False
    cooldown.init()
    assert not cooldown.is_blocked(channel_key, "gpt-test")
    print("  [PASS] fresh OpenAI recovery clears runtime and survives reload")


def test_fresh_openai_recovery_delete_failure_is_fail_closed_and_not_notified(
    m, monkeypatch,
):
    """A failed persistent cooldown delete must never look like recovery."""
    import asyncio
    from src import cooldown

    _setup(m)
    email = "runtime-delete-failure@openai.test"
    key = f"openai:{email}:acct-{email}"
    channel_key = f"oauth:{key}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    usage = _low_wham()
    cooldown.record_error(
        channel_key, "gpt-test", "429",
        cooldown_until=m["state_db"].now_ms() + 60_000,
    )
    assert cooldown.is_blocked(channel_key, "gpt-test")
    assert m["state_db"].error_load(channel_key, "gpt-test") is not None

    original_delete = m["state_db"].error_delete

    def fail_delete(_channel_key=None, _model=None):
        raise RuntimeError("synthetic state DB delete failure")

    async def fetch_usage(_account_key):
        return usage

    captured_results = []
    real_evaluate = m["oauth_manager"].evaluate_and_toggle_by_usage

    def capture_evaluate(*args, **kwargs):
        result = real_evaluate(*args, **kwargs)
        captured_results.append(result)
        return result

    notifications = []
    monkeypatch.setattr(m["state_db"], "error_delete", fail_delete)
    monkeypatch.setattr(m["oauth_manager"], "fetch_usage", fetch_usage)
    monkeypatch.setattr(
        m["oauth_manager"], "evaluate_and_toggle_by_usage", capture_evaluate,
    )
    monkeypatch.setattr(
        m["oauth_manager"].notifier,
        "throttled_notify_event_sync",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())
    result = captured_results[0]
    account = m["oauth_manager"].get_account(key)
    assert outcomes[email] == "resume_failed", outcomes
    assert result["action"] == "resume_failed", result
    assert result["error_code"] == "runtime_state_clear_failed", result
    assert result["runtime_state"]["cooldown_cleared"] is False, result
    assert result["runtime_state"]["required_state_cleared"] is False, result
    assert account["enabled"] is False and account["disabled_reason"] == "quota"
    assert notifications == []
    assert cooldown.is_blocked(channel_key, "gpt-test")

    # A simulated restart must reload the still-persisted cooldown and remain blocked.
    cooldown._entries.clear()
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.is_blocked(channel_key, "gpt-test")

    # Explicit cleanup keeps this standalone integration module isolated.
    monkeypatch.setattr(m["state_db"], "error_delete", original_delete)
    original_delete(channel_key, None)
    cooldown._entries.clear()
    print("  [PASS] failed cooldown delete stays disabled/current+reload/no notify")


def test_fresh_openai_recovery_enable_write_failure_stays_disabled_and_silent(
    m, monkeypatch,
):
    """Real set_enabled/config failure must not publish enable or recovery notices."""
    import asyncio
    from src import cooldown

    _setup(m)
    email = "runtime-enable-failure@openai.test"
    key = f"openai:{email}:acct-{email}"
    channel_key = f"oauth:{key}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    cooldown.record_error(
        channel_key, "gpt-test", "429",
        cooldown_until=m["state_db"].now_ms() + 60_000,
    )
    usage = _low_wham()

    async def fetch_usage(_account_key):
        return usage

    original_write = m["config"]._write_atomic

    def fail_enabling_candidate(candidate):
        target = next(
            acc for acc in candidate.get("oauthAccounts", [])
            if acc.get("email") == email
        )
        if target.get("enabled") is True:
            raise OSError("synthetic config enable persistence failure")
        return original_write(candidate)

    evaluated = []
    real_evaluate = m["oauth_manager"].evaluate_and_toggle_by_usage

    def capture_evaluate(*args, **kwargs):
        result = real_evaluate(*args, **kwargs)
        evaluated.append(result)
        return result

    notices = []
    with monkeypatch.context() as fault:
        fault.setattr(m["oauth_manager"], "fetch_usage", fetch_usage)
        fault.setattr(m["oauth_manager"], "evaluate_and_toggle_by_usage", capture_evaluate)
        fault.setattr(m["config"], "_write_atomic", fail_enabling_candidate)
        fault.setattr(
            cooldown.notifier, "notify_event",
            lambda event, *args, **kwargs: notices.append(event),
        )
        fault.setattr(
            m["oauth_manager"].notifier, "throttled_notify_event_sync",
            lambda event, *args, **kwargs: notices.append(event),
        )
        outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())

    result = evaluated[0]
    assert outcomes[email] == "resume_failed", outcomes
    assert result["action"] == "resume_failed", result
    assert result["error_code"] == "account_enable_failed", result
    assert result["runtime_state"]["required_state_cleared"] is True, result
    assert notices == []
    current = m["oauth_manager"].get_account(key)
    assert current["enabled"] is False and current["disabled_reason"] == "quota"
    disk = _disk_account(m["config"], email)
    assert disk["enabled"] is False and disk["disabled_reason"] == "quota"
    m["config"].reload()
    reloaded = m["oauth_manager"].get_account(key)
    assert reloaded["enabled"] is False and reloaded["disabled_reason"] == "quota"


def test_fresh_openai_recovery_notifies_once_only_after_durable_enable(m, monkeypatch):
    """Monitor success emits one quota_resumed after live and disk are enabled."""
    import asyncio
    from src import cooldown

    _setup(m)
    email = "runtime-enable-success@openai.test"
    key = f"openai:{email}:acct-{email}"
    channel_key = f"oauth:{key}"
    _add_openai(m, email)
    m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
    cooldown.record_error(
        channel_key, "gpt-test", "429",
        cooldown_until=m["state_db"].now_ms() + 60_000,
    )
    usage = _low_wham()

    async def fetch_usage(_account_key):
        return usage

    notices = []

    def capture_notice(event, *args, **kwargs):
        current = m["oauth_manager"].get_account(key)
        disk = _disk_account(m["config"], email)
        notices.append((event, current.get("enabled"), disk.get("enabled")))

    with monkeypatch.context() as fault:
        fault.setattr(m["oauth_manager"], "fetch_usage", fetch_usage)
        fault.setattr(cooldown.notifier, "notify_event", capture_notice)
        fault.setattr(
            m["oauth_manager"].notifier, "throttled_notify_event_sync", capture_notice,
        )
        outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())

    assert outcomes[email] == "resumed", outcomes
    assert notices == [("quota_resumed", True, True)], notices
    assert not cooldown.is_blocked(channel_key, "gpt-test")
    m["config"].reload()
    assert m["oauth_manager"].get_account(key)["enabled"] is True


def test_cooldown_record_then_clear_is_linearized_across_db_and_memory(m, monkeypatch):
    """clear cannot slip between record_error persistence and memory publish."""
    import threading
    from src import cooldown

    _setup(m)
    channel_key = "oauth:openai:record-before-clear@test:acct-rbc"
    model = "gpt-test"
    cooldown._entries.clear()
    original_save = m["state_db"].error_save
    original_delete = m["state_db"].error_delete
    original_delete(channel_key, None)
    save_entered = threading.Event()
    allow_save = threading.Event()
    delete_called = threading.Event()
    errors = []

    def delayed_save(*args, **kwargs):
        save_entered.set()
        assert allow_save.wait(2)
        return original_save(*args, **kwargs)

    def observed_delete(*args, **kwargs):
        delete_called.set()
        return original_delete(*args, **kwargs)

    def run(call):
        try:
            call()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(m["state_db"], "error_save", delayed_save)
    monkeypatch.setattr(m["state_db"], "error_delete", observed_delete)
    monkeypatch.setattr(cooldown.notifier, "notify_event", lambda *a, **kw: None)
    record = threading.Thread(target=lambda: run(lambda: cooldown.record_error(
        channel_key, model, "429",
        cooldown_until=m["state_db"].now_ms() + 60_000,
    )))
    record.start()
    assert save_entered.wait(2)
    clear = threading.Thread(target=lambda: run(lambda: cooldown.clear(channel_key)))
    clear.start()
    clear.join(0.05)
    assert clear.is_alive()
    assert not delete_called.is_set()
    allow_save.set()
    record.join(2)
    clear.join(2)
    assert not record.is_alive() and not clear.is_alive() and errors == []
    assert cooldown.get_state(channel_key, model) is None
    assert m["state_db"].error_load(channel_key, model) is None

    cooldown._entries.clear()
    cooldown._initialized = False
    cooldown.init()
    assert not cooldown.is_blocked(channel_key, model)
    monkeypatch.setattr(m["state_db"], "error_save", original_save)
    monkeypatch.setattr(m["state_db"], "error_delete", original_delete)
    print("  [PASS] record commit linearizes before clear; reload stays clear")


def test_cooldown_clear_then_new_record_is_consistent_after_reload(m, monkeypatch):
    """A genuinely new error after clear is present in both memory and DB."""
    import threading
    from src import cooldown

    _setup(m)
    channel_key = "oauth:openai:clear-before-record@test:acct-cbr"
    model = "gpt-test"
    cooldown._entries.clear()
    original_save = m["state_db"].error_save
    original_delete = m["state_db"].error_delete
    original_delete(channel_key, None)
    cooldown.record_error(
        channel_key, model, "old",
        cooldown_until=m["state_db"].now_ms() + 60_000,
    )
    delete_entered = threading.Event()
    allow_delete = threading.Event()
    save_called = threading.Event()
    errors = []

    def delayed_delete(*args, **kwargs):
        delete_entered.set()
        assert allow_delete.wait(2)
        return original_delete(*args, **kwargs)

    def observed_save(*args, **kwargs):
        save_called.set()
        return original_save(*args, **kwargs)

    def run(call):
        try:
            call()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(m["state_db"], "error_delete", delayed_delete)
    monkeypatch.setattr(m["state_db"], "error_save", observed_save)
    monkeypatch.setattr(cooldown.notifier, "notify_event", lambda *a, **kw: None)
    clear = threading.Thread(target=lambda: run(lambda: cooldown.clear(channel_key)))
    clear.start()
    assert delete_entered.wait(2)
    record = threading.Thread(target=lambda: run(lambda: cooldown.record_error(
        channel_key, model, "new",
        cooldown_until=m["state_db"].now_ms() + 120_000,
    )))
    record.start()
    record.join(0.05)
    assert record.is_alive()
    assert not save_called.is_set()
    allow_delete.set()
    clear.join(2)
    record.join(2)
    assert not clear.is_alive() and not record.is_alive() and errors == []
    assert cooldown.is_blocked(channel_key, model)
    assert m["state_db"].error_load(channel_key, model) is not None

    cooldown._entries.clear()
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.is_blocked(channel_key, model)
    monkeypatch.setattr(m["state_db"], "error_save", original_save)
    monkeypatch.setattr(m["state_db"], "error_delete", original_delete)
    original_delete(channel_key, None)
    cooldown._entries.clear()
    print("  [PASS] clear linearizes before a new record; reload keeps new block")


def test_cooldown_error_save_failure_does_not_publish_memory_state(m, monkeypatch):
    import pytest
    from src import cooldown

    _setup(m)
    channel_key = "oauth:openai:save-failure@test:acct-save"
    model = "gpt-test"
    cooldown._entries.clear()
    m["state_db"].error_delete(channel_key, None)

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("synthetic state DB save failure")

    monkeypatch.setattr(m["state_db"], "error_save", fail_save)
    with pytest.raises(RuntimeError, match="synthetic state DB save failure"):
        cooldown.record_error(
            channel_key, model, "429",
            cooldown_until=m["state_db"].now_ms() + 60_000,
        )
    assert cooldown.get_state(channel_key, model) is None
    assert m["state_db"].error_load(channel_key, model) is None
    print("  [PASS] failed error_save publishes neither DB nor memory state")


def test_non_genuine_openai_recovery_never_clears_runtime(m):
    """Stale, unknown and still-over-limit observations must leave cooldowns intact."""
    from src import cooldown

    scenarios = [
        ("stale", _low_wham(), False, "quota_stale_keep_disabled"),
        ("unknown", {"openai": {"source": "wham_usage"}}, True, "quota_unknown_keep_disabled"),
        (
            "over",
            {**_low_wham(), "five_hour": {"utilization": 99.0}},
            True,
            "still_over_quota",
        ),
    ]
    for suffix, usage, fresh, expected_action in scenarios:
        _setup(m)
        email = f"no-runtime-clear-{suffix}@openai.test"
        key = f"openai:{email}:acct-{email}"
        channel_key = f"oauth:{key}"
        _add_openai(m, email)
        m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
        cooldown.record_error(
            channel_key, "gpt-test", "429",
            cooldown_until=m["state_db"].now_ms() + 60_000,
        )

        result = m["oauth_manager"].evaluate_and_toggle_by_usage(
            key, usage, threshold=95, fresh=fresh,
        )
        assert result["action"] == expected_action, result
        assert cooldown.is_blocked(channel_key, "gpt-test"), (suffix, result)
        cooldown.clear(channel_key, model=None)
    print("  [PASS] stale/unknown/over-limit observations never clear runtime")


def test_explicit_wham_gate_keeps_quota_disabled_with_distinct_action(m):
    for field, value in (("allowed", False), ("limit_reached", True)):
        _setup(m)
        email = f"wham-{field}@openai.test"
        key = f"openai:{email}:acct-{email}"
        _add_openai(m, email)
        m["oauth_manager"].set_disabled_by_quota(key, "2099-01-01T00:00:00Z")
        usage = _low_wham(**{field: value})

        result = m["oauth_manager"].evaluate_and_toggle_by_usage(
            key, usage, threshold=95, fresh=True,
        )
        acc = m["oauth_manager"].get_account(key)
        assert result["action"] == "wham_limit_keep_disabled", result
        assert result["any_over"] is True, result
        assert acc["enabled"] is False and acc["disabled_reason"] == "quota", acc
    print("  [PASS] explicit WHAM gate keeps quota disabled with distinct action")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()

    import json
    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_quota_save_openai_snapshot_writes_all_columns,
        test_quota_save_auto_normalize,
        test_record_codex_snapshot_happy_path,
        test_record_codex_snapshot_throttle,
        test_record_skip_non_openai_channel,
        test_record_skip_no_codex_headers,
        test_oauth_menu_detail_openai_shows_provider_and_codex_usage,
        test_oauth_menu_list_cached_openai_over_dynamic_threshold_auto_disables,
        test_oauth_menu_list_cached_openai_below_dynamic_threshold_kept_enabled,
        test_oauth_menu_refresh_usage_openai_wham,
        test_oauth_menu_refresh_usage_openai_auto_disables_over_quota,
        test_openai_quota_resume_respects_active_codex_snapshot,
        test_openai_quota_ignores_expired_codex_snapshot_missing_reset,
        test_openai_quota_resumes_when_fresh_wham_supersedes_codex_snapshot,
        test_openai_quota_keeps_boundary_codex_snapshot_authoritative,
        test_codex_snapshot_superseded_helper_requires_both_timestamps,
        test_quota_monitor_resumes_openai_after_early_upstream_reset,
        test_openai_quota_resume_uses_fresh_usage_over_future_disabled_until,
        test_quota_monitor_notifies_when_openai_quota_really_resumes,
        test_openai_plan_workspace_label_disambiguates_same_email,
        test_oauth_menu_refresh_all_uses_wham_for_openai,
        test_probe_usage_writes_snapshot_in_mock_mode,
        test_delete_account_clears_codex_snapshot_throttle,
        test_on_refresh_token_openai_updates_usage_via_wham,
        test_status_menu_quota_warnings_tags_openai,
        test_fresh_openai_recovery_clears_runtime_but_preserves_fresh_quota,
        test_non_genuine_openai_recovery_never_clears_runtime,
        test_explicit_wham_gate_keeps_quota_disabled_with_distinct_action,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as exc:
                print(f"  [FAIL] {t.__name__}: {exc}")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  [ERR]  {t.__name__}: {exc}")
    finally:
        m["config"].update(lambda c: (c.clear(), c.update(orig_cfg)))

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
