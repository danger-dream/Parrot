"""Release matrix for OAuth account route propagation and safe compatibility."""
from __future__ import annotations

import asyncio
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

from src import config, oauth_manager as om, oauth_model_discovery
from src.oauth import antigravity, openai, xai


class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


@pytest.fixture
def accounts():
    before = copy.deepcopy(config.get())
    entries = [
        {"provider": "openai", "email": "a@x", "workspace_id": "ws-a", "access_token": "ta", "refresh_token": "ra", "expired": "2999-01-01T00:00:00Z"},
        {"provider": "xai", "email": "x@x", "subject": "sub-x", "access_token": "tx", "refresh_token": "rx", "expired": "2999-01-01T00:00:00Z", "base_url": "https://api.x.ai/v1"},
        {"provider": "antigravity", "email": "g@x", "project_id": "proj-g", "access_token": "tg", "refresh_token": "rg", "expired": "2999-01-01T00:00:00Z"},
        {"provider": "claude", "email": "c@x", "access_token": "tc", "refresh_token": "rc", "expired": "2999-01-01T00:00:00Z"},
        {"provider": "cursor", "email": "u@x", "subject": "sub-u", "access_token": "tu", "refresh_token": "ru", "expired": "2999-01-01T00:00:00Z"},
    ]
    config.update(lambda cfg: cfg.__setitem__("oauthAccounts", copy.deepcopy(entries)))
    keys = {entry["provider"]: om._canonical_key(entry) for entry in entries}
    yield keys
    config.update(lambda cfg: (cfg.clear(), cfg.update(copy.deepcopy(before))))


@pytest.mark.asyncio
async def test_all_provider_model_public_entry_routes_canonical_context(accounts, monkeypatch):
    calls = []

    def get_sync(url, **kwargs):
        calls.append((url, kwargs["proxy_purpose"], kwargs["proxy_channel"]))
        if "codex/models" in url:
            return Response({"models": [{"slug": "gpt-x", "visibility": "list"}]})
        if "anthropic.com/v1/models" in url:
            return Response({"data": [{"id": "claude-x"}], "has_more": False})
        if url.endswith("/language-models"):
            return Response({"data": [{"id": "grok-x"}]})
        if url.endswith("/models"):
            return Response({"data": [{"id": "grok-x", "context_length": 1}]})
        raise AssertionError(url)

    def post_sync(url, **kwargs):
        calls.append((url, kwargs["proxy_purpose"], kwargs["proxy_channel"]))
        return Response({"models": {"gemini-x": {}}})

    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get_sync)
    monkeypatch.setattr(oauth_model_discovery.network, "post_sync", post_sync)
    cursor_context = []
    monkeypatch.setattr(
        om.cursor_provider, "fetch_model_catalog_sync",
        lambda token, *, timeout=None, account_key="": cursor_context.append((token, account_key)) or {"models": [{"id": "cursor-x"}]},
    )
    monkeypatch.setattr(
        om.cursor_provider, "fetch_profile_sync",
        lambda token, *, account_key="", timeout=None: {"email": "u@x", "subject": "sub-u"},
    )

    for provider, key in accounts.items():
        result = await om.refresh_account_models(key, timeout_s=5)
        assert result["action"] == "updated", (provider, result)

    expected = {
        "openai": "oauth_openai", "xai": "oauth_xai",
        "antigravity": "oauth_antigravity", "claude": "oauth_anthropic",
    }
    for provider, purpose in expected.items():
        key = accounts[provider]
        matching = [call for call in calls if call[2] == f"oauth:{key}"]
        assert matching and all(call[1] == purpose for call in matching)
    assert cursor_context == [("tu", accounts["cursor"])]


@pytest.mark.asyncio
async def test_two_openai_accounts_interleaved_usage_context_does_not_cross(accounts, monkeypatch):
    second = {"provider": "openai", "email": "b@x", "workspace_id": "ws-b", "access_token": "tb", "refresh_token": "rb", "expired": "2999-01-01T00:00:00Z"}
    config.update(lambda cfg: cfg["oauthAccounts"].append(second))
    second_key = om._canonical_key(second)
    seen = []

    async def usage(token, *, account_id=None, account_key=""):
        await asyncio.sleep(0 if token == "tb" else .01)
        seen.append((token, account_id, account_key))
        return {"token": token}

    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", usage)
    results = await asyncio.gather(
        om.fetch_usage(accounts["openai"]), om.fetch_usage(second_key),
    )
    assert {item["token"] for item in results} == {"ta", "tb"}
    assert set(seen) == {
        ("ta", "ws-a", accounts["openai"]),
        ("tb", "ws-b", second_key),
    }


@pytest.mark.asyncio
async def test_manager_legacy_signature_once_and_body_typeerror_once(accounts, monkeypatch):
    legacy_calls = []
    async def legacy(token, *, account_id=None):
        legacy_calls.append((token, account_id))
        return {"ok": True}
    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", legacy)
    assert await om.fetch_usage(accounts["openai"]) == {"ok": True}
    assert legacy_calls == [("ta", "ws-a")]

    body_calls = []
    async def broken(token, *, account_id=None, account_key=""):
        body_calls.append((token, account_id, account_key))
        raise TypeError("unexpected keyword argument 'account_key' from business logic")
    monkeypatch.setattr(om.openai_provider, "fetch_wham_usage", broken)
    with pytest.raises(TypeError, match="business logic"):
        await om.fetch_usage(accounts["openai"])
    assert body_calls == [("ta", "ws-a", accounts["openai"])]


def test_pre_account_token_exchanges_never_invent_account_channel(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((kwargs["proxy_purpose"], kwargs.get("proxy_channel", "")))
        return Response({"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(openai.network, "post_sync", post)
    monkeypatch.setattr(xai.network, "post_sync", post)
    monkeypatch.setattr(antigravity.network, "post_sync", post)
    monkeypatch.setattr(openai, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(xai, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(antigravity, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(openai, "enrich_token_response_sync", lambda data, **_kwargs: data)
    openai.exchange_code_sync("code", "verifier")
    xai.exchange_code_sync("code", "verifier", redirect_uri="http://localhost")
    antigravity.exchange_code_sync("code", redirect_uri="http://localhost")
    assert calls == [
        ("oauth_openai", ""), ("oauth_xai", ""), ("oauth_antigravity", ""),
    ]
