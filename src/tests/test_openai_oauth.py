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


def test_callback_url_validation_matches_issued_redirect(m):
    p = m["openai_provider"]
    assert p.validate_callback_url(
        "http://localhost:1455/auth/callback?code=abc&state=state"
    )
    assert not p.validate_callback_url(
        "https://localhost:1455/auth/callback?code=abc&state=state"
    )
    assert not p.validate_callback_url(
        "http://127.0.0.1:1455/auth/callback?code=abc&state=state"
    )
    assert not p.validate_callback_url(
        "http://localhost:1455/other?code=abc&state=state"
    )


def test_accounts_check_current_list_shape_and_cli_headers(m):
    p = m["openai_provider"]
    captured = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "accounts": [
                    {
                        "id": "acct-personal",
                        "plan_type": "plus",
                        "name": "Personal",
                        "structure": "personal",
                    },
                    {
                        "id": "acct-team",
                        "plan_type": "team",
                        "name": "Engineering",
                        "structure": "workspace",
                        "workspace_backend_origin": "https://gov.chatgpt.com",
                        "account_routing_override": "us_cr",
                    },
                ],
                "account_ordering": ["acct-team", "acct-personal"],
                "default_account_id": "acct-team",
            }

    original_get = p.network.get_sync
    old_disable_env = _ap_os.environ.pop("DISABLE_OAUTH_NETWORK_CALLS", None)
    try:
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))

        def fake_get(url, *, headers=None, **kwargs):
            captured.update(url=url, headers=dict(headers or {}), kwargs=kwargs)
            return Resp()

        p.network.get_sync = fake_get
        info = p.fetch_accounts_check_sync("at", workspace_id="acct-team")
    finally:
        p.network.get_sync = original_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))
        if old_disable_env is not None:
            _ap_os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = old_disable_env

    assert captured["url"].endswith("/backend-api/wham/accounts/check")
    assert captured["headers"]["ChatGPT-Account-ID"] == "acct-team"
    assert "origin" not in captured["headers"] and "referer" not in captured["headers"]
    assert info["workspace_id"] == "acct-team"
    assert info["workspace_name"] == "Engineering"
    assert info["workspace_type"] == "workspace"
    assert info["plan_type"] == "team"
    assert info["workspace_backend_origin"] == "https://gov.chatgpt.com"
    assert info["account_routing_override"] == "us_cr"
    assert info["is_default"] is True


def test_current_rate_limit_headers_dynamic_families_and_persistence(m):
    _setup(m)
    p = m["openai_provider"]
    now = int(__import__("time").time())
    snap = p.parse_rate_limit_headers({
        "X-Codex-Primary-Used-Percent": "105.5",
        "X-Codex-Primary-Window-Minutes": "300",
        "X-Codex-Primary-Reset-At": str(now + 120),
        "X-Codex-Secondary-Used-Percent": "40",
        "X-Codex-Secondary-Window-Minutes": "10080",
        "X-Codex-Secondary-Reset-After-Seconds": "600",
        "X-Codex-Credits-Has-Credits": "true",
        "X-Codex-Credits-Unlimited": "0",
        "X-Codex-Credits-Balance": "12.50",
        "X-Codex-Rate-Limit-Reached-Type": "workspace_member_credits_depleted",
        "X-Gpt-Reserve-Limit-Name": "GPT reserve",
        "X-Gpt-Reserve-Primary-Used-Percent": "7.5",
        "X-Gpt-Reserve-Primary-Window-Minutes": "60",
        "X-Gpt-Reserve-Primary-Reset-At": str(now + 30),
    })
    assert snap["primary_used_pct"] == 100.0
    assert 0 <= snap["primary_reset_sec"] <= 120
    assert snap["primary_reset_at"] == now + 120
    assert snap["secondary_reset_sec"] == 600
    assert snap["credits"] == {
        "has_credits": True, "unlimited": False, "balance": "12.50",
    }
    assert snap["rate_limit_reached_type"] == "workspace_member_credits_depleted"
    assert snap["additional_rate_limits"][0]["limit_id"] == "gpt_reserve"
    assert snap["additional_rate_limits"][0]["primary"]["used_percent"] == 7.5

    account_key = "openai:current-headers@example.test:acct-current"
    m["state_db"].quota_save_openai_snapshot(account_key, snap)
    row = m["state_db"].quota_load(account_key)
    assert row["codex_primary_reset_at"] == now + 120
    assert row["codex_credits_has_credits"] is True
    assert row["codex_credits_unlimited"] is False
    usage = m["oauth_manager"].usage_from_quota_row(row)
    assert usage["openai"]["credits"]["balance"] == "12.50"
    assert usage["openai"]["additional_rate_limits"][0]["limit_id"] == "gpt_reserve"


