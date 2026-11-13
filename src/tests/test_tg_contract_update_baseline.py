"""Executable, non-destructive strict v0.31.13 baseline for TG-UPD-01."""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src import config, update_checker, updater
from src.management_control.auxiliary import updates as update_controls
from src.telegram import states, ui
from src.telegram.menus import update_menu as menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/auxiliary.jsonl"
CASES = [c for c in load_jsonl(SEGMENT) if c["capabilityId"] == "TG-UPD-01"] if SEGMENT.exists() else []
BASE = {"updateChecker": {"enabled": True, "includePrerelease": True, "autoUpdate": False, "intervalSeconds": 3600, "repo": "fake/Parrot", "ignoredVersions": []}}
LATEST = {"latest_version": "9.9.9", "latest_name": "Release <candidate>", "latest_published_at": "2026-01-02T03:04:05Z", "latest_prerelease": True, "latest_url": "https://fake.invalid/release", "latest_body": "Fix <unsafe> & improve\nsecond line"}
FROZEN_VERSION = "0.31.13"

def _cfg(**patch: Any) -> dict[str, Any]:
    out = deepcopy(BASE); out["updateChecker"].update(patch); return out

def _spec(name: str, op: str, **entry: Any) -> dict[str, Any]:
    state = entry.pop("state", None); cfg = deepcopy(entry.pop("config", BASE)); runtime = deepcopy(entry.pop("runtime", {}))
    return {"caseId": f"TG-UPD-01.{name}", "capabilityId": "TG-UPD-01", "entry": {"op": op, **entry}, "initialConfig": cfg, "initialState": state or {}, "initialRuntime": runtime, "tgApi": [], "stateSteps": [], "finalBusinessState": {}, "expectedException": None}

SPECS = [
    _spec("back-preserves-interval-state", "callback", data="menu:update", state={"action": "upd_interval", "data": {}}),
    _spec("show-latest", "send_new", runtime={"cached": LATEST, "newer": True}),
    _spec("show-latest-ignored", "callback", data="menu:update", config=_cfg(ignoredVersions=["9.9.9"]), runtime={"cached": LATEST, "newer": True}),
    *[_spec(f"show-stage-{stage}", "callback", data="menu:update", runtime={"cached": LATEST, "newer": True, "state": {"stage": stage, "message": "stage <detail>"}}) for stage in (updater.STAGE_BACKING_UP, updater.STAGE_PULLING, updater.STAGE_STAGED, updater.STAGE_RESTARTING, updater.STAGE_VERIFYING, updater.STAGE_SUCCESS, updater.STAGE_FAILED, updater.STAGE_ROLLED_BACK)],
    _spec("noop", "callback", data="upd:noop"),
    _spec("toggle-enabled", "callback", data="upd:toggle_enabled"),
    _spec("toggle-prerelease", "callback", data="upd:toggle_pre"),
    _spec("toggle-auto", "callback", data="upd:toggle_auto"),
    _spec("interval-ask", "callback", data="upd:edit_interval"),
    _spec("interval-invalid", "text", action="upd_interval", text="299"),
    _spec("interval-save", "text", action="upd_interval", text="7200"),
    _spec("refresh-success", "callback", data="upd:refresh", runtime={"cached": LATEST, "newer": True}),
    _spec("refresh-failure", "callback", data="upd:refresh", runtime={"refreshFailure": "fake release <down>"}),
    _spec("ignore-empty", "callback", data="upd:ignore:"),
    _spec("ignore-success", "callback", data="upd:ignore:9.9.9", runtime={"cached": LATEST, "newer": True}),
    _spec("unignore-empty", "callback", data="upd:unignore:"),
    _spec("unignore-success", "callback", data="upd:unignore:9.9.9", config=_cfg(ignoredVersions=["9.9.9"]), runtime={"cached": LATEST, "newer": True}),
    _spec("clear-ignored", "callback", data="upd:clear_ignored", config=_cfg(ignoredVersions=["1.0.0", "2.0.0"])),
    _spec("backups-empty", "callback", data="upd:backups"),
    _spec("backups-populated", "callback", data="upd:backups", runtime={"backups": [{"ref": "parrot-backup:one<&>", "version": "0.31.13", "target_tag": "9.9.9", "mode": "docker"}, {"ref": "git-ref-2", "version": "0.31.12", "target_tag": "9.0.0", "mode": "bare"}]}),
    _spec("failure-log-empty", "callback", data="upd:faillog"),
    _spec("failure-log-content", "callback", data="upd:faillog", runtime={"failureLog": "pull <failed> & stopped\nline 2"}),
    _spec("update-empty-version", "callback", data="upd:do_update:"),
    _spec("update-busy", "callback", data="upd:do_update:9.9.9", runtime={"busy": True, "cached": LATEST, "newer": True}),
    _spec("first-confirm", "callback", data="upd:do_update:9.9.9"),
    _spec("stage-success", "callback", data="upd:stage:9.9.9", runtime={"stageResult": [True, "staged"], "progress": [[updater.STAGE_BACKING_UP, "📦 备份中 <1>"], [updater.STAGE_PULLING, "⬇ 拉取中 &2"], [updater.STAGE_STAGED, "⏸ staged"]]}),
    _spec("stage-business-failure", "callback", data="upd:stage:9.9.9", runtime={"stageResult": [False, "fake pull <failed>"]}),
    _spec("restart-invalid-stage", "callback", data="upd:confirm_restart", runtime={"state": {"stage": updater.STAGE_IDLE}}),
    _spec("restart-triggered-fake", "callback", data="upd:confirm_restart", runtime={"state": {"stage": updater.STAGE_STAGED}, "restartResult": [True, "fake accepted"]}),
    _spec("restart-trigger-failure", "callback", data="upd:confirm_restart", runtime={"state": {"stage": updater.STAGE_STAGED}, "restartResult": [False, "fake restart denied"]}),
    _spec("cancel-staged-fake", "callback", data="upd:cancel_staged", runtime={"cancelResult": [True, "fake rollback complete"]}),
    _spec("cancel-staged-failure", "callback", data="upd:cancel_staged", runtime={"cancelResult": [False, "not staged"]}),
    _spec("health-entry-absent", "callback", data="upd:health"),
    _spec("rollback-entry-absent", "callback", data="upd:rollback"),
    _spec("unknown-state", "text", action="upd_unknown", text="x"),
]
EXPECTED_CASE_IDS = {s["caseId"] for s in SPECS}
EXPECTED_CALLBACK_FAMILIES = {"menu:update", "upd:noop", "upd:toggle_enabled", "upd:toggle_pre", "upd:toggle_auto", "upd:edit_interval", "upd:refresh", "upd:backups", "upd:faillog", "upd:do_update:*", "upd:stage:*", "upd:confirm_restart", "upd:cancel_staged", "upd:ignore:*", "upd:unignore:*", "upd:clear_ignored", "upd:health", "upd:rollback"}
EXPECTED_STATES = {"upd_interval", "upd_unknown"}

