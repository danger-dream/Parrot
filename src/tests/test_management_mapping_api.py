from __future__ import annotations

import copy

import pytest

from src import config, model_mapping, model_metadata

from src.tests.test_management_mapping_support import domain_client, operation_map


MAPPING_OPERATIONS = {
    "listModelMappings", "putModelMapping", "updateModelMapping", "deleteModelMapping",
    "getIngressDefaultModel", "putIngressDefaultModel", "deleteIngressDefaultModel",
    "getCompressionModel", "putCompressionModel", "deleteCompressionModel",
}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/management/v1/model-mappings", None),
        ("put", "/api/management/v1/model-mappings/alias", {"realModel": "real"}),
        ("patch", "/api/management/v1/model-mappings/alias", {"alias": "renamed", "realModel": "real"}),
        ("delete", "/api/management/v1/model-mappings/alias", None),
        ("get", "/api/management/v1/ingress-default-models/anthropic", None),
        ("put", "/api/management/v1/ingress-default-models/anthropic", {"modelId": "real"}),
        ("delete", "/api/management/v1/ingress-default-models/anthropic", None),
        ("get", "/api/management/v1/compression-model", None),
        ("put", "/api/management/v1/compression-model", {"modelId": "real"}),
        ("delete", "/api/management/v1/compression-model", None),
    ],
)
def test_mapping_operations_require_session_and_capability(domain_client, method, path, body):
    client, _runtime, _admin, read_only, denied = domain_client
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    forbidden_headers = denied if method == "get" else read_only
    response = client.request(method, path, json=body, headers=forbidden_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CAPABILITY_DENIED"


def test_mapping_openapi_is_typed_and_has_examples(domain_client):
    client, *_ = domain_client
    operations = operation_map(client)
    assert MAPPING_OPERATIONS <= set(operations)
    for operation_id in MAPPING_OPERATIONS:
        operation = operations[operation_id]
        assert operation["tags"]
        assert operation.get("security") == [{"ManagementSession": []}]
        success = next(
            response for code, response in operation["responses"].items()
            if code.startswith("2")
        )
        if operation_id.startswith("delete"):
            assert success["description"]
        else:
            assert success["content"]["application/json"].get("example")


def test_mapping_crud_filter_page_revision_and_audit(domain_client):
    client, runtime, admin, *_ = domain_client
    for alias, real in (("zeta", "real-z"), ("alpha", "real-a"), ("beta", "real-b")):
        response = client.put(
            f"/api/management/v1/model-mappings/{alias}",
            headers=admin,
            json={"realModel": real},
        )
        assert response.status_code == 200
        assert response.json()["data"]["sourceLine"] == "global"
    response = client.get(
        "/api/management/v1/model-mappings?query=a&sort=alias&page=1&pageSize=2",
        headers=admin,
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["alias"] for item in payload["data"]] == ["alpha", "beta"]
    assert payload["meta"] == {
        "requestId": payload["meta"]["requestId"],
        "page": 1, "pageSize": 2, "total": 3, "hasNext": True,
        "revision": payload["data"][0]["revision"],
    }
    assert config.get()["modelMapping"]["global"]["alpha"] == "real-a"

    stale = client.put(
        "/api/management/v1/model-mappings/alpha",
        headers={**admin, "If-Match": "rev_stale"},
        json={"realModel": "new-real"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    revision = payload["data"][0]["revision"]
    deleted = client.delete(
        "/api/management/v1/model-mappings/alpha",
        headers={**admin, "If-Match": revision},
    )
    assert deleted.status_code == 204
    assert "alpha" not in model_mapping.get_ingress_map("global")
    assert any(
        row["actor"] == "administrator" and row["action"] == "model_mapping.delete"
        for row in runtime.state_store.audit_snapshot()
    )


def test_mapping_validation_has_field_locations(domain_client):
    client, _runtime, admin, *_ = domain_client
    response = client.put(
        "/api/management/v1/model-mappings/same",
        headers=admin,
        json={"realModel": "same", "unknown": True},
    )
    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert fields and any("unknown" in field["path"] for field in fields)


def test_retired_ingress_default_rejects_valid_model_without_write(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client
    monkeypatch.setattr(model_mapping, "list_available_models_for", lambda _ingress: ["real-model"])
    before = copy.deepcopy(config.get())
    audit_before = runtime.state_store.audit_snapshot()
    put_default = client.put(
        "/api/management/v1/ingress-default-models/anthropic",
        headers=admin,
        json={"modelId": "real-model"},
    )
    assert put_default.status_code == 422
    assert put_default.json()["error"]["code"] == "UNSUPPORTED_VALUE"
    assert config.get() == before
    assert runtime.state_store.audit_snapshot() == audit_before


def test_compression_model_uses_real_inventory(domain_client, monkeypatch):
    client, _runtime, admin, *_ = domain_client
    inventory = model_metadata.ModelInventoryItem(
        scope_key="api:one", scope_type="api", scope_label="One",
        client_visible_model="real-model", outbound_model="real-model",
    )
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: [inventory])
    put_compression = client.put(
        "/api/management/v1/compression-model",
        headers=admin,
        json={"modelId": "real-model"},
    )
    assert put_compression.status_code == 200
    revision = put_compression.json()["data"]["revision"]
    assert client.delete(
        "/api/management/v1/compression-model",
        headers={**admin, "If-Match": revision},
    ).status_code == 204
