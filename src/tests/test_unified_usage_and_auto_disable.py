"""统一 OAuth 用量机制 + 响应头超限自动禁用测试（2026-04-20）。

覆盖：
  A. fetch_usage 按 provider 分派：
     - Claude 走 /api/oauth/usage（_usage_sync）
     - OpenAI 走 OpenAIOAuthChannel.probe_usage
     - OpenAI 响应头最近 5 分钟有被动采样 → fetch_usage 跳过 probe 零成本返回
     - OpenAI probe 节流桶（openaiProbeMinIntervalSeconds）
     - 删除账户级联清 openai probe 桶
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


def test_fetch_usage_openai_goes_to_probe(m):
    """OpenAI 账号 fetch_usage → 调 channel.probe_usage，内部合成 snapshot。"""
    _setup(m)
    _add_openai(m, "oa@o.io")
    m["registry"].rebuild_from_config()
    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:oa@o.io"))
    # mockMode probe 合成 primary=3% / secondary=1% → 7d=3 / 5h=1
    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 3.0
    # probe 标记被设置
    assert "openai:oa@o.io" in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage(openai): calls probe_usage, returns synthesized structure")


def test_fetch_usage_openai_skips_when_passive_fresh(m):
    """OpenAI：如果响应头被动采样 last_passive_update_at 在 5 分钟内 → 跳过 probe。"""
    _setup(m)
    _add_openai(m, "fresh@o.io")
    m["registry"].rebuild_from_config()

    # 模拟"刚被响应头采样过" → 写一行并把 last_passive_update_at 设成现在
    m["state_db"].quota_patch_passive("openai:fresh@o.io", {
        "five_hour_util": 10.0, "seven_day_util": 20.0,
    }, email="fresh@o.io")

    # 这时 fetch_usage 不应触发 probe（_OPENAI_PROBE_LAST 空 → 但 last_passive < 5min）
    usage = asyncio.run(m["oauth_manager"].fetch_usage("openai:fresh@o.io"))
    # 应返回被动采样合成结果，而不是 probe 的 1/3
    assert usage["five_hour"]["utilization"] == 10.0, usage
    assert usage["seven_day"]["utilization"] == 20.0, usage
    # probe 标记未被设置（证明没真正 probe）
    assert "openai:fresh@o.io" not in m["oauth_manager"]._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage(openai): skips probe when passive sample is fresh (<5min)")


def test_fetch_usage_openai_throttle_between_probes(m):
    """OpenAI probe 节流：同账号二次 fetch_usage 在最小间隔内应跳过 probe。"""
    _setup(m)
    _add_openai(m, "thr@o.io")
    m["registry"].rebuild_from_config()

    # 第一次：触发 probe
    usage1 = asyncio.run(m["oauth_manager"].fetch_usage("openai:thr@o.io"))
    assert usage1["five_hour"]["utilization"] == 1.0
    probe_time_1 = m["oauth_manager"]._OPENAI_PROBE_LAST["openai:thr@o.io"]

    # 把 last_passive 设为很久以前，强制 passive 不新鲜；但 probe 桶新鲜 → 应跳过
    conn = m["state_db"]._get_conn()
    conn.execute(
        "UPDATE oauth_quota_cache SET last_passive_update_at=? WHERE account_key=?",
        (0, "openai:thr@o.io"),
    )
    conn.commit()

    usage2 = asyncio.run(m["oauth_manager"].fetch_usage("openai:thr@o.io"))
    # 第二次应该返回 state_db 里的旧数据（合成出来的 five_hour/seven_day），不触发新 probe
    probe_time_2 = m["oauth_manager"]._OPENAI_PROBE_LAST["openai:thr@o.io"]
    assert probe_time_2 == probe_time_1, "probe should NOT have been triggered (throttle)"
    print("  [PASS] fetch_usage(openai): throttle bucket blocks rapid probes")


def test_delete_account_clears_openai_probe_bucket(m):
    """账号删除时级联清 probe 桶。"""
    _setup(m)
    _add_openai(m, "del@o.io")
    m["registry"].rebuild_from_config()
    asyncio.run(m["oauth_manager"].fetch_usage("openai:del@o.io"))
    assert "openai:del@o.io" in m["oauth_manager"]._OPENAI_PROBE_LAST

    m["oauth_manager"].delete_account("openai:del@o.io")
    assert "openai:del@o.io" not in m["oauth_manager"]._OPENAI_PROBE_LAST
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

    # 手动把 OpenAI probe 桶预置：表示刚 probe 过，这样 fetch_usage 节流
    # → 走 quota_load 返回空 → quota_monitor_once 视为 "ok:..."
    m["oauth_manager"]._OPENAI_PROBE_LAST["openai:mon@o.io"] = time.time()

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
    acc = m["oauth_manager"].get_account("openai:opr@o.io")
    ch = m["OpenAIOAuthChannel"](acc)

    resp = _FakeResp({
        "x-codex-primary-used-percent": "98",
        "x-codex-primary-reset-after-seconds": "600",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "10",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    acc_after = m["oauth_manager"].get_account("openai:opr@o.io")
    assert acc_after["disabled_reason"] == "quota", acc_after
    assert acc_after["enabled"] is False
    print("  [PASS] openai: primary 98% (>=95) → auto-disabled")


def test_openai_no_auto_disable_below_threshold(m):
    _setup(m)
    _add_openai(m, "ook@o.io")
    acc = m["oauth_manager"].get_account("openai:ook@o.io")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _FakeResp({
        "x-codex-primary-used-percent": "50",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "20",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:ook@o.io")
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
    acc = m["oauth_manager"].get_account("openai:thresh@o.io")
    ch = m["OpenAIOAuthChannel"](acc)
    # 85% > 80% → 应该禁用
    resp = _FakeResp({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:thresh@o.io")
    assert acc_after["disabled_reason"] == "quota"
    print("  [PASS] openai: custom disableThresholdPercent=80 honored")


def test_openai_user_disabled_not_touched(m):
    """user 主动禁用的账号不被响应头超限覆盖。"""
    _setup(m)
    _add_openai(m, "ud@o.io")
    m["oauth_manager"].set_enabled("openai:ud@o.io", False, reason="user")
    acc = m["oauth_manager"].get_account("openai:ud@o.io")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _FakeResp({
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    acc_after = m["oauth_manager"].get_account("openai:ud@o.io")
    assert acc_after["disabled_reason"] == "user"
    print("  [PASS] openai: user-disabled reason preserved")


# ==============================================================
# main
# ==============================================================

def main():
    m = _import_modules()
    tests = [
        # A. fetch_usage 统一门面
        test_fetch_usage_claude_goes_to_api,
        test_fetch_usage_openai_goes_to_probe,
        test_fetch_usage_openai_skips_when_passive_fresh,
        test_fetch_usage_openai_throttle_between_probes,
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
