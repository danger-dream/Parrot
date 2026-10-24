"""Console inbox: recorded notifications grouped per incident, plus legacy system texts.

Read marks are the only state this module owns; it never sends, mutes or acts.
"""
import html
import re
import secrets
import time
from . import content, store, subjects
from .catalog import CATALOG, LEGACY_SYSTEM, enabled

FEED_RETENTION = store.RETENTION
MAX_FEED = 500
MAX_MARKS = 3000
FEED_PAGES = {"status_alert": ("/alerts", "上游状态"), "network_monitor": ("/network", "网络检测"),
              "app_update": ("/updates", "版本更新"), "model_registry": ("/models", "模型中心"),
              "translation_failed": ("/translation", "翻译设置")}
_TAG = re.compile(r"<[^>]+>")
_BOLD = re.compile(r"<b>(.*?)</b>", re.S)


def _plain(value):
    # Tags first, entities after: escaped user text must not turn into markup.
    return html.unescape(_TAG.sub("", str(value))).strip()


def remember(event_key, text):
    """Keep a legacy Telegram text for the console; failures never reach the sender."""
    try:
        raw = str(text or "")
        first = raw.split("\n", 1)[0]
        bold = _BOLD.search(first)
        title = _plain(bold.group(1)) if bold else ""
        body = raw[bold.end():] if bold else raw
        lines = [line for line in (_plain(line) for line in body.split("\n")) if line]
        tone = "recovered" if "✅" in first else "error" if ("❌" in first or "🚨" in first) else "warning" if "⚠️" in first else "info"
        row = {"id": secrets.token_hex(8), "event_key": str(event_key), "at": time.time(), "tone": tone,
               "title": (title or LEGACY_SYSTEM.get(event_key) or str(event_key))[:200],
               "lines": [line[:400] for line in lines[:16]]}

        def change(rows):
            rows[row["id"]] = row
            cutoff = row["at"] - FEED_RETENTION
            ordered = sorted(store._rows(rows), key=lambda pair: pair[1].get("at", 0))
            size = len(ordered)
            for key, value in ordered:
                if size <= MAX_FEED and value.get("at", 0) >= cutoff:
                    break
                rows.pop(key, None)
                size -= 1
        store.db()._mutate("notification_feed", change, strict=True)
        announce()
    except Exception as exc:
        print(f"[notification] console feed rejected key={event_key} error={type(exc).__name__}")


def announce():
    from .. import request_events
    request_events.publish("notification", {})


def _links(event):
    s, facts = event["subject"], event["facts"]
    links = []
    target = subjects.current(s) if s["kind"] in {"account", "channel"} else None
    if target:
        if s["kind"] == "account":
            links.append({"kind": "account", "id": target["key"], "label": "账户详情"})
        else:
            links.append({"kind": "channel", "id": target["key"], "label": "渠道详情"})
    model = facts.get("model") or facts.get("requested_model")
    if model:
        links.append({"kind": "model", "id": str(model), "label": "模型详情"})
    if facts.get("request_id"):
        links.append({"kind": "request", "id": str(facts["request_id"]), "label": "调用日志"})
    if facts.get("api_key_name"):
        links.append({"kind": "apikey", "id": str(facts["api_key_name"]), "label": "Key 详情"})
    return links


def _structured_item(key, incident, event):
    spec = CATALOG[event["event_type"]]
    s, facts = event["subject"], event["facts"]
    # An empty observation list renders as a bare "（0）：" label, which says nothing here.
    facts_shown = {name: facts[name] for name in spec.fields if name in facts and facts[name] != []}
    body = [_plain(line) for line in content.fact_lines(facts_shown, labels=spec.labels, show=spec.show)]
    effect = _plain(content.effect_text(event["effect"]))
    advice = _plain(content.advice_text(event))
    origin = [_plain(line) for line in content.origin_lines(facts)]
    identity = [_plain(line) for line in content.identity(s, plan=spec.family in content.PLAN_FAMILIES)]
    if spec.episodic:
        status = None
    elif incident.get("active"):
        status = "active"
    else:
        status = "recovered" if event["phase"] == "recovered" else "ended"
    return {"id": "inc:" + key, "source": "event", "type": event["event_type"],
            "tone": "recovered" if event["phase"] == "recovered" else event["severity"],
            "title": _plain(content.headline(event)), "subject": identity[0] if identity else "",
            "summary": (body or [effect])[0] if (body or effect) else "",
            "lines": [line for line in (*origin, *body, effect, advice) if line],
            "status": status, "count": int(incident.get("count") or 1),
            "first_at": float(incident.get("first_at") or event["occurred_at"]),
            "last_at": float(incident.get("last_at") or event["occurred_at"]), "links": _links(event)}


