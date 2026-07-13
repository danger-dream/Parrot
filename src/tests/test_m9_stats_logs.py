"""M9 统计 + 日志菜单测试。

注入一批日志数据到 log_db，覆盖：
  - stats 汇总视图数字正确
  - stats 按渠道/按模型/按 API Key 分组（含按钮 ✓ 标记当前选项）
  - stats 4×4 切换：period/dim 按钮
  - logs 列表 + 详情（含 retry_chain 多条）
  - logs 短码失效保护
  - ui.fmt_tokens / fmt_rate / fmt_ms / fmt_bjt_ts
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sqlite3
import sys
import time


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, log_db, oauth_manager, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import logs_menu, stats_menu, proxy_menu
    return {
        "config": config, "log_db": log_db, "oauth_manager": oauth_manager, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
        "logs_menu": logs_menu, "stats_menu": stats_menu, "proxy_menu": proxy_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}
    def by(self, m): return [d for mm, d in self.calls if mm == m]
    def last(self, m):
        l = self.by(m); return l[-1] if l else None
    def clear(self):
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    # 清空当月 log
    conn = m["log_db"]._get_conn()
    conn.execute("DELETE FROM request_log")
    conn.execute("DELETE FROM request_detail")
    conn.execute("DELETE FROM retry_chain")
    conn.execute("DELETE FROM proxy_chain")
    conn.commit()
    def _cfg(c):
        c["oauthAccounts"] = [{
            "provider": "openai",
            "email": "o@openai.test",
            "workspace_id": "acct-raw-hidden",
            "chatgpt_account_id": "acct-raw-hidden",
            "access_token": "x",
            "refresh_token": "r",
            "models": ["gpt-5.1"],
        }]
    m["config"].update(_cfg)
    m["states"].clear_all()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _insert_success(
    m, request_id, api_key, model, channel_key, channel_type="api",
    input_tok=100, output_tok=20, cc=10, cr=50, retry_count=0, affinity_hit=0,
    connect_ms=150, first_token_ms=600, total_ms=3000, is_stream=True,
    ingress_protocol="anthropic", upstream_protocol=None, upstream_transport=None, http_status=200,
    response_body='{"id":"x"}', fast_mode=None,
):
    ld = m["log_db"]
    if upstream_protocol is None and str(model).startswith("gpt-5.6"):
        upstream_protocol = "openai-responses"
    ld.insert_pending(request_id, "1.1.1.1", api_key, model, is_stream,
                     msg_count=3, tool_count=0, request_headers={}, request_body={},
                     ingress_protocol=ingress_protocol, fast_mode=fast_mode)
    ld.finish_success(
        request_id, channel_key, channel_type, model,
        input_tokens=input_tok, output_tokens=output_tok,
        cache_creation_tokens=cc, cache_read_tokens=cr,
        connect_ms=connect_ms, first_token_ms=first_token_ms, total_ms=total_ms,
        retry_count=retry_count, affinity_hit=affinity_hit,
        response_body=response_body, http_status=http_status,
        upstream_protocol=upstream_protocol, upstream_transport=upstream_transport,
    )


def _insert_error(
    m, request_id, api_key, model, channel_key=None, channel_type=None,
    error_message="upstream boom", retry_count=1, http_status=502,
):
    ld = m["log_db"]
    ld.insert_pending(request_id, "1.1.1.1", api_key, model, True,
                     msg_count=1, tool_count=0, request_headers={}, request_body={})
    ld.finish_error(
        request_id, error_message, retry_count,
        final_channel_key=channel_key, final_channel_type=channel_type,
        final_model=model, http_status=http_status, total_ms=1500,
    )


# ─── Tests ───────────────────────────────────────────────────────

def test_fmt_helpers(m):
    ui = m["ui"]
    assert ui.fmt_tokens(500) == "500"
    assert ui.fmt_tokens(1500) == "1.5K"
    assert ui.fmt_tokens(2_500_000) == "2.5M"
    assert ui.fmt_tokens(12_325_000_000) == "12.3B"
    assert ui.fmt_tokens(2_500_000_000_000) == "2.5T"
    assert ui.fmt_tokens(None) == "0"

    assert ui.fmt_rate(50, 200) == "25.0%"
    assert ui.fmt_rate(0, 0) == "N/A"
    assert ui.fmt_rate(None, None) == "N/A"

    assert ui.prompt_total(100, 10, 50) == 160
    assert ui.fmt_cache_phrase(50, 160) == "缓存 50 (31.2%)"
    assert ui.fmt_cache_phrase(51_700, 85_000) == "缓存 51.7K (60.8%)"
    assert ui.fmt_usd(0.005) == "$0.01"
    assert ui.fmt_cost({
        "cost_ticks": 123_450_000_000,
        "costed_success": 1,
        "unpriced_success": 0,
    }) == "$12.35"
    assert ui.fmt_cost({
        "cost_ticks": 123_450_000_000,
        "costed_success": 1,
        "unpriced_success": 2,
    }) == "$12.35 · 2 次未计价"
    assert ui.fmt_cost({
        "cost_ticks": 123_450_000_000,
        "estimated_cost_ticks": 123_450_000_000,
        "estimated_costed_success": 1,
        "costed_success": 1,
    }) == "估算 $12.35"
    assert ui.fmt_cost({
        "cost_ticks": 150_000_000_000,
        "actual_cost_ticks": 50_000_000_000,
        "estimated_cost_ticks": 100_000_000_000,
        "actual_costed_success": 1,
        "estimated_costed_success": 2,
        "costed_success": 3,
    }) == "$15.00（实际 $5.00 + 估算 $10.00）"
    assert "≈" not in ui.fmt_cost({
        "cost_ticks": 123_450_000_000,
        "estimated_cost_ticks": 123_450_000_000,
        "costed_success": 1,
    })

    assert ui.fmt_ms(250) == "250ms"
    assert ui.fmt_ms(1500) == "1.5s"
    assert ui.fmt_ms(None) == "-"

    ts = ui.fmt_bjt_ts(1713350400, "%Y-%m-%d")
    assert "-" in ts
    print("  [PASS] ui fmt helpers")


def test_stats_overall(m):
    _setup(m)
    # 3 条成功 + 1 条失败 + 1 条 pending
    _insert_success(m, "r1", "k1", "claude-opus-4-7", "oauth:a@x.com", "oauth",
                    input_tok=1000, output_tok=100, cc=50, cr=800, retry_count=0, affinity_hit=1)
    _insert_success(m, "r2", "k1", "claude-sonnet-4-6", "oauth:a@x.com", "oauth",
                    input_tok=500, output_tok=60, cc=0, cr=400, retry_count=1, affinity_hit=1)
    _insert_success(m, "r3", "k2", "glm-5", "api:智谱", "api",
                    input_tok=300, output_tok=40, cc=0, cr=200)
    _insert_error(m, "r4", "k2", "glm-5", "api:智谱", "api",
                  error_message='HTTP 502: {"error":{"type":"api_error","message":"bad"}}')
    m["log_db"].insert_pending("r5", "1.1.1.1", "k1", "claude-opus-4-7", True, 1, 0, {}, {})

    rec = _install_recorder(m)
    m["stats_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    assert edit is not None
    text = edit["text"]
    assert "统计 — 今天" in text
    assert "共 5 次" in text
    assert "✅ 3" in text
    assert "❌ 1" in text
    assert "⏳ 1" in text
    # 按钮
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "stats:view:0:all" in btns
    assert "stats:view:3:all" in btns
    assert "stats:view:month:all" in btns
    # 亲和命中率（2/5 = 40%），缓存 token 率（1400/3250 = 43.1%）
    assert "40.0%" in text
    assert "缓存 1.4K (43.1%)" in text
    assert " · 💵 " in text
    assert "≈" not in text
    assert "cache" not in text
    print("  [PASS] stats overall with counts + flags")


def test_stats_cost_estimate_and_unknown_coverage(m):
    _setup(m)
    _insert_success(
        m,
        "cost-priced",
        "k1",
        "gpt-5.6-sol",
        "api:OpenAI",
        input_tok=1_000_000,
        output_tok=1_000_000,
        cc=1_000_000,
        cr=1_000_000,
    )
    _insert_success(
        m,
        "cost-unknown",
        "k2",
        "private-unknown-model",
        "api:Private",
        input_tok=500,
        output_tok=100,
        cc=0,
        cr=0,
    )

    result = m["log_db"].stats_summary(0)
    overall = result["overall"]
    assert overall["cost_ticks"] == int(68.5 * 10_000_000_000)
    assert overall["estimated_cost_ticks"] == overall["cost_ticks"]
    assert overall["actual_cost_ticks"] == 0
    assert overall["costed_success"] == 1
    assert overall["unpriced_success"] == 1

    rec = _install_recorder(m)
    m["stats_menu"].show(42, 100, "cb")
    text = rec.last("editMessageText")["text"]
    assert "💵 估算 $68.50" in text
    assert "≈" not in text


def test_stats_prefers_xai_actual_cost_ticks(m):
    _setup(m)
    actual_ticks = 123_456_789
    _insert_success(
        m,
        "cost-xai",
        "k-xai",
        "grok-private-model",
        "oauth:xai:account",
        channel_type="oauth",
        input_tok=999,
        output_tok=111,
        cc=0,
        cr=100,
        response_body=json.dumps({"usage": {"cost_in_usd_ticks": actual_ticks}}),
        ingress_protocol="responses",
        upstream_protocol="openai-responses",
    )

    result = m["log_db"].stats_summary(0)
    overall = result["overall"]
    assert overall["cost_ticks"] == actual_ticks
    assert overall["actual_cost_ticks"] == actual_ticks
    assert overall["estimated_cost_ticks"] == 0
    assert overall["costed_success"] == 1
    assert overall["unpriced_success"] == 0
    row = m["log_db"].recent_logs(limit=1)[0]
    assert m["ui"].fmt_cost_from_row(row) == "实际 $0.01"

    rec = _install_recorder(m)
    m["stats_menu"].show(42, 100, "cb")
    text = rec.last("editMessageText")["text"]
    assert "$0.01" in text
    assert "≈" not in text


def test_stats_labels_mixed_actual_and_estimated_cost(m):
    _setup(m)
    _insert_success(
        m,
        "cost-estimated-half",
        "k-est",
        "gpt-5.6-sol",
        "api:OpenAI",
        input_tok=100_000,
        output_tok=0,
        cc=0,
        cr=0,
    )
    _insert_success(
        m,
        "cost-actual-half",
        "k-xai",
        "grok-private-model",
        "oauth:xai:account",
        channel_type="oauth",
        input_tok=1,
        output_tok=1,
        cc=0,
        cr=0,
        response_body=json.dumps({"usage": {"cost_in_usd_ticks": 5_000_000_000}}),
        upstream_protocol="xai-responses",
    )
    overall = m["log_db"].stats_summary(0)["overall"]
    assert overall["cost_ticks"] == 10_000_000_000
    assert overall["actual_costed_success"] == 1
    assert overall["estimated_costed_success"] == 1
    assert m["ui"].fmt_cost(overall) == "$1.00（实际 $0.50 + 估算 $0.50）"


def test_stats_fast_mode_uses_priority_pricing(m):
    _setup(m)
    _insert_success(
        m,
        "cost-priority",
        "k1",
        "gpt-5.6-luna",
        "api:OpenAI",
        input_tok=100_000,
        output_tok=100_000,
        cc=0,
        cr=0,
        fast_mode=True,
    )
    result = m["log_db"].stats_summary(0)
    assert result["overall"]["cost_ticks"] == int(1.4 * 10_000_000_000)


def test_stats_classifies_long_context_per_request_before_sum(m):
    _setup(m)
    # Two 150k requests remain short even though their aggregate exceeds 272k.
    for idx in range(2):
        _insert_success(
            m,
            f"cost-short-{idx}",
            "k1",
            "gpt-5.6-sol",
            "api:OpenAI",
            input_tok=150_000,
            output_tok=0,
            cc=0,
            cr=0,
        )
    _insert_success(
        m,
        "cost-long",
        "k1",
        "gpt-5.6-sol",
        "api:OpenAI",
        input_tok=300_000,
        output_tok=0,
        cc=0,
        cr=0,
    )
    result = m["log_db"].stats_summary(0)
    # 2 × (150k × $5/M) + (300k × $10/M) = $4.50.
    assert result["overall"]["cost_ticks"] == int(4.5 * 10_000_000_000)
    assert result["overall"]["costed_success"] == 3


def test_ambiguous_legacy_cached_usage_is_explicitly_unpriced(m):
    _setup(m)
    _insert_success(
        m,
        "legacy-cache-semantics",
        "k1",
        "gpt-5.6-sol",
        "api:OpenAI",
        input_tok=100_000,
        output_tok=10_000,
        cc=0,
        cr=80_000,
        upstream_protocol="",
    )
    result = m["log_db"].stats_summary(0)
    assert result["overall"]["costed_success"] == 0
    assert result["overall"]["unpriced_success"] == 1
    row = m["log_db"].recent_logs(limit=1)[0]
    assert m["ui"].fmt_cost_from_row(row) == "未计价（1 次）"


def test_ambiguous_anthropic_cache_write_ttl_does_not_hide_other_costs(m):
    _setup(m)
    _insert_success(
        m,
        "claude-no-write",
        "k1",
        "claude-opus-4-6",
        "oauth:anthropic:test",
        input_tok=1_000,
        output_tok=100,
        cc=0,
        cr=0,
        upstream_protocol="anthropic",
    )
    _insert_success(
        m,
        "claude-unknown-ttl",
        "k1",
        "claude-opus-4-6",
        "oauth:anthropic:test",
        input_tok=1_000,
        output_tok=100,
        cc=1_000,
        cr=0,
        upstream_protocol="anthropic",
    )
    result = m["log_db"].stats_summary(0)
    overall = result["overall"]
    assert overall["cost_ticks"] == 75_000_000
    assert overall["costed_success"] == 1
    assert overall["unpriced_success"] == 1


def test_cost_available_to_all_request_and_window_cache_surfaces(m):
    _setup(m)
    expected = int(68.5 * 10_000_000_000)
    _insert_success(
        m,
        "cost-everywhere",
        "priced-key",
        "gpt-5.6-sol",
        "api:Priced",
        input_tok=1_000_000,
        output_tok=1_000_000,
        cc=1_000_000,
        cr=1_000_000,
    )

    lifetime = m["log_db"].stats_lifetime()
    channel = m["log_db"].tokens_for_channel("api:Priced", 0)
    apikey = m["log_db"].tokens_for_apikey("priced-key", 0)
    channel_model = m["log_db"].channel_model_stats("api:Priced", 0)[0]
    apikey_model = m["log_db"].apikey_model_stats("priced-key", 0)[0]
    for metrics in (lifetime, channel, apikey, channel_model, apikey_model):
        assert metrics["cost_ticks"] == expected
        assert metrics["costed_success"] == 1
        assert metrics["unpriced_success"] == 0

    row = m["log_db"].recent_logs(limit=1)[0]
    assert row["request_id"] == "cost-everywhere"
    assert m["log_db"].cost_for_log(row)["cost_ticks"] == expected
    detail = m["log_db"].log_detail("cost-everywhere")
    assert m["log_db"].cost_for_log(detail["log"])["cost_ticks"] == expected
    assert m["ui"].fmt_cost_from_row(row) == "估算 $68.50"

    # OAuth token 刷新通知也是 Telegram 展示面，月度缓存旁必须有金额。
    _insert_success(
        m,
        "cost-refresh-notice",
        "priced-key",
        "gpt-5.6-sol",
        "oauth:openai:notice@example.test:acct",
        channel_type="oauth",
        input_tok=1_000_000,
        output_tok=1_000_000,
        cc=1_000_000,
        cr=1_000_000,
    )
    notice = m["oauth_manager"]._build_refresh_notice(
        "openai:notice@example.test:acct", usage_flat=None,
    )
    assert "缓存 1.0M (33.3%) · 💵 估算 $68.50" in notice
    assert "≈" not in notice


def test_cache_miss_write_sample_includes_request_cost(m):
    _setup(m)
    _insert_success(
        m,
        "cost-cache-write",
        "priced-key",
        "gpt-5.6-sol",
        "api:Priced",
        input_tok=1_000_000,
        output_tok=1_000_000,
        cc=1_000_000,
        cr=0,
    )
    summary = m["log_db"].stats_summary(0)
    text = m["stats_menu"]._section_cache_misses(summary["recent_cache_misses"])
    assert "写 1.0M · 💵 估算 $67.50" in text
    assert "≈" not in text


def test_stats_without_cost_never_reads_response_body(m):
    _setup(m)
    _insert_success(
        m,
        "no-cost-body-read",
        "k1",
        "grok-4",
        "oauth:xai:test",
        input_tok=10,
        output_tok=2,
        cc=0,
        cr=0,
        response_body='{"usage":{"cost_in_usd_ticks":123}}',
        upstream_protocol="xai-responses",
    )
    conn = m["log_db"]._get_conn()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        m["log_db"].stats_summary(0, include_cost=False)
    finally:
        conn.set_trace_callback(None)
    assert not any("request_detail" in sql.lower() for sql in statements)


def test_stats_group_by_channel(m):
    _setup(m)
    _insert_success(m, "c1", "k1", "m1", "api:A")
    _insert_success(m, "c2", "k1", "m1", "api:A")
    _insert_success(m, "c3", "k2", "m2", "api:B")
    _insert_error(m, "c4", "k2", "m2", "api:B")

    rec = _install_recorder(m)
    m["stats_menu"].view(42, 100, "cb", period="0", dim="channel")
    edit = rec.last("editMessageText")
    text = edit["text"]
    assert "按渠道 — 今天" in text
    # 渠道展示用 emoji + short name（去掉 oauth:/api: 前缀），更人性化
    assert "🔀" in text
    assert ">A<" in text and ">B<" in text   # <code>A</code> / <code>B</code>
    assert "命中请求" in text
    assert "缓存 100 (31.2%)" in text
    assert " · 💵 " in text
    # 当前选中维度按钮应有 ✓ 标记
    btns_labels = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "text" in b]
    assert any("渠道 ✓" in l for l in btns_labels)
    print("  [PASS] stats by channel")


def test_stats_group_by_model_and_apikey(m):
    _setup(m)
    _insert_success(m, "m1", "k1", "claude-opus-4-7", "oauth:a@x.com", "oauth")
    _insert_success(m, "m2", "k2", "glm-5", "api:智谱", "api")
    _insert_error(m, "m3", "k2", "glm-5", "api:智谱", "api")

    rec = _install_recorder(m)
    m["stats_menu"].view(42, 100, "cb", period="0", dim="model")
    text = rec.last("editMessageText")["text"]
    assert "按模型 — 今天" in text
    assert "claude-opus-4-7" in text
    assert "glm-5" in text

    rec.clear()
    m["stats_menu"].view(42, 100, "cb", period="0", dim="apikey")
    text = rec.last("editMessageText")["text"]
    assert "按 Key — 今天" in text
    assert "k1" in text and "k2" in text
    print("  [PASS] stats by model + apikey")


def test_stats_period_switch(m):
    _setup(m)
    _insert_success(m, "p1", "k1", "m1", "api:A")

    rec = _install_recorder(m)
    # 点"7天" 按钮切换
    m["bot"]._handle_callback({
        "id": "cb", "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "stats:view:7:all",
    })
    text = rec.last("editMessageText")["text"]
    assert "最近 7 天" in text
    # 按钮上 7天 带 ✓
    btns_labels = [b["text"] for row in rec.last("editMessageText")["reply_markup"]["inline_keyboard"] for b in row if "text" in b]
    assert any("7天 ✓" in l for l in btns_labels)
    print("  [PASS] stats period switch")


def test_logs_list(m):
    _setup(m)
    # 构造 3 条
    _insert_success(m, "L1", "k1", "claude-opus-4-7", "oauth:a@x.com", "oauth")
    _insert_success(m, "L2", "k1", "glm-5", "api:智谱", "api", affinity_hit=1)
    _insert_error(m, "L3", "k2", "claude-sonnet-4-6",
                  error_message='HTTP 502: {"error":{"type":"api_error","message":"down"}}')

    rec = _install_recorder(m)
    m["logs_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    text = edit["text"]
    # 三条都出现
    assert "claude-opus-4-7" in text
    assert "glm-5" in text
    assert "claude-sonnet-4-6" in text
    # 成功/失败图标
    assert "✅" in text and "❌" in text
    # 亲和标志
    assert "★亲和" in text
    # 缓存量带百分比：默认 input=100, cache_write=10, cache_read=50，总 prompt=160
    assert "缓存 50 (31.2%)" in text
    # 错误摘要解包
    assert "down" in text

    assert "最近日志 · 请求日志 · 第 1/1 页 · 共 3 条" in text
    assert "Token: ↑ 160 · ↓ 20 · 缓存 50 (31.2%) · 💵 " in text
    assert "耗时: 连接 150ms · 首字 600ms · 总 3.0s" in text

    # 顶部可切换请求/多媒体日志；详情按钮仍单行 3 列紧凑排列。
    kb_rows = edit["reply_markup"]["inline_keyboard"]
    assert any(b.get("callback_data") == "media:logs" for b in kb_rows[0])
    first_detail_row = next(
        row for row in kb_rows
        if any(b.get("callback_data", "").startswith("logs:detail:") for b in row)
    )
    assert len(first_detail_row) == 3
    btns = [b["callback_data"] for row in kb_rows for b in row if "callback_data" in b]
    assert sum(1 for b in btns if b.startswith("logs:detail:")) >= 3
    assert any(b.startswith("logs:list:") for b in btns)
    labels = [b["text"] for row in kb_rows for b in row if "text" in b]
    assert "🏠 首页" in labels
    assert "🔎 查询" in labels
    assert not any(t.startswith(("🔑 账号：", "🤖 模型：", "📡 渠道：")) for t in labels)
    bottom_row = next(row for row in kb_rows if any(b.get("text") == "🔄 刷新" for b in row))
    assert [b.get("text") for b in bottom_row] == ["🔄 刷新", "🔎 查询", "◀ 返回主菜单"]
    print("  [PASS] logs list")


def test_logs_list_marks_responses_websocket(m):
    _setup(m)
    _insert_success(
        m, "WS1", "virus", "gpt-5.5",
        "oauth:openai:soarsky0204@gmail.com:51dbffb2-a422-4aec-a76a-f98e243b5b2d",
        "oauth", input_tok=27, output_tok=21, cc=0, cr=0,
        ingress_protocol="responses_ws", upstream_protocol="openai-responses", upstream_transport="ws", http_status=101,
    )
    _insert_success(
        m, "UPWS1", "virus", "gpt-5.5",
        "oauth:openai:soarsky0204@gmail.com:51dbffb2-a422-4aec-a76a-f98e243b5b2d",
        "oauth", input_tok=27, output_tok=21, cc=0, cr=0,
        ingress_protocol="responses", upstream_protocol="openai-responses", upstream_transport="ws", http_status=200,
    )
    rec = _install_recorder(m)
    m["logs_menu"].show(42, 100, "cb")
    text = rec.last("editMessageText")["text"]
    assert text.count("<code>[response]</code>") >= 2
    assert "传输协议: <b>WS</b>" in text
    assert "传输协议: <b>↑WS</b>" in text
    assert "gpt-5.5" in text
    print("  [PASS] logs WS marker")


def test_logs_list_combines_transport_and_proxy(m):
    ui = m["ui"]
    ws_and_proxy = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-terra",
        "ingress_protocol": "responses",
        "upstream_transport": "ws",
        "proxy_name": "misaka-lax",
    })
    assert "  传输协议: <b>↑WS</b> · 出站代理: misaka-lax" in ws_and_proxy
    assert "\n  出站代理: misaka-lax" not in ws_and_proxy

    proxy_only = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-terra",
        "proxy_name": "misaka-lax",
    })
    assert "  出站代理: misaka-lax" in proxy_only
    assert "传输协议:" not in proxy_only
    print("  [PASS] logs compact transport + proxy line")


def test_logs_pagination(m):
    _setup(m)
    for i in range(8):
        _insert_success(m, f"P{i}", "k1", f"model-{i}", "api:A")
        time.sleep(0.002)

    rec = _install_recorder(m)
    m["logs_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    text = edit["text"]
    assert "第 1/2 页 · 共 8 条" in text
    assert text.count("<b>#") == 6
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(b.startswith("logs:list:") for b in btns)
    labels = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "text" in b]
    assert "🏠 首页" in labels and "◀ 上一页" in labels and "下一页 ▶" in labels
    next_cb = next(b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if b.get("text") == "下一页 ▶")

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb", next_cb)
    edit = rec.last("editMessageText")
    text = edit["text"]
    assert "第 2/2 页 · 共 8 条" in text
    assert text.count("<b>#") == 2
    assert "<b>#7</b>" in text and "<b>#8</b>" in text
    assert "<b>#1</b>" not in text and "<b>#2</b>" not in text
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(b.startswith("logs:list:") for b in btns)
    detail_labels = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if b.get("callback_data", "").startswith("logs:detail:")]
    assert detail_labels == ["📄 #7", "📄 #8"]
    detail_cb = next(b for b in btns if b.startswith("logs:detail:"))

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb", detail_cb)
    detail_text = rec.last("editMessageText")["text"]
    assert "日志详情" in detail_text
    detail_btns = [b["callback_data"] for row in rec.last("editMessageText")["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(b.startswith("logs:list:") for b in detail_btns)
    print("  [PASS] logs pagination")


def test_logs_filters_preserve_state(m):
    _setup(m)
    def _cfg(c):
        c["apiKeys"] = {"k1": {"key": "x"}, "k2": {"key": "y"}}
        c["channels"] = [{"name": "A", "type": "api"}, {"name": "B", "type": "api"}]
        c["oauthAccounts"] = [{
            "provider": "openai",
            "email": "o@openai.test",
            "workspace_id": "acct-raw-hidden",
            "chatgpt_account_id": "acct-raw-hidden",
            "access_token": "x",
            "refresh_token": "r",
            "models": ["gpt-5.1"],
        }]
    m["config"].update(_cfg)
    _insert_success(m, "F1", "k1", "m1", "api:A")
    time.sleep(0.002)
    _insert_success(m, "F2", "k2", "m2", "api:B")
    time.sleep(0.002)
    _insert_success(m, "F3", "k1", "m2", "oauth:openai:o@openai.test:acct-raw-hidden", "oauth")
    time.sleep(0.002)
    _insert_success(m, "F4", "k3", "parrot-test-context-responses", "__tmp_context_orphan", "api")

    rec = _install_recorder(m)
    m["logs_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    query_cb = next(
        b["callback_data"]
        for row in edit["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "🔎 查询"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-query", query_cb)
    query_menu = rec.last("editMessageText")
    assert "查询日志" in query_menu["text"]
    assert "请选择查询方式" in query_menu["text"]
    query_rows = query_menu["reply_markup"]["inline_keyboard"]
    query_labels = [b.get("text", "") for row in query_rows for b in row]
    assert "🔑 账号" in query_labels and "🤖 模型" in query_labels and "📡 渠道" in query_labels
    query_methods_row = next(row for row in query_rows if any(b.get("text") == "🔑 账号" for b in row))
    assert [b.get("text") for b in query_methods_row] == ["🔑 账号", "🤖 模型", "📡 渠道"]
    assert "🧹 清空查询条件" in query_labels
    account_cb = next(
        b["callback_data"]
        for row in query_menu["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "🔑 账号"
    )
    model_cb = next(
        b["callback_data"]
        for row in query_menu["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "🤖 模型"
    )
    channel_cb = next(
        b["callback_data"]
        for row in query_menu["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "📡 渠道"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-model", model_cb)
    model_menu = rec.last("editMessageText")
    model_labels = [b.get("text", "") for row in model_menu["reply_markup"]["inline_keyboard"] for b in row]
    assert "m1" in model_labels and "m2" in model_labels
    assert not any(label.startswith("parrot-test") for label in model_labels)

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-channel", channel_cb)
    channel_menu = rec.last("editMessageText")
    channel_rows = channel_menu["reply_markup"]["inline_keyboard"]
    channel_labels = [b.get("text", "") for row in channel_rows for b in row]
    assert "📡 A" in channel_labels
    assert "📡 B" in channel_labels
    assert any(label.startswith("🔐 ") and "OAuth" not in label and "acct-raw-hidden" not in label for label in channel_labels)
    assert not any("__tmp_context" in label or "compact-rescue" in label for label in channel_labels)
    assert all(len(row) <= 2 for row in channel_rows[:-1])

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-filter", account_cb)
    filt = rec.last("editMessageText")
    assert "按 API KEY 账号筛选日志" in filt["text"]
    k1_cb = next(
        b["callback_data"]
        for row in filt["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "k1"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-toggle", k1_cb)
    filt2 = rec.last("editMessageText")
    assert "当前账号: k1" in filt2["text"]
    confirm_cb = next(
        b["callback_data"]
        for row in filt2["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "确认"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-confirm", confirm_cb)
    filtered = rec.last("editMessageText")
    assert "共 2 条" in filtered["text"]
    assert "m1" in filtered["text"] and "m2" in filtered["text"]
    assert "Key <code>k2</code>" not in filtered["text"]
    btns = [b for row in filtered["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b.get("text") == "🔎 查询" for b in btns)
    query_cb = next(b["callback_data"] for b in btns if b.get("text") == "🔎 查询")

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-query-filtered", query_cb)
    active_query_menu = rec.last("editMessageText")
    assert "账号：k1" in active_query_menu["text"]
    back_to_list_cb = next(
        b["callback_data"]
        for row in active_query_menu["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "◀ 返回日志"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-query-back", back_to_list_cb)
    filtered = rec.last("editMessageText")
    assert "共 2 条" in filtered["text"]
    btns = [b for row in filtered["reply_markup"]["inline_keyboard"] for b in row]
    detail_cb = next(b["callback_data"] for b in btns if b.get("callback_data", "").startswith("logs:detail:"))

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-detail", detail_cb)
    detail = rec.last("editMessageText")
    back_cb = next(
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text", "").startswith("◀ 返回第")
    )
    assert back_cb.startswith("logs:list:")

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-back", back_cb)
    back = rec.last("editMessageText")
    assert "共 2 条" in back["text"]
    btns_back = [b for row in back["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b.get("text") == "🔎 查询" for b in btns_back)

    query_cb = next(b["callback_data"] for b in btns_back if b.get("text") == "🔎 查询")
    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-query-clear-open", query_cb)
    active_query_menu = rec.last("editMessageText")
    clear_cb = next(
        b["callback_data"]
        for row in active_query_menu["reply_markup"]["inline_keyboard"]
        for b in row if b.get("text") == "🧹 清空查询条件"
    )

    rec.clear()
    m["logs_menu"].handle_callback(42, 100, "cb-query-clear", clear_cb)
    cleared = rec.last("editMessageText")
    assert "共 4 条" in cleared["text"]
    print("  [PASS] logs filters multi-select, clear, and preserve state through detail back")


def test_logs_detail_with_execution_chain(m):
    _setup(m)
    rid = "D-rid"
    # 手工构造一条带执行链的记录：普通失败尝试 + 本地搜索轮 + 成功尝试。
    m["log_db"].insert_pending(rid, "1.1.1.1", "k1", "claude-opus-4-7", True, 3, 0, {}, {})
    a1 = m["log_db"].record_retry_attempt(rid, 1, "api:A", "api", "claude-opus-4-7", time.time())
    p1 = m["log_db"].record_proxy_attempt(rid, a1, 1, "us-att", time.time())
    time.sleep(0.01)
    m["log_db"].update_proxy_attempt(p1, connect_ms=200, ended_at=time.time(),
                                      outcome="connect_error", error_detail="dial timeout",
                                      bytes_up=10, bytes_down=20)
    p2 = m["log_db"].record_proxy_attempt(rid, a1, 2, "misaka-lax", time.time())
    time.sleep(0.01)
    m["log_db"].update_proxy_attempt(p2, connect_ms=90, ended_at=time.time(),
                                      outcome="success", bytes_up=30, bytes_down=40)
    m["log_db"].update_retry_attempt(a1, connect_ms=200, first_byte_ms=None, ended_at=time.time(),
                                     outcome="http_error", error_detail="HTTP 500: boom")
    a_web = m["log_db"].record_retry_attempt(rid, 2, "api:A", "api", "claude-opus-4-7", time.time())
    time.sleep(0.01)
    m["log_db"].update_retry_attempt(a_web, connect_ms=100, first_byte_ms=300, ended_at=time.time(),
                                      outcome="local_web_tool_round", error_detail="executed 1 local web tool call(s), round=1")
    a2 = m["log_db"].record_retry_attempt(rid, 3, "api:B", "api", "claude-opus-4-7", time.time())
    time.sleep(0.01)
    m["log_db"].update_retry_attempt(a2, connect_ms=100, first_byte_ms=400, ended_at=time.time(),
                                     outcome="success", error_detail=None)
    m["log_db"].finish_success(rid, "api:B", "api", "claude-opus-4-7",
                               input_tokens=200, output_tokens=50, cache_creation_tokens=0, cache_read_tokens=100,
                               connect_ms=100, first_token_ms=400, total_ms=2500,
                               retry_count=1, affinity_hit=0, response_body='{}',
                               http_status=200)

    rec = _install_recorder(m)
    short = m["ui"].register_code(rid)
    m["logs_menu"].show_detail(42, 100, "cb", short)
    text = rec.last("editMessageText")["text"]
    assert "日志详情" in text
    assert rid in text
    assert "执行链 (3 次渠道尝试 / 2 个上游轮次)" in text
    assert "us-att" in text and "misaka-lax" in text
    assert "connect_error" in text and "dial timeout" in text
    assert "🔎 <b>尝试 2.</b>" in text
    assert "本地搜索轮" in text
    assert "❌ <b>2.</b>" not in text
    assert "<code>A</code>" in text and "<code>B</code>" in text
    assert "http_error" in text
    # Tokens / 耗时；input=200, cache_read=100，总 prompt=300，缓存率 33.3%
    assert "↑" in text and "↓" in text
    assert "缓存 100 (33.3%)" in text
    # 重试 1 次 flag
    assert "重试 1 次" in text
    print("  [PASS] logs detail with execution chain")


def test_log_inspector_localizes_pretty_json_and_redacts_encrypted(m):
    from src.telegram import log_inspector
    body = {
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"keyword\":\"abc\",\"limit\":2}"},
            }]},
            {"role": "assistant", "content": [{
                "type": "reasoning",
                "encrypted_content": "x" * 180,
                "summary": "hidden thinking",
            }]},
        ]
    }
    items = log_inspector.parse_request_body(body)
    user = next(x for x in items if x["kind"] == "user")
    assert log_inspector.button_label(user).startswith("#1 用户")
    tc = next(x for x in items if x["kind"] == "tool_call")
    assert "调用" in log_inspector.button_label(tc)
    assert '{\n  "keyword": "abc",\n  "limit": 2\n}' in tc["text"]
    reasoning = next(x for x in items if x["kind"] == "reasoning")
    assert "思考" in log_inspector.button_label(reasoning)
    assert "encrypted_content 已省略" in reasoning["text"]
    assert "x" * 100 not in reasoning["text"]
    print("  [PASS] log inspector localized labels + pretty json + encrypted redaction")


def test_logs_body_inspector_defaults_last_and_truncates(m):
    _setup(m)
    rid = "BODY-inspector"
    long_tail = "尾巴" * 1000
    body = {
        "model": "claude-test",
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "第一条用户消息"},
            {"role": "assistant", "content": long_tail},
        ],
    }
    m["log_db"].insert_pending(rid, "1.1.1.1", "k1", "claude-test", True, 3, 0, {}, body)
    m["log_db"].finish_success(rid, "api:A", "api", "claude-test", response_body='{}')

    rec = _install_recorder(m)
    short = m["ui"].register_code("logbody:" + rid)
    m["logs_menu"].show_request_body(42, 100, "cb", short)
    edit = rec.last("editMessageText")
    assert edit is not None
    text = edit["text"]
    assert "请求 Body" in text
    assert "消息: <b>#4</b>" in text  # params + 3 条 message，默认最后一条真实消息
    assert "已截断" in text
    assert len(text) < 4096
    assert rec.by("sendMessage") == []
    flat = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(cb.startswith("logs:ins:") for cb in flat)
    assert any(cb.startswith("logs:full:") for cb in flat)
    print("  [PASS] logs body inspector default last + truncate")


def test_logs_response_inspector_parses_messages_and_search_state(m):
    _setup(m)
    rid = "RESP-inspector"
    body = {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]}
    response = "\n".join([
        json.dumps({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "最终"}, ensure_ascii=False),
        json.dumps({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "回答"}, ensure_ascii=False),
        json.dumps({"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2}}}, ensure_ascii=False),
    ])
    m["log_db"].insert_pending(rid, "1.1.1.1", "k1", "gpt-test", True, 1, 0, {}, body, ingress_protocol="responses")
    m["log_db"].finish_success(rid, "oauth:openai:o@openai.test:acct-raw-hidden", "oauth", "gpt-test", response_body=response, upstream_protocol="openai-responses")

    rec = _install_recorder(m)
    short = m["ui"].register_code("logresp:" + rid)
    m["logs_menu"].show_response_body(42, 100, "cb", short)
    edit = rec.last("editMessageText")
    assert "响应" in edit["text"]
    assert "最终回答" in edit["text"]
    assert "消息: <b>#1</b>" in edit["text"]  # 默认跳过 trailing usage，选最后一条真实内容
    kb = edit["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    labels = [b["text"] for row in kb for b in row]
    assert any("全部 2" in x for x in labels)
    assert any("助手 1" in x for x in labels)
    assert any("用量 1" in x for x in labels)

    usage_cb = next(b["callback_data"] for row in kb for b in row if "用量 1" in b["text"])
    rec.clear()
    assert m["logs_menu"].handle_callback(42, 100, "cb", usage_cb) is True
    usage_edit = rec.last("editMessageText")
    assert "类型: <code>用量统计</code>" in usage_edit["text"]
    assert "用量统计" in usage_edit["text"]

    search_cb = next(cb for cb in flat if cb.startswith("logs:search:"))
    rec.clear()
    assert m["logs_menu"].handle_callback(42, 100, "cb", search_cb) is True
    assert m["states"].get_state(42)["action"] == "logs_search"
    assert rec.last("sendMessage") and "请输入搜索关键词" in rec.last("sendMessage")["text"]
    print("  [PASS] logs response inspector parse + type filter + search state")


def test_logs_detail_short_expired(m):
    _setup(m)
    rec = _install_recorder(m)
    m["logs_menu"].show_detail(42, 100, "cb", "00000000")
    edit = rec.last("editMessageText")
    assert edit and "过期" in edit["text"] or "找到" in edit["text"]
    print("  [PASS] logs detail invalid short")


def test_log_db_fast_mode_migration_for_old_month_db(m):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE request_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id TEXT UNIQUE NOT NULL,
      created_at REAL NOT NULL,
      reasoning_effort TEXT
    )""")
    conn.execute("""CREATE TABLE retry_chain (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id TEXT NOT NULL,
      attempt_order INTEGER NOT NULL,
      channel_key TEXT NOT NULL,
      channel_type TEXT NOT NULL,
      model TEXT NOT NULL,
      started_at REAL NOT NULL
    )""")

    m["log_db"]._ensure_migrations(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(request_log)").fetchall()}
    assert "fast_mode" in cols
    assert "proxy_bytes_up" in cols
    assert "proxy_bytes_down" in cols
    print("  [PASS] log_db old schema migration adds fast_mode")


def test_stats_lifetime_migrates_real_old_month_before_cost_query(m):
    _setup(m)
    ld = m["log_db"]
    path = os.path.join(ld._log_dir, "1999-01.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass
    try:
        conn = sqlite3.connect(path)
        conn.executescript(ld._schema_sql())
        conn.execute(
            """INSERT INTO request_log(
                   request_id, created_at, status, api_key_name, requested_model,
                   final_model, final_channel_key, input_tokens, output_tokens,
                   cache_creation_tokens, cache_read_tokens)
               VALUES('old-priced', 1, 'success', 'k', 'gpt-5.6-sol',
                      'gpt-5.6-sol', 'api:OpenAI', 100000, 10000, 0, 0)"""
        )
        for col in (
            "fast_mode", "ingress_protocol", "upstream_protocol",
            "upstream_transport", "proxy_name", "proxy_bytes_up", "proxy_bytes_down",
        ):
            conn.execute(f"ALTER TABLE request_log DROP COLUMN {col}")
        conn.commit()
        conn.close()

        lifetime = ld.stats_lifetime()
        assert lifetime["total"] == 1
        assert lifetime["costed_success"] == 1
        assert lifetime["unpriced_success"] == 0
        assert lifetime["cost_ticks"] == int(0.8 * 10_000_000_000)

        check = sqlite3.connect(path)
        cols = {row[1] for row in check.execute("PRAGMA table_info(request_log)")}
        check.close()
        assert {"fast_mode", "upstream_protocol", "proxy_bytes_up"} <= cols
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def test_stats_lifetime_readonly_archive_is_not_silently_dropped(m, monkeypatch):
    _setup(m)
    ld = m["log_db"]
    path = os.path.join(ld._log_dir, "1999-02.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass
    original = ld._get_conn_for_month
    try:
        conn = sqlite3.connect(path)
        conn.executescript(ld._schema_sql())
        conn.execute(
            """INSERT INTO request_log(
                   request_id, created_at, status, api_key_name, requested_model,
                   final_model, final_channel_key, input_tokens, output_tokens)
               VALUES('readonly-unpriced', 1, 'success', 'k', 'gpt-5.6-sol',
                      'gpt-5.6-sol', 'api:OpenAI', 100000, 10000)"""
        )
        conn.commit()
        conn.close()

        def fail_migration(month):
            if month == "1999-02":
                raise sqlite3.OperationalError("readonly archive")
            return original(month)

        monkeypatch.setattr(ld, "_get_conn_for_month", fail_migration)
        lifetime = ld.stats_lifetime()
        assert lifetime["total"] == 1
        assert lifetime["costed_success"] == 0
        assert lifetime["unpriced_success"] == 1
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


def test_extract_fast_mode_variants(m):
    ld = m["log_db"]
    assert ld.extract_fast_mode({"speed": "fast"}, "anthropic") is True
    assert ld.extract_fast_mode({"service_tier": "priority"}, "responses") is True
    assert ld.extract_fast_mode({}, "anthropic", {"anthropic-beta": "foo,fast-mode-2026-02-01"}) is True
    assert ld.extract_fast_mode({"service_tier": "default"}, "responses") is False
    print("  [PASS] fast mode extraction variants")


def test_fast_mode_badge_in_recent_logs_and_detail(m):
    _setup(m)
    rid = "FAST-log"
    body = {
        "model": "gpt-fast",
        "input": "hi",
        "service_tier": "priority",
    }
    m["log_db"].insert_pending(
        rid, "1.1.1.1", "k1", "gpt-fast", True, 1, 0, {}, body,
        ingress_protocol="responses",
    )
    m["log_db"].finish_success(
        rid, "api:fast", "api", "gpt-fast", input_tokens=1, output_tokens=1,
        response_body='{"object":"response","output":[]}', upstream_protocol="openai-responses",
    )

    row = m["log_db"].recent_logs(1)[0]
    assert row["fast_mode"] == 1
    assert "⚡ Fast" in m["ui"].fmt_log_entry_body(row)

    rec = _install_recorder(m)
    short = m["ui"].register_code(rid)
    m["logs_menu"].show_detail(42, 100, "cb", short)
    edit = rec.last("editMessageText")
    assert edit is not None
    assert "模式：⚡ Fast" in edit["text"]
    print("  [PASS] Fast mode badge in logs list + detail")


def test_openai_workspace_id_hidden_in_stats_and_logs(m):
    _setup(m)
    raw_key = "oauth:openai:o@openai.test:acct-raw-hidden"
    _insert_success(m, "OID1", "k1", "gpt-5.1", raw_key, "oauth")

    rec = _install_recorder(m)
    m["stats_menu"].view(42, 100, "cb", period="0", dim="channel")
    stats_text = rec.last("editMessageText")["text"]
    assert "o@openai.test" in stats_text
    assert "oauth:openai:" not in stats_text
    assert "acct-raw-hidden" not in stats_text

    rec.clear()
    m["logs_menu"].show(42, 100, "cb")
    logs_text = rec.last("editMessageText")["text"]
    assert "o@openai.test" in logs_text
    assert "oauth:openai:" not in logs_text
    assert "acct-raw-hidden" not in logs_text

    rid = "OID-detail"
    m["log_db"].insert_pending(rid, "1.1.1.1", "k1", "gpt-5.1", True, 1, 0, {}, {})
    att = m["log_db"].record_retry_attempt(rid, 1, raw_key, "oauth", "gpt-5.1", time.time())
    m["log_db"].update_retry_attempt(att, connect_ms=1, first_byte_ms=2, ended_at=time.time(), outcome="success")
    m["log_db"].finish_success(rid, raw_key, "oauth", "gpt-5.1", response_body="{}")
    rec.clear()
    short = m["ui"].register_code(rid)
    m["logs_menu"].show_detail(42, 100, "cb", short)
    detail_text = rec.last("editMessageText")["text"]
    assert "o@openai.test" in detail_text
    assert "oauth:openai:" not in detail_text
    assert "acct-raw-hidden" not in detail_text
    print("  [PASS] OpenAI workspace id hidden in stats/logs UI")


def test_router_dispatch(m):
    _setup(m)
    _insert_success(m, "R1", "k1", "m1", "api:A")
    rec = _install_recorder(m)
    m["ui"].configure("TOKEN", [42])

    m["bot"]._handle_callback({
        "id": "cb1", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:stats",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_callback({
        "id": "cb2", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:logs",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/stats"})
    assert rec.last("sendMessage") is not None
    rec.clear()
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/logs"})
    assert rec.last("sendMessage") is not None
    print("  [PASS] router + /stats /logs")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init(); m["log_db"].init()
    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_fmt_helpers,
        test_stats_overall,
        test_stats_group_by_channel,
        test_stats_group_by_model_and_apikey,
        test_stats_period_switch,
        test_logs_list,
        test_logs_list_marks_responses_websocket,
        test_logs_pagination,
        test_logs_filters_preserve_state,
        test_logs_detail_with_execution_chain,
        test_log_inspector_localizes_pretty_json_and_redacts_encrypted,
        test_logs_body_inspector_defaults_last_and_truncates,
        test_logs_response_inspector_parses_messages_and_search_state,
        test_logs_detail_short_expired,
        test_openai_workspace_id_hidden_in_stats_and_logs,
        test_router_dispatch,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m); passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig_cfg)
        m["config"].update(_restore)
        m["states"].clear_all()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())


