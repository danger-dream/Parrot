"""Protocol runtime primitives shared by failover transports.

Phase 9 moves protocol-specific helpers out of the HTTP/WS failover loops while
keeping their legacy behaviour.  This module intentionally contains only pure
selection/encoding/data-shape code: no network I/O, scheduler mutations, or
cooldown writes live here.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpcore
import httpx

from .. import blacklist, errors
from ..providers import registry as provider_registry
from .commit_gate import is_responses_visible_event_type
from . import errors as protocol_errors
from . import registry as protocol_registry


def toolkit_for_channel(channel) -> dict[str, Any]:
    """Return the legacy protocol toolkit dict for a channel.

    The failover loops still consume the historical dict shape.  The registry
    owns the protocol table, and unknown protocols fail loudly instead of
    silently parsing with the wrong codec.
    """
    proto = getattr(channel, "protocol", "anthropic")
    try:
        return protocol_registry.get_toolkit(proto).as_legacy_dict()
    except KeyError as exc:
        raise ValueError(
            f"no upstream toolkit registered for protocol {proto!r} "
            f"(channel={getattr(channel, 'key', '?')})"
        ) from exc


_ERR_TYPE_ANTHROPIC_TO_OPENAI = {
    errors.ErrType.API: errors.ErrTypeOpenAI.SERVER,
    errors.ErrType.TIMEOUT: errors.ErrTypeOpenAI.TIMEOUT,
    errors.ErrType.RATE_LIMIT: errors.ErrTypeOpenAI.RATE_LIMIT,
    errors.ErrType.INVALID_REQUEST: errors.ErrTypeOpenAI.INVALID_REQUEST,
    errors.ErrType.AUTH: errors.ErrTypeOpenAI.AUTH,
    errors.ErrType.PERMISSION: errors.ErrTypeOpenAI.PERMISSION,
    errors.ErrType.NOT_FOUND: errors.ErrTypeOpenAI.NOT_FOUND,
    errors.ErrType.OVERLOADED: errors.ErrTypeOpenAI.SERVER,
    errors.ErrType.REQUEST_TOO_LARGE: errors.ErrTypeOpenAI.INVALID_REQUEST,
}


def translate_error_type(anth_type: str, ingress: str) -> str:
    if ingress == "anthropic":
        return anth_type
    return _ERR_TYPE_ANTHROPIC_TO_OPENAI.get(anth_type, errors.ErrTypeOpenAI.API)


def sse_error_for_ingress(
    ingress: str,
    anth_err_type: str,
    message: str,
    *,
    code: str | None = None,
) -> bytes:
    if ingress == "anthropic":
        if protocol_errors.is_context_length_code_or_message(code, message):
            message = protocol_errors.context_length_error_message_for_claude_code(message)
            code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
        return errors.sse_error_line(anth_err_type, message, code=code)
    mapped = translate_error_type(anth_err_type, ingress)
    if ingress == "chat":
        return errors.sse_error_line_chat(mapped, message, code=code)
    return errors.sse_error_line_responses(mapped, message, code=code)


def json_error_for_ingress(
    ingress: str,
    status: int,
    anth_err_type: str,
    message: str,
    *,
    code: str | None = None,
    details: dict | None = None,
):
    if ingress == "anthropic":
        if protocol_errors.is_context_length_code_or_message(code, message):
            message = protocol_errors.context_length_error_message_for_claude_code(message)
            code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
        return errors.json_error_response(
            status, anth_err_type, message, code=code, details=details,
        )
    mapped = translate_error_type(anth_err_type, ingress)
    return errors.json_error_openai(
        status, mapped, message, code=code, details=details,
    )


def make_stream_translator(translator_ctx: Optional[dict]):
    """Instantiate the response stream translator described by translator_ctx."""
    if not isinstance(translator_ctx, dict):
        return None
    name = translator_ctx.get("response_translator")
    model = translator_ctx.get("model_for_response") or ""
    if name == "chat_to_responses":
        from ..openai.transform.stream_r2c import StreamTranslator as _R2C
        return _R2C(
            model=model,
            include_usage=bool(translator_ctx.get("include_usage", False)),
        )
    if name == "responses_to_chat":
        from ..openai.transform.stream_c2r import StreamTranslator as _C2R
        return _C2R(
            model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
        )
    if name == "anthropic_to_chat":
        from ..openai.transform.stream_chat_to_anthropic import StreamTranslator as _C2A
        return _C2A(model=model)
    if name == "anthropic_to_responses":
        from ..openai.transform.stream_responses_to_anthropic import StreamTranslator as _R2A
        return _R2A(model=model, request_body=translator_ctx.get("request_body"))
    if name == "chat_to_anthropic":
        from ..openai.transform.stream_anthropic_to_chat import StreamTranslator as _A2C
        return _A2C(
            model=model,
            include_usage=bool(translator_ctx.get("include_usage", False)),
        )
    if name == "responses_to_anthropic":
        from ..openai.transform.stream_anthropic_to_responses import StreamTranslator as _A2R
        return _A2R(
            model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
            request_body=translator_ctx.get("request_body"),
            namespace_tool_map=translator_ctx.get("namespace_tool_map"),
        )
    return None


def apply_non_stream_response_translator(obj: dict, translator_ctx: dict) -> dict:
    """Translate an upstream non-stream response back to the ingress protocol."""
    if not isinstance(translator_ctx, dict):
        return obj
    name = translator_ctx.get("response_translator")
    model = translator_ctx.get("model_for_response") or ""
    if name == "chat_to_responses":
        from ..openai.transform.chat_to_responses import translate_response as _t
        return _t(obj, model=model)
    if name == "responses_to_chat":
        from ..openai.transform.responses_to_chat import translate_response as _t2
        return _t2(
            obj,
            model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
        )
    if name == "anthropic_to_chat":
        from ..openai.transform.anthropic_to_chat import translate_response as _t3
        return _t3(obj, model=model)
    if name == "chat_to_anthropic":
        from ..openai.transform.chat_to_anthropic import translate_response as _t4
        return _t4(obj, model=model)
    if name == "anthropic_to_responses":
        from ..openai.transform.anthropic_to_responses import translate_response as _t5
        return _t5(obj, model=model, request_body=translator_ctx.get("request_body"))
    if name == "responses_to_anthropic":
        from ..openai.transform.responses_to_anthropic import translate_response as _t6
        return _t6(
            obj,
            model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
            namespace_tool_map=translator_ctx.get("namespace_tool_map"),
        )
    return obj


@dataclass
class AttemptResult:
    outcome: str
    success: bool = False
    # Narrow upstream account-protection fact; set only by typed OpenAI OAuth call sites.
    openai_oauth_html_403: bool = False
    stream_started: bool = False
    response: Any = None
    http_status: Optional[int] = None
    # Final/terminal upstream route-round timing.  ``connect_ms`` is the business
    # connection metric defined by transport mode (HTTP stream→final headers,
    # HTTP non-stream→request body sent, WS→handshake return), never TCP-only.
    round_id: Optional[str] = None
    connect_ms: Optional[int] = None
    first_byte_ms: Optional[int] = None
    idle_ms: Optional[int] = None
    total_ms: Optional[int] = None
    # Outer channel-attempt elapsed is stored separately and never aliases total_ms.
    attempt_elapsed_ms: Optional[int] = None
    request_upload_ms: Optional[int] = None
    response_headers_wait_ms: Optional[int] = None
    response_body_first_byte_wait_ms: Optional[int] = None
    dns_ms: Optional[int] = None
    tcp_ms: Optional[int] = None
    proxy_tcp_ms: Optional[int] = None
    proxy_tunnel_ms: Optional[int] = None
    tls_ms: Optional[int] = None
    target_tls_ms: Optional[int] = None
    ws_handshake_ms: Optional[int] = None
    error_detail: Optional[str] = None
    error_code: Optional[str] = None
    # Parsed upstream Retry-After value, bounded before it reaches retry sleeps.
    retry_after_seconds: Optional[float] = None
    # Explicit epoch-millisecond cooldown derived only from an authoritative 429.
    cooldown_until: Optional[int] = None
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    })
    # Independent from token values: an authoritative 0/0 usage is observed,
    # while a missing/malformed usage object is not.
    usage_observed: Optional[bool] = None
    full_response_text: Optional[str] = None
    assistant_response: Optional[dict] = None
    proxy_name: Optional[str] = None
    proxy_bytes_up: int = 0
    proxy_bytes_down: int = 0
    translator_ctx: Optional[dict] = None


@dataclass
class PreparedNonStreamResponse:
    obj: dict | None = None
    restored: bytes | None = None
    restored_text: str = ""
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    })
    assistant_msg: dict = field(default_factory=dict)
    error: AttemptResult | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and isinstance(self.obj, dict) and self.restored is not None


async def prepare_non_stream_response(
    channel,
    raw: bytes,
    *,
    dynamic_map: Optional[dict],
    connect_ms: int | None,
    total_ms: int,
    translator_ctx: Optional[dict] = None,
) -> PreparedNonStreamResponse:
    """Restore, parse, and classify a non-stream upstream response.

    Provider restoration happens before protocol parsing and before any
    cross-protocol response translator.  Scheduler scoring, DB writes, affinity,
    and response construction intentionally stay in the caller.
    """
    raw_text = bytes(raw).decode("utf-8", errors="replace")
    try:
        restored = await provider_registry.restore_response_bytes(
            channel,
            raw,
            dynamic_map=dynamic_map,
            translator_ctx=translator_ctx,
        )
    except Exception as exc:
        # Restoration is a downstream presentation concern.  Preserve the
        # exact upstream body so terminal billing can still inspect actual cost,
        # usage, and service tier even when an adapter itself fails.
        return PreparedNonStreamResponse(
            restored_text=raw_text,
            error=AttemptResult(
                outcome="transform_error",
                connect_ms=connect_ms,
                total_ms=total_ms,
                error_detail=f"response restoration failed: {exc}"[:2000],
                full_response_text=raw_text,
                translator_ctx=translator_ctx,
            ),
        )
    restored_text = (
        restored.decode("utf-8", errors="replace")
        if isinstance(restored, bytes)
        else str(restored)
    )

    try:
        obj = json.loads(restored)
    except Exception as exc:
        return PreparedNonStreamResponse(
            restored=restored,
            restored_text=restored_text,
            error=AttemptResult(
                outcome="upstream_malformed",
                connect_ms=connect_ms,
                total_ms=total_ms,
                error_detail=f"non-JSON response: {exc}",
                full_response_text=restored_text,
            ),
        )

    toolkit = toolkit_for_channel(channel)

    if toolkit["is_upstream_error_json"](obj):
        if protocol_errors.is_responses_max_output_incomplete(obj):
            error_detail = protocol_errors.responses_max_output_context_error_message(
                protocol_errors.responses_incomplete_reason(obj)
            )
        else:
            code, msg = protocol_errors.extract_error_info(obj, fallback="upstream error")
            if protocol_errors.is_context_length_code_or_message(code, msg):
                error_detail = protocol_errors.context_length_error_message_for_claude_code(msg)
            else:
                error_detail = json.dumps(obj.get("error", obj), ensure_ascii=False)[:2000]
        return PreparedNonStreamResponse(
            obj=obj,
            restored=restored,
            restored_text=restored_text,
            error=AttemptResult(
                outcome="upstream_error_json",
                connect_ms=connect_ms,
                total_ms=total_ms,
                error_detail=error_detail[:2000],
                full_response_text=restored_text,
                translator_ctx=translator_ctx,
            ),
        )

    bl_hit = blacklist.match(restored, getattr(channel, "key", ""))
    if bl_hit:
        return PreparedNonStreamResponse(
            obj=obj,
            restored=restored,
            restored_text=restored_text,
            error=AttemptResult(
                outcome="blacklist_hit",
                connect_ms=connect_ms,
                total_ms=total_ms,
                error_detail=f"blacklist: {bl_hit}",
                full_response_text=restored_text,
            ),
        )

    usage = toolkit["extract_usage_json"](obj)
    assistant_msg = {
        "role": obj.get("role", "assistant"),
        "content": obj.get("content") or [],
    }
    return PreparedNonStreamResponse(
        obj=obj,
        restored=restored,
        restored_text=restored_text,
        usage=usage,
        assistant_msg=assistant_msg,
    )


OUTCOMES_NO_COOLDOWN = frozenset({
    "success",
    "http_auth_error",
    "transform_error",
    "guard_error",
    "candidate_guard",
    "request_invalid",
    "client_disconnected",
    "connection_lifecycle",
})


def should_cooldown(outcome: str) -> bool:
    return outcome not in OUTCOMES_NO_COOLDOWN


def should_record_failure(outcome: str) -> bool:
    """Whether an unsuccessful attempt should affect channel health scoring."""
    return outcome not in {
        "candidate_guard", "request_invalid", "client_disconnected",
        "connection_lifecycle",
    }


_HTTP_NO_RESPONSE_DISCONNECT_DETAIL = (
    "Server disconnected without sending a response."
)


def is_connection_lifecycle_error(
    exc: BaseException | None = None,
    *,
    http_status: int | None = None,
    http_phase: str | None = None,
    ws_close_code: int | None = None,
) -> bool:
    """Identify only typed, verified transport lifecycle outcomes.

    WebSocket close codes are an independent adapter-owned signal.  HTTP is
    deliberately narrower: only a typed httpx/httpcore RemoteProtocolError at
    the pre-header phase with the exact httpcore HTTP/1.1 no-response detail is
    health-neutral.  Protocol/framing errors and generic EOF-like exceptions
    remain transport failures.
    """
    if ws_close_code is not None:
        return int(ws_close_code) in {1000, 1001, 1006}
    if http_status is not None or http_phase != "pre_headers" or exc is None:
        return False

    candidates = (exc, exc.__cause__)
    return any(
        isinstance(candidate, (httpx.RemoteProtocolError, httpcore.RemoteProtocolError))
        and str(candidate) == _HTTP_NO_RESPONSE_DISCONNECT_DETAIL
        for candidate in candidates
        if candidate is not None
    )


def connection_lifecycle_outcome(
    exc: BaseException | None = None,
    *,
    http_status: int | None = None,
    http_phase: str | None = None,
    ws_close_code: int | None = None,
) -> str | None:
    return (
        "connection_lifecycle"
        if is_connection_lifecycle_error(
            exc,
            http_status=http_status,
            http_phase=http_phase,
            ws_close_code=ws_close_code,
        )
        else None
    )


DEFAULT_TRANSIENT_RETRY_DELAYS_S = (0.75, 1.75)
MAX_CONFIGURED_TRANSIENT_RETRIES = 5
MAX_RETRY_AFTER_SECONDS = 60.0


def retry_config(cfg: dict | None) -> dict:
    raw = (cfg if isinstance(cfg, dict) else {}).get("retry") or {}
    return raw if isinstance(raw, dict) else {}


def transient_retry_config(cfg: dict | None) -> dict:
    raw = retry_config(cfg).get("transient") or {}
    return raw if isinstance(raw, dict) else {}


def transient_retry_limit(cfg: dict | None) -> int:
    try:
        value = int(transient_retry_config(cfg).get("maxExtraAttempts", 2))
    except (TypeError, ValueError):
        value = 2
    return max(0, min(value, MAX_CONFIGURED_TRANSIENT_RETRIES))


def transient_retry_allowed(kind: str | None, cfg: dict | None) -> bool:
    if not kind:
        return False
    transient = transient_retry_config(cfg)
    if not bool(transient.get("enabled", True)):
        return False
    event_flags = transient.get("errors") or {}
    if not isinstance(event_flags, dict):
        event_flags = {}
    return bool(event_flags.get(kind, True))


def recovery_retry_allowed(name: str, cfg: dict | None) -> bool:
    recovery = retry_config(cfg).get("recovery") or {}
    if not isinstance(recovery, dict):
        recovery = {}
    return bool(recovery.get(name, True))


def configured_transient_retry_delays(cfg: dict | None) -> tuple[float, ...]:
    raw = transient_retry_config(cfg).get("backoffSeconds", DEFAULT_TRANSIENT_RETRY_DELAYS_S)
    if not isinstance(raw, (list, tuple)):
        raw = DEFAULT_TRANSIENT_RETRY_DELAYS_S
    parsed: list[float] = []
    for value in raw[:MAX_CONFIGURED_TRANSIENT_RETRIES]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        parsed.append(max(0.0, min(number, 60.0)))
    return tuple(parsed) or DEFAULT_TRANSIENT_RETRY_DELAYS_S


def parse_retry_after_seconds(
    value: Any,
    *,
    now_ts: float | None = None,
    max_seconds: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse delta-seconds or an HTTP-date and clamp it to a safe wait ceiling."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        delay = float(text)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay = parsed.timestamp() - (time.time() if now_ts is None else float(now_ts))
    if not math.isfinite(delay):
        return None
    try:
        ceiling = float(max_seconds)
    except (TypeError, ValueError):
        ceiling = MAX_RETRY_AFTER_SECONDS
    if not math.isfinite(ceiling) or ceiling < 0:
        ceiling = MAX_RETRY_AFTER_SECONDS
    return max(0.0, min(delay, ceiling))


def retry_after_cooldown_until(
    value: Any,
    *,
    now_ts: float | None = None,
) -> int | None:
    """Convert a valid bounded Retry-After value to an epoch-ms deadline."""
    base = time.time() if now_ts is None else float(now_ts)
    delay = parse_retry_after_seconds(value, now_ts=base)
    if delay is None:
        return None
    return int((base + delay) * 1000)


_RETRYABLE_TRANSIENT_OUTCOMES = frozenset({
    "http_error",
    "upstream_error_json",
    "stream_upstream_error",
})


def _is_xai_channel(channel: Any) -> bool:
    """Recognize direct xAI API/OAuth channels without guessing from model names."""
    if str(getattr(channel, "provider", "") or "").strip().lower() == "xai":
        return True
    try:
        host = (urlparse(str(getattr(channel, "base_url", "") or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "api.x.ai" or host.endswith(".api.x.ai") or host == "cli-chat-proxy.grok.com"


def _is_openai_channel(channel: Any) -> bool:
    provider = str(getattr(channel, "provider", "") or "").strip().lower()
    if provider == "openai" or str(getattr(channel, "key", "") or "").startswith("oauth:openai:"):
        return True
    return str(getattr(channel, "protocol", "") or "").strip().lower().startswith("openai-")


def _upstream_error_identity(result: AttemptResult) -> tuple[str, str, bool]:
    """Extract exact lower-case (type, code, structured) attempt identity."""
    detail = str(getattr(result, "error_detail", "") or "").strip()
    obj: dict = {}
    structured = False
    start = detail.find("{")
    if start >= 0:
        try:
            parsed = json.loads(detail[start:])
            if isinstance(parsed, dict):
                obj = parsed
                structured = True
        except Exception:
            obj = {}
    error = obj.get("error") if isinstance(obj.get("error"), dict) else obj
    if not isinstance(error, dict):
        error = {}
    error_type = str(error.get("type") or obj.get("type") or "").strip().lower()
    error_code = str(
        getattr(result, "error_code", None)
        or error.get("code")
        or obj.get("code")
        or ""
    ).strip().lower()
    if not error_type and not error_code:
        # Older WS normalization stored ``server_error: message`` rather than JSON.
        match = re.match(r"^([a-z][a-z0-9_]+)\s*:", detail, flags=re.IGNORECASE)
        if match:
            error_type = match.group(1).lower()
    return error_type, error_code, structured


def bounded_account_quota_error(result: AttemptResult) -> dict[str, str] | None:
    """Recognize only explicit account/billing exhaustion signals.

    HTTP 402 is authoritative balance exhaustion. Generic 403/429 responses
    deliberately remain unclassified so their existing OAuth refresh and retry
    behaviour is unchanged.
    """
    try:
        status = int(getattr(result, "http_status", None) or 0)
    except (TypeError, ValueError):
        return None
    if status not in (402, 403, 429):
        return None

    detail = str(getattr(result, "error_detail", "") or "").strip()[:4000]
    error_type, error_code, _ = _upstream_error_identity(result)
    message = detail
    start = detail.find("{")
    if start >= 0:
        try:
            obj = json.loads(detail[start:])
        except Exception:
            obj = None
        if isinstance(obj, dict):
            error_obj = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            if isinstance(error_obj, dict):
                message = str(error_obj.get("message") or obj.get("message") or detail)
    low = " ".join((error_type, error_code, message.lower(), detail.lower()))

    matched = status == 402
    if status == 429:
        matched = error_code in {
            "quota_exhausted",
            "insufficient_quota",
            "billing_hard_limit_reached",
            "billing_limit_reached",
            "billing_not_active",
            "credits_exhausted",
            "credit_balance_exhausted",
        } or error_type in {"insufficient_quota", "billing_error"}
        if not matched:
            matched = any(marker in low for marker in (
                "exceeded your current quota",
                "insufficient quota",
                "check your plan and billing details",
                "billing hard limit",
                "billing limit has been reached",
                "out of credits",
                "no credits remaining",
                "not enough credits",
                "credit balance is too low",
                "usage credits are required",
            ))
    elif status == 403:
        matched = error_code in {
            "quota_exhausted",
            "insufficient_quota",
            "billing_hard_limit_reached",
            "billing_limit_reached",
            "billing_not_active",
            "credits_exhausted",
            "credit_balance_exhausted",
        } or error_type in {"insufficient_quota", "billing_error"}
        if not matched:
            matched = any(marker in low for marker in (
                "used all credits",
                "used all your credits",
                "all credits have been used",
                "credits exhausted",
                "monthly spending limit",
                "monthly spend limit",
                "monthly budget has been reached",
            ))
    if not matched:
        return None
    return {
        "classification": "balance_exhausted" if status == 402 else "quota_exhausted",
        "code": error_code or error_type or ("payment_required" if status == 402 else ""),
        "message": message,
    }


def retryable_transient_error_kind(channel: Any, result: AttemptResult) -> str | None:
    """Classify the small allowlist of safe pre-commit same-candidate retries.

    Returned values are config keys under ``retry.transient.errors``.  Generic
    5xx/503 responses deliberately return ``None``.
    """
    if getattr(result, "success", False) or getattr(result, "stream_started", False):
        return None
    if getattr(result, "outcome", None) not in _RETRYABLE_TRANSIENT_OUTCOMES:
        return None
    try:
        status = int(getattr(result, "http_status", None) or 0)
    except (TypeError, ValueError):
        status = 0
    error_type, error_code, structured = _upstream_error_identity(result)

    # xAI's REST 503 is the counterpart of its SDK UNAVAILABLE signal.  Check it
    # before generic OpenAI-compatible ``server_error`` classification.
    if status == 503 and _is_xai_channel(channel):
        return "xaiUnavailable"
    if error_code == "server_is_overloaded" or (
        not structured and error_type == "server_is_overloaded"
    ):
        return "openaiServerOverloaded"
    if status == 529 or error_type == "overloaded_error" or error_code == "overloaded_error":
        return "claudeOverloaded"
    if _is_openai_channel(channel) and (
        error_code == "server_error"
        or (not structured and error_type == "server_error")
    ):
        return "openaiServerError"
    return None


def is_retryable_overload_error(channel: Any, result: AttemptResult) -> bool:
    """Backward-compatible overload predicate (excludes generic OpenAI server_error)."""
    return retryable_transient_error_kind(channel, result) in {
        "openaiServerOverloaded", "claudeOverloaded", "xaiUnavailable",
    }


def is_html_error_document(body: Any) -> bool:
    """Return whether a readable body begins with an HTML document marker.

    Provider/channel identity and HTTP status intentionally stay at call sites.
    Content-Type is deliberately irrelevant: mislabeled JSON must not match and
    HTML remains detectable when the header is absent or wrong.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return False
    elif isinstance(body, str):
        text = body
    else:
        return False
    prefix = text.lstrip("\ufeff\t\n\r\v\f ").casefold()
    return prefix.startswith("<!doctype html") or prefix.startswith("<html")


