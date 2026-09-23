"""Zhipu uses the existing OAuth list/detail UI, not a parallel quota dump."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone

import pytest
from src import config, log_db, oauth_manager as om, state_db
from src.oauth.zhipu import runtime
from src.telegram import ui, states
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_zhipu_provider import account_env, credential

RESET = "2026-09-27T02:01:10.997000+00:00"


@pytest.fixture
def display(account_env, monkeypatch):
    log_db.init()
    config.update(lambda c: c.update(oauthUsageDisplayMode="used", quotaProgressBar=True))
    account = credential("api_key", "zai", models=["GLM-5.3"], last_refresh="2026-09-23T08:29:42Z")
    om.add_account(account)
    key = om.get_account_key(account)
    stats = dict(total=3, success_count=3, error_count=0, input=1200, output=300,
        cache_creation=0, cache_read=0, avg_tps=30, max_tps=40, min_tps=20,
        costed_success=0, unpriced_success=3, cost_ticks=0)
    monkeypatch.setattr(menu, "_account_period_stats", lambda *a, **k: stats)
    windows = []
    def window_detail(account_key, since, indent, window_name):
        windows.append(window_name)
        return indent + "↑ 1.2K · ↓ 300"
    monkeypatch.setattr(menu, "_window_usage_detail", window_detail)
    raw = {"limits": [
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 4000, "currentValue": 5, "remaining": 3995},
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 0},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 27,
         "nextResetTime": datetime.fromisoformat(RESET).timestamp() * 1000}]}
    quota_windows, limits = runtime.normalize_usage(raw)
    usage = {**quota_windows, "zhipu": {"windows": quota_windows, "limits": limits,
        "fetched_at": time.time() * 1000, "status": "known", "mcp": {}}}
    def save(value):
        state_db.quota_save(key, om.flatten_usage(value))
    save(usage)
    return key, usage, save, windows


def render(key):
    listed, list_kb = menu._list_text_and_kb()
    detail, detail_kb = menu._detail_text_and_kb(key, refresh_quota=False,
        actor_chat_id=42, model_stats=[], stats_loading=False)
    return listed, list_kb, detail, detail_kb


def test_actual_list_detail_share_quota_helpers_and_baseline_layout(display):
    key, usage, _, window_calls = display
    before = copy.deepcopy(om.get_account(key))
    cache_before = copy.deepcopy(state_db.quota_load(key))
    listed, list_kb, detail, kb = render(key)
    assert menu._format_usage_line_html("📊 5h", 0, None) in listed
    assert menu._format_usage_line_html("📊 7d", 27, RESET) in listed
    assert menu._format_usage_line_text("⏱ 5h", 0, None) in detail
    assert menu._format_usage_line_text("📅 7d", 27, RESET) in detail
    assert "2026-09-27 10:01:10" in detail and "上游未返回" in detail
    for text in (listed, detail):
        assert "Z.ai API Key · " in text
        assert "🛠 工具/月: 剩余 <b>3,995</b> 次" in text
        assert "███░░░░░░░" in text
        assert "↑ 1.2K · ↓ 300" in text
        assert 'emoji-id="6140727700454645813"' in text
        for forbidden in ("key-", "原始额度", "currentValue", "TOKENS_LIMIT", "TIME_LIMIT", "+00:00", "Token:", "原值不换算", "fixture.secret"):
            assert forbidden not in text
    assert "更新于" in detail and "模型目录: 1 个 · 已启用 1 · 禁用 0（全局 0 · 账号 0）" in detail
    assert "🔄 刷新:" not in detail
    assert listed.index("🏷️ 套餐") < listed.index("📊 5h") < listed.index("📊 7d") < listed.index("🛠 工具/月")
    assert {"5h", "7d"} <= set(window_calls)
    assert [[button["text"] for button in row] for row in kb["inline_keyboard"]] == [
        ["🔄 刷新连接", "📊 刷新额度"], ["管理模型", "🚦 并发上限"],
        ["🧹 清模型故障", "🔗 清亲和绑定"], ["⏸ 停用账户", "🗑 删除账户"],
        ["✏️ 账户名称"], ["🏠 主菜单", "◀ 返回列表"]]
    assert all(len(button.get("callback_data", "").encode()) <= 64 for row in kb["inline_keyboard"] for button in row)
    assert om.get_account(key) == before and state_db.quota_load(key) == cache_before
    print("\n--- Zhipu list preview ---\n" + ui._strip_html_tags(listed))
    print("\n--- Zhipu detail preview ---\n" + ui._strip_html_tags(detail))


@pytest.mark.parametrize("mode,bar,expected", [("used", True, "27%"), ("remaining", True, "73%"),
    ("remaining", False, "73%")])
def test_used_remaining_and_progress_preferences_apply_to_both_views(display, mode, bar, expected):
    key, _, _, _ = display
    config.update(lambda c: c.update(oauthUsageDisplayMode=mode, quotaProgressBar=bar))
    listed, _, detail, _ = render(key)
    for text in (listed, detail):
        assert expected in text
        assert ("█" in text) is bar
        if mode == "remaining":
            assert "100%" in text and "已用" not in text


def test_unknown_values_not_invented_and_stale_values_remain_visible(display):
    key, usage, save, _ = display
    usage["zhipu"]["fetched_at"] -= 1000000
    save(usage)
    assert "旧快照" in render(key)[0] and "旧快照" in render(key)[2]
    usage.pop("five_hour"); usage.pop("seven_day")
    usage["zhipu"].update(windows={}, limits=[{"type": "TIME_LIMIT", "unit": 5, "number": 1,
        "usage": 4000, "currentValue": 5}], fetched_at=time.time() * 1000)
    save(usage)
    listed, _, detail, _ = render(key)
    assert "尚未获取" in listed
    for text in (listed, detail):
        assert "27%" not in text and "0%" not in text and "4,000" not in text and "3,995" not in text
        assert "原始额度" not in text


def test_custom_label_preserved_and_escaped_only_at_display(display):
    key, _, _, _ = display
    account = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, account, lambda a: a.update(label="老号 <研发>"))
    listed, _, detail, _ = render(key)
    assert "老号 &lt;研发&gt;" in listed and "老号 &lt;研发&gt;" in detail
    assert om.get_account(key)["label"] == "老号 <研发>"


def test_oauth_team_detail_has_known_identity_without_api_key_expiry(display):
    account = credential("oauth", "bigmodel", plan_scope="team", organization_id="研发<组>", project_id="主项目",
        label="研发团队", models=["GLM-5.3"], entitlement="unassigned", management_status="relogin_required")
    om.add_account(account); key = om.get_account_key(account)
    listed = menu._format_account_block(om.get_account(key))
    detail, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=False)
    assert "未分配席位" in listed and "登录已过期" in detail
    assert "研发&lt;组&gt;" in detail and "主项目" in detail
    assert "模型目录: 1 个 · 已启用 1 · 禁用 0" in detail and "可用模型" not in detail
    assert "组织:" not in listed and "Token:" not in detail
    buttons = [button["text"] for row in kb["inline_keyboard"] for button in row]
    assert "🔄 更新凭据" in buttons and "♻️ 官方重置卡" in buttons
    assert "创建模型 Key（需确认）" not in buttons


def test_api_key_hides_reset_entry_and_old_callback_has_no_login_prompt(display, monkeypatch):
    key, _, _, _ = display
    output = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: output.append((a[2], k["reply_markup"])))
    monkeypatch.setattr(zh.control, "get_zhipu", lambda *a, **k: pytest.fail("API Key reset page must not call reset API"))
    detail, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=False)
    assert "重置卡" not in detail + str(kb)
    assert menu.handle_callback(42, 900, "cb", "oa:zh:reset:" + ui.register_code(key))
    assert "不支持" in output[-1][0]
    assert "OAuth 登录" not in str(output[-1]) and "oa:zh:login:" not in str(output[-1])


@pytest.mark.parametrize("globally,locally,expected", [
    (["glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7", "glm-5", "glm-5.1"], [],
     "11 个 · 已启用 5 · 禁用 6（全局 6 · 账号 0）"),
    (["glm-4.7"], ["glm-4.7", "glm-5.2", "not-in-catalog"],
     "11 个 · 已启用 9 · 禁用 2（全局 1 · 账号 2）"),
    (["not-in-catalog"], ["glm-5.2"],
     "11 个 · 已启用 10 · 禁用 1（全局 0 · 账号 1）"),
])
def test_model_switch_counts_include_global_and_account_disables(display, globally, locally, expected):
    key, _, _, _ = display
    models = ["glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7", "glm-5", "glm-5-turbo",
              "glm-5.1", "glm-5.2", "GLM-5.3", "GLM-5.3-Flash", "glm-5.3-flashx"]
    current = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, current, lambda a: a.update(models=models, disabledModels=locally))
    config.update(lambda c: c.update(modelCenter={"disabledModels": globally}))
    before = copy.deepcopy(config.get())
    detail = render(key)[2]
    assert "🧬 模型目录: " + expected in detail
    assert "可用模型" not in detail
    assert config.get() == before


def test_empty_account_manage_models_button_opens_real_model_center(display, monkeypatch):
    from src.management_control.models import ModelCenterControl
    from src.telegram.menus import model_center_menu as mc
    key, _, _, _ = display
    current = copy.deepcopy(om.get_account(key))
    om.mutate_account_if_unchanged(key, current, lambda a: a.update(models=[]))
    monkeypatch.setattr(mc, "_CONTROL", ModelCenterControl())
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    monkeypatch.setattr(mc, "_request_list_usage", lambda *a, **k: None)
    edits, popups = [], []
    monkeypatch.setattr(ui, "edit", lambda *a, **k: edits.append((a[2], k.get("reply_markup"))))
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: popups.append(a))
    _, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=False)
    button = next(b for row in kb["inline_keyboard"] for b in row if b["text"] == "管理模型")
    assert mc.handle_callback(42, 900, "cb", button["callback_data"])
    assert edits and "模型中心" in edits[-1][0] and "0 个" in edits[-1][0]
    assert "同步上游" in str(edits[-1][1])
    assert not any("已移除" in str(p) or "过期" in str(p) for p in popups)
    om.delete_account(key)
    assert mc.handle_callback(42, 900, "cb", button["callback_data"])
    assert "来源已变化" in str(popups[-1])
    mc.reset_for_tests()


def test_name_edit_changes_only_label_and_old_input_cannot_rename_replacement(display, monkeypatch):
    key, _, _, _ = display
    output = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: output.append(a[2]))
    monkeypatch.setattr(ui, "send", lambda *a, **k: output.append(a[1]))
    before = copy.deepcopy(om.get_account(key))
    assert menu.handle_callback(42, 900, "cb", "oa:zh:name:" + ui.register_code(key))
    assert menu.handle_text_state(42, "oa_zh_name", "智谱老号 <个人>")
    after = om.get_account(key)
    assert after == dict(before, label="智谱老号 <个人>")
    assert "智谱老号 &lt;个人&gt;" in menu._format_account_block(after)
    assert menu.handle_callback(42, 900, "cb", "oa:zh:name:" + ui.register_code(key))
    current = copy.deepcopy(after)
    om.mutate_account_if_unchanged(key, current, lambda a: a.update(generationId="replacement-generation"))
    assert menu.handle_text_state(42, "oa_zh_name", "不应保存")
    assert om.get_account(key)["label"] == "智谱老号 <个人>"
    assert "账户已变更" in output[-1]
    states.pop_state(42)


@pytest.mark.parametrize("skip", [False, True])
def test_key_import_prompts_for_optional_name(display, monkeypatch, skip):
    output = []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: output.append(a[2]))
    monkeypatch.setattr(ui, "send", lambda *a, **k: output.append(a[1]))
    monkeypatch.setattr(zh.control, "_post_save_account_effects", lambda *a, **k: {})
    assert menu.handle_callback(42, 900, "cb", "oa:zh:key:bigmodel")
    assert menu.handle_text_state(42, "oa_zh_input", "new-import.fixture-secret")
    current = states.get_state(42)
    assert current["action"] == "oa_zh_name" and "请发送账户名称" in output[-1]
    key = current["data"]["key"]
    if skip:
        assert menu.handle_callback(42, 900, "cb", "oa:zh:name_skip:" + current["data"]["nonce"])
        assert om.get_account(key)["label"] == om.get_account(key)["subject"]
    else:
        assert menu.handle_text_state(42, "oa_zh_name", "中国站老号")
        assert om.get_account(key)["label"] == "中国站老号"
    assert states.get_state(42) is None


def test_quota_pause_without_known_reset_has_no_fake_estimate(display):
    key, _, _, _ = display
    om.set_enabled(key, False, reason="quota")
    text = menu._format_account_block(om.get_account(key))
    assert "[配额禁用]" in text and "预计 ?" not in text
