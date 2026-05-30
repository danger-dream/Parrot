"""OpenAI OAuth (Codex / ChatGPT) Commit 1 测试。

覆盖：
  - src.oauth.openai 纯函数：PKCE / login URL / id_token 解码 / header 解析 / 归一化
  - oauth_manager：add_account(provider=openai) / 老数据 migrate_provider_field
  - state_db：oauth_quota_cache 新列幂等迁移
  - TG bot：OpenAI PKCE 登录流 + refresh_token 粘贴流（mockMode 下不连真实端点）

所有网络调用都由 openai_provider 的 mockMode 兜住（DISABLE_OAUTH_NETWORK_CALLS=1
或 oauth.mockMode=true）。
"""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_manager, state_db
    from src.oauth import openai as openai_provider
    from src.telegram import states, ui
    from src.telegram.menus import oauth_menu
    return {
        "config": config, "oauth_manager": oauth_manager, "state_db": state_db,
        "openai_provider": openai_provider,
        "states": states, "ui": ui, "oauth_menu": oauth_menu,
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
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
    m["config"].update(_reset)
    for row in m["state_db"].quota_load_all():
        m["state_db"].quota_delete(row.get("account_key") or row["email"])
    m["states"].clear_all()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


# ─── Pure function tests ─────────────────────────────────────────

def test_pkce_generate(m):
    p = m["openai_provider"]
    v, c = p.pkce_generate()
    # 对齐 codex-rs/login/src/pkce.rs：
    # verifier = base64url_no_pad(64 bytes) → 86 char，字母数字/-/_
    assert len(v) == 86, f"verifier not 86 chars: {v!r} (len={len(v)})"
    assert all(ch.isalnum() or ch in "-_" for ch in v), f"verifier not base64url: {v[:20]}..."
    assert "=" not in v, "verifier should not have padding"
    # challenge = base64url(sha256(verifier)) 无 padding
    import base64
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expected, f"challenge mismatch: {c} vs {expected}"
    assert "=" not in c, "challenge should not have padding"
    print("  [PASS] pkce_generate verifier=b64url86, challenge=b64url sha256 no-pad")


def test_build_login_url(m):
    p = m["openai_provider"]
    url = p.build_login_url("CHALLENGE", "STATE")
    assert url.startswith("https://auth.openai.com/oauth/authorize?"), url
    # 必带 OpenAI 专属参数
    assert "id_token_add_organizations=true" in url
    assert "codex_cli_simplified_flow=true" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in url
    # scope 带 offline_access + 新增的 api.connectors.*
    assert "offline_access" in url
    assert "api.connectors.read" in url
    assert "api.connectors.invoke" in url
    # 与 Codex 对齐：authorize URL 必带 originator
    assert "originator=codex_cli_rs" in url
    print("  [PASS] build_login_url contains all required OpenAI-specific params")


def test_decode_id_token_mock(m):
    p = m["openai_provider"]
    tok = p.exchange_code_sync("mock-code", "mock-verifier")
    assert tok.get("access_token") and tok.get("refresh_token") and tok.get("id_token")
    claims = p.decode_id_token(tok["id_token"])
    info = p.extract_user_info(claims)
    assert info["email"].startswith("mock-openai-") and info["email"].endswith("@local")
    assert info["chatgpt_account_id"].startswith("mock-acct-")
    assert info["organization_id"] == "org-mock"
    assert info["plan_type"] == "plus"
    print("  [PASS] decode_id_token + extract_user_info on mock token")


def test_decode_id_token_invalid(m):
    p = m["openai_provider"]
    try:
        p.decode_id_token("not.a.jwt.really")
        assert False, "expected IDTokenError"
    except p.IDTokenError:
        pass
    try:
        p.decode_id_token("only-two.parts")
        assert False, "expected IDTokenError"
    except p.IDTokenError:
        pass
    print("  [PASS] decode_id_token rejects malformed JWTs")


def test_parse_rate_limit_headers(m):
    p = m["openai_provider"]
    # 空 headers 返回 None
    assert p.parse_rate_limit_headers({}) is None
    # 有部分字段就返回 dict
    snap = p.parse_rate_limit_headers({
        "x-codex-primary-used-percent": "42.5",
        "x-codex-primary-reset-after-seconds": "3600",
        "x-codex-primary-window-minutes": "10080",  # 7d
        "x-codex-secondary-used-percent": "17",
        "x-codex-secondary-window-minutes": "300",   # 5h
    })
    assert snap is not None
    assert snap["primary_used_pct"] == 42.5
    assert snap["primary_window_min"] == 10080
    assert snap["secondary_used_pct"] == 17.0
    assert snap["secondary_window_min"] == 300
    # Normalize：primary window 大 → primary 是 7d
    norm = p.normalize_codex_snapshot(snap)
    assert norm["seven_day_util"] == 42.5
    assert norm["five_hour_util"] == 17.0
    print("  [PASS] parse_rate_limit_headers + normalize_codex_snapshot (primary=7d)")


def test_normalize_reverse_case(m):
    p = m["openai_provider"]
    # primary window 小 → primary 是 5h
    snap = {
        "primary_used_pct": 10.0, "primary_window_min": 300, "primary_reset_sec": 60,
        "secondary_used_pct": 50.0, "secondary_window_min": 10080, "secondary_reset_sec": 3600,
        "fetched_at": 0,
    }
    norm = p.normalize_codex_snapshot(snap)
    assert norm["five_hour_util"] == 10.0
    assert norm["seven_day_util"] == 50.0
    print("  [PASS] normalize_codex_snapshot reverse (primary=5h)")


def test_accounts_check_extracts_plan_and_subscription(m):
    """accounts/check: org 精确匹配并提取 plan_type / subscription_expires_at。"""
    p = m["openai_provider"]
    old_get = p.network.get_sync

    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "accounts": {
                    "org-free": {
                        "account": {"plan_type": "free", "is_default": True, "email": "free@example.com"},
                        "entitlement": {"expires_at": ""},
                    },
                    "org-pro": {
                        "account": {"plan_type": "pro", "is_default": False, "email": "pro@example.com"},
                        "entitlement": {"subscription_plan": "pro", "expires_at": "2026-06-01T00:00:00+00:00"},
                    },
                }
            }

    def fake_get(url, *, headers=None, timeout=None, **kwargs):
        assert url == p.ACCOUNTS_CHECK_URL
        assert headers["authorization"] == "Bearer at"
        assert headers["accept"] == "application/json"
        assert kwargs.get("proxy_purpose") == "oauth_openai"
        return Resp()

    old_disable_env = _ap_os.environ.pop("DISABLE_OAUTH_NETWORK_CALLS", None)
    try:
        p.network.get_sync = fake_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))
        info = p.fetch_accounts_check_sync("at", org_id="org-pro")
        assert info["plan_type"] == "pro", info
        assert info["subscription_expires_at"] == "2026-06-01T00:00:00+00:00", info
        assert info["email"] == "pro@example.com", info
    finally:
        if old_disable_env is not None:
            _ap_os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = old_disable_env
        p.network.get_sync = old_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))
    print("  [PASS] accounts/check: extracts plan + subscription_expires_at")

