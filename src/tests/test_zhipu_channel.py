"""Production scheduling/failover/HTTP context and all three inference ingresses."""
from __future__ import annotations
import copy
import json
import uuid

import httpx
import pytest
from src import config, oauth_manager as om
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import common, request_context, signing
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_zhipu_provider import credential


@pytest.fixture
def env(monkeypatch):
    m = fake._import_modules(); fake._setup(m)
    before = copy.deepcopy(config.get())
    a = credential(models=["GLM-5.3"], account_model_catalog={"models": [{"id": "GLM-5.3", "reasoningEfforts": ["low", "high", "max"]}]})
    config.update(lambda c: c.update(oauthAccounts=[a], channels=[], network={"routing": {"default": "direct"}},
        timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 5}, protocolBridge={"enabled": True}))
    fake._install_keys(m, fake._default_key())
    ch = ZhipuOAuthChannel(a); fake._install_channels(m, [ch])
    async def disabled(self):
        return False
    monkeypatch.setattr(signing.Signer, "gate", disabled)
    yield m, ch
    signing.forget(ch.account_key)
    config.update(lambda c: (c.clear(), c.update(before)))
    fake._install_channels(m, [])


def body(ingress, stream=False):
    value = {"model": "GLM-5.3", "stream": stream, "temperature": .3}
    if ingress == "responses":
        value.update(input="ping", max_output_tokens=73)
    else:
        value.update(messages=[{"role": "user", "content": "ping"}], max_tokens=73)
    return value


