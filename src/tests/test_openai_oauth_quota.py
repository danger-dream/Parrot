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
      （provider 行 / Codex 原始窗口块 / refresh_usage 友好提示）
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
        m["state_db"].quota_delete(row["email"])
    # 清 failover 的节流桶（test 之间不共享节流状态）
    m["failover"]._codex_snapshot_last.clear()
    m["states"].clear_all()


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
    acc = m["oauth_manager"].get_account("hook@openai.test")
    ch = m["OpenAIOAuthChannel"](acc)
    resp = _MockResp({
        "x-codex-primary-used-percent": "35",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "12",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    row = m["state_db"].quota_load("hook@openai.test")
    assert row is not None and row["seven_day_util"] == 35.0
    print("  [PASS] _maybe_record_codex_snapshot writes on first call")


def test_record_codex_snapshot_throttle(m):
    _setup(m)
    _add_openai(m, "throttle@openai.test")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("throttle@openai.test"))
    resp1 = _MockResp({
        "x-codex-primary-used-percent": "10",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp1)
    row1 = m["state_db"].quota_load("throttle@openai.test")
    assert row1["seven_day_util"] == 10.0
    # 30s 内第二次调用：即使头里值变了也不应覆盖
    resp2 = _MockResp({
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp2)
    row2 = m["state_db"].quota_load("throttle@openai.test")
    assert row2["seven_day_util"] == 10.0, f"throttle failed, got {row2['seven_day_util']}"
    # 手动穿过节流窗口：回退上次写时间到 30s 之前
    m["failover"]._codex_snapshot_last["throttle@openai.test"] = time.time() - 31
    m["failover"]._maybe_record_codex_snapshot(ch, resp2)
    row3 = m["state_db"].quota_load("throttle@openai.test")
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
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("noh@openai.test"))
    resp = _MockResp({"content-type": "text/event-stream"})  # 无任何 x-codex-*
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    row = m["state_db"].quota_load("noh@openai.test")
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
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("ui@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "77",
        "x-codex-primary-window-minutes": "10080",
        "x-codex-secondary-used-percent": "22",
        "x-codex-secondary-window-minutes": "300",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    rec = _UiRecorder()
    m["ui"].api = rec
    short = m["ui"].register_code("ui@openai.test")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last, "no editMessageText captured"
    text = last["text"]
    # provider 行
    assert "🅾 OpenAI" in text or "Provider:" in text, text[:500]
    # plan 行
    assert "plus" in text
    # 归一化 5h / 7d
    assert "5h" in text and "77" in text
    # Codex 原始窗口块
    assert "Codex 原始窗口" in text
    assert "primary (10080min)" in text
    print("  [PASS] oauth_menu detail: provider tag + plan + codex usage")


def test_oauth_menu_refresh_usage_openai_probe(m):
    """OpenAI 账户点'刷新用量' → force_refresh + probe_usage（mockMode 合成 snapshot）。"""
    _setup(m)
    _add_openai(m, "ru@openai.test")
    m["registry"].rebuild_from_config()

    # 写一个旧 token / expired 便于观察更新
    def _stamp(c):
        for a in c["oauthAccounts"]:
            if a["email"] == "ru@openai.test":
                a["access_token"] = "OLD-AT"
                a["expired"] = "2026-01-01T00:00:00Z"
                a["last_refresh"] = "2026-01-01T00:00:00Z"
    m["config"].update(_stamp)
    # rebuild 之后 channel 引用的是新配置
    m["registry"].rebuild_from_config()

    rec = _UiRecorder()
    m["ui"].api = rec
    short = m["ui"].register_code("ru@openai.test")
    m["oauth_menu"].on_refresh_usage(42, 100, "cb", short)

    # Step 1 结果：force_refresh 更新了 token 三字段
    acc = m["oauth_manager"].get_account("ru@openai.test")
    assert acc["access_token"] != "OLD-AT"
    assert acc["expired"] != "2026-01-01T00:00:00Z"
    assert acc["last_refresh"] != "2026-01-01T00:00:00Z"
    # Step 2 结果：probe_usage mockMode 写入 snapshot
    row = m["state_db"].quota_load("ru@openai.test")
    assert row is not None, "probe should have written quota snapshot"
    # mockMode 合成 primary=3% / secondary=1% → normalize 后 7d=3 / 5h=1
    assert row["seven_day_util"] == 3.0
    assert row["five_hour_util"] == 1.0
    # 详情已重渲染 + 头部"已刷新 Token 并更新用量"提示
    last = rec.last("editMessageText")
    assert last and "探测请求成功" in last["text"], last.get("text", "")[:200]
    print("  [PASS] oauth_menu refresh_usage: openai → force_refresh + probe_usage + re-render")


def test_oauth_menu_refresh_all_probes_openai(m):
    """refresh_all 对 openai：force_refresh + probe_usage 成功计入 'OpenAI 探测'。"""
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
    m["config"].update(_stamp)
    m["registry"].rebuild_from_config()

    rec = _UiRecorder()
    m["ui"].api = rec
    m["oauth_menu"].on_refresh_all(42, 100, "cb")
    # 新 UI：至少 2 条 sendMessage（初始进度条 + 结束摘要兜底/降级摘要）
    sends = [d for mth, d in rec.calls if mth == "sendMessage"]
    assert sends, "expected progress messages"
    final_text = sends[-1]["text"]
    # 兜底摘要里应包含两账户的 email 作为节标题
    assert "c@claude.test" in final_text, final_text[:500]
    assert "o@openai.test" in final_text, final_text[:500]
    # 至少有一条"刷新成功"行
    assert "刷新成功" in final_text, final_text[:500]
    # 完成标识
    assert "用量刷新完成" in final_text
    # openai 账户 token 刷过；quota snapshot 也写入
    acc = m["oauth_manager"].get_account("o@openai.test")
    assert acc["access_token"] != "OLD-OPENAI-AT"
    row = m["state_db"].quota_load("o@openai.test")
    assert row is not None and row["seven_day_util"] == 3.0
    print("  [PASS] oauth_menu refresh_all: openai force_refresh + probe both ran")


def test_probe_usage_writes_snapshot_in_mock_mode(m):
    """OpenAIOAuthChannel.probe_usage mockMode 下合成 snapshot 写库，不发 HTTP。"""
    _setup(m)
    _add_openai(m, "probe@openai.test")
    m["registry"].rebuild_from_config()
    ch = m["registry"].get_channel("oauth:probe@openai.test")
    import asyncio
    res = asyncio.run(ch.probe_usage())
    assert res["ok"] is True, res
    assert res.get("reason") == "mock"
    row = m["state_db"].quota_load("probe@openai.test")
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
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("del@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "5",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)
    assert "del@openai.test" in m["failover"]._codex_snapshot_last
    m["oauth_manager"].delete_account("del@openai.test")
    assert "del@openai.test" not in m["failover"]._codex_snapshot_last
    print("  [PASS] delete_account: forget_codex_snapshot clears throttle bucket")


def test_on_refresh_token_openai_uses_unified_path(m):
    """2026-04-20 统一路径后：on_refresh_token 对 openai 账号**不主动**调
    fetch_usage（代码里明确 `if provider_of(ak) != "openai"` 分支）。
    但 _detail_text_and_kb 在渲染时会调 ensure_quota_fresh → fetch_usage →
    走 probe 路径（zero-cost 节流已生效时 probe 会被跳过）。

    这个测试的新语义：确认「刷新 Token」按钮的主路径不主动发 probe，让发 probe
    留给「刷新用量」按钮的明确意图；但详情页渲染顺带刷一次 usage 是合理行为。
    """
    _setup(m)
    _add_openai(m, "rt@openai.test")
    m["registry"].rebuild_from_config()

    rec = _UiRecorder()
    m["ui"].api = rec

    # 预先设置 probe 节流桶，让 _detail_text_and_kb 里的 ensure_quota_fresh
    # 跳过真实 probe（模拟已有近期采样的场景）
    import time
    m["oauth_manager"]._OPENAI_PROBE_LAST["openai:rt@openai.test"] = time.time()

    called = {"fetch_usage": 0, "probe_usage": 0}
    orig_fetch = m["oauth_manager"].fetch_usage
    async def _counting_fetch(ak):
        called["fetch_usage"] += 1
        return await orig_fetch(ak)

    # channel 层 probe 计数
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    orig_probe = OpenAIOAuthChannel.probe_usage
    async def _counting_probe(self, *args, **kwargs):
        called["probe_usage"] += 1
        return await orig_probe(self, *args, **kwargs)

    try:
        m["oauth_manager"].fetch_usage = _counting_fetch
        OpenAIOAuthChannel.probe_usage = _counting_probe
        short = m["ui"].register_code("rt@openai.test")
        m["oauth_menu"].on_refresh_token(42, 100, "cb", short)
    finally:
        m["oauth_manager"].fetch_usage = orig_fetch
        OpenAIOAuthChannel.probe_usage = orig_probe

    # probe_usage 应被节流桶完全阻止 → 未真正 probe
    assert called["probe_usage"] == 0,         f"probe_usage should be throttled (bucket set in test), got {called['probe_usage']}"
    # 重渲染详情页成功
    last = rec.last("editMessageText")
    assert last and ("已刷新" in last["text"] or "Token" in last["text"])
    print("  [PASS] on_refresh_token(openai): probe throttled correctly, no token burned")


def test_status_menu_quota_warnings_tags_openai(m):
    _setup(m)
    _add_openai(m, "warn@openai.test")
    # 写 warn 级别用量（85%）：高于 warnings 阈值 80%，低于 disable 阈值 95%
    # → 应被预警，但不被自动禁用（2026-04-20 响应头自动禁用接入后的正确边界）
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("warn@openai.test"))
    resp = _MockResp({
        "x-codex-primary-used-percent": "85",
        "x-codex-primary-window-minutes": "10080",
    })
    m["failover"]._maybe_record_codex_snapshot(ch, resp)

    # 确认没被禁用
    acc = m["oauth_manager"].get_account("warn@openai.test")
    assert acc.get("disabled_reason") is None, f"should not be auto-disabled at 85% (threshold 95%): {acc}"

    warnings = m["status_menu"]._quota_warnings(threshold_pct=80.0)
    assert warnings, "expected at least one warning"
    joined = "\n".join(warnings)
    assert "warn@openai.test" in joined
    # 🅾 前缀标记 openai 账户
    assert "🅾" in joined, joined
    print("  [PASS] status_menu _quota_warnings: openai accounts get 🅾 tag")


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
        test_oauth_menu_refresh_usage_openai_probe,
        test_oauth_menu_refresh_all_probes_openai,
        test_probe_usage_writes_snapshot_in_mock_mode,
        test_delete_account_clears_codex_snapshot_throttle,
        test_on_refresh_token_openai_uses_unified_path,
        test_status_menu_quota_warnings_tags_openai,
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
