"""Cursor upstream rejections are authentication failures, not unknown errors."""
from __future__ import annotations

import asyncio

import pytest
from src.tests.test_oauth_refresh_observation import add, install_refresh, isolated  # noqa: F401  (fixture)
from src import oauth_errors, oauth_manager as om, oauth_refresh_failure
from src.cursor_bridge.errors import CursorAuthError, classify_cursor_failure


def _refresh_rejection(status):
    error = RuntimeError("Cursor token refresh HTTP failure")  # shape raised by refresh_cursor_token
    error.response_status = status
    return error


@pytest.mark.parametrize("exc,operation", [
    (_refresh_rejection(401), "refresh_token"),
    (CursorAuthError("usage endpoint unauthorized (401)"), "fetch_usage"),
    (CursorAuthError("token refresh failed: rejected"), "refresh_token"),
])
def test_cursor_401_is_an_authentication_failure(exc, operation):
    err = oauth_errors.describe_oauth_error(exc, provider="cursor", operation=operation)
    assert err.auth_error is True and err.status == 401
    assert err.title == "Cursor 登录已失效" and "重新登录" in err.action


def test_cursor_other_statuses_keep_their_generic_meaning():
    forbidden = classify_cursor_failure("Cursor 账号资料未授权 (403)", http_status=403)
    forbidden.response_status = 403
    assert oauth_errors.describe_oauth_error(forbidden, provider="cursor", operation="fetch_usage").auth_error is False
    limited = oauth_errors.describe_oauth_error(_refresh_rejection(429), provider="cursor", operation="refresh_token")
    assert limited.auth_error is False and limited.retryable is True


def test_rejected_cursor_refresh_pauses_account_as_auth_error(monkeypatch):
    key = add("cursor")
    def refresh(*args, **kwargs):
        raise _refresh_rejection(401)
    install_refresh(monkeypatch, "cursor", refresh)
    with pytest.raises(Exception) as caught:
        asyncio.run(om.force_refresh(key))
    error, effect = oauth_refresh_failure.apply(key, caught.value, provider="cursor")
    assert error.auth_error is True
    assert (effect.status, effect.code) == ("confirmed", "account_paused")
    account = om.get_account(key)
    assert account["enabled"] is False and account["disabled_reason"] == "auth_error"
