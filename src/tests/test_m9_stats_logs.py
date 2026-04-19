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
import sys
import time


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, log_db, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import logs_menu, stats_menu
    return {
        "config": config, "log_db": log_db, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
        "logs_menu": logs_menu, "stats_menu": stats_menu,
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
    conn.commit()
    m["states"].clear_all()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _insert_success(
    m, request_id, api_key, model, channel_key, channel_type="api",
    input_tok=100, output_tok=20, cc=10, cr=50, retry_count=0, affinity_hit=0,
    connect_ms=150, first_token_ms=600, total_ms=3000, is_stream=True,
):
    ld = m["log_db"]
    ld.insert_pending(request_id, "1.1.1.1", api_key, model, is_stream,
                     msg_count=3, tool_count=0, request_headers={}, request_body={})
    ld.finish_success(
        request_id, channel_key, channel_type, model,
        input_tokens=input_tok, output_tokens=output_tok,
        cache_creation_tokens=cc, cache_read_tokens=cr,
        connect_ms=connect_ms, first_token_ms=first_token_ms, total_ms=total_ms,
        retry_count=retry_count, affinity_hit=affinity_hit,
        response_body='{"id":"x"}', http_status=200,
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
    assert ui.fmt_tokens(None) == "0"

    assert ui.fmt_rate(50, 200) == "25.0%"
    assert ui.fmt_rate(0, 0) == "N/A"
    assert ui.fmt_rate(None, None) == "N/A"

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
    # 亲和命中率（2/5 = 40%）
    assert "40.0%" in text
    print("  [PASS] stats overall with counts + flags")


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
    # 错误摘要解包
    assert "down" in text

    # 按钮中应包含 3 个 detail 短码
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"]
            for b in row if "callback_data" in b]
    assert sum(1 for b in btns if b.startswith("logs:detail:")) >= 3
    assert "logs:refresh" in btns
    print("  [PASS] logs list")


def test_logs_detail_with_retry_chain(m):
    _setup(m)
    rid = "D-rid"
    # 手工构造一条带重试链的记录
    m["log_db"].insert_pending(rid, "1.1.1.1", "k1", "claude-opus-4-7", True, 3, 0, {}, {})
    a1 = m["log_db"].record_retry_attempt(rid, 1, "api:A", "api", "claude-opus-4-7", time.time())
    time.sleep(0.01)
    m["log_db"].update_retry_attempt(a1, connect_ms=200, first_byte_ms=None, ended_at=time.time(),
                                     outcome="http_error", error_detail="HTTP 500: boom")
    a2 = m["log_db"].record_retry_attempt(rid, 2, "api:B", "api", "claude-opus-4-7", time.time())
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
    assert "重试链 (2 次尝试)" in text
    assert "api:A" in text and "api:B" in text
    assert "http_error" in text
    # Tokens / 耗时
    assert "↑" in text and "↓" in text
    # 重试 1 次 flag
    assert "重试 1 次" in text
    print("  [PASS] logs detail with retry chain")


def test_logs_detail_short_expired(m):
    _setup(m)
    rec = _install_recorder(m)
    m["logs_menu"].show_detail(42, 100, "cb", "00000000")
    edit = rec.last("editMessageText")
    assert edit and "过期" in edit["text"] or "找到" in edit["text"]
    print("  [PASS] logs detail invalid short")


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
        test_logs_detail_with_retry_chain,
        test_logs_detail_short_expired,
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
