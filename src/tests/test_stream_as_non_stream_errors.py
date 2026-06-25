from __future__ import annotations

import json
import time

import httpx

from src.protocols.runtime import request_invalid_result_if_needed
from src.transports.http_runtime import aggregate_stream_as_non_stream_response
from src.openai.transform.stream_responses_to_anthropic import StreamTranslator


class _Ctx:
    def __init__(self) -> None:
        self.closed = False

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True


class _Channel:
    key = "api:test"
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


def _event(name: str, data: dict) -> bytes:
    return (
        f"event: {name}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


async def test_stream_only_nonstream_incomplete_max_output_is_context_error():
    chunks = [
        _event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress"},
        }),
        _event("response.incomplete", {
            "type": "response.incomplete",
            "response": {
                "id": "resp_1",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 271409, "output_tokens": 137, "total_tokens": 271546},
                "output": [],
            },
        }),
    ]
    ctx = _Ctx()
    resp = httpx.Response(200, stream=_ChunkedByteStream(chunks), headers={"content-type": "text/event-stream"})
    started = time.time()

    result = await aggregate_stream_as_non_stream_response(
        ctx,
        resp,
        _Channel(),
        "gpt-5.5",
        dynamic_map=None,
        connect_ms=3,
        start_time=started,
        deadline_ts=started + 30,
        total_timeout=30,
        first_byte_timeout=5,
        idle_timeout=5,
        translator_ctx=None,
    )

    assert ctx.closed is True
    assert result.error is not None
    assert result.error.outcome == "upstream_error_json"
    assert result.error.error_detail.startswith("Prompt is too long:")
    assert "context_length_exceeded" in result.error.error_detail
    assert "max_output_tokens" in result.error.error_detail

    normalized = request_invalid_result_if_needed(result.error)
    assert normalized.outcome == "request_invalid"
    assert normalized.http_status == 400


async def test_stream_only_nonstream_context_length_failed_is_not_success():
    chunks = [
        _event("response.created", {
            "type": "response.created",
            "response": {"id": "resp_1", "status": "in_progress"},
        }),
        _event("error", {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
                "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
                "param": "input",
            },
            "sequence_number": 2,
        }),
        _event("response.failed", {
            "type": "response.failed",
            "response": {
                "id": "resp_1",
                "status": "failed",
                "error": {
                    "code": "context_length_exceeded",
                    "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
                },
            },
        }),
    ]
    ctx = _Ctx()
    resp = httpx.Response(200, stream=_ChunkedByteStream(chunks), headers={"content-type": "text/event-stream"})
    started = time.time()

    result = await aggregate_stream_as_non_stream_response(
        ctx,
        resp,
        _Channel(),
        "gpt-5.5",
        dynamic_map=None,
        connect_ms=3,
        start_time=started,
        deadline_ts=started + 30,
        total_timeout=30,
        first_byte_timeout=5,
        idle_timeout=5,
        translator_ctx=None,
    )

    assert ctx.closed is True
    assert result.error is not None
    assert result.error.outcome == "upstream_error_json"
    assert result.error.http_status == 200
    assert result.error.error_detail.startswith("Prompt is too long:")
    assert "context_length_exceeded" in result.error.error_detail

    normalized = request_invalid_result_if_needed(result.error)
    assert normalized.outcome == "request_invalid"
    assert normalized.http_status == 400


def test_responses_to_anthropic_incomplete_max_output_emits_context_error():
    tr = StreamTranslator(model="gpt-5.5")
    chunk = _event("response.incomplete", {
        "type": "response.incomplete",
        "response": {
            "id": "resp_1",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        },
    })

    out = b"".join(tr.feed(chunk)).decode("utf-8")

    assert "event: error" in out
    assert '"type":"invalid_request_error"' in out
    assert '"code":"context_length_exceeded"' in out
    assert "Prompt is too long" in out
    assert "max_output_tokens" in out


def test_responses_to_anthropic_response_failed_adds_invalid_request_type():
    tr = StreamTranslator(model="gpt-5.5")
    chunk = _event("response.failed", {
        "type": "response.failed",
        "response": {
            "id": "resp_1",
            "status": "failed",
            "error": {
                "code": "context_length_exceeded",
                "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
            },
        },
    })

    out = b"".join(tr.feed(chunk)).decode("utf-8")

    assert "event: error" in out
    assert '"type":"invalid_request_error"' in out
    assert '"code":"context_length_exceeded"' in out
    assert "Prompt is too long" in out