NEW_CALLBACK_FAMILIES = {"upd:toggle_silent", "upd:edit_keep", "upd:clear_bk", "upd:clear_bk_ok", "upd:del_bk:*"}
NEW_STATES = {"upd_keep"}

def _callback_family(data: str) -> str:
    for prefix in ("upd:do_update:", "upd:stage:", "upd:ignore:", "upd:unignore:", "upd:del_bk:"):
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
    store = deepcopy(case["initialConfig"]); rt = case["initialRuntime"]; calls: list[dict[str, Any]] = []; events: list[Any] = []; progress_cb = [None]
    def api(method, data=None): calls.append({"method": method, "payload": deepcopy(data or {})}); return {"ok": True, "result": {"message_id": 901}}
    def update(mutator): events.append("config.update"); mutator(store); return store
    def set_ignored(values): store["updateChecker"]["ignoredVersions"] = values
    def refresh():
        events.append("force_refresh")
        if rt.get("refreshFailure"): raise RuntimeError(rt["refreshFailure"])
    def set_progress(cb): progress_cb[0] = cb; events.append(["progress_callback", cb is not None])
    def stage(version, chat_id=None, notify_msg_id=None):
        events.append(["stage_update", version, chat_id, notify_msg_id])
        for stage_name, text in rt.get("progress", []):
            if progress_cb[0]: progress_cb[0](stage_name, text)
        return tuple(rt.get("stageResult", [True, "staged"]))
    states.clear_all(); ui._code_to_name.clear(); ui.configure("fake-token-upd", [42])
    monkeypatch.setattr(states.time, "time", lambda: 1000.0); monkeypatch.setattr(config, "get", lambda: store); monkeypatch.setattr(config, "update", update); monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(update_controls, "__version__", FROZEN_VERSION)
    monkeypatch.setattr(update_checker, "get_cached", lambda: deepcopy(rt.get("cached", {}))); monkeypatch.setattr(update_checker, "_has_newer", lambda version: bool(rt.get("newer", False))); monkeypatch.setattr(update_checker, "force_refresh_sync", refresh)
    monkeypatch.setattr(update_checker, "add_ignored", lambda version: (events.append(["ignore", version]), set_ignored(list(dict.fromkeys(store["updateChecker"].get("ignoredVersions", []) + [version]))))); monkeypatch.setattr(update_checker, "remove_ignored", lambda version: (events.append(["unignore", version]), set_ignored([v for v in store["updateChecker"].get("ignoredVersions", []) if v != version]))); monkeypatch.setattr(update_checker, "clear_ignored", lambda: (events.append("clear_ignored"), set_ignored([])))
    monkeypatch.setattr(updater, "get_mode", lambda: rt.get("mode", "docker")); monkeypatch.setattr(updater, "load_state", lambda: deepcopy(rt.get("state", {"stage": updater.STAGE_IDLE}))); monkeypatch.setattr(updater, "is_busy", lambda: bool(rt.get("busy"))); monkeypatch.setattr(updater, "list_backups", lambda: deepcopy(rt.get("backups", []))); monkeypatch.setattr(updater, "get_update_log", lambda: rt.get("failureLog", "")); monkeypatch.setattr(updater, "set_progress_callback", set_progress); monkeypatch.setattr(updater, "stage_update", stage)
    monkeypatch.setattr(updater, "save_state", lambda **kw: events.append(["save_state", kw])); monkeypatch.setattr(updater, "confirm_restart", lambda: events.append("confirm_restart_fake") or tuple(rt.get("restartResult", [True, "fake accepted"]))); monkeypatch.setattr(updater, "cancel_staged", lambda: events.append("cancel_staged_fake") or tuple(rt.get("cancelResult", [True, "fake rollback complete"])))
    initial = case["initialState"]
    if initial: states.set_state(42, initial["action"], deepcopy(initial.get("data") or {}))
    entry = case["entry"]
    if entry["op"] == "text" and not initial: states.set_state(42, entry["action"])
    before = deepcopy(states.get_state(42)); handled = None; error = None
    try:
        if entry["op"] == "callback": handled = menu.handle_callback(42, 77, "cb-upd", entry["data"])
        elif entry["op"] == "text": handled = menu.handle_text_state(42, entry["action"], entry["text"])
        else: menu.send_new(42)
    except Exception as exc: error = {"type": type(exc).__name__, "message": str(exc)}
    return {"caseId": case["caseId"], "capabilityId": case["capabilityId"], "entry": deepcopy(entry), "initialConfig": deepcopy(case["initialConfig"]), "initialState": deepcopy(initial), "initialRuntime": deepcopy(rt), "tgApi": calls, "stateSteps": [{"after": "before", "state": before}, {"after": "invoke", "state": deepcopy(states.get_state(42))}], "finalBusinessState": {"handled": handled, "config": store, "events": events}, "expectedException": error}

