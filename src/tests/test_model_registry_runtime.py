"""Registry switched on: listing, routing, switches, allowlists and effort reach every source by model."""
from __future__ import annotations

import os
import sys

import pytest

from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, scheduler, state_db
    from src.channel import registry
    from src.model_registry import runtime
    from src.openai.channel.registration import register_factories
    return {"config": config, "scheduler": scheduler, "state_db": state_db, "registry": registry,
            "runtime": runtime, "register_factories": register_factories}


@pytest.fixture
def env(m):
    m["state_db"].init()
    m["register_factories"]()

    def mutate(cfg):
        cfg["oauthAccounts"] = []
        cfg["channels"] = [
            _api("Official", ["MiniMax-M3.1-Flash", "MiniMax-M2.7"]),
            _api("Reseller", ["minimax-m3.1-flash", "minimax-m2.7", "glm-5.3"]),
        ]
        cfg["channelSelection"] = "order"
        cfg["modelCenter"] = {"schemaVersion": 1, "disabledModels": [], "hiddenModels": [],
                              "apiSourceDisabledModels": {}, "sourceEnabledModels": {}}
        cfg["modelRegistry"] = {"enabled": True}
        cfg["apiKeys"] = {"lower": {"key": "sk-lower-test", "enabled": True, "allowedModels": ["MiniMax-M3.1-Flash"]}}

    m["config"].update(mutate)
    m["registry"].rebuild_from_config()
    assert m["runtime"].current() is not None
    yield m
    m["config"].update(lambda cfg: cfg.update(modelRegistry={"enabled": False}))
    m["registry"].rebuild_from_config()
    assert m["runtime"].current() is None


def _api(name, models):
    return {"name": name, "baseUrl": "https://example.com", "apiKey": "sk-testkey12345",
            "protocol": "openai-chat", "models": [{"real": model, "alias": model} for model in models],
            "enabled": True}


def _routes(m, model, key="", **body):
    result = m["scheduler"].schedule({"model": model, "messages": [{"role": "user", "content": "hi"}], **body},
                                     key, "1.1.1.1", ingress_protocol="chat")
    return [(channel.key, wire) for channel, wire in result.candidates], result


def _switches(m, **center):
    m["config"].update(lambda cfg: cfg["modelCenter"].update(center))
    m["registry"].rebuild_from_config()


def test_one_listed_name_routes_to_each_source_with_its_own_id(env):
    assert env["registry"].available_models() == ["glm-5.3", "minimax-m2.7", "minimax-m3.1-flash"]
    for spelling in ("MiniMax-M3.1-Flash", "minimax-m3.1-flash", "MINIMAX-M3.1-FLASH"):
        routes, _ = _routes(env, spelling)
        assert routes == [("api:Official", "MiniMax-M3.1-Flash"), ("api:Reseller", "minimax-m3.1-flash")]
    assert _routes(env, "no-such-model")[0] == []


def test_global_switch_covers_every_source_and_exceptions_stay_per_source(env):
    _switches(env, disabledModels=["MiniMax-M2.7"])
    assert _routes(env, "minimax-m2.7")[0] == []
    assert "minimax-m2.7" not in env["registry"].available_models()
    _switches(env, sourceEnabledModels={"api:Reseller": ["minimax-m2.7"]})
    assert _routes(env, "MiniMax-M2.7")[0] == [("api:Reseller", "minimax-m2.7")]
    _switches(env, disabledModels=[], sourceEnabledModels={},
              apiSourceDisabledModels={"api:Official": ["minimax-m3.1-flash"]})
    assert _routes(env, "minimax-m3.1-flash")[0] == [("api:Reseller", "minimax-m3.1-flash")]


def test_allowlist_matches_by_model_not_spelling(env):
    runtime = env["runtime"]
    assert runtime.permits(["MiniMax-M3.1-Flash"], "minimax-m3.1-flash")
    assert not runtime.permits(["MiniMax-M3.1-Flash"], "glm-5.3")
    assert env["registry"].available_models(api_key_name="lower") == ["minimax-m3.1-flash"]


