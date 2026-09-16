"""v0.31.13 traces, with explicit 2026-09-10 authorized cold-stats UX deltas."""

from __future__ import annotations

from copy import deepcopy
import itertools
import json
from pathlib import Path
import re
from typing import Any

import pytest

from src import concurrency, config, log_db, status_monitor, update_checker
from src.telegram import menu_cache, ui
from src.telegram.menus import stats_menu
from src.tests.tg_contract import (
    TraceCapture,
    assert_capability_coverage,
    assert_strict_equal,
    load_jsonl,
)

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/observability.jsonl"
ALL_CASES = load_jsonl(SEGMENT)
OWN_CAPABILITIES = {"TG-CACHE-01", "TG-STATS-01", "TG-STATS-02"}
CASES = [case for case in ALL_CASES if case["capabilityId"] in OWN_CAPABILITIES]
EXPECTED_CAPABILITIES = {
    "TG-CACHE-01", "TG-STATS-01", "TG-STATS-02", "TG-LOG-01",
    "TG-LOG-02", "TG-LOG-03", "TG-LOG-04", "TG-MEDIA-01",
}


class Capture(TraceCapture):
    """Record the raw payload handed to every fake Telegram transport."""

    def media(self, method: str, chat_id: int, path: str, caption: str = "") -> dict:
        self.record(method, {"chat_id": chat_id, "path": path, "caption": caption})
        return {"ok": True, "result": {}}

    def document(self, chat_id: int, text: str, *, filename: str, caption: str = "") -> dict:
        self.record("sendDocument", {
            "chat_id": chat_id, "text": text, "filename": filename, "caption": caption,
            "content_type": "text/plain; charset=utf-8",
        })
        return {"ok": True, "result": {}}


def actual_case(
    case: dict[str, Any], calls: list[dict[str, Any]], steps: list[dict[str, Any]],
    final: dict[str, Any], exception: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "caseId": case["caseId"],
        "capabilityId": case["capabilityId"],
        "entry": deepcopy(case["entry"]),
        "initialConfig": deepcopy(case["initialConfig"]),
        "initialState": deepcopy(case["initialState"]),
        "initialRuntime": deepcopy(case["initialRuntime"]),
        "tgApi": deepcopy(calls),
        "stateSteps": deepcopy(steps),
        "finalBusinessState": deepcopy(final),
        "expectedException": deepcopy(exception),
    }


