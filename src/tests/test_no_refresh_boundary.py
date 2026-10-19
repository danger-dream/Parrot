"""NO_REFRESH forbids credential rotation, never unrelated usage/activity work."""
from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
from fastapi import FastAPI

from src import config, oauth_manager as om
from src.management_control.oauth import OAuthControl
from src.management_control.errors import ManagementError
from src.oauth.workbuddy import actions, auth, billing, common, runtime
from src.telegram.menus.workbuddy_oauth_menu import result_text
from src.tests.test_oauth_readd_generation import clean_accounts, entry_for
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account, context
from src.tests.test_reaudit2_management_parity import _patch_successful_lifespan


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "cursor", "antigravity", "workbuddy"])
@pytest.mark.parametrize("expiry", ["", "2000-01-01T00:00:00Z"])
async def test_all_six_providers_keep_existing_tokens_even_when_expired_or_unknown(clean_accounts, monkeypatch, provider, expiry):
    entry = dict(entry_for(provider), expired=expiry)
    om.add_account(entry)
    key = om.get_account_key(entry)
    before = copy.deepcopy(om.get_account(key))
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(common.network, "sync_client", lambda **kw: pytest.fail("no refresh network"))
    monkeypatch.setattr(om, "_do_refresh_mock", lambda *a, **kw: pytest.fail("no mock token rotation either"))
    monkeypatch.setattr(auth, "refresh_sync", lambda *a, **kw: pytest.fail("no WorkBuddy rotation"))
    assert await om.ensure_valid_token(key) == "fixture-at"
    assert await om.force_refresh(key) == "fixture-at"
    assert om.get_account(key) == before


@pytest.mark.parametrize("realm,profile,domain", [("cn", "cli", "www.codebuddy.cn"), ("global", "ide", "www.codebuddy.ai")])
def test_activity_wire_uses_only_existing_access_token_and_all_refresh_layers_block(account, monkeypatch, realm, profile, domain):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    config.update(lambda c: c.update(oauth={**c.get("oauth", {}), "mockMode": False}))
    current = dict(om.get_account(account), realm=realm, domain=domain, workbuddy_client_profile=profile)
    seen = []
    def wire(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer fixture-at"
        assert "x-refresh-token" not in request.headers
        assert "auth/token/refresh" not in request.url.path
        # CN checkin acknowledges an award explicitly; an empty object is an
        # unknown result, unlike the Global trial endpoint's empty success.
        data = {"credit": 0} if realm == "cn" else {}
        return httpx.Response(200, json={"code": 0, "data": data})
    monkeypatch.setattr(common.network, "sync_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(wire)))
    action = "checkin" if realm == "cn" else "claim_trial"
    result = billing.execute_action_sync(current, action)
    assert result["status"] == "succeeded"
    if realm == "cn":
        assert result["awarded_credits"] == 0
    assert len(seen) == 1
    with pytest.raises(common.WorkBuddyError, match="disabled"):
        auth.refresh_sync("fixture-rt", account=current)
    with pytest.raises(common.WorkBuddyError, match="disabled"):
        runtime.refresh_locked(current, account, True)
    with pytest.raises(common.WorkBuddyError, match="disabled"):
        common.request(current, "/v2/plugin/auth/token/refresh", kind="refresh")
    assert len(seen) == 1


@pytest.mark.parametrize("action", ["checkin", "claim_trial"])
def test_confirmed_action_and_rejection_never_rotate_tokens(action_env, monkeypatch, action):
    key, ledger = action_env
    if action == "claim_trial":
        entry = dict(om.get_account(key), realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
        config.update(lambda c: c.update(oauthAccounts=[entry]))
        key = om.get_account_key(entry)
    config.update(lambda c: c["oauthAccounts"][0].update(expired="2000-01-01T00:00:00Z"))
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(auth, "refresh_sync", lambda *a, **kw: pytest.fail("activities cannot rotate shared tokens"))
    before = copy.deepcopy(om.get_account(key))
    calls = []
    def reject(*args, **kw):
        calls.append(1)
        raise common.WorkBuddyError("action", status=401)
    monkeypatch.setattr(billing, "execute_action_sync", reject)
    result = actions.execute(key, action, actor="fixture", free_trial_confirmed=action == "claim_trial")
    assert result["status"] == "rejected" and result["http_status"] == 401
    assert "重新登录" in result_text(result)
    assert om.get_account(key) == before
    assert actions.execute(key, action, actor="fixture", free_trial_confirmed=True)["status"] == "rejected"
    assert calls == [1]  # No auth retry and no implicit replay.


@pytest.mark.parametrize("realm", ["cn", "global"])
def test_confirmed_activity_succeeds_while_management_token_refresh_is_still_rejected(action_env, monkeypatch, realm):
    key, ledger = action_env
    if realm == "global":
        entry = dict(om.get_account(key), realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
        config.update(lambda c: c.update(oauthAccounts=[entry]))
        key = om.get_account_key(entry)
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(auth, "refresh_sync", lambda *a, **kw: pytest.fail("no refresh"))
    ctl, ctx = OAuthControl(), context()
    with pytest.raises(ManagementError) as blocked:
        ctl.refresh_token(ctx, key)
    assert blocked.value.fields[0].code == "REFRESH_DISABLED"
    action = "checkin" if realm == "cn" else "claim_trial"
    before = (om.get_account(key)["access_token"], om.get_account(key)["refresh_token"])
    plan = ctl.plan_workbuddy_action(ctx, key, action, free_trial_confirmed=realm == "global")
    assert ledger["calls"] == 0
    result = ctl.execute_workbuddy_action_now(ctx, key, plan["plan_token"])
    assert result["status"] == "succeeded" and ledger["calls"] == 1
    assert (om.get_account(key)["access_token"], om.get_account(key)["refresh_token"]) == before


@pytest.mark.parametrize("protected", [False, True])
async def test_background_loop_runs_opted_in_activity_independently_from_refresh(monkeypatch, protected):
    monkeypatch.setenv("PARROT_NO_REFRESH", "1" if protected else "0")
    calls, delays = [], []
    async def refresh():
        calls.append("refresh")
    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError()
    monkeypatch.setattr(om, "proactive_refresh_once", refresh)
    monkeypatch.setattr(actions, "auto_checkin_once", lambda: calls.append("activity"))
    monkeypatch.setattr(om.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await om.proactive_refresh_loop()
    assert calls == (["activity"] if protected else ["refresh", "activity"])
    assert delays == [30, 60]


async def test_protected_server_still_registers_quota_model_and_activity_scheduling(monkeypatch):
    import server
    events = []
    _patch_successful_lifespan(monkeypatch, events)
    expected = {"proactive_refresh_loop", "quota_monitor_loop", "oauth_model_sync_loop"}
    entered = set()
    def worker(name):
        async def run():
            entered.add(name)
            await asyncio.Event().wait()
        return run
    for name in expected:
        monkeypatch.setattr(om, name, worker(name))
    async with server.lifespan(FastAPI()):
        await asyncio.sleep(0)
        assert entered == expected
    server._background_tasks.clear()
