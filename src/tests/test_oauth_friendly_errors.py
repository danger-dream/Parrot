"""OAuth user-facing error message tests.

These tests ensure Telegram/Admin UI does not leak raw httpx exception strings such
as MDN URLs, and instead shows concise Chinese guidance.
"""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import httpx

from src import oauth_errors


def _http_error(status: int, url: str, body: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    resp = httpx.Response(status, request=req, json=body or {"error": "invalid_grant"})
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{url}'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401",
        request=req,
        response=resp,
    )


def test_openai_refresh_401_is_user_friendly():
    exc = _http_error(401, "https://auth.openai.com/oauth/token")
    text = oauth_errors.format_oauth_error_html(
        exc, provider="openai", operation="refresh_token",
    )
    assert "OpenAI 授权已失效" in text
    assert "重新登录这个 OpenAI 账号" in text
    assert "openai_token_401" in text
    assert "developer.mozilla" not in text
    assert "auth.openai.com" not in text
    assert "Client error" not in text


def test_openai_code_exchange_401_is_user_friendly():
    exc = _http_error(401, "https://auth.openai.com/oauth/token")
    text = oauth_errors.format_oauth_error_html(
        exc, provider="openai", operation="exchange_code",
    )
    assert "登录 code 已失效" in text
    assert "完整 callback URL" in text
    assert "openai_code_exchange_401" in text
    assert "developer.mozilla" not in text
    assert "Client error" not in text


def test_claude_usage_403_is_user_friendly():
    exc = _http_error(403, "https://api.anthropic.com/api/oauth/usage", {"error": {"message": "forbidden"}})
    text = oauth_errors.format_oauth_error_html(
        exc, provider="claude", operation="fetch_usage",
    )
    assert "Claude 用量接口拒绝访问" in text
    assert "先刷新 Token" in text
    assert "claude_usage_403" in text
    assert "developer.mozilla" not in text
    assert "api.anthropic.com" not in text
    assert "Client error" not in text


def test_transient_errors_are_retryable_not_auth_error():
    exc = _http_error(429, "https://auth.openai.com/oauth/token", {"error": "rate_limit"})
    err = oauth_errors.describe_oauth_error(exc, provider="openai", operation="refresh_token")
    assert err.retryable is True
    assert err.auth_error is False
    assert err.code == "openai_rate_limited"


def test_claude_refresh_400_invalid_request_not_auth_error():
    exc = _http_error(400, "https://platform.claude.com/v1/oauth/token", {"error": "invalid_request"})
    err = oauth_errors.describe_oauth_error(exc, provider="claude", operation="refresh_token")
    assert err.auth_error is False
    assert err.retryable is True
    assert err.code == "claude_refresh_token_failed"


def test_claude_refresh_400_invalid_grant_is_auth_error():
    exc = _http_error(400, "https://platform.claude.com/v1/oauth/token", {"error": "invalid_grant"})
    err = oauth_errors.describe_oauth_error(exc, provider="claude", operation="refresh_token")
    assert err.auth_error is True
    assert err.code == "claude_token_invalid"
