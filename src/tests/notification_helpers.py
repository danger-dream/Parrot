"""Business-trigger test adapters. Core delivery/lifecycle is tested without these spies."""
from src import config, notifications
from src.notifications import content
from src.notifications.catalog import enabled


def capture(monkeypatch, receive):
    """Capture the typed contract at the facade, not obsolete notifier HTML calls."""
    records = []
    def emit(event, **kwargs):
        record = event.record()  # unknown fields/types still fail the test
        records.append(record)
        if not kwargs.get("transactional") and not enabled(event.event_type, config.get().get("notifications") or {}):
            return False
        result = receive(event.event_type, content.render(record), record)
        return result is not False
    monkeypatch.setattr(notifications, "emit", emit)
    return records


def ui_operation_transport(monkeypatch):
    """Reuse a test's existing fake UI wire for new transaction-result messages."""
    from src.telegram import ui
    from src.notifications import delivery
    def request(method, payload):
        p = dict(payload)
        if method == "sendMessage":
            return ui.send(p["chat_id"], p["text"], reply_markup=p.get("reply_markup"), parse_mode=p.get("parse_mode", "HTML"))
        if method == "editMessageText":
            return ui.edit(p["chat_id"], p["message_id"], p["text"], reply_markup=p.get("reply_markup"), parse_mode=p.get("parse_mode", "HTML"))
        raise AssertionError("unexpected operation transport method: " + method)
    monkeypatch.setattr(delivery, "request", request)
