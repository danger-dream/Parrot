"""OpenAI usage authentication recovery with isolated accounts and provider I/O."""
from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest

from src.tests.test_oauth_refresh_observation import isolated, add as add_account, install_refresh
from src import config, log_db, oauth_manager as om, oauth_errors, state_db
from src.management_control.oauth import OAuthControl
from src.telegram import ui
from src.telegram.menus import oauth_menu as menu


@pytest.fixture(autouse=True)
def no_background_ui(monkeypatch):
    for name in ["_schedule_openai_metadata_for_ui", "_schedule_oauth_detail_stats"]:
        monkeypatch.setattr(menu, name, Mock())


def add_openai():
    return add_account("openai", name="usage-" + uuid.uuid4().hex)


def http_error(status=401, *, token=False, code="invalid_token"):
    request = httpx.Request("POST" if token else "GET",
                            "https://auth.openai.com/oauth/token" if token else
                            "https://chatgpt.com/backend-api/wham/usage")
    response = httpx.Response(status, request=request, json={"error": {"code": code}})
    return httpx.HTTPStatusError("upstream rejected credentials", request=request, response=response)


def usage(plan="plus"):
    return om.openai_provider.normalize_wham_usage({
        "plan_type": plan,
        "rate_limit": {"allowed": True, "limit_reached": False,
                       "primary_window": {"used_percent": 12, "limit_window_seconds": 18000}},
        "credits": {"has_credits": True, "balance": "37589"},
        "rate_limit_reset_credits": {"available_count": 0},
    })


def rotation(monkeypatch, **kwargs):
    refresh = Mock(**kwargs) if kwargs else Mock(return_value={
        "access_token": "fresh-at", "refresh_token": "fresh-rt", "expires_in": 3600,
    })
    install_refresh(monkeypatch, "openai", refresh)
    return refresh


@pytest.mark.parametrize("path", ["direct", "manual", "progress", "access", "monitor", "startup"])
def test_usage_401_recovers_once_through_shared_paths(monkeypatch, path):
    key = add_openai()
    refresh = rotation(monkeypatch)
    calls = []

    async def fetch(token, **kwargs):
        calls.append((token, kwargs))
        if token == "old-at":
            raise http_error()
        return usage()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    if path == "direct":
        result = asyncio.run(om.fetch_usage(key))
        state_db.quota_save(key, om.flatten_usage(result))
    elif path in {"manual", "progress"}:
        ctl = OAuthControl()
        from src.tests.test_workbuddy_lifecycle import context
        result = ctl.refresh_usage_now(context(), key,
                                       on_stage=(lambda *a: None) if path == "progress" else None)
        assert "error" not in result
    elif path == "access":
        monkeypatch.setattr(om, "_should_skip_access_refresh", lambda: False)
        assert asyncio.run(om.ensure_quota_fresh(key))
    elif path == "monitor":
        asyncio.run(om.quota_monitor_once())
    else:
        assert asyncio.run(om.preload_openai_reset_credit_details_once())[key] == "refreshed"
    assert [call[0] for call in calls] == ["old-at", "fresh-at"]
    assert all(call[1]["account_id"] == om.get_account(key)["workspace_id"]
               and call[1]["account_key"] == key for call in calls)
    refresh.assert_called_once()
    assert om.get_account(key)["enabled"] is True
    assert om.get_account(key).get("disabled_reason") is None
    assert om.usage_from_quota_row(state_db.quota_load(key))["openai"]["credits"]["balance"] == "37589"


@pytest.mark.parametrize("failure", ["retry_401", "refresh_invalid_grant", "expired_invalid_grant"])
def test_unrecoverable_authentication_pauses_and_preserves_cached_usage(monkeypatch, failure):
    key = add_openai()
    state_db.quota_save(key, om.flatten_usage(usage()))
    old = state_db.quota_load(key)
    error = http_error(400, token=True, code="invalid_grant")
    refresh = rotation(monkeypatch, side_effect=error) if failure != "retry_401" else rotation(monkeypatch)
    if failure == "expired_invalid_grant":
        config.update(lambda c: c["oauthAccounts"][0].update(
            expired=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))
    calls = []

    async def fetch(*args, **kwargs):
        calls.append(args[0])
        raise http_error()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(om.fetch_usage(key))
    assert len(calls) == {"retry_401": 2, "refresh_invalid_grant": 1, "expired_invalid_grant": 0}[failure]
    refresh.assert_called_once()
    assert om.get_account(key)["enabled"] is False
    assert om.get_account(key)["disabled_reason"] == "auth_error"
    row = state_db.quota_load(key)
    assert row["raw_data"] == old["raw_data"] and row["fetched_at"] == old["fetched_at"]
    assert row["codex_usage_failed_at"] is not None
    assert "重新登录" in str(menu._detail_text_and_kb(
        key, actor_chat_id=42, model_stats=[], stats_loading=False)[1])


@pytest.mark.parametrize("status", [400, 403, 429, 500, 502, 503])
def test_non_401_usage_error_neither_rotates_nor_disables(monkeypatch, status):
    key = add_openai()
    refresh = rotation(monkeypatch)

    async def fetch(*args, **kwargs):
        raise http_error(status, code="permission_denied" if status == 403 else "upstream_error")

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(om.fetch_usage(key))
    refresh.assert_not_called()
    assert om.get_account(key)["enabled"] is True
    assert om.get_account(key).get("disabled_reason") is None