def test_proxy_routing_priority_and_legacy_socks5(m):
    """Proxy manager: default direct 不应遮蔽 legacy socks5；账号路由优先级最高。"""
    cfg = m["config"]
    from src.proxy import manager as pm
    from src import network

    # 默认配置只有 routing.default=direct，不能算启用新代理系统，否则 legacy socks5 会失效。
    cfg.update(lambda c: c.setdefault("network", {}).__setitem__("socks5", {
        "enabled": True,
        "url": "socks5://127.0.0.1:9999",
    }))
    pm.init()
    assert pm.is_configured() is False
    assert network.active_socks5_url() == "socks5://127.0.0.1:9999"

    def add_proxy_config(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            "p1": {"type": "socks5", "url": "socks5://127.0.0.1:10001"},
            "p2": {"type": "socks5", "url": "socks5://127.0.0.1:10002"},
        }
        net["groups"] = {"g": ["p1", "direct"]}
        net["routing"] = {
            "default": "direct",
            "oauth_openai": "p2",
            "models": {"gpt-x": "p1"},
            "channels": {"oauth:openai:a:old": "p2"},
            "accounts": {
                "openai:a:new": "g",
                "oauth:openai:a:fallback": "p2",
            },
        }
    cfg.update(add_proxy_config)
    pm.init()
    assert pm.is_configured() is True
    assert pm.resolve_proxy_target(account_key="openai:a:new", channel_key="oauth:openai:a:old", model="gpt-x") == "g"
    assert pm.resolve_proxy_chain(account_key="openai:a:new", channel_key="oauth:openai:a:old", model="gpt-x") == ["p1", "direct"]
    assert pm.resolve_proxy_target(account_key="openai:a:missing", channel_key="oauth:openai:a:fallback", model="gpt-x") == "p2"
    assert pm.resolve_proxy_target(channel_key="oauth:openai:a:old", model="gpt-x") == "p2"
    assert pm.resolve_proxy_target(model="gpt-x") == "p1"
    assert pm.resolve_proxy_target(purpose="oauth_openai") == "p2"
    print("  [PASS] proxy routing priority + legacy socks5")