def failover_final_http_status(result: Any | None) -> int:
    """HTTP status for the final all-candidates-failed JSON response."""
    outcome = getattr(result, "outcome", None)
    if outcome in (
        "connection_timeout", "http_connect_timeout", "pool_timeout",
        "write_timeout", "read_timeout", "transport_timeout",
        "connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout",
    ):
        return 504
    if outcome in ("connect_error", "transport_error", "connection_lifecycle"):
        return 502
    if outcome == "candidate_guard":
        return int(getattr(result, "http_status", None) or 400)
    return 503


def upstream_ws_http_status_from_attempt(result: Any) -> int:
    """HTTP-style status for OpenAI OAuth Responses upstream WS attempts."""
    http_status = getattr(result, "http_status", None)
    if http_status is not None:
        return int(http_status)
    outcome = getattr(result, "outcome", None)
    if outcome in (
        "connection_timeout", "http_connect_timeout", "pool_timeout",
        "write_timeout", "read_timeout", "transport_timeout",
        "connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout",
    ):
        return 504
    if outcome in ("blacklist_hit", "upstream_error_json", "stream_upstream_error"):
        return 503
    if outcome in ("guard_error", "candidate_guard", "request_invalid"):
        return 400
    return 502


def responses_ws_http_status_from_attempt(result: Any | None) -> int:
    """HTTP-style status used by the downstream /v1/responses WS ingress."""
    if result is not None and getattr(result, "http_status", None):
        return int(getattr(result, "http_status"))
    outcome = getattr(result, "outcome", None) if result is not None else None
    if outcome in (
        "connection_timeout", "http_connect_timeout", "pool_timeout",
        "write_timeout", "read_timeout", "transport_timeout",
        "connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout",
    ):
        return 504
    if outcome in ("connect_error", "transport_error", "connection_lifecycle", "upstream_closed", "closed_before_first_byte"):
        return 502
    if outcome == "client_disconnected":
        return 499
    if outcome in ("guard_error", "candidate_guard", "request_invalid"):
        return 400
    return 503


