"""统一 OAuth 用量机制 + 响应头超限自动禁用测试（2026-04-20）。

覆盖：
  A. fetch_usage 按 provider 分派：
     - Claude 走 /api/oauth/usage（_usage_sync）
     - OpenAI 主动刷新走 ChatGPT wham/usage
     - OpenAI 响应头实时额度仍由 failover 被动采样保存
     - 删除账户级联清历史 openai probe 桶
  B. quota_monitor_once 对 OpenAI 账号不再 skip，走统一路径
  C. 响应头超限自动禁用：
     - Anthropic surpassed-threshold=true → set_disabled_by_quota
     - Anthropic util>=1.0 → set_disabled_by_quota
     - 已 disabled 的账号不重复触发
     - auth_error / user 禁用不被覆盖
     - OpenAI primary/secondary used_percent >= threshold → 禁用

运行：./venv/bin/python -m src.tests.test_unified_usage_and_auto_disable
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import sys
import time
import traceback


def _import_modules():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_manager, state_db, failover
    from src.channel import oauth_channel, openai_oauth_channel, registry
    return {
        "config": config,
        "oauth_manager": oauth_manager,
        "state_db": state_db,
        "failover": failover,
        "OAuthChannel": oauth_channel.OAuthChannel,
        "OpenAIOAuthChannel": openai_oauth_channel.OpenAIOAuthChannel,
        "registry": registry,
    }


def _setup(m):
    state_db = m["state_db"]
    state_db.init()
    conn = state_db._get_conn()
    conn.execute("DELETE FROM oauth_quota_cache")
    conn.execute("DELETE FROM performance_stats")
    conn.execute("DELETE FROM channel_errors")
    conn.commit()

    def clear_accounts(c):
        c["oauthAccounts"] = []
        oc = c.setdefault("oauth", {})
        oc["mockMode"] = True
        # 默认阈值
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 95
    m["config"].update(clear_accounts)
    m["oauth_manager"]._refresh_locks.clear()
    m["failover"]._codex_snapshot_last.clear()
    m["failover"]._anthropic_snapshot_last.clear()
    m["oauth_manager"]._OPENAI_PROBE_LAST.clear()


def _add_openai(m, email="o@openai.test", plan_type="plus"):
    m["oauth_manager"].add_account({
        "email": email, "provider": "openai",
        "access_token": "o-at", "refresh_token": "o-rt",
        "chatgpt_account_id": "acct-123",
        "organization_id": "org-x",
        "plan_type": plan_type,
    })


def _add_claude(m, email="c@claude.test"):
    m["oauth_manager"].add_account({
        "email": email, "provider": "claude",
        "access_token": "c-at", "refresh_token": "c-rt",
    })


class _FakeResp:
    def __init__(self, headers: dict):
        self.headers = dict(headers)


# ==============================================================
# A. fetch_usage 按 provider 分派
# ==============================================================

def test_fetch_usage_claude_goes_to_api(m):
    """Claude 账号 fetch_usage → 走 _usage_sync，返回 mock 结构。"""
    _setup(m)
    _add_claude(m, "ca@c.io")
    usage = asyncio.run(m["oauth_manager"].fetch_usage("claude:ca@c.io"))
    assert "five_hour" in usage and "seven_day" in usage
    # mockMode 默认 0.0 util
    assert usage["five_hour"]["utilization"] == 0.0
    print("  [PASS] fetch_usage(claude): calls _usage_sync, returns API structure")


def test_fetch_usage_openai_goes_to_wham(m):
    """OpenAI 账号主动 fetch_usage → ChatGPT wham/usage，不发 Codex probe。"""
    _setup(m)
    _add_openai(m, "oa@o.io")
    m["registry"].rebuild_from_config()
    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:oa@o.io:acct-123"))
    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 3.0
    assert usage.get("openai", {}).get("source") == "wham_usage"
    assert "openai:oa@o.io:acct-123" not in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage(openai): calls wham/usage, returns normalized structure")


def test_fetch_usage_openai_ignores_passive_fresh_for_active_refresh(m):
    """OpenAI 主动 fetch_usage 即使有新鲜响应头缓存，也应拉 wham 作为主动额度源。"""
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
    """quota_monitor_once 现在对 OpenAI 账号也走流程（不再 skip）。"""
    _setup(m)
    _add_openai(m, "mon@o.io")
    _add_claude(m, "mon@c.io")
    m["registry"].rebuild_from_config()

    outcomes = asyncio.run(m["oauth_manager"].quota_monitor_once())
    # 两个账号都应被处理，不再出现 "skipped:openai_uses_headers"
    openai_outcome = outcomes.get("mon@o.io")
    claude_outcome = outcomes.get("mon@c.io")
    assert openai_outcome is not None
    assert not openai_outcome.startswith("skipped:openai_uses_headers"), openai_outcome
    assert claude_outcome is not None
    print(f"  [PASS] quota_monitor_once: processes both (openai={openai_outcome}, claude={claude_outcome})")


# ==============================================================
# C. 响应头超限自动禁用 — Anthropic
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


def test_anthropic_auto_disable_idempotent_for_already_disabled(m):
    """已 disabled_reason="quota" 的账号不重复触发（避免 disabled_until 被覆盖）。"""
    _setup(m)
    _add_claude(m, "dq@c.io")
    # 预置：已经 disabled_reason=quota，disabled_until=一个固定值
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
# C2. 响应头超限自动禁用 — OpenAI
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


def test_openai_quota_disabled_waits_until_disabled_until_before_resume(m):
    _setup(m)
    _add_openai(m, "future@o.io")
    ak = "openai:future@o.io:acct-123"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    m["oauth_manager"].set_disabled_by_quota(ak, future)

    result = m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {
        "five_hour": {"utilization": 10, "resets_at": None},
        "seven_day": {"utilization": 20, "resets_at": None},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    })

    acc_after = m["oauth_manager"].get_account(ak)
    assert result["action"] == "quota_waiting_reset", result
    assert acc_after["disabled_reason"] == "quota"
    assert acc_after["enabled"] is False
    print("  [PASS] openai: quota-disabled account waits for disabled_until before resume")


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
        # C. Anthropic 自动禁用
        test_anthropic_auto_disable_surpassed_threshold,
        test_anthropic_auto_disable_util_ge_one,
        test_anthropic_no_auto_disable_when_below_limit,
        test_anthropic_auto_disable_idempotent_for_already_disabled,
        test_anthropic_auth_error_not_touched,
        # C2. OpenAI 自动禁用
        test_openai_auto_disable_primary_over_threshold,
        test_openai_no_auto_disable_below_threshold,
        test_openai_auto_disable_respects_custom_threshold,
        test_openai_user_disabled_not_touched,
        test_openai_quota_disabled_not_resumed_from_unknown_usage,
        test_openai_quota_disabled_waits_until_disabled_until_before_resume,
        test_openai_quota_disabled_resumes_after_reset_with_fresh_low_usage,
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
