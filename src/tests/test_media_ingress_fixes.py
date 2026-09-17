"""Correct-behavior regressions for media audit M1-M5 (M4 == CORE-14)."""
from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src import config, media_cache, media_db, model_metadata
from src.antigravity import images as ag_images
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.auxiliary.media import AntigravityMediaControl, XaiMediaControl
from src.management_control.models import ModelOwnerRef, ModelSourceType
from src.management_control.observability import MediaControl
from src.tests._config_isolation import isolated_config
from src.tests.management_auxiliary_support import bearer, build_auxiliary_app, create_session
from src.tests.test_model_center_media import (
    FakeConfig, FakeMediaGateway, FakeNetworkClient, FakeUpstreamResponse, admin_context,
)
from src.tests import test_openai_responses_ws as ws_fixtures
from src.tests.test_model_required_all_ingress import _request

pytestmark = pytest.mark.usefixtures("isolated_config")


@pytest.mark.parametrize("provider,kind", [("xai", "image"), ("xai", "video")])
def test_missing_media_rename_rejects_existing_target_without_write(provider, kind):
    cfg = FakeConfig({"xaiOAuth": {"imageModels": ["real-item"], "videoModels": ["real-item"]},
                      "antigravityOAuth": {"imageModels": ["real-item"]}})
    before = copy.deepcopy(cfg.value)
    ctx = admin_context()
    if provider == "xai":
        ctl = XaiMediaControl(config_gateway=cfg, media_gateway=FakeMediaGateway())
        extra = {"kind": kind}
    else:
        ctl = AntigravityMediaControl(config_gateway=cfg)
        extra = {"owner": ModelOwnerRef(ModelSourceType.GLOBAL)}
    with pytest.raises(ManagementError) as caught:
        ctl.rename_model(ctx, old_model_id="never-existed", new_model_id="real-item",
                         expected_revision=ctl.get_settings(ctx).revision, **extra)
    assert caught.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND
    assert cfg.updates == 0 and cfg.value == before


@pytest.mark.parametrize("provider,kind", [("xai", "image"), ("xai", "video")])
def test_missing_media_rename_http_is_404(provider, kind, tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    fixture.config.value["xaiOAuth"] = {"imageModels": ["real-item"], "videoModels": ["real-item"]}
    fixture.config.value["antigravityOAuth"] = {"imageModels": ["real-item"]}
    before = copy.deepcopy(fixture.config.value)
    base = "/api/management/v1"
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        settings = client.get(f"{base}/{provider}/media-settings", headers=headers).json()["data"]
        body = {"newModelId": "real-item"}
        if provider == "antigravity":
            body["owner"] = {"type": "global"}
        response = client.patch(f"{base}/{provider}/media-models/{kind}/never-existed", json=body,
                                headers={**headers, "If-Match": settings["revision"]})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert fixture.config.value == before








@pytest.mark.parametrize("mime,url,preferred,expected", [
    ("video/webm", "https://media.x.ai/file.mp4", "mp4", "webm"),
    ("image/webp; charset=binary", "https://media.x.ai/file.png", "jpg", "webp"),
    ("application/octet-stream", "https://media.x.ai/file.webm?x=.mp4", "mp4", "webm"),
    ("", "https://media.x.ai/file.PNG", "jpg", "png"),
    ("", "https://media.x.ai/file.jpeg", "png", "jpg"),
    ("", "https://media.x.ai/file.exe", "jpg", "jpg"),
])
def test_cache_extension_prefers_mime_then_url_then_default(mime, url, preferred, expected):
    assert media_cache.extension_for(media_type="image", mime=mime, source_url=url, preferred=preferred) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,extension,content_type", [("video", "webm", "video/webm"), ("image", "png", "image/png")])
async def test_xai_generic_mime_preserves_cache_and_download_format(monkeypatch, tmp_path, kind, extension, content_type):
    from src.xai import imagine
    cfg = {"cacheEnabled": True, "cachePath": str(tmp_path / "cache"),
           "cacheRetentionDays": 0, "cacheMaxBytes": 1024}
    monkeypatch.setattr(imagine, "_media_cache_settings", lambda media_kind: {**cfg, "_media_kind": media_kind})
    async def fake_download(*a, **k):
        return b"fixture-native-bytes", "application/octet-stream"
    monkeypatch.setattr(imagine, "_download_xai_media", fake_download)
    paths, _ = await imagine._cache_xai_results(
        [{"url": f"https://media.x.ai/fixture.{extension}"}], media_type=kind,
        action="generate", channel=SimpleNamespace(key="oauth:xai:fixture"), model="media")
    row = {"id": 1, "status": "success", "media_type": kind, "cache_paths": json.dumps(paths)}
    control = MediaControl(media_db=SimpleNamespace(get_log=lambda _: row),
                           config=SimpleNamespace(get=lambda: {"images": cfg}))
    artifact = control.artifacts(admin_context(), "1")[0]
    assert paths[0].endswith("." + extension) and artifact["contentType"] == content_type
    assert b"".join(control.download(admin_context(), "1", artifact["id"]).chunks) == b"fixture-native-bytes"


@pytest.fixture
def ws_modules(monkeypatch):
    m = ws_fixtures._import_modules()
    monkeypatch.setattr(m["registry"], "_channels", dict(m["registry"]._channels))
    cfg = ws_fixtures._setup(m)
    cfg["retry"] = {"transient": {"enabled": False}, "recovery": {"oauthRefresh": False}}
    return m, cfg


def _override(context=1000, output=10):
    return {"fields": {"contextWindow": context, "maxInputTokens": context, "maxOutputTokens": output}}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["ws", "sse"])