def ws_close_code_for_http_status(status: int) -> int:
    if status == 400:
        return 4400
    if status == 401:
        return 4401
    if status == 403:
        return 4403
    if status == 404:
        return 4404
    if status == 429:
        return 4429
    if status == 504:
        return 4504
    return 4500 if status >= 500 else 4400


def format_responses_ws_error(evt: dict) -> str:
    """Return the legacy human-readable detail for a Responses WS error event."""
    if protocol_errors.is_responses_max_output_incomplete(evt):
        return protocol_errors.responses_max_output_context_error_message(
            protocol_errors.responses_incomplete_reason(evt)
        )
    err = evt.get("error")
    if isinstance(evt.get("response"), dict) and isinstance(evt["response"].get("error"), dict):
        err = evt["response"]["error"]
    if isinstance(err, dict):
        code = err.get("code") or err.get("type") or err.get("error_type")
        message = err.get("message") or err.get("reason") or "upstream websocket error"
        code_s, msg_s = protocol_errors.extract_error_info({"error": err}, fallback="upstream websocket error")
        if protocol_errors.is_context_length_code_or_message(code_s or code, msg_s):
            return protocol_errors.context_length_error_message_for_claude_code(msg_s)
        return f"{code}: {message}" if code and str(code) not in str(message) else str(message)
    message = evt.get("message") or evt.get("reason") or "upstream websocket error"
    code = evt.get("code") or evt.get("error_type") or evt.get("type")
    code_s, msg_s = protocol_errors.extract_error_info(evt, fallback="upstream websocket error")
    if protocol_errors.is_context_length_code_or_message(code_s or code, msg_s):
        return protocol_errors.context_length_error_message_for_claude_code(msg_s)
    return f"{code}: {message}" if code and code != "error" and str(code) not in str(message) else str(message)