def test_current_nested_rate_limit_event_normalization(m):
    p = m["openai_provider"]
    now = int(__import__("time").time())
    snap = p.parse_rate_limit_event({
        "type": "codex.rate_limits",
        "plan_type": "pro",
        "metered_limit_name": "gpt-reserve",
        "rate_limits": {
            "primary": {
                "used_percent": -2,
                "window_minutes": 60,
                "reset_at": now + 90,
            },
            "secondary": {
                "used_percent": 12.5,
                "window_minutes": 1440,
                "reset_at": now + 180,
            },
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "5"},
    })
    assert snap["plan_type"] == "pro"
    assert snap["rate_limits"][0]["limit_id"] == "gpt_reserve"
    assert snap["rate_limits"][0]["primary"]["used_percent"] == 0.0
    assert snap["additional_rate_limits"] == snap["rate_limits"]
    assert snap["credits"]["balance"] == "5"
    # A legacy flat codex.rate_limits event deliberately falls through to the
    # caller's old adapter rather than becoming an empty current snapshot.
    assert p.parse_rate_limit_event({
        "type": "codex.rate_limits", "primary_used_pct": 42,
    }) is None


def test_revoke_request_preference_and_delete_failure_degrades_locally(m):
    _setup(m)
    p = m["openai_provider"]
    captured = []

    class Resp:
        def raise_for_status(self):
            return None

    original_post = p.network.post_sync
    old_disable_env = _ap_os.environ.pop("DISABLE_OAUTH_NETWORK_CALLS", None)
    try:
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))

        def fake_post(url, **kwargs):
            captured.append((url, kwargs))
            return Resp()

        p.network.post_sync = fake_post
        assert p.revoke_sync(
            refresh_token="refresh-secret", access_token="access-secret",
            account_key="openai:logout@example.test:acct-logout",
        )
        assert p.revoke_sync(access_token="access-only")
    finally:
        p.network.post_sync = original_post
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))
        if old_disable_env is not None:
            _ap_os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = old_disable_env

    assert captured[0][0] == p.REVOKE_URL
    assert captured[0][1]["json"] == {
        "token": "refresh-secret", "token_type_hint": "refresh_token",
        "client_id": p.CLIENT_ID,
    }
    assert captured[0][1]["proxy_channel"] == "oauth:openai:logout@example.test:acct-logout"
    assert captured[1][1]["json"] == {
        "token": "access-only", "token_type_hint": "access_token",
    }

    om = m["oauth_manager"]
    om.add_account({
        "email": "delete@example.test", "provider": "openai",
        "access_token": "at", "refresh_token": "rt",
        "chatgpt_account_id": "acct-delete",
    })
    original_revoke = p.revoke_sync
    p.revoke_sync = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("upstream down"))
    try:
        om.delete_account("openai:delete@example.test:acct-delete")
    finally:
        p.revoke_sync = original_revoke
    assert om.get_account("openai:delete@example.test:acct-delete") is None


def test_reset_credit_consume_forwards_optional_credit_id_without_browser_headers(m):
    p = m["openai_provider"]
    captured = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "reset", "windows_reset": 1}

    original_post = p.network.post_sync
    old_disable_env = _ap_os.environ.pop("DISABLE_OAUTH_NETWORK_CALLS", None)
    try:
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))

        def fake_post(url, **kwargs):
            captured.update(url=url, kwargs=kwargs)
            return Resp()

        p.network.post_sync = fake_post
        result = p.consume_rate_limit_reset_credit_sync(
            "at", idempotency_key="idem", account_id="acct",
            credit_id="credit-2", account_key="openai:x@example.test:acct",
        )
    finally:
        p.network.post_sync = original_post
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))
        if old_disable_env is not None:
            _ap_os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = old_disable_env

    assert result["outcome"] == "reset"
    assert captured["kwargs"]["json"] == {
        "redeem_request_id": "idem", "credit_id": "credit-2",
    }
    headers = captured["kwargs"]["headers"]
    assert headers["ChatGPT-Account-ID"] == "acct"
    assert "origin" not in headers and "referer" not in headers
    assert captured["kwargs"]["proxy_channel"] == "oauth:openai:x@example.test:acct"


