"""Protocol Runtime Phase 1 registry tests.

The registry is intentionally only a shell around the legacy failover toolkit in
this phase.  These tests pin the zero-behaviour-change contract: each protocol
still points at the same upstream parser/tracker/builder/usage helpers.
"""

from __future__ import annotations

import pytest

from src import upstream
from src.protocols import registry


EXPECTED_HELPERS = {
    "anthropic": {
        "stream_tracker": upstream.SSEUsageTracker,
        "stream_builder": upstream.SSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_sse_event,
        "extract_usage_json": upstream.extract_usage_from_json,
    },
    "openai-chat": {
        "stream_tracker": upstream.ChatSSEUsageTracker,
        "stream_builder": upstream.ChatSSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_chat_sse_event,
        "extract_usage_json": upstream.extract_usage_chat_json,
    },
    "openai-responses": {
        "stream_tracker": upstream.ResponsesSSEUsageTracker,
        "stream_builder": upstream.ResponsesSSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_responses_sse_event,
        "extract_usage_json": upstream.extract_usage_responses_json,
    },
}


def test_registered_protocols_are_the_existing_three_upstream_protocols():
    assert registry.registered_protocols() == (
        "anthropic",
        "openai-chat",
        "openai-responses",
    )


@pytest.mark.parametrize("protocol", EXPECTED_HELPERS)
def test_toolkit_points_at_existing_upstream_helpers(protocol: str):
    toolkit = registry.get_toolkit(protocol)
    assert toolkit.name == protocol

    legacy = toolkit.as_legacy_dict()
    expected = EXPECTED_HELPERS[protocol]
    for key, helper in expected.items():
        assert legacy[key] is helper

    assert set(legacy) == {
        "stream_tracker",
        "stream_builder",
        "first_event_parser",
        "extract_usage_json",
        "is_upstream_error_json",
    }


def test_error_detectors_keep_legacy_protocol_semantics():
    anthropic = registry.get_toolkit("anthropic")
    openai_chat = registry.get_toolkit("openai-chat")
    openai_responses = registry.get_toolkit("openai-responses")

    assert anthropic.is_upstream_error_json({"type": "error", "error": {"message": "x"}})
    assert anthropic.is_upstream_error_json({"error": {"message": "x"}})
    assert not anthropic.is_upstream_error_json({"type": "message", "content": []})

    for toolkit in (openai_chat, openai_responses):
        assert toolkit.is_upstream_error_json({"error": {"message": "x"}})
        assert not toolkit.is_upstream_error_json({"choices": []})


def test_unknown_protocol_fails_loudly():
    with pytest.raises(KeyError):
        registry.get_toolkit("unknown-protocol")
