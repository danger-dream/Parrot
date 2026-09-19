"""MCP audit fixes: real ASGI/control/adapter paths with isolated state only."""
from __future__ import annotations

import asyncio
import base64
import copy
import html
import io
import json
from types import SimpleNamespace

import httpx
import mcp_types
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.tests import _isolation

_isolation.isolate()

from src import apikey_limiter, config, log_db, search_service
from src.management_control.auxiliary.common import telegram_context
from src.management_control.mcp import MCPControl
from src.mcp import catalog, mount, policy, server as ms


@pytest.fixture(autouse=True)
def reset():
    saved = copy.deepcopy(config.get())
    config.update(lambda c: c.update({
        "mcp": copy.deepcopy(config.DEFAULT_CONFIG["mcp"]),
        "apiKeys": {"audit": {"key": "audit-test-secret", "enabled": True,
            "allowMcp": True, "allowImages": True, "allowVideos": True, "allowedModels": []}},
        "apiKeyConcurrency": {"enabled": True, "defaultMaxConcurrent": 1,
            "defaultMaxQueue": 0, "defaultQueueWaitSeconds": 1},
    }))
    apikey_limiter._slots.clear()
    log_db.init()
    yield
    apikey_limiter._slots.clear()
    config.update(lambda c: (c.clear(), c.update(saved)))


HEADERS = {"Authorization": "Bearer audit-test-secret",
           "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


def rpc(name="web_search", args=None, ident=1):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, "arguments": args or {"query": "audit query"}}}


def context():
    return SimpleNamespace(request=Request({"type": "http", "method": "POST", "path": "/mcp/",
        "headers": [], "scheme": "https", "server": ("audit.invalid", 443),
        "state": {"parrot_key_name": "audit"}}), meta=None, protocol_version="2025-11-25")


def app_server():
    server = ms.build_server()
    app = FastAPI()
    app.mount("/mcp", mount.build_asgi_app(server))
    return app, server


@pytest.mark.asyncio
async def test_real_http_and_mcp_share_key_limit(monkeypatch):
    # Use production middleware but never enter the application's startup lifespan.
    import server as root
    from src.openai import images_openai_compat as images, images_runtime

    monkeypatch.setattr(catalog, "image_sources", lambda: ["audit-image"])
    monkeypatch.setattr(images.image_catalog, "models", lambda: ["audit-image"])
    calls = []

    async def execute(parsed, **kwargs):
        calls.append((parsed.model, kwargs["key_name"]))
        return JSONResponse({"model": parsed.model, "data": [{"url": "https://audit.invalid/fake"}]})

    monkeypatch.setattr(images_runtime, "execute", execute)
    app, server = app_server()
    app.add_middleware(root._DrainHttpMiddleware)
    app.post("/v1/images/generations")(images.handle_generations)
    lease = await apikey_limiter.acquire("audit")
    try:
        async with server.session_manager.run(), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://audit.invalid") as client:
            rest = await client.post("/v1/images/generations", headers=HEADERS,
                                     json={"model": "audit-image", "prompt": "cat"})
            blocked = await client.post("/mcp/", headers=HEADERS,
                json=rpc("image_generate", {"source": "audit-image", "prompt": "cat"}))
            assert rest.status_code == 429
            assert blocked.status_code == 200 and blocked.json()["result"]["isError"]
            assert calls == []
            await lease.release()
            accepted = await client.post("/mcp/", headers=HEADERS,
                json=rpc("image_generate", {"source": "audit-image", "prompt": "cat"}))
            assert not accepted.json()["result"]["isError"]
            assert calls == [("audit-image", "audit")]
            assert apikey_limiter.key_snapshot("audit")["in_flight"] == 0
    finally:
        await lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", catalog.TOOL_NAMES)
async def test_every_tool_has_deadline_and_releases_lease(monkeypatch, tool_name):
    cancelled = asyncio.Event()

    async def delayed(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(ms, "_dispatch", delayed)
    result = await asyncio.wait_for(ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name=tool_name, arguments={"query": "deadline-" + tool_name, "timeout_seconds": 0.1})), 2)
    assert result.is_error and cancelled.is_set()
    assert apikey_limiter.key_snapshot("audit")["in_flight"] == 0
    row = next(r for r in log_db.mcp_call_entries(0) if "deadline-" + tool_name in (r.get("params_json") or ""))
    assert row["status"] == "timeout" and row["error_code"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 601, True, "oops", float("nan"), float("inf")])
