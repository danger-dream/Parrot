from __future__ import annotations

import ast
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.management_api.routers.oauth import router as oauth_router
from src.management_api.routers.oauth_support import get_oauth_control_dependency
from src.management_auth import AuthMethod
from src.management_control import BoundedAuditSink, ManagementError, ManagementErrorCode
from src.management_control.oauth import (
    CompleteOAuthLoginCommand,
    CreateOAuthAccountCommand,
    JsonCredential,
    ManualCredential,
    OAuthProvider,
    PageSpec,
    RefreshTokenCredential,
)
from src.telegram.menus import oauth_menu
from src.tests.management_oauth_fakes import build_control
from src.tests.test_management_api_foundation import bearer, build_app, create_session


ACCOUNT_ID = "openai:admin@example.test:workspace-1"
INVALID_ID = "claude:invalid@example.test"


def oauth_app(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    control, backend = build_control()
    app.include_router(oauth_router, prefix="/api/management/v1")
    app.dependency_overrides[get_oauth_control_dependency] = lambda: control
    app.openapi_schema = None
    return app, runtime, control, backend


def auth_client(tmp_path):
    app, runtime, control, backend = oauth_app(tmp_path)
    client = TestClient(app)
    client.__enter__()
    token = create_session(client)
    return client, bearer(token), runtime, control, backend


ROUTE_REQUESTS = [
    ("GET", "/oauth/accounts", None, {}),
    ("POST", "/oauth/accounts", {"credential": {"kind": "manual", "provider": "claude", "email": "new@example.test", "accessToken": "access", "refreshToken": "refresh"}}, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}", None, {}),
    ("PATCH", f"/oauth/accounts/{ACCOUNT_ID}", {"enabled": True}, {}),
    ("DELETE", f"/oauth/accounts/{ACCOUNT_ID}", None, {"If-Match": "revision"}),
    ("PUT", "/oauth/account-order", {"accountIds": [ACCOUNT_ID, INVALID_ID]}, {"If-Match": "revision"}),
    ("POST", "/oauth/login-flows", {"provider": "openai"}, {}),
    ("POST", "/oauth/login-flows/wbflow_invalid/poll", {"flowSecret": "invalid-secret-value"}, {}),
    ("POST", "/oauth/login-flows/wbflow_invalid/cancel", {"flowSecret": "invalid-secret-value"}, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy/actions/refresh-status", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy/action-plans", {"action": "checkin"}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy/actions/execute", {"planToken": "wbaction_invalid.invalid"}, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy/action-records", None, {}),
    ("PATCH", f"/oauth/accounts/{ACCOUNT_ID}/workbuddy/settings", {"autoCheckin": False}, {}),
    ("GET", "/oauth/workbuddy/settings", None, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}/zhipu", None, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}/zhipu/reset-status", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/zhipu/project-flows", None, {}),
    ("POST", "/oauth/login-flows/zhflow_invalid/zhipu/project", {"flowSecret": "invalid-secret-value", "organizationId": "", "projectId": ""}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/zhipu/action-plans", {"action": "opportunity", "resetType": "TOKENS_LIMIT"}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/zhipu/actions/execute", {"planToken": "zhaction_invalid.invalid"}, {}),
    ("POST", "/oauth/login-flows/oflow_invalid/complete", {"flowSecret": "invalid-secret-value", "code": "code", "state": "state"}, {}),
    ("POST", "/oauth/imports/preview", {"format": "openai", "payload": "[]"}, {}),
    ("POST", "/oauth/imports/oimport_invalid/commit", {"importSecret": "invalid-secret-value", "decisions": []}, {}),
    ("GET", "/oauth/invalid-accounts", None, {}),
    ("POST", "/oauth/invalid-accounts/delete-plan", {"all": True}, {}),
    ("POST", "/oauth/invalid-accounts/delete", {"planToken": "odelete_invalid.invalid"}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/refresh-token", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/refresh-usage", None, {}),
    ("POST", "/oauth/actions/refresh-usage", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota-plan", {}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota", {"planToken": "oquota_invalid.invalid"}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/clear-errors", None, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/clear-affinity", None, {}),
    ("POST", "/oauth/actions/clear-errors", None, {}),
    ("GET", f"/oauth/accounts/{ACCOUNT_ID}/models", None, {}),
    ("PATCH", f"/oauth/accounts/{ACCOUNT_ID}/models", {"modelIds": ["gpt-alpha"], "disabled": True}, {}),
    ("PATCH", f"/oauth/accounts/{ACCOUNT_ID}/models/settings", {"modelId": "gpt-alpha", "maxContextDefault": True}, {}),
    ("POST", f"/oauth/accounts/{ACCOUNT_ID}/models/actions/sync", None, {}),
    ("GET", "/oauth/settings", None, {}),
    ("PATCH", "/oauth/settings", {"cchMode": "dynamic"}, {}),
    ("GET", "/preferences/telegram/oauth", None, {}),
    ("PATCH", "/preferences/telegram/oauth", {"usageDisplayMode": "remaining"}, {}),
]


def request(client, method, path, body, headers):
    return client.request(
        method,
        "/api/management/v1" + path,
        json=body,
        headers=headers,
    )


def test_oauth_openapi_matches_owned_manifest_and_declares_security_and_secrets(tmp_path):
    app, _, _, _ = oauth_app(tmp_path)
    document = app.openapi()
    operations = {
        value["operationId"]: value
        for path, item in document["paths"].items()
        if path.startswith("/api/management/v1/oauth") or path == "/api/management/v1/preferences/telegram/oauth"
        for method, value in item.items()
        if method in {"get", "post", "patch", "put", "delete"}
    }
    manifested = {
        line for line in Path("src/tests/fixtures/management_api/operations/oauth.txt").read_text().splitlines()
        if line
    }
    assert set(operations) == manifested
    assert len(operations) == len(ROUTE_REQUESTS) == 44
    assert all(value.get("tags") == ["management-oauth"] for value in operations.values())
    assert all(value.get("security") == [{"ManagementSession": []}] for value in operations.values())
    no_content = {
        "deleteOAuthAccount", "clearOAuthAccountErrors", "clearOAuthAccountAffinity", "cancelOAuthLoginFlow",
    }
    for operation_id, operation in operations.items():
        successes = [
            response for status_code, response in operation["responses"].items()
            if status_code.startswith("2")
        ]
        assert len(successes) == 1
        if operation_id in no_content:
            assert "content" not in successes[0]
            assert successes[0]["headers"]["X-Request-Id"]["example"]
        else:
            assert successes[0]["content"]["application/json"]["example"]

    schemas = document["components"]["schemas"]
    write_only = {
        "ManualOAuthCredential": ("accessToken", "refreshToken"),
        "JsonOAuthCredential": ("payload",),
        "RefreshTokenOAuthCredential": ("refreshToken",),
        "CreateOAuthAccountRequest": ("replacePlanToken",),
        "CompleteOAuthLoginFlowRequest": ("flowSecret", "code", "state", "callbackUrl", "replacePlanToken"),
        "PreviewOAuthImportRequest": ("payload",),
        "CommitOAuthImportRequest": ("importSecret",),
        "CommitPlanRequest": ("planToken",),
    }
    for schema, fields in write_only.items():
        for field in fields:
            assert schemas[schema]["properties"][field]["writeOnly"] is True
    one_time_responses = {
        "OAuthReplaceConflictData": ("replacePlanToken",),
        "OAuthLoginFlowData": ("flowSecret",),
        "OAuthImportPreviewData": ("importSecret",),
        "OAuthDeletionPlanData": ("planToken",),
        "OAuthQuotaResetPlanData": ("planToken",),
    }
    for schema, fields in one_time_responses.items():
        for field in fields:
            assert "writeOnly" not in schemas[schema]["properties"][field]
    assert "writeOnly" not in schemas["OAuthLoginFlowData"]["properties"]["flowId"]
    assert "writeOnly" not in schemas["OAuthImportPreviewData"]["properties"]["importId"]
    import_encoding = schemas["PreviewOAuthImportRequest"]["properties"]["payloadEncoding"]
    assert import_encoding["enum"] == ["json", "base64"]
    assert import_encoding["default"] == "json"
    assert schemas["PreviewOAuthImportRequest"]["properties"]["payload"]["maxLength"] == 2_000_000
    assert all(schema.get("additionalProperties") is False for schema in schemas.values() if schema.get("type") == "object")
    serialized = json.dumps({key: operations[key] for key in operations}, ensure_ascii=False)
    assert "access-secret-in-storage" not in serialized
    assert "refresh-secret-in-storage" not in serialized
    assert "ReplaceOAuthDefaultModelsRequest" not in schemas


def test_every_oauth_route_rejects_missing_session_and_missing_capability(tmp_path):
    app, runtime, _, _ = oauth_app(tmp_path)
    restricted = runtime.sessions.issue_for_principal(
        subject_id="oauth-restricted",
        auth_method=AuthMethod.MANAGEMENT_KEY,
        roles=(),
        capabilities=(),
    )
    with TestClient(app) as client:
        for method, path, body, extra_headers in ROUTE_REQUESTS:
            response = request(client, method, path, body, extra_headers)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["error"]["code"] == "SESSION_REQUIRED"

            response = request(
                client, method, path, body,
                {**extra_headers, **bearer(restricted.credential)},
            )
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["error"]["code"] == "CAPABILITY_DENIED"


def test_account_read_update_reorder_and_delete_contract(tmp_path):
    client, headers, _, control, backend = auth_client(tmp_path)
    try:
        control.list_accounts = Mock(wraps=control.list_accounts)
        listed = request(client, "GET", "/oauth/accounts?page=1&pageSize=1", None, headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["meta"] == {
            "requestId": listed.headers["x-request-id"],
            "page": 1,
            "pageSize": 1,
            "total": 2,
            "hasNext": True,
        }
        list_data = listed.json()["data"]
        assert len(list_data["items"]) == 1
        assert list_data["revision"]
        assert "access_token" not in listed.text and "refresh_token" not in listed.text
        called_context = control.list_accounts.call_args.args[0]
        assert called_context.actor.subject_id == "administrator"
        assert control.list_accounts.call_count == 1
        bad_filter = request(
            client, "GET", "/oauth/accounts?filter=unknown", None, headers,
        )
        assert bad_filter.status_code == 422
        assert bad_filter.json()["error"]["fields"]
        missing = request(client, "GET", "/oauth/accounts/missing", None, headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        detail = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers)
        assert detail.status_code == 200, detail.text
        revision = detail.json()["data"]["account"]["revision"]
        assert detail.json()["data"]["credentialConfigured"] is True
        assert detail.json()["data"]["runtimeErrors"][0]["cooldownPermanent"] is True
        assert detail.json()["data"]["runtimeErrors"][0]["cooldownUntil"] is None
        assert "access-secret-in-storage" not in detail.text
        assert "refresh-secret-in-storage" not in detail.text

        changed = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}",
            {"displayName": "Renamed", "enabled": False, "maxConcurrent": 7},
            {**headers, "If-Match": revision},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["account"]["displayName"] == "Renamed"
        assert backend.get_account(ACCOUNT_ID)["maxConcurrent"] == 7

        stale = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}", {"enabled": True},
            {**headers, "If-Match": revision},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

        fresh_list = request(client, "GET", "/oauth/accounts", None, headers).json()["data"]
        reordered = request(
            client, "PUT", "/oauth/account-order",
            {"accountIds": [INVALID_ID, ACCOUNT_ID]},
            {**headers, "If-Match": fresh_list["revision"]},
        )
        assert reordered.status_code == 200, reordered.text
        assert [backend.account_id(item) for item in backend.accounts] == [INVALID_ID, ACCOUNT_ID]

        bad_set = request(
            client, "PUT", "/oauth/account-order", {"accountIds": [ACCOUNT_ID]},
            {**headers, "If-Match": reordered.json()["data"]["revision"]},
        )
        assert bad_set.status_code == 409
        assert bad_set.json()["error"]["code"] == "RESOURCE_CONFLICT"

        current_revision = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers).json()["data"]["account"]["revision"]
        deleted = request(
            client, "DELETE", f"/oauth/accounts/{ACCOUNT_ID}", None,
            {**headers, "If-Match": current_revision},
        )
        assert deleted.status_code == 204
        assert backend.get_account(ACCOUNT_ID) is None
    finally:
        client.__exit__(None, None, None)


