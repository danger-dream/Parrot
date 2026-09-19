"""Regressions for per-key media selection and video failure detail delivery."""
from __future__ import annotations

import base64
import json

import mcp_types
import pytest
from starlette.responses import JSONResponse

from src.tests.test_mcp_regressions import reset, context
from src import config, log_db
from src.mcp import catalog, server as ms


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["image_generate", "image_edit"])
async def test_image_auto_uses_allowed_alternative_and_keeps_http_authorization(monkeypatch, tool):
    from src.openai import images_openai_compat as images, images_runtime

    config.update(lambda c: (
        c["apiKeys"]["audit"].update(allowedModels=["allowed-image"]),
        c["images"].update(enabled=True, defaultModel="forbidden-image"),
    ))
    monkeypatch.setattr(catalog, "image_sources", lambda: ["forbidden-image", "allowed-image"])
    monkeypatch.setattr(images.image_catalog, "models", lambda: ["forbidden-image", "allowed-image"])
    executed = []

    async def execute(parsed, **kwargs):
        executed.append(parsed.model)
        return JSONResponse({"model": parsed.model, "data": [{"url": "https://audit.invalid/image"}]})

    monkeypatch.setattr(images_runtime, "execute", execute)
    listed = await ms.on_list_tools(context(), None)
    definition = next(t for t in listed.tools if t.name == tool)
    assert definition.input_schema["properties"]["source"]["enum"] == ["auto", "allowed-image"]
    assert "forbidden-image" not in definition.description
    args = {"prompt": "audit", "source": "auto"}
    if tool == "image_edit":
        args["images"] = ["data:image/png;base64," + base64.b64encode(b"test image").decode()]
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(name=tool, arguments=args))
    assert not result.is_error and result.structured_content["model"] == "allowed-image"

    # Explicit source must not silently switch to an allowed model.
    explicit = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name=tool, arguments={**args, "source": "forbidden-image"}))
    assert explicit.is_error
    # The legacy model parameter still reaches the real HTTP whitelist check.
    legacy = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name=tool, arguments={**args, "model": "forbidden-image"}))
    assert legacy.is_error and "model is not allowed" in legacy.content[0].text
    assert executed == ["allowed-image"]


@pytest.mark.asyncio
async def test_video_auto_passes_allowed_model_to_handler(monkeypatch):
    from src.xai import imagine

    config.update(lambda c: (
        c["apiKeys"]["audit"].update(allowedModels=["allowed-video"]),
        c.setdefault("videos", {}).update(defaultModel="forbidden-video"),
    ))
    monkeypatch.setattr(catalog, "video_sources", lambda: ["forbidden-video", "allowed-video"])
    calls = []

    async def create(request, *, action):
        body = await request.json()
        calls.append(body["model"])
        return JSONResponse({"request_id": "audit-job", "status": "pending", "model": body["model"]})

    monkeypatch.setattr(imagine, "handle_video_create", create)
    listed = await ms.on_list_tools(context(), None)
    definition = next(t for t in listed.tools if t.name == "video_generate")
    assert definition.input_schema["properties"]["source"]["enum"] == ["auto", "allowed-video"]
    assert "forbidden-video" not in definition.description
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="video_generate", arguments={"prompt": "audit", "source": "auto"}))
    assert not result.is_error and result.structured_content["model"] == "allowed-video"
    assert calls == ["allowed-video"]


@pytest.mark.parametrize("kind", ["image", "video"])
def test_catalog_and_auto_follow_live_grants_without_changing_priority(monkeypatch, kind):
    monkeypatch.setattr(catalog, kind + "_sources", lambda: ["first", "second"])
    config.update(lambda c: c.setdefault(kind + "s", {}).update(defaultModel="second"))
    assert catalog.media_sources(kind, "audit") == ["first", "second"]
    assert ms._auto_model(kind, "audit") == "second"
    config.update(lambda c: c["apiKeys"]["audit"].update(allowedModels=["first"]))
    assert catalog.media_sources(kind, "audit") == ["first"]
    assert ms._auto_model(kind, "audit") == "first"
    config.update(lambda c: c["apiKeys"]["audit"].update(allowedModels=["not-available"]))
    assert catalog.media_sources(kind, "audit") == []
    with pytest.raises(ms.ToolError, match="没有可用且获准"):
        ms._requested_model({}, kind=kind, key_name="audit")
    assert catalog.media_sources(kind, "deleted-key") == []
    config.update(lambda c: c["apiKeys"]["audit"].update(enabled=False))
    assert catalog.media_sources(kind, "audit") == []


@pytest.mark.parametrize("kind", ["image", "video"])
def test_granted_alias_is_preserved_and_disabled_alias_is_excluded(monkeypatch, kind):
    monkeypatch.setattr(catalog, kind + "_sources", lambda: ["real-model"])
    config.update(lambda c: (
        c.update(modelMapping={"global": {"granted-alias": "real-model"}}),
        c["apiKeys"]["audit"].update(allowedModels=["granted-alias"]),
    ))
    assert catalog.media_sources(kind, "audit") == ["granted-alias"]
    assert ms._requested_model({}, kind=kind, key_name="audit") == "granted-alias"
    config.update(lambda c: c.setdefault("modelCenter", {}).update(disabledModels=["granted-alias"]))
    assert catalog.media_sources(kind, "audit") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["real-image", "granted-alias"])
async def test_image_alias_grants_match_real_http_handler(monkeypatch, grant):
    from src.openai import images_openai_compat as images, images_runtime

    config.update(lambda c: (
        c.update(modelMapping={"global": {"granted-alias": "real-image"}}),
        c["apiKeys"]["audit"].update(allowedModels=[grant]),
        c["images"].update(enabled=True, defaultModel="granted-alias"),
    ))
    # Alias supplied by an available catalog stays usable with a real-name grant.
    monkeypatch.setattr(catalog, "image_sources", lambda: ["granted-alias", "real-image"])
    monkeypatch.setattr(images.image_catalog, "models", lambda: ["real-image"])
    calls = []

    async def execute(parsed, **kwargs):
        calls.append(parsed.model)
        return JSONResponse({"model": parsed.model, "data": [{"url": "https://audit.invalid/alias"}]})

    monkeypatch.setattr(images_runtime, "execute", execute)
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="image_generate", arguments={"prompt": "alias-audit"}))
    assert not result.is_error and calls == ["real-image"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    ("failed", {"code": "generation_failed", "message": "specific upstream failure"}),
    ("expired", {"message": "generation expired"}),
    ("cancelled", "generation cancelled"),
    ("pending", None),
])
async def test_video_status_preserves_failure_detail_and_query_semantics(monkeypatch, status, error):
    from src.xai import imagine

    async def response(request, request_id):
        return JSONResponse({"request_id": request_id, "status": status,
                             "model": "audit-video", "error": error})

    monkeypatch.setattr(imagine, "handle_video_result", response)
    request_id = "audit-video-" + status
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="video_status", arguments={"request_id": request_id}))
    assert not result.is_error  # The query succeeded even if generation failed.
    assert result.structured_content["status"] == status
    if error is not None:
        assert result.structured_content["error"] == error
    else:
        assert "error" not in result.structured_content
    assert json.loads(result.content[0].text) == result.structured_content
    row = next(row for row in log_db.mcp_call_entries(0)
               if request_id in (row.get("params_json") or ""))
    assert row["status"] == "success"
    detail = log_db.mcp_call_detail(row["call_id"])
    assert json.loads(detail["result_body"]) == result.structured_content
