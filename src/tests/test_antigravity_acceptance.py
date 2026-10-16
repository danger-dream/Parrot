"""Acceptance regressions from independent AG review; real ASGI, fake network."""
from __future__ import annotations

import base64
import copy
import gzip
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest

from src.tests.test_antigravity_oauth import _import_modules, _add_ag_account
from src.tests.test_antigravity_compatibility import (
    clean, official_catalog, _body, _tool_body, _inventory, _settled_attempt, BASE, MODEL, PNG,
)
from src.tests.test_antigravity_quota_scope import _discover
from src.openai.transform.guard import GuardError

PATHS = {"responses": "/v1/responses", "chat": "/v1/chat/completions", "anthropic": "/v1/messages"}


@pytest.fixture
async def rig(m, monkeypatch):
    from src import scheduler, upstream, log_db, scorer, cooldown
    from src.transports import http_runtime
    import server
    om = m["oauth_manager"]
    key = _add_ag_account(om, models=[BASE + "-low", MODEL, BASE + "-high"])
    ch = m["AntigravityOAuthChannel"](om.get_account(key))
    ch.tls_fingerprint = None
    m["config"].update(lambda cfg: cfg.update(
        apiKeys={"review": {"key": "synthetic-review-key", "allowedModels": []}}, modelMapping={},
    ))
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(scheduler.registry, "get_channel", lambda key: ch if key == ch.key else None)
    monkeypatch.setattr(http_runtime, "_resolve_http_route_chain", lambda *a, **k: ([("direct", None)], None))
    seen, tokens, failures = [], [], []
    monkeypatch.setattr(scorer, "record_failure", lambda *a, **k: failures.append((a, k)))
    monkeypatch.setattr(cooldown, "record_error", lambda *a, **k: failures.append((a, k)))
    async def token(*a, **k):
        tokens.append(True)
        return "synthetic-access"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    def transport(request):
        seen.append((str(request.url), json.loads(request.content)))
        payload = {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1, "totalTokenCount": 11}}}
        if "streamGenerateContent" in str(request.url):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: " + json.dumps(payload) + "\n\n")
        return httpx.Response(200, json=payload)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(upstream, "_client_pool", upstream._client_pool)
    upstream.set_client(client)
    log_db.init()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
                                     base_url="http://review.test", headers={"Authorization": "Bearer synthetic-review-key"}) as api:
            yield api, ch, seen, tokens, failures
    finally:
        await client.aclose()


@pytest.mark.parametrize("case,status", [("http", 400), ("port", 400), ("safety", 400), ("dns", 502), ("timeout", 502), ("connect", 502)])
@pytest.mark.asyncio
async def test_remote_image_rejections_do_not_score_model_accounts(rig, monkeypatch, case, status):
    from src.providers import remote_image
    api, ch, seen, tokens, failures = rig
    url = "http://example.invalid/image" if case == "http" else "https://example.invalid:bad/image" if case == "port" else "https://images.example/pixel"
    if case not in {"http", "port"}:
        errors = {
            "safety": remote_image.RemoteImageError("input_image URL resolves to a non-public address"),
            "dns": remote_image.RemoteImageDownloadError("input_image hostname could not be resolved"),
            "timeout": httpx.ReadTimeout("external image timeout"),
            "connect": httpx.ConnectError("external image connect"),
        }
        async def download(url): raise errors[case]
        monkeypatch.setattr(remote_image, "download_https_image", download)
    response = await api.post(PATHS["responses"], json=_tool_body("responses", url))
    assert response.status_code == status, response.text
    assert not seen and not tokens and not failures


