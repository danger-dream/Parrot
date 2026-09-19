from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest
from types import SimpleNamespace

from ._isolation import isolate

isolate()

from src import config, model_metadata, model_pricing, state_db  # noqa: E402
from src.channel import registry  # noqa: E402
from src.openai.channel.registration import register_factories  # noqa: E402


def _tariff(input_per_million: float = 1.0, output_per_million: float = 2.0):
    return asdict(model_pricing.PricingEntry(
        input_per_token=input_per_million / 1_000_000,
        output_per_token=output_per_million / 1_000_000,
        cache_write_per_token=input_per_million / 1_000_000,
        cache_read_per_token=input_per_million / 1_000_000,
    ))


def _snapshot(revision: str, context: int, *, input_price: float = 1.0):
    return {
        "catalogRevision": revision,
        "catalogSource": "fixture",
        "metadata": {
            "contextWindow": context,
            "maxInputTokens": context,
            "maxOutputTokens": min(100_000, context),
            "compactTriggerTokens": min(200_000, context),
            "vision": True,
            "toolCall": True,
            "structuredOutput": True,
            "reasoningEfforts": ["low", "high"],
            "serviceTiers": ["default", "priority"],
            "knowledgeCutoff": "2025-01",
            "cost": {"input": input_price, "output": 2.0},
        },
        "tariff": _tariff(input_price),
    }


def _entry(target: str, snapshot: dict, *, outbound: str | None = None, source: str = "auto"):
    result = {"target": target, "source": source, "autoSnapshot": copy.deepcopy(snapshot)}
    if outbound is not None:
        result["outboundModel"] = outbound
    return result


def _reset_metadata_config(*, defaults=None, scoped=None, overrides=None, channels=None):
    config.update(lambda cfg: cfg.update({
        "modelBindings": {
            "defaults": copy.deepcopy(defaults or {}),
            "scoped": copy.deepcopy(scoped or {}),
        },
        "modelMetadataOverrides": copy.deepcopy(
            overrides or {"defaults": {}, "scoped": {}},
        ),
        "channels": copy.deepcopy(channels or []),
    }))
    if channels is not None:
        state_db.init()
        register_factories()
        registry.rebuild_from_config()


def test_sparse_16_field_overrides_inherit_preserve_false_zero_empty_and_restore():
    base = _snapshot("old", 900_000, input_price=4.0)
    defaults = {"demo": _entry("demo/model", base)}
    scoped = {"api:A": {"demo": _entry("demo/model", base, outbound="real-demo")}}
    _reset_metadata_config(defaults=defaults, scoped=scoped)

    common = {
        "contextWindow": 1_000_000,
        "maxInputTokens": 900_000,
        "maxOutputTokens": 90_000,
        "compactTriggerTokens": 800_000,
        "vision": True,
        "toolCall": True,
        "structuredOutput": True,
        "reasoningEfforts": ["low", "high"],
        "serviceTiers": ["default", "priority"],
        "knowledgeCutoff": "2026-09",
        "cost": {
            "input": 5.0,
            "output": 10.0,
            "cacheRead": 0.5,
            "cacheWrite": 1.25,
            "longContextInput": 7.5,
            "longContextOutput": 15.0,
        },
    }
    assert model_metadata.patch_override_fields(
        "demo", scope_key=None, outbound_model=None, set_fields=common,
    )
    assert model_metadata.patch_override_fields(
        "demo", scope_key="api:A", outbound_model="real-demo",
        set_fields={
            "contextWindow": 300_000,
            "maxInputTokens": 300_000,
            "compactTriggerTokens": 250_000,
            "vision": False,
            "reasoningEfforts": [],
            "serviceTiers": [],
            "cost.input": 0,
        },
    )

    effective = model_metadata.resolve_binding(
        "demo", scope_key="api:A", outbound_model="real-demo",
    )
    assert effective is not None
    assert effective.metadata["contextWindow"] == 300_000
    assert effective.metadata["maxOutputTokens"] == 90_000
    assert effective.metadata["vision"] is False
    assert effective.metadata["reasoningEfforts"] == []
    assert effective.metadata["serviceTiers"] == []
    assert effective.metadata["cost"]["input"] == 0
    assert effective.metadata["cost"]["output"] == 10.0
    assert effective.value_source["contextWindow"] == "source-override"
    assert effective.value_source["maxOutputTokens"] == "common-override"
    assert effective.value_source["cost.input"] == "source-override"
    assert len(effective.common_override) == 16
    assert set(effective.source_override) == {
        "contextWindow", "maxInputTokens", "compactTriggerTokens", "vision",
        "reasoningEfforts", "serviceTiers", "cost.input",
    }

    pricing = model_pricing.build_pricing_binding(
        channel_key="api:A", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="real-demo", client_visible_model="demo",
    )
    assert pricing.tariff is not None
    assert pricing.tariff.input_per_token == 0
    assert pricing.tariff.output_per_token == 10.0 / 1_000_000
    assert pricing.tariff_source == "metadata-override"

    assert model_metadata.patch_override_fields(
        "demo", scope_key="api:A", outbound_model="real-demo", set_fields={},
        unset_fields=("contextWindow", "vision", "cost.input"),
    )
    restored = model_metadata.resolve_binding(
        "demo", scope_key="api:A", outbound_model="real-demo",
    )
    assert restored is not None
    assert restored.metadata["contextWindow"] == 1_000_000
    assert restored.metadata["vision"] is True
    assert restored.metadata["cost"]["input"] == 5.0
    assert restored.value_source["contextWindow"] == "common-override"

    assert model_metadata.delete_override_layer("demo", scope_key="api:A")
    source_restored = model_metadata.resolve_binding(
        "demo", scope_key="api:A", outbound_model="real-demo",
    )
    assert source_restored is not None and source_restored.source_override == {}
    assert source_restored.metadata["reasoningEfforts"] == ["low", "high"]
    assert model_metadata.delete_override_layer("demo")
    common_restored = model_metadata.resolve_binding(
        "demo", scope_key="api:A", outbound_model="real-demo",
    )
    assert common_restored is not None and common_restored.common_override == {}
    assert common_restored.metadata["contextWindow"] == 900_000


