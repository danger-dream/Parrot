"""Naming-mode changes preserve model switches and publish one committed state."""
from __future__ import annotations

import copy

import pytest

from src import config, scheduler, state_db
from src.channel import registry
from src.model_registry import runtime
from src.model_registry.pipeline import CatalogIndex
from src.openai.channel.registration import register_factories
from src.tests.test_management_mapping_support import domain_client  # noqa: F401
from src.tests.test_model_registry_runtime import _api

BASE = "/api/management/v1/models/registry"


@pytest.fixture
def toggle_env(domain_client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(runtime, "_queue_drift", lambda *_: None)
    monkeypatch.setattr(runtime, "_catalog_index", lambda: CatalogIndex([]))
    state_db.init()
    register_factories()
    registry.install_config_reload_hook()
    yield domain_client
    state_db.close()


def _seed(*, enabled, migrated=True, channels=None, rules=None, **center):
    def mutate(cfg):
        cfg["oauthAccounts"] = []
        cfg["channels"] = channels if channels is not None else [_api("Review", ["glm-5.3"])]
        cfg["modelRegistry"] = {
            "enabled": enabled,
            "rules": rules if rules is not None else [{"type": "merge", "from": "glm-5.3", "into": "review-renamed"}],
        }
        cfg["modelCenter"] = {
            "disabledModels": [], "hiddenModels": [],
            "apiSourceDisabledModels": {}, "sourceEnabledModels": {},
        }
        cfg["apiKeys"] = {}
        cfg["channelSelection"] = "order"
    config.update(mutate)
    if migrated:
        from src.model_registry.report import legacy_names
        built = runtime.compute(config.get(), legacy=legacy_names())
        config.update(lambda cfg: runtime.migrate_config(cfg, built, []))
    config.update(lambda cfg: cfg["modelCenter"].update(center))


def _toggle(env, enabled):
    client, _, admin, _, _ = env
    revision = client.get(BASE, headers=admin).json()["data"]["revision"]
    return client.put(BASE + "/enabled", headers={**admin, "If-Match": revision}, json={"enabled": enabled})


def _routes(model):
    routed = scheduler.schedule(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]},
        "", "127.0.0.1", ingress_protocol="chat",
    )
    return [(channel.key, wire) for channel, wire in routed.candidates]


@pytest.mark.parametrize("field", ["disabledModels", "hiddenModels"])
def test_switch_off_preserves_model_restrictions(toggle_env, field):
    _seed(enabled=True, **{field: ["review-renamed"]})
    assert registry.available_models() == []
    assert _toggle(toggle_env, False).status_code == 200
    if field == "disabledModels":
        assert _routes("glm-5.3") == []
    assert registry.available_models() == []
    assert _toggle(toggle_env, True).status_code == 200
    assert config.get()["modelCenter"][field] == ["review-renamed"]
    assert registry.available_models() == []


@pytest.mark.parametrize("field", ["disabledModels", "hiddenModels"])
def test_reenable_preserves_restrictions_created_while_off(toggle_env, field):
    _seed(enabled=False, **{field: ["glm-5.3"]})
    assert registry.available_models() == []
    assert _toggle(toggle_env, True).status_code == 200
    assert config.get()["modelCenter"][field] == ["review-renamed"]
    if field == "disabledModels":
        assert _routes("glm-5.3") == []
    assert registry.available_models() == []


def test_first_enable_publishes_only_migrated_state(toggle_env):
    _seed(enabled=False, migrated=False, disabledModels=["glm-5.3"])
    observed = []
    def observe_reload(cfg):
        plan = runtime.plan("review-renamed", {}, "chat", None)
        assert plan is not None and plan.error is None
        observed.append((cfg["modelRegistry"]["migrated"], dict(plan.choices)))
    config.on_reload(observe_reload)
    response = _toggle(toggle_env, True)
    assert response.status_code == 200, response.text
    assert observed == [(runtime.MIGRATION_VERSION, {})]
    assert _routes("glm-5.3") == []
    client, _, admin, _, _ = toggle_env
    assert response.json()["data"]["revision"] == client.get(BASE, headers=admin).json()["data"]["revision"]


def test_migration_reload_failure_is_reported_as_saved(toggle_env):
    _seed(enabled=False, migrated=False)
    def fail_after_migration(cfg):
        assert cfg["modelRegistry"]["migrated"] == runtime.MIGRATION_VERSION
        raise RuntimeError("injected migration reload failure")
    config.on_reload(fail_after_migration)
    response = _toggle(toggle_env, True)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["fields"][0]["code"] == "SAVED_RELOAD_UNCONFIRMED"
    assert error["retryable"] is False
    assert config.get()["modelRegistry"]["enabled"] is True