def responses_ws_error_detail(data: str | bytes) -> tuple[Optional[int], str]:
    """Extract HTTP-ish status and short detail from a Responses WS error frame."""
    if not isinstance(data, str):
        return None, "upstream websocket error"
    try:
        obj = json.loads(data)
    except Exception:
        return None, data[:2000]
    if not isinstance(obj, dict):
        return None, str(obj)[:2000]

    if protocol_errors.is_responses_max_output_incomplete(obj):
        return 400, protocol_errors.responses_max_output_context_error_message(
            protocol_errors.responses_incomplete_reason(obj)
        )

    err: Any = obj.get("error")
    if isinstance(obj.get("response"), dict) and isinstance(obj["response"].get("error"), dict):
        err = obj["response"]["error"]

    status = obj.get("status")
    if isinstance(err, dict):
        status = err.get("status") or status
        detail = format_responses_ws_error(obj)
    else:
        msg = obj.get("message") or obj.get("reason") or json.dumps(obj, ensure_ascii=False)
        detail = str(msg)

    try:
        status_i = int(status) if status is not None else None
    except Exception:
        status_i = None
    return status_i, detail[:2000]


def parse_wrapped_responses_ws_error(text: str) -> Optional[dict]:
    """Parse a top-level Responses WS ``type:error`` frame before accept."""
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "error":
        return None
    status = obj.get("status") or obj.get("status_code")
    raw_err = obj.get("error")
    err = raw_err if isinstance(raw_err, dict) else obj
    code = err.get("code") or err.get("type") or obj.get("code")
    message = err.get("message") or obj.get("message") or text[:2000]
    return {"status": int(status) if isinstance(status, int) else None, "code": code, "message": message}