def _feed_item(row):
    page = FEED_PAGES.get(row["event_key"])
    lines = list(row.get("lines") or [])
    return {"id": "feed:" + row["id"], "source": "legacy", "type": row["event_key"], "tone": row.get("tone", "info"),
            "title": row["title"], "subject": "", "summary": lines[0] if lines else "", "lines": lines,
            "status": None, "count": 1, "first_at": row["at"], "last_at": row["at"],
            "links": [{"kind": "page", "id": page[0], "label": page[1]}] if page else []}


def _event_type(db, event_id):
    # An event record never changes type; only the page being shown is read in full.
    if event_id not in _types:
        if len(_types) > 4 * store.MAX_EVENTS:
            _types.clear()
        event = db.get("notification_events", event_id)
        if not event:
            return None
        _types[event_id] = event.get("event_type")
    return _types[event_id]


_types = {}


def _entries(notifications):
    """(id, last_at, build) for every visible entry, newest first; build renders one entry."""
    db = store.db()
    rows = []
    for key, incident in db.items("notification_incidents").items():
        latest = incident.get("latest") or ""
        kind = _event_type(db, latest)
        if kind not in CATALOG:
            continue
        # Self-deleting Telegram notices are transient confirmations, not inbox entries.
        if CATALOG[kind].auto_delete or not enabled(kind, notifications):
            continue
        last = float(incident.get("last_at") or 0)
        rows.append(("inc:" + key, last, lambda key=key, incident=incident, latest=latest: _built(key, incident, latest)))
    flags = notifications.get("events") or {}
    if notifications.get("enabled", True):
        for row in db.values("notification_feed"):
            if flags.get(row.get("event_key")) is not False:
                rows.append(("feed:" + row["id"], float(row["at"]), lambda row=row: _feed_item(row)))
    rows.sort(key=lambda entry: entry[1], reverse=True)
    return rows


def _built(key, incident, latest):
    event = store.db().get("notification_events", latest)
    return _structured_item(key, incident, event) if event else None


def _marks():
    marks = {}
    for key, value in store.items("policy").items():
        if key.startswith("inbox:"):
            marks[key[6:]] = float(value.get("at", 0))
    return marks


def listing(view, page, page_size):
    from .. import config
    marks = _marks()
    floor = marks.get("*", 0)
    rows = [(entry, entry[1] > max(floor, marks.get(entry[0], 0))) for entry in _entries(config.get().get("notifications") or {})]
    unread = sum(1 for _, flag in rows if flag)
    selected = [entry for entry, flag in rows if flag == (view == "unread")]
    start = (page - 1) * page_size
    items = []
    for _, _, build in selected[start:start + page_size]:
        item = build()
        if item:
            item["unread"] = view == "unread"
            items.append(item)
    return items, len(selected), unread


def mark_read(ids=(), *, everything=False):
    now = time.time()
    wanted = {str(value) for value in ids}

    def change(rows):
        if everything:
            rows["inbox:*"] = {"at": now}
        for value in wanted:
            rows["inbox:" + value] = {"at": now}
        floor = (rows.get("inbox:*") or {}).get("at", 0)
        marks = sorted(((key, row) for key, row in store._rows(rows) if key.startswith("inbox:") and key != "inbox:*"),
                       key=lambda pair: pair[1].get("at", 0))
        size = len(marks)
        for key, row in marks:
            if row.get("at", 0) <= floor or row.get("at", 0) < now - store.RETENTION or size > MAX_MARKS:
                rows.pop(key, None)
                size -= 1
    store.mutate("policy", change)
