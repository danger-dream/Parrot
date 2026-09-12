"""Executable strict v0.31.13 baseline for TG-IMG-01."""
from __future__ import annotations

import ast
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src import config, image_db
from src.openai import images_simple
from src.telegram import bot, menu_cache, states, ui
from src.telegram.menus import image_menu as menu
from src.tests.tg_contract import assert_capability_coverage, assert_strict_equal, load_jsonl

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/auxiliary.jsonl"
CASES = [c for c in load_jsonl(SEGMENT) if c["capabilityId"] == "TG-IMG-01"] if SEGMENT.exists() else []
IMAGES = {"enabled": True, "cacheEnabled": False, "mainModel": "gpt-5.4-mini", "toolModel": "gpt-image-2", "cachePath": "images", "cacheRetentionDays": 30, "cacheMaxBytes": 1073741824, "disabledAccounts": []}
BASE = {"images": IMAGES}

def _short(tag: str) -> str: return hashlib.sha1(tag.encode()).hexdigest()[:8]
def _cfg(**patch: Any) -> dict[str, Any]:
    out = deepcopy(BASE); out["images"].update(patch); return out

def _spec(name: str, op: str, **entry: Any) -> dict[str, Any]:
    state = entry.pop("state", None); cfg = deepcopy(entry.pop("config", BASE)); runtime = deepcopy(entry.pop("runtime", {}))
    return {"caseId": f"TG-IMG-01.{name}", "capabilityId": "TG-IMG-01", "entry": {"op": op, **entry}, "initialConfig": cfg, "initialState": state or {}, "initialRuntime": runtime, "tgApi": [], "stateSteps": [], "finalBusinessState": {}, "expectedException": None}

ACC = "imgacc:OAuth:OpenAI:acct-1"
LOG = "imglog:41"
BADLOG = "imglog:not-int"
ACCOUNTS = [
    {"account_key": "OAuth:OpenAI:acct-1", "email": "one<&>@fake.invalid", "image_disabled": False, "enabled": True, "missing_account_id": False, "image_cooldown_until": 0},
    {"account_key": "acct-2", "email": "two@fake.invalid", "image_disabled": True, "enabled": False, "missing_account_id": False, "image_cooldown_until": 0},
    {"account_key": "acct-3", "email": "three@fake.invalid", "image_disabled": False, "enabled": True, "missing_account_id": True, "image_cooldown_until": 0},
    {"account_key": "acct-4", "email": "four@fake.invalid", "image_disabled": False, "enabled": True, "missing_account_id": False, "image_cooldown_until": 123},
]
SPECS = [
    _spec("show-preserves-input-state", "callback", data="img:show", state={"action": "img_set_main", "data": {}}),
    _spec("show-menu-alias", "callback", data="menu:images"),
    _spec("send-new", "send_new"),
    _spec("toggle-enabled", "callback", data="img:toggle"),
    _spec("toggle-cache", "callback", data="img:cache_toggle"),
    _spec("ask-main", "callback", data="img:set_main"),
    _spec("ask-tool", "callback", data="img:set_tool"),
    _spec("ask-path", "callback", data="img:set_path"),
    _spec("ask-retention", "callback", data="img:set_retention"),
    _spec("ask-max", "callback", data="img:set_max"),
    _spec("main-empty", "text", action="img_set_main", text=" "),
    _spec("main-save", "text", action="img_set_main", text=" model-<&> "),
    _spec("tool-empty", "text", action="img_set_tool", text=" "),
    _spec("tool-save", "text", action="img_set_tool", text=" image-model-x "),
    _spec("path-empty", "text", action="img_set_path", text=" "),
    _spec("path-save", "text", action="img_set_path", text=" ../fake/<cache> "),
    _spec("retention-invalid", "text", action="img_set_retention", text="-1"),
    _spec("retention-save", "text", action="img_set_retention", text="0"),
    _spec("max-invalid", "text", action="img_set_max", text="many"),
    _spec("max-save-fraction", "text", action="img_set_max", text="1.5MB"),
    _spec("max-save-zero", "text", action="img_set_max", text="0"),
    _spec("accounts-empty", "callback", data="img:accounts", runtime={"accounts": []}),
    _spec("accounts-statuses", "callback", data="img:accounts", runtime={"accounts": ACCOUNTS}),
    _spec("account-toggle-add", "callback", data=f"img:acc_toggle:{_short(ACC)}", runtime={"register": [ACC], "accounts": ACCOUNTS}),
    _spec("account-toggle-remove-case-insensitive", "callback", data=f"img:acc_toggle:{_short(ACC)}", config=_cfg(disabledAccounts=["oauth:openai:ACCT-1"]), runtime={"register": [ACC], "accounts": ACCOUNTS}),
    _spec("account-toggle-expired", "callback", data="img:acc_toggle:deadbeef", runtime={"accounts": []}),
    _spec("view-expired", "callback", data="img:view:deadbeef"),
    _spec("view-malformed-id", "callback", data=f"img:view:{_short(BADLOG)}", runtime={"register": [BADLOG]}),
    _spec("view-log-missing", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG]}),
    _spec("view-invalid-cache-json", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG], "row": {"id": 41, "cache_paths": "not-json", "action": "generate", "account_email": "a@fake.invalid"}}),
    _spec("view-cache-missing", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG], "row": {"id": 41, "pathNames": ["gone.png"], "action": "generate", "account_email": "a@fake.invalid"}}),
    _spec("view-generate-five-limit", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG], "row": {"id": 41, "pathNames": [f"p{i}.png" for i in range(6)], "action": "generate", "account_email": "a<&>@fake.invalid"}, "existing": [f"p{i}.png" for i in range(6)]}),
    _spec("view-edit-caption", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG], "row": {"id": 41, "pathNames": ["edit.png"], "action": "edit", "account_email": "edit@fake.invalid"}, "existing": ["edit.png"]}),
    _spec("view-db-failure", "callback", data=f"img:view:{_short(LOG)}", runtime={"register": [LOG], "dbFailure": "fake image db failed"}),
    _spec("media-log-navigation", "media_nav", data="media:logs"),
    _spec("unknown-callback", "callback", data="img:missing"),
    _spec("unknown-state", "text", action="img_unknown", text="x"),
]
EXPECTED_CASE_IDS = {s["caseId"] for s in SPECS}
EXPECTED_CALLBACK_FAMILIES = {"img:show", "menu:images", "img:toggle", "img:cache_toggle", "img:set_main", "img:set_tool", "img:set_path", "img:set_retention", "img:set_max", "img:accounts", "img:acc_toggle:*", "img:view:*", "media:logs", "img:missing"}
EXPECTED_STATES = {"img_set_main", "img_set_tool", "img_set_path", "img_set_retention", "img_set_max", "img_unknown"}

