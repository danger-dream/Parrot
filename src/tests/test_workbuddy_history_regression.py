"""WorkBuddy replay survives routing; malformed history never poisons the pool."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from src import config, upstream
from src.providers import workbuddy_replay
from src.providers.workbuddy_errors import TOOL_HISTORY_MESSAGE, request_rejection_info
from src.protocols.runtime import AttemptResult, request_invalid_result_if_needed, retry_body_without_encrypted_content
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_workbuddy_channel import ev, frames, wire_json
from src.tests.test_workbuddy_request_rejection import pool, health_state, latest, call, PATHS, MODEL


REJECTION = {"code": 11148, "msg": TOOL_HISTORY_MESSAGE,
             "extError": {"type": "invalid_request_error", "code": "invalid_request_error",
                          "message": TOOL_HISTORY_MESSAGE}, "requestId": "fixture"}


def history():
    items = [{"role": "user", "content": "Find both results"}]
    for turn in range(3):
        ids = [f"call-{turn}-{index}" for index in range(2)]
        items.extend({"type": "function_call", "call_id": cid, "name": "lookup", "arguments": "{}"} for cid in ids)
        items.append(workbuddy_replay.response_item({"reasoning": f"thought-{turn}",
                                                    "extra_fields": {"fixture": turn}}))
        items.extend({"type": "function_call_output", "call_id": cid, "output": "result " + cid} for cid in ids)
    items.append({"role": "user", "content": "Reply OK"})
    return items


def assert_batches(messages):
    pending = set()
    calls = results = 0
    for message in messages:
        if message.get("role") == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
            results += 1
        else:
            assert not pending, "assistant inserted before the tool batch finished"
            for tc in message.get("tool_calls", []):
                pending.add(tc["id"])
                calls += 1
    assert not pending and calls == results == 6


@pytest.mark.parametrize("preserve", [False, True])
def test_portable_cleanup_preserves_tagged_turn_but_removes_native_ciphertext(preserve):
    body = {"model": MODEL, "input": history()}
    original = copy.deepcopy(body)
    body["input"].insert(0, {"type": "reasoning", "encrypted_content": "foreign-ciphertext", "summary": []})
    clean, count = retry_body_without_encrypted_content(body, preserve_anthropic=preserve)
    assert count == 1 and "encrypted_content" not in clean["input"][0]
    assert clean["input"][1:] == original["input"]
    assert "encrypted_content" in body["input"][0]
    assert_batches(workbuddy_replay.responses_messages(clean["input"]))
    foreign = workbuddy_replay.strip_foreign(clean, "openai-responses")
    assert not any(workbuddy_replay.is_carrier(item.get("encrypted_content")) for item in foreign["input"])
    assert len([i for i in foreign["input"] if i.get("type") == "function_call"]) == 6
    assert len([i for i in foreign["input"] if i.get("type") == "function_call_output"]) == 6


@pytest.mark.parametrize("mixed_cipher", [False, True])
@pytest.mark.parametrize("fail_first", [False, True])
async def test_full_ingress_scheduler_and_failover_preserve_tool_batch(pool, mixed_cipher, fail_first):
    body = {"model": MODEL, "input": history(), "stream": False,
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]}
    if mixed_cipher:
        body["input"].insert(0, {"type": "reasoning", "encrypted_content": "foreign-ciphertext", "summary": []})
    def respond(req):
        messages = wire_json(req.content)["messages"]
        assert_batches(messages)
        assistants = [m for m in messages if m.get("tool_calls")]
        assert [m["reasoning"] for m in assistants] == [f"thought-{n}" for n in range(3)]
        if fail_first and len(pool.requests) == 1:
            return httpx.Response(500, json={"error": "fixture outage"})
        return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    response = await pool.client.post(PATHS["responses"], headers={"Authorization": "Bearer ccp-test"}, json=body)
    assert response.status_code == 200, response.text
    assert len(pool.requests) == (2 if fail_first else 1)
    assert latest(pool)[0]["status"] == "success"


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("transport", ["http", "sse_first", "sse_after_content"])
async def test_history_rejection_stops_retry_and_preserves_health(pool, ingress, transport):
    before = health_state(pool)
    accounts = copy.deepcopy(config.get()["oauthAccounts"])
    def respond(req):
        if transport == "http":
            return httpx.Response(400, json=REJECTION)
        chunks = []
        if transport == "sse_after_content":
            chunks.append(ev({"id": "fixture", "choices": [{"index": 0, "delta": {"content": "partial"}}]}))
        chunks.append(ev(REJECTION))
        return httpx.Response(200, stream=fake.ChunkedByteStream(chunks), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    for _ in range(4):
        response = await call(pool, ingress, stream=True)
        assert response.status_code == (200 if transport == "sse_after_content" else 400), response.text
        assert "11148" in response.text and TOOL_HISTORY_MESSAGE in response.text
        assert "response.completed" not in response.text and "[DONE]" not in response.text
        row, chain = latest(pool)
        assert row["http_status"] == 400 and len(chain) == 1
        assert chain[0]["outcome"] == "request_invalid"
        assert health_state(pool) == before
    assert len(pool.requests) == 4 and config.get()["oauthAccounts"] == accounts


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
def test_history_code_does_not_override_authoritative_status(status):
    result = AttemptResult(outcome="http_error", http_status=status,
                           full_response_text=json.dumps(REJECTION), error_detail=json.dumps(REJECTION))
    assert request_invalid_result_if_needed(result, channel=SimpleNamespace(provider="workbuddy")) is result
    assert result.outcome == "http_error"


@pytest.mark.parametrize("payload", [{"code": 11148}, {"code": 11148, "msg": "unknown"},
                                    {"code": 11149, "msg": TOOL_HISTORY_MESSAGE}])
def test_unknown_rejections_are_not_guessed(payload):
    assert request_rejection_info(payload) is None


def test_ext_error_and_nested_envelope_are_supported_without_leaking_extensions():
    for payload in ({"code": 11148, "extError": REJECTION["extError"]},
                    {"error": {"data": REJECTION}}):
        assert request_rejection_info(payload) == ("11148", TOOL_HISTORY_MESSAGE)
    result = AttemptResult(outcome="http_error", http_status=400,
                           full_response_text=json.dumps(REJECTION), error_detail=json.dumps(REJECTION))
    request_invalid_result_if_needed(result, channel=SimpleNamespace(provider="other"))
    assert result.outcome == "http_error"
