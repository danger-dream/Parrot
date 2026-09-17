from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from src.management_api.routers.oauth import router as oauth_router
from src.management_api.routers.oauth_support import get_oauth_control_dependency
from src.management_control import BoundedAuditSink, ManagementError, ManagementErrorCode
from src.management_control.oauth import (
    CompleteOAuthLoginCommand,
    CreateOAuthAccountCommand,
    ManualCredential,
    OAuthControl,
    OAuthProvider,
)
from src.management_control.oauth.contracts import is_sensitive_key, sanitize_text
from src.tests.management_oauth_fakes import ImmediateExecutor, InMemoryOAuthBackend, build_control
from src.tests.test_management_api_foundation import bearer, build_app, create_session
from src.tests.test_management_oauth_api import (
    ACCOUNT_ID,
    INVALID_ID,
    ROUTE_REQUESTS,
    auth_client,
    request,
)


def snapshot_backend(backend):
    return copy.deepcopy({
        "accounts": backend.accounts,
        "quota": backend.quota,
        "cooldowns": backend.cooldowns,
        "settings": backend.settings,
        "preferences": backend.preferences,
        "affinity": backend.affinity_cleared,
        "reset": backend.last_reset_idempotency_key,
        "exchange": backend.provider_exchange_count,
    })


def replace_token(response):
    assert "replacePlanToken" not in repr(response.json()["error"])
    return response.json()["conflict"]["replacePlanToken"]


def manual(email, access="access-value", refresh="refresh-value"):
    return {
        "kind": "manual",
        "provider": "claude",
        "email": email,
        "accessToken": access,
        "refreshToken": refresh,
    }


def add_cursor(backend, *, model="cursor-max", maximum=200000):
    account_id = "cursor:cursor-api-subject"
    backend.accounts.append({
        "_id": account_id,
        "provider": "cursor",
        "type": "cursor",
        "subject": "cursor-api-subject",
        "email": "cursor@example.test",
        "access_token": "cursor-access",
        "refresh_token": "cursor-refresh",
        "enabled": True,
        "models": [model],
        "model_records": [
            {
                "id": model,
                "name": "Cursor Max",
                "contextWindow": 128000,
                "contextWindowMaxMode": maximum,
            },
            {
                "id": "hidden-model",
                "name": "Hidden",
                "contextWindow": 128000,
                "contextWindowMaxMode": 200000,
            },
        ],
    })
    return account_id


def test_all_32_oauth_routes_reject_unknown_query_before_any_side_effect(tmp_path):
    client, headers, runtime, control, backend = auth_client(tmp_path)
    try:
        domain_audit = BoundedAuditSink()
        control._audit_sink = domain_audit
        before = snapshot_backend(backend)
        operation_count = len(runtime.operations._items)
        audit_count = len(runtime.state_store.audit_snapshot())
        for method, path, body, extra in ROUTE_REQUESTS:
            separator = "&" if "?" in path else "?"
            response = request(
                client, method, path + separator + "rogueParameter=1", body,
                {**extra, **headers},
            )
            assert response.status_code == 422, (method, path, response.text)
            assert response.json()["error"]["code"] == "VALIDATION_FAILED"
            assert response.json()["error"]["fields"][0]["path"] == "rogueParameter"
        assert snapshot_backend(backend) == before
        assert len(runtime.operations._items) == operation_count
        assert len(runtime.state_store.audit_snapshot()) == audit_count
        assert domain_audit.snapshot() == ()
    finally:
        client.__exit__(None, None, None)


def test_public_times_are_rfc3339_utc_and_permanent_cooldown_is_explicit(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        account = backend.get_account(ACCOUNT_ID)
        account["disabled_until"] = 1_800_000_000
        account["expired"] = "2027-01-15T08:00:00+08:00"
        account["last_model_sync"] = 1_800_000_000_000
        backend.quota[ACCOUNT_ID]["five_hour_reset"] = 1_800_003_600
        response = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        values = [
            data["account"]["disabledUntil"],
            data["expiresAt"],
            data["lastModelSync"],
            data["usageWindows"][0]["resetsAt"],
        ]
        assert all(value.endswith("Z") for value in values)
        runtime_error = data["runtimeErrors"][0]
        assert runtime_error == {
            "modelId": "gpt-beta",
            "message": "fake failure",
            "cooldownUntil": None,
            "cooldownPermanent": True,
        }
        models = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}/models", None, headers)
        permanent = next(item for item in models.json()["data"]["items"] if item["modelId"] == "gpt-beta")
        assert permanent["cooldownUntil"] is None
        assert permanent["cooldownPermanent"] is True
        # API normalization never rewrites TG/domain raw epochs or -1 sentinels.
        assert account["disabled_until"] == 1_800_000_000
        assert backend.cooldowns[0]["cooldown_until"] == -1
    finally:
        client.__exit__(None, None, None)


