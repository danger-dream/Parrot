from __future__ import annotations

from collections.abc import Mapping
import csv
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.management_api import (
    ManagementRuntime,
    install_management_error_handlers,
    install_management_routers,
)
from src.management_auth import (
    ApprovalService,
    AuthMethod,
    Capability,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import OperationRegistry, OperationStore, StoreAuditSink
from src.management_control.channels.models import ChannelProtocol, CompatibilityMode
from src.management_control.oauth.models import OAuthProvider
from src.management_api.schemas.mapping import Ingress
from src.providers.catalog import PROVIDER_CATALOG


FAKE_KEY = "pmk_" + "S" * 64
PRODUCTION_OPERATIONS = (
    Path(__file__).parent / "fixtures/management_api/production-operation-ids.txt"
)
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


class _Notifier:
    def send(self, admin_ids, notification):
        del admin_ids, notification
        return True


@pytest.fixture
def production_app(tmp_path):
    store = ManagementStateStore(str(tmp_path / "s8-management.db"), clock=time.time)
    sessions = SessionService(
        store,
        management_key=FAKE_KEY,
        policy=SessionPolicy(
            idle_timeout_seconds=3 * 24 * 60 * 60,
            absolute_timeout_seconds=30 * 24 * 60 * 60,
            touch_interval_seconds=300,
        ),
        clock=time.time,
    )
    approvals = ApprovalService(
        store,
        clock=time.time,
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
        allowed_origins=frozenset(),
        application_version="s8.test",
        documentation_url="https://docs.example.test/management-v1",
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    install_management_routers(app)
    install_management_error_handlers(app)
    try:
        yield app, runtime
    finally:
        runtime.close()


def _create_management_key_session(client: TestClient) -> str:
    response = client.post(
        "/api/management/v1/auth/sessions",
        json={"grantType": "managementKey", "managementKey": FAKE_KEY},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["credential"]


def _bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _openapi_operations(document: dict) -> dict[str, tuple[str, str, str]]:
    result = {}
    for path, path_item in document["paths"].items():
        if not path.startswith("/api/management/v1"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation["operationId"]
            assert operation_id not in result
            result[operation_id] = (operation["tags"][0], method.upper(), path)
    return result


def _raw_enum_value_sets(document: dict) -> set[frozenset[str]]:
    schemas = document["components"]["schemas"]
    found: set[frozenset[str]] = set()

    def walk(node, active_refs=frozenset()):
        if isinstance(node, list):
            for value in node:
                walk(value, active_refs)
            return
        if not isinstance(node, Mapping):
            return
        ref = node.get("$ref")
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix):
            name = ref[len(prefix) :]
            if name not in active_refs:
                walk(schemas[name], active_refs | {name})
            return
        values = node.get("enum")
        if isinstance(values, list):
            found.add(frozenset(str(value) for value in values if value is not None))
        elif "const" in node and node["const"] is not None:
            found.add(frozenset({str(node["const"])}))
        for key, value in node.items():
            if key not in {"example", "examples"}:
                walk(value, active_refs)

    for path, path_item in document["paths"].items():
        if not path.startswith("/api/management/v1"):
            continue
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                walk(operation)
    return found


def _discovered_actions(capabilities: dict) -> dict[str, tuple[str, str, str]]:
    result = {}
    for domain in capabilities["domains"]:
        details = domain["actionDetails"]
        assert domain["actions"] == [item["operationId"] for item in details]
        for action in details:
            operation_id = action["operationId"]
            assert operation_id not in result
            result[operation_id] = (
                domain["domain"],
                action["method"],
                action["path"],
            )
    return result


def _product_projection(capabilities: dict) -> dict:
    return {
        "supportedCapabilities": capabilities["supportedCapabilities"],
        "domains": [
            {key: value for key, value in domain.items() if key != "capabilities"}
            for domain in capabilities["domains"]
        ],
    }


def _assert_search_discovery(metadata, capabilities):
    path = PRODUCTION_OPERATIONS.with_name("search-operation-manifest.tsv")
    with path.open(encoding="utf-8", newline="") as handle:
        expected = {row["operationId"]: (row["method"], row["path"]) for row in csv.DictReader(handle, delimiter="\t")}
    domains = {item["domain"]: item for item in capabilities["domains"]}
    search = domains["management-search"]
    assert search["actions"] == sorted(expected)
    assert {item["operationId"]: (item["method"], item["path"]) for item in search["actionDetails"]} == expected
    features = {item["id"]: item["actionCount"] for item in metadata["features"]}
    assert features["management-search"] == 10
    assert "antigravity-media" not in domains and "antigravity-media" not in features
    enums = {item["name"]: set(item["values"]) for item in metadata["enums"]}
    assert enums["SearchBackendCreate.type"] == {"anysearch", "tavily", "exa", "brave", "openai", "xai", "anthropic"}
    for field in ("functionMode", "hostedMode"):
        assert enums["SearchSettingsData." + field] == {"managed", "passthrough", "disabled"}


def test_production_discovery_exactly_describes_routes_catalogs_and_enums(production_app):
    app, _ = production_app
    document = app.openapi()
    expected_operations = _openapi_operations(document)
    manifested = set(PRODUCTION_OPERATIONS.read_text(encoding="utf-8").splitlines())
    assert len(expected_operations) == len(manifested) == 237
    assert set(expected_operations) == manifested

    with TestClient(app) as client:
        credential = _create_management_key_session(client)
        metadata_response = client.get(
            "/api/management/v1/meta", headers=_bearer(credential)
        )
        capabilities_response = client.get(
            "/api/management/v1/capabilities", headers=_bearer(credential)
        )
    assert metadata_response.status_code == 200, metadata_response.text
    assert capabilities_response.status_code == 200, capabilities_response.text
    metadata = metadata_response.json()["data"]
    capabilities = capabilities_response.json()["data"]

    assert _discovered_actions(capabilities) == expected_operations
    _assert_search_discovery(metadata, capabilities)
    assert sum(feature["actionCount"] for feature in metadata["features"]) == 237
    assert {
        feature["id"]: feature["actionCount"] for feature in metadata["features"]
    } == {
        domain["domain"]: len(domain["actions"])
        for domain in capabilities["domains"]
    }
    assert set(capabilities["supportedCapabilities"]) == {
        capability.value for capability in Capability
    }
    assert capabilities["supportedCapabilities"] == metadata["supportedCapabilities"]

    enum_descriptors = {item["name"]: item["values"] for item in metadata["enums"]}
    assert enum_descriptors["OAuthProvider"] == [item.value for item in OAuthProvider]
    assert enum_descriptors["ChannelProtocol"] == [item.value for item in ChannelProtocol]
    assert enum_descriptors["CompatibilityMode"] == [item.value for item in CompatibilityMode]
    assert enum_descriptors["authMethod"] == enum_descriptors["AuthMethod"]
    assert enum_descriptors["operationStatus"] == enum_descriptors["OperationStatus"]
    reflected_sets = {frozenset(values) for values in enum_descriptors.values()}
    for declared_values in _raw_enum_value_sets(document):
        assert any(declared_values <= reflected for reflected in reflected_sets)

    domains = {item["domain"]: item for item in capabilities["domains"]}
    channel_domain = domains[expected_operations["getChannelCatalog"][0]]
    expected_providers = sorted(brand.id for brand in PROVIDER_CATALOG)
    expected_presets = sorted(
        f"{brand.id}/{preset.id}"
        for brand in PROVIDER_CATALOG
        for preset in brand.presets
    )
    assert channel_domain["providers"] == expected_providers
    assert channel_domain["presets"] == expected_presets
    assert channel_domain["protocols"] == sorted(item.value for item in ChannelProtocol)
    assert set(channel_domain["modes"]) == {
        "manual",
        "preset",
        *(item.value for item in CompatibilityMode),
    }
    assert channel_domain["features"] == sorted(
        ["context1m", "fast", "omitTemperature", "omitThinking", "ccMimicry"]
    )
    assert domains[expected_operations["listOAuthAccounts"][0]]["providers"] == sorted(
        item.value for item in OAuthProvider
    )
    assert domains[expected_operations["getIngressDefaultModel"][0]]["protocols"] == sorted(
        item.value for item in Ingress
    )
    assert domains[expected_operations["listStatusIncidents"][0]]["providers"] == [
        "claude",
        "cloudflare",
        "openai",
    ]
    assert all(
        not domain["presets"] or domain["domain"] == channel_domain["domain"]
        for domain in capabilities["domains"]
    )


def test_v1_legacy_discovery_fields_remain_additive_and_consumable(production_app):
    app, _ = production_app
    schemas = app.openapi()["components"]["schemas"]
    domain_schema = schemas["CapabilityDomain"]
    legacy_domain_fields = {
        "domain",
        "capabilities",
        "actions",
        "providers",
        "presets",
        "protocols",
        "modes",
    }
    assert legacy_domain_fields <= set(domain_schema["required"])
    assert domain_schema["properties"]["actions"]["items"] == {"type": "string"}
    assert domain_schema["properties"]["capabilities"]["deprecated"] is True
    assert domain_schema["properties"]["actionDetails"]["items"] == {
        "$ref": "#/components/schemas/ManagementActionDescriptor"
    }

    with TestClient(app) as client:
        credential = _create_management_key_session(client)
        metadata_response = client.get(
            "/api/management/v1/meta", headers=_bearer(credential)
        )
        capabilities_response = client.get(
            "/api/management/v1/capabilities", headers=_bearer(credential)
        )
    assert metadata_response.status_code == capabilities_response.status_code == 200
    metadata = metadata_response.json()["data"]
    capabilities = capabilities_response.json()["data"]

    for field in (
        "apiVersion",
        "applicationVersion",
        "supportedCapabilities",
        "enums",
        "documentationUrl",
    ):
        assert field in metadata
    enum_descriptors = {item["name"]: item["values"] for item in metadata["enums"]}
    assert {"authMethod", "operationStatus"} <= set(enum_descriptors)
    assert {"managementKey", "telegramApproval"} <= set(
        enum_descriptors["authMethod"]
    )
    assert enum_descriptors["operationStatus"] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]

    for domain in capabilities["domains"]:
        assert isinstance(domain["domain"], str)
        for field in (
            "capabilities",
            "actions",
            "providers",
            "presets",
            "protocols",
            "modes",
        ):
            assert isinstance(domain[field], list)
            assert all(isinstance(value, str) for value in domain[field])
        assert domain["capabilities"] == capabilities["principalCapabilities"]
        assert domain["actions"] == [
            action["operationId"] for action in domain["actionDetails"]
        ]


def test_real_server_app_exposes_the_same_complete_discovery(production_app, monkeypatch):
    import server

    _, runtime = production_app
    document = server.app.openapi()
    expected_operations = _openapi_operations(document)
    assert len(expected_operations) == 237
    monkeypatch.setattr(server.app.state, "management_runtime", runtime, raising=False)

    client = TestClient(server.app)
    try:
        credential = _create_management_key_session(client)
        metadata_response = client.get(
            "/api/management/v1/meta", headers=_bearer(credential)
        )
        capabilities_response = client.get(
            "/api/management/v1/capabilities", headers=_bearer(credential)
        )
    finally:
        client.close()

    assert metadata_response.status_code == 200, metadata_response.text
    assert capabilities_response.status_code == 200, capabilities_response.text
    assert sum(
        item["actionCount"] for item in metadata_response.json()["data"]["features"]
    ) == 237
    assert _discovered_actions(capabilities_response.json()["data"]) == expected_operations
    _assert_search_discovery(metadata_response.json()["data"], capabilities_response.json()["data"])


def test_product_discovery_is_stable_across_grants_and_separate_from_principal(production_app):
    app, runtime = production_app
    with TestClient(app) as client:
        management_key_credential = _create_management_key_session(client)

        approval = client.post(
            "/api/management/v1/auth/telegram-approvals",
            json={"clientName": "s8-browser", "deviceSummary": "test"},
        )
        assert approval.status_code == 201, approval.text
        challenge = approval.json()["data"]
        runtime.approvals.decide(
            challenge["approvalId"], telegram_user_id=42, approved=True
        )
        telegram_session = client.post(
            "/api/management/v1/auth/sessions",
            json={
                "grantType": "telegramApproval",
                "approvalId": challenge["approvalId"],
                "exchangeSecret": challenge["exchangeSecret"],
            },
        )
        assert telegram_session.status_code == 201, telegram_session.text
        telegram_credential = telegram_session.json()["data"]["credential"]

        restricted = runtime.sessions.issue_for_principal(
            subject_id="read-only",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(),
            capabilities=(Capability.READ,),
        )
        responses = [
            client.get(
                "/api/management/v1/capabilities", headers=_bearer(credential)
            )
            for credential in (
                management_key_credential,
                telegram_credential,
                restricted.credential,
            )
        ]
    assert all(response.status_code == 200 for response in responses)
    payloads = [response.json()["data"] for response in responses]
    assert _product_projection(payloads[0]) == _product_projection(payloads[1])
    assert _product_projection(payloads[0]) == _product_projection(payloads[2])
    assert set(payloads[0]["principalCapabilities"]) == {
        capability.value for capability in Capability
    }
    assert set(payloads[1]["principalCapabilities"]) == {
        capability.value for capability in Capability
    }
    assert payloads[2]["principalCapabilities"] == [Capability.READ.value]
    for payload in payloads:
        assert all(
            domain["capabilities"] == payload["principalCapabilities"]
            for domain in payload["domains"]
        )


def test_discovery_gets_do_not_start_operations_workers_or_network(production_app, monkeypatch):
    app, runtime = production_app
    controls = runtime.control_owner()
    catalog_calls = 0
    original_get_catalog = controls.channels.get_catalog

    def tracked_get_catalog(context):
        nonlocal catalog_calls
        catalog_calls += 1
        return original_get_catalog(context)

    def forbidden_network(*args, **kwargs):
        del args, kwargs
        raise AssertionError("discovery GET attempted network access")

    monkeypatch.setattr(controls.channels, "get_catalog", tracked_get_catalog)
    before_operations = len(runtime.operations._items)
    before_workers = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("management-operation")
    }

    with TestClient(app) as client:
        credential = _create_management_key_session(client)
        monkeypatch.setattr(socket, "create_connection", forbidden_network)
        meta = client.get("/api/management/v1/meta", headers=_bearer(credential))
        capabilities = client.get(
            "/api/management/v1/capabilities", headers=_bearer(credential)
        )
    assert meta.status_code == capabilities.status_code == 200
    assert catalog_calls == 1
    assert len(runtime.operations._items) == before_operations
    assert {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("management-operation")
    } == before_workers


def test_one_time_response_secrets_are_readable_then_absent_from_follow_up(production_app):
    app, runtime = production_app
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["ManagementKeyGrant"]["properties"]["managementKey"]["writeOnly"] is True
    assert schemas["TelegramApprovalGrant"]["properties"]["exchangeSecret"]["writeOnly"] is True
    assert "writeOnly" not in schemas["SessionCredentialData"]["properties"]["credential"]
    assert "writeOnly" not in schemas["TelegramApprovalCreatedData"]["properties"]["exchangeSecret"]

    with TestClient(app) as client:
        session_response = client.post(
            "/api/management/v1/auth/sessions",
            json={"grantType": "managementKey", "managementKey": FAKE_KEY},
        )
        assert session_response.status_code == 201
        assert session_response.headers["cache-control"] == "no-store"
        credential = session_response.json()["data"]["credential"]
        current = client.get(
            "/api/management/v1/auth/session", headers=_bearer(credential)
        )
        assert current.status_code == 200
        assert credential not in current.text
        assert "credential" not in current.json()["data"]

        approval_response = client.post(
            "/api/management/v1/auth/telegram-approvals",
            json={"clientName": "s8-once", "deviceSummary": "test"},
        )
        assert approval_response.status_code == 201
        assert approval_response.headers["cache-control"] == "no-store"
        challenge = approval_response.json()["data"]
        exchange_secret = challenge["exchangeSecret"]
        status_response = client.get(
            f"/api/management/v1/auth/telegram-approvals/{challenge['approvalId']}",
            headers={"Authorization": f"Approval {exchange_secret}"},
        )
        assert status_response.status_code == 200
        assert status_response.headers["cache-control"] == "no-store"
        assert exchange_secret not in status_response.text
        assert "exchangeSecret" not in status_response.json()["data"]

        runtime.approvals.decide(
            challenge["approvalId"], telegram_user_id=42, approved=True
        )
        exchanged = client.post(
            "/api/management/v1/auth/sessions",
            json={
                "grantType": "telegramApproval",
                "approvalId": challenge["approvalId"],
                "exchangeSecret": exchange_secret,
            },
        )
        assert exchanged.status_code == 201
        assert exchanged.headers["cache-control"] == "no-store"
        assert exchanged.json()["data"]["credential"]
        consumed_status = client.get(
            f"/api/management/v1/auth/telegram-approvals/{challenge['approvalId']}",
            headers={"Authorization": f"Approval {exchange_secret}"},
        )
        assert consumed_status.status_code == 200
        assert consumed_status.json()["data"]["status"] == "consumed"
        assert exchange_secret not in consumed_status.text
