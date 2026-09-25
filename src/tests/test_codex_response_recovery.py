"""Codex middle-layer recovery: preserve semantics and use existing owners."""
from __future__ import annotations

import asyncio
import json
import time
import httpx
from types import SimpleNamespace

import pytest

from src.tests.test_openai_responses_ws import (
    _import_modules, _setup, _make_oauth_channel_for_failover,
    _last_request_log, _retry_chain, _isolate_ws_config,
    FakeWebSocket, SequentialFakeWebSocket, FakeUpstreamWebSocket,
    FakeOAuthHttpWs, _call_failover_responses,
)
from src.openai import recovery
from src.protocols.runtime import AttemptResult


def test_deadline_is_captured_once_and_not_clamped_to_wait_budget(monkeypatch):
    clock = [1000.0, 100.0]
    monkeypatch.setattr(recovery.time, "time", lambda: clock[0])
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[1])
    r = AttemptResult(outcome="http_error", http_status=429,
                      error_detail='{"error":{"code":"slow_down"}}')
    recovery.capture_error_advice(r, headers={"Retry-After": "300"})
    assert r.cooldown_until == 1300000
    assert recovery.remaining_retry_delay(r) == 300
    clock[:] = [1004.0, 104.0]
    recovery.capture_error_advice(r, headers={"Retry-After": "300"})
    assert recovery.remaining_retry_delay(r) == 296
    assert r.cooldown_until == 1300000


@pytest.mark.parametrize("code,kind", [
    ("usage_limit_reached", "usage_limit"),
    ("usage_not_included", "entitlement"),
    ("organization_spend_limit_exceeded", "quota"),
    ("project_spend_limit_exceeded", "quota"),
    ("rate_limit_exceeded", "rate_limit"), ("slow_down", "rate_limit"),
])
def test_http_sse_and_ws_errors_share_semantics(code, kind):
    error = {"code": code, "message": "Please try again in 11.054s."}
    values = [{"error": error}, {"type": "error", "status": 429, "error": error},
              {"type": "response.failed", "response": {"error": error}}]
    for payload in values:
        r = AttemptResult(outcome="upstream_error_json")
        recovery.capture_error_advice(r, payload=payload)
        assert r.error_advice.kind == kind
        assert r.http_status == 429
        if kind == "rate_limit":
            assert 10 < recovery.remaining_retry_delay(r) <= 11.054


def test_ws_frame_quota_headers_are_not_lost():
    now = int(time.time())
    r = AttemptResult(outcome="upstream_error_json")
    recovery.capture_error_advice(r, codex=True, headers={"x-codex-active-limit": "codex"}, payload={
        "type": "error", "status": 429,
        "headers": {"x-codex-active-limit": "gpt-reserve", "Retry-After": "300"},
        "error": {"type": "usage_limit_reached", "resets_at": now + 3600},
    })
    assert r.error_advice.active_limit == "gpt_reserve"
    assert r.error_advice.reset_at == now + 3600
    recovery.capture_error_advice(r, codex=True)  # another owning layer
    assert r.error_advice.active_limit == "gpt_reserve"


@pytest.mark.parametrize("family,kind,account_disabled", [
    ("codex", "usage_limit_reached", True),
    ("gpt-reserve", "usage_limit_reached", False),
    ("codex", "usage_not_included", False),
    ("codex", "slow_down", False),
])
def test_quota_scope_uses_existing_account_or_model_gate(m, family, kind, account_disabled):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    r = AttemptResult(outcome="http_error", http_status=429)
    reset = int(time.time()) + 3600
    recovery.capture_error_advice(r, codex=True, headers={"x-codex-active-limit": family},
                                 payload={"error": {"type": kind, "resets_at": reset}})
    assert m["failover"]._apply_codex_error_policy(ch, "test-model", r)
    from src import oauth_manager
    acc = oauth_manager.get_account(ch.account_key)
    assert (acc.get("disabled_reason") == "quota") == account_disabled
    if account_disabled:
        hit = oauth_manager._cached_openai_codex_quota_hit(ch.account_key, 95, usage={})
        assert hit["any_over"]
        assert not m["cooldown"].is_blocked(ch.key, "test-model")
    else:
        assert m["cooldown"].is_blocked(ch.key, "test-model")
    assert not (m["scorer"].get_stats(ch.key, "test-model") or {}).get("total_requests", 0)


