"""Structured error details on SSE terminal frames.

Once bytes reached downstream, the terminal error must carry the same
``details`` contract as a pre-commit JSON error, so clients can tell a
transient body failure from a final request fault.
"""
from __future__ import annotations

import json

import pytest
from starlette.responses import JSONResponse

from src import errors, failover, local_web_tools
from src.protocols.runtime import AttemptResult, sse_error_for_ingress
from src.transports import precommit_sse

DISCONNECT = "stream transport error: peer closed connection without sending complete message body (incomplete chunked read)"


def _sse_data(frame: bytes) -> dict:
    line = next(line for line in frame.decode().splitlines() if line.startswith("data: "))
    return json.loads(line[6:])


def terminal_details(output: str) -> dict:
    """The structured details of the first terminal error frame in any SSE dialect."""
    def find(node):
        if isinstance(node, dict):
            if isinstance(node.get("details"), dict) and "root_cause" in node["details"]:
                return node["details"]
            return next((found for value in node.values() if (found := find(value))), None)
        if isinstance(node, list):
            return next((found for value in node if (found := find(value))), None)
        return None

    for line in output.splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            found = find(json.loads(line[6:]))
            if found:
                return found
    raise AssertionError(f"no structured terminal error in {output!r}")


@pytest.mark.parametrize("outcome,status,classification", [
    ("transport_error", 200, "transport_error"),
    ("connection_lifecycle", 200, "transport_error"),
    ("closed_before_first_byte", 200, "transport_error"),
    ("stream_truncated", 200, "transport_error"),
    ("upstream_closed", None, "transport_error"),
    ("idle_timeout", 200, "timeout"),
])
def test_body_failure_after_2xx_is_classified_by_outcome(outcome, status, classification):
    attempt = failover._structured_attempt_error(
        AttemptResult(outcome=outcome, http_status=status, error_detail=DISCONNECT), 1,
    )
    assert attempt["status"] == status
    assert attempt["classification"] == classification


def test_error_statuses_still_win_over_outcome():
    for status, classification in ((429, "rate_limit_error"), (502, "upstream_server_error"),
                                   (404, "upstream_http_error")):
        attempt = failover._structured_attempt_error(
            AttemptResult(outcome="transport_error", http_status=status, error_detail="x"), 1,
        )
        assert attempt["classification"] == classification


def test_committed_details_match_precommit_contract():
    details = failover._committed_stream_failure_details(None, "transport_error", DISCONNECT, http_status=200)
    assert set(details) == {"summary", "root_cause", "attempts"}
    assert details["root_cause"] == {
        "status": 200, "classification": "transport_error", "code": None,
        "message": DISCONNECT, "retryable": True, "retry_scope": "request",
    }
    assert len(details["attempts"]) == 1


def test_committed_request_invalid_is_final():
    root = failover._committed_stream_failure_details(
        None, "request_invalid", "prompt is too long", http_status=400, code="context_length_exceeded",
    )["root_cause"]
    assert root["classification"] == "invalid_request"
    assert root["code"] == "context_length_exceeded"
    assert (root["retryable"], root["retry_scope"]) == (False, "none")


def test_committed_blacklist_keeps_precommit_retryability():
    committed = failover._committed_stream_failure_details(None, "blacklist_hit", "blacklist: x", http_status=200)
    precommit = failover._structured_failure_details([failover._structured_attempt_error(
        AttemptResult(outcome="blacklist_hit", http_status=200, error_detail="blacklist: x"), 1,
    )])
    assert committed["root_cause"] == precommit["root_cause"]


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
def test_sse_error_frames_carry_details(ingress):
    details = failover._committed_stream_failure_details(None, "transport_error", DISCONNECT, http_status=200)
    frame = _sse_data(sse_error_for_ingress(ingress, errors.ErrType.API, DISCONNECT, details=details))
    carried = frame["details"] if ingress == "responses" else frame["error"]["details"]
    assert carried == details


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
def test_sse_error_frames_without_details_are_unchanged(ingress):
    frame = _sse_data(sse_error_for_ingress(ingress, errors.ErrType.API, "boom"))
    assert "details" not in frame
    assert "details" not in (frame.get("error") or {})


def _final_json_error(status: int = 503) -> JSONResponse:
    details = failover._structured_failure_details([failover._structured_attempt_error(
        AttemptResult(outcome="transport_error", http_status=None, error_detail="connect reset"), 1,
    )])
    return errors.json_error_response(status, errors.ErrType.API, details["summary"], details=details)


def test_keepalive_error_chunk_keeps_final_details():
    response = _final_json_error()
    expected = json.loads(response.body)["error"]["details"]
    assert _sse_data(precommit_sse._error_chunk(response))["details"] == expected


def test_local_web_tool_error_conversions_keep_final_details():
    response = _final_json_error()
    expected = json.loads(response.body)["error"]["details"]
    anthropic = local_web_tools._anthropic_error_payload_from_response(response, response.body)
    responses = local_web_tools._responses_error_payload_from_response(response, response.body)
    assert anthropic["error"]["details"] == expected
    assert responses["response"]["error"]["details"] == expected
