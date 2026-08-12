"""Focused contract tests for narrow OpenAI OAuth upstream HTML 403 protection."""

from __future__ import annotations

import pytest

from src.failover import _structured_attempt_error, _structured_failure_details
from src.openai.responses_ws import (
    _WsAttemptResult,
    _aggregate_failed_candidate_status,
    _responses_ws_upstream_transport,
)
from src.protocols.runtime import AttemptResult, is_html_error_document


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><title>blocked</title>",
        b" \r\n<HTML><body>blocked</body>",
        "\ufeff\t<!DoCtYpE hTmL public>",
        b"\xef\xbb\xbf  <html lang=en>",
    ],
)
def test_html_document_prefix_accepts_bom_whitespace_and_case(body):
    assert is_html_error_document(body) is True


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"forbidden",
        b'{"error":"forbidden"}',
        b"<?xml version='1.0'?><html/>",
        b"<svg><text>403</text></svg>",
        b"prefix <html>",
        b"\xff<html>",
        None,
    ],
)
def test_html_document_prefix_rejects_non_html_or_unreadable_body(body):
    assert is_html_error_document(body) is False


def test_marker_is_generic_sanitized_http_failure_not_permission():
    result = AttemptResult(
        outcome="http_auth_error",
        http_status=403,
        error_detail="HTTP 403: <!doctype html><title>account@example.test secret</title>",
        openai_oauth_html_403=True,
    )
    attempt = _structured_attempt_error(result, 1)
    assert attempt["status"] == 403
    assert attempt["classification"] == "upstream_http_error"
    assert attempt["message"] == "Upstream returned an HTTP 403 response"
    assert "example" not in _structured_failure_details([attempt])["summary"]


def test_structured_plain_403_is_unchanged_permission_failure():
    attempt = _structured_attempt_error(
        AttemptResult(outcome="http_auth_error", http_status=403, error_detail="forbidden"),
        1,
    )
    assert attempt["classification"] == "permission_error"
    assert attempt["message"] == "forbidden"


def test_http_mixed_root_cause_prefers_real_permission_over_marker():
    marker = _structured_attempt_error(
        AttemptResult(
            outcome="http_auth_error", http_status=403,
            error_detail="HTTP 403: <html>sensitive</html>",
            openai_oauth_html_403=True,
        ),
        1,
    )
    permission = _structured_attempt_error(
        AttemptResult(outcome="http_auth_error", http_status=403, error_detail="denied"),
        2,
    )
    details = _structured_failure_details([permission, marker])
    assert details["root_cause"]["classification"] == "permission_error"
    assert details["root_cause"]["message"] == "denied"


def test_ws_all_marker_is_403_but_mixed_marker_cannot_override_real_causes():
    assert _aggregate_failed_candidate_status([0, 0]) == 403
    assert _aggregate_failed_candidate_status([401, 0]) == 401
    assert _aggregate_failed_candidate_status([402, 0]) == 402
    assert _aggregate_failed_candidate_status([429, 0]) == 429
    assert _aggregate_failed_candidate_status([503, 0]) == 503


def test_ws_result_and_http_result_preserve_explicit_marker_and_status():
    http = AttemptResult(outcome="http_auth_error", http_status=403, openai_oauth_html_403=True)
    ws = _WsAttemptResult(outcome="http_auth_error", http_status=403, openai_oauth_html_403=True)
    assert http.http_status == ws.http_status == 403
    assert http.openai_oauth_html_403 and ws.openai_oauth_html_403


def test_openai_oauth_ws_ingress_cannot_use_http_sse_bridge():
    # A minimal instance avoids constructor/account I/O; this function only needs typed identity.
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel

    ch = object.__new__(OpenAIOAuthChannel)
    ch.responses_ws_upstream_transport = "sse"
    assert _responses_ws_upstream_transport(ch) == "ws"