async def test_invalid_deadline_never_dispatches(monkeypatch, value):
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid timeout reached execution")
    monkeypatch.setattr(ms, "_dispatch", unexpected)
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="web_search", arguments={"timeout_seconds": value}))
    assert result.is_error
    assert "timeout_seconds" in result.content[0].text


@pytest.mark.asyncio
async def test_queue_timeout_and_cancellation_do_not_leak(monkeypatch):
    config.update(lambda c: c["apiKeyConcurrency"].update(defaultMaxQueue=2, defaultQueueWaitSeconds=5))
    held = await apikey_limiter.acquire("audit")
    called = []
    async def dispatch(*args, **kwargs):
        called.append(True)
        return {}, {}
    monkeypatch.setattr(ms, "_dispatch", dispatch)
    try:
        result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
            name="web_search", arguments={"timeout_seconds": 0.1}))
        assert result.is_error and called == []
        assert apikey_limiter.key_snapshot("audit")["waiting"] == 0
        task = asyncio.create_task(ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
            name="web_search", arguments={"query": "queue-cancel"})))
        for _ in range(100):
            if apikey_limiter.key_snapshot("audit")["waiting"]: break
            await asyncio.sleep(0.01)
        assert apikey_limiter.key_snapshot("audit")["waiting"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert apikey_limiter.key_snapshot("audit")["waiting"] == 0
        assert apikey_limiter.key_snapshot("audit")["in_flight"] == 1
    finally:
        await held.release()


@pytest.mark.asyncio
async def test_origin_policy_and_session_isolation():
    config.update(lambda c: (c["management"].update({"allowedOrigins": ["https://trusted.invalid"]}),
        c["apiKeys"].update({"other": {"key": "other-test-secret", "enabled": True,
                                    "allowMcp": True, "mcpTools": ["web_fetch"]}})))
    app, server = app_server()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with server.session_manager.run(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://audit.invalid") as client:
        for origin in ("https://untrusted.invalid", "null", "http://audit.invalid", "https://audit.invalid:444"):
            denied = await client.post("/mcp/", headers={**HEADERS, "Origin": origin}, json=payload)
            assert denied.status_code == 403
        for origin in ("https://audit.invalid", "https://audit.invalid:443", "https://trusted.invalid"):
            response = await client.post("/mcp/", headers={**HEADERS, "Origin": origin}, json=payload)
            assert response.status_code == 200
        a, b = await asyncio.gather(
            client.post("/mcp/", headers=HEADERS, json=payload),
            client.post("/mcp/", headers={**HEADERS, "Authorization": "Bearer other-test-secret"}, json=payload))
        assert len(a.json()["result"]["tools"]) == 6
        assert [t["name"] for t in b.json()["result"]["tools"]] == ["web_fetch"]
        denied = await client.post("/mcp/", headers={**HEADERS, "Authorization": "Bearer other-test-secret"},
                                   json=rpc("image_generate", {"prompt": "cat"}))
        assert denied.json()["result"]["isError"]
        config.update(lambda c: c["management"].update(allowedOrigins=[]))
        assert (await client.post("/mcp/", headers={**HEADERS, "Origin": "https://trusted.invalid"}, json=payload)).status_code == 403
        assert (await client.post("/mcp/", json=payload)).status_code == 401
        assert (await client.post("/mcp", headers=HEADERS, json=payload)).status_code == 307


def test_management_patch_preserves_omitted_tools_and_revision():
    from src.management_control import ManagementError
    ctl, ctx = MCPControl(), telegram_context(1)
    ctl.set_tool(ctx, "image_generate", False)
    revision = ctl.get(ctx)["revision"]
    value = ctl.patch(ctx, {"tools": {"web_search": False}}, expected_revision=revision)
    assert value["tools"]["image_generate"] is False
    assert not policy.tool_allowed("audit", "image_generate")
    assert value["tools"]["web_fetch"] is True
    with pytest.raises(ManagementError):
        ctl.patch(ctx, {"tools": {"image_generate": True}}, expected_revision=revision)
    assert not policy.tool_allowed("audit", "image_generate")
    ctl.patch(ctx, {"tools": {}})
    assert not policy.tool_allowed("audit", "image_generate")


def test_stats_endpoint_serializes_populated_rows(tmp_path):
    from src.tests.test_management_apikey_api import build_app, session_headers
    from src.management_api import create_management_router
    from src.management_api.routers.mcp import router
    from fastapi.testclient import TestClient
    app, runtime, *_ = build_app(tmp_path)
    app.include_router(create_management_router([router]))
    h = log_db.record_mcp_call(call_id="audit-stats", tool_name="web_search")
    log_db.finish_mcp_call(h, status="success", elapsed_ms=10)
    try:
        with TestClient(app) as client:
            response = client.get("/api/management/v1/mcp/stats", headers=session_headers(runtime))
            assert response.status_code == 200
            item = next(row for row in response.json()["data"]["items"] if row["toolName"] == "web_search")
            assert item["success"] >= 1 and item["lastAt"] > 0
            assert "elapsed_sum" not in item and "tool_name" not in item
    finally:
        runtime.state_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_body_limit_changes_live_in_both_directions(chunked):
    app, server = app_server()
    ctl, ctx = MCPControl(), telegram_context(1)
    ctl.patch(ctx, {"maxRequestBodyBytes": 1048576})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "padding": "a" * 1048576}).encode()
    async def content():
        yield body[:700000]
        yield body[700000:]
    async with server.session_manager.run(), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://audit.invalid") as client:
        async def send():
            return await client.post("/mcp/", headers={**HEADERS, "Content-Type": "application/json"},
                                     content=content() if chunked else body)
        assert (await send()).status_code == 413
        ctl.patch(ctx, {"maxRequestBodyBytes": 2 * 1048576})
        assert (await send()).status_code == 200
        ctl.patch(ctx, {"maxRequestBodyBytes": 1048576})
        assert (await send()).status_code == 413


