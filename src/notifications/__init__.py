"""Structured notification facade. Business callers supply facts, never Telegram HTML."""
from .events import NotificationEvent, Effect
from . import subjects


def capture(account_key):
    """Capture BEFORE asynchronous work; a later observation cannot overwrite a newer one."""
    from . import store
    subject = subjects.account(account_key)
    return subject, store.next_version(subject)


def emit(event, *, recipients=None, transactional=False):
    from .. import config, notifier
    from . import store
    try:
        record = event.record()
        if record["version"] == 0:
            record["version"] = store.next_version(event.subject)
        cfg = config.get()
        targets = recipients if recipients is not None else (cfg.get("telegram") or {}).get("adminIds", [])
        targets = sorted({int(value) for value in targets})
        queued = store.record(record, cfg.get("notifications") or {}, targets, transactional=transactional)
        accepted = 0
        for delivery_id in queued:
            if not notifier.enqueue_delivery(delivery_id):
                store.not_submitted(delivery_id)
            else:
                accepted += 1
        return bool(accepted)
    except Exception as exc:
        # Auxiliary failure must not repeat or roll back a committed business action.
        print(f"[notification] record rejected type={event.event_type} error={type(exc).__name__}")
        return False


def account_event(event_type, account_key, facts, effect, *, source="local", scope="account", scope_ref="", dimension="", correlation_ref="", observation=None):
    subject, version = observation if observation is not None else (subjects.account(account_key), 0)
    return emit(NotificationEvent(event_type, subject, effect, facts, scope, scope_ref, dimension, source, version, correlation_ref))


def channel_event(event_type, channel_key, facts, effect, *, source="local", scope_ref="", dimension="", subject=None):
    return emit(NotificationEvent(event_type, subject or subjects.channel(channel_key), effect, facts,
                                  "model", scope_ref, dimension, source))


def notified(event_type, subject, *, scope="account", scope_ref="", dimension=""):
    """Whether the open episode already reached a recipient; a switched-off episode has not."""
    from . import store
    try:
        key = store.incident_for(event_type, subject, scope=scope, scope_ref=scope_ref, dimension=dimension)
        incident = store.get("incidents", key) or {}
        return bool(incident.get("active") and incident.get("pushed"))
    except Exception:
        return False


def resolve_silently(event_type, subject, *, scope="account", scope_ref="", dimension="", version=0):
    """Business-confirmed end of a non-broadcast episode (e.g. included quota returns)."""
    from . import store
    try:
        key = store.incident_for(event_type, subject, scope=scope, scope_ref=scope_ref, dimension=dimension)
        def change(rows):
            if key in rows and (not version or version >= rows[key].get("version", 0)):
                rows[key] = {**rows[key], "active": False, "version": max(version, rows[key].get("version", 0))}
        store.mutate("incidents", change)
    except Exception:
        print("[notification] silent resolution persistence failed")


def store_failed(*, model, api_key_name, response_id, error, source):
    # Only exception TYPE is safe to persist; file names/URLs/exception text can carry secrets.
    return emit(NotificationEvent("system.responses_store.failed", subjects.system(), Effect("failed", "history_unavailable"),
        {"model": str(model or ""), "api_key_name": str(api_key_name or ""), "response_id": str(response_id or ""), "error_code": type(error).__name__},
        scope="system", source=source))


def routing_unavailable(*, ingress, model, exclusions, api_key_name, request_id=""):
    safe = []
    for item in exclusions or []:
        channel = item.get("channel")
        subject = subjects.channel(channel) if channel else None
        safe.append({"subject": (subject.label or subject.identity or subject.service) + " · #" + subject.short_id if subject else "全局模型",
                     "reason": str(item.get("reason") or "unknown")})
    return emit(NotificationEvent("routing.unavailable", subjects.system("请求路由", kind="route"), Effect("confirmed", "route_blocked"),
        {"ingress": ingress, "model": model, "exclusions": safe[:300], "api_key_name": str(api_key_name or ""), "request_id": str(request_id)},
        scope="route", scope_ref=ingress + ":" + model, source=ingress))
