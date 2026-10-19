"""Narrow WorkBuddy request-rejection identity, not a global business-code rule."""
from __future__ import annotations


QUOTA_CODES = frozenset({"14001", "14012", "14013", "14014", "14018", "14019"})


def error_payload(payload):
    """Known WorkBuddy envelopes: code, error.code, and error.data.code."""
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if not isinstance(error, dict):
        return payload
    data = error.get("data")
    return data if "code" not in error and isinstance(data, dict) else error


def quota_code(result):
    """Exact structured business codes, only used by WorkBuddy callers."""
    import json
    code = str(getattr(result, "error_code", "") or "")
    if code in QUOTA_CODES:
        return code
    for raw in (getattr(result, "full_response_text", ""), getattr(result, "error_detail", "")):
        try:
            text = str(raw or "")
            obj = json.loads(text[text.index("{"):])
            error = error_payload(obj)
            code = str(error.get("code") or "")
            if code in QUOTA_CODES:
                return code
        except (ValueError, TypeError, AttributeError):
            continue
    return None


UNAPPROVED_CHANNEL_CODE = "11128"
UNAPPROVED_CHANNEL_MESSAGE = "Illegal API invocation from an unapproved channel"
CHANNEL_REJECTION_MESSAGES = frozenset({
    UNAPPROVED_CHANNEL_MESSAGE,
    "conversation id missing",
    "first message is not system prompt",
})
ACCOUNT_RESTRICTED_CODE = "11140"
ACCOUNT_RESTRICTED_MESSAGE = "WorkBuddy 账号被供应商限制使用，请在官方客户端提交反馈申诉；解除后可清除冷却再试。"
ACCOUNT_RESTRICTED_COOLDOWN_SECONDS = 2 * 60 * 60
TOOL_HISTORY_CODE = "11148"
TOOL_HISTORY_MESSAGE = "tool calls and tool results do not match, please start a new conversation and retry"


def unapproved_channel_error_info(payload: object) -> tuple[str, str] | None:
    """Recognize the exact rejection only in a caller-verified WorkBuddy context.

    The upstream can reject client identity/request policy without saying that
    credentials are unhealthy. This does not assert that every 11128 is a content
    error. Return only the recognized diagnostic, never arbitrary vendor fields
    (which may contain credentials, prompts, or untrusted display markup).
    """
    if not isinstance(payload, dict):
        return None
    error = error_payload(payload)
    code = error.get("code")
    if type(code) not in (int, str) or str(code) != UNAPPROVED_CHANNEL_CODE:
        return None
    message = error.get("msg") or error.get("message")
    if not isinstance(message, str) or message.strip() not in CHANNEL_REJECTION_MESSAGES:
        return None
    return UNAPPROVED_CHANNEL_CODE, message.strip()


def request_rejection_info(payload: object) -> tuple[str, str] | None:
    rejection = unapproved_channel_error_info(payload)
    if rejection is not None:
        return rejection
    error = error_payload(payload)
    code = error.get("code")
    if type(code) not in (int, str) or str(code) != TOOL_HISTORY_CODE:
        return None
    detail = error.get("extError")
    message = error.get("msg") or error.get("message")
    if not message and isinstance(detail, dict) and detail.get("type") == "invalid_request_error":
        message = detail.get("message")
    if isinstance(message, str) and message.strip() == TOOL_HISTORY_MESSAGE:
        return TOOL_HISTORY_CODE, TOOL_HISTORY_MESSAGE
    return None
