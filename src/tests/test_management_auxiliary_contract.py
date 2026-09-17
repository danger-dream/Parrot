from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import config
from src.management_api.routers.auxiliary_support import get_bound_auxiliary_controls
from src.management_api.routers.updates import router as updates_router
from src.management_auth import AuthMethod
from src.tests.management_auxiliary_support import bearer, build_auxiliary_app, create_session
from src.tests.test_management_api_foundation import build_app, EXPECTED_OPERATIONS as FOUNDATION_OPERATION_IDS


ROUTER_FILES = (
    Path("src/management_api/routers/translation.py"),
    Path("src/management_api/routers/status_alerts.py"),
    Path("src/management_api/routers/updates.py"),
    Path("src/management_api/routers/media_settings.py"),
)
_HTTP_METHODS = {"get", "patch", "put", "post", "delete"}
_REQUEST_BODIES = {
    "updateTranslationSettings": {"enabled": False},
    "testTranslation": {"text": "hello"},
    "updateStatusAlertSettings": {"enabled": True},
    "updateSettings": {"enabled": True},
    "activateStagedUpdate": {"planToken": "not-a-real-plan-token"},
    "updateImageSettings": {"enabled": True},
    "updateVideoSettings": {"enabled": True},
    "updateMediaSource": {"enabled": True},
    "updateImageAccountState": {"enabled": True},
    "updateXaiMediaSettings": {"jobTtlSeconds": 7200},
    "addXaiMediaModel": {"modelId": "grok-new"},
    "renameXaiMediaModel": {"newModelId": "grok-renamed"},
}
_REQUEST_HEADERS = {
    "stageUpdate": {"Idempotency-Key": "stage-one"},
    "activateStagedUpdate": {
        "Idempotency-Key": "activate-one",
        "If-Match": "rev_fake",
    },
    "addXaiMediaModel": {"If-Match": "rev_fake"},
    "renameXaiMediaModel": {"If-Match": "rev_fake"},
    "removeXaiMediaModel": {"If-Match": "rev_fake"},
}
_PATH_VALUES = {
    "{incidentId}": "inc-1",
    "{version}": "0.32.0",
    "{accountId}": "openai%3Auser%40example.com",
    "{kind}": "image",
    "{modelId:path}": "model-a",
    "{sourceId:path}": "oauth:xai:test",
}


def _operation_manifest():
    manifest = []
    for source_path in ROUTER_FILES:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                    and decorator.func.attr in _HTTP_METHODS
                ):
                    continue
                operation_id = next(
                    keyword.value.value
                    for keyword in decorator.keywords
                    if keyword.arg == "operation_id"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                )
                manifest.append({
                    "method": decorator.func.attr,
                    "path": decorator.args[0].value,
                    "operation_id": operation_id,
                    "decorator": decorator,
                })
    return tuple(manifest)


def _request_path(path: str) -> str:
    for placeholder, value in _PATH_VALUES.items():
        path = path.replace(placeholder, value)
    return path


AUXILIARY_OPERATION_MANIFEST = _operation_manifest()
ROUTES = [
    (
        item["method"],
        _request_path(item["path"]),
        _REQUEST_BODIES.get(item["operation_id"]),
        _REQUEST_HEADERS.get(item["operation_id"], {}),
    )
    for item in AUXILIARY_OPERATION_MANIFEST
]
EXPECTED_OPERATION_IDS = {
    "getTranslationSettings",
    "updateTranslationSettings",
    "getTranslationCacheStats",
    "clearTranslationCache",
    "testTranslation",
    "listTranslationLanguages",
    "getStatusAlertSettings",
    "updateStatusAlertSettings",
    "listStatusIncidents",
    "refreshStatusAlerts",
    "muteStatusIncident",
    "unmuteStatusIncident",
    "getSettings",
    "updateSettings",
    "checkForUpdates",
    "ignoreUpdateVersion",
    "unignoreUpdateVersion",
    "listUpdateBackups",
    "getUpdateFailureLog",
    "stageUpdate",
    "activateStagedUpdate",
    "cancelStagedUpdate",
    "getImageSettings",
    "getVideoSettings", "updateVideoSettings", "listMediaSources", "updateMediaSource",
    "updateImageSettings",
    "getImageAccountState",
    "updateImageAccountState",
    "getXaiMediaSettings",
    "updateXaiMediaSettings",
    "addXaiMediaModel",
    "renameXaiMediaModel",
    "removeXaiMediaModel",
}


def _request(client, method, path, *, body, headers):
    return client.request(
        method.upper(),
        "/api/management/v1" + path,
        json=body,
        headers=headers,
    )


