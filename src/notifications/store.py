"""Bounded durable notification records in the existing JSON StateStore."""
import hashlib
import json
import secrets
import time
from .catalog import CATALOG, enabled

DOMAINS = ("notification_events", "notification_incidents", "notification_deliveries", "notification_refs", "notification_policy")
RETENTION = 30 * 86400
MAX_EVENTS = 3000
MAX_INCIDENTS = 2000
MAX_DELIVERIES = 16000
MAX_OPERATIONS = 8000
OUTBOX = {"queued", "sending", "retry_wait"}


def db():
    from .. import state_db
    return state_db.get_store()


def get(domain, key):
    return db().get("notification_" + domain, key)


def items(domain):
    return db().items("notification_" + domain)


def mutate(domain, fn):
    def change(rows):
        result = fn(rows)
        if domain == "policy":
            _prune_policy(rows, time.time())
        elif domain == "deliveries":
            _trim_deliveries(rows, time.time())
        return result
    return db()._mutate("notification_" + domain, change, strict=True)


def next_version(subject):
    key = "version:" + subject.generation
    def change(rows):
        value = int((rows.get(key) or {}).get("value", 0)) + 1
        rows[key] = {"value": value, "at": time.time()}
        return value
    return mutate("policy", change)


def incident_key(event):
    s = event["subject"]
    spec = CATALOG[event["event_type"]]
    parts = [spec.family, s["kind"], s["exact_id"] if s["generation"] in {"missing", "conflict", "v1"} else "", s["generation"], event["scope"], event["scope_ref"], event["dimension"]]
    if spec.episodic:
        parts.append(event["correlation_ref"] or event["event_id"])
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def incident_for(event_type, subject, *, scope="account", scope_ref="", dimension=""):
    from dataclasses import asdict
    return incident_key({"event_type": event_type, "subject": asdict(subject), "scope": scope,
                         "scope_ref": scope_ref, "dimension": dimension, "correlation_ref": "", "event_id": ""})


def _prune_policy(rows, now):
    # Preferences are configuration, not event history. Each auxiliary type has
    # its own budget; operation history never consumes preference admission.
    groups = {}
    for key, value in list(rows.items()):
        kind = key.split(":", 1)[0]
        if kind == "mute" and value.get("until", 0) <= now:
            rows.pop(key, None)
            continue
        if kind == "operation" and value.get("status") not in {"queued", "running"} and value.get("at", now) < now - RETENTION:
            rows.pop(key, None)
            continue
        groups.setdefault(kind, []).append(key)
    for kind, keys in groups.items():
        limit = MAX_OPERATIONS if kind == "operation" else 8000
        if len(keys) <= limit:
            continue
        if kind == "operation":
            for key in sorted(keys, key=lambda k: rows[k].get("at", 0)):
                if len(keys) <= limit:
                    break
                if rows[key].get("status") not in {"queued", "running"}:
                    rows.pop(key, None)
                    keys.remove(key)
        if len(keys) > limit:
            raise RuntimeError("notification policy category capacity reached")


def _protected_delivery(row, now):
    return row["state"] in OUTBOX or (row["state"] == "unknown" and row.get("created_at", now) >= now - RETENTION)


def _trim_deliveries(rows, now):
    # Never evict an outbox item just because its event is no longer latest.
    removable = sorted((key for key, row in rows.items() if not _protected_delivery(row, now)),
        key=lambda key: (rows[key]["state"] != "suppressed", rows[key].get("created_at", 0)))
    for key in removable:
        row = rows[key]
        if len(rows) > MAX_DELIVERIES or row.get("created_at", now) < now - RETENTION:
            rows.pop(key, None)
    if len(rows) > MAX_DELIVERIES:
        raise RuntimeError("notification outbox capacity reached")


def _prune(tables, now):
    # Run AFTER the transaction's changes: recovery/update has no net incident
    # cost, and moving latest releases the obsolete observation atomically.
    events, incidents, deliveries, refs, policy = (tables[d] for d in DOMAINS)
    _trim_deliveries(deliveries, now)
    pending = {d["incident_id"] for d in deliveries.values() if _protected_delivery(d, now)}
    for key, value in sorted(list(incidents.items()), key=lambda pair: pair[1].get("last_at", 0)):
        if not value.get("active") and key not in pending and (len(incidents) > MAX_INCIDENTS or value.get("last_at", now) < now - RETENTION):
            incidents.pop(key, None)
    protected = {i.get("latest") for i in incidents.values() if i.get("active")}
    protected |= {d["event_id"] for d in deliveries.values() if _protected_delivery(d, now)}
    removable = sorted((e for e in events.values() if e["event_id"] not in protected), key=lambda e: e["occurred_at"])
    for e in removable:
        if len(events) > MAX_EVENTS or e["occurred_at"] < now - RETENTION:
            events.pop(e["event_id"], None)
    for key, d in list(deliveries.items()):
        if d["event_id"] not in events:
            deliveries.pop(key, None)
    for key, ref in list(refs.items()):
        if ref["expires_at"] < now or ref["delivery_id"] not in deliveries:
            refs.pop(key, None)
    _prune_policy(policy, now)
    if len(events) > MAX_EVENTS or len(incidents) > MAX_INCIDENTS:
        raise RuntimeError("notification capacity reached")


