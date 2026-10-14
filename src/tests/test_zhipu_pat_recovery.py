"""Production channel/failover PAT retry bounds and raw streaming boundaries."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from src import oauth_manager as om
from src.oauth.zhipu import auth, common as c, project_tokens as pt, response, signing
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_zhipu_channel import env, body, call
from src.tests.test_zhipu_provider import credential
from src.tests.zhipu_pat_fixtures import pat, envelope


@pytest.fixture
def oauth_env(env, monkeypatch):
    modules, old = env
    # Retain the real shared ingress/failover setup, replace only the synthetic account.
    om.delete_account_if_unchanged(old.account_key, om.get_account(old.account_key))
    a = credential("oauth", organization_id="org", project_id="project", models=["GLM-5.3"])
    om.add_account(a)
    key = om.get_account_key(a)
    channel = ZhipuOAuthChannel(om.get_account(key))
    fake._install_channels(modules, [channel])
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    issued = []
    def wire(url, **kw):
        if url.endswith("/api_keys"):
            return [{"name":"zcode-api-key", "apiKey":"fixture"}]
        assert url.endswith("/access_tokens"), url
        issued.append(kw)
        return envelope(accessToken=pat(tag=str(len(issued))))
    monkeypatch.setattr(c, "request", wire)
    async def headers(self, request):
        return {**request.headers, "X-Client-Sig":"mock-signature"}
    monkeypatch.setattr(signing.Signer, "headers", headers)
    return (modules, channel), issued


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("final_status", [200, 401])
async def test_plain_model_401_refreshes_once_and_only_final_failure_disables(oauth_env, stream, final_status):
    environment, issued = oauth_env
    attempts = []
    def wire(req):
        attempts.append(req)
        if len(attempts) == 1 or final_status == 401:
            return httpx.Response(401, json={"error":{"message":"token expired"}})
        return fake._anthropic_sse_response("recovered") if stream else fake._anthropic_response("recovered")
    result, text, requests = await call(environment, "anthropic", body("anthropic", stream), wire)
    assert len(requests) == 2 and len(issued) == 2
    assert requests[0].headers["x-api-key"] != requests[1].headers["x-api-key"]
    assert requests[0].content == requests[1].content
    for key in ("x-session-id", "x-query-id", "x-zcode-trace-id"):
        assert requests[0].headers[key] == requests[1].headers[key]
    assert requests[0].headers["x-request-id"] != requests[1].headers["x-request-id"]
    current = om.get_account(environment[1].account_key)
    assert current["enabled"] is (final_status == 200)
    if final_status == 200:
        assert result.status_code == 200 and "recovered" in text


@pytest.mark.parametrize("reason,expected", [("VERIFY_SIGNATURE_INVALID",3), ("VERIFY_APIKEY_EXPIRED",3),
    ("VERIFY_UNKNOWN",1), ("3007",1), ("security check failed (3007)",1), ("需要验证码",1), ("captcha_required",1), ("quota",1), ("1310",1)])
async def test_security_and_quota_401_never_refresh_pat(oauth_env, reason, expected):
    environment, issued = oauth_env
    _, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error":{"code":reason, "reason":reason, "message":reason}}))
    assert len(issued) == 1 and len(requests) == expected
    assert not signing.refreshable(401, {"reason":"3007"})


async def test_inner_signature_and_pat_bound_is_six_not_twelve(oauth_env):
    environment, issued = oauth_env
    count = []
    def wire(req):
        count.append(req)
        if len(count) in (1, 2, 4, 5):
            return httpx.Response(401, json={"reason":"VERIFY_SIGNATURE_INVALID"})
        if len(count) == 3:
            return httpx.Response(401, json={"error":{"message":"PAT rejected"}})
        return fake._anthropic_response("six sends")
    result, text, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert result.status_code == 200 and "six sends" in text
    assert len(requests) == 6 and len(issued) == 2
    assert [bool(r.headers.get("x-client-sig")) for r in requests] == [True,True,False,True,True,False]


async def test_thinking_repair_plus_pat_and_signer_replay_has_concrete_eight_send_path(oauth_env):
    environment, issued = oauth_env
    count = []
    def wire(req):
        count.append(req)
        n = len(count)
        if n in (1,3,4,6,7):
            return httpx.Response(401, json={"reason":"VERIFY_SIGNATURE_INVALID"})
        if n == 2:
            return httpx.Response(400, json={"error":{"message":"Invalid signature in thinking block"}})
        if n == 5:
            return httpx.Response(401, json={"error":{"message":"PAT rejected"}})
        return fake._anthropic_response("eight sends")
    result, text, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert result.status_code == 200 and "eight sends" in text
    assert len(requests) == 8 and len(issued) == 2


@pytest.mark.parametrize("stream", [False, True])
async def test_2xx_or_sse_body_auth_error_cannot_replay(oauth_env, stream):
    environment, issued = oauth_env
    def wire(req):
        if stream:
            return httpx.Response(200, content=b'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"expired","code":"3007"}}\n\n',
                headers={"content-type":"text/event-stream"})
        return httpx.Response(200, json={"code":3007, "msg":"expired"})
    _, _, requests = await call(environment, "anthropic", body("anthropic", stream), wire)
    assert len(requests) == 1 and len(issued) == 1
    # Terminal 3007 may still mark model auth failure; it must never replay.


@pytest.mark.parametrize("change", ["disable", "delete", "replace", "new_pat"])
async def test_late_401_never_disables_or_borrows_changed_account(oauth_env, change):
    environment, issued = oauth_env
    key = environment[1].account_key
    count = []
    def wire(req):
        count.append(req)
        if len(count) > 1:
            return fake._anthropic_response("peer token")
        current = copy.deepcopy(om.get_account(key))
        if change == "disable":
            om.set_enabled(key, False)
        elif change == "delete":
            om.delete_account_if_unchanged(key, current)
        elif change == "replace":
            om.mutate_account_if_unchanged(key, current, lambda a: a.update(access_token="new-login"))
        else:
            auth.resolve_model_key(current, account_key=key, force=True)
        return httpx.Response(401, json={"error":{"message":"old rejection"}})
    _, _, requests = await call(environment, "anthropic", body("anthropic"), wire)
    current = om.get_account(key)
    if change == "delete":
        assert current is None
    elif change == "disable":
        assert not current["enabled"] and current["disabled_reason"] == "user"
    else:
        assert current["enabled"]
    assert len(issued) == (2 if change == "new_pat" else 1)
    assert len(requests) == (2 if change == "new_pat" else 1)


@pytest.mark.parametrize("status", [0, 401])
async def test_pat_refresh_management_failure_does_not_disable_model(oauth_env, monkeypatch, status):
    environment, issued = oauth_env
    original = c.request
    def management(url, **kw):
        if url.endswith("/access_tokens") and issued:
            raise c.ZhipuError("project_token", "network" if not status else "upstream", status=status)
        return original(url, **kw)
    monkeypatch.setattr(c, "request", management)
    _, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error":{"message":"expired"}}))
    assert len(requests) == 1 and om.get_account(environment[1].account_key)["enabled"]
    assert (om.get_account(environment[1].account_key).get("management_status") == "relogin_required") is bool(status)


@pytest.mark.parametrize("args,phase", [
    ({"iterator_created":False,"status":200},"request_setup"),
    ({"iterator_created":True,"emitted_boundary":True,"status":401},"response_body"),
    ({"iterator_created":True,"status":200},"response_body"),
    ({"iterator_created":True,"status":401,"content_type":"text/event-stream"},"request_setup"),
    ({"iterator_created":True,"status":302,"error_chunk":True},"request_setup"),
    ({"iterator_created":True,"content_type":"text/event-stream"},"response_body"),
    ({"iterator_created":True,"error_chunk":True},"response_body")])
def test_phase_condition_order(args, phase):
    assert response.stream_phase(**args) == phase


@pytest.mark.parametrize("flag", ["resolved", "output_committed", "emitted_boundary", "stream_started"])
def test_claim_forbids_prepare_output_or_boundary(flag):
    observation = {"resolved":True,"status":401,"phase":"request_setup"}
    result = SimpleNamespace(translator_ctx={"zhipu_auth":observation}, http_status=401, stream_started=False)
    assert response.replayable_rejection(result)
    if flag == "stream_started":
        result.stream_started = True
    else:
        observation[flag] = flag != "resolved"
    assert not response.replayable_rejection(result)


@pytest.mark.parametrize("stream", [False, True])
async def test_accepted_body_never_moves_to_another_candidate(oauth_env, stream):
    environment, issued = oauth_env
    backup = credential(model_key="backup.secret", models=["GLM-5.3"])
    om.add_account(backup)
    backup_channel = ZhipuOAuthChannel(om.get_account(om.get_account_key(backup)))
    fake._install_channels(environment[0], [environment[1], backup_channel])
    count = []
    def wire(req):
        count.append(req)
        if len(count) > 1:
            return fake._anthropic_response("duplicate generation")
        if stream:
            return httpx.Response(200, content=b'event: error\ndata: {"type":"error","error":{"type":"authentication_error","message":"expired","code":"3007"}}\n\n',
                                  headers={"content-type":"text/event-stream"})
        return httpx.Response(200, json={"code":3007})
    _, text, requests = await call(environment, "anthropic", body("anthropic", stream), wire)
    assert len(requests) == 1 and "duplicate generation" not in text


async def test_2xx_json_read_failure_does_not_generate_again(oauth_env):
    environment, issued = oauth_env
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'
            raise httpx.ReadError("body interrupted after accepted headers")
    count = []
    def wire(req):
        count.append(req)
        if len(count) > 1:
            return fake._anthropic_response("duplicate generation")
        return httpx.Response(200, stream=BrokenBody(), headers={"content-type":"application/json"})
    _, _, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert len(requests) == 1 and len(issued) == 1


async def test_login_replaced_between_token_await_and_request_snapshot_cannot_borrow(oauth_env, monkeypatch):
    environment, _ = oauth_env
    channel = environment[1]
    original = om.ensure_channel_token
    async def replaced(ch):
        token = await original(ch)
        current = copy.deepcopy(om.get_account(ch.account_key))
        om.mutate_account_if_unchanged(ch.account_key, current, lambda a: a.update(access_token="new-login"))
        return token
    monkeypatch.setattr(om, "ensure_channel_token", replaced)
    with pytest.raises(c.ZhipuError, match="scope_invalidated"):
        await channel.build_upstream_request(body("anthropic"), "GLM-5.3")
    assert om.get_account(channel.account_key)["access_token"] == "new-login"
