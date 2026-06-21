"""Usage normalization tests for Protocol Runtime Phase 3."""

from __future__ import annotations

from src.protocols.usage import (
    Usage,
    UsageAccumulator,
    legacy_usage_from_anthropic_json,
    legacy_usage_from_openai_chat_json,
    legacy_usage_from_openai_responses_json,
    zero_legacy_usage,
)
from src.upstream import ChatSSEUsageTracker, ResponsesSSEUsageTracker


def test_usage_to_legacy_dict_keeps_existing_log_db_shape():
    usage = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_write_tokens=3,
        cache_read_tokens=7,
        reasoning_tokens=99,
        raw={"x": 1},
    )

    assert usage.to_legacy_dict() == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation": 3,
        "cache_read": 7,
    }


def test_zero_legacy_usage_matches_historical_four_key_shape():
    assert zero_legacy_usage() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }


def test_anthropic_json_usage_maps_cache_creation_to_cache_write():
    out = legacy_usage_from_anthropic_json({
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
        }
    })

    assert out == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_creation": 3,
        "cache_read": 4,
    }


def test_openai_chat_usage_subtracts_cached_prompt_tokens():
    out = legacy_usage_from_openai_chat_json({
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
    })

    assert out == {
        "input_tokens": 20,
        "output_tokens": 5,
        "cache_creation": 0,
        "cache_read": 80,
    }


def test_openai_chat_accumulator_preserves_reasoning_tokens_in_ir():
    acc = UsageAccumulator()
    acc.set_from_openai_chat_usage({
        "prompt_tokens": 100,
        "completion_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 7},
    })

    assert acc.usage.input_tokens == 20
    assert acc.usage.output_tokens == 12
    assert acc.usage.cache_read_tokens == 80
    assert acc.usage.reasoning_tokens == 7
    assert acc.legacy_dict() == {
        "input_tokens": 20,
        "output_tokens": 12,
        "cache_creation": 0,
        "cache_read": 80,
    }


def test_openai_responses_usage_subtracts_cached_input_tokens():
    out = legacy_usage_from_openai_responses_json({
        "usage": {
            "input_tokens": 100,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 80},
        }
    })

    assert out == {
        "input_tokens": 20,
        "output_tokens": 5,
        "cache_creation": 0,
        "cache_read": 80,
    }


def test_openai_responses_accumulator_preserves_reasoning_tokens_in_ir():
    acc = UsageAccumulator()
    acc.set_from_openai_responses_usage({
        "input_tokens": 100,
        "output_tokens": 12,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 7},
    })

    assert acc.usage.input_tokens == 20
    assert acc.usage.output_tokens == 12
    assert acc.usage.cache_read_tokens == 80
    assert acc.usage.reasoning_tokens == 7
    assert acc.legacy_dict() == {
        "input_tokens": 20,
        "output_tokens": 12,
        "cache_creation": 0,
        "cache_read": 80,
    }


def test_chat_stream_terminal_usage_preserves_reasoning_tokens_in_ir():
    tracker = ChatSSEUsageTracker()
    tracker.feed(
        b'data: {"id":"c","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":100,"completion_tokens":12,'
        b'"prompt_tokens_details":{"cached_tokens":80},'
        b'"completion_tokens_details":{"reasoning_tokens":7}}}\n\n'
    )

    assert tracker.saw_stream_end is True
    assert tracker.usage == {
        "input_tokens": 20,
        "output_tokens": 12,
        "cache_creation": 0,
        "cache_read": 80,
    }
    assert tracker._usage_acc.usage.reasoning_tokens == 7


def test_responses_stream_terminal_usage_preserves_reasoning_tokens_in_ir():
    tracker = ResponsesSSEUsageTracker()
    tracker.feed(
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"id":"r",'
        b'"usage":{"input_tokens":100,"output_tokens":12,'
        b'"input_tokens_details":{"cached_tokens":80},'
        b'"output_tokens_details":{"reasoning_tokens":7}}}}\n\n'
    )

    assert tracker.saw_stream_end is True
    assert tracker.usage == {
        "input_tokens": 20,
        "output_tokens": 12,
        "cache_creation": 0,
        "cache_read": 80,
    }
    assert tracker._usage_acc.usage.reasoning_tokens == 7


def test_accumulator_preserves_anthropic_stream_delta_behaviour():
    acc = UsageAccumulator()
    acc.update_from_anthropic_message_start({
        "input_tokens": 0,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 4,
    })
    acc.update_from_anthropic_message_delta({
        "input_tokens": 9,
        "output_tokens": 2,
        "cache_read_input_tokens": 6,
    })
    acc.update_from_anthropic_message_delta({
        "cache_read_input_tokens": 5,
    })

    assert acc.legacy_dict() == {
        "input_tokens": 9,
        "output_tokens": 2,
        "cache_creation": 3,
        "cache_read": 6,
    }


def test_accumulator_zero_filled_delta_does_not_erase_start_usage():
    acc = UsageAccumulator()
    acc.update_from_anthropic_message_start({
        "input_tokens": 5,
        "cache_creation_input_tokens": 1,
        "cache_read_input_tokens": 2,
        "output_tokens": 0,
    })
    acc.update_from_anthropic_message_delta({
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 3,
    })

    assert acc.legacy_dict() == {
        "input_tokens": 5,
        "output_tokens": 3,
        "cache_creation": 1,
        "cache_read": 2,
    }
