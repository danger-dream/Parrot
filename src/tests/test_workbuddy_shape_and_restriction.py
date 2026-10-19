"""WorkBuddy shape normalization and bounded account restriction policy."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from src import config, cooldown, oauth_manager, scorer, state_db
from src.channel.workbuddy_oauth_channel import merge_tool_call_messages
from src.providers.workbuddy_errors import ACCOUNT_RESTRICTED_MESSAGE
from src.protocols.runtime import AttemptResult, request_invalid_result_if_needed
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_workbuddy_channel import ev, frames, wire_json
from src.tests.test_workbuddy_request_rejection import pool, health_state, latest, call, PATHS, MODEL


def assistant(cid, **extra):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}], **extra}


def tool(cid):
    return {"role": "tool", "tool_call_id": cid, "content": "result"}


@pytest.mark.parametrize("content", [None, "", []])
def test_tool_only_batches_merge_without_mutating_source(content):
    messages = [assistant("a", content=content), assistant("b"), assistant("c"), tool("c"), tool("a"), tool("b")]
    original = copy.deepcopy(messages)
    body = {"messages": messages}
    merge_tool_call_messages(body)
    assert len(body["messages"]) == 4
    assert [c["id"] for c in body["messages"][0]["tool_calls"]] == ["a", "b", "c"]
    assert body["messages"][1:] == original[3:]
    assert messages == original
    merged = copy.deepcopy(body)
    merge_tool_call_messages(body)
    assert body == merged


@pytest.mark.parametrize("extra", [{"content": "text"}, {"content": " "}, {"reasoning": "thinking"},
    {"reasoning_content": "thinking"}, {"extra_fields": {"signature": "signed"}}, {"name": "agent"},
    {"refusal": "no"}, {"unknown": None}])
def test_message_metadata_is_not_lost_or_moved(extra):
    body = {"messages": [assistant("a"), assistant("b", **extra), assistant("c"), tool("a"), tool("b"), tool("c")]}
    original = copy.deepcopy(body)
    merge_tool_call_messages(body)
    assert body == original


def test_interleaved_or_orphan_tools_are_preserved():
    body = {"messages": [tool("orphan"), assistant("a"), tool("a"), assistant("b"), tool("b")]}
    original = copy.deepcopy(body)
    merge_tool_call_messages(body)
    assert body == original


@pytest.mark.parametrize("ingress", PATHS)
async def test_final_outbound_shape_is_normalized_for_every_ingress(pool, ingress):
    body = {"model": MODEL, "stream": False}
    if ingress == "chat":
        body["messages"] = [{"role": "user", "content": "lookup"}, assistant("a"), assistant("b"), tool("b"), tool("a")]
    elif ingress == "responses":
        body["input"] = [{"role": "user", "content": "lookup"},
            *[{"type": "function_call", "call_id": cid, "name": "lookup", "arguments": "{}"} for cid in ("a", "b")],
            *[{"type": "function_call_output", "call_id": cid, "output": "result"} for cid in ("b", "a")]]
    else:
        body.update(max_tokens=32, messages=[{"role": "user", "content": "lookup"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": cid, "name": "lookup", "input": {}} for cid in ("a", "b")]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "result"} for cid in ("b", "a")]}])
    def respond(req):
        messages = wire_json(req.content)["messages"]
        calls = [m for m in messages if m.get("tool_calls")]
        assert len(calls) == 1 and [c["id"] for c in calls[0]["tool_calls"]] == ["a", "b"]
        assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["b", "a"]
        return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    response = await pool.client.post(PATHS[ingress], headers={"Authorization": "Bearer ccp-test"}, json=body)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("message", ["conversation id missing", "first message is not system prompt"])
@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("transport", ["http", "sse_first", "sse_after_content"])
async def test_channel_rejection_variants_never_penalize_pool(pool, message, ingress, transport):
    before = health_state(pool)
    payload = {"error": {"data": {"code": 11128, "msg": message}}}
    def respond(req):
        if transport == "http":
            return httpx.Response(400, json=payload)
        chunks = [ev({"choices": [{"index": 0, "delta": {"content": "partial"}}]})] if transport == "sse_after_content" else []
        return httpx.Response(200, stream=fake.ChunkedByteStream(chunks + [ev(payload)]), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    response = await call(pool, ingress, stream=True)
    assert response.status_code == (200 if transport == "sse_after_content" else 400), response.text
    assert message in response.text and "11128" in response.text
    assert len(pool.requests) == 1 and health_state(pool) == before
    assert latest(pool)[1][0]["outcome"] == "request_invalid"


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("transport", ["http", "sse_first", "sse_after_content"])
async def test_restricted_account_pauses_all_models_without_refresh_or_ladder(pool, ingress, stream, transport):
    accounts = copy.deepcopy(config.get()["oauthAccounts"])
    before_scores = {ch.key: scorer.get_stats(ch.key, MODEL) for ch in pool.channels}
    restricted_auth = None
    def respond(req):
        nonlocal restricted_auth
        if restricted_auth is None:
            restricted_auth = req.headers["authorization"]
        if req.headers["authorization"] != restricted_auth:
            return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"})
        payload = {"error": {"data": {"code": 11140, "msg": "request illegal", "debug": "do-not-expose"}}}
        if transport == "http":
            return httpx.Response(403, json=payload)
        chunks = [ev({"choices": [{"index": 0, "delta": {"content": "partial"}}]})] if transport == "sse_after_content" else []
        return httpx.Response(200, stream=fake.ChunkedByteStream(chunks + [ev(payload)]), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    response = await call(pool, ingress, stream=stream)
    assert response.status_code == 200, response.text
    row, chain = latest(pool)
    assert any(item["outcome"] == "account_restricted" for item in chain), chain
    key = next(ch.key for ch in pool.channels if "Bearer " + ch.account["access_token"] == restricted_auth)
    state = cooldown.get_state(key, cooldown.ACCOUNT_SCOPE)
    assert state["error_count"] == 0
    assert 7190 < (state["cooldown_until"] / 1000 - time.time()) <= 7200
    assert state["last_error_message"] == ACCOUNT_RESTRICTED_MESSAGE
    assert cooldown.is_blocked(key, MODEL) and cooldown.is_blocked(key, "new-model-not-in-catalog")
    assert scorer.get_stats(key, MODEL) == before_scores[key]
    assert config.get()["oauthAccounts"] == accounts
    if stream and transport == "sse_after_content":
        assert len(pool.requests) == 1 and "11140" in response.text and "do-not-expose" not in response.text
        assert row["http_status"] == 403
    else:
        assert len(pool.requests) == 2
    pool.requests.clear()
    response = await call(pool, ingress)
    assert response.status_code == 200 and len(pool.requests) == 1
    assert pool.requests[0].headers["authorization"] != restricted_auth
    assert not cooldown.clear_on_success(key, MODEL)
    assert cooldown.is_blocked(key, MODEL)
    assert any(e["channel_key"] == key and e["model"] == cooldown.ACCOUNT_SCOPE for e in state_db.error_load_all())
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.is_blocked(key, "new-model-not-in-catalog")
    oauth_manager.reset_quota(key.removeprefix("oauth:"))
    assert not cooldown.is_blocked(key, MODEL)


@pytest.mark.parametrize("provider,status,payload", [
    ("other", 403, {"code": 11140}), ("workbuddy", 401, {"code": 11140}),
    ("workbuddy", 400, {"code": 11140}), ("workbuddy", 429, {"code": 11140}),
    ("workbuddy", 403, {"msg": "request illegal"}), ("workbuddy", 403, {"code": 11128}),
    ("workbuddy", 403, {"code": 14018}), ("workbuddy", 403, {"code": 11140.0}),
])
def test_restriction_requires_vendor_and_authoritative_signal(provider, status, payload):
    result = AttemptResult(outcome="http_error", http_status=status, full_response_text=json.dumps(payload))
    request_invalid_result_if_needed(result, channel=SimpleNamespace(provider=provider))
    assert result.outcome == "http_error"


async def test_account_pause_expiry_does_not_clear_other_model_faults(pool, monkeypatch):
    key = pool.channels[0].key
    now = int(time.time() * 1000)
    cooldown.record_error(key, MODEL, "other fault", cooldown_until=now + 100000)
    cooldown.record_account_pause(key, ACCOUNT_RESTRICTED_MESSAGE, cooldown_until=now + 10000)
    cooldown.record_account_pause(key, ACCOUNT_RESTRICTED_MESSAGE, cooldown_until=now + 50000)
    assert cooldown.get_state(key, MODEL)["cooldown_until"] == now + 10000
    monkeypatch.setattr(cooldown, "_now_ms", lambda: now + 10001)
    assert cooldown.is_blocked(key, MODEL)
    assert not cooldown.is_blocked(key, "new-model")
    assert cooldown.get_state(key, MODEL)["last_error_message"] == "other fault"


@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_all_restricted_accounts_return_permission_error_and_remain_paused(pool, transport):
    payload = {"code": 11140, "msg": "request illegal"}
    pool.response_factory = lambda req: (httpx.Response(403, json=payload) if transport == "http" else
        httpx.Response(200, stream=fake.ChunkedByteStream([ev(payload)]), headers={"content-type": "text/event-stream"}))
    response = await call(pool)
    assert response.status_code == 403, response.text
    assert "供应商限制" in response.text or "11140" in response.text
    assert len(pool.requests) == len(pool.channels)
    assert all(cooldown.is_blocked(ch.key, MODEL) for ch in pool.channels)