def test_proxy_upgrade_from_previous_config_is_smooth(m, tmp_path):
    """Old configs without the new proxy subtree must keep legacy SOCKS5 behavior.

    Previous releases only had network.socks5.  After deep-merge with the new
    DEFAULT_CONFIG, network.routing.default=direct appears automatically; that
    implicit default must not opt users into the new proxy subsystem or bypass
    the legacy SOCKS5 client.
    """
    import json
    import os
    import httpx
    from src import config as cfg_mod, network
    from src.proxy import manager as pm

    old_path = tmp_path / "old-config.json"
    old_path.write_text(json.dumps({
        "listen": {"host": "127.0.0.1", "port": 18082},
        "apiKeys": {},
        "oauthAccounts": [],
        "channels": [],
        "network": {
            "dns": {"servers": ["1.1.1.1"]},
            "socks5": {"enabled": True, "url": "socks5://127.0.0.1:19999"},
        },
    }))

    old_cfg_path = cfg_mod.CONFIG_PATH
    old_cache = getattr(cfg_mod, "_cache", None)
    old_mtime = getattr(cfg_mod, "_mtime", 0)
    try:
        cfg_mod.CONFIG_PATH = str(old_path)
        cfg_mod._cache = None
        cfg_mod._mtime = 0
        loaded = cfg_mod.reload()
        assert loaded["network"]["routing"] == {
            "default": "direct",
            "directFallback": False,
        }
        assert loaded["network"]["proxies"] == {}
        assert loaded["network"]["groups"] == {}

        pm.init()
        assert pm.is_configured() is False
        assert network.active_socks5_url() == "socks5://127.0.0.1:19999"

        captured = {}
        class DummyClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def close(self):
                pass
        old_client = httpx.Client
        httpx.Client = DummyClient
        try:
            c = network.sync_client(timeout=1)
            assert isinstance(c, DummyClient)
            assert captured["proxy"] == "socks5://127.0.0.1:19999"
            assert captured["trust_env"] is False
        finally:
            httpx.Client = old_client
    finally:
        cfg_mod.CONFIG_PATH = old_cfg_path
        cfg_mod._cache = old_cache
        cfg_mod._mtime = old_mtime
        cfg_mod.reload()
        pm.init()


