"""Targeted request-fault and connection-lifecycle health policy tests."""

from __future__ import annotations

import http.client
import json
import socket
import threading

import httpcore
import httpx
import pytest
import websockets

from src.protocols import errors as protocol_errors
from src.protocols import finalize
from src.protocols.runtime import (
    AttemptResult,
    connection_lifecycle_outcome,
    is_connection_lifecycle_error,
    request_invalid_result_if_needed,
    retry_after_cooldown_until,
    should_cooldown,
    should_record_failure,
)
from src.transports import http_runtime
from src.transports.ws_runtime import read_next_responses_ws_step


@pytest.mark.parametrize("status", [400, 409, 413, 422])
def test_plain_ambiguous_client_status_remains_retryable_upstream_failure(status):
    result = AttemptResult(
        outcome="http_error",
        http_status=status,
        error_detail=f"HTTP {status}: non-standard rejection",
    )

    normalized = request_invalid_result_if_needed(result)

    assert normalized.outcome == "http_error"
    assert normalized.http_status == status
    assert normalized.error_code is None
    assert should_cooldown(normalized.outcome) is True
    assert should_record_failure(normalized.outcome) is True


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_authoritative_auth_payment_rate_status_beats_invalid_request_body(status):
    result = AttemptResult(
        outcome="http_error",
        http_status=status,
        error_detail='{"error":{"type":"invalid_request_error","message":"bad"}}',
    )
    assert request_invalid_result_if_needed(result).outcome == "http_error"


def test_413_rate_limit_signal_remains_retryable_upstream_failure():
    result = AttemptResult(
        outcome="http_error",
        http_status=413,
        error_detail="HTTP 413: tokens per minute exceeded; retry later",
    )

    normalized = request_invalid_result_if_needed(result)

    assert normalized.outcome == "http_error"
    assert normalized.http_status == 413
    assert should_cooldown(normalized.outcome) is True
    assert should_record_failure(normalized.outcome) is True


def test_retry_after_deadline_is_bounded_and_invalid_is_ignored():
    now = 1_700_000_000.0
    assert retry_after_cooldown_until("5", now_ts=now) == 1_700_000_005_000
    assert retry_after_cooldown_until("999999", now_ts=now) == 1_700_000_060_000
    assert retry_after_cooldown_until("not-a-date", now_ts=now) is None
    assert retry_after_cooldown_until(
        "Tue, 14 Nov 2023 22:13:30 GMT", now_ts=now,
    ) == 1_700_000_010_000


@pytest.mark.parametrize("status", [400, 409, 413, 422])
def test_structured_request_fault_preserves_supported_client_status(status):
    result = AttemptResult(
        outcome="http_error",
        http_status=status,
        error_detail=f"HTTP {status}: " + json.dumps({
            "response": {
                "details": {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_value",
                        "message": "bad input",
                    },
                },
            },
        }),
    )

    normalized = request_invalid_result_if_needed(result)

    assert normalized.outcome == "request_invalid"
    assert normalized.http_status == status
    assert normalized.error_code == "invalid_value"
    assert normalized.error_detail == "bad input"
    assert should_cooldown(normalized.outcome) is False
    assert should_record_failure(normalized.outcome) is False


@pytest.mark.parametrize(
    "marker",
    [
        "invalid_prompt",
        "invalid_request",
        "invalid_request_error",
        "bad_request_error",
        "invalid_value",
        "unsupported_value",
        "context_length_exceeded",
        "message_too_big",
        "string_above_max_length",
        "previous_response_not_found",
        "cyber_policy",
    ],
)
def test_explicit_structured_request_markers_override_wrapping_5xx(marker):
    result = AttemptResult(
        outcome="http_error",
        http_status=503,
        error_detail=f'HTTP 503: {{"error":{{"code":"{marker}","message":"bad input"}}}}',
    )

    normalized = request_invalid_result_if_needed(result)

    assert normalized.outcome == "request_invalid"
    assert normalized.http_status == 400
    assert normalized.error_code == marker
    assert should_cooldown(normalized.outcome) is False
    assert should_record_failure(normalized.outcome) is False


def test_plain_or_message_only_5xx_remains_upstream_failure():
    for detail in (
        "HTTP 503: upstream temporarily unavailable",
        'HTTP 503: {"error":{"type":"api_error","message":"invalid request from edge"}}',
        "Prompt is too long according to a broken 503 service",
    ):
        result = AttemptResult(outcome="http_error", http_status=503, error_detail=detail)
        normalized = request_invalid_result_if_needed(result)
        assert normalized.outcome == "http_error"
        assert normalized.http_status == 503
        assert should_cooldown(normalized.outcome) is True
        assert should_record_failure(normalized.outcome) is True


def test_normalized_outcomes_keep_retry_and_health_policy_consistent():
    request_fault = protocol_errors.classify_attempt_outcome("request_invalid", 422)
    assert request_fault.http_status == 422
    assert request_fault.retryable_before_commit is False
    assert request_fault.should_cooldown is False
    assert request_fault.should_score_failure is False

    lifecycle = protocol_errors.classify_attempt_outcome("connection_lifecycle", None)
    assert lifecycle.http_status == 502
    assert lifecycle.retryable_before_commit is True
    assert lifecycle.retryable_after_commit is False
    assert lifecycle.should_cooldown is False
    assert lifecycle.should_score_failure is False