def test_effort_is_written_only_where_the_request_left_it_open(env):
    runtime = env["runtime"]
    _, result = _routes(env, "glm-5.3-high")
    choice = result.call_choices[("api:Reseller", "glm-5.3")]
    assert runtime.apply({"model": "glm-5.3-high"}, choice, "chat")["reasoning_effort"] == "high"
    _, result = _routes(env, "glm-5.3", reasoning_effort="low")
    choice = result.call_choices[("api:Reseller", "glm-5.3")]
    assert runtime.apply({"reasoning_effort": "low"}, choice, "chat") == {"reasoning_effort": "low"}
    # A source that declares no levels gets no default: its upstream decides.
    _, result = _routes(env, "glm-5.3")
    assert result.call_choices[("api:Reseller", "glm-5.3")].effort is None
    _, result = _routes(env, "glm-5.3-high", reasoning_effort="low")
    assert result.candidates == [] and "fixes effort=high" in result.guard_error


def test_anthropic_reasoning_controls_are_kept_and_fixed_levels_written():
    from src.model_registry import runtime
    from src.model_registry.offers import Offer
    from src.model_registry.resolver import Choice
    param = Choice(Offer("oauth:a", "x", "m-1", "M 1", efforts=("low", "high")), "high", None)
    fixed = Choice(Offer("oauth:b", "x", "m-1-high", "M 1 (High)", basis="name", traits={"effort": "high"}), "high", None)
    adaptive = {"thinking": {"type": "adaptive"}}
    assert runtime.apply(adaptive, param, "anthropic") is adaptive
    assert runtime.apply(adaptive, fixed, "anthropic")["output_config"] == {"effort": "high"}
    assert runtime.apply({}, param, "responses") == {"reasoning": {"effort": "high"}}
    off = Choice(param.offer, None, "off")
    assert runtime.apply({}, off, "anthropic") == {"thinking": {"type": "disabled"}}


def test_migration_rekeys_switches_by_model_and_keeps_old_names_callable():
    from src.model_registry import pipeline, runtime
    from src.model_registry.offers import Offer
    offers = [
        Offer("api:Official", "api", "MiniMax-M2.7", "MiniMax-M2.7", basis="name", legacy="MiniMax-M2.7"),
        Offer("api:Reseller", "api", "minimax-m2.7", "minimax-m2.7", basis="name", legacy="minimax-m2.7"),
        Offer("api:Gemini", "api", "gem-3-flash-low", "Gem 3 Flash (Low)", basis="name",
              traits={"effort": "low"}, legacy="gem-3-flash-low"),
        Offer("api:Gemini", "api", "gem-3-flash-high", "Gem 3 Flash (High)", basis="name",
              traits={"effort": "high"}, legacy="gem-3-flash-high"),
        Offer("api:Qoder", "api", "gmodel", "GLM-5.3", basis="name", legacy="qoder-glm-5.3"),
    ]
    registry = pipeline.build(offers, catalog=pipeline.CatalogIndex([]))
    cfg = {"modelCenter": {"disabledModels": ["MiniMax-M2.7", "gem-3-flash-low", "image-x"],
                           "hiddenModels": [], "sourceEnabledModels": {"api:Reseller": ["MiniMax-M2.7"]}}}
    notes = []
    runtime.migrate_config(cfg, registry, notes)
    center = cfg["modelCenter"]
    assert center["disabledModels"] == ["image-x", "minimax-m2.7"]
    assert center["apiSourceDisabledModels"] == {"api:Gemini": ["Gem 3 Flash (Low)"]}
    assert center["sourceEnabledModels"] == {"api:Reseller": ["minimax-m2.7"]}
    root = cfg["modelRegistry"]
    assert root["migrated"] == runtime.MIGRATION_VERSION
    assert root["aliases"]["qoder-glm-5.3"] == "glm-5.3"
    assert root["aliases"]["gem-3-flash-low"] == {"model": "gem-3-flash", "traits": {"effort": "low"}}
    assert set(root["models"]) == {"minimax-m2.7", "gem-3-flash", "glm-5.3"}
    assert len(notes) == 1 and "gem-3-flash-low" in notes[0]


def test_model_center_lists_one_row_per_model_with_every_source(env):
    from src.management_control.models import ModelCenterControl
    views = {view.model_id: view for view in ModelCenterControl().list_models().items}
    assert "MiniMax-M3.1-Flash" not in views
    sources = views["minimax-m3.1-flash"].sources
    assert {(row.id, row.outbound_model) for row in sources} == {
        ("api:Official", "MiniMax-M3.1-Flash"), ("api:Reseller", "minimax-m3.1-flash")}