def test_public_text_and_operation_results_use_exact_known_fields(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        ordinary = "Bearer provider routing is enabled; Basic tier remains active"
        backend.cooldowns[0]["last_error_message"] = ordinary
        backend.cooldowns[0]["last_error"] = "obsolete fake-only shape"
        response = request(
            client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["runtimeErrors"][0]["message"] == ordinary
        assert backend.cooldowns[0]["last_error_message"] == ordinary

        backend.cooldowns[0].pop("last_error_message")
        legacy_only = request(
            client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers,
        )
        assert legacy_only.status_code == 200
        assert legacy_only.json()["data"]["runtimeErrors"][0]["message"] is None

        secret = "SENTINEL-CREDENTIAL-987654"
        backend.sync_result = {
            "action": "updated",
            "account_key": ACCOUNT_ID,
            "key": "public-model-key",
            "channelKey": "oauth:public-channel",
            "credentialConfigured": True,
            "apiToken": secret,
            "message": ordinary,
        }
        started = request(
            client, "POST",
            f"/oauth/accounts/{ACCOUNT_ID}/models/actions/sync", None, headers,
        )
        operation = client.get(
            f"/api/management/v1/operations/{started.json()['data']['id']}",
            headers=headers,
        )
        result = operation.json()["data"]["result"]
        assert result["accountId"] == ACCOUNT_ID
        assert result["key"] == "public-model-key"
        assert result["channelKey"] == "oauth:public-channel"
        assert result["credentialConfigured"] is True
        assert result["apiToken"] == "[REDACTED]"
        assert result["message"] == ordinary
        assert secret not in operation.text
        assert backend.sync_result["apiToken"] == secret

        assert sanitize_text(ordinary) == ordinary
        assert is_sensitive_key("apiToken")
        assert not is_sensitive_key("key")
        assert not is_sensitive_key("channelKey")
        assert not is_sensitive_key("account_key")
    finally:
        client.__exit__(None, None, None)
def test_runtime_reuses_audit_bound_control_and_audits_success_and_failure(tmp_path):
    from src import config

    app, runtime, _ = build_app(tmp_path)
    app.include_router(oauth_router, prefix="/api/management/v1")
    app.openapi_schema = None
    original_config = copy.deepcopy(config.get())
    try:
        with TestClient(app) as client:
            headers = bearer(create_session(client))
            first = get_oauth_control_dependency(runtime)
            second = get_oauth_control_dependency(runtime)
            assert first is second
            assert first._audit_sink is runtime.audit_sink

            current = request(client, "GET", "/preferences/telegram/oauth", None, headers)
            revision = current.json()["data"]["revision"]
            succeeded = request(
                client,
                "PATCH",
                "/preferences/telegram/oauth",
                {"quotaProgressBar": not current.json()["data"]["quotaProgressBar"]},
                {**headers, "If-Match": revision, "X-Request-Id": "oauth-audit-success"},
            )
            assert succeeded.status_code == 200, succeeded.text
            failed = request(
                client,
                "PATCH",
                "/preferences/telegram/oauth",
                {"quotaProgressBar": True},
                {**headers, "If-Match": revision, "X-Request-Id": "oauth-audit-failure"},
            )
            assert failed.status_code == 409
            records = [
                item for item in runtime.state_store.audit_snapshot()
                if item["action"] == "oauth.telegram-preferences.update"
            ]
            assert {(item["result"], item["request_id"]) for item in records} >= {
                ("succeeded", "oauth-audit-success"),
                ("failed", "oauth-audit-failure"),
            }
            assert all(
                item["actor"] == "administrator"
                and item["target"] == "telegramOAuthPreferences"
                and item["occurred_at"]
                for item in records
            )
            assert "credential" not in repr(records).lower()
    finally:
        def restore(cfg):
            cfg.clear()
            cfg.update(copy.deepcopy(original_config))

        config.update(restore)


def test_quota_plan_binds_path_actor_account_and_quota_observation_before_consume(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        def plan():
            response = request(
                client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota-plan", {}, headers,
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]["planToken"]

        cross = plan()
        wrong = request(
            client, "POST", f"/oauth/accounts/{INVALID_ID}/actions/reset-quota",
            {"planToken": cross}, headers,
        )
        assert wrong.status_code == 400
        assert backend.last_reset_idempotency_key is None
        right = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": cross}, headers,
        )
        assert right.status_code == 200

        backend.quota[ACCOUNT_ID] = {
            "five_hour_util": 12.0,
            "openai_reset_credit_count": 1,
        }
        stale_quota = plan()
        original_quota = copy.deepcopy(backend.quota[ACCOUNT_ID])
        backend.quota[ACCOUNT_ID]["openai_reset_credit_count"] = 2
        before_key = backend.last_reset_idempotency_key
        stale = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": stale_quota}, headers,
        )
        assert stale.status_code == 409
        assert backend.last_reset_idempotency_key == before_key
        backend.quota[ACCOUNT_ID] = original_quota
        assert request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": stale_quota}, headers,
        ).status_code == 200

        backend.quota[ACCOUNT_ID] = copy.deepcopy(original_quota)
        stale_account = plan()
        account = backend.get_account(ACCOUNT_ID)
        old_label = account["label"]
        account["label"] = "concurrent-change"
        assert request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": stale_account}, headers,
        ).status_code == 409
        account["label"] = old_label
        assert request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": stale_account}, headers,
        ).status_code == 200
        replay = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/actions/reset-quota",
            {"planToken": stale_account}, headers,
        )
        assert replay.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_replace_plan_is_stable_bound_explicit_and_login_conflict_needs_no_reexchange(tmp_path):
    client, headers, _, control, backend = auth_client(tmp_path)
    try:
        candidate = manual("replace@example.test", "first-access", "first-refresh")
        assert request(client, "POST", "/oauth/accounts", {"credential": candidate}, headers).status_code == 201
        changed_candidate = {**candidate, "accessToken": "bound-access"}
        conflict = request(
            client, "POST", "/oauth/accounts", {"credential": changed_candidate}, headers,
        )
        token = replace_token(conflict)
        mismatched = request(
            client,
            "POST",
            "/oauth/accounts",
            {"credential": {**changed_candidate, "accessToken": "other-access"}, "replacePlanToken": token},
            headers,
        )
        assert mismatched.status_code == 409
        replaced = request(
            client,
            "POST",
            "/oauth/accounts",
            {"credential": changed_candidate, "replacePlanToken": token},
            headers,
        )
        assert replaced.status_code == 201, replaced.text
        assert backend.get_account("claude:replace@example.test")["access_token"] == "bound-access"
        assert request(
            client,
            "POST",
            "/oauth/accounts",
            {"credential": changed_candidate, "replacePlanToken": token},
            headers,
        ).status_code == 400

        # Pre-create the identity returned by the provider exchange.
        backend.add_account_if_absent({
            "provider": "openai",
            "type": "openai",
            "email": "flow@example.test",
            "workspace_id": "flow-workspace",
            "chatgpt_account_id": "flow-workspace",
            "access_token": "old-flow-access",
            "refresh_token": "old-flow-refresh",
            "models": [],
            "enabled": True,
        })
        started = request(client, "POST", "/oauth/login-flows", {"provider": "openai"}, headers)
        flow = started.json()["data"]
        state = parse_qs(urlparse(flow["authUrl"]).query)["state"][0]
        login_conflict = request(
            client,
            "POST",
            f"/oauth/login-flows/{flow['flowId']}/complete",
            {"flowSecret": flow["flowSecret"], "code": "provider-code", "state": state},
            headers,
        )
        assert login_conflict.status_code == 409
        assert backend.provider_exchange_count == 1
        login_token = replace_token(login_conflict)
        wrong_flow_secret = request(
            client,
            "POST",
            f"/oauth/login-flows/{flow['flowId']}/complete",
            {"flowSecret": "wrong-secret-value", "replacePlanToken": login_token},
            headers,
        )
        cross_flow_id = request(
            client,
            "POST",
            f"/oauth/login-flows/{flow['flowId']}-other/complete",
            {"replacePlanToken": login_token},
            headers,
        )
        assert wrong_flow_secret.status_code == cross_flow_id.status_code == 400
        assert wrong_flow_secret.json()["error"]["code"] == "INVALID_OPERATION_STATE"
        assert cross_flow_id.json()["error"]["code"] == "INVALID_OPERATION_STATE"
        assert backend.get_account("openai:flow@example.test:flow-workspace")["access_token"] == "old-flow-access"
        assert backend.provider_exchange_count == 1

        committed = request(
            client,
            "POST",
            f"/oauth/login-flows/{flow['flowId']}/complete",
            {"replacePlanToken": login_token},
            headers,
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["data"]["status"] == "replaced"
        assert backend.provider_exchange_count == 1
    finally:
        client.__exit__(None, None, None)


def test_concurrent_login_complete_exchanges_and_commits_at_most_once():
    backend = InMemoryOAuthBackend()
    control = OAuthControl(backend, executor=ImmediateExecutor())
    from src.management_control.oauth.menu_bridge import telegram_context

    context = telegram_context(42)
    flow = control.start_login_flow(context, OAuthProvider.OPENAI)
    state = parse_qs(urlparse(flow.auth_url).query)["state"][0]
    command = CompleteOAuthLoginCommand(code="provider-code", state=state)
    barrier = Barrier(3)

    def complete():
        barrier.wait()
        try:
            return control.complete_login_flow(
                context, flow.flow_id, flow.flow_secret, command,
            ).status
        except ManagementError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete) for _ in range(2)]
        barrier.wait()
        results = [future.result() for future in futures]
    assert backend.provider_exchange_count == 1
    assert results.count("created") == 1
    assert results.count(ManagementErrorCode.INVALID_OPERATION_STATE) == 1
    assert len([item for item in backend.accounts if item.get("email") == "flow@example.test"]) == 1


