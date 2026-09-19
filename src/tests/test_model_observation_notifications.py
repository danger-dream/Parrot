"""Parrot model observations: truthful notices, conflict facts, enqueue-only throttle."""
from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import config, log_db, model_reroute, notifier, upstream
from src.failover import _WsResponsesTracker
from src.openai.responses_ws import _WsTracker
from src.telegram import ui
from src.telegram.menus import logs_menu
from src.tests.test_model_reroute import _finish, _row, _sse


@pytest.fixture
def notifications(monkeypatch):
    settings = {"notifications": {"enabled": True, "events": {"model_degraded": True}}}
    pending = queue.Queue(maxsize=1)
    monkeypatch.setattr(notifier, "_ensure_worker", lambda: None)
    monkeypatch.setattr(notifier, "_queue", pending)
    monkeypatch.setattr(notifier, "_throttle_last_sent", {})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(model_reroute, "_account_label", lambda key: "fixture")
    monkeypatch.setattr(model_reroute, "muted_until", lambda key: None)
    return settings, pending


def observation(**extra):
    return model_reroute.RerouteObservation(**{
        "request_id": "r", "api_key_name": "client", "requested_model": "alias",
        "outbound_model": "model-a", "actual_model": "model-b",
        "channel_key": "oauth:openai:fixture", "channel_type": "oauth", **extra,
    })


@pytest.mark.parametrize("gate", ["global", "event", "mute"])
def test_disabled_or_muted_does_not_consume_throttle(notifications, monkeypatch, gate):
    settings, pending = notifications
    if gate == "global":
        settings["notifications"]["enabled"] = False
    elif gate == "event":
        settings["notifications"]["events"]["model_degraded"] = False
    else:
        monkeypatch.setattr(model_reroute, "muted_until", lambda key: 9999999999)
    assert model_reroute.notify_reroute(observation()) is False
    assert pending.empty() and notifier._throttle_last_sent == {}
    settings["notifications"]["enabled"] = True
    settings["notifications"]["events"]["model_degraded"] = True
    monkeypatch.setattr(model_reroute, "muted_until", lambda key: None)
    assert model_reroute.notify_reroute(observation()) is True
    text = pending.get_nowait()[0]
    assert "上游模型变更" in text and "模型降级" not in text
    assert all(label in text for label in ("请求模型:", "出站模型:", "上游报告模型:"))
    assert model_reroute.notify_reroute(observation()) is False
    assert pending.empty()


def test_queue_full_does_not_consume_throttle(notifications):
    _, pending = notifications
    pending.put_nowait(("already queued", None, None, None))
    assert model_reroute.notify_reroute(observation()) is False
    assert notifier._throttle_last_sent == {}
    pending.get_nowait()
    assert model_reroute.notify_reroute(observation()) is True
    assert pending.qsize() == 1


def test_concurrent_notifications_only_enqueue_one(notifications):
    _, pending = notifications
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: model_reroute.notify_reroute(observation()), range(16)))
    assert results.count(True) == 1 and pending.qsize() == 1


@pytest.mark.asyncio
async def test_async_and_sync_notification_share_committed_throttle(notifications):
    settings, pending = notifications
    settings["notifications"]["enabled"] = False
    await notifier.throttled_notify_event("model_degraded", "same-key", "async")
    assert notifier._throttle_last_sent == {}
    settings["notifications"]["enabled"] = True
    assert notifier.throttled_notify_event_sync("model_degraded", "same-key", "sync") is True
    pending.get_nowait()
    await notifier.throttled_notify_event("model_degraded", "same-key", "async")
    assert pending.empty()


