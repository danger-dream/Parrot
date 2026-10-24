"""Telegram WebUI page: show and reset the fixed management key without a restart."""
import copy

import pytest

from src import config
from src.management_auth import SessionAuthenticationError
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.system import SettingsControl
from src.tests.test_management_api_foundation import FAKE_KEY, build_app


@pytest.fixture
def cfg(monkeypatch):
    value = copy.deepcopy(config.DEFAULT_CONFIG)
    value["management"]["managementKey"] = FAKE_KEY
    monkeypatch.setattr(config, "get", lambda: value)
    monkeypatch.setattr(config, "update", lambda fn: fn(value))
    return value


def test_reset_through_runtime_controls_switches_the_live_key(tmp_path, cfg):
    _app, runtime, _ = build_app(tmp_path)
    settings = runtime.control_owner().system.settings
    context = settings.bind_telegram_actor(42)
    assert settings.management_key(context) == FAKE_KEY
    old = runtime.sessions.create_from_management_key(FAKE_KEY, source="old", request_id="r1")

    key, revoked = settings.reset_management_key(context)

    assert key.startswith("pmk_") and len(key) == 68 and key != FAKE_KEY
    assert cfg["management"]["managementKey"] == key
    assert settings.management_key(context) == key
    assert revoked == 1
    with pytest.raises(SessionAuthenticationError):
        runtime.sessions.verify(old.credential)
    with pytest.raises(SessionAuthenticationError):
        runtime.sessions.create_from_management_key(FAKE_KEY, source="stale", request_id="r2")
    assert runtime.sessions.create_from_management_key(key, source="new", request_id="r3")
    assert any(row["action"] == "settings.management-key.reset" and row["result"] == "succeeded"
               for row in runtime.state_store.audit_snapshot())


def test_reset_without_a_running_session_service_changes_nothing(cfg):
    settings = SettingsControl()
    with pytest.raises(ManagementError) as exc:
        settings.reset_management_key(settings.bind_telegram_actor(42))
    assert exc.value.code is ManagementErrorCode.SERVICE_NOT_READY
    assert cfg["management"]["managementKey"] == FAKE_KEY


def test_telegram_page_shows_the_key_and_resets_it(cfg, monkeypatch):
    from src.telegram import ui
    from src.telegram.menus import system_menu as sm
    calls = []
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, data)) or {"ok": True, "result": {}})
    rotated = []
    monkeypatch.setattr(sm, "_settings_control", SettingsControl(management_key_rotator=lambda key: rotated.append(key) or 2))
    assert sm.handle_callback(42, 7, "cb", "sys:show:webui")
    page = next(data for method, data in calls if method == "editMessageText")
    assert f"管理密钥：<code>{FAKE_KEY}</code>" in page["text"]
    calls.clear()
    assert sm.handle_callback(42, 7, "cb", "sys:webui:key_reset")
    new_key = cfg["management"]["managementKey"]
    assert rotated == [new_key] and new_key != FAKE_KEY
    assert ("answerCallbackQuery", {"callback_query_id": "cb", "text": "已重置，2 个密钥登录的会话已退出"}) in [
        (m, {k: v for k, v in d.items() if k in {"callback_query_id", "text"}}) for m, d in calls]
    page = next(data for method, data in calls if method == "editMessageText")
    assert f"管理密钥：<code>{new_key}</code>" in page["text"]