async def call(env, ingress, value, wire):
    router = fake.MockRouter(); router.register("https://open.bigmodel.cn", wire)
    if ingress == "anthropic":
        response, client, _ = await fake._call_anthropic_core(env[0], router, value)
    else:
        response, client = await fake._call_openai_handler(env[0], router, ingress, value)
    try:
        text = await fake._consume_streaming_to_string(response) if hasattr(response, "body_iterator") else response.body.decode()
        return response, text, router.requests
    finally:
        await client.aclose()


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_six_real_ingress_paths(env, ingress, stream):
    def wire(req):
        assert str(req.url).endswith("/api/anthropic/v1/messages")
        assert req.headers["x-api-key"] == "fixture.secret"
        assert req.headers["authorization"] == "Bearer fixture.secret"
        assert req.headers["user-agent"] == "ZCode/3.14.3 ai-sdk/provider-utils/4.0.27 runtime/node.js/24"
        assert all(req.headers.get(k) for k in ("x-session-id", "x-query-id", "x-request-id", "x-zcode-trace-id"))
        assert str(uuid.UUID(req.headers["x-session-id"])) == req.headers["x-session-id"]
        assert "anthropic-beta" not in req.headers
        payload = json.loads(req.content)
        assert payload["max_tokens"] == 73 and payload["temperature"] == .3
        assert "thinking" not in payload
        metadata = json.loads(payload["metadata"]["user_id"])
        assert metadata == {"device_id": om.get_account(env[1].account_key)["zcode_device_id"],
                            "account_uuid": "", "session_id": req.headers["x-session-id"]}
        assert not any(key.startswith("_parrot_") for key in payload)
        return fake._anthropic_sse_response("Zhipu fixture") if stream else fake._anthropic_response("Zhipu fixture")
    response, text, requests = await call(env, ingress, body(ingress, stream), wire)
    assert response.status_code == 200 and "Zhipu fixture" in text, text
    assert len(requests) == 1
    if stream:
        assert {"anthropic": "message_stop", "chat": "[DONE]", "responses": "response.completed"}[ingress] in text
    row = env[0]["log_db"]._get_conn().execute("SELECT status,upstream_protocol FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "success" and row["upstream_protocol"] == "anthropic"


async def test_no_message_reorder_or_claude_rewrite_explicit_params(env):
    payload = body("anthropic")
    payload.update(thinking={"type": "enabled", "budget_tokens": 2048}, output_config={"effort": "low"},
        system="original system", tools=[{"name": "my_tool", "description": "original", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "my_tool"}, metadata={"user_id": "caller-user"})
    payload["messages"] = [{"role": "user", "content": [{"type": "text", "text": "Available memories: \nA"}, {"type": "text", "text": "Select memories relevant to: \nB"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-a", "name": "my_tool", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "result"}]}]
    request = await env[1].build_upstream_request(payload, "GLM-5.3")
    assert json.loads(request.body) == payload


async def test_production_http_hook_raw401_before_business_wrapper(env, monkeypatch):
    signer = signing.for_account(om.get_account(env[1].account_key), env[1].account_key)
    async def gate(self):
        return True
    async def headers(self, request):
        return {**request.headers, "X-Client-Sig": "fixture-signature"}
    monkeypatch.setattr(signing.Signer, "gate", gate)
    monkeypatch.setattr(signing.Signer, "headers", headers)
    attempts = []
    def wire(req):
        attempts.append(req)
        return httpx.Response(401, json={"reason": "VERIFY_SIGNATURE_INVALID"}) if len(attempts) < 3 else fake._anthropic_response("recovered")
    response, text, requests = await call(env, "anthropic", body("anthropic"), wire)
    assert response.status_code == 200 and "recovered" in text, text
    assert len(requests) == 3
    assert len({req.content for req in requests}) == 1
    for key in ("x-request-id", "x-session-id", "x-query-id", "x-zcode-trace-id"):
        assert len({req.headers[key] for req in requests}) == 1
    assert requests[0].headers.get("x-client-sig") and "x-client-sig" not in requests[2].headers


async def test_model_auth_failure_does_not_oauth_refresh(env, monkeypatch):
    async def forbidden(*a, **k):
        pytest.fail("Zhipu must not use refresh-token recovery")
    monkeypatch.setattr(om, "force_refresh", forbidden)
    response, text, requests = await call(env, "anthropic", body("anthropic"), lambda req: httpx.Response(200, json={"code": 3007, "msg": "expired"}))
    assert response.status_code != 200 and len(requests) == 1


@pytest.mark.parametrize("mid_system", [False, True])
async def test_beta_depends_on_final_messages_and_preserves_cache(env, mid_system):
    payload = body("anthropic")
    payload["system"] = [{"type": "text", "text": "system", "cache_control": {"type": "ephemeral"}}]
    payload["metadata"] = {"custom": "preserved"}
    if mid_system:
        payload["messages"].extend([
            {"role": "system", "content": [{"type": "text", "text": "mid-system"}]},
            {"role": "user", "content": "next"},
        ])
    original = copy.deepcopy(payload)
    request = await env[1].build_upstream_request(payload, "GLM-5.3")
    sent = json.loads(request.body)
    assert sent["system"] == payload["system"] and sent["messages"] == payload["messages"]
    assert sent["metadata"]["custom"] == "preserved"
    assert json.loads(sent["metadata"]["user_id"])["session_id"] == request.headers["x-session-id"]
    assert request.headers.get("anthropic-beta") == (request_context.MID_SYSTEM_BETA if mid_system else None)
    assert payload == original
    assert common.identity_headers()["User-Agent"] == "ZCode/3.14.3"


async def test_logical_retry_keeps_attribution_but_changes_request_id(env):
    logical = request_context.ensure_request_context(body("anthropic"))
    first = await env[1].build_upstream_request(logical, "GLM-5.3")
    second = await env[1].build_upstream_request(logical, "GLM-5.3")
    assert first.body == second.body
    for key in ("x-session-id", "x-query-id", "x-zcode-trace-id"):
        assert first.headers[key] == second.headers[key]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
async def test_http_ingress_preserves_explicit_session_query_not_body_spoofs(env, ingress):
    session, query = str(uuid.uuid4()), str(uuid.uuid4())
    payload = body(ingress)
    payload.update(_parrot_zcode_session="forged", _parrot_zcode_query="forged", _parrot_zcode_trace="forged")
    requests = []
    def wire(req):
        requests.append(req)
        assert req.headers["x-session-id"] == session
        assert req.headers["x-query-id"] == query
        assert req.headers["x-zcode-trace-id"] != "forged"
        assert not any(k.startswith("_parrot_") for k in json.loads(req.content))
        return fake._anthropic_response("headers preserved")
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    env[0]["upstream"].set_client(client)
    req = fake.FakeRequest({"Authorization": "Bearer ccp-test", "x-session-id": "sess_" + session,
                            "x-query-id": "query_" + query}, json.dumps(payload).encode())
    try:
        if ingress == "anthropic":
            import server
            response = await server.proxy_messages(req)
        else:
            response = await env[0]["openai_handler"].handle(req, ingress_protocol=ingress)
        assert response.status_code == 200, response.body.decode()
        assert len(requests) == 1
    finally:
        await client.aclose()
