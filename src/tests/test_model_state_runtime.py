from __future__ import annotations

import copy

import pytest
from starlette.requests import Request

import server
from src import config, model_state, scheduler
from src.channel import registry


@pytest.fixture(autouse=True)
def isolated_runtime_state(monkeypatch):
    def mutate(cfg):
        cfg.clear()
        cfg.update(copy.deepcopy(config.DEFAULT_CONFIG))
        cfg["channels"] = [{
            "name": "one", "type": "api", "enabled": True,
            "protocol": "anthropic", "providerId": "claude",
            "baseUrl": "https://upstream.invalid", "apiKey": "test",
            "models": [
                {"real": "upstream-model", "alias": "public-model"},
                {"real": "other-upstream", "alias": "other-model"},
            ],
        }]
        cfg["modelMapping"] = {"global": {"friendly": "public-model"}}
    config.update(mutate)
    monkeypatch.setattr(registry, "_sync_state_db_with_channels", lambda: None)
    registry.rebuild_from_config()
    yield
    registry.rebuild_from_config()


def test_registry_and_scheduler_enforce_global_and_api_source_state_without_deleting_routes():
    assert "public-model" in registry.available_models()
    assert scheduler.schedule(
        {"model": "public-model", "messages": [{"role": "user", "content": "hi"}]},
        "key", "127.0.0.1", ingress_protocol="anthropic",
    ).candidates

    config.update(lambda cfg: model_state.set_api_source_enabled_in_config(
        cfg, "api:one", ["public-model"], False,
    ))
    assert "public-model" not in registry.available_models()
    assert "other-model" in registry.available_models()
    result = scheduler.schedule(
        {"model": "public-model", "messages": [{"role": "user", "content": "hi"}]},
        "key", "127.0.0.1", ingress_protocol="anthropic",
    )
    assert result.candidates == []
    assert any(item["reason"] == "source_model_disabled" for item in result.exclusions)
    assert config.get()["channels"][0]["models"][0] == {
        "real": "upstream-model", "alias": "public-model",
    }

    config.update(lambda cfg: model_state.set_api_source_enabled_in_config(
        cfg, "api:one", ["public-model"], True,
    ))
    config.update(lambda cfg: model_state.set_global_enabled_in_config(
        cfg, ["public-model"], False,
    ))
    assert "public-model" not in registry.available_models()
    result = scheduler.schedule(
        {"model": "public-model", "messages": []}, "key", "127.0.0.1",
        ingress_protocol="anthropic",
    )
    assert result.candidates == []
    assert result.exclusions == [{"channel": None, "reason": "global_model_disabled"}]


@pytest.mark.asyncio
async def test_v1_models_requires_one_enabled_source_for_each_discovered_model(monkeypatch):
    def add_second(cfg):
        cfg["channels"].append({
            "name": "two", "type": "api", "enabled": True,
            "protocol": "anthropic", "providerId": "claude",
            "baseUrl": "https://second.invalid", "apiKey": "test-two",
            "models": [{"real": "second-upstream", "alias": "public-model"}],
        })

    config.update(add_second)
    registry.rebuild_from_config()
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "headers": []})
    monkeypatch.setattr(server.auth, "validate", lambda _headers: ("client", None, None))

    config.update(lambda cfg: model_state.set_api_source_enabled_in_config(
        cfg, "api:one", ["public-model"], False,
    ))
    assert "public-model" in registry.available_models()
    payload = await server.list_models(request)
    assert "public-model" in {item["id"] for item in payload["data"]}

    config.update(lambda cfg: model_state.set_api_source_enabled_in_config(
        cfg, "api:two", ["public-model"], False,
    ))
    # The display switch remains open, but no usable source contributes the row.
    assert model_state.is_discovery_visible("public-model") is True
    assert "public-model" not in registry.available_models()
    payload = await server.list_models(request)
    assert "public-model" not in {item["id"] for item in payload["data"]}

    config.update(lambda cfg: model_state.set_api_source_enabled_in_config(
        cfg, "api:two", ["public-model"], True,
    ))
    config.update(lambda cfg: model_state.set_global_enabled_in_config(
        cfg, ["public-model"], False,
    ))
    assert "public-model" not in registry.available_models()
    payload = await server.list_models(request)
    assert "public-model" not in {item["id"] for item in payload["data"]}


@pytest.mark.asyncio
async def test_v1_models_combines_hidden_enabled_key_allowlist_and_alias_target(monkeypatch):
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "headers": []})
    monkeypatch.setattr(server.auth, "validate", lambda _headers: ("client", None, None))
    payload = await server.list_models(request)
    ids = {item["id"] for item in payload["data"]}
    assert {"public-model", "other-model", "friendly"} <= ids

    config.update(lambda cfg: model_state.set_visible_in_config(
        cfg, ["public-model"], False,
    ))
    payload = await server.list_models(request)
    ids = {item["id"] for item in payload["data"]}
    assert "public-model" not in ids
    assert "friendly" not in ids
    assert "other-model" in ids

    config.update(lambda cfg: model_state.set_visible_in_config(
        cfg, ["public-model"], True,
    ))
    monkeypatch.setattr(
        server.auth, "validate",
        lambda _headers: ("client", ["public-model"], None),
    )
    payload = await server.list_models(request)
    ids = {item["id"] for item in payload["data"]}
    assert ids == {"public-model"}
    monkeypatch.setattr(
        server.auth, "validate",
        lambda _headers: ("client", ["public-model", "friendly"], None),
    )
    payload = await server.list_models(request)
    assert {item["id"] for item in payload["data"]} == {"public-model", "friendly"}
