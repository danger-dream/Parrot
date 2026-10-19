"""WorkBuddy history semantics through real ASGI routing and final mock wire."""
import copy
import json

import httpx
import pytest

from src.providers import workbuddy_replay as replay
from src.tests.test_workbuddy_request_rejection import pool, PATHS, MODEL
from src.tests.test_workbuddy_channel import frames, wire_json
from src.tests import test_protocol_fake_upstreams as fake


def assistant(cid, **extra):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": cid, "type": "function", "function": {"name": "lookup", "arguments": ' {"城市":"昆明", "n": 1} '}}], **extra}


def tool(cid):
    return {"role": "tool", "tool_call_id": cid, "content": "result:" + cid}


def call_item(cid, **extra):
    return {"type": "function_call", "call_id": cid, "name": "lookup", "arguments": ' {"城市":"昆明", "n": 1} ', **extra}


def tool_use(cid):
    return {"type": "tool_use", "id": cid, "name": "lookup", "input": {"城市": "昆明", "n": 1}}


async def outgoing(pool, ingress, payload):
    bodies = []
    def respond(req):
        bodies.append(wire_json(req.content))
        return httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"})
    pool.response_factory = respond
    body = {"model": MODEL, "stream": False, **payload}
    if ingress == "anthropic":
        body["max_tokens"] = 64
    response = await pool.client.post(PATHS[ingress], headers={"Authorization": "Bearer ccp-test"}, json=body)
    assert response.status_code == 200, response.text
    assert len(bodies) == 1
    return bodies[0]["messages"][1:]  # inserted CodeBuddy identity is unrelated


@pytest.mark.parametrize("extra", [
    {"content": "正文"}, {"content": " "}, {"content": [{"type": "text", "text": "正文"}]},
    {"reasoning": "thinking"}, {"reasoning": "", "reasoning_content": "other"},
    {"reasoning_content": "thinking"}, {"reasoning_content": ""},
    {"reasoning": "A", "reasoning_content": "B", "extra_fields": {"signature": "signed", "nested": {"a": [1, 2]}}},
    {"extra_fields": {}}, {"extra_fields": {"signature": "signed"}},
    {"name": "agent"}, {"refusal": "no"}, {"unknown": None}, {"signature": "sig"},
])
async def test_chat_metadata_survives_full_ingress(pool, extra):
    messages = [{"role": "user", "content": "lookup"}, assistant("a"), assistant("b", **extra), assistant("c"), tool("c"), tool("a"), tool("b")]
    out = await outgoing(pool, "chat", {"messages": messages})
    assert out == messages


async def test_chat_duplicate_orphan_cross_turn_not_repaired(pool):
    messages = [{"role": "user", "content": "lookup"}, tool("orphan"), assistant("same"), assistant("same"), tool("same"), tool("same"), {"role": "user", "content": "next"}, assistant("same"), tool("same")]
    out = await outgoing(pool, "chat", {"messages": messages})
    assert out[0:2] == messages[0:2]
    assert out[2] == {**messages[2], "tool_calls": messages[2]["tool_calls"] + messages[3]["tool_calls"]}
    assert out[3:] == messages[4:]