def _guard_allowed_names(decorator: ast.Call) -> set[str]:
    dependencies = [
        keyword.value
        for keyword in decorator.keywords
        if keyword.arg == "dependencies"
    ]
    assert len(dependencies) == 1
    assert isinstance(dependencies[0], (ast.List, ast.Tuple))
    guards = []
    for dependency in dependencies[0].elts:
        if not (
            isinstance(dependency, ast.Call)
            and isinstance(dependency.func, ast.Name)
            and dependency.func.id == "Depends"
            and len(dependency.args) == 1
        ):
            continue
        guard = dependency.args[0]
        if (
            isinstance(guard, ast.Call)
            and isinstance(guard.func, ast.Name)
            and guard.func.id == "reject_unknown_query"
        ):
            guards.append(guard)
    assert len(guards) == 1
    assert not guards[0].keywords
    assert all(
        isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        for argument in guards[0].args
    )
    return {argument.value for argument in guards[0].args}


def test_auxiliary_manifest_has_exactly_one_pre_control_query_guard_per_operation(tmp_path):
    operation_ids = [item["operation_id"] for item in AUXILIARY_OPERATION_MANIFEST]
    assert len(operation_ids) == 35  # Five AG image operations explicitly retired.
    assert len(set(operation_ids)) == len(operation_ids)
    assert set(operation_ids) == EXPECTED_OPERATION_IDS

    app, _, _ = build_auxiliary_app(tmp_path)
    document = app.openapi()
    for item in AUXILIARY_OPERATION_MANIFEST:
        openapi_path = item["path"].replace("{modelId:path}", "{modelId}").replace("{sourceId:path}", "{sourceId}")
        operation = document["paths"]["/api/management/v1" + openapi_path][item["method"]]
        declared_query = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "query"
        }
        # Router-level dependencies execute before endpoint dependencies and the
        # endpoint itself, so no control call can precede this guard.
        assert _guard_allowed_names(item["decorator"]) == declared_query, item["operation_id"]


