"""CommitGate unit tests for Protocol Runtime Phase 2."""

from __future__ import annotations

import json

from src.protocols.commit_gate import (
    SseCommitGate,
    chunks_have_downstream_visible_event,
    is_responses_visible_event_type,
    is_sse_downstream_visible_event,
)


def _event(name: str, data: dict) -> bytes:
    return (
        f"event: {name}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def test_responses_metadata_is_buffered_until_visible_event():
    created = _event("response.created", {"type": "response.created"})
    delta = _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})

    gate = SseCommitGate(protocol="openai-responses")

    first = gate.feed(created)
    assert first.downstream_chunks == []
    assert first.error_event is None

    second = gate.feed(delta)
    assert second.error_event is None
    assert second.downstream_chunks == [created, delta]


def test_responses_pre_visible_error_is_retryable_error_event():
    created = _event("response.created", {"type": "response.created"})
    err = _event("error", {"error": {"type": "rate_limit", "message": "boom"}})

    gate = SseCommitGate(protocol="openai-responses")
    result = gate.feed(created + err)

    assert result.downstream_chunks == []
    assert result.error_event is not None
    assert result.error_event["_event_name"] == "error"
    assert result.error_event["error"]["message"] == "boom"


def test_responses_error_after_visible_in_same_chunk_is_post_commit():
    delta = _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})
    err = _event("error", {"error": {"type": "server_error", "message": "late boom"}})

    gate = SseCommitGate(protocol="openai-responses")
    result = gate.feed(delta + err)

    assert result.error_event is None
    assert result.downstream_chunks == [delta, err]


def test_partial_trailing_bytes_are_forwarded_only_after_commit():
    delta = _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})
    partial = b"event: response.output_text.delta\ndata: {"

    gate = SseCommitGate(protocol="openai-responses")
    result = gate.feed(delta + partial)

    assert result.error_event is None
    assert result.downstream_chunks == [delta, partial]


class _ResponsesMetadataThenVisibleTranslator:
    def __init__(self) -> None:
        self.seen: list[bytes] = []

    def feed(self, chunk: bytes):
        self.seen.append(chunk)
        if b"message_start" in chunk:
            return [
                _event("response.created", {"type": "response.created"}),
                _event("response.in_progress", {"type": "response.in_progress"}),
            ]
        if b"content_block_delta" in chunk:
            return [_event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})]
        return []


def test_translator_responses_metadata_is_buffered_until_visible_output():
    start = _event("message_start", {"type": "message_start"})
    translator = _ResponsesMetadataThenVisibleTranslator()
    gate = SseCommitGate(protocol="openai-responses", stream_translator=translator)

    result = gate.feed(start)

    assert translator.seen == [start]
    assert result.error_event is None
    assert result.downstream_chunks == []

    visible = _event("content_block_delta", {"type": "content_block_delta"})
    result2 = gate.feed(visible)
    assert result2.error_event is None
    events = b"".join(result2.downstream_chunks)
    assert b"response.created" in events
    assert b"response.in_progress" in events
    assert b"response.output_text.delta" in events


def test_chunks_have_downstream_visible_event_respects_responses_metadata():
    created = [_event("response.created", {"type": "response.created"})]
    delta = [_event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})]
    assert not chunks_have_downstream_visible_event(created, "openai-responses")
    assert chunks_have_downstream_visible_event(delta, "openai-responses")


def test_visible_event_helpers_match_responses_boundary_rules():
    assert is_responses_visible_event_type("response.output_text.delta")
    assert not is_responses_visible_event_type("response.created")
    assert not is_responses_visible_event_type("")
    assert not is_responses_visible_event_type(None)

    assert is_sse_downstream_visible_event(
        "response.output_text.delta",
        {"type": "response.output_text.delta"},
        "openai-responses",
    )
    assert not is_sse_downstream_visible_event(
        "response.created",
        {"type": "response.created"},
        "openai-responses",
    )
    assert is_sse_downstream_visible_event(
        None,
        {"type": "content_block_delta"},
        "anthropic",
    )
