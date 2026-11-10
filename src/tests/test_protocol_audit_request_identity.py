"""P04(non-stream), P06–P10, P12 offline regressions from the 2026-09-29 audit.

Use src/tests/isolated_pytest.py; no provider/account/network requests.
"""
from __future__ import annotations

import copy
import json

import pytest

from src.channel.api_channel import ApiChannel
from src.channel.oauth_channel import OAuthChannel
from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.transform import (
    anthropic_to_chat, anthropic_to_responses, chat_to_anthropic,
    chat_to_responses, responses_to_chat, responses_to_anthropic,
)
from src.openai.transform.codex_oauth_transform import apply_codex_oauth_transform
from src.openai.transform.guard import GuardError
from src.openai.transform.tool_arguments import ToolArgumentsError
from src.protocols.runtime import apply_non_stream_response_translator
from src.providers import registry
from src.transform import cc_mimicry

SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}},
          "required": ["result"], "additionalProperties": False}
CHAT_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "result", "description": "Result schema", "schema": SCHEMA, "strict": True}}
RESPONSES_FORMAT = {"type": "json_schema", **CHAT_FORMAT["json_schema"]}


def anthropic_channel(mimic=False):
    return ApiChannel({"name": "audit-anthropic", "baseUrl": "https://fixture.invalid",
                       "apiKey": "offline", "cc_mimicry": mimic, "models": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["c2r", "r2c", "c2a", "r2a"])
async def test_schema_reaches_actual_channel_wire(direction):
    chat = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "Return a result"}],
            "response_format": copy.deepcopy(CHAT_FORMAT)}
    responses = {"model": "gpt-4.1", "input": "Return a result",
                 "text": {"format": copy.deepcopy(RESPONSES_FORMAT)}}
    source = chat if direction.startswith("c") else responses
    before = copy.deepcopy(source)
    if direction.endswith("a"):
        channel = anthropic_channel()
        model = "claude-sonnet-4-6"
    else:
        channel = OpenAIApiChannel({"name": "audit-openai", "baseUrl": "https://fixture.invalid",
            "apiKey": "offline", "protocol": "openai-responses" if direction == "c2r" else "openai-chat"})
        model = "gpt-4.1"
    req = await channel.build_upstream_request(source, model,
        ingress_protocol="chat" if direction.startswith("c") else "responses")
    wire = json.loads(req.body)
    if direction == "c2r":
        assert wire["text"]["format"] == RESPONSES_FORMAT
    elif direction == "r2c":
        assert wire["response_format"] == CHAT_FORMAT
    else:
        assert wire["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert source == before


@pytest.mark.parametrize("fmt", [{"type": "text"}, {"type": "json_object"},
    {"type": "json_schema", "json_schema": {"name": "result", "schema": SCHEMA, "strict": False}}])
def test_schema_roundtrip_and_simple_formats(fmt):
    out = chat_to_responses.translate_request({"model": "gpt-4.1", "messages": [], "response_format": fmt})
    restored = responses_to_chat.translate_request(out)
    assert restored["response_format"] == fmt


@pytest.mark.asyncio
async def test_native_thinking_tool_continuation_keeps_order_signature_and_redaction():
    blocks = [{"type": "thinking", "thinking": "fixture thought", "signature": "fixture-signature"},
              {"type": "redacted_thinking", "data": "opaque-replay"},
              {"type": "tool_use", "id": "tool1", "name": "Read", "input": {"path": "a"}}]
    body = {"model": "claude-sonnet-4-5", "thinking": {"type": "enabled", "budget_tokens": 1024},
        "messages": [{"role": "user", "content": "Read file"}, {"role": "assistant", "content": blocks},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "ok"}]}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
    before = copy.deepcopy(body)
    req = await anthropic_channel().build_upstream_request(body, body["model"])
    wire = json.loads(req.body)
    assert wire["messages"][1]["content"] == blocks
    assert wire["thinking"] == body["thinking"]
    assert body == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mimic", [False, True])
