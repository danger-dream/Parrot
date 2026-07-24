"""xAI Imagine OAuth relay and model-selected image routing tests."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os as _ap_os
import sys as _ap_sys
from pathlib import Path
from typing import Any

_ap_sys.path.insert(
    0,
    _ap_os.path.dirname(
        _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))
    ),
)
from src.tests import _isolation

_isolation.isolate()

import httpx
import pytest
from fastapi import FastAPI, Request

from src import (
    apikey_limiter,
    concurrency,
    config,
    cooldown,
    image_db,
    media_db,
    oauth_manager,
    state_db,
)
from src.channel import registry
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.openai import images_openai_compat
from src.xai import imagine


class _AsgiClient:
    def __init__(self, app: FastAPI):
        self.app = app

    def request(self, method: str, path: str, **kwargs):
        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)


def _build_app() -> FastAPI:
    app = FastAPI()

    async def image_generation(request: Request):
        return await images_openai_compat.handle_generations(request)

    async def image_edit(request: Request):
        return await images_openai_compat.handle_edits(request)

    async def video_generation(request: Request):
        return await imagine.handle_video_create(request, action="generate")

    async def video_edit(request: Request):
        return await imagine.handle_video_create(request, action="edit")

    async def video_extension(request: Request):
        return await imagine.handle_video_create(request, action="extend")

    async def video_result(request: Request, request_id: str):
        return await imagine.handle_video_result(request, request_id)

    app.add_api_route("/v1/images/generations", image_generation, methods=["POST"])
    app.add_api_route("/v1/images/edits", image_edit, methods=["POST"])
    app.add_api_route("/v1/videos", video_generation, methods=["POST"])
    app.add_api_route("/v1/videos/generations", video_generation, methods=["POST"])
    app.add_api_route("/v1/videos/edits", video_edit, methods=["POST"])
    app.add_api_route("/v1/videos/extensions", video_extension, methods=["POST"])
    app.add_api_route("/v1/videos/{request_id}", video_result, methods=["GET"])
    return app


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    state_db.init()
    image_db.init()
    image_conn = image_db._get_conn()
    image_conn.execute("DELETE FROM image_attempt_logs")
    image_conn.execute("DELETE FROM image_call_logs")
    image_conn.commit()
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["stateDbPath"] = config.get().get("stateDbPath")
    cfg["channelSelection"] = "order"
    cfg["concurrency"] = {"enabled": False, "defaultMaxConcurrent": 0}
    cfg["apiKeyConcurrency"] = {"enabled": False}
    cfg["apiKeys"] = {
        "media-key": {
            "key": "sk-media",
            "enabled": True,
            "allowedModels": [],
            "allowImages": True,
            "allowVideos": True,
        },
        "other-key": {
            "key": "sk-other",
            "enabled": True,
            "allowedModels": [],
            "allowImages": True,
            "allowVideos": True,
        },
        "no-video": {
            "key": "sk-no-video",
            "enabled": True,
            "allowedModels": [],
            "allowImages": True,
            "allowVideos": False,
        },
    }
    cfg["images"]["cacheEnabled"] = False
    cfg["images"]["maxInputImageBytes"] = 4 * 1024 * 1024
    config._cache = cfg
    config._mtime = config._current_mtime()

    with registry._lock:
        registry._channels = {}
    concurrency._slots.clear()
    cooldown.clear_all()
    state_db.xai_video_job_delete()

    async def valid_token(account_key: str) -> str:
        return f"token-for-{account_key}"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    yield


def _install_channel(
    *,
    email: str = "imagine@example.test",
    subject: str = "subject-imagine",
) -> XAIOAuthChannel:
    account = {
        "provider": "xai",
        "email": email,
        "subject": subject,
        "enabled": True,
        "access_token": "not-read-by-tests",
        "refresh_token": "not-read-by-tests",
        "expired": "2999-01-01T00:00:00Z",
    }
    channel = XAIOAuthChannel(account)
    with registry._lock:
        registry._channels[channel.key] = channel
    return channel


def _headers(token: str = "sk-media") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class _FakePipelineResult:
    images = [{"b64_json": "R1BU", "output_format": "png"}]
    usage = None
    tool_model = "gpt-image-2"


# ── Shared image routes select by model ─────────────────────────────────────


def test_non_grok_image_model_keeps_existing_gpt_pipeline(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    async def unexpected_xai(*_args, **_kwargs):
        raise AssertionError("xAI media route must not handle GPT image models")

    monkeypatch.setattr(images_openai_compat, "_execute_pipeline", fake_pipeline)
    monkeypatch.setattr(imagine, "_request_upstream", unexpected_xai)

    response = _AsgiClient(_build_app()).post(
        "/v1/images/generations",
        headers=_headers(),
        json={"model": "gpt-image-1", "prompt": "draw a square"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "gpt-image-2"
    assert captured["action"] == "generate"


def test_grok_image_generation_routes_to_xai_and_keeps_batch(monkeypatch):
    channel = _install_channel()
    captured: dict[str, Any] = {}

    async def fake_request(ch, *, method, path, headers, body, model):
        captured.update(
            channel=ch,
            method=method,
            path=path,
            headers=headers,
            body=json.loads(body),
            model=model,
        )
        return httpx.Response(
            200,
            json={
                "data": [{"url": "https://imgen.x.ai/result.jpeg", "mime_type": "image/jpeg"}],
                "usage": {"cost_in_usd_ticks": 400000000},
            },
        )

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        "/v1/images/generations",
        headers=_headers(),
        json={
            "model": "grok-imagine-image",
            "prompt": "two blue dots",
            "n": 2,
            "size": "1792x1024",
            "quality": "high",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["url"] == "https://imgen.x.ai/result.jpeg"
    assert "parrot_warning" not in response.json()
    assert captured["channel"] is channel
    assert captured["method"] == "POST"
    assert captured["path"] == "/images/generations"
    assert captured["headers"]["authorization"].startswith("Bearer token-for-xai:")
    assert captured["body"] == {
        "model": "grok-imagine-image",
        "prompt": "two blue dots",
        "n": 2,
        "quality": "high",
        "aspect_ratio": "16:9",
        "resolution": "1k",
    }
    log = media_db.recent(1)[0]
    assert log["provider"] == "xai"
    assert log["media_type"] == "image"
    assert log["action"] == "generate"
    assert log["model"] == "grok-imagine-image"
    assert log["requested_count"] == 2
    assert log["image_count"] == 1
    assert log["cost_usd_ticks"] == 400_000_000
    assert log["account_key"] == channel.account_key


def test_grok_image_result_is_cached_for_media_log_view(monkeypatch, tmp_path):
    _install_channel()
    config._cache["images"].update({
        "cacheEnabled": True,
        "cachePath": str(tmp_path / "media-cache"),
        "cacheMaxBytes": 10 * 1024 * 1024,
    })

    async def fake_request(_channel, *, method, path, headers, body, model):
        return httpx.Response(
            200,
            json={
                "data": [{
                    "url": "https://imgen.x.ai/cached.jpeg",
                    "mime_type": "image/jpeg",
                }],
            },
        )

    async def fake_download(value, *, channel, model, max_bytes):
        assert value == "https://imgen.x.ai/cached.jpeg"
        return b"jpeg-result", "image/jpeg"

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    monkeypatch.setattr(imagine, "_download_xai_media", fake_download)
    response = _AsgiClient(_build_app()).post(
        "/v1/images/generations",
        headers=_headers(),
        json={"model": "grok-imagine-image", "prompt": "cached dot"},
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["url"] == "https://imgen.x.ai/cached.jpeg"
    log = media_db.recent(1)[0]
    paths = json.loads(log["cache_paths"])
    assert log["cached_images"] == 1
    assert log["image_bytes"] == len(b"jpeg-result")
    assert len(paths) == 1
    assert paths[0].endswith(".jpg")
    assert Path(paths[0]).read_bytes() == b"jpeg-result"


def test_unknown_grok_image_name_never_falls_back_to_gpt(monkeypatch):
    async def unexpected_pipeline(**_kwargs):
        raise AssertionError("unknown Grok image model must not use GPT")

    monkeypatch.setattr(images_openai_compat, "_execute_pipeline", unexpected_pipeline)
    response = _AsgiClient(_build_app()).post(
        "/v1/images/generations",
        headers=_headers(),
        json={"model": "grok-imagine-image-unknown", "prompt": "x"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"


def test_grok_image_edit_converts_multipart_to_xai_json(monkeypatch):
    _install_channel()
    captured: dict[str, Any] = {}

    async def fake_request(_channel, *, method, path, headers, body, model):
        captured.update(path=path, body=json.loads(body))
        return httpx.Response(200, json={"data": [{"url": "https://imgen.x.ai/edit.jpeg"}]})

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    files = [
        ("images[]", ("a.png", io.BytesIO(b"a" * 32), "image/png")),
        ("images[]", ("b.png", io.BytesIO(b"b" * 32), "image/png")),
    ]
    response = _AsgiClient(_build_app()).post(
        "/v1/images/edits",
        headers={"Authorization": "Bearer sk-media"},
        data={"model": "grok-imagine-image-quality", "prompt": "combine", "n": "1"},
        files=files,
    )

    assert response.status_code == 200, response.text
    assert captured["path"] == "/images/edits"
    assert len(captured["body"]["images"]) == 2
    assert all(
        item["url"].startswith("data:image/png;base64,")
        for item in captured["body"]["images"]
    )


def test_grok_image_edit_accepts_native_json_image_object(monkeypatch):
    _install_channel()
    captured: dict[str, Any] = {}

    async def fake_request(_channel, *, method, path, headers, body, model):
        captured.update(path=path, body=json.loads(body))
        return httpx.Response(200, json={"data": [{"url": "https://imgen.x.ai/edit.jpeg"}]})

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        "/v1/images/edits",
        headers=_headers(),
        json={
            "model": "grok-imagine-image-quality",
            "prompt": "add a hat",
            "image": {
                "url": "https://example.test/cat.png",
                "type": "image_url",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert captured["path"] == "/images/edits"
    assert captured["body"]["image"] == {"url": "https://example.test/cat.png"}


def test_grok_image_model_honors_api_key_model_allowlist(monkeypatch):
    _install_channel()
    config._cache["apiKeys"]["media-key"]["allowedModels"] = ["gpt-image-1"]

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("disallowed model must not reach xAI")

    monkeypatch.setattr(imagine, "_request_upstream", unexpected)
    response = _AsgiClient(_build_app()).post(
        "/v1/images/generations",
        headers=_headers(),
        json={"model": "grok-imagine-image", "prompt": "x"},
    )
    assert response.status_code == 403


# ── Video endpoints and persistent identity binding ─────────────────────────


@pytest.mark.parametrize("path", ["/v1/videos", "/v1/videos/generations"])
def test_video_generation_routes_and_persists_binding(monkeypatch, path):
    channel = _install_channel()
    captured: dict[str, Any] = {}

    async def fake_request(ch, *, method, path, headers, body, model):
        captured.update(channel=ch, method=method, path=path, body=json.loads(body))
        return httpx.Response(200, json={"request_id": "video-job-create"})

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        path,
        headers=_headers(),
        json={
            "model": "grok-imagine-video",
            "prompt": "a dot moves",
            "seconds": 1,
            "size": "720x1280",
            "input_reference": {"image_url": "https://example.test/start.png"},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"request_id": "video-job-create"}
    assert captured["channel"] is channel
    assert captured["path"] == "/videos/generations"
    assert captured["body"]["aspect_ratio"] == "9:16"
    assert captured["body"]["resolution"] == "720p"
    assert captured["body"]["image"] == {"url": "https://example.test/start.png"}
    assert "size" not in captured["body"]
    binding = state_db.xai_video_job_load("video-job-create")
    assert binding is not None
    assert binding["channel_key"] == channel.key
    assert binding["api_key_name"] == "media-key"
    log = media_db.get_by_upstream_request_id("video-job-create")
    assert log is not None
    assert log["provider"] == "xai"
    assert log["media_type"] == "video"
    assert log["status"] == "pending"
    assert log["media_duration_seconds"] == 1
    assert log["resolution"] == "720p"
    assert log["account_key"] == channel.account_key


@pytest.mark.parametrize(
    ("route", "expected_path"),
    [
        ("/v1/videos/edits", "/videos/edits"),
        ("/v1/videos/extensions", "/videos/extensions"),
    ],
)
def test_video_edit_and_extension_paths(monkeypatch, route, expected_path):
    _install_channel()
    captured: list[str] = []

    async def fake_request(_channel, *, method, path, headers, body, model):
        captured.append(path)
        return httpx.Response(200, json={"request_id": f"job-{len(captured)}"})

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        route,
        headers=_headers(),
        json={
            "model": "grok-imagine-video",
            "prompt": "change it",
            "video": {"url": "https://example.test/input.mp4"},
        },
    )
    assert response.status_code == 200, response.text
    assert captured == [expected_path]


def test_video_poll_reuses_bound_channel_and_owner_key(monkeypatch):
    channel = _install_channel()
    state_db.xai_video_job_save(
        "video-job-poll",
        channel_key=channel.key,
        api_key_name="media-key",
        model="grok-imagine-video",
        ttl_seconds=3600,
    )
    log_id = media_db.start_call(
        request_id="video-poll-local",
        api_key_name="media-key",
        provider="xai",
        media_type="video",
        action="generate",
        model="grok-imagine-video",
        prompt="a dot moves",
        media_duration_seconds=1,
    )
    media_db.finish_call(
        log_id,
        status="pending",
        account_key=channel.account_key,
        account_email=channel.email,
        upstream_request_id="video-job-poll",
        upstream_status="pending",
        expires_at=9_999_999_999,
    )
    captured: dict[str, Any] = {}

    async def fake_request(ch, *, method, path, headers, body, model):
        captured.update(channel=ch, method=method, path=path, model=model)
        return httpx.Response(
            200,
            json={
                "status": "done",
                "video": {"url": "https://vidgen.x.ai/result.mp4", "duration": 1},
                "model": model,
                "progress": 100,
                "usage": {"cost_in_usd_ticks": 500000000},
            },
        )

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    client = _AsgiClient(_build_app())
    response = client.get("/v1/videos/video-job-poll", headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert captured == {
        "channel": channel,
        "method": "GET",
        "path": "/videos/video-job-poll",
        "model": "grok-imagine-video",
    }
    log = media_db.get_by_upstream_request_id("video-job-poll")
    assert log is not None
    assert log["status"] == "success"
    assert log["upstream_status"] == "done"
    assert log["progress"] == 100
    assert log["cost_usd_ticks"] == 500_000_000
    assert media_db.count() == 1

    cross_key = client.get(
        "/v1/videos/video-job-poll",
        headers={"Authorization": "Bearer sk-other"},
    )
    assert cross_key.status_code == 404


def test_completed_grok_video_is_cached_once_for_media_log_view(monkeypatch, tmp_path):
    channel = _install_channel()
    config._cache["images"].update({
        "cacheEnabled": True,
        "cachePath": str(tmp_path / "media-cache"),
        "cacheMaxBytes": 10 * 1024 * 1024,
    })
    state_db.xai_video_job_save(
        "video-job-cached",
        channel_key=channel.key,
        api_key_name="media-key",
        model="grok-imagine-video",
        ttl_seconds=3600,
    )
    log_id = media_db.start_call(
        request_id="video-cached-local",
        api_key_name="media-key",
        provider="xai",
        media_type="video",
        action="generate",
        model="grok-imagine-video",
        prompt="cached video",
    )
    media_db.finish_call(
        log_id,
        status="pending",
        account_key=channel.account_key,
        account_email=channel.email,
        upstream_request_id="video-job-cached",
        upstream_status="pending",
        expires_at=9_999_999_999,
    )

    async def fake_request(_channel, *, method, path, headers, body, model):
        return httpx.Response(
            200,
            json={
                "status": "done",
                "video": {
                    "url": "https://vidgen.x.ai/cached.mp4",
                    "duration": 1,
                },
                "progress": 100,
            },
        )

    downloads = 0

    async def fake_download(value, *, channel, model, max_bytes):
        nonlocal downloads
        downloads += 1
        assert value == "https://vidgen.x.ai/cached.mp4"
        return b"mp4-result", "video/mp4"

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    monkeypatch.setattr(imagine, "_download_xai_media", fake_download)
    client = _AsgiClient(_build_app())
    assert client.get("/v1/videos/video-job-cached", headers=_headers()).status_code == 200
    assert client.get("/v1/videos/video-job-cached", headers=_headers()).status_code == 200

    log = media_db.get_by_upstream_request_id("video-job-cached")
    paths = json.loads(log["cache_paths"])
    assert downloads == 1
    assert log["cached_images"] == 1
    assert log["image_bytes"] == len(b"mp4-result")
    assert len(paths) == 1
    assert paths[0].endswith(".mp4")
    assert Path(paths[0]).read_bytes() == b"mp4-result"


def test_video_permission_and_model_guards(monkeypatch):
    _install_channel()

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("guarded request must not reach xAI")

    monkeypatch.setattr(imagine, "_request_upstream", unexpected)
    client = _AsgiClient(_build_app())

    denied = client.post(
        "/v1/videos/generations",
        headers=_headers("sk-no-video"),
        json={"model": "grok-imagine-video", "prompt": "x"},
    )
    assert denied.status_code == 403

    unsupported = client.post(
        "/v1/videos/generations",
        headers=_headers(),
        json={"model": "sora-2", "prompt": "x"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["param"] == "model"


def test_video_explicit_429_can_fail_over_and_binds_winner(monkeypatch):
    first = _install_channel(email="first@example.test", subject="subject-first")
    second = _install_channel(email="second@example.test", subject="subject-second")
    calls: list[str] = []

    async def fake_request(channel, *, method, path, headers, body, model):
        calls.append(channel.key)
        if channel is first:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={"request_id": "video-job-second"})

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        "/v1/videos/generations",
        headers=_headers(),
        json={"model": "grok-imagine-video", "prompt": "x", "duration": 1},
    )
    assert response.status_code == 200, response.text
    assert calls == [first.key, second.key]
    binding = state_db.xai_video_job_load("video-job-second")
    assert binding is not None and binding["channel_key"] == second.key


def test_video_ambiguous_timeout_is_not_retried(monkeypatch):
    first = _install_channel(email="first@example.test", subject="subject-first")
    _install_channel(email="second@example.test", subject="subject-second")
    calls: list[str] = []

    async def fake_request(channel, *, method, path, headers, body, model):
        calls.append(channel.key)
        raise httpx.ReadTimeout("ambiguous timeout")

    monkeypatch.setattr(imagine, "_request_upstream", fake_request)
    response = _AsgiClient(_build_app()).post(
        "/v1/videos/generations",
        headers=_headers(),
        json={"model": "grok-imagine-video", "prompt": "x", "duration": 1},
    )
    assert response.status_code == 504
    assert calls == [first.key]
    assert "not retried" in response.json()["error"]["message"]


# ── OAuth lifecycle, state TTL, and real route registration ─────────────────


def test_channel_media_headers_reuse_existing_token_helper(monkeypatch):
    channel = _install_channel()
    calls: list[str] = []

    async def token(account_key: str) -> str:
        calls.append(account_key)
        return "existing-access-token"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    headers = asyncio.run(channel.build_media_headers())
    assert calls == [channel.account_key]
    assert headers["authorization"] == "Bearer existing-access-token"
    assert headers["accept"] == "application/json"


def test_video_binding_expires_from_state_db():
    channel = _install_channel()
    state_db.xai_video_job_save(
        "video-job-expired",
        channel_key=channel.key,
        api_key_name="media-key",
        model="grok-imagine-video",
        ttl_seconds=1,
    )
    row = state_db.xai_video_job_load("video-job-expired")
    assert row is not None
    deleted = state_db.xai_video_job_cleanup(now=int(row["expires_at"]))
    assert deleted == 1
    assert state_db.xai_video_job_load("video-job-expired") is None


def test_server_registers_all_xai_video_routes():
    import server

    routes = {
        (
            getattr(route, "path", ""),
            tuple(sorted(getattr(route, "methods", set()) or set())),
        )
        for route in server.app.routes
    }
    assert ("/v1/videos", ("POST",)) in routes
    assert ("/v1/videos/generations", ("POST",)) in routes
    assert ("/v1/videos/edits", ("POST",)) in routes
    assert ("/v1/videos/extensions", ("POST",)) in routes
    assert ("/v1/videos/{request_id}", ("GET",)) in routes
    assert server._is_api_key_limited_http_request("GET", "/v1/videos/job-1") is True


def test_video_post_paths_have_nonqueued_body_guards():
    for path in (
        "/v1/videos",
        "/v1/videos/generations",
        "/v1/videos/edits",
        "/v1/videos/extensions",
    ):
        request = Request({"type": "http", "method": "POST", "path": path, "headers": []})
        assert apikey_limiter._requires_nonqueued_body_guard(request) is True
