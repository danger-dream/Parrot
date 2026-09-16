"""Executable byte-strict TG-MAIN-01/TG-HELP-01 archive plus current deltas."""

from __future__ import annotations

import ast
from copy import deepcopy
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import textwrap

import pytest

from src.telegram import bot, menu_cache, states, ui
from src.telegram.menus import help_menu, main as main_menu
from src.tests.tg_contract import (
    TraceCapture,
    assert_capability_coverage,
    assert_strict_equal,
    load_jsonl,
)


# The v0.31.13 recording remains immutable.  Current main-menu deltas are full
# strict cases in a caseId-keyed overlay, leaving every unaffected archived case
# byte-for-byte unchanged.
SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/main_status.jsonl"
CURRENT_OVERRIDES = (
    Path(__file__).parent
    / "fixtures/tg_contract/model-center-2026-09-13/overrides/main_status.jsonl"
)


def _current_cases():
    archived = load_jsonl(SEGMENT)
    overrides = load_jsonl(CURRENT_OVERRIDES) if CURRENT_OVERRIDES.exists() else []
    archived_ids = {case["caseId"] for case in archived}
    override_map = {case["caseId"]: case for case in overrides}
    if len(override_map) != len(overrides):
        raise AssertionError("duplicate main-status contract override caseId")
    unknown = set(override_map) - archived_ids
    if unknown:
        raise AssertionError(f"unknown main-status contract overrides: {sorted(unknown)}")
    return [override_map.get(case["caseId"], case) for case in archived]


CASES = _current_cases()
OWNED_CAPABILITIES = frozenset({"TG-MAIN-01", "TG-HELP-01", "TG-STATUS-01"})
MAIN_HELP_CASES = [
    case for case in CASES
    if case["capabilityId"] in {"TG-MAIN-01", "TG-HELP-01"}
]
EXPECTED_CASE_IDS = {
    "TG-MAIN-01": {
        "TG-MAIN-01.start-welcome",
        "TG-MAIN-01.menu-first-run",
        "TG-MAIN-01.menu-overview-rich",
        "TG-MAIN-01.menu-overview-disabled-concurrency",
        "TG-MAIN-01.menu-cold-cache",
        "TG-MAIN-01.back-success",
        "TG-MAIN-01.back-cold-cache",
    },
    "TG-HELP-01": {
        "TG-HELP-01.command-full-text",
        "TG-HELP-01.callback-full-text",
        "TG-HELP-01.unknown-callback",
    },
    "TG-STATUS-01": {
        "TG-STATUS-01.command-fallback",
        "TG-STATUS-01.command-rich",
        "TG-STATUS-01.refresh-empty",
        "TG-STATUS-01.refresh-stats-failure",
        "TG-STATUS-01.refresh-quota-failure",
        "TG-STATUS-01.unknown-callback",
    },
}
FROZEN_VERSION = "0.31.13"
EXPECTED_CALLBACK_PATTERNS = {
    "TG-MAIN-01": {"menu:main"},
    "TG-HELP-01": {"menu:help"},
    "TG-STATUS-01": {"menu:status"},
}


def _actual(
    case: dict[str, Any],
    *,
    calls: list[dict[str, Any]],
    state_steps: list[dict[str, Any]],
    final: dict[str, Any],
    exception: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "caseId": case["caseId"],
        "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]),
        "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]),
        "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": deepcopy(calls),
        "stateSteps": deepcopy(state_steps),
        "finalBusinessState": deepcopy(final),
        "expectedException": deepcopy(exception),
    }


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch):
    states.clear_all()
    ui.configure("fake-main-status-token", [42])
    ui._session = None
    monkeypatch.setattr(main_menu, "__version__", FROZEN_VERSION)
    monkeypatch.setattr(
        help_menu,
        "_HELP_TEXT",
        help_menu._HELP_TEXT.replace(
            f"Parrot v{help_menu.__version__} ·",
            f"Parrot v{FROZEN_VERSION} ·",
        ),
    )
    yield
    states.clear_all()
    ui._session = None


