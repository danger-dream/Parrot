"""A successful partial dashboard fetch must not prove that a missing pool recovered."""
from __future__ import annotations

import httpx
import pytest

from src import cooldown, oauth_manager, probe, scheduler, state_db
from src.channel import registry
from src.channel.cursor_oauth_channel import CursorOAuthChannel
from src.cursor_bridge.usage import fetch_cursor_usage
from src.oauth import cursor as cursor_provider
from src.telegram.menus import oauth_menu
from src.tests.test_cursor_quota_pause_display import env, pause, ACCOUNT, CHANNEL, FABLE, COMPOSER


def _route(model):
    return scheduler.schedule(
        {"model":model,"messages":[{"role":"user","content":"ping"}]},
        api_key_name="",client_ip="127.0.0.1",ingress_protocol="chat",
    )


def _install_channel(account, monkeypatch):
    channel = CursorOAuthChannel(account)
    monkeypatch.setattr(registry,"_channels",{channel.key:channel})


@pytest.mark.parametrize("period_status,period_body", [
    (500, {"error":"temporary upstream failure"}),
    (200, {"planUsage":{"autoPercentUsed":0.01}}),
])
def test_missing_pool_usage_keeps_real_scheduler_guard(env, monkeypatch, period_status, period_body):
    account, usage, deadline = env
    _install_channel(account, monkeypatch)
    pause(usage)
    before = state_db.error_load(CHANNEL,FABLE)
    assert not _route(FABLE).candidates and _route(COMPOSER).candidates

    def transport(request):
        if request.url.path.endswith("GetCurrentPeriodUsage"):
            return httpx.Response(period_status,json=period_body)
        if request.url.path.endswith("GetPlanInfo"):
            return httpx.Response(200,json={"planInfo":{
                "planName":"Ultra","includedAmountCents":40000,"billingCycleEnd":str(deadline),
            }})
        if request.url.path.endswith("full_stripe_profile"):
            return httpx.Response(200,json={"membershipType":"ultra","subscriptionStatus":"active"})
        return httpx.Response(200,json={})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        partial = cursor_provider.normalize_usage(fetch_cursor_usage("fixture",client=client))
    assert partial["cursor"]["api_percent_used"] is None
    outcome = oauth_manager.evaluate_and_toggle_by_usage(ACCOUNT,partial,threshold=100,fresh=True)
    assert outcome["action"] == "cursor_quota_unknown" and outcome["recovered_models"] == 0
    assert not _route(FABLE).candidates and _route(COMPOSER).candidates
    assert state_db.error_load(CHANNEL,FABLE) == before


def test_known_pool_recovers_without_releasing_unknown_pool(env, monkeypatch):
    account, usage, _ = env
    _install_channel(account, monkeypatch)
    usage["cursor"]["auto_percent_used"] = 100
    pause(usage)
    assert not _route(FABLE).candidates and not _route(COMPOSER).candidates
    usage["cursor"].update({"api_percent_used":20,"auto_percent_used":None})
    outcome = pause(usage)
    assert outcome["recovered_models"] == 2
    assert _route(FABLE).candidates and not _route(COMPOSER).candidates


async def test_recovery_probe_never_spends_on_an_active_quota_pause(env, monkeypatch):
    account, usage, _ = env
    _install_channel(account, monkeypatch)
    pause(usage)
    assert cooldown.get_state(CHANNEL,FABLE)["error_count"] == 0
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append((args,kwargs))
        raise AssertionError("quota pause must not issue recovery inference")
    monkeypatch.setattr(probe,"probe_channel_model",forbidden)
    assert await probe.recovery_run_once() == 0
    assert calls == [] and not _route(FABLE).candidates and _route(COMPOSER).candidates


@pytest.mark.parametrize("unknown", [False, True])
def test_manual_usage_refresh_labels_pause_and_unknown_usage(env, monkeypatch, unknown):
    account, usage, _ = env
    result = pause(usage)
    if unknown:
        usage["cursor"]["api_percent_used"] = None
        result = pause(usage)
    state_db.quota_save(ACCOUNT,oauth_manager.flatten_usage(usage),email=account["email"])
    monkeypatch.setattr(oauth_menu,"_fetch_and_save_usage_result_sync",lambda *a, **kw:{
        "usage":usage,"quota_action":result,
    })
    async def metadata(*args, **kwargs):
        return {"action":"skipped"}
    monkeypatch.setattr(oauth_menu.oauth_control,"refresh_cursor_models_raw",metadata)
    captured = []
    monkeypatch.setattr(oauth_menu.ui,"answer_cb",lambda *a, **kw:None)
    monkeypatch.setattr(oauth_menu.ui,"edit",lambda chat,message,text,**kw:captured.append(text))
    short = oauth_menu.ui.register_code(ACCOUNT)
    oauth_menu.on_refresh_usage(1,2,"cb",short)
    assert len(captured) == 1
    if unknown:
        assert "本次未取得完整分池用量，不据此解除配额暂停" in captured[0]
    else:
        assert "已按配额暂停" in captured[0] and "含已禁用模型" in captured[0]
        assert "已按额度池冷却" not in captured[0]
    assert cooldown.is_blocked(CHANNEL,FABLE)