def install_config(monkeypatch, initial: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(initial)
    monkeypatch.setattr(config, "get", lambda: current)

    def update(mutator):
        mutator(current)
        return current

    monkeypatch.setattr(config, "update", update)
    return current


def install_common(monkeypatch, case: dict[str, Any]) -> tuple[Capture, dict[str, Any]]:
    capture = Capture()
    current = install_config(monkeypatch, case["initialConfig"])
    monkeypatch.setattr(ui, "api", capture.api)
    ui._code_to_name.clear()
    monkeypatch.setattr(status_monitor, "get_active_summary", lambda: case["initialRuntime"].get("statusBanner", ""))
    monkeypatch.setattr(update_checker, "get_update_banner", lambda: case["initialRuntime"].get("updateBanner", ""))
    monkeypatch.setattr(concurrency, "totals", lambda: deepcopy(case["initialRuntime"].get(
        "concurrency", {"in_flight": 0, "waiting": 0, "tracked_channels": 0},
    )))
    monkeypatch.setattr(ui, "channel_display_name", lambda key, with_family=False: str(key).split(":", 1)[-1])
    monkeypatch.setattr(ui, "channel_provider", lambda key: "")
    monkeypatch.setattr(ui, "channel_provider_custom_emoji_html", lambda key: "")
    monkeypatch.setattr(log_db, "cost_for_log", lambda row: {
        "cost_ticks": int((row or {}).get("cost_ticks") or 0),
        "costed_success": int((row or {}).get("costed_success") or 0),
        "unpriced_success": int((row or {}).get("unpriced_success") or 0),
    })
    return capture, current


def _run_boundaries(case, monkeypatch):
    from datetime import datetime

    class FakeDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.fromisoformat(case["initialRuntime"]["now"]).astimezone(tz)

    monkeypatch.setattr(menu_cache, "datetime", FakeDateTime)
    final = {
        "todayStart": int(menu_cache.today_start_ts()),
        "monthStart": int(menu_cache.month_start_ts()),
        "initializationText": menu_cache.initialization_text(),
    }
    return actual_case(case, [], [], final)


def _run_swr(case, monkeypatch):
    now = [float(case["initialRuntime"]["monotonic"])]
    monkeypatch.setattr(menu_cache.time, "monotonic", lambda: now[0])
    queued: list[dict[str, Any]] = []

    def enqueue(cache, key, loader, generation):
        queued.append({"key": key, "generation": generation, "loader": loader, "cache": cache})

    monkeypatch.setattr(menu_cache.COORDINATOR, "enqueue", enqueue)
    cache = menu_cache.SWRCache(60)
    callbacks: list[dict[str, Any]] = []
    first = cache.request(
        "period", lambda: {"version": 1}, subscriber=(42, 77, 1),
        on_ready=lambda value, error: callbacks.append({
            "subscriber": 1, "value": value, "error": None if error is None else str(error),
        }),
    )
    second = cache.request(
        "period", lambda: {"version": 99}, subscriber=(42, 77, 1),
        on_ready=lambda value, error: callbacks.append({
            "subscriber": 1, "value": value, "error": None if error is None else str(error),
        }),
    )
    queued[0]["cache"]._execute_reserved("period", queued[0]["loader"], queued[0]["generation"])
    fresh = cache.peek("period")
    now[0] += 61
    stale = cache.request(
        "period", lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
        subscriber=(42, 77, 2), on_ready=lambda value, error: callbacks.append({
            "subscriber": 2, "value": value, "error": None if error is None else str(error),
        }),
    )
    queued[1]["cache"]._execute_reserved("period", queued[1]["loader"], queued[1]["generation"])
    after_error = cache.peek("period")
    final = {
        "first": {"value": first.value, "fresh": first.fresh, "refreshing": first.refreshing},
        "second": {"value": second.value, "fresh": second.fresh, "refreshing": second.refreshing},
        "fresh": {"value": fresh.value, "fresh": fresh.fresh, "refreshing": fresh.refreshing},
        "stale": {"value": stale.value, "fresh": stale.fresh, "refreshing": stale.refreshing},
        "afterError": {"value": after_error.value, "fresh": after_error.fresh, "refreshing": after_error.refreshing},
        "enqueued": len(queued), "callbacks": callbacks,
    }
    return actual_case(case, [], [], final)


def _run_scheduler(case, monkeypatch):
    events: list[dict[str, Any]] = []

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target, self.daemon, self.name = target, daemon, name
            self.alive = False
            events.append({"event": "created", "name": name, "daemon": daemon})

        def start(self):
            self.alive = True
            events.append({"event": "started", "name": self.name})

        def is_alive(self):
            return self.alive

        def join(self):
            events.append({"event": "joined", "name": self.name})
            self.alive = False

    monkeypatch.setattr(menu_cache.threading, "Thread", FakeThread)
    monkeypatch.setattr(menu_cache.time, "monotonic", lambda: case["initialRuntime"]["monotonic"])
    coordinator = menu_cache.StatsRefreshCoordinator()
    coordinator.register_periodic("first", 60, lambda: True, priority=0)
    coordinator.start()
    coordinator.start()
    running = coordinator.running
    thread_name = coordinator.thread.name
    coordinator.stop()
    final = {
        "events": events, "threadName": thread_name, "runningBeforeStop": running,
        "runningAfterStop": coordinator.running, "maxActiveJobs": coordinator.max_active_jobs,
    }
    return actual_case(case, [], [], final)


def _run_view_token_lock(case, monkeypatch):
    menu_cache.reset_for_tests()
    # reset_for_tests clears subscribers/locks but intentionally leaves the
    # process-wide monotonic token source alive. Pin that dynamic boundary so
    # this strict trace is independent of earlier tests in the same process.
    monkeypatch.setattr(menu_cache, "_view_counter", itertools.count(1))
    events: list[str] = []
    first = menu_cache.begin_view(42, 77)
    second = menu_cache.begin_view(42, 77)
    other = menu_cache.begin_view(42, 78)
    old_ran = menu_cache.run_if_current(42, 77, first, lambda: events.append("old"))
    current_ran = menu_cache.run_if_current(42, 77, second, lambda: events.append("current"))
    lock_same = menu_cache._message_lock(42, 77) is menu_cache._message_lock(42, 77)
    final = {
        "tokensIncrease": [second - first, other - second],
        "firstCurrent": menu_cache.is_current_view(42, 77, first),
        "secondCurrent": menu_cache.is_current_view(42, 77, second),
        "subscriber": list(menu_cache.subscriber(42, 77, second)),
        "oldRan": old_ran, "currentRan": current_ran, "events": events,
        "sameMessageLock": lock_same,
    }
    return actual_case(case, [], [], final)


def _run_reset(case, monkeypatch):
    monkeypatch.setattr(menu_cache.COORDINATOR, "stop", lambda: None)
    menu_cache.PERIOD_STATS.store("x", {"value": 1})
    menu_cache.begin_view(42, 77)
    menu_cache.reset_for_tests()
    final = {
        "periodValue": menu_cache.PERIOD_STATS.peek("x").value,
        "viewTokens": len(menu_cache._view_tokens), "messageLocks": len(menu_cache._message_locks),
    }
    return actual_case(case, [], [], final)


def _patch_stats_clock(monkeypatch, runtime):
    monkeypatch.setattr(menu_cache, "today_start_ts", lambda: float(runtime["todayStart"]))
    monkeypatch.setattr(menu_cache, "month_start_ts", lambda: float(runtime["monthStart"]))
    monkeypatch.setattr(stats_menu.time, "time", lambda: float(runtime["nowTs"]))
    monkeypatch.setattr(menu_cache.time, "monotonic", lambda: float(runtime.get("monotonic", 1000)))


def _run_stats_cold(case, monkeypatch):
    capture, _current = install_common(monkeypatch, case)
    _patch_stats_clock(monkeypatch, case["initialRuntime"])
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(menu_cache.COORDINATOR, "enqueue", lambda cache, key, loader, generation: enqueued.append({
        "key": list(key) if isinstance(key, tuple) else key, "generation": generation,
    }))
    entry = case["entry"]
    handled = None
    if entry["mode"] == "send":
        stats_menu.send_new(42)
    else:
        handled = stats_menu.handle_callback(42, 77, "cb-stats", entry["callbackData"])
    return actual_case(case, capture.calls, [], {"handled": handled, "enqueued": enqueued})


def _run_stats_view(case, monkeypatch):
    capture, _current = install_common(monkeypatch, case)
    runtime = case["initialRuntime"]
    _patch_stats_clock(monkeypatch, runtime)
    period = case["entry"]["period"]
    dim = case["entry"]["dim"]
    since = stats_menu._since_ts(period)
    key = stats_menu._period_cache_key(period, since)
    menu_cache.PERIOD_STATS.store(key, deepcopy(runtime["dbSnapshot"]), age_seconds=float(runtime.get("ageSeconds", 0)))
    enqueued: list[dict[str, Any]] = []
    monkeypatch.setattr(menu_cache.COORDINATOR, "enqueue", lambda cache, cache_key, loader, generation: enqueued.append({
        "key": list(cache_key) if isinstance(cache_key, tuple) else cache_key, "generation": generation,
    }))
    handled = stats_menu.handle_callback(42, 77, "cb-stats", f"stats:view:{period}:{dim}")
    return actual_case(case, capture.calls, [], {
        "handled": handled, "enqueued": enqueued,
        "viewTokenPresent": (42, 77) in menu_cache._view_tokens,
    })


def _run_stats_error_page(case, monkeypatch):
    capture, _current = install_common(monkeypatch, case)
    text, keyboard = stats_menu._error_page(RuntimeError(case["initialRuntime"]["error"]))
    ui.edit(42, 77, text, reply_markup=keyboard)
    return actual_case(case, capture.calls, [], {"rendered": True})


def _run_visibility(case, monkeypatch):
    capture, current = install_common(monkeypatch, case)
    entry = case["entry"]
    handled = stats_menu.handle_callback(42, 77, "cb-vis", entry["callbackData"])
    final = {
        "handled": handled,
        "statsVisibility": deepcopy((current.get("telegram") or {}).get("statsVisibility")),
    }
    return actual_case(case, capture.calls, [], final)


RUNNERS = {
    "boundaries": _run_boundaries,
    "swr": _run_swr,
    "scheduler": _run_scheduler,
    "view_token_lock": _run_view_token_lock,
    "reset": _run_reset,
    "stats_cold": _run_stats_cold,
    "stats_view": _run_stats_view,
    "stats_error_page": _run_stats_error_page,
    "visibility": _run_visibility,
}


def run_case(case: dict[str, Any], monkeypatch) -> dict[str, Any]:
    menu_cache.reset_for_tests()
    # clear() deliberately advances generations; pin test-only cache epochs so
    # a trace is independent of earlier pytest cases while production semantics remain exercised.
    for cache in (
        menu_cache.PERIOD_STATS, menu_cache.LIFETIME_STATS, menu_cache.DETAIL_STATS,
        menu_cache.WINDOW_STATS, menu_cache.HISTORY_TOTALS, menu_cache.BACKGROUND_JOBS,
    ):
        with cache._lock:
            cache._generation = 0
    ui._code_to_name.clear()
    return RUNNERS[case["entry"]["scenario"]](case, monkeypatch)


# Keep the historical fixture immutable. Current expectations layer the three
# approved cold callback traces and the provider custom-emoji ID replacement;
# all other payload fields, preferences and cache contracts remain strict.
_COLD_VIEW_DELTAS = {
    "TG-STATS-01.cold-today-callback": ("0", "今天"),
    "TG-STATS-01.cold-rolling-loading-queued": ("3", "最近 3 天"),
    "TG-STATS-01.invalid-view-falls-back-cold-today": ("0", "今天"),
}


_CURRENT_PROVIDER_ICON_IDS = {
    "5872779796257184592": "6140995813788099525",
    "5861557411784957025": "6141162084857031383",
    "5819115571463068721": "6138882363460952713",
}


def _replace_provider_icon_ids(value):
    if isinstance(value, str):
        for old, new in _CURRENT_PROVIDER_ICON_IDS.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace_provider_icon_ids(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_provider_icon_ids(item) for key, item in value.items()}
    return value


def _expected_current_trace(case):
    expected = deepcopy(case)
    if case["caseId"] in _COLD_VIEW_DELTAS:
        period, label = _COLD_VIEW_DELTAS[case["caseId"]]
        # Reuse the frozen keyboard for this exact period/dimension, not production
        # rendering code, so the intentional loading change cannot hide button drift.
        warm = next(item for item in CASES if
                    item["entry"].get("scenario") == "stats_view"
                    and item["entry"]["period"] == period and item["entry"]["dim"] == "all")
        keyboard = deepcopy(warm["tgApi"][-1]["payload"]["reply_markup"])
        expected["tgApi"] = [
            {"method": "answerCallbackQuery", "payload": {
                "callback_query_id": "cb-stats", "text": "正在统计，完成后自动更新",
            }},
            {"method": "editMessageText", "payload": {
                "chat_id": 42, "message_id": 77,
                "text": f"📊 <b>统计 — {label}</b>\n\n⏳ 正在加载，完成后自动更新，无需重复点击。",
                "parse_mode": "HTML", "reply_markup": keyboard,
            }},
        ]
        key = ["period", int(case["initialRuntime"]["todayStart"])] if period == "0" else ["rolling-period", "3"]
        expected["finalBusinessState"]["enqueued"] = [{"key": key, "generation": 0}]
    expected["tgApi"] = _replace_provider_icon_ids(expected["tgApi"])
    return expected


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_cache_stats_trace(case, monkeypatch):
    assert_strict_equal(_expected_current_trace(case), run_case(case, monkeypatch))


def test_observability_manifest_and_test_coverage_are_bidirectional():
    assert_capability_coverage(EXPECTED_CAPABILITIES, ALL_CASES)
    assert {case["caseId"] for case in CASES} == {
        case["caseId"] for case in ALL_CASES if case["capabilityId"] in OWN_CAPABILITIES
    }
    owners = {case["entry"].get("testModule") for case in ALL_CASES}
    assert owners == {"cache_stats", "logs_list_filter", "logs_detail_inspector", "media"}
    assert all(case["entry"].get("scenario") for case in ALL_CASES)

    expected_callbacks = {
        "menu:stats", "stats:view:", "stats:vis:", "stats:vistog:",
        "menu:logs", "logs:list:", "logs:page:", "logs:refresh", "logs:queryclear:",
        "logs:query:", "logs:filter:", "logs:ftoggle:", "logs:faction:",
        "logs:dpage:", "logs:detail:", "logs:body:", "logs:response:",
        "logs:ins:", "logs:full:", "logs:search:",
        "media:logs", "media:page:", "media:refresh:", "media:detail:", "media:view:",
    }
    manifested = {
        family for case in ALL_CASES for family in case["entry"].get("callbackFamilies", [])
    }
    assert manifested == expected_callbacks
    expected_states = {"logs_search"}
    assert {family for case in ALL_CASES for family in case["entry"].get("stateFamilies", [])} == expected_states

    source_paths = [
        Path(stats_menu.__file__),
        Path(__file__).parents[1] / "telegram/menus/logs_menu.py",
        Path(__file__).parents[1] / "telegram/menus/media_logs_menu.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    discovered = set(re.findall(r'(?:data == |data\.startswith\()"([^"]+)"', source))
    discovered = {item for item in discovered if item.startswith(("menu:stats", "stats:", "menu:logs", "logs:", "media:"))}
    assert discovered == expected_callbacks
    logs_source = source_paths[1].read_text(encoding="utf-8")
    assert set(re.findall(r'action != "([^"]+)"', logs_source)) == expected_states

    allowed = {case["caseId"] for case in ALL_CASES}
    tested = set()
    for path in Path(__file__).parent.glob("test_tg_contract_observability_*.py"):
        tested.update(re.findall(r'"(TG-(?:CACHE|STATS|LOG|MEDIA)-\d{2}\.[^"]+)"', path.read_text(encoding="utf-8")))
    # Every caseId must be declared by exactly one owned test module, and vice versa.
    assert all(case["entry"]["testModule"] in owners for case in ALL_CASES)
    assert tested == allowed
    combined = SEGMENT.read_text(encoding="utf-8") + "\n" + "\n".join(
        path.read_text(encoding="utf-8") for path in Path(__file__).parent.glob("test_tg_contract_observability_*.py")
    )
    forbidden_namespace = "ma" + "uth:"
    assert forbidden_namespace not in combined


def test_cache_stats_case_ids_are_bidirectional():
    declared = {
        "TG-CACHE-01.bjt-boundaries",
        "TG-CACHE-01.snapshot-swr-subscriber-failure",
        "TG-CACHE-01.unique-scheduler-lifecycle",
        "TG-CACHE-01.view-token-message-lock",
        "TG-CACHE-01.test-reset",
        "TG-STATS-01.cold-today-callback",
        "TG-STATS-01.cold-rolling-loading-queued",
        "TG-STATS-01.cold-command-send",
        "TG-STATS-01.today-all-family-miss-recent",
        "TG-STATS-01.view-today-channel",
        "TG-STATS-01.view-today-model",
        "TG-STATS-01.view-today-apikey",
        "TG-STATS-01.view-three-day-all",
        "TG-STATS-01.three-day-channel-expanded",
        "TG-STATS-01.view-three-day-model",
        "TG-STATS-01.view-three-day-apikey",
        "TG-STATS-01.view-seven-day-all",
        "TG-STATS-01.view-seven-day-channel",
        "TG-STATS-01.seven-day-model-expanded",
        "TG-STATS-01.view-seven-day-apikey",
        "TG-STATS-01.view-month-all",
        "TG-STATS-01.view-month-channel",
        "TG-STATS-01.view-month-model",
        "TG-STATS-01.month-apikey-expanded",
        "TG-STATS-01.stale-rolling-render-then-queue",
        "TG-STATS-01.query-error-page",
        "TG-STATS-01.invalid-view-falls-back-cold-today",
        "TG-STATS-02.visibility-defaults",
        "TG-STATS-02.visibility-invalid-period",
        "TG-STATS-02.all-hidden-basic-remains",
        "TG-STATS-02.toggle-bychannel",
        "TG-STATS-02.toggle-bymodel",
        "TG-STATS-02.toggle-byapikey",
        "TG-STATS-02.toggle-cachemisses",
        "TG-STATS-02.toggle-recentcalls",
        "TG-STATS-02.toggle-invalid-item",
    }
    assert {case["caseId"] for case in CASES} == declared


def test_observability_branch_matrix_is_frozen_without_contract_invention():
    stats_views = {
        (case["entry"]["period"], case["entry"]["dim"])
        for case in ALL_CASES
        if case["capabilityId"] == "TG-STATS-01"
        and case["entry"]["scenario"] == "stats_view"
    }
    assert stats_views == {
        (period, dim)
        for period in ("0", "3", "7", "month")
        for dim in ("all", "channel", "model", "apikey")
    }

    filter_kinds = {
        case["entry"].get("kind") for case in ALL_CASES
        if case["capabilityId"] == "TG-LOG-02"
        and case["entry"]["scenario"] in {"filter_open", "filter_transitions"}
    }
    assert filter_kinds == {"apikey", "model", "channel", "status"}
    status_case = next(case for case in ALL_CASES if case["caseId"].endswith("filter-status-negative"))
    assert status_case["tgApi"] == [{
        "method": "answerCallbackQuery",
        "payload": {"callback_query_id": "cb-filter", "text": "筛选状态已失效"},
    }]

    media_cases = [case for case in ALL_CASES if case["capabilityId"] == "TG-MEDIA-01"]
    media_methods = [call["method"] for case in media_cases for call in case["tgApi"]]
    assert "sendDocument" not in media_methods
    fallback = next(case for case in media_cases if case["caseId"].endswith("view-nonvideo-photo-fallback"))
    assert [call["method"] for call in fallback["tgApi"]] == [
        "answerCallbackQuery", "sendPhoto",
    ]
    list_case = next(case for case in media_cases if case["caseId"].endswith("list-send-six-statuses-three-actions"))
    rows = list_case["initialRuntime"]["rows"][:6]
    assert {row["status"] for row in rows} == {
        "running", "pending", "success", "failed", "expired", "cancelled",
    }
    assert {row["action"] for row in rows} == {"generate", "edit", "extend"}

    payload_text = json.dumps(
        [call["payload"] for case in ALL_CASES for call in case["tgApi"]],
        ensure_ascii=False,
    )
    assert "\u00a0" in payload_text
    assert "<b>" in payload_text
    assert "✅" in payload_text
