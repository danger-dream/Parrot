"""上游模型降级 / 安全审查：信号判定、落库、展示、静音与节流。

判定口径取自官方 Codex 客户端：大小写无关的完全相等比较，不等即被改路由。
安全审查只认响应体内的 ``safety_buffering``，不认账号级的
``x-codex-safety-buffering-*`` 响应头（那个每次响应都带）。
"""
from __future__ import annotations

import copy
import json

import pytest

from src import log_db, model_reroute


# ─── 信号提取与判定 ────────────────────────────────────────────────


def _sse(*objects: dict) -> str:
    return "\n".join(f"data: {json.dumps(obj, ensure_ascii=False)}" for obj in objects)


def test_body_model_is_the_fallback_when_no_header_is_returned() -> None:
    """ChatGPT 后端经常不回 openai-model 头，body 的终态 model 才是可靠来源。"""
    signals = model_reroute.extract_response_signals(_sse(
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {"type": "response.completed", "response": {"model": "gpt-5.6-terra"}},
    ))
    assert signals.header_model is None
    assert signals.body_model == "gpt-5.6-terra"
    assert model_reroute.resolve_actual_model(
        outbound_model="gpt-5.6-sol", signals=signals,
    ) == "gpt-5.6-terra"


def test_embedded_header_model_wins_over_the_body_model() -> None:
    signals = model_reroute.extract_response_signals(_sse(
        {"type": "response.created", "response": {
            "model": "gpt-5.6-sol", "headers": {"openai-model": "gpt-5.6-terra"},
        }},
        {"type": "response.completed", "response": {"model": "gpt-5.6-luna"}},
    ))
    assert signals.header_model == "gpt-5.6-terra"
    assert model_reroute.resolve_actual_model(
        outbound_model="gpt-5.6-sol", signals=signals,
    ) == "gpt-5.6-terra"


def test_case_insensitive_exact_match_is_not_a_reroute() -> None:
    """Codex 口径：只有大小写无关的不相等才算被改路由。"""
    signals = model_reroute.extract_response_signals(_sse(
        {"type": "response.completed", "response": {"model": "GPT-5.6-SOL"}},
    ))
    assert model_reroute.resolve_actual_model(
        outbound_model="gpt-5.6-sol", signals=signals,
    ) is None
    assert model_reroute.models_differ("gpt-5.6-sol", "gpt-5.6-sol") is False
    assert model_reroute.models_differ("gpt-5.6-sol", "gpt-5.6-terra") is True


def test_missing_actual_model_is_never_reported_as_a_reroute() -> None:
    signals = model_reroute.extract_response_signals("")
    assert model_reroute.resolve_actual_model(
        outbound_model="gpt-5.6-sol", signals=signals,
    ) is None


def test_safety_review_comes_from_event_payload_not_from_account_headers() -> None:
    signals = model_reroute.extract_response_signals(_sse(
        {"type": "response.metadata", "metadata": {
            "type": "safety_buffering", "use_cases": ["cyber"], "reasons": ["policy"],
        }},
        {"type": "response.completed", "response": {"model": "gpt-5.6-sol"}},
    ))
    assert signals.safety_review == {"use_cases": ["cyber"], "reasons": ["policy"]}
    # 账号级的 safety-buffering 响应头不是信号，不应产生审查标记。
    header_only = model_reroute.extract_response_signals(_sse(
        {"type": "response.created", "response": {"headers": {
            "x-codex-safety-buffering-enabled": "true",
            "x-codex-safety-buffering-faster-model": "gpt-5.6-luna",
        }}},
    ))
    assert header_only.safety_review is None


def test_safety_review_also_reads_a_top_level_object() -> None:
    signals = model_reroute.extract_response_signals(_sse(
        {"type": "response.safety_buffering", "safety_buffering": {"retry_model": "gpt-5.6-luna"}},
    ))
    assert signals.safety_review == {"retry_model": "gpt-5.6-luna"}


def test_api_channel_snapshot_is_labelled_differently_from_an_oauth_downgrade() -> None:
    assert model_reroute.reroute_label("oauth") == "上游模型变更"
    assert model_reroute.reroute_label("api") == "与调用模型不一致"


def test_only_openai_oauth_and_openai_protocol_api_channels_are_observed() -> None:
    assert model_reroute.observes_channel("oauth:openai:x", "oauth", "openai-responses")
    assert model_reroute.observes_channel("key-1", "api", "openai-responses")
    assert not model_reroute.observes_channel("oauth:claude:x", "oauth", "anthropic")
    assert not model_reroute.observes_channel("key-2", "api", "anthropic")


