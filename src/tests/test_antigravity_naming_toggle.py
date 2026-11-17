"""Naming off keeps AG directory IDs exact at scheduling and dispatch."""
from __future__ import annotations

import json

import pytest

from src import affinity, config, scheduler
from src.antigravity import argon, fixed_thinking
from src.channel import registry
from src.model_registry import runtime
from src.model_registry.pipeline import CatalogIndex
from src.openai.channel.registration import register_factories
from src.openai.transform.guard import GuardError
from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_fixed_thinking import _body
from src.tests.test_model_registry_runtime import _api

FAMILIES = {**fixed_thinking.FAMILIES, argon.BASE: argon.TIERS}
pytestmark = [pytest.mark.parametrize("base", FAMILIES),
              pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])]


@pytest.fixture
def env(m, monkeypatch):
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(registry, "_channels", {})
    monkeypatch.setattr(runtime, "_queue_drift", lambda *_: None)
    monkeypatch.setattr(runtime, "_catalog_index", lambda: CatalogIndex([]))
    _setup(m)
    config.update(lambda cfg: cfg.update(
        modelRegistry={"enabled": False}, modelCenter={}, modelMapping={},
        apiKeys={}, channelSelection="order",
    ))
    register_factories()
    registry.install_config_reload_hook()
    async def token(*args, **kwargs):
        return "synthetic-token"
    monkeypatch.setattr(m["oauth_manager"], "ensure_channel_token", token)
    yield m
    m["state_db"].close()


def _seed(env, base, *, native=False, api=True):
    models = [f"{base}-{tier}" for tier in FAMILIES[base]]
    if native:
        models.append(base)
    records = [{"id": f"{base}-{tier}", "name": f"{base} ({tier.title()})"}
               for tier in FAMILIES[base]]
    if native:
        records.append({"id": base, "name": base})
    key = _add_ag_account(env["oauth_manager"], models=models,
                          account_model_catalog={"models": records})
    config.update(lambda cfg: cfg.update(channels=[_api("Native", [base])] if api else []))
    return registry.get_channel("oauth:" + key)


def _schedule(body, ingress):
    return scheduler.schedule(body, "", "127.0.0.1", ingress_protocol=ingress, fp_query="naming-test")


@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_off_does_not_mix_variants_into_native_api_route(env, monkeypatch, base, ingress, effort):
    ch = _seed(env, base)
    monkeypatch.setattr(affinity, "get", lambda _: {"channel_key": ch.key, "model": base + "-high"})
    body = _body(base, ingress, effort)
    result = _schedule(body, ingress)
    assert runtime.current() is None
    assert ch.resolve_request_model(base, body, ingress) is None
    assert [(channel.key, wire) for channel, wire in result.candidates] == [("api:Native", base)]
    assert result.guard_errors == []
    assert result.bound_channel_key is None
    assert result.call_choices == {}


def test_off_unknown_bare_name_has_no_candidate_or_spurious_reasoning_error(env, base, ingress):
    _seed(env, base, api=False)
    assert base not in registry.available_models()
    result = _schedule(_body(base, ingress), ingress)
    assert result.candidates == []
    assert result.guard_errors == []


@pytest.mark.asyncio
async def test_off_exact_tiers_dispatch_and_keep_conflict_checks(env, base, ingress):
    ch = _seed(env, base, api=False)
    assert registry.available_models() == sorted(f"{base}-{tier}" for tier in FAMILIES[base])
    for tier in FAMILIES[base]:
        model = f"{base}-{tier}"
        body = _body(model, ingress)
        result = _schedule(body, ingress)
        assert result.candidates == [(ch, model)]
        request = await ch.build_upstream_request(body, model, ingress_protocol=ingress)
        assert json.loads(request.body)["model"] == model
        conflict = _body(model, ingress, "high" if tier == "low" else "low")
        assert _schedule(conflict, ingress).candidates == []
        with pytest.raises(GuardError, match="conflicts"):
            await ch.build_upstream_request(conflict, model, ingress_protocol=ingress)


@pytest.mark.asyncio
async def test_off_native_bare_id_is_not_retargeted_even_with_effort(env, base, ingress):
    ch = _seed(env, base, native=True, api=False)
    body = _body(base, ingress, "high")
    result = _schedule(body, ingress)
    assert result.candidates == [(ch, base)]
    request = await ch.build_upstream_request(body, base, ingress_protocol=ingress)
    assert json.loads(request.body)["model"] == base


@pytest.mark.asyncio
async def test_toggle_preserves_unified_selection_and_blocks_stale_alias_dispatch(env, base, ingress):
    _seed(env, base, api=False)
    config.update(lambda cfg: runtime.write_enabled(cfg, True))
    assert runtime.current() is not None
    assert registry.available_models() == [base]
    for effort in (None, *FAMILIES[base]):
        body = _body(base, ingress, effort)
        result = _schedule(body, ingress)
        ch, model = result.candidates[0]
        assert len(result.candidates) == 1
        assert model == f"{base}-{effort or 'high'}"
        choice = result.call_choices[(ch.key, model)]
        attempt = runtime.apply(body, choice, ingress)
        request = await ch.build_upstream_request(attempt, model, ingress_protocol=ingress)
        assert json.loads(request.body)["model"] == model
    config.update(lambda cfg: runtime.write_enabled(cfg, False))
    assert runtime.current() is None
    assert _schedule(body, ingress).candidates == []
    with pytest.raises(GuardError, match="enabled exact model"):
        await ch.build_upstream_request(attempt, model, ingress_protocol=ingress)
    assert registry.available_models() == sorted(f"{base}-{tier}" for tier in FAMILIES[base])
    config.update(lambda cfg: runtime.write_enabled(cfg, True))
    assert _schedule(body, ingress).candidates[0][1] == model


@pytest.mark.asyncio
async def test_off_explicit_model_mapping_to_exact_variant_still_works(env, base, ingress):
    from src import model_mapping
    ch = _seed(env, base, api=False)
    model = base + "-high"
    config.update(lambda cfg: cfg.update(modelMapping={"global": {base: model}}))
    body = _body(base, ingress, "high")
    model_mapping.apply_mapping(body, {"chat": "openai-chat", "responses": "openai-responses"}.get(ingress, ingress))
    assert body["model"] == model
    assert _schedule(body, ingress).candidates == [(registry.get_channel(ch.key), model)]
    request = await ch.build_upstream_request(body, model, ingress_protocol=ingress)
    assert json.loads(request.body)["model"] == model
