"""P12: real non-stream consumers + failover settlement, offline wire fixtures only."""
from __future__ import annotations

import json

import httpx
import pytest

from src.tests.test_protocol_fake_upstreams import (
    _import_modules, _setup, _install_channels, _install_keys, _default_key,
    _make_openai_channel, _make_openai_oauth_channel, _call_anthropic_core,
    _call_openai_handler, MockRouter, FakeOAuthResponseWs, ChunkedByteStream,
    stored_log_bodies,
)


RECOVERABLE = '```json\n{"path":"a",}\n```'
INVALID = '{"path":'


def response_json(protocol, arguments, *, truncated=False):
    if protocol == "openai-chat":
        return {"id": "chatcmpl_audit", "object": "chat.completion", "model": "gpt-real", "choices": [{
            "index": 0, "finish_reason": "length" if truncated else "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call1", "type": "function", "function": {"name": "Read", "arguments": arguments}}]}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}}
    return {"id": "resp_audit", "object": "response", "model": "gpt-real",
        "status": "incomplete" if truncated else "completed",
        "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
        "output": [{"type": "function_call", "id": "fc_audit", "call_id": "call1",
            "name": "Read", "arguments": arguments, "status": "completed"}],
        "usage": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11}}


def response_events(obj):
    # Completed snapshot preserves the exact original argument string.
    terminal = "response.incomplete" if obj["status"] == "incomplete" else "response.completed"
    return [
        {"type": "response.created", "response": {"id": obj["id"], "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**obj["output"][0], "arguments": "", "status": "in_progress"}},
        {"type": terminal, "response": obj},
    ]


def response_sse(protocol, obj):
    if protocol == "openai-chat":
        choice = obj["choices"][0]
        chunk = {**obj, "object": "chat.completion.chunk", "choices": [{
            "index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, **choice["message"]["tool_calls"][0]}]},
            "finish_reason": choice["finish_reason"]}]}
        return ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
    return b"".join(("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode()
                    for e in response_events(obj))


def observe_success_effects(monkeypatch, m):
    from src.openai import store
    calls = []
    targets = [
        (m["log_db"], "finish_success"),
        (m["failover"].finalize_policy, "apply_success_health_effects"),
        (m["failover"].compaction_owner, "persist_observed_safe"),
        (m["failover"], "_write_affinity_non_stream"),
        (m["failover"], "_maybe_save_native_responses_store"),
        (store, "save"),
    ]
    for owner, name in targets:
        original = getattr(owner, name)
        def observed(*args, _original=original, _name=name, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(owner, name, observed)
    return calls


def assert_settlement(m, response, effects, arguments, *, expected_stop="tool_use"):
    obj = json.loads(response.body)
    row = m["log_db"]._get_conn().execute(
        "SELECT r.status, r.http_status, d.response_body, r.output_tokens, r.usage_observed "
        "FROM request_log r LEFT JOIN request_detail d ON d.request_id=r.request_id ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    if arguments == INVALID:
        assert response.status_code >= 500, obj
        assert obj["type"] == "error"
        assert "Invalid tool arguments" in json.dumps(obj)
        assert "invalid_tool_arguments" in json.dumps(obj)
        assert "context_length_exceeded" not in json.dumps(obj)
        assert "Prompt is too long" not in json.dumps(obj)
        assert not effects, effects
        assert row["status"] == "error"
        assert row["http_status"] >= 500
        assert json.dumps(arguments) in row["response_body"]
        assert row["usage_observed"] == 1
    else:
        assert response.status_code == 200, obj
        assert obj["stop_reason"] == expected_stop
        assert obj["content"][0] == {"type": "tool_use", "id": "call1", "name": "Read", "input": {"path": "a"}}
        assert effects.count("finish_success") == 1
        assert row["status"] == "success"
        assert row["output_tokens"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("arguments", [RECOVERABLE, INVALID])
async def test_http_entry_tool_argument_translation_precedes_all_success_effects(m, monkeypatch, protocol, transport, arguments, stored_log_bodies):
    _setup(m)
    channel = _make_openai_channel("audit-args", "https://audit.invalid", protocol=protocol,
                                  alias="sonnet", real="gpt-real")
    channel.upstream_stream_only = transport == "sse"
    _install_channels(m, [channel])
    effects = observe_success_effects(monkeypatch, m)
    wire = response_json(protocol, arguments)
    router = MockRouter()
    def upstream(_request):
        if transport == "json":
            return httpx.Response(200, json=wire)
        data = response_sse(protocol, wire)
        return httpx.Response(200, stream=ChunkedByteStream([data[:17], data[17:]]),
                              headers={"content-type": "text/event-stream"})
    router.register("https://audit.invalid", upstream)
    called = []
    name = "_consume_non_stream" if transport == "json" else "_consume_stream_as_non_stream"
    consumer = getattr(m["failover"], name)
    async def trace_consumer(*args, **kwargs):
        called.append(name)
        result = await consumer(*args, **kwargs)
        assert result.usage["output_tokens"] == 4
        return result
    monkeypatch.setattr(m["failover"], name, trace_consumer)
    response, client, _ = await _call_anthropic_core(m, router, {
        "model": "sonnet", "stream": False, "max_tokens": 64,
        "messages": [{"role": "user", "content": "read"}],
        "tools": [{"name": "Read", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    })
    await client.aclose()
    assert called == [name]
    assert_settlement(m, response, effects, arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("arguments", ['{"path":"a"}', INVALID])
async def test_output_limit_with_tool_is_not_input_context_failure(m, monkeypatch, transport, arguments, stored_log_bodies):
    _setup(m)
    channel = _make_openai_channel("audit-limit", "https://audit.invalid", protocol="openai-responses",
                                  alias="sonnet", real="gpt-real")
    channel.upstream_stream_only = transport == "sse"
    _install_channels(m, [channel])
    effects = observe_success_effects(monkeypatch, m)
    wire = response_json("openai-responses", arguments, truncated=True)
    router = MockRouter()
    router.register("https://audit.invalid", lambda _: httpx.Response(200,
        **({"json": wire} if transport == "json" else {
            "content": response_sse("openai-responses", wire), "headers": {"content-type": "text/event-stream"}})))
    response, client, _ = await _call_anthropic_core(m, router, {
        "model": "sonnet", "stream": False, "messages": [{"role": "user", "content": "read"}],
    })
    await client.aclose()
    assert_settlement(m, response, effects, arguments, expected_stop="max_tokens")


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [RECOVERABLE, INVALID])
async def test_ws_nonstream_real_consumer_translates_before_success_and_settles_errors(m, monkeypatch, arguments, stored_log_bodies):
    _setup(m)
    _install_keys(m, _default_key())
    m["config"].update(lambda cfg: cfg.update({
        "network": {"routing": {"default": "direct"}},
        "timeouts": {"connect": 5, "firstByte": 5, "idle": 10, "total": 30},
        "openai": {"responsesUpstreamWsForOAuth": True},
        "oauthAccounts": [],
    }))
    channel = _make_openai_oauth_channel("audit-args@example.test")
    m["config"].update(lambda cfg: cfg.__setitem__("oauthAccounts", [{
        "email": "audit-args@example.test", "provider": "openai",
        "workspace_id": "ws-audit-args@example.test", "chatgpt_account_id": "ws-audit-args@example.test",
        "accessToken": "offline", "refreshToken": "offline", "models": ["gpt-5"],
        "account_model_catalog": {"schema": 1, "models": [{"id": "gpt-5", "useResponsesLite": False}]},
        "codexIdentity": channel.codex_account_identity.as_config(),
        "codexDeviceInstallationId": channel.codex_device_installation_id,
    }]))
    _install_channels(m, [channel])
    async def token(*args, **kwargs):
        return "offline"
    monkeypatch.setattr(m["failover"].oauth_manager, "ensure_valid_token", token)
    upstream = FakeOAuthResponseWs(response_events(response_json("openai-responses", arguments)))
    async def connect(*args, **kwargs):
        return upstream
    monkeypatch.setattr(m["failover"], "_connect_oauth_responses_ws", connect)
    # Current WS routing is native Responses. Exercise the consumer's supported
    # translator_ctx seam using the real Anthropic translator; do not fake the
    # translator, response collector, consumer, error result, or settlement.
    build = m["failover"]._build_oauth_responses_ws_upstream_request
    async def with_translator(*args, **kwargs):
        url, headers, frame, ctx, identity, metadata = await build(*args, **kwargs)
        return url, headers, frame, {**(ctx or {}), "response_translator": "anthropic_to_responses"}, identity, metadata
    monkeypatch.setattr(m["failover"], "_build_oauth_responses_ws_upstream_request", with_translator)
    consumer = m["failover"]._consume_oauth_responses_ws_non_stream
    called = []
    async def trace_consumer(*args, **kwargs):
        called.append(True)
        result = await consumer(*args, **kwargs)
        if arguments == INVALID:
            assert result.http_status == 502 and result.error_code == "invalid_tool_arguments"
            assert result.usage["output_tokens"] == 4 and result.usage_observed
            assert not result.success and not result.stream_started
            assert json.dumps(arguments) in result.full_response_text
        return result
    monkeypatch.setattr(m["failover"], "_consume_oauth_responses_ws_non_stream", trace_consumer)
    effects = observe_success_effects(monkeypatch, m)
    response, client = await _call_openai_handler(m, MockRouter(), "responses", {
        "model": "gpt-5", "stream": False, "input": "read", "prompt_cache_key": "audit-args",
    })
    await client.aclose()
    assert called == [True]
    assert upstream.closed
    if arguments == INVALID:
        obj = json.loads(response.body)
        assert response.status_code >= 500 and "error" in obj
        assert "invalid_tool_arguments" in json.dumps(obj)
        assert not effects
        row = m["log_db"]._get_conn().execute(
            "SELECT r.status, d.response_body, r.usage_observed FROM request_log r "
            "LEFT JOIN request_detail d ON d.request_id=r.request_id ORDER BY r.id DESC LIMIT 1").fetchone()
        assert row["status"] == "error" and json.dumps(arguments) in row["response_body"]
        assert row["usage_observed"] == 1
    else:
        assert_settlement(m, response, effects, arguments)