# ─── 落库 ─────────────────────────────────────────────────────────


def _finish(
    rid: str, channel_key: str, channel_type: str, model: str, body: str,
    *, upstream_protocol: str = "openai-responses",
) -> None:
    log_db.init()
    handle = log_db.insert_pending(
        rid, "127.0.0.1", "test-key", model, True,
        msg_count=1, tool_count=0,
        request_headers={"Authorization": "Bearer test"},
        request_body={"model": model},
        ingress_protocol="responses",
    )
    log_db.finish_success(
        handle, channel_key, channel_type, model,
        input_tokens=1, output_tokens=1,
        response_body=body,
        upstream_protocol=upstream_protocol,
    )


def _row(rid: str) -> dict:
    conn = log_db._get_conn()
    cur = conn.execute(
        "SELECT upstream_actual_model, safety_review, model_signal_conflict FROM request_log WHERE request_id=?",
        (rid,),
    )
    row = cur.fetchone()
    assert row is not None
    return dict(row)


def test_finish_success_records_conflicting_models_and_the_review_reason() -> None:
    body = _sse(
        {"type": "response.created", "response": {"model": "gpt-5.6-sol"}},
        {"type": "response.metadata", "metadata": {"type": "safety_buffering", "use_cases": ["cyber"]}},
        {"type": "response.completed", "response": {"model": "gpt-5.6-terra"}},
    )
    _finish("reroute-1", "oauth:openai:test@example.com", "oauth", "gpt-5.6-sol", body)
    row = _row("reroute-1")
    assert row["upstream_actual_model"] is None
    assert json.loads(row["model_signal_conflict"]) == {"body_models": ["gpt-5.6-sol", "gpt-5.6-terra"]}
    assert json.loads(row["safety_review"]) == {"use_cases": ["cyber"]}


def test_finish_success_leaves_both_columns_null_without_a_signal() -> None:
    body = _sse({"type": "response.completed", "response": {"model": "gpt-5.6-sol"}})
    _finish("reroute-2", "oauth:openai:test@example.com", "oauth", "gpt-5.6-sol", body)
    row = _row("reroute-2")
    assert row["upstream_actual_model"] is None
    assert row["safety_review"] is None


def test_other_providers_are_never_marked_as_a_reroute() -> None:
    """只有 OpenAI OAuth 与 OpenAI 协议 API 渠道被观测。

    xAI/Cursor/Claude 等渠道本来就会用自己的解析结果作答（比如带日期的快照
    名），把它们读成"降级"会把正常流量刷成告警。
    """
    cases = [
        ("xai-oauth", "oauth:xai:test@example.com", "oauth", "openai-chat", "grok-4.6"),
        ("cursor-oauth", "oauth:cursor:test@example.com", "oauth", "openai-responses", "auto"),
        ("claude-oauth", "oauth:claude:test@example.com", "oauth", "anthropic", "claude-opus-5"),
    ]
    for rid, key, ctype, proto, model in cases:
        body = _sse({"type": "response.completed", "response": {"model": f"{model}-20260101"}})
        _finish(rid, key, ctype, model, body, upstream_protocol=proto)
        assert _row(rid)["upstream_actual_model"] is None, rid


def test_openai_api_channel_is_observed_when_the_protocol_is_openai() -> None:
    body = _sse({"type": "response.completed", "response": {"model": "gpt-5.6-terra"}})
    _finish("api-openai", "key-openai", "api", "gpt-5.6-sol", body,
            upstream_protocol="openai-responses")
    assert _row("api-openai")["upstream_actual_model"] == "gpt-5.6-terra"


def test_router_rewrite_of_the_outbound_model_is_not_a_reroute() -> None:
    """Parrot 自己把 sol 路由到 terra 时，出站就是 terra，上游没有偷换模型。"""
    body = _sse({"type": "response.completed", "response": {"model": "gpt-5.6-terra"}})
    _finish("router-rewrite", "oauth:openai:test@example.com", "oauth", "gpt-5.6-terra", body)
    assert _row("router-rewrite")["upstream_actual_model"] is None


