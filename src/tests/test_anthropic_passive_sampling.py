"""Anthropic 响应头被动采样测试。

覆盖（2026-04-20 新增能力，参考 sub2api ratelimit_service.go）：

  - src/anthropic/rate_limit_headers.py
      * parse_rate_limit_headers: 5h/7d util/reset 解析
      * is_window_exceeded: surpassed-threshold / util>=1.0 判定
      * _parse_reset_iso: 秒/毫秒时间戳自适应
  - src/state_db.py
      * quota_patch_passive: 只更新 5h/7d 段，不碰 sonnet/opus/extra/raw_data
      * quota_patch_passive: 账号首次出现 → INSERT 只含白名单字段
      * last_passive_update_at 列迁移幂等
  - src/failover.py
      * _maybe_record_anthropic_snapshot: 30s 节流、provider=claude 才触发
      * forget_anthropic_snapshot: 级联清理

运行：./venv/bin/python -m src.tests.test_anthropic_passive_sampling
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import sys
import time
import traceback
from datetime import datetime, timezone


def _import_modules():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_manager, state_db, failover
    from src.anthropic import rate_limit_headers as rlh
    from src.channel import oauth_channel, openai_oauth_channel, api_channel, registry
    return {
        "config": config,
        "oauth_manager": oauth_manager,
        "state_db": state_db,
        "failover": failover,
        "rlh": rlh,
        "OAuthChannel": oauth_channel.OAuthChannel,
        "OpenAIOAuthChannel": openai_oauth_channel.OpenAIOAuthChannel,
        "ApiChannel": api_channel.ApiChannel,
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
        c.setdefault("oauth", {})["mockMode"] = True
    m["config"].update(clear_accounts)
    m["oauth_manager"]._refresh_locks.clear()
    # 清 failover 两个节流桶
    m["failover"]._codex_snapshot_last.clear()
    m["failover"]._anthropic_snapshot_last.clear()


class _FakeResp:
    """mock httpx.Response：只暴露 .headers 字段。"""
    def __init__(self, headers: dict):
        self.headers = dict(headers)


# ==============================================================
# 解析器：parse_rate_limit_headers
# ==============================================================

def test_parse_5h_utilization_fraction(m):
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.05",
    })
    assert out == {"five_hour_util": 5.0}, out
    print("  [PASS] parse: 5h util 0.05 → 5.0%")


def test_parse_7d_utilization_fraction(m):
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-7d-utilization": "0.65",
    })
    assert out == {"seven_day_util": 65.0}, out
    print("  [PASS] parse: 7d util 0.65 → 65.0%")


def test_parse_both_windows(m):
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "0.123",
        "anthropic-ratelimit-unified-7d-utilization": "0.8",
    })
    assert abs(out["five_hour_util"] - 12.3) < 1e-9, out
    assert out["seven_day_util"] == 80.0, out
    print("  [PASS] parse: both 5h + 7d util together")


def test_parse_reset_unix_seconds(m):
    # 2026-04-20 12:00:00 UTC
    ts = int(datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-5h-reset": str(ts),
    })
    assert out == {"five_hour_reset": "2026-04-20T12:00:00Z"}, out
    print("  [PASS] parse: 5h reset unix seconds → ISO8601")


def test_parse_reset_unix_milliseconds_auto_detect(m):
    ts_ms = int(datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-7d-reset": str(ts_ms),
    })
    assert out == {"seven_day_reset": "2026-04-27T00:00:00Z"}, out
    print("  [PASS] parse: 7d reset milliseconds auto-detected + normalized")


def test_parse_empty_headers_returns_none(m):
    assert m["rlh"].parse_rate_limit_headers({}) is None
    assert m["rlh"].parse_rate_limit_headers(None) is None
    # 非相关头
    assert m["rlh"].parse_rate_limit_headers({
        "content-type": "application/json",
        "x-request-id": "abc",
    }) is None
    print("  [PASS] parse: returns None for empty / irrelevant headers")


def test_parse_malformed_values_ignored(m):
    out = m["rlh"].parse_rate_limit_headers({
        "anthropic-ratelimit-unified-5h-utilization": "not-a-number",
        "anthropic-ratelimit-unified-7d-utilization": "0.5",
        "anthropic-ratelimit-unified-5h-reset": "abc",
        "anthropic-ratelimit-unified-7d-reset": str(int(time.time() + 7200)),
    })
    # 5h util/reset 非法 → 丢弃；7d 两个有效
    assert "five_hour_util" not in out
    assert "five_hour_reset" not in out
    assert out["seven_day_util"] == 50.0
    assert out["seven_day_reset"] is not None and out["seven_day_reset"].endswith("Z")
    print("  [PASS] parse: malformed values silently dropped, others preserved")


def test_parse_case_insensitive(m):
    """headers 大小写混用（如 httpx.Headers 某些情况下）仍能命中。"""
    out = m["rlh"].parse_rate_limit_headers({
        "Anthropic-Ratelimit-Unified-5h-Utilization": "0.42",
    })
    assert out == {"five_hour_util": 42.0}, out
    print("  [PASS] parse: case-insensitive header key matching")


# ==============================================================
# 窗口超限判定：is_window_exceeded
# ==============================================================

def test_window_exceeded_surpassed_threshold_true(m):
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-5h-surpassed-threshold": "true",
    }, "5h") is True
    # 大写 True 也认
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-7d-surpassed-threshold": "TRUE",
    }, "7d") is True
    print("  [PASS] window exceeded: surpassed-threshold='true' → True")


def test_window_exceeded_util_ge_one(m):
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-5h-utilization": "1.0",
    }, "5h") is True
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-7d-utilization": "1.05",
    }, "7d") is True
    print("  [PASS] window exceeded: util >= 1.0 → True")


def test_window_exceeded_util_float_epsilon(m):
    """0.9999999 应被视为超限（sub2api isAnthropicWindowExceeded 同款容差）。"""
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-5h-utilization": "0.9999999999",
    }, "5h") is True
    print("  [PASS] window exceeded: floating-point epsilon (0.9999999 → True)")


def test_window_not_exceeded_normal(m):
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-5h-utilization": "0.5",
    }, "5h") is False
    assert m["rlh"].is_window_exceeded({
        "anthropic-ratelimit-unified-7d-surpassed-threshold": "false",
    }, "7d") is False
    # 无相关头保守返回 False
    assert m["rlh"].is_window_exceeded({}, "5h") is False
    print("  [PASS] window not exceeded: normal / explicit false / absent → False")


def test_window_invalid_arg_raises(m):
    try:
        m["rlh"].is_window_exceeded({}, "1d")
    except ValueError:
        print("  [PASS] is_window_exceeded: rejects window != '5h' / '7d'")
        return
    assert False, "expected ValueError"


# ==============================================================
# state_db.quota_patch_passive 语义
# ==============================================================

def _add_claude(m, email="test@claude.io"):
    m["oauth_manager"].add_account({
        "email": email, "provider": "claude",
        "access_token": "a", "refresh_token": "r",
    })


def test_patch_passive_only_updates_whitelisted_columns(m):
    """核心回归：被动采样不得覆盖 sonnet / opus / extra 维度。"""
    _setup(m)
    _add_claude(m, "iso@c.io")
    ak = "claude:iso@c.io"

    # 先用主动拉写一条完整的行（模拟 _usage_sync 路径）
    m["state_db"].quota_save(ak, {
        "fetched_at": 1_700_000_000_000,
        "five_hour_util": 10.0, "five_hour_reset": "A",
        "seven_day_util": 20.0, "seven_day_reset": "B",
        "sonnet_util": 30.0, "sonnet_reset": "C",
        "opus_util": 40.0, "opus_reset": "D",
        "extra_used": 5.0, "extra_limit": 50.0, "extra_util": 10.0,
        "raw_data": '{"full":true}',
    }, email="iso@c.io")

    # 再用被动采样只更新 5h/7d
    m["state_db"].quota_patch_passive(ak, {
        "five_hour_util": 77.0, "five_hour_reset": "A2",
        "seven_day_util": 88.0, "seven_day_reset": "B2",
    }, email="iso@c.io")

    row = m["state_db"].quota_load(ak)
    # 白名单字段被更新
    assert row["five_hour_util"] == 77.0
    assert row["five_hour_reset"] == "A2"
    assert row["seven_day_util"] == 88.0
    assert row["seven_day_reset"] == "B2"
    # 非白名单字段保留
    assert row["sonnet_util"] == 30.0, "sonnet_util must not be touched by passive patch"
    assert row["opus_util"] == 40.0
    assert row["extra_used"] == 5.0
    assert row["extra_limit"] == 50.0
    assert row["raw_data"] == '{"full":true}', "raw_data must not be touched"
    # fetched_at（主动拉时间戳）必须保留
    assert row["fetched_at"] == 1_700_000_000_000
    # last_passive_update_at 被设置
    assert row["last_passive_update_at"] is not None
    assert row["last_passive_update_at"] > 0
    print("  [PASS] patch_passive: only updates 5h/7d, preserves sonnet/opus/extra/raw_data")


def test_patch_passive_insert_when_missing(m):
    """新账号从未主动拉过时，被动采样应 INSERT 一条只含白名单字段的行。"""
    _setup(m)
    _add_claude(m, "new@c.io")
    ak = "claude:new@c.io"

    # 被动采样先到
    m["state_db"].quota_patch_passive(ak, {
        "five_hour_util": 3.0, "five_hour_reset": "X",
        "seven_day_util": 5.0,
    }, email="new@c.io")

    row = m["state_db"].quota_load(ak)
    assert row is not None, "row should be INSERTed"
    assert row["email"] == "new@c.io"
    assert row["fetched_at"] == 0, "fetched_at=0 as sentinel for 'never actively fetched'"
    assert row["five_hour_util"] == 3.0
    assert row["five_hour_reset"] == "X"
    assert row["seven_day_util"] == 5.0
    # 主动拉才有的字段必须为 NULL
    assert row["sonnet_util"] is None
    assert row["opus_util"] is None
    assert row["extra_used"] is None
    assert row["raw_data"] is None
    assert row["last_passive_update_at"] > 0
    print("  [PASS] patch_passive: INSERTs minimal row when account not yet synced")


def test_patch_passive_ignores_non_whitelisted_keys(m):
    """传入非白名单 key（例如 sonnet_util）必须被忽略，不得写入。"""
    _setup(m)
    _add_claude(m, "wl@c.io")
    ak = "claude:wl@c.io"

    m["state_db"].quota_patch_passive(ak, {
        "five_hour_util": 1.0,
        "sonnet_util": 999.0,       # 非白名单
        "raw_data": '{"evil":1}',   # 非白名单
        "extra_limit": 100.0,       # 非白名单
    }, email="wl@c.io")

    row = m["state_db"].quota_load(ak)
    assert row["five_hour_util"] == 1.0
    assert row["sonnet_util"] is None
    assert row["raw_data"] is None
    assert row["extra_limit"] is None
    print("  [PASS] patch_passive: non-whitelisted keys silently dropped")


def test_patch_passive_empty_patch_is_noop(m):
    _setup(m)
    _add_claude(m, "noop@c.io")
    ak = "claude:noop@c.io"
    m["state_db"].quota_patch_passive(ak, {}, email="noop@c.io")
    assert m["state_db"].quota_load(ak) is None
    print("  [PASS] patch_passive: empty patch is a no-op (no INSERT)")


def test_patch_passive_all_non_whitelist_is_noop(m):
    """全传非白名单 → 不写任何行。"""
    _setup(m)
    _add_claude(m, "allbad@c.io")
    ak = "claude:allbad@c.io"
    m["state_db"].quota_patch_passive(ak, {"sonnet_util": 50.0}, email="allbad@c.io")
    assert m["state_db"].quota_load(ak) is None
    print("  [PASS] patch_passive: all-non-whitelist patch is a no-op")


# ==============================================================
# failover 集成：_maybe_record_anthropic_snapshot
# ==============================================================

def test_record_snapshot_on_claude_oauth_channel(m):
    _setup(m)
    _add_claude(m, "hook@c.io")
    acc = m["oauth_manager"].get_account("claude:hook@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-7d-utilization": "0.77",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    row = m["state_db"].quota_load("claude:hook@c.io")
    assert row is not None
    assert row["five_hour_util"] == 42.0
    assert row["seven_day_util"] == 77.0
    assert row["last_passive_update_at"] > 0
    print("  [PASS] _maybe_record_anthropic_snapshot: writes on first call")


def test_record_snapshot_throttled_within_30s(m):
    _setup(m)
    _add_claude(m, "thr@c.io")
    acc = m["oauth_manager"].get_account("claude:thr@c.io")
    ch = m["OAuthChannel"](acc, [])

    resp1 = _FakeResp({"anthropic-ratelimit-unified-5h-utilization": "0.10"})
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp1)
    row1 = m["state_db"].quota_load("claude:thr@c.io")
    assert row1["five_hour_util"] == 10.0

    # 30s 内第二次：即使头里值变了也不应覆盖
    resp2 = _MockStrongResp({"anthropic-ratelimit-unified-5h-utilization": "0.99"})
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp2)
    row2 = m["state_db"].quota_load("claude:thr@c.io")
    assert row2["five_hour_util"] == 10.0, f"throttle failed, got {row2['five_hour_util']}"

    # 手动推回 31s 前
    m["failover"]._anthropic_snapshot_last["claude:thr@c.io"] = time.time() - 31
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp2)
    row3 = m["state_db"].quota_load("claude:thr@c.io")
    assert row3["five_hour_util"] == 99.0, "expected write after throttle window"
    print("  [PASS] _maybe_record_anthropic_snapshot: 30s throttle honored")


# _MockStrongResp 与 _FakeResp 等价（别名，避免上面 assert 语义疑惑）
_MockStrongResp = _FakeResp


def test_record_snapshot_skips_non_oauth_channel(m):
    """API channel（非 OAuth）不应触发 Anthropic 采样。"""
    _setup(m)
    # 直接构造一个最小 ApiChannel 样本
    class _FakeApiCh:
        type = "api"
        key = "api:fake"
        email = None
        account_key = None
    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.50",
    })
    m["failover"]._maybe_record_anthropic_snapshot(_FakeApiCh(), resp)
    # state_db 不应有任何 oauth_quota_cache 行
    rows = m["state_db"]._get_conn().execute(
        "SELECT COUNT(*) AS c FROM oauth_quota_cache"
    ).fetchone()
    assert rows["c"] == 0
    print("  [PASS] _maybe_record_anthropic_snapshot: skips non-OAuthChannel")


def test_record_snapshot_skips_openai_oauth_channel(m):
    """OpenAI OAuth（Codex）不应走 Anthropic 采样路径（它有自己的 Codex snapshot）。"""
    _setup(m)
    m["oauth_manager"].add_account({
        "email": "openai@c.io", "provider": "openai",
        "access_token": "a", "refresh_token": "r",
        "chatgpt_account_id": "acct-1", "plan_type": "plus",
    })
    acc = m["oauth_manager"].get_account("openai:openai@c.io")
    ch = m["OpenAIOAuthChannel"](acc)

    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.50",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    # openai 账号不应被 Anthropic 采样写入
    row = m["state_db"].quota_load("openai:openai@c.io")
    assert row is None, "OpenAI OAuth should not be touched by anthropic snapshot"
    print("  [PASS] _maybe_record_anthropic_snapshot: skips OpenAIOAuthChannel")


def test_record_snapshot_no_headers_no_write(m):
    _setup(m)
    _add_claude(m, "none@c.io")
    acc = m["oauth_manager"].get_account("claude:none@c.io")
    ch = m["OAuthChannel"](acc, [])
    resp = _FakeResp({"content-type": "application/json"})
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    assert m["state_db"].quota_load("claude:none@c.io") is None
    print("  [PASS] _maybe_record_anthropic_snapshot: no write when headers absent")


def test_record_snapshot_preserves_active_fields(m):
    """集成回归：主动拉在前 → 被动采样在后，sonnet/opus/extra 必须保留。"""
    _setup(m)
    _add_claude(m, "mix@c.io")
    ak = "claude:mix@c.io"
    # 主动拉
    m["state_db"].quota_save(ak, {
        "fetched_at": 1_700_000_000_000,
        "five_hour_util": 1.0,
        "seven_day_util": 2.0,
        "sonnet_util": 33.3, "sonnet_reset": "S",
        "opus_util": 44.4, "opus_reset": "O",
        "extra_used": 1.2, "extra_limit": 10.0, "extra_util": 12.0,
        "raw_data": '{"active":1}',
    }, email="mix@c.io")

    # 被动采样
    acc = m["oauth_manager"].get_account(ak)
    ch = m["OAuthChannel"](acc, [])
    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.50",
        "anthropic-ratelimit-unified-7d-utilization": "0.90",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)

    row = m["state_db"].quota_load(ak)
    assert row["five_hour_util"] == 50.0
    assert row["seven_day_util"] == 90.0
    assert row["sonnet_util"] == 33.3, "sonnet must survive passive sampling"
    assert row["opus_util"] == 44.4
    assert row["extra_used"] == 1.2
    assert row["raw_data"] == '{"active":1}'
    print("  [PASS] integration: passive sampling preserves all active-only fields")


def test_forget_anthropic_snapshot_on_delete(m):
    _setup(m)
    _add_claude(m, "del@c.io")
    acc = m["oauth_manager"].get_account("claude:del@c.io")
    ch = m["OAuthChannel"](acc, [])
    resp = _FakeResp({
        "anthropic-ratelimit-unified-5h-utilization": "0.50",
    })
    m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    assert "claude:del@c.io" in m["failover"]._anthropic_snapshot_last

    m["oauth_manager"].delete_account("claude:del@c.io")
    assert "claude:del@c.io" not in m["failover"]._anthropic_snapshot_last
    assert "del@c.io" not in m["failover"]._anthropic_snapshot_last
    print("  [PASS] delete_account: forget_anthropic_snapshot clears throttle bucket")


def test_forget_anthropic_snapshot_by_bare_email(m):
    """兼容：forget_anthropic_snapshot 对裸 email 也有效。"""
    _setup(m)
    m["failover"]._anthropic_snapshot_last["bare@c.io"] = time.time()
    m["failover"]._anthropic_snapshot_last["claude:other@c.io"] = time.time()
    m["failover"].forget_anthropic_snapshot("bare@c.io")
    assert "bare@c.io" not in m["failover"]._anthropic_snapshot_last
    # 其他 key 不受影响
    assert "claude:other@c.io" in m["failover"]._anthropic_snapshot_last
    print("  [PASS] forget_anthropic_snapshot: works with bare email (compat)")


def test_snapshot_error_does_not_crash(m):
    """解析失败或 state_db 写失败都不影响主链路（failover 静默吞）。"""
    _setup(m)
    _add_claude(m, "crash@c.io")
    acc = m["oauth_manager"].get_account("claude:crash@c.io")
    ch = m["OAuthChannel"](acc, [])

    # 通过 monkeypatch 让 quota_patch_passive 抛错
    sdb = m["state_db"]
    orig = sdb.quota_patch_passive
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated db failure")
    sdb.quota_patch_passive = _boom
    try:
        resp = _FakeResp({"anthropic-ratelimit-unified-5h-utilization": "0.1"})
        # 不应抛到调用方
        m["failover"]._maybe_record_anthropic_snapshot(ch, resp)
    finally:
        sdb.quota_patch_passive = orig
    print("  [PASS] _maybe_record_anthropic_snapshot: swallows errors (does not crash)")


# ==============================================================
# main
# ==============================================================

def main():
    m = _import_modules()
    tests = [
        # 解析器
        test_parse_5h_utilization_fraction,
        test_parse_7d_utilization_fraction,
        test_parse_both_windows,
        test_parse_reset_unix_seconds,
        test_parse_reset_unix_milliseconds_auto_detect,
        test_parse_empty_headers_returns_none,
        test_parse_malformed_values_ignored,
        test_parse_case_insensitive,
        # 窗口超限判定
        test_window_exceeded_surpassed_threshold_true,
        test_window_exceeded_util_ge_one,
        test_window_exceeded_util_float_epsilon,
        test_window_not_exceeded_normal,
        test_window_invalid_arg_raises,
        # state_db.quota_patch_passive
        test_patch_passive_only_updates_whitelisted_columns,
        test_patch_passive_insert_when_missing,
        test_patch_passive_ignores_non_whitelisted_keys,
        test_patch_passive_empty_patch_is_noop,
        test_patch_passive_all_non_whitelist_is_noop,
        # failover 集成
        test_record_snapshot_on_claude_oauth_channel,
        test_record_snapshot_throttled_within_30s,
        test_record_snapshot_skips_non_oauth_channel,
        test_record_snapshot_skips_openai_oauth_channel,
        test_record_snapshot_no_headers_no_write,
        test_record_snapshot_preserves_active_fields,
        test_forget_anthropic_snapshot_on_delete,
        test_forget_anthropic_snapshot_by_bare_email,
        test_snapshot_error_does_not_crash,
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
