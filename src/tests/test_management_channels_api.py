from __future__ import annotations

import ast
import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src import affinity, config, cooldown, log_db, provider_usage, state_db
from src.channel import registry
from src.management_api import (
    ManagementOriginMiddleware,
    ManagementRuntime,
    create_management_router,
    install_management_error_handlers,
)
from src.management_api.dependencies import get_management_context
from src.management_api.routers import channels as channels_router
from src.management_auth import (
    ApprovalService,
    AuthMethod,
    ManagementPrincipal,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import (
    ManagementContext,
    OperationRegistry,
    OperationStore,
    StoreAuditSink,
)
from src.management_control.channels import ChannelControl
from src.management_control.channels import service as channel_service
from src.openai.channel.registration import register_factories
from src.telegram import states
from src.telegram.menus import channel_menu


FAKE_KEY = "pmk_" + "C" * 64
FAKE_CHANNEL_SECRET = "sk-fake-management-channel-key"
MANIFEST = Path("src/tests/fixtures/management_api/operations/channels.txt")


class _Notifier:
    def send(self, admin_ids, notification):
        return True


def _build_app(tmp_path):
    clock = time.time
    store = ManagementStateStore(str(tmp_path / "management-channels.db"), clock=clock)
    sessions = SessionService(
        store,
        management_key=FAKE_KEY,
        policy=SessionPolicy(
            idle_timeout_seconds=3 * 24 * 60 * 60,
            absolute_timeout_seconds=30 * 24 * 60 * 60,
            touch_interval_seconds=300,
        ),
        clock=clock,
    )
    approvals = ApprovalService(
        store,
        clock=clock,
        ttl_seconds=180,
        admin_ids_provider=lambda: (42,),
        telegram_configured_provider=lambda: True,
        notifier=_Notifier(),
    )
    audit_sink = StoreAuditSink(store)
    operations = OperationStore(audit_sink=audit_sink)
    runtime = ManagementRuntime(
        sessions=sessions,
        approvals=approvals,
        operations=operations,
        operation_registry=OperationRegistry(operations),
        audit_sink=audit_sink,
        state_store=store,
        allowed_origins=frozenset({"https://admin.example.test"}),
        application_version="0.channels-test",
        documentation_url="https://docs.example.test/management-v1",
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    app.include_router(create_management_router([channels_router.router]))
    install_management_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ManagementOriginMiddleware)
    return app, runtime


def _session(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/management/v1/auth/sessions",
        json={"grantType": "managementKey", "managementKey": FAKE_KEY},
        headers={"X-Request-Id": "request-channel-session"},
    )
    assert response.status_code == 201, response.text
    credential = response.json()["data"]["credential"]
    return {"Authorization": f"Bearer {credential}"}


def _reset_channels() -> None:
    state_db.init()
    log_db.init()
    register_factories()
    state_db.error_delete()
    state_db.affinity_delete()
    state_db.client_affinity_delete()
    for module in (cooldown, affinity):
        module._initialized = False
    cooldown.init()
    affinity.init()
    affinity.client_init()
    config.update(lambda current: current.__setitem__("channels", []))
    registry.rebuild_from_config()
    states.clear_all()


@pytest.fixture(autouse=True)
def isolated_channel_state():
    _reset_channels()
    yield
    _reset_channels()


def _manual_create(name="Channel API"):
    return {
        "mode": "manual",
        "name": name,
        "baseUrl": "https://provider.example.test/v1/messages",
        "apiKey": FAKE_CHANNEL_SECRET,
        "protocol": "anthropic",
        "models": [{"real": "model-real", "alias": "model-alias"}],
        "maxConcurrent": 3,
        "compatibility": {
            "context1m": {"mode": "force", "models": ["model-real"]},
            "fast": {"mode": "auto", "models": []},
        },
    }


def _preset_create(name="Preset API"):
    return {
        "mode": "preset",
        "name": name,
        "providerId": "kimi",
        "providerPresetId": "code",
        "apiKey": FAKE_CHANNEL_SECRET,
        "protocol": "anthropic",
        "models": [{"real": "kimi-for-coding", "alias": "kimi-for-coding"}],
    }


ENDPOINTS = [
    ("get", "/api/management/v1/channels", None, {}),
    ("post", "/api/management/v1/channels", _manual_create(), {}),
    ("put", "/api/management/v1/channels/order", {"channelIds": []}, {"If-Match": "chorder_fake"}),
    ("post", "/api/management/v1/channels/actions/clear-errors", None, {}),
    ("post", "/api/management/v1/channels/actions/clear-affinity", None, {}),
    ("get", "/api/management/v1/channel-catalog", None, {}),
    ("post", "/api/management/v1/channel-model-discoveries", {
        "source": "existing", "channelId": "api:missing",
    }, {}),
    ("post", "/api/management/v1/channel-drafts/probes", {
        "name": "draft", "baseUrl": "https://provider.example.test",
        "apiKey": FAKE_CHANNEL_SECRET, "protocol": "anthropic", "model": "model-real",
    }, {}),
    ("get", "/api/management/v1/channels/api:missing", None, {}),
    ("patch", "/api/management/v1/channels/api:missing", {"enabled": False}, {}),
    ("delete", "/api/management/v1/channels/api:missing", None, {"If-Match": "chrev_fake"}),
    ("post", "/api/management/v1/channels/api:missing/diagnostic-probes", {"model": "model-real"}, {}),
    ("post", "/api/management/v1/channels/api:missing/actions/refresh-usage", None, {}),
    ("post", "/api/management/v1/channels/api:missing/actions/clear-errors", None, {}),
    ("post", "/api/management/v1/channels/api:missing/actions/clear-affinity", None, {}),
    ("get", "/api/management/v1/channels/api:missing/compatibility", None, {}),
    ("patch", "/api/management/v1/channels/api:missing/compatibility", {
        "context1m": {"mode": "auto", "models": []},
        "fast": {"mode": "auto", "models": []},
    }, {}),
]


def _request(client, method, path, body, headers):
    return client.request(method.upper(), path, json=body, headers=headers)


def _channel_operations(document):
    return {
        operation["operationId"]: operation
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
        and "management-channels" in operation.get("tags", [])
    }


def _nodes(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def test_each_router_operation_calls_control_once_with_management_context():
    tree = ast.parse(Path(channels_router.__file__).read_text(encoding="utf-8"))
    calls_by_operation = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        operation_id = None
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "operation_id" and isinstance(keyword.value, ast.Constant):
                    operation_id = keyword.value.value
        if operation_id is None:
            continue
        calls = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "control"
        ]
        calls_by_operation[operation_id] = calls
    assert set(calls_by_operation) == set(MANIFEST.read_text(encoding="utf-8").splitlines())
    for operation_id, calls in calls_by_operation.items():
        assert len(calls) == 1, operation_id
        assert calls[0].args and isinstance(calls[0].args[0], ast.Name), operation_id
        assert calls[0].args[0].id == "context", operation_id


def test_openapi_operation_manifest_examples_strict_schemas_and_write_only_secrets(tmp_path):
    app, runtime = _build_app(tmp_path)
    document = app.openapi()
    operations = _channel_operations(document)
    expected = set(MANIFEST.read_text(encoding="utf-8").splitlines())
    assert set(operations) == expected
    assert all(operation.get("responses") for operation in operations.values())
    assert all(
        any(
            "example" in media
            for response in operation["responses"].values()
            for media in response.get("content", {}).values()
        )
        or "204" in operation["responses"]
        for operation in operations.values()
    )
    api_key_schemas = [
        node["apiKey"]
        for node in _nodes(document)
        if isinstance(node, dict) and "apiKey" in node and isinstance(node["apiKey"], dict)
    ]
    assert api_key_schemas and all(schema.get("writeOnly") is True for schema in api_key_schemas)
    channel_data = document["components"]["schemas"]["ChannelData"]
    assert "apiKey" not in channel_data["properties"]
    assert channel_data["additionalProperties"] is False
    datetime_fields = (
        ("ProviderUsageData", "fetchedAt"),
        ("ProviderUsageData", "errorAt"),
        ("ProviderUsageMetricData", "resetAt"),
        ("ProviderUsageMetricData", "startAt"),
        ("ProviderUsageMetricData", "endAt"),
        ("ChannelRuntimeModelData", "cooldownUntil"),
    )
    for schema_name, field_name in datetime_fields:
        schema = document["components"]["schemas"][schema_name]["properties"][field_name]
        assert any(
            node.get("type") == "string" and node.get("format") == "date-time"
            for node in _nodes(schema) if isinstance(node, dict)
        ), (schema_name, field_name, schema)
    assert FAKE_CHANNEL_SECRET not in json.dumps(document)
    runtime.close()


@pytest.mark.parametrize("method,path,body,headers", ENDPOINTS)
def test_every_channel_operation_requires_a_session(tmp_path, method, path, body, headers):
    app, runtime = _build_app(tmp_path)
    with TestClient(app) as client:
        response = _request(client, method, path, body, headers)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "SESSION_REQUIRED"
    runtime.close()


@pytest.mark.parametrize("method,path,body,headers", ENDPOINTS)
def test_every_channel_operation_rejects_missing_capability(tmp_path, method, path, body, headers):
    app, runtime = _build_app(tmp_path)
    principal = ManagementPrincipal.with_capabilities(
        subject_id="no-channel-capabilities",
        auth_method=AuthMethod.MANAGEMENT_KEY,
        capabilities=(),
        issued_at=datetime.now(timezone.utc),
        session_id="session-no-capabilities",
    )

    async def no_capability_context():
        return ManagementContext(request_id="request-no-capability", actor=principal)

    app.dependency_overrides[get_management_context] = no_capability_context
    with TestClient(app) as client:
        response = _request(client, method, path, body, headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "CAPABILITY_DENIED"
    runtime.close()


def _wait_operation(client, headers, operation_id):
    last = None
    for _ in range(100):
        last = client.get(
            f"/api/management/v1/operations/{operation_id}", headers=headers,
        )
        assert last.status_code == 200, last.text
        if last.json()["data"]["status"] in {"succeeded", "failed", "cancelled"}:
            return last.json()["data"]
        time.sleep(0.01)
    raise AssertionError(f"operation did not terminate: {last.text if last else operation_id}")


def test_all_channel_operations_happy_paths_side_effects_and_secret_redaction(tmp_path, monkeypatch):
    app, runtime = _build_app(tmp_path)

    async def successful_probe(*args, **kwargs):
        return True, 12, None

    monkeypatch.setattr(channel_service.probe, "probe_with_progress", successful_probe)
    monkeypatch.setattr(channel_service.provider_usage, "schedule_refresh", lambda *args, **kwargs: True)
    seen = {}

    with TestClient(app) as client:
        auth = _session(client)

        response = client.get("/api/management/v1/channel-catalog", headers=auth)
        assert response.status_code == 200
        seen["getChannelCatalog"] = 200

        response = client.post(
            "/api/management/v1/channels", json=_preset_create(), headers=auth,
        )
        assert response.status_code == 201, response.text
        first = response.json()["data"]
        assert first["id"] == "api:Preset API"
        assert "apiKey" not in first
        assert FAKE_CHANNEL_SECRET not in response.text
        seen["createChannel"] = 201

        response = client.post(
            "/api/management/v1/channels", json=_manual_create("Manual API"), headers=auth,
        )
        assert response.status_code == 201, response.text
        second = response.json()["data"]

        response = client.get(
            "/api/management/v1/channels?page=1&pageSize=1&sort=name&direction=asc",
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 2
        assert response.json()["meta"]["hasNext"] is True
        seen["listChannels"] = 200

        response = client.get("/api/management/v1/channels/api:Preset%20API", headers=auth)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["monthStats"]["total"] == 0
        seen["getChannel"] = 200

        response = client.get(
            "/api/management/v1/channels/api:Preset%20API/compatibility", headers=auth,
        )
        assert response.status_code == 200
        compatibility_revision = response.json()["data"]["revision"]
        seen["getChannelCompatibility"] = 200

        response = client.patch(
            "/api/management/v1/channels/api:Preset%20API/compatibility",
            json={
                "context1m": {"mode": "force", "models": ["kimi-for-coding"]},
                "fast": {"mode": "auto", "models": []},
            },
            headers={**auth, "If-Match": compatibility_revision},
        )
        assert response.status_code == 200, response.text
        seen["updateChannelCompatibility"] = 200

        revision = response.json()["data"]["revision"]
        response = client.patch(
            "/api/management/v1/channels/api:Preset%20API",
            json={"enabled": False, "maxConcurrent": 8},
            headers={**auth, "If-Match": revision},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["enabled"] is False
        seen["updateChannel"] = 200

        response = client.get("/api/management/v1/channels", headers=auth)
        order_meta = response.json()["meta"]
        ids = [item["id"] for item in response.json()["data"]]
        response = client.put(
            "/api/management/v1/channels/order",
            json={"channelIds": list(reversed(ids))},
            headers={**auth, "If-Match": order_meta["orderRevision"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["channelIds"] == list(reversed(ids))
        seen["reorderChannels"] = 200

        cooldown.record_error("api:Preset API", "kimi-for-coding", "fixed failure")
        affinity.upsert("api-server", "api:Preset API", "kimi-for-coding")
        affinity.client_upsert("api-client", "api:Preset API", "kimi-for-coding")
        response = client.post(
            "/api/management/v1/channels/api:Preset%20API/actions/clear-errors", headers=auth,
        )
        assert response.status_code == 200 and response.json()["data"]["affected"] == 1
        seen["clearChannelErrors"] = 200
        response = client.post(
            "/api/management/v1/channels/api:Preset%20API/actions/clear-affinity", headers=auth,
        )
        assert response.status_code == 200 and response.json()["data"]["affected"] == 2
        seen["clearChannelAffinity"] = 200

        cooldown.record_error("api:Manual API", "model-real", "fixed failure")
        affinity.upsert("all-server", "api:Manual API", "model-real")
        response = client.post(
            "/api/management/v1/channels/actions/clear-errors", headers=auth,
        )
        assert response.status_code == 200 and response.json()["data"]["affected"] == 1
        seen["clearAllChannelErrors"] = 200
        response = client.post(
            "/api/management/v1/channels/actions/clear-affinity", headers=auth,
        )
        assert response.status_code == 200 and response.json()["data"]["affected"] == 1
        seen["clearAllChannelAffinity"] = 200

        operation_requests = {
            "discoverChannelModels": client.post(
                "/api/management/v1/channel-model-discoveries",
                json={
                    "source": "draft", "baseUrl": "https://unused.example.test",
                    "apiKey": FAKE_CHANNEL_SECRET, "protocol": "anthropic",
                    "providerId": "kimi", "providerPresetId": "code",
                },
                headers=auth,
            ),
            "probeChannelDraft": client.post(
                "/api/management/v1/channel-drafts/probes",
                json={
                    "name": "api-draft", "baseUrl": "https://provider.example.test",
                    "apiKey": FAKE_CHANNEL_SECRET, "protocol": "anthropic",
                    "model": "model-real",
                },
                headers=auth,
            ),
            "probeExistingChannel": client.post(
                "/api/management/v1/channels/api:Preset%20API/diagnostic-probes",
                json={"model": "kimi-for-coding"}, headers=auth,
            ),
            "refreshChannelProviderUsage": client.post(
                "/api/management/v1/channels/api:Preset%20API/actions/refresh-usage",
                headers=auth,
            ),
        }
        for operation_id, response in operation_requests.items():
            assert response.status_code == 202, (operation_id, response.text)
            assert FAKE_CHANNEL_SECRET not in response.text
            operation = _wait_operation(client, auth, response.json()["data"]["id"])
            assert operation["status"] == "succeeded", (operation_id, operation)
            assert FAKE_CHANNEL_SECRET not in json.dumps(operation)
            seen[operation_id] = 202

        response = client.delete(
            "/api/management/v1/channels/api:Manual%20API",
            headers={**auth, "If-Match": second["revision"]},
        )
        assert response.status_code == 204, response.text
        assert registry.get_channel("api:Manual API") is None
        seen["deleteChannel"] = 204

    assert set(seen) == set(MANIFEST.read_text(encoding="utf-8").splitlines())
    runtime.close()


def test_validation_missing_resource_and_revision_errors_use_stable_contract(tmp_path):
    app, runtime = _build_app(tmp_path)
    with TestClient(app) as client:
        auth = _session(client)
        response = client.get(
            "/api/management/v1/channels?pageSize=201&sort=not-a-sort", headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert response.json()["error"]["fields"]

        invalid = _manual_create()
        invalid["unknownField"] = True
        response = client.post("/api/management/v1/channels", json=invalid, headers=auth)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert any("unknownField" in field["path"] for field in response.json()["error"]["fields"])

        response = client.get("/api/management/v1/channels/api:missing", headers=auth)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        for path, body in (
            ("/api/management/v1/channel-model-discoveries", {
                "source": "existing", "channelId": "api:missing",
            }),
            ("/api/management/v1/channels/api:missing/diagnostic-probes", {
                "model": "model-real",
            }),
            ("/api/management/v1/channels/api:missing/actions/refresh-usage", None),
        ):
            response = client.post(path, json=body, headers=auth)
            assert response.status_code == 404, response.text
            assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        created = client.post(
            "/api/management/v1/channels", json=_manual_create("Revision API"), headers=auth,
        ).json()["data"]
        response = client.post(
            "/api/management/v1/channels/api:Revision%20API/diagnostic-probes",
            json={"model": "unknown-model"}, headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        response = client.post(
            "/api/management/v1/channels/api:Revision%20API/actions/refresh-usage",
            headers=auth,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_VALUE"
        response = client.patch(
            "/api/management/v1/channels/api:Revision%20API",
            json={"enabled": False}, headers={**auth, "If-Match": "chrev_stale"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVISION_CONFLICT"
        response = client.delete(
            "/api/management/v1/channels/api:Revision%20API",
            headers={**auth, "If-Match": "chrev_stale"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVISION_CONFLICT"
        assert registry.get_channel(created["id"]) is not None
    runtime.close()


def test_channel_read_and_toggle_parity_between_telegram_and_management_api(tmp_path, monkeypatch):
    app, runtime = _build_app(tmp_path)
    control = ChannelControl()
    control.create_channel(
        channel_menu._ctx(7),
        channel_service.ChannelCreateCommand(
            name="Parity API",
            base_url="https://provider.example.test/v1/messages",
            api_key=FAKE_CHANNEL_SECRET,
            protocol=channel_service.ChannelProtocol.ANTHROPIC,
            models=(channel_service.ChannelModel(real="model-real", alias="model-alias"),),
        ),
    )
    original = copy.deepcopy(config.get()["channels"])

    with TestClient(app) as client:
        auth = _session(client)
        response = client.get("/api/management/v1/channels", headers=auth)
        assert response.status_code == 200
        api_row = response.json()["data"][0]
        tg_row = channel_menu._all_channels(7)[0]
        assert (api_row["id"], api_row["enabled"], api_row["protocol"], api_row["modelCount"]) == (
            tg_row.id, tg_row.enabled, tg_row.protocol.value, len(tg_row.models),
        )

        short = channel_menu.ui.register_code("Parity API")
        monkeypatch.setattr(channel_menu.ui, "answer_cb", lambda *args, **kwargs: None)
        monkeypatch.setattr(channel_menu, "_detail_text_and_kb", lambda *args, **kwargs: (None, None))
        channel_menu.on_toggle(7, 99, "callback", short)
        telegram_result = copy.deepcopy(config.get()["channels"])

        config.update(lambda current: current.__setitem__("channels", copy.deepcopy(original)))
        registry.rebuild_from_config()
        revision = client.get(
            "/api/management/v1/channels/api:Parity%20API", headers=auth,
        ).json()["data"]["revision"]
        response = client.patch(
            "/api/management/v1/channels/api:Parity%20API",
            json={"enabled": False}, headers={**auth, "If-Match": revision},
        )
        assert response.status_code == 200, response.text
        api_result = copy.deepcopy(config.get()["channels"])

    assert api_result == telegram_result
    runtime.close()


def test_channel_control_lifetime_is_scoped_to_app_runtime_with_injection_seam(tmp_path):
    app, runtime = _build_app(tmp_path)
    other_root = tmp_path / "other-runtime"
    other_root.mkdir()
    _, other_runtime = _build_app(other_root)
    request = SimpleNamespace(app=app)

    first = channels_router.get_channel_control(request, runtime)
    assert channels_router.get_channel_control(request, runtime) is first
    assert first._operation_registry is runtime.operation_registry
    assert first._operation_store is runtime.operations
    assert first._audit_sink is runtime.audit_sink

    replacement = channels_router.get_channel_control(request, other_runtime)
    assert replacement is not first
    assert replacement._operation_registry is other_runtime.operation_registry
    assert app.state.management_channel_control_runtime is other_runtime

    injected = ChannelControl()
    app.state.management_channel_control = injected
    del app.state.management_channel_control_runtime
    assert channels_router.get_channel_control(request, runtime) is injected
    assert not hasattr(channels_router, "_controls")

    runtime.close()
    other_runtime.close()


def test_channel_api_normalizes_all_absolute_times_to_rfc3339_utc(tmp_path, monkeypatch):
    snapshot = {
        "version": 1,
        "source": "fixed-provider",
        "balances": [],
        "windows": [{
            "id": "fixed-window",
            "label": "Fixed window",
            "kind": "window",
            "reset_at": "2023-11-15T06:13:20+08:00",
            "start_at": "2023-11-14T22:13:20Z",
            "end_at": "3600",
        }],
        "counters": [],
        "notices": [],
        "partial": False,
    }
    monkeypatch.setattr(channel_service.provider_usage, "spec_for", lambda channel: object())
    monkeypatch.setattr(channel_service.provider_usage, "cached", lambda channel: {
        "status": "fresh",
        "fetched_at": 1_700_000_000_000,
        "error_at": 1_700_003_600_000,
        "snapshot": snapshot,
    })
    app, runtime = _build_app(tmp_path)
    body = _manual_create("UTC Channel")
    monkeypatch.setattr(
        channel_service.quota_errors,
        "active_quota_cooldown",
        lambda row, now_ms=None: row.get("model") == "quota-model",
    )
    body["models"] = [
        {"real": "permanent-model", "alias": "permanent-model"},
        {"real": "temporary-model", "alias": "temporary-model"},
        {"real": "quota-model", "alias": "quota-model"},
    ]
    with TestClient(app) as client:
        auth = _session(client)
        created = client.post(
            "/api/management/v1/channels", json=body, headers=auth,
        )
        assert created.status_code == 201, created.text
        cooldown.record_error(
            "api:UTC Channel", "permanent-model", "fixed", cooldown_until=-1,
        )
        cooldown.record_error(
            "api:UTC Channel", "temporary-model", "fixed",
            cooldown_until=4_102_444_800_000,
        )
        cooldown.record_error(
            "api:UTC Channel", "quota-model", "fixed",
            cooldown_until=4_102_448_400_000,
        )
        response = client.get(
            "/api/management/v1/channels/api:UTC%20Channel", headers=auth,
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    usage = data["providerUsage"]
    assert usage["fetchedAt"] == "2023-11-14T22:13:20Z"
    assert usage["errorAt"] == "2023-11-14T23:13:20Z"
    window = usage["snapshot"]["windows"][0]
    assert window["resetAt"] == "2023-11-14T22:13:20Z"
    assert window["startAt"] == "2023-11-14T22:13:20Z"
    assert window["endAt"] is None
    runtime_models = {item["real"]: item for item in data["runtimeModels"]}
    assert runtime_models["permanent-model"]["cooldownKind"] == "permanent"
    assert runtime_models["permanent-model"]["cooldownUntil"] is None
    assert runtime_models["temporary-model"]["cooldownKind"] == "temporary"
    assert runtime_models["temporary-model"]["cooldownUntil"] == "2100-01-01T00:00:00Z"
    assert runtime_models["quota-model"]["cooldownKind"] == "quota"
    assert runtime_models["quota-model"]["cooldownUntil"] == "2100-01-01T01:00:00Z"
    assert "-1" not in json.dumps(runtime_models["permanent-model"])
    assert "+08:00" not in response.text
    runtime.close()


def test_runtime_audit_and_http_polled_failures_are_stable_and_secret_free(tmp_path, monkeypatch):
    app, runtime = _build_app(tmp_path)
    exception_marker = "fixed-original-exception"

    async def exploding_discovery(*args, **kwargs):
        raise RuntimeError(f"{exception_marker}:{FAKE_CHANNEL_SECRET}")

    async def exploding_probe(*args, **kwargs):
        raise RuntimeError(f"{exception_marker}:{FAKE_CHANNEL_SECRET}")

    monkeypatch.setattr(channel_service, "run_model_discovery", exploding_discovery)
    monkeypatch.setattr(channel_service.probe, "probe_with_progress", exploding_probe)
    wire_payloads = []
    with TestClient(app) as client:
        auth = _session(client)
        create_response = client.post(
            "/api/management/v1/channels",
            json=_manual_create("Audited Channel"),
            headers={**auth, "X-Request-Id": "request-channel-create-audit"},
        )
        assert create_response.status_code == 201, create_response.text
        wire_payloads.append(create_response.text)

        requests = (
            ("request-channel-discovery-failure", "/api/management/v1/channel-model-discoveries", {
                "source": "draft",
                "baseUrl": "https://provider.example.test",
                "apiKey": FAKE_CHANNEL_SECRET,
                "protocol": "anthropic",
            }),
            ("request-channel-probe-failure", "/api/management/v1/channel-drafts/probes", {
                "name": "failure-draft",
                "baseUrl": "https://provider.example.test",
                "apiKey": FAKE_CHANNEL_SECRET,
                "protocol": "anthropic",
                "model": "model-real",
            }),
        )
        failed_operations = []
        for request_id, path, body in requests:
            submitted = client.post(
                path, json=body, headers={**auth, "X-Request-Id": request_id},
            )
            assert submitted.status_code == 202, submitted.text
            wire_payloads.append(submitted.text)
            terminal = _wait_operation(client, auth, submitted.json()["data"]["id"])
            failed_operations.append((request_id, terminal))
            wire_payloads.append(json.dumps(terminal))

    for request_id, operation in failed_operations:
        assert operation["status"] == "failed", (request_id, operation)
        assert operation["error"]["code"] == "UPSTREAM_ERROR"
        assert operation["error"]["retryable"] is True
        assert "具体原因尚不确定" in operation["error"]["message"]
        assert "channel." in operation["error"]["message"]
        assert operation["result"] is None
    audits = runtime.state_store.audit_snapshot()
    channel_audit = next(
        row for row in audits
        if row["action"] == "channel.create" and row["target"] == "api:Audited Channel"
    )
    assert channel_audit == {
        "actor": "administrator",
        "action": "channel.create",
        "target": "api:Audited Channel",
        "result": "succeeded",
        "request_id": "request-channel-create-audit",
        "occurred_at": channel_audit["occurred_at"],
    }
    assert channel_audit["occurred_at"] > 0
    operation_audits = {
        row["request_id"]: row for row in audits
        if row["action"] == "operation.create"
        and row["request_id"] in {item[0] for item in failed_operations}
    }
    assert set(operation_audits) == {item[0] for item in failed_operations}
    assert all(
        row["actor"] == "administrator"
        and row["target"].startswith("op_")
        and row["result"] == "queued"
        and row["occurred_at"] > 0
        for row in operation_audits.values()
    )
    evidence = json.dumps(audits) + "".join(wire_payloads)
    assert FAKE_CHANNEL_SECRET not in evidence
    assert exception_marker not in evidence
    runtime.close()


def test_all_channel_routes_reject_unknown_query_and_list_allows_only_declared_names(tmp_path):
    app, runtime = _build_app(tmp_path)
    with TestClient(app) as client:
        auth = _session(client)
        allowed = client.get(
            "/api/management/v1/channels",
            params={
                "page": 1, "pageSize": 50, "search": "x", "enabled": True,
                "protocol": "anthropic", "providerId": "provider", "health": "unknown",
                "sort": "order", "direction": "asc",
            },
            headers=auth,
        )
        assert allowed.status_code == 200, allowed.text
        assert len(ENDPOINTS) == len(MANIFEST.read_text(encoding="utf-8").splitlines())
        for method, path, body, headers in ENDPOINTS:
            separator = "&" if "?" in path else "?"
            response = _request(
                client, method, f"{path}{separator}unexpectedFilter=1", body,
                {**auth, **headers},
            )
            assert response.status_code == 422, (method, path, response.text)
            assert response.json()["error"] == {
                "code": "VALIDATION_FAILED",
                "message": "Request validation failed",
                "fields": [{
                    "path": "unexpectedFilter",
                    "code": "unknown",
                    "message": "Unknown query parameter",
                }],
                "retryable": False,
                "requestId": response.headers["X-Request-Id"],
                "operationId": None,
            }
    runtime.close()


def test_all_four_channel_url_inputs_reject_username_or_password_userinfo(tmp_path):
    app, runtime = _build_app(tmp_path)
    markers = (
        "create-user-marker", "update-user-marker", "update-password-marker",
        "discovery-user-marker", "probe-user-marker", "probe-password-marker",
    )
    with TestClient(app) as client:
        auth = _session(client)
        created = client.post(
            "/api/management/v1/channels", json=_manual_create("URL Input API"), headers=auth,
        )
        assert created.status_code == 201, created.text
        create_body = _manual_create("Rejected URL Input")
        create_body["baseUrl"] = "https://create-user-marker@provider.example.test/v1/messages"
        requests = (
            ("post", "/api/management/v1/channels", create_body),
            ("patch", "/api/management/v1/channels/api:URL%20Input%20API", {
                "baseUrl": "https://update-user-marker:update-password-marker@[2001:db8::1]:8443/v1/messages",
            }),
            ("post", "/api/management/v1/channel-model-discoveries", {
                "source": "draft", "baseUrl": "https://discovery-user-marker@provider.example.test",
                "apiKey": FAKE_CHANNEL_SECRET, "protocol": "anthropic",
            }),
            ("post", "/api/management/v1/channel-drafts/probes", {
                "baseUrl": "https://probe-user-marker:probe-password-marker@provider.example.test",
                "apiKey": FAKE_CHANNEL_SECRET, "protocol": "anthropic", "model": "model-real",
            }),
        )
        wire = []
        for method, path, body in requests:
            response = client.request(method.upper(), path, json=body, headers=auth)
            wire.append(response.text)
            assert response.status_code == 422, (method, path, response.text)
            assert response.json()["error"]["code"] == "VALIDATION_FAILED"
            assert any(field["path"].endswith("baseUrl") for field in response.json()["error"]["fields"])
        assert not runtime.operations._items
        evidence = "".join(wire) + json.dumps(runtime.state_store.audit_snapshot()) + json.dumps(app.openapi())
        assert all(marker not in evidence for marker in markers)
    runtime.close()


def test_legacy_url_userinfo_is_stripped_from_read_and_mutation_without_config_change(tmp_path):
    legacy_url = "https://legacy-user-marker:legacy-password-marker@[2001:db8::7]:8443/root"
    safe_base = "https://[2001:db8::7]:8443/root"
    assert channels_router.strip_url_userinfo("https://user:password@[bad") == ""
    assert channels_router.strip_url_userinfo("https://user:password@host:invalid/path") == ""
    ChannelControl().create_channel(
        channel_menu._ctx(7),
        channel_service.ChannelCreateCommand(
            name="Legacy URL API", base_url=legacy_url, api_path="/v1/messages",
            api_key=FAKE_CHANNEL_SECRET, protocol=channel_service.ChannelProtocol.ANTHROPIC,
            models=(channel_service.ChannelModel(real="model-real", alias="model-real"),),
        ),
    )
    assert config.get()["channels"][0]["baseUrl"] == legacy_url
    app, runtime = _build_app(tmp_path)
    with TestClient(app) as client:
        auth = _session(client)
        listed = client.get("/api/management/v1/channels", headers=auth)
        detailed = client.get("/api/management/v1/channels/api:Legacy%20URL%20API", headers=auth)
        revision = detailed.json()["data"]["revision"]
        mutated = client.patch(
            "/api/management/v1/channels/api:Legacy%20URL%20API",
            json={"enabled": False}, headers={**auth, "If-Match": revision},
        )
    for response, data in (
        (listed, listed.json()["data"][0]),
        (detailed, detailed.json()["data"]),
        (mutated, mutated.json()["data"]),
    ):
        assert response.status_code == 200, response.text
        assert data["baseUrl"] == safe_base
        assert data["url"] == safe_base + "/v1/messages"
        assert "legacy-user-marker" not in response.text
        assert "legacy-password-marker" not in response.text
    assert config.get()["channels"][0]["baseUrl"] == legacy_url
    runtime.close()
