"""Shared authorized test harness for the P5 domain routers.

The filename remains inside the mapping-owned test prefix; other P5 tests import
its fixture explicitly rather than modifying global conftest/P0 fixtures.
"""

from __future__ import annotations

import ast
import copy
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config
from src.management_api import ManagementRuntime, create_management_router, install_management_error_handlers
from src.management_api.routers.load_balancing import router as load_balancing_router
from src.management_api.routers.mapping import router as mapping_router
from src.management_api.routers.model_metadata import router as model_metadata_router
from src.management_api.routers.models import router as models_router
from src.management_api.routers.proxy import router as proxy_router
from src.management_auth import (
    ApprovalService,
    AuthMethod,
    Capability,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import OperationRegistry, OperationStore, StoreAuditSink


FAKE_KEY = "pmk_" + "P" * 64
OWNED_OPERATION_IDS = {
    "listModelMappings", "putModelMapping", "updateModelMapping", "deleteModelMapping",
    "getIngressDefaultModel", "putIngressDefaultModel", "deleteIngressDefaultModel",
    "listModelInventory", "listModelMetadata", "getModelMetadata",
    "putModelMetadataBinding", "deleteModelMetadataBinding", "syncModelMetadata",
    "patchModelMetadataOverrides", "deleteModelMetadataOverrides",
    "searchModelCatalog", "getCompressionModel", "putCompressionModel",
    "deleteCompressionModel", "getLoadBalancing", "updateLoadBalancingMode",
    "getChannelOrder", "replaceChannelOrder", "getModelChannelOrder",
    "replaceModelChannelOrder", "deleteModelChannelOrder",
    "bulkReplaceModelChannelOrders", "clearAllAffinity", "clearFamilyAffinity",
    "listProxies", "createProxies", "getProxy", "updateProxy", "deleteProxy",
    "testProxy", "listProxyGroups", "createProxyGroups", "getProxyGroup",
    "updateProxyGroup", "deleteProxyGroup", "testProxyGroup", "getProxyRouting",
    "updateProxyRouting",
}
OWNED_TAGS = {
    "management-model-mapping",
    "management-model-metadata",
    "management-load-balancing",
    "management-proxy",
}


class FakeNotifier:
    def send(self, *_args, **_kwargs):
        return True


@pytest.fixture
def domain_client(tmp_path):
    config.update(
        lambda current: (current.clear(), current.update(copy.deepcopy(config.DEFAULT_CONFIG)))
    )
    store = ManagementStateStore(str(tmp_path / "management-p5.db"), clock=time.time)
    sessions = SessionService(
        store,
        management_key=FAKE_KEY,
        policy=SessionPolicy(
            idle_timeout_seconds=3_600,
            absolute_timeout_seconds=7_200,
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
        application_version="0.p5-test",
        documentation_url="https://docs.example.test/management-v1",
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    app.include_router(create_management_router((
        mapping_router,
        model_metadata_router,
        models_router,
        load_balancing_router,
        proxy_router,
    )))
    install_management_error_handlers(app)
    client = TestClient(app)
    response = client.post(
        "/api/management/v1/auth/sessions",
        json={"grantType": "managementKey", "managementKey": FAKE_KEY},
    )
    assert response.status_code == 201
    credential = response.json()["data"]["credential"]
    limited = sessions.issue_for_principal(
        subject_id="read-only",
        auth_method=AuthMethod.MANAGEMENT_KEY,
        roles=(),
        capabilities=(Capability.READ,),
    )
    denied = sessions.issue_for_principal(
        subject_id="no-capabilities",
        auth_method=AuthMethod.MANAGEMENT_KEY,
        roles=(),
        capabilities=(),
    )
    try:
        yield client, runtime, {
            "Authorization": f"Bearer {credential}",
        }, {
            "Authorization": f"Bearer {limited.credential}",
        }, {
            "Authorization": f"Bearer {denied.credential}",
        }
    finally:
        runtime.close()


def operation_map(client: TestClient) -> dict[str, dict]:
    document = client.get("/openapi.json").json()
    return {
        operation["operationId"]: operation
        for path, path_item in document["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in path_item.items()
        if method in {"get", "put", "post", "patch", "delete"}
    }


def test_openapi_matches_owned_operation_manifest(domain_client):
    client, *_ = domain_client
    owned = {
        operation_id
        for operation_id, operation in operation_map(client).items()
        if set(operation.get("tags") or ()) & OWNED_TAGS
    }
    assert owned == OWNED_OPERATION_IDS
    assert len(owned) == 43


def test_p5_router_operations_each_call_control_once_with_authorized_context():
    root = Path(__file__).resolve().parents[2]
    operations = []
    for relative in (
        "src/management_api/routers/mapping.py",
        "src/management_api/routers/model_metadata.py",
        "src/management_api/routers/load_balancing.py",
        "src/management_api/routers/proxy.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_operation = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "router"
                for decorator in node.decorator_list
            )
            if not is_operation:
                continue
            calls = [call for call in ast.walk(node) if isinstance(call, ast.Call)]
            control_calls = [
                call for call in calls
                if isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "control"
            ]
            strict_query_calls = [
                call for call in calls
                if isinstance(call.func, ast.Name)
                and call.func.id == "reject_unknown_query_parameters"
            ]
            assert len(control_calls) == 1, (relative, node.name, control_calls)
            assert len(strict_query_calls) == 1, (relative, node.name, strict_query_calls)
            assert strict_query_calls[0].lineno < control_calls[0].lineno, (
                relative, node.name, "strict query validation must precede control"
            )
            assert control_calls[0].args
            first_argument = control_calls[0].args[0]
            assert isinstance(first_argument, ast.Name)
            assert first_argument.id == "context", (relative, node.name)
            operations.append((relative, node.name))
    assert len(operations) == 43


STRICT_QUERY_CASES = [
    ("get", "/api/management/v1/model-mappings", None),
    ("put", "/api/management/v1/model-mappings/example", {"realModel": "model"}),
    ("delete", "/api/management/v1/model-mappings/example", None),
    ("get", "/api/management/v1/ingress-default-models/anthropic", None),
    ("put", "/api/management/v1/ingress-default-models/anthropic", {"modelId": "model"}),
    ("delete", "/api/management/v1/ingress-default-models/anthropic", None),
    ("get", "/api/management/v1/compression-model", None),
    ("put", "/api/management/v1/compression-model", {"modelId": "model"}),
    ("delete", "/api/management/v1/compression-model", None),
    ("get", "/api/management/v1/models/inventory", None),
    ("get", "/api/management/v1/model-metadata", None),
    ("get", "/api/management/v1/model-metadata/example", None),
    (
        "put", "/api/management/v1/model-metadata/example/binding",
        {"scope": "global", "targetModelId": "provider/model", "providerId": "provider"},
    ),
    ("delete", "/api/management/v1/model-metadata/example/binding", None),
    ("post", "/api/management/v1/model-metadata/actions/sync", {"scope": "full"}),
    ("get", "/api/management/v1/model-catalog", None),
    ("get", "/api/management/v1/load-balancing", None),
    ("patch", "/api/management/v1/load-balancing", {"mode": "smart"}),
    ("get", "/api/management/v1/load-balancing/channel-order", None),
    ("put", "/api/management/v1/load-balancing/channel-order", {"order": []}),
    ("get", "/api/management/v1/load-balancing/model-orders/example", None),
    ("put", "/api/management/v1/load-balancing/model-orders/example", {"order": []}),
    ("delete", "/api/management/v1/load-balancing/model-orders/example", None),
    (
        "put", "/api/management/v1/load-balancing/model-orders",
        {"modelIds": ["example"], "order": []},
    ),
    ("post", "/api/management/v1/affinity/actions/clear", None),
    ("post", "/api/management/v1/affinity/families/anthropic/actions/clear", None),
    ("get", "/api/management/v1/proxies", None),
    (
        "post", "/api/management/v1/proxies",
        {"name": "edge", "url": "socks5://127.0.0.1:1080"},
    ),
    ("get", "/api/management/v1/proxies/edge", None),
    ("patch", "/api/management/v1/proxies/edge", {"name": "renamed"}),
    ("delete", "/api/management/v1/proxies/edge", None),
    ("post", "/api/management/v1/proxies/edge/actions/test", None),
    ("get", "/api/management/v1/proxy-groups", None),
    (
        "post", "/api/management/v1/proxy-groups",
        {"name": "group", "members": ["direct"]},
    ),
    ("get", "/api/management/v1/proxy-groups/group", None),
    ("patch", "/api/management/v1/proxy-groups/group", {"members": []}),
    ("delete", "/api/management/v1/proxy-groups/group", None),
    ("post", "/api/management/v1/proxy-groups/group/actions/test", None),
    ("get", "/api/management/v1/proxy-routing", None),
    ("patch", "/api/management/v1/proxy-routing", {"directFallback": True}),
]


@pytest.mark.parametrize("method,path,body", STRICT_QUERY_CASES)
def test_every_p5_route_rejects_unknown_query_without_side_effects(
    domain_client, method, path, body,
):
    client, runtime, admin, *_ = domain_client
    assert len(STRICT_QUERY_CASES) == 40
    before_config = copy.deepcopy(config.get())
    before_audit = runtime.state_store.audit_snapshot()
    before_operations = copy.deepcopy(runtime.operations._items)
    kwargs = {"json": body} if body is not None else {}

    response = client.request(
        method, f"{path}?undeclared=1", headers=admin, **kwargs
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["fields"] == [{
        "path": "undeclared",
        "code": "UNKNOWN_QUERY_PARAMETER",
        "message": "query parameter 'undeclared' is not supported",
    }]
    assert config.get() == before_config
    assert runtime.state_store.audit_snapshot() == before_audit
    assert runtime.operations._items == before_operations