# ─── state_db schema 迁移 ────────────────────────────────────────

def test_state_store_openai_quota_shape(m):
    st=m["state_db"];st.init();st.quota_save_openai_snapshot("openai:shape@test", {"primary_used_pct":1,"primary_window_min":300})
    row=st.quota_load("openai:shape@test")
    for expected in ("codex_primary_used_pct","codex_secondary_used_pct","codex_primary_window_min","codex_primary_over_secondary_pct"):
        assert expected in row


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
    assert "📊" not in txt, txt
    assert "获取失败" not in txt
    om.add_account({
        "email": "nr2@claude.test", "provider": "claude",
        "access_token": "x", "refresh_token": "x",
    })
    txt2 = om._build_refresh_notice("nr2@claude.test", usage_flat=None)
    assert "📊" not in txt2
    assert "获取失败" not in txt2
    txt3 = om._build_refresh_notice("nr2@claude.test", usage_flat={
        "five_hour_util": 12.0,
        "five_hour_reset": "2026-08-23T12:00:00Z",
        "seven_day_util": 34.0,
        "seven_day_reset": "2026-08-30T12:00:00Z",
        "fable_util": 56.0,
        "fable_reset": "2026-09-02T12:00:00Z",
    })
    assert "5h 用量" in txt3
    assert "7d 用量" in txt3
    assert "Fable 7d: <b>56%</b>" in txt3
    assert "2026-09-02 20:00:00" in txt3
    print("  [PASS] _build_refresh_notice: empty quota is omitted; Claude includes Fable")


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


def test_openai_eight_day_refresh_fallback_uses_only_reliable_timestamp(m):
    _setup(m)
    om = m["oauth_manager"]
    now = datetime.now(timezone.utc)
    for email, last_refresh in (
        ("stale@openai.test", now - timedelta(days=9)),
        ("fresh@openai.test", now - timedelta(days=7)),
    ):
        om.add_account({
            "email": email,
            "provider": "openai",
            "access_token": f"at-{email}",
            "refresh_token": f"rt-{email}",
            "expired": "",  # eight-day policy is only the unknown-expiry fallback
            "last_refresh": last_refresh.isoformat(),
            "chatgpt_account_id": f"acct-{email}",
        })
    om.add_account({
        "email": "unknown@openai.test",
        "provider": "openai",
        "access_token": "at-unknown",
        "refresh_token": "rt-unknown",
        "expired": (now + timedelta(days=1)).isoformat(),
        "chatgpt_account_id": "acct-unknown",
    })

    import asyncio
    stale = asyncio.run(om.ensure_valid_token(
        "openai:stale@openai.test:acct-stale@openai.test"
    ))
    fresh = asyncio.run(om.ensure_valid_token(
        "openai:fresh@openai.test:acct-fresh@openai.test"
    ))
    unknown = asyncio.run(om.ensure_valid_token(
        "openai:unknown@openai.test:acct-unknown"
    ))
    assert stale.startswith("mock-openai-access-")
    assert fresh == "at-fresh@openai.test"
    assert unknown == "at-unknown"