@pytest.mark.asyncio
async def test_shared_downloader_distinguishes_fetch_from_safety_without_guard_semantics():
    from src.providers import remote_image
    from src.tests.test_antigravity_package_a import FakeClient, FakeResponse, public_resolver
    def failed_dns(*a): raise socket.gaierror("fixture DNS failure")
    with pytest.raises(remote_image.RemoteImageDownloadError):
        await remote_image.download_https_image("https://images.example/pixel", resolver=failed_dns, client=FakeClient([]))
    with pytest.raises(remote_image.RemoteImageDownloadError):
        await remote_image.download_https_image("https://images.example/pixel", resolver=public_resolver,
                                               client=FakeClient([FakeResponse(status=503)]))
    with pytest.raises(remote_image.RemoteImageError) as error:
        await remote_image.download_https_image("http://images.example/pixel", client=FakeClient([]))
    assert not isinstance(error.value, (remote_image.RemoteImageDownloadError, GuardError))


@pytest.mark.parametrize("phase", ["download", "token", "proxy_log"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_endpoint_change_at_async_preparation_boundaries_never_dispatches(m, monkeypatch, rig, phase, stream):
    from src import log_db
    from src.providers import remote_image
    api, ch, seen, tokens, failures = rig
    new = "https://new-endpoint.example/ag"
    def change():
        m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url=new))
    async def download(url):
        if phase == "download": change()
        return base64.b64decode(PNG), "image/png"
    monkeypatch.setattr(remote_image, "download_https_image", download)
    if phase == "token":
        async def token(*a, **k):
            tokens.append(True)
            change()
            return "synthetic-access"
        monkeypatch.setattr(m["oauth_manager"], "ensure_channel_token", token)
    if phase == "proxy_log":
        record = log_db.record_proxy_attempt
        def late_change(*a, **kw):
            change()
            return record(*a, **kw)
        monkeypatch.setattr(log_db, "record_proxy_attempt", late_change)
    body = _tool_body("responses", "https://images.example/pixel")
    body["stream"] = stream
    response = await api.post(PATHS["responses"], json=body)
    assert response.status_code == 400, response.text
    assert m["ag"].effective_request_base_url(m["oauth_manager"].get_account(ch.account_key)) == new
    assert not seen and not failures
    assert len(tokens) == (0 if phase == "download" else 1)


@pytest.mark.parametrize("changed", ["selected", "catalog", "source", "bare", "tier"])
@pytest.mark.asyncio
async def test_token_wait_rechecks_selected_models_source_and_both_model_identities(m, monkeypatch, rig, changed):
    api, ch, seen, tokens, failures = rig
    async def token(*a, **k):
        tokens.append(True)
        def mutate(cfg):
            account = cfg["oauthAccounts"][0]
            if changed == "selected": account["models"] = [BASE + "-low"]
            elif changed == "catalog":
                account["account_model_catalog"] = {
                    "antigravityGenerationBaseUrl": "https://other.example",
                    "models": [{"id": MODEL, "antigravitySources": [{"baseUrl": "https://other.example"}]}],
                }
            elif changed == "source": account["disabledModels"] = [BASE]
            else: cfg["modelCenter"]["disabledModels"] = [BASE if changed == "bare" else MODEL]
        m["config"].update(mutate)
        return "synthetic-access"
    monkeypatch.setattr(m["oauth_manager"], "ensure_channel_token", token)
    response = await api.post(PATHS["responses"], json=_body("responses", BASE, "medium"))
    assert response.status_code == 400, response.text
    assert tokens and not seen and not failures


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.asyncio
async def test_bare_disable_after_scheduling_cannot_be_bypassed_by_private_fields(m, monkeypatch, rig, ingress, alias):
    from src import scheduler
    api, ch, seen, tokens, failures = rig
    if alias:
        m["config"].update(lambda cfg: cfg.update(modelMapping={"global": {"review-alias": BASE}}))
    schedule = scheduler.schedule
    def after_schedule(*a, **kw):
        route = schedule(*a, **kw)
        assert route.candidates == [(ch, MODEL)]
        m["config"].update(lambda cfg: cfg["modelCenter"].update(disabledModels=[BASE]))
        return route
    monkeypatch.setattr(scheduler, "schedule", after_schedule)
    body = _body(ingress, "review-alias" if alias else BASE, "medium")
    body.update(_client_visible_model=MODEL, _antigravity_requested_models=["safe"], _parrot_original_model="safe")
    response = await api.post(PATHS[ingress], json=body)
    assert response.status_code == 400, response.text
    assert not seen and not tokens and not failures


