"""Strict executable v0.31.13 traces for TG-PX-01 and TG-PX-02."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src import config
from src.channel import registry
from src.proxy import manager as pm
from src.telegram import states, ui
from src.telegram.menus import proxy_menu as menu
from src.tests.tg_contract import assert_strict_equal, load_jsonl


from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/model_routing.jsonl"
CASE_NAMES = (
    "TG-PX-01.list-mask-pagination-view-expiry",
    "TG-PX-01.add-states-validation-test-success-failure",
    "TG-PX-01.test-delete-cancel-cascade",
    "TG-PX-01.business-exception-propagates",
    "TG-PX-02.groups-member-picker-save-edit",
    "TG-PX-02.group-test-delete-cancel-cascade",
    "TG-PX-02.routing-default-functions-account-channel-model",
    "TG-PX-02.routing-state-expiry-invalid",
    "TG-PX-02.routing-business-exception-propagates",
)


class FakeConnector:
    def __init__(self, name: str, kind: str, *, url="", attempts=0, success=0, attempt=0,
                 server="", port=0, cipher=""):
        self.name = name; self.type = kind; self.url = url
        self.server = server; self.port = port; self.cipher = cipher
        self.stats = SimpleNamespace(total_attempts=attempts, last_success_ts=success,
                                     last_attempt_ts=attempt)

    def display(self):
        return self.url or f"{self.server}:{self.port}"


class FakeChannel:
    def __init__(self, key: str, kind: str, name: str, *, provider="", protocol="anthropic"):
        self.key = key; self.type = kind; self.display_name = name
        self.provider = provider; self.protocol = protocol


def _base_config() -> dict[str, Any]:
    return {"oauthAccounts": [{"provider": "openai", "email": "user@example.test"}]}


def _connector_specs() -> list[dict[str, Any]]:
    return [
        {"name": "direct", "kind": "direct"},
        {"name": "proxy-a", "kind": "socks5", "url": "socks5://fake-user:fake-pass@proxy-a.invalid:1080",
         "attempts": 3, "success": 100, "attempt": 100},
        {"name": "proxy-b", "kind": "ss2022", "server": "proxy-b.invalid", "port": 443,
         "cipher": "2022-blake3-aes-256-gcm", "attempts": 2, "success": 90, "attempt": 100},
        {"name": "proxy-c", "kind": "socks5", "url": "socks5://proxy-c.invalid:1080"},
        {"name": "proxy-d", "kind": "socks5", "url": "socks5://proxy-d.invalid:1080"},
        {"name": "proxy-e", "kind": "socks5", "url": "socks5://proxy-e.invalid:1080"},
        {"name": "proxy-f", "kind": "socks5", "url": "socks5://proxy-f.invalid:1080"},
    ]


def _base_groups() -> dict[str, list[str]]:
    return {
        "group-a": ["proxy-a", "proxy-b"], "group-b": ["proxy-c"],
        "group-c": ["proxy-d"], "group-d": ["proxy-e"],
        "group-e": ["proxy-f"], "group-f": ["proxy-a", "direct"],
    }


def _base_routing() -> dict[str, Any]:
    return {
        "default": "proxy-a", "directFallback": False, "telegram": "group-a",
        "oauth_openai": "proxy-b", "accounts": {"oauth:openai:user@example.test": "proxy-a"},
        "channels": {"api:channel-one": "group-a"}, "models": {"model-01": "proxy-b"},
    }


def _setup(monkeypatch: pytest.MonkeyPatch):
    cfg = _base_config()
    connectors = {
        spec["name"]: FakeConnector(**spec) for spec in _connector_specs()
    }
    groups = _base_groups()
    routing = _base_routing()
    calls: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    channels = [
        FakeChannel("oauth:openai:user@example.test", "oauth", "OpenAI User",
                    provider="openai", protocol="openai-responses"),
        FakeChannel("api:channel-one", "api", "Channel <One>", protocol="anthropic"),
    ]

    def api(method, data=None):
        calls.append({"method": method, "payload": deepcopy(data or {})})
        result = {"message_id": 601} if method == "sendMessage" else {}
        return {"ok": True, "result": result}

    def add_proxy(name, proxy_cfg):
        events.append({"event": "add_proxy", "name": name, "proxy": deepcopy(proxy_cfg)})
        kind = proxy_cfg.get("type", "socks5")
        connectors[name] = FakeConnector(
            name, kind, url=proxy_cfg.get("url", ""), server=proxy_cfg.get("server", ""),
            port=proxy_cfg.get("port", 0), cipher=proxy_cfg.get("cipher", ""),
        )

    def remove_refs(target):
        for members in groups.values():
            members[:] = [member for member in members if member != target]
        for key in list(routing):
            value = routing[key]
            if isinstance(value, dict):
                for child in list(value):
                    if value[child] == target:
                        value.pop(child)
            elif value == target:
                routing.pop(key)

    def remove_proxy(name):
        events.append({"event": "remove_proxy", "name": name})
        connectors.pop(name, None); remove_refs(name)

    def add_group(name, members):
        events.append({"event": "add_group", "name": name, "members": list(members)})
        groups[name] = list(members)

    def remove_group(name):
        events.append({"event": "remove_group", "name": name})
        groups.pop(name, None); remove_refs(name)

    def set_routing(key, value, *, section=""):
        events.append({"event": "set_routing", "key": key, "value": value, "section": section})
        if value not in connectors and value not in groups:
            raise ValueError("fake unknown routing target")
        (routing.setdefault(section, {}) if section else routing).__setitem__(key, value)

    def remove_routing(key, *, section=""):
        events.append({"event": "remove_routing", "key": key, "section": section})
        (routing.setdefault(section, {}) if section else routing).pop(key, None)

    async def test_proxy(name, timeout=10):
        events.append({"event": "test_proxy", "name": name, "timeout": timeout})
        if name == "proxy-explode":
            raise RuntimeError("fake transport exploded")
        if name in {"proxy-b", "bad-added"}:
            return {"ok": False, "error": "fake connect <failed>"}
        return {"ok": True, "ip": "203.0.113.9", "latency_ms": 37}

    async def test_group(name, timeout=10):
        events.append({"event": "test_group", "name": name, "timeout": timeout})
        if name == "group-explode":
            raise RuntimeError("fake group exploded")
        return [
            {"name": "proxy-a", "ok": True, "ip": "203.0.113.9", "latency_ms": 37},
            {"name": "proxy-b", "ok": False, "error": "fake member failed <x>"},
        ]

    monkeypatch.setattr(states.time, "time", lambda: 3000.0)
    monkeypatch.setattr(config, "get", lambda: cfg)
    monkeypatch.setattr(pm, "init", lambda: events.append({"event": "proxy_init"}))
    monkeypatch.setattr(pm, "all_connectors", lambda: connectors)
    monkeypatch.setattr(pm, "all_groups", lambda: groups)
    monkeypatch.setattr(pm, "get_group", lambda name: deepcopy(groups.get(name)))
    monkeypatch.setattr(pm, "get_routing", lambda: deepcopy(routing))
    monkeypatch.setattr(pm, "direct_fallback_enabled", lambda: bool(routing.get("directFallback", False)))
    monkeypatch.setattr(pm, "set_direct_fallback", lambda enabled: (
        events.append({"event": "set_direct_fallback", "enabled": bool(enabled)}),
        routing.__setitem__("directFallback", bool(enabled)),
    )[-1])
    monkeypatch.setattr(pm, "add_proxy", add_proxy)
    monkeypatch.setattr(pm, "remove_proxy", remove_proxy)
    monkeypatch.setattr(pm, "add_group", add_group)
    monkeypatch.setattr(pm, "remove_group", remove_group)
    monkeypatch.setattr(pm, "set_routing", set_routing)
    monkeypatch.setattr(pm, "remove_routing", remove_routing)
    monkeypatch.setattr(pm, "test_proxy", test_proxy)
    monkeypatch.setattr(pm, "test_group", test_group)
    monkeypatch.setattr(registry, "all_channels", lambda: list(channels))
    monkeypatch.setattr(registry, "available_models", lambda: [f"model-{i:02d}" for i in range(1, 11)])
    monkeypatch.setattr(menu, "_get_proxy_stats", lambda: {
        "proxy-a": {"proxy_name": "proxy-a", "requests": 4, "successes": 3, "failures": 1,
                    "input_tokens": 1000, "output_tokens": 500, "cache_creation_tokens": 100,
                    "cache_read_tokens": 50, "total_tokens": 1650, "bytes_up": 1024,
                    "bytes_down": 2048, "total_bytes": 3072, "connect_sum_ms": 80,
                    "connect_sample_count": 4, "first_byte_sum_ms": 160,
                    "first_byte_sample_count": 4, "idle_sum_ms": 0, "idle_sample_count": 0,
                    "total_sum_ms": 800, "total_sample_count": 4, "avg_connect_ms": 20,
                    "avg_first_byte_ms": 40, "avg_idle_ms": 0, "avg_total_ms": 200},
    })
    monkeypatch.setattr(ui, "api", api)
    states.clear_all(); ui._code_to_name.clear(); menu._item_index.clear()
    return cfg, connectors, groups, routing, calls, events


def _snapshot_manager(connectors, groups, routing):
    return {
        "connectors": [{"name": name, "type": conn.type, "display": conn.display()}
                       for name, conn in connectors.items()],
        "groups": deepcopy(groups), "routing": deepcopy(routing),
    }


def _actual(case_name: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg, connectors, groups, routing, calls, events = _setup(monkeypatch)
    callbacks: list[str] = []
    state_actions: list[str] = []
    steps: list[dict[str, Any]] = []
    expected_exception = None

    def snap():
        return deepcopy(states.get_state(42))

    def cb(data: str):
        callbacks.append(data)
        handled = menu.handle_callback(42, 77, "cb-px", data)
        steps.append({"after": data, "handled": handled, "state": snap()})
        return handled

    def text(action: str, value: str):
        state_actions.append(action)
        handled = menu.handle_text_state(42, action, value)
        steps.append({"afterText": action, "input": value, "handled": handled, "state": snap()})
        return handled

    scenario = case_name.split(".", 1)[1]
    if scenario == "list-mask-pagination-view-expiry":
        cb("px:show"); cb("px:noop"); cb("px:page:2"); cb("px:page:bad")
        code = ui.register_code("proxy-a")
        cb(f"px:view:{code}:1"); cb("px:view:deadbeef:2")
        cb("px:edit:proxy-a"); cb("px:update:proxy-a")  # no v0.31.13 update entry
    elif scenario == "add-states-validation-test-success-failure":
        cb("px:add"); cb("px:show")  # cancel/back keeps the pending text state
        cb("px:add")
        text("px_add_url", "not-a-proxy")
        text("px_add_url", "socks5://fake-user:fake-pass@new-proxy.invalid:1080")
        text("px_add_name", "Bad Name"); text("px_add_name", "direct")
        text("px_add_name", "proxy-a"); text("px_add_name", "new-proxy")
        states.set_state(42, "px_add_name", {})
        text("px_add_name", "expired-name")
        states.set_state(42, "px_add_url")
        text("px_add_url", "socks5://bad-added.invalid:1080#bad-added")
    elif scenario == "test-delete-cancel-cascade":
        cb("px:test:proxy-a"); cb("px:test:proxy-b"); cb("px:test:proxy-explode")
        code = ui.register_code("proxy-a")
        cb(f"px:testv:{code}:1"); cb("px:testv:deadbeef:2")
        cb("px:del_confirm:proxy-a"); cb(f"px:view:{code}:1")
        cb(f"px:del_confirm_v:{code}:1"); cb("px:del_confirm_v:deadbeef:2")
        cb("px:del_exec:proxy-b")
        code_b = ui.register_code("proxy-c")
        cb(f"px:del_exec_v:{code_b}:1"); cb("px:del_exec_v:deadbeef:2")
    elif scenario == "business-exception-propagates":
        monkeypatch.setattr(pm, "remove_proxy", lambda _name: (_ for _ in ()).throw(RuntimeError("fake remove failure")))
        callbacks.append("px:del_exec:proxy-a")
        try:
            menu.handle_callback(42, 77, "cb-px", callbacks[-1])
        except Exception as exc:
            expected_exception = {"type": type(exc).__name__, "message": str(exc)}
        steps.append({"after": callbacks[-1], "handled": None, "state": snap()})
    elif scenario == "groups-member-picker-save-edit":
        cb("px:groups"); cb("px:grp_noop"); cb("px:grp_page:2"); cb("px:grp_page:bad")
        code = ui.register_code("group-a")
        cb(f"px:grp_view:{code}:1"); cb("px:grp_view:deadbeef:2")
        cb("px:grp_add"); cb("px:groups")  # cancel/back keeps the pending text state
        cb("px:grp_add"); text("px_grp_add_name", "Bad Name")
        text("px_grp_add_name", "group-a"); text("px_grp_add_name", "group-new")
        cb("px:grp_save"); cb("px:grp_pick:proxy-a"); cb("px:grp_pick:proxy-b")
        cb("px:grp_rm:proxy-a"); cb("px:grp_clear"); cb("px:grp_pick:proxy-c"); cb("px:grp_save")
        cb("px:grp_edit:group-new")
        new_code = ui.register_code("group-new")
        cb(f"px:grp_edit_v:{new_code}:1"); cb("px:grp_edit_v:deadbeef:2")
    elif scenario == "group-test-delete-cancel-cascade":
        cb("px:grp_test:group-a"); cb("px:grp_test:group-explode")
        code = ui.register_code("group-a")
        cb(f"px:grp_test_v:{code}:1"); cb("px:grp_test_v:deadbeef:2")
        cb(f"px:grp_del_ask:{code}:1"); cb(f"px:grp_view:{code}:1")
        cb("px:grp_del_ask:deadbeef:2")
        cb("px:grp_del:group-b")
        code_c = ui.register_code("group-a")
        cb(f"px:grp_del_exec:{code_c}:1"); cb("px:grp_del_exec:deadbeef:2")
    elif scenario == "routing-default-functions-account-channel-model":
        cb("px:routing"); cb("px:rt_df"); cb("px:rt_func")
        cb("px:rt_pick:default"); cb("px:rt_do:default:group-a")
        cb("px:rt_pick:telegram"); cb("px:rt_do:telegram:__del__")
        cb("px:rt_accounts"); cb("px:rt_item:a:0"); cb("px:rt_accounts")
        cb("px:rt_item:a:0"); cb("px:rt_s:proxy-b")
        cb("px:rt_channels"); cb("px:rt_item:c:0"); cb("px:rt_s:__del__")
        cb("px:rt_models"); cb("px:rt_mp:2"); cb("px:rt_item:m:8"); cb("px:rt_s:group-f")
    elif scenario == "routing-state-expiry-invalid":
        cb("px:rt_mp:bad"); cb("px:rt_item:m:bad"); cb("px:rt_item:m:999")
        cb("px:rt_pick:oauth_openai")
        cb("px:rt_do:oauth_openai:direct")
        states._states[42] = {"action": "px_rt_pending", "data": {
            "context": "models:model-01", "back": "px:rt_models"}, "ts": 2000.0}
        cb("px:rt_s:proxy-a")
        cb("px:rt_s:proxy-a")
        callbacks.append("px:unknown")
        handled = menu.handle_callback(42, 77, "cb-px", "px:unknown")
        steps.append({"after": "px:unknown", "handled": handled, "state": snap()})
        state_actions.append("other")
        handled = menu.handle_text_state(42, "other", "x")
        steps.append({"afterText": "other", "input": "x", "handled": handled, "state": snap()})
    elif scenario == "routing-business-exception-propagates":
        monkeypatch.setattr(pm, "set_routing", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake route save failure")))
        callbacks.append("px:rt_do:default:proxy-a")
        try:
            menu.handle_callback(42, 77, "cb-px", callbacks[-1])
        except Exception as exc:
            expected_exception = {"type": type(exc).__name__, "message": str(exc)}
        steps.append({"after": callbacks[-1], "handled": None, "state": snap()})
    else:
        raise AssertionError(scenario)

    capability = case_name.split(".", 1)[0]
    return {
        "caseId": case_name, "capabilityId": capability,
        "entry": {"scenario": scenario, "callbacks": callbacks, "stateActions": state_actions},
        "initialConfig": {"telegram": _base_config(), "proxyManager": {
            "connectors": _connector_specs(), "groups": _base_groups(), "routing": _base_routing()}},
        "initialState": {},
        "initialRuntime": {"clock": 3000.0, "sendMessageId": 601, "transport": "fake",
                           "registry": "fake", "proxyManager": "fake"},
        "tgApi": calls, "stateSteps": steps,
        "finalBusinessState": {"manager": _snapshot_manager(connectors, groups, routing),
                               "events": events, "state": snap(),
                               "routeIndex": {str(key): deepcopy(value) for key, value in menu._item_index.items()},
                               "shortCodes": dict(sorted(ui._code_to_name.items()))},
        "expectedException": expected_exception,
    }


def _cases():
    return load_current_jsonl(SEGMENT) if SEGMENT.exists() else []


EXPECTED = {case["caseId"]: case for case in _cases() if case["capabilityId"].startswith("TG-PX-")}


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_proxy_and_routing_strict_trace(case_name, monkeypatch):
    assert case_name in EXPECTED, f"missing fixture case {case_name}"
    assert_strict_equal(EXPECTED[case_name], _actual(case_name, monkeypatch))


def _normalize_callback(value: str) -> str:
    exact = {"px:show", "px:noop", "px:add", "px:groups", "px:grp_noop", "px:grp_add",
             "px:grp_clear", "px:grp_save", "px:routing", "px:rt_df", "px:rt_func",
             "px:rt_accounts", "px:rt_channels", "px:rt_models"}
    if value in exact:
        return value
    for prefix in ("px:del_confirm_v:", "px:del_confirm:", "px:del_exec_v:", "px:del_exec:",
                   "px:grp_del_exec:", "px:grp_del_ask:", "px:grp_edit_v:", "px:grp_test_v:",
                   "px:grp_view:", "px:grp_page:", "px:grp_edit:", "px:grp_test:",
                   "px:grp_pick:", "px:grp_rm:", "px:grp_del:", "px:testv:", "px:test:",
                   "px:page:", "px:view:", "px:rt_accounts", "px:rt_channels", "px:rt_models",
                   "px:rt_item:", "px:rt_pick:", "px:rt_s:", "px:rt_do:", "px:rt_mp:"):
        if value.startswith(prefix):
            return prefix
    return value


def _source_callback_families() -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handle_callback")
    found: set[str] = set()
    for node in ast.walk(handler):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "data"):
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "data":
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    found.add(comparator.value)
    return found


def test_proxy_callback_and_state_family_bidirectional_coverage():
    assert set(EXPECTED) == set(CASE_NAMES)
    invoked = {_normalize_callback(value) for case in EXPECTED.values() for value in case["entry"]["callbacks"]}
    # v0.31.13 has no proxy rename/URL-update callback; freeze the real negative dispatch.
    assert invoked == _source_callback_families() | {"px:edit:proxy-a", "px:update:proxy-a", "px:unknown"}
    state_actions = {value for case in EXPECTED.values() for value in case["entry"]["stateActions"]}
    assert state_actions == {"px_add_url", "px_add_name", "px_grp_add_name", "other"}
    produced_states = {
        step["state"]["action"] for case in EXPECTED.values() for step in case["stateSteps"]
        if step.get("state")
    }
    assert produced_states == {"px_add_url", "px_add_name", "px_grp_add_name",
                               "px_grp_pick_members", "px_rt_pending"}
    assert all("mauth:" not in value for case in EXPECTED.values() for value in case["entry"]["callbacks"])
