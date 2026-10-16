"""Antigravity exact-model quota gates, separate from display-only quota groups.

Catalog observations are consumed only by the successful discovery commit, never
by scheduling or usage-cache reads. Request refusals own a different marker and
cannot be recovered by a model listing. No group/family mapping is inferred.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Any

from ..providers.antigravity_errors import parse_antigravity_429, parse_google_duration

CATALOG_PAUSE = "antigravity_catalog_quota"
REQUEST_PAUSE = "antigravity_model_429"
MAX_RESET_SECONDS = 45 * 24 * 3600
CATALOG_FRESH_MS = 5 * 60_000


def normalize_quota_info(raw: Any) -> dict:
    """Keep only validated, documented model quota fields (not opaque metadata)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    fraction = raw.get("remainingFraction")
    if type(fraction) in (int, float) and 0 <= fraction <= 1 and math.isfinite(fraction):
        out["remainingFraction"] = fraction
    reset = raw.get("resetTime")
    if isinstance(reset, str) and len(reset) <= 128:
        try:
            dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                out["resetTime"] = reset
        except ValueError:
            pass
    return out


def _marker(state: dict) -> dict:
    try:
        payload = json.loads(state.get("last_error_message") or "")
        error = payload.get("error")
        return error if isinstance(error, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def apply_request_429(
    channel_key: str, model: str, raw: Any, *, account_key: str,
    expected_state_key: str | None = None, retry_after: Any = None,
) -> None:
    """Shared text/image rejection policy; caller owns pre-commit retry limits."""
    from .. import channel_state, config, cooldown, oauth_manager

    signal = parse_antigravity_429(raw)
    with oauth_manager.account_generation_guard(expected_state_key) as current:
        if not current:
            return
        if signal["account_quota_exhausted"]:
            if expected_state_key is not None:
                account_key = channel_state.resolve(expected_state_key).removeprefix("oauth:")
            try:
                oauth_manager.set_disabled_by_quota(account_key, None, observation={
                    "source": "antigravity_account_quota",
                    "reason": "INSUFFICIENT_G1_CREDITS_BALANCE",
                })
            except Exception as exc:
                # Preserve failover on a failed account-state write; never log
                # the upstream body or credentials here.
                print(f"[antigravity] quota disable failed: {type(exc).__name__}")
            return
        delay = signal.get("retry_after")
        if delay is None:
            delay = parse_google_duration(retry_after)
        if delay is None or not 0 < delay <= MAX_RESET_SECONDS:
            # Existing maximum finite ladder window; never reach its permanent
            # rung just because Google omitted a reset/delay.
            windows = config.get().get("errorWindows") or [1, 3, 5, 10, 15, 0]
            delay = min(MAX_RESET_SECONDS, max((int(w) for w in windows if int(w) > 0), default=15) * 60)
        until = int((time.time() + delay) * 1000)
        state = cooldown.get_state(channel_state.resolve(channel_key), model) or {}
        previous_until = state.get("cooldown_until")
        # In-flight errors cannot shorten another restriction or undo a manual
        # permanent freeze. Replacing our catalog pause with a refusal is safe:
        # subsequent listings will no longer own its recovery.
        if previous_until == -1:
            return
        if (cooldown.is_blocked(channel_key, model)
                and _marker(state).get("code") not in {CATALOG_PAUSE, REQUEST_PAUSE}):
            return
        if isinstance(previous_until, int):
            until = max(until, previous_until)
        message = json.dumps({"error": {"code": REQUEST_PAUSE}}, separators=(",", ":"))
        cooldown.record_error(channel_key, model, message, cooldown_until=until)


def apply_fresh_catalog(account: dict, catalog: dict, *, expected_state_key: str) -> None:
    """Act only on exact model quota from the unchanged generation endpoint.

    No valid future reset => display only, matching existing scoped quota policy.
    Endpoint presence alone (including a missing quotaInfo) proves no recovery.
    """
    from .. import channel_state, cooldown, oauth_manager
    from ..oauth import antigravity as provider

    with oauth_manager.account_generation_guard(expected_state_key) as current:
        if not current:
            return
        key = channel_state.resolve(expected_state_key).removeprefix("oauth:")
        live = oauth_manager.get_account(key) or {}
        if live.get("disabled_reason") in {"user", "auth_error"}:
            return
        base = provider.effective_request_base_url(live)
        # Route changes during discovery invalidate quota applicability, not the
        # retained source metadata. Never choose a host from the catalog.
        if base != provider.effective_request_base_url(account) or catalog.get("antigravityGenerationBaseUrl") != base:
            return
        now = int(time.time() * 1000)
        records = catalog.get("models")
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            model = record.get("id")
            if not isinstance(model, str) or model not in live.get("models", []):
                continue
            sources = record.get("antigravitySources")
            if not isinstance(sources, list):
                continue
            source = next((s for s in sources if isinstance(s, dict) and s.get("baseUrl") == base), {})
            observed = source.get("observedAt")
            if type(observed) is not int or not 0 <= now - observed <= CATALOG_FRESH_MS:
                continue
            quota = normalize_quota_info(source.get("quotaInfo"))
            fraction = quota.get("remainingFraction")
            if fraction is None:
                continue
            state = cooldown.get_state(channel_state.resolve(expected_state_key), model) or {}
            if state.get("cooldown_until") == -1:
                continue  # We never create permanent pauses, even with our old marker.
            marker = _marker(state)
            owned = marker.get("code") == CATALOG_PAUSE and marker.get("baseUrl") == base
            if fraction > 0:
                if owned:
                    cooldown.clear(expected_state_key, model, notify_recovered=False)
                continue
            if cooldown.is_blocked(expected_state_key, model) and not owned:
                continue
            reset = quota.get("resetTime")
            if not reset:
                continue
            until = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() * 1000)
            if not 0 < until - now <= MAX_RESET_SECONDS * 1000:
                continue
            message = json.dumps({"error": {"code": CATALOG_PAUSE, "baseUrl": base}}, separators=(",", ":"))
            cooldown.record_quota_pause(expected_state_key, model, message, cooldown_until=until)
