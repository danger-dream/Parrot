from __future__ import annotations

import asyncio
import copy
import json

import pytest

from src.openai import deepseek_reasoning
from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.transform.guard import GuardError
from src.upstream import ChatSSEAssistantBuilder


MODEL = "deepseek-v4-flash"
CALL_ID = "call_0123456789abcdef"
REASONING = "inspect the tool inputs, then call lookup"


def setup_function():
    deepseek_reasoning.clear()


def _anthropic_tool_result_body(*, thinking=None, forced=False):
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look it up"}]},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": CALL_ID, "name": "lookup", "input": {"q": "x"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": CALL_ID, "content": "result",
            }]},
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup"} if forced else {"type": "auto"},
        "max_tokens": 1024,
        "stream": True,
    }
    if thinking is not None:
        body["thinking"] = thinking
    return body


def _channel(protocol: str) -> OpenAIApiChannel:
    return OpenAIApiChannel({
        "name": "DeepSeek",
        "baseUrl": "https://api.deepseek.com",
        "apiKey": "sk-test",
        "protocol": protocol,
        "models": [{"alias": MODEL, "real": MODEL}],
    })


def test_chat_sse_builder_preserves_reasoning_content_for_terminal_cache():
    builder = ChatSSEAssistantBuilder()
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "inspect "}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "state"}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": CALL_ID, "type": "function",
            "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
        }]}, "finish_reason": "tool_calls"}]},
    ]
    for obj in chunks:
        builder.feed(b"data: " + json.dumps(obj).encode() + b"\n\n")

    assistant = builder.get_assistant()
    assert assistant["reasoning_content"] == "inspect state"
    assert assistant["tool_calls"][0]["id"] == CALL_ID
    assert deepseek_reasoning.cache_from_chat_assistant(assistant, model=MODEL) == 1
    assert deepseek_reasoning.has_replay_for_tool_call(CALL_ID, model=MODEL)


def test_chat_reasoning_replays_exactly_into_chat_and_responses():
    assistant = {
        "role": "assistant",
        "content": "calling lookup",
        "reasoning_content": REASONING,
        "tool_calls": [{
            "id": CALL_ID,
            "type": "function",
            "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
        }],
    }
    assert deepseek_reasoning.cache_from_chat_assistant(assistant, model=MODEL) == 1

    chat_payload = {"messages": [{
        "role": "assistant",
        "content": "calling lookup",
        "tool_calls": copy.deepcopy(assistant["tool_calls"]),
    }]}
    assert deepseek_reasoning.inject_into_chat_payload(chat_payload, model=MODEL) == 1
    assert chat_payload["messages"][0]["reasoning_content"] == REASONING

    responses_payload = {"input": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "calling lookup"}]},
        {"type": "function_call", "call_id": CALL_ID, "name": "lookup", "arguments": "{\"q\":\"x\"}"},
        {"type": "function_call_output", "call_id": CALL_ID, "output": "result"},
    ]}
    assert deepseek_reasoning.inject_into_responses_payload(responses_payload, model=MODEL) == 1
    reasoning_item = responses_payload["input"][1]
    assert reasoning_item["type"] == "reasoning"
    assert reasoning_item["content"] == [{"type": "reasoning_text", "text": REASONING}]
    assert responses_payload["input"][2]["call_id"] == CALL_ID

    # Full-history retries are idempotent and do not duplicate the item.
    assert deepseek_reasoning.inject_into_responses_payload(responses_payload, model=MODEL) == 0