def _callback_family(data: str) -> str:
    for prefix in ("img:acc_toggle:", "img:view:"):
        if data.startswith(prefix): return prefix + "*"
    return data

def _source_families(function_name: str, variable: str) -> set[str]:
    tree = ast.parse(Path(menu.__file__).read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    found: set[str] = set()
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and node.args and isinstance(node.args[0], ast.Constant): found.add(node.args[0].value + "*")
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == variable:
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str): found.add(comparator.value)
                elif isinstance(comparator, (ast.Tuple, ast.Set)): found.update(x.value for x in comparator.elts if isinstance(x, ast.Constant) and isinstance(x.value, str))
    return found

class _Response:
    def json(self): return {"ok": True, "result": {}}

class _MultipartSession:
    def __init__(self, calls): self.calls = calls
    def post(self, url, json=None, data=None, files=None):
        if json is not None: payload = deepcopy(json)
        else:
            encoded = {}
            for field, item in (files or {}).items():
                raw = item[1].read() if hasattr(item[1], "read") else bytes(item[1])
                encoded[field] = {"filename": item[0], "contentHex": raw.hex(), "contentType": item[2] if len(item) > 2 else None}
            payload = {"data": deepcopy(data or {}), "files": encoded}
        self.calls.append({"method": url.rsplit("/", 1)[-1], "payload": payload}); return _Response()