def is_retryable_responses_ws_error_before_accept(err: dict) -> bool:
    status = err.get("status")
    code = str(err.get("code") or "")
    if code == "websocket_connection_limit_reached":
        return True
    return isinstance(status, int) and status in (401, 403, 429, 500, 502, 503, 504)


def is_responses_ws_visible_event_type(event_type: str | None) -> bool:
    return is_responses_visible_event_type(event_type)


def is_invalid_encrypted_content_error(error_detail: Optional[str]) -> bool:
    """OpenAI/Codex encrypted_content validation failures are request-scoped."""
    low = str(error_detail or "").lower()
    if not low:
        return False
    return (
        "invalid_encrypted_content" in low
        or ("encrypted content" in low and (
            "could not be verified" in low
            or "could not be decrypted" in low
            or "could not be decrypted or parsed" in low
        ))
    )


_CONTEXT_OVERFLOW_HINT_RE = re.compile(
    r"context.*(?:overflow|too\s+(?:large|long)|exceed|limit|max(?:imum)?|tokens)"
    r"|context window.*(?:exceed|over|limit|max(?:imum)?|requested|sent|tokens)"
    r"|prompt.*(?:too\s+(?:large|long)|exceed|over|limit|max(?:imum)?)"
    r"|(?:request|input).*(?:context|window|length|token).*"
    r"(?:too\s+(?:large|long)|exceed|over|limit|max(?:imum)?)",
    re.IGNORECASE,
)