@pytest.mark.parametrize("kind", ["tagged", "untagged", "redacted", "untagged_barrier"])
async def test_anthropic_thinking_keeps_call_boundaries(pool, kind):
    value = {"reasoning": "do not move me", "extra_fields": {"signature": "sig"}}
    thinking = replay.thinking_block(value) if kind == "tagged" else {"type": "thinking", "thinking": "do not drop me", "signature": "native-signature"}
    if kind == "redacted":
        thinking = {"type": "redacted_thinking", "data": "native-redacted"}
    assistants = [{"role": "assistant", "content": [tool_use("a")]}, {"role": "assistant", "content": [thinking, tool_use("b")]}, {"role": "assistant", "content": [tool_use("c")]}]
    if kind == "untagged_barrier":
        assistants = [{"role": "assistant", "content": [tool_use("a")]}, {"role": "assistant", "content": [thinking]}, {"role": "assistant", "content": [tool_use("b")]}]
    messages = [{"role": "user", "content": "lookup"}, *assistants, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "result:" + cid} for cid in ("a", "b", "c") if kind != "untagged_barrier" or cid != "c"]}]
    out = await outgoing(pool, "anthropic", {"messages": messages})
    calls = [m for m in out if m.get("tool_calls")]
    assert len(calls) == (2 if kind == "untagged_barrier" else 3)
    if kind == "tagged":
        assert replay.fields(calls[1]) == value
    else:
        assert all(not replay.fields(m) for m in calls)
        assert "native-signature" not in json.dumps(out)
        assert "native-redacted" not in json.dumps(out)


async def test_responses_tagged_boundaries_full_ingress(pool):
    items = [{"role": "user", "content": "lookup"}]
    values = [{"reasoning": "A", "extra_fields": {"signature": "sa"}}, {"reasoning_content": "B", "extra_fields": {"signature": "sb"}}]
    for cid, value in zip(("a", "b"), values):
        items += [call_item(cid), replay.response_item(value)]
    items += [{"type": "function_call_output", "call_id": cid, "output": "result:" + cid} for cid in ("b", "a")]
    out = await outgoing(pool, "responses", {"input": items})
    calls = [m for m in out if m.get("tool_calls")]
    assert [replay.fields(m) for m in calls] == values
    assert [[c["id"] for c in m["tool_calls"]] for m in calls] == [["a"], ["b"]]
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["b", "a"]


async def test_responses_carrier_after_text_does_not_move_to_earlier_calls(pool):
    value = {"reasoning": "belongs with B", "extra_fields": {"signature": "sb"}}
    items = [{"role": "user", "content": "lookup"}, call_item("a"), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "now B"}]}, call_item("b"), replay.response_item(value),
        *[{"type": "function_call_output", "call_id": cid, "output": "result:" + cid} for cid in ("b", "a")]]
    out = await outgoing(pool, "responses", {"input": items})
    calls = [m for m in out if m.get("tool_calls")]
    assert len(calls) == 2
    assert replay.fields(calls[1]) == value
    assert replay.fields(calls[0]) == {"reasoning_content": ""}


async def test_anthropic_business_encrypted_content_argument_preserved(pool):
    block = tool_use("a")
    block["input"]["encrypted_content"] = "business-value-not-replay"
    messages = [{"role": "user", "content": "lookup"}, {"role": "assistant", "content": [block]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "result:a"}]}]
    out = await outgoing(pool, "anthropic", {"messages": messages})
    args = json.loads(next(m for m in out if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"])
    assert args == block["input"]


async def test_chat_extra_fields_encrypted_content_preserved(pool):
    messages = [{"role": "user", "content": "lookup"}, assistant("a", reasoning="r", extra_fields={"signature": "sig", "encrypted_content": "business-value-not-replay"}), tool("a")]
    out = await outgoing(pool, "chat", {"messages": messages})
    assert out == messages


async def test_responses_explicit_text_message_metadata_must_stay_with_text(pool, monkeypatch):
    from src.channel import workbuddy_oauth_channel as wb
    before_merge = []
    original = wb.merge_tool_call_messages
    def observe(payload):
        before_merge.append(copy.deepcopy(payload["messages"]))
        original(payload)
    monkeypatch.setattr(wb, "merge_tool_call_messages", observe)
    value = {"reasoning": "belongs to text B", "extra_fields": {"signature": "sb"}}
    items = [{"role": "user", "content": "lookup"}, call_item("a"),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "text B"}], "providerData": value}, call_item("b"),
        *[{"type": "function_call_output", "call_id": cid, "output": "result:" + cid} for cid in ("b", "a")]]
    out = await outgoing(pool, "responses", {"input": items})
    assert out == before_merge[0]  # proves the final merger does not cause this move
    with_text = next(m for m in out if m.get("content") == "text B")
    assert replay.fields(with_text) == value
    assert [m["tool_calls"][0]["id"] for m in out if m.get("tool_calls")] == ["a", "b"]
    assert replay.fields(next(m for m in out if m.get("tool_calls"))) == {"reasoning_content": ""}