@pytest.fixture(autouse=True)
def _cleanup():
    states.clear_all(); ui._code_to_name.clear(); yield; states.clear_all(); ui._code_to_name.clear()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["caseId"])
def test_update_strict_trace_without_restart_or_update(case, monkeypatch):
    expected = deepcopy(case)
    for call in expected.get("tgApi", []):
        kb = (call.get("payload") or {}).get("reply_markup", {}).get("inline_keyboard")
        if isinstance(kb, list):
            for idx, row in enumerate(kb):
                if row == [{"text": "📋 更新日志", "callback_data": "upd:faillog"}]:
                    kb[idx] = [
                        {"text": "🔔 静默更新: 关", "callback_data": "upd:toggle_silent"},
                        {"text": "📋 更新日志", "callback_data": "upd:faillog"},
                    ]
    if case["caseId"] == "TG-UPD-01.cancel-staged-failure":
        # M-04: the archived trace falsely claimed cancellation succeeded.
        # Keep the archive intact and explicitly review this one corrected receipt.
        expected["tgApi"][1]["payload"]["text"] = "❌ <b>取消更新失败</b>\n\nnot staged"
    elif case["caseId"] == "TG-UPD-01.backups-empty":
        expected["tgApi"][1]["payload"]["text"] = "🗂 <b>备份列表</b>（自动保留最近 <code>1</code> 份）\n\n暂无备份。"
        expected["tgApi"][1]["payload"]["reply_markup"] = ui.inline_kb([
            [ui.btn("📦 保留份数: 1", "upd:edit_keep"), ui.btn("◀ 返回", "menu:update")],
        ])
    elif case["caseId"] == "TG-UPD-01.backups-populated":
        ui._code_to_name.clear()
        c1 = ui.register_code("parrot-backup:one<&>")
        c2 = ui.register_code("git-ref-2")
        ui._code_to_name.clear()
        expected["tgApi"][1]["payload"]["text"] = (
            "🗂 <b>备份列表</b>（最近在前 · 自动保留最近 <code>1</code> 份）\n\n"
            "<b>#1</b> <code>parrot-backup:one&lt;&amp;&gt;</code>\n"
            "  v0.31.13 → 9.9.9 (docker)\n"
            "<b>#2</b> <code>git-ref-2</code>\n"
            "  v0.31.12 → 9.0.0 (bare)"
        )
        expected["tgApi"][1]["payload"]["reply_markup"] = ui.inline_kb([
            [ui.btn("🗑 删除 #1", f"upd:del_bk:{c1}"), ui.btn("🗑 删除 #2", f"upd:del_bk:{c2}")],
            [ui.btn("🧹 清空可删备份", "upd:clear_bk")],
            [ui.btn("📦 保留份数: 1", "upd:edit_keep"), ui.btn("◀ 返回", "menu:update")],
        ])
    assert_strict_equal(expected, _run(case, monkeypatch))

