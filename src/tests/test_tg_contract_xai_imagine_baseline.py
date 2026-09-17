"""Executable strict v0.31.13 baseline for TG-XIM-01."""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src import config
from src.telegram import states, ui
from src.telegram.menus import xai_imagine_menu as menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/auxiliary.jsonl"
ALL_CASES = load_jsonl(SEGMENT) if SEGMENT.exists() else []
CASES = [c for c in ALL_CASES if c["capabilityId"] == "TG-XIM-01"]
BASE = {"xaiOAuth": {"imageModels": ["grok-imagine-image", "grok-image<&>"], "videoModels": ["grok-imagine-video"], "videoJobTtlSeconds": 10800, "mediaRequestTimeoutSeconds": 180}}


def _spec(name: str, op: str, **entry: Any) -> dict[str, Any]:
    state = entry.pop("state", None)
    cfg = deepcopy(entry.pop("config", BASE))
    runtime = deepcopy(entry.pop("runtime", {}))
    return {"caseId": f"TG-XIM-01.{name}", "capabilityId": "TG-XIM-01", "entry": {"op": op, **entry}, "initialConfig": cfg, "initialState": state or {}, "initialRuntime": runtime, "tgApi": [], "stateSteps": [], "finalBusinessState": {}, "expectedException": None}


SPECS = [
    _spec("show-cancels-state", "callback", data="xim:show", state={"action": "xim_edit_image_models", "data": {"draft": "A"}}),
    _spec("send-new", "send_new"),
    _spec("ask-image", "callback", data="xim:edit:image"),
    _spec("ask-video", "callback", data="xim:edit:video"),
    _spec("ask-ttl", "callback", data="xim:edit:ttl"),
    _spec("ask-timeout", "callback", data="xim:edit:timeout"),
    _spec("image-models-dedupe", "text", action="xim_edit_image_models", text="new-one, new-two\nnew-one"),
    _spec("models-empty-invalid", "text", action="xim_edit_image_models", text="   "),
    _spec("models-name-too-long", "text", action="xim_edit_image_models", text="x" * 129),
    _spec("models-too-many", "text", action="xim_edit_video_models", text=" ".join(f"m{i}" for i in range(51))),
    *[_spec(f"clear-alias-{index}", "text", action="xim_edit_video_models", text=value) for index, value in enumerate(("-", "clear", "none", "清空", "无"), 1)],
    _spec("ttl-seconds", "text", action="xim_edit_job_ttl", text="90"),
    _spec("ttl-s", "text", action="xim_edit_job_ttl", text="90s"),
    _spec("ttl-minutes", "text", action="xim_edit_job_ttl", text="3m"),
    _spec("ttl-hours", "text", action="xim_edit_job_ttl", text="4H"),
    _spec("ttl-days", "text", action="xim_edit_job_ttl", text="2d"),
    _spec("timeout-success", "text", action="xim_edit_request_timeout", text="5m"),
    _spec("timeout-days-rejected", "text", action="xim_edit_request_timeout", text="1d"),
    _spec("duration-malformed", "text", action="xim_edit_job_ttl", text="1.5h"),
    _spec("duration-zero", "text", action="xim_edit_job_ttl", text="0"),
    _spec("duration-overflow", "text", action="xim_edit_job_ttl", text="24856d"),
    _spec("model-update-failure", "text", action="xim_edit_image_models", text="new-model", runtime={"updateFailure": "fake config write failed"}),
    _spec("unknown-callback", "callback", data="xim:missing"),
    _spec("unknown-state", "text", action="xim_unknown", text="value"),
]
EXPECTED_CASE_IDS = {s["caseId"] for s in SPECS}
EXPECTED_CALLBACKS = {"xim:show", "xim:edit:image", "xim:edit:video", "xim:edit:ttl", "xim:edit:timeout", "xim:missing"}
EXPECTED_STATES = {"xim_edit_image_models", "xim_edit_video_models", "xim_edit_job_ttl", "xim_edit_request_timeout", "xim_unknown"}


def _snapshot(chat_id: int) -> dict[str, Any] | None:
    return deepcopy(states.get_state(chat_id))


