"""OpenAI (ChatGPT / Codex CLI) OAuth provider。

对应 sub2api (Wei-Shaw/sub2api) 里的 Codex CLI OAuth 流程：
  - PKCE: code_verifier 是 **hex(64 随机字节)**，与 Anthropic 侧 base64url 不同
  - Authorize URL 必带 `id_token_add_organizations=true` +
    `codex_cli_simplified_flow=true`
  - Token 端点用 **form-urlencoded**（不是 JSON）
  - refresh 时 scope 不含 `offline_access`
  - email / chatgpt_account_id / organizations 从 id_token payload 解码拿到
    （**不验 JWT 签名**，仅校验 exp，与 sub2api 对齐）

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

import httpx


# ─── OAuth 常量（与 sub2api 对齐，来源 Codex CLI 官方）────────────

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"

SCOPES_AUTHORIZE = "openid profile email offline_access"
# 刷新时 scope 不能带 offline_access（sub2api 经验，带了会被拒）
SCOPES_REFRESH = "openid profile email"

# 固定的 Codex CLI User-Agent。未来若观察到被针对可加开关。
USER_AGENT = "codex_cli_rs/0.104.0"

# 运行期请求超时（换 token / 刷 token）。
_TOKEN_HTTP_TIMEOUT = 15.0


# ─── mock 开关（与 oauth_manager.mock_mode_enabled 同语义） ────────

def _mock_mode_enabled() -> bool:
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1":
        return True
    from .. import config  # 延迟导入，避免循环依赖
    return bool(config.get().get("oauth", {}).get("mockMode", False))


def _mock_id_token(email: str | None = None) -> str:
    """为 mockMode 构造一个合法结构的 id_token（3 段 base64）。

    payload 包含 sub2api 需要的所有字段，签名部分留空（我们本来也不验签）。
    """
    header = {"alg": "none", "typ": "JWT"}
    if not email:
        email = f"mock-openai-{secrets.token_hex(4)}@local"
    payload = {
        "sub": f"user-{secrets.token_hex(4)}",
        "email": email,
        "email_verified": True,
        "iss": "https://auth.openai.com",
        "aud": [CLIENT_ID],
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "https://api.openai.com/auth": {
            "chatgpt_account_id": f"mock-acct-{secrets.token_hex(4)}",
            "chatgpt_user_id": f"mock-user-{secrets.token_hex(4)}",
            "chatgpt_plan_type": "plus",
            "user_id": f"user-{secrets.token_hex(4)}",
            "poid": "org-mock",
            "organizations": [
                {"id": "org-mock", "role": "owner", "title": "Mock Org",
                 "is_default": True},
            ],
        },
    }

    def _b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_b64(header)}.{_b64(payload)}."


def _mock_token_response(email: str | None = None) -> dict:
    return {
        "access_token": "mock-openai-access-" + secrets.token_hex(8),
        "refresh_token": "mock-openai-refresh-" + secrets.token_hex(8),
        "id_token": _mock_id_token(email),
        "token_type": "Bearer",
        "expires_in": 28800,
        "scope": SCOPES_AUTHORIZE,
    }


# ─── PKCE ────────────────────────────────────────────────────────

def pkce_generate() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。

    OpenAI 特殊：verifier 必须是 hex（64 字节随机 → 128 字符 hex）；
    challenge 仍是 base64url(sha256(verifier)) 无 padding。
    """
    verifier = secrets.token_bytes(64).hex()
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
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ─── code → token ─────────────────────────────────────────────────

def _post_token_form(data: dict) -> dict:
    """同步 POST form-urlencoded 到 token 端点。"""
    resp = httpx.post(
        TOKEN_URL,
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": USER_AGENT,
        },
        timeout=_TOKEN_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def exchange_code_sync(code: str, code_verifier: str,
                       *, redirect_uri: str | None = None) -> dict:
    """同步版：换 token。mockMode 下返回假 token 响应。"""
    if _mock_mode_enabled():
        return _mock_token_response()
    return _post_token_form({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri or REDIRECT_URI,
        "code_verifier": code_verifier,
    })


async def exchange_code(code: str, code_verifier: str,
                        *, redirect_uri: str | None = None) -> dict:
    return await asyncio.to_thread(
        exchange_code_sync, code, code_verifier, redirect_uri=redirect_uri
    )


def refresh_sync(refresh_token: str, *, email: str | None = None) -> dict:
    """同步版：用 refresh_token 换新 access_token。

    email 仅用于 mockMode 伪造 id_token 时保持 email 一致，真实 HTTP 不用。
    """
    if _mock_mode_enabled():
        return _mock_token_response(email)
    return _post_token_form({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": SCOPES_REFRESH,
    })


async def refresh(refresh_token: str, *, email: str | None = None) -> dict:
    return await asyncio.to_thread(refresh_sync, refresh_token, email=email)


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
    plan_type = str(openai_auth.get("chatgpt_plan_type") or "")
    organizations = openai_auth.get("organizations") or []
    if not isinstance(organizations, list):
        organizations = []

    organization_id = ""
    for org in organizations:
        if isinstance(org, dict) and org.get("is_default"):
            organization_id = str(org.get("id") or "")
            break
    if not organization_id and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            organization_id = str(first.get("id") or "")

    return {
        "email": email,
        "chatgpt_account_id": chatgpt_account_id,
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
    use_7d_from_primary = False
    if p_win is not None and s_win is not None:
        if p_win < s_win:
            use_5h_from_primary = True
        else:
            use_7d_from_primary = True
    elif p_win is not None:
        if p_win <= 360:
            use_5h_from_primary = True
        else:
            use_7d_from_primary = True
    elif s_win is not None:
        if s_win <= 360:
            use_7d_from_primary = True   # secondary 是 5h → primary 侧归 7d
        else:
            use_5h_from_primary = True
    else:
        use_7d_from_primary = True        # 两边都没有 window_min：回落

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