@pytest.mark.parametrize("failure", ["compute", "migrate_config", "_write_atomic"])
def test_failed_preparation_or_save_does_not_enable_registry(toggle_env, monkeypatch, failure):
    _seed(enabled=False, migrated=False, hiddenModels=["glm-5.3"])
    before = copy.deepcopy(config.get())
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(config if failure == "_write_atomic" else runtime, failure, fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        config.update(lambda cfg: runtime.write_enabled(cfg, True))
    assert config.get() == before
    assert runtime.current() is None


def test_round_trip_preserves_source_exceptions_and_unrelated_state(toggle_env):
    _seed(enabled=True, channels=[_api("A", ["MiniMax-M2.7"]), _api("B", ["minimax-m2.7"])], rules=[],
          disabledModels=["minimax-m2.7", "image-only"],
          hiddenModels=["unknown-model"], sourceEnabledModels={"api:B": ["minimax-m2.7"]})
    assert _routes("minimax-m2.7") == [("api:B", "minimax-m2.7")]
    for _ in range(2):
        assert _toggle(toggle_env, False).status_code == 200
        assert _routes("MiniMax-M2.7") == []
        assert _routes("minimax-m2.7") == [("api:B", "minimax-m2.7")]
        assert _toggle(toggle_env, True).status_code == 200
        assert _routes("minimax-m2.7") == [("api:B", "minimax-m2.7")]
    center = config.get()["modelCenter"]
    assert center["disabledModels"] == ["image-only", "minimax-m2.7"]
    assert center["hiddenModels"] == ["unknown-model"]
    assert center["sourceEnabledModels"] == {"api:B": ["minimax-m2.7"]}
    assert center["apiSourceDisabledModels"] == {}


def test_api_source_disable_follows_renamed_model_both_ways(toggle_env):
    _seed(enabled=True, apiSourceDisabledModels={"api:Review": ["review-renamed"]})
    assert _routes("review-renamed") == []
    assert _toggle(toggle_env, False).status_code == 200
    assert _routes("glm-5.3") == []
    assert config.get()["modelCenter"]["apiSourceDisabledModels"] == {"api:Review": ["glm-5.3"]}
    assert _toggle(toggle_env, True).status_code == 200
    assert _routes("review-renamed") == []
    client, _, admin, _, _ = toggle_env
    revision = client.get("/api/management/v1/models", headers=admin).json()["meta"]["revision"]
    response = client.patch("/api/management/v1/models/actions/state", headers={**admin, "If-Match": revision}, json={
        "scope": {"type": "api", "id": "api:Review"},
        "selection": {"mode": "ids", "modelIds": ["review-renamed"]},
        "target": {"enabled": True},
    })
    assert response.status_code == 200, response.text
    assert _routes("review-renamed") == [("api:Review", "glm-5.3")]
    assert config.get()["modelCenter"]["apiSourceDisabledModels"] == {}


def test_partial_legacy_disable_does_not_disable_other_sources(toggle_env):
    _seed(enabled=False, channels=[_api("A", ["MiniMax-M2.7"]), _api("B", ["minimax-m2.7"])], rules=[],
          disabledModels=["MiniMax-M2.7"])
    assert _routes("minimax-m2.7") == [("api:B", "minimax-m2.7")]
    assert _toggle(toggle_env, True).status_code == 200
    assert _routes("minimax-m2.7") == [("api:B", "minimax-m2.7")]
    assert config.get()["modelCenter"]["disabledModels"] == []
    assert config.get()["modelCenter"]["apiSourceDisabledModels"] == {"api:A": ["MiniMax-M2.7"]}


def test_oauth_product_switch_and_source_exception_survive_round_trip(toggle_env):
    from src.tests.test_cursor_oauth_integration import _account
    _seed(enabled=True, channels=[], rules=[])
    first = _account()
    second = dict(first, email="second@local", subject="cursor-user-2", sub="cursor-user-2")
    config.update(lambda cfg: cfg.update(oauthAccounts=[first, second]))
    assert _routes("cursor-composer-2.5")
    config.update(lambda cfg: cfg["modelCenter"].update(
        disabledModels=["cursor-composer-2.5"], sourceEnabledModels={"oauth:cursor:cursor-user-2": ["cursor-composer-2.5"]}))
    assert _routes("cursor-composer-2.5") == [("oauth:cursor:cursor-user-2", "composer-2.5")]
    assert _toggle(toggle_env, False).status_code == 200
    assert _routes("composer-2.5") == [("oauth:cursor:cursor-user-2", "composer-2.5")]
    assert _toggle(toggle_env, True).status_code == 200
    assert _routes("cursor-composer-2.5") == [("oauth:cursor:cursor-user-2", "composer-2.5")]
    assert config.get()["modelCenter"]["disabledModels"] == ["cursor-composer-2.5"]


def test_repeated_enable_is_a_noop_and_preserves_newer_migration(toggle_env):
    _seed(enabled=True)
    config.update(lambda cfg: cfg["modelRegistry"].update(migrated=runtime.MIGRATION_VERSION + 1))
    before = copy.deepcopy(config.get())
    observed = []
    config.on_reload(lambda cfg: observed.append(cfg))
    assert _toggle(toggle_env, True).status_code == 200
    assert observed == []
    assert config.get() == before
