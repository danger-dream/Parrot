"""Adjacent positive, zero-write, CAS and real-consumer coverage for CORE fixes."""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src import config, failover, log_db, model_metadata, model_pricing, scheduler, token_counter
from src.channel import registry
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.mapping import MappingControl
from src.openai.transform.guard import GuardError
from src.tests.test_core_audit_fixes import channel, clean_review, domain_client


@pytest.mark.parametrize("maximum", [False, True])
@pytest.mark.parametrize("scope", [None, "oauth:cursor:cursor-user-1"])
@pytest.mark.parametrize("cap,expected", [(200_000, 200_000), (500_000, 300_000)])
def test_cursor_normal_and_max_share_operator_ceiling(maximum, scope, cap, expected):
    from src.tests.test_cursor_oauth_integration import _account
    config.update(lambda c: c.update(oauthAccounts=[_account()]))
    model_metadata.patch_override_fields(
        "claude-fable-5", scope_key=scope,
        outbound_model="claude-fable-5" if scope else None,
        set_fields={"contextWindow": cap, "maxOutputTokens": 10_000,
                    "toolCall": False, "reasoningEfforts": []},
    )
    effective_scope = "oauth:cursor:cursor-user-1"
    meta = model_metadata.get_metadata("claude-fable-5", scope_key=effective_scope)
    assert meta["toolCall"] is False and meta["reasoningEfforts"] == []
    assert meta["maxOutputTokens"] == 10_000
    budget = model_metadata.effective_request_budget(
        "claude-fable-5", scope_key=effective_scope, use_max_context=maximum,
        requested_output_tokens=10_000,
    )
    window = cap if maximum else expected
    assert budget.context_window == window
    assert budget.effective_input_budget == window
    # Use the real final-wire consumer (including Max Context selection), not
    # just the DTO returned to a management client.
    ch = SimpleNamespace(key=effective_scope, uses_max_context=lambda *_: maximum)
    with pytest.raises(GuardError) as error:
        failover._validate_wire_payload_budget(
            ch, "claude-fable-5", {"model": "claude-fable-5"},
            {"model": "claude-fable-5", "input": "hi", "max_output_tokens": 10_001}, None,
        )
    assert error.value.scope == "candidate"


def test_cursor_max_cannot_expand_explicit_input_or_trigger():
    from src.tests.test_cursor_oauth_integration import _account
    config.update(lambda c: c.update(oauthAccounts=[_account()]))
    scope = "oauth:cursor:cursor-user-1"
    model_metadata.patch_override_fields(
        "claude-fable-5", scope_key=scope, outbound_model="claude-fable-5",
        set_fields={"maxInputTokens": 300_000, "compactTriggerTokens": 100_000},
    )
    budget = model_metadata.effective_request_budget(
        "claude-fable-5", scope_key=scope, use_max_context=True,
    )
    assert budget.context_window == 1_000_000
    assert budget.max_input_tokens == budget.effective_input_budget == 300_000
    assert budget.compact_trigger_tokens == 100_000
    before = copy.deepcopy(config.get())
    with pytest.raises(ValueError):
        model_metadata.patch_override_fields(
            "claude-fable-5", scope_key=scope, outbound_model="claude-fable-5",
            set_fields={"contextWindow": 1_000_001},
        )
    assert config.get() == before


@pytest.mark.parametrize("nested", [False, True])
def test_responses_final_counter_includes_instructions_without_double_count(nested):
    request = {"input": "hi", "instructions": ("long system instructions " * 100).strip()}
    wire = {"type": "response.create", "response": request} if nested else request
    segments = token_counter.request_prompt_segments(wire)
    assert segments.count(request["instructions"]) == 1
    assert token_counter.count_request_tokens(wire) > 100


