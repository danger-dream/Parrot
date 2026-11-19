"""Regression tests for OpenAI Responses API channel namespace translation and token counting."""
from __future__ import annotations

import json

import pytest

from src import token_counter
from src.openai.channel.api_channel import OpenAIApiChannel
from src.protocols.matrix import DEFAULT_MATRIX, capabilities_for_channel, extract_request_features
from src.providers import registry as provider_registry


def _make_channel() -> OpenAIApiChannel:
    return OpenAIApiChannel({
        "name": "openai-resp-api",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-responses",
        "models": [{"alias": "m", "real": "real-m"}],
    })


def test_responses_api_channel_exposes_translated_namespace_capability():
    ch = _make_channel()
    caps = capabilities_for_channel(ch)
    assert "namespace" in caps.translated_state
    assert "namespace" not in caps.native_state
    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "namespace", "name": "mcp_fs", "tools": [
            {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
        ]}],
    }
    plan = DEFAULT_MATRIX.plan("responses", "openai-responses", extract_request_features("responses", body), caps)
    assert plan.cost == 0


@pytest.mark.asyncio
async def test_responses_api_passthrough_without_namespace_has_no_dynamic_map():
    ch = _make_channel()
    req = await ch.build_upstream_request({
        "model": "m",
        "input": "hello",
        "stream": True,
        "tools": [{"type": "function", "name": "ping", "parameters": {"type": "object"}}],
    }, "real-m", ingress_protocol="responses")
    assert req.dynamic_tool_map is None


@pytest.mark.asyncio
async def test_responses_api_flattens_and_restores_sse_and_eof_tail():
    ch = _make_channel()
    body = {
        "model": "m",
        "stream": True,
        "input": [
            {"type": "function_call", "call_id": "c0", "namespace": "db", "name": "lookup", "arguments": '{"id":1}'},
            {"type": "function_call_output", "call_id": "c0", "output": "ok"},
        ],
        "tools": [
            {"type": "function", "name": "db__lookup", "parameters": {"type": "object"}},
            {
                "type": "namespace",
                "name": "db",
                "description": "DB tools",
                "tools": [
                    {"type": "function", "name": "lookup", "description": "Find row", "parameters": {"type": "object"}},
                ],
            },
            {
                "type": "namespace",
                "name": "shells",
                "tools": [
                    {"type": "custom", "name": "exec", "format": {"type": "text"}},
                ],
            },
        ],
    }
    req = await ch.build_upstream_request(body, "real-m", ingress_protocol="responses")
    assert req.dynamic_tool_map is not None
    wire = json.loads(req.body)
    names = [t["name"] for t in wire["tools"]]
    assert len(names) == 3 and len(set(names)) == 3
    flat_lookup = names[1]
    flat_exec = names[2]
    assert flat_lookup != "db__lookup"
    assert wire["input"][0]["name"] == flat_lookup
    assert "namespace" not in wire["input"][0]

    sse_1 = (
        f'event: response.output_item.added\n'
        f'data: {{"type":"response.output_item.added","item":{{"type":"function_call","call_id":"c1","name":"{flat_lookup}","arguments":""}}}}\n\n'
        f'event: response.function_call_arguments.done\n'
        f'data: {{"type":"response.function_call_arguments.done","call_id":"c1","name":"{flat_lookup}","arguments":"{{}}"}}\n\n'
    ).encode()
    # Final frame without trailing \n\n to exercise finish_response_bytes flush
    sse_tail = (
        f'event: response.completed\n'
        f'data: {{"type":"response.completed","response":{{"output":['
        f'{{"type":"function_call","call_id":"c1","name":"{flat_lookup}","arguments":"{{}}"}},'
        f'{{"type":"custom_tool_call","call_id":"c2","name":"{flat_exec}","input":"ls"}},'
        f'{{"type":"message","role":"assistant","content":[{{"type":"output_text","text":"literal {flat_lookup}"}}]}}'
        f']}}}}'
    ).encode()

    out = bytearray()
    out += await provider_registry.restore_response_bytes(ch, sse_1[:45], dynamic_map=req.dynamic_tool_map)
    out += await provider_registry.restore_response_bytes(ch, sse_1[45:] + sse_tail, dynamic_map=req.dynamic_tool_map)
    out += await provider_registry.finish_response_bytes(ch, dynamic_map=req.dynamic_tool_map)

    frames = [
        json.loads(line[5:].lstrip())
        for block in out.decode("utf-8").split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    assert len(frames) == 3
    assert frames[0]["item"]["name"] == "lookup" and frames[0]["item"]["namespace"] == "db"
    assert frames[1]["name"] == "lookup" and "namespace" not in frames[1]
    comp_out = frames[2]["response"]["output"]
    assert comp_out[0]["name"] == "lookup" and comp_out[0]["namespace"] == "db"
    assert comp_out[1]["name"] == "exec" and comp_out[1]["namespace"] == "shells" and comp_out[1]["input"] == "ls"
    assert comp_out[2]["content"][0]["text"] == f"literal {flat_lookup}"


@pytest.mark.asyncio
async def test_responses_api_non_stream_chunked_json_and_allowed_tools():
    ch = _make_channel()
    req = await ch.build_upstream_request({
        "model": "m",
        "stream": False,
        "input": "go",
        "tools": [
            {"type": "function", "name": "keep_out", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "ns", "tools": [
                {"type": "function", "name": "pick_me", "parameters": {"type": "object"}},
            ]},
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "namespace": "ns", "name": "pick_me"}],
        },
    }, "real-m", ingress_protocol="responses")
    wire = json.loads(req.body)
    assert len(wire["tools"]) == 1
    flat = wire["tools"][0]["name"]
    assert wire["tool_choice"] == "required"

    raw = json.dumps({
        "id": "resp_1",
        "object": "response",
        "output": [{"type": "function_call", "call_id": "c1", "name": flat, "arguments": "{}"}],
    }).encode()
    p1 = await provider_registry.restore_response_bytes(ch, raw[:20], dynamic_map=req.dynamic_tool_map)
    p2 = await provider_registry.restore_response_bytes(ch, raw[20:], dynamic_map=req.dynamic_tool_map)
    assert p1 == b""
    restored = json.loads(p2)
    assert restored["output"][0]["name"] == "pick_me"
    assert restored["output"][0]["namespace"] == "ns"