def _scripted_httpx_get(response_bytes: bytes):
    """Return a real httpx result/exception from one byte-scripted HTTP/1.1 peer."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        try:
            conn.recv(65535)
            if response_bytes:
                conn.sendall(response_bytes)
        finally:
            conn.close()
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        return httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0)
    except Exception as exc:  # The exception object/cause is the test result.
        return exc
    finally:
        thread.join(timeout=2.0)


def test_real_no_response_disconnect_is_the_only_http_lifecycle_exception():
    exc = _scripted_httpx_get(b"")

    assert isinstance(exc, httpx.RemoteProtocolError)
    assert isinstance(exc.__cause__, httpcore.RemoteProtocolError)
    assert str(exc) == "Server disconnected without sending a response."
    assert connection_lifecycle_outcome(
        exc, http_phase="pre_headers",
    ) == "connection_lifecycle"
    assert should_cooldown("connection_lifecycle") is False
    assert should_record_failure("connection_lifecycle") is False
    # The same typed/detail signal is not neutral once response headers exist.
    assert connection_lifecycle_outcome(
        exc, http_status=200, http_phase="response_body",
    ) is None


@pytest.mark.parametrize("response_bytes,status,phase", [
    (b"XYZ\r\n\r\n", None, "pre_headers"),
    (b"HTTP/1.1 200 OK\r\nBad Header\r\n\r\n", None, "pre_headers"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc", 200, "response_body"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc", 200, "response_body"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZ\r\n", 200, "response_body"),
    (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"0\r\nBad Footer\r\n\r\n",
        200,
        "response_body",
    ),
])
def test_real_protocol_and_body_framing_errors_are_transport_failures(
    response_bytes, status, phase,
):
    exc = _scripted_httpx_get(response_bytes)

    assert isinstance(exc, httpx.RemoteProtocolError)
    assert isinstance(exc.__cause__, httpcore.RemoteProtocolError)
    assert connection_lifecycle_outcome(
        exc, http_status=status, http_phase=phase,
    ) is None
    assert should_cooldown("transport_error") is True
    assert should_record_failure("transport_error") is True


def test_real_legal_close_delimited_body_is_success():
    response = _scripted_httpx_get(
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc"
    )
    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert response.content == b"abc"


@pytest.mark.parametrize("exc", [
    httpx.StreamClosed(),
    EOFError("unexpected EOF"),
    http.client.IncompleteRead(b"abc", 5),
    RuntimeError("Server disconnected without sending a response."),
])
def test_untyped_or_local_eof_like_errors_are_not_http_lifecycle(exc):
    assert connection_lifecycle_outcome(exc, http_phase="pre_headers") is None
    assert connection_lifecycle_outcome(
        exc, http_status=200, http_phase="response_body",
    ) is None


@pytest.mark.asyncio
async def test_post_header_stream_call_passes_status_and_body_phase(monkeypatch):
    exc = _scripted_httpx_get(
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc"
    )
    assert isinstance(exc, httpx.RemoteProtocolError)
    seen = {}

    def classify(caught, **kwargs):
        seen.update(kwargs)
        return None

    class BrokenBody:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise exc

    monkeypatch.setattr(http_runtime, "connection_lifecycle_outcome", classify)
    step = await http_runtime.read_next_stream_step(
        aiter=BrokenBody(),
        channel=object(),
        dynamic_map=None,
        tracker=object(),
        builder=object(),
        stream_translator=None,
        deadline_ts=9999999999,
        start_time=0,
        idle_timeout=30,
        upstream_status=200,
    )

    assert step.outcome == "transport_error"
    assert seen == {"http_status": 200, "http_phase": "response_body"}


@pytest.mark.asyncio
async def test_non_success_status_wins_when_error_body_read_fails():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )
            yield b""  # pragma: no cover - makes this an async generator

    class Context:
        async def __aexit__(self, *_args):
            return None

    response = httpx.Response(503, stream=BrokenStream())
    result = await http_runtime.read_http_error_response(
        Context(), response, connect_ms=1,
    )

    assert result.outcome == "http_error"
    assert result.http_status == 503
    assert should_cooldown(result.outcome) is True
    assert should_record_failure(result.outcome) is True


def test_ws_close_code_policy_remains_adapter_specific():
    for code in (1000, 1001, 1006):
        assert is_connection_lifecycle_error(ws_close_code=code) is True
    assert is_connection_lifecycle_error(ws_close_code=1011) is False


@pytest.mark.asyncio
async def test_responses_ws_close_without_terminal_is_lifecycle_not_http_error():
    class ClosingWs:
        async def recv(self):
            raise websockets.ConnectionClosed(None, None)

    class Tracker:
        response_completed = False
        response_failed = False

    step = await read_next_responses_ws_step(
        ClosingWs(),
        Tracker(),
        channel_key="ch",
        deadline_ts=9999999999,
        idle_timeout=30,
    )

    assert step.outcome == "connection_lifecycle"
    assert step.http_status is None
    assert step.close_code == 1000


def test_lifecycle_is_retryable_before_commit_but_health_neutral_after_commit():
    # Outer failover retains its existing next-candidate policy for an
    # unsuccessful, uncommitted attempt.  Finalization is health-neutral.
    result = AttemptResult(outcome="connection_lifecycle", stream_started=False)
    assert result.success is False
    assert result.stream_started is False
    assert should_cooldown(result.outcome) is False
    assert should_record_failure(result.outcome) is False

    committed = finalize.error_plan(
        "connection_lifecycle", failure_policy="post_commit_stream",
    )
    assert committed.terminal == "error"
    assert committed.log_error is True
    assert committed.record_failure is False
    assert committed.record_cooldown_error is False
