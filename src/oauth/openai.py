"""OpenAI (ChatGPT / Codex CLI) OAuth provider。

对齐 openai/codex 上游 (codex-rs/login) 当前实现：
  - PKCE: code_verifier 是 **base64url_no_pad(64 随机字节)**（与官方 Codex 一致；
    历史版本曾用 hex 也能跑，但已切回官方编码以避免上游 fingerprint 收紧时翻车）
  - Authorize URL 必带 `id_token_add_organizations=true` +
    `codex_cli_simplified_flow=true` + `originator=codex_cli_rs`，scope 含
    `api.connectors.read api.connectors.invoke`
  - exchange_code 用 **form-urlencoded**
  - **refresh 用 JSON**，只发 `{client_id, grant_type, refresh_token}`，不带 scope
    （Codex 已在 codex-rs/login/src/auth/manager.rs 把刷新切到 JSON 且去掉 scope）
  - email / chatgpt_account_id / organizations 从 id_token payload 解码拿到
    （**不验 JWT 签名**，仅校验 exp）

上游请求（responses）与用量（response 头）在本模块之外完成：
  - 请求构造：src/channel/openai_oauth_channel.py (Commit 2)
  - 响应头解析：parse_rate_limit_headers (见下)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from .. import network


# ─── OAuth 常量（与 sub2api 对齐，来源 Codex CLI 官方）────────────

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"

SCOPES_AUTHORIZE = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)
# 刷新时 Codex 官方实现完全不传 scope（refresh 默认继承原 scope）。
# 历史 SCOPES_REFRESH 仅在 mockMode 下作为响应里的占位字段使用。
SCOPES_REFRESH = "openid profile email"

# 固定的 Codex CLI User-Agent。与 sub2api 最新 Codex 上游请求指纹保持一致。
USER_AGENT = "codex_cli_rs/0.125.0"

# 运行期请求超时（换 token / 刷 token）。
_TOKEN_HTTP_TIMEOUT = 120.0

# ChatGPT backend-api accounts/check：用于在 token 刷新后补全最新 plan / 订阅过期时间。
ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
_ACCOUNTS_CHECK_TIMEOUT = 15.0

# ChatGPT/Codex 私有用量端点。它不是 OpenAI public API；只用于主动 quota
# 刷新/后台 monitor。业务请求返回的 x-codex-* 响应头仍由 failover 实时采样。
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_WHAM_USAGE_TIMEOUT = 30.0


# ─── mock 开关（与 oauth_manager.mock_mode_enabled 同语义） ────────

def _mock_mode_enabled() -> bool:
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1":
        return True
    from .. import config  # 延迟导入，避免循环依赖
    return bool(config.get().get("oauth", {}).get("mockMode", False))


def _mock_id_token(email: str | None = None, *, account_id: str | None = None,
                   org_id: str | None = None) -> str:
    """为 mockMode 构造一个合法结构的 id_token（3 段 base64）。

    payload 包含 sub2api 需要的所有字段，签名部分留空（我们本来也不验签）。
    """
    header = {"alg": "none", "typ": "JWT"}
    if not email:
        email = f"mock-openai-{secrets.token_hex(4)}@local"
    if not account_id:
        account_id = f"mock-acct-{secrets.token_hex(4)}"
    if not org_id:
        org_id = "org-mock"
    payload = {
        "sub": f"user-{secrets.token_hex(4)}",
        "email": email,
        "email_verified": True,
        "iss": "https://auth.openai.com",
        "aud": [CLIENT_ID],
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": f"mock-user-{secrets.token_hex(4)}",
            "chatgpt_plan_type": "plus",
            "user_id": f"user-{secrets.token_hex(4)}",
            "poid": org_id,
            "organizations": [
                {"id": org_id, "role": "owner", "title": "Mock Org",
                 "is_default": True},
            ],
        },
    }

    def _b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_b64(header)}.{_b64(payload)}."


def _mock_token_response(email: str | None = None, *, workspace_id: str | None = None,
                         org_id: str | None = None) -> dict:
    return {
        "access_token": "mock-openai-access-" + secrets.token_hex(8),
        "refresh_token": "mock-openai-refresh-" + secrets.token_hex(8),
        "id_token": _mock_id_token(email, account_id=workspace_id, org_id=org_id),
        "token_type": "Bearer",
        "expires_in": 28800,
        "scope": SCOPES_AUTHORIZE,
    }


# ─── PKCE ────────────────────────────────────────────────────────

def pkce_generate() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。

    与 openai/codex (codex-rs/login/src/pkce.rs) 对齐：
      verifier  = base64url_no_pad(64 随机字节)  → 86 字符
      challenge = base64url_no_pad(sha256(verifier))
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_login_url(code_challenge: str, state: str,
                    *, redirect_uri: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri or REDIRECT_URI,
        "scope": SCOPES_AUTHORIZE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # OpenAI / Codex CLI 特有
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        # 与 codex-rs/login/src/server.rs:508 对齐：authorize URL 必带 originator
        "originator": "codex_cli_rs",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ─── code → token ─────────────────────────────────────────────────

def _post_token_form(data: dict) -> dict:
    """同步 POST form-urlencoded 到 token 端点（用于 authorization_code 换 token）。"""
    resp = network.post_sync(
        TOKEN_URL,
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
            "user-agent": USER_AGENT,
            # 与 codex-rs/login/src/auth/default_client.rs:234 对齐：Codex HTTP 都带 originator
            "originator": "codex_cli_rs",
        },
        timeout=_TOKEN_HTTP_TIMEOUT,
        proxy_purpose="oauth_openai",
    )
    resp.raise_for_status()
    return resp.json()


def _post_token_json(data: dict) -> dict:
    """同步 POST application/json 到 token 端点。

    Codex 当前的 refresh_token 走 JSON
    （codex-rs/login/src/auth/manager.rs:817-829）。
    """
    resp = network.post_sync(
        TOKEN_URL,
        json=data,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": USER_AGENT,
            "originator": "codex_cli_rs",
        },
        timeout=_TOKEN_HTTP_TIMEOUT,
        proxy_purpose="oauth_openai",
    )
    resp.raise_for_status()
    return resp.json()



def _extract_org_id_from_token(token: str | None) -> str:
    """从 access_token/id_token payload 里提取 OpenAI poid/org id（不验签）。"""
    if not token:
        return ""
    try:
        claims = decode_id_token(token)
    except Exception:
        return ""
    openai_auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(openai_auth, dict):
        return ""
    return str(openai_auth.get("poid") or "")


def _extract_plan_type(account: dict) -> str:
    acct = account.get("account")
    if isinstance(acct, dict):
        plan = acct.get("plan_type") or acct.get("subscription_plan")
        if isinstance(plan, str) and plan:
            return plan
    ent = account.get("entitlement")
    if isinstance(ent, dict):
        plan = ent.get("subscription_plan")
        if isinstance(plan, str) and plan:
            return plan
    return ""


def _extract_subscription_expires_at(account: dict) -> str:
    ent = account.get("entitlement")
    if isinstance(ent, dict):
        expires_at = ent.get("expires_at")
        if isinstance(expires_at, str):
            return expires_at
    return ""


def _extract_account_workspace_id(account: dict, *, account_id: str = "") -> str:
    for key in ("workspace_id", "chatgpt_account_id", "account_id"):
        value = account.get(key)
        if isinstance(value, str) and value:
            return value
    acct = account.get("account")
    if isinstance(acct, dict):
        for key in ("workspace_id", "chatgpt_account_id", "account_id", "id"):
            value = acct.get(key)
            if isinstance(value, str) and value:
                return value
    workspace = account.get("workspace")
    if isinstance(workspace, dict):
        for key in ("workspace_id", "chatgpt_account_id", "account_id", "id"):
            value = workspace.get(key)
            if isinstance(value, str) and value:
                return value
    # accounts/check 的 accounts 通常是 workspace/account id → payload 的映射，
    # 真实 workspace id 可能只出现在映射 key 上；但 org-* key 是组织 id，
    # 不能拿来当 chatgpt-account-id。
    if account_id and not str(account_id).startswith("org-"):
        return str(account_id)
    return ""


def _extract_workspace_name(account: dict) -> str:
    for obj in (account.get("workspace"), account.get("account"), account):
        if not isinstance(obj, dict):
            continue
        for key in ("workspace_name", "account_name", "display_name", "name", "title"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _infer_workspace_type(*, plan_type: str = "", account: dict | None = None,
                          org: dict | None = None) -> str:
    for obj in (account, account.get("account") if isinstance(account, dict) else None, org):
        if not isinstance(obj, dict):
            continue
        for key in ("workspace_type", "account_type", "type", "plan_category"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    p = (plan_type or "").lower()
    if any(x in p for x in ("team", "business")):
        return "team"
    if any(x in p for x in ("enterprise", "edu")):
        return "enterprise"
    if any(x in p for x in ("plus", "pro", "free")):
        return "personal"
    return ""


def _extract_email_from_account(account: dict) -> str:
    for obj in (account, account.get("account"), account.get("user"), account.get("profile")):
        if not isinstance(obj, dict):
            continue
        email = obj.get("email")
        if isinstance(email, str) and email:
            return email
    return ""


def _account_check_candidate(account: dict, *, account_id: str = "") -> dict:
    plan_type = _extract_plan_type(account)
    workspace_id = _extract_account_workspace_id(account, account_id=account_id)
    org_id = str(account.get("organization_id") or account.get("org_id") or "")
    if not org_id:
        for obj in (account.get("account"), account.get("workspace")):
            if not isinstance(obj, dict):
                continue
            org_id = str(obj.get("organization_id") or obj.get("org_id") or "")
            if org_id:
                break
    if not org_id and str(account.get("id") or "").startswith("org-"):
        org_id = str(account.get("id") or "")
    if not org_id and str(account_id or "").startswith("org-"):
        org_id = str(account_id or "")
    return {
        "workspace_id": workspace_id,
        "chatgpt_account_id": workspace_id,
        "workspace_name": _extract_workspace_name(account),
        "workspace_type": _infer_workspace_type(plan_type=plan_type, account=account),
        "organization_id": org_id,
        "plan_type": plan_type,
        "subscription_expires_at": _extract_subscription_expires_at(account),
        "email": _extract_email_from_account(account),
        "is_default": bool((account.get("account") or {}).get("is_default"))
        if isinstance(account.get("account"), dict) else False,
    }


def _candidate_matches(info: dict, *, workspace_id: str = "", org_id: str = "") -> bool:
    if workspace_id and str(info.get("workspace_id") or "") == workspace_id:
        return True
    if workspace_id and str(info.get("chatgpt_account_id") or "") == workspace_id:
        return True
    if org_id and str(info.get("organization_id") or "") == org_id:
        return True
    return False


def _fetch_accounts_check_payload_sync(access_token: str) -> dict | None:
    if not access_token or _mock_mode_enabled():
        return None
    try:
        resp = network.get_sync(
            ACCOUNTS_CHECK_URL,
            headers={
                "authorization": f"Bearer {access_token}",
                "origin": "https://chatgpt.com",
                "referer": "https://chatgpt.com/",
                "accept": "application/json",
                "user-agent": USER_AGENT,
            },
            timeout=_ACCOUNTS_CHECK_TIMEOUT,
            proxy_purpose="oauth_openai",
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _accounts_check_candidates(payload: dict, *, email: str | None = None) -> list[dict]:
    """把 accounts/check payload 规范化为候选列表。

    仅供选择“当前 token 对应 workspace”的补全信息使用；不会在保存层全量展开。
    """
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        return []

    wanted_email = str(email or "").strip().lower()
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_id, raw in accounts.items():
        if not isinstance(raw, dict):
            continue
        info = _account_check_candidate(raw, account_id=str(raw_id or ""))
        cand_email = str(info.get("email") or "").strip().lower()
        if wanted_email and cand_email and cand_email != wanted_email:
            continue
        # 至少要有一个可用识别字段；否则上层无法安全匹配当前 workspace。
        if not (info.get("workspace_id") or info.get("organization_id") or info.get("plan_type")):
            continue
        key = (
            str(info.get("workspace_id") or ""),
            str(info.get("organization_id") or ""),
            str(info.get("plan_type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(info)
    return out


def _select_accounts_check_candidate(candidates: list[dict], *, workspace_id: str = "",
                                     org_id: str = "") -> dict | None:
    identity_given = bool(workspace_id or org_id)
    if workspace_id:
        for info in candidates:
            if _candidate_matches(info, workspace_id=workspace_id, org_id=""):
                return info

    if org_id:
        for info in candidates:
            if _candidate_matches(info, workspace_id="", org_id=org_id):
                return info

    # 如果当前 token 已带 workspace/org 身份，但 accounts/check 没匹配上，
    # 不要 fallback 到 default/paid/any，避免把 Personal 的 plan/name 错写到 Team。
    if identity_given:
        return None

    default_c: dict | None = None
    paid_c: dict | None = None
    any_c: dict | None = None
    for info in candidates:
        if not info.get("plan_type"):
            continue
        if _candidate_matches(info, workspace_id=workspace_id, org_id=org_id):
            return info
        if any_c is None:
            any_c = info
        if info.get("is_default") is True:
            default_c = info
        if str(info.get("plan_type") or "").lower() != "free" and paid_c is None:
            paid_c = info

    return default_c or paid_c or any_c


def fetch_accounts_check_sync(access_token: str, *, org_id: str | None = None,
                              workspace_id: str | None = None,
                              email: str | None = None) -> dict | None:
    """调用 ChatGPT accounts/check 补全 plan / subscription 信息。

    这是 best-effort 辅助能力：失败、结构不符合预期、无 plan 时均返回 None，
    调用方继续使用 id_token 里的信息，不影响 token 刷新主流程。
    """
    org_id = str(org_id or _extract_org_id_from_token(access_token) or "")
    workspace_id = str(workspace_id or "")
    payload = _fetch_accounts_check_payload_sync(access_token)
    if not payload:
        return None
    candidates = _accounts_check_candidates(payload, email=email)
    if not candidates:
        return None
    return _select_accounts_check_candidate(
        candidates, workspace_id=workspace_id, org_id=org_id,
    )


def enrich_token_response_sync(data: dict, *, workspace_id: str | None = None,
                               org_id: str | None = None) -> dict:
    """用 accounts/check 补全 token 响应中的 plan / subscription 信息。"""
    if not isinstance(data, dict) or not data.get("access_token"):
        return data

    selected_org_id = org_id or _extract_org_id_from_token(data.get("access_token")) or ""
    selected_workspace_id = workspace_id or ""
    selected_email = ""
    if data.get("id_token"):
        try:
            info = extract_user_info(decode_id_token(data["id_token"]))
            selected_org_id = selected_org_id or str(info.get("organization_id") or "")
            selected_workspace_id = selected_workspace_id or str(
                info.get("workspace_id") or info.get("chatgpt_account_id") or ""
            )
            selected_email = str(info.get("email") or "")
        except Exception:
            selected_org_id = selected_org_id or ""
    info = fetch_accounts_check_sync(
        data.get("access_token", ""),
        org_id=selected_org_id,
        workspace_id=selected_workspace_id,
        email=selected_email,
    )
    if not info:
        return data

    enriched = dict(data)
    for key in (
        # chatgpt_account_id/workspace_id 必须来自 token/id_token 本身；
        # accounts/check 只用于按当前 identity 补 plan/name/subscription，不能改主键。
        "workspace_name", "workspace_type",
        "organization_id", "plan_type", "subscription_expires_at", "email",
    ):
        val = info.get(key)
        if val:
            enriched[key] = val
    return enriched

def exchange_code_sync(code: str, code_verifier: str,
                       *, redirect_uri: str | None = None) -> dict:
    """同步版：换 token。mockMode 下返回假 token 响应。"""
    if _mock_mode_enabled():
        return _mock_token_response()
    return enrich_token_response_sync(_post_token_form({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri or REDIRECT_URI,
        "code_verifier": code_verifier,
    }))


async def exchange_code(code: str, code_verifier: str,
                        *, redirect_uri: str | None = None) -> dict:
    return await asyncio.to_thread(
        exchange_code_sync, code, code_verifier, redirect_uri=redirect_uri
    )


def refresh_sync(refresh_token: str, *, email: str | None = None,
                 workspace_id: str | None = None,
                 org_id: str | None = None) -> dict:
    """同步版：用 refresh_token 换新 access_token。

    email 仅用于 mockMode 伪造 id_token 时保持 email 一致，真实 HTTP 不用。
    """
    if _mock_mode_enabled():
        return _mock_token_response(email, workspace_id=workspace_id, org_id=org_id)
    # 与 codex-rs/login/src/auth/manager.rs:817 对齐：refresh 走 JSON，仅 3 字段，不带 scope。
    return enrich_token_response_sync(_post_token_json({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }), workspace_id=workspace_id, org_id=org_id)


async def refresh(refresh_token: str, *, email: str | None = None,
                  workspace_id: str | None = None,
                  org_id: str | None = None) -> dict:
    return await asyncio.to_thread(
        refresh_sync, refresh_token,
        email=email, workspace_id=workspace_id, org_id=org_id,
    )


# ─── ChatGPT/Codex quota: wham/usage ─────────────────────────────

def _coerce_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_from_epoch_or_after(*, reset_at: Any = None,
                             reset_after_seconds: Any = None) -> str | None:
    ts = _coerce_int(reset_at)
    if ts is None or ts <= 0:
        after = _coerce_int(reset_after_seconds)
        if after is None:
            return None
        ts = int(time.time()) + max(0, after)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _wham_window_block(win: dict | None) -> dict:
    if not isinstance(win, dict):
        return {}
    util = _coerce_float(win.get("used_percent"))
    if util is None:
        return {}
    return {
        "utilization": util,
        "resets_at": _iso_from_epoch_or_after(
            reset_at=win.get("reset_at"),
            reset_after_seconds=win.get("reset_after_seconds"),
        ),
        "limit_window_seconds": _coerce_int(win.get("limit_window_seconds")),
    }


def normalize_wham_usage(payload: dict) -> dict:
    """把 ChatGPT `backend-api/wham/usage` 响应规范成通用 usage 结构。

    wham 返回 primary_window / secondary_window；真实观测 primary=5h、
    secondary=7d，但这里仍优先按 limit_window_seconds 做小窗/大窗映射，
    避免把字段名硬编码成业务语义。
    """
    rate = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate, dict):
        rate = {}

    windows: list[tuple[str, int | None, dict]] = []
    for name in ("primary_window", "secondary_window"):
        raw = rate.get(name)
        block = _wham_window_block(raw)
        if not block:
            continue
        windows.append((name, block.get("limit_window_seconds"), block))

    five_hour: dict = {}
    seven_day: dict = {}
    if len(windows) >= 2:
        # 有窗口长度时，小窗口归 5h，大窗口归 7d。缺长度的排到后面；
        # 两个都缺时按 wham 当前语义 fallback：primary=5h、secondary=7d。
        if any(sec is not None for _, sec, _ in windows):
            ordered = sorted(windows, key=lambda item: item[1] if item[1] is not None else 10**18)
        else:
            ordered = windows
        five_hour = dict(ordered[0][2])
        seven_day = dict(ordered[-1][2])
    elif len(windows) == 1:
        name, sec, block = windows[0]
        # 单窗口时仍尽量按窗口长度判断；缺长度则按字段名做保守 fallback。
        if sec is not None:
            if sec <= 6 * 3600:
                five_hour = dict(block)
            else:
                seven_day = dict(block)
        elif name == "primary_window":
            five_hour = dict(block)
        else:
            seven_day = dict(block)

    # 内部辅助字段不写进通用展示路径。
    for block in (five_hour, seven_day):
        block.pop("limit_window_seconds", None)

    credits = payload.get("credits") if isinstance(payload, dict) else None
    if not isinstance(credits, dict):
        credits = {}
    credit_balance = _coerce_float(credits.get("balance"))
    extra_enabled = bool(
        credits.get("has_credits")
        or credits.get("unlimited")
        or credit_balance is not None
    )

    return {
        "five_hour": five_hour,
        "seven_day": seven_day,
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {
            "is_enabled": extra_enabled,
            "used_credits": 0,
            "monthly_limit": 0,
            "utilization": 0,
            "balance": credit_balance,
            "unlimited": bool(credits.get("unlimited")),
            "overage_limit_reached": bool(credits.get("overage_limit_reached")),
        },
        "openai": {
            "source": "wham_usage",
            "plan_type": payload.get("plan_type") if isinstance(payload, dict) else None,
            "allowed": rate.get("allowed"),
            "limit_reached": rate.get("limit_reached"),
            "rate_limit_reached_type": payload.get("rate_limit_reached_type") if isinstance(payload, dict) else None,
        },
    }


def _mock_wham_payload() -> dict:
    now = int(time.time())
    return {
        "user_id": "mock-user",
        "account_id": "mock-acct",
        "email": "mock-openai@local",
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 1,
                "limit_window_seconds": 18000,
                "reset_after_seconds": 3600,
                "reset_at": now + 3600,
            },
            "secondary_window": {
                "used_percent": 3,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 604800,
                "reset_at": now + 604800,
            },
        },
        "credits": {"has_credits": False, "unlimited": False},
    }


def fetch_wham_usage_sync(access_token: str) -> dict:
    """主动拉 ChatGPT/Codex quota。

    失败会抛异常；调用方必须把失败视为“额度未知”，不能用旧/空数据恢复
    quota disabled 账号。响应头实时 quota 仍由 failover/openai 子模块采样。
    """
    if _mock_mode_enabled():
        return normalize_wham_usage(_mock_wham_payload())
    resp = network.get_sync(
        WHAM_USAGE_URL,
        headers={
            "authorization": f"Bearer {access_token}",
            "accept": "application/json",
            "user-agent": USER_AGENT,
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/codex/settings/usage",
        },
        timeout=_WHAM_USAGE_TIMEOUT,
        proxy_purpose="oauth_openai",
    )
    resp.raise_for_status()
    return normalize_wham_usage(resp.json())


async def fetch_wham_usage(access_token: str) -> dict:
    return await asyncio.to_thread(fetch_wham_usage_sync, access_token)


# ─── id_token 解码 ───────────────────────────────────────────────

class IDTokenError(ValueError):
    pass


def decode_id_token(id_token: str, *, verify_exp: bool = False,
                    skew_seconds: int = 120) -> dict:
    """解码 id_token JWT 的 payload（**不验签**）。

    仅在 verify_exp=True 时校验 exp（允许 120s 时钟偏差）。默认不校验——
    OAuth 流程里我们立即使用它抽取 email 等字段，对 exp 不敏感。
    """
    if not id_token or id_token.count(".") < 2:
        raise IDTokenError(f"invalid JWT: got {id_token!r}")
    parts = id_token.split(".")
    if len(parts) != 3:
        raise IDTokenError(f"invalid JWT: expected 3 parts, got {len(parts)}")
    payload_b64 = parts[1]
    # 补 padding
    padding = (-len(payload_b64)) % 4
    if padding:
        payload_b64 += "=" * padding
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
    except Exception as exc:
        raise IDTokenError(f"decode base64: {exc}") from exc
    try:
        claims = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise IDTokenError(f"parse JSON: {exc}") from exc
    if verify_exp:
        exp = claims.get("exp")
        if isinstance(exp, int) and exp > 0 and time.time() > exp + skew_seconds:
            raise IDTokenError(f"id_token expired (exp={exp})")
    return claims


def extract_user_info(id_token_claims: dict) -> dict:
    """从 id_token claims 抽取账户元信息。

    返回字段：email, chatgpt_account_id, organization_id, plan_type,
    organizations（原始列表，便于后续调试）。缺失项为空字符串 / 空列表。
    """
    email = str(id_token_claims.get("email") or "")
    openai_auth = id_token_claims.get("https://api.openai.com/auth") or {}
    if not isinstance(openai_auth, dict):
        openai_auth = {}

    chatgpt_account_id = str(openai_auth.get("chatgpt_account_id") or "")
    workspace_id = str(openai_auth.get("workspace_id") or chatgpt_account_id or "")
    plan_type = str(openai_auth.get("chatgpt_plan_type") or "")
    organizations = openai_auth.get("organizations") or []
    if not isinstance(organizations, list):
        organizations = []

    organization_id = ""
    selected_org: dict | None = None
    for org in organizations:
        if isinstance(org, dict) and org.get("is_default"):
            selected_org = org
            organization_id = str(org.get("id") or "")
            break
    if not organization_id and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            selected_org = first
            organization_id = str(first.get("id") or "")

    workspace_name = ""
    if selected_org:
        workspace_name = str(
            selected_org.get("workspace_name")
            or selected_org.get("name")
            or selected_org.get("title")
            or ""
        )
    workspace_type = _infer_workspace_type(plan_type=plan_type, org=selected_org)

    return {
        "email": email,
        "chatgpt_account_id": chatgpt_account_id,
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_type": workspace_type,
        "organization_id": organization_id,
        "plan_type": plan_type,
        "organizations": organizations,
    }


# ─── 响应头解析：Codex rate limit ─────────────────────────────────

def _parse_float(headers: dict, key: str) -> float | None:
    v = headers.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_int(headers: dict, key: str) -> int | None:
    v = headers.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_rate_limit_headers(headers: Any) -> dict | None:
    """从 `chatgpt.com/backend-api/codex/responses` 的响应头抽 codex 用量。

    接受 dict 或 httpx.Headers（用 dict(headers) 扁平化）。无任何字段时
    返回 None；有任何一个字段就返回完整 snapshot dict，字段含义：
      primary_used_pct / primary_reset_sec / primary_window_min
      secondary_used_pct / secondary_reset_sec / secondary_window_min
      primary_over_secondary_pct
    这些原样落库到 oauth_quota_cache，同时调 Normalize 映射到 5h/7d。
    """
    if headers is None:
        return None
    # 统一成小写 key 的普通 dict，避免大小写/类型混乱。
    if hasattr(headers, "items"):
        flat = {str(k).lower(): v for k, v in headers.items()}
    else:
        return None

    snap = {
        "primary_used_pct":          _parse_float(flat, "x-codex-primary-used-percent"),
        "primary_reset_sec":         _parse_int(flat,   "x-codex-primary-reset-after-seconds"),
        "primary_window_min":        _parse_int(flat,   "x-codex-primary-window-minutes"),
        "secondary_used_pct":        _parse_float(flat, "x-codex-secondary-used-percent"),
        "secondary_reset_sec":       _parse_int(flat,   "x-codex-secondary-reset-after-seconds"),
        "secondary_window_min":      _parse_int(flat,   "x-codex-secondary-window-minutes"),
        "primary_over_secondary_pct": _parse_float(
            flat, "x-codex-primary-over-secondary-limit-percent"
        ),
    }
    if all(v is None for v in snap.values()):
        return None
    snap["fetched_at"] = int(time.time() * 1000)
    return snap


def normalize_codex_snapshot(snap: dict) -> dict:
    """把 primary/secondary 映射到 5h/7d。参考 sub2api `Normalize()`。

    策略：有 window_minutes 时，较小窗口归为 5h、较大归为 7d；只有一边
    window_minutes 时按 ≤360 min 判 5h。两边都缺 → 回落把 primary 当 7d。
    返回 {"five_hour_util", "five_hour_reset_sec", "seven_day_util", ...}
    （沿用现有 `oauth_quota_cache.five_hour_*` 列名，避免多套展示逻辑）。
    """
    p_win = snap.get("primary_window_min")
    s_win = snap.get("secondary_window_min")

    use_5h_from_primary = False
    if p_win is not None and s_win is not None:
        if p_win < s_win:
            use_5h_from_primary = True
        else:
            pass
    elif p_win is not None:
        if p_win <= 360:
            use_5h_from_primary = True
        else:
            pass
    elif s_win is not None:
        if s_win <= 360:
            pass   # secondary 是 5h → primary 侧归 7d
        else:
            use_5h_from_primary = True
    else:
        pass        # 两边都没有 window_min：回落

    if use_5h_from_primary:
        five_util, five_reset = snap.get("primary_used_pct"), snap.get("primary_reset_sec")
        seven_util, seven_reset = snap.get("secondary_used_pct"), snap.get("secondary_reset_sec")
    else:
        five_util, five_reset = snap.get("secondary_used_pct"), snap.get("secondary_reset_sec")
        seven_util, seven_reset = snap.get("primary_used_pct"), snap.get("primary_reset_sec")

    return {
        "five_hour_util": five_util,
        "five_hour_reset_sec": five_reset,
        "seven_day_util": seven_util,
        "seven_day_reset_sec": seven_reset,
    }
