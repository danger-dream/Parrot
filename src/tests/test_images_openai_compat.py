"""Image parser/legacy payload regression tests. Unified routing, exact counts, HTTP URLs, masks and failures are exercised with real pixels in test_images_unified.py. Run through src/tests/isolated_pytest.py."""

from __future__ import annotations

import base64
import asyncio
import io
import os as _ap_os
import sys as _ap_sys
from typing import Any

_ap_sys.path.insert(
    0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
)
from src.tests import _isolation
_isolation.isolate()

import pytest
import httpx
from fastapi import FastAPI, Request

from src import auth, config, errors, image_db
from src.openai import images_openai_compat as compat, images_runtime as runtime
from src.openai import images_simple


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _setup_module():
    image_db.init()
    cfg = config.get()
    cfg["apiKeys"] = {
        "test-key": {"key": "test-token", "allowImages": True, "allowedModels": []},
        "readonly-key": {"key": "readonly-token", "allowImages": False, "allowedModels": []},
    }
    cfg["images"] = {
        "enabled": True,
        "mainModel": "gpt-5.4-mini",
        "toolModel": "gpt-image-2",
        "cacheEnabled": False,
        "maxPromptChars": 4000,
        "maxInputImageBytes": 4 * 1024 * 1024,
        "requestTimeoutSeconds": 60,
    }


def _import_modules():
    return {}


# ── 桩：替代 _execute_pipeline ─────────────────────────────────────────────


class _FakePipelineResult:
    def __init__(self, images=None, usage=None, tool_model="gpt-image-2"):
        self.images = images or [
            {
                "b64_json": base64.b64encode(b"fake-image-bytes").decode("ascii"),
                "revised_prompt": "a refined prompt",
                "output_format": "png",
                "size": "1024x1024",
                "bytes": 16,
            }
        ]
        self.usage = usage or {"input_tokens": 12, "output_tokens": 5}
        self.request_id = "req-fake"
        self.main_model = "gpt-5.4-mini"
        self.tool_model = tool_model
        self.account_email = "fake@example.com"
        self.duration_ms = 123
        self.cached = False


def _build_app() -> FastAPI:
    app = FastAPI()

    async def _gen(request: Request):
        return await compat.handle_generations(request)

    async def _edits(request: Request):
        return await compat.handle_edits(request)

    async def _legacy_gen(request: Request):
        return await images_simple.handle_generate(request)

    async def _legacy_edit(request: Request):
        return await images_simple.handle_edit(request)

    app.add_api_route("/v1/images/generate", _legacy_gen, methods=["POST"])
    app.add_api_route("/v1/images/edit", _legacy_edit, methods=["POST"])
    app.add_api_route("/v1/images/generations", _gen, methods=["POST"])
    app.add_api_route("/images/generations", _gen, methods=["POST"])
    app.add_api_route("/v1/images/edits", _edits, methods=["POST"])
    app.add_api_route("/images/edits", _edits, methods=["POST"])
    return app


class _AsgiTestClient:
    def __init__(self, app: FastAPI):
        self._app = app

    def post(self, url: str, **kwargs):
        # Existing success/error fixtures now state the required client model
        # explicitly. New missing-model cases pass ``explicit_model=None``.
        explicit_model = kwargs.pop("explicit_model", "dall-e-3")
        if explicit_model is not None:
            if isinstance(kwargs.get("json"), dict) and "model" not in kwargs["json"]:
                kwargs["json"] = {**kwargs["json"], "model": explicit_model}
            if isinstance(kwargs.get("data"), dict) and "model" not in kwargs["data"]:
                kwargs["data"] = {**kwargs["data"], "model": explicit_model}
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.post(url, **kwargs)

        return asyncio.run(_run())


def _make_client(app):
    return _AsgiTestClient(app)


def _auth_headers():
    return {"Authorization": "Bearer test-token", "Content-Type": "application/json"}


# ── 基本路由 / 字段透传 ────────────────────────────────────────────────────