def test_proxy_stats_include_tokens_latency_and_real_bytes(m):
    """Proxy stats: 统计次数、tokens、平均连接/首字/耗时、总代理字节数。"""
    ld = m["log_db"]
    ld.init()
    conn = ld._get_conn()
    conn.execute("DELETE FROM request_log")
    conn.execute("DELETE FROM request_detail")
    conn.execute("DELETE FROM retry_chain")
    conn.commit()

    ld.insert_pending("px-1", "1.1.1.1", "k", "gpt-x", True, 1, 0, {}, {})
    ld.finish_success(
        "px-1", "oauth:openai:a:new", "oauth", "gpt-x",
        input_tokens=100, output_tokens=50, cache_creation_tokens=10, cache_read_tokens=5,
        connect_ms=100, first_token_ms=400, total_ms=1200,
        proxy_name="p1", proxy_bytes_up=1024, proxy_bytes_down=4096,
    )
    ld.insert_pending("px-2", "1.1.1.1", "k", "gpt-x", True, 1, 0, {}, {})
    ld.finish_error(
        "px-2", "boom", final_channel_key="oauth:openai:a:new", final_channel_type="oauth", final_model="gpt-x",
        connect_ms=300, first_token_ms=800, total_ms=2400,
        proxy_name="p1", proxy_bytes_up=2048, proxy_bytes_down=1024,
    )

    stats = ld.proxy_stats(limit=10)
    p1 = next(x for x in stats if x["proxy_name"] == "p1")
    assert p1["requests"] == 2
    assert p1["successes"] == 1
    assert p1["failures"] == 1
    assert p1["input_tokens"] == 100
    assert p1["output_tokens"] == 50
    assert p1["cache_creation_tokens"] == 10
    assert p1["cache_read_tokens"] == 5
    assert p1["total_tokens"] == 165
    assert p1["bytes_up"] == 3072
    assert p1["bytes_down"] == 5120
    assert p1["total_bytes"] == 8192
    assert p1["avg_connect_ms"] == 200
    assert p1["avg_first_byte_ms"] == 600
    assert p1["avg_total_ms"] == 1800

    from src.telegram.menus import proxy_menu, system_menu
    rendered = proxy_menu._fmt_proxy_stats(p1)
    assert "请求 <code>2</code> 次" in rendered
    assert "Tokens: <code>165</code> tok" in rendered
    assert "代理流量: <code>8.0KB</code>" in rendered
    assert "连接 <code>200ms</code>" in rendered
    assert "首字 <code>600ms</code>" in rendered
    assert "总耗时 <code>1800ms</code>" in rendered

    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {"p1": {"type": "socks5", "url": "socks5://127.0.0.1:10001"}}
        net["groups"] = {"g1": ["p1", "direct"]}
    m["config"].update(_cfg)
    lines, *_ = system_menu._network_summary()
    text = "\n".join(lines)
    assert "📊 <code>2</code>次" in text
    assert "🧮 <code>165</code> tok" in text
    assert "📦 <code>8.0KB</code>" in text
    assert "组内总计" in text
    assert "连接 <code>200ms</code>" in text
    assert "首字 <code>600ms</code>" in text
    assert "总耗时 <code>1800ms</code>" in text
    print("  [PASS] proxy stats tokens + latency + bytes")



