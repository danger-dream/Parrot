"""Error classifier tests for Protocol Runtime Phase 4."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from src import errors as legacy_errors
from src.protocols import errors, runtime


def test_retry_after_parser_supports_delta_date_and_safe_bounds():
    assert runtime.parse_retry_after_seconds("7") == 7.0
    assert runtime.parse_retry_after_seconds("999") == runtime.MAX_RETRY_AFTER_SECONDS
    assert runtime.parse_retry_after_seconds("nan") is None

    now = datetime(2026, 7, 24, 13, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(now + timedelta(seconds=12), usegmt=True)
    assert runtime.parse_retry_after_seconds(header, now_ts=now.timestamp()) == 12.0


def test_retry_delay_config_discards_non_finite_values():
    cfg = {"retry": {"transient": {"backoffSeconds": ["nan", "inf", 1.25]}}}
    assert runtime.configured_transient_retry_delays(cfg) == (1.25,)


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


def test_request_invalid_error_info_accepts_explicit_input_validation_shapes():
    openai = {
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_value",
            "param": "input",
            "message": "Invalid image data.",
        }
    }
    provider = {
        "type": "invalid_request_error",
        "code": "invalid_image",
        "param": "messages[0].content[0]",
        "message": "Image could not be decoded.",
    }

    assert errors.request_invalid_error_info(openai) == ("invalid_value", "Invalid image data.")
    assert errors.request_invalid_error_info(provider) == ("invalid_image", "Image could not be decoded.")


def test_request_invalid_error_info_fails_closed_for_channel_or_ambiguous_400_bodies():
    assert errors.request_invalid_error_info({"message": "bad request"}) is None
    assert errors.request_invalid_error_info({
        "error": {
            "type": "api_error",
            "code": "upstream_rejected",
            "message": "channel request was rejected",
        }
    }) is None
    assert errors.request_invalid_error_info({
        "error": {
            "type": "invalid_request_error",
            "code": "model_not_found",
            "param": "model",
            "message": "configured upstream model does not exist",
        }
    }) is None
    assert errors.request_invalid_error_info({
        "error": {
            "type": "invalid_request_error",
            "code": "1310",
            "message": "[1310][您已达到每周/每月使用上限，您的限额将在 2099-01-01 00:00:00 重置。]",
        }
    }) is None


def test_zhipu_1301_content_policy_is_an_explicit_request_fault():
    message = (
        "[1301][系统检测到输入或生成内容可能包含不安全或敏感内容，"
        "请您避免输入易产生敏感内容的提示语，感谢您的配合。][req-1]"
    )
    payload = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "1301",
            "message": message,
        },
        "request_id": "req-1",
    }

    assert errors.request_invalid_error_info(payload) == ("1301", message)
    assert errors.is_zhipu_content_policy_code_or_message("1301", "unrelated") is True
    assert errors.is_zhipu_content_policy_code_or_message(None, message) is True
    assert errors.is_zhipu_content_policy_code_or_message(None, "[1310][每周/每月使用上限]") is False

    chat = runtime.json_error_for_ingress(
        "chat", 400, "invalid_request_error", message, code="1301",
    )
    chat_body = chat.body.decode() if isinstance(chat.body, (bytes, bytearray)) else chat.body
    chat_payload = json.loads(chat_body)
    assert chat.status_code == 400
    assert chat_payload["error"]["code"] == "1301"
    assert chat_payload["error"]["message"] == message
    assert chat_payload["error"]["type"] == "invalid_request_error"


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