@pytest.mark.parametrize("legacy", ["account", "provider", "channel"])
@pytest.mark.parametrize("drift", [False, True])
def test_legacy_sync_freezes_server_revision_without_header(domain_client, monkeypatch, legacy, drift):
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(
        channels=[channel(model="gpt-5.4")],
        oauthAccounts=[{"provider": "openai", "email": "sync@example.test",
                        "workspace_id": "w", "models": ["gpt-5.4"]}],
    ))
    registry.rebuild_from_config()
    assert model_pricing.binding_snapshot("openai/gpt-5.4")
    selector = {"account": {"accountId": "openai:sync@example.test:w"},
                "provider": {"providerId": "openai"}, "channel": {"channelId": "api:A"}}[legacy]
    workers = []
    monkeypatch.setattr(runtime.operations, "submit", lambda _, worker: workers.append(worker))
    response = client.post("/api/management/v1/model-metadata/actions/sync", headers=admin,
                           json={"scope": legacy, "refreshCatalog": False, **selector})
    assert response.status_code == 202, response.text
    if drift:
        model_metadata.set_binding("gpt-5.4", "xai/grok-4.5", source="manual")
    before = copy.deepcopy(config.get())
    workers[0]()
    terminal = client.get("/api/management/v1/operations/" + response.json()["data"]["id"],
                          headers=admin).json()["data"]
    if drift:
        assert terminal["status"] == "failed", terminal
        assert terminal["error"]["code"] == "REVISION_CONFLICT"
        assert config.get() == before
    else:
        assert terminal["status"] == "succeeded", terminal
        assert config.get()["modelBindings"]["scoped"]


def test_background_snapshot_commit_cas_preserves_manual(monkeypatch):
    model_metadata.set_binding("gpt-5.4", "openai/gpt-5.4", source="auto")
    reached, release = threading.Event(), threading.Event()
    original = model_pricing.canonical_official_model
    def pause(model):
        reached.set()
        assert release.wait(10)
        return original(model)
    monkeypatch.setattr(model_pricing, "canonical_official_model", pause)
    failures = []
    def worker():
        try:
            model_metadata.reconcile_auto_snapshots()
        except model_metadata.MetadataSyncConflict as error:
            failures.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert reached.wait(10)
        model_metadata.set_binding("gpt-5.4", "xai/grok-4.5", source="manual")
        saved = copy.deepcopy(config.get())
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert config.get() == saved


def test_native_tier_ids_roundtrip_validate_without_mutating_raw_catalog(domain_client):
    from src.channel.openai_oauth_channel import OpenAIOAuthChannel
    client, _, admin, *_ = domain_client
    account = {"provider": "openai", "email": "tier@example.test", "workspace_id": "w",
               "models": ["tier-model"], "account_model_catalog": {"models": [
                   {"id": "tier-model", "serviceTiers": [{"id": "priority", "name": "Fast"}]}]}}
    config.update(lambda c: c.update(oauthAccounts=[account]))
    registry.rebuild_from_config()
    native = OpenAIOAuthChannel(account)
    assert native.service_tier_catalog_status("tier-model", "priority") == "advertised"
    assert native.service_tier_catalog_status("tier-model", "flex") == "not_advertised"
    url = "/api/management/v1/model-metadata/tier-model"
    got = client.get(url, params={"scopeId": "openai:tier@example.test:w"}, headers=admin)
    assert got.status_code == 200, got.text
    assert got.json()["data"]["effective"]["serviceTiers"] == ["priority"]
    original_catalog = copy.deepcopy(account["account_model_catalog"])
    for tiers, status in [(["priority"], 200), (["flex"], 422), ([], 200)]:
        before = copy.deepcopy(config.get())
        result = client.patch(url + "/overrides", headers={**admin, "If-Match": MappingControl._metadata_revision()},
                              json={"scope": "oauth", "accountId": "openai:tier@example.test:w",
                                    "set": {"serviceTiers": tiers}})
        assert result.status_code == status, result.text
        if status == 422:
            assert config.get() == before
        else:
            assert result.json()["data"]["effective"]["serviceTiers"] == tiers
    assert config.get()["oauthAccounts"][0]["account_model_catalog"] == original_catalog


