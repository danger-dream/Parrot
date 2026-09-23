"""End-to-end loopback crypto plus quota/concurrency and authorization boundaries."""
from __future__ import annotations
import asyncio
import copy
import json
import time
import uuid

import httpx
import pytest
from src import config, oauth_manager as om, cooldown
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import auth, common, signing
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_zhipu_signing import local_upstream
from src.tests.test_zhipu_provider import account_env, credential, window, quota
from src.tests.test_zhipu_management import ctl
from src.tests.test_workbuddy_lifecycle import context


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_full_production_entry_real_local_http_crypto(account_env, local_upstream, monkeypatch, ingress, stream):
    from src.tests.test_zhipu_channel import body
    base, state = local_upstream
    state["reject"] = 1
    m = fake._import_modules(); fake._setup(m)
    a = credential(models=["GLM-5.3"])
    om.add_account(a); key = om.get_account_key(a)
    config.update(lambda c: c.update(protocolBridge={"enabled": True}, network={"routing": {"default": "direct"}},
        timeouts={"connect": 3, "firstByte": 3, "idle": 3, "total": 10}))
    monkeypatch.setattr(common, "MODEL_ORIGINS", {**common.MODEL_ORIGINS, "bigmodel": base})
    signer = signing.Signer("fixture.secret", base, key, allow_insecure=True)
    monkeypatch.setattr(signing, "for_account", lambda *a, **k: signer)
    ch = ZhipuOAuthChannel(om.get_account(key)); fake._install_channels(m, [ch]); fake._install_keys(m, fake._default_key())
    payload = body(ingress, stream)
    async with httpx.AsyncClient(trust_env=False) as client:
        m["upstream"].set_client(client)
        if ingress == "anthropic":
            request_id = "loopback-" + uuid.uuid4().hex
            m["log_db"].insert_pending(request_id, "127.0.0.1", "ccp-test", payload["model"], stream, 1, 0, {}, payload, ingress_protocol=ingress)
            route = m["scheduler"].schedule(payload, api_key_name="ccp-test", client_ip="127.0.0.1", ingress_protocol=ingress)
            assert route and route.candidates
            response = await m["failover"].run_failover(route, payload, request_id, "ccp-test", "127.0.0.1", is_stream=stream,
                start_time=time.time(), ingress_protocol=ingress)
        else:
            request = fake.FakeRequest({"Authorization": "Bearer ccp-test"}, json.dumps(payload).encode())
            response = await m["openai_handler"].handle(request, ingress_protocol=ingress)
        text = await fake._consume_streaming_to_string(response) if hasattr(response, "body_iterator") else response.body.decode()
        assert response.status_code == 200 and "loopback" in text, text
    assert len(state["requests"]) == 2 and state["handshakes"] == 2 and state["gates"] == 1
    assert state["requests"][0][0] == state["requests"][1][0]
    fake._install_channels(m, [])


async def test_quota_cooldown_recovery_preserves_new_model_concurrency(account_env, monkeypatch):
    a = credential(models=["GLM-5.3", "unrelated"]); om.add_account(a); key = om.get_account_key(a)
    cooldown.init()
    old = '{"error":{"code":"1310","message":"quota"}}'
    cooldown.record_error("oauth:"+key, "GLM-5.3", old, cooldown_until=int((time.time()+500)*1000))
    data = [quota(window(100))]
    monkeypatch.setattr(common, "request", lambda *a, **k: data[0])
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "disabled"
    cooldown.record_error("oauth:"+key, "unrelated", "model access denied", cooldown_until=int((time.time()+500)*1000))
    unrelated = cooldown.get_state("oauth:"+key, "unrelated")
    data[0] = quota(window(0))
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "resumed"
    assert cooldown.get_state("oauth:"+key, "GLM-5.3") is None
    assert cooldown.get_state("oauth:"+key, "unrelated") == unrelated
    # New restriction for the same model must not be removed by the older quota observation.
    cooldown.record_error("oauth:"+key, "GLM-5.3", old, cooldown_until=int((time.time()+500)*1000))
    data[0] = quota(window(100)); om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))
    data[0] = quota(window(0)); recovery = await om.fetch_usage(key)
    cooldown.record_error("oauth:"+key, "GLM-5.3", "3008 concurrency", cooldown_until=int((time.time()+100)*1000))
    updated = cooldown.get_state("oauth:"+key, "GLM-5.3")
    om.evaluate_and_toggle_by_usage(key, recovery)
    assert cooldown.get_state("oauth:"+key, "GLM-5.3") == updated


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_actual_oauth_wire_read_only_existing_team(site, monkeypatch):
    calls = []
    def wire(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/cli/init"):
            assert kwargs["body"] == {"provider": site}
            assert len(kwargs["headers"]["Authorization"].removeprefix("Bearer ")) == 64
            return {"flow_id": "flow", "authorize_url": "https://bigmodel.cn/login" if site == "bigmodel" else "https://chat.z.ai/api/oauth/authorize",
                    "expires_at": time.time()+300, "poll_interval_sec": 2}
        if "/cli/poll/" in url:
            return {"status": "ready", "token": "platform-fixture", "user": {"user_id": "user-fixture"}, site: {"access_token": "oauth-fixture"}}
        if url.endswith("/api/auth/z/login"):
            assert kwargs["body"] == {"token": "oauth-fixture"}
            return {"access_token": "business-fixture", "expires_in": 3600}
        assert kwargs["headers"]["Authorization"] == ("oauth-fixture" if site == "bigmodel" else "Bearer business-fixture")
        if url.endswith("/api/oauth/userinfo"):
            return {"sub": "user-fixture", "name": "Fixture account"}
        if url.endswith("getCustomerInfo"):
            return {"customerName": "Fixture account", "organizations": [{"organizationId": "org", "projects": [{"projectId": "project", "projectType": "2"}]}]}
        if url.endswith("querySubscribeDetail"):
            assert kwargs["headers"]["bigmodel-organization"] == "org"
            return {"hasSubscription": True, "status": "EFFECTIVE", "memberGrantStatus": "VALID"}
        if "/copy/" in url:
            return {"secretKey": "secret"}
        if url.endswith("/api_keys"):
            return [{"apiKey": "existing", "name": "zcode-api-key"}]
        pytest.fail(url)
    monkeypatch.setattr(common, "request", wire)
    payload = auth.start_login_sync(site=site); auth.poll_login_sync(payload)
    assert payload["status"] == "ready"
    from src.management_control.oauth.backend import OAuthBackend
    choices = auth.project_choices(payload["credential"])
    account = OAuthBackend().zhipu_select_project(payload["credential"], choices[0])
    assert not account.get("model_key")
    assert auth.resolve_model_key(account) == "existing.secret" and auth.entitlement(account) == "available"
    assert not any(kw.get("method") == "POST" and url.endswith("/api_keys") for url,kw in calls)


def test_api_key_batch_import_and_invalid_mode(ctl):
    from src.management_control.oauth import OAuthImportDecision
    control, _ = ctl
    entries = [{"site": site, "credential_mode": "api_key", "model_key": "fixture.secret"} for site in ("bigmodel", "zai")]
    preview = control.preview_import(context(), format="zhipu", payload=json.dumps(entries))
    assert not preview.errors and len(preview.candidates) == 2
    result = control.commit_import(context(), preview.import_id, preview.import_secret,
        [OAuthImportDecision(item.candidate_id, "overwrite") for item in preview.candidates])
    assert len(result.added) == 2
    assert all(not a.get("refresh_token") for a in om.list_accounts())
