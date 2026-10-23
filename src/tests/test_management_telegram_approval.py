from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.management_auth import ApprovalNotification
from src.telegram import bot, ui


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data or {})))
        return {"ok": True, "result": {}}


def test_management_approval_message_is_isolated_and_contains_no_browser_secret(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42])
    now = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    notification = ApprovalNotification(
        approval_id="map_fake_public_id",
        requested_at=now,
        expires_at=now + timedelta(minutes=3),
        client_name="browser<script>",
        source_address="203.0.113.7",
        device_summary="fake-device",
        approve_callback="mauth:a:map_fake_public_id",
        deny_callback="mauth:d:map_fake_public_id",
    )
    assert bot.send_management_approval((42,), notification) is True
    assert len(recorder.calls) == 1
    method, payload = recorder.calls[0]
    assert method == "sendMessage"
    assert "browser&lt;script&gt;" in payload["text"]
    assert "exchange" not in payload["text"].lower()
    callbacks = [
        button["callback_data"]
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert callbacks == ["mauth:a:map_fake_public_id", "mauth:d:map_fake_public_id"]
    assert all(len(value.encode()) <= 64 for value in callbacks)


def test_mauth_dispatch_uses_callback_from_id_and_bypasses_legacy_menus(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42])
    monkeypatch.setattr(
        bot.menu_cache,
        "begin_view",
        lambda *args: (_ for _ in ()).throw(AssertionError("legacy view touched")),
    )
    calls = []

    def handler(approval_id, telegram_user_id, approved):
        calls.append((approval_id, telegram_user_id, approved))
        return "approved"

    bot.configure_management_approval_handler(handler)
    try:
        bot._handle_callback(
            {
                "id": "callback-id",
                "from": {"id": 99},
                "message": {"chat": {"id": 42}, "message_id": 10},
                "data": "mauth:a:map_fake_public_id",
            }
        )
    finally:
        bot.configure_management_approval_handler(None)
    assert calls == [("map_fake_public_id", 99, True)]
    assert [method for method, _ in recorder.calls] == ["answerCallbackQuery", "editMessageText"]
    assert recorder.calls[0][1]["show_alert"] is True
    edited = recorder.calls[1][1]
    assert (edited["chat_id"], edited["message_id"]) == (42, 10) and "reply_markup" not in edited


def test_mauth_duplicate_decision_is_not_reported_as_success(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42])
    bot.configure_management_approval_handler(
        lambda approval_id, telegram_user_id, approved: "alreadyDecided"
    )
    try:
        bot._handle_callback(
            {
                "id": "duplicate-callback-id",
                "from": {"id": 42},
                "message": {"chat": {"id": 42}, "message_id": 10},
                "data": "mauth:a:map_fake_public_id",
            }
        )
    finally:
        bot.configure_management_approval_handler(None)
    assert [method for method, _ in recorder.calls] == ["answerCallbackQuery", "editMessageText"]
    answer = recorder.calls[0][1]
    assert answer["show_alert"] is True
    assert answer["text"] == "登录批准已处理，不能重复决定"
    assert "已批准登录" not in answer["text"]


def test_mauth_fails_closed_without_management_handler(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [])
    bot.configure_management_approval_handler(None)
    bot._handle_callback(
        {
            "id": "callback-id",
            "from": {"id": 42},
            "message": {"chat": {"id": 42}, "message_id": 10},
            "data": "mauth:d:map_fake_public_id",
        }
    )
    assert [method for method, _ in recorder.calls] == ["answerCallbackQuery"]
    assert "暂不可用" in recorder.calls[0][1]["text"]


class SentRecorder(Recorder):
    """sendMessage answers with a message id, like the Bot API."""

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data or {})))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}
        return {"ok": True, "result": {}}


def _notification(seconds=180):
    now = datetime.now(timezone.utc)
    return ApprovalNotification(
        approval_id="map_copies",
        requested_at=now,
        expires_at=now + timedelta(seconds=seconds),
        client_name="Parrot WebUI",
        source_address="203.0.113.7",
        device_summary="Chrome",
        approve_callback="mauth:a:map_copies",
        deny_callback="mauth:d:map_copies",
        confirm_code="N2F9",
    )


def test_a_decision_rewrites_every_admin_copy_without_buttons(monkeypatch):
    recorder = SentRecorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42, 43])
    assert bot.send_management_approval((42, 43), _notification()) is True
    sent = [(d["chat_id"], m) for m, d in recorder.calls if m == "sendMessage"]
    assert len(sent) == 2 and "有效期至" in recorder.calls[0][1]["text"] and "+00:00" not in recorder.calls[0][1]["text"]
    recorder.calls.clear()
    bot.configure_management_approval_handler(lambda approval_id, telegram_user_id, approved: "denied")
    try:
        bot._handle_callback({
            "id": "cb", "from": {"id": 42, "first_name": "Boss"},
            "message": {"chat": {"id": 42}, "message_id": 101}, "data": "mauth:d:map_copies",
        })
    finally:
        bot.configure_management_approval_handler(None)
    edits = [d for m, d in recorder.calls if m == "editMessageText"]
    assert sorted((d["chat_id"], d["message_id"]) for d in edits) == [(42, 101), (43, 102)]
    assert all("已拒绝" in d["text"] and "Boss" in d["text"] and "reply_markup" not in d for d in edits)
    assert "map_copies" not in bot._approval_messages


def test_an_undecided_request_is_marked_expired(monkeypatch):
    recorder = SentRecorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42])
    bot.send_management_approval((42,), _notification(seconds=-5))
    timer = bot._approval_messages["map_copies"]["timer"]
    timer.join(3)
    edits = [d for m, d in recorder.calls if m == "editMessageText"]
    assert len(edits) == 1 and "已过期" in edits[0]["text"] and "reply_markup" not in edits[0]


def test_a_message_sent_before_a_restart_still_loses_its_buttons(monkeypatch):
    recorder = SentRecorder()
    monkeypatch.setattr(ui, "api", recorder)
    ui.configure("fake-telegram-token", [42])
    bot.configure_management_approval_handler(lambda approval_id, telegram_user_id, approved: "approved")
    try:
        bot._handle_callback({
            "id": "cb", "from": {"id": 42},
            "message": {"chat": {"id": 42}, "message_id": 7, "text": "🔐 Parrot 控制台登录\n确认码：AB12"},
            "data": "mauth:a:map_unknown",
        })
    finally:
        bot.configure_management_approval_handler(None)
    edit = [d for m, d in recorder.calls if m == "editMessageText"][0]
    assert edit["message_id"] == 7 and "确认码：AB12" in edit["text"] and "已批准" in edit["text"]