def test_fetch_usage_openai_goes_through_wham(m):
    """OpenAI 的主动 fetch_usage 走 ChatGPT wham/usage，不依赖 channel 注册/probe。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "x@openai.test",
        "provider": "openai",
        "access_token": "at", "refresh_token": "rt",
        "expired": "2099-01-01T00:00:00Z",
        "chatgpt_account_id": "acct-x", "plan_type": "plus",
    })

    called = {}
    orig_fetch = m["openai_provider"].fetch_wham_usage

    async def _fake_fetch(access_token: str, *, account_id: str | None = None):
        called["access_token"] = access_token
        called["account_id"] = account_id
        return {
            "five_hour": {"utilization": 1.0},
            "seven_day": {"utilization": 3.0},
            "seven_day_sonnet": {},
            "seven_day_opus": {},
            "extra_usage": {"is_enabled": False},
            "openai": {"source": "wham_usage"},
        }

    import asyncio
    try:
        m["openai_provider"].fetch_wham_usage = _fake_fetch
        usage = asyncio.run(om.fetch_usage("openai:x@openai.test:acct-x"))
    finally:
        m["openai_provider"].fetch_wham_usage = orig_fetch

    assert called == {"access_token": "at", "account_id": "acct-x"}, called
    assert "five_hour" in usage and "seven_day" in usage
    assert usage["five_hour"]["utilization"] == 1.0, usage
    assert usage["seven_day"]["utilization"] == 3.0, usage
    assert usage.get("openai", {}).get("source") == "wham_usage"
    # 不应再标记 probe 节流桶。
    assert "openai:x@openai.test:acct-x" not in om._OPENAI_PROBE_LAST
    print("  [PASS] fetch_usage openai: unified path (wham/usage + account id)")


def test_fetch_wham_usage_sends_account_id_header(m):
    """wham/usage 请求显式带 ChatGPT-Account-ID，与 Codex BackendClient 对齐。"""
    _setup(m)
    p = m["openai_provider"]
    captured = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"used_percent": 1, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 3, "limit_window_seconds": 604800},
                },
                "credits": {
                    "has_credits": True, "unlimited": False, "balance": "9.99",
                },
                "rate_limit_reached_type": {
                    "type": "workspace_member_credits_depleted",
                },
                "additional_rate_limits": [{
                    "metered_feature": "gpt_reserve",
                    "limit_name": "GPT reserve",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 7,
                            "limit_window_seconds": 3600,
                            "reset_at": 1_900_000_000,
                        },
                    },
                }],
            }

    orig_get = p.network.get_sync
    try:
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))

        def _fake_get(url, *, headers=None, **kwargs):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            return _Resp()

        p.network.get_sync = _fake_get
        usage = p.fetch_wham_usage_sync("at-token", account_id="acct-x")
    finally:
        p.network.get_sync = orig_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))

    assert captured["url"] == p.WHAM_USAGE_URL
    assert captured["headers"].get("authorization") == "Bearer at-token"
    assert captured["headers"].get("ChatGPT-Account-ID") == "acct-x"
    assert captured["headers"].get("x-openai-codex-luna-reserve") == "1"
    assert "origin" not in captured["headers"] and "referer" not in captured["headers"]
    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 3.0
    assert usage["openai"]["credits"] == {
        "has_credits": True, "unlimited": False, "balance": "9.99",
    }
    assert usage["openai"]["rate_limit_reached_type"] == "workspace_member_credits_depleted"
    additional = usage["openai"]["additional_rate_limits"]
    assert additional[0]["limit_id"] == "gpt_reserve"
    assert additional[0]["primary"]["window_minutes"] == 60
    print("  [PASS] fetch_wham_usage_sync: sends ChatGPT-Account-ID header")


def test_fetch_rate_limit_reset_credits_sends_account_id_header(m):
    """reset-card detail list mirrors Codex WHAM path and account-id routing."""
    _setup(m)
    p = m["openai_provider"]
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "available_count": 2,
                "total_earned_count": 4,
                "credits": [
                    {
                        "id": "credit-1",
                        "reset_type": "codex_rate_limits",
                        "status": "available",
                        "granted_at": "2026-06-17T00:00:00Z",
                        "expires_at": "2026-07-17T00:00:00Z",
                        "title": "ignored",
                    },
                    {
                        "id": "credit-2",
                        "reset_type": "codex_rate_limits",
                        "status": "available",
                        "granted_at": "2026-06-18T00:00:00Z",
                        "expires_at": None,
                    },
                ],
            }

    orig_get = p.network.get_sync
    try:
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", False))

        def _fake_get(url, *, headers=None, **kwargs):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["timeout"] = kwargs.get("timeout")
            captured["proxy_purpose"] = kwargs.get("proxy_purpose")
            return _Resp()

        p.network.get_sync = _fake_get
        details = p.fetch_rate_limit_reset_credits_sync("at-token", account_id="acct-x")
    finally:
        p.network.get_sync = orig_get
        m["config"].update(lambda c: c.setdefault("oauth", {}).__setitem__("mockMode", True))

    assert captured["url"] == p.WHAM_RESET_CREDIT_LIST_URL
    assert captured["headers"].get("authorization") == "Bearer at-token"
    assert captured["headers"].get("ChatGPT-Account-ID") == "acct-x"
    assert captured["proxy_purpose"] == "oauth_openai"
    assert details == {
        "available_count": 2,
        "data": [
            {
                "id": "credit-1",
                "reset_type": "codex_rate_limits",
                "status": "available",
                "granted_at": "2026-06-17T00:00:00Z",
                "expires_at": "2026-07-17T00:00:00Z",
                "title": "ignored",
            },
            {
                "id": "credit-2",
                "reset_type": "codex_rate_limits",
                "status": "available",
                "granted_at": "2026-06-18T00:00:00Z",
                "expires_at": None,
            },
        ],
    }
    print("  [PASS] fetch_rate_limit_reset_credits_sync: WHAM path + sanitized payload")


def test_oauth_manager_fetches_openai_reset_credit_details_with_account_id(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "cards@openai.test",
        "provider": "openai",
        "access_token": "at", "refresh_token": "rt",
        "expired": "2099-01-01T00:00:00Z",
        "chatgpt_account_id": "acct-cards", "plan_type": "plus",
    })

    called = {}
    orig_fetch = m["openai_provider"].fetch_rate_limit_reset_credits

    async def _fake_fetch(access_token: str, *, account_id: str | None = None):
        called["access_token"] = access_token
        called["account_id"] = account_id
        return {"available_count": 1, "data": []}

    import asyncio
    try:
        m["openai_provider"].fetch_rate_limit_reset_credits = _fake_fetch
        details = asyncio.run(om.fetch_openai_rate_limit_reset_credits("openai:cards@openai.test:acct-cards"))
    finally:
        m["openai_provider"].fetch_rate_limit_reset_credits = orig_fetch

    assert called == {"access_token": "at", "account_id": "acct-cards"}, called
    assert details["available_count"] == 1
    print("  [PASS] oauth_manager reset-credit details: account token + workspace id")


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
    import uuid
    assert uuid.UUID(acc["codexDeviceInstallationId"]).version == 4
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


def test_tg_openai_callback_url_requires_state(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]

    cm.on_login_openai_start(42, 100, "cb")
    rec.clear()
    cm.on_login_openai_code_input(
        42, "http://localhost:1455/auth/callback?code=mock_auth_code",
    )
    sent = rec.last("sendMessage")
    assert sent and "缺少 state" in sent["text"]
    assert not any(
        account.get("provider") == "openai"
        for account in m["config"].get()["oauthAccounts"]
    )


def test_tg_openai_callback_rejects_wrong_redirect_but_keeps_bare_code_compat(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["oauth_menu"]

    cm.on_login_openai_start(42, 100, "cb")
    state = m["states"].get_state(42)["data"]["state"]
    rec.clear()
    cm.on_login_openai_code_input(
        42, f"https://attacker.example/auth/callback?code=abc&state={state}",
    )
    sent = rec.last("sendMessage")
    assert sent and "回调地址不是本次" in sent["text"]
    assert not any(
        account.get("provider") == "openai"
        for account in m["config"].get()["oauthAccounts"]
    )

    # The rejected callback consumes the pending state. Start a fresh flow and
    # verify existing bare-code compatibility; PKCE still binds it to this chat.
    rec.clear()
    cm.on_login_openai_start(42, 100, "cb")
    rec.clear()
    cm.on_login_openai_code_input(42, "mock-bare-code")
    sent = rec.last("sendMessage")
    assert sent and "已添加" in sent["text"]


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
    # id_token 与默认开启的 workspace-scoped device identity 一并落库
    assert acc.get("id_token")
    import uuid
    assert uuid.UUID(acc["codexDeviceInstallationId"]).version == 4
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
        test_state_store_openai_quota_shape,
        test_add_account_openai_provider,
        test_add_account_claude_default_provider,
        test_migrate_provider_field_idempotent,
        test_migrate_provider_field_skip_write_when_nothing_to_do,
        test_refresh_notice_openai_wording,
        test_openai_refresh_updates_id_token_metadata,
        test_fetch_usage_openai_goes_through_wham,
        test_fetch_wham_usage_sends_account_id_header,
        test_fetch_rate_limit_reset_credits_sends_account_id_header,
        test_oauth_manager_fetches_openai_reset_credit_details_with_account_id,
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
