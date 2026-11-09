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


def is_quota_pause_marker(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    return _marker(state).get("code") in {CATALOG_PAUSE, REQUEST_PAUSE}


def clear_quota_pauses_for_credits(account_key: str) -> int:
    """Clear only exact-model quota pauses (never manual freezes or non-quota errors)."""
    from .. import channel_state, cooldown

    resolved = channel_state.resolve(f"oauth:{account_key}")
    cleared = 0
    for entry in list(cooldown.snapshot()):
        if not isinstance(entry, dict) or entry.get("channel_key") != resolved:
            continue
        if entry.get("cooldown_until") == -1:
            continue
        model = entry.get("model")
        if isinstance(model, str) and model and is_quota_pause_marker(entry):
            until = entry.get("cooldown_until")
            if isinstance(until, (int, float)) and until > int(time.time() * 1000):
                _record_model_quota_exhausted_until(account_key, model, int(until))
            cooldown.clear(resolved, model, notify_recovered=False)
            cleared += 1
    return cleared


def _catalog_model_exhausted(account: dict, model: str) -> bool:
    from ..oauth import antigravity as provider

    base = provider.effective_request_base_url(account)
    catalog = account.get("account_model_catalog")
    if not isinstance(catalog, dict) or catalog.get("antigravityGenerationBaseUrl") != base:
        return False
    now = int(time.time() * 1000)
    for record in catalog.get("models") or []:
        if not isinstance(record, dict) or record.get("id") != model:
            continue
        sources = record.get("antigravitySources")
        if not isinstance(sources, list):
            return False
        source = next((s for s in sources if isinstance(s, dict) and s.get("baseUrl") == base), None)
        if not isinstance(source, dict):
            return False
        quota = normalize_quota_info(source.get("quotaInfo"))
        fraction = quota.get("remainingFraction")
        if fraction is None or fraction > 0.01:
            return False
        reset = quota.get("resetTime")
        if not reset:
            return True
        try:
            until = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return False
        return until > now
    return False


def should_inject_credits(
    account_key: str, model: str, *, force: bool = False,
) -> bool:
    """Return True when GOOGLE_ONE_AI credits should be injected for this model.

     Mirrors OpenAI's allowCredits design:
    - requires explicit per-account ``allowCredits=True`` and known/supported credits;
    - injects only when the model's normal quota is exhausted (either ``force=True``
      on a 429 retry or an active quota_exhausted window / catalog 0% observation).
    """
    from .. import oauth_manager

    if not oauth_manager.antigravity_credits_usable(account_key):
        return False
    if force:
        return True
    account = oauth_manager.get_account(account_key) or {}
    now_ms = int(time.time() * 1000)
    exhausted_map = account.get("antigravity_quota_exhausted_until")
    if isinstance(exhausted_map, dict):
        until = exhausted_map.get(model)
        if isinstance(until, (int, float)) and until > now_ms:
            return True
    return _catalog_model_exhausted(account, model)


def _record_model_quota_exhausted_until(account_key: str, model: str, until_ms: int) -> None:
    from .. import config, oauth_manager

    def mutate(cfg: dict) -> None:
        for acc in cfg.get("oauthAccounts", []):
            if oauth_manager.provider_of(acc) == "antigravity" and oauth_manager._canonical_key(acc) == account_key:
                mapping = acc.get("antigravity_quota_exhausted_until")
                if not isinstance(mapping, dict):
                    mapping = {}
                now_ms = int(time.time() * 1000)
                mapping = {
                    k: int(v) for k, v in mapping.items()
                    if isinstance(k, str) and isinstance(v, (int, float)) and int(v) > now_ms
                }
                prev = mapping.get(model, 0)
                mapping[model] = max(int(until_ms), int(prev))
                acc["antigravity_quota_exhausted_until"] = mapping
                break

    try:
        config.update(mutate)
    except Exception:
        pass


def can_retry_429_with_credits(
    account_key: str, raw: Any, *, already_used_credits: bool = False,
) -> bool:
    """Whether a pre-commit 429 should immediately retry on the same account with GOOGLE_ONE_AI."""
    from .. import oauth_manager

    if already_used_credits:
        return False
    signal = parse_antigravity_429(raw)
    if signal["account_quota_exhausted"] or not signal["quota_exhausted"]:
        return False
    return oauth_manager.antigravity_credits_usable(account_key)


def apply_request_429(
    channel_key: str, model: str, raw: Any, *, account_key: str,
    expected_state_key: str | None = None, retry_after: Any = None,
    used_credits: bool = False,
) -> bool:
    """Shared text/image rejection policy; caller owns pre-commit retry limits.

    Returns True when the caller should immediately retry on the same account
    with ``enabledCreditTypes=["GOOGLE_ONE_AI"]`` instead of advancing to the
    next candidate.
    """
    from .. import channel_state, config, cooldown, oauth_manager

    signal = parse_antigravity_429(raw)
    with oauth_manager.account_generation_guard(expected_state_key) as current:
        if not current:
            return False
        if expected_state_key is not None:
            account_key = channel_state.resolve(expected_state_key).removeprefix("oauth:")
        if signal["account_quota_exhausted"]:
            try:
                oauth_manager.set_disabled_by_quota(account_key, None, observation={
                    "source": "antigravity_account_quota",
                    "reason": "INSUFFICIENT_G1_CREDITS_BALANCE",
                })
            except Exception as exc:
                # Preserve failover on a failed account-state write; never log
                # the upstream body or credentials here.
                print(f"[antigravity] quota disable failed: {type(exc).__name__}")
            return False
        delay = signal.get("retry_after")
        if delay is None:
            delay = parse_google_duration(retry_after)
        if delay is None or not 0 < delay <= MAX_RESET_SECONDS:
            # Existing maximum finite ladder window; never reach its permanent
            # rung just because Google omitted a reset/delay.
            windows = config.get().get("errorWindows") or [1, 3, 5, 10, 15, 0]
            delay = min(MAX_RESET_SECONDS, max((int(w) for w in windows if int(w) > 0), default=15) * 60)
        until = int((time.time() + delay) * 1000)
        if signal["quota_exhausted"] and not used_credits and oauth_manager.antigravity_credits_usable(account_key):
            _record_model_quota_exhausted_until(account_key, model, until)
            return True
        state = cooldown.get_state(channel_state.resolve(channel_key), model) or {}
        previous_until = state.get("cooldown_until")
        # In-flight errors cannot shorten another restriction or undo a manual
        # permanent freeze. Replacing our catalog pause with a refusal is safe:
        # subsequent listings will no longer own its recovery.
        if previous_until == -1:
            return False
        if (cooldown.is_blocked(channel_key, model)
                and _marker(state).get("code") not in {CATALOG_PAUSE, REQUEST_PAUSE}):
            return False
        if isinstance(previous_until, int):
            until = max(until, previous_until)
        message = json.dumps({"error": {"code": REQUEST_PAUSE}}, separators=(",", ":"))
        cooldown.record_error(channel_key, model, message, cooldown_until=until)
        return False


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
        credits_usable = oauth_manager.antigravity_credits_usable(key)
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
            if fraction > 0 or credits_usable:
                if owned or (credits_usable and marker.get("code") == REQUEST_PAUSE):
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