def test_accounts_check_selects_current_workspace_by_identity(m):
    """accounts/check: 只选择当前 token identity 对应 workspace 作为补全信息。"""
    p = m["openai_provider"]
    old_get = p.network.get_sync

    class Resp:
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "accounts": {
                    "acct-personal": {
                        "account": {"email": "same@example.com", "plan_type": "plus", "is_default": True, "name": "Personal"},
                        "entitlement": {"subscription_plan": "plus"},
                    },
                    "acct-team": {
                        "account": {"email": "same@example.com", "plan_type": "team", "name": "Team Space"},
                        "entitlement": {"subscription_plan": "team", "expires_at": "2026-06-01T00:00:00+00:00"},
                    },
                    "acct-other": {
                        "account": {"email": "other@example.com", "plan_type": "team", "name": "Other Team"},
                    },
                }
            }

    def fake_get(url, *, headers=None, timeout=None, **kwargs):
        assert url == p.ACCOUNTS_CHECK_URL
        assert kwargs.get("proxy_purpose") == "oauth_openai"
        return Resp()

    old_disable_env = _ap_os.environ.pop("DISABLE_OAUTH_NETWORK_CALLS", None)
    try:
        p.network.get_sync = fake_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))
        chosen = p.fetch_accounts_check_sync("at", workspace_id="acct-team", email="same@example.com")
        assert chosen["workspace_id"] == "acct-team"
        assert chosen["workspace_name"] == "Team Space"
        assert chosen["plan_type"] == "team"
    finally:
        if old_disable_env is not None:
            _ap_os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = old_disable_env
        p.network.get_sync = old_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))
    print("  [PASS] accounts/check: selects current workspace by identity")


