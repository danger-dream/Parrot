"""Qoder inference paths against fixture Qoder SSE, never live upstreams."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest

from src import config
from src.channel.qoder_oauth_channel import QoderOAuthChannel
from src.oauth.qoder import auth, catalog, signing, device_identity, device_profile
from src.openai.transform.guard import GuardError
from src.tests import test_protocol_fake_upstreams as fake

LISTING = json.loads((Path(__file__).parent / "fixtures" / "qoder" / "listing.json").read_text())
CHAT_URL = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"


@pytest.fixture
def env(monkeypatch):
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    entry = auth.normalize_credential({"realm": "global", "uid": "qd-" + uuid.uuid4().hex, "email": "q@example.com",
        "access_token": "fixture-job", "refresh_token": "fixture-refresh", "expired": "2099-01-01T00:00:00Z",
        "machine_id": "fixture-machine", "device_token": "fixture-device", "device_refresh_token": "fixture-device-refresh"})
    entry["qoder_device_profile"] = device_profile.generate("fixture-machine")
    monkeypatch.setattr(device_identity, "headers", lambda account, **kw: {
        "Cosy-MachineId": account["machine_id"], "Cosy-MachineToken": "issued", "Cosy-MachineType": "type",
        "Cosy-MachineCode": "code", "Cosy-MachineOS": "x86_64_linux"})
    records = catalog.records_from_listing(LISTING)
    entry.update(models=["ultimate", "efficient"], account_model_catalog={"schema": 1, "models": records})
    config.update(lambda c: c.update(oauthAccounts=[entry], channels=[],
        network={"routing": {"default": "direct"}},
        timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 5},
        protocolBridge={"enabled": True},
        concurrency={"queueWaitSeconds": 1}))
    fake._install_keys(m, fake._default_key())
    ch = QoderOAuthChannel(entry)
    fake._install_channels(m, [ch])
    yield m, ch
    config.update(lambda c: (c.clear(), c.update(before)))
    fake._install_channels(m, [])


def frame(body) -> bytes:
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return b"data:" + json.dumps({"headers": {}, "body": raw, "statusCodeValue": 200, "statusCode": "OK"}, ensure_ascii=False).encode() + b"\n\n"


def frames(*, tool=False):
    def item(delta, finish=None):
        return {"choices": [{"index": 0, "delta": delta, **({"finish_reason": finish} if finish else {})}], "object": "chat.completion.chunk"}
    values = [frame(item({"reasoning_content": "fixture thought"}))]
    if tool:
        values.append(frame(item({"content": 'Calling.\n<tool_call>{"name":"fixture_tool","arguments":{"city":"昆明"}}</tool_call>'})))
    else:
        values += [frame(item({"content": "你好，"})), frame(item({"content": "Qoder"}))]
    values.append(frame(item({"content": ""}, "stop")))
    values.append(frame({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 3}, "completion_tokens_details": {"reasoning_tokens": 2}}}))
    values.append(frame("[DONE]"))
    raw = b"".join(values)
    return [raw[i:i + 13] for i in range(0, len(raw), 13)]


def request_body(ingress, stream, *, tool=False):
    body = {"model": "ultimate", "stream": stream}
    if ingress == "responses":
        body["input"] = "ping"
        if tool:
            body["tools"] = [{"type": "function", "name": "fixture_tool", "parameters": {"type": "object"}}]
    else:
        body["messages"] = [{"role": "user", "content": "ping"}]
        if ingress == "anthropic":
            body["max_tokens"] = 64
            if tool:
                body["tools"] = [{"name": "fixture_tool", "input_schema": {"type": "object"}}]
        elif tool:
            body["tools"] = [{"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object"}}}]
    return body


def unsigned(req: httpx.Request) -> dict:
    """The plaintext Qoder body, after checking the COSY signature over the wire body."""
    wire = req.content.decode()
    _, payload, signature = req.headers["authorization"].removeprefix("Bearer ").split(".")
    expected = hashlib.md5(f"{payload}\n{req.headers['cosy-key']}\n{req.headers['cosy-date']}\n{wire}\n"
                           "/api/v2/service/pro/sse/agent_chat_generation".encode()).hexdigest()
    assert signature == expected
    assert json.loads(base64.b64decode(payload))["cosyVersion"] == "1.24.2"
    assert req.headers["cosy-machineid"] == "fixture-machine" and req.headers["x-model-key"] == "ultimate"
    return json.loads(signing.decode_body(wire))


async def call(env, ingress, body, response_factory):
    m, ch = env
    router = fake.MockRouter()
    seen = []

    def wire(req):
        assert str(req.url) == CHAT_URL
        seen.append(unsigned(req))
        return response_factory(req)

    router.register("https://api3.qoder.sh", wire)
    # Qoder owns explicit trust_env=False clients even for the direct route.
    from src.proxy.connector import DirectConnector
    from unittest.mock import patch
    patcher = patch.object(DirectConnector, "create_httpx_client",
        lambda self, **kw: httpx.AsyncClient(transport=httpx.MockTransport(router.handle), timeout=10))
    patcher.start()
    if ingress == "anthropic":
        response, client, route = await fake._call_anthropic_core(m, router, body)
        assert route.candidates[0][0].provider == "qoder"
    else:
        response, client = await fake._call_openai_handler(m, router, ingress, body)
    try:
        text = await fake._consume_streaming_to_string(response) if hasattr(response, "body_iterator") else response.body.decode()
    finally:
        await client.aclose()
        patcher.stop()
    return response, text, seen


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool", [False, True])
async def test_six_protocol_paths_text_tools_and_usage(env, ingress, stream, tool):
    body = request_body(ingress, stream, tool=tool)
    response, text, seen = await call(env, ingress, body, lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames(tool=tool)), headers={"content-type": "text/event-stream"}))
    assert len(seen) == 1 and response.status_code == 200, text
    sent = seen[0]
    assert sent["model_config"]["key"] == "ultimate" and sent["session_type"] == "app"
    assert sent["messages"][-1] == {"role": "user", "content": [{"type": "text", "text": "ping"}]}
    assert ("tools" in sent) == tool
    if stream:
        assert ("fixture_tool" in text and "昆明" in text) if tool else ("你好，" in text and "Qoder" in text)
        assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[ingress] in text
        assert "<tool_call>" not in text
    else:
        obj = json.loads(text)
        if ingress == "chat":
            message = obj["choices"][0]["message"]
            assert obj["usage"]["prompt_tokens"] == 12 and obj["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
            if tool:
                assert obj["choices"][0]["finish_reason"] == "tool_calls"
                assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"city": "昆明"}
                assert message["content"] == "Calling.\n"
            else:
                assert message["content"] == "你好，Qoder"
        else:
            assert ("fixture_tool" in text) if tool else ("你好，Qoder" in text)
    row = env[0]["log_db"]._get_conn().execute(
        "SELECT status,final_channel_key,input_tokens,output_tokens,cache_read_tokens FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "success" and row["final_channel_key"] == env[1].key
    assert row["input_tokens"] + row["cache_read_tokens"] == 12 and row["output_tokens"] == 4


async def test_reasoning_effort_and_thinking_off_reach_qoder_parameters(env):
    _, _, seen = await call(env, "chat", dict(request_body("chat", False), reasoning_effort="minimal"), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"}))
    assert seen[0]["parameters"]["enable_thinking"] is True and seen[0]["parameters"]["reasoning_effort"] == "low"
    body = dict(request_body("anthropic", False), thinking={"type": "disabled"})
    _, _, seen = await call(env, "anthropic", body, lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"}))
    assert seen[0]["parameters"]["enable_thinking"] is False and "reasoning_effort" not in seen[0]["parameters"]


@pytest.mark.parametrize("status,body,expected,classification", [
    (403, {"code": "10605", "message": json.dumps({"isQueued": False, "retryAfterSeconds": 5, "serviceAvailable": False})}, 503, None),
    (403, {"code": "112", "message": "credits used up"}, 429, "quota_exhausted"),
    (400, {"code": "406", "message": "sensitive"}, 400, "invalid_request"),
])
async def test_qoder_failures_are_mapped_before_parrot_classifies_them(env, status, body, expected, classification):
    response, text, seen = await call(env, "chat", request_body("chat", False), lambda req:
        httpx.Response(status, json=body))
    error = json.loads(text)["error"]
    if classification == "invalid_request":
        # A refusal of the request itself reaches the client as it is, with no health penalty.
        assert response.status_code == 400 and error["type"] == "invalid_request_error" and error["code"] == "406", text
    else:
        root = error["details"]["root_cause"]
        assert root["status"] == expected and root["code"] == body["code"], text
        if classification:
            assert root["classification"] == classification
    assert seen  # the request was signed and sent
    account = config.get()["oauthAccounts"][0]
    assert account.get("disabled_reason") is None  # a busy queue or quota is not a lapsed sign-in


@pytest.mark.parametrize("broken", ["empty", "done_only", "unreadable_call", "error_envelope"])
async def test_a_bad_stream_never_becomes_success(env, broken):
    errors = {
        "empty": [], "done_only": [frame("[DONE]")],
        "unreadable_call": [frame({"choices": [{"index": 0, "delta": {"content": '<tool_call>{"name":"x","arguments":{"a":}}</tool_call>'}}]})],
        "error_envelope": [b"data:" + json.dumps({"body": json.dumps({"code": "10605"}), "statusCodeValue": 403}).encode() + b"\n\n"],
    }
    response, text, _ = await call(env, "chat", request_body("chat", False), lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(errors[broken]), headers={"content-type": "text/event-stream"}))
    assert response.status_code != 200, text


async def test_a_real_model_sync_persists_what_every_request_sends(env, monkeypatch):
    from src import oauth_manager as om
    from src.oauth import qoder as qoder_pkg
    _, ch = env
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    config.update(lambda c: (c.setdefault("oauth", {}).update(mockMode=False),
        c["oauthAccounts"][0].update(models=[], account_model_catalog={}, disabledModels=["mmodel"], label="保留备注")))
    key = ch.account_key
    seen = []

    def fetch(account, *, account_key="", timeout=15.0):
        seen.append((account["access_token"], account_key))
        return catalog.records_from_listing(LISTING)

    monkeypatch.setattr(qoder_pkg, "fetch_models_sync", fetch)
    result = await om._discover_account_models_once(key, timeout_s=10)
    assert result["action"] == "updated", result
    assert seen == [("fixture-job", key)]
    saved = om.get_account(key)
    assert saved["models"][:3] == ["ultimate", "performance", "fast"] and saved["disabledModels"] == ["mmodel"]
    assert saved["label"] == "保留备注" and saved["last_model_sync_source"] == "upstream:qoder"
    record = next(r for r in om.account_model_records(saved) if r["id"] == "ultimate")
    assert record["qoderConfig"]["key"] == "ultimate" and record["qoderWindows"] == [200000, 400000, 1000000]
    channel = QoderOAuthChannel(saved)
    assert "mmodel" not in [m["real"] for m in channel.models]
    request = await channel.build_upstream_request({"model": "ultimate", "messages": [{"role": "user", "content": "x"}]},
                                                   "ultimate", ingress_protocol="chat")
    assert json.loads(request.body)["model_config"]["key"] == "ultimate"


async def test_model_without_synced_config_or_images_is_a_candidate_guard(env):
    _, ch = env
    with pytest.raises(GuardError):
        await ch.build_upstream_request({"model": "nope", "messages": [{"role": "user", "content": "x"}]}, "nope", ingress_protocol="chat")
    config.update(lambda c: c["oauthAccounts"][0]["account_model_catalog"]["models"][0].pop("qoderConfig"))
    with pytest.raises(GuardError):
        await ch.build_upstream_request({"model": "ultimate", "messages": [{"role": "user", "content": "x"}]}, "ultimate", ingress_protocol="chat")
