"""Claude usage/bootstrap production paths, with network-only fakes."""
from __future__ import annotations

import httpx
import pytest

from src import config, oauth_manager as oauth, state_db


@pytest.fixture
def account(monkeypatch):
    state_db.init()
    config.update(lambda cfg: cfg.update(oauthAccounts=[], oauth={"mockMode": True}))
    entry = {
        "provider": "claude", "email": "usage-recovery@example.test",
        "access_token": "fake-old-at", "refresh_token": "fake-rt",
        "expired": "2999-01-01T00:00:00Z", "models": ["claude-fable-5.1"],
    }
    oauth.add_account(entry)
    monkeypatch.setattr(oauth, "mock_mode_enabled", lambda: False)
    return "claude:usage-recovery@example.test", entry


def response(url, status, data):
    return httpx.Response(status, json=data, request=httpx.Request("GET", url))


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [200, 401])
async def test_usage_401_refreshes_persists_and_retries_once(account, monkeypatch, second_status):
    key, _ = account
    gets, posts, reset_gets = [], [], []
    fresh = {"five_hour": {"utilization": 1.0, "resets_at": None}}

    def get(url, **kwargs):
        if url == oauth.OAUTH_PROFILE_URL:
            return response(url, 200, {"organization": {"organization_type": "claude_pro"}})
        if url in {oauth.OAUTH_USAGE_URL + "?cedar_ember=1&skip_spend=1", oauth.OAUTH_USAGE_URL + "?at_wall=1&skip_spend=1"}:
            reset_gets.append(kwargs)
            return response(url, 200, {})
        assert url == oauth.OAUTH_USAGE_URL
        gets.append(kwargs)
        return response(url, 401 if len(gets) == 1 else second_status, fresh)

    def post(url, **kwargs):
        assert url == oauth.OAUTH_TOKEN_URL
        posts.append(kwargs)
        return response(url, 200, {"access_token": "fake-new-at", "refresh_token": "fake-new-rt",
                                   "expires_in": 3600, "scope": "user:profile user:inference user:plugins"})

    monkeypatch.setattr(oauth.network, "get_sync", get)
    monkeypatch.setattr(oauth.network, "post_sync", post)
    if second_status == 200:
        observed = await oauth.fetch_usage_snapshot(key)
        assert observed["five_hour"] == fresh["five_hour"]
        assert all(row["state"] == "not_provided" for row in observed["claude_reset_queries"].values())
        assert [call["headers"]["Authorization"] for call in reset_gets] == ["Bearer fake-new-at"] * 2
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await oauth.fetch_usage_snapshot(key)
    assert [call["headers"]["Authorization"] for call in gets] == ["Bearer fake-old-at", "Bearer fake-new-at"]
    assert len(posts) == 1
    if second_status == 401:
        assert reset_gets == []
    saved = oauth.get_account(key)
    assert saved["access_token"] == "fake-new-at"
    assert saved["refresh_token"] == "fake-new-rt"
    assert saved["scopes"].endswith("user:plugins")
    assert saved["plan_type"] == "claude_pro"
    assert saved["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500])
async def test_usage_non_401_does_not_rotate_credentials(account, monkeypatch, status):
    key, _ = account
    monkeypatch.setattr(oauth.network, "get_sync", lambda url, **kw: response(url, status, {}))
    def unexpected(*a, **k):
        raise AssertionError("non-401 must not refresh")
    monkeypatch.setattr(oauth.network, "post_sync", unexpected)
    with pytest.raises(httpx.HTTPStatusError):
        await oauth.fetch_usage(key)
    assert oauth.get_account(key)["access_token"] == "fake-old-at"


@pytest.mark.asyncio
async def test_usage_reuses_concurrently_rotated_token(account, monkeypatch):
    key, _ = account
    calls, reset_calls = [], []
    def get(url, **kwargs):
        if "?" in url:
            reset_calls.append(kwargs["headers"]["Authorization"])
            return response(url, 200, {})
        calls.append(kwargs["headers"]["Authorization"])
        if len(calls) == 1:
            oauth._save_token_fields(key, {"access_token": "fake-concurrent-at"})
            return response(url, 401, {})
        return response(url, 200, {"five_hour": {"utilization": 2}})
    async def unexpected(*a, **k):
        raise AssertionError("must reuse the already-rotated access token")
    monkeypatch.setattr(oauth.network, "get_sync", get)
    monkeypatch.setattr(oauth, "force_refresh", unexpected)
    assert (await oauth.fetch_usage(key))["five_hour"]["utilization"] == 2
    assert calls == ["Bearer fake-old-at", "Bearer fake-concurrent-at"]
    assert reset_calls == ["Bearer fake-concurrent-at"] * 2


@pytest.mark.asyncio
async def test_late_usage_401_cannot_refresh_recreated_account(account, monkeypatch):
    key, entry = account
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        oauth.delete_account(key)
        oauth.add_account({**entry, "access_token": "fake-replacement-at"})
        return response(url, 401, {})
    async def unexpected(*a, **k):
        raise AssertionError("retired generation cannot refresh replacement")
    monkeypatch.setattr(oauth.network, "get_sync", get)
    monkeypatch.setattr(oauth, "force_refresh", unexpected)
    with pytest.raises(httpx.HTTPStatusError):
        await oauth.fetch_usage(key)
    assert len(calls) == 1
    assert oauth.get_account(key)["access_token"] == "fake-replacement-at"


def test_bootstrap_oauth_identity_is_current(monkeypatch):
    calls = []
    monkeypatch.setattr(oauth, "mock_mode_enabled", lambda: False)
    def get(url, **kwargs):
        calls.append(kwargs)
        return response(url, 200, {})
    monkeypatch.setattr(oauth.network, "get_sync", get)
    oauth._bootstrap_sync("fake-bootstrap-token")
    assert calls[0]["headers"]["User-Agent"] == "claude-code/2.1.282"
    assert calls[0]["headers"]["anthropic-beta"] == "oauth-2025-04-20"