def test_codex_image_headers_follow_configured_cli_identity():
    original = dict(config.get().get("openaiOAuth") or {})
    try:
        config.update(lambda cfg: cfg.setdefault("openaiOAuth", {}).update({
            "codexCliVersion": "0.153.4",
            "codexProtocolProfile": "rust-v0.153.4",
        }))
        headers = images_simple._build_headers("token", "account", "gpt-image-test")
        assert headers["Version"] == "0.153.4"
        assert headers["User-Agent"].startswith("codex_cli_rs/0.153.4 ")
        assert headers["Originator"] == "codex_cli_rs"
        assert headers["x-codex-routing-hint"] == "model=gpt-image-test"
    finally:
        config.update(lambda cfg: cfg.__setitem__("openaiOAuth", original))


def test_old_responses_image_executor_is_removed():
    for name in ('_build_payload', '_execute_pipeline', '_call_upstream_once', 'UpstreamImageError'):
        assert not hasattr(images_simple, name)
    assert not hasattr(compat, '_execute_pipeline')


# ── edits / mask ───────────────────────────────────────────────────────────


def test_edits_requires_image(monkeypatch):
    async def fake_execute(**kwargs):
        raise AssertionError("pipeline should not run when image missing")

    monkeypatch.setattr(runtime, "execute", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/edits",
        headers=_auth_headers(),
        json={"prompt": "remove background"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "image"




def test_edits_rejects_file_id():
    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/edits",
        headers=_auth_headers(),
        json={
            "prompt": "edit",
            "images": [{"file_id": "file_xxx"}],
        },
    )
    assert r.status_code == 400
    assert "file_id" in r.json()["error"]["message"]






# ── 入参校验 ───────────────────────────────────────────────────────────────




def test_invalid_response_format_returns_400():
    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "response_format": "garbage"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "response_format"


def test_missing_prompt_returns_400():
    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": ""})
    assert r.status_code == 400


def test_invalid_n_type_returns_400():
    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "n": "abc"},
    )
    assert r.status_code == 400


def test_prompt_non_string_returns_400():
    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": 123},
    )
    assert r.status_code == 400
    assert "prompt" in r.json()["error"]["message"]


def test_auth_required():
    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", json={"prompt": "x"})
    assert r.status_code == 401


def test_images_not_allowed_for_key():
    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers={"Authorization": "Bearer readonly-token", "Content-Type": "application/json"},
        json={"prompt": "x"},
    )
    assert r.status_code == 403


# ── 上游错误映射 + retry-after ─────────────────────────────────────────────








# ── 多账号 failover（在 _execute_pipeline 层） ────────────────────────────






# ── 显式 model 必填（所有标准/私有创建入口）──────────────────────────────


_ABSENT = object()


@pytest.mark.parametrize(
    "route",
    [
        "/v1/images/generations", "/images/generations",
        "/v1/images/edits", "/images/edits",
        "/v1/images/generate", "/v1/images/edit",
    ],
)
@pytest.mark.parametrize("invalid_model", [_ABSENT, None, "", "   ", 7])
def test_all_image_create_routes_require_explicit_model_before_pipeline(
    monkeypatch, route, invalid_model,
):
    calls = 0

    async def unexpected(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid model must not reach image pipeline")

    monkeypatch.setattr(runtime, "execute", unexpected)
    payload = {"prompt": "x", "image": "https://example.test/input.png"}
    if invalid_model is not _ABSENT:
        payload["model"] = invalid_model
    response = _make_client(_build_app()).post(
        route, headers=_auth_headers(), json=payload, explicit_model=None,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "invalid_request_error"
    if route.endswith("generations") or route.endswith("edits"):
        assert response.json()["error"].get("param") == "model"
    assert calls == 0


@pytest.mark.parametrize("route", ["/v1/images/generations", "/v1/images/edits", "/v1/images/generate", "/v1/images/edit"])
def test_image_multipart_requires_model_before_file_or_pipeline(monkeypatch, route):
    calls = 0

    async def unexpected(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("missing model must not reach image pipeline")

    monkeypatch.setattr(runtime, "execute", unexpected)
    response = _make_client(_build_app()).post(
        route,
        headers={"Authorization": "Bearer test-token"},
        data={"prompt": "x"},
        files={"image": ("in.png", b"not-read", "image/png")},
        explicit_model=None,
    )
    assert response.status_code == 400, response.text
    assert calls == 0


# ── 老入口回归 ────────────────────────────────────────────────────────────
