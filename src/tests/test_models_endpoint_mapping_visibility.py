from __future__ import annotations

import asyncio

from starlette.requests import Request

from ._isolation import isolate

isolate()

import server as parrot_server  # noqa: E402
from src import config  # noqa: E402


def _request() -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/models",
        "raw_path": b"/v1/models",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 22122),
    })


def test_models_endpoint_exposes_only_global_mapping_aliases(monkeypatch):
    monkeypatch.setattr(
        parrot_server.auth, "validate",
        lambda headers: ("test-key", None, None),
    )
    monkeypatch.setattr(
        parrot_server.registry, "discovery_models",
        lambda: ["claude-opus-4-8", "gpt-5.6-sol"],
    )
    monkeypatch.setattr(
        parrot_server.model_mapping, "get_global_map",
        lambda: {"gpt": "gpt-5.6-sol"},
    )
    # 若端点回退到旧行为（遍历 ingress map），测试应立即失败。
    monkeypatch.setattr(
        parrot_server.model_mapping, "get_ingress_map",
        lambda ingress: (_ for _ in ()).throw(
            AssertionError(f"legacy ingress mapping leaked into /v1/models: {ingress}")
        ),
    )

    payload = asyncio.run(parrot_server.list_models(_request()))
    ids = [item["id"] for item in payload["data"]]

    assert ids == ["claude-opus-4-8", "gpt", "gpt-5.6-sol"]
    assert "claude" not in ids


def test_models_endpoint_does_not_expose_global_alias_with_missing_target(monkeypatch):
    monkeypatch.setattr(
        parrot_server.auth, "validate",
        lambda headers: ("test-key", None, None),
    )
    monkeypatch.setattr(
        parrot_server.registry, "discovery_models",
        lambda: ["real-present"],
    )
    monkeypatch.setattr(
        parrot_server.model_mapping, "get_global_map",
        lambda: {"usable": "real-present", "stale": "real-missing"},
    )

    payload = asyncio.run(parrot_server.list_models(_request()))
    ids = [item["id"] for item in payload["data"]]

    assert ids == ["real-present", "usable"]
    assert "stale" not in ids


def test_models_endpoint_isolated_from_oauth_catalog_and_metadata_bindings(monkeypatch):
    monkeypatch.setattr(
        parrot_server.auth, "validate", lambda headers: ("test-key", None, None),
    )
    monkeypatch.setattr(
        parrot_server.registry, "discovery_models", lambda: ["routable-only"],
    )
    monkeypatch.setattr(parrot_server.model_mapping, "get_global_map", lambda: {})
    monkeypatch.setattr(
        parrot_server.model_metadata, "resolve_binding",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("/v1/models must not resolve metadata")
        ),
    )
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [{
            "provider": "openai", "email": "metadata@example.com",
            "models": ["catalog-only"],
            "account_model_catalog": {"models": [{
                "id": "catalog-only", "contextWindow": 872_000,
            }]},
        }],
        "modelBindings": {
            "defaults": {"binding-only": {"target": "openai/gpt-5.4"}},
            "scoped": {},
        },
    }))

    payload = asyncio.run(parrot_server.list_models(_request()))

    assert [item["id"] for item in payload["data"]] == ["routable-only"]
    assert set(payload) == {"data", "first_id", "last_id", "has_more"}
    assert set(payload["data"][0]) == {"type", "id", "display_name", "created_at"}
