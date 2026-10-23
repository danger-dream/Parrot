"""Structured request-log facts Telegram renders (logs_menu / ui.fmt_log_entry_*).

Round 3: every fact the TG list card and detail page show is exposed as a
typed field instead of pre-formatted text. Values are copied or derived with
the same formulas as Telegram (``cache_display``, ``ui.calc_row_tps``,
``ui.log_fast_mode_badge``, ``model_reroute``); nothing here invents data.
"""

from __future__ import annotations

import json
from typing import Any

from src import cache_display, model_reroute

from .common import sanitize_credentials


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tokens_per_second(row: dict) -> float | None:
    """Same formula as Telegram ``ui.calc_row_tps``."""
    if not row or row.get("status") != "success":
        return None
    out = _int(row.get("output_tokens"))
    total = _num(row.get("total_time_ms"))
    first = _num(row.get("first_token_time_ms"))
    if out <= 0 or total is None or total <= 0:
        return None
    if row.get("is_stream") and first is not None and total > first:
        return out * 1000.0 / (total - first)
    if not row.get("is_stream"):
        return out * 1000.0 / total
    return None


def speed_mode(row: dict) -> str | None:
    """``ultrafast`` / ``fast`` / ``tier`` (other explicit tier) / None — TG badge."""
    tier = str(row.get("actual_service_tier") or "").strip().lower()
    if tier == "ultrafast":
        return "ultrafast"
    if tier in {"priority", "fast"}:
        return "fast"
    if tier and tier not in {"auto", "default", "standard", "standard_only"}:
        return "tier"
    try:
        fast = bool(int(row.get("fast_mode") or 0))
    except (TypeError, ValueError):
        fast = bool(row.get("fast_mode"))
    return "fast" if fast else None


def websocket_leg(row: dict) -> str | None:
    """``ingress`` (WS) / ``upstream`` (↑WS) / None, as TG's transport tag."""
    ingress = str(row.get("ingress_protocol") or "")
    if ingress == "responses_ws":
        return "ingress"
    if ingress == "responses" and str(row.get("upstream_transport") or "").lower() == "ws":
        return "upstream"
    return None


def error_summary(raw: Any) -> str | None:
    """TG ``_extract_error_summary``: unwrap ``HTTP nnn: {json error}``."""
    if not raw:
        return None
    text = str(raw)
    prefix, json_part = "", text
    if text.startswith("HTTP "):
        index = text.find(": ")
        if index <= 0:
            return text
        prefix, json_part = text[:index], text[index + 2:]
    try:
        obj = json.loads(json_part)
    except Exception:
        return text
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict) and err.get("message"):
        summary = f"{err.get('type')}: {err['message']}" if err.get("type") else str(err["message"])
        return f"{prefix} — {summary}" if prefix else summary
    return f"{prefix} — {json_part}" if prefix else json_part


def upstream_observation(row: dict) -> dict[str, Any]:
    conflict = model_reroute.decode_model_conflict(row.get("model_signal_conflict"))
    review = model_reroute.decode_safety_review(row.get("safety_review"))
    actual = str(row.get("upstream_actual_model") or "").strip() or None
    return {
        "upstreamActualModel": actual,
        "modelSignalConflict": ({
            "httpHeader": [str(v)[:128] for v in conflict.get("http_header") or []],
            "eventHeaders": [str(v)[:128] for v in conflict.get("event_headers") or []],
            "bodyModels": [str(v)[:128] for v in conflict.get("body_models") or []],
        } if conflict else None),
        "safetyReview": ({
            "flagged": True,
            "reason": model_reroute.safety_review_reason_text(review) or None,
        } if review is not None else None),
    }


def list_fields(row: dict, *, channel: tuple[str | None, str | None] = (None, None)) -> dict[str, Any]:
    """Fields of one TG list card beyond the round-1 base record."""
    clean = sanitize_credentials(dict(row))
    prompt = cache_display.prompt_total_from_row(clean)
    cache_read = _int(clean.get("cache_read_tokens"))
    tps = tokens_per_second(clean)
    return {
        "httpStatus": _int(clean.get("http_status")) or None,
        "channelName": channel[0],
        "channelProvider": channel[1],
        "channelType": clean.get("final_channel_type") or None,
        "ingressProtocol": clean.get("ingress_protocol") or None,
        "upstreamProtocol": clean.get("upstream_protocol") or None,
        "websocket": websocket_leg(clean),
        "isStream": bool(clean.get("is_stream")),
        "cacheCreationTokens": _int(clean.get("cache_creation_tokens")),
        "cacheReadTokens": cache_read,
        "promptTokens": prompt,
        "cacheReadRatio": (cache_read / prompt) if prompt > 0 else None,
        "connectMilliseconds": _num(clean.get("connect_time_ms")),
        "firstTokenMilliseconds": _num(clean.get("first_token_time_ms")),
        "idleMilliseconds": _num(clean.get("idle_time_ms")),
        "tokensPerSecond": round(tps, 3) if tps is not None else None,
        "reasoningEffort": clean.get("reasoning_effort") or None,
        "serviceTier": clean.get("actual_service_tier") or None,
        "speedMode": speed_mode(clean),
        "affinityHit": bool(clean.get("affinity_hit")),
        "proxyName": clean.get("proxy_name") or None,
        "proxyBytesUp": _int(clean.get("proxy_bytes_up")) or None,
        "proxyBytesDown": _int(clean.get("proxy_bytes_down")) or None,
        "localSearchCount": _int(clean.get("local_web_count")),
        "usageObserved": (None if clean.get("usage_observed") is None else bool(clean.get("usage_observed"))),
        "errorSummary": error_summary(clean.get("error_message")),
        **upstream_observation(clean),
    }