def test_all_manifest_routes_reject_unknown_query_without_side_effects(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        session_headers = bearer(create_session(client))
        for item in AUXILIARY_OPERATION_MANIFEST:
            operation_id = item["operation_id"]
            config_before = copy.deepcopy(fixture.config.value)
            config_updates_before = fixture.config.updates
            control_audit_before = fixture.audit.snapshot()
            runtime_audit_before = runtime.state_store.audit_snapshot()
            operations_before = copy.deepcopy(runtime.operations._items)

            response = _request(
                client,
                item["method"],
                _request_path(item["path"]) + "?undeclared=1",
                body=_REQUEST_BODIES.get(operation_id),
                headers={**session_headers, **_REQUEST_HEADERS.get(operation_id, {})},
            )

            assert response.status_code == 422, (operation_id, response.text)
            error = response.json()["error"]
            assert error["code"] == "VALIDATION_FAILED", operation_id
            assert error["fields"] == [{
                "path": "undeclared",
                "code": "UNKNOWN_QUERY_PARAMETER",
                "message": "Unknown query parameter",
            }]
            assert fixture.config.value == config_before, operation_id
            assert fixture.config.updates == config_updates_before, operation_id
            assert fixture.audit.snapshot() == control_audit_before, operation_id
            assert runtime.state_store.audit_snapshot() == runtime_audit_before, operation_id
            assert runtime.operations._items == operations_before, operation_id


@pytest.mark.parametrize("method,path,body,extra_headers", ROUTES)
def test_every_auxiliary_operation_requires_session_and_capability(
    tmp_path, method, path, body, extra_headers,
):
    app, runtime, _ = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        missing = _request(client, method, path, body=body, headers=extra_headers)
        assert missing.status_code == 401, missing.text
        assert missing.json()["error"]["code"] == "SESSION_REQUIRED"

        restricted = runtime.sessions.issue_for_principal(
            subject_id="restricted",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            roles=(),
            capabilities=(),
        )
        headers = {**extra_headers, **bearer(restricted.credential)}
        denied = _request(client, method, path, body=body, headers=headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "CAPABILITY_DENIED"


@pytest.mark.parametrize(
    "path,body,field",
    [
        ("/translation", {"unknown": True}, "unknown"),
        ("/status-alerts/settings", {"minImpact": "catastrophic"}, "minImpact"),
        ("/updates/settings", {"intervalSeconds": 1}, "intervalSeconds"),
        ("/images/settings", {"cacheRetentionDays": -1}, "cacheRetentionDays"),
        ("/images/accounts/openai%3Auser%40example.com", {"enabled": True, "token": "must-not-appear"}, "token"),
        ("/xai/media-settings", {"jobTtlSeconds": 0}, "jobTtlSeconds"),
    ],
)
def test_typed_patch_validation_has_field_locations(tmp_path, path, body, field):
    app, _, _ = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        token = create_session(client)
        response = client.patch("/api/management/v1" + path, json=body, headers=bearer(token))
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_FAILED"
        assert any(field in item["path"] for item in error["fields"])
        assert "must-not-appear" not in response.text


def test_openapi_has_exact_auxiliary_operations_typed_schemas_and_examples(tmp_path):
    app, _, _ = build_auxiliary_app(tmp_path)
    document = app.openapi()
    operations = {
        operation["operationId"]: operation
        for path, path_item in document["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in path_item.items()
        if method in {"get", "patch", "put", "post", "delete"}
        and operation["operationId"] not in FOUNDATION_OPERATION_IDS
    }
    assert set(operations) == EXPECTED_OPERATION_IDS
    for operation in operations.values():
        assert operation["tags"]
        assert operation["security"] == [{"ManagementSession": []}]
        success = next(
            value for code, value in operation["responses"].items()
            if code.startswith("2")
        )
        if success.get("content"):
            media = success["content"]["application/json"]
            assert "example" in media or "examples" in media
        assert "422" in operation["responses"]
    serialized = repr(document)
    assert "top-secret" not in serialized
    schemas = document["components"]["schemas"]
    assert schemas["ActivateStagedUpdateRequest"]["properties"]["planToken"]["writeOnly"] is True
    assert "writeOnly" not in schemas["StageUpdateOperationData"]["properties"]["activationPlanToken"]
    assert "502" in operations["checkForUpdates"]["responses"]

    def is_date_time(property_schema):
        return property_schema.get("format") == "date-time" or any(
            item.get("format") == "date-time" for item in property_schema.get("anyOf", [])
        )

    for schema_name, fields in {
        "StatusIncidentData": ("createdAt", "updatedAt", "mutedAt"),
        "UpdateCheckData": ("publishedAt",),
        "UpdateBackupData": ("createdAt",),
        "ImageAccountStateData": ("imageCooldownUntil",),
    }.items():
        for field in fields:
            assert is_date_time(schemas[schema_name]["properties"][field]), (schema_name, field)
    backup_example = operations["listUpdateBackups"]["responses"]["200"]["content"]["application/json"]["example"]
    assert backup_example["data"]["items"][0]["createdAt"] == "2026-01-02T03:04:05Z"
    for name in (
        "TranslationSettingsData",
        "StatusAlertSettingsData",
        "UpdateSettingsData",
        "ImageSettingsData",
        "ImageAccountStateData",
        "XaiMediaSettingsData",
    ):
        assert name in document["components"]["schemas"]


def test_owned_telegram_menus_call_auxiliary_controls_without_direct_business_io():
    root = Path("src/telegram/menus")
    for name in (
        "translation_menu.py",
        "status_alert_menu.py",
        "update_menu.py",
        "image_menu.py",
        "xai_imagine_menu.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "management_control.auxiliary" in source
        for direct_call in (
            "config.update(",
            "translation.translate_text_for_test(",
            "status_monitor._process_provider(",
            "update_checker.force_refresh_sync(",
            "updater.stage_update(",
            "images_simple.list_image_accounts(",
            "image_db.get_log(",
        ):
            assert direct_call not in source, (name, direct_call)


def test_auxiliary_routers_have_no_direct_config_runtime_or_updater_imports():
    root = Path("src/management_api/routers")
    files = [
        root / "translation.py",
        root / "status_alerts.py",
        root / "updates.py",
        root / "media_settings.py",
    ]
    forbidden = {
        "src.config",
        "src.translation",
        "src.status_monitor",
        "src.update_checker",
        "src.updater",
        "src.image_db",
        "src.openai.images_simple",
    }
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imports.isdisjoint(forbidden), (path, imports & forbidden)


def test_production_auxiliary_dependency_binds_runtime_audit_without_override(
    tmp_path, monkeypatch,
):
    private_config = {
        "updateChecker": {
            "enabled": True,
            "includePrerelease": False,
            "autoUpdate": False,
            "intervalSeconds": 3600,
            "ignoredVersions": [],
        }
    }
    config_path = tmp_path / "private-config.json"
    config_path.write_text(json.dumps(private_config), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config, "_cache", copy.deepcopy(private_config))
    monkeypatch.setattr(config, "_mtime", config_path.stat().st_mtime)
    monkeypatch.setattr(config, "_reload_callbacks", [])

    app, runtime, _ = build_app(tmp_path)
    app.include_router(updates_router, prefix="/api/management/v1")
    assert get_bound_auxiliary_controls not in app.dependency_overrides
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        response = client.patch(
            "/api/management/v1/updates/settings",
            json={"intervalSeconds": 7200},
            headers={**headers, "X-Request-Id": "production-audit-probe"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["intervalSeconds"] == 7200

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["updateChecker"]["intervalSeconds"] == 7200
    records = runtime.state_store.audit_snapshot()
    assert any(
        row["action"] == "updates.settings.update"
        and row["request_id"] == "production-audit-probe"
        for row in records
    )
    controls = app.state.management_auxiliary_controls
    assert app.state.management_auxiliary_controls_runtime is runtime
    assert all(
        control._audit_sink is runtime.audit_sink
        for control in (
            controls.translation,
            controls.status_alerts,
            controls.updates,
            controls.images,
            controls.xai_media,
        )
    )
