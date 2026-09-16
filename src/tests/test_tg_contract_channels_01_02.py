"""Executable strict traces for TG-CH-01 and TG-CH-02."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest

from src.tests.tg_contract import (
    assert_capability_coverage,
    assert_strict_equal,
)
from src.tests.test_tg_contract_channels_support import SEGMENT, current_cases, run_menu_case


ALL_CASES = current_cases()
CASES = [
    case for case in ALL_CASES
    if case["capabilityId"] in {"TG-CH-01", "TG-CH-02"}
]
CHANNEL_IDS = {f"TG-CH-{number:02d}" for number in range(1, 6)}
APIKEY_IDS = {f"TG-AK-{number:02d}" for number in range(1, 5)}
ASSIGNED_IDS = CHANNEL_IDS | APIKEY_IDS
CHANNEL_SOURCE = (
    Path(__file__).parents[1] / "telegram/menus/channel_menu.py",
    Path(__file__).parents[1] / "telegram/menus/channel_wizard.py",
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_channel_01_02_strict_trace(case, monkeypatch):
    actual = run_menu_case(case, "channel", monkeypatch)
    assert_strict_equal(case, actual)


def _callback_families(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "handle_callback"
    )
    found: set[str] = set()
    for node in ast.walk(handler):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "data":
                continue
            for item in node.comparators:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    found.add(item.value)
    return found


def _manifest_families(prefix: str) -> set[str]:
    return {
        family
        for case in ALL_CASES
        if case["capabilityId"].startswith(prefix)
        for family in case["entry"]["callbackFamilies"]
    }


def _callback_covered(family: str, callback: str) -> bool:
    return callback == family or callback.startswith(family)


def _state_literals(paths: tuple[Path, ...], prefix: str) -> set[str]:
    pattern = re.compile(rf'["\']({re.escape(prefix)}[A-Za-z0-9_:]+)["\']')
    found: set[str] = set()
    for path in paths:
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_segment_schema_unique_assigned_ids_and_no_mauth():
    assert_capability_coverage(ASSIGNED_IDS, ALL_CASES)
    case_ids = [case["caseId"] for case in ALL_CASES]
    assert len(case_ids) == len(set(case_ids))
    assert not any("mauth:" in json.dumps(case, ensure_ascii=False) for case in ALL_CASES)


def test_channel_callback_families_are_bidirectionally_complete_and_executed():
    source = _callback_families(CHANNEL_SOURCE[0])
    manifest = _manifest_families("TG-CH-")
    assert source == manifest
    scripted = [
        step["data"]
        for case in ALL_CASES
        if case["capabilityId"].startswith("TG-CH-")
        for step in case["entry"]["steps"]
        if step["kind"] == "callback"
    ]
    for family in manifest:
        assert any(_callback_covered(family, callback) for callback in scripted), family


def test_channel_state_families_are_bidirectionally_complete():
    source = _state_literals(CHANNEL_SOURCE, "ch_")
    source.discard("ch_edit_")
    # _edit_prompt builds these four concrete states from a fixed field literal.
    source.update({"ch_edit_name", "ch_edit_url", "ch_edit_key", "ch_edit_max"})
    manifest = {
        family
        for case in ALL_CASES
        if case["capabilityId"].startswith("TG-CH-")
        for family in case["entry"]["stateFamilies"]
    }
    assert source == manifest


def test_channel_capability_branch_matrix_covers_normative_paths():
    required = {
        "TG-CH-01": {
            "success", "pagination", "detail", "provider", "protocol", "health",
            "model", "monthly", "usage-reset", "top", "bottom", "up", "down",
            "reset", "save", "cancel", "expired", "illegal", "business-failure",
        },
        "TG-CH-02": {
            "success", "preset", "brands", "discover", "manual", "picker",
            "pagination", "all", "invert", "confirm", "adopt", "force", "back",
            "retry", "skip", "save", "cancel", "expired", "illegal",
            "business-failure", "source-only-state", "fallback",
        },
        "TG-CH-03": {
            "success", "business-failure", "pre-save", "existing-diagnostic",
            "wording-contradiction", "record-error", "no-record-error",
            "clear-cooldown", "affinity-unchanged", "auto-delete-8",
            "auto-delete-30", "expired", "illegal",
        },
        "TG-CH-04": {
            "success", "name", "url", "base-only", "switch-protocol", "protocol",
            "key", "picker", "discover", "maxConcurrent", "cc",
            "omit-temperature", "omit-thinking", "auto", "force", "all-models",
            "per-model", "expired", "illegal", "business-failure",
        },
        "TG-CH-05": {
            "success", "single", "all", "cancel", "delete", "registry-cascade",
            "cooldown", "affinity", "client-affinity", "scorer", "mapping",
            "load-balancing", "expired", "illegal", "business-failure",
        },
    }
    for capability, expected in required.items():
        actual = {
            branch
            for case in ALL_CASES if case["capabilityId"] == capability
            for branch in case["entry"]["branches"]
        }
        assert expected <= actual, (capability, sorted(expected - actual))


def test_ch01_trace_contains_protocol_health_monthly_and_usage_reset_rendering():
    case = next(
        item for item in ALL_CASES
        if item["caseId"] == "TG-CH-01.list-page-detail-usage-toggle"
    )
    text = "\n".join(
        call["payload"].get("text", "") for call in case["tgApi"]
        if call["method"] in {"sendMessage", "editMessageText"}
    )
    assert "🔌 协议:" in text and "/v1/chat/completions" in text
    assert "冷却中" in text and "fixed health failure" in str(case["finalBusinessState"])
    assert "💎 Parrot 月度：↑ 160 · ↓ 20" in text
    assert "重置" in text
    assert "暂时无需重复更新" in json.dumps(case["tgApi"], ensure_ascii=False)


def test_all_manifest_callbacks_fit_telegram_limit_and_inputs_are_fake():
    for case in ALL_CASES:
        for step in case["entry"]["steps"]:
            if step["kind"] == "callback":
                assert len(step["data"].encode("utf-8")) <= 64
    raw = SEGMENT.read_text(encoding="utf-8")
    assert "sk-live" not in raw
    assert "api.telegram.org" not in raw