def record(event, config, recipients, *, transactional=False):
    now = time.time()
    spec = CATALOG[event["event_type"]]
    key = incident_key(event)
    event["incident_id"] = key
    def change(t):
        events, incidents, deliveries, refs, policy = (t[d] for d in DOMAINS)
        old = incidents.get(key) or {}
        if event["version"] and event["version"] < old.get("version", 0):
            return []  # stale business observation must not reopen a recovered incident
        if event["event_id"] in events:
            return []
        recovered = event["phase"] == "recovered"
        rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        affected = set(event["facts"].get("models", [])) | set(event["facts"].get("windows", []))
        expanded = event["facts"].get("count", 0) > old.get("count_fact", 0) or bool(affected - set(old.get("affected", [])))
        decision = event["effect"]["code"] in {"account_paused", "model_paused"} and event["effect"]["status"] == "confirmed" and (old.get("effect") != event["effect"]["code"] or old.get("effect_status") != "confirmed")
        upgrade = rank[event["severity"]] > rank.get(old.get("severity"), -1) or expanded or decision
        first = not old if spec.episodic else not old.get("active", False)
        sources = sorted(set(old.get("sources", [])) | {event["source"]})
        incident = {**old, "latest": event["event_id"], "active": not recovered and not spec.episodic,
                    "first_at": now if first else old["first_at"], "last_at": now,
                    "count": old.get("count", 0) + 1, "version": max(event["version"], old.get("version", 0)),
                    "sources": sources, "severity": event["severity"], "count_fact": event["facts"].get("count", 0),
                    "pushed": {} if first and not recovered else dict(old.get("pushed") or {}),
                    "affected": sorted(affected), "effect": event["effect"]["code"], "effect_status": event["effect"]["status"],
                    "episode": old.get("episode", 0) + (1 if first and not recovered else 0),
                    "push_delivery": {} if first and not recovered else dict(old.get("push_delivery") or {})}
        if recovered:
            event["previous_event"] = old.get("latest")
            event["incident_started_at"] = old.get("first_at")
        events[event["event_id"]] = event
        queued = []
        for recipient in recipients:
            rid = str(recipient)
            previous = incident["pushed"].get(rid, 0)
            preference = policy.get(f"recipient:{rid}:{event['subject']['generation']}:{spec.family}") or {}
            muted = (policy.get(f"mute:{rid}:{key}") or {}).get("until", 0) > now
            allow = transactional or (enabled(event["event_type"], config) and not preference.get("disabled"))
            interval = spec.repeat
            if spec.repeat == 1800:
                reminders = (old.get("reminders") or {}).get(rid, 0)
                interval = 1800 if reminders == 0 else 21600 if reminders == 1 else 86400
            due = first or upgrade or (interval and now - previous >= interval) or not previous
            if recovered:
                due = bool(old.get("active")) and bool(previous)
                if not old and event["facts"].get("previous") and spec.recovery_from and enabled(spec.recovery_from, config):
                    due = True  # observed state transition, not a healthy startup sample
            if muted and not first and not upgrade and not recovered:
                due = False
            state = "queued" if allow and (due or transactional) else "suppressed"
            delivery_id = secrets.token_hex(12)
            deliveries[delivery_id] = {"id": delivery_id, "event_id": event["event_id"], "incident_id": key,
                "episode": incident["episode"], "recipient": int(recipient), "chat_id": int(recipient), "bypass_repeat_mute": bool(first or upgrade or recovered), "state": state, "created_at": now,
                "message_id": None, "attempts": 0, "transactional": transactional, "reason": "policy" if not allow else "duplicate" if not due else ""}
            if state == "queued":
                queued.append(delivery_id)
                incident["pushed"][rid] = now
                incident["push_delivery"][rid] = delivery_id
                counts = dict(incident.get("reminders") or {})
                counts[rid] = 0 if first or upgrade else counts.get(rid, 0) + 1
                incident["reminders"] = counts
        incidents[key] = incident
        if len(events) > MAX_EVENTS or len(deliveries) > MAX_DELIVERIES:
            for did, row in list(deliveries.items()):
                if row["incident_id"] != key or row["state"] not in {"queued", "retry_wait"} or row["event_id"] == event["event_id"]:
                    continue
                prior = events.get(row["event_id"])
                if prior and not spec.episodic and (recovered != (prior["phase"] == "recovered") or row.get("episode", 0) < incident["episode"]):
                    deliveries[did] = {**row, "state": "suppressed", "reason": "superseded_before_send"}
        _prune(t, now)
        return queued
    return db()._mutate_many(DOMAINS, change, strict=True)


