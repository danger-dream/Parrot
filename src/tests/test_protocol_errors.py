"""Error classifier tests for Protocol Runtime Phase 4."""

from __future__ import annotations

from src import errors as legacy_errors
from src.protocols import errors


def test_http_status_classification_preserves_legacy_anthropic_types():
    for status in (400, 401, 403, 404, 408, 413, 429, 504, 529, 500, 418):
        norm = errors.normalize_http_status(status)
        assert norm.anthropic_error_type == legacy_errors.classify_http_status(status)
        assert errors.legacy_anthropic_error_type_for_http_status(status) == legacy_errors.classify_http_status(status)


def test_http_status_classification_categories_and_oauth_refresh():
    assert errors.normalize_http_status(401).category == "auth"
    assert errors.normalize_http_status(401).should_refresh_oauth is True
    assert errors.normalize_http_status(403).category == "permission"
    assert errors.normalize_http_status(429).category == "rate_limit"
    assert errors.normalize_http_status(413).category == "request_too_large"
    assert errors.normalize_http_status(529).category == "overloaded"
    assert errors.normalize_http_status(500).category == "upstream"


def test_attempt_outcome_without_status_matches_old_err_type_mapping():
    assert errors.classify_attempt_outcome("first_byte_timeout", None).anthropic_error_type == legacy_errors.ErrType.TIMEOUT
    assert errors.classify_attempt_outcome("transform_error", None).anthropic_error_type == legacy_errors.ErrType.INVALID_REQUEST
    assert errors.classify_attempt_outcome("transport_error", None).anthropic_error_type == legacy_errors.ErrType.API


def test_extract_error_info_supports_wrapped_anthropic_and_openai_shapes():
    assert errors.extract_error_info({"error": {"type": "rate_limit_error", "message": "slow"}}) == (
        "rate_limit_error",
        "rate_limit_error: slow",
    )
    assert errors.extract_error_info({"response": {"error": {"code": "ERR", "message": "bad thing"}}}) == (
        "ERR",
        "ERR: bad thing",
    )
    # Preserve the legacy helper's precedence: when a top-level dict contains
    # both type=error and error_type, the first dict branch treats type as the
    # code before falling through to top-level error_type handling.
    assert errors.extract_error_info({"type": "error", "error_type": "server_error", "message": "boom"}) == (
        "error",
        "error: boom",
    )
    assert errors.extract_error_info({"message": "plain"}) == (None, "plain")


def test_context_length_errors_are_formatted_for_claude_code_compact():
    code, message = errors.extract_error_info({
        "error": {
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
            "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
        }
    })

    assert code == "context_length_exceeded"
    assert message.startswith("Prompt is too long:")
    assert "context_length_exceeded" in message
    assert "context window" in message


def test_context_length_formatter_preserves_parseable_token_gap():
    message = errors.context_length_error_message_for_claude_code(
        "This model's maximum context length is 272000 tokens. 300000 tokens > 272000 maximum."
    )

    assert message.startswith("Prompt is too long: 300000 tokens > 272000 maximum.")
