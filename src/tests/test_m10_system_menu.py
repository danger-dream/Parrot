"""M10 系统设置菜单测试。

覆盖：
  - 主设置页渲染（7 大项）
  - 超时 / 错误阶梯 输入：合法 / 非法
  - 评分参数：4 字段各自修改（范围校验）
  - 亲和参数：TTL 字段
  - CCH / OAuth 配额监控入口已迁移到 OAuth 账户设置，不再出现在系统设置
  - channelSelection 旧入口兼容：smart/order/priority 切换
  - 首包黑名单：加/删默认 + 加渠道专属
  - 整路径路由 + /settings 命令
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sys


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, log_db, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import load_balancing_menu, system_menu
    return {
        "config": config, "log_db": log_db, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
        "load_balancing_menu": load_balancing_menu,
        "system_menu": system_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}
    def by(self, m): return [d for mm, d in self.calls if mm == m]
    def last(self, m):
        l = self.by(m); return l[-1] if l else None
    def clear(self): self.calls.clear()


def _reset(m):
    m["state_db"].init()
    m["states"].clear_all()


def _install(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


# ─── Tests ───────────────────────────────────────────────────────

def test_main_page(m):
    _reset(m)
    rec = _install(m)
    m["system_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    text = edit["text"]
    # 主页应含系统项；负载均衡入口保留在主菜单，系统设置不重复展示。
    for s in ("超时", "错误阶梯", "评分", "亲和", "调度", "黑名单"):
        assert s in text, s
    assert "CCH" not in text
    assert "配额监控" not in text
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"]
            for b in row if "callback_data" in b]
    expected = {"sys:show:timeouts", "sys:show:errwin", "sys:show:scoring",
                "sys:show:affinity", "sys:show:notif", "menu:status_alert", "sys:show:retention",
                "sys:show:blacklist", "sys:show:aklim", "sys:show:ws_mode", "sys:show:retry",
                "menu:main"}
    for e in expected:
        assert e in btns, f"missing btn {e}"
    assert "menu:loadbalancing" not in btns
    assert "sys:show:cch" not in btns
    assert "sys:show:quota" not in btns
    bottom = edit["reply_markup"]["inline_keyboard"][-1]
    assert [button["callback_data"] for button in bottom] == ["sys:show:retry", "menu:main"]
    assert "候选按序故障转移" in text
    print("  [PASS] main settings page")


def test_retry_settings_menu_and_controls(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    original = json.loads(json.dumps(m["config"].get().get("retry") or {}))
    defaults = {
        "transient": {
            "enabled": True,
            "maxExtraAttempts": 2,
            "backoffSeconds": [0.75, 1.75],
            "errors": {
                "openaiServerOverloaded": True,
                "openaiServerError": True,
                "claudeOverloaded": True,
                "xaiUnavailable": True,
            },
        },
        "recovery": {
            "oauthRefresh": True,
            "invalidEncryptedContent": True,
            "claudeContext1mFallback": True,
        },
    }
    try:
        m["config"].update(lambda c: c.__setitem__("retry", json.loads(json.dumps(defaults))))
        sm._show_retry(42, 100, "cb")
        page = rec.last("editMessageText")
        assert "全请求共享 2 次" in page["text"]
        assert "仍失败则继续后续账号 / 渠道" in page["text"]
        assert "每个报错账号刷新后" in page["text"]
        callbacks = [
            button["callback_data"]
            for row in page["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        for expected_cb in (
            "sys:retry:toggle_transient",
            "sys:retry:edit_attempts",
            "sys:retry:edit_backoff",
            "sys:retry:toggle_error:openaiServerError",
            "sys:retry:toggle_recovery:oauthRefresh",
        ):
            assert expected_cb in callbacks

        sm._toggle_retry_transient(42, 100, "cb")
        assert m["config"].get()["retry"]["transient"]["enabled"] is False
        sm._toggle_retry_transient(42, 100, "cb")
        assert m["config"].get()["retry"]["transient"]["enabled"] is True

        sm._toggle_retry_item(
            42, 100, "cb", group="errors", key="openaiServerError",
        )
        assert m["config"].get()["retry"]["transient"]["errors"]["openaiServerError"] is False

        sm._edit_retry_attempts(42, 100, "cb")
        sm._on_retry_attempts_input(42, "6")
        assert m["states"].get_state(42) is not None
        sm._on_retry_attempts_input(42, "3")
        assert m["config"].get()["retry"]["transient"]["maxExtraAttempts"] == 3
        assert m["states"].get_state(42) is None

        sm._edit_retry_backoff(42, 100, "cb")
        sm._on_retry_backoff_input(42, "-1,2")
        assert m["states"].get_state(42) is not None
        sm._on_retry_backoff_input(42, "nan,1")
        assert m["states"].get_state(42) is not None
        sm._on_retry_backoff_input(42, "inf,1")
        assert m["states"].get_state(42) is not None
        sm._on_retry_backoff_input(42, "0.5, 1.25")
        assert m["config"].get()["retry"]["transient"]["backoffSeconds"] == [0.5, 1.25]
        assert m["states"].get_state(42) is None

        sm._show_notif(42, 100, "cb")
        assert "配额冷却" in rec.last("editMessageText")["text"]
    finally:
        m["config"].update(lambda c: c.__setitem__("retry", original))
        m["states"].clear_all()
    print("  [PASS] retry settings UI + hot config controls")


def test_timeouts_edit(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_timeouts(42, 100, "cb")
    assert rec.last("editMessageText") is not None

    sm._edit_timeouts(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_timeouts"

    # 非法输入
    sm._on_timeouts_input(42, "10,30,30")
    assert m["states"].get_state(42) is not None
    sm._on_timeouts_input(42, "a,b,c,d")
    assert m["states"].get_state(42) is not None
    sm._on_timeouts_input(42, "-1,30,30,600")
    assert m["states"].get_state(42) is not None

    # 合法输入
    sm._on_timeouts_input(42, "11, 31, 32, 650")
    assert m["states"].get_state(42) is None
    t = m["config"].get()["timeouts"]
    assert t["connect"] == 11 and t["firstByte"] == 31 and t["idle"] == 32 and t["total"] == 650
    print("  [PASS] timeouts edit")


def test_errwin_edit(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_errwin(42, 100, "cb")
    sm._edit_errwin(42, 100, "cb")
    # 非法
    sm._on_errwin_input(42, "")
    assert m["states"].get_state(42) is not None
    sm._on_errwin_input(42, "a,b")
    assert m["states"].get_state(42) is not None
    sm._on_errwin_input(42, "1,-1")
    assert m["states"].get_state(42) is not None
    # 合法
    sm._on_errwin_input(42, "2, 5, 10, 30, 0")
    assert m["config"].get()["errorWindows"] == [2, 5, 10, 30, 0]
    print("  [PASS] errwin edit")


def test_scoring_fields(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    sm._show_scoring(42, 100, "cb")

    # emaAlpha float
    sm._edit_scoring(42, 100, "cb", "emaAlpha")
    assert m["states"].get_state(42)["action"] == "sys_scoring:emaAlpha"
    sm._on_scoring_input(42, "sys_scoring:emaAlpha", "0.33")
    assert m["config"].get()["scoring"]["emaAlpha"] == 0.33

    # recentWindow int 范围校验
    sm._edit_scoring(42, 100, "cb", "recentWindow")
    sm._on_scoring_input(42, "sys_scoring:recentWindow", "-1")
    assert m["states"].get_state(42) is not None  # 未通过，状态仍在
    sm._on_scoring_input(42, "sys_scoring:recentWindow", "42")
    assert m["config"].get()["scoring"]["recentWindow"] == 42

    # errorPenaltyFactor int
    sm._edit_scoring(42, 100, "cb", "errorPenaltyFactor")
    sm._on_scoring_input(42, "sys_scoring:errorPenaltyFactor", "10")
    assert m["config"].get()["scoring"]["errorPenaltyFactor"] == 10

    # explorationRate float 范围
    sm._edit_scoring(42, 100, "cb", "explorationRate")
    sm._on_scoring_input(42, "sys_scoring:explorationRate", "0.1")
    assert m["config"].get()["scoring"]["explorationRate"] == 0.1
    print("  [PASS] scoring 4 fields + range checks")


def test_affinity_fields(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    sm._show_affinity(42, 100, "cb")

    sm._edit_affinity(42, 100, "cb", "ttlMinutes")
    sm._on_affinity_input(42, "sys_affinity:ttlMinutes", "0")
    assert m["states"].get_state(42) is not None
    sm._on_affinity_input(42, "sys_affinity:ttlMinutes", "45")
    assert m["config"].get()["affinity"]["ttlMinutes"] == 45

    print("  [PASS] affinity fields")


def test_cch_mode_switch(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_cch(42, 100, "cb")

    sm._on_cch_set(42, 100, "cb", "dynamic")
    assert m["config"].get()["cchMode"] == "dynamic"

    sm._on_cch_set(42, 100, "cb", "disabled")
    assert m["config"].get()["cchMode"] == "disabled"

    # static 是历史测试模式，新 UI 不再允许切换。
    sm._on_cch_set(42, 100, "cb", "static")
    assert m["config"].get()["cchMode"] == "disabled"

    # 无效模式
    sm._on_cch_set(42, 100, "cb", "bogus")
    assert m["config"].get()["cchMode"] == "disabled"
    print("  [PASS] CCH mode switch")


def test_chsel_switch(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._on_chsel_set(42, 100, "cb", "order")
    assert m["config"].get()["channelSelection"] == "order"

    sm._on_chsel_set(42, 100, "cb", "smart")
    assert m["config"].get()["channelSelection"] == "smart"

    sm._on_chsel_set(42, 100, "cb", "priority")
    assert m["config"].get()["channelSelection"] == "priority"
    assert m["config"].get()["loadBalancing"]["initialized"] is True

    sm._on_chsel_set(42, 100, "cb", "bogus")
    assert m["config"].get()["channelSelection"] == "priority"
    print("  [PASS] channelSelection switch")


def test_blacklist_default_add_and_remove(m):
    _reset(m)
    # 清空黑名单
    m["config"].update(lambda c: c.__setitem__("contentBlacklist", {"default": [], "byChannel": {}}))
    rec = _install(m)
    sm = m["system_menu"]

    sm._bl_add_default(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_bl_add_default"
    sm._on_bl_add_default_input(42, "policy_violation")
    assert "policy_violation" in m["config"].get()["contentBlacklist"]["default"]
    assert m["states"].get_state(42) is None

    # 添加第二个
    sm._bl_add_default(42, 100, "cb")
    sm._on_bl_add_default_input(42, "content_filter")
    defaults = m["config"].get()["contentBlacklist"]["default"]
    assert defaults == ["policy_violation", "content_filter"]

    # 空输入被拒
    sm._bl_add_default(42, 100, "cb")
    sm._on_bl_add_default_input(42, "   ")
    assert m["states"].get_state(42) is not None

    # 删除：取列表 → 删其中一个
    rec.clear()
    sm._bl_del_default(42, 100, "cb")
    edit = rec.last("editMessageText")
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"]
            for b in row if "callback_data" in b]
    exec_cbs = [b for b in btns if b.startswith("sys:bl_del_exec:")]
    assert len(exec_cbs) == 2
    # 执行删除第一个
    short = exec_cbs[0].split(":", 2)[2]
    sm._bl_del_exec(42, 100, "cb", short)
    # 列表缩减
    remaining = m["config"].get()["contentBlacklist"]["default"]
    assert len(remaining) == 1
    print("  [PASS] blacklist default add+remove")


def test_blacklist_by_channel(m):
    _reset(m)
    m["config"].update(lambda c: c.__setitem__("contentBlacklist", {"default": [], "byChannel": {}}))
    rec = _install(m)
    sm = m["system_menu"]

    sm._bl_add_ch(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_bl_add_ch"

    # 非法格式
    sm._on_bl_add_ch_input(42, "no-equal-sign")
    assert m["states"].get_state(42) is not None

    # 合法
    sm._on_bl_add_ch_input(42, "智谱Coding Plan Max=dangerous_term")
    by_ch = m["config"].get()["contentBlacklist"]["byChannel"]
    assert "智谱Coding Plan Max" in by_ch
    assert by_ch["智谱Coding Plan Max"] == ["dangerous_term"]
    print("  [PASS] blacklist byChannel add")



def test_ws_mode_menu_and_toggle(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    m["config"].update(lambda c: c.setdefault("openai", {}).__setitem__("responsesUpstreamWsForOAuth", False))
    sm._show_ws_mode(42, 100, "cb")
    edit = rec.last("editMessageText")
    assert "下游 WebSocket <code>/v1/responses</code>：已支持，默认可用" in edit["text"]
    assert "HTTP Responses 转上游 WS：<code>关闭</code>" in edit["text"]
    assert "OpenAI OAuth" in edit["text"]

    sm._on_ws_mode_toggle(42, 100, "cb")
    assert m["config"].get()["openai"]["responsesUpstreamWsForOAuth"] is True
    edit = rec.last("editMessageText")
    assert "HTTP Responses 转上游 WS：<code>开启</code>" in edit["text"]

    sm._on_ws_mode_toggle(42, 100, "cb")
    assert m["config"].get()["openai"]["responsesUpstreamWsForOAuth"] is False
    print("  [PASS] WS mode menu + toggle")

def test_log_retention_two_confirm_flow(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    ld = m["log_db"]
    sm._retention_pending.clear()
    calls = {"plan": 0, "apply": []}
    plan = {
        "days": 7,
        "cutoff": 1_770_000_000.0,
        "reference_ts": 1_770_604_800.0,
        "base_policy": {"mode": "forever", "days": None},
        "items": [{
            "month": "2026-07", "action": "trim_and_vacuum", "bundle_bytes": 1024,
            "total_requests": 10, "expired_requests": 3,
        }],
        "errors": [],
        "scanned_months": 1,
        "scanned_bytes": 1024,
        "preflight": {"ok": True, "effective_available_bytes": 20 * 1024**3, "required_bytes": 2 * 1024**3},
        "signature": "test-signature",
    }
    old_plan = ld.plan_retention
    old_apply = ld.apply_retention_plan
    old_busy = ld.retention_cleanup_busy
    try:
        def fake_plan(days):
            calls["plan"] += 1
            assert days == 7
            return plan
        def fake_apply(got_plan, *, activate_policy=False, progress=None):
            calls["apply"].append((got_plan, activate_policy))
            if progress:
                progress({"phase": "item_start", "item": plan["items"][0], "index": 1, "total": 1})
            return {
                "ok": True, "days": 7, "full_months_deleted": 0,
                "deleted_requests": 3, "actual_free_bytes": 1024,
            }
        ld.plan_retention = fake_plan
        ld.apply_retention_plan = fake_apply
        ld.retention_cleanup_busy = lambda: False

        sm._show_retention(42, 100, "cb")
        assert "请求日志数据留存" in rec.last("editMessageText")["text"]
        sm._edit_retention_days(42, 100, "cb")
        assert m["states"].get_state(42)["action"] == "sys_retention_days"
        sm._on_retention_days_input(42, "7")
        # 输入阶段只有第一层确认，尚未扫描 / 保存 / 删除。
        assert calls["plan"] == 0
        first = rec.last("sendMessage")
        scan_cb = next(
            b["callback_data"] for row in first["reply_markup"]["inline_keyboard"]
            for b in row if b["callback_data"].startswith("sys:retention:scan:")
        )
        sm.handle_callback(42, 200, "cb", scan_cb)
        assert calls["plan"] == 1
        preview = rec.last("editMessageText")
        assert "数据清理预览" in preview["text"]
        commit_cb = next(
            b["callback_data"] for row in preview["reply_markup"]["inline_keyboard"]
            for b in row if b["callback_data"].startswith("sys:retention:commit:")
        )
        sm.handle_callback(42, 200, "cb", commit_cb)
        assert calls["apply"] == [(plan, True)]
        assert "数据留存策略已生效" in rec.last("editMessageText")["text"]
    finally:
        ld.plan_retention = old_plan
        ld.apply_retention_plan = old_apply
        ld.retention_cleanup_busy = old_busy
        sm._retention_pending.clear()
    print("  [PASS] log retention two-confirm flow")


def test_log_body_storage_toggle_from_retention_menu(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    original = m["config"].get().get("logStoreBodies", True) is not False
    try:
        m["config"].update(lambda cfg: cfg.__setitem__("logStoreBodies", True))
        sm._show_retention(42, 100, "cb")
        page = rec.last("editMessageText")
        assert "保存完整请求：<code>开启</code>" in page["text"]
        assert "切换只影响后续新请求" in page["text"]
        buttons = [
            button
            for row in page["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        toggle = next(
            button for button in buttons
            if button["callback_data"] == "sys:retention:toggle_bodies"
        )
        assert toggle["text"] == "☑ 保存完整请求"

        rec.clear()
        assert sm.handle_callback(
            42, 100, "cb", "sys:retention:toggle_bodies",
        ) is True
        assert m["config"].get()["logStoreBodies"] is False
        page = rec.last("editMessageText")
        assert "保存完整请求：<code>关闭</code>" in page["text"]
        toggle = next(
            button
            for row in page["reply_markup"]["inline_keyboard"]
            for button in row
            if button["callback_data"] == "sys:retention:toggle_bodies"
        )
        assert toggle["text"] == "☐ 保存完整请求"

        rec.clear()
        assert sm.handle_callback(
            42, 100, "cb", "sys:retention:toggle_bodies",
        ) is True
        assert m["config"].get()["logStoreBodies"] is True
        assert "保存完整请求：<code>开启</code>" in rec.last("editMessageText")["text"]
    finally:
        m["config"].update(
            lambda cfg: cfg.__setitem__("logStoreBodies", original)
        )
    print("  [PASS] log body storage toggle from retention menu")


def test_log_retention_days_mode_can_update_day_value(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    sm._retention_pending.clear()
    m["config"].update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "days", "days": 3}))
    try:
        sm._show_retention(42, 100, "cb")
        page = rec.last("editMessageText")
        assert "当前留存模式：<code>按天留存</code>" in page["text"]
        assert "保留天数：<code>3 天</code>" in page["text"]
        callbacks = [
            button["callback_data"]
            for row in page["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert "sys:retention:days" in callbacks
        assert "sys:retention:forever" in callbacks

        # 3 → 5 仍是按天留存模式，只更新天数，不生成删除预览。
        sm._edit_retention_days(42, 100, "cb")
        assert "当前为按天留存，保留 <code>3</code> 天" in rec.last("editMessageText")["text"]
        sm._on_retention_days_input(42, "5")
        assert m["config"].get()["logRetention"] == {"mode": "days", "days": 5}
        assert "按天留存天数已修改" in rec.last("sendMessage")["text"]
        assert not sm._retention_pending

        # 5 → 2 会缩短留存期，必须回到两次确认的删除预览流程。
        sm._edit_retention_days(42, 100, "cb")
        sm._on_retention_days_input(42, "2")
        warning = rec.last("sendMessage")
        assert "从 5 天缩短为 2 天" in warning["text"]
        assert any(
            button["callback_data"].startswith("sys:retention:scan:")
            for row in warning["reply_markup"]["inline_keyboard"]
            for button in row
        )
    finally:
        m["config"].update(lambda cfg: cfg.__setitem__("logRetention", {"mode": "forever", "days": None}))
        sm._retention_pending.clear()
        m["states"].clear_all()
    print("  [PASS] log retention mode/day-value separation")


def test_router_dispatch(m):
    _reset(m)
    rec = _install(m)
    m["ui"].configure("TOKEN", [42])

    m["bot"]._handle_callback({
        "id": "cb1", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:settings",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_callback({
        "id": "cb2", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "sys:show:timeouts",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/settings"})
    assert rec.last("sendMessage") is not None
    print("  [PASS] router + /settings")


def test_text_state_dispatch_to_system(m):
    _reset(m)
    rec = _install(m)
    m["ui"].configure("TOKEN", [42])
    m["states"].set_state(42, "sys_timeouts")

    m["bot"]._handle_message({"chat": {"id": 42}, "text": "15,40,45,700"})
    assert m["states"].get_state(42) is None
    assert m["config"].get()["timeouts"]["connect"] == 15
    print("  [PASS] bot text → system state handler")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()
    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_main_page,
        test_retry_settings_menu_and_controls,
        test_timeouts_edit,
        test_errwin_edit,
        test_scoring_fields,
        test_affinity_fields,
        test_chsel_switch,
        test_blacklist_default_add_and_remove,
        test_blacklist_by_channel,
        test_ws_mode_menu_and_toggle,
        test_log_retention_two_confirm_flow,
        test_log_body_storage_toggle_from_retention_menu,
        test_log_retention_days_mode_can_update_day_value,
        test_router_dispatch,
        test_text_state_dispatch_to_system,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m); passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig_cfg)
        m["config"].update(_restore)
        m["states"].clear_all()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
