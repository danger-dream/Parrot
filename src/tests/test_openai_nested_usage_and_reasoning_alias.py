from __future__ import annotations

import asyncio
import json

import pytest

from src import model_pricing
from src.openai.transform.common import normalize_chat_reasoning_alias
from src.protocols.usage import (
    select_openai_chat_usage,
    select_openai_responses_usage,
)
from src.upstream import (
    ChatSSEUsageTracker,
    ResponsesSSEUsageTracker,
    extract_usage_chat_json,
    extract_usage_responses_json,
)


def _chat(prompt: int, output: int, cached: int = 0) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def _responses(prompt: int, output: int, cached: int = 0) -> dict:
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "input_tokens_details": {"cached_tokens": cached},
    }


def _envelopes(usages: list[dict]) -> dict:
    return {
        "usage": usages[0],
        "response": {"usage": usages[1]},
        "data": {"usage": usages[2], "response": {"usage": usages[3]}},
    }


@pytest.mark.parametrize("index", range(4))
def test_each_openai_usage_envelope_is_supported(index: int):
    containers = [
        lambda u: {"usage": u},
        lambda u: {"response": {"usage": u}},
        lambda u: {"data": {"usage": u}},
        lambda u: {"data": {"response": {"usage": u}}},
    ]
    obj = containers[index](_chat(13, 7, 3))
    assert extract_usage_chat_json(obj) == {
        "input_tokens": 10, "output_tokens": 7, "cache_creation": 0, "cache_read": 3,
    }
    billed = model_pricing.normalize_response_billing(obj)
    assert billed.usage_observed and not billed.usage_invalid
    assert (billed.input_tokens, billed.output_tokens, billed.cache_read_tokens) == (10, 7, 3)


def test_whole_candidate_precedence_zero_and_no_cross_envelope_fill():
    obj = _envelopes([
        _chat(0, 0, 0), _chat(20, 2, 5), _chat(30, 3), _chat(40, 4),
    ])
    selected = select_openai_chat_usage(obj)
    assert selected.usage_observed and selected.legacy_dict() == {
        "input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0,
    }
    billed = model_pricing.normalize_response_billing(obj)
    assert billed.usage_observed and (billed.input_tokens, billed.output_tokens) == (0, 0)


def test_malformed_higher_falls_through_but_all_malformed_is_invalid():
    recovered = {
        "usage": _chat(2, 1, 3),
        "response": {"usage": _chat(9, 4, 2)},
    }
    immediate = select_openai_chat_usage(recovered)
    billed = model_pricing.normalize_response_billing(recovered)
    assert immediate.usage_observed and not immediate.usage_invalid
    assert immediate.legacy_dict()["input_tokens"] == 7
    assert billed.usage_observed and not billed.usage_invalid and billed.input_tokens == 7

    malformed = {"usage": [], "response": {"usage": {"prompt_tokens": "bad"}}}
    assert select_openai_chat_usage(malformed).usage_invalid
    strict = model_pricing.normalize_response_billing(malformed)
    assert strict.usage_invalid and not strict.usage_observed


def test_service_tier_is_selected_independently_from_usage():
    obj = {
        "usage": _chat(8, 2),
        "response": {"service_tier": " Priority ", "usage": _chat(99, 99)},
        "data": {"service_tier": "flex"},
    }
    billed = model_pricing.normalize_response_billing(obj)
    assert billed.service_tier == "priority"
    assert (billed.input_tokens, billed.output_tokens) == (8, 2)


def test_responses_nonstream_and_sse_use_nested_precedence_and_cache():
    obj = {"data": {"response": {"usage": _responses(18, 6, 5)}}}
    assert extract_usage_responses_json(obj) == {
        "input_tokens": 13, "output_tokens": 6, "cache_creation": 0, "cache_read": 5,
    }
    assert select_openai_responses_usage(obj).usage_observed

    responses = ResponsesSSEUsageTracker()
    payload = {
        "type": "response.completed",
        "usage": _responses(10, 1, 2),
        "response": {"usage": _responses(80, 8, 20)},
    }
    responses.feed(
        b"event: response.completed\ndata: " + json.dumps(payload).encode() + b"\n\n"
    )
    assert responses.usage == {
        "input_tokens": 8, "output_tokens": 1, "cache_creation": 0, "cache_read": 2,
    }

    chat = ChatSSEUsageTracker()
    evt = {"response": {"usage": _chat(12, 3, 2)}, "choices": []}
    chat.feed(b"data: " + json.dumps(evt).encode() + b"\n\n")
    assert chat.usage_observed and chat.usage["input_tokens"] == 10


def test_later_terminal_event_replaces_earlier_event_usage():
    tracker = ChatSSEUsageTracker()
    tracker.feed(b"data: " + json.dumps({"usage": _chat(5, 1), "choices": []}).encode() + b"\n\n")
    tracker.feed(b"data: " + json.dumps({"usage": _chat(9, 4), "choices": []}).encode() + b"\n\n")
    assert tracker.usage["input_tokens"] == 9
    assert tracker.usage["output_tokens"] == 4


def test_reasoning_alias_helper_precedence_types_and_roles():
    body = {"messages": [
        {"role": "assistant", "reasoning_content": "", "reasoning": "alias"},
        {"role": "assistant", "reasoning_content": 7, "reasoning": "kept exact  "},
        {"role": "assistant", "reasoning": 42},
        {"role": "user", "reasoning": "user alias"},
        "not-a-message",
    ]}
    normalize_chat_reasoning_alias(body)
    first, second, third, user, _ = body["messages"]
    assert first["reasoning_content"] == "" and "reasoning" not in first
    assert second["reasoning_content"] == "kept exact  " and "reasoning" not in second
    assert second["reasoning_content"] != 7
    assert third == {"role": "assistant"}
    assert user["reasoning"] == "user alias" and "reasoning_content" not in user


def test_chat_handler_normalizes_before_guard(monkeypatch):
    from src.openai import handler

    captured = {}

    class StopAtGuard(Exception):
        pass

    class Request:
        headers = {}
        client = type("Client", (), {"host": "127.0.0.1"})()

        async def body(self):
            return json.dumps({
                "model": "gpt-test",
                "messages": [{"role": "assistant", "reasoning": "raw alias"}],
            }).encode()

    monkeypatch.setattr(handler.auth, "validate", lambda _headers: ("test", [], None))

    def guard(body):
        captured.update(body)
        raise StopAtGuard

    monkeypatch.setattr(handler, "guard_chat_ingress", guard)
    with pytest.raises(StopAtGuard):
        asyncio.run(handler.handle(Request(), ingress_protocol="chat"))
    assert captured["messages"][0]["reasoning_content"] == "raw alias"
    assert "reasoning" not in captured["messages"][0]


def test_responses_ingress_shape_is_not_rewritten_by_chat_helper():
    body = {
        "reasoning": {"effort": "high"},
        "input": [{"role": "assistant", "reasoning": "encrypted-or-native"}],
    }
    before = json.loads(json.dumps(body))
    normalize_chat_reasoning_alias(body)
    assert body == before
