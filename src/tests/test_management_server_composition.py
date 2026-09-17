from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server
from src.management_api import ManagementRuntime, install_management_routers


EXPECTED_OPERATIONS = frozenset(
    (Path(__file__).parent / "fixtures/management_api/production-operation-ids.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)


def settings(tmp_path):
    return {
        "managementKey": "pmk_" + "S" * 64,
        "stateDbPath": str(tmp_path / "management-composition.db"),
        "allowedOrigins": (),
        "sessionIdleTimeoutSeconds": 3 * 24 * 60 * 60,
        "sessionAbsoluteTimeoutSeconds": 30 * 24 * 60 * 60,
        "sessionTouchIntervalSeconds": 300,
        "telegramApprovalTtlSeconds": 180,
        "authRateLimitWindowSeconds": 60,
        "authRateLimitPerSource": 5,
        "authRateLimitGlobal": 30,
        "maxOperations": 100,
        "maxAuditRecords": 100,
    }


def test_management_routes_are_cloned_directly_into_the_application(monkeypatch):
    app = FastAPI()
    included = []
    include_router = app.include_router

    def record_direct_mount(router, *args, **kwargs):
        included.append((router, kwargs.get("prefix")))
        return include_router(router, *args, **kwargs)

    monkeypatch.setattr(app, "include_router", record_direct_mount)
    install_management_routers(app)

    # Foundation plus 23 ordered domain routers, including search and MCP. Assert the
    # identities/order, not only a count that could conceal a missing domain.
    expected_router_names = (
        "foundation", "oauth", "channels", "apikey", "overview", "status", "stats",
        "retention", "logs", "media", "mapping", "model_metadata", "models",
        "load_balancing", "proxy", "translation", "status_alerts", "updates",
        "media_settings", "system_settings", "search", "content_blacklist", "network",
        "mcp",
    )
    assert [router for router, _prefix in included] == [
        import_module("src.management_api.routers." + name).router for name in expected_router_names
    ]
    assert len(included) == 24
    assert all(prefix == "/api/management/v1" for _router, prefix in included)
    # FastAPI may retain nested includes (such as the WorkBuddy router) in
    # router.routes as well as app.routes, rather than flattening them. Count
    # the complete public operations below, not internal router objects.
    operations = [
        (method.upper(), path, operation["operationId"])
        for path, path_item in app.openapi()["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in path_item.items()
        if method in {"get", "post", "delete", "put", "patch"}
    ]
    assert len(operations) == 237
    assert len({(method, path) for method, path, _operation in operations}) == 237
    assert {operation for _method, _path, operation in operations} == EXPECTED_OPERATIONS

    source = Path("server.py").read_text()
    assert "install_management_routers(app)" in source
    assert "app.include_router(create_management_router())" not in source


def test_server_mounts_all_domain_routers_and_preserves_lifecycle_order():
    document = server.app.openapi()
    operations = [
        (method.upper(), path, operation["operationId"])
        for path, path_item in document["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in path_item.items()
        if method in {"get", "post", "delete", "put", "patch"}
    ]
    operation_ids = [operation_id for _method, _path, operation_id in operations]
    method_paths = [(method, path) for method, path, _operation_id in operations]
    assert len(operations) == 237
    assert len(set(operation_ids)) == 237
    assert len(set(method_paths)) == 237
    assert len({path for _method, path in method_paths}) == 175  # OAuth default-models retirement removes two paths
    assert set(operation_ids) == EXPECTED_OPERATIONS

    source = Path("server.py").read_text()
    assert source.index("state_db.init()") < source.index("network.init()")
    assert source.index("network.init()") < source.index("log_db.init()")
    assert source.index("log_db.init()") < source.index("image_db.init()")
    assert source.index("image_db.init()") < source.index("translation.init()")
    assert source.index("translation.init()") < source.index("_initialize_management_runtime(app)")
    assert source.index("provider_usage.schedule_startup_refresh()") < source.index("tgbot.start()")
    stop = source.index("await tgbot.stop_async()")
    assert stop < source.index("await _close_management_runtime(app)", stop)
    assert source.index("await _close_management_runtime(app)", stop) < source.index(
        "await provider_usage.stop()", stop,
    )


def test_management_initialization_and_shutdown_are_isolated(tmp_path, monkeypatch, capsys):
    app = FastAPI()
    values = settings(tmp_path)
    monkeypatch.setattr(server.config, "management_settings", lambda: values)
    runtime = server._initialize_management_runtime(app)
    assert isinstance(runtime, ManagementRuntime)
    owner = runtime.control_owner()
    assert app.state.management_runtime is runtime
    assert app.state.management_controls is owner
    assert app.state.management_controls_runtime is runtime
    assert server.tgbot.model_center_menu._CONTROL is owner.models
    assert owner.models.mapping is owner.mapping
    assert owner.models.images is owner.auxiliary.images
    assert owner.models.xai_media is owner.auxiliary.xai_media
    assert owner.models.antigravity_media is owner.auxiliary.antigravity_media
    assert server.tgbot.mapping_menu.mapping_control is owner.mapping
    assert server.tgbot.system_menu._settings_control is owner.system.settings
    assert server.tgbot.system_menu._runtime_control is owner.system_runtime
    assert (tmp_path / "management-composition.db").exists()
    output = capsys.readouterr().out
    assert values["managementKey"] not in output
    asyncio.run(server._close_management_runtime(app))
    assert app.state.management_runtime is None
    assert app.state.management_controls is None
    assert server.tgbot.model_center_menu._CONTROL is not owner.models
    assert server.tgbot.mapping_menu.mapping_control is not owner.mapping
    assert server.tgbot.system_menu._settings_control is not owner.system.settings
    assert server.tgbot._management_approval_handler is None


def test_server_metadata_links_to_served_fastapi_documentation(
    tmp_path, monkeypatch,
):
    values = settings(tmp_path)
    monkeypatch.setattr(server.config, "management_settings", lambda: values)
    holder = FastAPI()
    runtime = server._initialize_management_runtime(holder)
    assert isinstance(runtime, ManagementRuntime)
    issued = runtime.sessions.create_from_management_key(
        values["managementKey"],
        source="composition-test",
        request_id="documentation-test",
    )

    original_runtime = server.app.state.management_runtime
    server.drain.reset_for_tests()
    server.app.state.management_runtime = runtime
    try:
        client = TestClient(server.app)
        metadata = client.get(
            "/api/management/v1/meta",
            headers={"Authorization": f"Bearer {issued.credential}"},
        )
        assert metadata.status_code == 200
        documentation_url = metadata.json()["data"]["documentationUrl"]
        assert documentation_url == "/docs"
        documentation = client.get(documentation_url)
        assert documentation.status_code == 200
        assert "/openapi.json" in documentation.text
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        assert "/api/management/v1/meta" in openapi.json()["paths"]
    finally:
        server.app.state.management_runtime = original_runtime
        asyncio.run(server._close_management_runtime(holder))
        server.drain.reset_for_tests()


def test_runtime_close_orders_operations_before_store_and_is_idempotent():
    class Operations:
        def __init__(self, events):
            self.events = events

        def close(self):
            self.events.append("operations")

        async def aclose(self):
            self.events.append("operations")

    class StateStore:
        def __init__(self, events):
            self.events = events

        def close(self):
            self.events.append("store")

    for asynchronous in (False, True):
        events = []
        runtime = ManagementRuntime(
            sessions=None,
            approvals=None,
            operations=Operations(events),
            operation_registry=None,
            audit_sink=None,
            state_store=StateStore(events),
            allowed_origins=frozenset(),
            application_version="test",
            documentation_url="/docs",
        )
        if asynchronous:
            asyncio.run(runtime.aclose())
            asyncio.run(runtime.aclose())
        else:
            runtime.close()
            runtime.close()
        assert events == ["operations", "store"]


def test_management_initialization_failure_stays_fail_closed_and_inference_health_works(
    monkeypatch,
):
    app = FastAPI()

    def fail_settings():
        raise ValueError("fake-sensitive-management-config-value")

    monkeypatch.setattr(server.config, "management_settings", fail_settings)
    assert server._initialize_management_runtime(app) is None
    assert app.state.management_runtime is None

    original = server.app.state.management_runtime
    server.drain.reset_for_tests()
    server.app.state.management_runtime = None
    # This test intentionally skips lifespan. Model a ready inference runtime
    # independently of the unavailable management runtime under test.
    monkeypatch.setattr(server.upstream, "client_health", lambda: {"state": "ready", "ready": True})
    try:
        client = TestClient(server.app)
        management = client.get("/api/management/v1/meta")
        assert management.status_code == 503
        assert management.json()["error"]["code"] == "SERVICE_NOT_READY"
        inference_health = client.get("/health")
        assert inference_health.status_code == 200
        assert "status" in inference_health.json()
    finally:
        server.app.state.management_runtime = original
        server.drain.reset_for_tests()
