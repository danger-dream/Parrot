from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.management_api import ManagementRuntime, create_management_router, install_management_error_handlers
from src.management_api.dependencies import get_management_context
from src.management_api.routers.apikey import router as apikey_router
from src.management_auth import (
    ApprovalService,
    AuthMethod,
    Capability,
    ManagementPrincipal,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import ManagementContext, OperationRegistry, OperationStore, StoreAuditSink
from src.tests.test_management_apikey_control import Sequence, make_control


FAKE_MANAGEMENT_KEY = "pmk_" + "K" * 64
OPERATIONS = {
    "listApiKeys",
    "createApiKey",
    "getApiKey",
    "updateApiKey",
    "deleteApiKey",
    "planApiKeyRegeneration",
    "regenerateApiKey",
    "replaceApiKeySecret",
    "reorderApiKeys",
    "resetApiKeyLimiter",
    "getApiKeyStats",
}


class FakeNotifier:
    def send(self, admin_ids, notification):
        return True


def build_app(tmp_path, *, initial_config=None, generated=None):
    store = ManagementStateStore(str(tmp_path / "management-apikey.db"), clock=time.time)
    sessions = SessionService(
        store,
        management_key=FAKE_MANAGEMENT_KEY,
        policy=SessionPolicy(
            idle_timeout_seconds=3600,
            absolute_timeout_seconds=7200,
            touch_interval_seconds=60,
        ),
        clock=time.time,
    )
    approvals = ApprovalService(
        store,
        clock=time.time,
        ttl_seconds=180,
        admin_ids_provider=lambda: (42,),
        telegram_configured_provider=lambda: True,
        notifier=FakeNotifier(),
    )
    audit = StoreAuditSink(store)
    operations = OperationStore(audit_sink=audit)
    runtime = ManagementRuntime(
        sessions=sessions,
        approvals=approvals,
        operations=operations,
        operation_registry=OperationRegistry(operations),
        audit_sink=audit,
        state_store=store,
        allowed_origins=frozenset(),
        application_version="test",
        documentation_url="/docs",
    )
    control, config_store, limiter, _ = make_control(
        initial_config,
        generated=generated or Sequence(["ccp-" + "r" * 48]),
        tokens=Sequence(["plan-public", "plan-secret"]),
        provenance_store=store,
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    app.state.management_apikey_control = control
    app.include_router(create_management_router([apikey_router]))
    install_management_error_handlers(app)
    return app, runtime, control, config_store, limiter


def session_headers(runtime):
    issued = runtime.sessions.create_from_management_key(
        FAKE_MANAGEMENT_KEY,
        source="api-test",
        request_id="session-test",
    )
    return {
        "Authorization": f"Bearer {issued.credential}",
        "X-Request-Id": "apikey-request",
    }


def operation_map(document):
    return {
        operation["operationId"]: (method, path, operation)
        for path, item in document["paths"].items()
        if path.startswith("/api/management/v1/api-keys")
        for method, operation in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }


def all_examples(value):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "example":
                found.append(item)
            elif key == "examples":
                if isinstance(item, dict):
                    found.extend(
                        nested.get("value", nested) if isinstance(nested, dict) else nested
                        for nested in item.values()
                    )
                else:
                    found.append(item)
            else:
                found.extend(all_examples(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(all_examples(item))
    return found


def test_openapi_exact_operation_ids_typed_schemas_examples_and_write_only(tmp_path):
    app, runtime, _, _, _ = build_app(tmp_path)
    try:
        document = app.openapi()
        operations = operation_map(document)
        assert set(operations) == OPERATIONS
        assert len(operations) == 11
        for operation_id, (_, _, operation) in operations.items():
            assert operation["tags"] == ["api-keys"]
            assert operation.get("security") == [{"ManagementSession": []}]
            assert all_examples(operation), operation_id
            assert "responses" in operation

        schemas = document["components"]["schemas"]
        assert schemas["ApiKeyCreateRequest"]["properties"]["customSecret"]["writeOnly"] is True
        assert schemas["ApiKeyReplaceSecretRequest"]["properties"]["customSecret"]["writeOnly"] is True
        assert schemas["ApiKeyRegenerateRequest"]["properties"]["planToken"]["writeOnly"] is True
        assert not schemas["ApiKeySecretData"]["properties"]["secret"].get("writeOnly", False)
        assert not schemas["ApiKeyReplacementPlanData"]["properties"]["planToken"].get("writeOnly", False)
        serialized = json.dumps(document, ensure_ascii=False)
        assert "alpha-secret" not in serialized
        assert "client-secret+/=" not in serialized
        assert re.search(r'"key"\s*:', serialized) is None
        assert "additionalProperties\": false" in serialized
    finally:
        runtime.close()


_REQUESTS = [
    ("get", "/api/management/v1/api-keys", None, {}),
    ("post", "/api/management/v1/api-keys", {"mode": "generated", "name": "new"}, {}),
    ("get", "/api/management/v1/api-keys/alpha", None, {}),
    ("patch", "/api/management/v1/api-keys/alpha", {"enabled": False}, {}),
    ("delete", "/api/management/v1/api-keys/alpha", None, {"If-Match": '"ak-fake"'}),
    ("post", "/api/management/v1/api-keys/alpha/actions/generate-replacement-plan", None, {}),
    ("post", "/api/management/v1/api-keys/alpha/actions/generate-replacement", {"planId": "akplan_fake", "planToken": "fake-token"}, {}),
    ("put", "/api/management/v1/api-keys/alpha/secret", {"customSecret": "replacement-secret"}, {"If-Match": '"ak-fake"'}),
    ("put", "/api/management/v1/api-keys/order", {"keyIds": ["alpha", "busy"]}, {"If-Match": '"aks-fake"'}),
    ("post", "/api/management/v1/api-keys/alpha/actions/reset-limiter", None, {}),
    ("get", "/api/management/v1/api-keys/alpha/stats", None, {}),
]


@pytest.mark.parametrize("method,path,body,extra", _REQUESTS)
def test_every_operation_requires_session(tmp_path, method, path, body, extra):
    app, runtime, _, _, _ = build_app(tmp_path)
    try:
        response = TestClient(app).request(method, path, json=body, headers=extra)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "SESSION_REQUIRED"
    finally:
        runtime.close()


@pytest.mark.parametrize("method,path,body,extra", _REQUESTS)
def test_every_operation_requires_its_capability_and_never_calls_control(
    tmp_path, method, path, body, extra,
):
    app, runtime, control, _, _ = build_app(tmp_path)
    denied = ManagementContext(
        request_id="denied-request",
        actor=ManagementPrincipal.with_capabilities(
            subject_id="denied",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            capabilities=(),
            issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    app.dependency_overrides[get_management_context] = lambda: denied
    called = []
    names = [
        "list_api_keys", "create_api_key", "get_api_key", "update_api_key",
        "delete_api_key", "plan_regeneration", "regenerate_api_key",
        "replace_api_key_secret", "reorder_api_keys", "reset_api_key_limiter",
        "get_api_key_stats",
    ]
    originals = {name: getattr(control, name) for name in names}
    for name, original in originals.items():
        setattr(control, name, lambda *args, _name=name, _original=original, **kwargs: (called.append(_name), _original(*args, **kwargs))[1])
    try:
        response = TestClient(app).request(method, path, json=body, headers=extra)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "CAPABILITY_DENIED"
        assert called == []
    finally:
        runtime.close()


def test_all_operations_happy_path_call_control_once_with_authenticated_actor(tmp_path):
    app, runtime, control, store, limiter = build_app(tmp_path)
    headers = session_headers(runtime)
    calls = []
    names = [
        "list_api_keys", "create_api_key", "get_api_key", "update_api_key",
        "delete_api_key", "plan_regeneration", "regenerate_api_key",
        "replace_api_key_secret", "reorder_api_keys", "reset_api_key_limiter",
        "get_api_key_stats",
    ]
    for name in names:
        original = getattr(control, name)
        def wrapped(*args, _name=name, _original=original, **kwargs):
            calls.append((_name, args, kwargs))
            return _original(*args, **kwargs)
        setattr(control, name, wrapped)

    client = TestClient(app)
    try:
        listed = client.get(
            "/api/management/v1/api-keys?page=1&pageSize=1&enabled=all&sort=monthCallsDesc",
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["meta"]["requestId"] == "apikey-request"
        assert listed.json()["meta"]["total"] == 2
        assert len(listed.json()["data"]["items"]) == 1
        # Round 3 (user requirement): list/detail return the saved secret to
        # secrets-capable sessions, with no-store; mutation/error paths never do.
        assert listed.json()["data"]["items"][0]["secret"] == "ccp-busy-secret"
        assert listed.headers["cache-control"] == "no-store"

        detail = client.get("/api/management/v1/api-keys/alpha", headers=headers)
        assert detail.status_code == 200, detail.text
        alpha_revision = detail.json()["data"]["revision"]
        assert detail.json()["data"]["modelStats"][0]["model"] == "model-a"
        assert detail.json()["data"]["secret"] == "alpha-secret"
        assert detail.headers["cache-control"] == "no-store"

        stats = client.get("/api/management/v1/api-keys/alpha/stats", headers=headers)
        assert stats.status_code == 200, stats.text
        assert stats.json()["data"]["overall"]["total"] == 2

        created = client.post(
            "/api/management/v1/api-keys",
            headers=headers,
            json={"mode": "custom", "name": "client.new", "customSecret": "ccp-client-custom-secret"},
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store"
        assert created.json()["data"]["secret"] == "ccp-client-custom-secret"
        assert created.json()["data"]["apiKey"]["source"] == "custom"
        assert store.value["apiKeys"]["client.new"]["source"] == "custom"
        custom_items = client.get(
            "/api/management/v1/api-keys?source=custom&pageSize=200",
            headers=headers,
        ).json()["data"]["items"]
        assert "client.new" in [item["keyId"] for item in custom_items]
        client_revision = created.json()["data"]["apiKey"]["revision"]

        updated = client.patch(
            "/api/management/v1/api-keys/client.new",
            headers={**headers, "If-Match": client_revision},
            json={
                "enabled": False,
                "allowImages": True,
                "allowedModels": ["model-a"],
                "limitOverride": {"maxConcurrent": 2, "maxQueue": 4},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["enabled"] is False
        assert updated.json()["data"]["limitOverride"]["maxConcurrent"] == 2

        plan = client.post(
            "/api/management/v1/api-keys/client.new/actions/generate-replacement-plan",
            headers=headers,
        )
        assert plan.status_code == 200, plan.text
        assert plan.headers["cache-control"] == "no-store"
        plan_data = plan.json()["data"]
        assert "ccp-client-custom-secret" not in plan.text

        regenerated = client.post(
            "/api/management/v1/api-keys/client.new/actions/generate-replacement",
            headers=headers,
            json={"planId": plan_data["planId"], "planToken": plan_data["planToken"]},
        )
        assert regenerated.status_code == 200, regenerated.text
        assert regenerated.headers["cache-control"] == "no-store"
        assert regenerated.json()["data"]["secret"] == "ccp-" + "r" * 48
        assert regenerated.json()["data"]["apiKey"]["source"] == "generated"
        regenerated_revision = regenerated.json()["data"]["apiKey"]["revision"]

        replay = client.post(
            "/api/management/v1/api-keys/client.new/actions/generate-replacement",
            headers=headers,
            json={"planId": plan_data["planId"], "planToken": plan_data["planToken"]},
        )
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "INVALID_OPERATION_STATE"
        assert "ccp-" + "r" * 48 not in replay.text

        replaced = client.put(
            "/api/management/v1/api-keys/alpha/secret",
            headers={**headers, "If-Match": alpha_revision},
            json={"customSecret": "alpha-replacement"},
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.headers["cache-control"] == "no-store"
        assert replaced.json()["data"]["secret"] == "alpha-replacement"
        assert replaced.json()["data"]["apiKey"]["source"] == "custom"

        reset = client.post(
            "/api/management/v1/api-keys/busy/actions/reset-limiter",
            headers=headers,
        )
        assert reset.status_code == 200 and reset.json()["data"]["inFlight"] == 1

        current_list = client.get(
            "/api/management/v1/api-keys?pageSize=200",
            headers=headers,
        ).json()
        key_ids = [item["keyId"] for item in current_list["data"]["items"]]
        ordered = list(reversed(key_ids))
        reorder = client.put(
            "/api/management/v1/api-keys/order",
            headers={**headers, "If-Match": current_list["meta"]["revision"]},
            json={"keyIds": ordered},
        )
        assert reorder.status_code == 200, reorder.text
        assert reorder.json()["data"]["keyIds"] == ordered

        current_client = client.get(
            "/api/management/v1/api-keys/client.new", headers=headers,
        ).json()["data"]
        deleted = client.delete(
            "/api/management/v1/api-keys/client.new",
            headers={**headers, "If-Match": current_client["revision"]},
        )
        assert deleted.status_code == 204 and deleted.content == b""
        assert "client.new" not in store.value["apiKeys"]
        assert limiter.forgot == ["client.new", "alpha", "busy", "client.new"]

        expected_once = {
            "list_api_keys": 3,
            "get_api_key": 2,
            "get_api_key_stats": 1,
            "create_api_key": 1,
            "update_api_key": 1,
            "plan_regeneration": 1,
            "regenerate_api_key": 2,
            "replace_api_key_secret": 1,
            "reset_api_key_limiter": 1,
            "reorder_api_keys": 1,
            "delete_api_key": 1,
        }
        assert {name: sum(call[0] == name for call in calls) for name in expected_once} == expected_once
        for _, args, _ in calls:
            assert isinstance(args[0], ManagementContext)
            assert args[0].actor.subject_id == "administrator"
            assert args[0].request_id == "apikey-request"
    finally:
        runtime.close()


def test_validation_missing_conflict_revision_confirmation_and_list_boundaries(tmp_path):
    app, runtime, _, _, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    client = TestClient(app)
    try:
        invalid_enum = client.get(
            "/api/management/v1/api-keys?sort=unknown",
            headers=headers,
        )
        assert invalid_enum.status_code == 422
        assert invalid_enum.json()["error"]["fields"][0]["path"] == "sort"

        invalid_size = client.get(
            "/api/management/v1/api-keys?pageSize=201",
            headers=headers,
        )
        assert invalid_size.status_code == 422
        assert invalid_size.json()["error"]["fields"][0]["path"] == "pageSize"

        unknown_field = client.post(
            "/api/management/v1/api-keys",
            headers=headers,
            json={"mode": "generated", "name": "new", "unexpected": True},
        )
        assert unknown_field.status_code == 422
        assert unknown_field.json()["error"]["fields"][0]["path"] == "unexpected"

        invalid_secret = client.put(
            "/api/management/v1/api-keys/alpha/secret",
            headers={**headers, "If-Match": '"anything"'},
            json={"customSecret": "short"},
        )
        assert invalid_secret.status_code == 422
        assert invalid_secret.json()["error"]["fields"][0]["path"] == "customSecret"

        missing = client.get("/api/management/v1/api-keys/missing", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        duplicate = client.post(
            "/api/management/v1/api-keys",
            headers=headers,
            json={"mode": "custom", "name": "alpha", "customSecret": "another-secret"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "RESOURCE_CONFLICT"

        no_confirmation = client.delete(
            "/api/management/v1/api-keys/alpha", headers=headers,
        )
        assert no_confirmation.status_code == 400
        assert no_confirmation.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

        stale = client.patch(
            "/api/management/v1/api-keys/alpha",
            headers={**headers, "If-Match": '"ak-stale"'},
            json={"enabled": False},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

        listed = client.get(
            "/api/management/v1/api-keys?page=2&pageSize=1&enabled=disabled&sort=nameAsc",
            headers=headers,
        )
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] == 1
        assert listed.json()["data"]["items"] == []
        assert listed.json()["meta"]["hasNext"] is False
    finally:
        runtime.close()


def test_body_operations_reject_unknown_fields_with_structured_422(tmp_path):
    app, runtime, _, _, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    client = TestClient(app)
    cases = [
        ("post", "/api/management/v1/api-keys", {"mode": "generated", "name": "new", "unknown": 1}),
        ("patch", "/api/management/v1/api-keys/alpha", {"enabled": True, "unknown": 1}),
        ("post", "/api/management/v1/api-keys/alpha/actions/generate-replacement", {"planId": "akplan_fake", "planToken": "fake-token", "unknown": 1}),
        ("put", "/api/management/v1/api-keys/alpha/secret", {"customSecret": "replacement-secret", "unknown": 1}),
        ("put", "/api/management/v1/api-keys/order", {"keyIds": ["alpha", "busy"], "unknown": 1}),
    ]
    try:
        for method, path, body in cases:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == 422, (path, response.text)
            error = response.json()["error"]
            assert error["code"] == "VALIDATION_FAILED"
            assert any(field["path"] == "unknown" for field in error["fields"])
    finally:
        runtime.close()


def test_resource_operations_return_404_for_missing_valid_id(tmp_path):
    app, runtime, _, _, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    client = TestClient(app)
    cases = [
        ("get", "/api/management/v1/api-keys/missing", None, {}),
        ("patch", "/api/management/v1/api-keys/missing", {"enabled": False}, {}),
        ("delete", "/api/management/v1/api-keys/missing", None, {"If-Match": '"ak-any"'}),
        ("post", "/api/management/v1/api-keys/missing/actions/generate-replacement-plan", None, {}),
        ("post", "/api/management/v1/api-keys/missing/actions/generate-replacement", {"planId": "akplan_fake", "planToken": "fake-token"}, {}),
        ("put", "/api/management/v1/api-keys/missing/secret", {"customSecret": "replacement-secret"}, {"If-Match": '"ak-any"'}),
        ("post", "/api/management/v1/api-keys/missing/actions/reset-limiter", None, {}),
        ("get", "/api/management/v1/api-keys/missing/stats", None, {}),
    ]
    try:
        for method, path, body, extra in cases:
            response = client.request(
                method, path, headers={**headers, **extra}, json=body,
            )
            assert response.status_code == 404, (path, response.text)
            assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    finally:
        runtime.close()


def test_all_key_id_routes_reject_invalid_path_with_structured_422(tmp_path):
    app, runtime, _, _, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    client = TestClient(app)
    root = "/api/management/v1/api-keys/bad%20key"
    cases = [
        ("get", root, None),
        ("patch", root, {"enabled": False}),
        ("delete", root, None),
        ("post", root + "/actions/generate-replacement-plan", None),
        ("post", root + "/actions/generate-replacement", {"planId": "akplan_fake", "planToken": "fake-token"}),
        ("put", root + "/secret", {"customSecret": "replacement-secret"}),
        ("post", root + "/actions/reset-limiter", None),
        ("get", root + "/stats", None),
    ]
    try:
        for method, path, body in cases:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == 422, (path, response.text)
            error = response.json()["error"]
            assert error["code"] == "VALIDATION_FAILED"
            assert any(field["path"] == "keyId" for field in error["fields"])
    finally:
        runtime.close()


def test_high_risk_mutations_reject_stale_revision_and_expired_plan(tmp_path):
    app, runtime, control, store, _ = build_app(tmp_path)
    headers = session_headers(runtime)
    client = TestClient(app)
    try:
        for method, path, body, extra in [
            ("delete", "/api/management/v1/api-keys/alpha", None, {"If-Match": '"ak-stale"'}),
            ("put", "/api/management/v1/api-keys/alpha/secret", {"customSecret": "replacement-secret"}, {"If-Match": '"ak-stale"'}),
            ("put", "/api/management/v1/api-keys/order", {"keyIds": ["alpha", "busy"]}, {"If-Match": '"aks-stale"'}),
        ]:
            response = client.request(method, path, json=body, headers={**headers, **extra})
            assert response.status_code == 409, (path, response.text)
            assert response.json()["error"]["code"] == "REVISION_CONFLICT"

        plan = client.post(
            "/api/management/v1/api-keys/alpha/actions/generate-replacement-plan",
            headers=headers,
        ).json()["data"]
        client.patch(
            "/api/management/v1/api-keys/alpha",
            json={"enabled": False},
            headers=headers,
        )
        stale_plan = client.post(
            "/api/management/v1/api-keys/alpha/actions/generate-replacement",
            json={"planId": plan["planId"], "planToken": plan["planToken"]},
            headers=headers,
        )
        assert stale_plan.status_code == 409
        assert stale_plan.json()["error"]["code"] == "REVISION_CONFLICT"
        assert store.value["apiKeys"]["alpha"]["key"] == "alpha-secret"

        control._token_factory = Sequence(["plan-expired", "token-expired"])
        expired_plan = client.post(
            "/api/management/v1/api-keys/alpha/actions/generate-replacement-plan",
            headers=headers,
        ).json()["data"]
        control._clock = lambda: 1_767_225_901.0
        expired = client.post(
            "/api/management/v1/api-keys/alpha/actions/generate-replacement",
            json={
                "planId": expired_plan["planId"],
                "planToken": expired_plan["planToken"],
            },
            headers=headers,
        )
        assert expired.status_code == 400
        assert expired.json()["error"]["code"] == "INVALID_OPERATION_STATE"
        assert "alpha-secret" not in expired.text
    finally:
        runtime.close()