async def test_native_server_tool_types_and_names_survive_many_tools(mimic):
    native = [{"type": "code_execution_20250825", "name": "code_execution"},
              {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"},
              {"type": "future_native_20260929", "name": "future_native", "native_option": True}]
    tools = native + [{"name": f"fn{i}", "input_schema": {"type": "object"}} for i in range(7)]
    req = await anthropic_channel(mimic).build_upstream_request(
        {"messages": [{"role": "user", "content": "hi"}], "tools": tools}, "claude-sonnet-4-6")
    wire = json.loads(req.body)
    for actual, expected in zip(wire["tools"], native):
        assert {k: v for k, v in actual.items() if k != "cache_control"} == expected
        assert "input_schema" not in actual


def parse_events(wire):
    from src.protocols.sse import split_sse_events
    tail, events = split_sse_events(wire)
    assert not tail
    out = []
    for event in events:
        data = []
        for line in event.splitlines():
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
        if data:
            out.append(json.loads(b"\n".join(data)))
    return out


def sse_fixture(newline, multiline):
    event = {"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "call1", "name": "fakeRead01", "input": {
            "type": "tool_use", "name": "fakeRead01", "text": "中文 fakeRead01"}}}
    text = {"type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "中文 fakeRead01 cc_sess_list"}}
    data = json.dumps(event, ensure_ascii=False)
    if multiline:
        data = data.replace(', "index"', ',' + newline + 'data: "index"', 1)
    raw = ("id: audit" + newline + "event: content_block_start" + newline + "data: " + data + newline * 2
           + "event: content_block_delta" + newline + "data: " + json.dumps(text, ensure_ascii=False) + newline * 2).encode()
    expected = copy.deepcopy(event)
    expected["content_block"]["name"] = "Read"
    return raw, [expected, text]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("multiline", [False, True])
def test_tool_name_recovery_at_every_network_boundary(newline, multiline):
    raw, expected = sse_fixture(newline, multiline)
    for split in range(len(raw) + 1):
        state = cc_mimicry.ToolNameRestoreMap({"Read": "fakeRead01"})
        wire = state.feed(raw[:split]) + state.feed(raw[split:])
        assert parse_events(wire) == expected, split
    state = cc_mimicry.ToolNameRestoreMap({"Read": "fakeRead01"})
    assert parse_events(b"".join(state.feed(bytes([byte])) for byte in raw)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth", [False, True])
async def test_request_scoped_restore_state_is_wired_and_isolated(monkeypatch, oauth):
    if oauth:
        from src import oauth_manager
        async def token(*args, **kwargs):
            return "offline-token"
        monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
        channel = OAuthChannel({"email": "audit@example.test", "models": ["claude-sonnet-4-6"]})
    else:
        channel = anthropic_channel(True)
    body = {"stream": True, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "sessions_list", "input_schema": {"type": "object"}}]}
    one = await channel.build_upstream_request(body, "claude-sonnet-4-6")
    two = await channel.build_upstream_request(body, "claude-sonnet-4-6")
    assert isinstance(one.dynamic_tool_map, cc_mimicry.ToolNameRestoreMap)
    assert one.dynamic_tool_map is not two.dynamic_tool_map
    raw = b'data: {"type":"content_block_start","content_block":{"type":"tool_use","name":"cc_sess_list","input":{}}}\n\n'
    raw = raw.replace(b"cc_sess_list", json.loads(one.body)["tools"][0]["name"].encode())
    split = len(raw) // 2
    assert await registry.restore_response_bytes(channel, raw[:split], dynamic_map=one.dynamic_tool_map) == b""
    full = await registry.restore_response_bytes(channel, raw, dynamic_map=two.dynamic_tool_map)
    rest = await registry.restore_response_bytes(channel, raw[split:], dynamic_map=one.dynamic_tool_map)
    assert parse_events(full) == parse_events(rest)
    assert parse_events(full)[0]["content_block"]["name"] == "sessions_list"


TOOLS = [{"type": "function", "name": name, "parameters": {"type": "object"}} for name in ("Read", "Write")]


@pytest.mark.parametrize("lite", [False, True])
@pytest.mark.parametrize("mode", ["required", "auto"])
def test_codex_allowed_tools_compiles_restriction_not_auto(lite, mode):
    body = {"model": "gpt-5.4", "input": "hi", "tools": copy.deepcopy(TOOLS),
            "tool_choice": {"type": "allowed_tools", "mode": mode, "tools": [{"type": "function", "name": "Read"}]}}
    out = apply_codex_oauth_transform(body, use_responses_lite=lite, lite_thread_context="audit-session")
    tools = next(item["tools"] for item in out["input"] if item.get("type") == "additional_tools") if lite else out["tools"]
    assert tools == TOOLS[:1]
    assert out["tool_choice"] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("lite", [False, True])
async def test_codex_allowed_tools_reaches_actual_channel_wire(monkeypatch, lite):
    from src import oauth_manager
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    account = {"email": "audit@example.test", "provider": "openai", "models": ["gpt-5.4"],
        "chatgpt_account_id": "audit-account", "account_model_catalog": {"schema": 1, "models": [
            {"id": "gpt-5.4", "useResponsesLite": lite}]}}
    async def token(*args, **kwargs):
        return "offline-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(oauth_manager, "get_account", lambda *args: account)
    channel = OpenAIOAuthChannel(account)
    req = await channel.build_upstream_request({"model": "gpt-5.4", "input": "hi",
        "tools": copy.deepcopy(TOOLS), "tool_choice": {"type": "allowed_tools", "mode": "required",
            "tools": [{"type": "function", "name": "Read"}]}}, "gpt-5.4", ingress_protocol="responses")
    wire = json.loads(req.body)
    tools = next(i["tools"] for i in wire["input"] if i.get("type") == "additional_tools") if lite else wire["tools"]
    assert tools == TOOLS[:1]
    assert wire["tool_choice"] == "required"