@pytest.mark.asyncio
async def test_fetch_caps_content_and_filters_source_capability(monkeypatch):
    config.update(lambda c: c.update({"search": {**search_service.DEFAULTS, "maxFetchChars": 50000,
        "backends": [{"id": "tav", "type": "tavily", "enabled": True, "apiKeys": ["fake-key"]},
                     {"id": "brave", "type": "brave", "enabled": True, "apiKeys": ["fake-key"]}]}}))
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, *args, **kwargs):
            calls.append(args)
            return httpx.Response(200, json={"results": [{"url": "https://public.invalid", "raw_content": "x" * 5000}]})
    monkeypatch.setattr(search_service.network, "async_client", lambda **kwargs: Client())
    value, _ = await ms._run_search("web_fetch", {"url": "https://public.invalid", "max_chars": 1000, "source": "tav"}, request_id="audit-fetch")
    assert len(value["content"]) == 1000 and value["truncated"]
    assert "content_truncated_to_mcp_max_chars" in value["warnings"]
    options = ms.build_tool("web_fetch").input_schema["properties"]["source"]["enum"]
    assert options == ["auto", "tav"]
    assert "brave" in catalog.available_engines()
    with pytest.raises(ms.ToolError):
        await ms._run_search("web_fetch", {"url": "https://public.invalid", "source": "brave"}, request_id="bad-fetch")
    assert len(calls) == 1
    full, _ = await ms._run_search("web_fetch", {"url": "https://public.invalid", "source": "tav"}, request_id="full-fetch")
    assert len(full["content"]) == 5000


