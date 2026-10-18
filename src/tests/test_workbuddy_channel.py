"""Six HTTP inference paths against fixture WorkBuddy SSE, never live upstreams."""
from __future__ import annotations

import asyncio
import copy
import json
import gzip
import time
import uuid

import httpx
import pytest

from src import config, notifier, oauth_manager as om
from src.channel import registry
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel, normalize_tool_choice
from src.oauth.workbuddy import auth, common
from src.openai.transform.guard import GuardError
from src.providers import registry as providers
from src.providers.workbuddy_codec import WorkBuddyStream
from src.tests import test_protocol_fake_upstreams as fake


@pytest.fixture
def env(monkeypatch):
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    entry = auth.normalize_credential({"realm": "cn", "uid": "wb-" + uuid.uuid4().hex,
        "access_token": "fixture-access", "refresh_token": "fixture-refresh"})
    entry.update(models=["glm-fixture", "deepseek-fixture"], account_model_catalog={"schema": 1, "models": [
        {"id": "glm-fixture", "reasoningEfforts": ["low", "high"], "maxInputTokens": 100000},
        {"id": "deepseek-fixture"}]})
    config.update(lambda c: c.update(oauthAccounts=[entry], channels=[],
        network={"routing": {"default": "direct"}},
        timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 5},
        protocolBridge={"enabled": True},
        concurrency={"queueWaitSeconds": 1}))
    fake._install_keys(m, fake._default_key())
    ch = WorkBuddyOAuthChannel(entry)
    fake._install_channels(m, [ch])
    yield m, ch
    config.update(lambda c: (c.clear(), c.update(before)))
    fake._install_channels(m, [])


def wire_json(raw):
    """Decode captured WorkBuddy JSON, including modern default gzip bodies."""
    return json.loads(gzip.decompress(raw) if isinstance(raw, bytes) and raw.startswith(b"\x1f\x8b") else raw)


