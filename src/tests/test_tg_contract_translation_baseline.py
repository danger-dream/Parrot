"""Executable strict v0.31.13 baseline for TG-TL-01."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from src import config, translation
from src.channel import registry
from src.telegram import states, ui
from src.telegram.menus import translation_menu as menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl

from src.tests.tg_contract.current import load_current_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/auxiliary.jsonl"
CASES = [c for c in load_current_jsonl(SEGMENT) if c["capabilityId"] == "TG-TL-01"] if SEGMENT.exists() else []
TL = {"enabled": False, "model": "model-a", "fallbackModel": "model-b", "targetLanguage": "English", "prompt": "", "timeoutSeconds": 10, "maxHistoryMessages": 20, "cacheTtlDays": 3, "cachePreloadCount": 100, "failureAlertThreshold": 10, "memoryCacheMaxMb": 100, "memoryCacheTtlSeconds": 7200, "translateSystemMessages": False, "scope": {"models": [], "channels": []}, "modelOverrides": {}}
BASE = {"translation": TL}

def _short(tag: str) -> str:
    return hashlib.sha1(tag.encode()).hexdigest()[:8]

def _cfg(**patch: Any) -> dict[str, Any]:
    out = deepcopy(BASE); out["translation"].update(patch); return out

def _spec(name: str, op: str, **entry: Any) -> dict[str, Any]:
    state = entry.pop("state", None); cfg = deepcopy(entry.pop("config", BASE)); runtime = deepcopy(entry.pop("runtime", {}))
    return {"caseId": f"TG-TL-01.{name}", "capabilityId": "TG-TL-01", "entry": {"op": op, **entry}, "initialConfig": cfg, "initialState": state or {}, "initialRuntime": runtime, "tgApi": [], "stateSteps": [], "finalBusinessState": {}, "expectedException": None}

NUMERIC = {"timeout": "12", "max_hist": "21", "ttl": "4", "preload": "101", "alert": "11", "mem_mb": "101", "mem_ttl": "7201"}
MODEL_TAG = "tl:m:model-<&>"
SCOPE_MODEL_TAG = "tl:scope:model:model-11"
SCOPE_CHANNEL_TAG = "tl:scope:channel:oauth:openai:acct-1"
SPECS = [
    _spec("show-cancels-state", "callback", data="tl:show", state={"action": "tl_prompt", "data": {}}),
    _spec("send-new", "send_new"),
    _spec("toggle-disable", "callback", data="tl:toggle", config=_cfg(enabled=True)),
    _spec("toggle-enable", "callback", data="tl:toggle"),
    _spec("toggle-enable-not-ready", "callback", data="tl:toggle", runtime={"ready": [False, "缺少 <model>"]}),
    _spec("toggle-system", "callback", data="tl:toggle:system"),
    _spec("model-picker-empty", "callback", data="tl:edit:model:0", runtime={"models": []}),
    _spec("model-picker-page-clamp", "callback", data="tl:edit:model:99", runtime={"models": [f"model-{i}" for i in range(12)]}),
    _spec("fallback-picker", "callback", data="tl:edit:fallback:bad", runtime={"models": ["model-a", "model-b"]}),
    _spec("pick-model", "callback", data=f"tl:pick:model:{_short(MODEL_TAG)}", runtime={"register": [MODEL_TAG]}),
    _spec("pick-model-expired", "callback", data="tl:pick:model:deadbeef"),
    _spec("pick-fallback", "callback", data=f"tl:pick:fallback:{_short(MODEL_TAG)}", runtime={"register": [MODEL_TAG]}),
    _spec("pick-fallback-expired", "callback", data="tl:pick:fallback:deadbeef"),
    _spec("clear-fallback", "callback", data="tl:clear:fallback"),
    _spec("language-picker", "callback", data="tl:edit:lang"),
    _spec("pick-language", "callback", data="tl:pick:lang:Japanese"),
    _spec("pick-language-unvalidated", "callback", data="tl:pick:lang:<bad>"),
    *[_spec(f"ask-{key}", "callback", data=f"tl:edit:{key}") for key in NUMERIC],
    *[_spec(f"save-{key}", "text", action=f"tl_{key}", text=value) for key, value in NUMERIC.items()],
    _spec("numeric-not-int", "text", action="tl_timeout", text="ten"),
    _spec("numeric-out-of-range", "text", action="tl_timeout", text="61"),
    _spec("scope-models-first", "callback", data="tl:scope:models:0", runtime={"models": [f"model-{i}" for i in range(12)]}),
    _spec("scope-models-last", "callback", data="tl:scope:models:1", config=_cfg(scope={"models": ["model-11"], "channels": []}), runtime={"models": [f"model-{i}" for i in range(12)]}),
    _spec("scope-models-clear", "callback", data="tl:scope:models:clear", config=_cfg(scope={"models": ["model-1"], "channels": []}), runtime={"models": ["model-1"]}),
    _spec("scope-model-toggle", "callback", data=f"tl:scope:model:{_short(SCOPE_MODEL_TAG)}", runtime={"register": [SCOPE_MODEL_TAG], "models": ["model-11"]}),
    _spec("scope-model-expired", "callback", data="tl:scope:model:deadbeef"),
    _spec("scope-channels-first", "callback", data="tl:scope:channels:0", runtime={"channels": [[f"api:{i}", f"🔑 Channel {i}"] for i in range(12)]}),
    _spec("scope-channels-last", "callback", data="tl:scope:channels:1", config=_cfg(scope={"models": [], "channels": ["api:11"]}), runtime={"channels": [[f"api:{i}", f"🔑 Channel {i}"] for i in range(12)]}),
    _spec("scope-channels-clear", "callback", data="tl:scope:channels:clear", config=_cfg(scope={"models": [], "channels": ["api:1"]}), runtime={"channels": [["api:1", "🔑 Channel 1"]]}),
    _spec("scope-channel-toggle", "callback", data=f"tl:scope:channel:{_short(SCOPE_CHANNEL_TAG)}", runtime={"register": [SCOPE_CHANNEL_TAG], "channels": [["oauth:openai:acct-1", "OpenAI account"]]}),
    _spec("scope-channel-expired", "callback", data="tl:scope:channel:deadbeef"),
    _spec("params-no-model", "callback", data="tl:show:params", config=_cfg(model="")),
    _spec("params-cancel-preserves-state", "callback", data="tl:show:params", state={"action": "tl_params_body", "data": {}}, config=_cfg(modelOverrides={"model-a": {"body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high", "temperature": 0}}})),
    _spec("thinking-high", "callback", data="tl:params:think:high"),
    _spec("thinking-max", "callback", data="tl:params:think:max"),
    _spec("thinking-off", "callback", data="tl:params:think:off", config=_cfg(modelOverrides={"model-a": {"body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high", "keep": True}}})),
    _spec("thinking-unknown", "callback", data="tl:params:think:low"),
    _spec("thinking-no-model", "callback", data="tl:params:think:high", config=_cfg(model="")),
    _spec("params-clear", "callback", data="tl:params:clear", config=_cfg(modelOverrides={"model-a": {"body": {"x": 1}}})),
    _spec("params-clear-no-model", "callback", data="tl:params:clear", config=_cfg(model="")),
    _spec("body-ask", "callback", data="tl:params:body", config=_cfg(modelOverrides={"model-a": {"body": {"x": "<&>"}}})),
    _spec("body-ask-no-model", "callback", data="tl:params:body", config=_cfg(model="")),
    _spec("body-json-error", "text", action="tl_params_body", text="{"),
    _spec("body-not-object", "text", action="tl_params_body", text="[1]"),
    _spec("body-sanitize", "text", action="tl_params_body", text='{"_parrot_secret":1,"temperature":0,"thinking":{"type":"enabled"}}'),
    _spec("body-empty", "text", action="tl_params_body", text="{}", config=_cfg(modelOverrides={"model-a": {"body": {"x": 1}}})),
    _spec("body-model-disappeared", "text", action="tl_params_body", text="{}", config=_cfg(model="")),
    _spec("prompt-cancel-preserves-state", "callback", data="tl:show:prompt", state={"action": "tl_prompt", "data": {}}),
    _spec("prompt-show-custom", "callback", data="tl:show:prompt", config=_cfg(prompt="Translate <&> to {target_language}")),
    _spec("prompt-ask", "callback", data="tl:edit:prompt"),
    _spec("prompt-empty", "text", action="tl_prompt", text="   "),
    _spec("prompt-save", "text", action="tl_prompt", text=" Translate <&> to {target_language} \n"),
    _spec("prompt-reset", "callback", data="tl:prompt:reset", config=_cfg(prompt="custom")),
    _spec("test-not-ready", "callback", data="tl:test", runtime={"ready": [False, "fake unavailable"]}),
    _spec("test-ask", "callback", data="tl:test"),
    _spec("test-empty", "text", action="tl_test", text=" "),
    _spec("test-success-cached", "text", action="tl_test", text="原文<&>", runtime={"testResult": {"ok": True, "cached": True, "targetLanguage": "English", "original": "原文<&>", "translated": "translation<&>"}, "sendMessageId": 901}),
    _spec("test-failure-send-fallback", "text", action="tl_test", text="bad<&>", runtime={"testResult": {"ok": False, "reason": "provider <down>"}, "sendMessageId": None}),
    _spec("cache-ask", "callback", data="tl:cache:clear", runtime={"cacheCount": 7}),
    _spec("cache-confirm", "callback", data="tl:cache:confirm", runtime={"clearCount": 7}),
    _spec("unknown-callback", "callback", data="tl:missing"),
    _spec("unknown-state", "text", action="tl_missing", text="x"),
]
EXPECTED_CASE_IDS = {s["caseId"] for s in SPECS}
EXPECTED_STATES = {f"tl_{k}" for k in NUMERIC} | {"tl_prompt", "tl_params_body", "tl_test", "tl_missing"}
EXPECTED_CALLBACK_FAMILIES = {
    "tl:show", "tl:toggle", "tl:toggle:system", "tl:test",
    "tl:edit:model:*", "tl:edit:fallback:*", "tl:pick:model:*", "tl:pick:fallback:*", "tl:clear:fallback",
    "tl:edit:lang", "tl:pick:lang:*",
    *(f"tl:edit:{key}" for key in NUMERIC),
    "tl:scope:models:*", "tl:scope:channels:*", "tl:scope:model:*", "tl:scope:channel:*",
    "tl:show:params", "tl:params:think:*", "tl:params:clear", "tl:params:body",
    "tl:show:prompt", "tl:edit:prompt", "tl:prompt:reset", "tl:cache:clear", "tl:cache:confirm", "tl:missing",
}

def _callback_family(data: str) -> str:
    for prefix in ("tl:edit:model:", "tl:edit:fallback:", "tl:pick:model:", "tl:pick:fallback:", "tl:pick:lang:", "tl:scope:models:", "tl:scope:channels:", "tl:scope:model:", "tl:scope:channel:", "tl:params:think:"):
        if data.startswith(prefix): return prefix + "*"
    return data

def _source_families(function_name: str, variable: str) -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    found: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and isinstance(node.func.value, ast.Name) and node.func.value.id == variable and node.args and isinstance(node.args[0], ast.Constant): found.add(node.args[0].value + "*")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == variable:
            found.update(x.value for x in node.comparators if isinstance(x, ast.Constant) and isinstance(x.value, str))
    return found

def _run(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = deepcopy(case["initialConfig"]); rt = case["initialRuntime"]; calls: list[dict[str, Any]] = []; events: list[Any] = []
    def api(method, data=None):
        calls.append({"method": method, "payload": deepcopy(data or {})})
        mid = rt.get("sendMessageId", 901)
        return {"ok": True, "result": {} if mid is None else {"message_id": mid}}
    def update(mutator):
        events.append("config.update"); mutator(store); return store
    async def translate_fake(sample):
        events.append(["translate", sample]); return deepcopy(rt.get("testResult", {"ok": True, "original": sample, "translated": "translated", "targetLanguage": "English"}))
    states.clear_all(); ui._code_to_name.clear(); ui.configure("fake-token-tl", [42])
    monkeypatch.setattr(states.time, "time", lambda: 1000.0); monkeypatch.setattr(config, "get", lambda: store); monkeypatch.setattr(config, "update", update); monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(registry, "available_models", lambda: list(rt.get("models", ["model-a", "model-b"])))
    monkeypatch.setattr(menu, "_channel_scope_items", lambda: [tuple(x) for x in rt.get("channels", [["api:one", "🔑 One"]])])
    monkeypatch.setattr(translation, "validate_ready", lambda cfg, require_enabled=False: tuple(rt.get("ready", [True, ""])))
    monkeypatch.setattr(translation, "cache_count", lambda: int(rt.get("cacheCount", 3))); monkeypatch.setattr(translation, "cache_hit_stats", lambda: {"memoryBytes": 1536, "memoryEntries": 2, "hits": 5, "misses": 1})
    monkeypatch.setattr(translation, "clear_cache", lambda: int(rt.get("clearCount", 3))); monkeypatch.setattr(translation, "translate_text_for_test", translate_fake); monkeypatch.setattr(menu, "_SYNC_TEST", True)
    for tag in rt.get("register", []): ui.register_code(tag)
    initial = case["initialState"]
    if initial: states.set_state(42, initial["action"], deepcopy(initial.get("data") or {}))
    entry = case["entry"]
    if entry["op"] == "text" and not initial: states.set_state(42, entry["action"])
    before = deepcopy(states.get_state(42)); handled = None; error = None
    try:
        if entry["op"] == "callback": handled = menu.handle_callback(42, 77, "cb-tl", entry["data"])
        elif entry["op"] == "text": handled = menu.handle_text_state(42, entry["action"], entry["text"])
        else: menu.send_new(42)
    except Exception as exc: error = {"type": type(exc).__name__, "message": str(exc)}
    after = deepcopy(states.get_state(42))
    return {"caseId": case["caseId"], "capabilityId": case["capabilityId"], "entry": deepcopy(entry), "initialConfig": deepcopy(case["initialConfig"]), "initialState": deepcopy(initial), "initialRuntime": deepcopy(rt), "tgApi": calls, "stateSteps": [{"after": "before", "state": before}, {"after": "invoke", "state": after}], "finalBusinessState": {"handled": handled, "config": store, "events": events}, "expectedException": error}

@pytest.fixture(autouse=True)
def _cleanup():
    states.clear_all(); ui._code_to_name.clear(); yield; states.clear_all(); ui._code_to_name.clear()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["caseId"])
def test_translation_strict_trace(case, monkeypatch):
    assert_strict_equal(case, _run(case, monkeypatch))

def test_translation_case_callback_and_state_coverage_is_bidirectional():
    assert_capability_coverage({"TG-TL-01"}, CASES)
    assert {c["caseId"] for c in CASES} == EXPECTED_CASE_IDS
    assert {_callback_family(c["entry"]["data"]) for c in CASES if c["entry"]["op"] == "callback"} == EXPECTED_CALLBACK_FAMILIES
    assert {c["entry"]["action"] for c in CASES if c["entry"]["op"] == "text"} == EXPECTED_STATES
    assert _source_families("handle_callback", "data") | {f"tl:edit:{key}" for key in NUMERIC} | {"tl:missing"} == EXPECTED_CALLBACK_FAMILIES
    assert _source_families("handle_text_state", "action") | {f"tl_{key}" for key in NUMERIC} | {"tl_missing"} == EXPECTED_STATES
