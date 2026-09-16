"""Strict executable v0.31.13 traces for TG-MAP-01 and TG-MAP-02."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src import model_mapping, model_metadata, model_pricing
from src.telegram import states, ui
from src.telegram.menus import mapping_menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl


from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/model_routing.jsonl"
CAPABILITIES = {"TG-MAP-01", "TG-MAP-02", "TG-LB-01", "TG-PX-01", "TG-PX-02"}
MAP_CASE_NAMES = (
    "TG-MAP-01.overview-default-pagination",
    "TG-MAP-01.add-global-legacy-one-level",
    "TG-MAP-01.item-edit-delete-cancel",
    "TG-MAP-01.invalid-expired-business-failure",
    "TG-MAP-02.inventory-navigation",
    "TG-MAP-02.scope-binding-detail-delete",
    "TG-MAP-02.catalog-search",
    "TG-MAP-02.sync-success-running-failure-unmatched",
    "TG-MAP-02.compression-and-expiry",
    "TG-MAP-02.invalid-callbacks-and-states",
)


def _base_config() -> dict[str, Any]:
    defaults = {
        f"model-{i:02d}": {"target": f"provider-a/model-{i:02d}", "source": "auto"}
        for i in range(1, 8)
    }
    return {
        "modelMapping": {
            "global": {"alias-a": "model-01", "chain-a": "alias-a"},
            "anthropic": {"legacy-only": "legacy-real", "alias-a": "legacy-shadow"},
            "openai-chat": {"legacy-only": "legacy-chat"},
            "openai-responses": {"legacy-only": "legacy-responses"},
        },
        "ingressDefaultModel": {"global": "alias-a", "anthropic": "model-03"},
        "modelBindings": {
            "defaults": defaults,
            "scoped": {
                "oauth:openai:acct@example.test": {
                    "model-01": {
                        "target": "provider-b/model-01",
                        "source": "manual",
                        "outboundModel": "upstream-01",
                    }
                }
            },
            "legacyMigrationVersion": 1,
        },
        "compressionModel": "model-01",
        "oauthAccounts": [],
    }


def _catalog() -> list[dict[str, str]]:
    rows = []
    for provider, label in (("provider-a", "Provider A"), ("provider-b", "Provider B")):
        for i in range(1, 9):
            model = f"model-{i:02d}"
            rows.append({
                "key": f"{provider}/{model}", "id": model, "name": f"Model {i:02d}",
                "provider_id": provider, "provider_name": label,
            })
    return rows


def _catalog_meta(key: str) -> dict[str, Any] | None:
    if key not in {row["key"] for row in _catalog()}:
        return None
    number = int(key.rsplit("-", 1)[-1])
    return {
        "name": f"Model {number:02d}", "contextWindow": 100000 + number,
        "compactTriggerTokens": 80000 + number, "maxOutputTokens": 10000 + number,
        "vision": number % 2 == 0, "reasoning": True, "toolCall": True,
        "structuredOutput": False, "reasoningEfforts": ["low", "high"],
        "releaseDate": "2026-01-02",
        "cost": {"input": 1.25, "output": 5.5, "cache_read": 0.25, "cache_write": 1.5},
    }


def _inventory() -> list[model_metadata.ModelInventoryItem]:
    result = []
    for scope, kind, label, prefix in (
        ("oauth:openai:acct@example.test", "oauth", "OpenAI Test", "upstream"),
        ("api:test-channel", "api", "API <测试>", "api-real"),
    ):
        for i in range(1, 9):
            result.append(model_metadata.ModelInventoryItem(
                scope, kind, label, f"model-{i:02d}", f"{prefix}-{i:02d}",
            ))
    return result


def _state_snapshot(chat_id: int = 42) -> dict[str, Any] | None:
    return deepcopy(states.get_state(chat_id))


def _setup(monkeypatch: pytest.MonkeyPatch):
    cfg = _base_config()
    events: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def update(mutator, **_kwargs):
        before = deepcopy(cfg)
        mutator(cfg)
        if before != cfg:
            events.append({"event": "config_update", "config": deepcopy(cfg)})
        return deepcopy(cfg)

    def api(method, data=None):
        calls.append({"method": method, "payload": deepcopy(data or {})})
        result = {"message_id": 501} if method == "sendMessage" else {}
        return {"ok": True, "result": result}

    catalog = _catalog()
    monkeypatch.setattr(states.time, "time", lambda: 1000.0)
    monkeypatch.setattr(model_mapping.config, "get", lambda: cfg)
    monkeypatch.setattr(model_mapping.config, "update", update)
    monkeypatch.setattr(model_mapping, "list_available_models_for", lambda _line: [f"model-{i:02d}" for i in range(1, 14)])
    monkeypatch.setattr(model_metadata, "inventory_items", _inventory)
    monkeypatch.setattr(model_pricing, "catalog_models", lambda: deepcopy(catalog))
    monkeypatch.setattr(model_pricing, "catalog_providers", lambda: [
        {"id": "provider-a", "name": "Provider A"},
        {"id": "provider-b", "name": "Provider B"},
    ])
    monkeypatch.setattr(model_pricing, "catalog_provider_models", lambda provider: [
        deepcopy(row) for row in catalog if row["provider_id"] == provider
    ])
    monkeypatch.setattr(model_pricing, "catalog_metadata", _catalog_meta)
    monkeypatch.setattr(model_pricing, "catalog_model", lambda key: (
        {**(_catalog_meta(key) or {}), "cost": {
            "input": 1.25, "output": 5.5,
            "tiers": [{"tier": {"type": "context", "size": 200000}, "input": 2.5, "output": 8.0}],
        }} if _catalog_meta(key) is not None else None
    ))
    monkeypatch.setattr(model_pricing, "canonical_official_model", lambda name: (
        f"provider-a/{str(name).lower()}" if f"provider-a/{str(name).lower()}" in {r["key"] for r in catalog} else None
    ))
    monkeypatch.setattr(model_pricing, "catalog_status", lambda: {"metadata_models": 16, "providers": 2})
    monkeypatch.setattr(mapping_menu.compact_rescue, "chunk_target_tokens", lambda: 123456)
    monkeypatch.setattr(ui, "api", api)
    states.clear_all()
    ui._code_to_name.clear()
    mapping_menu._METADATA_SYNC_RUNNING = False
    return cfg, events, calls


def _actual(case_name: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg, events, calls = _setup(monkeypatch)
    callbacks: list[str] = []
    state_actions: list[str] = []
    steps: list[dict[str, Any]] = []
    exception = None

    def cb(data: str):
        callbacks.append(data)
        handled = mapping_menu.handle_callback(42, 77, "cb-map", data)
        steps.append({"after": data, "handled": handled, "state": _state_snapshot()})
        return handled

    def text(action: str, value: str):
        state_actions.append(action.split(":", 1)[0] + (":" if ":" in action else ""))
        handled = mapping_menu.handle_text_state(42, action, value)
        steps.append({"afterText": action, "input": value, "handled": handled, "state": _state_snapshot()})
        return handled

    scenario = case_name.split(".", 1)[1]
    if scenario == "overview-default-pagination":
        cb("map:show"); cb("map:line:glo"); cb("map:set_default:glo")
        cb("map:page_default:glo:1")
        code = ui.register_code("map:model:model-13")
        cb(f"map:pick_default:glo:{code}:1"); cb("map:clear_default:glo")
        cb("map:line:anp"); cb("map:set_default:anp")
        legacy_code = ui.register_code("map:model:model-02")
        cb(f"map:pick_default:anp:{legacy_code}:0"); cb("map:clear_default:anp")
    elif scenario == "add-global-legacy-one-level":
        cb("map:add:glo"); cb("map:line:glo")  # cancel button retains the pending text state
        text("map_alias_input:glo", " legacy-only ")
        alias_code = ui.register_code("map:pending_alias:global:legacy-only")
        cb(f"map:page_add:glo:{alias_code}:1")
        model_code = ui.register_code("map:model:model-12")
        cb(f"map:pick_real:glo:{alias_code}:{model_code}:1")
        bodies = [{}, {"model": "chain-a"}, {"model": "alias-a"}]
        model_mapping.apply_default(bodies[0], "openai-chat")
        first = model_mapping.apply_mapping(bodies[0], "openai-chat")
        chain = model_mapping.apply_mapping(bodies[1], "openai-chat")
        direct = model_mapping.apply_mapping(bodies[2], "anthropic")
        events.append({"event": "runtime_semantics", "bodies": bodies,
                       "results": [list(first) if first else None, list(chain) if chain else None,
                                   list(direct) if direct else None]})
    elif scenario == "item-edit-delete-cancel":
        alias_code = ui.register_code("map:alias:global:alias-a")
        cb(f"map:item:glo:{alias_code}"); cb(f"map:edit_alias:glo:{alias_code}")
        cb(f"map:item:glo:{alias_code}")  # cancel/back retains the pending text state
        cb(f"map:edit_alias:glo:{alias_code}")
        text(f"map_alias_edit:glo:{alias_code}", " ")
        text(f"map_alias_edit:glo:{alias_code}", "alias-renamed")
        new_code = ui.register_code("map:alias:global:alias-renamed")
        cb(f"map:item:glo:{new_code}"); cb(f"map:edit_real:glo:{new_code}")
        cb(f"map:page_edit_real:glo:{new_code}:1")
        model_code = ui.register_code("map:model:model-11")
        cb(f"map:pick_edit_real:glo:{new_code}:{model_code}:1")
        cb(f"map:rm:glo:{new_code}"); cb(f"map:item:glo:{new_code}")
        cb(f"map:rm:glo:{new_code}"); cb(f"map:rm_ok:glo:{new_code}")
    elif scenario == "invalid-expired-business-failure":
        for data in ("map:", "map:line:bad", "map:unknown:glo", "map:pick_default:glo",
                     "map:pick_real:glo", "map:item:glo", "map:edit_alias:glo",
                     "map:edit_real:glo", "map:pick_edit_real:glo", "map:page_edit_real:glo",
                     "map:rm:glo", "map:rm_ok:glo"):
            cb(data)
        cb("map:item:glo:deadbeef"); cb("map:page_add:glo:deadbeef:0")
        alias_code = ui.register_code("map:pending_alias:global:same")
        same_code = ui.register_code("map:model:same")
        cb(f"map:pick_real:glo:{alias_code}:{same_code}:0")
        good_code = ui.register_code("map:model:model-01")
        original = model_mapping.set_default
        monkeypatch.setattr(model_mapping, "set_default", lambda *_args: (_ for _ in ()).throw(ValueError("fake default failure")))
        cb(f"map:pick_default:glo:{good_code}:0")
        monkeypatch.setattr(model_mapping, "set_default", original)
        text("map_alias_input:bad", "x")
        text("map_alias_edit:bad:deadbeef", "x")
    elif scenario == "inventory-navigation":
        states.set_state(42, "meta_catalog_search:old")
        cb("map:meta"); cb("map:meta_view:d:1"); cb("map:meta_view:s:0")
        cb("map:meta_page:not-int"); cb("map:meta_noop")
        from src.telegram.menus import main as main_menu
        monkeypatch.setattr(main_menu, "handle_back", lambda chat, msg, callback: events.append(
            {"event": "main_back", "args": [chat, msg, callback]}))
        states.set_state(42, "meta_catalog_search:old")
        cb("map:meta_main")
    elif scenario == "scope-binding-detail-delete":
        cb("map:meta_scope:o:0"); cb("map:meta_scope:a:0"); cb("map:meta_scope:0")
        scope_code = mapping_menu._binding_tag(scope="oauth:openai:acct@example.test", model="__scope__",
                                               flow="add", scope_kind="o", scope_page=0)
        cb(f"map:meta_models:{scope_code}:1")
        select_code = mapping_menu._binding_tag(scope="oauth:openai:acct@example.test", model="model-02",
                                                outbound="upstream-02", flow="add", scope_kind="o",
                                                scope_page=0, model_page=0)
        cb(f"map:meta_candidates:{select_code}:0")
        cb(f"map:meta_providers:{select_code}:1")
        provider_code = ui.register_code("models-provider:provider-b")
        cb(f"map:meta_catalog:{select_code}:{provider_code}:1")
        target_code = ui.register_code("models-target:provider-b/model-02")
        cb(f"map:meta_save:{select_code}:{target_code}")
        detail_code = mapping_menu._binding_tag(scope="oauth:openai:acct@example.test", model="model-02",
                                                outbound="upstream-02", flow="detail", view="s", page=0)
        cb(f"map:meta_item:{detail_code}"); cb(f"map:meta_del:{detail_code}")
        cb(f"map:meta_item:{detail_code}"); cb(f"map:meta_del_ok:{detail_code}")
    elif scenario == "catalog-search":
        select_code = mapping_menu._binding_tag(scope=None, model="model-01", flow="detail",
                                                view="d", page=0, candidate_page=0)
        cb(f"map:meta_search:{select_code}")
        cb(f"map:meta_candidates:{select_code}:0")  # back clears search state
        cb(f"map:meta_search:{select_code}")
        text(f"meta_catalog_search:{select_code}", "  ")
        text(f"meta_catalog_search:{select_code}", "Provider B model-01")
        query_code = ui.register_code("models-query:Provider B model-01")
        cb(f"map:meta_search_results:{select_code}:{query_code}:0")
        states.set_state(42, f"meta_catalog_search:{select_code}")
        text(f"meta_catalog_search:{select_code}", "x" * 81)
    elif scenario == "sync-success-running-failure-unmatched":
        monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", lambda: True)
        monkeypatch.setattr(model_pricing, "reload_local_catalog", lambda: events.append({"event": "catalog_reload"}))
        monkeypatch.setattr(model_metadata, "auto_sync_metadata", lambda: {
            "scanned": 4, "created": ["model-08"], "updated": ["model-02"],
            "unchanged": ["model-01"], "unmatched": ["mystery-模型"], "success": 2,
        })
        monkeypatch.setattr(mapping_menu, "_start_metadata_sync_worker", lambda target: target())
        cb("map:meta_sync")
        result = {"scanned": 4, "created": ["model-08"], "updated": ["model-02"],
                  "unchanged": ["model-01"], "unmatched": ["mystery-模型"], "catalog": "updated"}
        result_code = mapping_menu._sync_result_tag(result)
        cb(f"map:meta_sync_result:{result_code}")
        cb(f"map:meta_sync_list:{result_code}:a:0")
        unmatched_code = ui.register_code("metadata-unmatched:mystery-模型")
        cb(f"map:meta_unmatched:{unmatched_code}")
        mapping_menu._METADATA_SYNC_RUNNING = True
        cb("map:meta_sync")
        mapping_menu._METADATA_SYNC_RUNNING = False
        monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", lambda: (_ for _ in ()).throw(RuntimeError("fake remote failure")))
        cb("map:meta_sync")  # remote failure falls back to the saved local catalog
        monkeypatch.setattr(mapping_menu, "_perform_metadata_sync", lambda: (_ for _ in ()).throw(RuntimeError("fake sync failure")))
        cb("map:meta_sync")
    elif scenario == "compression-and-expiry":
        cb("map:compact:0"); cb("map:compact:1")
        code = ui.register_code("compact-model:model-08")
        cb(f"map:compact_pick:{code}:1"); cb("map:compact_clear:1"); cb("map:compact_clear:bad")
        cb("map:compact_pick:deadbeef:0")
    elif scenario == "invalid-callbacks-and-states":
        for data in ("map:inventory", "map:meta_inventory", "map:meta_models:deadbeef:0", "map:meta_candidates:deadbeef:0",
                     "map:meta_search:deadbeef", "map:meta_search_results:deadbeef:deadbeef:0",
                     "map:meta_providers:deadbeef:0", "map:meta_catalog:deadbeef:deadbeef:0",
                     "map:meta_save:deadbeef:deadbeef", "map:meta_item:deadbeef",
                     "map:meta_del:deadbeef", "map:meta_del_ok:deadbeef",
                     "map:meta_sync_result:deadbeef", "map:meta_sync_list:deadbeef:a:0",
                     "map:meta_unmatched:deadbeef"):
            cb(data)
        select_code = mapping_menu._binding_tag(scope=None, model="model-01", flow="detail")
        missing_target = ui.register_code("models-target:missing-provider/missing-model")
        cb(f"map:meta_save:{select_code}:{missing_target}")
        text("meta_catalog_search:deadbeef", "query")
        state_actions.append("other")
        handled = mapping_menu.handle_text_state(42, "other", "x")
        steps.append({"afterText": "other", "input": "x", "handled": handled, "state": _state_snapshot()})
    else:
        raise AssertionError(scenario)

    capability = case_name.split(".", 1)[0]
    return {
        "caseId": case_name,
        "capabilityId": capability,
        "entry": {"scenario": scenario, "callbacks": callbacks, "stateActions": state_actions},
        "initialConfig": _base_config(),
        "initialState": {},
        "initialRuntime": {"clock": 1000.0, "sendMessageId": 501,
                           "transport": "fake", "registry": "fake", "metadataCatalog": "fake"},
        "tgApi": calls,
        "stateSteps": steps,
        "finalBusinessState": {"config": deepcopy(cfg), "events": events,
                               "shortCodes": dict(sorted(ui._code_to_name.items())),
                               "state": _state_snapshot()},
        "expectedException": exception,
    }


def _segment_cases() -> list[dict[str, Any]]:
    if not SEGMENT.exists():
        return []
    return load_current_jsonl(SEGMENT)


CASES = _segment_cases()
MAP_EXPECTED = {case["caseId"]: case for case in CASES if case["capabilityId"].startswith("TG-MAP-")}


@pytest.mark.parametrize("case_name", MAP_CASE_NAMES)
def test_mapping_and_metadata_strict_trace(case_name, monkeypatch):
    assert case_name in MAP_EXPECTED, f"missing fixture case {case_name}"
    actual = _actual(case_name, monkeypatch)
    assert_strict_equal(MAP_EXPECTED[case_name], actual)


def _mapping_source_actions() -> set[str]:
    tree = ast.parse(Path(mapping_menu.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    in_handler = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "handle_callback":
            in_handler = True
            for child in ast.walk(node):
                if (isinstance(child, ast.Compare) and isinstance(child.left, ast.Name)
                        and child.left.id == "action"):
                    for comparator in child.comparators:
                        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                            found.add(comparator.value)
            break
    assert in_handler
    return found


def test_model_routing_segment_schema_ids_callbacks_states_and_no_mauth():
    assert_capability_coverage(CAPABILITIES, CASES)
    assert len({case["caseId"] for case in CASES}) == len(CASES)
    assert set(MAP_EXPECTED) == set(MAP_CASE_NAMES)
    invoked = {
        callback.split(":", 2)[1]
        for case in MAP_EXPECTED.values() for callback in case["entry"]["callbacks"]
        if callback.startswith("map:") and len(callback.split(":", 2)) > 1
    }
    # API-spec inventory has no dedicated v0.31.13 TG callback; scope pages consume
    # inventory internally. Empty/unknown/inventory actions freeze real negatives.
    assert invoked == _mapping_source_actions() | {"", "unknown", "inventory", "meta_inventory"}
    state_families = {
        action for case in MAP_EXPECTED.values() for action in case["entry"]["stateActions"]
    }
    assert state_families == {"map_alias_input:", "map_alias_edit:", "meta_catalog_search:", "other"}
    raw = SEGMENT.read_text(encoding="utf-8")
    assert "mauth:" not in raw
