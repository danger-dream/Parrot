"""Upstream-reported model changes, conflicting signals and safety review.

Header and safety signal definitions are informed by the official Codex client
(``codex-rs/codex-api/src/sse/responses.rs`` and
``codex-rs/core/src/session/mod.rs``):

* **Effective model** — the ``openai-model`` (alias ``x-openai-model``) value.
  It arrives as an HTTP response header, or embedded in a stream event as
  ``response.headers`` / top-level ``headers``. The embedded value takes
  precedence over the HTTP header. A case-insensitive inequality with the
  outbound model reports a model change, not proof of lower capability.
* **Safety review** — an event-level ``safety_buffering`` object, or a
  ``response.metadata`` event whose ``metadata.type == "safety_buffering"``
  (fields ``use_cases`` / ``reasons`` / ``retry_model``). The
  ``x-codex-safety-buffering-*`` response headers are an account-level
  treatment that is present on every response; they are *not* a signal.
* **Body model** — the terminal response object's ``model`` field. It is the
  fallback effective model when no header is present. OpenAI API channels
  answer with a dated snapshot name here, so for them the fact is rendered as
  "与调用模型不一致" rather than "模型降级".

Parrot also retains body-model fallback. OpenAI OAuth conflicting model facts
are reported separately, without claiming one is the actual model.
Only OpenAI OAuth (Codex) and OpenAI-protocol API channels are observed.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import model_pricing

_MODEL_HEADER_NAMES = frozenset({"openai-model", "x-openai-model"})
_OPENAI_PROTOCOLS = frozenset({"openai-chat", "openai-responses"})
_OPENAI_OAUTH_PREFIX = "oauth:openai:"

NOTIFY_EVENT_KEY = "model_degraded"
NOTIFY_COOLDOWN_SECONDS = 2 * 3600
MUTE_DOMAIN = "model_reroute_mutes"


# ─── Signal extraction ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResponseModelSignals:
    """Facts extracted from one logged upstream response body."""

    header_model: str | None = None
    body_model: str | None = None
    safety_review: dict[str, Any] | None = None
    header_models: tuple[str, ...] = ()
    body_models: tuple[str, ...] = ()


def _distinct_models(*groups) -> tuple[str, ...]:
    """Retain a small set of observed values; case-only differences agree."""
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            if not isinstance(raw, str) or not raw.strip():
                continue
            value = raw.strip()
            if value.casefold() not in seen:
                seen.add(value.casefold())
                values.append(value)
                if len(values) >= 8:
                    return tuple(values)
    return tuple(values)


def _header_value(headers: Any, names: frozenset[str]) -> str | None:
    try:
        items = headers.items()
    except Exception:
        return None
    for raw_name, raw_value in items:
        if str(raw_name).lower() not in names:
            continue
        value = str(raw_value or "").strip()
        if value and len(value) <= 512 and "\r" not in value and "\n" not in value:
            return value
    return None


def header_model(headers: Any) -> str | None:
    """Return the effective model reported through HTTP response headers."""
    return _header_value(headers, _MODEL_HEADER_NAMES)


def _event_header_model(obj: Mapping[str, Any]) -> str | None:
    response = obj.get("response")
    if isinstance(response, Mapping):
        value = _header_value(response.get("headers"), _MODEL_HEADER_NAMES)
        if value:
            return value
    return _header_value(obj.get("headers"), _MODEL_HEADER_NAMES)


def _terminal_model(obj: Mapping[str, Any]) -> str | None:
    response = obj.get("response")
    if isinstance(response, Mapping):
        model = response.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    if "type" in obj:
        # Responses events carry the model only inside ``response``.
        return None
    model = obj.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip() if isinstance(item, (str, int, float)) else ""
        if text and text not in out:
            out.append(text[:80])
    return out[:8]


def _safety_review(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    payload: Any = obj.get("safety_buffering")
    if not isinstance(payload, Mapping):
        payload = None
        if str(obj.get("type") or "") == "response.metadata":
            metadata = obj.get("metadata")
            if isinstance(metadata, Mapping) and metadata.get("type") == "safety_buffering":
                payload = metadata
    if payload is None:
        return None
    review: dict[str, Any] = {}
    use_cases = _string_list(payload.get("use_cases"))
    reasons = _string_list(payload.get("reasons"))
    if use_cases:
        review["use_cases"] = use_cases
    if reasons:
        review["reasons"] = reasons
    retry_model = payload.get("retry_model")
    if isinstance(retry_model, str) and retry_model.strip():
        review["retry_model"] = retry_model.strip()[:128]
    return review


def _merge_review(base: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base or {})
    for key in ("use_cases", "reasons"):
        values = list(merged.get(key) or [])
        for item in extra.get(key) or []:
            if item not in values:
                values.append(item)
        if values:
            merged[key] = values[:8]
    if extra.get("retry_model"):
        merged["retry_model"] = extra["retry_model"]
    return merged


def merge_response_signals(
    base: ResponseModelSignals, extra: ResponseModelSignals,
) -> ResponseModelSignals:
    """Accumulate compact facts before display-body truncation; later models win."""
    return ResponseModelSignals(
        header_model=extra.header_model or base.header_model,
        body_model=extra.body_model or base.body_model,
        safety_review=(
            _merge_review(base.safety_review, extra.safety_review)
            if extra.safety_review is not None else base.safety_review
        ),
        header_models=_distinct_models(base.header_models, (base.header_model,), extra.header_models, (extra.header_model,)),
        body_models=_distinct_models(base.body_models, (base.body_model,), extra.body_models, (extra.body_model,)),
    )


def extract_response_signals(response_body: Any) -> ResponseModelSignals:
    """Extract effective-model and safety-review facts from a logged body.

    Accepts the same shapes as billing normalization: one JSON object, SSE
    ``data:`` lines, or one WS JSON frame per line. Later objects win for the
    model fields because the terminal event is the last one emitted.
    """
    header: str | None = None
    body: str | None = None
    review: dict[str, Any] | None = None
    headers: tuple[str, ...] = ()
    bodies: tuple[str, ...] = ()
    for obj in model_pricing._strict_response_objects(response_body):
        if obj.get("_parrot_truncated_billing_evidence"):
            continue
        value = _event_header_model(obj)
        if value:
            header = value
            headers = _distinct_models(headers, (value,))
        # Preserve a conflicting top-level header as evidence, without changing
        # response.headers precedence for ordinary non-conflicting responses.
        headers = _distinct_models(headers, (_header_value(obj.get("headers"), _MODEL_HEADER_NAMES),))
        value = _terminal_model(obj)
        if value:
            body = value
            bodies = _distinct_models(bodies, (value,))
        found = _safety_review(obj)
        if found is not None:
            review = _merge_review(review, found)
    return ResponseModelSignals(
        header_model=header, body_model=body, safety_review=review,
        header_models=headers, body_models=bodies,
    )


def models_differ(outbound: Any, actual: Any) -> bool:
    """Codex comparison rule: case-insensitive exact match, no fuzzy matching."""
    sent = str(outbound or "").strip().lower()
    got = str(actual or "").strip().lower()
    return bool(sent and got and sent != got)


def is_openai_oauth_channel(channel_key: Any) -> bool:
    return str(channel_key or "").startswith(_OPENAI_OAUTH_PREFIX)


def observes_channel(channel_key: Any, channel_type: Any, upstream_protocol: Any) -> bool:
    """Only OpenAI OAuth and OpenAI-protocol API channels are observed."""
    if is_openai_oauth_channel(channel_key):
        return True
    return (
        str(channel_type or "") == "api"
        and str(upstream_protocol or "").strip().lower() in _OPENAI_PROTOCOLS
    )


def resolve_actual_model(
    *,
    outbound_model: Any,
    signals: ResponseModelSignals,
    http_header_model: str | None = None,
) -> str | None:
    """Return the effective model only when it differs from the outbound one."""
    actual = signals.header_model or http_header_model or signals.body_model
    if actual and models_differ(outbound_model, actual):
        return actual[:128]
    return None


def model_conflict(signals: ResponseModelSignals, http_header_model: str | None = None) -> dict | None:
    """Report contradictory upstream facts, never compare against request intent."""
    groups = {
        "http_header": _distinct_models((http_header_model,)),
        "event_headers": _distinct_models(signals.header_models, (signals.header_model,)),
        "body_models": _distinct_models(signals.body_models, (signals.body_model,)),
    }
    if len(_distinct_models(*groups.values())) < 2:
        return None
    return {key: list(values) for key, values in groups.items() if values}


def decode_model_conflict(raw: Any) -> dict | None:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(value, Mapping):
        return None
    groups = {
        key: list(_distinct_models(value[key]))
        for key in ("http_header", "event_headers", "body_models")
        if isinstance(value.get(key), (list, tuple))
    }
    return groups if len(_distinct_models(*groups.values())) > 1 else None


def model_conflict_lines(conflict: Mapping) -> list[str]:
    labels = {"http_header": "HTTP 模型头", "event_headers": "事件模型头", "body_models": "正文模型"}
    return [
        f"{label}: " + " → ".join(str(model)[:128] for model in conflict[key])
        for key, label in labels.items() if conflict.get(key)
    ]


def encode_safety_review(review: dict[str, Any] | None) -> str | None:
    if review is None:
        return None
    return json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_safety_review(raw: Any) -> dict[str, Any] | None:
    """Return the stored review dict, or ``None`` when the row is not flagged."""
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


# ─── Display ────────────────────────────────────────────────────────────────


def reroute_label(channel_type: Any) -> str:
    return "上游模型变更" if str(channel_type or "") == "oauth" else "与调用模型不一致"


SAFETY_REVIEW_TEXT = "本次请求被标记为需要额外安全审查（响应可能较慢）"


def safety_review_reason_text(review: dict[str, Any] | None) -> str:
    """Plain-text reasons for the detail page; empty when upstream sent none."""
    if not review:
        return ""
    parts: list[str] = []
    if review.get("use_cases"):
        parts.append("场景 " + " / ".join(str(v) for v in review["use_cases"]))
    if review.get("reasons"):
        parts.append("原因 " + " / ".join(str(v) for v in review["reasons"]))
    return " · ".join(parts)


# ─── Per-channel mute (state store) ─────────────────────────────────────────


def channel_code(channel_key: Any) -> str:
    """Stable 8-hex code so callback_data never carries a raw channel key."""
    return hashlib.sha1(str(channel_key or "").encode("utf-8")).hexdigest()[:8]


def _mute_key(code: str) -> str:
    return str(code)


def mute_channel(channel_key: str | None, *, code: str | None = None, days: int) -> int:
    """Mute reroute notifications for one channel; returns the expiry epoch.

    The mute is a preference, not a durable fact the caller must act on: when the
    store is unavailable the press is acknowledged with the requested expiry and
    the suppression simply does not take effect. The button must not depend on
    store availability, so its reply is deterministic.
    """
    from . import state_db

    resolved_code = code or channel_code(channel_key)
    until = int(time.time()) + int(days) * 86400
    row = {
        "code": resolved_code,
        "channel_key": str(channel_key or ""),
        "muted_at": int(time.time()),
        "until": until,
    }
    try:
        state_db._mut(MUTE_DOMAIN, lambda d: d.__setitem__(_mute_key(resolved_code), row), strict=True)
    except Exception:
        pass
    return until


def unmute_channel(*, code: str) -> bool:
    from . import state_db

    try:
        return bool(state_db._mut(MUTE_DOMAIN, lambda d: d.pop(_mute_key(code), None) is not None, strict=True))
    except Exception:
        return False


def muted_until(channel_key: str) -> int | None:
    """Return the active mute expiry for ``channel_key`` (expired rows are dropped)."""
    from . import state_db

    code = channel_code(channel_key)
    try:
        row = state_db._get(MUTE_DOMAIN, _mute_key(code))
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    until = int(row.get("until") or 0)
    if until > int(time.time()):
        return until
    # Expired: drop lazily so the durable snapshot is only rewritten on change.
    try:
        state_db._mut(MUTE_DOMAIN, lambda d: d.pop(_mute_key(code), None), strict=False)
    except Exception:
        pass
    return None


# ─── Notification ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RerouteObservation:
    request_id: str
    api_key_name: str
    requested_model: str
    outbound_model: str
    actual_model: str
    channel_key: str
    channel_type: str
    observed_at: float = field(default_factory=time.time)
    model_conflict: dict[str, Any] | None = None


def build_mute_buttons(channel_key: str) -> dict:
    code = channel_code(channel_key)
    return {
        "inline_keyboard": [
            [
                {"text": "🔕 不再提醒（今天）", "callback_data": f"sys:mdg_mute:1:{code}"},
                {"text": "🔕 不再提醒（7 天）", "callback_data": f"sys:mdg_mute:7:{code}"},
            ],
            [{"text": "🚫 禁用提醒（永久）", "callback_data": "sys:mdg_off"}],
        ]
    }


def build_unmute_buttons(code: str) -> dict:
    return {"inline_keyboard": [[{"text": "🔔 取消静音", "callback_data": f"sys:mdg_unmute:{code}"}]]}


def _account_label(channel_key: str) -> str:
    try:
        from . import oauth_manager

        account_key = channel_key[len("oauth:"):] if channel_key.startswith("oauth:") else ""
        acc = oauth_manager.get_account(account_key) if account_key else None
        if isinstance(acc, dict):
            return str(acc.get("email") or acc.get("label") or account_key)
        return oauth_manager.account_key_to_email(account_key) if account_key else channel_key
    except Exception:
        return channel_key


def format_notification(obs: RerouteObservation) -> str:
    from . import notifier

    ek = notifier.escape_html
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(obs.observed_at))
    requested = obs.requested_model or obs.outbound_model
    lines = [
        "⚠️ <b>模型信息不一致</b>" if obs.model_conflict else "⚠️ <b>上游模型变更</b>",
        f"时间: <code>{ek(when)}</code>",
        f"API Key: <code>{ek(obs.api_key_name or '?')}</code>",
        f"请求模型: <code>{ek(requested)}</code>",
    ]
    lines.append(f"出站模型: <code>{ek(obs.outbound_model)}</code>")
    if obs.model_conflict:
        lines.extend(f"<code>{ek(line)}</code>" for line in model_conflict_lines(obs.model_conflict))
        lines.append("上游模型信号相互矛盾，无法据此确定实际模型或是否降级。")
    else:
        lines.append(f"上游报告模型: <code>{ek(obs.actual_model)}</code>")
    lines.append(
        f"账号: {notifier.provider_custom_emoji_html('openai')} <code>{ek(_account_label(obs.channel_key))}</code>"
    )
    return "\n".join(lines)


def notify_reroute(obs: RerouteObservation) -> bool:
    """Send the TG reroute alert for an OpenAI OAuth channel.

    API-channel mismatches are recorded in the log but never notified: dated
    snapshot names would fire on every request. Per-channel mutes and the
    (channel, model pair) 2-hour throttle apply before the event switch.
    """
    if not is_openai_oauth_channel(obs.channel_key):
        return False
    if muted_until(obs.channel_key) is not None:
        return False
    from . import notifier

    alert_key = f"model_reroute:{obs.channel_key}:{obs.outbound_model}->{obs.actual_model}".lower()
    if obs.model_conflict:
        values = sorted({model.casefold() for group in obs.model_conflict.values() for model in group})
        signature = hashlib.sha256(json.dumps(values).encode()).hexdigest()[:16]
        alert_key = f"model_conflict:{obs.channel_key}:{obs.outbound_model}:{signature}".lower()
    return notifier.throttled_notify_event_sync(
        NOTIFY_EVENT_KEY,
        alert_key,
        format_notification(obs),
        cooldown_seconds=NOTIFY_COOLDOWN_SECONDS,
        reply_markup=build_mute_buttons(obs.channel_key),
    )


def on_log_observation(payload: Mapping[str, Any]) -> None:
    """``log_db`` listener: called after a reroute fact is persisted."""
    try:
        obs = RerouteObservation(
            request_id=str(payload.get("request_id") or ""),
            api_key_name=str(payload.get("api_key_name") or ""),
            requested_model=str(payload.get("requested_model") or ""),
            outbound_model=str(payload.get("final_model") or ""),
            actual_model=str(payload.get("upstream_actual_model") or ""),
            channel_key=str(payload.get("final_channel_key") or ""),
            channel_type=str(payload.get("final_channel_type") or ""),
            model_conflict=decode_model_conflict(payload.get("model_signal_conflict")),
        )
        if obs.actual_model or obs.model_conflict:
            notify_reroute(obs)
    except Exception as exc:  # notification is auxiliary; never break logging
        print(f"[model_reroute] notify failed: {type(exc).__name__}: {exc}")