def _run(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    store = deepcopy(case["initialConfig"]); rt = case["initialRuntime"]; calls: list[dict[str, Any]] = []; events: list[Any] = []
    def api(method, data=None): calls.append({"method": method, "payload": deepcopy(data or {})}); return {"ok": True, "result": {"message_id": 901}}
    def update(mutator): events.append("config.update"); mutator(store); return store
    def get_log(log_id):
        events.append(["get_log", log_id])
        if rt.get("dbFailure"): raise RuntimeError(rt["dbFailure"])
        row = deepcopy(rt.get("row"))
        if row and "pathNames" in row:
            names = row.pop("pathNames"); row["cache_paths"] = json.dumps([str(tmp_path / name) for name in names])
        return row
    states.clear_all(); ui._code_to_name.clear(); ui.configure("fake-token-img", [42])
    monkeypatch.setattr(states.time, "time", lambda: 1000.0); monkeypatch.setattr(config, "get", lambda: store); monkeypatch.setattr(config, "update", update); monkeypatch.setattr(ui, "api", api); monkeypatch.setattr(images_simple, "settings", lambda: deepcopy(store["images"])); monkeypatch.setattr(images_simple, "list_image_accounts", lambda include_disabled=True: deepcopy(rt.get("accounts", []))); monkeypatch.setattr(image_db, "get_log", get_log)
    existing = set(rt.get("existing", [])); monkeypatch.setattr(menu.os.path, "exists", lambda path: Path(path).name in existing)
    for name in existing: (tmp_path / name).write_bytes(("fake:" + name).encode())
    session = _MultipartSession(calls)
    monkeypatch.setattr(ui, "_session_lease", lambda: nullcontext(session))
    for tag in rt.get("register", []): ui.register_code(tag)
    initial = case["initialState"]
    if initial: states.set_state(42, initial["action"], deepcopy(initial.get("data") or {}))
    entry = case["entry"]
    if entry["op"] == "text" and not initial: states.set_state(42, entry["action"])
    before = deepcopy(states.get_state(42)); handled = None; error = None
    try:
        if entry["op"] == "callback": handled = menu.handle_callback(42, 77, "cb-img", entry["data"])
        elif entry["op"] == "text": handled = menu.handle_text_state(42, entry["action"], entry["text"])
        elif entry["op"] == "media_nav":
            monkeypatch.setattr(menu_cache, "begin_view", lambda chat, msg: events.append(["begin_view", chat, msg]))
            modules = [bot.status_menu, bot.help_menu, bot.oauth_menu, bot.oauth_account_models_menu, bot.xai_imagine_menu, bot.channel_menu, bot.stats_menu, bot.load_balancing_menu]
            for module in modules: monkeypatch.setattr(module, "handle_callback", lambda *args: False)
            monkeypatch.setattr(bot.media_logs_menu, "handle_callback", lambda *args: events.append("media_logs") or True)
            bot._handle_callback({"id": "cb-img", "message": {"chat": {"id": 42}, "message_id": 77}, "data": "media:logs"}); handled = True
        else: menu.send_new(42)
    except Exception as exc: error = {"type": type(exc).__name__, "message": str(exc)}
    return {"caseId": case["caseId"], "capabilityId": case["capabilityId"], "entry": deepcopy(entry), "initialConfig": deepcopy(case["initialConfig"]), "initialState": deepcopy(initial), "initialRuntime": deepcopy(rt), "tgApi": calls, "stateSteps": [{"after": "before", "state": before}, {"after": "invoke", "state": deepcopy(states.get_state(42))}], "finalBusinessState": {"handled": handled, "config": store, "events": events}, "expectedException": error}

@pytest.fixture(autouse=True)
def _cleanup():
    states.clear_all(); ui._code_to_name.clear(); yield; states.clear_all(); ui._code_to_name.clear()

@pytest.mark.parametrize("case", CASES, ids=lambda c: c["caseId"])
def test_image_strict_trace(case, monkeypatch, tmp_path): assert_strict_equal(case, _run(case, monkeypatch, tmp_path))

def test_image_case_callback_and_state_coverage_is_bidirectional():
    assert_capability_coverage({"TG-IMG-01"}, CASES); assert {c["caseId"] for c in CASES} == EXPECTED_CASE_IDS
    assert {_callback_family(c["entry"]["data"]) for c in CASES if c["entry"]["op"] in ("callback", "media_nav")} == EXPECTED_CALLBACK_FAMILIES
    assert {c["entry"]["action"] for c in CASES if c["entry"]["op"] == "text"} == EXPECTED_STATES
    assert not any(call["method"] == "sendDocument" for c in CASES for call in c["tgApi"])
    assert _source_families("handle_callback", "data") | {"media:logs", "img:missing"} == EXPECTED_CALLBACK_FAMILIES
    assert _source_families("handle_text_state", "action") | {"img_unknown"} == EXPECTED_STATES