def update_delivery(delivery_id, **changes):
    def change(rows):
        row = rows.get(delivery_id)
        if not row:
            return None
        row = {**row, **changes, "updated_at": time.time()}
        rows[delivery_id] = row
        return row
    return mutate("deliveries", change)


def action_ref(delivery_id, action, *, ttl=RETENTION, message_id=None, payload=None):
    return action_refs(delivery_id, [(action, payload or {})], ttl=ttl, message_id=message_id)[0]


def action_refs(delivery_id, choices, *, ttl=RETENTION, message_id=None):
    """Persist one keyboard atomically, not one entire durable snapshot per button."""
    pending = {}
    for action, payload in choices:
        key = secrets.token_urlsafe(15)
        row = {"delivery_id": delivery_id, "action": action,
               "expires_at": time.time() + (min(ttl, 300) if action == "confirm_resend" else ttl),
               "message_id": message_id, "payload": payload or {}}
        # Only safe navigation metadata; never a control plan/token.
        if set(row["payload"]) - {"page", "kind", "index", "type", "subject", "delivery"}:
            raise ValueError("unsafe notification action reference")
        if "subject" in row["payload"]:
            from .events import Subject
            value = row["payload"]["subject"]
            if not isinstance(value, dict) or set(value) != set(Subject.__dataclass_fields__):
                raise ValueError("invalid navigation subject")
        pending[key] = row
    if not pending:
        return []
    def save(rows):
        for old, value in list(rows.items()):
            if value["expires_at"] < time.time():
                rows.pop(old, None)
        if len(rows) + len(pending) > 20000:
            raise ValueError("通知按钮引用容量已满")
        rows.update(pending)
    mutate("refs", save)
    return ["nt:v1:" + key for key in pending]


def recover_queue():
    """No automatic replay after uncertain sends, and no replay of business actions."""
    queued = []
    def change(rows):
        for key, value in list(rows.items()):
            if value["state"] == "sending":
                rows[key] = {**value, "state": "unknown", "reason": "process_interrupted"}
            elif value["state"] in {"queued", "retry_wait"}:
                queued.append(key)
    mutate("deliveries", change)
    def interrupt(rows):
        for key, row in list(rows.items()):
            if row.get("kind") == "operation" and row.get("status") in {"queued", "running"}:
                rows[key] = {**row, "status": "unknown"}
    mutate("policy", interrupt)
    return queued


def not_submitted(delivery_id, reason="queue_full"):
    def change(t):
        rows = t["notification_deliveries"]
        row = rows.get(delivery_id)
        if not row:
            return
        rows[delivery_id] = {**row, "state": "failed", "reason": reason}
        incident = t["notification_incidents"].get(row["incident_id"])
        if incident:
            rid = str(row["recipient"])
            owner = (incident.get("push_delivery") or {}).get(rid)
            if owner == delivery_id or (not owner and incident.get("latest") == row["event_id"]):
                incident.setdefault("pushed", {}).pop(rid, None)
                incident.setdefault("push_delivery", {}).pop(rid, None)
    db()._mutate_many(("notification_deliveries", "notification_incidents"), change, strict=True)


def explicit_resend(delivery_id, recipient):
    key = secrets.token_hex(12)
    def change(rows):
        previous = rows.get(delivery_id)
        if not previous or previous["recipient"] != recipient or previous["state"] not in {"failed", "unknown"}:
            raise ValueError("此投递不属于本人或已不需重发")
        rows[key] = {**previous, "id": key, "state": "queued", "message_id": None, "panel_id": None,
                     "attempts": 0, "created_at": time.time(), "reason": "explicit_resend", "resend_of": delivery_id}
    mutate("deliveries", change)
    return key
