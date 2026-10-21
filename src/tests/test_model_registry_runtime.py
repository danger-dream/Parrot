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