ACCOUNT_PATH_MUTATIONS = [
    ("GET", "/oauth/accounts/{id}", None, {}),
    ("PATCH", "/oauth/accounts/{id}", {"enabled": False}, {}),
    ("DELETE", "/oauth/accounts/{id}", None, {"If-Match": "anything"}),
    ("POST", "/oauth/accounts/{id}/actions/refresh-token", None, {}),
    ("POST", "/oauth/accounts/{id}/actions/refresh-usage", None, {}),
    ("POST", "/oauth/accounts/{id}/actions/reset-quota-plan", {}, {}),
    ("POST", "/oauth/accounts/{id}/actions/reset-quota", {"planToken": "invalid.invalid"}, {}),
    ("POST", "/oauth/accounts/{id}/actions/clear-errors", None, {}),
    ("POST", "/oauth/accounts/{id}/actions/clear-affinity", None, {}),
    ("GET", "/oauth/accounts/{id}/models", None, {}),
    ("PATCH", "/oauth/accounts/{id}/models", {"modelIds": ["gpt-alpha"], "disabled": True}, {}),
    ("PATCH", "/oauth/accounts/{id}/models/settings", {"modelId": "gpt-alpha", "maxContextDefault": True}, {}),
    ("POST", "/oauth/accounts/{id}/models/actions/sync", None, {}),
]


