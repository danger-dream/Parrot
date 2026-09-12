"""Ordinary ingress failures through the real ASGI app; isolated DB/upstreams."""
from __future__ import annotations

import json
import time

import httpx
import pytest
import server
from src.tests.test_protocol_fake_upstreams import (
    _import_modules, _setup, _install_keys, _default_key, _install_channels,
    _make_anthropic_channel, _make_openai_channel,
)

PATHS = ["/v1/messages", "/v1/chat/completions", "/v1/responses"]


@pytest.fixture
async def app_client(m, monkeypatch):
    _setup(m)
    _install_keys(m, _default_key())
    _install_channels(m, [])
    # ASGITransport does not run lifespan: explicitly install a ready local
    # client so these channel/cooldown tests don't report runtime uninitialized.
    monkeypatch.setattr(m["upstream"], "_client_pool", m["upstream"].SharedClientPool(m["upstream"]._new_client))
    ready_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    m["upstream"].set_client(ready_client)
    async with ready_client, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
        base_url="http://audit.invalid", headers={"Authorization": "Bearer ccp-test"},
    ) as client:
        yield client


def rows(m):
    return [dict(row) for row in m["log_db"]._get_conn().execute(
        "SELECT request_id,status,http_status,finished_at,requested_model FROM request_log ORDER BY id"
    ).fetchall()]


def body(**values):
    return {"model": "audit-model", "messages": [{"role": "user", "content": "hello"}],
            "input": "hello", "max_tokens": 16, **values}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("payload", [[], None, "not-an-object"], ids=["array", "null", "string"])
async def test_nonobject_json_is_400_without_pending_row(m, app_client, path, payload):
    before = rows(m)
    response = await app_client.post(path, content=json.dumps(payload), headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "JSON object" in response.json()["error"]["message"]
    assert rows(m) == before


@pytest.mark.parametrize("path,field", [(path, "tools") for path in PATHS] +
    [(path, "messages") for path in PATHS[:2]])
@pytest.mark.parametrize("value", [1, {}, "not-an-array"])
async def test_collection_type_errors_are_400_before_routing(m, app_client, path, field, value):
    before = rows(m)
    response = await app_client.post(path, json=body(**{field: value}))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert field in response.json()["error"]["message"]
    assert rows(m) == before


@pytest.mark.parametrize("path", PATHS)
async def test_model_array_is_a_client_error_not_a_server_failure(m, app_client, path):
    before = rows(m)
    response = await app_client.post(path, json=body(model=["first-model", "second-model"]))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert rows(m) == before


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("registered", [False, True], ids=["unknown-404", "disabled-503"])
async def test_no_candidate_response_and_log_status_match(m, app_client, path, registered):
    if registered:
        if path == "/v1/messages":
            channel = _make_anthropic_channel(m, "audit-disabled", "http://disabled.invalid", alias="audit-model", real="audit-model")
        else:
            channel = _make_openai_channel("audit-disabled", "http://disabled.invalid",
                protocol="openai-chat" if path.endswith("completions") else "openai-responses",
                alias="audit-model", real="audit-model")
        channel.enabled = False
        _install_channels(m, [channel])
    response = await app_client.post(path, json=body())
    assert response.status_code == (503 if registered else 404), response.text
    row = rows(m)[-1]
    assert row["status"] == "error" and row["finished_at"] is not None
    assert row["http_status"] == response.status_code
    assert m["log_db"]._get_conn().execute(
        "SELECT COUNT(*) FROM retry_chain WHERE request_id=?", (row["request_id"],)
    ).fetchone()[0] == 0


@pytest.mark.parametrize("path,ingress", zip(PATHS, ["anthropic", "openai-chat", "openai-responses"]))
@pytest.mark.parametrize("collection", [None, []], ids=["null", "empty"])
async def test_empty_collections_and_default_model_semantics_remain(m, app_client, path, ingress, collection):
    m["config"].update(lambda c: c.update(ingressDefaultModel={ingress: "default-audit-model"}))
    response = await app_client.post(path, json=body(model=None, messages=collection, tools=collection))
    assert response.status_code == 404, response.text
    assert rows(m)[-1]["requested_model"] == "default-audit-model"


@pytest.mark.parametrize("path", PATHS)
async def test_auth_remains_before_body_validation(m, app_client, path):
    before = rows(m)
    response = await app_client.post(path, content="[]", headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 401, response.text
    assert rows(m) == before


@pytest.mark.parametrize("provider", ["workbuddy", "claude", "api"])
@pytest.mark.parametrize("blocked", [False, True], ids=["healthy", "all-cooling"])
async def test_health_accepts_both_oauth_model_shapes_and_uses_upstream_cooldown(m, app_client, provider, blocked):
    if provider == "workbuddy":
        from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
        from src.tests.test_workbuddy_provider import credential
        channel = WorkBuddyOAuthChannel(credential(models=["auto"]))
        assert channel.list_client_models() == ["workbuddy-auto"]
    elif provider == "claude":
        from src.channel.oauth_channel import OAuthChannel
        channel = OAuthChannel({"provider": "claude", "email": "health@example.invalid", "models": ["auto"]}, ["auto"])
    else:
        channel = _make_openai_channel("health-api", "http://health.invalid", protocol="openai-chat", alias="public-auto", real="auto")
    _install_channels(m, [channel])
    if blocked:
        m["cooldown"].record_error(channel.key, "auto", cooldown_until=int(time.time() * 1000) + 60000)
    response = await app_client.get("/health")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == ("degraded" if blocked else "ok")
    assert value["channels"]["enabled"] == 1
