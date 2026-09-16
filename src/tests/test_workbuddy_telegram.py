"""Real Telegram renderers/dispatchers with isolated config and shared controls."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src import config, oauth_manager as om, state_db
from src.management_control.oauth import OAuthControl
from src.oauth.workbuddy import billing, common
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as menu, workbuddy_oauth_menu as wb
from src.tests.management_oauth_fakes import ImmediateExecutor
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account


@pytest.fixture
def tg(action_env, monkeypatch):
    key, ledger = action_env
    clock = [datetime.now(timezone.utc)]
    ctl = OAuthControl(clock=lambda: clock[0], executor=ImmediateExecutor())
    monkeypatch.setattr(ctl, "_post_save_account_effects", lambda *a, **k: {})
    monkeypatch.setattr(menu, "oauth_control", ctl)
    monkeypatch.setattr(wb, "oauth_control", ctl)
    # Content/contract tests run deterministically; separate runtime regressions
    # exercise the real background thread and view-token cancellation boundary.
    monkeypatch.setattr(wb, "_start_query_worker", lambda worker: worker())
    ledger["login_workers"] = []
    monkeypatch.setattr(wb, "_start_login_worker", ledger["login_workers"].append)
    wb._login_jobs.clear()
    monkeypatch.setattr(ui, "answer_cb", Mock())
    output = []
    def emit(chat, *args, **kwargs):
        text = args[-1]
        output.append((text, kwargs.get("reply_markup")))
        return {"ok": True, "result": {"message_id": 900}}
    monkeypatch.setattr(ui, "edit", emit)
    monkeypatch.setattr(ui, "send", emit)
    monkeypatch.setattr(ui, "send_result", emit)
    config.update(lambda c: (c.update(oauthUsageDisplayMode="used", quotaProgressBar=True), c["oauthAccounts"][0].update(
        label="研发号 A", models=["glm-fixture", "deepseek-fixture"], disabledModels=["deepseek-fixture"])))
    credits = {"capacity": 500, "remaining": 320, "used": 180, "reliable": True, "scope": "personal"}
    snap = {"realm": "cn", "scope": "personal", "complete": True, "status": "known", "fetched_at": int(time.time()*1000),
            "credits": credits, "personal_credits": credits, "packages": [dict(credits, name="包 A", expires_at="2030-09-30T15:59:59Z")],
            "checkin": {"active": True, "today_checked_in": False, "observed_at": int(time.time()*1000)}}
    def save(value):
        state_db.quota_save(key, {"raw_data": json.dumps({"workbuddy": value})})
    save(snap)
    states.clear_all()
    yield key, ctl, ledger, output, snap, save, clock
    for job in wb._login_jobs.values():
        job["stop"].set()
        job["wake"].set()
    wb._login_jobs.clear()
    states.clear_all()


def buttons(kb):
    return [b for row in kb["inline_keyboard"] for b in row]


def click(tg, contains, *, chat=42):
    candidate = next(b for b in buttons(tg[3][-1][1]) if contains in b["text"])
    assert len(candidate["callback_data"].encode()) <= 64
    assert menu.handle_callback(chat, 900, "cb", candidate["callback_data"])
    return candidate["callback_data"]


def open_page(tg, kind):
    nav = (ui.register_code(tg[0]), 3, "quota")
    assert menu.handle_callback(42, 900, "cb", wb._cb(kind, nav))
    return nav


def test_list_and_detail_keep_baseline_order_stats_and_fixed_buttons(tg, monkeypatch):
    key, ctl, ledger, output, snap, save, _ = tg
    stats = dict(total=123, success_count=120, error_count=3, input=300000, output=80000,
                 cache_creation=0, cache_read=900000, avg_tps=58.2, max_tps=86.4, min_tps=31.7,
                 costed_success=120, unpriced_success=0, cost_ticks=1240000000)
    monkeypatch.setattr(menu, "_account_period_stats", lambda *a, **k: stats)
    monkeypatch.setattr(ctl, "evaluate_cached_quota_raw", Mock(side_effect=AssertionError("WB browse must not mutate")))
    monkeypatch.setattr(ctl, "refresh_workbuddy_status_now", Mock(side_effect=AssertionError("WB browse must not refresh")))
    text, kb = menu._list_text_and_kb()
    for a, b in zip(["研发号 A", "🏷️ 账户", "🪙 积分", "💎 本地自然月", "⚡ TPS", "💵"], ["🏷️ 账户", "🪙 积分", "💎 本地自然月", "⚡ TPS", "💵", None]):
        if b: assert text.index(a) < text.index(b)
    assert ui.provider_tag("workbuddy") in text and "████░░░░░░" in text and "36.00%" in text
    assert 'emoji-id="6120617435214132136"' in text
    assert buttons(kb)[0]["icon_custom_emoji_id"] == "6120617435214132136"
    assert "Token:" not in text and "UID" not in text and "协议" not in text and "fixture-at" not in text
    detail, kb = menu._detail_text_and_kb(
        key, refresh_quota=False, actor_chat_id=42,
        model_stats=[dict(stats, final_model="glm-fixture")],
    )
    assert ui.provider_tag("workbuddy") in detail
    assert "总体: 123 次 · ✅ 120 · ❌ 3" in detail
    assert "按模型:" in detail and "glm-fixture" in detail and "缓存" in detail and "峰值" in detail and "累计金额" in detail
    assert "📦 资源包: 1 个" in detail and "🎁 签到: 今日未签到" in detail
    assert [[b["text"] for b in row] for row in kb["inline_keyboard"]][:6] == [
        ["🔄 刷新 Token", "📊 刷新额度"], ["管理模型", "🚦 并发上限"],
        ["🧹 清模型故障", "🔗 清亲和绑定"], ["⏸ 停用账户", "🗑 删除账户"],
        ["🎁 签到/领额度"], ["🏠 主菜单", "◀ 返回列表"]]
    manage = kb["inline_keyboard"][1][0]
    assert manage["callback_data"].startswith("mc:src:")
    assert manage["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("workbuddy")
    assert "包 A" in detail and "资源包到期:" in detail
    assert not any(b["text"] == "📦 积分明细" for b in buttons(kb))
    assert ledger["calls"] == 0


@pytest.mark.parametrize("mode,bar,pct,pattern", [("used", True, "36.00%", "████░░░░░░"), ("remaining", True, "64.00%", "██████░░░░"), ("used", False, "36.00%", None)])
def test_credit_mode_and_progress_bar(tg, mode, bar, pct, pattern):
    config.update(lambda c: c.update(oauthUsageDisplayMode=mode, quotaProgressBar=bar))
    text = wb.usage_block(tg[0])
    assert pct in text
    if pattern: assert pattern in text
    else: assert "█" not in text and "░" not in text


def test_unknown_partial_old_snapshot_scope_and_checkin_date(tg):
    key, _, _, _, snap, save, _ = tg
    snap["credits"].pop("capacity")
    save(snap)
    text = wb.usage_block(key)
    assert "总量未知" in text and "%" not in text and "█" not in text
    snap["complete"] = False
    snap["credits"] = {"reliable": False, "scope": "enterprise"}
    snap["last_success_credits"] = {"remaining": 80, "capacity": 100, "used": 20, "reliable": True, "scope": "enterprise"}
    snap["last_success_at"] = int(time.time()*1000)-60000
    snap["checkin"]["observed_at"] -= 86400000
    save(snap)
    text = wb.usage_block(key, detail=True)
    assert "旧快照" in text and "企业积分" in text and "个人积分（独立）" in text and "今日状态未知" in text
    om.set_enabled(key, False, reason="quota")
    listed = menu._format_account_block(om.get_account(key))
    assert "🔒" in listed and "[配额禁用]" in listed and "预计" not in listed


def test_package_pagination_navigation_and_callback_size(tg):
    key, _, ledger, _, snap, save, _ = tg
    snap["packages"] = [dict(snap["packages"][0], name=f"资源包 {i}") for i in range(9)]
    save(snap)
    nav = open_page(tg, "credits")
    assert "资源包 0" in tg[3][-1][0] and "资源包 4" not in tg[3][-1][0]
    click(tg, "2 ▶")
    assert "资源包 4" in tg[3][-1][0] and "资源包 0" not in tg[3][-1][0]
    back = next(b for b in buttons(tg[3][-1][1]) if b["text"] == "◀ 返回列表")
    assert back["callback_data"] == menu._page_callback(3, "quota")
    assert "⏳ Token:" in tg[3][-1][0]
    manage = next(b for b in buttons(tg[3][-1][1]) if b["text"] == "管理模型")
    assert manage["callback_data"].startswith("mc:src:")
    assert manage["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("workbuddy")
    assert not any(b["text"] == "📦 积分明细" for b in buttons(tg[3][-1][1]))
    assert ledger["calls"] == 0


def test_activity_page_is_read_only_confirm_cancel_and_single_dispatch(tg):
    key, _, ledger, output, _, _, _ = tg
    open_page(tg, "activity")
    assert ledger["calls"] == 0
    click(tg, "立即签到")
    old_confirm = next(b["callback_data"] for b in buttons(output[-1][1]) if "确认执行" in b["text"])
    assert ledger["calls"] == 0
    click(tg, "取消")
    menu.handle_callback(42, 900, "cb", old_confirm)
    assert ledger["calls"] == 0
    open_page(tg, "activity")
    click(tg, "立即签到")
    done_cb = click(tg, "确认执行")
    assert ledger["calls"] == 1 and "本次获得 3.00 分" in output[-1][0]
    menu.handle_callback(42, 900, "cb", done_cb)
    assert ledger["calls"] == 1
    open_page(tg, "activity")
    assert not any("立即签到" in b["text"] for b in buttons(output[-1][1]))


def test_confirm_is_bound_to_chat_and_old_nonce_cannot_change_new_plan(tg):
    key, _, ledger, output, _, _, _ = tg
    open_page(tg, "activity")
    click(tg, "立即签到")
    cb = next(b["callback_data"] for b in buttons(output[-1][1]) if "确认执行" in b["text"])
    menu.handle_callback(99, 900, "cb", cb)
    assert ledger["calls"] == 0 and states.get_state(42)
    open_page(tg, "activity")
    click(tg, "立即签到")
    menu.handle_callback(42, 900, "cb", cb)
    assert ledger["calls"] == 0


def test_unknown_outcome_only_reconciles_never_replays(tg, monkeypatch):
    key, _, ledger, output, _, _, _ = tg
    def timeout(*a, **k):
        ledger["calls"] += 1
        raise common.WorkBuddyError("checkin", kind="network")
    monkeypatch.setattr(billing, "execute_action_sync", timeout)
    open_page(tg, "activity")
    click(tg, "立即签到")
    click(tg, "确认执行")
    assert "结果未知" in output[-1][0] and ledger["calls"] == 1
    open_page(tg, "activity")
    assert not any("立即签到" in b["text"] for b in buttons(output[-1][1]))
    ledger["checked"] = True
    click(tg, "核对结果")
    assert ledger["calls"] == 1 and "已完成" in output[-1][0]


def test_unknown_status_additional_ack_and_balance_failure_wording(tg, monkeypatch):
    key, _, ledger, output, _, _, _ = tg
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {"active": None, "today_checked_in": None})
    async def fail(*a, **k): raise ValueError("fixture failed balance")
    monkeypatch.setattr(om, "fetch_usage_snapshot", fail)
    open_page(tg, "activity")
    click(tg, "立即签到")
    assert "状态未知" in output[-1][0] and ledger["calls"] == 0
    click(tg, "接受风险")
    assert ledger["calls"] == 0
    click(tg, "确认执行")
    assert ledger["calls"] == 1 and "已完成" in output[-1][0] and "余额待更新" in output[-1][0]


def test_protection_stops_token_but_not_confirmed_activity(tg, monkeypatch):
    key, _, ledger, output, _, _, _ = tg
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    menu.on_refresh_token(42, 900, "cb", ui.register_code(key))
    assert "已阻止刷新" in output[-1][0] and "Token 已刷新" not in output[-1][0]
    open_page(tg, "activity")
    assert "保护模式暂停执行" not in output[-1][0]
    click(tg, "立即签到")
    assert ledger["calls"] == 0
    click(tg, "确认执行")
    assert ledger["calls"] == 1 and "已完成" in output[-1][0]


def test_auto_checkin_needs_confirmation_cas_and_is_not_immediate_action(tg):
    key, _, ledger, output, _, _, _ = tg
    open_page(tg, "activity")
    click(tg, "开启自动签到")
    assert not om.get_account(key).get("workbuddy_auto_checkin")
    assert "09:05" in output[-1][0]
    click(tg, "确认执行")
    assert om.get_account(key)["workbuddy_auto_checkin"] is True and ledger["calls"] == 0
    open_page(tg, "activity")
    click(tg, "关闭自动签到")
    config.update(lambda c: c["oauthAccounts"][0].update(label="并发修改"))
    click(tg, "确认执行")
    assert om.get_account(key)["workbuddy_auto_checkin"] is True
    assert "发生变化" in output[-1][0]


def test_login_automatic_save_manual_wake_cancel_and_secret_non_echo(tg, monkeypatch):
    key, ctl, ledger, output, _, _, clock = tg
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", lambda: {"status": "pending", "realm": "cn", "auth_url": "https://www.codebuddy.cn/login", "state": "fixture-state"})
    monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", lambda p: None)
    menu.handle_callback(42, 900, "cb", "oa:wb:login")
    click(tg, "检查登录")
    click(tg, "检查登录")
    assert len(ledger["login_workers"]) == 1  # Manual checks never duplicate workers.
    def authorize_next(_timeout):
        clock[0] += timedelta(seconds=5)
        monkeypatch.setattr(ctl.backend, "workbuddy_poll_login", lambda p: p.update(status="ready", entry=dict(om.get_account(key), access_token="login-fixture-at")))
    monkeypatch.setattr(wb._login_jobs[42]["wake"], "wait", authorize_next)
    ledger["login_workers"].pop(0)()
    assert any("等待浏览器授权" in text for text, _ in output)
    assert "授权已更新" in output[-1][0] and "研发号 A" in output[-1][0]
    assert not any("保存账户" in b["text"] or "更新此账户授权" in b["text"] for _, kb in output if kb for b in buttons(kb))
    assert states.get_state(42) is None and not wb._login_jobs
    assert om.get_account(key)["access_token"] == "login-fixture-at"
    assert "login-fixture-at" not in repr(output) and "fixture-rt" not in repr(output)
    menu.handle_callback(42, 900, "cb", "oa:wb:login")
    data = states.get_state(42)["data"].copy()
    click(tg, "取消")
    assert ctl.poll_login_flow(wb.telegram_context(42), data["flow_id"], data["flow_secret"]).status == "cancelled"
    assert states.get_state(42) is None and ledger["calls"] == 0


@pytest.mark.parametrize("callback", ["oa:wb:import", "oa:wb:import_page:old:2",
    "oa:wb:import_keep:old", "oa:wb:import_overwrite_ask:old", "oa:wb:import_overwrite:old"])
def test_old_import_callbacks_are_retired_without_writes(tg, monkeypatch, callback):
    _, ctl, ledger, output, _, _, _ = tg
    before = copy.deepcopy(config.get()["oauthAccounts"])
    parser = Mock(side_effect=AssertionError("removed parser must not run"))
    monkeypatch.setattr(ctl.backend, "parse_import", parser)
    states.set_state(42, "oa_wb_import_preview", {"nonce": "old", "secret": "old-fixture-credential"})
    assert menu.handle_callback(42, 900, "cb", callback)
    assert states.get_state(42) is None
    assert "JSON 导入已移除" in output[-1][0] and "old-fixture-credential" not in repr(output)
    parser.assert_not_called()
    assert config.get()["oauthAccounts"] == before and ledger["calls"] == 0


@pytest.mark.parametrize("action", ["oa_wb_import", "oa_wb_import_preview"])
@pytest.mark.parametrize("kind", ["text", "document"])
def test_old_import_input_states_do_not_parse_or_download_credentials(tg, monkeypatch, action, kind):
    _, ctl, ledger, output, _, _, _ = tg
    before = copy.deepcopy(config.get()["oauthAccounts"])
    blocked = Mock(side_effect=AssertionError("removed import must not read input"))
    monkeypatch.setattr(ctl.backend, "parse_import", blocked)
    monkeypatch.setattr(ui, "download_file", blocked)
    states.set_state(42, action, {"nonce": "old"})
    if kind == "text":
        assert menu.handle_text_state(42, action, "fixture-credential-not-to-echo")
    else:
        assert menu.handle_document_state(42, action, {"document": {"file_id": "fixture-credential-not-to-echo"}})
    blocked.assert_not_called()
    assert states.get_state(42) is None and "JSON 导入已移除" in output[-1][0]
    assert "fixture-credential-not-to-echo" not in repr(output)
    assert config.get()["oauthAccounts"] == before and ledger["calls"] == 0


def test_add_menu_only_offers_workbuddy_browser_login_and_keeps_other_imports(tg):
    menu.on_add_menu(42, 900, "cb")
    callbacks = [b.get("callback_data") for b in buttons(tg[3][-1][1])]
    assert [v for v in callbacks if v and v.startswith("oa:wb:")] == ["oa:wb:login", "oa:wb:login:global"]
    assert "oa:import:cpa" in callbacks and "oa:import:sub2api" in callbacks


def test_unused_workbuddy_has_zero_local_rows_without_inventing_tps(tg, monkeypatch):
    monkeypatch.setattr(menu, "_account_period_stats", lambda *a, **k: {})
    text, _ = menu._list_text_and_kb()
    assert "💎 本地自然月: ↑ 0 · ↓ 0" in text
    assert "💵 自然月 $0.00" in text
    assert "⚡ TPS:" not in text
    pending = menu._format_account_block(om.get_account(tg[0]), stats_loading=True)
    assert "💵 自然月 $0.00" not in pending


def test_package_cycle_boundary_and_expiry_are_distinct_in_detail(tg):
    key, _, _, _, snap, save, _ = tg
    snap["packages"][0].update(cycle_end="2026-09-30T15:59:59Z", expires_at="2030-09-30T15:59:59Z")
    save(snap)
    text, _ = menu._detail_text_and_kb(key, refresh_quota=False)
    assert "本周期结束: 2026-09-30 23:59:59" in text
    assert "资源包到期: 2030-09-30 23:59:59" in text


def test_global_trial_requires_terms_then_confirmation_not_auto(tg):
    oldkey, _, ledger, output, _, _, _ = tg
    entry = dict(om.get_account(oldkey), realm="global", domain="www.workbuddy.ai")
    config.update(lambda c: c.update(oauthAccounts=[entry]))
    key = om.get_account_key(entry)
    nav = (ui.register_code(key), 1, "all")
    menu.handle_callback(42, 900, "cb", wb._cb("activity", nav))
    assert "申请试用额度" in output[-1][0] and "自动签到" not in output[-1][0]
    click(tg, "申请试用额度")
    assert "免费" in output[-1][0] and ledger["calls"] == 0
    click(tg, "已核实免费条件")
    assert ledger["calls"] == 0
    click(tg, "确认执行")
    assert ledger["calls"] == 1
    assert "250" not in repr(output) and "14 天" not in repr(output)


def test_removed_import_callback_does_not_cancel_a_new_login(tg, monkeypatch):
    _, ctl, _, _, _, _, _ = tg
    cancel = Mock(side_effect=AssertionError("old import button cannot cancel a new login"))
    monkeypatch.setattr(ctl, "cancel_login_flow", cancel)
    states.set_state(42, "oa_wb_login", {"nonce": "new-login", "flow_id": "new-flow"})
    before = copy.deepcopy(states.get_state(42))
    assert menu.handle_callback(42, 900, "cb", "oa:wb:import_overwrite:old")
    assert states.get_state(42) == before
    cancel.assert_not_called()


def test_model_details_and_common_account_controls_for_no_email_wb(tg, monkeypatch):
    from src.telegram.menus import oauth_account_models_menu as models_menu
    key, ctl, ledger, output, _, _, _ = tg
    monkeypatch.setattr(models_menu, "oauth_control", ctl)
    config.update(lambda c: c["oauthAccounts"][0].update(models=["wb-ui-model"], account_model_catalog={"models": [
        {"id": "wb-ui-model", "maxInputTokens": 100000, "maxOutputTokens": 8192, "reasoningEfforts": ["low", "high"]}]}))
    text, kb = models_menu._detail_render(key, "wb-ui-model", model_page=1, account_page=1, filter_key="all")
    assert "最大输入" in text and "最大输出" in text and "high" in text and "上下文:" not in text
    short = ui.register_code(key)
    menu.on_toggle(42, 900, "cb", short)
    assert om.get_account(key)["disabled_reason"] == "user"
    menu.on_toggle(42, 900, "cb", short)
    assert om.get_account(key)["enabled"]
    menu.on_edit_max_concurrent(42, 900, "cb", short)
    menu.handle_text_state(42, "oa_emax", "7")
    assert om.get_account(key)["maxConcurrent"] == 7
    om.set_disabled_by_quota(key, None)
    menu.on_reset_quota(42, 900, "cb", short)
    assert om.get_account(key)["disabled_reason"] is None
    menu.on_clear_errors(42, 900, "cb", short)
    menu.on_clear_affinity(42, 900, "cb", short)
    menu.on_delete_ask(42, 900, "cb", short)
    assert "研发号 A" in output[-1][0] and ledger["calls"] == 0


def test_explicit_refresh_all_includes_no_email_wb_browse_does_not(tg):
    key = tg[0]
    account = om.get_account(key)
    assert not account.get("email")
    assert menu._refreshable_account_keys_for_ui([account]) == []
    assert menu._refreshable_account_keys_for_ui([account], explicit=True) == [key]
    om.set_enabled(key, False, reason="auth_error")
    assert key in {om.get_account_key(item) for item in menu._invalid_accounts()}
