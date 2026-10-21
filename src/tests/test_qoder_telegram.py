"""Qoder Telegram pages and callbacks with isolated config and the shared OAuthControl."""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config, oauth_manager as om, state_db
from src.management_control.oauth import OAuthControl
from src.oauth.qoder import auth, device_profile
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, qoder_oauth_menu as qd
from src.tests.management_oauth_fakes import ImmediateExecutor


class InlineThread:
    def __init__(self, target=None, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def tg(monkeypatch):
    from src.tests import test_protocol_fake_upstreams as fake
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    entry = auth.normalize_credential({"realm": "global", "uid": "qd-" + uuid.uuid4().hex, "email": "q@example.com",
        "access_token": "jt", "refresh_token": "jrt", "expired": "2099-01-01T00:00:00Z", "machine_id": "m",
        "device_token": "dt", "device_refresh_token": "drt"})
    entry["qoder_device_profile"] = device_profile.generate("m")
    entry.update(label="Qoder 主号", models=["ultimate"])
    config.update(lambda c: c.update(oauthAccounts=[entry], channels=[]))
    key = om.get_account_key(om.get_account("qoder:" + auth.identity(entry)))
    ctl = OAuthControl(clock=lambda: datetime.now(timezone.utc), executor=ImmediateExecutor())
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    monkeypatch.setattr(menu, "oauth_control", ctl)
    monkeypatch.setattr(qd, "oauth_control", ctl)
    monkeypatch.setattr(qd, "threading", SimpleNamespace(**{**vars(qd.threading), "Thread": InlineThread}))
    monkeypatch.setattr(ui, "answer_cb", Mock())
    output = []

    def emit(chat, *args, **kwargs):
        output.append((args[-1], kwargs.get("reply_markup")))
        return {"ok": True, "result": {"message_id": 900}}

    for name in ("edit", "send", "send_result"):
        monkeypatch.setattr(ui, name, emit)
    state_db.quota_save(key, {"raw_data": json.dumps({"qoder": {
        "plan": "Pro", "until": "2030-01-01T00:00:00Z", "fetched_at": int(time.time() * 1000), "status": "ok",
        "pools": [{"kind": "user", "name": "Credits", "total": 2000, "used": 500, "remaining": 1500, "used_percent": 25.0, "unit": "credits"}]}})})
    state_db.qoder_checkin_forget(key)
    states.clear_all()
    qd._login_jobs.clear()
    yield key, ctl, output
    states.clear_all()
    qd._login_jobs.clear()
    config.update(lambda c: (c.clear(), c.update(before)))


def buttons(kb):
    return [b for row in kb["inline_keyboard"] for b in row]


def click(output, contains, *, chat=42):
    candidate = next(b for b in buttons(output[-1][1]) if contains in b["text"])
    assert len(candidate["callback_data"].encode()) <= 64
    assert menu.handle_callback(chat, 900, "cb", candidate["callback_data"])


def open_detail(key):
    assert menu.handle_callback(42, 900, "cb", "oa:view:" + ui.register_code(key) + ":1")


def test_detail_shows_route_credits_and_daily_credit_controls(tg, monkeypatch):
    key, ctl, output = tg
    monkeypatch.setattr("src.oauth.qoder.billing.runtime_info_path", lambda **kw: "/bin/true")
    open_detail(key)
    text, kb = output[-1]
    assert "Qoder 国际 · qoder.com" in text and "设备授权" in text
    assert 'emoji-id="6251484272679462431"' in text
    device_button = next(b for b in buttons(kb) if b["text"] == "设备档案 / 重授权")
    assert device_button["icon_custom_emoji_id"] == "6251484272679462431"
    assert "积分: 剩余 <code>1,500</code> / 2,000（已用 25%）" in text and "Pro" in text
    assert "每日签到: 尚未执行" in text
    labels = [b["text"] for b in buttons(kb)]
    assert "🎁 立即签到" in labels and "⬜ 每日自动签到" in labels


def test_checkin_now_and_auto_toggle_render_on_the_account(tg, monkeypatch):
    key, ctl, output = tg
    calls = []

    def checkin(account_id):
        calls.append(account_id)
        record = {"business_date": datetime.now(qd._BJT).strftime("%Y-%m-%d"), "status": "claimed", "credit": 100,
                  "at": time.time(), "source": "manual", "realm": "global"}
        state_db.qoder_checkin_save(account_id, record)
        return record

    monkeypatch.setattr(ctl.backend, "qoder_checkin", checkin)
    monkeypatch.setattr("src.oauth.qoder.billing.runtime_info_path", lambda **kw: "/bin/true")
    open_detail(key)
    click(output, "立即签到")
    assert calls == [key]
    assert output[-1][0].startswith("✅ 已领取今日积分 +100") and "每日签到: 今日已领取 +100" in output[-1][0]
    click(output, "每日自动签到")
    assert om.get_account(key)["qoder_auto_checkin"] is True
    assert "已开启每日自动签到" in output[-1][0]
    assert any(b["text"] == "✅ 每日自动签到" for b in buttons(output[-1][1]))


def test_legacy_account_requires_explicit_device_initialization(tg, monkeypatch):
    key, ctl, output = tg
    config.update(lambda c: c["oauthAccounts"][0].pop("qoder_device_profile"))
    open_detail(key)
    assert "请先确认设备档案并重授权" in output[-1][0]


def test_add_menu_device_login_saves_the_account(tg, monkeypatch):
    key, ctl, output = tg
    monkeypatch.setattr(ctl.backend, "qoder_start_login", lambda *, realm, profile: {
        "realm": realm, "nonce": "n", "verifier": "v", "machine_id": profile["machine_id"], "qoder_device_profile": profile, "status": "pending",
        "auth_url": "https://qoder.cn/device/selectAccounts?x=1"})

    def poll(payload):
        payload.update(status="ready", entry=auth.normalize_credential({"realm": payload["realm"], "uid": "cn-user",
            "nickname": "CN 用户", "access_token": "dt", "refresh_token": "drt", "machine_id": payload["machine_id"],
            "device_token": "dt", "device_refresh_token": "drt", "qoder_device_chat": True,
            "qoder_device_profile": payload["qoder_device_profile"]}, source="login"))

    monkeypatch.setattr(ctl.backend, "qoder_poll_login", poll)
    assert menu.handle_callback(42, 900, "cb", "oa:add:qoder")
    assert 'emoji-id="6251484272679462431"' in output[-1][0]
    labels = [b["text"] for b in buttons(output[-1][1])]
    assert labels[:3] == ["🌐 国际区 · 浏览器授权", "🔑 国际区 · PAT", "🇨🇳 中国区 · 浏览器授权"]
    click(output, "中国区 · 浏览器授权")
    assert states.get_state(42)["action"] == "oa_qd_device"
    assert 'emoji-id="6251484272679462431"' in output[-1][0]
    click(output, "确认并继续")
    assert any("Qoder 中国区登录" in text for text, _ in output)
    assert "✅ 授权已保存" in output[-1][0] and "设备 Token" in output[-1][0]
    saved = om.get_account("qoder:cn:cn-user")
    assert saved and saved["qoder_device_chat"] is True and saved["machine_id"] == saved["qoder_device_profile"]["machine_id"]


def test_pat_input_creates_an_international_pat_account(tg, monkeypatch):
    key, ctl, output = tg
    seen = []

    def pat_login(pat, *, realm, profile, pending=None):
        seen.append((pat, realm))
        return auth.normalize_credential({"realm": realm, "uid": "pat-user", "access_token": "jt", "refresh_token": "",
            "qoder_auth_method": "pat", "qoder_pat": pat, "machine_id": profile["machine_id"], "qoder_device_profile": profile}, source="login")

    monkeypatch.setattr(ctl.backend, "qoder_pat_login", pat_login)
    assert menu.handle_callback(42, 900, "cb", "oa:add:qoder")
    click(output, "国际区 · PAT")
    assert states.get_state(42)["action"] == "oa_qd_device"
    assert seen == []
    click(output, "确认并继续")
    assert states.get_state(42)["action"] == "oa_qd_pat"
    assert 'emoji-id="6251484272679462431"' in output[-1][0]
    assert menu.handle_text_state(42, "oa_qd_pat", "  pt-secret  ")
    assert seen == [("pt-secret", "global")]
    saved = om.get_account("qoder:global:pat-user")
    assert saved and saved["qoder_auth_method"] == "pat" and saved["qoder_pat"] == "pt-secret"
    assert "✅ 授权已保存" in output[-1][0]
    assert "pt-secret" not in "".join(text for text, _ in output)


def test_draft_regenerate_old_button_and_cancel_are_local(tg, monkeypatch):
    key, ctl, output = tg
    start = Mock(side_effect=AssertionError("must not start upstream before confirmation"))
    monkeypatch.setattr(ctl.backend, "qoder_start_login", start)
    menu.handle_callback(42, 900, "cb", "oa:add:qoder")
    click(output, "国际区 · 浏览器授权")
    old = dict(states.get_state(42)["data"])
    stale = next(b["callback_data"] for b in buttons(output[-1][1]) if "确认" in b["text"])
    click(output, "重新生成")
    current = states.get_state(42)["data"]
    assert current["revision"] != old["revision"] and current["flow_id"] == old["flow_id"]
    fresh_kb = output[-1][1]
    menu.handle_callback(42, 900, "cb", stale)
    assert states.get_state(42)["data"] is current and not start.called
    cancel = next(b["callback_data"] for b in buttons(fresh_kb) if "取消" in b["text"])
    menu.handle_callback(42, 900, "cb", cancel)
    assert states.get_state(42) is None and not start.called
    from src.management_control.oauth.menu_bridge import telegram_context
    assert ctl.poll_login_flow(telegram_context(42), old["flow_id"], old["flow_secret"]).status == "cancelled"


def test_login_page_uses_original_preparation_deadline(tg, monkeypatch):
    from src.management_control.oauth import OAuthProvider
    from src.tests.test_workbuddy_lifecycle import context
    key, ctl, output = tg
    flow = ctl.start_login_flow(context(), OAuthProvider.QODER)
    expires = flow.expires_at
    # Rendering/confirmation must not promise a fresh fifteen-minute lifetime.
    from dataclasses import replace
    started = replace(flow, auth_url='https://qoder.com/device/test')
    monkeypatch.setattr(qd, '_login_poll', lambda *a, **kw: None)
    qd._login_started(42, 900, started, realm='global')
    assert expires.isoformat() in output[-1][0] and '15分钟' not in output[-1][0]
    assert '从准备档案起计时' in output[-1][0]
    assert 'emoji-id="6251484272679462431"' in output[-1][0]
    link = next(b for b in buttons(output[-1][1]) if b.get("url"))
    assert link["url"] == started.auth_url and link["icon_custom_emoji_id"] == "6251484272679462431"
    _, kb = qd._login_feedback(states.get_state(42)["data"])
    assert next(b for b in buttons(kb) if b.get("url"))["icon_custom_emoji_id"] == "6251484272679462431"