def test_proxy_menu_group_edit_preserves_remove_and_clear_members(m):
    _setup(m)
    pmenu = m["proxy_menu"]
    cfg = m["config"]
    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            "p1": {"type": "socks5", "url": "socks5://127.0.0.1:10001"},
            "p2": {"type": "socks5", "url": "socks5://127.0.0.1:10002"},
        }
        net["groups"] = {"g1": ["p1", "p2"]}
    cfg.update(_cfg)
    rec = _install_recorder(m)

    assert pmenu.handle_callback(100, 10, "cb", "px:grp_edit:g1") is True
    st = m["states"].get_state(100)
    assert st["data"]["members"] == ["p1", "p2"]
    edit = rec.last("editMessageText")
    kb = edit["reply_markup"]["inline_keyboard"]
    flat_cb = [b["callback_data"] for row in kb for b in row]
    assert "px:grp_rm:p1" in flat_cb
    assert "px:grp_clear" in flat_cb

    assert pmenu.handle_callback(100, 10, "cb", "px:grp_rm:p1") is True
    st = m["states"].get_state(100)
    assert st["data"]["members"] == ["p2"]

    assert pmenu.handle_callback(100, 10, "cb", "px:grp_clear") is True
    st = m["states"].get_state(100)
    assert st["data"]["members"] == []