@pytest.mark.parametrize("text", ["正文B", " "])
async def test_anthropic_text_boundary_full_ingress(pool, text):
    messages = [{"role": "user", "content": "lookup"},
        {"role": "assistant", "content": [tool_use("a")]},
        {"role": "assistant", "content": [{"type": "text", "text": text}, tool_use("b")]},
        {"role": "assistant", "content": [tool_use("c")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": "result:" + cid} for cid in ("c", "b", "a")]}]
    out = await outgoing(pool, "anthropic", {"messages": messages})
    calls = [m for m in out if m.get("tool_calls")]
    assert len(calls) == 3 and calls[1]["content"] == text
    assert [[c["id"] for c in m["tool_calls"]] for m in calls] == [["a"], ["b"], ["c"]]
    assert all(json.loads(m["tool_calls"][0]["function"]["arguments"]) == {"城市": "昆明", "n": 1} for m in calls)
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["c", "b", "a"]


async def test_responses_native_cleanup_preserves_business_fields_on_final_wire(pool):
    data = {"type": "reasoning", "encrypted_content": "business-value",
            "content": [{"type": "encrypted_content", "data": "business-block"}]}
    first = {"reasoning": "A", "extra_fields": {"signature": "sa", **data}}
    second = {"reasoning": "B", "extra_fields": {"signature": "sb", **data}}
    args = json.dumps(data, ensure_ascii=False, indent=2)
    items = [{"role": "user", "content": "lookup"},
        {"type": "reasoning", "encrypted_content": "native-ciphertext", "summary": []},
        call_item("a", arguments=args, providerData=first),
        {"type": "message", "role": "assistant", "content": "正文B", "providerData": second},
        call_item("b", arguments=args), replay.response_item(second),
        *[{"type": "function_call_output", "call_id": cid, "output": args} for cid in ("b", "a")]]
    schema = {"type": "object", "properties": {"encrypted_content": {"type": "string"}, "payload": {"const": data}}}
    out = await outgoing(pool, "responses", {"input": items, "tools": [{"type": "function", "name": "lookup", "parameters": schema}]})
    calls = [m for m in out if m.get("tool_calls")]
    assert [replay.fields(m) for m in calls] == [first, second]
    assert calls[1]["content"] == "正文B"
    assert [m["tool_calls"][0]["function"]["arguments"] for m in calls] == [args, args]
    assert [(m["tool_call_id"], m["content"]) for m in out if m["role"] == "tool"] == [("b", args), ("a", args)]
    wire = wire_json(pool.requests[-1].content)
    assert wire["tools"][0]["function"]["parameters"] == schema
    assert "native-ciphertext" not in json.dumps(wire)


async def test_anthropic_plain_batch_still_merges_beside_replayable_thinking(pool):
    fields = {"reasoning": "prior turn", "extra_fields": {"signature": "sig"}}
    messages = [{"role": "user", "content": "lookup"},
        {"role": "assistant", "content": [replay.thinking_block(fields), {"type": "text", "text": "prior answer"}]},
        {"role": "user", "content": "next"},
        *[{"role": "assistant", "content": [tool_use(cid)]} for cid in ("a", "b")],
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": cid} for cid in ("b", "a")]}]
    out = await outgoing(pool, "anthropic", {"messages": messages})
    assert replay.fields(out[1]) == fields and out[1]["content"] == "prior answer"
    calls = [m for m in out if m.get("tool_calls")]
    assert len(calls) == 1 and [c["id"] for c in calls[0]["tool_calls"]] == ["a", "b"]