def test_per_model_priority_follows_the_model_under_any_spelling(env):
    def mutate(cfg):
        cfg["channelSelection"] = "priority"
        cfg["loadBalancing"] = {"initialized": True, "channelPriorityOrder": ["api:Official", "api:Reseller"],
                                "modelPriorityOrders": {"MiniMax-M3.1-Flash": ["api:Reseller", "api:Official"]}}
    env["config"].update(mutate)
    env["registry"].rebuild_from_config()
    assert [key for key, _ in _routes(env, "minimax-m3.1-flash")[0]] == ["api:Reseller", "api:Official"]
    assert [key for key, _ in _routes(env, "minimax-m2.7")[0]] == ["api:Official", "api:Reseller"]


def test_default_level_leaves_short_outputs_alone_but_named_levels_still_apply():
    from src.model_registry import runtime
    from src.model_registry.offers import Offer
    from src.model_registry.resolver import Choice
    offer = Offer("oauth:zhipu:a", "zhipu", "glm-5.3", "GLM-5.3", efforts=("low", "high", "max"))
    default = Choice(offer, "high", None)
    assert runtime.apply({"max_tokens": 64}, default, "chat") == {"max_tokens": 64}
    assert runtime.apply({"max_output_tokens": 1024}, default, "responses") == {"max_output_tokens": 1024}
    assert runtime.apply({"max_tokens": 4096}, default, "chat")["reasoning_effort"] == "high"
    named = Choice(offer, "high", None, explicit=True)
    assert runtime.apply({"max_tokens": 64}, named, "chat")["reasoning_effort"] == "high"


def _hy3_registry(rules=()):
    from src.model_registry import pipeline
    from src.model_registry.offers import Offer
    offers = [Offer("oauth:wb", "workbuddy", "hy3", "Hy3", basis="name"),
              Offer("oauth:wb", "workbuddy", "hy3-x", "Hy3", basis="name")]
    return pipeline.build(offers, rules=rules, catalog=pipeline.CatalogIndex([]))


def test_pending_offers_carry_choices_and_an_assign_rule_settles_them(monkeypatch):
    from src.model_registry import runtime
    registry = _hy3_registry()
    monkeypatch.setattr(runtime, "_registry", registry)
    item = runtime.overview({"modelRegistry": {"enabled": True}}).pending[0]
    assert (item.wire, item.model, item.options) == ("hy3-x", "workbuddy-hy3-x", ("hy3",))
    settled = _hy3_registry([{"type": "assign", "source": "oauth:wb", "wire": "hy3-x", "model": "hy3-x"}])
    assert not any(model.pending for model in settled.models.values())
    assert settled.lookup("hy3-x")[0] == "hy3-x" and settled.lookup("hy3")[0] == "hy3"


def test_drift_reports_moves_new_pending_and_stale_pins_but_not_ruled_moves():
    from src.model_registry import runtime
    previous = {"placements": {"api:a|x": "m1", "api:a|y": "m2"}, "pending": [], "manual": [], "stalePins": []}
    current = {"placements": {"api:a|x": "m9", "api:a|y": "m3", "api:b|z": "m4"}, "pending": ["api:b|z"],
               "manual": ["api:a|y"], "stalePins": ["deepseek-flash>deepseek-v4.1-flash|DeepSeek V4.2 Flash"]}
    assert runtime.drift_lines(previous, current) == [
        "api:a 的 x 从 m1 改归 m9",
        "新的待确认：api:b 的 z",
        "人工合并 deepseek-flash → deepseek-v4.1-flash 的依据变了：目录里现在叫 “DeepSeek V4.2 Flash”",
    ]
    assert runtime.drift_lines(current, current) == []


