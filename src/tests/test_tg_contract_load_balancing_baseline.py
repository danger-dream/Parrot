"""Strict executable v0.31.13 traces for TG-LB-01."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src import affinity, config, load_balancing, model_mapping
from src.channel import registry
from src.telegram import states, ui
from src.telegram.menus import load_balancing_menu as menu
from src.tests.tg_contract import assert_strict_equal, load_jsonl


from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/model_routing.jsonl"
CASE_NAMES = (
    "TG-LB-01.modes-success-and-failure",
    "TG-LB-01.channel-order-move-reset-save-cancel-text",
    "TG-LB-01.model-order-pagination-clear-expiry",
    "TG-LB-01.bulk-model-order-save-cancel-invalid",
    "TG-LB-01.affinity-confirm-exec-cancel-invalid",
)


class FakeChannel:
    def __init__(self, key: str, models: list[str], *, kind="api", protocol="anthropic",
                 enabled=True, reason=None, provider=""):
        self.key = key
        self.models = list(models)
        self.type = kind
        self.protocol = protocol
        self.enabled = enabled
        self.disabled_reason = reason
        self.provider = provider
        self.display_name = key.split(":", 1)[-1]

    def list_client_models(self):
        return list(self.models)

    def supports_model(self, model):
        return model if model in self.models else None


def _base_config() -> dict[str, Any]:
    return {
        "channelSelection": "smart",
        "loadBalancing": {
            "initialized": True,
            "channelPriorityOrder": ["api:beta", "api:alpha", "oauth:openai:acct@example.test"],
            "modelPriorityOrders": {"model-01": ["api:alpha", "api:beta"]},
            "priorityOrders": {"anthropic": ["api:alpha"], "openai": ["api:beta"]},
        },
        "oauthAccounts": [{"provider": "openai", "email": "acct@example.test", "enabled": True}],
    }


def _channels() -> list[FakeChannel]:
    all_models = [f"model-{i:02d}" for i in range(1, 9)]
    return [
        FakeChannel("api:alpha", all_models, protocol="anthropic"),
        FakeChannel("api:beta", all_models[:7], protocol="openai-chat", enabled=False, reason="user"),
        FakeChannel("oauth:openai:acct@example.test", all_models[1:], kind="oauth",
                    protocol="openai-responses", provider="openai", reason="quota"),
    ]


def _setup(monkeypatch: pytest.MonkeyPatch):
    cfg = _base_config()
    channels = _channels()
    calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    affinity_state = {"fp": 3, "client": 2}

    def update(mutator, **_kwargs):
        before = deepcopy(cfg)
        mutator(cfg)
        if before != cfg:
            events.append({"event": "config_update", "config": deepcopy(cfg)})
        return deepcopy(cfg)

    def api(method, data=None):
        calls.append({"method": method, "payload": deepcopy(data or {})})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(states.time, "time", lambda: 2000.0)
    monkeypatch.setattr(config, "get", lambda: cfg)
    monkeypatch.setattr(config, "update", update)
    monkeypatch.setattr(registry, "all_channels", lambda: list(channels))
    monkeypatch.setattr(registry, "get_channel", lambda key: next((c for c in channels if c.key == key), None))
    monkeypatch.setattr(model_mapping, "get_ingress_map", lambda _line: {"client-alias": "model-08"})
    monkeypatch.setattr(affinity, "count", lambda: affinity_state["fp"])
    monkeypatch.setattr(affinity, "client_count", lambda: affinity_state["client"])

    def delete_all():
        events.append({"event": "affinity_delete_all", "count": affinity_state["fp"]})
        affinity_state["fp"] = 0

    def client_delete_all():
        events.append({"event": "client_affinity_delete_all", "count": affinity_state["client"]})
        affinity_state["client"] = 0

    def delete_family(family):
        events.append({"event": "affinity_delete_family", "family": family, "count": 4})
        return 4

    def client_delete_family(family):
        events.append({"event": "client_affinity_delete_family", "family": family, "count": 5})
        return 5

    monkeypatch.setattr(affinity, "delete_all", delete_all)
    monkeypatch.setattr(affinity, "client_delete_all", client_delete_all)
    monkeypatch.setattr(affinity, "delete_by_protocol", delete_family)
    monkeypatch.setattr(affinity, "client_delete_by_protocol", client_delete_family)
    monkeypatch.setattr(ui, "api", api)
    states.clear_all(); ui._code_to_name.clear()
    return cfg, calls, events, affinity_state


def _actual(case_name: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg, calls, events, affinity_state = _setup(monkeypatch)
    callbacks: list[str] = []
    state_actions: list[str] = []
    steps: list[dict[str, Any]] = []

    def snap():
        return deepcopy(states.get_state(42))

    def cb(data: str):
        callbacks.append(data)
        handled = menu.handle_callback(42, 77, "cb-lb", data)
        steps.append({"after": data, "handled": handled, "state": snap()})
        return handled

    def text(action: str, value: str):
        state_actions.append(action)
        handled = menu.handle_text_state(42, action, value)
        steps.append({"afterText": action, "input": value, "handled": handled, "state": snap()})
        return handled

    scenario = case_name.split(".", 1)[1]
    if scenario == "modes-success-and-failure":
        cb("menu:loadbalancing"); cb("lb:mode:order"); cb("lb:mode:priority")
        cb("lb:mode:smart"); cb("lb:mode:not-a-mode")
        original = load_balancing.set_mode
        monkeypatch.setattr(load_balancing, "set_mode", lambda _mode: (_ for _ in ()).throw(RuntimeError("fake config failure")))
        cb("lb:mode:priority")
        monkeypatch.setattr(load_balancing, "set_mode", original)
    elif scenario == "channel-order-move-reset-save-cancel-text":
        cb("lb:channels"); cb("lb:sel:bad"); cb("lb:sel:9"); cb("lb:sel:2")
        cb("lb:mv:top"); cb("lb:mv:bottom"); cb("lb:mv:up"); cb("lb:mv:down")
        cb("lb:reset"); cb("lb:mv:sideways"); cb("lb:order_input")
        for value in ("", "1,no", "0,1,2", "1,1,2", "1,2"):
            text("lb_order_input", value)
        text("lb_order_input", "3，1;2")
        cb("lb:order_cancel"); cb("lb:save"); cb("lb:fam:anthropic"); cb("lb:cancel")
        state_actions.append("other")
        handled = menu.handle_text_state(42, "other", "1")
        steps.append({"afterText": "other", "input": "1", "handled": handled, "state": snap()})
    elif scenario == "model-order-pagination-clear-expiry":
        cb("lb:models:bad"); cb("lb:models:2")
        model_code = menu._model_code("model-01")
        cb(f"lb:model:{model_code}:2"); cb("lb:model_clear"); cb("lb:cancel")
        cb("lb:model:deadbeef:bad")
        states._states[42] = {"action": "lb_edit", "data": {"kind": "channels", "draft": ["api:alpha"]}, "ts": 1000.0}
        cb("lb:sel:1"); cb("lb:reset"); cb("lb:save"); cb("lb:model_clear")
    elif scenario == "bulk-model-order-save-cancel-invalid":
        cb("lb:model_bulk"); cb("lb:model_bulk_confirm"); cb("lb:model_pick:deadbeef")
        code1 = menu._model_code("model-01"); code2 = menu._model_code("model-02")
        cb(f"lb:model_pick:{code1}"); cb(f"lb:model_pick:{code2}")
        cb("lb:model_bulk_confirm"); cb("lb:sel:3"); cb("lb:mv:top"); cb("lb:save")
        cb("lb:model_bulk"); cb(f"lb:model_pick:{code1}"); cb("lb:model_bulk_cancel")
        cb("lb:bulk"); cb("lb:bulk_cancel")
    elif scenario == "affinity-confirm-exec-cancel-invalid":
        cfg["channelSelection"] = "priority"
        cb("lb:aff_all"); cb("menu:loadbalancing"); cb("lb:aff_all"); cb("lb:aff_all_exec")
        cb("lb:aff_fam:anthropic"); cb("menu:loadbalancing")
        cb("lb:aff_fam:openai"); cb("lb:aff_fam_exec:openai")
        cb("lb:aff_fam:bad"); cb("lb:aff_fam_exec:bad")
        callbacks.append("other:callback")
        handled = menu.handle_callback(42, 77, "cb-lb", "other:callback")
        steps.append({"after": "other:callback", "handled": handled, "state": snap()})
    else:
        raise AssertionError(scenario)

    return {
        "caseId": case_name, "capabilityId": "TG-LB-01",
        "entry": {"scenario": scenario, "callbacks": callbacks, "stateActions": state_actions},
        "initialConfig": _base_config(), "initialState": {},
        "initialRuntime": {"clock": 2000.0, "transport": "fake", "registry": "fake",
                           "affinity": {"fp": 3, "client": 2}},
        "tgApi": calls, "stateSteps": steps,
        "finalBusinessState": {"config": deepcopy(cfg), "events": events,
                               "affinity": deepcopy(affinity_state), "state": snap(),
                               "shortCodes": dict(sorted(ui._code_to_name.items()))},
        "expectedException": None,
    }


def _cases():
    return load_current_jsonl(SEGMENT) if SEGMENT.exists() else []


EXPECTED = {case["caseId"]: case for case in _cases() if case["capabilityId"] == "TG-LB-01"}


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_load_balancing_strict_trace(case_name, monkeypatch):
    assert case_name in EXPECTED, f"missing fixture case {case_name}"
    assert_strict_equal(EXPECTED[case_name], _actual(case_name, monkeypatch))


def _normalize_callback(value: str) -> str:
    if value == "menu:loadbalancing" or not value.startswith("lb:"):
        return value
    exact = {"lb:channels", "lb:model_bulk", "lb:model_bulk_confirm", "lb:model_bulk_cancel",
             "lb:reset", "lb:save", "lb:model_clear", "lb:cancel", "lb:order_input",
             "lb:bulk", "lb:order_cancel", "lb:bulk_cancel", "lb:aff_all", "lb:aff_all_exec"}
    if value in exact:
        return value
    for prefix in ("lb:aff_fam_exec:", "lb:aff_fam:", "lb:model_pick:", "lb:models:",
                   "lb:model:", "lb:mode:", "lb:fam:", "lb:sel:", "lb:mv:"):
        if value.startswith(prefix):
            return prefix
    return value


def _source_callback_families() -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handle_callback")
    found: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "data":
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    found.add(comparator.value)
                elif isinstance(comparator, (ast.Set, ast.Tuple)):
                    found.update(e.value for e in comparator.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return found


def test_lb_callback_and_state_family_bidirectional_coverage():
    assert set(EXPECTED) == set(CASE_NAMES)
    invoked = {_normalize_callback(value) for case in EXPECTED.values() for value in case["entry"]["callbacks"]}
    assert invoked == _source_callback_families() | {"other:callback"}
    state_actions = {value for case in EXPECTED.values() for value in case["entry"]["stateActions"]}
    assert state_actions == {"lb_order_input", "other"}
    produced_states = {
        step["state"]["action"] for case in EXPECTED.values() for step in case["stateSteps"]
        if step.get("state")
    }
    assert produced_states == {"lb_edit", "lb_order_input", "lb_model_select"}
    assert all("mauth:" not in value for case in EXPECTED.values() for value in case["entry"]["callbacks"])
