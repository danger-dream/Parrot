"""Executable, byte-strict TG-STATUS-01 archive plus current deltas."""

from __future__ import annotations

from copy import deepcopy
import datetime as datetime_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from src.telegram import bot, states, ui
from src.telegram.menus import status_menu
from src.tests.tg_contract import TraceCapture, assert_strict_equal, load_jsonl


SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/main_status.jsonl"
CURRENT_OVERRIDES = (
    Path(__file__).parent
    / "fixtures/tg_contract/model-center-2026-09-13/overrides/main_status.jsonl"
)


def _current_cases() -> list[dict[str, Any]]:
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
STATUS_CASES = [case for case in CASES if case["capabilityId"] == "TG-STATUS-01"]


def _actual(
    case: dict[str, Any],
    *,
    calls: list[dict[str, Any]],
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
        "stateSteps": [],
        "finalBusinessState": deepcopy(final),
        "expectedException": deepcopy(exception),
    }


@pytest.fixture(autouse=True)
def _reset_globals():
    states.clear_all()
    ui.configure("fake-main-status-token", [42])
    ui._session = None
    yield
    states.clear_all()
    ui._session = None


def _patch_status(case: dict[str, Any], monkeypatch) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cfg = deepcopy(case["initialConfig"]["app"])
    runtime = case["initialRuntime"]
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(status_menu.config, "get", lambda: cfg)
    monkeypatch.setattr(status_menu.time, "time", lambda: runtime["clock"])

    class FixedDateTime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(runtime["clock"], tz)

    fake_datetime = ModuleType("datetime")
    for name in dir(datetime_module):
        setattr(fake_datetime, name, getattr(datetime_module, name))
    fake_datetime.datetime = FixedDateTime
    monkeypatch.setitem(sys.modules, "datetime", fake_datetime)
    monkeypatch.setattr(status_menu, "_SERVICE_START_TS", runtime["serviceStart"])
    monkeypatch.setattr(status_menu.affinity, "count", lambda: runtime.get("affinityCount", 0))
    monkeypatch.setattr(status_menu, "codex_cli_version", lambda: runtime.get("codexVersion", "0.0.0"))

    channels = [SimpleNamespace(**item) for item in runtime.get("registryChannels", [])]
    monkeypatch.setattr(status_menu.registry, "all_channels", lambda: channels)
    cooldown_entries = deepcopy(runtime.get("cooldownEntries") or [])
    monkeypatch.setattr(status_menu.cooldown, "active_entries", lambda: deepcopy(cooldown_entries))
    monkeypatch.setattr(
        status_menu.quota_errors,
        "active_quota_cooldown",
        lambda entry: entry.get("kind") == "quota",
    )
    monkeypatch.setattr(
        status_menu.quota_errors,
        "format_bjt_ms",
        lambda timestamp_ms, compact=False: runtime.get("quotaResetText", "01-02 03:04"),
    )

    monkeypatch.setattr(status_menu.scorer, "snapshot", lambda: deepcopy(runtime.get("scores") or []))
    monkeypatch.setattr(
        status_menu.ui,
        "channel_display_name",
        lambda key, with_family=True: (runtime.get("channelNames") or {}).get(key, key),
    )

    today = deepcopy(runtime.get("today") or {})

    def stats_summary(*, since_ts, family, summary_top_limit, include_cost):
        events.append({
            "event": "stats_summary",
            "sinceTs": since_ts,
            "family": family,
            "summaryTopLimit": summary_top_limit,
            "includeCost": include_cost,
        })
        if runtime.get("statsFailure"):
            raise RuntimeError("fake stats unavailable")
        return {"overall": deepcopy(today.get("total" if family is None else family) or {})}

    monkeypatch.setattr(status_menu.log_db, "stats_summary", stats_summary)
    tps_map = {
        (item["channelKey"], item["model"]): item["tps"]
        for item in runtime.get("monthTps", [])
    }

    def month_tps(*, since_ts):
        events.append({"event": "month_tps_by_channel_model", "sinceTs": since_ts})
        if runtime.get("tpsFailure"):
            raise RuntimeError("fake monthly TPS unavailable")
        return deepcopy(tps_map)

    monkeypatch.setattr(status_menu.log_db, "tps_by_channel_model", month_tps)

    accounts = deepcopy(cfg.get("oauthAccounts") or [])
    monkeypatch.setattr(status_menu.oauth_manager, "list_accounts", lambda: deepcopy(accounts))
    quota_rows = deepcopy(runtime.get("quotaRows") or {})
    monkeypatch.setattr(status_menu.state_db, "quota_load", lambda key: deepcopy(quota_rows.get(key)))

    def ensure_quota(keys):
        events.append({"event": "ensure_quota_fresh_sync", "accountKeys": list(keys)})
        if runtime.get("quotaRefreshFailure"):
            raise RuntimeError("fake quota refresh failed")

    monkeypatch.setattr(status_menu.oauth_manager, "ensure_quota_fresh_sync", ensure_quota)
    monkeypatch.setattr(
        status_menu.oauth_manager,
        "fable_display_from_quota_row",
        lambda row: (row.get("fable_util"), row.get("fable_reset")),
    )
    monkeypatch.setattr(
        status_menu.oauth_manager,
        "usage_from_quota_row",
        lambda row: {"cursor": deepcopy(row.get("cursorUsage") or {})},
    )

    monkeypatch.setattr(
        status_menu.apikey_limiter,
        "totals",
        lambda: deepcopy(runtime.get("apiKeyTotals") or {
            "in_flight": 0, "waiting": 0, "tracked_keys": 0,
        }),
    )
    monkeypatch.setattr(
        status_menu.apikey_limiter,
        "snapshot",
        lambda: deepcopy(runtime.get("apiKeySnapshot") or []),
    )
    monkeypatch.setattr(
        status_menu.concurrency,
        "totals",
        lambda: deepcopy(runtime.get("concurrencyTotals") or {
            "in_flight": 0, "waiting": 0, "tracked_channels": 0,
        }),
    )
    monkeypatch.setattr(
        status_menu.concurrency,
        "snapshot",
        lambda: deepcopy(runtime.get("concurrencySnapshot") or []),
    )
    return cfg, events