def test_setup_resets_quota_cache_for_reused_fake_account(m):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    future = int(time.time()) + 60
    m["state_db"].quota_save_openai_snapshot(ch.account_key, {
        "fetched_at": future * 1000,
        "primary_used_pct": 0, "primary_window_min": 300,
        "primary_reset_at": future + 3600,
        "secondary_used_pct": 0, "secondary_window_min": 10080,
        "secondary_reset_at": future + 604800,
    })
    assert m["state_db"].quota_load(ch.account_key) is not None
    _setup(m)
    recreated = _make_oauth_channel_for_failover(m)
    assert recreated.account_key == ch.account_key
    assert m["state_db"].quota_load(recreated.account_key) is None


def _done(response_id="recovered"):
    return [{"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_text.delta", "delta": "ok", "output_index": 0, "content_index": 0},
            {"type": "response.completed", "response": {"id": response_id, "output": [], "usage": {}}}]


@pytest.mark.asyncio
async def test_native_ws_connection_expiry_recovers_same_account_once(m, monkeypatch):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}
    first = FakeUpstreamWebSocket([{"type": "error", "status": 400, "error": {
        "code": "websocket_connection_limit_reached", "message": "connect again"}}])
    second = FakeUpstreamWebSocket(_done())
    sockets = [first, second]
    seen = []
    async def connect(url, **kwargs):
        seen.append((url, dict(kwargs["headers"])))
        return sockets.pop(0)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello"})
    await m["responses_ws"].handle_responses_ws(ws)
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0]
    assert any(json.loads(x).get("delta") == "ok" for x in ws.sent_texts)
    assert not any(json.loads(x).get("type") == "error" for x in ws.sent_texts)
    row = _last_request_log(m)
    assert row["status"] == "success"
    assert [x["outcome"] for x in _retry_chain(m, row["request_id"])] == ["connection_lifecycle", "success"]
    assert not m["cooldown"].is_blocked(ch.key, "test-model")


@pytest.mark.asyncio
async def test_ws_does_not_replay_after_response_created(m, monkeypatch):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    with m["registry"]._lock:
        m["registry"]._channels = {ch.key: ch}
    seen = []
    async def connect(url, **kwargs):
        seen.append(url)
        return FakeUpstreamWebSocket([
            {"type": "response.created", "response": {"id": "already-created"}},
            {"type": "error", "status": 400, "error": {
                "code": "websocket_connection_limit_reached", "message": "connect again"}},
        ])
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    ws = SequentialFakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello"})
    await m["responses_ws"].handle_responses_ws(ws)
    assert len(seen) == 1
    assert any(json.loads(x).get("type") == "error" for x in ws.sent_texts)


@pytest.mark.asyncio
async def test_native_ws_recovers_second_turn_from_connection_history_without_store(m, monkeypatch):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    first = FakeUpstreamWebSocket(_done("first") + [{"type": "error", "status": 400, "error": {
        "code": "previous_response_not_found", "message": "state expired"}}])
    second = FakeUpstreamWebSocket(_done("second"))
    sockets = [first, second]
    async def connect(url, **kwargs):
        return sockets.pop(0)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    ws = SequentialFakeWebSocket(
        {"type": "response.create", "model": "test-model", "input": "first question"},
        {"type": "response.create", "model": "test-model", "previous_response_id": "first", "input": "second question"},
    )
    await m["responses_ws"].handle_responses_ws(ws)
    assert not sockets
    replay = json.loads(second.sent[0])
    assert "previous_response_id" not in replay
    texts = json.dumps(replay["input"])
    assert "first question" in texts and "second question" in texts
    assert len([x for x in ws.sent_texts if json.loads(x).get("type") == "response.completed"]) == 2
    assert not any(json.loads(x).get("type") == "error" for x in ws.sent_texts)
    assert not m["cooldown"].is_blocked(ch.key, "test-model")


