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


def test_callless_named_function_call_output_is_not_coerced_to_call_id():
    """Codex Desktop injects standalone context items such as codex_apps_open_page
    and codex_apps_selected_page_part as function_call_output with an fco_* id
    and name, without a preceding function_call. Copying id into call_id causes
    upstream Responses to fail with 400 No tool call found.
    """
    out = _apply([
        {
            "type": "function_call_output",
            "id": "fco_01a1ba42-a1ab-76a0-8239-e99c9ed8d808",
            "name": "codex_apps_open_page",
            "output": "{}",
        },
        {
            "type": "function_call_output",
            "id": "fco_01a1ba42-a1ab-76a0-8239-e99c9ed8d809",
            "call_id": "fco_01a1ba42-a1ab-76a0-8239-e99c9ed8d809",
            "name": "codex_apps_selected_page_part",
            "output": "{}",
        },
        {"type": "message", "role": "user", "content": "hi"},
    ])
    outputs = [i for i in out["input"] if i.get("type") == "function_call_output"]
    assert len(outputs) == 2
    assert "call_id" not in outputs[0]
    assert outputs[0]["name"] == "codex_apps_open_page"
    assert "call_id" not in outputs[1]
    assert outputs[1]["name"] == "codex_apps_selected_page_part"


def test_callless_fco_converted_to_user_context_across_chat_anthropic_and_gemini():
    from src.openai.transform import responses_to_anthropic, responses_to_chat
    from src.providers import antigravity_codec

    body = {
        "model": "gpt-5.5",
        "input": [
            {
                "type": "function_call_output",
                "id": "fco_01a1ba42-a1ab-76a0-8239-e99c9ed8d808",
                "name": "codex_apps_open_page",
                "output": "page: index.ts",
            },
            {"type": "message", "role": "user", "content": "explain"},
        ],
    }
    chat_out = responses_to_chat.translate_request(deepcopy(body))
    assert [m["role"] for m in chat_out["messages"]] == ["user", "user"]
    assert "[codex_apps_open_page]" in chat_out["messages"][0]["content"]

    anth_out = responses_to_anthropic.translate_request(deepcopy(body))
    assert all(m["role"] == "user" for m in anth_out["messages"])
    assert not any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for m in anth_out["messages"]
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
    )

    gem_out = antigravity_codec.responses_to_gemini(deepcopy(body))
    assert not any(
        "functionResponse" in p
        for c in gem_out.get("contents", [])
        for p in c.get("parts", [])
    )
    assert any(
        "[codex_apps_open_page]" in str(p.get("text", ""))
        for c in gem_out.get("contents", [])
        for p in c.get("parts", [])
    )

