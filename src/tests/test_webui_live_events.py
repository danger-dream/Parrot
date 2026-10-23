"""WS /api/management/v1/events and the request-path event hook."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src import log_db, request_events
from src.management_api.routers import live_events as live_module
from src.management_api.routers.live_events import encode_event
from src.management_control.observability.live_events import LiveEventBus
from src.tests.webui_support import (
    API, MonthlyLogStore, bearer, build_app, key_session,
)


WS = f"{API}/events"
SAME_ORIGIN = {"origin": "http://testserver"}
LOG_RECORD = {
    "id": "req-live", "status": "success", "createdAt": 1_760_000_000,
    "apiKeyName": "client", "requestedModel": "model-a", "finalModel": "model-a",
    "channelId": "api:alpha", "protocol": "anthropic", "transport": "http",
    "retryCount": 1, "durationMilliseconds": 900, "inputTokens": 10, "outputTokens": 20,
    "costTicks": 30, "billing": {
        "costTicks": 30, "actualCostTicks": 30, "estimatedCostTicks": 0,
        "actualCostedSuccess": 1, "estimatedCostedSuccess": 0,
        "costedSuccess": 1, "unpricedSuccess": 0,
    }, "error": None, "revision": "rev_live",
}
SNAPSHOT = {
    "channelTotals": {"inFlight": 1, "waiting": 0, "trackedChannels": 1},
    "channels": [], "apiKeyTotals": {}, "apiKeys": [], "revision": "rev_snapshot",
}


class FakeLogs:
    def __init__(self):
        self.calls = []

    def list_records(self, context, ids, *, month=None):
        self.calls.append((list(ids), month, threading.current_thread().name))
        return [{**LOG_RECORD, "id": request_id} for request_id in ids]


class FakeStatus:
    def __init__(self):
        self.calls = 0

    def api_concurrency_snapshot(self, context):
        self.calls += 1
        return dict(SNAPSHOT)


@pytest.fixture(autouse=True)
def clean_hook():
    request_events._reset_for_tests()
    yield
    request_events._reset_for_tests()


def _install_bus(runtime, **kwargs):
    logs, status = FakeLogs(), FakeStatus()
    options = {"snapshot_interval": 0.05, "ping_interval": 0.1, "finished_delay": 0.0, **kwargs}
    bus = LiveEventBus(logs=logs, status=status, encode=encode_event, **options)
    runtime.live_events = bus
    return bus, logs, status


def _receive_until(ws, event_type, limit=200):
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == event_type:
            return message
    raise AssertionError(f"no {event_type}")


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _closed_code(client, headers) -> int:
    with client.websocket_connect(WS, headers=headers) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    return exc.value.code


# ── request-path hook ───────────────────────────────────────────────

def test_hook_is_noop_without_listeners_and_swallows_listener_errors():
    assert request_events.active() is False
    request_events.publish("started", {"id": "x"})  # no listener: no effect
    seen = []

    def broken(kind, payload):
        raise RuntimeError("listener failure must not reach the request path")

    unsubscribe_broken = request_events.subscribe(broken)
    unsubscribe_seen = request_events.subscribe(lambda kind, payload: seen.append(kind))
    assert request_events.active() is True
    request_events.publish("started", {"id": "x"})
    assert seen == ["started"]
    unsubscribe_broken()
    unsubscribe_seen()
    unsubscribe_seen()  # idempotent
    assert request_events.active() is False


def test_log_db_emits_small_payloads_only_when_subscribed(monkeypatch, tmp_path):
    store = MonthlyLogStore(monkeypatch, tmp_path)
    try:
        calls = []
        original_publish = request_events.publish
        monkeypatch.setattr(request_events, "publish", lambda kind, payload: calls.append(kind))
        log_db.insert_pending("quiet-1", "127.0.0.1", "client", "model-a", True, 1, 0, None, None)
        assert calls == []  # inactive hook: payload is never even built
        monkeypatch.setattr(request_events, "publish", original_publish)

        events = []
        unsubscribe = request_events.subscribe(lambda kind, payload: events.append((kind, dict(payload))))
        try:
            handle = log_db.insert_pending(
                "live-1", "203.0.113.5", "client", "model-a", True, 2, 0, None, None,
                ingress_protocol="chat", reasoning_effort="high", fast_mode=True,
            )
            log_db.record_retry_attempt(handle, 2, "api:beta", "api", "model-b", time.time())
            log_db.finish_success(handle, "api:beta", "api", "model-b", input_tokens=3, output_tokens=4)
        finally:
            unsubscribe()
        kinds = [kind for kind, _ in events]
        assert kinds == ["started", "updated", "finished"]
        started = events[0][1]
        assert started == {
            "id": "live-1", "createdAt": started["createdAt"], "apiKeyName": "client",
            "requestedModel": "model-a", "protocol": "chat", "isStream": True,
            "clientIp": "203.0.113.5", "reasoningEffort": "high", "fastMode": True,
        }
        assert events[1][1] == {"id": "live-1", "channelId": "api:beta", "model": "model-b", "retryCount": 1}
        assert events[2][1] == {"id": "live-1", "month": handle.db.month}

        def explode(kind, payload):
            raise RuntimeError("boom")

        unsubscribe = request_events.subscribe(explode)
        try:
            handle = log_db.insert_pending("live-2", "127.0.0.1", None, None, False, 1, 0, None, None)
            log_db.finish_error(handle, "upstream failed")
        finally:
            unsubscribe()
        row = log_db._get_conn().execute(
            "SELECT status FROM request_log WHERE request_id='live-2'",
        ).fetchone()
        assert row["status"] == "error"
    finally:
        store.close()


# ── WebSocket authentication and Origin ─────────────────────────────

def test_ws_rejects_unauthenticated_cross_origin_and_over_capacity(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    _install_bus(runtime, max_clients=1)
    with TestClient(app) as client:
        credential = key_session(client).json()["data"]["credential"]
        assert _closed_code(client, {}) == 4401
        assert _closed_code(client, bearer("pms_bad.invalid")) == 4401
        assert _closed_code(client, {**bearer(credential), "origin": "https://evil.example"}) == 4403
        # A cookie without Origin can only be a forged/hijacked socket.
        cookie = {"cookie": f"parrot_management_session={credential}"}
        assert _closed_code(client, cookie) == 4403
        assert _closed_code(client, {**cookie, "origin": "https://evil.example"}) == 4403
        with client.websocket_connect(WS, headers={**cookie, **SAME_ORIGIN}) as first:
            assert first.receive_json()["type"] == "hello"
            assert _closed_code(client, bearer(credential)) == 1013
        _wait(lambda: runtime.live_events.subscriber_count == 0)


def test_ws_hello_and_bearer_without_origin(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    _install_bus(runtime)
    with TestClient(app) as client:
        session = key_session(client).json()["data"]
        with client.websocket_connect(WS, headers=bearer(session["credential"])) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["ts"].endswith("Z")
            assert hello["data"]["version"] == "0.webui-test"
            assert hello["data"]["serverTime"].endswith("Z")
            parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
            expected = min(parse(session["session"]["expiresAt"]), parse(session["session"]["idleExpiresAt"]))
            assert parse(hello["data"]["sessionExpiresAt"]) == expected
            ws.send_text("ignored client message")
            assert _receive_until(ws, "ping")["data"] == {}


# ── event types ─────────────────────────────────────────────────────

def test_ws_delivers_request_and_runtime_events(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    bus, logs, status = _install_bus(runtime)
    with TestClient(app) as client:
        credential = key_session(client).json()["data"]["credential"]
        with client.websocket_connect(WS, headers=bearer(credential)) as ws:
            assert ws.receive_json()["type"] == "hello"
            assert request_events.active() is True
            request_events.publish("started", {
                "id": "req-live", "createdAt": 1_760_000_000.5, "apiKeyName": "client",
                "requestedModel": "model-a", "protocol": "anthropic", "isStream": True,
                "clientIp": "203.0.113.5", "reasoningEffort": None, "fastMode": False,
            })
            started = _receive_until(ws, "request.started")
            assert started["data"] == {
                "id": "req-live", "createdAt": "2025-10-09T08:53:20.500000Z",
                "apiKeyName": "client", "requestedModel": "model-a", "protocol": "anthropic",
                "isStream": True, "clientIp": "203.0.113.5", "reasoningEffort": None,
                "fastMode": False,
            }
            request_events.publish("updated", {"id": "req-live", "channelId": "api:beta", "model": "m", "retryCount": 1})
            assert _receive_until(ws, "request.updated")["data"] == {
                "id": "req-live", "channelId": "api:beta", "model": "m", "retryCount": 1,
            }
            request_events.publish("updated", {"id": "req-live"})
            assert _receive_until(ws, "request.updated")["data"] == {"id": "req-live"}

            request_events.publish("finished", {"id": "req-live", "month": "2025-10"})
            finished = _receive_until(ws, "request.finished")
            from src.management_api.schemas.observability import RequestLogData
            assert finished["data"]["log"] == RequestLogData.model_validate(LOG_RECORD).model_dump(mode="json")
            ids, month, thread_name = logs.calls[0]
            assert ids == ["req-live"] and month == "2025-10"
            assert thread_name != "MainThread"  # enrichment ran off the event loop

            snapshot = _receive_until(ws, "runtime.snapshot")
            # Exactly the GET /runtime/concurrency data mapping.
            from src.management_api.routers.status import _revisioned
            from src.management_api.schemas.observability import ConcurrencyData
            assert snapshot["data"] == _revisioned(ConcurrencyData, SNAPSHOT).model_dump(mode="json")
            assert snapshot["data"]["revision"] == "rev_snapshot"
            assert _receive_until(ws, "ping")["type"] == "ping"
    _wait(lambda: request_events.active() is False)


def test_ws_queue_overflow_collapses_into_resync(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    bus, _logs, _status = _install_bus(runtime, queue_size=3, snapshot_interval=60)
    with TestClient(app) as client:
        credential = key_session(client).json()["data"]["credential"]
        with client.websocket_connect(WS, headers=bearer(credential)) as ws:
            assert ws.receive_json()["type"] == "hello"
            done = threading.Event()

            def flood():
                for index in range(50):
                    bus.broadcast("request.updated", {"id": f"r{index}"})
                done.set()

            bus._loop.call_soon_threadsafe(flood)
            assert done.wait(2)
            messages = [ws.receive_json() for _ in range(1)]
            assert messages[0]["type"] == "resync"
            assert messages[0]["data"] == {"reason": "overflow"}
            # Events after the overflow flow normally again.
            request_events.publish("updated", {"id": "after"})
            for _ in range(10):
                message = ws.receive_json()
                if message["type"] == "request.updated" and message["data"] == {"id": "after"}:
                    break
            else:
                raise AssertionError("events did not resume after resync")
            subscription = bus._subscribers[0]
            assert subscription.dropped >= 47


def test_ws_closes_4401_when_session_revoked_during_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(live_module, "SESSION_CHECK_SECONDS", 0.05)
    app, runtime, _ = build_app(tmp_path)
    _install_bus(runtime, snapshot_interval=60)
    with TestClient(app) as client:
        session = key_session(client).json()["data"]
        with client.websocket_connect(WS, headers=bearer(session["credential"])) as ws:
            assert ws.receive_json()["type"] == "hello"
            runtime.state_store.revoke_session(session["session"]["sessionId"])
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(100):
                    ws.receive_text()
            assert exc.value.code == 4401


def test_runtime_close_ends_streams_and_unhooks(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    _install_bus(runtime, snapshot_interval=60)
    with TestClient(app) as client:
        credential = key_session(client).json()["data"]["credential"]
        with client.websocket_connect(WS, headers=bearer(credential)) as ws:
            assert ws.receive_json()["type"] == "hello"
            runtime.close()
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(100):
                    ws.receive_text()
            assert exc.value.code == 1001
    assert request_events.active() is False


# ── end-to-end with the real log store and list mapping ─────────────

def test_finished_event_equals_logs_list_item(tmp_path, monkeypatch):
    store = MonthlyLogStore(monkeypatch, tmp_path)
    app, runtime, _ = build_app(tmp_path)
    try:
        bus = live_module.event_bus(runtime)
        bus.finished_delay = 0.0
        with TestClient(app) as client:
            credential = key_session(client).json()["data"]["credential"]
            with client.websocket_connect(WS, headers=bearer(credential)) as ws:
                assert ws.receive_json()["type"] == "hello"
                handle = log_db.insert_pending(
                    "e2e-1", "203.0.113.9", "client", "model-a", True, 1, 0, None, None,
                )
                started = _receive_until(ws, "request.started")
                assert started["data"]["id"] == "e2e-1"
                log_db.record_retry_attempt(handle, 1, "api:alpha", "api", "model-a", time.time())
                log_db.finish_success(
                    handle, "api:alpha", "api", "model-a", input_tokens=12,
                    output_tokens=34, total_ms=250, retry_count=0,
                )
                finished = _receive_until(ws, "request.finished")
            listed = client.get(f"{API}/logs", headers=bearer(credential)).json()["data"]
        item = next(row for row in listed if row["id"] == "e2e-1")
        assert finished["data"]["log"] == item
    finally:
        store.close()
