from __future__ import annotations

import json

import pytest
from starlette.requests import Request

import server
from src import config
from src.openai import handler as openai_handler


_ABSENT = object()
_INVALID_MODELS = (_ABSENT, None, "", "   ", 123)


def _request(body: dict) -> Request:
    raw = json.dumps(body).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    return Request({
        "type": "http", "method": "POST", "path": "/", "query_string": b"",
        "headers": [(b"authorization", b"Bearer test")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "scheme": "http",
    }, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_model", _INVALID_MODELS)
async def test_anthropic_messages_requires_explicit_model_before_schedule_or_log(
    monkeypatch, invalid_model,
):
    config._cache["ingressDefaultModel"] = {"anthropic": "must-not-default"}
    effects = {"schedule": 0, "log": 0}
    monkeypatch.setattr(server.auth, "validate", lambda _headers: ("test-key", [], None))

    def schedule(*_args, **_kwargs):
        effects["schedule"] += 1
        raise AssertionError("invalid model must not schedule")

    def insert(*_args, **_kwargs):
        effects["log"] += 1
        raise AssertionError("invalid model must not create pending log")

    monkeypatch.setattr(server.scheduler, "schedule", schedule)
    monkeypatch.setattr(server.log_db, "insert_pending", insert)
    body = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}
    if invalid_model is not _ABSENT:
        body["model"] = invalid_model
    response = await server.proxy_messages(_request(body))
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert effects == {"schedule": 0, "log": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["chat", "responses"])
@pytest.mark.parametrize("invalid_model", _INVALID_MODELS)
async def test_openai_inference_requires_explicit_model_before_schedule_or_log(
    monkeypatch, ingress, invalid_model,
):
    key = "openai-chat" if ingress == "chat" else "openai-responses"
    config._cache["ingressDefaultModel"] = {key: "must-not-default"}
    effects = {"schedule": 0, "log": 0}
    monkeypatch.setattr(openai_handler.auth, "validate", lambda _headers: ("test-key", [], None))

    def schedule(*_args, **_kwargs):
        effects["schedule"] += 1
        raise AssertionError("invalid model must not schedule")

    def insert(*_args, **_kwargs):
        effects["log"] += 1
        raise AssertionError("invalid model must not create pending log")

    monkeypatch.setattr(openai_handler.scheduler, "schedule", schedule)
    monkeypatch.setattr(openai_handler.log_db, "insert_pending", insert)
    body = (
        {"messages": [{"role": "user", "content": "hello"}]}
        if ingress == "chat" else {"input": "hello"}
    )
    if invalid_model is not _ABSENT:
        body["model"] = invalid_model
    response = await openai_handler.handle(_request(body), ingress_protocol=ingress)
    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"].get("param") == "model"
    assert effects == {"schedule": 0, "log": 0}


def test_shared_validator_preserves_explicit_alias_after_trimming():
    from src import model_validation

    body = {"model": "  alias-model  "}
    assert model_validation.require_explicit_model(body) == "alias-model"
