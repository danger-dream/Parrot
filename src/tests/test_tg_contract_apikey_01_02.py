"""Executable strict traces for TG-AK-01 and TG-AK-02."""

from __future__ import annotations

import pytest

from src.tests.tg_contract import assert_strict_equal, load_jsonl
from src.tests.test_tg_contract_channels_support import SEGMENT, current_cases, run_menu_case


ALL_CASES = current_cases()
CASES = [
    case for case in ALL_CASES
    if case["capabilityId"] in {"TG-AK-01", "TG-AK-02"}
]
BY_ID = {case["caseId"]: case for case in ALL_CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_apikey_01_02_strict_trace(case, monkeypatch):
    actual = run_menu_case(case, "apikey", monkeypatch)
    assert_strict_equal(case, actual)


def _message_texts(case_id: str) -> list[str]:
    return [
        call["payload"].get("text", "")
        for call in BY_ID[case_id]["tgApi"]
        if call["method"] in {"sendMessage", "editMessageText"}
    ]


def _occurrences(case_id: str, value: str) -> int:
    return sum(text.count(value) for text in _message_texts(case_id))


def test_ak01_generated_secret_has_fixed_prefix_and_is_shown_once():
    case = BY_ID["TG-AK-01.create-auto-one-shot"]
    generated = "ccp-" + "ab" * 24
    assert _occurrences(case["caseId"], generated) == 1
    assert case["finalBusinessState"]["config"]["apiKeys"]["client-a"]["key"] == generated
    pop = [step for step in case["stateSteps"] if step["event"] == "pop"]
    assert pop[-1]["afterTgCall"] == 4
    assert pop[-1]["state"] is None


def test_ak01_custom_secret_is_shown_once_and_validators_keep_state():
    case_id = "TG-AK-01.create-custom-validation-one-shot"
    case = BY_ID[case_id]
    assert _occurrences(case_id, "custom-secret+/=") == 1
    assert case["finalBusinessState"]["config"]["apiKeys"]["custom-new"]["key"] == "custom-secret+/="
    text = "\n".join(_message_texts(case_id))
    assert "key 太短，至少 8 个字符" in text
    assert "key 含非法字符" in text
    assert "key 太长，最多 256 个字符" in text
    assert "key 已被其他 key 使用" in text
    assert "名称 <code>alpha</code> 已存在" in text
    checkpoints = [step for step in case["stateSteps"] if step["event"] == "checkpoint"]
    # All four rejected secret values retain the add-key-input state.
    for index in (4, 5, 6, 7):
        assert checkpoints[index]["state"]["action"] == "ak_add_key_input"


def test_ak01_list_is_four_per_page_and_detail_locks_month_model_stats():
    case = BY_ID["TG-AK-01.list-page-detail-monthly"]
    calls = case["tgApi"]
    first = next(call for call in calls if call["method"] == "sendMessage")
    keyboard = first["payload"]["reply_markup"]["inline_keyboard"]
    view_buttons = [
        button for row in keyboard for button in row
        if button.get("callback_data", "").startswith("ak:view:")
    ]
    assert len(view_buttons) == 4
    text = "\n".join(_message_texts(case["caseId"]))
    assert "共 5 个" in text and "第 1/2 页" in text and "第 2/2 页" in text
    assert "<b>📊 本月使用统计</b>" in text
    assert "<code>model-a</code>" in text
    assert "3 次 · ✅ 2 · ❌ 1" in text


def test_ak02_regen_and_rekey_each_expose_new_value_once():
    case_id = "TG-AK-02.regen-rekey-one-shot-runtime"
    generated = "ccp-" + "ab" * 24
    assert _occurrences(case_id, generated) == 1
    assert _occurrences(case_id, "new-alpha-secret") == 1
    text = "\n".join(_message_texts(case_id))
    assert "key 已被其他 key 使用" in text
    final = BY_ID[case_id]["finalBusinessState"]
    assert final["config"]["apiKeys"]["alpha"]["key"] == "new-alpha-secret"
    # This freezes v0.31.13 as implemented: regen/rekey do not call forget_key.
    assert final["runtimeEvents"]["limiterForget"] == []


def test_ak02_delete_calls_limiter_forget_but_toggles_do_not():
    case = BY_ID["TG-AK-02.toggle-delete-cleanup"]
    final = case["finalBusinessState"]
    assert final["config"]["apiKeys"] == {}
    assert final["runtimeEvents"]["limiterForget"] == ["alpha"]
    text = "\n".join(_message_texts(case["caseId"]))
    assert "确认删除 <b>alpha</b>" in text
    assert "✅ 已删除 <code>alpha</code>" in text


def test_ak02_confirmation_cancel_is_real_detail_callback_not_synthetic_state():
    case = BY_ID["TG-AK-02.toggle-delete-cleanup"]
    scripted = [
        step["data"] for step in case["entry"]["steps"]
        if step["kind"] == "callback"
    ]
    delete_index = next(i for i, value in enumerate(scripted) if value.startswith("ak:del:"))
    assert scripted[delete_index + 1].startswith("ak:view:")
    assert case["finalBusinessState"]["state"] is None