def test_chat_reasoning_preserves_boundary_whitespace_exactly():
    exact_reasoning = "\n  inspect the exact boundary whitespace  \n"
    assistant = {
        "reasoning_content": exact_reasoning,
        "tool_calls": [{
            "id": CALL_ID,
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    }
    assert deepseek_reasoning.cache_from_chat_assistant(assistant, model=MODEL) == 1

    chat_payload = {"messages": [{
        "role": "assistant",
        "tool_calls": copy.deepcopy(assistant["tool_calls"]),
    }]}
    assert deepseek_reasoning.inject_into_chat_payload(chat_payload, model=MODEL) == 1
    assert chat_payload["messages"][0]["reasoning_content"] == exact_reasoning

    responses_payload = {"input": [{
        "type": "function_call", "call_id": CALL_ID,
        "name": "lookup", "arguments": "{}",
    }]}
    assert deepseek_reasoning.inject_into_responses_payload(responses_payload, model=MODEL) == 1
    assert responses_payload["input"][0]["content"] == [
        {"type": "reasoning_text", "text": exact_reasoning},
    ]


def test_replay_cache_capacity_uses_lru_eviction(monkeypatch):
    monkeypatch.setattr(deepseek_reasoning, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(deepseek_reasoning, "_EVICT_BATCH", 1)

    def cache(call_id: str) -> None:
        assert deepseek_reasoning.cache_from_chat_assistant({
            "reasoning_content": f"reasoning for {call_id}",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }, model=MODEL) == 1

    cache("call_a")
    cache("call_b")
    assert deepseek_reasoning.has_replay_for_tool_call("call_a", model=MODEL)
    cache("call_c")

    assert not deepseek_reasoning.has_replay_for_tool_call("call_b", model=MODEL)
    assert deepseek_reasoning.has_replay_for_tool_call("call_a", model=MODEL)
    assert deepseek_reasoning.has_replay_for_tool_call("call_c", model=MODEL)
    assert deepseek_reasoning._debug_size() == 2


def test_native_responses_reasoning_item_is_replayed_without_rewriting():
    native_item = {
        "type": "reasoning",
        "id": "rs_native",
        "status": "completed",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": REASONING}],
    }
    response = {
        "model": MODEL,
        "output": [
            native_item,
            {"type": "function_call", "id": "fc_1", "call_id": CALL_ID, "name": "lookup", "arguments": "{}"},
        ],
    }
    assert deepseek_reasoning.cache_from_responses_response(response, model=MODEL) == 1
    payload = {"input": [{"type": "function_call", "call_id": CALL_ID, "name": "lookup", "arguments": "{}"}]}
    assert deepseek_reasoning.inject_into_responses_payload(payload, model=MODEL) == 1
    assert payload["input"][0] == native_item


def test_known_chat_tool_call_without_reasoning_does_not_trigger_guard_or_placeholder():
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": CALL_ID,
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    }
    assert deepseek_reasoning.cache_from_chat_assistant(assistant, model=MODEL) == 1
    assert deepseek_reasoning.has_observed_tool_call_state(CALL_ID, model=MODEL)
    assert not deepseek_reasoning.has_replay_for_tool_call(CALL_ID, model=MODEL)

    body = _anthropic_tool_result_body()
    chat_payload = json.loads(asyncio.run(
        _channel("openai-chat").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    ).body)
    chat_assistant = next(
        message for message in chat_payload["messages"]
        if any(call.get("id") == CALL_ID for call in message.get("tool_calls") or [])
    )
    assert "reasoning_content" not in chat_assistant
    assert deepseek_reasoning.missing_chat_tool_call_ids(
        chat_payload, model=MODEL,
    ) == []

    responses_payload = json.loads(asyncio.run(
        _channel("openai-responses").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    ).body)
    assert not any(item.get("type") == "reasoning" for item in responses_payload["input"])
    assert deepseek_reasoning.missing_responses_tool_call_ids(
        responses_payload, model=MODEL,
    ) == []


def test_known_responses_tool_call_without_reasoning_continues_natively():
    response = {
        "model": MODEL,
        "status": "completed",
        "output": [{
            "type": "function_call",
            "id": "fc_without_reasoning",
            "call_id": CALL_ID,
            "name": "lookup",
            "arguments": "{}",
            "status": "completed",
        }],
    }
    assert deepseek_reasoning.cache_from_responses_response(response, model=MODEL) == 1
    assert deepseek_reasoning.has_observed_tool_call_state(CALL_ID, model=MODEL)
    assert not deepseek_reasoning.has_replay_for_tool_call(CALL_ID, model=MODEL)

    body = _anthropic_tool_result_body()
    payload = json.loads(asyncio.run(
        _channel("openai-responses").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    ).body)
    assert payload["reasoning"] == {"effort": "high"}
    assert not any(item.get("type") == "reasoning" for item in payload["input"])
    assert deepseek_reasoning.missing_responses_tool_call_ids(
        payload, model=MODEL,
    ) == []


def test_missing_replay_never_fabricates_reasoning():
    chat_payload = {"messages": [{
        "role": "assistant",
        "tool_calls": [{"id": "call_missing", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }]}
    responses_payload = {"input": [{"type": "function_call", "call_id": "call_missing", "name": "lookup", "arguments": "{}"}]}
    assert deepseek_reasoning.inject_into_chat_payload(chat_payload, model=MODEL) == 0
    assert "reasoning_content" not in chat_payload["messages"][0]
    assert deepseek_reasoning.missing_chat_tool_call_ids(chat_payload) == ["call_missing"]
    assert deepseek_reasoning.inject_into_responses_payload(responses_payload, model=MODEL) == 0
    assert responses_payload["input"] == [{"type": "function_call", "call_id": "call_missing", "name": "lookup", "arguments": "{}"}]


def test_anthropic_tool_subturn_keeps_thinking_and_replays_on_both_upstreams():
    deepseek_reasoning.cache_from_chat_assistant({
        "reasoning_content": REASONING,
        "tool_calls": [{"id": CALL_ID, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }, model=MODEL)
    body = _anthropic_tool_result_body()

    chat_req = asyncio.run(_channel("openai-chat").build_upstream_request(body, MODEL, ingress_protocol="anthropic"))
    chat_payload = json.loads(chat_req.body)
    assistant_messages = [m for m in chat_payload["messages"] if m.get("role") == "assistant"]
    assert chat_payload["thinking"] == {"type": "enabled"}
    assert assistant_messages[-1]["reasoning_content"] == REASONING

    responses_req = asyncio.run(_channel("openai-responses").build_upstream_request(body, MODEL, ingress_protocol="anthropic"))
    responses_payload = json.loads(responses_req.body)
    assert responses_payload["reasoning"] == {"effort": "high"}
    function_index = next(i for i, item in enumerate(responses_payload["input"]) if item.get("type") == "function_call" and item.get("call_id") == CALL_ID)
    assert responses_payload["input"][function_index - 1]["content"] == [
        {"type": "reasoning_text", "text": REASONING},
    ]


def test_only_explicit_disabled_turns_thinking_off_but_history_still_replays():
    deepseek_reasoning.cache_from_chat_assistant({
        "reasoning_content": REASONING,
        "tool_calls": [{"id": CALL_ID, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }, model=MODEL)
    body = _anthropic_tool_result_body(thinking={"type": "disabled"}, forced=True)

    chat_payload = json.loads(asyncio.run(
        _channel("openai-chat").build_upstream_request(body, MODEL, ingress_protocol="anthropic")
    ).body)
    assert chat_payload["thinking"] == {"type": "disabled"}
    assert next(m for m in chat_payload["messages"] if m.get("role") == "assistant")["reasoning_content"] == REASONING

    responses_payload = json.loads(asyncio.run(
        _channel("openai-responses").build_upstream_request(body, MODEL, ingress_protocol="anthropic")
    ).body)
    assert responses_payload["reasoning"] == {"effort": "none"}
    assert any(item.get("type") == "reasoning" for item in responses_payload["input"])


def test_missing_responses_replay_rejects_only_the_strict_candidate():
    body = _anthropic_tool_result_body()
    with pytest.raises(GuardError) as exc_info:
        asyncio.run(_channel("openai-responses").build_upstream_request(body, MODEL, ingress_protocol="anthropic"))
    assert "missing exact reasoning_text replay" in exc_info.value.message
    assert exc_info.value.scope == "candidate"


def test_missing_chat_replay_rejects_only_the_strict_candidate():
    body = _anthropic_tool_result_body()
    with pytest.raises(GuardError) as exc_info:
        asyncio.run(_channel("openai-chat").build_upstream_request(body, MODEL, ingress_protocol="anthropic"))
    assert "missing exact reasoning_content replay" in exc_info.value.message
    assert exc_info.value.scope == "candidate"


def test_explicit_disabled_allows_missing_chat_replay_without_placeholder():
    body = _anthropic_tool_result_body(thinking={"type": "disabled"}, forced=True)
    request = asyncio.run(
        _channel("openai-chat").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    )
    payload = json.loads(request.body)
    assistant = next(message for message in payload["messages"] if message.get("role") == "assistant")
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_content" not in assistant


def test_omitted_thinking_with_forced_tool_is_rejected_not_silently_disabled():
    deepseek_reasoning.cache_from_chat_assistant({
        "reasoning_content": REASONING,
        "tool_calls": [{"id": CALL_ID, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }, model=MODEL)
    body = _anthropic_tool_result_body(forced=True)
    for protocol in ("openai-chat", "openai-responses"):
        with pytest.raises(GuardError) as exc_info:
            asyncio.run(_channel(protocol).build_upstream_request(body, MODEL, ingress_protocol="anthropic"))
        assert "explicitly disable thinking" in exc_info.value.message


def test_fresh_responses_conversation_is_not_rejected_for_missing_replay():
    body = {
        "messages": [{"role": "user", "content": "look it up"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "max_tokens": 1024,
        "stream": True,
    }

    request = asyncio.run(
        _channel("openai-responses").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    )
    payload = json.loads(request.body)
    assert payload["reasoning"] == {"effort": "high"}
    assert not any(item.get("type") == "function_call" for item in payload["input"])
    assert deepseek_reasoning.missing_responses_tool_call_ids(payload, model=MODEL) == []


def test_fresh_chat_conversation_is_not_rejected_for_missing_replay():
    body = {
        "messages": [{"role": "user", "content": "look it up"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "max_tokens": 1024,
        "stream": True,
    }

    request = asyncio.run(
        _channel("openai-chat").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    )
    payload = json.loads(request.body)
    assert payload["thinking"] == {"type": "enabled"}
    assert deepseek_reasoning.missing_chat_tool_call_ids(payload) == []


def test_missing_replay_before_latest_ordinary_user_does_not_block_active_chain():
    old_call_id = "call_old_non_thinking"
    body = {
        "messages": [
            {"role": "user", "content": "old non-thinking lookup"},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": old_call_id, "name": "lookup", "input": {"q": "old"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": old_call_id, "content": "old result",
            }]},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new thinking lookup"},
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": CALL_ID, "name": "lookup", "input": {"q": "new"},
            }]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": CALL_ID, "content": "new result",
            }]},
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "max_tokens": 1024,
        "stream": True,
    }
    deepseek_reasoning.cache_from_chat_assistant({
        "reasoning_content": REASONING,
        "tool_calls": [{"id": CALL_ID, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
    }, model=MODEL)

    chat_request = asyncio.run(
        _channel("openai-chat").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    )
    chat_payload = json.loads(chat_request.body)
    assert chat_payload["thinking"] == {"type": "enabled"}
    assert deepseek_reasoning.missing_chat_tool_call_ids(chat_payload) == []
    old_chat_message = next(
        message for message in chat_payload["messages"]
        if any(call.get("id") == old_call_id for call in message.get("tool_calls") or [])
    )
    active_chat_message = next(
        message for message in chat_payload["messages"]
        if any(call.get("id") == CALL_ID for call in message.get("tool_calls") or [])
    )
    assert "reasoning_content" not in old_chat_message
    assert active_chat_message["reasoning_content"] == REASONING

    request = asyncio.run(
        _channel("openai-responses").build_upstream_request(
            body, MODEL, ingress_protocol="anthropic",
        )
    )
    payload = json.loads(request.body)
    assert payload["reasoning"] == {"effort": "high"}
    assert deepseek_reasoning.missing_responses_tool_call_ids(payload, model=MODEL) == []
    active_index = next(
        index for index, item in enumerate(payload["input"])
        if item.get("type") == "function_call" and item.get("call_id") == CALL_ID
    )
    assert payload["input"][active_index - 1]["content"] == [
        {"type": "reasoning_text", "text": REASONING},
    ]
    old_index = next(
        index for index, item in enumerate(payload["input"])
        if item.get("type") == "function_call" and item.get("call_id") == old_call_id
    )
    assert old_index == 1
