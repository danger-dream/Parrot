"""Official-source constructed wire fixtures, never captures/live credentials."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src import config, upstream
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
from src.openai.transform.guard import GuardError
from src.providers import registry, workbuddy_context as context, workbuddy_replay as replay
from src.providers.workbuddy_codec import WorkBuddyStream
from src.protocols import runtime
from src.tests.test_workbuddy_channel import env, call, request_body, ev, frames, wire_json
from src.tests import test_protocol_fake_upstreams as fake

FIXTURES = Path(__file__).parent / "fixtures" / "workbuddy"


def data_events(raw):
    from src.protocols.sse import split_sse_events
    _, events = split_sse_events(raw if isinstance(raw, bytes) else raw.encode())
    result = []
    for event in events:
        data = b"\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
        if data and data != b"[DONE]":
            result.append(json.loads(data))
    return result


def fixture_round(round):
    if round == 0:
        raw = (FIXTURES / "sse_multiturn_toolcall_null_usage.sse").read_bytes()
        # Contract mergeExtraFields: scalars replace, nested dicts shallow merge.
        extra = ev({"choices": [{"index": 0, "delta": {"extra_fields": {"signature": "old", "_meta": {"a": 1}}}}]})
        raw = extra + raw.replace(b'data: [DONE]', ev({"choices": [{"index": 0, "delta": {"extra_fields": {"signature": "final-0", "_meta": {"b": 2}}}}]}).strip() + b'\n\ndata: [DONE]')
        expected = {"reasoning_content": "The user wants to read a.py. First call read_file, then summarize.",
                    "extra_fields": {"signature": "final-0", "_meta": {"a": 1, "b": 2}}}
    elif round == 1:
        raw = (FIXTURES / "sse_text_normal_usage.sse").read_bytes().replace(b'"reasoning_content":', b'"reasoning":')
        raw = ev({"choices": [{"index": 0, "delta": {"extra_fields": {"signature": "final-1"}}}]}) + raw
        expected = {"reasoning": "Trivial question, answer directly.", "extra_fields": {"signature": "final-1"}}
    else:
        raw = ev({"choices": [{"index": 0, "delta": {"reasoning_content": "orphan-thought"}, "finish_reason": "length"}]}) + b"data: [DONE]\n\n"
        expected = {"reasoning_content": "orphan-thought"}
    return raw, expected


def assistant_response(ingress, stream, text):
    if not stream:
        obj = json.loads(text)
        return obj["choices"][0]["message"] if ingress == "chat" else obj["output"] if ingress == "responses" else {"role": "assistant", "content": obj["content"]}
    if ingress == "responses":
        return next(e["response"]["output"] for e in data_events(text) if e.get("type") in ("response.completed", "response.incomplete"))
    builder = upstream.ChatSSEAssistantBuilder() if ingress == "chat" else upstream.SSEAssistantBuilder()
    builder.preserve_provider_reasoning = True
    builder.feed(text.encode())
    return builder.get_assistant()


def add_history(body, ingress, assistant, round):
    if ingress == "responses":
        body["input"].extend(assistant)
        body["input"].append({"type": "function_call_output", "call_id": "call_ab12", "output": "tool result"} if round == 0 else {"role": "user", "content": "next"})
    else:
        body["messages"].append(assistant)
        if round == 0:
            body["messages"].append({"role": "tool", "tool_call_id": "call_ab12", "content": "tool result"} if ingress == "chat" else
                                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_ab12", "content": "tool result"}]})
        else:
            body["messages"].append({"role": "user", "content": "next"})


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_three_round_replay_actual_http_paths(env, ingress, stream):
    body = request_body(ingress, stream)
    if ingress == "responses":
        body["input"] = [{"role": "user", "content": "read a.py"}]
    expected_history = []
    for round in range(3):
        raw, expected = fixture_round(round)
        def wire(req):
            messages = wire_json(req.content)["messages"]
            assistants = [m for m in messages if m["role"] == "assistant"]
            assert len(assistants) == len(expected_history)
            for msg, want in zip(assistants, expected_history):
                assert replay.fields(msg) == want
            return httpx.Response(200, stream=fake.ChunkedByteStream([raw[i:i+17] for i in range(0, len(raw), 17)]), headers={"content-type": "text/event-stream"})
        response, text, requests = await call(env, ingress, body, wire)
        assert response.status_code == 200, text
        assert len(requests) == 1
        assert '"error"' not in text or ingress == "responses"  # Responses error:null is standard.
        assistant = assistant_response(ingress, stream, text)
        if ingress == "chat":
            assert replay.fields(assistant) == expected
        elif ingress == "responses":
            assert replay.decode(assistant[-1]["encrypted_content"]) == expected
            assert len([i for i in assistant if i.get("type") == "reasoning"]) == 1
        else:
            assert replay.decode(assistant["content"][-1]["signature"]) == expected
        add_history(body, ingress, assistant, round)
        expected_history.append(expected)
    request = await env[1].build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)
    assistants = [m for m in wire_json(request.body)["messages"] if m["role"] == "assistant"]
    assert [replay.fields(m) for m in assistants] == expected_history
    assert assistants[0]["tool_calls"][0]["id"] == "call_ab12"


@pytest.mark.parametrize("filename", ["request_toolchoice_function.json", "request_toolchoice_none_keeps_tools.json", "request_multiturn_reasoning_signature.json"])
async def test_official_request_fixtures_preserve_parameters_and_history(env, filename):
    body = json.loads((FIXTURES / filename).read_text())
    body.update(model="glm-fixture", parallel_tool_calls=False, temperature=0.37, top_p=0.8, max_thinking_tokens=123,
                reasoning_summary="auto")
    before = copy.deepcopy(body)
    request = await env[1].build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    payload = wire_json(request.body)
    assert payload["messages"][1:] == before["messages"]
    for key in ("tool_choice", "tools", "temperature", "top_p", "max_tokens", "parallel_tool_calls", "reasoning_summary", "max_thinking_tokens"):
        assert payload.get(key) == before.get(key)
    assert body == before


@pytest.mark.parametrize("effort", ["none", "off"])
@pytest.mark.parametrize("declared", [None, False, True])
async def test_disable_thinking_is_explicit_catalog_contract(env, effort, declared):
    def update(c):
        record = c["oauthAccounts"][0]["account_model_catalog"]["models"][0]
        if declared is None:
            record.pop("canDisableThinking", None)
        else:
            record["canDisableThinking"] = declared
    config.update(update)
    body = request_body("chat", False)
    body["reasoning_effort"] = effort
    if declared is not True:
        with pytest.raises(GuardError):
            await env[1].build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    else:
        req = await env[1].build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
        assert "reasoning_effort" not in wire_json(req.body)


@pytest.mark.parametrize("profile", ["cn", "ide", "legacy"])
async def test_catalog_wire_id_gzip_and_profile_ids(env, profile):
    def update(c):
        account = c["oauthAccounts"][0]
        if profile != "cn":
            account.update(realm="global", domain="www.codebuddy.ai" if profile == "ide" else "www.workbuddy.ai",
                           workbuddy_client_profile="ide" if profile == "ide" else "cli")
        account["account_model_catalog"]["models"][0].update(wireModel="real-wire-id", requestBodyGzip=True)
    config.update(update)
    ch = WorkBuddyOAuthChannel(config.get()["oauthAccounts"][0])
    body = request_body("chat", False)
    context.capture_headers(body, {"session-id": "explicit-session", "x-conversation-request-id": "explicit-turn"})
    body["_api_key_name"] = "owner"
    body = context.ensure_context(body, request_id="logical", api_key_name="owner")
    first = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    retry = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    assert first.translator_ctx["model_for_response"] != "real-wire-id"
    assert first.dispatch_metadata.outbound_model_id == "real-wire-id"
    if profile == "legacy":
        assert "Content-Encoding" not in first.headers and "X-Conversation-ID" not in first.headers
        assert json.loads(first.body)["model"] == "real-wire-id"
        return
    assert first.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(first.body))["model"] == "real-wire-id"
    assert first.headers["X-Conversation-ID"] == retry.headers["X-Conversation-ID"]
    assert first.headers["X-Conversation-Request-ID"] == retry.headers["X-Conversation-Request-ID"]
    assert first.headers["X-Private-Data"] == "true"
    assert not any("device-token" in key.lower() for key in first.headers)
    if profile == "ide":
        assert first.headers["X-Request-ID"] == first.headers["X-Conversation-Request-ID"]
        assert first.headers["X-Conversation-Message-ID"] != retry.headers["X-Conversation-Message-ID"]
        assert first.headers["X-Model-ID"] == "glm-fixture"
    else:
        assert first.headers["X-Request-ID"] == first.headers["X-Conversation-Message-ID"] == retry.headers["X-Request-ID"]
        assert "X-Model-ID" not in first.headers
    # New logical tool step, same explicit turn; new user turn, new turn id.
    body.pop(context.KEY)
    step = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    assert step.headers["X-Conversation-ID"] == first.headers["X-Conversation-ID"]
    assert step.headers["X-Conversation-Request-ID"] == first.headers["X-Conversation-Request-ID"]
    assert step.headers["X-Conversation-Message-ID"] != first.headers["X-Conversation-Message-ID"]
    context.capture_headers(body, {"session-id": "explicit-session", "x-conversation-request-id": "next-turn"})
    later = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")
    assert later.headers["X-Conversation-ID"] == first.headers["X-Conversation-ID"]
    assert later.headers["X-Conversation-Request-ID"] != first.headers["X-Conversation-Request-ID"]


def test_no_fake_session_or_cross_owner_attribution():
    body = context.ensure_context({}, request_id="request")
    assert "X-Conversation-ID" not in context.headers(body, "account")
    context.capture_headers(body, {"session-id": "same", "x-query-id": "same"})
    a = context.headers(context.ensure_context(body, api_key_name="a"), "account")
    b = context.headers(context.ensure_context(body, api_key_name="b"), "account")
    assert a["X-Conversation-ID"] != b["X-Conversation-ID"]


def test_codec_choice_isolation_signature_merge_and_tool_indexes():
    wire = b""
    for index in (0, 1):
        for delta in ({"reasoning": f"r{index}", "extra_fields": {"signature": "old"}},
                      {"extra_fields": {"signature": f"s{index}"}, "tool_calls": [
                          {"id": "a", "function": {"name": "f", "arguments": "{"}},
                          {"id": "b", "function": {"name": "g", "arguments": "{"}}]},
                      {"tool_calls": [{"id": "a", "function": {"arguments": "}"}}, {"id": "b", "function": {"arguments": "}"}}]}):
            wire += ev({"choices": [{"index": index, "delta": delta}]})
        wire += ev({"choices": [{"index": index, "delta": {}, "finish_reason": "tool_calls"}]})
    decoded = WorkBuddyStream().feed(wire + b"data: [DONE]\n\n")
    builder = upstream.ChatSSEAssistantBuilder()
    builder.preserve_provider_reasoning = True
    builder.feed(decoded)
    choices = builder.to_full_json(id="fixture", model="fixture", created=0)["choices"]
    for i, choice in enumerate(choices):
        assert choice["message"]["reasoning"] == f"r{i}"
        assert choice["message"]["extra_fields"] == {"signature": f"s{i}"}
        assert [t["id"] for t in choice["message"]["tool_calls"]] == ["a", "b"]
        assert [t["function"]["arguments"] for t in choice["message"]["tool_calls"]] == ["{}", "{}"]


@pytest.mark.parametrize("role_only", [False, True])
async def test_usage_without_output_never_success(env, role_only):
    raw = (ev({"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}) if role_only else b"")
    raw += ev({"choices": None, "usage": {"prompt_tokens": 1}}) + b"data: [DONE]\n\n"
    result, text, requests = await call(env, "chat", request_body("chat", False), lambda req: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))
    assert result.status_code != 200 and len(requests) == 1


def test_foreign_provider_does_not_get_tagged_replay_or_new_parameters():
    from src.providers.base import OpenAIApiAdapter, AnthropicStandardAdapter
    value = {"reasoning": "private", "extra_fields": {"signature": "sig"}}
    item = replay.response_item(value)
    block = replay.thinking_block(value)
    adapter = OpenAIApiAdapter()
    filtered = adapter.filter_request_payload({"input": [item], "max_thinking_tokens": 2}, protocol="openai-responses")
    assert filtered["input"] == [] and "max_thinking_tokens" not in filtered
    filtered = AnthropicStandardAdapter().filter_request_payload({"messages": [{"role": "assistant", "content": [block, {"type": "text", "text": "keep"}]}]}, protocol="anthropic")
    assert filtered["messages"][0]["content"] == [{"type": "text", "text": "keep"}]
    orphan = AnthropicStandardAdapter().filter_request_payload({"messages": [{"role": "assistant", "content": [block]}]}, protocol="anthropic")
    assert orphan["messages"] == []
    chat = adapter.filter_request_payload({"messages": [{"role": "assistant", "content": "keep", **value}]}, protocol="openai-chat")
    assert "extra_fields" not in chat["messages"][0]


@pytest.mark.parametrize("status", [200, 401, 429, 500])
@pytest.mark.parametrize("code", [14001, 14012, 14013, 14014, 14018, 14019])
def test_explicit_quota_never_retried_even_if_server_error(status, code):
    result = runtime.AttemptResult(outcome="upstream_http_error", http_status=status,
                                   full_response_text=json.dumps({"error": {"type": "server_error", "code": code}}))
    ch = SimpleNamespace(provider="workbuddy", protocol="openai-chat")
    assert runtime.bounded_account_quota_error(result, ch)["code"] == str(code)
    assert runtime.retryable_transient_error_kind(ch, result) is None


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_gzip_actual_http_wire_and_public_response_model(env, ingress, stream):
    config.update(lambda c: c["oauthAccounts"][0]["account_model_catalog"]["models"][0].update(wireModel="wire-only-model", requestBodyGzip=True))
    def wire(req):
        assert req.headers["content-encoding"] == "gzip"
        assert int(req.headers["content-length"]) == len(req.content)
        payload = json.loads(gzip.decompress(req.content))
        assert payload["model"] == "wire-only-model"
        raw = b"".join(frames()).replace(b'glm-fixture', b'wire-only-model')
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})
    response, text, requests = await call(env, ingress, request_body(ingress, stream), wire)
    assert response.status_code == 200 and len(requests) == 1, text
    assert "wire-only-model" not in text and "glm-fixture" in text


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_store_preserves_final_carrier_for_next_request(env, stream):
    from src.openai import store
    store.init()
    store._reset_for_test()
    config.update(lambda c: c.setdefault("openai", {}).setdefault("store", {}).update(enabled=True))
    raw, expected = fixture_round(0)
    body = request_body("responses", stream)
    response, text, _ = await call(env, "responses", body, lambda req: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))
    assert response.status_code == 200, text
    obj = next(e["response"] for e in data_events(text) if e.get("type") == "response.completed") if stream else json.loads(text)
    body["previous_response_id"] = obj["id"]
    body["input"] = [{"type": "function_call_output", "call_id": "call_ab12", "output": "result"}]
    def wire(req):
        payload = wire_json(req.content)
        assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
        assert replay.fields(assistant) == expected
        assert assistant["tool_calls"][0]["id"] == "call_ab12"
        return httpx.Response(200, content=fixture_round(1)[0], headers={"content-type": "text/event-stream"})
    response, text, requests = await call(env, "responses", body, wire)
    assert response.status_code == 200 and len(requests) == 1, text


@pytest.mark.parametrize("profile", ["cn", "ide", "legacy"])
@pytest.mark.parametrize("gzip_flag", ["missing", False, True])
async def test_last_good_catalog_inherits_profile_gzip_default(env, profile, gzip_flag):
    account = copy.deepcopy(env[1].account)
    if profile != "cn":
        account.update(realm="global", domain="www.codebuddy.ai" if profile == "ide" else "www.workbuddy.ai",
                       workbuddy_client_profile="ide" if profile == "ide" else "cli")
    record = account["account_model_catalog"]["models"][0]
    record.pop("requestBodyGzip", None)
    if gzip_flag != "missing":
        record["requestBodyGzip"] = gzip_flag
    config.update(lambda c: c.update(oauthAccounts=[account]))
    ch = WorkBuddyOAuthChannel(account)
    before = copy.deepcopy(config.get()["oauthAccounts"][0]["account_model_catalog"])
    req = await ch.build_upstream_request(request_body("chat", False), "glm-fixture", ingress_protocol="chat")
    compressed = profile != "legacy" and gzip_flag is not False
    assert (req.headers.get("Content-Encoding") == "gzip") is compressed
    assert req.body.startswith(b"\x1f\x8b") is compressed
    assert wire_json(req.body)["model"] == "glm-fixture"
    assert config.get()["oauthAccounts"][0]["account_model_catalog"] == before


@pytest.mark.parametrize("ingress", ["chat", "responses"])
@pytest.mark.parametrize("effort,declared,accepted", [("none", ["none", "low"], True), ("off", ["none", "low"], False),
                                                      ("off", ["off", "low"], True), ("none", [], True)])
async def test_legacy_effort_keeps_original_declared_validation(env, ingress, effort, declared, accepted):
    account = copy.deepcopy(env[1].account)
    account.update(realm="global", domain="www.workbuddy.ai", workbuddy_client_profile="cli")
    record = account["account_model_catalog"]["models"][0]
    record["reasoningEfforts"] = declared
    record.pop("canDisableThinking", None)
    config.update(lambda c: c.update(oauthAccounts=[account]))
    ch = WorkBuddyOAuthChannel(account)
    body = request_body(ingress, False)
    body["reasoning_effort" if ingress == "chat" else "reasoning"] = effort if ingress == "chat" else {"effort": effort}
    if not accepted:
        with pytest.raises(GuardError):
            await ch.build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)
    else:
        req = await ch.build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)
        assert json.loads(req.body)["reasoning_effort"] == effort
        assert "Content-Encoding" not in req.headers


@pytest.mark.parametrize("profile", ["cn", "ide"])
async def test_modern_anthropic_disabled_still_requires_declaration(env, profile):
    account = dict(env[1].account)
    if profile == "ide":
        account.update(realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
    config.update(lambda c: c.update(oauthAccounts=[account]))
    body = request_body("anthropic", False)
    body["thinking"] = {"type": "disabled"}
    with pytest.raises(GuardError):
        await WorkBuddyOAuthChannel(account).build_upstream_request(body, "glm-fixture", ingress_protocol="anthropic")


@pytest.mark.parametrize("output_effort", [None, "low"])
async def test_legacy_anthropic_disabled_retains_original_translation(env, output_effort):
    account = dict(env[1].account, realm="global", domain="www.workbuddy.ai", workbuddy_client_profile="cli")
    config.update(lambda c: c.update(oauthAccounts=[account]))
    body = request_body("anthropic", False)
    body["thinking"] = {"type": "disabled"}
    if output_effort:
        body["output_config"] = {"effort": output_effort}
    req = await WorkBuddyOAuthChannel(account).build_upstream_request(body, "glm-fixture", ingress_protocol="anthropic")
    assert json.loads(req.body).get("reasoning_effort") == output_effort


async def test_gzip_remote_false_and_no_defaults(env):
    config.update(lambda c: c["oauthAccounts"][0]["account_model_catalog"]["models"][0].update(requestBodyGzip=False, defaultReasoningEffort="high"))
    req = await env[1].build_upstream_request(request_body("chat", False), "glm-fixture", ingress_protocol="chat")
    payload = wire_json(req.body)
    assert "Content-Encoding" not in req.headers
    assert not {"reasoning_effort", "temperature", "max_tokens", "max_completion_tokens"} & payload.keys()


@pytest.mark.parametrize("done", [False, True])
async def test_usage_only_and_bare_error_event_cannot_complete(env, done):
    raw = ev({"choices": None, "usage": {"prompt_tokens": 1}}) + (b"data: [DONE]\n\n" if done else b"")
    response, _, requests = await call(env, "chat", request_body("chat", False), lambda req: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))
    assert response.status_code != 200 and len(requests) == 1
    decoder = WorkBuddyStream()
    assert b'"error"' in decoder.feed(b"event:error\n\n")


def test_both_reasoning_spellings_and_invalid_carrier_are_not_guessed():
    value = {"reasoning": "one", "reasoning_content": "two", "extra_fields": {"signature": "sig"}}
    assert replay.decode(replay.encode(value)) == value
    item = replay.response_item(value)
    assert replay.fields(replay.responses_messages([item, {"role": "user", "content": "next"}])[0]) == value
    with pytest.raises(GuardError):
        replay.decode(replay.PREFIX + "bad-base64")
    assert replay.decode("other-provider-signature") is None
    assert b'"error"' in WorkBuddyStream().feed(ev({"error": {}, "choices": []}))
    assert b'12153' in WorkBuddyStream().feed(ev({"error": {"code": "12153"}}))


def test_provider_aggregation_is_opt_in():
    wire = ev({"choices": [{"index": 0, "delta": {"reasoning": "one", "extra_fields": {"signature": "sig"}, "content": "text"}, "finish_reason": "stop"}]})
    ordinary = upstream.ChatSSEAssistantBuilder()
    ordinary.feed(wire)
    assert ordinary.get_assistant() == {"role": "assistant", "content": "text"}


async def test_401_replay_once_preserves_cn_logical_message_ids(env, monkeypatch):
    from src import oauth_manager
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    config.update(lambda c: c.update(retry={"recovery": {"oauthRefresh": True}}))
    calls = []
    async def refresh(*args, **kwargs):
        calls.append(kwargs)
        return "fixture-access"
    monkeypatch.setattr(oauth_manager, "force_refresh", refresh)
    async def ensure(*args, **kwargs):
        return "fixture-access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", ensure)
    response, text, requests = await call(env, "chat", request_body("chat", False), lambda req: httpx.Response(401, json={"error": {"code": "invalid_token"}}))
    assert response.status_code != 200 and len(calls) == 1 and len(requests) == 2
    assert calls[0].get("observation")  # Existing token-observation/CAS path remains wired.
    for key in ("x-conversation-request-id", "x-conversation-message-id", "x-request-id"):
        assert requests[0].headers[key] == requests[1].headers[key]
    assert "x-conversation-id" not in requests[0].headers


async def test_quota_http_error_does_not_loop(env, monkeypatch):
    from src import oauth_manager
    async def forbidden(*args, **kwargs):
        pytest.fail("quota must not refresh authentication")
    monkeypatch.setattr(oauth_manager, "force_refresh", forbidden)
    response, _, requests = await call(env, "chat", request_body("chat", False), lambda req: httpx.Response(429, json={"error": {"type": "server_error", "code": 14018}}))
    assert response.status_code != 200 and len(requests) == 1


@pytest.mark.parametrize("pieces,wanted", [(["{}", '{"a":', '1}'], '{"a":1}'), (["{", "}", "{", '"b":2}'], '{"b":2}'), (["{", "}"], "{}")])
def test_tool_empty_object_prefix_normalization(pieces, wanted):
    raw = ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "f"}}]}}]})
    for piece in pieces:
        raw += ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-a", "function": {"arguments": piece}}]}}]})
    raw += ev({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]\n\n"
    builder = upstream.ChatSSEAssistantBuilder()
    builder.feed(WorkBuddyStream().feed(raw))
    assert builder.get_assistant()["tool_calls"][0]["function"]["arguments"] == wanted


def test_bridge_rejects_multiple_candidates_instead_of_mixing_signature():
    raw = ev({"choices": [{"index": i, "delta": {"content": str(i), "extra_fields": {"signature": str(i)}}} for i in (0, 1)]})
    assert b'"error"' in WorkBuddyStream(single_choice=True).feed(raw)


def test_official_pending_reasoning_items_and_nested_extra_merge():
    items = [
        {"type": "reasoning", "rawContent": [{"type": "reasoning_text", "text": "thought"}], "providerData": {"extra_fields": {"signature": "old", "_meta": {"a": 1}}}},
        {"type": "function_call", "call_id": "a", "name": "f", "arguments": "{}", "providerData": {"reasoning": "thought", "extra_fields": {"signature": "new", "_meta": {"b": 2}}}},
        {"type": "function_call_output", "call_id": "a", "output": "result"},
        {"type": "reasoning", "rawContent": [{"type": "reasoning_text", "text": "orphan"}]},
        {"role": "user", "content": "next"}]
    messages = replay.responses_messages(items)
    assert messages[0]["reasoning"] == "thought"
    assert messages[0]["extra_fields"] == {"signature": "new", "_meta": {"a": 1, "b": 2}}
    assert messages[2] == {"role": "assistant", "content": [], "reasoning": "orphan"}
    assert messages[3] == {"role": "user", "content": "next"}


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_name_fragment_is_not_deduplicated_and_unsigned_models_stay_plain(env, ingress, stream):
    raw = ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "f", "arguments": "{"}}]}}]})
    raw += ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "f", "arguments": "}"}}]}, "finish_reason": "tool_calls"}]}) + b"data: [DONE]\n\n"
    response, text, requests = await call(env, ingress, request_body(ingress, stream), lambda req: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))
    assert response.status_code == 200 and len(requests) == 1, text
    assert replay.PREFIX not in text
    assistant = assistant_response(ingress, stream, text)
    name = assistant["tool_calls"][0]["function"]["name"] if ingress == "chat" else assistant[0]["name"] if ingress == "responses" else assistant["content"][0]["name"]
    assert name == "ff"