def _patch_main(case: dict[str, Any], monkeypatch) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = deepcopy(case["initialConfig"]["app"])
    runtime = case["initialRuntime"]
    events: list[dict[str, Any]] = []
    control = main_menu._CONTROL
    monkeypatch.setattr(control.config, "get", lambda: cfg)
    monkeypatch.setattr(control.oauth_manager, "list_accounts", lambda: deepcopy(cfg.get("oauthAccounts") or []))
    quota_rows = deepcopy(runtime.get("quotaRows") or {})
    monkeypatch.setattr(control.state_db, "quota_load", lambda key: deepcopy(quota_rows.get(key)))
    channels = [SimpleNamespace(**item) for item in runtime.get("registryChannels", [])]
    monkeypatch.setattr(control.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(control.affinity, "count", lambda: runtime.get("affinityCount", 0))
    monkeypatch.setattr(control.public_ip_service, "get", lambda: runtime.get("publicIp"))
    monkeypatch.setattr(
        control.concurrency,
        "totals",
        lambda: deepcopy(runtime.get("concurrencyTotals") or {
            "in_flight": 0, "waiting": 0, "tracked_channels": 0,
        }),
    )
    lifetime = deepcopy(runtime.get("lifetime"))
    monkeypatch.setattr(
        menu_cache.LIFETIME_STATS,
        "peek",
        lambda key: menu_cache.CacheRead(lifetime, lifetime is not None, False),
    )
    monkeypatch.setattr(
        menu_cache,
        "begin_view",
        lambda chat_id, message_id: events.append({
            "event": "begin_view", "chatId": chat_id, "messageId": message_id,
        }) or 9001,
    )
    banners = runtime.get("banners") or {}
    monkeypatch.setattr(control.status_monitor, "get_active_summary", lambda: banners.get("status"))
    monkeypatch.setattr(control.update_checker, "get_update_banner", lambda: banners.get("update"))
    monkeypatch.setattr(control.network_monitor, "active_summary", lambda: banners.get("network"))
    return cfg, events


def _run_main(case: dict[str, Any], monkeypatch) -> dict[str, Any]:
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    cfg_before = deepcopy(case["initialConfig"].get("app") or {})
    scenario = case["entry"]["scenario"]
    events: list[dict[str, Any]] = []
    cfg = cfg_before
    if scenario != "start_welcome":
        cfg, events = _patch_main(case, monkeypatch)

    if scenario == "start_welcome":
        main_menu.on_start_command(case["entry"]["chatId"])
    elif scenario == "menu_show":
        main_menu.on_menu_command(case["entry"]["chatId"])
    elif scenario == "back":
        main_menu.handle_back(
            case["entry"]["chatId"],
            case["entry"]["messageId"],
            case["entry"]["callbackId"],
        )
    else:  # pragma: no cover - fixture schema gate keeps scenarios closed
        raise AssertionError(f"unknown main scenario: {scenario}")

    return _actual(
        case,
        calls=capture.calls,
        state_steps=[],
        final={
            "configUnchanged": cfg == cfg_before,
            "chatState": states.get_state(case["entry"]["chatId"]),
            "runtimeEvents": events,
        },
    )


def _run_help(case: dict[str, Any], monkeypatch) -> dict[str, Any]:
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    scenario = case["entry"]["scenario"]
    handled: bool | None = None
    if scenario == "send_new":
        help_menu.send_new(case["entry"]["chatId"])
    elif scenario in {"callback", "unknown_callback"}:
        handled = help_menu.handle_callback(
            case["entry"]["chatId"],
            case["entry"]["messageId"],
            case["entry"]["callbackId"],
            case["entry"]["data"],
        )
    else:  # pragma: no cover
        raise AssertionError(f"unknown help scenario: {scenario}")
    return _actual(
        case,
        calls=capture.calls,
        state_steps=[],
        final={"handled": handled, "chatState": states.get_state(case["entry"]["chatId"])},
    )


@pytest.mark.parametrize("case", MAIN_HELP_CASES, ids=lambda case: case["caseId"])
def test_main_and_help_trace(case, monkeypatch):
    runner = _run_main if case["capabilityId"] == "TG-MAIN-01" else _run_help
    assert_strict_equal(case, runner(case, monkeypatch))


def _data_eq_literals(function) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left_is_data = isinstance(node.left, ast.Name) and node.left.id == "data"
        if not left_is_data or not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
            continue
        value = node.comparators[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
    return values


def _main_back_literals() -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(bot._handle_callback)))
    values: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls_main_back = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "handle_back"
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "main_menu"
            for statement in node.body
            for child in ast.walk(statement)
        )
        if not calls_main_back:
            continue
        for child in ast.walk(node.test):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value.startswith("menu:")
            ):
                values.add(child.value)
    return values


def _all_string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _all_string_values(key)
            yield from _all_string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_string_values(item)


def test_segment_schema_case_assignment_callback_state_and_namespace_gates():
    assert_capability_coverage(OWNED_CAPABILITIES, CASES)
    assert len({case["caseId"] for case in CASES}) == len(CASES)
    actual_case_ids = {
        capability: {case["caseId"] for case in CASES if case["capabilityId"] == capability}
        for capability in OWNED_CAPABILITIES
    }
    assert actual_case_ids == EXPECTED_CASE_IDS

    fixture_patterns = {
        capability: {
            pattern for case in CASES if case["capabilityId"] == capability
            for pattern in [case["entry"].get("callbackPattern")]
            if pattern is not None
        }
        for capability in OWNED_CAPABILITIES
    }
    assert fixture_patterns == EXPECTED_CALLBACK_PATTERNS
    production_patterns = {
        "TG-MAIN-01": _main_back_literals(),
        "TG-HELP-01": _data_eq_literals(help_menu.handle_callback),
        "TG-STATUS-01": _data_eq_literals(bot.status_menu.handle_callback),
    }
    assert production_patterns == EXPECTED_CALLBACK_PATTERNS

    # None of these menus owns a text/document state family in v0.31.13.
    for module in (main_menu, help_menu, bot.status_menu):
        assert not hasattr(module, "handle_text_state")
        assert not hasattr(module, "handle_document_state")
    assert all(case["initialState"] == {} and case["stateSteps"] == [] for case in CASES)
    assert not any(text.startswith("mauth:") for case in CASES for text in _all_string_values(case))