@pytest.mark.parametrize("during_refresh", [False, True])
def test_transient_network_failure_does_not_mark_login_invalid(monkeypatch, during_refresh):
    key = add_openai()
    error = httpx.ReadTimeout("temporary timeout")
    refresh = rotation(monkeypatch, side_effect=error)

    async def fetch(*args, **kwargs):
        raise http_error() if during_refresh else error

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(om.fetch_usage(key))
    assert refresh.call_count == int(during_refresh)
    assert om.get_account(key)["enabled"] is True
    assert om.get_account(key).get("disabled_reason") is None


@pytest.mark.parametrize("change", ["login", "user", "delete_recreate"])
def test_late_second_401_cannot_pause_new_credentials_or_override_user(monkeypatch, change):
    key = add_openai()
    refresh = rotation(monkeypatch)
    calls = []
    preserved = []

    async def fetch(token, **kwargs):
        calls.append(token)
        if len(calls) == 2:
            if change == "login":
                config.update(lambda c: c["oauthAccounts"][0].update(access_token="login-at", refresh_token="login-rt"))
            elif change == "user":
                om.set_enabled(key, False, reason="user")
            else:
                previous = copy.deepcopy(om.get_account(key))
                om.delete_account(key)
                om.add_account(previous)
            preserved.append(copy.deepcopy(om.get_account(key)))
        raise http_error()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    expected = httpx.HTTPStatusError if change == "user" else oauth_errors.OAuthRefreshStateError
    with pytest.raises(expected) as caught:
        asyncio.run(om.fetch_usage(key))
    if change != "user":
        described = oauth_errors.describe_oauth_error(caught.value, provider="openai", operation="fetch_usage")
        assert not described.auth_error and "凭据已更新" in described.title
    assert calls == ["old-at", "fresh-at"]
    refresh.assert_called_once()
    assert om.get_account(key) == preserved[0]


def test_concurrent_usage_401s_share_one_credential_rotation(monkeypatch):
    key = add_openai()
    refresh = rotation(monkeypatch)
    gate = threading.Barrier(4)
    calls = []

    async def fetch(token, **kwargs):
        calls.append(token)
        if token == "old-at":
            gate.wait(timeout=5)
            raise http_error()
        return usage()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: asyncio.run(om.fetch_usage(key)), range(4)))
    assert all(result["openai"]["plan_type"] == "plus" for result in results)
    assert calls.count("old-at") == 4 and calls.count("fresh-at") == 4
    refresh.assert_called_once()
    assert om.get_account(key)["enabled"] is True


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("failure", ["retry_401", "invalid_grant"])
def test_refresh_callback_redraws_failure_and_relogin_even_without_cache(monkeypatch, cached, failure):
    key = add_openai()
    if cached:
        state_db.quota_save(key, om.flatten_usage(usage()))
    if failure == "invalid_grant":
        rotation(monkeypatch, side_effect=http_error(400, token=True, code="invalid_grant"))
    else:
        rotation(monkeypatch)
    log_db.init()
    monkeypatch.setattr(menu, "oauth_control", OAuthControl())
    for name in ["send", "edit", "answer_cb"]:
        monkeypatch.setattr(ui, name, Mock())
    for name in ["_schedule_openai_metadata_for_ui", "_schedule_oauth_detail_stats"]:
        monkeypatch.setattr(menu, name, Mock())

    async def fetch(*args, **kwargs):
        raise http_error()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    menu.on_refresh_usage(42, 100, "cb", ui.register_code(key), page=3, filter_key="invalid")
    assert ui.answer_cb.called
    assert ui.edit.called
    text = ui.edit.call_args.args[2]
    assert ("OpenAI 登录状态已失效" if failure == "retry_401" else "OpenAI 授权已失效") in text
    assert "用量刷新失败" in text and "重新登录这个 OpenAI 账号" in text
    assert ("保留上次数据" in text) is cached
    assert "🔐 重新登录" in str(ui.edit.call_args.kwargs["reply_markup"])
    assert ("openai_usage_401" if failure == "retry_401" else "openai_token_invalid") in text
    if failure == "retry_401":
        assert "refresh_token" not in text
    ui.send.assert_not_called()


@pytest.mark.parametrize("reason", ["quota", "user", "auth_error"])
def test_usage_auth_recovery_respects_pause_reason(monkeypatch, reason):
    key = add_openai()
    om.set_enabled(key, False, reason=reason)
    refresh = rotation(monkeypatch)
    calls = []

    async def fetch(token, **kwargs):
        calls.append(token)
        if token == "old-at":
            raise http_error()
        return usage()

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    if reason == "quota":
        result = asyncio.run(om.fetch_usage(key))
        assert result["openai"]["plan_type"] == "plus"
        assert calls == ["old-at", "fresh-at"]
        refresh.assert_called_once()
    else:
        with pytest.raises(oauth_errors.OAuthRefreshStateError):
            asyncio.run(om.fetch_usage(key))
        refresh.assert_not_called()
    assert om.get_account(key)["disabled_reason"] == reason
    assert om.get_account(key)["enabled"] is False


def test_free_plan_is_not_authentication_failure(monkeypatch):
    key = add_openai()
    refresh = rotation(monkeypatch)

    async def fetch(*args, **kwargs):
        return usage("free")

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", fetch)
    assert asyncio.run(om.fetch_usage(key))["openai"]["plan_type"] == "free"
    refresh.assert_not_called()
    assert om.get_account(key)["enabled"] is True
    assert om.get_account(key).get("disabled_reason") is None


def test_usage_401_error_describes_usage_credentials_not_refresh_token():
    error = oauth_errors.describe_oauth_error(http_error(), provider="openai", operation="fetch_usage")
    assert error.auth_error and error.status == 401 and error.code == "openai_usage_401"
    assert "用量接口" in error.reason and "refresh_token" not in error.reason