# ─── state_db schema 迁移 ────────────────────────────────────────

def test_state_db_openai_cols_migration(m):
    st = m["state_db"]
    st.init()
    conn = st._get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")}
    for expected in ("codex_primary_used_pct", "codex_secondary_used_pct",
                     "codex_primary_window_min", "codex_primary_over_secondary_pct"):
        assert expected in cols, f"missing column: {expected}"
    # 幂等：再调一次迁移不抛
    st._migrate_oauth_quota_cache_openai_cols(conn)
    print("  [PASS] state_db oauth_quota_cache codex_* columns migrated & idempotent")


# ─── oauth_manager: provider field ────────────────────────────────

def test_add_account_openai_provider(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "foo@openai.test",
        "provider": "openai",
        "access_token": "at-xxx",
        "refresh_token": "rt-xxx",
        "id_token": "header.payload.sig",
        "chatgpt_account_id": "acct-123",
        "plan_type": "pro",
        "subscription_expires_at": "2026-06-01T00:00:00+00:00",
    })
    acc = om.get_account("openai:foo@openai.test:acct-123")
    assert acc["provider"] == "openai"
    assert acc["chatgpt_account_id"] == "acct-123"
    assert acc["plan_type"] == "pro"
    assert acc["subscription_expires_at"] == "2026-06-01T00:00:00+00:00"
    assert acc["id_token"] == "header.payload.sig"
    assert om.provider_of("openai:foo@openai.test:acct-123") == "openai"
    print("  [PASS] add_account(provider=openai) saves openai-specific fields")


def test_add_account_claude_default_provider(m):
    _setup(m)
    om = m["oauth_manager"]
    # 不传 provider → 默认 claude
    om.add_account({
        "email": "bar@claude.test",
        "access_token": "at-y",
        "refresh_token": "rt-y",
    })
    acc = om.get_account("bar@claude.test")
    assert acc["provider"] == "claude"
    assert om.provider_of("bar@claude.test") == "claude"
    # openai 专属字段不应出现
    assert "chatgpt_account_id" not in acc
    print("  [PASS] add_account default provider=claude")


def test_migrate_provider_field_idempotent(m):
    _setup(m)
    om = m["oauth_manager"]
    # 写一个没有 provider 的老账户（直接改 config 模拟）
    def _legacy(c):
        c["oauthAccounts"] = [{
            "email": "legacy@old",
            "access_token": "x", "refresh_token": "x",
            "type": "claude", "enabled": True,
        }]
    m["config"].update(_legacy)
    n = om.migrate_provider_field()
    assert n == 1, f"migrated {n}, expected 1"
    acc = om.get_account("legacy@old")
    assert acc["provider"] == "claude"
    # 再跑一次，0 条变更
    n2 = om.migrate_provider_field()
    assert n2 == 0
    print("  [PASS] migrate_provider_field idempotent: 1 then 0")


def test_migrate_provider_field_skip_write_when_nothing_to_do(m):
    """Commit 5 ⑦：无变更时不应触发 config.update（避免无意义 write）。"""
    _setup(m)
    om = m["oauth_manager"]
    # 安装 config.update 计数桩
    real_update = m["config"].update
    call_count = {"n": 0}
    def counting_update(mutator):
        call_count["n"] += 1
        return real_update(mutator)
    try:
        m["config"].update = counting_update
        # 无账户
        n0 = om.migrate_provider_field()
        assert n0 == 0
        assert call_count["n"] == 0, "no-op should not call config.update"
        # 加一个已有 provider 的账户
        real_update(lambda c: c.setdefault("oauthAccounts", []).append({
            "email": "new@x", "provider": "claude",
            "access_token": "x", "refresh_token": "x",
        }))
        call_count["n"] = 0
        n1 = om.migrate_provider_field()
        assert n1 == 0
        assert call_count["n"] == 0, "all-provider-present should not call update"
    finally:
        m["config"].update = real_update
    print("  [PASS] migrate_provider_field skips config.update when no-op")


