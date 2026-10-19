"""WorkBuddy late admission and Responses WS/SSE terminal ownership regressions.

Real scheduler/failover/WS handlers, fixture credentials and MockTransport only.
Run via src/tests/isolated_pytest.py; no lifespan or external network.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from src import channel_state, concurrency, config, cooldown, failover, oauth_manager, scorer, scheduler
from src.openai import responses_ws
from src.protocols.runtime import AttemptResult
from src.providers.workbuddy_errors import (
    ACCOUNT_RESTRICTED_MESSAGE, CHANNEL_REJECTION_MESSAGES, TOOL_HISTORY_MESSAGE,
)
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_openai_responses_ws import FakeWebSocket
from src.tests.test_workbuddy_channel import ev, frames
from src.tests.test_workbuddy_request_rejection import pool, call, latest, MODEL


RESTRICTION = {"error": {"data": {"code": 11140, "msg": "request illegal"}}}
ERROR_CASES = [(11128, message) for message in sorted(CHANNEL_REJECTION_MESSAGES)] + [
    (11148, TOOL_HISTORY_MESSAGE), (11140, "request illegal"),
]


def install(pool, count=1):
    channels = pool.channels[:count]
    for ch in channels:
        ch.state_key = oauth_manager.account_state_key(ch.account)
    fake._install_channels(pool.m, channels)
    return channels


def restrict(ch):
    return failover._request_invalid_result_if_needed(AttemptResult(
        outcome="http_auth_error", http_status=403,
        full_response_text=json.dumps(RESTRICTION)), ch)


def success():
    return httpx.Response(200, stream=fake.ChunkedByteStream(frames()),
                          headers={"content-type": "text/event-stream"})


def assert_released(channels):
    for ch in channels:
        slot = concurrency._slots.get(channel_state.effect_key(ch))
        assert slot is None or slot.in_flight == 0


@pytest.mark.parametrize("phase", ["slot", "build", "dispatch"])
@pytest.mark.parametrize("healthy", [False, True])
async def test_late_pause_skips_candidate_without_penalty_or_refresh(pool, monkeypatch, phase, healthy):
    channels = install(pool, 2 if healthy else 1)
    ch = channels[0]
    before = scorer.get_stats(ch.key, MODEL)
    monkeypatch.setattr(scheduler, "schedule", lambda *a, **k: scheduler.ScheduleResult(
        [(item, MODEL) for item in channels], None, False))
    built = []
    original_build = ch.build_upstream_request
    async def build(*a, **kw):
        built.append(1)
        request = await original_build(*a, **kw)
        if phase == "build":
            # Simulate the other request committing while async token/build work
            # is suspended. The completed wire request must not be sent.
            await asyncio.sleep(0)
            restrict(ch)
        return request
    monkeypatch.setattr(ch, "build_upstream_request", build)
    if phase == "slot":
        acquire = concurrency.try_acquire
        async def acquire_then_pause(key):
            result = await acquire(key)
            if key == ch.state_key and result:
                restrict(ch)
            return result
        monkeypatch.setattr(concurrency, "try_acquire", acquire_then_pause)
    if phase == "dispatch":
        opening = failover.open_response_with_proxy_chain
        async def pause_after_transport_handoff(**kw):
            if kw["channel"] is ch:
                await asyncio.sleep(0)
                restrict(ch)
            return await opening(**kw)
        monkeypatch.setattr(failover, "open_response_with_proxy_chain", pause_after_transport_handoff)
    pool.response_factory = lambda req: success()
    response = await call(pool)
    assert response.status_code == (200 if healthy else 403), response.text
    assert len(pool.requests) == int(healthy)
    assert all(req.headers["authorization"] != "Bearer " + ch.account["access_token"] for req in pool.requests)
    if not healthy:
        assert response.json()["error"]["code"] == "11140"
    assert len(built) == (0 if phase == "slot" else 1)
    assert scorer.get_stats(ch.key, MODEL) == before
    assert cooldown.get_state(ch.key, MODEL)["error_count"] == 0
    assert_released(channels)


async def test_real_403_request_does_not_dispatch_queued_request(pool, monkeypatch):
    ch, = install(pool)
    config.update(lambda c: c.update(concurrency={"enabled": True, "queueWaitSeconds": 3}))
    ch.max_concurrent = 1
    fake._install_channels(pool.m, [ch])
    entered, permit, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    before = scorer.get_stats(ch.key, MODEL)
    class Delayed403(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await permit.wait()
            yield json.dumps(RESTRICTION).encode()
    pool.response_factory = lambda req: httpx.Response(403, stream=Delayed403(),
        headers={"content-type": "application/json"})
    acquire = concurrency.acquire_from_candidates
    async def observed(*a, **kw):
        queued.set()
        return await acquire(*a, **kw)
    monkeypatch.setattr(concurrency, "acquire_from_candidates", observed)
    first = asyncio.create_task(call(pool))
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(call(pool))
    await asyncio.wait_for(queued.wait(), 3)
    permit.set()
    a, b = await asyncio.wait_for(asyncio.gather(first, second), 4)
    assert (a.status_code, b.status_code) == (403, 403)
    assert len(pool.requests) == 1
    assert scorer.get_stats(ch.key, MODEL) == before
    assert cooldown.is_blocked(ch.key, MODEL)
    assert_released([ch])


async def test_paused_queue_winner_preserves_other_waiting_candidates(pool, monkeypatch):
    ch, healthy = install(pool, 2)
    config.update(lambda c: c.update(concurrency={"enabled": True, "queueWaitSeconds": 3}))
    ch.max_concurrent = healthy.max_concurrent = 1
    fake._install_channels(pool.m, [ch, healthy])
    assert await concurrency.try_acquire(ch.state_key)
    assert await concurrency.try_acquire(healthy.state_key)
    waiting = asyncio.Event()
    acquire = concurrency.acquire_from_candidates
    async def observed(*a, **kw):
        waiting.set()
        return await acquire(*a, **kw)
    monkeypatch.setattr(concurrency, "acquire_from_candidates", observed)
    pool.response_factory = lambda req: success()
    task = asyncio.create_task(call(pool))
    await asyncio.wait_for(waiting.wait(), 3)
    restrict(ch)
    concurrency.release(ch.state_key)
    concurrency.release(healthy.state_key)
    response = await asyncio.wait_for(task, 4)
    assert response.status_code == 200, response.text
    assert [req.headers["authorization"] for req in pool.requests] == ["Bearer " + healthy.account["access_token"]]
    assert_released([ch, healthy])


class TerminalAwareWebSocket(FakeWebSocket):
    async def send_text(self, text):
        await super().send_text(text)
        if json.loads(text).get("type") == "error":
            self._terminal_seen = True
            self._closed.set()


def websocket():
    return TerminalAwareWebSocket({"type": "response.create", "model": MODEL, "input": "hello",
        "tools": [{"type": "function", "name": "web_search", "parameters": {"type": "object"}}]},
        extra_headers={"Authorization": "Bearer ccp-test"})


def terminal_error(ws):
    terminals = [obj for text in ws.sent_texts if (obj := json.loads(text)).get("type") in
                 {"error", "response.failed", "response.incomplete", "response.completed"}]
    assert len(terminals) == 1, terminals
    item = terminals[0]
    return item.get("error") or (item.get("response") or {}).get("error") or item


@pytest.mark.parametrize("transport", ["http", "sse_first", "sse_after", "sse_same_batch"])
@pytest.mark.parametrize("code,message", ERROR_CASES)
@pytest.mark.parametrize("mode", ["managed", "passthrough"])
async def test_ws_bridge_first_terminal_keeps_vendor_classification(pool, monkeypatch, transport, code, message, mode):
    ch, = install(pool)
    config.update(lambda c: c.setdefault("search", {}).update(functionMode=mode))
    fake._install_channels(pool.m, [ch])
    status = 403 if code == 11140 else 400
    payload = {"error": {"data": {"code": code, "msg": message, "debug": "must-not-leak"}}}
    pauses = []
    record = cooldown.record_account_pause
    def recording(*a, **kw):
        pauses.append(1)
        return record(*a, **kw)
    monkeypatch.setattr(cooldown, "record_account_pause", recording)
    def response(req):
        if transport == "http":
            return httpx.Response(status, json=payload)
        chunks = [ev({"choices": [{"index": 0, "delta": {"content": "partial"}}]})] if transport in {"sse_after", "sse_same_batch"} else []
        chunks.append(ev(payload))
        if transport == "sse_same_batch":
            chunks = [b"".join(chunks)]
        return httpx.Response(200, stream=fake.ChunkedByteStream(chunks), headers={"content-type": "text/event-stream"})
    pool.response_factory = response
    before = scorer.get_stats(ch.key, MODEL)
    ws = websocket()
    await asyncio.wait_for(responses_ws.handle_responses_ws(ws), 5)
    row, chain = latest(pool)
    assert len(pool.requests) == 1
    assert len(pauses) == (1 if code == 11140 else 0)
    assert row["http_status"] == status, row
    assert [r["outcome"] for r in chain] == ["account_restricted" if code == 11140 else "request_invalid"]
    error = terminal_error(ws)
    assert error["code"] == str(code), error
    assert error.get("error_type", error.get("type")) == ("permission_error" if code == 11140 else "invalid_request_error")
    assert "must-not-leak" not in "".join(ws.sent_texts)
    if transport in {"sse_after", "sse_same_batch"}:
        assert "partial" in "".join(ws.sent_texts)
    assert scorer.get_stats(ch.key, MODEL) == before
    assert cooldown.is_blocked(ch.key, "outside-catalog") is (code == 11140)
    assert_released([ch])


@pytest.mark.parametrize("mode", ["managed", "passthrough"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("code,message", [ERROR_CASES[0], (11148, TOOL_HISTORY_MESSAGE), (11140, "request illegal")])
async def test_ws_disconnect_respects_observed_vendor_terminal(pool, monkeypatch, mode, observed, code, message):
    ch, = install(pool)
    config.update(lambda c: c.setdefault("search", {}).update(functionMode=mode))
    fake._install_channels(pool.m, [ch])
    output, blocked, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never = asyncio.Event()
    class Source(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ev({"choices": [{"index": 0, "delta": {"content": "partial"}}]})
            await output.wait()
            if not observed:
                blocked.set()
                await never.wait()
            yield ev({"error": {"data": {"code": code, "msg": message}}})
            await never.wait()
        async def aclose(self):
            closed.set()
    pool.response_factory = lambda req: httpx.Response(200, stream=Source(), headers={"content-type": "text/event-stream"})
    ws = websocket()
    send = ws.send_text
    async def controlled_send(text):
        kind = json.loads(text).get("type")
        if observed and kind == "response.output_text.done":
            # Upstream finalization precedes item closures, but managed search
            # has not consumed the terminal frame yet. Cancel in that exact gap.
            blocked.set()
            await never.wait()
        await send(text)
        if kind == "response.output_text.delta":
            output.set()
    ws.send_text = controlled_send
    before = scorer.get_stats(ch.key, MODEL)
    task = asyncio.create_task(responses_ws.handle_responses_ws(ws))
    await asyncio.wait_for(blocked.wait(), 4)
    ws._closed.set()  # real handler receives a client disconnect
    await asyncio.wait_for(task, 4)
    row, chain = latest(pool)
    assert row["http_status"] == ((403 if code == 11140 else 400) if observed else 499), row
    assert cooldown.is_blocked(ch.key, MODEL) is (observed and code == 11140)
    assert scorer.get_stats(ch.key, MODEL) == before
    assert closed.is_set()
    assert_released([ch])


async def test_other_provider_numeric_error_mapping_is_unchanged(pool):
    # A forged WorkBuddy-looking error from an ordinary Chat provider must not
    # acquire trusted stream metadata or the WorkBuddy terminal rewrite.
    ch = fake._make_openai_channel("other-review", "https://other.example", protocol="openai-chat", alias=MODEL, real=MODEL)
    fake._install_channels(pool.m, [ch])
    from src import upstream
    await upstream.close_client()
    upstream.set_client(httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,
        stream=fake.ChunkedByteStream([ev({"choices": [{"index": 0, "delta": {"content": "partial"}}]}),
                                    ev({"error": {"type": "permission_error", "code": "11140", "message": "other vendor"}})]),
        headers={"content-type": "text/event-stream"}))))
    ws = websocket()
    await asyncio.wait_for(responses_ws.handle_responses_ws(ws), 5)
    assert terminal_error(ws)["code"] == "server_error"
    assert latest(pool)[0]["http_status"] == 502
    assert cooldown.get_state(ch.key, cooldown.ACCOUNT_SCOPE) is None