@pytest.mark.parametrize("alias", [
    "admin@example.test", "openai:admin@example.test", "workspace-1", "unknown-account",
])
def test_every_account_path_requires_exact_canonical_id_with_zero_side_effect(alias, tmp_path):
    client, headers, runtime, _, backend = auth_client(tmp_path)
    try:
        before = snapshot_backend(backend)
        op_count = len(runtime.operations._items)
        for method, pattern, body, extra in ACCOUNT_PATH_MUTATIONS:
            response = request(
                client, method, pattern.format(id=alias), body, {**extra, **headers},
            )
            assert response.status_code == 404, (method, pattern, alias, response.text)
            assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert snapshot_backend(backend) == before
        assert len(runtime.operations._items) == op_count
        # The raw/TG compatibility lookup intentionally continues to accept aliases.
        if alias in {"admin@example.test", "openai:admin@example.test"}:
            assert backend.get_account(alias) is backend.get_account(ACCOUNT_ID)
    finally:
        client.__exit__(None, None, None)


def test_atomic_cas_prevents_interleaving_lost_updates_across_all_owned_mutations(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        detail = request(client, "GET", f"/oauth/accounts/{ACCOUNT_ID}", None, headers)
        revision = detail.json()["data"]["account"]["revision"]
        backend.interleave_once = lambda: backend.get_account(ACCOUNT_ID).__setitem__("concurrent", "kept")
        update = request(
            client, "PATCH", f"/oauth/accounts/{ACCOUNT_ID}", {"displayName": "lost"},
            {**headers, "If-Match": revision},
        )
        assert update.status_code == 409
        assert backend.get_account(ACCOUNT_ID)["label"] == "Primary"
        assert backend.get_account(ACCOUNT_ID)["concurrent"] == "kept"

        listing = request(client, "GET", "/oauth/accounts", None, headers).json()["data"]
        added_id = "claude:concurrent@example.test"
        backend.interleave_once = lambda: backend.accounts.append({
            "_id": added_id,
            "provider": "claude",
            "email": "concurrent@example.test",
            "access_token": "a",
            "refresh_token": "r",
            "models": [],
        })
        reorder = request(
            client,
            "PUT",
            "/oauth/account-order",
            {"accountIds": list(reversed([ACCOUNT_ID, INVALID_ID]))},
            {**headers, "If-Match": listing["revision"]},
        )
        assert reorder.status_code == 409
        assert [backend.account_id(item) for item in backend.accounts][-1] == added_id

        model_revision = request(
            client, "GET", f"/oauth/accounts/{ACCOUNT_ID}/models", None, headers,
        ).json()["data"]["revision"]
        backend.interleave_once = lambda: backend.get_account(ACCOUNT_ID).__setitem__("models", ["gpt-alpha"])
        model_update = request(
            client,
            "PATCH",
            f"/oauth/accounts/{ACCOUNT_ID}/models",
            {"modelIds": ["gpt-beta"], "disabled": True},
            {**headers, "If-Match": model_revision},
        )
        assert model_update.status_code == 409
        assert backend.account_disabled_models(ACCOUNT_ID) == {"gpt-beta"}

        settings = request(client, "GET", "/oauth/settings", None, headers).json()["data"]
        backend.interleave_once = lambda: backend.settings.__setitem__(1, 333)
        changed = request(
            client, "PATCH", "/oauth/settings", {"cchMode": "dynamic"},
            {**headers, "If-Match": settings["revision"]},
        )
        assert changed.status_code == 409
        assert backend.settings[1] == 333 and backend.settings[3] == "disabled"

        preferences = request(client, "GET", "/preferences/telegram/oauth", None, headers).json()["data"]
        backend.interleave_once = lambda: backend.preferences.__setitem__(0, "remaining")
        pref = request(
            client, "PATCH", "/preferences/telegram/oauth", {"quotaProgressBar": False},
            {**headers, "If-Match": preferences["revision"]},
        )
        assert pref.status_code == 409
        assert backend.preferences == ["remaining", True]

        delete_revision = request(
            client, "GET", f"/oauth/accounts/{INVALID_ID}", None, headers,
        ).json()["data"]["account"]["revision"]
        backend.interleave_once = lambda: backend.get_account(INVALID_ID).__setitem__("concurrent", True)
        deleted = request(
            client, "DELETE", f"/oauth/accounts/{INVALID_ID}", None,
            {**headers, "If-Match": delete_revision},
        )
        assert deleted.status_code == 409
        assert backend.get_account(INVALID_ID)["concurrent"] is True
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "action, code",
    [
        ("error", "UPSTREAM_ERROR"),
        ("empty", "UPSTREAM_ERROR"),
        ("fetch_empty", "UPSTREAM_ERROR"),
        ("timeout", "UPSTREAM_TIMEOUT"),
        ("stale", "REVISION_CONFLICT"),
        ("network_disabled", "DEPENDENCY_UNAVAILABLE"),
    ],
)
def test_model_sync_business_failures_become_stable_failed_operations(action, code, tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        backend.sync_result = {
            "action": action,
            "error": "token=SENTINEL-SYNC-SECRET",
        }
        started = request(
            client, "POST", f"/oauth/accounts/{ACCOUNT_ID}/models/actions/sync", None, headers,
        )
        assert started.status_code == 202
        polled = client.get(
            f"/api/management/v1/operations/{started.json()['data']['id']}", headers=headers,
        )
        operation = polled.json()["data"]
        assert operation["status"] == "failed"
        assert operation["error"]["code"] == code
        assert operation["result"] is None
        assert "SENTINEL-SYNC-SECRET" not in polled.text
        assert backend.sync_result["error"].endswith("SENTINEL-SYNC-SECRET")
    finally:
        client.__exit__(None, None, None)


def test_cursor_setting_requires_cursor_visible_model_and_valid_tier_with_modelid_field(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        cursor_id = add_cursor(backend)
        good = request(
            client,
            "PATCH",
            f"/oauth/accounts/{cursor_id}/models/settings",
            {"modelId": "cursor-max", "maxContextDefault": False},
            headers,
        )
        assert good.status_code == 200, good.text
        hidden = request(
            client,
            "PATCH",
            f"/oauth/accounts/{cursor_id}/models/settings",
            {"modelId": "hidden-model", "maxContextDefault": True},
            headers,
        )
        assert hidden.status_code == 422
        assert hidden.json()["error"]["fields"][0]["path"] == "modelId"
        backend.get_account(cursor_id)["model_records"][0]["contextWindowMaxMode"] = 128000
        tier = request(
            client,
            "PATCH",
            f"/oauth/accounts/{cursor_id}/models/settings",
            {"modelId": "cursor-max", "maxContextDefault": True},
            headers,
        )
        assert tier.status_code == 422
        assert tier.json()["error"]["fields"][0] == {
            "path": "modelId",
            "code": "UNSUPPORTED_TIER",
            "message": "Model has no Max Context tier",
        }
    finally:
        client.__exit__(None, None, None)




def test_historical_modes_and_invalid_predicate_follow_frozen_tg_fallback(tmp_path):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        backend.settings[3] = "static"
        backend.preferences[0] = "legacy-invalid-mode"
        backend.accounts.append({
            "_id": "claude:no-email",
            "provider": "claude",
            "email": "",
            "disabled_reason": "auth_error",
            "access_token": "a",
            "refresh_token": "r",
            "models": [],
        })
        settings = request(client, "GET", "/oauth/settings", None, headers)
        preferences = request(client, "GET", "/preferences/telegram/oauth", None, headers)
        invalid = request(client, "GET", "/oauth/invalid-accounts", None, headers)
        assert settings.status_code == preferences.status_code == invalid.status_code == 200
        assert settings.json()["data"]["cchMode"] == "disabled"
        assert preferences.json()["data"]["usageDisplayMode"] == "used"
        assert [item["accountId"] for item in invalid.json()["data"]["items"]] == [INVALID_ID]
        assert backend.settings[3] == "static"
        assert backend.preferences[0] == "legacy-invalid-mode"
    finally:
        client.__exit__(None, None, None)


def test_replace_plan_actor_old_revision_and_candidate_revision_are_bound():
    from src.management_control.oauth.menu_bridge import telegram_context

    audit = BoundedAuditSink()
    backend = InMemoryOAuthBackend()
    control = OAuthControl(backend, audit_sink=audit, executor=ImmediateExecutor())
    owner = telegram_context(42)
    other = telegram_context(43)
    first = ManualCredential(
        OAuthProvider.CLAUDE, "bound@example.test", "first-access", "first-refresh",
    )
    second = ManualCredential(
        OAuthProvider.CLAUDE, "bound@example.test", "second-access", "second-refresh",
    )
    control.create_account(owner, CreateOAuthAccountCommand(first))
    with pytest.raises(ManagementError) as conflict:
        control.create_account(owner, CreateOAuthAccountCommand(second))
    token = conflict.value.plan_token
    assert "second-access" not in str(conflict.value)

    with pytest.raises(ManagementError) as wrong_actor:
        control.create_account(
            other, CreateOAuthAccountCommand(second, replace_plan_token=token),
        )
    assert wrong_actor.value.code is ManagementErrorCode.INVALID_OPERATION_STATE

    account = backend.get_account("claude:bound@example.test")
    original_label = account.get("label")
    account["label"] = "concurrent-old-account-change"
    with pytest.raises(ManagementError) as stale:
        control.create_account(
            owner, CreateOAuthAccountCommand(second, replace_plan_token=token),
        )
    assert stale.value.code is ManagementErrorCode.REVISION_CONFLICT
    if original_label is None:
        account.pop("label", None)
    else:
        account["label"] = original_label
    result = control.create_account(
        owner, CreateOAuthAccountCommand(second, replace_plan_token=token),
    )
    assert result.status == "replaced"
    assert backend.get_account(result.account_id)["access_token"] == "second-access"
    assert "second-access" not in repr(audit.snapshot())
    assert "second-refresh" not in repr(audit.snapshot())


def test_production_oauth_manager_cas_authority_rejects_stale_snapshots_without_writes():
    from src import config, oauth_manager

    original = copy.deepcopy(config.get().get("oauthAccounts") or [])
    account = {
        "email": "cas@example.test",
        "provider": "claude",
        "type": "claude",
        "access_token": "cas-access",
        "refresh_token": "cas-refresh",
        "enabled": True,
        "models": ["claude-test"],
    }
    try:
        config.update(lambda cfg: cfg.__setitem__("oauthAccounts", [copy.deepcopy(account)]))
        account_id = oauth_manager.get_account_key(account)
        exposed = copy.deepcopy(oauth_manager.get_account(account_id))
        config.update(
            lambda cfg: cfg["oauthAccounts"][0].__setitem__("concurrent", "survives")
        )
        stale_mutation = oauth_manager.mutate_account_if_unchanged(
            account_id, exposed, lambda current: current.__setitem__("enabled", False),
        )
        assert stale_mutation["status"] == "revision_conflict"
        assert oauth_manager.get_account(account_id)["enabled"] is True
        assert oauth_manager.get_account(account_id)["concurrent"] == "survives"

        replacement = {
            **account,
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "last_refresh": "2030-01-01T00:00:00Z",
        }
        stale_replace = oauth_manager.replace_exact_identity(
            account_id, replacement, expected_account=exposed,
        )
        assert stale_replace["status"] == "revision_conflict"
        assert oauth_manager.get_account(account_id)["access_token"] == "cas-access"

        order = [account_id]
        config.update(lambda cfg: cfg["oauthAccounts"].append({
            "email": "added@example.test",
            "provider": "claude",
            "access_token": "a",
            "refresh_token": "r",
        }))
        stale_order = oauth_manager.reorder_accounts_if_unchanged(order, order)
        assert stale_order["status"] == "revision_conflict"
        assert len(config.get()["oauthAccounts"]) == 2
        stale_delete = oauth_manager.delete_account_if_unchanged(account_id, exposed)
        assert stale_delete["status"] == "revision_conflict"
        assert oauth_manager.get_account(account_id) is not None

        configured = copy.deepcopy(config.get()["oauthAccounts"])
        duplicate = copy.deepcopy(configured[0])
        config.update(lambda cfg: cfg["oauthAccounts"].append(duplicate))
        duplicate_order = [
            oauth_manager.get_account_key(item) for item in configured
        ] + [account_id]
        refused = oauth_manager.reorder_accounts_if_unchanged(
            duplicate_order, list(dict.fromkeys(duplicate_order)),
        )
        assert refused["status"] == "resource_conflict"
        assert len(config.get()["oauthAccounts"]) == 3
    finally:
        config.update(lambda cfg: cfg.__setitem__("oauthAccounts", original))


def test_typed_business_validation_indexes_each_model_id(tmp_path):
    client, headers, _, _, _ = auth_client(tmp_path)
    try:
        response = request(
            client,
            "PATCH",
            f"/oauth/accounts/{ACCOUNT_ID}/models",
            {"modelIds": ["unknown-a", "gpt-alpha", "unknown-b"], "disabled": True},
            headers,
        )
        assert response.status_code == 422
        assert [item["path"] for item in response.json()["error"]["fields"]] == [
            "modelIds[0]", "modelIds[2]",
        ]
    finally:
        client.__exit__(None, None, None)
