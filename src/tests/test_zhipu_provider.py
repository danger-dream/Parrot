"""Coding Plan credential/lifecycle contracts, using isolated fake data only."""
from __future__ import annotations
import asyncio
import copy
import json
import time
import uuid

import pytest
from src import config, oauth_manager as om, state_db
from src.oauth import normalize_provider
from src.oauth.zhipu import auth, common, runtime
from src.oauth_ids import account_key
from src.channel import registry
from src.providers import registry as providers


def credential(mode="api_key", site="bigmodel", **values):
    return auth.normalize_credential({"provider": "zhipu", "credential_mode": mode, "site": site,
        "model_key": "fixture.secret", **({"subject": "fixture-user", "access_token": "fixture-biz", "zcode_token": "fixture-platform", "entitlement": "available"} if mode == "oauth" else {}), **values})


@pytest.fixture
def account_env(monkeypatch):
    from src.telegram.menus import zhipu_oauth_menu as zh
    # Most tests execute business work deterministically; dedicated dispatch
    # tests restore the real executor and assert non-blocking behavior.
    monkeypatch.setattr(zh, "_submit_remote", lambda run: run())
    state_db.init()
    before = copy.deepcopy(config.get())
    config.update(lambda c: c.update(oauthAccounts=[], channels=[]))
    yield
    config.update(lambda c: (c.clear(), c.update(before)))


@pytest.mark.parametrize("mode", ["api_key", "oauth"])
@pytest.mark.parametrize("site", ["bigmodel", "zai"])
async def test_real_account_registry_modes_scope_no_fake_email(account_env, mode, site):
    entry = credential(mode, site)
    om.add_account(entry)
    key = account_key(entry)
    saved = om.get_account(key)
    assert saved and not saved.get("refresh_token") and not saved.get("email")
    assert await om.ensure_valid_token(key) == entry["model_key"]
    for org, project in (("a:b", "c"), ("a", "b:c")):
        team = credential(mode, site, plan_scope="team", organization_id=org, project_id=project)
        om.add_account(team)
    assert len(om.list_accounts()) == 3
    registry.rebuild_from_config()
    assert normalize_provider("zhipu") == "zhipu"
    from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
    channel = ZhipuOAuthChannel(saved)
    assert providers.adapter_for_channel(channel).name == "zhipu-oauth"
    assert not channel.cc_mimicry and channel.protocol == "anthropic"


def test_mode_must_be_explicit():
    with pytest.raises(ValueError, match="credential_mode"):
        auth.normalize_credential({"site": "zai", "model_key": "fixture"})


@pytest.mark.parametrize("has,status,grant,expected", [(True,"EFFECTIVE","VALID","available"),
    (True,"EFFECTIVE","UNASSIGNED","unassigned"),(True,"EXPIRED","VALID","expired"),
    (False,"EFFECTIVE","VALID","unavailable"),(None,"EFFECTIVE","VALID","unknown"),
    (True,"FUTURE","VALID","unknown"),(True,"EFFECTIVE","FUTURE","unknown")])
def test_team_entitlement_enums_no_local_expiry(monkeypatch, has, status, grant, expected):
    monkeypatch.setattr(common, "request", lambda *a, **k: {"hasSubscription": has, "status": status, "memberGrantStatus": grant, "subscribeEndTime": 1})
    assert auth.entitlement(credential("oauth", plan_scope="team", organization_id="org", project_id="project")) == expected


@pytest.mark.parametrize("site,prefix", [("bigmodel", ""), ("zai", "Bearer ")])
def test_model_key_read_only_confirmed_create_wire(monkeypatch, site, prefix):
    calls = []
    def wire(url, **kw):
        calls.append((url, kw))
        assert kw["headers"]["Authorization"] == prefix + "fixture-biz"
        if "/copy/" in url:
            assert url.endswith("/copy/id%2Fencoded")
            return {"secretKey": "secret"}
        if kw.get("method") == "POST":
            return {"apiKey": "id/encoded", "name": "zcode-api-key"}
        return []
    monkeypatch.setattr(common, "request", wire)
    a = credential("oauth", site, organization_id="o", project_id="p")
    with pytest.raises(common.ZhipuError, match="creation_confirmation_required"):
        auth.resolve_model_key(a)
    assert all(kw.get("method", "GET") == "GET" for _, kw in calls)
    assert auth.resolve_model_key(a, create=True) == "id/encoded.secret"
    assert len([kw for _, kw in calls if kw.get("method") == "POST"]) == 1


async def test_oauth_expiry_preserves_model_key(account_env, monkeypatch):
    a = credential("oauth", expired="2000-01-01T00:00:00Z")
    om.add_account(a)
    key = account_key(a)
    assert await om.ensure_valid_token(key) == "fixture.secret"
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(auth, "resolve_model_key", lambda *a, **k: (_ for _ in ()).throw(common.ZhipuError("management", status=401)))
    monkeypatch.setattr(auth, "entitlement", lambda *a, **k: pytest.fail("failed Key authentication must not proceed to enrichment"))
    with pytest.raises(common.ZhipuError):
        await om.force_refresh(key)
    assert om.get_account(key)["model_key"] == "fixture.secret"
    assert om.get_account(key)["enabled"] is True
    assert om.get_account(key)["management_status"] == "relogin_required"
    assert await om.ensure_valid_token(key) == "fixture.secret"


def quota(*rows):
    return {"limits": list(rows)}


def window(pct, *, week=False):
    return {"type": "TOKENS_LIMIT", "unit": 6 if week else 3, "number": 1 if week else 5,
            "percentage": pct, "usage": 999, "currentValue": 123, "nextResetTime": (time.time()+3600)*1000}


async def test_quota_monitor_missing_windows_and_stale_cas(account_env, monkeypatch):
    a = credential()
    om.add_account(a)
    key = account_key(a)
    data = [quota(window(100), window(100, week=True))]
    monkeypatch.setattr(common, "request", lambda *a, **k: data[0])
    usage = await om.fetch_usage(key)
    assert "thirty_day" not in usage and usage["zhipu"]["limits"][0]["usage"] == 999
    assert om.evaluate_and_toggle_by_usage(key, usage)["action"] == "disabled"
    assert om.get_account(key)["quota_observation"]["windows"] == ["five_hour", "seven_day"]
    data[0] = quota(window(0))
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "noop_unknown"
    data[0] = quota(window(0), window(0, week=True))
    recovery = await om.fetch_usage(key)
    om.set_disabled_by_quota(key, None)
    assert om.evaluate_and_toggle_by_usage(key, recovery)["action"] == "noop_stale"
    assert not om.get_account(key)["enabled"]
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "resumed"
    data[0] = quota({"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 100})
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "noop_unknown"
    assert om.get_account(key)["enabled"]


async def test_catalog_sync_key_mode_no_subscription_invention(account_env, monkeypatch):
    from src.oauth.zhipu import catalog
    a = credential()
    om.add_account(a)
    key = account_key(a)
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(catalog, "fetch_models", lambda *a, **k: [catalog.model_record("GLM-5.3")])
    result = await om.refresh_account_models(key)
    assert result["action"] in {"updated", "synced", "refreshed"}, result
    assert om.account_model_selection(key)["effective_models"] == ["GLM-5.3"]
    om.set_account_model_disabled(key, "GLM-5.3", True)
    assert om.account_model_selection(key)["effective_models"] == []
    oauth = credential("oauth", entitlement="unassigned", models=["GLM-5.3"])
    om.add_account(oauth)
    assert om.account_model_selection(account_key(oauth))["effective_models"] == []