@pytest.mark.parametrize("queued", [False, True])
async def test_ws_oversized_output_is_clamped_and_dispatched(monkeypatch, ws_modules, transport, queued):
    m, cfg = ws_modules
    ch = ws_fixtures._make_channel(m, extra={"responsesWsUpstreamTransport": transport})
    if queued:
        from src.scheduler import ScheduleResult
        route = ScheduleResult(candidates=[], saturated=[(ch, "real-model")], affinity_hit=False, fp_query=None)
        monkeypatch.setattr(m["responses_ws"].scheduler, "schedule", lambda *a, **k: route)
        async def acquired(*a, **k):
            return ch.key, (ch, "real-model")
        monkeypatch.setattr(m["concurrency"], "acquire_from_candidates", acquired)
    cfg["modelMetadataOverrides"] = {"defaults": {"test-model": _override()}, "scoped": {}}
    frame = {"type": "response.create", "model": "test-model", "input": "hello", "max_output_tokens": 20}
    ws = ws_fixtures.FakeWebSocket(frame)
    upstream = ws_fixtures.FakeUpstreamWebSocket([])
    async def fake_connect(*a, **k):
        return upstream
    sent_http = []
    async def fake_http(**kwargs):
        sent_http.append(json.loads(kwargs["upstream_req"].body))
        return SimpleNamespace(error=m["responses_ws"]._WsAttemptResult(
            outcome="transport_error", error_detail="fixture stop before network",
        ))
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    monkeypatch.setattr(m["responses_ws"], "open_response_with_proxy_chain", fake_http)
    await m["responses_ws"].handle_responses_ws(ws)
    row = ws_fixtures._last_request_log(m)
    # The oversized output request is clamped to the route cap and genuinely
    # dispatched; the refusal seen downstream is the upstream's, not a local
    # pre-transport 400.
    if transport == "ws":
        assert len(upstream.sent) == 1
        sent = json.loads(upstream.sent[0])
        assert sent["max_output_tokens"] == 10 and "source=" not in sent.get("model", "")
    else:
        assert len(sent_http) == 1 and sent_http[0]["max_output_tokens"] == 10
    assert row["http_status"] in (500, 502, 503, 504)  # upstream refusal, not a local pre-transport 400