def test_refresh_notice_openai_wording(m):
    """Commit 5 ④：OpenAI 账户 refresh 通知显示'响应头路径'而非'获取失败'。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "nr@openai.test", "provider": "openai",
        "access_token": "x", "refresh_token": "x",
        "chatgpt_account_id": "acct-1",
    })
    txt = om._build_refresh_notice("openai:a@x.com:acct-1", usage_flat=None)
    assert "响应头" in txt, txt
    assert "获取失败" not in txt
    # Claude 账户仍走老文案
    om.add_account({
        "email": "nr2@claude.test", "provider": "claude",
        "access_token": "x", "refresh_token": "x",
    })
    txt2 = om._build_refresh_notice("nr2@claude.test", usage_flat=None)
    assert "获取失败" in txt2
    print("  [PASS] _build_refresh_notice: openai gets header-path wording")


def test_openai_refresh_updates_id_token_metadata(m):
    """force_refresh 成功后应从新 id_token 解出 chatgpt_account_id / plan_type /
    organization_id 写回 config，便于 plan 升级/换组织后的 UI 立即反映。"""
    _setup(m)
    om = m["oauth_manager"]
    # 预置一个旧 metadata 的账户
    om.add_account({
        "email": "meta@openai.test",
        "provider": "openai",
        "access_token": "old-at", "refresh_token": "rt-meta",
        "id_token": "old.token.sig",
        "chatgpt_account_id": "old-acct",
        "plan_type": "free",
        "organization_id": "old-org",
    })

    import asyncio
    asyncio.run(om.force_refresh("openai:old-acct"))

    acc = om.get_account("openai:old-acct")
    # mockMode 会保留调用方传入的 workspace/account id，避免刷新时误换主键；
    # 但 plan/org/id_token 仍应从新 token 写回。
    assert acc["plan_type"] == "plus", acc.get("plan_type")
    assert acc["chatgpt_account_id"] == "old-acct", acc.get("chatgpt_account_id")
    assert acc["workspace_id"] == "old-acct", acc.get("workspace_id")
    assert acc["organization_id"] == "old-org", acc.get("organization_id")
    assert acc["id_token"] != "old.token.sig"
    print("  [PASS] force_refresh: openai decodes new id_token without changing account key")


def test_fetch_usage_openai_goes_through_wham(m):
    """OpenAI 的主动 fetch_usage 走 ChatGPT wham/usage，不依赖 channel 注册/probe。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "x@openai.test",
        "provider": "openai",
        "access_token": "at", "refresh_token": "rt",
        "chatgpt_account_id": "acct-x", "plan_type": "plus",
    })
    import asyncio
    usage = asyncio.run(om.fetch_usage("openai:x@openai.test:acct-x"))
    assert "five_hour" in usage and "seven_day" in usage
    assert usage["five_hour"]["utilization"] == 1.0, usage
    assert usage["seven_day"]["utilization"] == 3.0, usage
    assert usage.get("openai", {}).get("source") == "wham_usage"
    # 不应再标记 probe 节流桶。
    assert "openai:x@openai.test:acct-x" not in om._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage openai: unified path (wham/usage)")


# ─── TG bot: OpenAI add via PKCE ─────────────────────────────────

