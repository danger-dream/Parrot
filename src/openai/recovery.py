"""Response-side recovery facts shared by HTTP, SSE and native Responses WS.

This module does not retry, mutate account state, or choose another model. The
owning request loop applies these facts within its existing attempt budget.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any


QUOTA_CODES = frozenset({
    "insufficient_quota", "credit_balance_exhausted", "credits_exhausted",
    "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded", "billing_hard_limit_reached",
    "billing_limit_reached", "quota_exhausted",
})
WS_RESET_CODES = frozenset({
    "websocket_connection_limit_reached", "previous_response_not_found",
})
RATE_CODES = frozenset({"rate_limit_exceeded", "slow_down"})
_DELAY_RE = re.compile(r"(?:please\s+)?try again in\s+(\d+(?:\.\d+)?)\s*(ms|seconds?|s)\b", re.I)


@dataclass(frozen=True)
class ResponseErrorAdvice:
    code: str = ""
    kind: str = ""
    active_limit: str | None = None
    reset_at: int | None = None
    retry_at: float | None = None  # monotonic: never restarted by another layer
    cooldown_until: int | None = None  # epoch ms, for the existing scheduler
    observed_at: float = 0.0
    quota_snapshot: dict | None = None


def error_payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return {}
    try:
        obj = json.loads(value[value.index("{"):])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        # Ledgers may contain a complete SSE/WS transcript. Only a terminal
        # error frame supplies advice; successful metadata isn't an error.
        for line in reversed(value.splitlines()):
            text = line.removeprefix("data:").strip()
            if not text.startswith("{"):
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") in {"error", "response.failed"}:
                return obj
        return {}


def retry_delay(value: Any, *, now: float | None = None) -> float | None:
    """Parse the server interval, independently of our maximum *wait* budget."""
    if value is None or isinstance(value, bool):
        return None
    try:
        delay = float(str(value).strip())
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(str(value).strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delay = dt.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return None
    # Reject nonsensical advice; never shorten an accepted upstream restriction.
    if not math.isfinite(delay) or delay > 366 * 86400:
        return None
    return max(0.0, delay)


def capture_error_advice(result, *, headers=None, payload=None, codex: bool = False):
    """Capture once at receipt; WS error-frame headers override handshake headers."""
    old = getattr(result, "error_advice", None)
    now = time.time()
    mono = time.monotonic()
    obj = error_payload(payload)
    if not obj:
        for value in (getattr(result, "full_response_text", None),
                      getattr(result, "response_text", None), getattr(result, "error_detail", None)):
            obj = error_payload(value)
            if obj:
                break
    response = obj.get("response") if isinstance(obj.get("response"), dict) else obj
    err = response.get("error") if isinstance(response.get("error"), dict) else response
    code = str(err.get("code") or err.get("type")
               or getattr(result, "error_code", None) or "").lower()
    if code == "error" or code.startswith("response."):
        code = ""
    error_type = str(err.get("type") or "").lower()
    if not code and old is not None:
        code = old.code
    # websockets.Headers.items() raises on legal repeated Set-Cookie values.
    items = headers.raw_items() if hasattr(headers, "raw_items") else (headers or {}).items()
    flat = {str(k).lower(): v for k, v in items}
    frame_headers = obj.get("headers")
    if isinstance(frame_headers, dict):
        flat.update({str(k).lower(): v for k, v in frame_headers.items()})
    kind = ""
    identities = {code, error_type}
    if "usage_limit_reached" in identities:
        kind = "usage_limit"
    elif identities & QUOTA_CODES:
        kind = "quota"
    elif "usage_not_included" in identities:
        kind = "entitlement"
    elif identities & RATE_CODES:
        kind = "rate_limit"
    elif code in WS_RESET_CODES:
        kind = "ws_reset"
    elif old is not None:
        kind = old.kind
    advice = old or ResponseErrorAdvice(observed_at=now)
    advice = replace(advice, code=code or advice.code, kind=kind)
    if code:
        result.error_code = code
    status = obj.get("status") or obj.get("status_code") or err.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        result.http_status = status
    elif (kind in {"usage_limit", "quota", "entitlement", "rate_limit"}
          and getattr(result, "http_status", None) in (None, 101, 200)):
        result.http_status = 429

    delay = retry_delay(flat.get("retry-after"), now=now)
    if delay is None and kind == "rate_limit":
        match = _DELAY_RE.search(str(err.get("message") or ""))
        if match:
            delay = retry_delay(float(match[1]) / (1000 if match[2].lower() == "ms" else 1), now=now)
    # Advice captured from a frame wins over an earlier handshake, but a second
    # pass through the owning loop must not restart the same frame's countdown.
    if advice.retry_at is None and delay is not None:
        advice = replace(advice, retry_at=mono + delay,
                         cooldown_until=int((now + delay) * 1000))
    if advice.retry_at is not None:
        result.retry_after_seconds = max(0.0, advice.retry_at - mono)
        if getattr(result, "http_status", None) == 429:
            result.cooldown_until = advice.cooldown_until

    if codex:
        from ..oauth import openai as provider
        incoming_active = str(flat.get("x-codex-active-limit") or "").strip().lower().replace("-", "_") or None
        active = ((incoming_active if isinstance(frame_headers, dict) else None)
                  or advice.active_limit or incoming_active)
        snapshot = provider.parse_rate_limit_headers(flat) if flat else None
        reset = provider.parse_codex_reset_at(err.get("resets_at"), observed_at=now)
        if reset is None and kind == "usage_limit" and snapshot:
            family_id = active or "codex"
            for family in snapshot.get("rate_limits") or []:
                if family.get("limit_id") != family_id:
                    continue
                candidates = [w.get("reset_at") or (
                    int(now + w["reset_after_seconds"]) if w.get("reset_after_seconds") is not None else None
                ) for name in ("primary", "secondary")
                    if isinstance(w := family.get(name), dict)
                    and w.get("used_percent") is not None and w["used_percent"] >= 100]
                valid = [provider.parse_codex_reset_at(x, observed_at=now) for x in candidates]
                reset = max((x for x in valid if x is not None and x > now), default=None)
        advice = replace(advice, active_limit=active, reset_at=reset or advice.reset_at,
                         quota_snapshot=(snapshot if isinstance(frame_headers, dict) else None)
                         or advice.quota_snapshot or snapshot)
    result.error_advice = advice
    return result


def remaining_retry_delay(result) -> float | None:
    advice = getattr(result, "error_advice", None)
    if advice is not None and advice.retry_at is not None:
        return max(0.0, advice.retry_at - time.monotonic())
    return retry_delay(getattr(result, "retry_after_seconds", None))


def rebuild_full_request(body: dict, *, api_key_name: str, channel_key: str,
                         model: str, previous: tuple[str, dict, list] | None = None) -> dict | None:
    """Recover only complete, same-owner history; never delete an opaque anchor."""
    # Internal identity contexts own locks/leases; only wire fields are copied.
    full = {key: value if str(key).startswith("_") else copy.deepcopy(value)
            for key, value in body.items()}
    anchor = str(full.get("previous_response_id") or "")
    if not anchor:
        return full
    if previous is not None and anchor == previous[0]:
        prior = previous[1]
        if any(key not in full and prior.get(key) for key in ("instructions", "tools")):
            return None
        prefix = prior.get("input") or []
        if isinstance(prefix, str):
            prefix = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": prefix}]}]
        current_items = full.get("input") or []
        if isinstance(current_items, str):
            current_items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": current_items}]}]
        if not isinstance(prefix, list) or not isinstance(current_items, list):
            return None
        from .transform.responses_to_chat import resolve_item_references
        from .transform.guard import GuardError
        try:
            full["input"] = resolve_item_references(copy.deepcopy(prefix + previous[2] + current_items))
        except GuardError:
            # An upstream-resolvable reference need not be in our local cache.
            # Incomplete recovery evidence must not abort an otherwise valid turn.
            return None
        full.pop("previous_response_id", None)
        return full
    # Continuations may inherit these fields at the server. Our items store is
    # not a store of request settings, so it cannot guess omitted instructions/tools.
    if "instructions" not in full or "tools" not in full:
        return None
    from . import store
    from .transform.responses_to_chat import resolve_item_references
    from .. import channel_state
    if not store.is_enabled():
        return None
    chain = []
    seen = set()
    current = anchor
    try:
        while current:
            if current in seen or len(chain) >= 50:
                return None
            seen.add(current)
            rec = store.lookup(current, api_key_name=api_key_name)
            if (channel_state.resolve(rec.channel_key or "") != channel_state.resolve(channel_key)
                    or rec.model != model):
                return None
            chain.append(rec)
            current = rec.parent_id
        items = []
        for rec in reversed(chain):
            items.extend(rec.input_items)
            items.extend(rec.output_items)
        current_items = full.get("input") or []
        if isinstance(current_items, str):
            current_items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": current_items}]}]
        if not isinstance(current_items, list):
            return None
        full["input"] = resolve_item_references(items + current_items)
    except Exception:
        # Store availability/expiry/ownership failures preserve the original error.
        return None
    full.pop("previous_response_id", None)
    return full
