"""Regression tests for preserving opaque Responses item identities."""

from __future__ import annotations

from copy import deepcopy

import src.openai.transform.codex_oauth_transform as transform


def _apply(input_items: list[dict], *, tools: bool = True) -> dict:
    body: dict = {"model": "gpt-5.5", "input": input_items}
    if tools:
        body["tools"] = [{"type": "function", "name": "lookup"}]
    return transform.apply_codex_oauth_transform(body, resolved_model="gpt-5.5")


def test_preserves_existing_item_ids_and_forward_and_backward_references():
    legacy_id = "legacy-message-identity"
    long_id = "server-item-" + ("x" * 160)
    function_id = "caller-supplied-function-identity"
    out = _apply([
        # Forward reference: the target appears later in the input.
        {"type": "item_reference", "id": legacy_id},
        {"type": "message", "role": "assistant", "id": legacy_id, "content": "prior"},
        {"type": "message", "role": "assistant", "id": long_id, "content": "long"},
        # Backward reference: the target has already appeared.
        {"type": "item_reference", "id": long_id},
        {"type": "function_call", "id": function_id, "call_id": "fcOpaqueToken",
         "name": "lookup", "arguments": "{}"},
        {"type": "item_reference", "id": function_id},
        {"type": "function_call_output", "id": "caller-output-identity",
         "call_id": "fcOpaqueToken", "output": "ok"},
    ])
    items = out["input"]

    messages = [item for item in items if item.get("type") == "message"]
    assert [item["id"] for item in messages] == [legacy_id, long_id]
    assert len(messages[1]["id"]) > 64

    function_call = next(item for item in items if item.get("type") == "function_call")
    function_output = next(item for item in items if item.get("type") == "function_call_output")
    assert function_call["id"] == function_id
    assert function_output["id"] == "caller-output-identity"
    assert function_call["call_id"] == function_output["call_id"] == "fcOpaqueToken"

    references = [item["id"] for item in items if item.get("type") == "item_reference"]
    assert references == [legacy_id, long_id, function_id]

    once = deepcopy(out)
    transform.apply_codex_oauth_transform(out, resolved_model="gpt-5.5")
    assert out == once


def test_missing_item_ids_are_not_generated():
    out = _apply([
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "fcOpaqueToken",
         "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "fcOpaqueToken", "output": "ok"},
    ])
    assert all("id" not in item for item in out["input"])


def test_non_continuation_still_drops_message_id():
    out = _apply([
        {"type": "message", "role": "user", "id": "existing-message", "content": "hi"},
    ], tools=False)
    assert "id" not in out["input"][0]


def test_filter_without_preserved_references_drops_all_existing_item_ids():
    filtered = transform._filter_codex_input([
        {"type": "message", "id": "message-id", "content": "hi"},
        {"type": "function_call", "id": "function-id", "call_id": "fcOpaqueToken",
         "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "id": "output-id", "call_id": "fcOpaqueToken",
         "output": "ok"},
        {"type": "item_reference", "id": "message-id"},
    ], preserve_references=False)
    assert [item.get("type") for item in filtered] == [
        "message", "function_call", "function_call_output",
    ]
    assert all("id" not in item for item in filtered)
    assert filtered[1]["call_id"] == filtered[2]["call_id"] == "fcOpaqueToken"


def test_encrypted_reasoning_remains_self_contained_without_item_id():
    encrypted = "opaque-encrypted-reasoning"
    for tools in (False, True):
        out = _apply([
            {"type": "reasoning", "id": "existing-reasoning", "summary": [],
             "encrypted_content": encrypted},
            {"type": "message", "role": "user", "content": "continue"},
        ], tools=tools)
        reasoning = next(item for item in out["input"] if item.get("type") == "reasoning")
        assert reasoning["encrypted_content"] == encrypted
        assert "id" not in reasoning