def test_token_counter_counts_responses_tool_outputs_parameters_and_skips_base64_images():
    text_blob = "alpha beta gamma delta " * 400
    body_text = {
        "model": "gpt-5",
        "input": [
            {"type": "function_call", "call_id": "c1", "namespace": "ns", "name": "read", "arguments": '{"path":"a.txt"}'},
            {"type": "function_call_output", "call_id": "c1", "output": text_blob},
            {"type": "reasoning", "summary": [{"type": "text", "text": "short summary"}]},
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "ns",
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "file path"}}},
                    },
                ],
            },
        ],
    }
    tokens = token_counter.count_request_tokens(body_text, model="gpt-5")
    assert tokens > 1500
    assert token_counter.count_message_tokens(body_text["input"][1], model="gpt-5") > 1500

    body_img = {
        "model": "gpt-5",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": [{"type": "input_image", "image_url": "data:image/png;base64," + ("A" * 120_000)}],
            },
        ],
    }
    assert token_counter.count_request_tokens(body_img, model="gpt-5") < 50

    body_reasoning_content = {
        "model": "gpt-5",
        "input": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": text_blob}]},
        ],
    }
    assert token_counter.count_request_tokens(body_reasoning_content, model="gpt-5") > 1500


@pytest.mark.asyncio
async def test_responses_api_rejects_namespace_tool_choice_and_cross_block_duplicate_children():
    from src.openai.transform.guard import GuardError
    ch = _make_channel()
    with pytest.raises(GuardError) as choice_exc:
        await ch.build_upstream_request({
            "model": "m",
            "input": "hi",
            "tools": [{"type": "namespace", "name": "ns", "tools": [
                {"type": "function", "name": "a", "parameters": {"type": "object"}},
            ]}],
            "tool_choice": {"type": "namespace", "name": "ns"},
        }, "real-m", ingress_protocol="responses")
    assert choice_exc.value.status == 400 and choice_exc.value.scope == "candidate"

    with pytest.raises(GuardError) as dup_exc:
        await ch.build_upstream_request({
            "model": "m",
            "input": "hi",
            "tools": [
                {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "a", "parameters": {"type": "object"}}]},
                {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "a", "parameters": {"type": "object"}}]},
            ],
        }, "real-m", ingress_protocol="responses")
    assert dup_exc.value.status == 400 and dup_exc.value.scope == "candidate"


@pytest.mark.asyncio
async def test_non_stream_truncated_body_with_namespace_map_flushes_raw_text():
    from src.protocols.runtime import prepare_non_stream_response
    ch = _make_channel()
    req = await ch.build_upstream_request({
        "model": "m",
        "stream": False,
        "input": "hi",
        "tools": [{"type": "namespace", "name": "ns", "tools": [
            {"type": "function", "name": "a", "parameters": {"type": "object"}},
        ]}],
    }, "real-m", ingress_protocol="responses")
    prepared = await prepare_non_stream_response(
        ch,
        b"<html>502 Bad Gateway</html>",
        dynamic_map=req.dynamic_tool_map,
        connect_ms=10,
        total_ms=20,
    )
    assert prepared.error is not None
    assert "<html>502 Bad Gateway</html>" in prepared.restored_text


