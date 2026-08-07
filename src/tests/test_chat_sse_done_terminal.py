from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.transports import http_runtime
from src.upstream import ChatSSEAssistantBuilder, ChatSSEUsageTracker


def _event(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _usage(prompt: int, completion: int) -> bytes:
    return _event({"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}})


def test_tracker_and_builder_ignore_everything_after_done():
    tracker = ChatSSEUsageTracker()
    builder = ChatSSEAssistantBuilder()
    payload = (
        _event({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        + _usage(7, 3)
        + b"data: [DONE]\n\n"
        + _event({"choices": [{"delta": {"content": "leak"}, "finish_reason": None}]})
        + _usage(999, 999)
        + b"data: {not-json}\n\n"
        + b"data: [DONE]\n\n"
    )
    tracker.feed(payload)
    builder.feed(payload)

    assert tracker.usage["input_tokens"] == 7
    assert tracker.usage["output_tokens"] == 3
    assert "leak" not in builder.get_assistant().get("content", "")
    assert "999" not in tracker.get_full_response()


def test_finish_reason_does_not_block_later_usage_before_done():
    tracker = ChatSSEUsageTracker()
    tracker.feed(_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    tracker.feed(_usage(11, 5))
    assert tracker.usage["input_tokens"] == 11
    assert tracker.usage["output_tokens"] == 5
    assert tracker.done_received is False


def test_relay_filter_handles_split_done_and_drops_repeated_or_malformed_tail():
    tracker = ChatSSEUsageTracker()
    first = tracker.filter_relay_chunk(_event({"choices": [{"delta": {"content": "ok"}}]}) + b"data: [DO")
    second = tracker.filter_relay_chunk(b"NE]\n\ndata: {bad}\n\ndata: [DONE]\n\n")

    assert b"ok" in first
    assert second == b"data: [DONE]\n\n"
    assert tracker.filter_relay_chunk(_usage(50, 50)) == b""


def test_missing_done_keeps_finish_reason_terminal_semantics():
    tracker = ChatSSEUsageTracker()
    tracker.feed(_event({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    assert tracker.saw_stream_end is True
    assert tracker.done_received is False


def test_actual_read_step_exposes_done_once_and_no_tail(monkeypatch):
    tracker = ChatSSEUsageTracker()
    builder = ChatSSEAssistantBuilder()

    async def restore(_channel, raw, **_kwargs):
        return raw

    monkeypatch.setattr(http_runtime.provider_registry, "restore_response_bytes", restore)

    async def chunks():
        yield b"data: [DONE]\n\n" + _usage(900, 900) + b"data: {bad}\n\n"

    step = asyncio.run(http_runtime.read_next_stream_step(
        aiter=chunks().__aiter__(),
        channel=SimpleNamespace(key="chat", protocol="openai-chat"),
        dynamic_map=None,
        tracker=tracker,
        builder=builder,
        stream_translator=None,
        deadline_ts=10**20,
        start_time=0,
        idle_timeout=30,
    ))

    assert step.downstream_chunks == [b"data: [DONE]\n\n"]
    assert tracker.done_received is True
    assert tracker.usage["input_tokens"] == 0
