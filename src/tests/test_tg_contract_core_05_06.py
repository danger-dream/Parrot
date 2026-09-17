"""Executable v0.31.13 Telegram traces for state and UI transport."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import errno
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.telegram import bot, states, ui
from src.tests.tg_contract import TraceCapture, assert_strict_equal, load_jsonl


SEGMENT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13/segments/core.jsonl"
CASES = load_jsonl(SEGMENT)
CASES_05_06 = [case for case in CASES if case["capabilityId"] >= "TG-CORE-05"]

TEXT_HANDLERS = [
    ("apikey", bot.apikey_menu),
    ("oauth", bot.oauth_menu),
    ("image", bot.image_menu),
    ("xai_imagine", bot.xai_imagine_menu),
    ("channel", bot.channel_menu),
    ("load_balancing", bot.load_balancing_menu),
    ("logs", bot.logs_menu),
    ("status_alert", bot.status_alert_menu),
    ("update", bot.update_menu),
    ("proxy", bot.proxy_menu),
    ("translation", bot.translation_menu),
    ("system", bot.system_menu),
    ("mapping", bot.mapping_menu),
]


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


def _exception_dict(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _state_snapshot(chat_id: int) -> dict[str, Any]:
    state = states.get_state(chat_id)
    return {"chatId": chat_id, "state": deepcopy(state)}


@pytest.fixture(autouse=True)
def _reset_tg_globals():
    states.clear_all()
    ui.configure("fake-token-core", [42])
    ui._session = None
    ui._code_to_name.clear()
    bot._running = False
    yield
    states.clear_all()
    ui._session = None
    ui._code_to_name.clear()


def _run_state_overwrite_pop(case, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(states.time, "time", lambda: now[0])
    steps = []
    states.set_state(42, "first", {"value": "A\u00a0B"})
    steps.append({"after": "set_first", **_state_snapshot(42)})
    now[0] = 1001.0
    states.set_state(42, "second", {"value": 2})
    steps.append({"after": "overwrite", **_state_snapshot(42)})
    popped = states.pop_state(42)
    steps.append({"after": "pop", "chatId": 42, "state": deepcopy(popped)})
    return _actual(
        case,
        tg_api=[],
        state_steps=steps,
        final={"size": states.size(), "state": states.get_state(42)},
    )


def _run_state_ttl_boundary(case, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(states.time, "time", lambda: now[0])
    states.set_state(42, "ttl", {})
    now[0] = 1600.0
    at_boundary = _state_snapshot(42)
    now[0] = 1600.001
    expired = _state_snapshot(42)
    return _actual(
        case,
        tg_api=[],
        state_steps=[
            {"atSeconds": 600.0, **at_boundary},
            {"atSeconds": 600.001, **expired},
        ],
        final={"size": states.size()},
    )


def _run_state_cleanup_clear(case, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(states.time, "time", lambda: now[0])
    states.set_state(41, "old", {})
    now[0] = 1200.0
    states.set_state(42, "boundary", {})
    now[0] = 1800.0
    cleaned = states.cleanup()
    after_cleanup = {
        "after": "cleanup",
        "removed": cleaned,
        "size": states.size(),
        "remainingChatIds": sorted(states._states),
    }
    states.clear_all()
    return _actual(
        case,
        tg_api=[],
        state_steps=[after_cleanup, {"after": "clear_all", "size": states.size()}],
        final={"size": states.size()},
    )


def _install_text_spies(monkeypatch, selected: str | None, visits: list[str]):
    for name, module in TEXT_HANDLERS:
        def handler(chat_id, action, text, n=name):
            visits.append(n)
            return n == selected
        monkeypatch.setattr(module, "handle_text_state", handler)


def _run_text_dispatch(case, monkeypatch):
    capture = TraceCapture()
    visits: list[str] = []
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(states.time, "time", lambda: 2000.0)
    _install_text_spies(monkeypatch, case["entry"]["selectedHandler"], visits)
    states.set_state(42, case["initialState"]["action"], case["initialState"]["data"])
    before = _state_snapshot(42)
    bot._handle_update(deepcopy(case["entry"]["update"]))
    after = _state_snapshot(42)
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"after": "before_dispatch", **before}, {"after": "dispatch", **after}],
        final={"handlerVisits": visits},
    )


def _run_document_dispatch(case, monkeypatch):
    capture = TraceCapture()
    visits: list[str] = []
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(states.time, "time", lambda: 2000.0)
    document_result = case["entry"]["documentHandled"]

    def document_handler(chat_id, action, message):
        visits.append("oauth_document")
        return document_result

    monkeypatch.setattr(bot.oauth_menu, "handle_document_state", document_handler)
    _install_text_spies(monkeypatch, case["entry"]["selectedHandler"], visits)
    states.set_state(42, case["initialState"]["action"], case["initialState"]["data"])
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"after": "dispatch", **_state_snapshot(42)}],
        final={"handlerVisits": visits},
    )


def _patch_handlers_before_logs(monkeypatch):
    for name, module in TEXT_HANDLERS:
        if name == "logs":
            break
        monkeypatch.setattr(module, "handle_text_state", lambda *args: False)


def _run_logs_state(case, monkeypatch):
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(states.time, "time", lambda: 2000.0)
    _patch_handlers_before_logs(monkeypatch)
    data = deepcopy(case["initialState"]["data"])
    states.set_state(42, "logs_search", data)
    before = _state_snapshot(42)
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"after": "before_dispatch", **before}, {"after": "dispatch", **_state_snapshot(42)}],
        final={"state": states.get_state(42)},
    )


def _run_expired_state(case, monkeypatch):
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(states.time, "time", lambda: 1700.001)
    states._states[42] = {"action": "logs_search", "data": {}, "ts": 1100.0}
    bot._handle_update(deepcopy(case["entry"]["update"]))
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[{"after": "expired_get", **_state_snapshot(42)}],
        final={"state": states.get_state(42)},
    )


def _run_message_helpers(case, monkeypatch):
    capture = TraceCapture()
    monkeypatch.setattr(ui, "api", capture.api)
    keyboard = ui.inline_kb([
        [ui.btn("A\u00a0B", "core:one"), ui.btn("第二行?", "core:two")],
        [ui.btn("返回", "menu:main")],
    ])
    ui.send(42, "<b>标题</b>\nA\u00a0B", reply_markup=keyboard)
    ui.send(42, "empty keyboard omitted", reply_markup={})
    ui.edit(42, 77, "<i>编辑</i>\n")
    ui.answer_cb("cb-1")
    ui.answer_cb("cb-2", "", show_alert=True)
    ui.delete_message(42, 77)
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"returnKinds": ["send", "send", "edit", "answer", "answer", "delete"]},
    )


class _Response:
    def __init__(self, payload=None, *, content=b"", status_code=200):
        self._payload = payload if payload is not None else {"ok": True, "result": {}}
        self.content = content
        self.status_code = status_code

    def json(self):
        return deepcopy(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://fake.invalid/file")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("fake status", request=request, response=response)


class _MultipartSession:
    def __init__(self, capture: TraceCapture, *, download_content=b"file-bytes"):
        self.capture = capture
        self.download_content = download_content
        self.closed = False

    @staticmethod
    def _method(url: str) -> str:
        return url.rsplit("/", 1)[-1]

    def post(self, url, json=None, data=None, files=None):
        method = self._method(url)
        if json is not None:
            payload = deepcopy(json)
        else:
            encoded_files = {}
            for field, item in (files or {}).items():
                filename = item[0]
                source = item[1]
                raw = source.read() if hasattr(source, "read") else bytes(source)
                encoded_files[field] = {
                    "filename": filename,
                    "contentHex": raw.hex(),
                    "contentType": item[2] if len(item) > 2 else None,
                }
            payload = {"data": deepcopy(data or {}), "files": encoded_files}
        self.capture.record(method, payload)
        return _Response()

    def get(self, url):
        self.capture.record("downloadFile", {"url": url})
        return _Response(content=self.download_content)

    def close(self):
        self.closed = True


def _run_download_success(case, monkeypatch):
    capture = TraceCapture({
        "getFile": {"ok": True, "result": {"file_path": "files/fake.bin", "file_size": 10}},
    })
    session = _MultipartSession(capture, download_content=b"0123456789")
    monkeypatch.setattr(ui, "api", capture.api)
    monkeypatch.setattr(ui, "_session_lease", lambda: nullcontext(session))
    content, file_path = ui.download_file("fake-file-id", max_bytes=10)
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"contentHex": content.hex(), "filePath": file_path, "size": len(content)},
    )


def _run_download_error(case, monkeypatch):
    capture = TraceCapture({"getFile": deepcopy(case["entry"]["getFileResponse"])})
    monkeypatch.setattr(ui, "api", capture.api)
    exception = None
    try:
        ui.download_file("fake-file-id", max_bytes=10)
    except Exception as exc:  # characterized below
        exception = _exception_dict(exc)
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"downloaded": False},
        exception=exception,
    )


def _run_uploads(case, monkeypatch, tmp_path):
    capture = TraceCapture()
    session = _MultipartSession(capture)
    monkeypatch.setattr(ui, "_session_lease", lambda: nullcontext(session))
    photo = tmp_path / "fake-photo.bin"
    video = tmp_path / "fake-video.mp4"
    photo.write_bytes(b"PNG\x00")
    video.write_bytes(b"MP4\x00")
    ui.send_photo(42, str(photo), "<b>图片</b>")
    ui.send_video(42, str(video), "<i>视频</i>")
    ui.send_document_bytes(
        42,
        b"A\x00B",
        filename="fake-data.bin",
        caption="<code>文档</code>",
        content_type="application/octet-stream",
    )
    ui.send_document_text(42, "行一\n行二\u00a0", filename="fake.txt")
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"uploads": 4},
    )


class _ScriptedJsonSession:
    def __init__(self, capture: TraceCapture, responses: list[dict[str, Any]]):
        self.capture = capture
        self.responses = list(responses)

    def post(self, url, json=None, **kwargs):
        self.capture.record(url.rsplit("/", 1)[-1], deepcopy(json or {}))
        return _Response(self.responses.pop(0))

    def close(self):
        pass


def _run_parse_fallback(case, monkeypatch):
    capture = TraceCapture()
    session = _ScriptedJsonSession(capture, [
        {"ok": False, "description": "Bad Request: can't parse entities"},
        {"ok": True, "result": {"message_id": 901}},
    ])
    ui._session = session
    result = ui.send(42, "<b>A &amp; B</b>\nC\u00a0D")
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={"result": result},
    )


class _FailingSession:
    def __init__(self, capture: TraceCapture, events: list[str]):
        self.capture = capture
        self.events = events

    def post(self, url, json=None, **kwargs):
        self.capture.record(url.rsplit("/", 1)[-1], deepcopy(json or {}))
        cause = OSError(errno.EADDRNOTAVAIL, "fake local address unavailable")
        raise RuntimeError("fake wrapped transport failure") from cause

    def close(self):
        self.events.append("old_session_closed")


def _run_eaddr_rebuild(case, monkeypatch):
    capture = TraceCapture()
    events: list[str] = []
    failing = _FailingSession(capture, events)
    replacement = _MultipartSession(capture)
    ui._session = failing

    def invalidate(host):
        events.append(f"dns_invalidated:{host}")

    def make_session():
        events.append("replacement_created")
        return replacement

    monkeypatch.setattr(ui.network, "invalidate_dns_cache", invalidate)
    monkeypatch.setattr(ui, "_make_session", make_session)
    result = ui.send(42, "network failure")
    # Recovery invalidates without replaying the failed POST. The failed lease
    # drains first; only the next acquisition lazily builds the current route.
    assert ui.wait_session_idle(1.0)
    assert capture.calls == [{
        "method": "sendMessage",
        "payload": {"chat_id": 42, "text": "network failure", "parse_mode": "HTML"},
    }]
    ui._get_session()
    return _actual(
        case,
        tg_api=capture.calls,
        state_steps=[],
        final={
            "events": events,
            "result": result,
            "replacementInstalled": ui._session is replacement,
        },
    )


def _run_button_limits(case, monkeypatch):
    long_label = "界" * 61
    callback_64_bytes = "é" * 32
    button = ui.btn(long_label, callback_64_bytes)
    url_button = ui.btn_url(long_label, "https://fake.invalid/path")
    short = ui.register_code("fake/account/账户")
    return _actual(
        case,
        tg_api=[],
        state_steps=[],
        final={
            "button": button,
            "urlButton": url_button,
            "labelLength": len(button["text"]),
            "callbackBytes": len(button["callback_data"].encode("utf-8")),
            "shortCode": short,
            "resolved": ui.resolve_code(short),
            "missingResolved": ui.resolve_code("00000000"),
        },
    )


def _run_callback_too_long(case, monkeypatch):
    exception = None
    try:
        ui.btn("oversize", "é" * 32 + "a")
    except Exception as exc:
        exception = _exception_dict(exc)
    return _actual(
        case,
        tg_api=[],
        state_steps=[],
        final={"buttonCreated": False},
        exception=exception,
    )


RUNNERS = {
    "state_overwrite_pop": _run_state_overwrite_pop,
    "state_ttl_boundary": _run_state_ttl_boundary,
    "state_cleanup_clear": _run_state_cleanup_clear,
    "text_dispatch": _run_text_dispatch,
    "document_dispatch": _run_document_dispatch,
    "logs_state": _run_logs_state,
    "expired_state": _run_expired_state,
    "message_helpers": _run_message_helpers,
    "download_success": _run_download_success,
    "download_error": _run_download_error,
    "uploads": _run_uploads,
    "parse_fallback": _run_parse_fallback,
    "eaddr_rebuild": _run_eaddr_rebuild,
    "button_limits": _run_button_limits,
    "callback_too_long": _run_callback_too_long,
}


@pytest.mark.parametrize("case", CASES_05_06, ids=lambda case: case["caseId"])
def test_core_05_and_06_trace(case, monkeypatch, tmp_path):
    if case["caseId"] == "TG-CORE-05.text-illegal-action":
        pytest.skip("Retired OAuth fallback handler no longer participates in text dispatch")
    runner = RUNNERS[case["entry"]["scenario"]]
    if case["entry"]["scenario"] == "uploads":
        actual = runner(case, monkeypatch, tmp_path)
    else:
        actual = runner(case, monkeypatch)
    assert_strict_equal(case, actual)
