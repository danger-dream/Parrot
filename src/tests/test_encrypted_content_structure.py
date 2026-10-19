"""Ciphertext cleanup only owns Responses protocol slots, never business JSON."""
from __future__ import annotations

import copy
import json

import pytest

from src.openai import anthropic_reasoning_replay
from src.protocols.runtime import retry_body_without_encrypted_content
from src.providers import workbuddy_replay


def business():
    # Deliberately indistinguishable from a reasoning item without its path.
    return {"type": "reasoning", "encrypted_content": "business-secret",
            "content": [{"type": "encrypted_content", "data": "business-block"}],
            "input": [{"type": "reasoning", "encrypted_content": "nested-business"}]}


@pytest.mark.parametrize("field", ["input", "instructions"])
@pytest.mark.parametrize("preserve", [False, True])
def test_cleanup_is_structural_and_preserves_provider_carriers(field, preserve):
    native = {"type": "reasoning", "id": "rs-native", "encrypted_content": "native-cipher",
              "summary": [{"type": "summary_text", "text": "portable summary"}],
              "content": [{"type": "reasoning_text", "text": "portable thought"}],
              "extra_fields": business(), "providerData": business()}
    tagged = workbuddy_replay.response_item({"reasoning": "wb thought", "extra_fields": business()})
    # These carriers are indivisible, including provider extension values.
    tagged["content"] = [{"type": "encrypted_content", "data": "tagged extension"}]
    anthropic = [{"type": "reasoning", "summary": [], "encrypted_content": anthropic_reasoning_replay.encode(block),
                  "extra_fields": business()} for block in (
        {"type": "thinking", "thinking": "thought", "signature": "sig"},
        {"type": "redacted_thinking", "data": "redacted"},
    )]
    items = [native, tagged, *anthropic,
             {"type": "function_call", "call_id": "a", "name": "f", "arguments": json.dumps(business()),
              "extra_fields": business(), "providerData": business()},
             {"type": "custom_tool_call", "call_id": "b", "name": "c", "input": json.dumps(business())},
             {"type": "function_call_output", "call_id": "a", "output": json.dumps(business())},
             {"type": "encrypted_content", "data": "native-block"}]
    body = {"model": "fixture", field: items, "metadata": business(), "encrypted_content": "business-top",
            "tools": [{"type": "function", "name": "f", "parameters": {"type": "object", "properties": {
                "encrypted_content": {"type": "string"}, "payload": {"const": business()}}}}],
            "include": ["reasoning.encrypted_content"]}
    original = copy.deepcopy(body)
    clean, count = retry_body_without_encrypted_content(body, preserve_anthropic=preserve)
    assert count == (2 if preserve else 4)
    expected = copy.deepcopy(body)
    expected[field].pop()
    expected[field][0].pop("encrypted_content")
    if not preserve:
        for item in expected[field][2:4]:
            item.pop("encrypted_content")
    assert clean == expected
    assert body == original
    clean[field][1]["summary"].clear()
    assert body == original  # includes preserved carrier deep-copy isolation


@pytest.mark.parametrize("item", [
    {"type": "reasoning", "content": []},
    {"type": "message", "role": "assistant", "content": []},
    {"role": "user", "content": []},
    {"type": "function_call_output", "call_id": "a", "output": []},
    {"type": "custom_tool_call_output", "call_id": "a", "output": []},
])
def test_cleanup_removes_only_typed_blocks_in_protocol_content_slots(item):
    key = "output" if "output" in item else "content"
    value = copy.deepcopy(item)
    kept = {"type": "input_text", "text": json.dumps(business()), "extra_fields": business()}
    value[key] = [{"type": "encrypted_content", "encrypted_content": "cipher"}, kept]
    value["extra_fields"] = business()
    body = {"input": [value]}
    original = copy.deepcopy(body)
    clean, count = retry_body_without_encrypted_content(body)
    assert count == 1
    assert clean == {"input": [{**value, key: [kept]}]}
    assert body == original


@pytest.mark.parametrize("protocol", ["chat", "anthropic", "responses"])
def test_business_names_alone_do_not_trigger_cleanup(protocol):
    payload = business()
    if protocol == "chat":
        history = {"messages": [{"role": "assistant", "content": None, "extra_fields": payload,
            "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f", "arguments": json.dumps(payload)}}]},
            {"role": "tool", "tool_call_id": "a", "content": json.dumps(payload)}]}
    elif protocol == "anthropic":
        history = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "f", "input": payload}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": json.dumps(payload)}]}]}
    else:
        history = {"input": [{"type": "function_call", "call_id": "a", "name": "f", "arguments": json.dumps(payload),
                              "providerData": payload, "extra_fields": payload},
                             {"type": "function_call_output", "call_id": "a", "output": json.dumps(payload)}]}
    body = {**history, "metadata": payload, "tools": [{"parameters": {"properties": {"encrypted_content": payload}}}],
            "text": {"format": {"schema": payload}}, "encrypted_content": "ordinary top-level key"}
    original = copy.deepcopy(body)
    clean, count = retry_body_without_encrypted_content(body)
    assert count == 0
    assert clean == original == body
    assert clean is not body