def _looks_like_rate_limit_or_quota(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "rate limit",
            "rate_limit",
            "requests per minute",
            "tokens per minute",
            "request per minute",
            "token per minute",
            "quota",
        )
    ) or re.search(r"\b(?:rpm|tpm)\b", text) is not None


def is_context_length_exceeded_error(error_detail: Optional[str]) -> bool:
    """Return True for provider/client errors meaning the prompt is too large."""
    raw = str(error_detail or "")
    low = raw.lower()
    if not low:
        return False

    # Groq and some OpenAI-compatible providers use token wording for TPM/RPM
    # rate limits. Those must remain ordinary upstream/rate-limit failures.
    if _looks_like_rate_limit_or_quota(low):
        return False

    precise_markers = (
        "context_length_exceeded",
        "context_window_exceeded",
        "model_context_window_exceeded",
        "request_too_large",
    )
    if any(marker in low for marker in precise_markers):
        return True

    direct_phrases = (
        "request exceeds the maximum size",
        "context length exceeded",
        "maximum context length",
        "prompt is too long",
        "prompt too long",
        "exceeds model context window",
        "model token limit",
        "exceed context limit",
        "exceeds the model's maximum context",
        "input is too long for this model",
        "input too long for the model",
        "input exceeds the maximum number of tokens",
    )
    if any(phrase in low for phrase in direct_phrases):
        return True

    has_request_size_exceeds = "request size exceeds" in low
    has_context_window = (
        "context window" in low
        or "context length" in low
        or "maximum context length" in low
    )
    if has_request_size_exceeds and has_context_window:
        return True
    if "input length" in low and "exceed" in low and "context" in low:
        return True
    if "max_tokens" in low and "exceed" in low and "context" in low:
        return True
    if "413" in low and "too large" in low:
        return True
    if any(phrase in raw for phrase in ("上下文过长", "上下文超出", "上下文长度超", "超出最大上下文", "请压缩上下文")):
        return True

    return bool(_CONTEXT_OVERFLOW_HINT_RE.search(raw))