def test_proxy_menu_function_route_all_targets_are_supported(m):
    _setup(m)
    pmenu = m["proxy_menu"]
    cfg = m["config"]
    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            "ss-only": {"type": "ss2022", "server": "127.0.0.1", "port": 8388,
                         "cipher": "2022-blake3-aes-128-gcm", "password": "x"},
            "s5": {"type": "socks5", "url": "socks5://127.0.0.1:10001"},
        }
        net["groups"] = {"g-ss": ["ss-only"], "g-mixed": ["ss-only", "s5"]}
    cfg.update(_cfg)
    labels = dict(pmenu._all_targets())
    assert "功能路由不支持" not in labels["ss-only"]
    assert "功能路由不支持" not in labels["g-ss"]
    assert "功能路由不支持" not in labels["g-mixed"]
    assert "功能路由不支持" not in labels["s5"]



def test_log_db_migrates_proxy_columns_on_old_schema(m):
    ld = m["log_db"]
    ld.init()
    conn = ld._get_conn()
    # Simulate an old monthly DB where request_log/retry_chain existed before proxy columns.
    conn.execute("ALTER TABLE request_log RENAME TO request_log_new")
    conn.execute("ALTER TABLE retry_chain RENAME TO retry_chain_new")
    conn.execute("""
        CREATE TABLE request_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT UNIQUE NOT NULL,
          created_at REAL NOT NULL,
          finished_at REAL,
          client_ip TEXT,
          api_key_name TEXT,
          requested_model TEXT,
          final_channel_key TEXT,
          final_channel_type TEXT,
          final_model TEXT,
          status TEXT DEFAULT 'pending',
          http_status INTEGER,
          error_message TEXT,
          is_stream INTEGER DEFAULT 1,
          msg_count INTEGER DEFAULT 0,
          tool_count INTEGER DEFAULT 0,
          input_tokens INTEGER DEFAULT 0,
          output_tokens INTEGER DEFAULT 0,
          cache_creation_tokens INTEGER DEFAULT 0,
          cache_read_tokens INTEGER DEFAULT 0,
          connect_time_ms INTEGER,
          first_token_time_ms INTEGER,
          total_time_ms INTEGER,
          retry_count INTEGER DEFAULT 0,
          affinity_hit INTEGER DEFAULT 0,
          fingerprint TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE retry_chain (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT NOT NULL,
          attempt_order INTEGER NOT NULL,
          channel_key TEXT NOT NULL,
          channel_type TEXT NOT NULL,
          model TEXT NOT NULL,
          started_at REAL NOT NULL,
          connect_ms INTEGER,
          first_byte_ms INTEGER,
          ended_at REAL,
          outcome TEXT,
          error_detail TEXT
        )
    """)
    conn.execute("DROP TABLE request_log_new")
    conn.execute("DROP TABLE retry_chain_new")
    conn.commit()

    ld._ensure_migrations(conn)
    req_cols = {r[1] for r in conn.execute("PRAGMA table_info(request_log)")}
    retry_cols = {r[1] for r in conn.execute("PRAGMA table_info(retry_chain)")}
    proxy_cols = {r[1] for r in conn.execute("PRAGMA table_info(proxy_chain)")}
    assert {"proxy_name", "proxy_bytes_up", "proxy_bytes_down"}.issubset(req_cols)
    assert {"proxy_name", "bytes_up", "bytes_down"}.issubset(retry_cols)
    assert {"request_id", "retry_attempt_id", "proxy_name", "outcome", "error_detail"}.issubset(proxy_cols)

    ld.insert_pending("oldpx", "1.1.1.1", "k", "m", False, 1, 0, {}, {})
    aid = ld.record_retry_attempt("oldpx", 1, "api:ch", "api", "m", time.time(), proxy_name="p1")
    ld.update_retry_attempt(aid, outcome="success", bytes_up=3, bytes_down=4)
    ld.finish_success("oldpx", "api:ch", "api", "m", proxy_name="p1", proxy_bytes_up=3, proxy_bytes_down=4)
    ps = ld.proxy_stats(limit=10)
    row = next(x for x in ps if x["proxy_name"] == "p1")
    assert row["total_bytes"] == 7



