"""Strict v0.31.13 unified media-log and cached resend traces."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
from typing import Any

import pytest

from src import config, media_db
from src.telegram import ui
from src.telegram.menus import media_logs_menu
from src.tests.tg_contract import assert_strict_equal, load_jsonl
from src.tests.test_tg_contract_observability_cache_stats import (
    Capture, actual_case, install_config,
)

SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/observability.jsonl"
ALL_CASES = load_jsonl(SEGMENT)
CASES = [case for case in ALL_CASES if case["capabilityId"] == "TG-MEDIA-01"]


def _install(case, monkeypatch):
    capture = Capture()
    runtime = case["initialRuntime"]
    install_config(monkeypatch, case["initialConfig"])
    monkeypatch.setattr(ui, "api", capture.api)
    ui._code_to_name.clear()
    rows = deepcopy(runtime.get("rows") or [])
    db_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(media_db, "count", lambda: len(rows))

    def recent(limit, offset=0):
        db_calls.append({"method": "recent", "limit": limit, "offset": offset})
        return deepcopy(rows[offset:offset + limit])

    monkeypatch.setattr(media_db, "recent", recent)
    monkeypatch.setattr(media_db, "summary", lambda: deepcopy(runtime.get("summary") or {}))
    monkeypatch.setattr(media_db, "account_top", lambda limit: deepcopy(runtime.get("accountTop") or [])[:limit])

    def get_log(log_id):
        db_calls.append({"method": "get_log", "id": log_id})
        return deepcopy(next((row for row in rows if row.get("id") == log_id), None))

    monkeypatch.setattr(media_db, "get_log", get_log)
    monkeypatch.setattr(media_db, "seconds_since", lambda created: int(runtime.get("waitSeconds", 0)))
    monkeypatch.setattr(media_db, "fmt_bjt", lambda value: str((runtime.get("formattedTimes") or {}).get(str(value), value or "?")))

    media_results = list(runtime.get("mediaResults") or [])

    def send_media(method, chat_id, path, caption=""):
        assert Path(path).is_file() and not Path(path).is_symlink()
        capture.record(method, {"chat_id": chat_id, "path": path, "caption": caption})
        return deepcopy(media_results.pop(0)) if media_results else {"ok": True, "result": {}}

    monkeypatch.setattr(ui, "send_photo", lambda chat_id, path, caption="": send_media("sendPhoto", chat_id, path, caption))
    monkeypatch.setattr(ui, "send_video", lambda chat_id, path, caption="": send_media("sendVideo", chat_id, path, caption))
    return capture, db_calls, media_results


def _codes_final() -> dict[str, Any]:
    families: dict[str, int] = {}
    for value in ui._code_to_name.values():
        family = value.split(":", 1)[0] + ":" if ":" in value else "raw"
        families[family] = families.get(family, 0) + 1
    return {"registeredCodeCount": len(ui._code_to_name), "registeredCodeFamilies": families}


def _run_list(case, monkeypatch):
    capture, db_calls, remaining = _install(case, monkeypatch)
    entry = case["entry"]
    handled = None
    if entry["mode"] == "send":
        media_logs_menu.send_new(42, page=entry.get("page", 1))
    else:
        handled = media_logs_menu.handle_callback(42, 77, "cb-media", entry["callbackData"])
    return actual_case(case, capture.calls, [], {
        "handled": handled, "dbCalls": db_calls, "unusedMediaResults": len(remaining), **_codes_final(),
    })


def _run_detail(case, monkeypatch):
    capture, db_calls, remaining = _install(case, monkeypatch)
    entry = case["entry"]
    short = ui.register_code(f"medialog:{entry['logId']}") if entry.get("validShort", True) else "deadbeef"
    handled = media_logs_menu.handle_callback(
        42, 77, "cb-media-detail", f"media:detail:{short}:{entry.get('page', 1)}",
    )
    return actual_case(case, capture.calls, [], {
        "handled": handled, "dbCalls": db_calls, "unusedMediaResults": len(remaining), **_codes_final(),
    })


def _run_view(case, monkeypatch):
    capture, db_calls, remaining = _install(case, monkeypatch)
    entry = case["entry"]
    short = ui.register_code(f"medialog:{entry['logId']}") if entry.get("validShort", True) else "deadbeef"
    handled = media_logs_menu.handle_callback(
        42, 77, "cb-media-view", f"media:view:{short}:{entry.get('page', 1)}",
    )
    return actual_case(case, capture.calls, [], {
        "handled": handled, "dbCalls": db_calls, "unusedMediaResults": len(remaining), **_codes_final(),
    })


RUNNERS = {"list": _run_list, "detail": _run_detail, "view": _run_view}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_media_trace(case, monkeypatch, tmp_path):
    # Instantiate archived /fake paths as isolated real files, before execution.
    # Both the input and exact expected transport path use this fixture value;
    # the comparator and all sending/failure/unused-result assertions are intact.
    archived = json.dumps(case, ensure_ascii=False)
    assert str(tmp_path) not in archived
    case = json.loads(archived.replace('/fake/', str(tmp_path) + '/fake/'))
    for value in case['initialRuntime'].get('existingPaths', []):
        path = Path(value)
        assert path.is_relative_to(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'isolated media contract fixture')
        assert path.is_file() and not path.is_symlink()
    actual = RUNNERS[case["entry"]["scenario"]](case, monkeypatch)
    assert_strict_equal(case, actual)


def test_media_case_ids_are_bidirectional():
    declared = {
        "TG-MEDIA-01.list-send-six-statuses-three-actions",
        "TG-MEDIA-01.menu-list",
        "TG-MEDIA-01.page-two",
        "TG-MEDIA-01.refresh-invalid-page",
        "TG-MEDIA-01.detail-image",
        "TG-MEDIA-01.detail-video",
        "TG-MEDIA-01.detail-nonvideo-image-fallback",
        "TG-MEDIA-01.detail-expired",
        "TG-MEDIA-01.view-image-five-files",
        "TG-MEDIA-01.view-video-first-file",
        "TG-MEDIA-01.view-nonvideo-photo-fallback",
        "TG-MEDIA-01.view-log-expired",
        "TG-MEDIA-01.view-cache-missing",
        "TG-MEDIA-01.view-transport-business-failure",
    }
    assert {case["caseId"] for case in CASES} == declared