def _request_invalid_status(result: AttemptResult) -> int:
    if isinstance(result.http_status, int) and 400 <= result.http_status < 500:
        return int(result.http_status)
    return 400


def _mark_request_invalid(result: AttemptResult, status: int) -> AttemptResult:
    result.outcome = "request_invalid"
    result.http_status = int(status)
    if result.response is None:
        result.stream_started = False
    return result


def _structured_request_invalid_error_info(
    result: AttemptResult,
) -> tuple[str | None, str] | None:
    """Return code/message only for an explicit structured request fault."""
    status = result.http_status
    if status is not None and status not in {200, 400, 409, 413, 422} and status < 500:
        return None
    raw = str(result.error_detail or "").strip()
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(raw[start:])
    except Exception:
        return None
    return protocol_errors.request_invalid_error_info(payload)


def request_invalid_result_if_needed(result: AttemptResult) -> AttemptResult:
    # Candidate-local guards retain their established alternate-candidate path.
    if result.outcome in {"candidate_guard", "guard_error"}:
        return result
    # Authentication, payment, and rate-limit statuses are authoritative and
    # cannot be stolen by generic body markers.
    if result.http_status in {401, 402, 403, 429}:
        return result
    request_error = _structured_request_invalid_error_info(result)
    if request_error is not None:
        result.error_code, result.error_detail = request_error
        if protocol_errors.is_context_length_code_or_message(
            result.error_code, result.error_detail,
        ):
            result.error_code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
            context_detail = str(result.error_detail or "")
            if protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE not in context_detail:
                context_detail = (
                    f"{protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE}: {context_detail}"
                )
            result.error_detail = protocol_errors.context_length_error_message_for_claude_code(
                context_detail,
            )
        return _mark_request_invalid(result, _request_invalid_status(result))
    # Text-only compatibility recognizers are limited to status-less and the
    # request-like HTTP statuses accepted above. Status alone is deliberately
    # insufficient: 400/409/413/422 are all used by compatible upstreams for
    # ambiguous channel/state/rate failures. In particular, ordinary 5xx
    # messages never become request faults without an explicit structured marker.
    status = result.http_status
    text_classification_allowed = status is None or status in {200, 400, 409, 413, 422}
    if text_classification_allowed and is_invalid_encrypted_content_error(result.error_detail):
        return _mark_request_invalid(result, _request_invalid_status(result))
    if text_classification_allowed and is_context_length_exceeded_error(result.error_detail):
        return _mark_request_invalid(result, _request_invalid_status(result))
    return result


