"""Account lifetime checks across routing IO and every signed physical send."""
import asyncio
import copy
import uuid

import httpx
import pytest

from src import oauth_manager as om
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import common as c, endpoint_routing as er, project_tokens as pt, runtime, signing
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.zhipu_pat_fixtures import envelope
from src.transports.http import HttpStreamRequest


@pytest.fixture(params=["oauth", "api_key"])
def channel(account_env, monkeypatch, request):
    account = credential(request.param, organization_id="org", project_id="project",
                         project_key_id="fixture", models=["GLM-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    def issue(url, **kwargs):
        assert url.endswith("/access_tokens")
        return envelope()
    monkeypatch.setattr(c, "request", issue)
    try:
        yield ZhipuOAuthChannel(om.get_account(key))
    finally:
        runtime.forget(key)


def body():
    return {"model": "GLM-5.3", "messages": [{"role": "user", "content": "fixture"}]}


def invalidate(channel, change):
    key = channel.account_key
    if change == "delete":
        om.delete_account(key)
    elif change == "pause":
        om.set_enabled(key, False)
    else:
        current = copy.deepcopy(om.get_account(key))
        assert om.mutate_account_if_unchanged(key, current,
            lambda a: a.update(generationId=str(uuid.uuid4())))["status"] == "updated"


@pytest.mark.parametrize("change", ["delete", "replace", "pause"])
async def test_routing_wait_cannot_recreate_invalidated_signer(channel, monkeypatch, change):
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(self):
        entered.set()
        await release.wait()
        return b'{"code":0,"data":{}}'
    monkeypatch.setattr(er.EndpointRouter, "_fetch_raw", fetch)
    task = asyncio.create_task(channel.build_upstream_request(body(), "GLM-5.3"))
    replacement = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        invalidate(channel, change)
        if change == "replace":
            replacement = signing.for_account(dict(om.get_account(channel.account_key), model_key="new.secret"),
                                               channel.account_key)
        release.set()
        with pytest.raises(c.ZhipuError, match="scope_invalidated"):
            await task
        if replacement is not None:
            assert signing._cache[channel.account_key][1] is replacement
            assert not replacement.disposed
        else:
            assert channel.account_key not in signing._cache
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("phase", ["signing", "replay", "bypass"])
async def test_each_physical_send_checks_account_after_signing(channel, monkeypatch, phase):
    async def fetch(self):
        return b'{"code":0,"data":{}}'
    monkeypatch.setattr(er.EndpointRouter, "_fetch_raw", fetch)
    upstream = await channel.build_upstream_request(body(), "GLM-5.3")
    async def headers(self, request):
        if phase == "signing":
            om.set_enabled(channel.account_key, False)
        return {**request.headers, "X-Client-Sig": "fixture"}
    monkeypatch.setattr(signing.Signer, "headers", headers)
    if phase == "bypass":
        signing._cache[channel.account_key][1].bypass = True
        om.set_enabled(channel.account_key, False)
    sent = []
    class Context:
        def __init__(self, request):
            self.request = request
        async def __aenter__(self):
            sent.append(self.request)
            if phase == "replay":
                om.set_enabled(channel.account_key, False)
                return httpx.Response(401, json={"error": {"reason": "VERIFY_SIGNATURE_INVALID"}})
            return httpx.Response(200)
        async def __aexit__(self, *args):
            pass
    request = HttpStreamRequest("POST", upstream.url, upstream.headers, upstream.body, 2, 5)
    with pytest.raises(c.ZhipuError, match="scope_invalidated"):
        async with upstream.stream_context_hook(request, Context):
            pytest.fail("invalid account dispatched")
    assert len(sent) == (1 if phase == "replay" else 0)


async def test_bound_pat_must_still_be_current_after_routing(account_env, monkeypatch):
    account = credential("oauth", organization_id="org", project_id="project",
                         project_key_id="fixture", models=["GLM-5.3"])
    om.add_account(account)
    channel = ZhipuOAuthChannel(om.get_account(om.get_account_key(account)))
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(c, "request", lambda *a, **kw: envelope())
    async def fetch(self):
        pt.forget(channel.account_key)
        return b'{"code":0,"data":{}}'
    monkeypatch.setattr(er.EndpointRouter, "_fetch_raw", fetch)
    try:
        with pytest.raises(c.ZhipuError, match="scope_invalidated"):
            await channel.build_upstream_request(body(), "GLM-5.3")
        assert channel.account_key not in signing._cache
    finally:
        runtime.forget(channel.account_key)
