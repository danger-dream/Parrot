from __future__ import annotations

from src import config
from src.tests.test_management_mapping_support import domain_client, operation_map


MODEL_OPERATIONS = {"listModels", "getModel", "setModelState"}


def _catalog() -> None:
    config.update(lambda cfg: cfg.__setitem__("channels", [{
        "name": "api-one", "type": "api", "enabled": True,
        "protocol": "openai-chat", "providerId": "openai",
        "baseUrl": "https://upstream.invalid", "apiKey": "test",
        "models": [{"real": "upstream-one", "alias": "model-one"}],
    }]))


def test_models_api_permissions_strict_query_typed_openapi_and_details(domain_client):
    client, _runtime, admin, read_only, denied = domain_client
    _catalog()
    assert client.get("/api/management/v1/models").status_code == 401
    forbidden = client.get("/api/management/v1/models", headers=denied)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "CAPABILITY_DENIED"

    unknown_query = client.get("/api/management/v1/models?provider=openai", headers=admin)
    assert unknown_query.status_code == 422
    assert unknown_query.json()["error"]["code"] == "VALIDATION_FAILED"

    response = client.get(
        "/api/management/v1/models?type=chat&text=upstream-one&sourceType=api&sourceId=api%3Aapi-one&status=enabled",
        headers=admin,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["modelId"] for item in payload["data"]] == ["model-one"]
    assert payload["data"][0]["sources"][0]["effectiveMetadata"] == {}
    key = payload["data"][0]["resourceKey"]
    detail = client.get(f"/api/management/v1/models/{key}", headers=read_only)
    assert detail.status_code == 200
    assert detail.json()["data"]["resourceKey"] == key
    missing = client.get("/api/management/v1/models/mdl_missing", headers=admin)
    assert missing.status_code == 404
    unknown_source = client.get(
        "/api/management/v1/models?sourceType=api&sourceId=api%3Amissing", headers=admin,
    )
    assert unknown_source.status_code == 404

    operations = operation_map(client)
    assert MODEL_OPERATIONS <= set(operations)
    for operation_id in MODEL_OPERATIONS:
        operation = operations[operation_id]
        assert operation["tags"] == ["management-models"]
        assert operation.get("security") == [{"ManagementSession": []}]
        assert any(code.startswith("2") for code in operation["responses"])


def test_models_state_http_error_mapping_and_explicit_targets(domain_client):
    client, _runtime, admin, read_only, _denied = domain_client
    _catalog()
    listed = client.get("/api/management/v1/models?type=chat", headers=admin).json()
    revision = listed["meta"]["revision"]
    body = {
        "scope": {"type": "global"},
        "selection": {"mode": "ids", "modelIds": ["model-one"]},
        "target": {"visible": False},
    }
    forbidden = client.patch(
        "/api/management/v1/models/actions/state", headers=read_only, json=body,
    )
    assert forbidden.status_code == 403
    no_revision = client.patch(
        "/api/management/v1/models/actions/state", headers=admin, json=body,
    )
    assert no_revision.status_code == 400
    assert no_revision.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    stale = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": "rev_stale"}, json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    invalid = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": revision},
        json={**body, "target": {"enabled": False, "visible": False}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"
    unknown = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": revision},
        json={**body, "selection": {"mode": "ids", "modelIds": ["missing"]}},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    unsupported = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": revision},
        json={**body, "scope": {"type": "api", "id": "api:api-one"}},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_VALUE"

    updated = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": revision}, json=body,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["items"] == [{"modelId": "model-one", "status": "updated"}]
    hidden = client.get("/api/management/v1/models?status=hidden", headers=admin)
    assert hidden.status_code == 200
    assert any(item["modelId"] == "model-one" for item in hidden.json()["data"])
