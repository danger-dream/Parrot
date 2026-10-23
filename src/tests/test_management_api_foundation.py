from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.management_api import (
    MANAGEMENT_ERROR_STATUS,
    ManagementOriginMiddleware,
    ManagementRuntime,
    create_management_router,
    install_management_error_handlers,
    management_error_responses,
)
from src.management_api.routers import foundation as foundation_router
from src.management_auth import (
    ApprovalService,
    AuthMethod,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import (
    ManagementContext,
    ManagementErrorCode,
    OperationRegistry,
    OperationStore,
    StoreAuditSink,
)


FAKE_KEY = "pmk_" + "M" * 64
EXPECTED_OPERATIONS = {
    "createManagementSession",
    "getCurrentManagementSession",
    "revokeCurrentManagementSession",
    "createTelegramApproval",
    "getTelegramApproval",
    "getManagementMetadata",
    "getManagementCapabilities",
    "getManagementOperation",
    "cancelManagementOperation",
}
EXPECTED_ERROR_CODES = {
    "createManagementSession": {
        401: {"AUTHENTICATION_FAILED"},
        403: {"ORIGIN_DENIED"},
        422: {"VALIDATION_FAILED"},
        429: {"RATE_LIMITED"},
        503: {"SERVICE_NOT_READY"},
    },
    "getCurrentManagementSession": {
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "revokeCurrentManagementSession": {
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "createTelegramApproval": {
        403: {"ORIGIN_DENIED"},
        422: {"VALIDATION_FAILED"},
        429: {"RATE_LIMITED"},
        503: {"SERVICE_NOT_READY", "DEPENDENCY_UNAVAILABLE"},
    },
    "getTelegramApproval": {
        401: {"AUTHENTICATION_FAILED"},
        403: {"ORIGIN_DENIED"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "getManagementMetadata": {
        400: {"INVALID_REQUEST"},
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED", "CAPABILITY_DENIED"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "getManagementCapabilities": {
        400: {"INVALID_REQUEST"},
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED", "CAPABILITY_DENIED"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "getManagementOperation": {
        400: {"INVALID_REQUEST"},
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED", "CAPABILITY_DENIED"},
        404: {"OPERATION_NOT_FOUND"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
    "cancelManagementOperation": {
        400: {"INVALID_REQUEST", "INVALID_OPERATION_STATE"},
        401: {"SESSION_REQUIRED", "SESSION_EXPIRED"},
        403: {"ORIGIN_DENIED", "CAPABILITY_DENIED"},
        404: {"OPERATION_NOT_FOUND"},
        422: {"VALIDATION_FAILED"},
        503: {"SERVICE_NOT_READY"},
    },
}
SUCCESS_STATUS = {
    "createManagementSession": 201,
    "getCurrentManagementSession": 200,
    "revokeCurrentManagementSession": 204,
    "createTelegramApproval": 201,
    "getTelegramApproval": 200,
    "getManagementMetadata": 200,
    "getManagementCapabilities": 200,
    "getManagementOperation": 200,
    "cancelManagementOperation": 204,
}


class FakeNotifier:
    def __init__(self) -> None:
        self.notifications = []

    def send(self, admin_ids, notification):
        self.notifications.append((admin_ids, notification))
        return True


def build_app(tmp_path, *, allowed_origins=("https://admin.example.test",)):
    clock = time.time
    store = ManagementStateStore(
        str(tmp_path / "management-api.db"),
        clock=clock,
    )
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
    notifier = FakeNotifier()
    approvals = ApprovalService(
        store,
        clock=clock,
        ttl_seconds=180,
        admin_ids_provider=lambda: (42,),
        telegram_configured_provider=lambda: True,
        notifier=notifier,
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
        allowed_origins=frozenset(allowed_origins),
        application_version="0.test",
        documentation_url="https://docs.example.test/management-v1",
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    app.include_router(create_management_router(()))
    install_management_error_handlers(app)
    # Deliberately reproduce the inference API's broad CORS, then wrap it with
    # the path-scoped management policy.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ManagementOriginMiddleware)
    return app, runtime, notifier


def create_session(client: TestClient, *, key: str = FAKE_KEY) -> str:
    response = client.post(
        "/api/management/v1/auth/sessions",
        json={"grantType": "managementKey", "managementKey": key},
        headers={"X-Request-Id": "req-create-session"},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == "req-create-session"
    return response.json()["data"]["credential"]


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def management_operations(document: dict) -> dict[str, dict]:
    return {
        operation["operationId"]: operation
        for path, item in document["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in item.items()
        if method.lower() in {"get", "post", "delete", "patch", "put"}
    }


def example_values(node) -> list[object]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "example":
                found.append(value)
            elif key == "examples":
                if isinstance(value, list):
                    found.extend(value)
                elif isinstance(value, dict):
                    for example in value.values():
                        if isinstance(example, dict) and "value" in example:
                            found.append(example["value"])
                        else:
                            found.append(example)
            else:
                found.extend(example_values(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(example_values(value))
    return found


def test_openapi_has_exact_p0_operation_ids_typed_schemas_and_security(tmp_path):
    app, _, _ = build_app(tmp_path)
    document = app.openapi()
    operations = {}
    for path, item in document["paths"].items():
        if not path.startswith("/api/management/v1"):
            continue
        for method, operation in item.items():
            if method.lower() in {"get", "post", "delete", "patch", "put"}:
                operations[operation["operationId"]] = (method, path, operation)
    manifest = Path("src/tests/fixtures/management_api/v1-operation-ids.txt")
    manifested = {line for line in manifest.read_text().splitlines() if line}
    assert manifested == EXPECTED_OPERATIONS
    assert set(operations) == manifested
    assert len(operations) == 9
    for operation_id, (_, _, operation) in operations.items():
        assert operation["tags"]
        assert "responses" in operation
        if operation_id not in {"createManagementSession", "createTelegramApproval", "getTelegramApproval"}:
            assert operation.get("security") == [{"ManagementSession": []}]

    schemas = document["components"]["schemas"]
    serialized = repr(schemas)
    assert "SessionCredentialData" in schemas
    assert schemas["ManagementKeyGrant"]["properties"]["managementKey"]["writeOnly"] is True
    assert schemas["TelegramApprovalGrant"]["properties"]["exchangeSecret"]["writeOnly"] is True
    assert "writeOnly" not in schemas["SessionCredentialData"]["properties"]["credential"]
    assert "writeOnly" not in schemas["TelegramApprovalCreatedData"]["properties"]["exchangeSecret"]
    assert "additionalProperties': False" in serialized
    assert "pms_" not in serialized and "pmk_" not in serialized and "max_" not in serialized


def test_openapi_examples_enum_and_route_specific_error_contract(tmp_path):
    app, _, _ = build_app(tmp_path)
    document = app.openapi()
    operations = management_operations(document)
    assert set(operations) == EXPECTED_OPERATIONS

    for operation_id, operation in operations.items():
        examples = example_values(operation)
        assert examples, f"{operation_id} has no machine-readable example"
        serialized_examples = json.dumps(examples, ensure_ascii=False)
        assert re.search(
            r"(?i)(pmk_|pms_|max_|sk-[a-z0-9]|bearer\s+[a-z0-9._-]{8,})",
            serialized_examples,
        ) is None

        success_status = SUCCESS_STATUS[operation_id]
        if operation_id in {"createManagementSession", "createTelegramApproval"}:
            media = operation["requestBody"]["content"]["application/json"]
            assert media["examples"]
        if success_status == 204:
            success = operation["responses"]["204"]
            assert success["headers"]["X-Request-Id"]["example"]
        else:
            success = operation["responses"][str(success_status)]
            assert success["content"]["application/json"]["example"]

        expected = EXPECTED_ERROR_CODES[operation_id]
        assert set(operation["responses"]) == {
            str(success_status), *(str(value) for value in expected)
        }
        for http_status, expected_codes in expected.items():
            response = operation["responses"][str(http_status)]
            assert response["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorEnvelope"
            }
            examples_by_code = response["content"]["application/json"]["examples"]
            assert set(examples_by_code) == expected_codes
            for code, example in examples_by_code.items():
                assert example["value"]["error"]["code"] == code
                assert MANAGEMENT_ERROR_STATUS[ManagementErrorCode(code)] == http_status

    schemas = document["components"]["schemas"]
    assert schemas["ErrorDetailSchema"]["properties"]["code"] == {
        "$ref": "#/components/schemas/ManagementErrorCode"
    }
    assert set(schemas["ManagementErrorCode"]["enum"]) == {
        code.value for code in ManagementErrorCode
    }

    assert set(MANAGEMENT_ERROR_STATUS) == set(ManagementErrorCode)
    reusable = management_error_responses(
        ManagementErrorCode.INVALID_REQUEST,
        ManagementErrorCode.DEPENDENCY_UNAVAILABLE,
    )
    assert set(reusable) == {400, 503}


def test_management_key_grant_bearer_session_revoke_and_replay(tmp_path):
    app, _, _ = build_app(tmp_path)
    with TestClient(app) as client:
        credential = create_session(client)
        current = client.get("/api/management/v1/auth/session", headers=bearer(credential))
        assert current.status_code == 200
        data = current.json()["data"]
        assert data["authMethod"] == "managementKey"
        assert data["subjectId"] == "administrator"
        assert "management.read" in data["capabilities"]
        assert current.headers["cache-control"] == "no-store"

        meta = client.get("/api/management/v1/meta", headers=bearer(credential))
        assert meta.status_code == 200
        assert meta.json()["data"]["apiVersion"] == "v1"
        assert meta.json()["data"]["applicationVersion"] == "0.test"

        revoked = client.delete("/api/management/v1/auth/session", headers=bearer(credential))
        assert revoked.status_code == 204 and revoked.content == b""
        assert revoked.headers["cache-control"] == "no-store"
        replay = client.get("/api/management/v1/meta", headers=bearer(credential))
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "SESSION_REQUIRED"
        assert credential not in replay.text


def test_auth_failure_validation_envelope_unknown_fields_and_capability(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        bad_key = client.post(
            "/api/management/v1/auth/sessions",
            json={"grantType": "managementKey", "managementKey": "not-the-key"},
        )
        assert bad_key.status_code == 401
        assert bad_key.json()["error"]["code"] == "AUTHENTICATION_FAILED"
        assert "not-the-key" not in bad_key.text

        unknown = client.post(
            "/api/management/v1/auth/sessions",
            json={
                "grantType": "managementKey",
                "managementKey": FAKE_KEY,
                "unexpected": True,
            },
        )
        assert unknown.status_code == 422
        body = unknown.json()["error"]
        assert body["code"] == "VALIDATION_FAILED"
        assert body["fields"] and "unexpected" in body["fields"][0]["path"]

        restricted = runtime.sessions.issue_for_principal(
            subject_id="restricted",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            roles=(),
            capabilities=(),
        )
        own_session = client.get(
            "/api/management/v1/auth/session", headers=bearer(restricted.credential),
        )
        assert own_session.status_code == 200
        denied = client.get("/api/management/v1/meta", headers=bearer(restricted.credential))
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "CAPABILITY_DENIED"


def test_telegram_approval_create_runs_sync_notifier_off_event_loop(
    tmp_path, monkeypatch,
):
    app, runtime, notifier = build_app(tmp_path)
    calls = []

    async def tracked_run_in_threadpool(function, /, *args, **kwargs):
        loop_thread = threading.get_ident()
        worker_threads = []
        results = []

        def invoke():
            worker_threads.append(threading.get_ident())
            results.append(function(*args, **kwargs))

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join()
        calls.append((function, loop_thread, worker_threads[0]))
        return results[0]

    # Management routes offload with Starlette's threadpool (anyio workers),
    # never the asyncio default executor shared with inference logging.
    monkeypatch.setattr(foundation_router, "run_in_threadpool", tracked_run_in_threadpool)
    with TestClient(app) as client:
        created = client.post(
            "/api/management/v1/auth/telegram-approvals",
            json={"clientName": "thread-boundary", "deviceSummary": "test-device"},
        )
    assert created.status_code == 201
    assert len(calls) == 1
    function, loop_thread, worker_thread = calls[0]
    assert function.__self__ is runtime.approvals
    assert function.__name__ == "create"
    assert worker_thread != loop_thread
    assert notifier.notifications


def test_telegram_grant_polling_binding_consumption_and_no_store(tmp_path):
    app, runtime, notifier = build_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/management/v1/auth/telegram-approvals",
            json={"clientName": "test-browser", "deviceSummary": "fake-device"},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        challenge = created.json()["data"]
        assert challenge["pollAfterSeconds"] >= 1
        assert notifier.notifications
        notification = notifier.notifications[-1][1]
        assert challenge["exchangeSecret"] not in repr(notification)

        wrong = client.get(
            f"/api/management/v1/auth/telegram-approvals/{challenge['approvalId']}",
            headers={"Authorization": "Approval wrong-browser"},
        )
        assert wrong.status_code == 401
        runtime.approvals.decide(
            challenge["approvalId"], telegram_user_id=42, approved=True,
        )
        polled = client.get(
            f"/api/management/v1/auth/telegram-approvals/{challenge['approvalId']}",
            headers={"Authorization": f"Approval {challenge['exchangeSecret']}"},
        )
        assert polled.status_code == 200
        assert polled.json()["data"]["status"] == "approved"
        assert "credential" not in polled.text

        exchanged = client.post(
            "/api/management/v1/auth/sessions",
            json={
                "grantType": "telegramApproval",
                "approvalId": challenge["approvalId"],
                "exchangeSecret": challenge["exchangeSecret"],
            },
        )
        assert exchanged.status_code == 201
        assert exchanged.json()["data"]["session"]["authMethod"] == "telegramApproval"
        replay = client.post(
            "/api/management/v1/auth/sessions",
            json={
                "grantType": "telegramApproval",
                "approvalId": challenge["approvalId"],
                "exchangeSecret": challenge["exchangeSecret"],
            },
        )
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "AUTHENTICATION_FAILED"


def test_operation_auth_session_visibility_and_cancel(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        owner_token = create_session(client)
        other_token = create_session(client)
        owner_principal = runtime.sessions.verify(owner_token).principal
        operation = runtime.operations.create(
            ManagementContext(request_id="op-create", actor=owner_principal),
            kind="test.pending",
            cancellable=True,
        )
        missing_auth = client.get(f"/api/management/v1/operations/{operation.id}")
        assert missing_auth.status_code == 401
        hidden = client.get(
            f"/api/management/v1/operations/{operation.id}",
            headers=bearer(other_token),
        )
        assert hidden.status_code == 404
        assert hidden.json()["error"]["code"] == "OPERATION_NOT_FOUND"
        visible = client.get(
            f"/api/management/v1/operations/{operation.id}",
            headers=bearer(owner_token),
        )
        assert visible.status_code == 200
        assert visible.json()["data"]["status"] == "queued"
        cancelled = client.delete(
            f"/api/management/v1/operations/{operation.id}",
            headers=bearer(owner_token),
        )
        assert cancelled.status_code == 204
        final = client.get(
            f"/api/management/v1/operations/{operation.id}",
            headers=bearer(owner_token),
        )
        assert final.json()["data"]["status"] == "cancelled"


def test_management_origin_policy_overrides_broad_cors_but_cli_bearer_works(tmp_path):
    app, _, _ = build_app(tmp_path)
    with TestClient(app) as client:
        denied = client.post(
            "/api/management/v1/auth/sessions",
            json={"grantType": "managementKey", "managementKey": FAKE_KEY},
            headers={"Origin": "https://evil.example.test"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ORIGIN_DENIED"
        assert "access-control-allow-origin" not in denied.headers

        allowed = client.post(
            "/api/management/v1/auth/sessions",
            json={"grantType": "managementKey", "managementKey": FAKE_KEY},
            headers={"Origin": "https://admin.example.test"},
        )
        assert allowed.status_code == 201
        assert allowed.headers["access-control-allow-origin"] == "https://admin.example.test"
        assert allowed.headers["access-control-allow-origin"] != "*"

        preflight = client.options(
            "/api/management/v1/meta",
            headers={
                "Origin": "https://admin.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "https://admin.example.test"

        for method in ("PATCH", "PUT"):
            mutation_preflight = client.options(
                "/api/management/v1/future-resource",
                headers={
                    "Origin": "https://admin.example.test",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "Authorization, If-Match",
                },
            )
            assert mutation_preflight.status_code == 204
            assert method in mutation_preflight.headers["access-control-allow-methods"]
            assert "If-Match" in mutation_preflight.headers["access-control-allow-headers"]

        for method, headers in (
            ("TRACE", "Authorization"),
            ("PATCH", "Authorization, X-Unknown-Header"),
        ):
            rejected_preflight = client.options(
                "/api/management/v1/future-resource",
                headers={
                    "Origin": "https://admin.example.test",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": headers,
                },
            )
            assert rejected_preflight.status_code == 403
            assert rejected_preflight.json()["error"]["code"] == "ORIGIN_DENIED"
            assert "access-control-allow-origin" not in rejected_preflight.headers

        cli_token = create_session(client)
        cli = client.get("/api/management/v1/meta", headers=bearer(cli_token))
        assert cli.status_code == 200
        assert "access-control-allow-origin" not in cli.headers


def test_router_accepts_explicit_domain_registration_without_common_type_changes(tmp_path):
    extra = APIRouter()

    @extra.get("/domain-probe", operation_id="getDomainProbe", tags=["probe"])
    async def probe():
        return {"ok": True}

    app = FastAPI()
    app.include_router(create_management_router([extra]))
    assert app.openapi()["paths"]["/api/management/v1/domain-probe"]["get"]["operationId"] == "getDomainProbe"
