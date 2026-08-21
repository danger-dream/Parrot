"""M7 OAuth 菜单测试。

在 mockMode=True 下覆盖：
  - 空列表 / 有账户列表展示
  - 账户详情渲染（含配额缓存）
  - 刷新 Token：access_token 替换 + 用量缓存写入
  - 刷新用量：缓存更新
  - 启用/禁用切换
  - 删除（二次确认 + state.db 级联清除）
  - 刷新全部用量
  - PKCE 登录流程（mock 返回）：账户入 config
  - 手动 JSON：必填校验 + 入 config

所有 TG API 调用被 ApiRecorder 拦截；不连 api.telegram.org。
OAuth 远端全走 oauth_manager.mockMode，不连 api.anthropic.com。
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
import time
from datetime import datetime, timedelta, timezone


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, cooldown, log_db, oauth_manager, state_db
    from src.telegram import bot, menu_cache, states, ui
    from src.telegram.menus import oauth_menu, main as main_menu
    return {
        "config": config, "cooldown": cooldown, "log_db": log_db, "oauth_manager": oauth_manager, "state_db": state_db,
        "bot": bot, "menu_cache": menu_cache, "states": states, "ui": ui,
        "oauth_menu": oauth_menu, "main_menu": main_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def by(self, method):
        return [d for m, d in self.calls if m == method]

    def last(self, method):
        deadline = time.time() + 5
        while True:
            calls = self.by(method)
            item = calls[-1] if calls else None
            text = str((item or {}).get("text") or "")
            loading = any(marker in text for marker in (
                "正在加载，完成后", "统计正在加载", "统计加载中", "历史统计加载中",
            ))
            if not loading or time.time() >= deadline:
                return item
            time.sleep(0.01)

    def clear(self):
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].quota_delete("")  # 无操作，仅确保已初始化
    # 清干净
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
        c["oauthUsageDisplayMode"] = "used"
        c["cchMode"] = "disabled"
        c.setdefault("quotaMonitor", {})["enabled"] = False
        c.setdefault("quotaMonitor", {})["intervalSeconds"] = 60
        c.setdefault("quotaMonitor", {})["disableThresholdPercent"] = 95
        c.setdefault("quotaMonitor", {})["resumeThresholdPercent"] = 95
    m["config"].update(_reset)
    # 清 quota 缓存 / 模型冷却
    for row in m["state_db"].quota_load_all():
        m["state_db"].quota_delete(row["account_key"])
    m["cooldown"].init()
    m["cooldown"].clear_all()
    conn = m["log_db"]._get_conn()
    conn.execute("DELETE FROM request_log")
    conn.execute("DELETE FROM request_detail")
    conn.execute("DELETE FROM retry_chain")
    conn.commit()
    m["states"].clear_all()


def _seed_common_snapshots(m) -> None:
    """测试显式模拟中央调度器及低频详情队列已生成快照。"""
    cache = m["menu_cache"]
    since = cache.month_start_ts()
    cache.PERIOD_STATS.store(
        ("period", int(since)), m["log_db"].stats_period_snapshot(since),
    )
    accounts = m["oauth_manager"].list_accounts()
    for account in accounts:
        account_key = m["oauth_manager"]._account_key(account)
        cache.DETAIL_STATS.store(
            ("oauth-model", account_key, int(since)),
            m["log_db"].channel_model_stats(
                f"oauth:{account_key}", since_ts=since,
            ),
        )
    for key, account_key, window_since in m["oauth_menu"]._oauth_window_specs(accounts):
        cache.WINDOW_STATS.store(
            key,
            m["log_db"].tokens_for_channel(
                f"oauth:{account_key}", since_ts=window_since,
            ),
        )


def _install_recorder(m):
    _seed_common_snapshots(m)
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec




def _account_key_for(m, email: str) -> str:
    for acc in m["oauth_manager"].list_accounts():
        if acc.get("email") == email:
            return m["oauth_manager"]._account_key(acc)
    raise AssertionError(f"account not found: {email}")


def _insert_oauth_success(m, email: str, request_id: str = "oauth-r1", *, model: str = "gpt-5.5") -> None:
    ak = _account_key_for(m, email)
    ld = m["log_db"]
    ld.insert_pending(request_id, "1.1.1.1", "k1", model, True,
                      msg_count=3, tool_count=0, request_headers={}, request_body={})
    ld.finish_success(
        request_id, f"oauth:{ak}", "oauth", model,
        input_tokens=100, output_tokens=20, cache_creation_tokens=10, cache_read_tokens=50,
        connect_ms=100, first_token_ms=300, total_ms=1500,
        retry_count=0, affinity_hit=1, response_body='{}', http_status=200,
    )


def _add_fake_account(m, email, **kw):
    acc = {
        "email": email,
        "access_token": "old-token-" + email,
        "refresh_token": "r-" + email,
        "expired": kw.get(
            "expired",
            (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
        "last_refresh": kw.get("last_refresh",
                               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "type": "claude",
        "enabled": kw.get("enabled", True),
        "disabled_reason": kw.get("disabled_reason"),
        "disabled_until": kw.get("disabled_until"),
        "models": [],
    }
    def _m(cfg):
        cfg.setdefault("oauthAccounts", []).append(acc)
    m["config"].update(_m)


def _add_openai_fake_account(m, email, **kw):
    acc = {
        "email": email,
        "provider": "openai",
        "access_token": "old-openai-token-" + email,
        "refresh_token": "r-openai-" + email,
        "expired": kw.get(
            "expired",
            (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
        "last_refresh": kw.get("last_refresh",
                               datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "chatgpt_account_id": kw.get("chatgpt_account_id", "acct-123"),
        "workspace_id": kw.get("workspace_id", "acct-123"),
        "organization_id": kw.get("organization_id", "org-x"),
        "plan_type": kw.get("plan_type", "plus"),
        "enabled": kw.get("enabled", True),
        "disabled_reason": kw.get("disabled_reason"),
        "disabled_until": kw.get("disabled_until"),
        "models": [],
    }
    def _m(cfg):
        cfg.setdefault("oauthAccounts", []).append(acc)
    m["config"].update(_m)


# ─── Tests ───────────────────────────────────────────────────────

def test_list_empty_and_populated(m):
    _setup(m)
    rec = _install_recorder(m)
    m["oauth_menu"].show(chat_id=42, message_id=100)
    last = rec.last("editMessageText")
    assert last, "expect editMessageText called"
    assert "共 0 个账户" in last["text"]
    assert "暂无账户" in last["text"]
    # 新增账户按钮
    kb = last["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert "oa:add" in flat
    assert "oa:invalid:list" in flat
    assert "oa:refresh_all:1" in flat
    assert "oa:settings" in flat
    assert "menu:main" in flat
    assert "oa:page:1" not in flat
    assert "oa:page:1:available" not in flat
    assert "oa:page:1:quota" not in flat
    assert "oa:page:1:invalid" not in flat
    texts = [b["text"] for row in kb for b in row if "text" in b]
    assert "➕ 新增账户" in texts
    assert "🧨 移除失效" in texts
    assert "🔄 刷新用量/重置卡" in texts
    assert "⚙️ 账户设置" in texts

    # 添加两个账户后再渲染
    _add_fake_account(m, "user1@x.com")
    _add_fake_account(m, "user2@x.com", disabled_reason="user", enabled=False)
    _insert_oauth_success(m, "user1@x.com")
    _seed_common_snapshots(m)
    rec.clear()
    m["oauth_menu"].show(42, 100)
    last = rec.last("editMessageText")
    assert "共 2 个账户" in last["text"]
    assert "user1@x.com" in last["text"]
    assert "user2@x.com" in last["text"]
    assert "用户禁用" in last["text"]
    assert "缓存 50 (31.2%)" in last["text"]
    assert "\n💵 " in last["text"]
    assert "缓存 50 (31.2%) · 💵" not in last["text"]
    assert "≈" not in last["text"]
    assert "⏳ Token" not in last["text"]
    flat = [b["callback_data"] for row in last["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:sort:1:all" not in flat
    # 每个账户一个按钮
    email_btns = [
        b for row in last["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b and b["callback_data"].startswith("oa:view:")
    ]
    assert len(email_btns) == 2
    print("  [PASS] oauth list empty + populated")


def test_oauth_sort_reorders_accounts(m):
    _setup(m)
    for i in range(1, 10):
        _add_fake_account(m, f"sort{i}@x.com")
    rec = _install_recorder(m)
    om = m["oauth_menu"]

    om.show(42, 100, page=3)
    page3 = rec.last("editMessageText")
    flat = [
        b["callback_data"]
        for row in page3["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:sort:3:all" in flat

    rec.clear()
    assert om.handle_callback(42, 100, "cb", "oa:sort:3:available") is True
    sort_page = rec.last("editMessageText")
    assert sort_page and "OAuth 账户排序" in sort_page["text"]
    assert "sort1@x.com" in sort_page["text"] and "sort9@x.com" in sort_page["text"]
    assert "返回时保留过滤" in sort_page["text"]

    rec.clear()
    assert om.handle_callback(42, 100, "cb", "oa:sort_sel:9") is True
    selected = rec.last("editMessageText")
    btn_texts = [b["text"] for row in selected["reply_markup"]["inline_keyboard"] for b in row]
    assert "9 ✅" in btn_texts

    rec.clear()
    assert om.handle_callback(42, 100, "cb", "oa:sort_mv:top") is True
    moved = rec.last("editMessageText")
    first_line = next(line for line in moved["text"].splitlines() if "sort" in line)
    assert "sort9@x.com" in first_line, first_line

    rec.clear()
    assert om.handle_callback(42, 100, "cb", "oa:sort_save") is True
    accounts = m["config"].get()["oauthAccounts"]
    assert accounts[0]["email"] == "sort9@x.com", [a["email"] for a in accounts[:3]]
    assert [a["email"] for a in accounts[1:4]] == ["sort1@x.com", "sort2@x.com", "sort3@x.com"]
    saved = rec.last("editMessageText")
    assert saved and "已保存 OAuth 账户排序" in saved["text"]
    saved_flat = [
        b["callback_data"]
        for row in saved["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:sort:3:available" in saved_flat and "oa:page:3:available" in saved_flat
    print("  [PASS] oauth sort reorders accounts")


def test_view_detail_with_quota_cache(m):
    _setup(m)
    _add_fake_account(m, "alice@x.com")
    # 写入 quota 缓存（fetched_at 用当前时间，避免被 ensure_quota_fresh 节流判定为 stale
    # 从而触发 mock fetch 覆盖掉这里的断言值）
    m["state_db"].quota_save("alice@x.com", {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 12.0, "five_hour_reset": "2026-04-18T14:00:00Z",
        "seven_day_util": 45.0, "seven_day_reset": "2026-04-24T00:00:00Z",
        "sonnet_util": None, "opus_util": None,
        "raw_data": "{}",
    })
    _insert_oauth_success(m, "alice@x.com")

    rec = _install_recorder(m)
    short = m["ui"].register_code("alice@x.com")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last and "alice@x.com" in last["text"]
    assert "5h: 已用 12%" in last["text"]
    assert "7d: 已用 45%" in last["text"]
    assert "缓存 50 (31.2%)" in last["text"]
    assert "均 " in last["text"] and " · $0.000" in last["text"]
    assert "累计金额：$0.00" in last["text"]
    assert "缓存 50 (31.2%) · 💵" not in last["text"]
    assert "≈" not in last["text"]
    assert "↑ 160 · ↓ 20" in last["text"]
    # 详情按钮
    kb = last["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert any(x.startswith("oa:refresh_token:") for x in flat)
    assert any(x.startswith("oa:refresh_usage:") for x in flat)
    assert any(x.startswith("oa:toggle:") for x in flat)
    assert any(x.startswith("oa:delete_ask:") for x in flat)
    print("  [PASS] oauth detail (含 quota 缓存渲染)")


def test_openai_window_cost_is_inline_three_decimals_and_detail_uses_amount_label(m):
    _setup(m)
    email = "window-cost@openai.test"
    _add_openai_fake_account(m, email)
    account_key = _account_key_for(m, email)
    now = datetime.now(timezone.utc)
    m["state_db"].quota_save(account_key, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 20.0,
        "five_hour_reset": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seven_day_util": 40.0,
        "seven_day_reset": (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "raw_data": "{}",
    })
    stats = {
        "total": 1, "success_count": 1, "error_count": 0,
        "input": 250, "output": 300,
        "cache_creation": 50, "cache_read": 900,
        "avg_tps": 42.5, "max_tps": 50.0, "min_tps": 30.0,
        "cost_ticks": 12_345_000_000, "costed_success": 1,
    }
    for key, _account_key, _since in m["oauth_menu"]._oauth_window_specs(
        m["oauth_manager"].list_accounts()
    ):
        m["menu_cache"].WINDOW_STATS.store(key, dict(stats))

    month_snapshot = {"by_channel": {f"oauth:{account_key}": dict(stats)}}
    account = m["oauth_manager"].get_account(account_key)
    list_text = m["oauth_menu"]._format_account_block(
        account, month_snapshot=month_snapshot,
    )
    inline = (
        "↑1.2K ↓300 · 缓存 900 (75.0%) · 均 42.5 t/s · $1.235"
    )
    assert list_text.count(inline) == 2
    assert "\n" + m["oauth_menu"]._USAGE_DETAIL_INDENT_LIST + "💵" not in list_text

    usage_text = m["oauth_menu"]._format_usage_block(
        account_key, month_snapshot=month_snapshot,
    )
    assert usage_text.count(inline) == 2
    assert "\n" + m["oauth_menu"]._USAGE_DETAIL_INDENT_BLOCK + "💵" not in usage_text

    month_text = m["oauth_menu"]._format_month_stats_block(
        account_key,
        month_snapshot=month_snapshot,
        by_model=[dict(stats, final_model="gpt-5.6-sol")],
    )
    assert month_text.count("累计金额：$1.23") == 2
    assert "💵" not in month_text


def test_missing_reset_shows_upstream_not_returned(m):
    _setup(m)
    _add_fake_account(m, "missing-reset@x.com")
    m["state_db"].quota_save("missing-reset@x.com", {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 0.0, "five_hour_reset": None,
        "seven_day_util": 45.0, "seven_day_reset": None,
        "sonnet_util": 0.0, "sonnet_reset": None,
        "opus_util": 0.0, "opus_reset": None,
        "raw_data": "{}",
    })

    rec = _install_recorder(m)
    m["oauth_menu"].show(42, 100)
    list_text = rec.last("editMessageText")["text"]
    assert "📊 5h: 已用 <b>0%</b> · 重置 <code>?</code>" in list_text
    assert "📊 7d: 已用 <b>45%</b> · 重置 <code>?</code>" in list_text

    rec.clear()
    short = m["ui"].register_code("missing-reset@x.com")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    detail_text = rec.last("editMessageText")["text"]
    assert "⏱ 5h: 已用 0% (重置: 上游未返回)" in detail_text
    assert "📅 7d: 已用 45% (重置: 上游未返回)" in detail_text
    assert "🤖 Sonnet 7d: 已用 0% (重置: 上游未返回)" in detail_text
    assert "🧠 Opus 7d: 已用 0% (重置: 上游未返回)" in detail_text
    print("  [PASS] missing reset renders current list fallback + 上游未返回 in detail")


def test_settings_usage_display_mode_toggle(m):
    _setup(m)
    _add_fake_account(m, "mode@x.com")
    m["state_db"].quota_save("mode@x.com", {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 20.0, "five_hour_reset": None,
        "seven_day_util": 60.0, "seven_day_reset": None,
        "raw_data": "{}",
    })
    rec = _install_recorder(m)

    m["oauth_menu"].on_settings(42, 100, "cb-settings")
    settings = rec.last("editMessageText")
    assert settings and "OAuth 账户设置" in settings["text"]
    assert "OAuth 模型目录" in settings["text"]
    assert "按账号自动同步" in settings["text"]
    assert "🎨 <b>媒体能力</b>" in settings["text"]
    assert "GPT / Codex 图片:" in settings["text"]
    assert "Grok Imagine: 图片 <b>2</b> · 视频 <b>2</b>" in settings["text"]
    assert "tg-emoji" in settings["text"]
    assert "📊 <b>用量显示模式</b>" in settings["text"]
    assert "当前模式: 已使用量" in settings["text"]
    assert "CCH 模式（Claude Code 伪装）" in settings["text"]
    assert "当前模式: 🚫 已关闭" in settings["text"]
    assert "OAuth 配额监控" in settings["text"]
    assert "状态: 🚫 已停用" in settings["text"]
    keyboard = settings["reply_markup"]["inline_keyboard"]
    texts = [b["text"] for row in keyboard for b in row]
    assert [b["text"] for b in keyboard[0]] == ["🧩 模型目录", "📈 配额监控"]
    assert [b["text"] for b in keyboard[1]] == ["GPT 图片", "Grok 图片"]
    assert "GPT 图片" in texts
    assert "Grok 图片" in texts
    assert "📈 配额监控" in texts
    assert "🎭 CCH模式：开启" in texts
    assert "📊 显示: 剩余用量" in texts

    rec.clear()
    assert m["oauth_menu"].handle_callback(42, 100, "cb-toggle", "oa:usage_mode:toggle") is True
    assert m["config"].get()["oauthUsageDisplayMode"] == "remaining"
    toggled = rec.last("editMessageText")
    assert toggled and "当前模式: 剩余用量" in toggled["text"]
    texts = [b["text"] for row in toggled["reply_markup"]["inline_keyboard"] for b in row]
    assert "📊 显示: 已使用量" in texts

    rec.clear()
    m["oauth_menu"].show(42, 100)
    list_text = rec.last("editMessageText")["text"]
    assert "📊 5h: 剩余 <b>80%</b>" in list_text
    assert "📊 7d: 剩余 <b>40%</b>" in list_text

    rec.clear()
    short = m["ui"].register_code("mode@x.com")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    detail_text = rec.last("editMessageText")["text"]
    assert "⏱ 5h: 剩余 80%" in detail_text
    assert "📅 7d: 剩余 40%" in detail_text
    print("  [PASS] OAuth settings toggles usage display mode and persists config")


def test_settings_cch_and_quota_monitor_controls(m):
    _setup(m)
    rec = _install_recorder(m)
    om = m["oauth_menu"]

    assert om.handle_callback(42, 100, "cb-cch", "oa:cch_toggle") is True
    assert m["config"].get()["cchMode"] == "dynamic"
    text = rec.last("editMessageText")["text"]
    assert "当前模式: ✅ 已启用" in text
    texts = [b["text"] for row in rec.last("editMessageText")["reply_markup"]["inline_keyboard"] for b in row]
    assert "🎭 CCH模式：关闭" in texts

    rec.clear()
    assert om.handle_callback(42, 100, "cb-quota", "oa:quota") is True
    quota = rec.last("editMessageText")
    assert quota and "OAuth 配额监控" in quota["text"]
    flat = [b["callback_data"] for row in quota["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:quota_toggle" in flat
    assert "oa:edit:quota_interval" in flat
    assert "oa:edit:quota_threshold" in flat
    assert "oa:settings" in flat and "menu:main" in flat

    rec.clear()
    assert om.handle_callback(42, 100, "cb-quota-toggle", "oa:quota_toggle") is True
    assert m["config"].get()["quotaMonitor"]["enabled"] is True
    assert "状态: <b>✅ 已启用</b>" in rec.last("editMessageText")["text"]

    rec.clear()
    assert om.handle_callback(42, 100, "cb-edit-int", "oa:edit:quota_interval") is True
    assert m["states"].get_state(42)["action"] == "oa_quota_interval"
    om.handle_text_state(42, "oa_quota_interval", "600")
    assert m["states"].get_state(42) is None
    assert m["config"].get()["quotaMonitor"]["intervalSeconds"] == 600
    result = rec.last("sendMessage")
    assert result and "600s" in result["text"]
    btns = [b["callback_data"] for row in result["reply_markup"]["inline_keyboard"] for b in row]
    assert btns == ["menu:main", "oa:settings"]

    rec.clear()
    assert om.handle_callback(42, 100, "cb-edit-th", "oa:edit:quota_threshold") is True
    assert m["states"].get_state(42)["action"] == "oa_quota_threshold"
    om.handle_text_state(42, "oa_quota_threshold", "98")
    assert m["states"].get_state(42) is None
    qm = m["config"].get()["quotaMonitor"]
    assert qm["disableThresholdPercent"] == 98.0
    assert qm["resumeThresholdPercent"] == 98.0
    print("  [PASS] OAuth settings CCH toggle + quota monitor submenu")


def test_refresh_token_updates_access_and_usage(m):
    _setup(m)
    _add_fake_account(m, "bob@x.com")
    rec = _install_recorder(m)
    short = m["ui"].register_code("bob@x.com")

    before = m["oauth_manager"].get_account("bob@x.com")["access_token"]
    m["oauth_menu"].on_refresh_token(42, 100, "cb", short)
    after = m["oauth_manager"].get_account("bob@x.com")["access_token"]
    assert before != after, "access_token 应被替换"
    assert after.startswith("mock-access-")
    # 刷新后 quota 缓存应被写入
    row = m["state_db"].quota_load("bob@x.com")
    assert row is not None
    # UI 反馈
    last = rec.last("editMessageText")
    assert last and "Token 已刷新" in last["text"]
    print("  [PASS] refresh_token 替换 access_token + 写入 usage 缓存")


def test_refresh_usage_only(m):
    _setup(m)
    _add_fake_account(m, "carol@x.com")
    rec = _install_recorder(m)
    short = m["ui"].register_code("carol@x.com")

    before = m["oauth_manager"].get_account("carol@x.com")["access_token"]
    m["oauth_menu"].on_refresh_usage(42, 100, "cb", short)
    after = m["oauth_manager"].get_account("carol@x.com")["access_token"]
    assert before == after  # token 不变
    row = m["state_db"].quota_load("carol@x.com")
    assert row is not None
    print("  [PASS] refresh_usage 只更新 quota 缓存")


def test_toggle_disable_then_enable(m):
    _setup(m)
    _add_fake_account(m, "dave@x.com")
    rec = _install_recorder(m)
    short = m["ui"].register_code("dave@x.com")

    m["oauth_menu"].on_toggle(42, 100, "cb", short)
    acc = m["oauth_manager"].get_account("dave@x.com")
    assert acc["enabled"] is False
    assert acc["disabled_reason"] == "user"

    m["oauth_menu"].on_toggle(42, 100, "cb", short)
    acc = m["oauth_manager"].get_account("dave@x.com")
    assert acc["enabled"] is True
    assert acc["disabled_reason"] is None
    print("  [PASS] toggle disable→enable")


def test_reset_quota_button_and_callback(m):
    _setup(m)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _add_fake_account(m, "quota@x.com", enabled=False, disabled_reason="quota", disabled_until=future)
    ak = _account_key_for(m, "quota@x.com")
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 99.0,
        "five_hour_reset": future,
        "seven_day_util": 20.0,
        "raw_data": "{}",
    }, email="quota@x.com")
    m["cooldown"].record_error(
        f"oauth:{ak}", "claude-reset-model", "quota",
        cooldown_until=m["state_db"].now_ms() + 600_000,
    )

    rec = _install_recorder(m)
    short = m["ui"].register_code(ak)
    m["oauth_menu"].on_view(42, 100, "cb", short)
    detail = rec.last("editMessageText")
    flat = [
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    reset_cb = next(x for x in flat if x.startswith("oa:reset_quota:"))

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-reset", reset_cb)
    assert handled
    acc_after = m["oauth_manager"].get_account(ak)
    assert acc_after["enabled"] is True
    assert acc_after.get("disabled_reason") is None
    assert m["state_db"].quota_load(ak) is None
    assert not m["cooldown"].is_blocked(f"oauth:{ak}", "claude-reset-model")
    answer = rec.last("answerCallbackQuery")
    assert answer and answer.get("text") == "已清本地配额禁用"
    updated = rec.last("editMessageText")
    assert updated and "已清理本地配额禁用" in updated["text"]
    print("  [PASS] local reset quota button clears quota-disabled/cache/cooldown")


def test_reset_quota_failure_callback_never_claims_success(m, monkeypatch):
    _setup(m)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    email = "quota-failure@x.com"
    _add_fake_account(m, email, enabled=False, disabled_reason="quota", disabled_until=future)
    ak = _account_key_for(m, email)
    m["state_db"].quota_save(
        ak,
        {
            "fetched_at": m["state_db"].now_ms(),
            "five_hour_util": 99.0,
            "five_hour_reset": future,
            "seven_day_util": 20.0,
            "raw_data": "{}",
        },
        email=email,
    )
    m["cooldown"].record_error(
        f"oauth:{ak}", "claude-reset-model", "quota",
        cooldown_until=m["state_db"].now_ms() + 600_000,
    )

    rec = _install_recorder(m)
    short = m["ui"].register_code(ak)
    monkeypatch.setattr(
        m["state_db"], "quota_delete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic quota_delete failure")
        ),
    )

    m["oauth_menu"].on_reset_quota(42, 100, "cb-reset-fail", short)

    account = m["oauth_manager"].get_account(ak)
    assert account["enabled"] is False and account["disabled_reason"] == "quota"
    answer = rec.last("answerCallbackQuery")
    assert answer and "失败" in answer.get("text", ""), answer
    assert "已清" not in answer.get("text", "")
    updated = rec.last("editMessageText")
    assert updated and "重置失败" in updated["text"], updated
    assert "已清理本地配额禁用" not in updated["text"]


def test_quota_window_since_uses_reset_minus_window_with_fallback(m):
    m["state_db"].init()
    now_ts = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    reset_ts = datetime(2026, 6, 25, 13, 30, 0, tzinfo=timezone.utc).timestamp()

    since = m["oauth_menu"]._quota_window_since_ts("2026-06-25T13:30:00Z", 5 * 3600, now_ts=now_ts)
    assert since == reset_ts - 5 * 3600

    fallback = now_ts - 5 * 3600
    assert m["oauth_menu"]._quota_window_since_ts(None, 5 * 3600, now_ts=now_ts) == fallback
    assert m["oauth_menu"]._quota_window_since_ts("", 5 * 3600, now_ts=now_ts) == fallback
    assert m["oauth_menu"]._quota_window_since_ts("bad-reset", 5 * 3600, now_ts=now_ts) == fallback
    assert m["oauth_menu"]._quota_window_since_ts("2026-06-25T11:00:00Z", 5 * 3600, now_ts=now_ts) == fallback
    assert m["oauth_menu"]._quota_window_since_ts("2026-06-26T00:00:00Z", 5 * 3600, now_ts=now_ts) == fallback
    print("  [PASS] quota window detail uses reset-window and falls back to now-window")


def test_openai_reset_credit_count_display_in_list_and_detail(m):
    _setup(m)
    _add_openai_fake_account(m, "show-reset@x.com", plan_type="pro")
    ak = _account_key_for(m, "show-reset@x.com")
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 12.0,
        "five_hour_reset": "2026-06-25T13:30:00Z",
        "seven_day_util": 44.0,
        "seven_day_reset": "2026-06-28T10:00:00Z",
        "raw_data": json.dumps({"openai": {
            "rate_limit_reset_credits": {"available_count": 2},
            "rate_limit_reset_credit_details": {
                "available_count": 2,
                "data": [{
                    "id": "card-1",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "expires_at": "2026-07-17T00:00:00Z",
                }],
            },
        }}),
    }, email="show-reset@x.com")

    rec = _install_recorder(m)
    m["oauth_menu"].show(42, 100)
    listing = rec.last("editMessageText")
    assert listing and "🏷 套餐: <code>pro</code> · ♻️ 官方重置次数: <code>2 次</code>" in listing["text"]

    rec.clear()
    short = m["ui"].register_code(ak)
    m["oauth_menu"].on_view(42, 100, "cb", short)
    detail = rec.last("editMessageText")
    assert detail and "♻️ 官方重置次数: <code>2 次</code>" in detail["text"]
    assert "♻️ 官方重置卡" in detail["text"]
    assert "Codex 额度重置" in detail["text"]
    assert "发放:" in detail["text"] and "过期:" in detail["text"]
    assert "Codex 原始窗口" not in detail["text"]
    detail_rows = detail["reply_markup"]["inline_keyboard"]
    action_row = next(row for row in detail_rows if any(b.get("callback_data", "").startswith("oa:reset_quota_ask:") for b in row))
    assert [b["text"] for b in action_row] == ["⚡ 并发上限", "♻️ 重置次数"]
    assert action_row[0]["callback_data"].startswith("oa:emax:")
    assert action_row[1]["callback_data"].startswith("oa:reset_quota_ask:")

    # 不是 OpenAI OAuth 账号时，即使构造 reset-count callback，也只清 loading、不弹提示、不改页面。
    _setup(m)
    _add_fake_account(m, "claude-no-reset@x.com")
    rec = _install_recorder(m)
    claude_short = m["ui"].register_code("claude-no-reset@x.com")
    assert m["oauth_menu"].handle_callback(42, 100, "cb-not-openai", f"oa:reset_quota_ask:{claude_short}:1")
    assert rec.last("editMessageText") is None
    cb_answer = rec.last("answerCallbackQuery")
    assert cb_answer is not None and "text" not in cb_answer

    # OpenAI 但当前没有可用 reset 次数时，也静默不弹提示、不改页面。
    _setup(m)
    _add_openai_fake_account(m, "zero-click@x.com", plan_type="plus")
    ak_zero_click = _account_key_for(m, "zero-click@x.com")
    rec = _install_recorder(m)
    zero_short_click = m["ui"].register_code(ak_zero_click)
    original_fetch_and_save = m["oauth_menu"]._fetch_and_save_usage_sync
    def _zero_usage(_ak, *, email=None):
        return {
            "five_hour": {"utilization": 1.0, "resets_at": None},
            "seven_day": {"utilization": 2.0, "resets_at": None},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "extra_usage": {"is_enabled": False},
            "openai": {"rate_limit_reset_credits": {"available_count": 0}},
        }
    m["oauth_menu"]._fetch_and_save_usage_sync = _zero_usage
    try:
        assert m["oauth_menu"].handle_callback(42, 100, "cb-zero", f"oa:reset_quota_ask:{zero_short_click}:1")
    finally:
        m["oauth_menu"]._fetch_and_save_usage_sync = original_fetch_and_save
    assert rec.last("editMessageText") is None
    cb_answer = rec.last("answerCallbackQuery")
    assert cb_answer is not None and "text" not in cb_answer


    _setup(m)
    _add_openai_fake_account(m, "zero-reset@x.com", plan_type="plus", enabled=False, disabled_reason="user")
    ak0 = _account_key_for(m, "zero-reset@x.com")
    m["state_db"].quota_save(ak0, {
        "fetched_at": m["state_db"].now_ms(),
        "raw_data": json.dumps({"openai": {"rate_limit_reset_credits": {"available_count": 0}}}),
    }, email="zero-reset@x.com")
    rec = _install_recorder(m)
    m["oauth_menu"].show(42, 100)
    listing0 = rec.last("editMessageText")
    assert listing0 and "官方重置次数" not in listing0["text"]
    short0 = m["ui"].register_code(ak0)
    rec.clear()
    m["oauth_menu"].on_view(42, 100, "cb", short0)
    detail0 = rec.last("editMessageText")
    assert detail0 and "♻️ 官方重置次数: <code>0 次</code>" in detail0["text"]
    assert "♻️ 官方重置卡" not in detail0["text"]
    print("  [PASS] openai reset credits shown in list/detail; list hides 0")


def test_quota_disabled_openai_missing_cache_list_does_not_auto_refresh(m):
    _setup(m)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _add_openai_fake_account(
        m, "missing-cache@x.com", enabled=False,
        disabled_reason="quota", disabled_until=future,
    )
    ak = _account_key_for(m, "missing-cache@x.com")
    assert m["state_db"].quota_load(ak) is None

    rec = _install_recorder(m)
    m["oauth_menu"].show(42, 100)

    rendered = rec.last("editMessageText")
    assert rendered is not None
    assert "missing-cache@x.com" in rendered["text"]
    assert "尚未获取" in rendered["text"]
    assert m["state_db"].quota_load(ak) is None
    # 进入常用列表只读快照；远端用量必须由显式刷新按钮触发，页面也不二次改写。
    time.sleep(0.05)
    assert len(rec.by("editMessageText")) == 1
    print("  [PASS] OAuth list does not auto-refresh missing remote usage")


def test_openai_reset_credit_cards_block_uses_post_consume_count_override(m):
    _setup(m)
    block = m["oauth_menu"]._format_reset_credit_cards_block(
        {
            "available_count": 2,
            "data": [
                {
                    "id": "old-card-1",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "expires_at": "2026-07-17T00:00:00Z",
                },
                {
                    "id": "old-card-2",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-18T00:00:00Z",
                    "expires_at": "2026-07-18T00:00:00Z",
                },
            ],
        },
        cached_count=1,
        available_count_override=1,
    )
    assert "当前可用 <code>1 次</code>" in block
    assert "仍在同步" in block
    assert "old-card" not in block
    assert "发放:" not in block

    hidden = m["oauth_menu"]._format_reset_credit_cards_block(
        {"available_count": 1, "data": [{"status": "available"}]},
        cached_count=0,
        available_count_override=0,
    )
    assert hidden == ""
    print("  [PASS] reset-card post-consume count override avoids stale card list")


def test_openai_official_reset_credit_ask_and_confirm(m):
    _setup(m)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _add_openai_fake_account(m, "quota-openai@x.com", enabled=False, disabled_reason="quota", disabled_until=future)
    ak = _account_key_for(m, "quota-openai@x.com")
    m["state_db"].quota_save(ak, {
        "fetched_at": m["state_db"].now_ms(),
        "five_hour_util": 99.0,
        "five_hour_reset": future,
        "seven_day_util": 20.0,
        "raw_data": json.dumps({"openai": {
            "rate_limit_reset_credits": {"available_count": 2},
            "rate_limit_reset_credit_details": {
                "available_count": 2,
                "data": [{
                    "id": "card-1",
                    "reset_type": "codex_rate_limits",
                    "status": "available",
                    "granted_at": "2026-06-17T00:00:00Z",
                    "expires_at": "2026-07-17T00:00:00Z",
                }],
            },
        }}),
    }, email="quota-openai@x.com")
    m["cooldown"].record_error(
        f"oauth:{ak}", "gpt-5-codex", "quota",
        cooldown_until=m["state_db"].now_ms() + 600_000,
    )

    rec = _install_recorder(m)
    short = m["ui"].register_code(ak)
    m["oauth_menu"].on_view(42, 100, "cb", short)
    detail = rec.last("editMessageText")
    flat = [
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    action_row = next(row for row in detail["reply_markup"]["inline_keyboard"] if any(b.get("callback_data", "").startswith("oa:reset_quota_ask:") for b in row))
    assert [b["text"] for b in action_row] == ["⚡ 并发上限", "♻️ 重置次数"]
    ask_cb = next(x for x in flat if x.startswith("oa:reset_quota_ask:"))

    # 旧按钮/直达回调不能绕过二次确认直接消耗官方 reset credit。
    rec.clear()
    assert m["oauth_menu"].handle_callback(42, 100, "cb-direct", f"oa:reset_quota:{short}:1")
    blocked = rec.last("editMessageText")
    acc_still_disabled = m["oauth_manager"].get_account(ak)
    assert blocked and "未执行重置" in blocked["text"]
    assert acc_still_disabled["enabled"] is False
    assert acc_still_disabled.get("disabled_reason") == "quota"

    rec.clear()
    assert m["oauth_menu"].handle_callback(42, 100, "cb-ask", ask_cb)
    ask_msg = rec.last("editMessageText")
    assert ask_msg and "当前可用官方重置次数" in ask_msg["text"]
    assert "这一步 <b>不会消耗</b>" in ask_msg["text"]
    confirm_page_cb = next(
        b["callback_data"]
        for row in ask_msg["reply_markup"]["inline_keyboard"]
        for b in row if b.get("callback_data", "").startswith("oa:reset_quota_confirm:")
    )
    confirm_payload = confirm_page_cb.split(":", 2)[2]
    confirm_short = confirm_payload.split(":", 1)[0]
    resolved_confirm = m["ui"].resolve_code(confirm_short)
    assert resolved_confirm and resolved_confirm.startswith(ak + "|") and resolved_confirm.endswith("|confirm")

    rec.clear()
    assert m["oauth_menu"].handle_callback(42, 100, "cb-confirm-page", confirm_page_cb)
    final_msg = rec.last("editMessageText")
    assert final_msg and "最终确认：消耗 1 次 OpenAI 官方重置" in final_msg["text"]
    assert "当前可用官方重置次数: <code>2 次</code>" in final_msg["text"]
    final_cb = next(
        b["callback_data"]
        for row in final_msg["reply_markup"]["inline_keyboard"]
        for b in row if b.get("callback_data", "").startswith("oa:reset_quota:")
    )
    reset_payload = final_cb.split(":", 2)[2]
    reset_short = reset_payload.split(":", 1)[0]
    resolved_reset = m["ui"].resolve_code(reset_short)
    assert resolved_reset and resolved_reset.startswith(ak + "|") and resolved_reset.endswith("|execute")

    rec.clear()
    assert m["oauth_menu"].handle_callback(42, 100, "cb-confirm", final_cb)
    updated = rec.last("editMessageText")
    acc_after = m["oauth_manager"].get_account(ak)
    row = m["state_db"].quota_load(ak)
    assert updated and "OpenAI 官方额度重置已执行" in updated["text"]
    assert acc_after["enabled"] is True and acc_after.get("disabled_reason") is None
    assert row is not None and row.get("five_hour_util") == 1.0
    assert not m["cooldown"].is_blocked(f"oauth:{ak}", "gpt-5-codex")
    print("  [PASS] openai official reset credit flow asks, consumes, clears local state")


def test_delete_flow(m):
    _setup(m)
    _add_fake_account(m, "eve@x.com")
    rec = _install_recorder(m)
    short = m["ui"].register_code("eve@x.com")

    # 请求确认
    m["oauth_menu"].on_delete_ask(42, 100, "cb", short)
    assert any("确认删除" in d.get("text", "") for _, d in rec.calls)

    # 执行删除
    rec.clear()
    m["oauth_menu"].on_delete_exec(42, 100, "cb", short)
    assert m["oauth_manager"].get_account("eve@x.com") is None
    # 确保 UI 通知
    assert any("已删除" in d.get("text", "") for _, d in rec.calls)
    print("  [PASS] delete flow (ask → exec + config 清理)")


def test_refresh_all_usage(m):
    _setup(m)
    _add_fake_account(m, "u1@x.com")
    _add_fake_account(m, "u2@x.com")
    rec = _install_recorder(m)

    m["oauth_menu"].on_refresh_all(42, 100, "cb")
    # 两个都应有缓存
    assert m["state_db"].quota_load("u1@x.com") is not None
    assert m["state_db"].quota_load("u2@x.com") is not None
    # 旧版简洁 UI：追加式进度消息 + 兜底摘要；两账户都应出现在同一条消息里且都"刷新成功"
    sent = [d["text"] for _, d in rec.calls if "text" in d]
    final = sent[-1] if sent else ""
    assert "u1@x.com" in final and "u2@x.com" in final, final[:500]
    assert final.count("✅ 刷新成功") >= 2, final[:500]
    assert "用量刷新完成：" in final, final[:500]
    print("  [PASS] refresh_all 两个账户都写入了 quota 缓存")


def test_pkce_login_flow(m):
    _setup(m)
    rec = _install_recorder(m)

    # 启动登录
    m["oauth_menu"].on_login_start(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "oa_login_code"

    # 模拟用户粘贴 code#state
    # mock 模式下 exchange_code 返回 mock token；fetch_profile 返回 mock@example.com
    m["oauth_menu"].on_login_code_input(42, "code123#state456")
    assert m["states"].get_state(42) is None

    accounts = m["oauth_manager"].list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["email"] == "mock@example.com"
    assert accounts[0]["access_token"].startswith("mock-access-")
    assert accounts[0]["refresh_token"].startswith("mock-refresh-")
    # 成功消息
    assert any("OAuth 账户已添加" in d.get("text", "") for _, d in rec.calls)
    print("  [PASS] PKCE login → mock account added (email from profile)")


def test_pkce_login_expired_session(m):
    _setup(m)
    rec = _install_recorder(m)
    # 不设置状态，直接进入 code_input
    m["oauth_menu"].on_login_code_input(42, "code123#state")
    texts = [d.get("text", "") for _, d in rec.calls]
    assert any("登录会话已失效" in t for t in texts)
    assert len(m["oauth_manager"].list_accounts()) == 0
    print("  [PASS] PKCE login rejects expired session")


def test_set_json_valid(m):
    _setup(m)
    rec = _install_recorder(m)

    m["oauth_menu"].on_set_json_start(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "oa_set_json"

    payload = json.dumps({
        "email": "imported@x.com",
        "access_token": "at-x",
        "refresh_token": "rt-x",
        "expired": "2099-01-01T00:00:00Z",
    })
    m["oauth_menu"].on_set_json_input(42, payload)
    assert m["states"].get_state(42) is None
    accounts = m["oauth_manager"].list_accounts()
    assert any(a["email"] == "imported@x.com" for a in accounts)
    assert any("已添加" in d.get("text", "") for _, d in rec.calls)
    print("  [PASS] set_json 合法 JSON 入 config")


def test_set_json_missing_fields(m):
    _setup(m)
    rec = _install_recorder(m)

    m["oauth_menu"].on_set_json_start(42, 100, "cb")
    # 缺 refresh_token
    m["oauth_menu"].on_set_json_input(42, json.dumps({
        "email": "x@x.com", "access_token": "at",
    }))
    accounts = m["oauth_manager"].list_accounts()
    assert not any(a["email"] == "x@x.com" for a in accounts)
    assert any("缺少必填字段" in d.get("text", "") for _, d in rec.calls)
    print("  [PASS] set_json 缺字段拒绝")


def test_oauth_detail_preserves_list_page(m):
    """从非首页进入账户详情后，返回列表应保留原分页。"""
    _setup(m)
    for i in range(1, 10):
        _add_fake_account(m, f"user{i}@x.com")
    rec = _install_recorder(m)

    m["oauth_menu"].show(42, 100, page=3)
    page3 = rec.last("editMessageText")
    assert page3 and "第 3/3 页" in page3["text"]
    assert "user9@x.com" in page3["text"]
    page3_flat = [
        b["callback_data"]
        for row in page3["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    view_cbs = [x for x in page3_flat if x.startswith("oa:view:")]
    assert len(view_cbs) == 1
    assert view_cbs[0].endswith(":3")

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-view-p3", view_cbs[0])
    assert handled
    detail = rec.last("editMessageText")
    assert detail and "user9@x.com" in detail["text"]
    detail_flat = [
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:page:3" in detail_flat
    assert any(x.startswith("oa:refresh_usage:") and x.endswith(":3") for x in detail_flat)
    assert any(x.startswith("oa:toggle:") and x.endswith(":3") for x in detail_flat)

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-back-p3", "oa:page:3")
    assert handled
    back = rec.last("editMessageText")
    assert back and "第 3/3 页" in back["text"]
    assert "user9@x.com" in back["text"]

    # 旧消息里的 oa:view:<short> 仍然兼容，默认按第一页处理。
    legacy_short = m["ui"].register_code("user9@x.com")
    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-view-old", f"oa:view:{legacy_short}")
    assert handled
    legacy_detail = rec.last("editMessageText")
    legacy_flat = [
        b["callback_data"]
        for row in legacy_detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:page:1" in legacy_flat
    print("  [PASS] oauth detail preserves page + legacy oa:view compatible")


def test_oauth_filter_preserved_through_detail(m):
    _setup(m)
    _add_fake_account(m, "ok1@x.com")
    _add_fake_account(m, "ok2@x.com")
    _add_fake_account(m, "ok3@x.com")
    _add_fake_account(m, "quota@x.com", enabled=False, disabled_reason="quota")
    _add_fake_account(m, "bad@x.com", enabled=False, disabled_reason="auth_error")
    rec = _install_recorder(m)

    handled = m["oauth_menu"].handle_callback(42, 100, "cb-filter", "oa:page:1:invalid")
    assert handled
    page = rec.last("editMessageText")
    assert page and "当前过滤" in page["text"] and "失效" in page["text"]
    assert "bad@x.com" in page["text"]
    assert "ok1@x.com" not in page["text"]
    texts = [b["text"] for row in page["reply_markup"]["inline_keyboard"] for b in row if "text" in b]
    assert "失效√" in texts

    flat = [
        b["callback_data"]
        for row in page["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    view_cb = next(x for x in flat if x.startswith("oa:view:"))
    assert view_cb.endswith(":1:invalid")

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-view", view_cb)
    assert handled
    detail = rec.last("editMessageText")
    flat2 = [
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:page:1:invalid" in flat2

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-back", "oa:page:1:invalid")
    assert handled
    back = rec.last("editMessageText")
    assert back and "bad@x.com" in back["text"] and "ok1@x.com" not in back["text"]
    print("  [PASS] oauth filter preserved through detail")


def test_invalid_remove_select_and_delete(m):
    _setup(m)
    _add_fake_account(m, "ok@x.com")
    _add_fake_account(m, "bad1@x.com", enabled=False, disabled_reason="auth_error")
    _add_fake_account(m, "bad2@x.com", enabled=False, disabled_reason="auth_error")
    rec = _install_recorder(m)

    handled = m["oauth_menu"].handle_callback(42, 100, "cb-invalid", "oa:invalid:list")
    assert handled
    panel = rec.last("editMessageText")
    assert panel and "移除失效账户" in panel["text"]
    assert "bad1@x.com" in panel["text"] and "bad2@x.com" in panel["text"]
    flat = [
        b["callback_data"]
        for row in panel["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "oa:invalid:remove_all" in flat
    assert "oa:invalid:remove_selected" in flat
    toggle = next(x for x in flat if x.startswith("oa:invalid:toggle:"))

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-toggle", toggle)
    assert handled
    panel2 = rec.last("editMessageText")
    texts = [b["text"] for row in panel2["reply_markup"]["inline_keyboard"] for b in row if "text" in b]
    assert any(t.startswith("✅ ") for t in texts)

    rec.clear()
    handled = m["oauth_menu"].handle_callback(42, 100, "cb-remove", "oa:invalid:remove_selected")
    assert handled
    result = rec.last("editMessageText")
    assert result and "已移除 1 个" in result["text"]
    emails = {a["email"] for a in m["oauth_manager"].list_accounts()}
    assert len({"bad1@x.com", "bad2@x.com"} & emails) == 1
    assert "ok@x.com" in emails
    print("  [PASS] invalid account remove select/delete")


def test_add_menu_cancel_buttons(m):
    _setup(m)
    rec = _install_recorder(m)

    m["oauth_menu"].on_add_menu(42, 100, "cb")
    add = rec.last("editMessageText")
    flat = [b["callback_data"] for row in add["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "menu:main" in flat

    rec.clear()
    m["oauth_menu"].on_login_start(42, 100, "cb")
    claude_login = rec.last("editMessageText")
    flat = [b["callback_data"] for row in claude_login["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:add" in flat

    rec.clear()
    m["oauth_menu"].on_set_json_start(42, 100, "cb")
    claude_json = rec.last("editMessageText")
    flat = [b["callback_data"] for row in claude_json["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:add" in flat

    rec.clear()
    m["oauth_menu"].on_login_openai_start(42, 100, "cb")
    openai_login = rec.last("editMessageText")
    flat = [b["callback_data"] for row in openai_login["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:add" in flat

    rec.clear()
    m["oauth_menu"].on_set_rt_openai_start(42, 100, "cb")
    openai_rt = rec.last("editMessageText")
    flat = [b["callback_data"] for row in openai_rt["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert "oa:add" in flat
    print("  [PASS] add menu/cancel buttons")


def test_router_dispatch(m):
    """通过 bot._handle_callback 间接验证路由在一起能跑通（admin 身份）。"""
    _setup(m)
    _add_fake_account(m, "routed@x.com")
    rec = _install_recorder(m)
    m["ui"].configure("TOKEN", [42])

    m["bot"]._handle_callback({
        "id": "cb-list",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "menu:oauth",
    })
    assert rec.last("editMessageText") is not None

    short = m["ui"].register_code("routed@x.com")
    rec.clear()
    m["bot"]._handle_callback({
        "id": "cb-view",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": f"oa:view:{short}",
    })
    last = rec.last("editMessageText")
    assert last and "routed@x.com" in last["text"]
    print("  [PASS] bot routing: menu:oauth / oa:view")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()

    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_list_empty_and_populated,
        test_oauth_sort_reorders_accounts,
        test_view_detail_with_quota_cache,
        test_settings_usage_display_mode_toggle,
        test_settings_cch_and_quota_monitor_controls,
        test_refresh_token_updates_access_and_usage,
        test_refresh_usage_only,
        test_toggle_disable_then_enable,
        test_reset_quota_button_and_callback,
        test_quota_window_since_uses_reset_minus_window_with_fallback,
        test_openai_reset_credit_count_display_in_list_and_detail,
        test_openai_reset_credit_cards_block_uses_post_consume_count_override,
        test_openai_official_reset_credit_ask_and_confirm,
        test_delete_flow,
        test_refresh_all_usage,
        test_pkce_login_flow,
        test_pkce_login_expired_session,
        test_set_json_valid,
        test_set_json_missing_fields,
        test_oauth_detail_preserves_list_page,
        test_oauth_filter_preserved_through_detail,
        test_invalid_remove_select_and_delete,
        test_add_menu_cancel_buttons,
        test_router_dispatch,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m)
                passed += 1
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
