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


def unapproved_channel_error_info(payload: object) -> tuple[str, str] | None:
    """Recognize the exact rejection only in a caller-verified WorkBuddy context.

    The upstream can reject client identity/request policy without saying that
    credentials are unhealthy. This does not assert that every 11128 is a content
    error. Return only the recognized diagnostic, never arbitrary vendor fields
    (which may contain credentials, prompts, or untrusted display markup).
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = error.get("code")
    if type(code) not in (int, str) or str(code) != UNAPPROVED_CHANNEL_CODE:
        return None
    message = error.get("msg") or error.get("message")
    if not isinstance(message, str) or message.strip() != UNAPPROVED_CHANNEL_MESSAGE:
        return None
    return UNAPPROVED_CHANNEL_CODE, UNAPPROVED_CHANNEL_MESSAGE
