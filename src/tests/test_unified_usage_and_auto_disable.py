"""统一 OAuth 用量机制 + 响应头超限自动禁用测试(2026-04-20)。

覆盖:
  A. fetch_usage 按 provider 分派:
     - Claude 走 /api/oauth/usage(_usage_sync)
     - OpenAI 主动刷新走 ChatGPT wham/usage
     - OpenAI 响应头实时额度仍由 failover 被动采样保存
     - 删除账户级联清历史 openai probe 桶
  B. quota_monitor_once 对 OpenAI 账号不再 skip,走统一路径
  C. 响应头超限自动禁用:
     - Anthropic surpassed-threshold=true → set_disabled_by_quota
     - Anthropic util>=1.0 → set_disabled_by_quota
     - 已 disabled 的账号不重复触发
     - auth_error / user 禁用不被覆盖
     - OpenAI primary/secondary used_percent >= threshold → 禁用

运行:./venv/bin/python -m src.tests.test_unified_usage_and_auto_disable
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import sys
import time
import traceback

import pytest


def _import_modules():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, cooldown, oauth_manager, scheduler, state_db, failover
    from src.channel import oauth_channel, openai_oauth_channel, registry
    return {
        "config": config,
        "cooldown": cooldown,
        "oauth_manager": oauth_manager,
        "scheduler": scheduler,
        "state_db": state_db,
        "failover": failover,
        "OAuthChannel": oauth_channel.OAuthChannel,
        "OpenAIOAuthChannel": openai_oauth_channel.OpenAIOAuthChannel,
        "registry": registry,
    }


def _setup(m):
    state_db = m["state_db"]
    state_db.init()
    for row in state_db.quota_load_all():
        state_db.quota_delete(row["account_key"])
    state_db.perf_delete()
    state_db.error_delete()

    def clear_accounts(c):
        c["oauthAccounts"] = []
        oc = c.setdefault("oauth", {})
        oc["mockMode"] = True
        # 默认阈值
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 95
    m["config"].update(clear_accounts)
    m["oauth_manager"]._refresh_locks.clear()
    m["failover"]._codex_snapshot_last.clear()
    m["failover"]._codex_snapshot_inflight.clear()
    m["failover"]._anthropic_snapshot_last.clear()
    m["failover"]._anthropic_snapshot_inflight.clear()
    m["oauth_manager"]._OPENAI_PROBE_LAST.clear()
    m["cooldown"].init()
    m["cooldown"].clear_all()


def _add_openai(m, email="o@openai.test", plan_type="plus"):
    m["oauth_manager"].add_account({
        "email": email, "provider": "openai",
        "access_token": "o-at", "refresh_token": "o-rt",
        "chatgpt_account_id": "acct-123",
        "organization_id": "org-x",
        "plan_type": plan_type,
    })


def _add_claude(m, email="c@claude.test", *, models=None):
    m["oauth_manager"].add_account({
        "email": email, "provider": "claude",
        "access_token": "c-at", "refresh_token": "c-rt",
        "models": list(models or []),
    })


class _FakeResp:
    def __init__(self, headers: dict):
        self.headers = dict(headers)


# ==============================================================
# A. fetch_usage 按 provider 分派
# ==============================================================

def test_fetch_usage_claude_goes_to_api(m):
    """Claude 账号 fetch_usage → 走 _usage_sync,返回 mock 结构。"""
    _setup(m)
    _add_claude(m, "ca@c.io")
    usage = asyncio.run(m["oauth_manager"].fetch_usage("claude:ca@c.io"))
    assert "five_hour" in usage and "seven_day" in usage
    # mockMode 默认 0.0 util
    assert usage["five_hour"]["utilization"] == 0.0
    print("  [PASS] fetch_usage(claude): calls _usage_sync, returns API structure")


def test_fetch_usage_openai_goes_to_wham(m):
    """OpenAI 账号主动 fetch_usage → ChatGPT wham/usage,不发 Codex probe。"""
    _setup(m)
    _add_openai(m, "oa@o.io")
    m["registry"].rebuild_from_config()
    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:oa@o.io:acct-123"))
    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 3.0
    assert usage.get("openai", {}).get("source") == "wham_usage"
    assert usage.get("openai", {}).get("rate_limit_reset_credits", {}).get("available_count") == 2
    assert "openai:oa@o.io:acct-123" not in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage(openai): calls wham/usage, returns normalized structure")


def test_fetch_usage_openai_ignores_passive_fresh_for_active_refresh(m):
    """OpenAI 主动 fetch_usage 即使有新鲜响应头缓存,也应拉 wham 作为主动额度源。"""
    _setup(m)
    _add_openai(m, "fresh@o.io")
    m["registry"].rebuild_from_config()
    m["state_db"].quota_patch_passive("openai:fresh@o.io:acct-123", {
        "five_hour_util": 10.0, "seven_day_util": 20.0,
    }, email="fresh@o.io")

    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:fresh@o.io:acct-123"))
    assert usage["five_hour"]["utilization"] == 1.0, usage
    assert usage["seven_day"]["utilization"] == 3.0, usage
    assert "openai:fresh@o.io:acct-123" not in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage(openai): active refresh uses wham even with passive cache")


def test_fetch_usage_openai_no_probe_throttle(m):
    """OpenAI 主动 wham 刷新不受历史 probe 节流桶影响。"""
    _setup(m)
    _add_openai(m, "thr@o.io")
    m["registry"].rebuild_from_config()
    m["oauth_manager"]._OPENAI_PROBE_LAST["openai:thr@o.io:acct-123"] = time.time()
    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:thr@o.io:acct-123"))
    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 3.0
    print("  [PASS] fetch_usage(openai): wham path ignores old probe throttle bucket")


def test_delete_account_clears_openai_probe_bucket(m):
    """账号删除时级联清 probe 桶。"""
    _setup(m)
    _add_openai(m, "del@o.io")
    m["registry"].rebuild_from_config()
    m["oauth_manager"]._OPENAI_PROBE_LAST["openai:del@o.io:acct-123"] = time.time()
    m["oauth_manager"].delete_account("openai:del@o.io:acct-123")
    assert "openai:del@o.io:acct-123" not in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] delete_account: openai probe bucket cleared")


# ==============================================================
# B. quota_monitor_once 不再 skip OpenAI
# ==============================================================

def test_quota_monitor_processes_openai_accounts(m):
    """quota_monitor_once 现在对 OpenAI 账号也走流程(不再 skip)。"""
    _setup(m)
    _add_openai(m, "mon@o.io")
    _add_claude(m, "mon@c.io")
    m["registry"].rebuild_from_config()

    outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())
    # 两个账号都应被处理,不再出现 "skipped:openai_uses_headers"
    openai_outcome = outcomes.get("mon@o.io")
    claude_outcome = outcomes.get("mon@c.io")
    assert openai_outcome is not None
    assert not openai_outcome.startswith("skipped:openai_uses_headers"), openai_outcome
    assert claude_outcome is not None
    print(f"  [PASS] quota_monitor_once: processes both (openai={openai_outcome}, claude={claude_outcome})")


def test_quota_monitor_resumes_openai_despite_old_passive_timestamp(m):
    """后台主动 wham/usage 刷新不应被旧响应头采样时间戳误判为 stale。"""
    _setup(m)
    _add_openai(m, "resume-passive@o.io")
    m["registry"].rebuild_from_config()
    ak = "openai:resume-passive@o.io:acct-123"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    m["oauth_manager"].set_disabled_by_quota(ak, past)

    m["state_db"].quota_patch_passive(ak, {
        "five_hour_util": 10.0,
        "seven_day_util": 20.0,
    }, email="resume-passive@o.io")
    old_ms = m["state_db"].now_ms() - 10 * 60 * 1000
    m["state_db"].quota_set_observation_times(
        ak, last_passive_update_at=old_ms,
    )

    outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())
    acc_after = m["oauth_manager"].get_account(ak)
    assert outcomes.get("resume-passive@o.io") == "resumed", outcomes
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    print("  [PASS] quota_monitor_once: old passive timestamp does not block OpenAI resume")


# ==============================================================
# C. 响应头超限自动禁用 - Anthropic
# ==============================================================

def test_anthropic_auto_disable_surpassed_threshold(m):
    _setup(m)
    _add_claude(m, "over@c.io")
    acc = m["oauth_manager"].get_account("claude:over@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "1.0",
        "anthropic-ratelimit-unified-5h-surpassed-threshold": "true",
        "anthropic-ratelimit-unified-5h-reset": str(int(time.time() + 7200)),
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("claude:over@c.io")
    assert acc_after["disabled_reason"] == "quota", acc_after
    assert acc_after["enabled"] is False
    assert acc_after.get("disabled_until") is not None
    print("  [PASS] anthropic: surpassed-threshold=true → auto-disabled with reset_at")


def test_anthropic_auto_disable_util_ge_one(m):
    _setup(m)
    _add_claude(m, "util1@c.io")
    acc = m["oauth_manager"].get_account("claude:util1@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp = _FakeResp({
        "anthropic-ratelimit-unified-7d-utilization": "1.0",
        "anthropic-ratelimit-unified-7d-reset": str(int(time.time() + 3600)),
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("claude:util1@c.io")
    assert acc_after["disabled_reason"] == "quota"
    print("  [PASS] anthropic: utilization>=1.0 → auto-disabled")


def test_anthropic_no_auto_disable_when_below_limit(m):
    _setup(m)
    _add_claude(m, "ok@c.io")
    acc = m["oauth_manager"].get_account("claude:ok@c.io")
    ch = m["OAuthChannel"](acc, [])
    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.5",
        "anthropic-ratelimit-unified-7d-utilization": "0.8",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("claude:ok@c.io")
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    print("  [PASS] anthropic: below limit → no auto-disable")


def test_claude_fable_scoped_quota_only_cools_fable_model(m):
    _setup(m)
    email = "fable-pool@c.io"
    account_key = f"claude:{email}"
    channel_key = f"oauth:{account_key}"
    _add_claude(
        m,
        email,
        models=["claude-fable-5", "claude-sonnet-4-6"],
    )
    m["registry"].rebuild_from_config()
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400))

    def usage(percent, *, active=True):
        return {
            "five_hour": {"utilization": 20, "resets_at": None},
            "seven_day": {"utilization": 50, "resets_at": None},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "limits": [{
                "kind": "weekly_scoped",
                "is_active": active,
                "percent": percent,
                "resets_at": reset,
                "scope": {"model": {"display_name": "Fable"}},
            }],
        }

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        account_key, usage(100), threshold=95, fresh=True,
    )
    account = m["oauth_manager"].get_account(account_key)
    assert result["action"] == "claude_fable_model_cooldown", result
    assert result["hit_windows"] == ["fable"] and result["cooled_models"] == 1
    assert account["enabled"] is True and account.get("disabled_reason") is None
    assert m["cooldown"].is_blocked(channel_key, "claude-fable-5")
    assert not m["cooldown"].is_blocked(channel_key, "claude-sonnet-4-6")

    fable_schedule = m["scheduler"].schedule(
        {"model": "claude-fable-5", "messages": [{"role": "user", "content": "hi"}]},
        "test-key", "127.0.0.1",
    )
    sonnet_schedule = m["scheduler"].schedule(
        {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]},
        "test-key", "127.0.0.1",
    )
    assert channel_key not in {
        ch.key for ch, _ in fable_schedule.candidates + fable_schedule.saturated
    }
    assert channel_key in {
        ch.key for ch, _ in sonnet_schedule.candidates + sonnet_schedule.saturated
    }

    recovered = m["oauth_manager"].evaluate_and_toggle_by_usage(
        account_key, usage(10), threshold=95, fresh=True,
    )
    assert recovered["action"] == "claude_fable_model_recovered", recovered
    assert not m["cooldown"].is_blocked(channel_key, "claude-fable-5")

    inactive = m["oauth_manager"].evaluate_and_toggle_by_usage(
        account_key, usage(100, active=False), threshold=95, fresh=True,
    )
    assert inactive["action"] == "kept_enabled", inactive
    assert not m["cooldown"].is_blocked(channel_key, "claude-fable-5")
    assert m["oauth_manager"].get_account(account_key)["enabled"] is True
    print("  [PASS] Claude Fable scoped quota cools only Fable and respects active scope")


def test_claude_account_quota_reset_excludes_fable_subcap(m):
    _setup(m)
    email = "fable-and-account@c.io"
    account_key = f"claude:{email}"
    _add_claude(
        m,
        email,
        models=["claude-fable-5", "claude-sonnet-4-6"],
    )
    account_reset = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600),
    )
    fable_reset = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400),
    )
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(
        account_key,
        {
            "five_hour": {"utilization": 96, "resets_at": account_reset},
            "seven_day": {"utilization": 50, "resets_at": None},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "limits": [{
                "kind": "weekly_scoped",
                "is_active": True,
                "percent": 100,
                "resets_at": fable_reset,
                "scope": {"model": {"display_name": "Fable"}},
            }],
        },
        threshold=95,
        fresh=True,
    )
    account = m["oauth_manager"].get_account(account_key)
    assert result["action"] == "disabled", result
    assert result["hit_windows"] == ["5h"], result
    assert result["disabled_until"] == account_reset, result
    assert account["enabled"] is False and account["disabled_reason"] == "quota"
    assert account["disabled_until"] == account_reset
    assert m["cooldown"].is_blocked(f"oauth:{account_key}", "claude-fable-5")
    print("  [PASS] Claude account reset excludes later Fable model sub-cap reset")


def test_anthropic_auto_disable_idempotent_for_already_disabled(m):
    """已 disabled_reason="quota" 的账号不重复触发(避免 disabled_until 被覆盖)。"""
    _setup(m)
    _add_claude(m, "dq@c.io")
    # 预置:已经 disabled_reason=quota,disabled_until=一个固定值
    m["oauth_manager"].set_disabled_by_quota("claude:dq@c.io", "2099-01-01T00:00:00Z")
    acc = m["oauth_manager"].get_account("claude:dq@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "1.0",
        "anthropic-ratelimit-unified-5h-reset": str(int(time.time() + 1000)),
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("claude:dq@c.io")
    # disabled_until 不应被新的短时 reset 覆盖
    assert acc_after["disabled_until"] == "2099-01-01T00:00:00Z"
    print("  [PASS] anthropic: already-disabled quota acct not re-disabled (idempotent)")


def test_anthropic_auth_error_not_touched(m):
    """auth_error 禁用的账号不被响应头超限覆盖。"""
    _setup(m)
    _add_claude(m, "ae@c.io")
    m["oauth_manager"].set_enabled("claude:ae@c.io", False, reason="auth_error")
    acc = m["oauth_manager"].get_account("claude:ae@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "1.0",
        "anthropic-ratelimit-unified-5h-surpassed-threshold": "true",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("claude:ae@c.io")
    assert acc_after["disabled_reason"] == "auth_error", \
        "auth_error must not be overwritten by quota"
    print("  [PASS] anthropic: auth_error disabled reason preserved")


# ==============================================================
# C2. 响应头超限自动禁用 - OpenAI
# ==============================================================

def test_openai_auto_disable_primary_over_threshold(m):
    _setup(m)
    _add_openai(m, "opr@o.io")
    acc = m["oauth_manager"].get_account("openai:opr@o.io:acct-123")
    ch = m["OpenAIOAuthChannel"](acc)

    resp = _FakeResp({
        "x-codex-primary-used-percent": "98",
        "x-codex-primary-reset-after-seconds": "600",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "10",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("openai:opr@o.io:acct-123")
    assert acc_after["disabled_reason"] == "quota", acc_after
    assert acc_after["enabled"] is False
    print("  [PASS] openai: primary 98% (>=95) → auto-disabled")


def test_openai_auto_disable_survives_locked_quota_cache(m, monkeypatch):
    """A failed auxiliary snapshot write must not bypass realtime disable."""
    import sqlite3

    _setup(m)
    email = "locked-cache@o.io"
    account_key = f"openai:{email}:acct-123"
    _add_openai(m, email)
    acc = m["oauth_manager"].get_account(account_key)
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _FakeResp({
        "x-codex-primary-used-percent": "98",
        "x-codex-primary-reset-after-seconds": "600",
        "x-codex-primary-window-minutes": "10080",
    })
    attempts = []

    def locked(*_args, **_kwargs):
        attempts.append("write")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(m["state_db"], "quota_save_openai_snapshot", locked)
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    current = m["oauth_manager"].get_account(account_key)
    assert current["enabled"] is False, current
    assert current["disabled_reason"] == "quota", current
    assert account_key not in m["failover"]._codex_snapshot_last
    assert account_key not in m["failover"]._codex_snapshot_inflight

    # A failed write must not advance the 30s throttle; the next response
    # retries persistence immediately (disable remains idempotent).
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    assert attempts == ["write", "write"]


def test_anthropic_auto_disable_survives_locked_quota_cache(m, monkeypatch):
    """The Anthropic passive sampler follows the same fail-open cache rule."""
    import sqlite3

    _setup(m)
    email = "locked-cache@c.io"
    account_key = f"claude:{email}"
    _add_claude(m, email)
    acc = m["oauth_manager"].get_account(account_key)
    ch = m["OAuthChannel"](acc, [])
    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "1.0",
        "anthropic-ratelimit-unified-5h-surpassed-threshold": "true",
        "anthropic-ratelimit-unified-5h-reset": str(int(time.time() + 600)),
    })
    attempts = []

    def locked(*_args, **_kwargs):
        attempts.append("write")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(m["state_db"], "quota_patch_passive", locked)
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    current = m["oauth_manager"].get_account(account_key)
    assert current["enabled"] is False, current
    assert current["disabled_reason"] == "quota", current
    assert account_key not in m["failover"]._anthropic_snapshot_last
    assert account_key not in m["failover"]._anthropic_snapshot_inflight
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    assert attempts == ["write", "write"]


def test_openai_no_auto_disable_below_threshold(m):
    _setup(m)
    _add_openai(m, "ook@o.io")
    acc = m["oauth_manager"].get_account("openai:ook@o.io:acct-123")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _FakeResp({
        "x-codex-primary-used-percent": "50",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "20",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:ook@o.io:acct-123")
    assert acc_after.get("disabled_reason") is None
    print("  [PASS] openai: 50/20% → no auto-disable")


def test_openai_auto_disable_respects_custom_threshold(m):
    """disableThresholdPercent 配置项应被读取。"""
    _setup(m)
    # 把阈值改成 80
    def patch(c):
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 80
    m["config"].update(patch)

    _add_openai(m, "thresh@o.io")
    acc = m["oauth_manager"].get_account("openai:thresh@o.io:acct-123")
    ch = m["OpenAIOAuthChannel"](acc)
    # 85% > 80% → 应该禁用
    resp = _FakeResp({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:thresh@o.io:acct-123")
    assert acc_after["disabled_reason"] == "quota"
    print("  [PASS] openai: custom disableThresholdPercent=80 honored")


def test_openai_user_disabled_not_touched(m):
    """user 主动禁用的账号不被响应头超限覆盖。"""
    _setup(m)
    _add_openai(m, "ud@o.io")
    m["oauth_manager"].set_enabled("openai:ud@o.io:acct-123", False, reason="user")
    acc = m["oauth_manager"].get_account("openai:ud@o.io:acct-123")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _FakeResp({
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:ud@o.io:acct-123")
    assert acc_after["disabled_reason"] == "user"
    print("  [PASS] openai: user-disabled reason preserved")



def test_openai_quota_disabled_not_resumed_from_unknown_usage(m):
    _setup(m)
    _add_openai(m, "unknown@o.io")
    ak = "openai:unknown@o.io:acct-123"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    m["oauth_manager"].set_disabled_by_quota(ak, past)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {
        "five_hour": {},
        "seven_day": {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    })

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "quota_unknown_keep_disabled", result
    assert acc_after["disabled_reason"] == "quota"
    assert acc_after["enabled"] is False
    print("  [PASS] openai: quota-disabled account is not resumed from unknown usage")


def test_openai_cached_thirty_day_over_threshold_sets_disabled_until(m):
    _setup(m)
    _add_openai(m, "thirty-over@o.io")
    ak = "openai:thirty-over@o.io:acct-123"
    reset = "2026-07-19T21:23:07Z"
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 1.0,
        "seven_day_util": 2.0,
        "thirty_day_util": 99.0,
        "thirty_day_reset": reset,
    }, email="thirty-over@o.io")

    result = m["oauth_manager"].evaluate_and_toggle_by_cached_quota(ak, threshold=95)
    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "disabled", result
    assert result["hit_windows"] == ["30d"], result
    assert result["disabled_until"] == reset, result
    assert acc_after["disabled_reason"] == "quota"
    assert acc_after["disabled_until"] == reset
    print("  [PASS] openai: cached 30d over threshold carries disabled_until")


def test_openai_quota_disabled_resumes_from_fresh_low_usage_before_disabled_until(m):
    _setup(m)
    _add_openai(m, "future@o.io")
    ak = "openai:future@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {
        "five_hour": {"utilization": 10, "resets_at": None},
        "seven_day": {"utilization": 20, "resets_at": None},
        "openai": {"thirty_day": {"utilization": 1, "resets_at": "2026-07-19T21:23:07Z"}},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    }, fresh=True)

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "resumed", result
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    assert acc_after.get("disabled_until") is None
    print("  [PASS] openai: fresh low usage overrides obsolete disabled_until")


def test_openai_quota_disabled_keeps_waiting_on_stale_low_usage(m):
    _setup(m)
    _add_openai(m, "stale@o.io")
    ak = "openai:stale@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {
        "five_hour": {"utilization": 10, "resets_at": None},
        "seven_day": {"utilization": 20, "resets_at": None},
        "openai": {"thirty_day": {"utilization": 1, "resets_at": "2026-07-19T21:23:07Z"}},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    }, fresh=False)

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "quota_stale_keep_disabled", result
    assert acc_after["disabled_reason"] == "quota"
    assert acc_after["enabled"] is False
    print("  [PASS] openai: stale low usage does not resume quota-disabled account")


def test_openai_quota_disabled_resumes_after_reset_with_fresh_low_usage(m):
    _setup(m)
    _add_openai(m, "fresh@o.io")
    ak = "openai:fresh@o.io:acct-123"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
    m["oauth_manager"].set_disabled_by_quota(ak, past)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {
        "five_hour": {"utilization": 10, "resets_at": None},
        "seven_day": {"utilization": 20, "resets_at": None},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    }, fresh=True)

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "resumed", result
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    print("  [PASS] openai: quota-disabled account resumes after reset with fresh low usage")


def test_openai_official_reset_credit_clears_local_quota_after_upstream_success(m):
    _setup(m)
    _add_openai(m, "official-reset@o.io")
    ak = "openai:official-reset@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 99.0,
        "five_hour_reset": future,
        "seven_day_util": 20.0,
    }, email="official-reset@o.io")
    m["cooldown"].record_error(f"oauth:{ak}", "gpt-5-codex", "quota", cooldown_until=m["state_db"].now_ms() + 600_000)

    result = asyncio.run(m["oauth_manager"].redeem_openai_rate_limit_reset_credit(ak, idempotency_key="idem-1"))

    acc_after = m["oauth_manager"].get_account(ak)
    row = m["state_db"].quota_load(ak)
    assert result["outcome"] == "reset", result
    assert result.get("available_count") == 2, result
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    assert row is not None and row["five_hour_util"] == 1.0
    assert not m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")
    print("  [PASS] openai: official reset credit success clears local quota/cooldown and refetches usage")


def test_openai_official_reset_keeps_quota_disabled_when_fresh_usage_still_over(m):
    _setup(m)
    _add_openai(m, "still-over@o.io")
    ak = "openai:still-over@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["cooldown"].record_error(f"oauth:{ak}", "gpt-5-codex", "quota", cooldown_until=m["state_db"].now_ms() + 600_000)

    orig_fetch = m["oauth_manager"].fetch_usage
    async def _over_usage(_ak):
        return {
            "five_hour": {"utilization": 99, "resets_at": future},
            "seven_day": {"utilization": 20, "resets_at": None},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "extra_usage": {"is_enabled": False},
            "openai": {"rate_limit_reset_credits": {"available_count": 1}},
        }
    m["oauth_manager"].fetch_usage = _over_usage
    try:
        result = asyncio.run(m["oauth_manager"].redeem_openai_rate_limit_reset_credit(ak, idempotency_key="idem-over"))
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch

    acc_after = m["oauth_manager"].get_account(ak)
    row = m["state_db"].quota_load(ak)
    assert result["outcome"] == "reset", result
    assert result["quota_action"]["action"] == "still_over_quota", result
    assert acc_after["enabled"] is False and acc_after.get("disabled_reason") == "quota"
    assert row is not None and row["five_hour_util"] == 99.0
    assert m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")
    print("  [PASS] openai: official reset keeps quota-disabled when fresh usage still over threshold")


def test_openai_official_reset_keeps_quota_disabled_when_usage_refresh_fails(m):
    _setup(m)
    _add_openai(m, "refresh-fail@o.io")
    ak = "openai:refresh-fail@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["cooldown"].record_error(f"oauth:{ak}", "gpt-5-codex", "quota", cooldown_until=m["state_db"].now_ms() + 600_000)
    m["state_db"].quota_save(ak, {"fetched_at": m["state_db"].now_ms(), "five_hour_util": 99.0}, email="refresh-fail@o.io")

    orig_fetch = m["oauth_manager"].fetch_usage
    async def _boom(_ak):
        raise RuntimeError("usage down")
    m["oauth_manager"].fetch_usage = _boom
    try:
        result = asyncio.run(m["oauth_manager"].redeem_openai_rate_limit_reset_credit(ak, idempotency_key="idem-fail"))
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch

    acc_after = m["oauth_manager"].get_account(ak)
    row = m["state_db"].quota_load(ak)
    assert result["outcome"] == "reset", result
    assert result.get("refresh_error"), result
    assert result["quota_action"]["action"] == "refresh_failed_keep_disabled"
    assert acc_after["enabled"] is False and acc_after.get("disabled_reason") == "quota"
    assert row is not None and row["five_hour_util"] == 99.0
    assert m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")
    print("  [PASS] openai: official reset keeps quota-disabled when fresh usage refresh fails")


def test_openai_metadata_refresh_updates_plan_without_rotating_tokens(m):
    _setup(m)
    _add_openai(m, "plan@o.io", plan_type="plus")
    ak = "openai:plan@o.io:acct-123"
    before = m["oauth_manager"].get_account(ak)
    orig_fetch = m["oauth_manager"].openai_provider.fetch_accounts_check_sync
    def _fake_accounts_check(access_token, *, org_id=None, workspace_id=None, email=None):
        return {
            "workspace_id": "acct-123",
            "chatgpt_account_id": "acct-123",
            "organization_id": "org-x",
            "workspace_name": "Personal",
            "workspace_type": "personal",
            "plan_type": "pro",
            "subscription_expires_at": "2026-07-01T00:00:00Z",
            "email": "plan@o.io",
        }
    m["oauth_manager"].openai_provider.fetch_accounts_check_sync = _fake_accounts_check
    try:
        result = m["oauth_manager"].refresh_openai_metadata_sync(ak, force=True)
    finally:
        m["oauth_manager"].openai_provider.fetch_accounts_check_sync = orig_fetch

    after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "updated", result
    assert after["plan_type"] == "pro"
    assert after["subscription_expires_at"] == "2026-07-01T00:00:00Z"
    assert after["access_token"] == before["access_token"]
    assert after["refresh_token"] == before["refresh_token"]
    assert after["workspace_id"] == "acct-123"
    assert after.get("last_metadata_refresh")
    print("  [PASS] openai: metadata refresh updates plan without rotating tokens or changing identity")


def test_manual_reset_quota_clears_local_quota_cache_and_cooldown(m):
    _setup(m)
    _add_openai(m, "manual-reset@o.io")
    ak = "openai:manual-reset@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 99.0,
        "five_hour_reset": future,
        "seven_day_util": 20.0,
    }, email="manual-reset@o.io")
    m["cooldown"].record_error(f"oauth:{ak}", "gpt-5-codex", "quota", cooldown_until=m["state_db"].now_ms() + 600_000)
    assert m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")

    result = m["oauth_manager"].reset_quota(ak)

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "reset", result
    assert result["required_state_cleared"] is True, result
    assert acc_after.get("disabled_reason") is None
    assert acc_after["enabled"] is True
    assert m["state_db"].quota_load(ak) is None
    assert not m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")
    print("  [PASS] openai: manual reset clears local quota-disabled/cache/cooldown state")


def test_manual_reset_commits_required_clears_before_enable(m, monkeypatch):
    _setup(m)
    email = "manual-reset-order@o.io"
    _add_openai(m, email)
    ak = f"openai:{email}:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["state_db"].quota_save(
        ak, {"fetched_at": m["state_db"].now_ms(), "five_hour_util": 99.0},
        email=email,
    )
    m["cooldown"].record_error(
        f"oauth:{ak}", "gpt-5-codex", "quota",
        cooldown_until=m["state_db"].now_ms() + 600_000,
    )

    original_error_delete = m["state_db"].error_delete
    original_quota_delete = m["state_db"].quota_delete
    original_write = m["config"]._write_atomic
    order = []

    def observed_error_delete(*args, **kwargs):
        order.append("cooldown_delete")
        return original_error_delete(*args, **kwargs)

    def observed_quota_delete(*args, **kwargs):
        order.append("quota_delete")
        return original_quota_delete(*args, **kwargs)

    def observed_write(candidate):
        target = next(
            acc for acc in candidate.get("oauthAccounts", [])
            if acc.get("email") == email
        )
        if target.get("enabled") is True:
            order.append("enable_write")
        return original_write(candidate)

    with monkeypatch.context() as observed:
        observed.setattr(m["state_db"], "error_delete", observed_error_delete)
        observed.setattr(m["state_db"], "quota_delete", observed_quota_delete)
        observed.setattr(m["config"], "_write_atomic", observed_write)
        result = m["oauth_manager"].reset_quota(ak)

    assert result["action"] == "reset", result
    assert order == ["cooldown_delete", "quota_delete", "enable_write"], order


def _disk_account(config, email):
    with open(config.path(), "r", encoding="utf-8") as f:
        raw = json.load(f)
    return next(acc for acc in raw.get("oauthAccounts", []) if acc.get("email") == email)


@pytest.mark.parametrize(
    ("failure_point", "expected_error", "cooldown_remains", "quota_remains"),
    [
        ("error_delete", "runtime_state_clear_failed", True, False),
        ("quota_delete", "runtime_state_clear_failed", False, True),
        ("set_enabled", "account_enable_failed", False, False),
    ],
)
def test_manual_reset_quota_persistence_failures_are_fail_closed(
    m, monkeypatch, failure_point, expected_error, cooldown_remains, quota_remains,
):
    _setup(m)
    email = f"manual-reset-{failure_point}@o.io"
    _add_openai(m, email)
    ak = f"openai:{email}:acct-123"
    channel_key = f"oauth:{ak}"
    model = "gpt-5-codex"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)
    m["state_db"].quota_save(
        ak,
        {
            "fetched_at": m["state_db"].now_ms(),
            "five_hour_util": 99.0,
            "five_hour_reset": future,
            "seven_day_util": 20.0,
        },
        email=email,
    )
    m["cooldown"].record_error(
        channel_key, model, "quota",
        cooldown_until=m["state_db"].now_ms() + 600_000,
    )

    original_write = m["config"]._write_atomic
    notices = []
    monkeypatch.setattr(
        m["cooldown"].notifier, "notify_event",
        lambda event, *args, **kwargs: notices.append(event),
    )
    with monkeypatch.context() as fault:
        if failure_point == "error_delete":
            fault.setattr(
                m["state_db"], "error_delete",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("synthetic error_delete failure")
                ),
            )
        elif failure_point == "quota_delete":
            fault.setattr(
                m["state_db"], "quota_delete",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("synthetic quota_delete failure")
                ),
            )
        else:
            def fail_enabling_candidate(candidate):
                target = next(
                    acc for acc in candidate.get("oauthAccounts", [])
                    if acc.get("email") == email
                )
                if target.get("enabled") is True:
                    raise OSError("synthetic config enable persistence failure")
                return original_write(candidate)

            fault.setattr(m["config"], "_write_atomic", fail_enabling_candidate)

        result = m["oauth_manager"].reset_quota(ak)

    assert result["action"] == "reset_failed", result
    assert result["error_code"] == expected_error, result
    assert result["required_state_cleared"] is (failure_point == "set_enabled"), result
    assert notices == []
    current = m["oauth_manager"].get_account(ak)
    assert current["enabled"] is False and current["disabled_reason"] == "quota"
    disk = _disk_account(m["config"], email)
    assert disk["enabled"] is False and disk["disabled_reason"] == "quota"
    m["config"].reload()
    reloaded = m["oauth_manager"].get_account(ak)
    assert reloaded["enabled"] is False and reloaded["disabled_reason"] == "quota"
    assert m["cooldown"].is_blocked(channel_key, model) is cooldown_remains
    assert (m["state_db"].quota_load(ak) is not None) is quota_remains

# ==============================================================
# main
# ==============================================================

def main():
    m = _import_modules()
    tests = [
        # A. fetch_usage 统一门面
        test_fetch_usage_claude_goes_to_api,
        test_fetch_usage_openai_goes_to_wham,
        test_fetch_usage_openai_ignores_passive_fresh_for_active_refresh,
        test_fetch_usage_openai_no_probe_throttle,
        test_delete_account_clears_openai_probe_bucket,
        # B. quota_monitor_once 对齐
        test_quota_monitor_processes_openai_accounts,
        test_quota_monitor_resumes_openai_despite_old_passive_timestamp,
        # C. Anthropic 自动禁用
        test_anthropic_auto_disable_surpassed_threshold,
        test_anthropic_auto_disable_util_ge_one,
        test_anthropic_no_auto_disable_when_below_limit,
        test_claude_fable_scoped_quota_only_cools_fable_model,
        test_claude_account_quota_reset_excludes_fable_subcap,
        test_anthropic_auto_disable_idempotent_for_already_disabled,
        test_anthropic_auth_error_not_touched,
        # C2. OpenAI 自动禁用
        test_openai_auto_disable_primary_over_threshold,
        test_openai_no_auto_disable_below_threshold,
        test_openai_auto_disable_respects_custom_threshold,
        test_openai_user_disabled_not_touched,
        test_openai_quota_disabled_not_resumed_from_unknown_usage,
        test_openai_cached_thirty_day_over_threshold_sets_disabled_until,
        test_openai_quota_disabled_resumes_from_fresh_low_usage_before_disabled_until,
        test_openai_quota_disabled_keeps_waiting_on_stale_low_usage,
        test_openai_quota_disabled_resumes_after_reset_with_fresh_low_usage,
        test_openai_official_reset_credit_clears_local_quota_after_upstream_success,
        test_openai_official_reset_keeps_quota_disabled_when_fresh_usage_still_over,
        test_openai_official_reset_keeps_quota_disabled_when_usage_refresh_fails,
        test_openai_metadata_refresh_updates_plan_without_rotating_tokens,
        test_manual_reset_quota_clears_local_quota_cache_and_cooldown,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t(m)
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"  [ERR]  {t.__name__}:")
            traceback.print_exc()
    print(f"\nRESULT: {passed} / {passed + failed} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
