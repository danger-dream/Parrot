"""#44: Gemini tool-call generation failures are request faults, not account health.

Real ASGI ingress/failover/Antigravity channels; only the Google transport is fake.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.tests.test_antigravity_oauth import _import_modules, _add_ag_account
from src.tests.test_antigravity_compatibility import clean, _body, BASE, MODEL
from src.tests import test_protocol_fake_upstreams as fake

PATHS = {"responses": "/v1/responses", "chat": "/v1/chat/completions", "anthropic": "/v1/messages"}
PROJECTS = ["proj-a", "proj-b", "proj-c"]
FINISH_MESSAGE = "Malformed function call: Failed to parse function call: Function call is empty - no input to parse."


def candidate(reason, parts=(), message=None):
    item = {"content": {"role": "model", "parts": list(parts)}, "finishReason": reason}
    if message:
        item["finishMessage"] = message
    return {"response": {"candidates": [item],
                         "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 0, "totalTokenCount": 10}}}


def reply(request, payload):
    if "streamGenerateContent" in str(request.url):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="data: " + json.dumps(payload) + "\n\n")
    return httpx.Response(200, json=payload)


OK = candidate("STOP", [{"text": "OK"}])
MALFORMED = candidate("MALFORMED_FUNCTION_CALL", message=FINISH_MESSAGE)


@pytest.fixture
async def rig(m, monkeypatch):
    from src import scheduler, upstream, log_db, scorer, cooldown
    from src.transports import http_runtime
    import server
    om = m["oauth_manager"]
    channels = []
    for project in PROJECTS:
        key = _add_ag_account(om, email=f"{project}@gmail.com", project=project,
                              models=[BASE + "-low", MODEL, BASE + "-high"])
        ch = m["AntigravityOAuthChannel"](om.get_account(key))
        ch.tls_fingerprint = None
        channels.append(ch)
    m["config"].update(lambda cfg: cfg.update(
        apiKeys={"review": {"key": "synthetic-review-key", "allowedModels": []}}, modelMapping={},
        retry={"transient": {"backoffSeconds": [0, 0]}},
    ))
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: list(channels))
    monkeypatch.setattr(scheduler.registry, "get_channel",
                        lambda key: next((ch for ch in channels if ch.key == key), None))
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("direct", None)], None))
    health = []
    monkeypatch.setattr(scorer, "record_failure", lambda *a, **k: health.append(("failure", a)))
    monkeypatch.setattr(cooldown, "record_error", lambda *a, **k: health.append(("cooldown", a)))

    async def token(*a, **k):
        return "synthetic-access"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    state = {"responses": [], "projects": []}

    def transport(request):
        state["projects"].append(json.loads(request.content)["project"])
        factory = state["responses"].pop(0) if len(state["responses"]) > 1 else state["responses"][0]
        return factory(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(upstream, "_client_pool", upstream._client_pool)
    upstream.set_client(client)
    log_db.init()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
                                     base_url="http://review.test",
                                     headers={"Authorization": "Bearer synthetic-review-key"}) as api:
            yield api, state, health
    finally:
        await client.aclose()


def latest_log():
    from src import log_db
    conn = log_db._get_conn()
    row = dict(conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 1").fetchone())
    chain = [dict(r) for r in conn.execute(
        "SELECT * FROM retry_chain WHERE request_id=? ORDER BY attempt_order", (row["request_id"],))]
    return row, chain


async def call(api, ingress, stream):
    body = _body(ingress, MODEL)
    body["stream"] = stream
    return await api.post(PATHS[ingress], json=body)


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_malformed_call_retries_same_account_then_returns_400_without_health_effects(rig, ingress, stream):
    api, state, health = rig
    state["responses"] = [lambda req: reply(req, MALFORMED)]
    response = await call(api, ingress, stream)
    assert response.status_code == 400, response.text
    assert FINISH_MESSAGE in response.text
    assert "response.completed" not in response.text and "[DONE]" not in response.text
    # One account, the initial attempt plus the shared budget of two replays.
    assert len(state["projects"]) == 3 and len(set(state["projects"])) == 1
    assert health == []
    row, chain = latest_log()
    assert row["status"] == "error" and row["http_status"] == 400
    assert FINISH_MESSAGE in row["error_message"]
    assert [r["outcome"] for r in chain] == ["request_invalid"] * 3


@pytest.mark.parametrize("reason,parts", [
    ("UNEXPECTED_TOOL_CALL", []),
    # Parrot's own check: finishReason STOP with arguments that do not parse.
    ("STOP", [{"functionCall": {"name": "lookup", "args": "{\"city\":"}}]),
])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_other_sampled_tool_call_failures_share_the_policy(rig, reason, parts, stream):
    api, state, health = rig
    state["responses"] = [lambda req: reply(req, candidate(reason, parts))]
    response = await call(api, "chat", stream)
    assert health == []
    if stream and parts:
        # The call was already streamed, so the client owns the failure.
        assert response.status_code == 200 and "gemini_malformed_function_call" in response.text
        assert len(state["projects"]) == 1
        return
    assert response.status_code == 400, response.text
    assert len(state["projects"]) == 3 and len(set(state["projects"])) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_same_account_replay_can_recover(rig, stream):
    api, state, health = rig
    state["responses"] = [lambda req: reply(req, MALFORMED), lambda req: reply(req, OK)]
    response = await call(api, "chat", stream)
    assert response.status_code == 200, response.text
    assert "OK" in response.text
    assert len(state["projects"]) == 2 and len(set(state["projects"])) == 1
    assert health == []
    row, chain = latest_log()
    assert row["status"] == "success"
    assert [r["outcome"] for r in chain] == ["request_invalid", "success"]


@pytest.mark.asyncio
async def test_replay_switch_off_returns_first_failure(m, rig):
    api, state, health = rig
    m["config"].update(lambda cfg: cfg["retry"]["transient"].update(errors={"antigravityToolCallFailure": False}))
    state["responses"] = [lambda req: reply(req, MALFORMED)]
    response = await call(api, "chat", False)
    assert response.status_code == 400 and FINISH_MESSAGE in response.text
    assert len(state["projects"]) == 1 and health == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_missing_thought_signature_is_not_replayed(rig, stream):
    api, state, health = rig
    state["responses"] = [lambda req: reply(req, candidate("MISSING_THOUGHT_SIGNATURE"))]
    response = await call(api, "chat", stream)
    assert response.status_code == 400, response.text
    assert "MISSING_THOUGHT_SIGNATURE" in response.text
    assert len(state["projects"]) == 1 and health == []


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.asyncio
async def test_failure_after_visible_output_keeps_health(rig, ingress):
    api, state, health = rig
    chunks = [("data: " + json.dumps(candidate(None, [{"text": "partial-content"}])) + "\n\n").encode(),
              ("data: " + json.dumps(MALFORMED) + "\n\n").encode()]
    state["responses"] = [lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                     stream=fake.ChunkedByteStream(chunks))]
    response = await call(api, ingress, True)
    assert response.status_code == 200, response.text
    assert "partial-content" in response.text and FINISH_MESSAGE in response.text
    assert len(state["projects"]) == 1 and health == []
    row, _chain = latest_log()
    assert row["status"] == "error" and row["http_status"] == 400


@pytest.mark.asyncio
async def test_other_failed_finish_reasons_keep_account_health_policy(rig):
    api, state, health = rig
    state["responses"] = [lambda req: reply(req, candidate("MALFORMED_RESPONSE"))]
    response = await call(api, "chat", False)
    assert response.status_code == 503, response.text
    assert sorted(state["projects"]) == PROJECTS
    assert {kind for kind, _ in health} == {"failure", "cooldown"}