@pytest.mark.parametrize("snapshot_present", [True, False])
def test_actual_dispatch_freezes_unpriced_snapshot_not_active_tariff(snapshot_present):
    snapshot = model_pricing.binding_snapshot("openai/gpt-5.4")
    assert snapshot and snapshot["tariff"]
    snapshot["metadata"]["cost"] = None
    snapshot["tariff"] = None
    binding = {"target": "openai/gpt-5.4", "source": "auto"}
    if snapshot_present:
        binding["autoSnapshot"] = snapshot
    config.update(lambda c: c.update(modelBindings={"defaults": {"demo": binding}, "scoped": {}}))
    log_db.init()
    rid = "core-price-" + uuid4().hex
    log_db.insert_pending(rid, "127.0.0.1", "fixture", "demo", False, 1, 0, {}, {}, ingress_protocol="responses")
    attempt = log_db.record_retry_attempt(rid, 1, "api:A", "api", "demo", time.time(), upstream_protocol="openai-responses")
    log_db.mark_retry_attempt_dispatch(attempt, {"model": "demo"})
    frozen = log_db._get_conn().execute("SELECT binding_json FROM retry_chain WHERE id=?", (attempt.row_id,)).fetchone()[0]
    assert (json.loads(frozen)["tariff"] is None) is snapshot_present
    # After dispatch, changing the selected binding cannot change this attempt.
    model_metadata.set_binding("demo", "openai/gpt-5.4", source="manual")
    log_db.finish_success(rid, "api:A", "api", "demo", input_tokens=10, output_tokens=5,
                          response_body=json.dumps({"usage": {"input_tokens": 10, "output_tokens": 5}}), usage_observed=True)
    usage = log_db._get_conn().execute("SELECT cost_source,cost_ticks FROM upstream_attempt_usage WHERE retry_attempt_id=?", (attempt.row_id,)).fetchone()
    assert usage is not None
    if snapshot_present:
        assert tuple(usage) == ("unpriced", None)
    else:
        assert usage["cost_source"] == "estimated" and usage["cost_ticks"] > 0


@pytest.mark.parametrize("number", ["1e309", "-1e309", "NaN", "Infinity", "-Infinity"])
def test_all_nonfinite_api_prices_rejected_with_disk_zero_write(domain_client, number):
    client, _, admin, *_ = domain_client
    model_metadata.set_binding("demo", "openai/gpt-5.4")
    before = Path(config.path()).read_bytes()
    response = client.patch("/api/management/v1/model-metadata/demo/overrides",
                            headers={**admin, "If-Match": MappingControl._metadata_revision(), "Content-Type": "application/json"},
                            content='{"scope":"global","set":{"cost":{"input":' + number + '}}}')
    assert response.status_code == 422, response.text
    assert Path(config.path()).read_bytes() == before
    with pytest.raises(ValueError):
        model_metadata.normalize_override_fields({"cost.input": float(number)})


@pytest.mark.parametrize("verb", ["put", "patch"])
@pytest.mark.parametrize("disabled", [True, False])
def test_alias_api_reserves_fallback_and_disabled_real_ids(domain_client, verb, disabled):
    client, _, admin, *_ = domain_client
    config.update(lambda c: c.update(oauthAccounts=[{"provider": "claude", "email": "fallback@example.test", "models": [],
                                                    "disabledModels": ["real-a"] if disabled else []}],
                                    oauthDefaultModels=["real-a", "real-b"], modelMapping={"global": {"old": "real-b"}}))
    registry.rebuild_from_config()
    before = Path(config.path()).read_bytes()
    url = "/api/management/v1/model-mappings/" + ("real-a" if verb == "put" else "old")
    body = {"realModel": "real-b"}
    if verb == "patch":
        body["alias"] = "real-a"
    response = getattr(client, verb)(url, headers={**admin, "If-Match": MappingControl._mapping_revision()}, json=body)
    assert response.status_code == 409, response.text
    assert Path(config.path()).read_bytes() == before