_STAGE_KEYS = (
    ("dns", "dns_ms"), ("tcp", "tcp_ms"), ("proxyTcp", "proxy_tcp_ms"),
    ("proxyTunnel", "proxy_tunnel_ms"), ("targetTls", "target_tls_ms"), ("tls", "tls_ms"),
    ("wsHandshake", "ws_handshake_ms"), ("requestUpload", "request_upload_ms"),
    ("responseHeadersWait", "response_headers_wait_ms"),
    ("responseBodyFirstByteWait", "response_body_first_byte_wait_ms"),
)


def detail_fields(row: dict) -> dict[str, Any]:
    """Extra facts only the TG detail page shows (请求总览 / 客户端)."""
    clean = sanitize_credentials(dict(row))
    stages = {name: _num(clean.get(key)) for name, key in _STAGE_KEYS if clean.get(key) is not None}
    if "targetTls" in stages:
        stages.pop("tls", None)  # TG shows one reliable TLS phase only
    return {
        "clientIp": clean.get("client_ip") or None,
        "finalRoundId": clean.get("final_round_id") or None,
        "requestElapsedMilliseconds": _num(clean.get("request_elapsed_ms")),
        "stageMilliseconds": stages,
    }


class ChannelNames:
    """Per-call memo of TG's ``channel_display_name(with_family=False)`` facts.

    Returns ``(name, provider)``; the provider is the raw id (TG shows its
    label next to the name). Lookups never fail a log read.
    """

    def __init__(self, oauth_manager: Any, registry: Any = None) -> None:
        self._oauth = oauth_manager
        self._registry = registry
        self._memo: dict[str, tuple[str | None, str | None]] = {}
        self._openai_emails: dict[str, int] | None = None

    def _email_counts(self) -> dict[str, int]:
        if self._openai_emails is None:
            counts: dict[str, int] = {}
            try:
                for item in self._oauth.list_accounts():
                    if self._oauth.provider_of(item) == "openai":
                        email = str(item.get("email") or "")
                        counts[email] = counts.get(email, 0) + 1
            except Exception:
                pass
            self._openai_emails = counts
        return self._openai_emails

    def __call__(self, channel_key: Any) -> tuple[str | None, str | None]:
        key = str(channel_key or "")
        if not key:
            return None, None
        if key not in self._memo:
            try:
                self._memo[key] = self._resolve(key)
            except Exception:
                self._memo[key] = (None, None)
        return self._memo[key]

    def _resolve(self, key: str) -> tuple[str | None, str | None]:
        if key.startswith("api:"):
            provider = None
            if self._registry is not None:
                channel = self._registry.get_channel(key)
                provider = getattr(channel, "provider_id", None) if channel is not None else None
            return key.split(":", 1)[1], (str(provider) if provider else None)
        if not key.startswith("oauth:"):
            return (key.split(":", 1)[1] if ":" in key else key), None
        account_key = key[len("oauth:"):]
        account = self._oauth.get_account(account_key)
        if account is None:
            provider = self._oauth.provider_of(account_key)
            return (self._oauth.account_key_to_email(account_key) or None), (provider or None)
        provider = self._oauth.provider_of(account)
        if provider in {"workbuddy", "zhipu", "qoder"}:
            name = account.get("label") or account.get("nickname") or account.get("email") or account.get("uid")
        elif provider == "cursor":
            name = account.get("label") or account.get("email")
        else:
            name = account.get("email")
        name = str(name or "") or None
        if provider == "openai" and name and self._email_counts().get(name, 0) > 1:
            workspace = (account.get("workspace_name") or account.get("workspace_type")
                         or account.get("plan_type") or "workspace")
            name = f"{name} · {workspace}"
        return name, (provider or None)
