"""Executable strict traces and probe/cascade guards for TG-CH-03..05."""

from __future__ import annotations

import pytest

from src.tests.tg_contract import assert_strict_equal
from src.tests.test_tg_contract_channels_support import current_cases, run_menu_case


ALL_CASES = current_cases()
CASES = [
    case for case in ALL_CASES
    if case["capabilityId"] in {"TG-CH-03", "TG-CH-04", "TG-CH-05"}
]
BY_ID = {case["caseId"]: case for case in ALL_CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_channel_03_05_strict_trace(case, monkeypatch):
    actual = run_menu_case(case, "channel", monkeypatch)
    assert_strict_equal(case, actual)


def _texts(case_id: str) -> list[str]:
    return [
        call["payload"].get("text", "")
        for call in BY_ID[case_id]["tgApi"]
        if call["method"] in {"sendMessage", "editMessageText"}
    ]


def _final(case_id: str) -> dict:
    return BY_ID[case_id]["finalBusinessState"]


def test_ch03_existing_panel_locks_intentional_wording_contradiction():
    text = "\n".join(_texts("TG-CH-03.existing-panel-wording"))
    assert "本次测试不会修改冷却状态，只反映联通性。" in text


def test_ch03_existing_success_still_clears_cooldown_but_not_affinity():
    final = _final("TG-CH-03.existing-single-success-clears-only")
    assert final["cooldown"] == []
    assert final["affinity"] == {
        "fp-existing": {
            "channel_key": "api:probe-existing",
            "model": "ok",
            "last_used": 1700000000000,
            "prompt_cache_key": None,
        }
    }
    assert final["runtimeEvents"]["deleteDelays"] == [8]
    assert final["runtimeEvents"]["probeCalls"] == [
        {"channelKey": "api:probe-existing", "model": "ok"}
    ]


def test_ch03_existing_failure_does_not_record_error_and_uses_30_seconds():
    final = _final("TG-CH-03.existing-single-failure-does-not-record")
    assert final["cooldown"] == []
    assert final["affinity"]["fp-existing"]["model"] == "bad"
    assert final["runtimeEvents"]["deleteDelays"] == [30]
    text = "\n".join(_texts("TG-CH-03.existing-single-failure-does-not-record"))
    assert "模型测试失败，失败原因: diagnostic failed" in text
    assert "本消息将在 30 秒后自动删除" in text


def test_ch03_existing_all_preserves_success_clear_and_failure_no_record():
    final = _final("TG-CH-03.existing-all-mixed-side-effects")
    assert final["cooldown"] == []
    assert final["runtimeEvents"]["deleteDelays"] == [30]
    assert final["runtimeEvents"]["probeCalls"] == [
        {"channelKey": "api:probe-existing", "model": "ok"},
        {"channelKey": "api:probe-existing", "model": "bad"},
    ]


def test_ch03_wizard_initial_failure_is_recorded_only_by_save():
    case = BY_ID["TG-CH-03.wizard-all-mixed-records-on-save"]
    final = case["finalBusinessState"]
    assert final["cooldown"] == [{
        "channel_key": "api:mixed-probe",
        "model": "bad",
        "error_count": 1,
        "cooldown_until": 1700000060000,
        "remaining": "60s",
        "last_error_message": "initial probe failed: fixed upstream failure",
    }]
    assert final["runtimeEvents"]["deleteDelays"] == [30]
    pop_steps = [step for step in case["stateSteps"] if step["event"] == "pop"]
    assert len(pop_steps) == 1
    # The save callback removes state after the probe result trace and before
    # its final answerCallbackQuery/edit result pair.
    assert pop_steps[0]["step"] == 1
    assert pop_steps[0]["afterTgCall"] == 7


def test_ch03_wizard_success_clears_preexisting_temp_cooldown_and_uses_8_seconds():
    final = _final("TG-CH-03.wizard-single-success-clears")
    assert final["cooldown"] == []
    assert final["runtimeEvents"]["deleteDelays"] == [8]
    text = "\n".join(_texts("TG-CH-03.wizard-single-success-clears"))
    assert "已自动清除冷却与失败计数" in text
    assert "本消息将在 8 秒后自动删除" in text


def test_ch05_delete_uses_full_registry_cascade():
    final = _final("TG-CH-05.delete-cancel-exec-full-cascade")
    assert final["registry"] == []
    assert final["cooldown"] == []
    assert final["affinity"] == {}
    assert final["clientAffinity"] == {}
    assert final["scorer"] == []
    assert final["config"]["channels"] == []
    assert final["config"]["modelMapping"] == {
        "global": {"keep": "keep-target"},
        "anthropic": {},
    }
    assert final["config"]["loadBalancing"]["channelPriorityOrder"] == []
    assert final["config"]["loadBalancing"]["modelPriorityOrders"] == {}
    generation = "api-generation:0000000000000000000000000000003c"
    events = final["runtimeEvents"]
    assert events["retiredGenerations"] == [generation, generation]
    assert events["concurrencyRetire"] == [{
        "channelKey": generation,
        "kwargs": {"frozen_max": 0, "deleted_target": generation},
    }]
    assert len(events["providerCleanup"]) == 1
    assert events["providerCleanup"][0].startswith("pu1:")
    assert final["retiredChannelKeys"] == [generation]


def test_every_trace_uses_exact_payload_shape_and_explicit_state_checkpoints():
    allowed_methods = {
        "sendMessage", "editMessageText", "answerCallbackQuery", "deleteMessage",
    }
    for case in ALL_CASES:
        assert all(call["method"] in allowed_methods for call in case["tgApi"])
        assert all(set(call) == {"method", "payload"} for call in case["tgApi"])
        checkpoints = [step for step in case["stateSteps"] if step["event"] == "checkpoint"]
        assert len(checkpoints) == len(case["entry"]["steps"])
        assert [step["step"] for step in checkpoints] == list(range(len(checkpoints)))