def test_account_creation_identity_conflict_plan_and_secret_non_echo(tmp_path):
    client, headers, _, control, backend = auth_client(tmp_path)
    try:
        credential = {
            "kind": "manual",
            "provider": "claude",
            "email": "created@example.test",
            "accessToken": "new-access-secret",
            "refreshToken": "new-refresh-secret",
        }
        created = request(client, "POST", "/oauth/accounts", {"credential": credential}, headers)
        assert created.status_code == 201, created.text
        account_id = created.json()["data"]["accountId"]
        assert account_id == "claude:created@example.test"
        assert "new-access-secret" not in created.text
        assert "new-refresh-secret" not in created.text

        replacement_credential = {
            **credential, "accessToken": "replacement-access-secret",
        }
        conflict = request(
            client, "POST", "/oauth/accounts",
            {"credential": replacement_credential}, headers,
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "IDENTITY_CONFLICT"
        token = conflict.json()["conflict"]["replacePlanToken"]
        assert "replacePlanToken" not in repr(conflict.json()["error"])
        replaced = request(
            client, "POST", "/oauth/accounts",
            {"credential": replacement_credential, "replacePlanToken": token},
            headers,
        )
        assert replaced.status_code == 201, replaced.text
        assert replaced.json()["data"]["status"] == "replaced"
        assert backend.get_account(account_id)["access_token"] == "replacement-access-secret"
        replay = request(
            client, "POST", "/oauth/accounts",
            {"credential": replacement_credential, "replacePlanToken": token},
            headers,
        )
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "INVALID_OPERATION_STATE"
    finally:
        client.__exit__(None, None, None)


def test_login_import_invalid_delete_and_quota_plans_are_one_shot(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        started = request(client, "POST", "/oauth/login-flows", {"provider": "openai"}, headers)
        assert started.status_code == 201, started.text
        flow = started.json()["data"]
        state = parse_qs(urlparse(flow["authUrl"]).query)["state"][0]
        missing_state = request(
            client, "POST", f"/oauth/login-flows/{flow['flowId']}/complete",
            {"flowSecret": flow["flowSecret"], "code": "flow-code"}, headers,
        )
        assert missing_state.status_code == 409
        assert missing_state.json()["error"]["code"] == "STATE_CONFLICT"
        completed = request(
            client, "POST", f"/oauth/login-flows/{flow['flowId']}/complete",
            {"flowSecret": flow["flowSecret"], "code": "flow-code", "state": state}, headers,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["data"]["accountId"] == "openai:flow@example.test:flow-workspace"
        replay = request(
            client, "POST", f"/oauth/login-flows/{flow['flowId']}/complete",
            {"flowSecret": flow["flowSecret"], "code": "flow-code", "state": state}, headers,
        )
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "INVALID_OPERATION_STATE"

        candidate = {
            "provider": "openai", "type": "openai",
            "email": "import@example.test",
            "refresh_token": "import-refresh-secret-1234567890",
        }
        preview = request(
            client, "POST", "/oauth/imports/preview",
            {"format": "openai", "payload": json.dumps([candidate])}, headers,
        )
        assert preview.status_code == 200, preview.text
        preview_data = preview.json()["data"]
        committed = request(
            client, "POST", f"/oauth/imports/{preview_data['importId']}/commit",
            {"importSecret": preview_data["importSecret"], "decisions": [{"candidateId": preview_data["candidates"][0]["candidateId"], "action": "overwrite"}]},
            headers,
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["data"]["added"] == [
            "openai:import@example.test:import-import"
        ]
        replay_import = request(
            client, "POST", f"/oauth/imports/{preview_data['importId']}/commit",
            {"importSecret": preview_data["importSecret"], "decisions": [{"candidateId": "candidate-1", "action": "overwrite"}]}, headers,
        )
        assert replay_import.status_code == 400
        assert replay_import.json()["error"]["code"] == "INVALID_OPERATION_STATE"

        stale_preview = request(
            client, "POST", "/oauth/imports/preview",
            {"format": "openai", "payload": json.dumps([{
                **candidate, "email": "stale-import@example.test",
            }])}, headers,
        ).json()["data"]
        backend.get_account(ACCOUNT_ID)["label"] = "changed-after-preview"
        stale_commit = request(
            client, "POST", f"/oauth/imports/{stale_preview['importId']}/commit",
            {"importSecret": stale_preview["importSecret"], "decisions": [{"candidateId": "candidate-1", "action": "overwrite"}]}, headers,
        )
        assert stale_commit.status_code == 409
        assert stale_commit.json()["error"]["code"] == "REVISION_CONFLICT"

        invalid_list = request(client, "GET", "/oauth/invalid-accounts", None, headers)
        assert invalid_list.status_code == 200
        assert [item["accountId"] for item in invalid_list.json()["data"]["items"]] == [INVALID_ID]
        invalid_plan = request(
            client, "POST", "/oauth/invalid-accounts/delete-plan", {"all": True}, headers,
        )
        assert invalid_plan.status_code == 200, invalid_plan.text
        plan_token = invalid_plan.json()["data"]["planToken"]
        removed = request(
            client, "POST", "/oauth/invalid-accounts/delete", {"planToken": plan_token}, headers,
        )
        assert removed.status_code == 200 and removed.json()["data"]["deleted"] == 1
        replay_delete = request(
            client, "POST", "/oauth/invalid-accounts/delete", {"planToken": plan_token}, headers,
        )
        assert replay_delete.status_code == 400

        quota_plan = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota-plan", {}, headers,
        )
        assert quota_plan.status_code == 200, quota_plan.text
        quota_token = quota_plan.json()["data"]["planToken"]
        reset = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": quota_token}, headers,
        )
        assert reset.status_code == 200, reset.text
        assert backend.last_reset_idempotency_key
        replay_reset = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": quota_token}, headers,
        )
        assert replay_reset.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_login_and_import_capabilities_split_public_path_ids_from_body_secrets(tmp_path):
    app, runtime, control, backend = oauth_app(tmp_path)
    domain_audit = BoundedAuditSink()
    control._audit_sink = domain_audit
    request_urls = []

    @app.middleware("http")
    async def capture_request_url(request_, call_next):
        request_urls.append(str(request_.url))
        return await call_next(request_)

    with TestClient(app) as client:
        headers = bearer(create_session(client))
        flows = [
            request(client, "POST", "/oauth/login-flows", {"provider": "openai"}, headers).json()["data"]
            for _ in range(2)
        ]
        for flow in flows:
            assert flow["flowId"].startswith("oflow_")
            assert "." not in flow["flowId"]
            assert flow["flowSecret"] not in flow["flowId"]

        state = parse_qs(urlparse(flows[0]["authUrl"]).query)["state"][0]
        before_accounts = len(backend.accounts)
        wrong_secret = request(
            client,
            "POST",
            f"/oauth/login-flows/{flows[0]['flowId']}/complete",
            {"flowSecret": "wrong-secret-value", "code": "flow-code", "state": state},
            headers,
        )
        cross_flow = request(
            client,
            "POST",
            f"/oauth/login-flows/{flows[0]['flowId']}/complete",
            {"flowSecret": flows[1]["flowSecret"], "code": "flow-code", "state": state},
            headers,
        )
        assert [wrong_secret.status_code, cross_flow.status_code] == [400, 400]
        assert all(
            item.json()["error"]["code"] == "INVALID_OPERATION_STATE"
            for item in (wrong_secret, cross_flow)
        )
        assert backend.provider_exchange_count == 0
        assert len(backend.accounts) == before_accounts

        completed = request(
            client,
            "POST",
            f"/oauth/login-flows/{flows[0]['flowId']}/complete",
            {"flowSecret": flows[0]["flowSecret"], "code": "flow-code", "state": state},
            headers,
        )
        assert completed.status_code == 200, completed.text
        after_login_accounts = len(backend.accounts)
        replay = request(
            client,
            "POST",
            f"/oauth/login-flows/{flows[0]['flowId']}/complete",
            {"flowSecret": flows[0]["flowSecret"], "code": "flow-code", "state": state},
            headers,
        )
        assert replay.status_code == 400
        assert backend.provider_exchange_count == 1
        assert len(backend.accounts) == after_login_accounts

        previews = []
        for index in range(2):
            candidate = {
                "provider": "openai",
                "type": "openai",
                "email": f"split-import-{index}@example.test",
                "refresh_token": f"split-import-refresh-{index}-1234567890",
            }
            preview = request(
                client,
                "POST",
                "/oauth/imports/preview",
                {"format": "openai", "payload": json.dumps([candidate])},
                headers,
            )
            assert preview.status_code == 200, preview.text
            previews.append(preview.json()["data"])
        for preview in previews:
            assert preview["importId"].startswith("oimport_")
            assert "." not in preview["importId"]
            assert preview["importSecret"] not in preview["importId"]

        decisions = [{"candidateId": "candidate-1", "action": "overwrite"}]
        before_import_accounts = len(backend.accounts)
        wrong_import = request(
            client,
            "POST",
            f"/oauth/imports/{previews[0]['importId']}/commit",
            {"importSecret": "wrong-secret-value", "decisions": decisions},
            headers,
        )
        cross_import = request(
            client,
            "POST",
            f"/oauth/imports/{previews[0]['importId']}/commit",
            {"importSecret": previews[1]["importSecret"], "decisions": decisions},
            headers,
        )
        assert [wrong_import.status_code, cross_import.status_code] == [400, 400]
        assert all(
            item.json()["error"]["code"] == "INVALID_OPERATION_STATE"
            for item in (wrong_import, cross_import)
        )
        assert len(backend.accounts) == before_import_accounts

        committed = request(
            client,
            "POST",
            f"/oauth/imports/{previews[0]['importId']}/commit",
            {"importSecret": previews[0]["importSecret"], "decisions": decisions},
            headers,
        )
        assert committed.status_code == 200, committed.text
        after_import_accounts = len(backend.accounts)
        replay_import = request(
            client,
            "POST",
            f"/oauth/imports/{previews[0]['importId']}/commit",
            {"importSecret": previews[0]["importSecret"], "decisions": decisions},
            headers,
        )
        assert replay_import.status_code == 400
        assert len(backend.accounts) == after_import_accounts

        capability_secrets = {
            *(flow["flowSecret"] for flow in flows),
            *(preview["importSecret"] for preview in previews),
        }
        assert all(secret not in url for secret in capability_secrets for url in request_urls)
        assert all(
            secret not in response.text
            for secret in capability_secrets
            for response in (wrong_secret, cross_flow, replay, wrong_import, cross_import, replay_import)
        )
        assert all(secret not in repr(runtime.state_store.audit_snapshot()) for secret in capability_secrets)
        assert all(secret not in repr(domain_audit.snapshot()) for secret in capability_secrets)
        assert all(secret not in json.dumps(app.openapi()) for secret in capability_secrets)


def test_models_settings_preferences_defaults_actions_and_operations(tmp_path):
    client, headers, runtime, _, backend = auth_client(tmp_path)
    try:
        model_list = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}/models", None, headers)
        assert model_list.status_code == 200, model_list.text
        model_revision = model_list.json()["data"]["revision"]
        assert [item["modelId"] for item in model_list.json()["data"]["items"]] == ["gpt-alpha", "gpt-beta"]
        updated = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}/models",
            {"modelIds": ["gpt-alpha"], "disabled": True},
            {**headers, "If-Match": model_revision},
        )
        assert updated.status_code == 200, updated.text
        assert backend.account_disabled_models(ACCOUNT_ID) == {"gpt-alpha", "gpt-beta"}
        cursor_id = "cursor:cursor-api-subject"
        backend.accounts.append({
            "_id": cursor_id,
            "provider": "cursor",
            "type": "cursor",
            "subject": "cursor-api-subject",
            "email": "cursor@example.test",
            "access_token": "cursor-access",
            "refresh_token": "cursor-refresh",
            "enabled": True,
            "models": ["cursor-max"],
            "model_records": [{
                "id": "cursor-max",
                "name": "Cursor Max",
                "contextWindow": 128000,
                "contextWindowMaxMode": 200000,
            }],
        })
        settings_update = request(
            client, "PATCH", f"/oauth/accounts/{cursor_id}/models/settings",
            {"modelId": "cursor-max", "maxContextDefault": False}, headers,
        )
        assert settings_update.status_code == 200, settings_update.text
        assert backend.cursor_max_context_default(cursor_id, "cursor-max") is False
        wrong_provider = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}/models/settings",
            {"modelId": "gpt-alpha", "maxContextDefault": False}, headers,
        )
        assert wrong_provider.status_code == 422
        assert wrong_provider.json()["error"]["fields"][0]["path"] == "modelId"
        invalid_model = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}/models",
            {"modelIds": ["unknown"], "disabled": True}, headers,
        )
        assert invalid_model.status_code == 422
        assert invalid_model.json()["error"]["code"] == "VALIDATION_FAILED"

        synced = request(client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/models/actions/sync", None, headers)
        assert synced.status_code == 202, synced.text
        operation_id = synced.json()["data"]["id"]
        polled = client.get(f"/api/management/v1/operations/{operation_id}", headers=headers)
        assert polled.status_code == 200 and polled.json()["data"]["status"] == "succeeded"

        token_refresh = request(client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/refresh-token", None, headers)
        assert token_refresh.status_code == 200
        assert "rotated-access-secret" not in token_refresh.text
        usage_refresh = request(client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/refresh-usage", None, headers)
        assert usage_refresh.status_code == 202
        all_refresh = request(client, "POST", "/oauth/actions/refresh-usage", None, headers)
        assert all_refresh.status_code == 202

        cleared = request(client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/clear-errors", None, headers)
        assert cleared.status_code == 204 and backend.cooldowns == []
        affinity = request(client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/clear-affinity", None, headers)
        assert affinity.status_code == 204 and backend.affinity_cleared == [ACCOUNT_ID]
        backend.cooldowns.append({"channel_key": f"oauth:{ACCOUNT_ID}", "model": "gpt-alpha"})
        clear_all = request(client, "POST", "/oauth/actions/clear-errors", None, headers)
        assert clear_all.status_code == 200 and clear_all.json()["data"]["cleared"] == 1

        settings = request(client, "GET", "/oauth/settings", None, headers)
        assert settings.status_code == 200
        settings_revision = settings.json()["data"]["revision"]
        changed = request(
            client, "PATCH", "/oauth/settings",
            {"quotaMonitor": {"enabled": True, "intervalSeconds": 120, "thresholdPercent": 90}, "cchMode": "dynamic", "antigravityTlsFingerprintEnabled": True},
            {**headers, "If-Match": settings_revision},
        )
        assert changed.status_code == 200, changed.text
        assert backend.settings == [True, 120, 90.0, "dynamic", True]
        bad_settings = request(client, "PATCH", "/oauth/settings", {"quotaMonitor": {"intervalSeconds": 1}}, headers)
        assert bad_settings.status_code == 422
        assert bad_settings.json()["error"]["fields"]

        preferences = request(client, "GET", "/preferences/telegram/oauth", None, headers)
        pref_revision = preferences.json()["data"]["revision"]
        preference_update = request(
            client, "PATCH", "/preferences/telegram/oauth",
            {"usageDisplayMode": "remaining", "quotaProgressBar": False},
            {**headers, "If-Match": pref_revision},
        )
        assert preference_update.status_code == 200
        assert backend.preferences == ["remaining", False]

    finally:
        client.__exit__(None, None, None)


def test_oauth_read_write_parity_between_telegram_adapter_and_api(tmp_path, monkeypatch):
    tg_control, tg_backend = build_control()
    monkeypatch.setattr(oauth_menu, "oauth_control", tg_control)
    monkeypatch.setattr(oauth_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(oauth_menu.ui, "edit", lambda *args, **kwargs: None)

    assert oauth_menu._quota_monitor_values() == (False, 60, 95.0)
    oauth_menu.on_quota_toggle(42, 100, "callback")
    assert tg_backend.settings == [True, 60, 95.0, "disabled", False]

    client, headers, _, _, api_backend = auth_client(tmp_path)
    try:
        read = request(client, "GET", "/oauth/settings", None, headers)
        assert read.status_code == 200
        assert read.json()["data"]["quotaMonitor"] == {
            "enabled": False, "intervalSeconds": 60, "thresholdPercent": 95.0,
        }
        assert read.json()["data"]["antigravityTlsFingerprintEnabled"] is False
        written = request(
            client, "PATCH", "/oauth/settings",
            {"quotaMonitor": {"enabled": True}}, headers,
        )
        assert written.status_code == 200
        assert api_backend.settings == tg_backend.settings
    finally:
        client.__exit__(None, None, None)


def test_owned_telegram_menus_have_no_direct_oauth_business_reads_or_writes():
    domain_aliases = {
        "config", "oauth_manager", "state_db", "cooldown", "affinity",
        "load_balancing", "log_db", "cursor_model_catalog", "cursor_provider",
        "openai_provider", "xai_provider", "antigravity_provider",
    }
    # Presentation-only helpers remain in the Telegram adapter. They neither
    # read nor mutate OAuth business state and intentionally preserve frozen text.
    presentation_allowlist = {
        ("antigravity_provider", "format_credits_usage_text"),
        ("antigravity_provider", "credits_tier_label"),
    }
    violations = []
    for relative in (
        "src/telegram/menus/oauth_menu.py",
        "src/telegram/menus/oauth_account_models_menu.py",
    ):
        tree = ast.parse(Path(relative).read_text(), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in domain_aliases:
                continue
            if (owner.id, node.func.attr) not in presentation_allowlist:
                violations.append((relative, node.lineno, owner.id, node.func.attr))
    assert violations == []


@pytest.mark.parametrize("provider", list(OAuthProvider))
def test_control_login_flows_cover_every_supported_provider(provider):
    from src.management_control.oauth.menu_bridge import telegram_context

    control, backend = build_control()
    context = telegram_context(42)
    flow = control.start_login_flow(context, provider, **({"site": "bigmodel"} if provider is OAuthProvider.ZHIPU else {}))
    if provider is OAuthProvider.ZHIPU:
        poll = control.poll_login_flow(context, flow.flow_id, flow.flow_secret)
        assert poll.status == "completed" and poll.save_status == "created"
        assert backend.get_account(poll.account_id) is not None
        command = CompleteOAuthLoginCommand(completed=True)
    elif provider is OAuthProvider.WORKBUDDY:
        poll = control.poll_login_flow(context, flow.flow_id, flow.flow_secret)
        assert poll.status == "completed" and poll.save_status == "created"
        assert backend.get_account(poll.account_id) is not None
        command = CompleteOAuthLoginCommand(completed=True)
    elif provider is OAuthProvider.CURSOR:
        command = CompleteOAuthLoginCommand(completed=True)
    else:
        state = parse_qs(urlparse(flow.auth_url or "").query)["state"][0]
        if provider is OAuthProvider.ANTIGRAVITY:
            command = CompleteOAuthLoginCommand(
                callback_url=f"http://localhost/callback?code=provider-code&state={state}",
            )
        else:
            command = CompleteOAuthLoginCommand(code="provider-code", state=state)
    result = control.complete_login_flow(context, flow.flow_id, flow.flow_secret, command)
    assert result.status == "created"
    account = backend.get_account(result.account_id)
    assert account is not None
    assert backend.provider_of(account) == provider.value
    if provider is OAuthProvider.ZHIPU:
        assert account["credential_mode"] == "oauth" and account["model_key"] == "fixture.secret"
        assert account.get("access_token") and not account.get("refresh_token")
    else:
        assert account.get("access_token") and account.get("refresh_token")


@pytest.mark.parametrize(
    "credential, expected_provider",
    [
        (
            JsonCredential(
                provider=OAuthProvider.CLAUDE,
                payload=json.dumps({
                    "email": "json@example.test",
                    "access_token": "json-access",
                    "refresh_token": "json-refresh",
                }),
            ),
            "claude",
        ),
        (
            RefreshTokenCredential(
                provider=OAuthProvider.OPENAI,
                refresh_token="openai-refresh-token-long-enough",
                email_hint="refresh-openai@example.test",
            ),
            "openai",
        ),
        (
            RefreshTokenCredential(
                provider=OAuthProvider.XAI,
                refresh_token="xai-refresh-token-long-enough",
                email_hint="refresh-xai@example.test",
            ),
            "xai",
        ),
    ],
)
def test_control_supports_json_and_registered_refresh_token_credentials(
    credential, expected_provider,
):
    from src.management_control.oauth.menu_bridge import telegram_context

    control, backend = build_control()
    result = control.create_account(
        telegram_context(42), CreateOAuthAccountCommand(credential),
    )
    assert result.status == "created"
    assert backend.provider_of(backend.get_account(result.account_id)) == expected_provider


def test_control_rejects_unregistered_refresh_token_provider():
    from src.management_control.oauth.menu_bridge import telegram_context

    control, _ = build_control()
    with pytest.raises(ManagementError) as unsupported:
        control.create_account(
            telegram_context(42),
            CreateOAuthAccountCommand(
                RefreshTokenCredential(
                    provider=OAuthProvider.CURSOR,
                    refresh_token="cursor-refresh-token-long-enough",
                )
            ),
        )
    assert unsupported.value.code is ManagementErrorCode.UNSUPPORTED_VALUE


def test_control_plan_expiry_actor_binding_and_revision_conflict():
    from datetime import datetime, timedelta, timezone

    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    control, backend = build_control()
    # Build a second instance with an injectable clock; this also verifies all
    # nested one-shot stores use the control clock rather than wall time.
    from src.management_control.oauth import OAuthControl
    from src.tests.management_oauth_fakes import ImmediateExecutor

    control = OAuthControl(backend, clock=lambda: now[0], executor=ImmediateExecutor())
    from src.management_control.oauth.menu_bridge import telegram_context

    owner = telegram_context(42)
    other = telegram_context(43)
    plan = control.plan_invalid_deletion(owner, [INVALID_ID])
    with pytest.raises(ManagementError) as actor_error:
        control.delete_invalid_accounts(other, plan.plan_token)
    assert actor_error.value.code is ManagementErrorCode.INVALID_OPERATION_STATE

    now[0] += timedelta(minutes=11)
    with pytest.raises(ManagementError) as expired:
        control.delete_invalid_accounts(owner, plan.plan_token)
    assert expired.value.code is ManagementErrorCode.INVALID_OPERATION_STATE

    created = control.create_account(
        owner,
        CreateOAuthAccountCommand(
            ManualCredential(
                provider=OAuthProvider.CLAUDE,
                email="plan@example.test",
                access_token="first-access",
                refresh_token="first-refresh",
            )
        ),
    )
    assert created.status == "created"
    with pytest.raises(ManagementError) as conflict:
        control.create_account(
            owner,
            CreateOAuthAccountCommand(
                ManualCredential(
                    provider=OAuthProvider.CLAUDE,
                    email="plan@example.test",
                    access_token="second-access",
                    refresh_token="second-refresh",
                )
            ),
        )
    assert conflict.value.code is ManagementErrorCode.IDENTITY_CONFLICT