@pytest.mark.parametrize("boundary", ["httpcore", "trace_await", "curl"])
@pytest.mark.asyncio
async def test_last_transport_handoff_validates_after_connection_and_trace_await(m, monkeypatch, rig, boundary):
    from src.channel.antigravity_oauth_channel import _guarded_stream_context
    from src.transports.http import HttpStreamRequest
    api, ch, seen, tokens, failures = rig
    def change():
        m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url="https://new.example"))
    async def trace(name, info):
        if boundary == "trace_await": change()
    extensions = {"trace": trace}
    if boundary == "curl": extensions["parrot_transport_lifecycle"] = {"mark_connection_complete": lambda: None}
    request = HttpStreamRequest("POST", ch.base_url, {}, b"{}", 10, 30, extensions=extensions)
    @asynccontextmanager
    async def physical(req):
        if boundary != "trace_await": change()
        if boundary == "curl":
            req.extensions["parrot_before_native_handoff"]()
            req.extensions["parrot_transport_lifecycle"]["mark_connection_complete"]()
        else: await req.extensions["trace"]("http2.send_request_headers.started", {})
        seen.append("physical send")
        yield object()
    with pytest.raises(GuardError):
        async with _guarded_stream_context(request, physical, lambda: ch.validate_dispatch(MODEL, (BASE,))): pass
    assert not seen


@pytest.mark.parametrize("boundary", ["httpcore", "curl"])
@pytest.mark.asyncio
async def test_dispatch_guard_does_not_reject_already_sent_response(m, rig, boundary):
    from src.channel.antigravity_oauth_channel import _guarded_stream_context
    from src.transports.http import HttpStreamRequest
    api, ch, seen, tokens, failures = rig
    async def trace(name, info): pass
    extensions = {"trace": trace}
    if boundary == "curl": extensions["parrot_transport_lifecycle"] = {"mark_connection_complete": lambda: None}
    request = HttpStreamRequest("POST", ch.base_url, {}, b"{}", 10, 30, extensions=extensions)
    @asynccontextmanager
    async def physical(req):
        if boundary == "curl":
            req.extensions["parrot_before_native_handoff"]()
            req.extensions["parrot_transport_lifecycle"]["mark_connection_complete"]()
        else: await req.extensions["trace"]("http2.send_request_headers.started", {})
        seen.append("sent before switch")
        m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url="https://new.example"))
        # curl signals dispatch retrospectively when it obtains response headers.
        event = "http2.send_request_headers.started" if boundary == "curl" else "http2.receive_response_headers.complete"
        await req.extensions["trace"](event, {})
        yield "OK"
    async with _guarded_stream_context(request, physical, lambda: ch.validate_dispatch(MODEL, (BASE,))) as response:
        assert response == "OK"
    assert seen == ["sent before switch"]


