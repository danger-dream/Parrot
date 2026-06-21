"""OpenAI Images API 兼容入口单元测试。

覆盖：
- 路由（/v1/images/generations、/images/generations、/v1/images/edits、/images/edits）
- payload 字段解析（JSON / multipart）+ 字段透传到 image_generation tool
- n>1 降为 1 并出现 parrot_warning
- response_format=url 时返回 data URL（且空字符串不算覆盖默认）
- 响应 model 是实际 tool_model，客户端请求 model 放在 parrot_requested_model
- edits 多张 images[] / mask 解析（含 multipart）
- generate 入口收到 mask 时被丢弃 + warning 日志
- prompt 必须是字符串
- 多账号 failover（全失败回 429 + Retry-After 透传）
- 老入口 /v1/images/generate /edit 行为不变（回归）

运行：
  ./venv/bin/python -m pytest -q src/tests/test_images_openai_compat.py
"""

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
from src.openai import images_openai_compat as compat
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


def test_basic_generation_returns_openai_shape(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "a red apple", "size": "1024x1024", "model": "dall-e-3"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "created" in body
    assert isinstance(body["data"], list) and len(body["data"]) == 1
    item = body["data"][0]
    assert "b64_json" in item and "url" not in item
    assert item["revised_prompt"] == "a refined prompt"
    # 响应 model 是实际 tool_model
    assert body["model"] == "gpt-image-2"
    # 客户端请求 model 单独放
    assert body["parrot_requested_model"] == "dall-e-3"

    assert captured["action"] == "generate"
    assert captured["prompt"] == "a red apple"
    assert captured["size"] == "1024x1024"


def test_response_format_url_returns_data_url(monkeypatch):
    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "response_format": "url"},
    )
    assert r.status_code == 200, r.text
    item = r.json()["data"][0]
    assert "url" in item and "b64_json" not in item
    assert item["url"].startswith("data:image/png;base64,")


def test_response_format_empty_string_falls_back_to_b64(monkeypatch):
    """空字符串 response_format 不应该被当成无效值；视为没传 = b64_json。"""

    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "response_format": ""},
    )
    assert r.status_code == 200, r.text
    assert "b64_json" in r.json()["data"][0]


def test_response_format_with_only_whitespace_treated_as_empty(monkeypatch):
    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "response_format": "  "},
    )
    assert r.status_code == 200, r.text
    assert "b64_json" in r.json()["data"][0]


def test_n_greater_than_1_is_downgraded_with_warning(monkeypatch):
    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={"prompt": "x", "n": 4},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "parrot_warning" in body
    assert "n=4" in body["parrot_warning"]


def test_native_options_pass_through_to_tool(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={
            "prompt": "x",
            "quality": "high",
            "background": "transparent",
            "output_format": "webp",
            "moderation": "low",
            "style": "vivid",
            "output_compression": 80,
            "partial_images": 2,
        },
    )
    assert r.status_code == 200, r.text
    opts = captured["native_options"]
    assert opts == {
        "quality": "high",
        "background": "transparent",
        "output_format": "webp",
        "moderation": "low",
        "style": "vivid",
        "output_compression": 80,
        "partial_images": 2,
    }


def test_build_payload_includes_native_options_and_mask():
    payload = images_simple._build_payload(
        action="edit",
        prompt="prompt",
        main_model="gpt-5.4-mini",
        tool_model="gpt-image-2",
        size="1024x1024",
        images=["data:image/png;base64,AAA"],
        native_options={
            "quality": "high", "background": "transparent",
            "output_format": "webp", "output_compression": 70, "partial_images": 1,
            "ignored_field": "nope",
        },
        mask_url="data:image/png;base64,MASK",
    )
    tool = payload["tools"][0]
    assert tool["action"] == "edit"
    assert tool["model"] == "gpt-image-2"
    assert tool["size"] == "1024x1024"
    assert tool["quality"] == "high"
    assert tool["background"] == "transparent"
    assert tool["output_format"] == "webp"
    assert tool["output_compression"] == 70
    assert tool["partial_images"] == 1
    assert "ignored_field" not in tool
    assert tool["input_image_mask"] == {"image_url": "data:image/png;base64,MASK"}
    msg = payload["input"][0]["content"]
    assert msg[0] == {"type": "input_text", "text": "prompt"}
    assert msg[1]["type"] == "input_image"
    assert msg[1]["image_url"] == "data:image/png;base64,AAA"