@pytest.mark.parametrize("kind", ["sse", "http-to-ws", "ws"])
def test_earlier_model_change_survives_a_later_matching_value(kind):
    events = [
        {"type": "response.created", "response": {"headers": {"openai-model": "model-b"}}},
        {"type": "response.completed", "response": {"headers": {"openai-model": "model-a"}}},
    ]
    if kind == "sse":
        tracker = upstream.ResponsesSSEUsageTracker()
        for event in events:
            tracker.feed(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
        signals = model_reroute.extract_response_signals(tracker.get_full_response())
    else:
        tracker = _WsResponsesTracker() if kind == "http-to-ws" else _WsTracker()
        for event in events:
            tracker.feed_text(json.dumps(event))
        signals = tracker.response_signals
    actual, _, conflict = log_db._upstream_observation(
        "", "model-a", channel_key="oauth:openai:fixture", response_signals=signals,
    )
    assert actual is None
    assert json.loads(conflict)["event_headers"] == ["model-b", "model-a"]


@pytest.mark.parametrize("body,header,expected,conflicting", [
    ({"type": "response.completed", "response": {"model": "model-b"}}, None, "model-b", False),
    ({"type": "response.completed", "response": {"model": "MODEL-B", "headers": {"openai-model": "model-b"}}}, "Model-B", "model-b", False),
    ({"type": "response.completed", "response": {"model": "model-b"}}, "model-a", None, True),
    ({"type": "response.completed", "response": {"headers": {"openai-model": "model-a"}}}, "model-b", None, True),
])
def test_body_fallback_agreement_and_conflict(body, header, expected, conflicting):
    actual, _, conflict = log_db._upstream_observation(
        json.dumps(body), "model-a", channel_key="oauth:openai:fixture", http_header_model=header,
    )
    assert actual == expected and bool(conflict) == conflicting


@pytest.mark.parametrize("error", [False, True])
def test_conflict_is_persisted_notified_and_displayed_without_false_actual(monkeypatch, error):
    rid = f"model-conflict-{error}"
    log_db.init()
    handle = log_db.insert_pending(rid, "127.0.0.1", "client", "alias", True, 1, 0, {}, {})
    queued = []
    monkeypatch.setattr(model_reroute, "muted_until", lambda key: None)
    monkeypatch.setattr(notifier, "throttled_notify_event_sync", lambda event, key, text, **kw: queued.append((key, text)) or True)
    kwargs = dict(final_channel_key="oauth:openai:fixture", final_channel_type="oauth", final_model="model-a",
                  upstream_protocol="openai-responses", http_header_model="model-a",
                  response_body=json.dumps({"model": "model-b", "safety_buffering": {"reasons": ["policy"]}}))
    if error:
        log_db.finish_error(handle, "upstream error", **kwargs)
    else:
        log_db.finish_success(handle, **kwargs)
    row = _row(rid)
    assert row["upstream_actual_model"] is None
    assert json.loads(row["model_signal_conflict"]) == {"http_header": ["model-a"], "body_models": ["model-b"]}
    assert json.loads(row["safety_review"]) == {"reasons": ["policy"]}
    assert len(queued) == 1 and "模型信息不一致" in queued[0][1]
    assert "上游报告模型:" not in queued[0][1] and "无法据此确定实际模型" in queued[0][1]
    rendered = ui.log_upstream_observation_lines(row)
    assert "模型信息不一致" in rendered[0] and "model-b" not in rendered[0]
    detail = "\n".join(logs_menu._upstream_observation_detail_lines(row))
    assert "HTTP 模型头: model-a" in detail and "正文模型: model-b" in detail
    assert "policy" in detail


def test_api_keeps_previous_precedence_and_never_sends_conflict_notice(monkeypatch):
    notices = []
    monkeypatch.setattr(notifier, "throttled_notify_event_sync", lambda *a, **k: notices.append(a) or True)
    body = _sse({"type": "response.completed", "response": {
        "model": "snapshot-model", "headers": {"openai-model": "header-model"},
    }})
    _finish("model-api-conflict", "api:fixture", "api", "model-a", body)
    row = _row("model-api-conflict")
    assert row["upstream_actual_model"] == "header-model" and row["model_signal_conflict"] is None
    assert notices == []


def test_conflict_and_model_change_have_separate_stable_throttle_keys(notifications):
    _, pending = notifications
    conflict = {"http_header": ["model-a"], "body_models": ["model-b"]}
    assert model_reroute.notify_reroute(observation(actual_model="", model_conflict=conflict)) is True
    pending.get_nowait()
    assert model_reroute.notify_reroute(observation()) is True
    pending.get_nowait()
    same = {"body_models": ["MODEL-B"], "http_header": ["MODEL-A"]}
    assert model_reroute.notify_reroute(observation(actual_model="", model_conflict=same)) is False


def test_old_schema_adds_nullable_conflict_column_without_rewriting_history(tmp_path):
    _finish("schema-before-conflict", "api:fixture", "api", "model-a", '{"model":"model-b"}')
    old = sqlite3.connect(tmp_path / "old.db")
    log_db._get_conn().backup(old)
    old.execute("ALTER TABLE request_log DROP COLUMN model_signal_conflict")
    old.commit()
    log_db._ensure_migrations(old)
    assert old.execute("SELECT model_signal_conflict FROM request_log WHERE request_id='schema-before-conflict'").fetchone() == (None,)
    assert old.execute("SELECT upstream_actual_model FROM request_log WHERE request_id='schema-before-conflict'").fetchone() == ("model-b",)
    old.close()