def test_rename_carries_switches_and_a_merge_named_by_alias_acts_on_its_model(env):
    runtime = env["runtime"]
    _switches(env, disabledModels=["minimax-m2.7"], sourceEnabledModels={"api:Reseller": ["minimax-m2.7"]})
    env["config"].update(lambda cfg: runtime.write_rules(
        cfg, [{"type": "merge", "from": "MiniMax-M2.7", "into": "minimax-m2.7-legacy"}], []))
    env["registry"].rebuild_from_config()
    cfg = env["config"].get()
    assert cfg["modelRegistry"]["rules"] == [{"type": "merge", "from": "minimax-m2.7", "into": "minimax-m2.7-legacy"}]
    assert cfg["modelCenter"]["disabledModels"] == ["minimax-m2.7-legacy"]
    assert cfg["modelCenter"]["sourceEnabledModels"] == {"api:Reseller": ["minimax-m2.7-legacy"]}
    assert runtime.current().lookup("MiniMax-M2.7")[0] == "minimax-m2.7-legacy"
    assert _routes(env, "minimax-m2.7")[0] == [("api:Reseller", "minimax-m2.7")]


def test_metadata_saved_under_old_names_is_found_by_model_and_edits_consolidate(env):
    from src import model_metadata
    binding = {"provider": "minimax", "target": "MiniMax-M3.1-Flash", "source": "manual"}
    override = {"fields": {"maxOutputTokens": 4096}}
    env["config"].update(lambda cfg: cfg.update(
        modelBindings={"defaults": {"MiniMax-M3.1-Flash": binding}},
        modelMetadataOverrides={"defaults": {"MiniMax-M3.1-Flash": override}}))
    assert model_metadata._model_keys("", "minimax-m3.1-flash", {"MiniMax-M3.1-Flash": 1}) == ["MiniMax-M3.1-Flash"]
    assert model_metadata.get_override_fields("minimax-m3.1-flash")[0] == {"maxOutputTokens": 4096}
    assert {(item.scope_key, item.client_visible_model) for item in model_metadata.inventory_items()} >= {
        ("api:Official", "minimax-m3.1-flash"), ("api:Reseller", "minimax-m3.1-flash")}
    assert model_metadata.delete_override_layer("minimax-m3.1-flash")
    assert env["config"].get()["modelMetadataOverrides"]["defaults"] == {}


def test_second_migration_keys_metadata_by_model_without_touching_switches():
    from src.model_registry import pipeline, runtime
    from src.model_registry.offers import Offer
    offers = [Offer("api:Official", "api", "MiniMax-M2.7", "MiniMax-M2.7", basis="name", legacy="MiniMax-M2.7"),
              Offer("api:G", "api", "gem-3-high", "Gem 3 (High)", basis="name", traits={"effort": "high"}, legacy="gem-3-high"),
              Offer("api:G", "api", "gem-3-low", "Gem 3 (Low)", basis="name", traits={"effort": "low"}, legacy="gem-3-low")]
    registry = pipeline.build(offers, catalog=pipeline.CatalogIndex([]))
    cfg = {"modelRegistry": {"migrated": 1}, "modelCenter": {"disabledModels": ["MiniMax-M2.7"]},
           "modelBindings": {"defaults": {"MiniMax-M2.7": {"target": "a"}, "gem-3-high": {"target": "h"},
                                          "gem-3-low": {"target": "l"}}},
           "modelMetadataOverrides": {"scoped": {"api:G": {"gem-3-high": {"fields": {"vision": True}}}}}}
    notes = []
    runtime.migrate_config(cfg, registry, notes)
    assert cfg["modelRegistry"]["migrated"] == runtime.MIGRATION_VERSION == 2
    assert cfg["modelCenter"]["disabledModels"] == ["MiniMax-M2.7"]
    defaults = cfg["modelBindings"]["defaults"]
    assert defaults["minimax-m2.7"] == {"target": "a"} and defaults["MiniMax-M2.7"] == {"target": "a"}
    assert defaults["gem-3"] == {"target": "h"}
    assert cfg["modelMetadataOverrides"]["scoped"]["api:G"]["gem-3"] == {"fields": {"vision": True}}
    assert notes == ["元数据匹配里 gem-3-high、gem-3-low 都属于 gem-3，按模型查询时采用 gem-3-high 的设置"]


