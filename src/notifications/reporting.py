"""Typed scenario adapters for existing business facts; no HTML or upstream I/O."""
from . import account_event, channel_event, subjects, emit
from .events import Effect, NotificationEvent
from .providers import quota_metrics, REGISTRY


def observation(key):
    from . import capture
    try:
        return capture(key)
    except Exception:
        return None


def refresh_failed(key, error, effect, *, source, observation=None):
    return account_event("account.auth.refresh_failed", key, {
        "error_code": error.code, "error_title": error.title, "error_reason": error.reason,
        "advice": error.action, "retryable": error.retryable, "auth_error": error.auth_error,
    }, effect, source=source, observation=observation)


def refreshed(key, *, source="background", observation=None, recovery_only=False):
    from . import store
    subject = subjects.account(key)
    target = subjects.current(subject)
    if target is None:
        return False
    expiry = str(target["account"].get("expired") or "未取得")
    # A normal refresh only closes a known auth incident; first login is not recovery.
    probe = NotificationEvent("account.auth.recovered", subject, Effect("confirmed", "auth_refreshed"), {"expires_at": expiry}).record()
    try:
        incident = store.get("incidents", store.incident_key(probe)) or {}
    except Exception:
        incident = {}
    if incident.get("active"):
        return account_event("account.auth.recovered", key, {"expires_at": expiry}, Effect("confirmed", "auth_refreshed"), source=source, observation=observation)
    if recovery_only:
        return False
    previous = store.get("events", incident.get("latest", "")) if incident else None
    if previous and previous["event_type"] == "account.auth.recovered" and previous["facts"].get("expires_at") == expiry:
        return False  # one credential commit must not produce recovery + duplicate normal success
    return account_event("account.auth.refreshed", key, {"expires_at": expiry}, Effect("confirmed", "auth_refreshed"), source=source, correlation_ref=expiry, observation=observation)


def quota(key, *, recovered=False, windows=(), usage=None, metrics=None, threshold=None, reset_at=None, source="background", observation=None, operation=None):
    subject = observation[0] if observation else subjects.account(key)
    target = subjects.current(subject)
    if target is None:
        return False
    a = target["account"]
    paused = not a.get("enabled", True) or bool(a.get("disabled_reason"))
    confirmed = (recovered and a.get("disabled_reason") != "quota") or (not recovered and paused and a.get("disabled_reason") == "quota")
    effect = Effect("confirmed" if confirmed else "unknown", "still_paused" if recovered and paused else "account_resumed" if recovered else "account_paused")
    facts = {"windows": [str(w) for w in windows], "metrics": metrics if metrics is not None else quota_metrics(usage or {})}
    adapter = REGISTRY.get(subject.provider)
    if adapter and adapter.facts and usage is not None:
        facts["metrics"] = adapter.facts(usage)
    if threshold is not None and not recovered:
        facts["threshold"] = threshold
    if reset_at and not recovered:
        facts["reset_at"] = str(reset_at)
    if recovered:
        facts["previous"] = "本次检查确实解除了已有的配额暂停"
    if operation and recovered:
        facts["operation"] = operation
    return account_event("account.quota.recovered" if recovered else "account.quota.blocked", key, facts, effect,
                         source=source, observation=observation)


def model_quota(key, *, pool, models, recovered=False, used=None, threshold=None, reset_at=None, source="background"):
    names = {"cursor_models": "Cursor 自有模型", "other_models": "Cursor 其他模型", "fable": "Claude Fable周额度"}
    facts = {"pool": names.get(pool, pool), "models": list(models), "count": len(models)}
    if not recovered:
        if used is not None:
            facts["metrics"] = [{"metric_id": pool, "display_name": names.get(pool, pool), "kind": "percent", "used": used, "unit": "%"}]
        if threshold is not None:
            facts["threshold"] = threshold
        if reset_at:
            facts["reset_at"] = str(reset_at)
    return account_event("model.quota.recovered" if recovered else "model.quota.blocked", key, facts,
        Effect("confirmed", "model_resumed" if recovered else "model_paused"), source=source, scope="pool", scope_ref=pool)


def benefit_granted(key, account, before, after, grant_id):
    from collections import Counter
    import time
    facts = {"operation": "领取重置卡", "result": "领取已确认；新增数量未核实", "benefits": [], "inventory": []}
    # Native card layout is explicitly adapted; no raw provider objects retained.
    from datetime import datetime, timezone, timedelta
    def expiries(snapshot, kind):
        return [c["expire_at"] for c in (snapshot or {}).get("available_" + kind + "_resets", [])
                if isinstance(c, dict) and type(c.get("expire_at")) in (int, float) and c["expire_at"] > time.time() * 1000]
    for kind, label in (("five_hour", "5小时额度重置卡"), ("week", "周额度重置卡")):
        new = expiries(after, kind) if isinstance(after, dict) else []
        if isinstance(after, dict):
            facts["inventory"].append({"name": label, "count": len(new)})
        if isinstance(before, dict):
            old = expiries(before, kind)
            delta = Counter(new) - Counter(old)
            for expiry, count in sorted(delta.items()):
                facts["benefits"].append({"name": label, "count": count, "expires_at": datetime.fromtimestamp(expiry / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")})
    if facts["benefits"]:
        facts["result"] = "本次新增权益已核实；库存另列"
    return account_event("account.benefit.granted", key, facts, Effect("confirmed", "benefit_saved"),
                         source="benefit_action", correlation_ref=str(grant_id),
                         observation=(subjects.account(account), 0))


def channel_recovered(subject, model, previous):
    from . import store
    kind = "channel.model.recovered"
    facts = {"model": model, "previous": previous}
    try:
        for candidate in ("model.quota.blocked", "model.entitlement.blocked"):
            key = store.incident_for(candidate, subject, scope="model", scope_ref=model)
            if (store.get("incidents", key) or {}).get("active"):
                kind = candidate.replace("blocked", "recovered")
                facts = {"models": [model], "count": 1} if candidate == "model.quota.blocked" else {"model": model, "previous": "套餐不含此模型"}
                break
    except Exception:
        pass
    return channel_event(kind, subject.exact_id, facts, Effect("confirmed", "model_resumed"), scope_ref=model, source="cooldown", subject=subject)