def test_store_rebuild_requires_complete_same_owner_chain(m, monkeypatch):
    _setup(m)
    from src.openai import store
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    rec = SimpleNamespace(channel_key="api:owner", model="test-model", parent_id=None,
                          input_items=[{"type": "message", "role": "user", "content": "old"}], output_items=[])
    def lookup(response_id, *, api_key_name):
        if api_key_name != "owner-key":
            raise store.ResponseForbidden()
        return rec
    monkeypatch.setattr(store, "lookup", lookup)
    body = {"previous_response_id": "old", "model": "test-model", "input": "new", "instructions": "same", "tools": []}
    args = dict(api_key_name="owner-key", channel_key="api:owner", model="test-model")
    assert recovery.rebuild_full_request(body, **args)["input"][0]["content"] == "old"
    assert recovery.rebuild_full_request(body, **{**args, "api_key_name": "other"}) is None
    assert recovery.rebuild_full_request(body, **{**args, "channel_key": "api:other"}) is None
    rec.parent_id = "old"  # a cyclic/truncated history is not a complete request
    assert recovery.rebuild_full_request(body, **args) is None
    assert body["previous_response_id"] == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete", "cancelled"])
async def test_426_http_fallback_keeps_ws_session_and_reconstructs_next_turn(m, monkeypatch, terminal):
    _setup(m)
    _make_oauth_channel_for_failover(m)
    before = {r["request_id"] for r in m["log_db"]._get_conn().execute("SELECT request_id FROM request_log")}
    from websockets.exceptions import InvalidStatus
    from src.transports.http_runtime import OpenedHttpResponse
    requests = []
    contexts, live_locks = [], []
    from src.openai.codex_identity import active_thread_turn_queue_count
    class Stream(httpx.AsyncByteStream):
        def __init__(self, events):
            self.events = events
        async def __aiter__(self):
            for event in self.events:
                yield (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()
    class Context:
        async def __aexit__(self, *args):
            return None
    async def connect(url, **kwargs):
        raise InvalidStatus(SimpleNamespace(status_code=426, headers={}, body=b"upgrade unavailable"))
    async def opened(**kwargs):
        raw = kwargs["upstream_req"].body
        requests.append(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
        contexts.append(kwargs["upstream_req"].translator_ctx["codex_identity_context"])
        live_locks.append(active_thread_turn_queue_count())
        ident = "first" if len(requests) == 1 else "second"
        events = _done(ident)
        if len(requests) == 1 and terminal == "response.incomplete":
            events[-1]["type"] = terminal
            events[-1]["response"].update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        response = httpx.Response(200, stream=Stream(events), request=httpx.Request("POST", "https://example.test/responses"))
        return OpenedHttpResponse(ctx=Context(), response=response, connect_ms=1)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    monkeypatch.setattr(m["responses_ws"], "open_response_with_proxy_chain", opened)
    ws = SequentialFakeWebSocket(
        {"type": "response.create", "model": "test-model", "input": "first question"},
        {"type": "response.create", "model": "test-model", **({"previous_response_id": "first"} if terminal == "response.completed" else {}), "input": "second question"},
    )
    if terminal == "cancelled":
        loop = asyncio.get_running_loop()
        original_record = m["log_db"].record_retry_attempt
        handle = None
        def cancel_during_second_record(request_id, *args, **kwargs):
            value = original_record(request_id, *args, **kwargs)
            if ":ws:2" in request_id:
                loop.call_soon_threadsafe(handle.cancel)
            return value
        monkeypatch.setattr(m["log_db"], "record_retry_attempt", cancel_during_second_record)
        handle = asyncio.create_task(m["responses_ws"].handle_responses_ws(ws))
        with pytest.raises(asyncio.CancelledError):
            await handle
        rows = [r for r in m["log_db"]._get_conn().execute("SELECT request_id, status FROM request_log ORDER BY id")
                if r["request_id"] not in before]
        assert [r["status"] for r in rows] == ["success", "cancelled"]
        assert [r["outcome"] for r in _retry_chain(m, rows[1]["request_id"])] == ["cancelled"]
        assert active_thread_turn_queue_count() == 0
        return
    await m["responses_ws"].handle_responses_ws(ws)
    assert len(requests) == 2
    assert "previous_response_id" not in requests[1]
    if terminal == "response.completed":
        assert "first question" in json.dumps(requests[1]["input"])
    assert "second question" in json.dumps(requests[1]["input"])
    assert contexts[0].logical_session.root_thread_id == contexts[1].logical_session.root_thread_id
    assert live_locks == [1, 1]
    assert active_thread_turn_queue_count() == 0
    assert not ws.close_calls
    assert [json.loads(x)["type"] for x in ws.sent_texts if json.loads(x).get("type") in {"response.completed", "response.incomplete"}] == [terminal, "response.completed"]
    rows = [r for r in m["log_db"]._get_conn().execute("SELECT request_id, status FROM request_log")
            if r["request_id"] not in before]
    assert [r["status"] for r in rows] == ["success" if terminal == "response.completed" else "error", "success"]


@pytest.mark.asyncio
async def test_second_turn_reconnect_failure_never_rewrites_completed_first_turn(m, monkeypatch):
    _setup(m)
    _make_oauth_channel_for_failover(m)
    before = {r["request_id"] for r in m["log_db"]._get_conn().execute("SELECT request_id FROM request_log")}
    first = FakeUpstreamWebSocket(_done("first") + [{"type": "error", "status": 400,
        "error": {"code": "websocket_connection_limit_reached", "message": "connect again"}}])
    calls = []
    async def connect(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return first
        raise OSError("reconnect unavailable")
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    ws = SequentialFakeWebSocket(
        {"type": "response.create", "model": "test-model", "input": "first"},
        {"type": "response.create", "model": "test-model", "previous_response_id": "first", "input": "second"},
    )
    await m["responses_ws"].handle_responses_ws(ws)
    rows = [r for r in m["log_db"]._get_conn().execute("SELECT request_id, status FROM request_log ORDER BY id")
            if r["request_id"] not in before]
    assert len(calls) == 2
    assert [r["status"] for r in rows] == ["success", "error"]
    assert [r["outcome"] for r in _retry_chain(m, rows[0]["request_id"])] == ["success"]
    assert all(r["outcome"] != "open" for r in _retry_chain(m, rows[1]["request_id"]))
    from src.openai.codex_identity import active_thread_turn_queue_count
    assert active_thread_turn_queue_count() == 0


@pytest.mark.asyncio
async def test_fallback_disconnect_cancels_inflight_http_turn(m, monkeypatch):
    _setup(m)
    started, cancelled = asyncio.Event(), asyncio.Event()
    class Disconnecting:
        async def receive(self):
            await started.wait()
            return {"type": "websocket.disconnect", "code": 1000}
    async def pending(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(m["responses_ws"], "_try_sse_channel", pending)
    result = await asyncio.wait_for(m["responses_ws"]._run_codex_sse_turn(Disconnecting()), 1)
    assert cancelled.is_set()
    assert result.outcome == "client_disconnected"


def test_unresolvable_cached_history_does_not_abort_successful_session():
    previous = ("prev", {"input": "first"}, [])
    body = {"previous_response_id": "prev", "input": [{"type": "item_reference", "id": "not-cached"}]}
    assert recovery.rebuild_full_request(body, api_key_name="owner", channel_key="owner", model="m", previous=previous) is None


@pytest.mark.asyncio
async def test_long_retry_after_is_not_slept_or_shortened(m, monkeypatch):
    cfg = _setup(m)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(m["failover"].asyncio, "sleep", sleep)
    result = AttemptResult(outcome="http_error", http_status=429)
    recovery.capture_error_advice(result, headers={"Retry-After": "300"})
    delay = recovery.remaining_retry_delay(result)
    assert await m["failover"]._wait_for_overload_retry(0, time.time() + 600, retry_after_seconds=delay) is None
    assert await m["responses_ws"]._wait_for_transient_retry(0, cfg, time.time() + 600, retry_after_seconds=delay) is None
    assert sleeps == []
    assert result.cooldown_until > (time.time() + 299) * 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(recovery.WS_RESET_CODES))
async def test_http_ingress_recovers_rejected_ws_create_on_same_account(m, monkeypatch, code):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m)
    first = FakeOAuthHttpWs([{"type": "error", "status": 400, "error": {"code": code, "message": "reconnect"}}])
    second = FakeOAuthHttpWs(_done())
    sockets = [first, second]
    async def connect(url, **kwargs):
        return sockets.pop(0)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", connect)
    response, rid = await _call_failover_responses(m, ch, {"model": "test-model", "stream": False, "input": "hello"})
    assert response.status_code == 200
    assert not sockets
    assert m["log_db"].log_detail(rid)["log"]["status"] == "success"
    assert [r["outcome"] for r in _retry_chain(m, rid)] == ["connection_lifecycle", "success"]
    assert not m["cooldown"].is_blocked(ch.key, "test-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["http", "ws"])
@pytest.mark.parametrize("code", ["usage_not_included", "usage_limit_reached"])
async def test_known_quota_403_never_refreshes_authentication(m, monkeypatch, ingress, code):
    cfg = _setup(m)
    cfg.setdefault("openai", {})["responsesUpstreamWsForOAuth"] = True
    ch = _make_oauth_channel_for_failover(m)
    from websockets.exceptions import InvalidStatus
    refreshes = []
    async def refresh(*args, **kwargs):
        refreshes.append(args)
        raise AssertionError("Quota/entitlement is not an authentication failure")
    async def connect(url, **kwargs):
        raise InvalidStatus(SimpleNamespace(status_code=403, headers={},
            body=json.dumps({"error": {"code": code, "message": "not available"}}).encode()))
    monkeypatch.setenv("PARROT_NO_REFRESH", "0")
    monkeypatch.setattr(m["failover"].oauth_manager, "force_refresh", refresh)
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", connect)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    body = {"model": "test-model", "input": "hello"}
    if ingress == "http":
        response, _rid = await _call_failover_responses(m, ch, {**body, "stream": False})
        # The outer all-candidates-exhausted status keeps its existing rule;
        # the structured cause preserves the authoritative upstream status.
        root = json.loads(response.body)["error"]["details"]["root_cause"]
        assert root["status"] == 403
        assert root["code"] == code
    else:
        await m["responses_ws"].handle_responses_ws(FakeWebSocket({"type": "response.create", **body}))
    assert refreshes == []
    from src import oauth_manager
    assert oauth_manager.get_account(ch.account_key).get("disabled_reason") != "auth_error"


@pytest.mark.asyncio
async def test_ws_rate_error_without_status_uses_bounded_same_account_retry(m, monkeypatch):
    _setup(m)
    ch = _make_oauth_channel_for_failover(m)
    sockets = [FakeUpstreamWebSocket([{"type": "error", "headers": {"Retry-After": "0"},
                "error": {"code": "slow_down", "message": "slow down"}}]),
               FakeUpstreamWebSocket(_done())]
    async def connect(url, **kwargs):
        return sockets.pop(0)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    ws = FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello"})
    await m["responses_ws"].handle_responses_ws(ws)
    assert not sockets
    assert any(json.loads(frame).get("type") == "response.completed" for frame in ws.sent_texts)
    assert not m["cooldown"].is_blocked(ch.key, "test-model")