def _run(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from src.telegram.menus import model_center_menu
    monkeypatch.setattr(model_center_menu, '_image_settings_render', lambda *args: ('图片面板', ui.inline_kb([[ui.btn('返回', 'menu:main')]])))
    monkeypatch.setattr(model_center_menu, '_video_settings_render', lambda *args: ('视频面板', ui.inline_kb([[ui.btn('返回', 'menu:main')]])))
    store = deepcopy(case["initialConfig"])
    updates: list[str] = []
    capture: list[dict[str, Any]] = []

    def api(method: str, data: dict | None = None):
        capture.append({"method": method, "payload": deepcopy(data or {})})
        return {"ok": True, "result": {"message_id": 901}}

    def update(mutator):
        updates.append("attempt")
        failure = case["initialRuntime"].get("updateFailure")
        if failure:
            raise RuntimeError(failure)
        mutator(store)
        return store

    states.clear_all()
    ui._code_to_name.clear()
    ui.configure("fake-token-xim", [42])
    monkeypatch.setattr(states.time, "time", lambda: 1000.0)
    monkeypatch.setattr(config, "get", lambda: store)
    monkeypatch.setattr(config, "update", update)
    monkeypatch.setattr(ui, "api", api)
    initial = case["initialState"]
    if initial:
        states.set_state(42, initial["action"], deepcopy(initial.get("data") or {}))
    before = _snapshot(42)
    handled = None
    error = None
    entry = case["entry"]
    try:
        if entry["op"] == "callback":
            handled = menu.handle_callback(42, 77, "cb-xim", entry["data"])
        elif entry["op"] == "text":
            if not initial:
                states.set_state(42, entry["action"])
                before = _snapshot(42)
            handled = menu.handle_text_state(42, entry["action"], entry["text"])
        else:
            menu.send_new(42)
    except Exception as exc:  # exact legacy failure propagation is part of the baseline
        error = {"type": type(exc).__name__, "message": str(exc)}
    after = _snapshot(42)
    return {
        "caseId": case["caseId"], "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]), "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]), "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": capture, "stateSteps": [{"after": "before", "state": before}, {"after": "invoke", "state": after}],
        "finalBusinessState": {"handled": handled, "config": store, "updateAttempts": len(updates)},
        "expectedException": error,
    }


@pytest.fixture(autouse=True)
def _cleanup():
    states.clear_all(); ui._code_to_name.clear()
    yield
    states.clear_all(); ui._code_to_name.clear()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["caseId"])
def test_xim_retired_callbacks_and_inputs_never_write(case, monkeypatch):
    actual = _run(case, monkeypatch)
    assert actual['expectedException'] is None
    assert actual['finalBusinessState']['config'] == case['initialConfig']
    assert actual['finalBusinessState']['updateAttempts'] == 0
    entry = case['entry']
    if entry['op'] == 'text' and entry['action'] != 'xim_unknown':
        assert actual['stateSteps'][-1]['state'] is None
        assert '已过期' in str(actual['tgApi'])
    elif entry.get('data') != 'xim:missing' and entry.get('action') != 'xim_unknown':
        assert '面板' in str(actual['tgApi'])


def test_auxiliary_segment_has_exact_five_ids_unique_cases_and_no_mauth():
    assert_capability_coverage({"TG-XIM-01", "TG-TL-01", "TG-STAT-01", "TG-UPD-01", "TG-IMG-01"}, ALL_CASES)
    assert len({case["caseId"] for case in ALL_CASES}) == len(ALL_CASES)
    assert not any("mauth:" in repr(case) for case in ALL_CASES)


def _source_families(function_name: str, variable: str) -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    found: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == variable:
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str): found.add(comparator.value)
                elif isinstance(comparator, (ast.Tuple, ast.Set)):
                    found.update(item.value for item in comparator.elts if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return found


def test_xim_case_and_family_coverage_is_bidirectional():
    assert_capability_coverage({"TG-XIM-01"}, CASES)
    assert {c["caseId"] for c in CASES} == EXPECTED_CASE_IDS
    assert {s["entry"]["data"] for s in SPECS if s["entry"]["op"] == "callback"} == EXPECTED_CALLBACKS
    assert {s["entry"]["action"] for s in SPECS if s["entry"]["op"] == "text"} == EXPECTED_STATES
    assert _source_families("handle_callback", "data") | {"xim:missing"} == EXPECTED_CALLBACKS
    assert _source_families("handle_text_state", "action") | {menu._IMAGE_MODELS_STATE, menu._VIDEO_MODELS_STATE, menu._JOB_TTL_STATE, menu._REQUEST_TIMEOUT_STATE, "xim_unknown"} == EXPECTED_STATES