@pytest.mark.parametrize("query,status", [
    ({"sourceId": "api:A"}, 422), ({"sourceType": "api"}, 422),
    ({"sourceType": "global", "sourceId": ""}, 422),
    ({"sourceType": "oauth", "sourceId": ""}, 422),
    ({"sourceType": "api", "sourceId": "api:A"}, 200),
])
def test_get_source_selector_pairing_and_positive_scope(domain_client, query, status):
    client, _, admin, *_ = domain_client
    config.update(lambda c: c.update(channels=[channel(), channel("B", "other")]))
    result = client.get("/api/management/v1/models", params={"type": "chat", **query}, headers=admin)
    assert result.status_code == status, result.text
    if status == 200:
        assert [item["modelId"] for item in result.json()["data"]] == ["demo"]


@pytest.mark.asyncio
async def test_candidate_captured_before_disable_is_rechecked_before_build(domain_client, monkeypatch):
    client, _, admin, *_ = domain_client
    config.update(lambda c: c.update(oauthAccounts=[{"provider": "claude", "email": "stale@example.test", "models": ["demo"]}]))
    registry.rebuild_from_config()
    route = scheduler.schedule({"model": "demo", "messages": []}, "fixture", "127.0.0.1")
    ch, model = route.candidates[0]
    def failed_reload(_):
        raise RuntimeError("isolated reload sentinel")
    monkeypatch.setattr(config, "_reload_callbacks", [failed_reload])
    listing = client.get("/api/management/v1/models?type=chat", headers=admin).json()
    response = client.patch("/api/management/v1/models/actions/state", headers={**admin, "If-Match": listing["meta"]["revision"]},
                            json={"scope": {"type": "oauth", "id": "claude:stale@example.test"},
                                  "selection": {"mode": "ids", "modelIds": ["demo"]}, "target": {"enabled": False}})
    assert response.status_code == 503
    built = []
    async def forbidden_build(*args, **kwargs):
        built.append(True)
        raise AssertionError("disabled source may not build or refresh tokens")
    monkeypatch.setattr(ch, "build_upstream_request", forbidden_build)
    now = time.time()
    result = await failover._try_channel(ch, model, {"model": "demo", "messages": []}, False, now + 60, now, None, [], None,
                                         "127.0.0.1", "fixture", 0, 0, ingress_protocol="anthropic",
                                         start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic())
    assert result.outcome == "candidate_guard" and not built
    assert "demo" not in registry.available_models()


@pytest.mark.parametrize("candidate_snapshot", [False, True])
@pytest.mark.asyncio
async def test_catalog_input_projection_never_blocks_real_wire_before_transport(monkeypatch, candidate_snapshot):
    from src.openai.channel.registration import register_factories
    register_factories()
    raw = {"id": "demo", "limit": {"context": 1000, "input": 100, "output": 200},
           "cost": {"input": 1, "output": 2}}
    binding = {"target": "fixture/demo", "source": "auto"}
    if candidate_snapshot:
        candidate = model_pricing.CatalogCandidate(
            api_payload={}, models_payload=[], catalog={}, aliases={},
            providers=frozenset({"fixture"}), metadata_models={"fixture/demo": raw},
            provider_names={}, official_models={"demo": "fixture/demo"}, revision="input-fixture",
        )
        binding["autoSnapshot"] = model_pricing.candidate_binding_snapshot(candidate, "fixture/demo")
    else:
        monkeypatch.setattr(model_pricing, "catalog_model", lambda _: copy.deepcopy(raw))
    config.update(lambda c: c.update(channels=[channel(protocol="openai-responses")],
                                    modelBindings={"defaults": {"demo": binding}, "scoped": {}}))
    registry.rebuild_from_config()
    ch = registry.get_channel("api:A")
    sent = []
    async def probe_transport(**kwargs):
        sent.append(json.loads(kwargs["upstream_req"].body))
        return SimpleNamespace(error=failover.AttemptResult(
            outcome="transport_error", error_detail="fixture stop before network",
        ))
    monkeypatch.setattr(failover, "open_response_with_proxy_chain", probe_transport)
    now = time.time()
    body = {"model": "demo", "input": "input tokens " * 100, "max_output_tokens": 20}
    assert 100 < token_counter.count_request_tokens(body) < 980
    result = await failover._try_channel(ch, "demo", body, False, now + 60, now, None, [], None,
                                         "127.0.0.1", "fixture", 0, 0, ingress_protocol="responses",
                                         start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic())
    # Local input estimates (tiktoken, provider-divergent) no longer hard-reject
    # a wire payload: upstream stays the authority for input length.
    assert result.outcome == "transport_error" and len(sent) == 1
    # Oversized output is clamped to the route cap, never rejected.
    big = {"model": "demo", "input": "input tokens " * 100, "max_output_tokens": 500}
    result = await failover._try_channel(ch, "demo", big, False, now + 60, now, None, [], None,
                                         "127.0.0.1", "fixture", 0, 0, ingress_protocol="responses",
                                         start_monotonic=time.monotonic(), attempt_start_monotonic=time.monotonic())
    assert result.outcome == "transport_error" and len(sent) == 2


