"""Executable strict traces and coverage gates for TG-AK-03 and TG-AK-04."""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from src.tests.tg_contract import assert_strict_equal, load_jsonl
from src.tests.test_tg_contract_channels_support import SEGMENT, current_cases, run_menu_case


ALL_CASES = current_cases()
CASES = [
    case for case in ALL_CASES
    if case["capabilityId"] in {"TG-AK-03", "TG-AK-04"}
]
BY_ID = {case["caseId"]: case for case in ALL_CASES}
SOURCE = Path(__file__).parents[1] / "telegram/menus/apikey_menu.py"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_apikey_03_04_strict_trace(case, monkeypatch):
    actual = run_menu_case(case, "apikey", monkeypatch)
    assert_strict_equal(case, actual)


def _callback_families() -> set[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
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


def test_apikey_callback_families_are_bidirectionally_complete_and_executed():
    manifest = {
        family
        for case in ALL_CASES if case["capabilityId"].startswith("TG-AK-")
        for family in case["entry"]["callbackFamilies"]
    }
    assert manifest == _callback_families()
    callbacks = [
        step["data"]
        for case in ALL_CASES if case["capabilityId"].startswith("TG-AK-")
        for step in case["entry"]["steps"] if step["kind"] == "callback"
    ]
    for family in manifest:
        assert any(value == family or value.startswith(family) for value in callbacks), family


def test_apikey_state_families_are_bidirectionally_complete():
    literals = set(re.findall(
        r'["\'](ak_[A-Za-z0-9_:]+)["\']', SOURCE.read_text(encoding="utf-8"),
    ))
    literals.remove("ak_limit_edit:")
    literals.update({
        "ak_limit_edit:concurrent", "ak_limit_edit:queue", "ak_limit_edit:wait",
    })
    manifest = {
        family
        for case in ALL_CASES if case["capabilityId"].startswith("TG-AK-")
        for family in case["entry"]["stateFamilies"]
    }
    assert literals == manifest


def test_apikey_capability_branch_matrix_covers_normative_paths():
    required = {
        "TG-AK-01": {
            "success", "pagination", "detail", "monthly", "model-stats",
            "generated", "custom", "one-shot", "duplicate", "expired", "illegal",
            "business-failure",
        },
        "TG-AK-02": {
            "success", "cancel", "regen", "rekey", "toggle", "delete",
            "one-shot", "limiter-side-effect", "limiter-cleanup", "duplicate",
            "expired", "illegal", "business-failure",
        },
        "TG-AK-03": {
            "success", "clear", "save", "cancel", "media-models", "toggle",
            "concurrent", "queue", "wait", "reset", "default", "empty",
            "expired", "illegal", "business-failure",
        },
        "TG-AK-04": {
            "success", "top", "bottom", "up", "down", "reset", "save",
            "cancel", "expired", "illegal", "business-failure",
        },
    }
    for capability, expected in required.items():
        actual = {
            branch
            for case in ALL_CASES if case["capabilityId"] == capability
            for branch in case["entry"]["branches"]
        }
        assert expected <= actual, (capability, sorted(expected - actual))


def test_ak03_permission_draft_save_and_cancel_state_pop_timing():
    case = BY_ID["TG-AK-03.permission-complete"]
    final = case["finalBusinessState"]
    assert final["config"]["apiKeys"]["alpha"]["allowedModels"] == ["image-1"]
    assert final["state"] is None
    pop_steps = [step for step in case["stateSteps"] if step["event"] == "pop"]
    # Steps 7/9 close the model-permission draft; 14/17 close the MCP tool draft
    # that the reviewed MCP flow opens and cancels after it.
    assert [step["step"] for step in pop_steps] == [7, 9, 14, 17]
    text = "\n".join(
        call["payload"].get("text", "") for call in case["tgApi"]
        if call["method"] in {"sendMessage", "editMessageText"}
    )
    assert "🖼 为图片模型，🎬 为视频模型" in text
    assert "清空 → 视为无限制" in text
    assert "MCP 工具授权" in text
    # The MCP tool picker renders one row per tool plus save/clear/cancel.
    picker = next(
        (call["payload"]["reply_markup"]["inline_keyboard"] for call in case["tgApi"]
         if call["method"] == "editMessageText"
         and "MCP 工具授权" in call["payload"].get("text", "")),
        None,
    )
    assert picker is not None
    picker_buttons = [b["callback_data"] for row in picker for b in row]
    assert any(x.startswith("ak:mtg:") for x in picker_buttons)
    assert any(x.startswith("ak:mts:") for x in picker_buttons)
    assert any(x.startswith("ak:mtc:") for x in picker_buttons)
    assert any(x.startswith("ak:mtx:") for x in picker_buttons)
    assert any(
        call["payload"].get("text") == "短码不匹配"
        for call in case["tgApi"] if call["method"] == "answerCallbackQuery"
    )
    assert any(
        call["payload"].get("text") == "索引无效"
        for call in case["tgApi"] if call["method"] == "answerCallbackQuery"
    )


def test_ak03_limiter_parses_all_fields_and_reset_removes_override():
    case = BY_ID["TG-AK-03.limiter-complete-parsing-reset"]
    text = "\n".join(
        call["payload"].get("text", "") for call in case["tgApi"]
        if call["method"] in {"sendMessage", "editMessageText"}
    )
    assert "并发上限已更新为 <code>不限</code>" in text
    assert "❌ 输入无效，请重新输入" in text
    assert "队列上限已更新为 <code>5</code>" in text
    assert "最长等待已更新为 <code>2s</code>" in text
    assert "limits" not in case["finalBusinessState"]["config"]["apiKeys"]["alpha"]


def test_ak04_save_persists_order_and_cancel_preserves_original_order():
    saved = BY_ID["TG-AK-04.order-complete-save"]["finalBusinessState"]
    assert list(saved["config"]["apiKeys"]) == [
        "key-5", "key-1", "key-2", "key-3", "key-4",
    ]
    assert saved["state"] is None
    cancelled = BY_ID["TG-AK-04.order-expired-invalid-cancel"]["finalBusinessState"]
    assert list(cancelled["config"]["apiKeys"]) == [
        "key-1", "key-2", "key-3", "key-4", "key-5",
    ]
    assert cancelled["state"] is None
