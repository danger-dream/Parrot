"""Source enable exceptions exercised through routing, dispatch and discovery."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from src import auth, config, failover, model_mapping, model_state, scheduler
from src.channel import registry
from src.channel.api_channel import ApiChannel
from src.openai.transform.guard import GuardError
from src.protocols.runtime import AttemptResult


@pytest.fixture
def runtime(monkeypatch):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg.update(channels=[], oauthAccounts=[], channelSelection="order")
    cfg["modelMapping"] = {"global": {"friendly": "shared"}}
    channels = []
    for name in ("one", "two"):
        entry = {"name": name, "protocol": "anthropic", "enabled": True,
                 "cc_mimicry": False, "baseUrl": "https://fixture.invalid", "apiKey": "test",
                 "models": [{"alias": "shared", "real": "upstream"}]}
        cfg["channels"].append(entry)
        channels.append(ApiChannel(entry))
    monkeypatch.setattr(config, "get", lambda: cfg)
    monkeypatch.setattr(registry, "_channels", {ch.key: ch for ch in channels})
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *a: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *a: False)
    model_state.set_global_enabled_in_config(cfg, ["shared"], False)
    return cfg, channels


def _grant(cfg, source, *models):
    model_state.set_source_enable_override_in_config(cfg, source, models or ["shared"], True)


def _candidates(body=None, key=None):
    return scheduler._filter_candidates("shared", body=body or {"model": "shared"}, api_key_name=key)[0]


def test_select_discover_only_explicit_source_and_new_source_inherits_default(runtime):
    cfg, (one, two) = runtime
    assert not _candidates() and "shared" not in registry.available_models()
    _grant(cfg, one.key)
    assert _candidates() == [(one, "upstream")]
    assert registry.available_models() == ["shared"]
    three = ApiChannel({**cfg["channels"][0], "name": "three"})
    registry._channels[three.key] = three
    assert _candidates() == [(one, "upstream")]
    cfg["apiKeys"] = {"other": {"allowedChannels": [two.key]}, "selected": {"allowedChannels": [one.key]}}
    assert not _candidates(key="other")
    assert registry.available_models(api_key_name="other") == []
    assert registry.available_models(api_key_name="selected") == ["shared"]
    cfg["modelCenter"]["hiddenModels"] = ["shared"]
    assert registry.available_models() == []
    assert _candidates() == [(one, "upstream")]
    model_state.set_global_enabled_in_config(cfg, ["shared"], False)
    assert not _candidates()


@pytest.mark.parametrize("block", ["channel", "reason", "source", "cooldown"])
def test_exception_does_not_override_other_candidate_gates(runtime, monkeypatch, block):
    cfg, (one, _two) = runtime
    _grant(cfg, one.key)
    if block == "channel": one.enabled = False
    elif block == "reason": one.disabled_reason = "account unavailable"
    elif block == "source": cfg["modelCenter"]["apiSourceDisabledModels"] = {one.key: ["shared"]}
    else: monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *a: True)
    assert not _candidates()


def test_alias_and_target_exceptions_cannot_be_combined_across_sources(runtime):
    cfg, (one, two) = runtime
    model_state.set_global_enabled_in_config(cfg, ["friendly", "upstream"], False)
    _grant(cfg, one.key, "friendly")
    _grant(cfg, two.key, "shared", "upstream")
    body = {"model": "friendly", "_parrot_model_alias": "spoofed"}
    model_mapping.apply_mapping(body, "anthropic")
    assert body["_parrot_model_alias"] == "friendly"
    assert not _candidates(body)
    assert not registry.available_model_aliases({"friendly": "shared"})
    _grant(cfg, two.key, "friendly")
    assert _candidates(body) == [(two, "upstream")]
    assert registry.available_model_aliases({"friendly": "shared"}) == {"friendly"}
    cfg["modelCenter"]["hiddenModels"] = ["friendly"]
    assert not registry.available_model_aliases({"friendly": "shared"})
    assert _candidates(body) == [(two, "upstream")]


@pytest.mark.asyncio
async def test_actual_models_endpoint_keeps_source_exception_and_hidden(runtime, monkeypatch):
    import server
    cfg, (one, two) = runtime
    _grant(cfg, one.key)
    monkeypatch.setattr(auth, "validate", lambda _: ("client", None, None))
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "headers": []})
    async def ids():
        return {row["id"] for row in (await server.list_models(request))["data"]}
    assert {"shared", "friendly"} <= await ids()
    cfg["apiKeys"] = {"client": {"allowedChannels": [two.key]}}
    assert not await ids()
    cfg["apiKeys"] = {}
    cfg["modelCenter"]["hiddenModels"] = ["shared"]
    assert not await ids()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_real_failover_rechecks_preselected_candidates(runtime, monkeypatch, revoke):
    cfg, (one, two) = runtime
    _grant(cfg, one.key)
    sent = []
    async def acquire(*a, **kw): return True
    monkeypatch.setattr(failover.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(failover.concurrency, "release", lambda *a: None)
    monkeypatch.setattr(failover, "_pick_non_direct_proxy_name", lambda *a: None)
    monkeypatch.setattr(failover, "_should_use_responses_upstream_ws", lambda *a, **kw: False)
    monkeypatch.setattr(failover.local_web_tools, "request_declares_supported_tools", lambda *a: False)
    monkeypatch.setattr(failover.local_web_tools, "openai_responses_local_web_active", lambda *a: False)
    monkeypatch.setattr(failover.log_db, "record_retry_attempt", lambda *a, **kw: int(a[1]))
    for name in ("update_retry_attempt", "update_pending", "update_pending_fast_mode_from_upstream", "finish_error"):
        monkeypatch.setattr(failover.log_db, name, lambda *a, **kw: None)
    monkeypatch.setattr(failover.finalize_policy, "apply_error_health_effects", lambda *a, **kw: None)
    async def transport(**kw):
        sent.append(kw["channel"].key)
        return SimpleNamespace(error=AttemptResult(outcome="transport_error", error_detail="fixture refusal"))
    monkeypatch.setattr(failover, "open_response_with_proxy_chain", transport)
    # Simulate a stale/preselected list containing both sources: failover itself,
    # not only the scheduler, must refuse the ungranted source.
    route = scheduler.ScheduleResult([(one, "upstream"), (two, "upstream")], None, False)
    if revoke: model_state.set_global_enabled_in_config(cfg, ["shared"], False)
    await failover.run_failover(route, {"model": "shared", "max_tokens": 10, "messages": []},
        "source-exception", None, "127.0.0.1", False, time.time(),
        ingress_protocol="anthropic", start_monotonic=time.monotonic())
    assert sent == ([] if revoke else [one.key])


def test_mapping_identity_does_not_leak_into_independent_compression_request(runtime):
    from src import compact_rescue
    cfg, (one, _two) = runtime
    body = {"model": "friendly", "_parrot_original_model": "friendly"}
    model_mapping.apply_mapping(body, "anthropic")
    model_state.set_global_enabled_in_config(cfg, ["friendly"], False)
    summary, _ = compact_rescue.sanitized_compact_base(body)
    summary.update(model="compress", _client_visible_model="compress")
    assert model_mapping.request_model_names(summary, "compress-upstream") == {"compress", "compress-upstream"}
    assert failover._candidate_budget_body(one, "compress-upstream", summary) == summary


def test_pre_wire_guard_rechecks_alias_target_and_outbound_after_selection(runtime):
    cfg, (one, _two) = runtime
    _grant(cfg, one.key)
    body = {"model": "friendly"}
    model_mapping.apply_mapping(body, "anthropic")
    assert failover._candidate_budget_body(one, "upstream", body) == body
    for model in ("friendly", "shared", "upstream"):
        model_state.set_global_enabled_in_config(cfg, [model], False)
        with pytest.raises(GuardError):
            failover._validate_wire_payload_budget(one, "upstream", body, json.dumps({"model": "upstream"}), None)
        _grant(cfg, one.key, model)
        failover._validate_wire_payload_budget(one, "upstream", body, {"model": "upstream"}, None)