@pytest.mark.asyncio
async def test_ws_local_guard_still_tries_larger_candidate(monkeypatch, ws_modules):
    m, cfg = ws_modules
    small = ws_fixtures._make_channel(m, extra={"name": "small"})
    large = ws_fixtures._make_channel(m, extra={"name": "large"})
    cfg["channelSelection"] = "order"
    m["registry"]._channels = {ch.key: ch for ch in [small, large]}
    cfg["modelMetadataOverrides"] = {"defaults": {}, "scoped": {
        small.key: {"test-model": {**_override(output=10), "outboundModel": "real-model"}},
        large.key: {"test-model": {**_override(output=100), "outboundModel": "real-model"}},
    }}
    attempts = []
    sockets = []
    async def fake_connect(*a, **k):
        upstream = ws_fixtures.FakeUpstreamWebSocket([
            {"type": "response.created", "response": {"id": "fixture-response"}},
            {"type": "response.completed", "response": {"id": "fixture-response", "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ])
        sockets.append(upstream)
        return upstream
    original_try = m["responses_ws"]._try_ws_channel
    async def record_try(*a, **k):
        attempts.append(k["ch"].key)
        return await original_try(*a, **k)
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", fake_connect)
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", record_try)
    ws = ws_fixtures.FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello", "max_output_tokens": 20})
    await m["responses_ws"].handle_responses_ws(ws)
    # The small candidate's lower cap now clamps the request instead of
    # rejecting it, so it wins outright and no second candidate is tried.
    assert attempts == [small.key]
    assert len(sockets[0].sent) == 1
    assert json.loads(sockets[0].sent[0])["max_output_tokens"] == 10
    assert ws_fixtures._last_request_log(m)["status"] == "success"
    assert any(json.loads(frame).get("type") == "response.completed" for frame in ws.sent_texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("large_context", [1000000, 300000])
async def test_messages_small_candidate_does_not_preempt_large_wire_dispatch(monkeypatch, ws_modules, large_context):
    """CORE-14: public Messages -> real failover -> final wire guard -> fake HTTP."""
    import server
    m, cfg = ws_modules
    cfg["channelSelection"] = "order"
    channels = [m["OpenAIApiChannel"]({
        "name": name, "type": "api", "protocol": "openai-chat", "enabled": True,
        "baseUrl": f"https://{name}.invalid", "apiKey": "fixture",
        "models": [{"alias": "test-model", "real": "real-model"}],
    }) for name in ["small", "large"]]
    m["registry"]._channels = {ch.key: ch for ch in channels}
    cfg["modelMetadataOverrides"] = {"defaults": {}, "scoped": {
        ch.key: {"test-model": {**_override(context=size, output=20000), "outboundModel": "real-model"}}
        for ch, size in zip(channels, [300000, large_context])
    }}
    monkeypatch.setattr(server.auth, "validate", lambda _: ("ws-key", [], None))
    monkeypatch.setattr(server.token_counter, "count_request_tokens", lambda *a, **k: 310000)
    body = {"model": "test-model", "max_tokens": 10000, "messages": [{"role": "user", "content": "controlled 310k prompt"}]}
    route = server.scheduler.schedule(body, "ws-key", "127.0.0.1")
    assert [ch.key for ch, _ in route.candidates] == ["api:small", "api:large"]
    assert model_metadata.effective_request_budget("test-model", scope_key="api:large", outbound_model="real-model", request_shape=body).can_fit(310000) is (large_context == 1000000)
    sent = []
    def transport(request):
        sent.append((request.url.host, json.loads(request.content)))
        return httpx.Response(200, json={"id": "fixture-completion", "object": "chat.completion", "model": "real-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 310000, "completion_tokens": 1, "total_tokens": 310001}})
    monkeypatch.setattr(m["upstream"], "_client_pool", m["upstream"].SharedClientPool(m["upstream"]._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        m["upstream"].set_client(client)
        response = await server.proxy_messages(_request(body))
    row = ws_fixtures._last_request_log(m)
    attempts = ws_fixtures._retry_chain(m, row["request_id"])
    # No local input preemption: the first candidate is genuinely dispatched
    # (this mock transport trips the transport-activity bookkeeping, so
    # downstream behavior beyond the first dispatch is out of scope here).
    assert sent and sent[0][0] == "small.invalid"
    assert sent[0][1]["model"] == "real-model"
    assert [attempt["outcome"] for attempt in attempts] != ["candidate_guard", "candidate_guard"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_first", [True, False])
async def test_ws_local_guard_does_not_mask_actual_upstream_rate_limit(monkeypatch, ws_modules, guard_first):
    m, cfg = ws_modules
    cfg["channelSelection"] = "order"
    channels = [ws_fixtures._make_channel(m, extra={"name": name}) for name in ["local", "upstream"]]
    if not guard_first:
        channels.reverse()
    m["registry"]._channels = {ch.key: ch for ch in channels}
    async def fake_try(*a, **k):
        if k["ch"].key == "api:local":
            return m["responses_ws"]._WsAttemptResult(outcome="candidate_guard", http_status=400,
                error_detail="Requested output exceeds this route's effective maxOutputTokens: requested=20 max=10 source=api:local")
        return m["responses_ws"]._WsAttemptResult(outcome="http_error", http_status=429, error_detail="private upstream error")
    monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", fake_try)
    ws = ws_fixtures.FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hello"})
    await m["responses_ws"].handle_responses_ws(ws)
    assert ws.close_calls[-1][0] == 4429
    assert ws_fixtures._last_request_log(m)["http_status"] == 429
    assert "private upstream error" not in "".join(ws.sent_texts)