def ev(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"


def frames(*, tool=False, terminal=True, usage=True):
    def item(delta, finish=None):
        return {"id": "chatcmpl-workbuddy-fixture", "created": 1700000000,
            "model": "glm-fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    values = [ev(item({"role": "assistant", "reasoning_content": "fixture thought"}))]
    if tool:
        values += [ev(item({"tool_calls": [{"index": 0, "id": "call-fixture", "type": "function",
            "function": {"name": "fixture_tool", "arguments": '{"city":'}}]})),
            ev(item({"tool_calls": [{"index": 0, "function": {"arguments": '"昆明"}'}}]}))]
    else:
        values += [ev(item({"content": "你好，"})), ev(item({"content": "WorkBuddy"}))]
    if terminal:
        values.append(ev(item({}, "tool_calls" if tool else "stop")))
    if usage:
        values.append(ev({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4,
            "total_tokens": 16, "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2}}}))
    if terminal:
        values.append(b"data: [DONE]\r\n\r\n")
    raw = b"".join(values)
    # Cuts include CRLF, UTF-8 and JSON boundaries, not just whole events.
    return [raw[i:i+13] for i in range(0, len(raw), 13)]


def request_body(ingress, stream, *, tool=False):
    body = {"model": "glm-fixture", "stream": stream}
    if ingress == "responses":
        body["input"] = "ping"
        if tool:
            body.update(tools=[{"type": "function", "name": "fixture_tool", "parameters": {"type": "object"}}],
                tool_choice={"type": "function", "name": "fixture_tool"})
    else:
        body["messages"] = [{"role": "user", "content": "ping"}]
        if ingress == "anthropic":
            body["max_tokens"] = 64
            if tool:
                body.update(tools=[{"name": "fixture_tool", "input_schema": {"type": "object"}}], tool_choice={"type": "tool", "name": "fixture_tool"})
        elif tool:
            body.update(tools=[{"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object"}}}],
                tool_choice={"type": "function", "function": {"name": "fixture_tool"}})
    return body


async def call(env, ingress, body, response_factory):
    m, ch = env
    router = fake.MockRouter()
    def wire(req):
        assert str(req.url) == "https://copilot.tencent.com/v2/chat/completions"
        assert req.headers["authorization"] == "Bearer fixture-access"
        assert "x-refresh-token" not in req.headers
        payload = json.loads(gzip.decompress(req.content) if req.headers.get("content-encoding") == "gzip" else req.content)
        assert payload["stream"] is True and payload["stream_options"]["include_usage"] is True
        assert "reasoning_effort" not in payload
        assert not any(k.startswith("_parrot") for k in payload)
        return response_factory(req)
    router.register("https://copilot.tencent.com", wire)
    if ingress == "anthropic":
        response, client, route = await fake._call_anthropic_core(m, router, body)
        assert route.candidates[0][0].provider == "workbuddy"
    else:
        response, client = await fake._call_openai_handler(m, router, ingress, body)
    try:
        text = await fake._consume_streaming_to_string(response) if hasattr(response, "body_iterator") else response.body.decode()
    finally:
        await client.aclose()
    return response, text, router.requests


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool", [False, True])
async def test_six_protocol_paths_text_tools_usage_and_mime(env, ingress, stream, tool):
    body = request_body(ingress, stream, tool=tool)
    def wire(req):
        if tool:
            assert wire_json(req.content)["tool_choice"] == {"type": "function", "function": {"name": "fixture_tool"}}
        return httpx.Response(200, stream=fake.ChunkedByteStream(frames(tool=tool)), headers={"content-type": "text/event-stream"})
    response, text, requests = await call(env, ingress, body, wire)
    assert len(requests) == 1 and response.status_code == 200, text
    assert response.headers["content-type"].startswith("text/event-stream" if stream else "application/json")
    if stream:
        assert ("fixture_tool" in text and "call-fixture" in text) if tool else ("你好，" in text and "WorkBuddy" in text)
        assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[ingress] in text
    else:
        obj = json.loads(text)
        assert "fixture_tool" in text if tool else "你好，WorkBuddy" in text
        if ingress == "chat":
            assert obj["id"] == "chatcmpl-workbuddy-fixture" and obj["created"] == 1700000000
            assert obj["usage"]["prompt_tokens"] == 12 and obj["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
            if tool:
                assert obj["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"city":"昆明"}'
        elif ingress == "responses":
            assert obj["usage"]["input_tokens"] == 12 and obj["usage"]["input_tokens_details"]["cached_tokens"] == 3
        else:
            assert obj["usage"]["input_tokens"] + obj["usage"].get("cache_read_input_tokens", 0) == 12
    row = env[0]["log_db"]._get_conn().execute(
        "SELECT status,final_channel_key,input_tokens,output_tokens,cache_read_tokens,upstream_protocol FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "success" and row["final_channel_key"] == env[1].key
    assert row["input_tokens"] + row["cache_read_tokens"] == 12 and row["output_tokens"] == 4
    assert row["upstream_protocol"] == "openai-chat"


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
async def test_tool_result_roundtrip_preserves_call_id_and_content(env, ingress):
    m, ch = env
    body = request_body(ingress, False, tool=True)
    if ingress == "chat":
        body["messages"] += [{"role": "assistant", "content": None, "tool_calls": [{"id": "call-fixture", "type": "function", "function": {"name": "fixture_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-fixture", "content": "工具原始结果"}]
    elif ingress == "responses":
        body["input"] = [{"type": "function_call", "call_id": "call-fixture", "name": "fixture_tool", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-fixture", "output": "工具原始结果"}]
    else:
        body["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "call-fixture", "name": "fixture_tool", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-fixture", "content": "工具原始结果"}]}]
    request = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)
    payload = wire_json(request.body)
    result = next(x for x in payload["messages"] if x["role"] == "tool")
    assert result["tool_call_id"] == "call-fixture" and result["content"] == "工具原始结果"


@pytest.mark.parametrize("broken", ["empty", "done_only", "json_error", "vendor_error", "malformed", "truncated", "late_error"])
async def test_bad_stream_never_becomes_non_stream_success(env, broken):
    errors = {"empty": [], "done_only": [b"data: [DONE]\n\n"],
        "json_error": [ev({"error": {"code": "invalid_token", "message": "fixture-secret"}})],
        "vendor_error": [ev({"code": 12153, "msg": "fixture-secret"})],
        "malformed": [b"data: {bad}\n\n"], "truncated": frames(terminal=False),
        "late_error": frames(terminal=False) + [ev({"error": {"message": "fixture-secret"}})]}
    response, text, requests = await call(env, "chat", request_body("chat", False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(errors[broken]), headers={"content-type": "text/event-stream"}))
    assert response.status_code != 200, text
    assert "fixture-secret" not in text


async def test_done_ends_aggregation_without_waiting_for_connection_eof(env):
    stream = fake.TerminalThenHangByteStream(frames())
    response, text, _ = await asyncio.wait_for(call(env, "chat", request_body("chat", False), lambda req:
        httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})), timeout=1)
    assert response.status_code == 200 and stream.closed.is_set()


async def test_stream_error_after_first_content_does_not_fake_completion(env):
    response, text, _ = await call(env, "chat", request_body("chat", True), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames(terminal=False) + [ev({"error": {"code": "rate_limit_exceeded"}})]), headers={"content-type": "text/event-stream"}))
    assert '"error"' in text and "你好" in text
    row = env[0]["log_db"]._get_conn().execute("SELECT status FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] != "success"


async def test_no_default_effort_no_model_name_heuristics_and_disabled_models(env):
    m, ch = env
    assert providers.adapter_for_channel(ch).name == "workbuddy-oauth"
    for model in ["glm-fixture", "deepseek-fixture"]:
        for ingress in ["chat", "responses", "anthropic"]:
            body = request_body(ingress, False)
            request = await ch.build_upstream_request(body, model, ingress_protocol=ingress)
            payload = wire_json(request.body)
            assert "reasoning_effort" not in payload and "thinking" not in payload
    body = request_body("chat", False)
    body["reasoning_effort"] = "low"
    assert wire_json((await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)["reasoning_effort"] == "low"
    body["reasoning_effort"] = "max"
    with pytest.raises(GuardError):
        await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    config.update(lambda c: c["oauthAccounts"][0].update(disabledModels=["glm-fixture"]))
    fresh = WorkBuddyOAuthChannel(om.get_account(ch.account_key))
    assert fresh.supports_model("glm-fixture") is None
    with pytest.raises(GuardError):
        await ch.build_upstream_request(request_body("chat", False), "glm-fixture", ingress_protocol="chat")
    assert WorkBuddyOAuthChannel(dict(om.get_account(ch.account_key), models=[])).list_client_models() == []


@pytest.mark.parametrize("choice,wanted", [("none", "none"), ({"type": "none"}, "none"), ({"type": "auto"}, "auto"),
    ({"type": "required"}, "required"), ({"type": "function", "function": {"name": "f"}}, {"type": "function", "function": {"name": "f"}})])
def test_choice_official_wire_shape(choice, wanted):
    body = {"tool_choice": choice, "tools": [{"type": "function"}], "messages": [{"role": "user", "content": "unaltered"}]}
    normalize_tool_choice(body)
    assert body.get("tool_choice") == wanted
    assert "tools" in body
    assert body["messages"][0]["content"] == "unaltered"


def test_decoder_ignores_data_after_done_and_rejects_malformed_tools():
    decoder = WorkBuddyStream()
    text = b"".join(decoder.feed(x) for x in frames())
    assert decoder.feed(ev({"error": {"message": "late"}})) == b""
    assert text.count(b"[DONE]") == 1
    decoder = WorkBuddyStream()
    assert b'"error"' in decoder.feed(ev({"choices": [{"delta": {"tool_calls": [{"index": "bad"}]}}]}))


@pytest.mark.parametrize("status,refresh_result,protected", [
    (403, "never", False), (401, "network", False),
    (401, "auth", False), (401, "ok", False), (401, "never", True),
])
async def test_auth_refresh_boundary_and_no_false_auth_disable(env, monkeypatch, status, refresh_result, protected):
    m, ch = env
    if not protected:
        monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    config.update(lambda c: c.update(retry={"recovery": {"oauthRefresh": True}}))
    notices = []
    capture(monkeypatch, lambda kind, text, record: notices.append(text))
    calls = []
    async def ensure(*a, **k):
        return "fixture-access"
    async def refresh(*a, **k):
        calls.append(1)
        if refresh_result == "network":
            raise common.WorkBuddyError("refresh", kind="network")
        if refresh_result == "auth":
            raise common.WorkBuddyError("refresh", code=12153)
        assert refresh_result == "ok"
        return "fixture-access"
    monkeypatch.setattr(om, "ensure_valid_token", ensure)
    monkeypatch.setattr(om, "force_refresh", refresh)
    attempts = []
    def upstream(req):
        attempts.append(1)
        if len(attempts) == 2 and refresh_result == "ok":
            return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"})
        return httpx.Response(status, json={"error": {"message": "fixture authorization error"}})
    response, text, _ = await call(env, "chat", request_body("chat", False), upstream)
    assert len(calls) == (0 if refresh_result == "never" else 1)
    assert len(attempts) == (2 if refresh_result == "ok" else 1)
    assert (om.get_account(ch.account_key).get("disabled_reason") == "auth_error") is (refresh_result == "auth")
    assert (response.status_code == 200) is (refresh_result == "ok")
    if refresh_result == "auth":
        assert any("重新授权" in text for text in notices)
        assert all("JSON" not in text for text in notices)


async def test_workbuddy_stream_close_records_cancelled_and_closes_upstream(env):
    import asyncio
    m, ch = env
    stream = fake.TerminalThenHangByteStream(b"".join(frames(terminal=False, usage=False)))
    router = fake.MockRouter()
    router.register("https://copilot.tencent.com", lambda req: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"}))
    response, client = await fake._call_openai_handler(m, router, "chat", request_body("chat", True))
    emitted = b""
    try:
        for _ in range(10):
            item = await asyncio.wait_for(anext(response.body_iterator), 1)
            emitted += item.encode() if isinstance(item, str) else item
            if b"WorkBuddy" in emitted:
                break
        assert b"WorkBuddy" in emitted and b"[DONE]" not in emitted
        await response.body_iterator.aclose()
    finally:
        await client.aclose()
    assert stream.closed.is_set() and len(router.requests) == 1
    latest = m["log_db"]._get_conn().execute("SELECT status,error_message FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert latest["status"] == "cancelled" and latest["error_message"] == "client disconnected"
    assert m["cooldown"].get_state(ch.key, "glm-fixture") is None


from src.tests.notification_helpers import capture
