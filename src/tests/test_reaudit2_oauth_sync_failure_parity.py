"""P14 regressions for frozen OAuth post-save synchronous failure semantics."""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from threading import Event
from types import SimpleNamespace

import pytest

from src.tests import _isolation

_isolation.isolate()

from src import config, oauth_manager  # noqa: E402
from src.telegram import bot, states, ui  # noqa: E402
from src.telegram.menus import oauth_menu  # noqa: E402


CHAT_ID = 42
MESSAGE_ID = 100
CALLBACK_ID = "cb-oauth"


@dataclass
class UiTrace:
    events: list[tuple] = field(default_factory=list)
    async_finished: Event = field(default_factory=Event)

    def send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        self.events.append(("send", chat_id, text, reply_markup, parse_mode))
        if "模型同步失败" in text:
            self.async_finished.set()
        return {"ok": True, "result": {"message_id": 900}}

    def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        self.events.append(
            ("edit", chat_id, message_id, text, reply_markup, parse_mode)
        )
        if "模型同步失败" in text:
            self.async_finished.set()
        return {"ok": True, "result": {"message_id": message_id}}

    def answer_cb(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> dict:
        self.events.append(("answer", callback_query_id, text, show_alert))
        return {"ok": True}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ui, "send", self.send)
        monkeypatch.setattr(ui, "edit", self.edit)
        monkeypatch.setattr(ui, "answer_cb", self.answer_cb)

    def visible_texts(self) -> list[str]:
        return [
            event[2] if event[0] == "send" else event[3]
            for event in self.events
            if event[0] in {"send", "edit"}
        ]


def setup_function(_function) -> None:
    states.clear_all()
    config.update(
        lambda cfg: cfg.update(
            {
                "oauthAccounts": [],
                "oauth": {"mockMode": True},
                "loadBalancing": {
                    "priorityOrders": {"anthropic": [], "openai": []},
                    "modelPriorityOrders": {},
                },
            }
        )
    )


def _claude_entry(*, access_token: str = "access-old") -> dict:
    return {
        "provider": "claude",
        "type": "claude",
        "email": "claude-sync@example.test",
        "access_token": access_token,
        "refresh_token": "refresh-token",
        "models": [],
        "enabled": True,
    }