def test_tg_openai_add_via_pkce(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]
    # Step 1: 打开添加面板 → 选 OpenAI
    cm.on_add_menu(42, 100, "cb")
    assert rec.last("editMessageText")
    rec.clear()
    cm.on_add_openai(42, 100, "cb")
    last = rec.last("editMessageText")
    assert last and "OpenAI" in last["text"]
    # Step 2: 开始登录
    rec.clear()
    cm.on_login_openai_start(42, 100, "cb")
    last = rec.last("editMessageText")
    assert last and "auth.openai.com" in last["text"]
    st = m["states"].get_state(42)
    assert st and st["action"] == "oa_openai_code"
    verifier = st["data"]["code_verifier"]
    state = st["data"]["state"]
    # Step 3: 粘贴回调 URL（mock 下 exchange_code_sync 返回合法 token）
    callback_url = f"http://localhost:1455/auth/callback?code=mock_auth_code&state={state}"
    rec.clear()
    cm.on_login_openai_code_input(42, callback_url)
    # 应看到成功消息
    sent = rec.last("sendMessage")
    assert sent and "已添加" in sent["text"]
    # 配置里应该多了一条 openai 账户
    accounts = m["config"].get()["oauthAccounts"]
    openai_accs = [a for a in accounts if a.get("provider") == "openai"]
    assert len(openai_accs) == 1
    acc = openai_accs[0]
    assert acc["email"].startswith("mock-openai-") and acc["email"].endswith("@local")
    assert acc["chatgpt_account_id"]
    assert acc["plan_type"] == "plus"
    # state 已消费
    assert m["states"].get_state(42) is None
    print("  [PASS] tg openai add via PKCE (mock) → account saved with provider=openai")


def test_tg_openai_add_state_mismatch(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]
    cm.on_login_openai_start(42, 100, "cb")
    # 故意错 state
    bad_url = "http://localhost:1455/auth/callback?code=abc&state=WRONG"
    rec.clear()
    cm.on_login_openai_code_input(42, bad_url)
    sent = rec.last("sendMessage")
    assert sent and "state 不匹配" in sent["text"]
    # 没写入账户
    accounts = m["config"].get()["oauthAccounts"]
    assert not any(a.get("provider") == "openai" for a in accounts)
    print("  [PASS] tg openai add: state mismatch rejected, no account saved")


def test_tg_openai_add_via_rt(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]
    cm.on_add_openai(42, 100, "cb")
    rec.clear()
    cm.on_set_rt_openai_start(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "oa_openai_rt"
    # 粘 refresh_token（mockMode 下 refresh_sync 返回合法结构）
    rec.clear()
    cm.on_set_rt_openai_input(42, "abcdefghijklmnopqrstuvwxyz1234567890")
    sent = rec.last("sendMessage")
    assert sent and "已添加" in sent["text"]
    openai_accs = [a for a in m["config"].get()["oauthAccounts"]
                   if a.get("provider") == "openai"]
    assert len(openai_accs) == 1
    acc = openai_accs[0]
    # source=rt 分支
    assert "rt" in sent["text"]
    # id_token 落库
    assert acc.get("id_token")
    print("  [PASS] tg openai add via refresh_token (mock) → account saved")


def test_tg_openai_add_rt_too_short(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]
    cm.on_set_rt_openai_start(42, 100, "cb")
    rec.clear()
    cm.on_set_rt_openai_input(42, "short")
    sent = rec.last("sendMessage")
    assert sent and "过短" in sent["text"]
    print("  [PASS] tg openai add via RT rejects too-short input")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()

    orig_cfg = __import__("json").loads(__import__("json").dumps(m["config"].get()))

    tests = [
        test_pkce_generate,
        test_build_login_url,
        test_decode_id_token_mock,
        test_decode_id_token_invalid,
        test_parse_rate_limit_headers,
        test_normalize_reverse_case,
        test_accounts_check_extracts_plan_and_subscription,
        test_state_db_openai_cols_migration,
        test_add_account_openai_provider,
        test_add_account_claude_default_provider,
        test_migrate_provider_field_idempotent,
        test_migrate_provider_field_skip_write_when_nothing_to_do,
        test_refresh_notice_openai_wording,
        test_openai_refresh_updates_id_token_metadata,
        test_fetch_usage_openai_goes_through_wham,
        test_tg_openai_add_via_pkce,
        test_tg_openai_add_state_mismatch,
        test_tg_openai_add_via_rt,
        test_tg_openai_add_rt_too_short,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as exc:
                print(f"  [FAIL] {t.__name__}: {exc}")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  [ERR]  {t.__name__}: {exc}")
    finally:
        # 恢复 config
        m["config"].update(lambda c: (c.clear(), c.update(orig_cfg)))

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
