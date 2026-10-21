"""Qoder list/detail views show current settings and the saved generated profile."""
import copy
import json
import time
from datetime import datetime

import pytest

from src import config, oauth_manager as om, state_db
from src.oauth.qoder import device_profile
from src.telegram.menus import oauth_menu as menu, qoder_oauth_menu as qd
from src.tests.test_qoder_telegram import tg, open_detail, buttons


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("name", ["Credits", "Add-on credits", "Shared credits", "Dedicated credits"])
def test_credit_pool_label_is_always_jifen(name, detail):
    snapshot = {"usage": {"plan": "personal_standard", "pools": [{"kind": "add_on", "name": name,
        "remaining": 100, "total": 100, "used_percent": 0}]}, "checkin_supported": True}
    before = copy.deepcopy(snapshot)
    text = qd.usage_block("fixture", detail=detail, snapshot=snapshot)
    assert "💳 积分: 剩余 <code>100</code> / 100（已用 0%）" in text
    assert name not in text and snapshot == before


@pytest.mark.parametrize("auto", [False, True])
def test_list_only_shows_enabled_auto_checkin_not_claim_history(tg, auto):
    key, ctl, _ = tg
    config.update(lambda c: c["oauthAccounts"][0].update(qoder_auto_checkin=auto))
    state_db.qoder_checkin_save(key, {"business_date": datetime.now(qd._BJT).strftime("%Y-%m-%d"),
        "status": "claimed", "credit": 100, "at": time.time(), "source": "manual", "realm": "global"})
    text = menu._format_account_block(om.get_account(key))
    assert ("🎁 自动签到已开" in text) is auto
    assert "每日签到" not in text and "已领取" not in text and "+100" not in text
    assert "到期 2030-01-01 08:00" in text


@pytest.mark.parametrize("until", ["9999-12-31T00:00:00Z", "9999-12-31T23:59:59Z", 253402214400, 253402214400000])
@pytest.mark.parametrize("detail", [False, True])
def test_unlimited_plan_does_not_render_expiration(until, detail):
    snapshot = {"usage": {"plan": "personal_standard", "until": until}, "checkin_supported": True}
    text = qd.usage_block("fixture", detail=detail, snapshot=snapshot)
    assert "personal_standard" in text and "9999" not in text and "到期" not in text


def test_detail_keeps_claim_result_but_auto_state_is_only_in_button(tg):
    key, ctl, output = tg
    config.update(lambda c: c["oauthAccounts"][0].update(qoder_auto_checkin=True))
    state_db.qoder_checkin_save(key, {"business_date": datetime.now(qd._BJT).strftime("%Y-%m-%d"),
        "status": "claimed", "credit": 100, "at": time.time(), "source": "manual", "realm": "global"})
    open_detail(key)
    text, kb = output[-1]
    assert "🎁 每日签到: 今日已领取 +100" in text
    assert "自动签到已开" not in text and "自动签到" not in text
    assert any(b["text"] == "✅ 每日自动签到" for b in buttons(kb))
    assert "🖥️ 设备:" in text and "🌐 代理:" in text


@pytest.mark.parametrize("platform,model,architecture,memory", [
    ("macos-arm64", "Mac16,10", "ARM64", "内存: 16 GiB · 磁盘: 256 GiB"),
    ("windows-x64", "Generated Windows device", "x86_64", "可用内存: 8 GiB"),
    ("linux-x64", "Generated device", "x86_64", "内存: 8 GiB"),
    ("linux-arm64", "Generated device", "ARM64", "内存: 8 GiB"),
])
def test_hardware_display_comes_from_saved_profile_without_identity_changes(platform, model, architecture, memory):
    profile = device_profile.generate(platform=platform)
    before = copy.deepcopy(profile)
    digest = device_profile.fingerprint(profile)
    snapshot = {"usage": {}, "device": device_profile.summary({"qoder_device_profile": profile}),
                "proxy": "fixture-route", "checkin_supported": True}
    text = qd.usage_block("fixture", detail=True, snapshot=snapshot)
    assert model in text and "架构: " + architecture in text and memory in text
    assert "主板:" not in text and "CPU:" not in text and "未设置" not in text
    assert "档案 ID: <code>" + digest + "</code>" in text
    assert "🌐 代理: fixture-route" in text
    assert profile == before and device_profile.fingerprint(profile) == digest
    safe = json.dumps(snapshot["device"])
    assert "blocks" not in safe and "machine_id" not in safe and "token" not in safe


@pytest.mark.parametrize("hardware,expected", [
    ({}, []),
    ({"system": "", "architecture": None, "cpu": None, "motherboard": ""}, []),
    ({"cpu": "Example CPU"}, ["CPU: Example CPU"]),
    ({"motherboard": "Example board"}, ["主板: Example board"]),
    ({"cpu": "Example CPU", "motherboard": "Example board"}, ["主板: Example board · CPU: Example CPU"]),
])
def test_optional_hardware_fields_only_render_when_present(hardware, expected):
    lines = qd._device_lines({"status": "bound", "hardware": hardware})
    assert lines[1:] == expected
    assert "未设置" not in "\n".join(lines)


def test_detail_device_fields_are_html_escaped():
    text = "\n".join(qd._device_lines({"status": "bound", "profile_fingerprint": "<id>",
        "platform_label": "<platform>", "hardware": {"model": "<model>", "system": "<system>",
        "architecture": "<architecture>", "motherboard": "<board>", "cpu": "<cpu>"}}))
    for field in ("id", "platform", "model", "system", "architecture", "board", "cpu"):
        assert "<" + field + ">" not in text and "&lt;" + field + "&gt;" in text
