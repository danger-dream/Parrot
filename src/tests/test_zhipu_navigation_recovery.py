"""Commands must escape a failed Zhipu login through the actual TG dispatcher."""
from types import SimpleNamespace

import pytest

from src.management_control.errors import ManagementError, ManagementErrorCode
from src.oauth.zhipu.common import ZhipuError
from src.telegram import bot, states, ui
from src.telegram.menus import zhipu_oauth_menu as zh


@pytest.fixture
def navigation(monkeypatch):
    monkeypatch.setattr(zh, "_submit_remote", lambda run: run())
    states.pop_state(42)
    rendered, messages, cancelled = [], [], []
    def cancel(*args):
        cancelled.append(args)
        # An already-expired management flow must not prevent local cleanup.
        raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
    control = SimpleNamespace(cancel_login_flow=cancel)
    monkeypatch.setattr(zh, "control", control)
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    monkeypatch.setattr(ui, "send", lambda chat, text, **kw: messages.append(text))
    monkeypatch.setattr(ui, "edit", lambda chat, mid, text, **kw: messages.append(text))
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    for module, method, label in [
        (bot.main_menu, "on_start_command", "start"),
        (bot.main_menu, "on_menu_command", "menu"),
        (bot.main_menu, "handle_back", "main_callback"),
        (bot.stats_menu, "send_new", "stats"),
        (bot.oauth_menu, "send_new", "oauth"),
        (bot.apikey_menu, "send_new", "keys"),
        (bot.system_menu, "send_new", "settings"),
    ]:
        monkeypatch.setattr(module, method, lambda *a, _label=label, **kw: rendered.append(_label))
    yield control, rendered, messages, cancelled
    states.pop_state(42)


def pending(action="oa_zh_callback"):
    data = {"flow_id": "expired-flow", "flow_secret": "fixture-secret", "nonce": "nonce"}
    states.set_state(42, action, data)
    return data


def message(text):
    bot._handle_update({"update_id": 123, "message": {"chat": {"id": 42}, "text": text}})


@pytest.mark.parametrize("action", ["oa_zh_callback", "oa_zh_login", "oa_zh_name", "oa_zh_input"])
@pytest.mark.parametrize("command,expected", [
    ("/start", "start"), ("/menu", "menu"), ("/stats", "stats"),
    ("/menu@ParrotBot", "menu"), ("/oauth", "oauth"),
    ("/keys", "keys"), ("/settings", "settings"),
])
def test_menu_commands_escape_zhipu_input_before_dispatch(navigation, action, command, expected):
    _, rendered, messages, cancelled = navigation
    pending(action)
    message(command)
    assert rendered == [expected]
    assert states.get_state(42) is None and len(cancelled) == 1
    assert messages == []


def test_screenshot_command_sequence_works_without_restart(navigation):
    _, rendered, messages, cancelled = navigation
    pending()
    for command in ("/start", "/menu", "/stats"):
        message(command)
    assert rendered == ["start", "menu", "stats"]
    assert messages == [] and len(cancelled) == 1


@pytest.mark.parametrize("callback", ["menu:main", "menu:oauth"])
def test_navigation_buttons_cancel_pending_background_login(navigation, monkeypatch, callback):
    _, rendered, _, cancelled = navigation
    pending("oa_zh_login")
    def oauth(chat, mid, cb, data):
        if data == "menu:oauth":
            rendered.append("oauth_callback")
            return True
        return False
    monkeypatch.setattr(bot.oauth_menu, "handle_callback", oauth)
    bot._handle_update({"callback_query": {"id": "cb", "data": callback,
        "message": {"chat": {"id": 42}, "message_id": 100}}})
    assert rendered == ["main_callback" if callback == "menu:main" else "oauth_callback"]
    assert states.get_state(42) is None and len(cancelled) == 1


@pytest.mark.parametrize("callback", ["oa:zh:project:nonce:0", "oa:zh:cancel:nonce", "oa:zh:confirm:nonce"])
def test_own_project_and_confirmation_buttons_keep_their_state(navigation, callback):
    _, _, _, cancelled = navigation
    data = pending("oa_zh_login")
    zh.before_callback(42, callback)
    assert states.get_state(42)["data"] is data and cancelled == []


@pytest.mark.parametrize("exc", [
    ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE),
    ZhipuError("request", status=500),
    ValueError("callback_expired"),
])
def test_failed_callback_clears_input_and_next_command_is_usable(navigation, exc):
    control, rendered, messages, cancelled = navigation
    def fail(*args):
        raise exc
    control.submit_zhipu_callback = fail
    pending()
    message("http://127.0.0.1:1456/auth/callback?authCode=fixture&state=fixture")
    assert states.get_state(42) is None and len(cancelled) == 1
    assert len(messages) == 1 and "此次登录已结束" in messages[0]
    message("/menu")
    assert rendered == ["menu"] and len(messages) == 1


def test_typo_keeps_callback_retry_but_menu_always_exits(navigation):
    control, rendered, messages, _ = navigation
    def typo(*args):
        raise ValueError("invalid_callback")
    control.submit_zhipu_callback = typo
    data = pending()
    message("not-a-callback")
    assert states.get_state(42)["data"] is data and "回调地址不匹配" in messages[-1]
    message("/menu")
    assert rendered == ["menu"] and states.get_state(42) is None


def test_hooks_do_not_clear_other_editors_or_run_before_admin_check(navigation, monkeypatch):
    _, rendered, _, cancelled = navigation
    states.set_state(42, "unrelated_editor", {"keep": True})
    zh.before_command(42, "/menu")
    zh.before_callback(42, "menu:main")
    assert states.get_state(42)["action"] == "unrelated_editor"
    data = pending()
    monkeypatch.setattr(ui, "is_admin", lambda chat: False)
    message("/start")
    assert states.get_state(42)["data"] is data and not cancelled and not rendered