def test_declared_capabilities_hold_for_every_schedulable_source(env, monkeypatch):
    from src.model_registry import capabilities
    metadata = {
        "api:Official": {"contextWindow": 1_000_000, "maxOutputTokens": 128_000, "vision": True,
                         "toolCall": True, "reasoningEfforts": ["low", "medium", "high", "max"]},
        "api:Reseller": {"contextWindow": 200_000, "maxOutputTokens": 64_000, "vision": True,
                         "toolCall": False, "reasoningEfforts": ["low", "high"]},
    }
    monkeypatch.setattr(capabilities, "_metadata", lambda model_id, offer: metadata[offer.source])
    assert capabilities.describe("minimax-m3.1-flash", None) == {
        "context_window": 200_000, "context_length": 200_000, "max_input_tokens": 200_000, "max_output_tokens": 64_000,
        "capabilities": {"vision": True, "tool_call": False, "reasoning_efforts": ["low", "high"]}}
    # A source out of scheduling no longer narrows what is declared; one declaring nothing does not count.
    _switches(env, apiSourceDisabledModels={"api:Reseller": ["minimax-m3.1-flash"]})
    metadata["api:Official"].pop("vision")
    declared = capabilities.describe("minimax-m3.1-flash", None)
    assert declared["context_window"] == 1_000_000 and "vision" not in declared["capabilities"]


def test_ids_fixed_to_one_effort_declare_only_their_levels(monkeypatch):
    from src.model_registry import capabilities, pipeline, runtime
    from src.model_registry.offers import Offer
    offers = [Offer("oauth:ag", "antigravity", f"m-1-{level}", f"M 1 ({level.title()})", basis="name",
                    traits={"effort": level}) for level in ("low", "high")]
    monkeypatch.setattr(runtime, "_registry", pipeline.build(offers, catalog=pipeline.CatalogIndex([])))
    monkeypatch.setattr(capabilities, "serving_offers", lambda model_id, api_key: offers)
    monkeypatch.setattr(capabilities, "_metadata", lambda model_id, offer: {"reasoningEfforts": ["low", "high", "max"]})
    assert capabilities.describe("m-1", None) == {"capabilities": {"reasoning_efforts": ["low", "high"]}}


def test_models_endpoint_declares_context_and_capabilities(env, monkeypatch):
    import asyncio
    import server
    from starlette.requests import Request
    from src.model_registry import capabilities
    monkeypatch.setattr(capabilities, "_metadata", lambda model_id, offer: {"contextWindow": 300_000, "vision": False})
    monkeypatch.setattr(server.auth, "validate", lambda headers: ("lower", ["MiniMax-M3.1-Flash"], None))
    body = asyncio.run(server.list_models(Request({"type": "http", "method": "GET", "path": "/v1/models",
                                                    "headers": [], "query_string": b""})))
    assert body["data"] == [{
        "type": "model", "id": "minimax-m3.1-flash", "display_name": "MiniMax-M3.1-Flash",
        "created_at": "2025-01-01T00:00:00Z", "context_window": 300_000, "context_length": 300_000,
        "max_input_tokens": 300_000, "capabilities": {"vision": False}}]


def test_models_endpoint_applies_an_unscoped_keys_allowlist(env, monkeypatch):
    import asyncio
    import server
    from starlette.requests import Request
    monkeypatch.setattr(server.auth, "validate", lambda headers: ("lower", ["MiniMax-M3.1-Flash"], None))
    body = asyncio.run(server.list_models(Request({"type": "http", "method": "GET", "path": "/v1/models",
                                                    "headers": [], "query_string": b""})))
    assert [item["id"] for item in body["data"]] == ["minimax-m3.1-flash"]


def test_a_source_that_always_thinks_still_serves_a_request_asking_thinking_off():
    from src.model_registry import pipeline, resolver
    from src.model_registry.offers import Offer
    always = Offer("oauth:wb", "workbuddy", "glm-5.3", "GLM-5.3", thinking="always", efforts=("low", "high"))
    registry = pipeline.build([always], catalog=pipeline.CatalogIndex([]))
    found = resolver.resolve(registry, "glm-5.3")
    choices = resolver.choose(registry, found, lambda offer: True, thinking="off")
    assert [(choice.offer.wire, choice.thinking) for choice in choices] == [("glm-5.3", None)]


def test_scheduler_records_why_each_source_was_left_out(env):
    _switches(env, apiSourceDisabledModels={"api:Official": ["minimax-m3.1-flash"]}, disabledModels=["minimax-m2.7"],
              sourceEnabledModels={})
    _, result = _routes(env, "minimax-m3.1-flash")
    assert {(item["channel"], item["reason"]) for item in result.exclusions} >= {("api:Official", "source_model_disabled")}
    _, result = _routes(env, "minimax-m2.7")
    assert {(item["channel"], item["reason"]) for item in result.exclusions} >= {
        ("api:Official", "global_model_disabled"), ("api:Reseller", "global_model_disabled")}


