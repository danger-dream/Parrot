from __future__ import annotations

import json
import time

import httpx

from src.openai.transform.stream_r2c import StreamTranslator
from src.protocols.commit_gate import SseCommitGate
from src.protocols.sse import split_sse_events
from src.transports.http_runtime import aggregate_stream_as_non_stream_response
from src.upstream import ResponsesSSEAssistantBuilder


def _event(name: str, data: dict, *, crlf: bool = False) -> bytes:
    nl = "\r\n" if crlf else "\n"
    return (
        f"event: {name}{nl}"
        f"data: {json.dumps(data, separators=(',', ':'))}{nl}{nl}"
    ).encode("utf-8")


def _chat_frames(raw: bytes) -> list[dict]:
    out: list[dict] = []
    for part in raw.decode("utf-8", "replace").split("\n\n"):
        for line in part.split("\n"):
            if line.startswith("data: "):
                value = line[6:].strip()
                if value and value != "[DONE]":
                    out.append(json.loads(value))
    return out


def test_shared_sse_splitter_accepts_crlf_across_network_chunks():
    created = _event("response.created", {"type": "response.created"}, crlf=True)
    completed = _event("response.completed", {"type": "response.completed"}, crlf=True)
    first = created[:-1]

    remaining, blocks = split_sse_events(first)
    assert blocks == []
    assert remaining == first

    remaining, blocks = split_sse_events(remaining + created[-1:] + completed)
    assert remaining == b""
    assert len(blocks) == 2
    assert blocks[0].startswith(b"event: response.created\r\n")
    assert blocks[1].startswith(b"event: response.completed\r\n")


def test_r2c_accepts_crlf_done_only_text_and_real_terminal():
    translator = StreamTranslator(model="gpt-test")
    events = (
        _event("response.output_text.done", {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": "CRLF terminal text",
        }, crlf=True)
        + _event("response.completed", {
            "type": "response.completed",
            "response": {"id": "resp_crlf", "status": "completed", "output": []},
        }, crlf=True)
    )

    # Split inside the CRLF blank line to exercise incremental framing.
    frames = list(translator.feed(events[:-2]))
    frames.extend(translator.feed(events[-2:]))
    frames.extend(translator.close())
    text = b"".join(frames)
    parsed = _chat_frames(text)
    assert any(
        choice.get("delta", {}).get("content") == "CRLF terminal text"
        for frame in parsed for choice in frame.get("choices", [])
    )
    assert b"data: [DONE]\n\n" in text


def test_r2c_missing_terminal_is_an_error_not_a_fake_stop():
    translator = StreamTranslator(model="gpt-test")
    partial = _event("response.output_text.delta", {
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": "partial",
    })
    frames = list(translator.feed(partial)) + list(translator.close())
    text = b"".join(frames)
    assert b"upstream stream ended without a terminal response event" in text
    assert b'"finish_reason": "stop"' not in text


def test_responses_builder_merges_done_snapshots_and_authoritative_completed_output():
    builder = ResponsesSSEAssistantBuilder()
    payload = b"".join([
        _event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message", "id": "msg_1", "role": "assistant",
                "content": [{"type": "output_text", "text": "stale", "annotations": []}],
            },
        }, crlf=True),
        _event("response.output_text.done", {
            "type": "response.output_text.done",
            "output_index": 0, "content_index": 0, "text": "terminal text",
        }, crlf=True),
        _event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "lookup", "arguments": "",
            },
        }, crlf=True),
        _event("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "output_index": 1, "arguments": '{"city":"SZ"}',
        }, crlf=True),
        _event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": "resp_1", "status": "completed", "model": "gpt-test",
                # Simulate an upstream that exposes sparse completed output while
                # detailed done events carried the actual payload.
                "output": [
                    {"type": "message", "id": "msg_1", "role": "assistant",
                     "content": [{"type": "output_text", "text": "", "annotations": []}]},
                    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "lookup", "arguments": ""},
                ],
            },
        }, crlf=True),
    ])
    builder.feed(payload)

    full = builder.to_full_json(fallback_model="unused")
    output = full["output"]
    assert output[0]["content"][0]["text"] == "terminal text"
    assert output[1]["arguments"] == '{"city":"SZ"}'
    assert full["model"] == "gpt-test"


def test_empty_responses_completed_event_commits_normally():
    gate = SseCommitGate(protocol="openai-responses")
    created = _event("response.created", {"type": "response.created"})
    completed = _event("response.completed", {
        "type": "response.completed",
        "response": {"id": "resp_empty", "status": "completed", "output": []},
    })

    result = gate.feed(created + completed)
    assert result.error_event is None
    assert result.downstream_chunks == [created, completed]


def test_r2c_done_only_function_call_finishes_as_tool_calls():
    translator = StreamTranslator(model="gpt-test")
    events = [
        _event("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "lookup", "arguments": '{"q":"x"}',
            },
        }),
        # Some upstreams omit output from completed even though output_item.done
        # already carried the real tool call.
        _event("response.completed", {
            "type": "response.completed",
            "response": {"id": "resp_tool", "status": "completed", "output": []},
        }),
    ]
    frames: list[bytes] = []
    for event in events:
        frames.extend(translator.feed(event))
    frames.extend(translator.close())
    parsed = _chat_frames(b"".join(frames))
    assert any(
        choice.get("delta", {}).get("tool_calls")
        for frame in parsed for choice in frame.get("choices", [])
    )
    assert parsed[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_r2c_empty_completed_stream_emits_normal_role_and_stop():
    translator = StreamTranslator(model="gpt-test")
    frames = list(translator.feed(_event("response.completed", {
        "type": "response.completed",
        "response": {"id": "resp_empty", "status": "completed", "output": []},
    })))
    frames.extend(translator.close())
    parsed = _chat_frames(b"".join(frames))
    assert parsed[0]["choices"][0]["delta"]["role"] == "assistant"
    assert parsed[-1]["choices"][0]["finish_reason"] == "stop"


class _Ctx:
    def __init__(self) -> None:
        self.closed = False

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True


class _Channel:
    key = "api:responses-test"
    type = "api"
    protocol = "openai-responses"

    async def restore_response(self, chunk: bytes, dynamic_map=None) -> bytes:
        return chunk


class _ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


async def test_non_stream_aggregate_rejects_eof_without_terminal_event():
    ctx = _Ctx()
    response = httpx.Response(
        200,
        stream=_ChunkedByteStream([_event("response.output_text.delta", {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial",
        })]),
        headers={"content-type": "text/event-stream"},
    )
    started = time.time()
    result = await aggregate_stream_as_non_stream_response(
        ctx,
        response,
        _Channel(),
        "gpt-test",
        dynamic_map=None,
        connect_ms=1,
        start_time=started,
        deadline_ts=started + 30,
        total_timeout=30,
        first_byte_timeout=5,
        idle_timeout=5,
    )

    assert ctx.closed is True
    assert result.error is not None
    assert result.error.outcome == "upstream_malformed"
    assert "without a terminal SSE event" in (result.error.error_detail or "")