def test_codex_allowed_tools_narrows_namespace_and_mcp_without_broadening():
    body = {"model": "gpt-5.4", "input": [], "tools": [
        {"type": "namespace", "name": "fs", "tools": copy.deepcopy(TOOLS)},
        {"type": "mcp", "server_label": "files", "server_url": "https://fixture.invalid",
            "allowed_tools": {"tool_names": ["Read", "Write"], "read_only": True}}],
        "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": [
            {"type": "function", "namespace": "fs", "name": "Read"},
            {"type": "mcp", "server_label": "files", "name": "Read"}]}}
    out = apply_codex_oauth_transform(body, use_responses_lite=False)
    assert out["tools"][0]["tools"] == TOOLS[:1]
    assert out["tools"][1]["allowed_tools"] == {"tool_names": ["Read"], "read_only": True}
    assert out["tool_choice"] == "required"


@pytest.mark.parametrize("lite", [False, True])
def test_codex_allowed_tools_empty_auto_disables_tools(lite):
    body = {"model": "gpt-5.4", "input": [], "tools": copy.deepcopy(TOOLS),
            "tool_choice": {"type": "allowed_tools", "mode": "auto", "tools": []}}
    out = apply_codex_oauth_transform(body, use_responses_lite=lite, lite_thread_context="audit-session")
    if lite:
        assert next(i["tools"] for i in out["input"] if i.get("type") == "additional_tools") == []
    else:
        assert not out.get("tools")
    assert out.get("tool_choice", "none") == "none"


@pytest.mark.parametrize("refs", [[], [{"type": "function", "name": "Missing"}]])
def test_codex_unsatisfiable_required_constraint_is_not_broadened(refs):
    body = {"model": "gpt-5.4", "input": [], "tools": copy.deepcopy(TOOLS),
            "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": refs}}
    with pytest.raises(GuardError, match="allowed_tools"):
        apply_codex_oauth_transform(body, use_responses_lite=False)


def upstream_tool_response(protocol, arguments):
    if protocol == "responses":
        return {"id": "resp1", "status": "completed", "output": [{"type": "function_call",
            "call_id": "call1", "name": "Read", "arguments": arguments}]}
    return {"id": "chat1", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
            "function": {"name": "Read", "arguments": arguments}}]}}]}


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("raw", ['{"path":"a"}', '\ufeff{"path":"a"}', '```json\n{"path":"a"}\n```',
                                '"{\\"path\\":\\"a\\"}"', '{"path":"a",}'])
def test_tool_arguments_recover_without_fake_raw_input(protocol, raw):
    obj = upstream_tool_response(protocol, raw)
    out = apply_non_stream_response_translator(obj, {"response_translator": "anthropic_to_" + protocol})
    assert out["content"][0] == {"type": "tool_use", "id": "call1", "name": "Read", "input": {"path": "a"}}
    assert out["stop_reason"] == "tool_use"
    assert obj == upstream_tool_response(protocol, raw)


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("raw", ["{bad", '{"path":', '{"path":"a"', '[]', 'null', '42', '"text"', '', '{"x":NaN}', '{,}', '{"x":[,]}'])
def test_unrecoverable_arguments_keep_original_error_and_never_tool_success(protocol, raw):
    with pytest.raises(ToolArgumentsError) as caught:
        apply_non_stream_response_translator(upstream_tool_response(protocol, raw),
                                            {"response_translator": "anthropic_to_" + protocol})
    err = caught.value
    assert err.status == 502 and err.err_type == "api_error"
    assert err.raw_arguments == raw and err.original_error
    assert repr(raw) in err.message
    assert err.tool_name == "Read"


def test_trailing_comma_repair_does_not_modify_string_values():
    raw = '{"path":"a,}","data":["x,]",1,],}'
    out = anthropic_to_chat.translate_response(upstream_tool_response("chat", raw))
    assert out["content"][0]["input"] == {"path": "a,}", "data": ["x,]", 1]}


def test_history_arguments_use_same_recovery_not_blanket_rejection():
    body = {"messages": [{"role": "assistant", "tool_calls": [{"type": "function", "id": "c1",
        "function": {"name": "Read", "arguments": '```json\n{"path":"a"}\n```'}}]}]}
    out = chat_to_anthropic.translate_request(body)
    assert out["messages"][0]["content"][0]["input"] == {"path": "a"}
    body["messages"][0]["tool_calls"][0]["function"]["arguments"] = "{bad"
    with pytest.raises(ToolArgumentsError) as caught:
        chat_to_anthropic.translate_request(body)
    assert caught.value.status == 400 and caught.value.raw_arguments == "{bad"


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded"])
def test_nonstream_anthropic_truncation_wins_over_tool_presence(reason):
    obj = {"id": "msg1", "stop_reason": reason, "content": [{"type": "tool_use", "id": "t1",
        "name": "Read", "input": {"path": "a"}}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    chat = chat_to_anthropic.translate_response(obj)
    assert chat["choices"][0]["finish_reason"] == "length"
    responses = responses_to_anthropic.translate_response(obj, model="fixture")
    assert responses["status"] == "incomplete"
    assert responses["incomplete_details"] == {"reason": "max_output_tokens"}
    assert responses["output"][0]["type"] == "function_call"
