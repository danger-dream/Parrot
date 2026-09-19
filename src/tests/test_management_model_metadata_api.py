from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from src import config, model_metadata, model_pricing
from src.channel import registry
from src.tests.test_management_mapping_support import domain_client, operation_map


METADATA_OPERATIONS = {
    "listModelInventory", "listModelMetadata", "getModelMetadata",
    "putModelMetadataBinding", "deleteModelMetadataBinding",
    "patchModelMetadataOverrides", "deleteModelMetadataOverrides",
    "syncModelMetadata", "searchModelCatalog",
}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/management/v1/models/inventory", None),
        ("get", "/api/management/v1/model-metadata", None),
        ("get", "/api/management/v1/model-metadata/example", None),
        ("put", "/api/management/v1/model-metadata/example/binding", {"scope": "global", "targetModelId": "p/m", "providerId": "p"}),
        ("delete", "/api/management/v1/model-metadata/example/binding", None),
        ("patch", "/api/management/v1/model-metadata/example/overrides", {"scope": "global", "set": {"vision": False}}),
        ("delete", "/api/management/v1/model-metadata/example/overrides?scope=global", None),
        ("post", "/api/management/v1/model-metadata/actions/sync", {"scope": "full"}),
        ("get", "/api/management/v1/model-catalog", None),
    ],
)
def test_metadata_operations_require_session_and_capability(domain_client, method, path, body):
    client, _runtime, _admin, read_only, denied = domain_client
    assert client.request(method, path, json=body).status_code == 401
    forbidden = denied if method == "get" else read_only
    response = client.request(method, path, headers=forbidden, json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CAPABILITY_DENIED"


def test_metadata_openapi_is_typed_and_has_examples(domain_client):
    client, *_ = domain_client
    operations = operation_map(client)
    assert METADATA_OPERATIONS <= set(operations)
    for operation_id in METADATA_OPERATIONS:
        operation = operations[operation_id]
        assert operation["tags"]
        assert operation.get("security") == [{"ManagementSession": []}]
        success = next(value for code, value in operation["responses"].items() if code.startswith("2"))
        if operation_id in {"deleteModelMetadataBinding", "deleteModelMetadataOverrides"}:
            assert success["description"]
        else:
            assert success["content"]["application/json"]["example"]


def test_override_patch_unset_and_set_use_final_inherited_limits(domain_client, monkeypatch):
    client, _runtime, admin, *_ = domain_client
    config.update(lambda cfg: cfg.update({
        "modelBindings": {"defaults": {"atomic-demo": {
            "target": "fixture/atomic-demo", "source": "auto",
            "autoSnapshot": {"catalogRevision": "fixture", "metadata": {
                "contextWindow": 1000, "maxInputTokens": 1000,
            }, "tariff": None},
        }}, "scoped": {}},
        "modelMetadataOverrides": {"defaults": {"atomic-demo": {
            "fields": {"contextWindow": 100},
        }}, "scoped": {}},
    }))
    path = "/api/management/v1/model-metadata/atomic-demo"
    current = client.get(path, headers=admin).json()["data"]
    writes = []
    write_atomic = config._write_atomic

    def write(candidate):
        writes.append(copy.deepcopy(candidate))
        return write_atomic(candidate)

    monkeypatch.setattr(config, "_write_atomic", write)
    response = client.patch(
        path + "/overrides", headers={**admin, "If-Match": current["revision"]},
        json={"scope": "global", "set": {"maxInputTokens": 500}, "unset": ["contextWindow"]},
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["effective"]["contextWindow"] == 1000
    assert updated["effective"]["maxInputTokens"] == 500
    assert updated["commonOverride"] == {"maxInputTokens": 500}
    assert len(writes) == 1
    invalid = client.patch(
        path + "/overrides", headers={**admin, "If-Match": updated["revision"]},
        json={"scope": "global", "set": {"maxInputTokens": 1001}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"
    assert len(writes) == 1
    assert client.get(path, headers=admin).json()["data"] == updated


def test_catalog_filter_sort_page_and_binding_crud(domain_client):
    client, _runtime, admin, *_ = domain_client
    catalog = client.get(
        "/api/management/v1/model-catalog?sort=provider&page=1&pageSize=2",
        headers=admin,
    )
    assert catalog.status_code == 200
    payload = catalog.json()
    assert len(payload["data"]) == 2
    assert payload["meta"]["total"] >= 2
    assert payload["meta"]["hasNext"] is True
    selected = payload["data"][0]
    response = client.put(
        "/api/management/v1/model-metadata/client-visible/binding",
        headers=admin,
        json={
            "scope": "global",
            "targetModelId": selected["key"],
            "providerId": selected["providerId"],
        },
    )
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    assert detail["modelId"] == "client-visible"
    assert detail["target"] == selected["key"]
    assert detail["scope"] == "global"
    assert detail["revision"].startswith("rev_")

    listed = client.get(
        "/api/management/v1/model-metadata?query=client-visible&pageSize=1",
        headers=admin,
    )
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 1
    got = client.get(
        "/api/management/v1/model-metadata/client-visible",
        headers=admin,
    )
    assert got.status_code == 200
    assert got.json()["data"]["raw"] is not None
    stale = client.delete(
        "/api/management/v1/model-metadata/client-visible/binding",
        headers={**admin, "If-Match": "rev_stale"},
    )
    assert stale.status_code == 409
    deleted = client.delete(
        "/api/management/v1/model-metadata/client-visible/binding",
        headers={**admin, "If-Match": detail["revision"]},
    )
    assert deleted.status_code == 204
    assert model_metadata.resolve_binding("client-visible") is None


def test_inventory_filters_and_total(domain_client, monkeypatch):
    client, _runtime, admin, *_ = domain_client
    items = [
        model_metadata.ModelInventoryItem(
            scope_key=f"api:channel-{index}", scope_type="api", scope_label=f"Channel {index}",
            client_visible_model=f"model-{index}", outbound_model=f"upstream-{index}",
        )
        for index in range(3)
    ]
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: items)
    response = client.get(
        "/api/management/v1/models/inventory?query=model&page=2&pageSize=2&sort=modelId",
        headers=admin,
    )
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1
    assert response.json()["meta"]["total"] == 3
    assert response.json()["meta"]["hasNext"] is False


def test_metadata_override_api_enforces_revision_and_exposes_effective_sources(domain_client):
    client, _runtime, admin, *_ = domain_client
    selected = client.get(
        "/api/management/v1/model-catalog?pageSize=1", headers=admin,
    ).json()["data"][0]
    created = client.put(
        "/api/management/v1/model-metadata/override-demo/binding",
        headers=admin,
        json={
            "scope": "global", "targetModelId": selected["key"],
            "providerId": selected["providerId"],
        },
    )
    assert created.status_code == 200
    revision = created.json()["data"]["revision"]
    body = {
        "scope": "global",
        "set": {
            "contextWindow": 300000, "maxInputTokens": 300000,
            "maxOutputTokens": 20000, "compactTriggerTokens": 250000,
            "vision": False, "toolCall": False, "structuredOutput": False,
            "reasoningEfforts": [], "serviceTiers": [],
            "knowledgeCutoff": "2026-09",
            "cost": {
                "input": 0, "output": 2, "cacheRead": 0,
                "cacheWrite": 1, "longContextInput": 3,
                "longContextOutput": 4,
            },
        },
    }
    before = copy.deepcopy(config.get().get("modelMetadataOverrides"))
    missing = client.patch(
        "/api/management/v1/model-metadata/override-demo/overrides",
        headers=admin, json=body,
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    stale = client.patch(
        "/api/management/v1/model-metadata/override-demo/overrides",
        headers={**admin, "If-Match": "rev_stale"}, json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert config.get().get("modelMetadataOverrides") == before

    updated = client.patch(
        "/api/management/v1/model-metadata/override-demo/overrides",
        headers={**admin, "If-Match": revision}, json=body,
    )
    assert updated.status_code == 200, updated.text
    data = updated.json()["data"]
    assert data["effective"]["contextWindow"] == 300000
    assert data["effective"]["vision"] is False
    assert data["effective"]["reasoningEfforts"] == []
    assert data["effective"]["cost"]["input"] == 0
    assert data["valueSource"]["contextWindow"] == "common-override"
    assert data["valueSource"]["cost.input"] == "common-override"
    assert len(data["commonOverride"]) == 16

    listed = client.get(
        "/api/management/v1/model-metadata?query=override-demo", headers=admin,
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["effective"]["contextWindow"] == 300000
    invalid = client.patch(
        "/api/management/v1/model-metadata/override-demo/overrides",
        headers={**admin, "If-Match": data["revision"]},
        json={"scope": "global", "set": {"contextWindow": None}},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"

    restored = client.patch(
        "/api/management/v1/model-metadata/override-demo/overrides",
        headers={**admin, "If-Match": data["revision"]},
        json={
            "scope": "global", "set": {},
            "unset": ["contextWindow", "vision", "cost.input"],
        },
    )
    assert restored.status_code == 200, restored.text
    restored_data = restored.json()["data"]
    assert "contextWindow" not in restored_data["commonOverride"]
    assert "vision" not in restored_data["commonOverride"]
    assert "cost.input" not in restored_data["commonOverride"]
    deleted = client.delete(
        "/api/management/v1/model-metadata/override-demo/overrides?scope=global",
        headers={**admin, "If-Match": restored_data["revision"]},
    )
    assert deleted.status_code == 204
    assert model_metadata.get_override_fields("override-demo") == ({}, {})


def test_selected_sync_operation_is_idempotent_and_keeps_unselected_catalog_metadata_tariff(
    domain_client, monkeypatch,
):
    client, _runtime, admin, *_ = domain_client
    active_snapshot = model_pricing.binding_snapshot("openai/gpt-5.4")
    assert active_snapshot is not None
    other_snapshot = copy.deepcopy(active_snapshot)
    other_snapshot["catalogRevision"] = "other-frozen"
    config.update(lambda cfg: cfg.update({
        "modelBindings": {
            "defaults": {
                "selected-model": {
                    "target": "openai/gpt-5.4", "source": "auto",
                    "autoSnapshot": copy.deepcopy(active_snapshot),
                },
                "other-model": {
                    "target": "openai/gpt-5.4", "source": "auto",
                    "autoSnapshot": copy.deepcopy(other_snapshot),
                },
            },
            "scoped": {},
        },
        "modelMetadataOverrides": {"defaults": {}, "scoped": {}},
    }))
    inventory = [
        model_metadata.ModelInventoryItem("api:A", "api", "A", "selected-model", "selected-model"),
        model_metadata.ModelInventoryItem("api:B", "api", "B", "other-model", "other-model"),
    ]
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: list(inventory))
    candidate = SimpleNamespace(revision="candidate-new")
    candidate_snapshot = copy.deepcopy(active_snapshot)
    candidate_snapshot["catalogRevision"] = "candidate-new"
    candidate_snapshot["catalogSource"] = "candidate"
    candidate_snapshot["metadata"]["contextWindow"] = 333000
    candidate_snapshot["tariff"]["input_per_token"] = 9 / 1_000_000
    monkeypatch.setattr(model_pricing, "fetch_catalog_candidate_sync", lambda: candidate)
    monkeypatch.setattr(
        model_pricing, "candidate_canonical_official_model",
        lambda _candidate, _model: "openai/gpt-5.4",
    )
    monkeypatch.setattr(
        model_pricing, "candidate_binding_snapshot",
        lambda _candidate, _target: copy.deepcopy(candidate_snapshot),
    )

    current = client.get(
        "/api/management/v1/model-metadata/selected-model", headers=admin,
    ).json()["data"]
    revision = current["revision"]
    active_status = copy.deepcopy(model_pricing.catalog_status())
    other_entry = copy.deepcopy(config.get()["modelBindings"]["defaults"]["other-model"])
    other_effective = copy.deepcopy(model_metadata.get_metadata("other-model"))
    other_tariff = model_pricing.build_pricing_binding(
        channel_key="api:B", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="other-model", client_visible_model="other-model",
    ).binding_json
    body = {
        "mode": "one", "targets": [{"modelId": "selected-model"}],
        "refreshCatalog": True,
    }
    missing = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={**admin, "Idempotency-Key": "selected-missing"}, json=body,
    )
    assert missing.status_code == 400
    stale = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "selected-stale", "If-Match": "rev_stale",
        },
        json=body,
    )
    assert stale.status_code == 409
    response = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "selected-once", "If-Match": revision,
        },
        json=body,
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["data"]["id"]
    terminal = None
    for _ in range(100):
        terminal = client.get(
            f"/api/management/v1/operations/{operation_id}", headers=admin,
        ).json()["data"]
        if terminal["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.005)
    assert terminal is not None and terminal["status"] == "succeeded"
    assert terminal["result"]["mode"] == "one"
    assert terminal["result"]["results"][0]["status"] == "updated"
    assert terminal["result"]["results"][0]["catalogRevision"] == "candidate-new"
    assert config.get()["modelBindings"]["defaults"]["selected-model"]["autoSnapshot"]["catalogRevision"] == "candidate-new"
    assert config.get()["modelBindings"]["defaults"]["other-model"] == other_entry
    assert model_metadata.get_metadata("other-model") == other_effective
    assert model_pricing.build_pricing_binding(
        channel_key="api:B", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="other-model", client_visible_model="other-model",
    ).binding_json == other_tariff
    assert model_pricing.catalog_status() == active_status

    replay = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "selected-once", "If-Match": revision,
        },
        json=body,
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["id"] == operation_id
    conflict = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "selected-once", "If-Match": revision,
        },
        json={"mode": "one", "targets": [{"modelId": "other-model"}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "STATE_CONFLICT"


def test_selected_sync_rechecks_revision_before_commit_and_enforces_single_running(
    domain_client, monkeypatch,
):
    client, runtime, admin, *_ = domain_client
    snapshot = model_pricing.binding_snapshot("openai/gpt-5.4")
    assert snapshot is not None
    config.update(lambda cfg: cfg.update({
        "modelBindings": {
            "defaults": {"drift-model": {
                "target": "openai/gpt-5.4", "source": "auto",
                "autoSnapshot": copy.deepcopy(snapshot),
            }},
            "scoped": {},
        },
        "modelMetadataOverrides": {"defaults": {}, "scoped": {}},
    }))
    inventory = [model_metadata.ModelInventoryItem(
        "api:A", "api", "A", "drift-model", "drift-model",
    )]
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: list(inventory))
    candidate = SimpleNamespace(revision="candidate-drift")
    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["catalogRevision"] = "candidate-drift"
    monkeypatch.setattr(model_pricing, "fetch_catalog_candidate_sync", lambda: candidate)
    monkeypatch.setattr(
        model_pricing, "candidate_canonical_official_model",
        lambda _candidate, _model: "openai/gpt-5.4",
    )
    monkeypatch.setattr(
        model_pricing, "candidate_binding_snapshot",
        lambda _candidate, _target: copy.deepcopy(changed_snapshot),
    )
    workers = []
    monkeypatch.setattr(
        runtime.operations, "submit",
        lambda _operation_id, worker: workers.append(worker),
    )
    revision = client.get(
        "/api/management/v1/model-metadata/drift-model", headers=admin,
    ).json()["data"]["revision"]
    body = {"mode": "one", "targets": [{"modelId": "drift-model"}]}
    started = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "drift-first", "If-Match": revision,
        },
        json=body,
    )
    assert started.status_code == 202 and len(workers) == 1
    concurrent = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={
            **admin, "Idempotency-Key": "drift-second", "If-Match": revision,
        },
        json=body,
    )
    assert concurrent.status_code == 429
    assert concurrent.json()["error"]["code"] == "OPERATION_ALREADY_RUNNING"

    binding_before = copy.deepcopy(config.get()["modelBindings"])
    config.update(lambda cfg: cfg["modelMetadataOverrides"]["defaults"].update({
        "drift-model": {"fields": {"vision": False}},
    }))
    workers[0]()
    operation_id = started.json()["data"]["id"]
    terminal = client.get(
        f"/api/management/v1/operations/{operation_id}", headers=admin,
    ).json()["data"]
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "REVISION_CONFLICT"
    assert config.get()["modelBindings"] == binding_before