@pytest.mark.parametrize("scope", [None, "api:A"])
@pytest.mark.parametrize("unset_field", ["contextWindow", "maxInputTokens"])
def test_override_patch_validates_final_inheritance_in_one_commit(monkeypatch, scope, unset_field):
    from pathlib import Path

    base = _snapshot("inheritance", 1000)
    common = {"vision": False, "cost.input": 0}
    if scope:
        common["contextWindow"] = 800
    else:
        common[unset_field] = 100
    overrides = {"defaults": {"demo": {"fields": common}}, "scoped": {}}
    if scope:
        overrides["scoped"][scope] = {"demo": {
            "outboundModel": "real-demo", "fields": {unset_field: 100},
        }}
    _reset_metadata_config(
        defaults={"demo": _entry("demo/model", base)},
        scoped={"api:A": {"demo": _entry("demo/model", base, outbound="real-demo")}},
        overrides=overrides,
    )
    outbound = "real-demo" if scope else None
    set_fields = {"maxInputTokens": 500, "compactTriggerTokens": 400} if unset_field == "contextWindow" else {"compactTriggerTokens": 500}
    before = copy.deepcopy(config.get())
    writes, published = [], []
    write_atomic = config._write_atomic

    def write(candidate):
        writes.append(copy.deepcopy(candidate))
        return write_atomic(candidate)

    monkeypatch.setattr(config, "_write_atomic", write)
    monkeypatch.setattr(config, "_reload_callbacks", [lambda cfg: published.append(copy.deepcopy(cfg))])
    assert model_metadata.patch_override_fields(
        "demo", scope_key=scope, outbound_model=outbound,
        set_fields=set_fields, unset_fields=(unset_field,),
    )
    assert len(writes) == len(published) == 1
    saved = config.get()
    assert json.loads(Path(config.path()).read_text()) == saved == published[0]
    assert saved["modelBindings"] == before["modelBindings"]
    expected_overrides = copy.deepcopy(overrides)
    layer = expected_overrides["scoped"][scope]["demo"]["fields"] if scope else expected_overrides["defaults"]["demo"]["fields"]
    layer.pop(unset_field)
    layer.update(set_fields)
    assert saved["modelMetadataOverrides"] == expected_overrides
    effective = model_metadata.get_metadata("demo", scope_key=scope, outbound_model=outbound)
    assert effective["contextWindow"] == (800 if scope else 1000)
    assert effective["maxInputTokens"] == (500 if unset_field == "contextWindow" else (800 if scope else 1000))
    assert effective["compactTriggerTokens"] == set_fields["compactTriggerTokens"]
    assert effective["vision"] is False and effective["cost"]["input"] == 0