def test_disabled_oauth_source_can_restore_and_sync_without_enabling(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client
    config.update(lambda c: c.update(oauthAccounts=[{
        "provider": "openai", "email": "disabled@example.test", "workspace_id": "w",
        "models": ["gpt-5.4"], "disabledModels": ["gpt-5.4"],
    }]))
    registry.rebuild_from_config()
    url = "/api/management/v1/model-metadata/gpt-5.4/overrides"
    selector = {"scope": "oauth", "accountId": "openai:disabled@example.test:w"}
    patch = client.patch(url, headers={**admin, "If-Match": MappingControl._metadata_revision()},
                         json={**selector, "set": {"maxInputTokens": 100_000}})
    assert patch.status_code == 200, patch.text
    restored = client.delete(url, params=selector, headers={**admin, "If-Match": MappingControl._metadata_revision()})
    assert restored.status_code == 204, restored.text
    workers = []
    monkeypatch.setattr(runtime.operations, "submit", lambda _, worker: workers.append(worker))
    response = client.post("/api/management/v1/model-metadata/actions/sync",
                           headers={**admin, "If-Match": MappingControl._metadata_revision()},
                           json={"mode": "source", "source": {"type": "oauth", "id": "openai:disabled@example.test:w"},
                                 "refreshCatalog": False})
    assert response.status_code == 202, response.text
    workers[0]()
    terminal = client.get("/api/management/v1/operations/" + response.json()["data"]["id"], headers=admin).json()["data"]
    assert terminal["status"] == "succeeded", terminal
    assert config.get()["oauthAccounts"][0]["disabledModels"] == ["gpt-5.4"]
    assert "gpt-5.4" not in registry.available_models()


def test_state_disk_failure_remains_zero_write_not_saved_reload_feedback(domain_client, monkeypatch):
    from fastapi.testclient import TestClient
    client, _, admin, *_ = domain_client
    config.update(lambda c: c.update(channels=[channel()]))
    listing = client.get("/api/management/v1/models?type=chat", headers=admin).json()
    before = copy.deepcopy(config.get())
    disk = Path(config.path()).read_bytes()
    def fail_write(_):
        raise OSError("isolated write failure")
    monkeypatch.setattr(config, "_write_atomic", fail_write)
    with TestClient(client.app, raise_server_exceptions=False) as public:
        response = public.patch("/api/management/v1/models/actions/state",
                                headers={**admin, "If-Match": listing["meta"]["revision"]},
                                json={"scope": {"type": "global"},
                                      "selection": {"mode": "ids", "modelIds": ["demo"]},
                                      "target": {"enabled": False}})
    assert response.status_code >= 500
    assert "SAVED_RELOAD_UNCONFIRMED" not in response.text
    assert config.get() == before and Path(config.path()).read_bytes() == disk