def test_proxy_menu_detail_line_does_not_repeat_proxy_name(m):
    _setup(m)
    from src.proxy import manager as pm
    from src.telegram.menus import proxy_menu

    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            "misaka-lax": {
                "type": "ss2022",
                "server": "38.175.109.137",
                "port": 48888,
                "cipher": "2022-blake3-aes-128-gcm",
                "password": "x",
            },
            "s5-main": {"type": "socks5", "url": "socks5://user:pass@example.com:1080"},
        }
    m["config"].update(_cfg)
    pm.init()
    conns = pm.all_connectors()
    ss_line = proxy_menu._proxy_detail_line(conns["misaka-lax"])
    s5_line = proxy_menu._proxy_detail_line(conns["s5-main"])
    assert "misaka-lax" not in ss_line
    assert "SS2022" in ss_line
    assert "38.175.109.137:48888" in ss_line
    assert "s5-main" not in s5_line
    assert "SOCKS5" in s5_line
    assert "pass" not in s5_line
    assert "***" in s5_line



def test_proxy_menu_list_uses_pagination_and_detail_buttons(m):
    _setup(m)
    from src.telegram.menus import proxy_menu
    cfg = m["config"]
    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            f"p{i}": {"type": "socks5", "url": f"socks5://127.0.0.1:{10000+i}"}
            for i in range(1, 8)
        }
    cfg.update(_cfg)
    rec = _install_recorder(m)

    proxy_menu.show(100, 10, page=1)
    edit = rec.last("editMessageText")
    text = edit["text"]
    assert "第 1/2 页" in text
    kb = edit["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row]
    assert any(cb.startswith("px:view:") for cb in flat)
    assert not any(cb.startswith("px:test:p") for cb in flat)
    assert not any(cb.startswith("px:del_confirm:p") for cb in flat)
    assert "px:groups" in flat
    assert "px:routing" in flat
    assert "px:page:2" in flat

    view_cb = next(cb for cb in flat if cb.startswith("px:view:"))
    rec.clear()
    assert proxy_menu.handle_callback(100, 10, "cb", view_cb) is True
    detail = rec.last("editMessageText")
    detail_flat = [b["callback_data"] for row in detail["reply_markup"]["inline_keyboard"] for b in row]
    assert any(cb.startswith("px:testv:") for cb in detail_flat)
    assert any(cb.startswith("px:del_confirm_v:") for cb in detail_flat)
    assert "px:page:1" in detail_flat



def test_proxy_group_menu_uses_pagination_and_detail_buttons(m):
    _setup(m)
    from src.telegram.menus import proxy_menu
    cfg = m["config"]
    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            f"p{i}": {"type": "socks5", "url": f"socks5://127.0.0.1:{11000+i}"}
            for i in range(1, 4)
        }
        net["groups"] = {
            f"g{i}": ["p1", "p2"]
            for i in range(1, 8)
        }
    cfg.update(_cfg)
    rec = _install_recorder(m)

    proxy_menu._show_groups(100, 10, "", page=1)
    edit = rec.last("editMessageText")
    text = edit["text"]
    assert "代理组" in text
    assert "第 1/2 页" in text
    kb = edit["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row]
    assert any(cb.startswith("px:grp_view:") for cb in flat)
    assert not any(cb.startswith("px:grp_edit:g") for cb in flat)
    assert not any(cb.startswith("px:grp_test:g") for cb in flat)
    assert not any(cb.startswith("px:grp_del:g") for cb in flat)
    assert "px:grp_page:2" in flat
    assert "px:show" in flat
    assert "px:routing" in flat

    view_cb = next(cb for cb in flat if cb.startswith("px:grp_view:"))
    rec.clear()
    assert proxy_menu.handle_callback(100, 10, "cb", view_cb) is True
    detail = rec.last("editMessageText")
    detail_text = detail["text"]
    assert "成员:" in detail_text
    detail_flat = [b["callback_data"] for row in detail["reply_markup"]["inline_keyboard"] for b in row]
    assert any(cb.startswith("px:grp_test_v:") for cb in detail_flat)
    assert any(cb.startswith("px:grp_edit_v:") for cb in detail_flat)
    assert any(cb.startswith("px:grp_del_ask:") for cb in detail_flat)
    assert "px:grp_page:1" in detail_flat

    edit_cb = next(cb for cb in detail_flat if cb.startswith("px:grp_edit_v:"))
    rec.clear()
    assert proxy_menu.handle_callback(100, 10, "cb", edit_cb) is True
    picker = rec.last("editMessageText")
    picker_flat = [b["callback_data"] for row in picker["reply_markup"]["inline_keyboard"] for b in row]
    assert "px:grp_page:1" in picker_flat



def test_failover_proxy_route_kwargs_uses_provider_family_function_route(m):
    _setup(m)
    from src import failover

    class Ch:
        key = "api:anthropic-main"
        protocol = "anthropic"
        account_key = ""

    assert failover._proxy_route_kwargs(Ch(), "claude-x") == {
        "channel_key": "api:anthropic-main",
        "model": "claude-x",
        "purpose": "oauth_anthropic",
        "account_key": "",
    }

    class OpenAICh:
        key = "oauth:openai:a"
        protocol = "openai-responses"
        account_key = "openai:a"

    assert failover._proxy_route_kwargs(OpenAICh(), "gpt-x") == {
        "channel_key": "oauth:openai:a",
        "model": "gpt-x",
        "purpose": "oauth_openai",
        "account_key": "openai:a",
    }


def test_proxy_routing_family_priority_between_model_and_default(m):
    _setup(m)
    from src.proxy import manager as pm
    cfg = m["config"]
    def _cfg(c):
        net = c.setdefault("network", {})
        net["proxies"] = {
            "p-model": {"type": "socks5", "url": "socks5://127.0.0.1:10001"},
            "p-family": {"type": "socks5", "url": "socks5://127.0.0.1:10002"},
            "p-default": {"type": "socks5", "url": "socks5://127.0.0.1:10003"},
        }
        net["groups"] = {}
        net["routing"] = {
            "default": "p-default",
            "oauth_anthropic": "p-family",
            "models": {"claude-special": "p-model"},
        }
    try:
        cfg.update(_cfg)
        pm.init()
        assert pm.resolve_proxy_target(model="claude-special", purpose="oauth_anthropic") == "p-model"
        assert pm.resolve_proxy_target(model="claude-normal", purpose="oauth_anthropic") == "p-family"
        assert pm.resolve_proxy_target(model="claude-normal") == "p-default"
    finally:
        cfg.update(lambda c: c.setdefault("network", {}).update({
            "proxies": {}, "groups": {}, "routing": {"default": "direct"},
            "socks5": {"enabled": False, "url": ""},
        }))
        pm.init()