def test_finish_error_also_records_the_observation() -> None:
    log_db.init()
    handle = log_db.insert_pending(
        "reroute-err", "127.0.0.1", "test-key", "gpt-5.6-sol", True,
        msg_count=1, tool_count=0,
        request_headers={"Authorization": "Bearer test"},
        request_body={"model": "gpt-5.6-sol"},
        ingress_protocol="responses",
    )
    log_db.finish_error(
        handle, "upstream 500",
        final_channel_key="oauth:openai:test@example.com",
        final_channel_type="oauth",
        final_model="gpt-5.6-sol",
        response_body=_sse({"type": "response.completed", "response": {"model": "gpt-5.6-terra"}}),
        upstream_protocol="openai-responses",
    )
    assert _row("reroute-err")["upstream_actual_model"] == "gpt-5.6-terra"


def test_narrow_update_does_not_touch_other_summary_columns() -> None:
    body = _sse({"type": "response.completed", "response": {"model": "gpt-5.6-sol"}})
    _finish("reroute-narrow", "oauth:openai:test@example.com", "oauth", "gpt-5.6-sol", body)
    assert log_db.record_upstream_model_observation(
        "reroute-narrow", actual_model="gpt-5.6-terra", safety_review='{"reasons":["policy"]}',
    ) is True
    row = _row("reroute-narrow")
    assert row["upstream_actual_model"] == "gpt-5.6-terra"
    assert row["safety_review"] == '{"reasons":["policy"]}'
    # 窄更新只改这两列：同行的模型/状态事实保持原样。
    conn = log_db._get_conn()
    other = conn.execute(
        "SELECT final_model, status FROM request_log WHERE request_id=?",
        ("reroute-narrow",),
    ).fetchone()
    assert other["final_model"] == "gpt-5.6-sol"
    assert other["status"] == "success"


# ─── 展示 ─────────────────────────────────────────────────────────


def _ui():
    from src.telegram import ui

    return ui


def test_list_row_appends_the_reroute_mark_after_the_timing_line() -> None:
    ui = _ui()
    text = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-sol",
        "final_model": "gpt-5.6-sol",
        "final_channel_type": "oauth",
        "status": "success",
        "total_time_ms": 33300,
        "upstream_actual_model": "gpt-5.6-terra",
    })
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1] == "  ⚠️ 上游模型变更：→ <code>gpt-5.6-terra</code>"
    # 变更行只写上游报告的模型，不重复请求模型或声称能力降低。
    assert "gpt-5.6-sol" not in lines[-1]


def test_list_row_uses_the_api_wording_for_an_api_channel() -> None:
    ui = _ui()
    text = ui.fmt_log_entry_body({
        "requested_model": "gpt-4o",
        "final_model": "gpt-4o",
        "final_channel_type": "api",
        "status": "success",
        "upstream_actual_model": "gpt-4o-2024-08-06",
    })
    assert "与调用模型不一致：↓ <code>gpt-4o-2024-08-06</code>" in text
    assert "模型降级" not in text


def test_list_row_marks_a_safety_review_without_a_model_name() -> None:
    ui = _ui()
    text = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-sol",
        "final_model": "gpt-5.6-sol",
        "final_channel_type": "oauth",
        "status": "success",
        "safety_review": '{"use_cases":["cyber"]}',
    })
    assert "🛡️ 本次请求被标记为需要额外安全审查（响应可能较慢）" in text
    assert "cyber" not in text          # 列表页不带原因
    assert "gpt-5.6-terra" not in text


def test_list_row_is_unchanged_without_any_signal() -> None:
    ui = _ui()
    text = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-sol",
        "final_model": "gpt-5.6-sol",
        "final_channel_type": "oauth",
        "status": "success",
    })
    assert "模型降级" not in text
    assert "安全审查" not in text


def test_list_row_escapes_the_upstream_model() -> None:
    ui = _ui()
    text = ui.fmt_log_entry_body({
        "requested_model": "gpt-5.6-sol",
        "final_model": "gpt-5.6-sol",
        "final_channel_type": "oauth",
        "status": "success",
        "upstream_actual_model": "<script>x</script>",
    })
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_detail_page_shows_the_review_reason_and_keeps_it_out_of_the_bottom() -> None:
    from src.telegram.menus import logs_menu

    text = logs_menu._render_detail({
        "log": {
            "request_id": "rid",
            "requested_model": "gpt-5.6-sol",
            "final_model": "gpt-5.6-sol",
            "final_channel_type": "oauth",
            "status": "success",
            "upstream_actual_model": "gpt-5.6-terra",
            "safety_review": '{"use_cases":["cyber"],"reasons":["policy"]}',
        },
    })
    assert "上游模型变更" in text
    assert "gpt-5.6-terra" in text
    assert "cyber" in text and "policy" in text


