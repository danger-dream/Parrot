"""Continuous TG setup and cancellation across the remote dispatch boundary."""
from __future__ import annotations

import threading
import pytest

from src import oauth_manager as om, state_db
from src.oauth.zhipu import actions, common
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_zhipu_provider import account_env
from src.tests.test_zhipu_management import ctl, reset_env
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_callback_login import callback_env
from src.tests.test_zhipu_usage_recovery import login, refresh


@pytest.fixture
def cards(monkeypatch):
    output = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
    monkeypatch.setattr(ui, "send", lambda *a, **kw: pytest.fail("must edit the existing card"))
    def edit(chat, mid, text, **kw):
        assert chat == 42 and mid == 500
        output.append((text, kw.get("reply_markup") or {}))
    monkeypatch.setattr(ui, "edit", edit)
    yield output
    states.pop_state(42)


def click(callback):
    assert menu.handle_callback(42, 500, "cb", callback)


def buttons(card):
    return [button for row in card[1].get("inline_keyboard", []) for button in row]


def confirm_plan(control, key, monkeypatch, action="create_key"):
    monkeypatch.setattr(zh, "control", control)
    click("oa:zh:plan:" + ui.register_code(key) + ":" + action + (":FIVE_HOUR:0" if action == "use" else ""))
    return states.get_state(42)["data"]["nonce"]


def test_missing_key_auto_creation_continues_to_ready_on_same_card(callback_env, cards, monkeypatch):
    control, _, _ = callback_env
    wire = common.request
    created = []
    def request(url, **kw):
        if url.endswith("/api_keys"):
            if kw.get("method", "GET") == "POST":
                created.append(True)
                return {"apiKey": "new-id", "name": "zcode-api-key"}
            return [{"apiKey": "new-id", "name": "zcode-api-key"}] if created else []
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    from src.management_control.oauth.menu_bridge import telegram_context
    from src.tests.test_zhipu_callback_login import callback
    ctx = telegram_context(42)
    flow = control.start_zhipu_callback_login(ctx, site="bigmodel")
    poll = control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow))
    data = zh._state(42, "oa_zh_login", flow_id=flow.flow_id, flow_secret=flow.flow_secret)
    zh._login_saved(42, 500, data, poll)
    assert not any("创建并继续" in str(card) for card in cards)
    saved = om.list_accounts()[0]
    key = om.get_account_key(saved)
    assert created == [True]
    assert saved["model_key"] == "new-id.existing-secret"
    assert saved["models"] and state_db.quota_load(key)
    assert "账户已就绪" in cards[-1][0]
    assert states.get_state(42) is None


@pytest.mark.parametrize("leave", ["cancel", "back"])
@pytest.mark.parametrize("action", ["create_key", "use"])
def test_cancel_during_preflight_prevents_post_without_waiting(reset_env, cards, monkeypatch, leave, action):
    control, key, state, _ = reset_env
    nonce = confirm_plan(control, key, monkeypatch, action)
    wire = common.request
    entered, release = threading.Event(), threading.Event()
    futures = []
    def request(url, **kw):
        if url.endswith("/api_keys" if action == "create_key" else "/status"):
            entered.set()
            assert release.wait(5)
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    def submit(run):
        future = zh._remote_executor.submit(run)
        futures.append(future)
        return future
    monkeypatch.setattr(zh, "_submit_remote", submit)
    click("oa:zh:confirm:" + nonce)
    try:
        assert entered.wait(2)
        assert any(b["text"] == "取消操作" for b in buttons(cards[-1]))
        click("oa:zh:confirm:" + nonce)  # Busy repeats must not enqueue another effect.
        assert len(futures) == 1
        if leave == "cancel":
            click("oa:zh:cancel_action:" + nonce)
            assert "未提交" in cards[-1][0]
        else:
            zh.before_callback(42, "oa:view:" + ui.register_code(key) + ":1:all")
        assert states.get_state(42) is None and not release.is_set()
        before = list(cards)
    finally:
        release.set()
        for future in futures:
            future.result(5)
    assert cards == before
    assert not any(kw.get("method") == "POST" for _, kw in state["calls"])
    result = actions.history(om.get_account(key))[-1]
    assert result["status"] == "not_submitted" and result["error_kind"] == "cancelled"


def test_stop_after_dispatch_preserves_result_and_does_not_claim_rollback(reset_env, cards, monkeypatch):
    control, key, state, _ = reset_env
    nonce = confirm_plan(control, key, monkeypatch)
    wire = common.request
    entered, release = threading.Event(), threading.Event()
    futures = []
    def request(url, **kw):
        if kw.get("method") == "POST":
            entered.set()
            assert release.wait(5)
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    def submit(run):
        future = zh._remote_executor.submit(run)
        futures.append(future)
        return future
    monkeypatch.setattr(zh, "_submit_remote", submit)
    click("oa:zh:confirm:" + nonce)
    try:
        assert entered.wait(2)
        assert any(b["text"] == "停止等待" for b in buttons(cards[-1]))
        click("oa:zh:cancel_action:" + nonce)
        assert "无法撤销" in cards[-1][0] and "未提交新的" not in cards[-1][0]
        before = list(cards)
    finally:
        release.set()
        for future in futures:
            future.result(5)
    assert cards == before
    assert om.get_account(key)["model_key"] == "new-id.new-secret"
    assert actions.history(om.get_account(key))[-1]["status"] == "succeeded"
    assert sum(kw.get("method") == "POST" for _, kw in state["calls"]) == 1


def test_preflight_timeout_shows_reason_and_direct_retry_confirmation(reset_env, cards, monkeypatch):
    control, key, state, _ = reset_env
    nonce = confirm_plan(control, key, monkeypatch)
    def timeout(*a, **kw):
        raise common.ZhipuError("key_list", "timeout", timeout_phase="connect")
    monkeypatch.setattr(common, "request", timeout)
    click("oa:zh:confirm:" + nonce)
    assert "超时" in cards[-1][0] and "未提交" in cards[-1][0]
    assert any(b["text"] == "重新确认创建" for b in buttons(cards[-1]))
    assert not any(kw.get("method") == "POST" for _, kw in state["calls"])


def test_cancel_confirmation_does_not_execute(reset_env, cards, monkeypatch):
    control, key, state, _ = reset_env
    nonce = confirm_plan(control, key, monkeypatch)
    click("oa:zh:cancel_action:" + nonce)
    assert states.get_state(42) is None
    assert not state["calls"]