def test_update_case_callback_and_state_coverage_is_bidirectional():
    assert_capability_coverage({"TG-UPD-01"}, CASES); assert {c["caseId"] for c in CASES} == EXPECTED_CASE_IDS
    assert {_callback_family(c["entry"]["data"]) for c in CASES if c["entry"]["op"] == "callback"} == EXPECTED_CALLBACK_FAMILIES
    assert {c["entry"]["action"] for c in CASES if c["entry"]["op"] == "text"} == EXPECTED_STATES
    absent = {c["entry"]["data"] for c in CASES if c["caseId"].endswith("entry-absent")}
    assert absent == {"upd:health", "upd:rollback"}
    assert _source_families("handle_callback", "data") | absent == EXPECTED_CALLBACK_FAMILIES | NEW_CALLBACK_FAMILIES
    assert _source_families("handle_text_state", "action") | {"upd_unknown"} == EXPECTED_STATES | NEW_STATES


def test_update_backup_management_and_keep_backups_actions(monkeypatch):
    store = deepcopy(BASE)
    backups = [
        {"ref": "docker-0.36.4-20261112", "version": "0.36.4", "target_tag": "v0.36.5", "mode": "docker", "protected": True},
        {"ref": "docker-0.36.3-20261110", "version": "0.36.3", "target_tag": "", "mode": "docker", "orphan": True},
    ]
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(config, "get", lambda: store)
    monkeypatch.setattr(config, "update", lambda fn: (fn(store), store)[1])
    monkeypatch.setattr(ui, "api", lambda method, data=None: (calls.append({"method": method, "payload": deepcopy(data or {})}), {"ok": True, "result": {"message_id": 901}})[1])
    monkeypatch.setattr(updater, "list_backups", lambda: deepcopy(backups))

    def fake_del(ref: str) -> str:
        for item in list(backups):
            if item["ref"] == ref:
                if item.get("protected"):
                    return "protected"
                backups.remove(item)
                return "deleted"
        return "not_found"

    def fake_clear() -> dict[str, Any]:
        kept = [b for b in backups if b.get("protected")]
        deleted = len(backups) - len(kept)
        backups[:] = kept
        return {"deleted": deleted, "keptProtected": bool(kept), "failed": 0}

    monkeypatch.setattr(updater, "delete_backup", fake_del)
    monkeypatch.setattr(updater, "clear_backups", fake_clear)

    # Render backups list: #1 is protected (no delete button), #2 is orphan (has delete button)
    assert menu.handle_callback(42, 77, "cb-1", "upd:backups") is True
    rendered = calls[-1]["payload"]["text"]
    assert "🔒回滚保护中" in rendered and "历史镜像标签" in rendered

    # Protected backup rejected with alert; deletable backup deleted
    assert menu.handle_callback(42, 77, "cb-2", f"upd:del_bk:{ui.register_code('docker-0.36.4-20261112')}") is True
    assert calls[-2]["payload"].get("show_alert") is True
    assert len(backups) == 2

    # Confirm clear removes non-protected backup and keeps protected anchor
    assert menu.handle_callback(42, 77, "cb-3", "upd:clear_bk") is True
    assert "确认清空可删备份" in calls[-1]["payload"]["text"]
    assert menu.handle_callback(42, 77, "cb-4", "upd:clear_bk_ok") is True
    assert [b["ref"] for b in backups] == ["docker-0.36.4-20261112"]

    # Edit keepBackups: out-of-range rejected, 0 accepted
    assert menu.handle_callback(42, 77, "cb-5", "upd:edit_keep") is True
    assert states.get_state(42)["action"] == "upd_keep"
    assert menu.handle_text_state(42, "upd_keep", "25") is True
    assert states.get_state(42)["action"] == "upd_keep"
    assert menu.handle_text_state(42, "upd_keep", "0") is True
    assert states.get_state(42) is None
    assert store["updateChecker"]["keepBackups"] == 0

    # Toggle silent auto-update on and off
    assert menu.handle_callback(42, 77, "cb-6", "upd:toggle_silent") is True
    assert store["updateChecker"]["silentUpdate"] is True
    assert "静默（空闲自动升级）" in calls[-1]["payload"]["text"]
    assert menu.handle_callback(42, 77, "cb-7", "upd:toggle_silent") is True
    assert store["updateChecker"]["silentUpdate"] is False