@pytest.mark.parametrize("scope", [None, "api:A"])
def test_override_patch_rejects_invalid_final_inheritance_without_write(monkeypatch, scope):
    from pathlib import Path

    overrides = {"defaults": {"demo": {"fields": {"contextWindow": 2000}}}, "scoped": {}}
    if scope:
        overrides["defaults"]["demo"]["fields"]["contextWindow"] = 800
        overrides["scoped"][scope] = {"demo": {
            "outboundModel": "real-demo", "fields": {"contextWindow": 2000},
        }}
    _reset_metadata_config(defaults={"demo": _entry("demo/model", _snapshot("base", 1000))}, overrides=overrides)
    before = copy.deepcopy(config.get())
    disk = Path(config.path()).read_bytes()
    monkeypatch.setattr(config, "_write_atomic", lambda cfg: pytest.fail("invalid PATCH wrote config"))
    with pytest.raises(ValueError, match="maxInputTokens must not exceed contextWindow"):
        model_metadata.patch_override_fields(
            "demo", scope_key=scope, outbound_model="real-demo" if scope else None,
            set_fields={"maxInputTokens": 1500}, unset_fields=("contextWindow",),
        )
    assert config.get() == before
    assert Path(config.path()).read_bytes() == disk


def test_invalid_cross_field_and_native_hard_overrides_are_zero_write():
    native = {
        "id": "native-model", "contextWindow": 300_000,
        "maxInputTokens": 280_000, "maxOutputTokens": 20_000,
        "supportsImages": False, "toolCall": False,
        "reasoningEfforts": ["low"],
    }
    _reset_metadata_config(
        defaults={"native-model": _entry("demo/model", _snapshot("old", 1_000_000))},
    )
    config.update(lambda cfg: cfg.__setitem__("oauthAccounts", [{
        "provider": "openai", "email": "a@example.com", "workspace_id": "ws",
        "models": ["native-model"],
        "account_model_catalog": {"models": [native]},
    }]))
    scope = "oauth:openai:a@example.com:ws"
    before = json.dumps(config.get().get("modelMetadataOverrides"), sort_keys=True)
    invalid_values = (
        {"contextWindow": 300_001},
        {"vision": True},
        {"toolCall": True},
        {"reasoningEfforts": ["high"]},
        {"contextWindow": 100_000, "compactTriggerTokens": 100_001},
    )
    for values in invalid_values:
        try:
            model_metadata.patch_override_fields(
                "native-model", scope_key=scope, outbound_model="native-model",
                set_fields=values,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid override accepted: {values!r}")
        assert json.dumps(config.get().get("modelMetadataOverrides"), sort_keys=True) == before

    assert model_metadata.patch_override_fields(
        "native-model", scope_key=scope, outbound_model="native-model",
        set_fields={"contextWindow": 250_000, "vision": False, "reasoningEfforts": []},
    )
    effective = model_metadata.resolve_binding(
        "native-model", scope_key=scope, outbound_model="native-model",
    )
    assert effective is not None
    assert effective.metadata["contextWindow"] == 250_000
    assert effective.metadata["vision"] is False
    assert effective.metadata["reasoningEfforts"] == []


def test_candidate_sync_changes_only_explicit_auto_snapshot_and_never_active_catalog(monkeypatch):
    defaults = {
        "selected": _entry("demo/selected", _snapshot("old", 100_000, input_price=1.0)),
        "other": _entry("demo/other", _snapshot("old", 200_000, input_price=2.0)),
        "manual": _entry(
            "demo/manual", _snapshot("manual", 700_000, input_price=7.0),
            source="manual",
        ),
    }
    scoped = {
        "api:A": {"shared": _entry("demo/shared", _snapshot("a-old", 300_000), outbound="real-a")},
        "api:B": {"shared": _entry("demo/shared", _snapshot("b-old", 400_000), outbound="real-b")},
    }
    _reset_metadata_config(defaults=defaults, scoped=scoped)
    candidate = SimpleNamespace(revision="candidate-new")

    monkeypatch.setattr(
        model_pricing, "candidate_canonical_official_model",
        lambda _candidate, model: f"demo/{model}",
    )
    monkeypatch.setattr(
        model_pricing, "candidate_binding_snapshot",
        lambda _candidate, _target: _snapshot("candidate-new", 333_000, input_price=9.0),
    )
    active_status = copy.deepcopy(model_pricing.catalog_status())
    other_before = copy.deepcopy(config.get()["modelBindings"]["defaults"]["other"])
    manual_before = copy.deepcopy(config.get()["modelBindings"]["defaults"]["manual"])
    other_effective_before = copy.deepcopy(model_metadata.get_metadata("other"))
    other_price_before = model_pricing.build_pricing_binding(
        channel_key="api:unused", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="other", client_visible_model="other",
    ).binding_json

    result = model_metadata.sync_auto_snapshots(
        [("selected", None, None), ("manual", None, None)], candidate=candidate,
    )
    assert [item["status"] for item in result["results"]] == ["updated", "protected"]
    assert config.get()["modelBindings"]["defaults"]["selected"]["autoSnapshot"]["catalogRevision"] == "candidate-new"
    assert config.get()["modelBindings"]["defaults"]["other"] == other_before
    assert config.get()["modelBindings"]["defaults"]["manual"] == manual_before
    assert model_metadata.get_metadata("other") == other_effective_before
    assert model_pricing.build_pricing_binding(
        channel_key="api:unused", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="other", client_visible_model="other",
    ).binding_json == other_price_before
    assert model_pricing.catalog_status() == active_status

    b_before = copy.deepcopy(config.get()["modelBindings"]["scoped"]["api:B"]["shared"])
    global_before = copy.deepcopy(config.get()["modelBindings"]["defaults"])
    scoped_result = model_metadata.sync_auto_snapshots(
        [("shared", "api:A", "real-a")], candidate=candidate,
    )
    assert scoped_result["results"][0]["status"] == "updated"
    assert config.get()["modelBindings"]["scoped"]["api:A"]["shared"]["autoSnapshot"]["catalogRevision"] == "candidate-new"
    assert config.get()["modelBindings"]["scoped"]["api:B"]["shared"] == b_before
    assert config.get()["modelBindings"]["defaults"] == global_before
    assert model_pricing.catalog_status() == active_status


def test_legacy_binding_without_snapshot_resolves_active_catalog_without_eager_rewrite():
    _reset_metadata_config(defaults={
        "legacy-model": {"target": "openai/gpt-5.4", "source": "auto"},
    })
    before = copy.deepcopy(config.get()["modelBindings"])
    binding = model_metadata.resolve_binding("legacy-model")
    assert binding is not None and binding.auto_snapshot is None
    active = model_pricing.catalog_metadata("openai/gpt-5.4")
    assert active is not None
    for key, value in active.items():
        assert binding.metadata[key] == value
    assert config.get()["modelBindings"] == before


def test_full_reconcile_refreshes_existing_auto_snapshot_and_preserves_manual_binding():
    active_gpt = model_pricing.binding_snapshot("openai/gpt-5.4")
    active_grok = model_pricing.binding_snapshot("xai/grok-4.5")
    assert active_gpt is not None and active_grok is not None
    old_gpt = copy.deepcopy(active_gpt)
    old_gpt["catalogRevision"] = "old-generation"
    defaults = {
        "gpt-5.4": _entry("openai/gpt-5.4", old_gpt),
        "grok-4.5": _entry("xai/grok-4.5", active_grok, source="manual"),
    }
    _reset_metadata_config(defaults=defaults)
    manual_before = copy.deepcopy(config.get()["modelBindings"]["defaults"]["grok-4.5"])
    result = model_metadata.auto_sync_metadata([
        model_metadata.ModelInventoryItem("api:A", "api", "A", "gpt-5.4", "gpt-5.4"),
        model_metadata.ModelInventoryItem("api:B", "api", "B", "grok-4.5", "grok-4.5"),
    ], include_results=True)
    statuses = {(item["modelId"], item["source"]): item["status"] for item in result["results"]}
    assert statuses[("gpt-5.4", None)] == "updated"
    assert statuses[("grok-4.5", None)] == "protected"
    refreshed = config.get()["modelBindings"]["defaults"]["gpt-5.4"]["autoSnapshot"]
    assert refreshed["catalogRevision"] == model_pricing.catalog_status()["revision"]
    assert config.get()["modelBindings"]["defaults"]["grok-4.5"] == manual_before


def test_candidate_snapshot_persistence_failure_keeps_previous_config(monkeypatch):
    before_entry = _entry("demo/selected", _snapshot("old", 100_000))
    _reset_metadata_config(defaults={"selected": before_entry})
    before = copy.deepcopy(config.get()["modelBindings"])
    candidate = SimpleNamespace(revision="candidate-new")
    monkeypatch.setattr(
        model_pricing, "candidate_canonical_official_model",
        lambda _candidate, model: f"demo/{model}",
    )
    monkeypatch.setattr(
        model_pricing, "candidate_binding_snapshot",
        lambda _candidate, _target: _snapshot("candidate-new", 333_000),
    )

    def fail_write(_candidate):
        raise OSError("simulated atomic persistence failure")

    monkeypatch.setattr(config, "_write_atomic", fail_write)
    with pytest.raises(OSError, match="persistence failure"):
        model_metadata.sync_auto_snapshots(
            [("selected", None, None)], candidate=candidate,
        )
    assert config.get()["modelBindings"] == before


def test_channel_outbound_change_clears_only_exact_source_model_metadata():
    channel_a = {
        "name": "A", "baseUrl": "https://a.example.test/v1/messages",
        "apiKey": "sk-test-a", "protocol": "anthropic", "enabled": True,
        "models": [
            {"real": "old-real", "alias": "public"},
            {"real": "same-real", "alias": "stable"},
        ],
    }
    channel_b = {
        "name": "B", "baseUrl": "https://b.example.test/v1/messages",
        "apiKey": "sk-test-b", "protocol": "anthropic", "enabled": True,
        "models": [{"real": "b-real", "alias": "public"}],
    }
    snapshot = _snapshot("old", 300_000)
    defaults = {"public": _entry("demo/public", snapshot)}
    scoped = {
        "api:A": {
            "public": _entry("demo/public", snapshot, outbound="old-real"),
            "stable": _entry("demo/stable", snapshot, outbound="same-real"),
        },
        "api:B": {"public": _entry("demo/public", snapshot, outbound="b-real")},
    }
    overrides = {
        "defaults": {"public": {"fields": {"contextWindow": 900_000}}},
        "scoped": {
            "api:A": {
                "public": {"outboundModel": "old-real", "fields": {"contextWindow": 300_000}},
                "stable": {"outboundModel": "same-real", "fields": {"contextWindow": 250_000}},
            },
            "api:B": {
                "public": {"outboundModel": "b-real", "fields": {"contextWindow": 400_000}},
            },
        },
    }
    _reset_metadata_config(
        defaults=defaults, scoped=scoped, overrides=overrides,
        channels=[channel_a, channel_b],
    )
    result = registry.update_api_channel("A", {"models": [
        {"real": "new-real", "alias": "public"},
        {"real": "same-real", "alias": "stable"},
    ]})
    assert result is not None
    cfg = config.get()
    assert "public" not in cfg["modelBindings"]["scoped"]["api:A"]
    assert "public" not in cfg["modelMetadataOverrides"]["scoped"]["api:A"]
    assert cfg["modelBindings"]["scoped"]["api:A"]["stable"]["outboundModel"] == "same-real"
    assert cfg["modelMetadataOverrides"]["scoped"]["api:A"]["stable"]["fields"] == {"contextWindow": 250_000}
    assert cfg["modelBindings"]["scoped"]["api:B"]["public"]["outboundModel"] == "b-real"
    assert cfg["modelMetadataOverrides"]["scoped"]["api:B"]["public"]["fields"] == {"contextWindow": 400_000}
    assert cfg["modelBindings"]["defaults"] == defaults
    assert cfg["modelMetadataOverrides"]["defaults"] == overrides["defaults"]

    before = copy.deepcopy(cfg)
    registry.update_api_channel("A", {"models": [
        {"real": "new-real", "alias": "public"},
        {"real": "same-real", "alias": "stable"},
    ]})
    after = config.get()
    assert after["modelBindings"] == before["modelBindings"]
    assert after["modelMetadataOverrides"] == before["modelMetadataOverrides"]