def test_metadata_sync_returns_202_and_reaches_terminal_without_network(
    domain_client, monkeypatch,
):
    client, _runtime, admin, *_ = domain_client
    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", lambda: False)
    monkeypatch.setattr(model_pricing, "reload_local_catalog", lambda: None)
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", lambda _items=None: {
        "scanned": 0, "created": [], "updated": [], "unchanged": [],
        "unmatched": [], "success": 0,
    })
    response = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={**admin, "Idempotency-Key": "metadata-sync-test"},
        json={"scope": "full"},
    )
    assert response.status_code == 202
    operation_id = response.json()["data"]["id"]
    replay = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={**admin, "Idempotency-Key": "metadata-sync-test"},
        json={"scope": "full"},
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["id"] == operation_id
    conflict = client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers={**admin, "Idempotency-Key": "metadata-sync-test"},
        json={"scope": "channel", "channelId": "api:different"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "STATE_CONFLICT"
    terminal = None
    for _ in range(100):
        polled = client.get(
            f"/api/management/v1/operations/{operation_id}", headers=admin
        )
        assert polled.status_code == 200
        terminal = polled.json()["data"]
        if terminal["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.005)
    assert terminal["status"] == "succeeded"
    assert terminal["result"]["catalog"] == "local"
    assert "token" not in repr(terminal).lower()


def test_metadata_schema_validation_and_missing_ids(domain_client):
    client, _runtime, admin, *_ = domain_client
    invalid = client.put(
        "/api/management/v1/model-metadata/example/binding",
        headers=admin,
        json={
            "scope": "oauth", "targetModelId": "p/m", "providerId": "p",
            "unknown": "x",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["fields"]
    assert client.get(
        "/api/management/v1/model-metadata/missing", headers=admin
    ).status_code == 404
    assert client.post(
        "/api/management/v1/model-metadata/actions/sync",
        headers=admin,
        json={"scope": "channel", "channelId": "api:missing"},
    ).status_code == 404


def test_scoped_metadata_uses_current_inventory_outbound_and_rejects_stale_binding(
    domain_client, monkeypatch,
):
    client, _runtime, admin, *_ = domain_client
    catalog = model_pricing.catalog_models()
    assert len(catalog) >= 2
    global_target, scoped_target = catalog[0]["key"], catalog[1]["key"]
    scope_id = "api:current-channel"
    model_id = "client-alias"
    model_metadata.set_binding(model_id, global_target, source="test-global")
    model_metadata.set_binding(
        model_id,
        scoped_target,
        scope_key=scope_id,
        outbound_model="old-outbound",
        source="test-scoped",
    )
    channel = SimpleNamespace(key=scope_id, type="api")
    inventory = [model_metadata.ModelInventoryItem(
        scope_key=scope_id,
        scope_type="api",
        scope_label="Current channel",
        client_visible_model=model_id,
        outbound_model="new-outbound",
    )]
    monkeypatch.setattr(
        registry, "get_channel", lambda key: channel if key == scope_id else None
    )
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: inventory)

    runtime_binding = model_metadata.resolve_binding(
        model_id, scope_key=scope_id, outbound_model="new-outbound"
    )
    assert runtime_binding is not None
    assert runtime_binding.target == global_target
    assert runtime_binding.scope_key is None

    detail = client.get(
        f"/api/management/v1/model-metadata/{model_id}?scopeId={scope_id}",
        headers=admin,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["target"] == global_target
    assert detail.json()["data"]["scope"] == "global"
    assert detail.json()["data"]["scopeId"] is None
    assert detail.json()["data"]["outboundModel"] is None

    listed = client.get(
        f"/api/management/v1/model-metadata?scope=api&scopeId={scope_id}",
        headers=admin,
    )
    assert listed.status_code == 200, listed.text
    assert [(item["modelId"], item["target"], item["scope"])
            for item in listed.json()["data"]] == [
        (model_id, global_target, "global")
    ]


def test_metadata_selectors_are_strict_and_never_mutate_on_rejection(
    domain_client, monkeypatch,
):
    client, _runtime, admin, *_ = domain_client
    selected = model_pricing.catalog_models()[0]
    model_id = "selector-model"
    model_metadata.set_binding(model_id, selected["key"], source="test")
    scope_channels = {
        "oauth:account": SimpleNamespace(key="oauth:account", type="oauth"),
        "api:channel": SimpleNamespace(key="api:channel", type="api"),
        "other:scope": SimpleNamespace(key="other:scope", type="unknown"),
    }
    monkeypatch.setattr(registry, "get_channel", scope_channels.get)
    before = copy.deepcopy(config.get().get("modelBindings"))

    rejected = [
        client.put(
            f"/api/management/v1/model-metadata/{model_id}/binding",
            headers=admin,
            json={
                "scope": "global",
                "targetModelId": selected["key"],
                "providerId": selected["provider_id"],
                "outboundModel": "silently-discarded-before-fix",
            },
        ),
        client.put(
            f"/api/management/v1/model-metadata/{model_id}/binding",
            headers=admin,
            json={
                "scope": "global",
                "targetModelId": selected["key"],
                "providerId": selected["provider_id"],
                "outboundModel": None,
            },
        ),
        client.put(
            f"/api/management/v1/model-metadata/{model_id}/binding",
            headers=admin,
            json={
                "scope": "global",
                "targetModelId": selected["key"],
                "providerId": selected["provider_id"],
                "accountId": None,
            },
        ),
        client.delete(
            f"/api/management/v1/model-metadata/{model_id}/binding"
            "?scope=global&accountId=oauth:account",
            headers=admin,
        ),
        client.delete(
            f"/api/management/v1/model-metadata/{model_id}/binding"
            "?scope=oauth&accountId=oauth:account&channelId=api:channel",
            headers=admin,
        ),
        client.delete(
            f"/api/management/v1/model-metadata/{model_id}/binding"
            "?scope=api&channelId=api:channel&accountId=oauth:account",
            headers=admin,
        ),
        client.delete(
            f"/api/management/v1/model-metadata/{model_id}/binding"
            "?scope=oauth&accountId=api:channel",
            headers=admin,
        ),
        client.get(
            "/api/management/v1/model-metadata"
            "?scope=global&scopeId=api:channel",
            headers=admin,
        ),
        client.get(
            "/api/management/v1/model-metadata"
            "?scope=oauth&scopeId=api:channel",
            headers=admin,
        ),
        client.get(
            f"/api/management/v1/model-metadata/{model_id}?scopeId=other:scope",
            headers=admin,
        ),
        client.get(
            f"/api/management/v1/model-metadata/{model_id}?scopeId=",
            headers=admin,
        ),
    ]
    for response in rejected:
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert response.json()["error"]["fields"]
    assert config.get().get("modelBindings") == before

    for method, path in (
        ("get", "/api/management/v1/model-metadata?scopeId=api:missing"),
        ("get", f"/api/management/v1/model-metadata/{model_id}?scopeId=api:missing"),
        (
            "delete",
            f"/api/management/v1/model-metadata/{model_id}/binding"
            "?scope=api&channelId=api:missing",
        ),
    ):
        missing = client.request(method, path, headers=admin)
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert config.get().get("modelBindings") == before


@pytest.mark.parametrize(
    "scope,selector_field,scope_id,public_scope_id",
    [
        ("oauth", "accountId", "oauth:valid-account", "valid-account"),
        ("api", "channelId", "api:valid-channel", "api:valid-channel"),
    ],
)
def test_valid_scoped_binding_put_and_delete_remain_supported(
    domain_client, monkeypatch, scope, selector_field, scope_id, public_scope_id,
):
    client, _runtime, admin, *_ = domain_client
    selected = model_pricing.catalog_models()[0]
    channel = SimpleNamespace(key=scope_id, type=scope)
    monkeypatch.setattr(
        registry, "get_channel", lambda key: channel if key == scope_id else None
    )
    model_id = f"valid-{scope}-model"
    created = client.put(
        f"/api/management/v1/model-metadata/{model_id}/binding",
        headers=admin,
        json={
            "scope": scope,
            "targetModelId": selected["key"],
            "providerId": selected["provider_id"],
            selector_field: scope_id,
            "outboundModel": "current-outbound",
        },
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["scope"] == scope
    assert data["scopeId"] == public_scope_id
    assert data["outboundModel"] == "current-outbound"

    deleted = client.delete(
        f"/api/management/v1/model-metadata/{model_id}/binding"
        f"?scope={scope}&{selector_field}={scope_id}",
        headers={**admin, "If-Match": data["revision"]},
    )
    assert deleted.status_code == 204, deleted.text
    assert model_metadata.resolve_binding(
        model_id, scope_key=scope_id, outbound_model="current-outbound"
    ) is None