# ── edits / mask ───────────────────────────────────────────────────────────


def test_edits_requires_image(monkeypatch):
    async def fake_execute(**kwargs):
        raise AssertionError("pipeline should not run when image missing")

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/edits",
        headers=_auth_headers(),
        json={"prompt": "remove background"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "image"


def test_edits_json_accepts_image_array_and_mask(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/edits",
        headers=_auth_headers(),
        json={
            "prompt": "edit",
            "image": ["data:image/png;base64," + base64.b64encode(b"a" * 200).decode("ascii")],
            "images": [{"image_url": "https://cdn.example.com/x.png"}],
            "mask": "data:image/png;base64," + base64.b64encode(b"b" * 200).decode("ascii"),
        },
    )
    assert r.status_code == 200, r.text
    imgs = captured["input_image_urls"]
    assert len(imgs) == 2
    assert imgs[0].startswith("data:image/png;base64,")
    assert imgs[1] == "https://cdn.example.com/x.png"
    assert captured["mask_url"].startswith("data:image/png;base64,")


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


def test_edits_multipart_form_with_image_and_mask(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    mask_bytes = b"\x89PNG\r\n\x1a\n" + b"y" * 100
    files = {
        "image": ("img.png", io.BytesIO(image_bytes), "image/png"),
        "mask": ("mask.png", io.BytesIO(mask_bytes), "image/png"),
    }
    data = {"prompt": "edit", "size": "1024x1024", "quality": "high", "n": "1"}
    r = client.post(
        "/v1/images/edits",
        headers={"Authorization": "Bearer test-token"},
        data=data, files=files,
    )
    assert r.status_code == 200, r.text
    assert len(captured["input_image_urls"]) == 1
    assert captured["input_image_urls"][0].startswith("data:image/png;base64,")
    assert captured["mask_url"].startswith("data:image/png;base64,")
    assert captured["native_options"]["quality"] == "high"


def test_generate_with_mask_ignores_mask_and_warns(monkeypatch, capsys):
    captured: dict[str, Any] = {}

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generations",
        headers=_auth_headers(),
        json={
            "prompt": "x",
            "mask": "data:image/png;base64," + base64.b64encode(b"m" * 200).decode("ascii"),
        },
    )
    assert r.status_code == 200, r.text
    assert captured["mask_url"] is None
    out = capsys.readouterr().out
    assert "WARNING" in out and "mask" in out


# ── 入参校验 ───────────────────────────────────────────────────────────────


def test_no_v1_prefix_routes_also_work(monkeypatch):
    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post("/images/generations", headers=_auth_headers(), json={"prompt": "hi"})
    assert r.status_code == 200


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


def test_upstream_error_maps_to_status_code(monkeypatch):
    async def fake_execute(**kwargs):
        raise images_simple.UpstreamImageError(
            "no available OpenAI OAuth account for images",
            503, errors.ErrTypeOpenAI.SERVER, retryable=False,
        )

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": "x"})
    assert r.status_code == 503
    assert "no available" in r.json()["error"]["message"]


def test_upstream_429_includes_retry_after_header(monkeypatch):
    async def fake_execute(**kwargs):
        raise images_simple.UpstreamImageError(
            "rate limited", 429, errors.ErrTypeOpenAI.RATE_LIMIT,
            retryable=True, cooldown=True, retry_after=42,
        )

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": "x"})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "42"


def test_upstream_429_without_retry_after_no_header(monkeypatch):
    async def fake_execute(**kwargs):
        raise images_simple.UpstreamImageError(
            "rate limited", 429, errors.ErrTypeOpenAI.RATE_LIMIT, retry_after=None,
        )

    monkeypatch.setattr(compat, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": "x"})
    assert r.status_code == 429
    assert "retry-after" not in {k.lower(): v for k, v in r.headers.items()}


# ── 多账号 failover（在 _execute_pipeline 层） ────────────────────────────


def test_pipeline_failover_across_accounts(monkeypatch):
    """全部账号 429 → pipeline 应返回 UpstreamImageError(429, retry_after=最后一次)，
    上层 handler 应回 429 + Retry-After header。"""
    calls: list[str] = []

    fake_rows = [
        {
            "account": {"workspace_id": f"acc-{i}", "email": f"a{i}@x"},
            "account_key": f"key-{i}",
            "email": f"a{i}@x",
        }
        for i in range(3)
    ]
    monkeypatch.setattr(images_simple, "_candidate_accounts", lambda: fake_rows)

    async def fake_call_upstream_once(row, payload, *, timeout_s, refresh_first=False):
        calls.append(row["account_key"])
        # 模拟最后一次给出 retry_after=7
        ra = 7 if row["account_key"] == "key-2" else 3
        raise images_simple.UpstreamImageError(
            f"429 from {row['account_key']}", 429, errors.ErrTypeOpenAI.RATE_LIMIT,
            retryable=True, cooldown=True, retry_after=ra,
        )

    monkeypatch.setattr(images_simple, "_call_upstream_once", fake_call_upstream_once)

    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": "x"})
    assert r.status_code == 429
    # 三个账号都被试过
    assert calls == ["key-0", "key-1", "key-2"]
    # 透传最后一次的 retry-after
    assert r.headers.get("retry-after") == "7"


def test_pipeline_success_after_one_failover(monkeypatch):
    fake_rows = [
        {"account": {"workspace_id": "acc-0", "email": "a0@x"}, "account_key": "key-0", "email": "a0@x"},
        {"account": {"workspace_id": "acc-1", "email": "a1@x"}, "account_key": "key-1", "email": "a1@x"},
    ]
    monkeypatch.setattr(images_simple, "_candidate_accounts", lambda: fake_rows)

    fake_image = {
        "b64_json": base64.b64encode(b"ok").decode("ascii"),
        "revised_prompt": "", "output_format": "png", "size": "1024x1024", "bytes": 2,
    }
    calls: list[str] = []

    async def fake_call_upstream_once(row, payload, *, timeout_s, refresh_first=False):
        calls.append(row["account_key"])
        if row["account_key"] == "key-0":
            raise images_simple.UpstreamImageError(
                "429", 429, errors.ErrTypeOpenAI.RATE_LIMIT,
                retryable=True, cooldown=True, retry_after=10,
            )
        return [fake_image], {"input_tokens": 1, "output_tokens": 1}, 2

    monkeypatch.setattr(images_simple, "_call_upstream_once", fake_call_upstream_once)

    client = _make_client(_build_app())
    r = client.post("/v1/images/generations", headers=_auth_headers(), json={"prompt": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls == ["key-0", "key-1"]
    assert body["data"][0]["b64_json"] == fake_image["b64_json"]
    # 成功响应不带 Retry-After
    assert "retry-after" not in {k.lower(): v for k, v in r.headers.items()}


# ── 老入口回归 ────────────────────────────────────────────────────────────


def test_legacy_generate_still_returns_parrot_object(monkeypatch):
    async def fake_execute(**kwargs):
        return _FakePipelineResult()

    monkeypatch.setattr(images_simple, "_execute_pipeline", fake_execute)

    client = _make_client(_build_app())
    r = client.post(
        "/v1/images/generate",
        headers=_auth_headers(),
        json={"prompt": "old api still works"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "parrot.image.generate"
    assert body["account"] == "fake@example.com"
    assert body["data"][0]["b64_json"]