def _install_sync_launch_failure(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fail_start(account_id: str):
        calls.append(account_id)
        raise RuntimeError("injected synchronous launch failure")

    monkeypatch.setattr(
        oauth_menu.oauth_control.backend,
        "start_account_model_refresh",
        fail_start,
    )


@pytest.mark.parametrize("failure_stage", ["selection", "submit"])
def test_control_distinguishes_both_pre_future_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    entry = _claude_entry()
    oauth_manager.add_account(entry)
    account_id = oauth_manager.get_account_key(entry)
    calls: list[tuple[str, str]] = []

    def select(account_or_id: dict | str) -> dict:
        assert isinstance(account_or_id, str)
        calls.append(("selection", account_or_id))
        if failure_stage == "selection":
            raise RuntimeError("selection failed before Future")
        return {"models": [], "fallback": True}

    def submit(target_account_id: str):
        calls.append(("submit", target_account_id))
        raise RuntimeError("submit failed before Future")

    monkeypatch.setattr(
        oauth_menu.oauth_control.backend,
        "account_model_selection",
        select,
    )
    monkeypatch.setattr(
        oauth_menu.oauth_control.backend,
        "start_account_model_refresh",
        submit,
    )

    expected = (
        "selection failed before Future"
        if failure_stage == "selection"
        else "submit failed before Future"
    )
    post_save = oauth_menu.oauth_control.start_post_save_model_sync(
        oauth_menu._management_context(CHAT_ID), account_id
    )

    assert post_save["model_sync_future"] is None
    assert isinstance(post_save["model_sync_error"], RuntimeError)
    assert str(post_save["model_sync_error"]) == expected
    assert calls == (
        [("selection", account_id)]
        if failure_stage == "selection"
        else [("selection", account_id), ("submit", account_id)]
    )


def test_cursor_create_sync_launch_failure_uses_frozen_save_failure_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = UiTrace()
    trace.install(monkeypatch)
    provider_calls: list[tuple] = []
    launch_calls: list[str] = []
    now_ms = int(time.time() * 1000)

    def poll_login(uuid: str, verifier: str) -> SimpleNamespace:
        provider_calls.append(("poll", uuid, verifier))
        return SimpleNamespace(
            access_token="cursor-access",
            refresh_token="cursor-refresh",
            expires_at_ms=now_ms + 3_600_000,
        )

    def subject_from_token(access_token: str) -> str:
        provider_calls.append(("subject", access_token))
        return "cursor-sync-subject"

    def fetch_profile(access_token: str, *, account_key: str) -> dict:
        provider_calls.append(("profile", access_token, account_key))
        return {
            "email": "cursor-sync@example.test",
            "name": "Cursor Sync",
            "id": "cursor-profile",
            "email_verified": True,
        }

    def fetch_usage(access_token: str) -> dict:
        provider_calls.append(("usage", access_token))
        return {"cursor": {"plan_name": "Pro"}}

    monkeypatch.setattr(oauth_menu.oauth_control, "cursor_poll_login", poll_login)
    monkeypatch.setattr(oauth_menu.oauth_control, "cursor_subject", subject_from_token)
    monkeypatch.setattr(oauth_menu.oauth_control, "cursor_profile", fetch_profile)
    monkeypatch.setattr(oauth_menu.oauth_control, "cursor_usage", fetch_usage)
    _install_sync_launch_failure(monkeypatch, launch_calls)
    states.set_state(
        CHAT_ID,
        "oa_cursor_login",
        {"uuid": "cursor-uuid", "verifier": "cursor-verifier", "created_at": time.time()},
    )

    oauth_menu.on_login_cursor_done(CHAT_ID, MESSAGE_ID, CALLBACK_ID)

    account_id = "cursor:cursor-sync-subject"
    assert provider_calls == [
        ("poll", "cursor-uuid", "cursor-verifier"),
        ("subject", "cursor-access"),
        ("profile", "cursor-access", account_id),
        ("usage", "cursor-access"),
    ]
    assert launch_calls == [account_id]
    saved = oauth_manager.get_account(account_id)
    assert saved is not None and saved["access_token"] == "cursor-access"
    # Frozen Cursor completion reads (rather than consumes) this state and only
    # clears it after post-save succeeds.
    assert states.get_state(CHAT_ID)["action"] == "oa_cursor_login"

    assert [event[0] for event in trace.events] == ["answer", "edit", "edit"]
    assert trace.events[0] == (
        "answer",
        CALLBACK_ID,
        "登录成功，正在保存账户...",
        False,
    )
    assert "正在同步模型，请稍候" in trace.events[1][3]
    assert trace.events[2][3] == (
        "❌ 保存 Cursor 账户失败："
        "<code>injected synchronous launch failure</code>"
    )
    rendered = "\n".join(trace.visible_texts())
    assert "后台将静默重试" not in rendered
    assert "Cursor OAuth 账户已添加" not in rendered


def test_claude_create_sync_launch_failure_uses_frozen_save_failure_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = UiTrace()
    trace.install(monkeypatch)
    provider_calls: list[tuple] = []
    launch_calls: list[str] = []

    def exchange_code(code: str, verifier: str, expected_state: str) -> dict:
        provider_calls.append(("exchange", code, verifier, expected_state))
        return {
            "access_token": "claude-access",
            "refresh_token": "claude-refresh",
            "expires_in": 3600,
            "scope": "user:profile",
        }

    async def fetch_profile(access_token: str) -> dict:
        provider_calls.append(("profile", access_token))
        return {"account": {"email": "claude-sync@example.test"}}

    def extract_plan(profile: dict) -> dict:
        provider_calls.append(("plan", profile["account"]["email"]))
        return {}

    monkeypatch.setattr(oauth_menu.oauth_control, "claude_exchange_code", exchange_code)
    monkeypatch.setattr(oauth_menu.oauth_control, "claude_fetch_profile", fetch_profile)
    monkeypatch.setattr(oauth_menu.oauth_control, "claude_extract_plan", extract_plan)
    _install_sync_launch_failure(monkeypatch, launch_calls)
    states.set_state(
        CHAT_ID,
        "oa_login_code",
        {"code_verifier": "claude-verifier", "state": "claude-state"},
    )

    oauth_menu.on_login_code_input(CHAT_ID, "claude-code#ignored-fragment")

    account_id = "claude:claude-sync@example.test"
    assert provider_calls == [
        ("exchange", "claude-code", "claude-verifier", "claude-state"),
        ("profile", "claude-access"),
        ("plan", "claude-sync@example.test"),
    ]
    assert launch_calls == [account_id]
    saved = oauth_manager.get_account(account_id)
    assert saved is not None and saved["access_token"] == "claude-access"
    assert states.get_state(CHAT_ID) is None

    assert [event[0] for event in trace.events] == ["send", "send"]
    assert "正在同步模型，请稍候" in trace.events[0][2]
    assert trace.events[1][2] == (
        "❌ 保存失败: <code>injected synchronous launch failure</code>"
    )
    rendered = "\n".join(trace.visible_texts())
    assert "后台将静默重试" not in rendered
    assert "Anthropic OAuth 账户已添加" not in rendered


def test_overwrite_sync_launch_failure_uses_frozen_callback_failure_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = UiTrace()
    trace.install(monkeypatch)
    launch_calls: list[str] = []
    old_entry = _claude_entry(access_token="access-old")
    oauth_manager.add_account(old_entry)
    account_id = oauth_manager.get_account_key(old_entry)
    replacement = _claude_entry(access_token="access-new")
    nonce = "overwrite-nonce"
    states.set_state(
        CHAT_ID,
        "oa_oauth_overwrite_confirm",
        {
            "nonce": nonce,
            "target_key": account_id,
            "provider": "claude",
            "entry": replacement,
            "source": "test",
            "usage": None,
        },
    )
    _install_sync_launch_failure(monkeypatch, launch_calls)

    def is_admin(chat_id: int) -> bool:
        assert chat_id == CHAT_ID
        return True

    update = {
        "update_id": 700,
        "callback_query": {
            "id": CALLBACK_ID,
            "data": f"oa:overwrite:confirm:{nonce}",
            "from": {"id": CHAT_ID},
            "message": {"message_id": MESSAGE_ID, "chat": {"id": CHAT_ID}},
        },
    }

    def get_updates(method: str, data: dict | None = None) -> dict:
        trace.events.append(("api", method, data))
        assert method == "getUpdates"
        assert data == {"offset": 0, "timeout": 30}
        return {"ok": True, "result": [update]}

    def send_then_stop(*args, **kwargs):
        # End this single-iteration fixture after error handling, not while the
        # long poll is returning: stopped generations now correctly drop results.
        try:
            return trace.send(*args, **kwargs)
        finally:
            bot._running = False

    monkeypatch.setattr(ui, "send", send_then_stop)
    monkeypatch.setattr(ui, "is_admin", is_admin)
    monkeypatch.setattr(ui, "api", get_updates)
    monkeypatch.setattr(bot, "_offset", 0)
    monkeypatch.setattr(bot, "_running", True)

    bot._poll_loop()

    assert launch_calls == [account_id]
    saved = oauth_manager.get_account(account_id)
    assert saved is not None and saved["access_token"] == "access-new"
    assert states.get_state(CHAT_ID) is None
    assert [event[0] for event in trace.events] == ["api", "edit", "send"]
    assert "正在同步模型，请稍候" in trace.events[1][3]
    assert trace.events[2][2] == "❌ 内部错误，请稍后重试或联系管理员。"
    assert not any(event[0] == "answer" for event in trace.events)
    rendered = "\n".join(trace.visible_texts())
    assert "覆盖成功" not in rendered
    assert "OAuth 账户已覆盖" not in rendered
    assert "后台将静默重试" not in rendered


def test_returned_future_failure_still_uses_async_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = UiTrace()
    trace.install(monkeypatch)
    entry = _claude_entry()
    oauth_manager.add_account(entry)
    account_id = oauth_manager.get_account_key(entry)
    launch_calls: list[str] = []
    failed_future: concurrent.futures.Future = concurrent.futures.Future()
    failed_future.set_exception(RuntimeError("injected asynchronous refresh failure"))

    def return_failed_future(target_account_id: str) -> concurrent.futures.Future:
        launch_calls.append(target_account_id)
        return failed_future

    monkeypatch.setattr(
        oauth_menu.oauth_control.backend,
        "start_account_model_refresh",
        return_failed_future,
    )

    result = oauth_menu._foreground_account_model_sync(
        CHAT_ID,
        account_id,
        provider="claude",
        label="claude-sync@example.test",
    )

    assert result["action"] == "started"
    assert result["future"] is failed_future
    assert launch_calls == [account_id]
    assert trace.async_finished.wait(1)
    assert [event[0] for event in trace.events] == ["send", "edit"]
    assert "正在同步模型，请稍候" in trace.events[0][2]
    assert "⚠️ 模型同步失败" in trace.events[1][3]
    assert "injected asynchronous refresh failure" in trace.events[1][3]
    assert "后台将静默重试" in trace.events[1][3]
