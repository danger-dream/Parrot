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
from datetime import datetime, timedelta, timezone


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_manager, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import oauth_menu, main as main_menu
    return {
        "config": config, "oauth_manager": oauth_manager, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
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
        l = self.by(method)
        return l[-1] if l else None

    def clear(self):
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["state_db"].quota_delete("")  # 无操作，仅确保已初始化
    # 清干净
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
    m["config"].update(_reset)
    # 清 quota 缓存
    for row in m["state_db"].quota_load_all():
        m["state_db"].quota_delete(row["email"])
    m["states"].clear_all()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


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
    assert "oa:refresh_all" in flat
    assert "menu:main" in flat

    # 添加两个账户后再渲染
    _add_fake_account(m, "user1@x.com")
    _add_fake_account(m, "user2@x.com", disabled_reason="user", enabled=False)
    rec.clear()
    m["oauth_menu"].show(42, 100)
    last = rec.last("editMessageText")
    assert "共 2 个账户" in last["text"]
    assert "user1@x.com" in last["text"]
    assert "user2@x.com" in last["text"]
    assert "用户禁用" in last["text"]
    # 每个账户一个按钮
    email_btns = [
        b for row in last["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b and b["callback_data"].startswith("oa:view:")
    ]
    assert len(email_btns) == 2
    print("  [PASS] oauth list empty + populated")


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

    rec = _install_recorder(m)
    short = m["ui"].register_code("alice@x.com")
    m["oauth_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last and "alice@x.com" in last["text"]
    assert "5h: 12%" in last["text"]
    assert "7d: 45%" in last["text"]
    # 详情按钮
    kb = last["reply_markup"]["inline_keyboard"]
    flat = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert any(x.startswith("oa:refresh_token:") for x in flat)
    assert any(x.startswith("oa:refresh_usage:") for x in flat)
    assert any(x.startswith("oa:toggle:") for x in flat)
    assert any(x.startswith("oa:delete_ask:") for x in flat)
    print("  [PASS] oauth detail (含 quota 缓存渲染)")


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
    # UI 反馈含"刷新完成"
    sent = [d["text"] for _, d in rec.calls if "text" in d]
    assert any("刷新完成" in t for t in sent)
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
        test_view_detail_with_quota_cache,
        test_refresh_token_updates_access_and_usage,
        test_refresh_usage_only,
        test_toggle_disable_then_enable,
        test_delete_flow,
        test_refresh_all_usage,
        test_pkce_login_flow,
        test_pkce_login_expired_session,
        test_set_json_valid,
        test_set_json_missing_fields,
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