def _strip_encrypted_content_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        removed = 0
        for k, v in value.items():
            if k == "encrypted_content":
                removed += 1
                continue
            if isinstance(v, list):
                new_list: list[Any] = []
                for item in v:
                    if isinstance(item, dict) and item.get("type") == "encrypted_content":
                        removed += 1
                        continue
                    new_item, n = _strip_encrypted_content_value(item)
                    removed += n
                    new_list.append(new_item)
                out[k] = new_list
                continue
            new_v, n = _strip_encrypted_content_value(v)
            removed += n
            out[k] = new_v
        return out, removed
    if isinstance(value, list):
        out = []
        removed = 0
        for item in value:
            if isinstance(item, dict) and item.get("type") == "encrypted_content":
                removed += 1
                continue
            new_item, n = _strip_encrypted_content_value(item)
            removed += n
            out.append(new_item)
        return out, removed
    return value, 0


def retry_body_without_encrypted_content(body: dict) -> tuple[dict, int]:
    """Deep-copy a request and recursively remove only encrypted content.

    Reasoning items themselves are portable once their opaque EC field is gone;
    summary/content/id and adjacent message/tool items must remain available to a
    failover candidate.
    """
    copied = copy.deepcopy(body)
    stripped, removed = _strip_encrypted_content_value(copied)
    return (stripped if isinstance(stripped, dict) else copied), removed


def is_context_1m_credit_error(result: AttemptResult, resolved_model: str, body: dict) -> bool:
    """Whether a context-1m entitlement error should retry without context-1m."""
    if result.http_status != 429:
        return False
    if result.outcome not in ("http_error", "upstream_error_json"):
        return False
    if "usage credits are required for long context requests" not in (result.error_detail or "").lower():
        return False
    from ..transform import cc_mimicry
    if not cc_mimicry.model_supports_context_1m(resolved_model):
        return False
    if body.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY) is True:
        return True
    return cc_mimicry.request_wants_context_1m(
        body,
        downstream_betas=body.get(cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY),
        original_model=body.get(cc_mimicry.PARROT_ORIGINAL_MODEL_KEY),
        resolved_model=resolved_model,
    )


def retry_body_without_context_1m(body: dict) -> dict:
    from ..transform import cc_mimicry
    retry_body = dict(body)
    retry_body[cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY] = False
    return retry_body
