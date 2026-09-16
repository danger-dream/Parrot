"""Executable strict v0.31.13 baseline for TG-STAT-01."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from src import config, status_monitor
from src.telegram import states, ui
from src.telegram.menus import status_alert_menu as menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl

from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/auxiliary.jsonl"
CASES = [c for c in load_current_jsonl(SEGMENT) if c["capabilityId"] == "TG-STAT-01"] if SEGMENT.exists() else []
BASE = {"statusMonitor": {"enabled": True, "intervalSeconds": 60, "targets": ["claude", "openai", "cloudflare"], "minImpact": "minor"}, "notifications": {"events": {"status_alert": True}}}

def _short(tag: str) -> str: return hashlib.sha1(tag.encode()).hexdigest()[:8]
def _cfg(**patch: Any) -> dict[str, Any]:
    out = deepcopy(BASE); out["statusMonitor"].update(patch); return out

def _spec(name: str, op: str, **entry: Any) -> dict[str, Any]:
    state = entry.pop("state", None); cfg = deepcopy(entry.pop("config", BASE)); runtime = deepcopy(entry.pop("runtime", {}))
    return {"caseId": f"TG-STAT-01.{name}", "capabilityId": "TG-STAT-01", "entry": {"op": op, **entry}, "initialConfig": cfg, "initialState": state or {}, "initialRuntime": runtime, "tgApi": [], "stateSteps": [], "finalBusinessState": {}, "expectedException": None}

MUTE = "stat_mute:claude:inc-1|Provider <down>"
UNMUTE = "stat_unmute:openai:inc:2"
ACTIVE = {"claude": [{"id": "inc-1", "name": "Provider <down>", "impact": "major", "status": "investigating", "shortlink": "https://fake.invalid/i/1"}], "openai": [], "cloudflare": []}
MUTED = [{"provider": "openai", "incident_id": "inc:2", "name": "API <issue>", "muted_at": 1700000000.0}]
SPECS = [
    _spec("back-preserves-interval-state", "callback", data="menu:status_alert", state={"action": "stat_interval", "data": {}}),
    _spec("show-active", "send_new", runtime={"active": ACTIVE, "muted": MUTED}),
    _spec("toggle-enabled", "callback", data="stat:toggle_enabled"),
    _spec("target-remove-claude", "callback", data="stat:toggle_tgt:claude"),
    _spec("target-add-openai", "callback", data="stat:toggle_tgt:openai", config=_cfg(targets=["claude"])),
    _spec("target-remove-cloudflare", "callback", data="stat:toggle_tgt:cloudflare"),
    _spec("target-unknown", "callback", data="stat:toggle_tgt:other"),
    _spec("interval-ask", "callback", data="stat:edit_interval"),
    _spec("interval-invalid", "text", action="stat_interval", text="9"),
    _spec("interval-save", "text", action="stat_interval", text="120"),
    _spec("impact-picker", "callback", data="stat:show_impact"),
    *[_spec(f"impact-{value}", "callback", data=f"stat:set_impact:{value}") for value in ("none", "minor", "major", "critical")],
    _spec("impact-invalid", "callback", data="stat:set_impact:severe"),
    _spec("refresh-first-and-push", "callback", data="stat:refresh", config=_cfg(targets=["claude", "openai"]), runtime={"initialized": ["openai"]}),
    _spec("refresh-failure", "callback", data="stat:refresh", config=_cfg(targets=["claude"]), runtime={"processFailure": "fake upstream <down>"}),
    _spec("history-mixed", "callback", data="stat:history", config=_cfg(targets=["claude", "openai"]), runtime={"recent": {"claude": [{"name": "Old <incident>", "impact": "critical", "status": "resolved", "created_at": "2026-01-02T03:04:05Z"}]}, "recentFailure": {"openai": "fake history failure"}}),
    _spec("history-empty", "callback", data="stat:history", config=_cfg(targets=["cloudflare"])),
    _spec("mute-success", "callback", data=f"stat:mute:{_short(MUTE)}", runtime={"register": [MUTE], "active": ACTIVE}),
    _spec("mute-expired", "callback", data="stat:mute:deadbeef"),
    _spec("mute-malformed", "callback", data=f"stat:mute:{_short('stat_mute::|bad')}", runtime={"register": ["stat_mute::|bad"]}),
    _spec("muted-empty", "callback", data="stat:muted_list"),
    _spec("muted-populated", "callback", data="stat:muted_list", runtime={"muted": MUTED, "displayTime": "11-14 22:13"}),
    _spec("unmute-success", "callback", data=f"stat:unmute:{_short(UNMUTE)}", runtime={"register": [UNMUTE], "muted": MUTED}),
    _spec("unmute-expired", "callback", data="stat:unmute:deadbeef"),
    _spec("unmute-malformed", "callback", data=f"stat:unmute:{_short('stat_unmute::')}", runtime={"register": ["stat_unmute::"]}),
    _spec("unknown-callback", "callback", data="stat:missing"),
    _spec("unknown-state", "text", action="stat_unknown", text="x"),
]
EXPECTED_CASE_IDS = {s["caseId"] for s in SPECS}
EXPECTED_CALLBACK_FAMILIES = {"menu:status_alert", "stat:toggle_enabled", "stat:toggle_tgt:*", "stat:edit_interval", "stat:show_impact", "stat:set_impact:*", "stat:refresh", "stat:history", "stat:mute:*", "stat:muted_list", "stat:unmute:*", "stat:missing"}
EXPECTED_STATES = {"stat_interval", "stat_unknown"}

def _callback_family(data: str) -> str:
    for prefix in ("stat:toggle_tgt:", "stat:set_impact:", "stat:mute:", "stat:unmute:"):
        if data.startswith(prefix): return prefix + "*"
    return data

def _source_families(function_name: str, variable: str) -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    found: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and node.args and isinstance(node.args[0], ast.Constant): found.add(node.args[0].value + "*")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == variable: found.update(x.value for x in node.comparators if isinstance(x, ast.Constant) and isinstance(x.value, str))
    return found

def _run(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = deepcopy(case["initialConfig"]); rt = case["initialRuntime"]; calls: list[dict[str, Any]] = []; events: list[Any] = []; muted = deepcopy(rt.get("muted", []))
    def api(method, data=None): calls.append({"method": method, "payload": deepcopy(data or {})}); return {"ok": True, "result": {"message_id": 901}}
    def update(mutator): events.append("config.update"); mutator(store); return store
    def process(provider, push=False):
        events.append(["process", provider, push])
        if rt.get("processFailure"): raise RuntimeError(rt["processFailure"])
    def recent(provider, limit=5):
        if provider in rt.get("recentFailure", {}): raise RuntimeError(rt["recentFailure"][provider])
        events.append(["recent", provider, limit]); return deepcopy(rt.get("recent", {}).get(provider, []))
    def mute(provider, iid, name=""):
        events.append(["mute", provider, iid, name]); muted.append({"provider": provider, "incident_id": iid, "name": name, "muted_at": 1700000000.0})
    def unmute(provider, iid): events.append(["unmute", provider, iid]); muted[:] = [r for r in muted if not (r["provider"] == provider and r["incident_id"] == iid)]
    states.clear_all(); ui._code_to_name.clear(); ui.configure("fake-token-stat", [42])
    monkeypatch.setattr(states.time, "time", lambda: 1000.0); monkeypatch.setattr(config, "get", lambda: store); monkeypatch.setattr(config, "update", update); monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(status_monitor, "snapshot_active", lambda: deepcopy(rt.get("active", {}))); monkeypatch.setattr(status_monitor, "list_muted", lambda: deepcopy(muted)); monkeypatch.setattr(status_monitor, "forget_provider", lambda provider: events.append(["forget", provider])); monkeypatch.setattr(status_monitor, "_process_provider", process); monkeypatch.setattr(status_monitor, "list_recent_incidents", recent); monkeypatch.setattr(status_monitor, "mute_incident", mute); monkeypatch.setattr(status_monitor, "unmute_incident", unmute); monkeypatch.setattr(status_monitor, "_initialized_providers", set(rt.get("initialized", [])))
    class FakeDateTime:
        @staticmethod
        def fromtimestamp(value): return SimpleNamespace(strftime=lambda fmt: rt.get("displayTime", "01-01 00:00"))
    monkeypatch.setitem(sys.modules, "datetime", SimpleNamespace(datetime=FakeDateTime))
    for tag in rt.get("register", []): ui.register_code(tag)
    initial = case["initialState"]
    if initial: states.set_state(42, initial["action"], deepcopy(initial.get("data") or {}))
    entry = case["entry"]
    if entry["op"] == "text" and not initial: states.set_state(42, entry["action"])
    before = deepcopy(states.get_state(42)); handled = None; error = None
    try:
        if entry["op"] == "callback": handled = menu.handle_callback(42, 77, "cb-stat", entry["data"])
        elif entry["op"] == "text": handled = menu.handle_text_state(42, entry["action"], entry["text"])
        else: menu.send_new(42)
    except Exception as exc: error = {"type": type(exc).__name__, "message": str(exc)}
    return {"caseId": case["caseId"], "capabilityId": case["capabilityId"], "entry": deepcopy(entry), "initialConfig": deepcopy(case["initialConfig"]), "initialState": deepcopy(initial), "initialRuntime": deepcopy(rt), "tgApi": calls, "stateSteps": [{"after": "before", "state": before}, {"after": "invoke", "state": deepcopy(states.get_state(42))}], "finalBusinessState": {"handled": handled, "config": store, "events": events, "muted": muted, "initialized": sorted(status_monitor._initialized_providers)}, "expectedException": error}

@pytest.fixture(autouse=True)
def _cleanup():
    states.clear_all(); ui._code_to_name.clear(); yield; states.clear_all(); ui._code_to_name.clear()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["caseId"])
def test_status_alert_strict_trace(case, monkeypatch): assert_strict_equal(case, _run(case, monkeypatch))

def test_status_alert_case_callback_state_and_enum_coverage_is_bidirectional():
    assert_capability_coverage({"TG-STAT-01"}, CASES); assert {c["caseId"] for c in CASES} == EXPECTED_CASE_IDS
    assert {_callback_family(c["entry"]["data"]) for c in CASES if c["entry"]["op"] == "callback"} == EXPECTED_CALLBACK_FAMILIES
    assert {c["entry"]["action"] for c in CASES if c["entry"]["op"] == "text"} == EXPECTED_STATES
    assert {c["entry"]["data"].split(":")[-1] for c in CASES if c["entry"].get("data", "").startswith("stat:set_impact:") and not c["caseId"].endswith("invalid")} == {"none", "minor", "major", "critical"}
    assert {"claude", "openai", "cloudflare"} <= {c["entry"]["data"].split(":")[-1] for c in CASES if c["entry"].get("data", "").startswith("stat:toggle_tgt:")}
    assert _source_families("handle_callback", "data") | {"stat:missing"} == EXPECTED_CALLBACK_FAMILIES
    assert _source_families("handle_text_state", "action") | {"stat_unknown"} == EXPECTED_STATES
