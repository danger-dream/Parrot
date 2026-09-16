"""Executable v0.31.13 Telegram traces for TG-CORE-01 through TG-CORE-04."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src import notifier
from src.telegram import bot, menu_cache, states, ui
from src.tests.tg_contract import (
    CORE_CAPABILITY_IDS,
    StrictMismatch,
    TraceCapture,
    assert_capability_coverage,
    assert_strict_equal,
    load_jsonl,
    replay_and_compare,
)


SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/core.jsonl"
from src.tests.tg_contract.current import load_current_jsonl
CASES = load_current_jsonl(SEGMENT)
CASES_01_04 = [case for case in CASES if case["capabilityId"] <= "TG-CORE-04"]

CALLBACK_HANDLERS = [
    ("status", bot.status_menu),
    ("help", bot.help_menu),
    ("model_center", bot.model_center_menu),
    ("oauth", bot.oauth_menu),
    ("oauth_account_models", bot.oauth_account_models_menu),
    ("image", bot.image_menu),
    ("xai_imagine", bot.xai_imagine_menu),
    ("channel", bot.channel_menu),
    ("stats", bot.stats_menu),
    ("load_balancing", bot.load_balancing_menu),
    ("media_logs", bot.media_logs_menu),
    ("logs", bot.logs_menu),
    ("status_alert", bot.status_alert_menu),
    ("update", bot.update_menu),
    ("translation", bot.translation_menu),
    ("proxy", bot.proxy_menu),
    ("system", bot.system_menu),
    ("apikey", bot.apikey_menu),
    ("mapping", bot.mapping_menu),
    ("oauth_defaults", bot.oauth_defaults_menu),
]

COMMAND_TARGETS = {
    "start": (bot.main_menu, "on_start_command"),
    "menu": (bot.main_menu, "on_menu_command"),
    "stats": (bot.stats_menu, "send_new"),
    "logs": (bot.logs_menu, "send_new"),
    "channels": (bot.channel_menu, "send_new"),
    "oauth": (bot.oauth_menu, "send_new"),
    "keys": (bot.apikey_menu, "send_new"),
    "settings": (bot.system_menu, "send_new"),
    "model_center": (bot.model_center_menu, "send_new"),
    "loadbalancing": (bot.load_balancing_menu, "send_new"),
    "oauth_defaults": (bot.oauth_defaults_menu, "send_new"),
    "help": (bot.help_menu, "send_new"),
}


def _actual(
    case: dict[str, Any],
    *,
    tg_api: list[dict[str, Any]],
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
        "tgApi": deepcopy(tg_api),
        "stateSteps": deepcopy(state_steps),
        "finalBusinessState": deepcopy(final),
        "expectedException": deepcopy(exception),
    }


@pytest.fixture(autouse=True)
def _reset_tg_globals():
    states.clear_all()
    bot.model_center_menu.reset_for_tests()
    ui.configure("fake-token-core", [42])
    ui._session = None
    bot._offset = 0
    bot._running = False
    bot._thread = None
    notifier.set_handler(None)
    yield
    bot._running = False
    states.clear_all()
    notifier.set_handler(None)
    ui._session = None


def _run_admin_allowlist(case, monkeypatch):
    ui.configure("fake-token-core", [42, "43", "bad", None])
    restricted = {
        "adminIds": sorted(ui.admin_ids()),
        "checks": [ui.is_admin(42), ui.is_admin("43"), ui.is_admin(99), ui.is_admin("bad")],
    }
    ui.configure("fake-token-core", [])
    restricted["emptyListAllows"] = ui.is_admin(999)
    return _actual(case, tg_api=[], state_steps=[], final=restricted)


def _run_unauthorized(case, monkeypatch):
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    ui.configure("fake-token-core", [42])
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"authorized": False, "dispatchStopped": True},
    )


def _run_drop_pending(case, monkeypatch):
    capture = TraceCapture({
        "getUpdates": {"ok": True, "result": [{"update_id": 700}]},
    })
    monkeypatch.setattr(ui, "api", capture.api)
    bot._offset = case["initialRuntime"]["offset"]
    bot._drop_pending_updates()
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"after": "drop_pending_updates", "offset": bot._offset}],
        final={"offset": bot._offset},
    )


def _run_lifecycle(case, monkeypatch):
    events: list[dict[str, Any]] = []
    capture = TraceCapture({"getUpdates": {"ok": True, "result": []}})

    def api(method, data=None):
        events.append({"event": "tg", "method": method})
        return capture.api(method, data)

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target, self.daemon, self.name = target, daemon, name
            events.append({"event": "thread_created", "daemon": daemon, "name": name})

        def start(self):
            events.append({"event": "thread_started", "name": self.name})

    monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(ui, "install_notify_handler", lambda: events.append({"event": "notifier_install"}))
    monkeypatch.setattr(ui, "close_session", lambda: events.append({"event": "session_close"}))
    monkeypatch.setattr(menu_cache, "start", lambda: events.append({"event": "scheduler_start"}))
    monkeypatch.setattr(menu_cache, "stop", lambda: events.append({"event": "scheduler_stop"}))
    monkeypatch.setattr(bot.threading, "Thread", FakeThread)
    ui.configure("fake-token-core", [42])
    bot.start()
    after_start = {"after": "start", "running": bot._running, "threadName": bot._thread.name}
    bot.stop()
    after_stop = {"after": "stop", "running": bot._running, "threadName": bot._thread.name}
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[after_start, after_stop],
        final={"events": events, "running": bot._running},
    )


def _run_poll_offset(case, monkeypatch):
    capture = TraceCapture()
    handled: list[int] = []

    def api(method, data=None):
        capture.record(method, {} if data is None else data)
        bot._running = False
        return {
            "ok": True,
            "result": [{"update_id": 40, "message": {"chat": {"id": 42}, "text": "/menu"}}],
        }

    monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(bot, "_handle_update", lambda update: handled.append(update["update_id"]))
    bot._offset = case["initialRuntime"]["offset"]
    bot._running = True
    bot._poll_loop()
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"afterUpdateId": 40, "offset": bot._offset}],
        final={"handledUpdateIds": handled, "offset": bot._offset, "running": bot._running},
    )


def _run_notifier(case, monkeypatch):
    installed: list[Any] = []
    sent_meta: list[int] = []
    capture = TraceCapture({
        "sendMessage": [
            {"ok": True, "result": {"message_id": 501}},
            {"ok": True, "result": {"message_id": 502}},
        ],
    })
    monkeypatch.setattr(notifier, "set_handler", lambda handler: installed.append(handler))
    monkeypatch.setattr(ui, "api", capture.api)
    ui.configure("fake-token-core", [11, 22])
    ui.install_notify_handler()
    installed[0](
        "<b>告警</b>\nA\u00a0B",
        reply_markup={"inline_keyboard": [[{"text": "查看", "callback_data": "menu:logs"}]]},
        meta={"on_sent": lambda chat_id, message_id: sent_meta.extend([chat_id, message_id])},
    )
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"handlerInstalled": True, "onSent": sent_meta},
    )


def _run_commands(case, monkeypatch):
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(bot, "_drop_pending_updates", lambda: None)
    monkeypatch.setattr(ui, "install_notify_handler", lambda: None)
    monkeypatch.setattr(menu_cache, "start", lambda: None)

    class FakeThread:
        def __init__(self, **kwargs):
            self.name = kwargs["name"]

        def start(self):
            pass

    monkeypatch.setattr(bot.threading, "Thread", FakeThread)
    ui.configure("fake-token-core", [42])
    bot.start()
    commands = capture.calls[1]["payload"]["commands"]
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"commandCount": len(commands), "orderedCommands": [item["command"] for item in commands]},
    )


def _run_command_dispatch(case, monkeypatch):
    capture = TraceCapture()
    routes: list[str] = []
    monkeypatch.setattr(ui, "api", capture.api)
    for route_name, (module, attribute) in COMMAND_TARGETS.items():
        monkeypatch.setattr(module, attribute, lambda chat_id, n=route_name: routes.append(n))
    admin_ids = case["initialConfig"]["telegram"]["adminIds"]
    ui.configure("fake-token-core", admin_ids)
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"routeEvents": routes, "authorized": ui.is_admin(case["entry"]["update"]["message"]["chat"]["id"])},
    )


def _run_callback_dispatch(case, monkeypatch):
    capture = TraceCapture()
    visits: list[str] = []
    selected = case["entry"]["selectedHandler"]
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(menu_cache, "begin_view", lambda chat_id, message_id: None)
    monkeypatch.setattr(bot.main_menu, "handle_back", lambda *args: visits.append("main"))
    for name, module in CALLBACK_HANDLERS:
        def handler(chat_id, message_id, cb_id, data, n=name):
            visits.append(n)
            return n == selected
        monkeypatch.setattr(module, "handle_callback", handler)
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"handlerVisits": visits, "selectedHandler": selected},
    )


RUNNERS = {
    "admin_allowlist": _run_admin_allowlist,
    "unauthorized_message": _run_unauthorized,
    "unauthorized_callback": _run_unauthorized,
    "drop_pending": _run_drop_pending,
    "lifecycle": _run_lifecycle,
    "poll_offset": _run_poll_offset,
    "notifier": _run_notifier,
    "commands": _run_commands,
    "command_dispatch": _run_command_dispatch,
    "callback_dispatch": _run_callback_dispatch,
}


@pytest.mark.parametrize("case", CASES_01_04, ids=lambda case: case["caseId"])
def test_core_01_to_04_trace(case, monkeypatch):
    actual = RUNNERS[case["entry"]["scenario"]](case, monkeypatch)
    assert_strict_equal(case, actual)


def test_core_fragment_schema_unique_ids_and_bidirectional_capability_coverage():
    assert_capability_coverage(CORE_CAPABILITY_IDS, CASES)
    assert len({case["caseId"] for case in CASES}) == len(CASES)
    # `mauth:` is the contract-authorized P0 isolated callback surface, not
    # existing v0.31.13 callback behavior and therefore must not be frozen here.
    assert not any(
        case["entry"].get("update", {}).get("callback_query", {}).get("data", "").startswith("mauth:")
        for case in CASES
    )


def test_strict_comparator_and_replay_have_no_normalization_or_ignore_policy():
    canonical = {
        "text": "<b>A</b>\nB\u00a0C",
        "parse_mode": "HTML",
        "flag": True,
        "reply_markup": {"inline_keyboard": [
            [{"text": "一", "callback_data": "x:1"}],
            [{"text": "二", "callback_data": "x:2"}],
        ]},
    }
    variants = []
    for mutate in (
        lambda value: value.__setitem__("text", "<b>A</b> B\u00a0C"),
        lambda value: value.__setitem__("text", "<b>A</b>\nB C"),
        lambda value: value.pop("parse_mode"),
        lambda value: value.__setitem__("flag", 1),
        lambda value: value["reply_markup"]["inline_keyboard"].reverse(),
    ):
        variant = deepcopy(canonical)
        mutate(variant)
        variants.append(variant)
    for variant in variants:
        with pytest.raises(StrictMismatch):
            assert_strict_equal(canonical, variant)

    calls = [{"method": "sendMessage", "payload": canonical}]
    received = []
    replay_and_compare(calls, lambda method, payload: received.append((method, payload)))
    assert received == [("sendMessage", canonical)]