@pytest.mark.asyncio
async def test_cancelled_tool_closes_log_and_releases_slot(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ms, "_dispatch", blocked)
    task = asyncio.create_task(ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="web_search", arguments={"query": "cancel-audit"})))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    row = next(r for r in log_db.mcp_call_entries(0) if "cancel-audit" in (r.get("params_json") or ""))
    assert row["status"] == "error" and row["error_code"] == "cancelled" and row["elapsed_ms"] is not None
    assert apikey_limiter.key_snapshot("audit")["in_flight"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_sdk_exit_settles_inflight_or_queued_call(monkeypatch, queued):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    config.update(lambda c: c["apiKeyConcurrency"].update(defaultMaxQueue=1, defaultQueueWaitSeconds=5))
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(ms, "_dispatch", blocked)
    held = await apikey_limiter.acquire("audit") if queued else None
    app, server = app_server()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://audit.invalid") as client:
            async with server.session_manager.run():
                task = asyncio.create_task(client.post("/mcp/", headers=HEADERS,
                    json=rpc(args={"query": "shutdown-audit-" + str(queued)})))
                if queued:
                    for _ in range(100):
                        if apikey_limiter.key_snapshot("audit")["waiting"]: break
                        await asyncio.sleep(0.01)
                    assert apikey_limiter.key_snapshot("audit")["waiting"] == 1
                else:
                    await asyncio.wait_for(entered.wait(), 2)
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 500
        assert not entered.is_set() if queued else cancelled.is_set()
        row = next(r for r in log_db.mcp_call_entries(0) if "shutdown-audit-" + str(queued) in (r.get("params_json") or ""))
        assert row["status"] == "error" and row["error_code"] == "cancelled"
        snap = apikey_limiter.key_snapshot("audit")
        assert snap["waiting"] == 0 and snap["in_flight"] == int(queued)
    finally:
        if held: await held.release()


def test_tg_details_use_exact_call_id_beyond_200(monkeypatch):
    from src.telegram.menus import mcp_menu as menu
    from src.telegram import ui
    for i in range(201):
        log_db.record_mcp_call(call_id=f"audit-page-{i}", tool_name="web_search")
    # The detail path must not call the paginated list, even for old visible rows.
    monkeypatch.setattr(menu._CONTROL, "logs", lambda *a, **k: pytest.fail("detail scanned list"))
    row = menu._resolve_log(1, ui.register_code("mcplog:audit-page-0"))
    assert row is not None and row["callId"] == "audit-page-0"
    assert menu._resolve_log(1, ui.register_code("mcplog:missing-audit")) is None


def test_video_auto_falls_back_to_available_source():
    config.update(lambda c: c.update({"video_models": {"xai": ["video-disabled", "video-enabled"]},
        "videos": {"enabled": True, "defaultModel": "video-disabled"},
        "modelCenter": {"disabledModels": ["video-disabled"]},
        "oauthAccounts": [{"provider": "xai", "email": "video@example.invalid", "subject": "audit-video",
            "access_token": "fake", "enabled": True, "videoModels": ["video-disabled", "video-enabled"]}]}))
    assert ms._auto_model("video") == "video-enabled"
    assert ms.build_tool("video_generate").input_schema["properties"]["source"]["enum"] == ["auto", "video-enabled"]
    config.update(lambda c: c["oauthAccounts"][0].update(enabled=False, disabled_reason="user"))
    assert catalog.video_sources() == []


def test_account_specific_video_is_grantable():
    from src.management_control.apikey import ApiKeyControl
    config.update(lambda c: c.update({"video_models": {"xai": ["global-video"]},
        "oauthAccounts": [{"provider": "xai", "email": "audit@example.invalid", "videoModels": ["account-video"],
            "access_token": "fake", "enabled": True, "models": []}]}))
    ctl = ApiKeyControl(model_registry=SimpleNamespace(available_models=lambda: []))
    assert "account-video" in catalog.video_sources()
    assert "account-video" in ctl.available_permission_models_unchecked()
    result = ctl.update_api_key(telegram_context(1), "audit", changes={"allowed_models": ["account-video"]})
    assert result.allowed_models == ("account-video",)
    config.update(lambda c: c["oauthAccounts"][0].update(enabled=False, disabled_reason="user"))
    assert "account-video" in ctl.available_permission_models_unchecked()


def test_tg_result_pages_preserve_escaped_text(monkeypatch):
    from src.telegram.menus import mcp_menu as menu
    from src.telegram import ui
    body = "<&\"" * 3100 + "AUDIT_TAIL"
    monkeypatch.setattr(menu._CONTROL, "result_body", lambda *a: {"body": json.dumps({"content": body})})
    messages = []
    monkeypatch.setattr(ui, "api", lambda method, data=None: messages.append((method, data)) or {"ok": True})
    pages = menu._chunk_pages(body)
    assert "".join(pages) == body and len(pages) > 1
    rendered = []
    for index in range(1, len(pages) + 1):
        menu.show_result(1, 1, "cb", ui.register_code("mcpbody:audit"), result_page=index)
        payload = messages[-1][1]
        text = payload["text"]
        assert len(text) < 3900 and text.endswith("</pre>")
        rendered.append(html.unescape(text.split("<pre>", 1)[1].removesuffix("</pre>")))
        nav = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
        if index < len(pages): assert any(x.endswith(":" + str(index + 1)) and x.startswith("mcp:result:") for x in nav)
    assert "".join(rendered) == body


@pytest.mark.asyncio
async def test_partial_images_deliver_urls_with_error(monkeypatch, tmp_path):
    from PIL import Image
    from src import image_catalog, image_artifacts
    from src.openai import images_runtime, images_openai_compat as images
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    source = image_catalog.ImageSource("audit-image", "gpt-image-2", "openai", "api:audit", "audit", True, True)
    monkeypatch.setattr(image_catalog, "sources", lambda *a, **k: [source])
    monkeypatch.setattr(images_runtime.registry, "get_channel", lambda key: SimpleNamespace(key=key))
    config.update(lambda c: (c.update({"channelSelection": "round_robin"}),
        c["images"].update({"cachePath": str(tmp_path), "cacheEnabled": True})))
    async def fake_send(*args, **kwargs):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]})
    monkeypatch.setattr(images_runtime, "_send", fake_send)
    real_handler = images._run_handler
    responses = []
    async def capture(*args, **kwargs):
        response = await real_handler(*args, **kwargs)
        responses.append(response)
        return response
    monkeypatch.setattr(images, "_run_handler", capture)
    result = await ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="image_generate", arguments={"source": "audit-image", "prompt": "partial-audit", "n": 2}))
    response = responses[0]
    data = json.loads(response.body)["data"]
    assert response.status_code == 502 and len(data) == 1
    assert image_artifacts.url_available(data[0]["url"])
    assert result.is_error and result.structured_content["complete"] is False
    assert result.structured_content["images"] == [data[0]["url"]]
    assert "upstream returned 1 images" in result.structured_content["error"]["message"]
    assert json.loads(result.content[0].text) == result.structured_content
    row = next(r for r in log_db.mcp_call_entries(0) if "partial-audit" in (r.get("params_json") or ""))
    assert row["status"] == "error" and row["error_code"] == "partial_result" and row["result_count"] == 1