@pytest.mark.parametrize("active", ["daily", "prod"])
@pytest.mark.parametrize("capability", [False, True])
@pytest.mark.asyncio
async def test_effective_active_capabilities_survive_auto_binding_and_price_refresh(m, monkeypatch, tmp_path, official_catalog, active, capability):
    from src import model_metadata, model_pricing
    om, ag = m["oauth_manager"], m["ag"]
    base = ag.daily_api_base_url() if active == "daily" else ag.api_base_url()
    key = _add_ag_account(om, models=[MODEL], request_base_url=base)
    scope = "oauth:" + key
    def record(value, limit):
        return {"supportsThinking": value, "supportsImages": value, "maxTokens": limit,
                "maxOutputTokens": limit // 50, "inputModalities": ["text", "image"] if value else ["text"]}
    expected = record(capability, 2000000)
    side = "prod" if active == "daily" else "daily"
    assert (await _discover(m, monkeypatch, key, {
        active: {"models": {MODEL: expected}}, side: {"models": {MODEL: record(not capability, 3000000)}},
    }))["action"] == "updated"
    payload = copy.deepcopy(official_catalog)
    raw = payload["api"]["google"]["models"][BASE]
    raw["reasoning"] = not capability
    raw["modalities"]["input"] = ["text"] if capability else ["text", "image"]
    def publish():
        with gzip.open(tmp_path / "models_dev_catalog.json.gz", "wt") as stream: json.dump(payload, stream)
        assert model_pricing.reload_local_catalog()
        model_metadata.auto_sync_metadata(_inventory([MODEL], scope=scope))
    def check():
        binding = model_metadata.resolve_binding(MODEL, scope_key=scope, outbound_model=MODEL)
        metadata = binding.metadata
        assert metadata["reasoning"] is metadata["supportsThinking"] is capability
        assert metadata["vision"] is metadata["supportsImages"] is capability
        assert metadata["contextWindow"] == expected["maxTokens"]
        assert metadata["maxOutputTokens"] == expected["maxOutputTokens"]
        assert metadata["inputModalities"] == expected["inputModalities"]
        assert binding.value_source["reasoning"] == "account-native"
        price, ledger = _settled_attempt(MODEL, scope=scope)
        assert price.pricing_key == "google/" + BASE and ledger["cost_source"] == "estimated"
        return price
    publish()
    old = check()
    raw["cost"]["input"] *= 2
    raw["cost"]["input_audio"] *= 2
    publish()
    new = check()
    assert new.tariff.input_per_token == 2 * old.tariff.input_per_token
    assert new.source_revision != old.source_revision
    # Existing explicit operator overrides still win within native hard ceilings.
    model_metadata.patch_override_fields(MODEL, scope_key=scope, outbound_model=MODEL,
                                         set_fields={"maxOutputTokens": 1000, "vision": False, "cost.input": 7.0})
    after = model_metadata.get_metadata(MODEL, scope_key=scope, outbound_model=MODEL)
    assert after["maxOutputTokens"] == 1000 and after["vision"] is after["supportsImages"] is False
    assert _settled_attempt(MODEL, scope=scope)[0].tariff.input_per_token == 7 / 1000000


@pytest.mark.asyncio
async def test_unknown_and_inapplicable_native_fields_do_not_invent_capabilities_or_override_manual_binding(m, monkeypatch, official_catalog):
    from src import model_metadata
    om = m["oauth_manager"]
    key = _add_ag_account(om, models=[MODEL])
    scope = "oauth:" + key
    # Side-only false is never treated as an active capability.
    await _discover(m, monkeypatch, key, {"daily": {"models": {MODEL: {}}}, "prod": {"models": {MODEL: {"supportsThinking": False}}}})
    model_metadata.auto_sync_metadata(_inventory([MODEL], scope=scope))
    assert model_metadata.get_metadata(MODEL, scope_key=scope, outbound_model=MODEL)["reasoning"] is True
    await _discover(m, monkeypatch, key, {"daily": {"models": {MODEL: {"supportsThinking": False}}}, "prod": {"models": {MODEL: {}}}})
    assert model_metadata.get_metadata(MODEL, scope_key=scope, outbound_model=MODEL)["reasoning"] is False
    # Explicit manual metadata binding retains its existing priority.
    model_metadata.set_binding(MODEL, "google/" + BASE, scope_key=scope, outbound_model=MODEL, source="manual")
    manual = model_metadata.get_metadata(MODEL, scope_key=scope, outbound_model=MODEL)
    assert manual["reasoning"] is manual["supportsThinking"] is True
    model_metadata.delete_binding(MODEL, scope_key=scope)
    m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url="https://other.example"))
    assert model_metadata.get_metadata(MODEL, scope_key=scope, outbound_model=MODEL)["reasoning"] is True