def _exception_dict(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _run_status(case: dict[str, Any], monkeypatch) -> dict[str, Any]:
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    cfg_before = deepcopy(case["initialConfig"].get("app") or {})
    cfg = cfg_before
    events: list[dict[str, Any]] = []
    scenario = case["entry"]["scenario"]
    handled: bool | None = None
    exception = None

    if scenario not in {"command_fallback", "unknown_callback"}:
        cfg, events = _patch_status(case, monkeypatch)
    try:
        if scenario == "command_fallback":
            bot._handle_update(deepcopy(case["entry"]["update"]))
        elif scenario == "send_new":
            status_menu.send_new(case["entry"]["chatId"])
        elif scenario in {"callback", "unknown_callback"}:
            handled = status_menu.handle_callback(
                case["entry"]["chatId"],
                case["entry"]["messageId"],
                case["entry"]["callbackId"],
                case["entry"]["data"],
            )
        else:  # pragma: no cover
            raise AssertionError(f"unknown status scenario: {scenario}")
    except Exception as exc:  # characterized failure behavior, never real network
        exception = _exception_dict(exc)

    return _actual(
        case,
        calls=capture.calls,
        final={
            "handled": handled,
            "configUnchanged": cfg == cfg_before,
            "chatState": states.get_state(case["entry"]["chatId"]),
            "runtimeEvents": events,
        },
        exception=exception,
    )


@pytest.mark.parametrize("case", STATUS_CASES, ids=lambda case: case["caseId"])
def test_status_trace(case, monkeypatch):
    assert_strict_equal(case, _run_status(case, monkeypatch))


def test_status_queries_fresh_sources_on_every_compose(monkeypatch):
    case = next(case for case in STATUS_CASES if case["caseId"].endswith("command-rich"))
    _cfg, events = _patch_status(case, monkeypatch)
    expected_payload = case["tgApi"][0]["payload"]
    expected_events = case["finalBusinessState"]["runtimeEvents"]

    for _ in range(2):
        text, keyboard = status_menu._compose()
        assert text == expected_payload["text"]
        assert keyboard == expected_payload["reply_markup"]

    assert events == expected_events + expected_events