@pytest.mark.asyncio
async def test_cancel_during_initial_log_write_is_not_orphaned(monkeypatch):
    import threading
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = log_db.record_mcp_call

    def slow_record(**kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(log_db, "record_mcp_call", slow_record)
    async def unexpected(*args, **kwargs):
        pytest.fail("cancelled request reached tool execution")
    monkeypatch.setattr(ms, "_dispatch", unexpected)
    task = asyncio.create_task(ms.on_call_tool(context(), mcp_types.CallToolRequestParams(
        name="web_search", arguments={"query": "cancel-before-log-handle"})))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # repeated task cancellation must not abandon the same write
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        release.set()
    row = next(r for r in log_db.mcp_call_entries(0) if "cancel-before-log-handle" in (r.get("params_json") or ""))
    assert row["status"] == "error" and row["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_queue_admission_rechecks_revoked_permission(monkeypatch):
    config.update(lambda c: c["apiKeyConcurrency"].update(defaultMaxQueue=1, defaultQueueWaitSeconds=5))
    held = await apikey_limiter.acquire("audit")
    async def unexpected(*args, **kwargs):
        pytest.fail("revoked key reached tool execution")
    monkeypatch.setattr(ms, "_dispatch", unexpected)
    task = asyncio.create_task(ms.on_call_tool(context(), mcp_types.CallToolRequestParams(name="web_search")))
    try:
        for _ in range(100):
            if apikey_limiter.key_snapshot("audit")["waiting"]: break
            await asyncio.sleep(0.01)
        assert apikey_limiter.key_snapshot("audit")["waiting"] == 1
        config.update(lambda c: c["apiKeys"]["audit"].update(allowMcp=False))
        await held.release()
        result = await asyncio.wait_for(task, 2)
        assert result.is_error
        assert apikey_limiter.key_snapshot("audit")["in_flight"] == 0
    finally:
        await held.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_exact_log_lookup_authorizes_before_db(monkeypatch):
    from src.management_auth import AuthMethod, ManagementPrincipal
    from src.management_control import ManagementContext, ManagementError
    from datetime import datetime, timezone
    actor = ManagementPrincipal.with_capabilities(subject_id="no-read", auth_method=AuthMethod.MANAGEMENT_KEY,
                                                  capabilities=(), issued_at=datetime.now(timezone.utc))
    ctx = ManagementContext(request_id="denied", actor=actor)
    monkeypatch.setattr(log_db, "mcp_call_entry", lambda *a, **k: pytest.fail("DB read without capability"))
    with pytest.raises(ManagementError):
        MCPControl().log_entry(ctx, "audit-page-0")


@pytest.mark.asyncio
async def test_fetch_cap_preserves_existing_global_truncation_warning(monkeypatch):
    async def extract(*args, **kwargs):
        return {"content": "x" * 2000, "truncated": True, "warnings": ["global_cap"]}
    monkeypatch.setattr(search_service, "extract", extract)
    value, _ = await ms._run_search("web_fetch", {"url": "https://public.invalid", "max_chars": 1000}, request_id="both-caps")
    assert value["warnings"] == ["global_cap", "content_truncated_to_mcp_max_chars"]
    assert len(value["content"]) == 1000 and value["truncated"]
    value, _ = await ms._run_search("web_fetch", {"url": "https://public.invalid", "max_chars": 4000}, request_id="global-cap")
    assert value["warnings"] == ["global_cap"] and len(value["content"]) == 2000