def test_plan_names_sources_whose_ids_cannot_take_the_asked_effort(monkeypatch):
    from src.model_registry import pipeline, runtime
    from src.model_registry.offers import Offer
    offers = [Offer("oauth:ag", "antigravity", f"m-1-{level}", f"M 1 ({level.title()})", basis="name",
                    traits={"effort": level}) for level in ("low", "high")]
    monkeypatch.setattr(runtime, "_registry", pipeline.build(offers, catalog=pipeline.CatalogIndex([])))
    monkeypatch.setattr(runtime.Switches, "blocked", lambda self, offer, model_id: None)
    planned = runtime.plan("m-1", {"reasoning_effort": "xhigh"}, "chat", None)
    assert planned.choices == {} and planned.rejected == {"oauth:ag": "reasoning_unsupported"}


def test_a_failed_rebuild_keeps_the_last_good_registry_and_alerts_once(env, monkeypatch):
    runtime = env["runtime"]
    alerts = []
    monkeypatch.setattr(runtime, "alert", alerts.append)
    good = runtime.current()
    monkeypatch.setattr(runtime, "compute", lambda cfg, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    env["registry"].rebuild_from_config()
    env["registry"].rebuild_from_config()
    assert runtime.current() is good
    assert len(alerts) == 1 and "上一份可用" in alerts[0] and "boom" in alerts[0]
    monkeypatch.setattr(runtime, "_registry", None)
    with pytest.raises(RuntimeError):
        runtime.rebuild(env["config"].get())


def test_a_malformed_rule_is_skipped_on_its_own(env, monkeypatch):
    runtime = env["runtime"]
    alerts = []
    monkeypatch.setattr(runtime, "alert", alerts.append)
    monkeypatch.setattr(runtime, "_bad_rules", set())
    env["config"].update(lambda cfg: cfg["modelRegistry"].update(rules=[
        "oops", {"type": "merge", "from": "minimax-m2.7", "into": "minimax-m2.7-legacy"}]))
    env["registry"].rebuild_from_config()
    assert runtime.current().lookup("MiniMax-M2.7")[0] == "minimax-m2.7-legacy"
    env["registry"].rebuild_from_config()
    assert alerts == ["忽略一条格式不对的人工规则：'oops'"]


def test_resubmitting_a_merge_in_another_spelling_keeps_it(env):
    runtime = env["runtime"]
    merge = {"type": "merge", "from": "minimax-m2.7", "into": "minimax-m2.7-legacy"}
    env["config"].update(lambda cfg: runtime.write_rules(cfg, [dict(merge)], []))
    env["registry"].rebuild_from_config()
    env["config"].update(lambda cfg: runtime.write_rules(
        cfg, [{"type": "merge", "from": "MiniMax-M2.7", "into": "MINIMAX-M2.7-LEGACY"}], []))
    env["registry"].rebuild_from_config()
    stored = env["config"].get()["modelRegistry"]["rules"]
    assert len(stored) == 1 and stored[0]["from"].lower() == "minimax-m2.7"
    assert runtime.current().lookup("minimax-m2.7")[0] == "minimax-m2.7-legacy"


def test_a_newer_migration_marker_is_left_alone(monkeypatch):
    from src import config
    from src.model_registry import runtime
    monkeypatch.setattr(config, "get", lambda: {"modelRegistry": {"enabled": True, "migrated": runtime.MIGRATION_VERSION + 1}})
    assert runtime.migrate_if_needed() is None


def test_drift_is_reported_off_the_reload_path_and_only_for_the_latest_build(monkeypatch):
    import threading
    import time
    from src.model_registry import runtime
    seen, gate, done = [], threading.Event(), threading.Event()

    def report(registry, cfg):
        gate.wait(5)
        seen.append(registry)
        if registry == "c":
            done.set()
    monkeypatch.setattr(runtime, "_report_drift", report)
    runtime._queue_drift("a", {})
    time.sleep(0.2)
    runtime._queue_drift("b", {})
    runtime._queue_drift("c", {})
    gate.set()
    assert done.wait(5) and seen == ["a", "c"]
