"""Side effects of a matched error rule: its steps, its thresholds, notifications.

Kept apart from the pure matching engine. Failover runs the retry steps itself
and calls :func:`run_steps` for the rest and :func:`record_hit` once per hit;
a success on a channel/model calls :func:`reset_streak`.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from datetime import datetime
from typing import Any

from . import error_rule_conditions as conditions
from . import error_rules
from .error_rules import _BJT, CATEGORIES, ErrorObservation, RuleMatch

DISABLED_REASON = "error_rule"
_notified: dict[tuple[str, str, str, str], float] = {}
_notified_lock = threading.Lock()
_counters: dict[tuple[str, int, str, str], dict[str, Any]] = {}
_counters_lock = threading.Lock()


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, _BJT).isoformat(timespec="seconds") if ms else None


def _code_conditions(tree: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in tree["items"]:
        if item["kind"] == "group":
            out += _code_conditions(item)
        elif item["field"] == "code" and item["op"] in ("eq", "in"):
            out.append(item)
    return out


def display_code(found: RuleMatch, obs: ErrorObservation) -> str:
    """The error code worth showing: the one the rule matched, else a business-looking one."""
    for c in _code_conditions(found.rule["when"]):
        for code in obs.codes:
            if any(conditions._equals(code, target, False) for target in c["values"]):
                return code
    safe = [c for c in obs.codes if c.replace("_", "").replace("-", "").replace(".", "").isalnum() and len(c) <= 160]
    return next((c for c in safe if c.isdigit()), safe[0] if safe else "")


def disable_reason_label(found: RuleMatch, reason: str) -> str:
    from .quota_errors import ACCOUNT_BLOCK_LABELS
    return ACCOUNT_BLOCK_LABELS.get(reason) or f"被错误处理规则「{found.rule['name']}」停用，处理好后手动启用"


def _disable_source(channel: Any, reason: str) -> bool:
    reason = reason or DISABLED_REASON
    if getattr(channel, "type", "") == "oauth":
        from . import oauth_manager
        account_key = getattr(channel, "account_key", None)
        with oauth_manager.account_generation_guard(getattr(channel, "state_key", None)) as current:
            account = oauth_manager.get_account(account_key) if current and account_key else None
            if not account or not account.get("enabled", True) or account.get("disabled_reason"):
                return False
            outcome = oauth_manager.mutate_account_if_unchanged(
                account_key, account, lambda value: value.update(enabled=False, disabled_reason=reason))
            return outcome.get("status") == "updated"
    name = str(getattr(channel, "name", "") or str(getattr(channel, "key", "")).removeprefix("api:"))
    if not name or not getattr(channel, "enabled", True):
        return False
    from .channel import registry
    return registry.update_api_channel(name, {"enabled": False, "disabled_reason": reason}) is not None


def _notify(channel: Any, model: str, found: RuleMatch, obs: ErrorObservation, *, until_ms: int | None,
            disabled: bool, repeat_hours: int, disable_reason: str = "", trigger: str = "") -> None:
    from . import channel_state, notifications
    from .notifications.events import Effect
    key = channel_state.effect_key(channel)
    gate = (found.id, str(getattr(channel, "key", key)), model, trigger)
    now = time.monotonic()
    with _notified_lock:
        last = _notified.get(gate)
        if last is not None and now - last < repeat_hours * 3600:
            return
        _notified[gate] = now
    code = display_code(found, obs) or found.category
    category, reset_at = found.category, _iso(until_ms)
    if disabled and getattr(channel, "type", "") == "oauth":
        notifications.account_event(
            "account.access.blocked", getattr(channel, "account_key", None),
            {"error_code": code, "error_reason": disable_reason_label(found, disable_reason)},
            Effect("confirmed", "account_paused"), source="upstream_error")
    elif category == "quota_exhausted" and until_ms and not trigger:
        facts = {"models": [model], "windows": [found.rule["name"]], "error_code": code}
        if reset_at:
            facts["reset_at"] = reset_at
        notifications.channel_event("model.quota.blocked", key, facts, Effect("confirmed", "model_paused"),
                                    scope_ref=model, source="error_rule")
    elif category == "plan_excluded" and until_ms and not trigger:
        notifications.channel_event("model.entitlement.blocked", key,
                                    {"model": model, "error_code": code, "retry_at": reset_at},
                                    Effect("confirmed", "model_paused"), scope_ref=model, source="error_rule")
    else:
        name = found.rule["name"] + (f"（{trigger}）" if trigger else "")
        facts = {"rule_name": name, "category": CATEGORIES[category]["label"], "model": model, "error_code": code}
        if reset_at:
            facts["reset_at"] = reset_at
        notifications.channel_event("error.rule.matched", key, facts, Effect("confirmed", "rule_applied"),
                                    scope_ref=model, dimension=found.id, source="error_rule")


def _active_until(key: str, model: str, scope: str) -> int | None:
    """End of the pause already blocking this model (or account); a permanent one counts as endless."""
    from . import cooldown
    state = cooldown.get_state(key, cooldown.ACCOUNT_SCOPE if scope == "account" else model) or {}
    until = state.get("cooldown_until")
    if until == -1:
        return 2 ** 62
    if isinstance(until, (int, float)) and until > time.time() * 1000:
        return int(until)
    return None


def run_steps(channel: Any, model: str, found: RuleMatch, obs: ErrorObservation, steps: list[dict[str, Any]], *,
              message: str, connect_ms: int | None = None, trigger: str = "") -> dict[str, Any]:
    """Pause / deprioritize / disable / notify steps, in order; retry steps belong to failover."""
    from . import channel_state, cooldown, scorer
    key = channel_state.effect_key(channel)
    latest: int | None = None
    disabled = False
    disable_reason = ""
    has_disable = False
    notifies: list[dict[str, Any]] = []
    paused = False
    for step in steps:
        kind = step["type"]
        if kind == "pause":
            paused = True
            until, _source = error_rules.resolve_until(step, obs, channel=channel)
            held = _active_until(key, model, step["scope"])
            if until is None:
                cooldown.record_error(key, model, message, rule_id=found.id,
                                      transient_transport=obs.phase == "transport")
            elif held is not None and held >= until:
                until = held  # A longer pause (e.g. from a threshold) is already in force.
            elif step["scope"] == "account":
                cooldown.record_account_pause(key, message, cooldown_until=until, rule_id=found.id)
            else:
                cooldown.record_quota_pause(key, model, message, cooldown_until=until, rule_id=found.id)
            if until and (latest is None or until > latest):
                latest = until
        elif kind == "deprioritize":
            scorer.record_failure(key, model, connect_ms=connect_ms)
        elif kind == "disable":
            has_disable = True
            disable_reason = disable_reason or step["reason"]
            disabled = _disable_source(channel, step["reason"]) or disabled
        elif kind == "notify":
            notifies.append(step)
    # A disable that changed nothing (already off, or a manual disable stands) has nothing to report.
    if notifies and not (has_disable and not disabled):
        try:
            _notify(channel, model, found, obs, until_ms=latest, disabled=disabled,
                    repeat_hours=min(s["repeatHours"] for s in notifies), disable_reason=disable_reason,
                    trigger=trigger)
        except Exception as exc:
            print(f"[error_rules] notification failed for {found.id}: {type(exc).__name__}")
    return {"until": latest, "disabled": disabled, "paused": paused or has_disable}


def _counter_key(found: RuleMatch, index: int, threshold: dict[str, Any], key: str, model: str) -> tuple:
    if threshold["scope"] == "model":
        return (found.id, index, key, model)
    if threshold["scope"] == "account":
        return (found.id, index, key, "")
    return (found.id, index, "*", "")


def trigger_text(threshold: dict[str, Any]) -> str:
    if threshold["mode"] == "consecutive":
        return f"连续 {threshold['count']} 次"
    return f"{threshold['windowMinutes']} 分钟内 {threshold['count']} 次"


def record_hit(channel: Any, model: str, found: RuleMatch, obs: ErrorObservation, *, message: str,
               connect_ms: int | None = None) -> dict[str, Any]:
    """Count one hit toward the rule's thresholds and run the actions of every threshold it reaches."""
    from . import channel_state
    key = channel_state.effect_key(channel)
    now = time.monotonic()
    latest: int | None = None
    fired: list[str] = []
    stopped = False
    for index, threshold in enumerate(found.rule["thresholds"]):
        ck = _counter_key(found, index, threshold, key, model)
        signature = (threshold["mode"], threshold["count"], threshold["windowMinutes"], threshold["scope"])
        with _counters_lock:
            entry = _counters.get(ck)
            if entry is None or entry["signature"] != signature:
                entry = _counters[ck] = {"signature": signature, "streak": 0, "times": deque()}
            if threshold["mode"] == "consecutive":
                entry["streak"] += 1
                count = entry["streak"]
            else:
                times = entry["times"]
                times.append(now)
                horizon = now - threshold["windowMinutes"] * 60
                while times and times[0] < horizon:
                    times.popleft()
                count = len(times)
            reached = count >= threshold["count"]
            if reached:
                entry["streak"] = 0
                entry["times"].clear()
        if reached:
            trigger = trigger_text(threshold)
            fired.append(trigger)
            print(f"[error_rules] 「{found.rule['name']}」 reached {trigger} on {key}/{model}")
            result = run_steps(channel, model, found, obs, threshold["steps"], message=message,
                               connect_ms=connect_ms, trigger=trigger)
            if result["until"] and (latest is None or result["until"] > latest):
                latest = result["until"]
            stopped = stopped or result["paused"]
    # ``stopped``: a threshold just paused or disabled this source, so retrying it is pointless.
    return {"fired": fired, "until": latest, "stopped": stopped}


def progress(found: RuleMatch, channel_key: str, model: str) -> list[int]:
    """How far each threshold of the rule has counted for this channel and model."""
    now = time.monotonic()
    out: list[int] = []
    with _counters_lock:
        for index, threshold in enumerate(found.rule["thresholds"]):
            ck = _counter_key(found, index, threshold, channel_key, model)
            entry = _counters.get(ck)
            if entry is None:
                out.append(0)
            elif threshold["mode"] == "consecutive":
                out.append(entry["streak"])
            else:
                horizon = now - threshold["windowMinutes"] * 60
                out.append(sum(1 for t in entry["times"] if t >= horizon))
    return out


def reset_streak(channel_key: str, model: str) -> None:
    """A success on this channel and model ends every consecutive run counted there."""
    with _counters_lock:
        for (_rid, _idx, key, counted_model), entry in _counters.items():
            if key == channel_key and counted_model in (model, ""):
                entry["streak"] = 0