# ─── 静音与节流 ───────────────────────────────────────────────────


def test_a_mute_suppresses_only_its_own_channel() -> None:
    from src import state_db

    state_db.init()
    code = model_reroute.channel_code("oauth:openai:muted@example.com")
    model_reroute.mute_channel(None, code=code, days=1)
    try:
        assert model_reroute.muted_until("oauth:openai:muted@example.com") is not None
        assert model_reroute.muted_until("oauth:openai:other@example.com") is None
    finally:
        model_reroute.unmute_channel(code=code)


def test_an_expired_mute_no_longer_suppresses() -> None:
    from src import state_db

    state_db.init()
    code = model_reroute.channel_code("oauth:openai:expired@example.com")
    model_reroute.mute_channel(None, code=code, days=-1)
    assert model_reroute.muted_until("oauth:openai:expired@example.com") is None
    model_reroute.unmute_channel(code=code)


def test_only_an_oauth_channel_alert_is_notified() -> None:
    """API 渠道的不一致只记录，不通知（带日期快照名会让它每条都响）。"""
    api_obs = model_reroute.RerouteObservation(
        request_id="r", api_key_name="k", requested_model="gpt-4o",
        outbound_model="gpt-4o", actual_model="gpt-4o-2024-08-06",
        channel_key="key-1", channel_type="api",
    )
    assert model_reroute.notify_reroute(api_obs) is False


def test_the_alert_carries_three_mute_buttons() -> None:
    buttons = model_reroute.build_mute_buttons("oauth:openai:test@example.com")
    labels = [b["text"] for row in buttons["inline_keyboard"] for b in row]
    assert len(labels) == 3
    assert any("今天" in label for label in labels)
    assert any("7 天" in label for label in labels)
    assert any("永久" in label for label in labels)


def test_throttle_sends_only_once_inside_the_two_hour_window(monkeypatch) -> None:
    from src import notifier

    sent: list[str] = []
    monkeypatch.setattr(notifier, "notify_event", lambda *a, **k: sent.append(a[0]) or True)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(notifier._t, "time", lambda: clock["now"])
    notifier._throttle_last_sent.clear()

    obs = model_reroute.RerouteObservation(
        request_id="r", api_key_name="k", requested_model="gpt-5.6-sol",
        outbound_model="gpt-5.6-sol", actual_model="gpt-5.6-terra",
        channel_key="oauth:openai:throttle@example.com", channel_type="oauth",
    )
    assert model_reroute.notify_reroute(obs) is True
    clock["now"] += 60
    assert model_reroute.notify_reroute(obs) is False      # 同一渠道同一模型对，2 小时内只发一次
    clock["now"] += model_reroute.NOTIFY_COOLDOWN_SECONDS
    assert model_reroute.notify_reroute(obs) is True


def test_a_disabled_event_switch_suppresses_the_alert(monkeypatch) -> None:
    """事件开关关闭时，降级告警不应进入发送队列（走真实 notify_event 判定）。"""
    from src import config, notifier

    queued: list[str] = []
    # 拦截最底层的入队函数，保留 notify_event 自己的开关判定。
    monkeypatch.setattr(notifier, "notify", lambda text, **k: queued.append(text))
    notifier._throttle_last_sent.clear()

    original = copy.deepcopy(config.get().get("notifications"))
    try:
        def _switch_off(cfg):
            notifications = cfg.setdefault("notifications", {})
            notifications["enabled"] = True
            events = notifications.setdefault("events", {})
            events["model_degraded"] = False
        config.update(_switch_off)

        obs = model_reroute.RerouteObservation(
            request_id="r", api_key_name="k", requested_model="gpt-5.6-sol",
            outbound_model="gpt-5.6-sol", actual_model="gpt-5.6-terra",
            channel_key="oauth:openai:switched-off@example.com", channel_type="oauth",
        )
        model_reroute.notify_reroute(obs)
        assert queued == []
    finally:
        if original is not None:
            config.update(lambda cfg: cfg.__setitem__("notifications", original))
        notifier._throttle_last_sent.clear()


def test_the_event_key_and_cooldown_match_the_agreed_values() -> None:
    assert model_reroute.NOTIFY_EVENT_KEY == "model_degraded"
    assert model_reroute.NOTIFY_COOLDOWN_SECONDS == 2 * 3600
