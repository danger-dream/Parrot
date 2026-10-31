"""Local policy denies before admission/routing; all state is test-isolated."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import server
from src import api_key_policy as policy, apikey_limiter, config, drain
from src.management_api.schemas.apikey import ApiKeyUpdateRequest
from src.tests.test_management_apikey_api import build_app, session_headers

PATHS = {"/v1/messages": "anthropic", "/v1/chat/completions": "openai-chat", "/v1/responses": "openai-responses"}


def rule(model="glm-5.3", **kwargs):
    return {"model": model, "enabled": True, "preset": "quota_exhausted", "message": None, **kwargs}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    value = deepcopy(config.DEFAULT_CONFIG)
    value["apiKeys"] = {"a": {"key": "test-a", "modelBlocks": [rule()]}, "b": {"key": "test-b"}}
    value["apiKeyConcurrency"] = {"enabled": True, "defaultMaxConcurrent": 1, "defaultMaxQueue": 0}
    monkeypatch.setattr(config, "get", lambda: value)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    from src.model_registry import runtime
    from src.channel import registry
    monkeypatch.setattr(runtime, "_registry", None)
    monkeypatch.setattr(registry, "_channels", {})
    drain.reset_for_tests()
    apikey_limiter._slots.clear()
    yield value
    assert apikey_limiter._queued_body_bytes_total == 0
    assert apikey_limiter._queued_body_spool_bytes_total == 0
    apikey_limiter._slots.clear()
    drain.reset_for_tests()


@pytest.mark.parametrize("ingress", list(PATHS.values()))
def test_exact_isolation_mapping_and_presets(cfg, ingress, capsys):
    cfg["modelMapping"] = {"global": {"friend": "glm-5.3", "glm-5.3": "other"}}
    assert policy.check("a", "friend", ingress).status == 402
    log = capsys.readouterr().out
    assert "本地策略拦截" in log
    assert "no upstream, no retry" in log
    assert "test-a" not in log
    assert policy.check("a", "glm-5.3", ingress)
    assert policy.check("a", "other", ingress) is None  # Don't map the rule to 'other'.
    assert policy.check("a", "glm-5.30", ingress) is None
    assert policy.check("b", "friend", ingress) is None
    assert policy.check("a", "friend[1m]", ingress)
    cfg["apiKeys"]["a"]["modelBlocks"][0]["enabled"] = False
    assert policy.check("a", "friend", ingress) is None
    for preset, (status, message) in policy.PRESETS.items():
        cfg["apiKeys"]["a"]["modelBlocks"] = [rule(preset=preset)]
        denial = policy.check("a", "friend", ingress)
        assert (denial.status, denial.message, denial.code) == (status, message, preset)
        response = denial.http_response(ingress)
        assert response.headers["x-should-retry"] == "false"
        assert "retry-after" not in response.headers
    cfg["apiKeys"]["a"]["modelBlocks"] = [rule(message="Model access is exhausted.")]
    assert policy.check("a", "friend", ingress).message == "Model access is exhausted."


def test_registry_canonical_names(cfg, monkeypatch):
    from src.model_registry import runtime
    from src.model_registry.pipeline import build
    from src.model_registry.offers import Offer
    registry = build([Offer(source="oauth:zhipu:test", provider="zhipu", wire="GLM-5.3", name="GLM-5.3", basis="name")])
    monkeypatch.setattr(runtime, "_registry", registry)
    assert policy.check("a", "GLM-5.3", "openai-chat")
    assert policy.check("a", "GLM-5.30", "openai-chat") is None


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("saturated", [False, True])
async def test_http_admission_zero_side_effects(cfg, monkeypatch, path, stream, saturated):
    from src import failover, scheduler, log_db, cooldown, notifier, scorer
    forbidden = Mock(side_effect=AssertionError("local denial entered upstream machinery"))
    for mod, name in [(scheduler, "schedule"), (failover, "run_failover"), (log_db, "insert_pending"),
                      (notifier, "notify_event"), (cooldown, "record_error"), (scorer, "record_failure")]:
        monkeypatch.setattr(mod, name, forbidden)
    # Saturated requests must win over 429 without enqueueing; immediately
    # admitted requests are denied by the normal handler, not a bounded pre-read.
    lease = await apikey_limiter.acquire("a") if saturated else None
    monkeypatch.setattr(apikey_limiter, "_wait_for_turn_or_abort", forbidden)
    if not saturated:
        cfg["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] = 8
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            for _ in range(2):  # Even a forcibly repeated client request stays local.
                response = await client.post(path, headers={"authorization": "Bearer test-a"},
                                             json={"model": "glm-5.3", "stream": stream})
                assert response.status_code == 402
                assert response.headers["content-type"] == "application/json"
                assert response.json()["error"]["code"] == "quota_exhausted"
                assert response.headers["x-should-retry"] == "false"
                assert "retry-after" not in response.headers
        forbidden.assert_not_called()
        assert apikey_limiter.key_snapshot("a")["in_flight"] == int(saturated)
        assert apikey_limiter.key_snapshot("a")["waiting"] == 0
    finally:
        if lease is not None:
            await lease.release()


@pytest.mark.parametrize("path,ingress", PATHS.items())
async def test_handler_defense_without_middleware(cfg, monkeypatch, path, ingress):
    from src.openai.handler import handle
    from src import scheduler
    fail = Mock(side_effect=AssertionError("scheduled"))
    monkeypatch.setattr(scheduler, "schedule", fail)
    raw = json.dumps({"model": "glm-5.3", "stream": True}).encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": path,
                       "headers": [(b"authorization", b"Bearer test-a")]}, receive)
    result = await server.proxy_messages(request) if ingress == "anthropic" else await handle(request, ingress_protocol="chat" if ingress == "openai-chat" else "responses")
    assert result.status_code == 402
    fail.assert_not_called()


def echo_app():
    app = FastAPI()
    app.add_middleware(server._DrainHttpMiddleware)
    @app.post("/v1/messages")
    @app.post("/v1/responses")
    @app.post("/v1/chat/completions")
    async def echo(request: Request):
        return {"raw": (await request.body()).decode()}
    return app


@pytest.mark.parametrize("key,model,disabled", [("a", "other", False), ("b", "glm-5.3", False), ("a", "glm-5.3", True)])
async def test_unmatched_body_replay(cfg, key, model, disabled):
    cfg["apiKeys"]["a"]["modelBlocks"][0]["enabled"] = not disabled
    cfg["apiKeyConcurrency"]["queuedBodySpoolThresholdBytes"] = 1
    raw = json.dumps({"model": model, "messages": [{"role": "user", "content": "hello"}]})
    async def chunks():
        for chunk in [raw[:9], raw[9:20], raw[20:]]:
            yield chunk.encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app()), base_url="http://test") as client:
        result = await client.post("/v1/chat/completions", headers={"authorization": f"Bearer test-{key}"}, content=chunks())
    assert result.status_code == 200
    assert result.json()["raw"] == raw


async def test_unmatched_queued_replay_and_cleanup(cfg):
    cfg["apiKeyConcurrency"].update(defaultMaxQueue=1, defaultQueueWaitSeconds=3, queuedBodySpoolThresholdBytes=1)
    lease = await apikey_limiter.acquire("a")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app()), base_url="http://test") as client:
        raw = '{"model":"other","input":"preserve every byte"}'
        pending = asyncio.create_task(client.post("/v1/chat/completions", headers={"authorization": "Bearer test-a"}, content=raw))
        for _ in range(100):
            if apikey_limiter.key_snapshot("a")["waiting"]:
                break
            await asyncio.sleep(.001)
        assert apikey_limiter.key_snapshot("a")["waiting"] == 1
        await lease.release()
        response = await pending
        assert response.json()["raw"] == raw


@pytest.mark.parametrize("body,limit", [(b'{"model":"glm-5.3"}', 8), (b'x' * 200, 100)])
@pytest.mark.parametrize("queue_size", [0, 1])
async def test_saturated_preflight_body_limit(cfg, body, limit, queue_size):
    cfg["apiKeyConcurrency"].update(defaultMaxRequestBodyBytes=limit, defaultMaxQueue=queue_size)
    lease = await apikey_limiter.acquire("a")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            response = await client.post("/v1/chat/completions", headers={"authorization": "Bearer test-a"}, content=body)
        assert response.status_code == 413
        assert apikey_limiter.key_snapshot("a")["waiting"] == 0
    finally:
        await lease.release()


@pytest.mark.parametrize("value", [None, {}, [{"model": ""}], [rule(enabled="false")], [rule(preset="oops")],
                                     [rule(status=429)], [rule(status=503)], [rule(message="x" * 1001)],
                                     [rule(), rule()], [rule(model="x" * 257)]])
def test_schema_rejects_invalid_rules(value):
    with pytest.raises(ValueError):
        ApiKeyUpdateRequest(modelBlocks=value)


def test_management_revision_roundtrip_and_normalize(tmp_path):
    app, runtime, _, store, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    path = "/api/management/v1/api-keys/alpha"
    with TestClient(app) as client:
        original = client.get(path, headers=headers).json()["data"]
        assert original["modelBlocks"] == []
        updated = client.patch(path, headers={**headers, "If-Match": original["revision"]}, json={"modelBlocks": [rule()]})
        assert updated.status_code == 200, updated.text
        data = updated.json()["data"]
        assert data["modelBlocks"] == [rule()]
        assert data["revision"] != original["revision"]
        assert client.get(path, headers=headers).json()["data"]["modelBlocks"] == [rule()]
        assert client.patch(path, headers={**headers, "If-Match": original["revision"]}, json={"modelBlocks": []}).status_code == 409
        assert client.patch(path, headers={**headers, "If-Match": data["revision"]}, json={"allowImages": True}).status_code == 200
        assert store.get()["apiKeys"]["alpha"]["modelBlocks"] == [rule()]
        fresh = client.get(path, headers=headers).json()["data"]
        assert client.patch(path, headers={**headers, "If-Match": fresh["revision"]}, json={"modelBlocks": []}).status_code == 200
        assert store.get()["apiKeys"]["alpha"]["modelBlocks"] == []
    candidate = {"apiKeys": {"a": {"key": "synthetic", "modelBlocks": [{"model": " glm-5.3 "}]}}}
    config._normalize_api_keys(candidate)
    assert candidate["apiKeys"]["a"]["modelBlocks"] == [rule()]
    candidate["apiKeys"]["a"]["modelBlocks"][0]["status"] = 429
    with pytest.raises(ValueError):
        config._normalize_api_keys(candidate)


@pytest.mark.parametrize("registry_enabled", [False, True])
def test_source_aliases_do_not_resolve_unrelated_mapping(cfg, monkeypatch, registry_enabled):
    from src.channel import registry
    from src.channel.api_channel import ApiChannel
    channel = ApiChannel({"name": "fixture", "protocol": "anthropic", "models": [
        {"alias": "friend", "real": "glm-5.3"}, {"alias": "other", "real": "other-real"},
    ]})
    monkeypatch.setattr(registry, "_channels", {channel.key: channel})
    if registry_enabled:
        from src.model_registry import runtime
        from src.model_registry.pipeline import build
        from src.model_registry.offers import Offer
        unified = build([Offer(source=channel.key, provider="api", wire=m["real"], name=m["alias"], basis="name")
                         for m in channel.models])
        monkeypatch.setattr(runtime, "_registry", unified)
    assert policy.check("a", "friend", "anthropic")
    cfg["modelMapping"] = {"global": {"friend": "other"}}
    assert policy.check("a", "friend", "anthropic") is None
    assert policy.check("a", "other", "anthropic") is None
    if registry_enabled:
        return
    # Provider-specific spelling is not a blanket lowercasing of all API names.
    channel.provider = "zhipu"
    channel.key = "oauth:zhipu:fixture"
    channel.supports_model = lambda model: "glm-5.3" if model.casefold() == "glm-5.3" else None
    assert policy.check("a", "GLM-5.3", "anthropic")
    cfg["apiKeys"]["a"]["modelBlocks"] = [rule("GLM-5.3")]
    assert policy.check("a", "glm-5.3", "anthropic")


def test_registry_alias_mapped_elsewhere_is_not_false_positive(cfg, monkeypatch):
    from src.model_registry import runtime
    from src.model_registry.pipeline import build
    from src.model_registry.offers import Offer
    unified = build([Offer(source="api:test", provider="api", wire="glm-5.3", name="glm-5.3", basis="name")],
                    aliases={"friend": {"model": "glm-5.3"}})
    monkeypatch.setattr(runtime, "_registry", unified)
    assert policy.check("a", "friend", "openai-chat")
    cfg["modelMapping"] = {"global": {"friend": "other"}}
    assert policy.check("a", "friend", "openai-chat") is None


async def test_model_listing_does_not_hide_blocked_model(cfg, monkeypatch):
    from src.channel import registry
    from src.channel.api_channel import ApiChannel
    ch = ApiChannel({"name": "fixture", "protocol": "anthropic", "models": [
        {"alias": "glm-5.3", "real": "glm-5.3"}, {"alias": "other", "real": "other"}]})
    monkeypatch.setattr(registry, "_channels", {ch.key: ch})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        before = await client.get("/v1/models", headers={"authorization": "Bearer test-a"})
        cfg["apiKeys"]["a"]["modelBlocks"] = []
        after = await client.get("/v1/models", headers={"authorization": "Bearer test-a"})
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()
    assert "glm-5.3" in [m["id"] for m in before.json()["data"]]


@pytest.mark.parametrize("mode", ["disconnect", "cancel", "event-limit", "invalid-json"])
async def test_preflight_receive_safety(cfg, mode):
    cfg["apiKeyConcurrency"].update(queuedBodySpoolThresholdBytes=1, defaultMaxRequestBodyEvents=1 if mode == "event-limit" else 10)
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "query_string": b"",
             "headers": [(b"authorization", b"Bearer test-a")]}
    first = True
    reached = asyncio.Event()
    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b'{"model":', "more_body": mode != "invalid-json"}
        reached.set()
        if mode == "cancel":
            await asyncio.Event().wait()
        if mode == "event-limit":
            return {"type": "http.request", "body": b'"glm-5.3"}', "more_body": False}
        return {"type": "http.disconnect"}
    sent = []
    async def send(message):
        sent.append(message)
    app = echo_app()
    lease = await apikey_limiter.acquire("a")
    try:
        if mode == "disconnect":
            await app(scope, receive, send)
            assert sent == []
        elif mode == "cancel":
            task = asyncio.create_task(app(scope, receive, send))
            await reached.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await app(scope, receive, send)
            assert sent[0]["status"] == (413 if mode == "event-limit" else 429)
        assert apikey_limiter.key_snapshot("a")["in_flight"] == 1
        assert apikey_limiter.key_snapshot("a")["waiting"] == 0
    finally:
        await lease.release()


@pytest.mark.parametrize("invalid", [[rule(status=429)], [rule(status=503)], [rule(), rule()], None])
def test_management_rejects_policy_without_writes(tmp_path, invalid):
    app, runtime, control, store, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    with TestClient(app) as client:
        before = deepcopy(store.get())
        response = client.patch("/api/management/v1/api-keys/alpha", headers=headers, json={"modelBlocks": invalid})
        assert response.status_code == 422
        assert store.get() == before
    from src.management_auth import Capability
    from src.management_control import ManagementError
    from src.tests.test_management_apikey_control import context
    with pytest.raises(ManagementError):
        control.update_api_key(context(Capability.WRITE), "alpha", changes={"model_blocks": invalid})
    assert store.get() == before


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mode", ["free-slot", "unlimited", "disabled", "key-disabled"])
@pytest.mark.parametrize("transfer", ["content-length", "chunked"])
async def test_unmatched_nonqueued_requests_ignore_queue_body_budgets(cfg, monkeypatch, path, mode, transfer):
    # A is blocked; B must keep its original nonqueued semantics, regardless of
    # body size/events or the queue's per-key/process memory and disk budgets.
    limits = cfg["apiKeyConcurrency"]
    limits.update(defaultMaxRequestBodyBytes=8, defaultMaxRequestBodyEvents=1,
                  defaultMaxQueuedBodyBytesPerKey=1, maxQueuedBodyBytes=1,
                  queuedBodySpoolThresholdBytes=1,
                  defaultMaxQueuedBodySpoolBytesPerKey=1, maxQueuedBodySpoolBytes=1)
    if mode == "unlimited":
        limits["defaultMaxConcurrent"] = 0
    elif mode == "disabled":
        limits["enabled"] = False
    elif mode == "key-disabled":
        cfg["apiKeys"]["a"]["limits"] = {"enabled": False}
    forbidden = Mock(side_effect=AssertionError("nonqueued request allocated a body guard"))
    monkeypatch.setattr(apikey_limiter, "_body_preserving_receive", forbidden)
    raw = json.dumps({"model": "other", "input": "unchanged" * 100})
    async def chunks():
        for offset in range(0, len(raw), 7):
            yield raw[offset:offset + 7].encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app()), base_url="http://test") as client:
        response = await client.post(path, headers={"authorization": "Bearer test-a"},
                                     content=chunks() if transfer == "chunked" else raw)
    assert response.status_code == 200
    assert response.json()["raw"] == raw
    forbidden.assert_not_called()
    assert apikey_limiter.key_snapshot("a")["in_flight"] == 0


@pytest.mark.parametrize("budget", ["body", "events", "memory", "spool"])
async def test_unmatched_saturated_requests_keep_queue_safety(cfg, budget):
    limits = cfg["apiKeyConcurrency"]
    limits.update(defaultMaxQueue=1, defaultQueueWaitSeconds=2)
    if budget == "body":
        limits["defaultMaxRequestBodyBytes"] = 8
    elif budget == "events":
        limits["defaultMaxRequestBodyEvents"] = 1
    elif budget == "memory":
        limits["maxQueuedBodyBytes"] = 1
    else:
        limits.update(queuedBodySpoolThresholdBytes=1, maxQueuedBodySpoolBytes=1)
    lease = await apikey_limiter.acquire("a")
    try:
        async def chunks():
            yield b'{"model":'
            yield b'"other","input":"unchanged"}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=echo_app()), base_url="http://test") as client:
            response = await client.post("/v1/responses", headers={"authorization": "Bearer test-a"}, content=chunks())
        assert response.status_code == (413 if budget in {"body", "events"} else 429), response.text
        if budget in {"memory", "spool"}:
            assert response.json()["error"]["code"] == "queued_body_capacity"
        assert apikey_limiter.key_snapshot("a")["in_flight"] == 1
        assert apikey_limiter.key_snapshot("a")["waiting"] == 0
    finally:
        await lease.release()


async def test_concurrent_admission_cannot_skip_saturated_policy_hook(cfg):
    calls = []
    async def check():
        calls.append(True)
        assert not slot.lock.locked()
        assert slot.in_flight == 1
        return None
    slot = await apikey_limiter._get_slot("a")
    await slot.lock.acquire()
    # Both requests arrive while the key has capacity. Exactly one can reserve
    # it; the other must observe saturation under lock and invoke the hook.
    first = asyncio.create_task(apikey_limiter.acquire("a", before_queue=check))
    second = asyncio.create_task(apikey_limiter.acquire("a", before_queue=check))
    await asyncio.sleep(0)
    slot.lock.release()
    results = await asyncio.gather(first, second, return_exceptions=True)
    leases = [result for result in results if isinstance(result, apikey_limiter.ApiKeyLease)]
    try:
        assert len(leases) == 1
        assert sum(isinstance(result, apikey_limiter.ApiKeyLimitError) for result in results) == 1
        assert len(calls) == 1
        assert slot.in_flight == 1 and slot.waiters == []
    finally:
        for lease in leases:
            await lease.release()


@pytest.mark.parametrize("refill", [False, True])
async def test_prequeue_hook_rechecks_capacity_without_holding_slot_lock(cfg, refill):
    active = await apikey_limiter.acquire("a")
    started, proceed = asyncio.Event(), asyncio.Event()
    count = 0
    async def check():
        nonlocal count
        count += 1
        started.set()
        await proceed.wait()
        return None
    pending = asyncio.create_task(apikey_limiter.acquire("a", before_queue=check))
    await asyncio.wait_for(started.wait(), 1)
    # Must be able to release while the policy reader awaits a slow client.
    await asyncio.wait_for(active.release(), 1)
    competing = await apikey_limiter.acquire("a") if refill else None
    admitted = None
    try:
        proceed.set()
        if refill:
            with pytest.raises(apikey_limiter.ApiKeyLimitError):
                await pending
        else:
            admitted = await pending
        assert count == 1
        assert apikey_limiter.key_snapshot("a")["in_flight"] == 1
        assert apikey_limiter.key_snapshot("a")["waiting"] == 0
    finally:
        if competing:
            await competing.release()
        if admitted:
            await admitted.release()


def test_rule_config_normalization_does_not_import_inference_or_management():
    import subprocess
    import sys

    # A fresh interpreter catches import cycles/dependencies masked by this
    # test module's already imported server. Inherits isolated test paths.
    subprocess.run([sys.executable, "-c", '''
import sys
from src.api_key_rules import validate_rules
assert "src.config" not in sys.modules
from src import config
candidate = {"apiKeys": {"legacy": {"key": "fixture-old"},
                          "configured": {"key": "fixture-new", "modelBlocks": [{"model": " model-a "}]}}}
config._normalize_api_keys(candidate)
assert "modelBlocks" not in candidate["apiKeys"]["legacy"]
assert candidate["apiKeys"]["configured"]["modelBlocks"] == validate_rules([{"model": "model-a"}])
assert "src.api_key_policy" not in sys.modules
assert not any(name.startswith("src.management_") for name in sys.modules)
'''], check=True)
