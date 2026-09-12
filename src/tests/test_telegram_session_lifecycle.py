from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.telegram import bot, ui


class _JsonResponse:
    def json(self):
        return {"ok": True, "result": {"message_id": 1}}


class _Session:
    def __init__(self, *, close_gate: threading.Event | None = None):
        self.posts = 0
        self.close_count = 0
        self.close_started = threading.Event()
        self.close_gate = close_gate

    def post(self, _url, json=None, **_kwargs):
        self.posts += 1
        return _JsonResponse()

    def close(self):
        self.close_count += 1
        self.close_started.set()
        if self.close_gate is not None:
            self.close_gate.wait(2)


@pytest.fixture(autouse=True)
def _reset_lifecycle_state():
    ui.close_session()
    assert ui.wait_session_idle(2)
    with ui._session_condition:
        ui._session = None
        ui._session_holder = None
        ui._session_holders.clear()
        ui._session_building.clear()
        ui._session_enabled = True
        ui._session_generation += 1
    with bot._lifecycle_lock:
        bot._stop_event.set()
        bot._running = False
        bot._starting = False
        start_done = threading.Event()
        start_done.set()
        bot._start_done_event = start_done
        bot._stopping = False
        bot._stop_cycle = None
        bot._stop_waiters = 0
        bot._run_generation += 1
        bot._thread = None
    ui.configure("fixture-token", [])
    yield
    with bot._lifecycle_lock:
        bot._stop_event.set()
        bot._running = False
        bot._starting = False
        start_done = threading.Event()
        start_done.set()
        bot._start_done_event = start_done
        bot._stopping = False
        bot._stop_cycle = None
        bot._stop_waiters = 0
        bot._run_generation += 1
        bot._thread = None
    ui.close_session()
    assert ui.wait_session_idle(2)


def test_external_dns_reload_during_first_build_does_not_deadlock():
    script = r'''
import json
import os
from src import config, network, upstream
from src.proxy import manager as pm
from src.telegram import ui
network.init()
pm.init()
ui.configure("fixture-token", [])
upstream.reset_client_sync = lambda: None
path = config.path()
raw = json.loads(open(path, encoding="utf-8").read())
raw.setdefault("network", {}).setdefault("dns", {})["servers"] = ["1.1.1.1"]
old = os.path.getmtime(path)
open(path, "w", encoding="utf-8").write(json.dumps(raw))
os.utime(path, (old + 2, old + 2))
ui._session = None
print("BEFORE", flush=True)
client = ui._get_session()
print("AFTER", client is ui._session, flush=True)
ui.close_session()
assert ui.wait_session_idle(2)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=4,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    assert "BEFORE" in completed.stdout
    assert "AFTER True" in completed.stdout


def test_lazy_build_allows_reload_reentry_without_session_lock_deadlock(monkeypatch):
    stale = _Session()
    current = _Session()
    attempts = 0

    def make_session():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Models config.get() synchronously firing network.on_config_reload
            # while network.sync_client is still constructing the first session.
            ui.rebuild_session()
            return stale
        return current

    monkeypatch.setattr(ui, "_make_session", make_session)
    result: list[httpx.Client] = []
    worker = threading.Thread(target=lambda: result.append(ui._get_session()))
    worker.start()
    worker.join(1)

    assert not worker.is_alive()
    assert result == [current]
    assert attempts == 2
    assert ui.wait_session_idle(1)
    assert stale.close_count == 1
    assert current.close_count == 0


def test_failed_current_generation_build_never_falls_back_to_old_session(monkeypatch):
    old = _Session()
    replacement = _Session()
    ui._session = old
    ui.rebuild_session()
    assert ui.wait_session_idle(1)

    attempts = 0

    def make_session():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fixture construction failure")
        return replacement

    monkeypatch.setattr(ui, "_make_session", make_session)
    assert ui.api("sendMessage", {"chat_id": 1, "text": "first"}) is None
    assert old.posts == 0
    assert ui._session is None

    assert ui.api("sendMessage", {"chat_id": 1, "text": "second"})["ok"] is True
    assert attempts == 2
    assert replacement.posts == 1
    assert old.close_count == 1


def test_close_rejects_new_operations_until_explicit_activation(monkeypatch):
    current = _Session()
    ui._session = current
    ui.close_session()
    assert ui.wait_session_idle(1)

    replacement = _Session()
    builds = []
    monkeypatch.setattr(ui, "_make_session", lambda: builds.append(True) or replacement)
    assert ui.api("sendMessage", {"chat_id": 1, "text": "stopped"}) is None
    assert builds == []

    ui.activate_session()
    assert ui.api("sendMessage", {"chat_id": 1, "text": "started"})["ok"] is True
    assert builds == [True]
    assert replacement.posts == 1


@pytest.mark.parametrize("invalidate", [ui.rebuild_session, ui.close_session])
def test_invalidation_does_not_close_real_httpx_response_in_flight(invalidate):
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "8")
            self.end_headers()
            self.wfile.write(b"a")
            self.wfile.flush()
            entered.set()
            release.wait(2)
            self.wfile.write(b"bcdefgh")
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    old = httpx.Client(trust_env=False, timeout=3)
    ui._session = old
    outcome: list[bytes | BaseException] = []

    def request():
        try:
            with ui._session_lease() as client:
                response = client.get(f"http://127.0.0.1:{server.server_address[1]}/")
                outcome.append(response.content)
        except BaseException as exc:
            outcome.append(exc)

    request_thread = threading.Thread(target=request)
    request_thread.start()
    try:
        assert entered.wait(1)
        invalidate()
        assert not old.is_closed
        release.set()
        request_thread.join(2)
        assert not request_thread.is_alive()
        assert outcome == [b"abcdefgh"]
        assert ui.wait_session_idle(1)
        assert old.is_closed
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(2)


def test_rebuild_returns_without_waiting_for_slow_client_close():
    release_close = threading.Event()
    old = _Session(close_gate=release_close)
    ui._session = old

    started = time.monotonic()
    ui.rebuild_session()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert old.close_started.wait(1)
    assert old.close_count == 1
    release_close.set()
    assert ui.wait_session_idle(1)


def test_concurrent_leases_single_flight_and_close_only_after_last_request(monkeypatch):
    shared = _Session()
    builds = 0
    entered = threading.Barrier(9)
    releases = [threading.Event() for _ in range(8)]

    def make_session():
        nonlocal builds
        builds += 1
        time.sleep(0.02)
        return shared

    monkeypatch.setattr(ui, "_make_session", make_session)

    def request(index: int):
        with ui._session_lease() as client:
            assert client is shared
            entered.wait()
            releases[index].wait(2)

    workers = [threading.Thread(target=request, args=(index,)) for index in range(8)]
    for worker in workers:
        worker.start()
    entered.wait()
    assert builds == 1

    ui.rebuild_session()
    assert shared.close_count == 0
    for release in releases[:-1]:
        release.set()
    for worker in workers[:-1]:
        worker.join(1)
    assert shared.close_count == 0

    releases[-1].set()
    workers[-1].join(1)
    assert all(not worker.is_alive() for worker in workers)
    assert ui.wait_session_idle(1)
    assert shared.close_count == 1


def test_obsolete_concurrent_build_cannot_overwrite_new_generation(monkeypatch):
    stale = _Session()
    current = _Session()
    stale_started = threading.Event()
    release_stale = threading.Event()

    def make_session():
        if threading.current_thread().name == "stale-tg-builder":
            stale_started.set()
            release_stale.wait(2)
            return stale
        return current

    monkeypatch.setattr(ui, "_make_session", make_session)
    stale_result: list[httpx.Client] = []
    stale_thread = threading.Thread(
        target=lambda: stale_result.append(ui._get_session()),
        name="stale-tg-builder",
    )
    stale_thread.start()
    assert stale_started.wait(1)

    ui.rebuild_session()
    assert ui._get_session() is current
    release_stale.set()
    stale_thread.join(1)

    assert not stale_thread.is_alive()
    assert stale_result == [current]
    assert ui._session is current
    assert ui.wait_session_idle(1)
    assert stale.close_count == 1
    assert current.close_count == 0


@pytest.mark.parametrize("late_result", [
    {"ok": True, "result": [{"update_id": 7}]},
    None,
])
def test_stop_waits_for_poll_and_discards_late_result_or_failure(
    monkeypatch, late_result,
):
    entered = threading.Event()
    release = threading.Event()
    handled: list[int] = []
    rebuilt: list[bool] = []
    api_calls = 0

    def api(_method, _data=None):
        nonlocal api_calls
        api_calls += 1
        if late_result is None and api_calls < 10:
            return None
        entered.set()
        release.wait(2)
        return late_result

    monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(ui, "rebuild_session", lambda: rebuilt.append(True))
    monkeypatch.setattr(bot, "_handle_update", lambda update: handled.append(update["update_id"]))
    monkeypatch.setattr(
        bot,
        "_poll_backoff",
        lambda _seconds, generation, stop_event: bot._poll_generation_active(
            generation, stop_event
        ),
    )
    monkeypatch.setattr(bot.menu_cache, "stop", lambda: None)

    generation = bot._run_generation + 1
    stop_event = threading.Event()
    with bot._lifecycle_lock:
        bot._run_generation = generation
        bot._stop_event = stop_event
        bot._running = True
    poll = threading.Thread(target=bot._poll_loop, args=(generation, stop_event))
    bot._thread = poll
    poll.start()
    assert entered.wait(1)

    stopped: list[None] = []
    stopper = threading.Thread(target=lambda: stopped.append(bot.stop()))
    stopper.start()
    assert stop_event.wait(1)
    time.sleep(0.02)
    assert stopper.is_alive()
    release.set()
    stopper.join(2)

    assert not stopper.is_alive()
    assert stopped == [None]
    assert not poll.is_alive()
    assert handled == []
    assert rebuilt == []
    assert ui._session is None


def test_start_is_refused_until_prior_stop_finishes_then_uses_new_generation(monkeypatch):
    old_stop = threading.Event()
    with bot._lifecycle_lock:
        bot._running = False
        bot._starting = False
        bot._stopping = True
        bot._thread = None
        bot._stop_event = old_stop
    old_generation = bot._run_generation

    monkeypatch.setattr(bot, "_drop_pending_updates", lambda: None)
    monkeypatch.setattr(ui, "delete_my_commands", lambda: None)
    monkeypatch.setattr(ui, "set_my_commands", lambda _commands: None)
    monkeypatch.setattr(ui, "install_notify_handler", lambda: None)
    monkeypatch.setattr(bot.menu_cache, "start", lambda: None)

    created: list[tuple[object, str]] = []

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
            self.name = name
            created.append((target, name))

        def start(self):
            pass

    monkeypatch.setattr(bot.threading, "Thread", FakeThread)
    bot.start()
    assert created == []
    assert bot._run_generation == old_generation

    with bot._lifecycle_lock:
        bot._stopping = False
    bot.start()
    assert len(created) == 1
    assert bot._running is True
    assert bot._run_generation == old_generation + 1
    assert bot._stop_event is not old_stop


def test_stop_waits_for_start_gap_before_activation(monkeypatch):
    at_activation_gap = threading.Event()
    release_start = threading.Event()
    start_finished = threading.Event()
    stop_finished = threading.Event()
    events: list[str] = []
    original_activate = bot._activate_start_generation

    def gated_activate(generation, stop_event):
        at_activation_gap.set()
        release_start.wait(2)
        return original_activate(generation, stop_event)

    monkeypatch.setattr(bot, "_activate_start_generation", gated_activate)
    monkeypatch.setattr(ui, "activate_session", lambda: events.append("activate"))
    monkeypatch.setattr(ui, "close_session", lambda: events.append("close"))
    monkeypatch.setattr(ui, "wait_session_idle", lambda timeout=None: True)
    monkeypatch.setattr(bot, "_drop_pending_updates", lambda: events.append("drop"))
    monkeypatch.setattr(ui, "delete_my_commands", lambda: events.append("delete"))
    monkeypatch.setattr(ui, "set_my_commands", lambda _commands: events.append("set"))
    monkeypatch.setattr(ui, "install_notify_handler", lambda: events.append("notify"))
    monkeypatch.setattr(bot.menu_cache, "start", lambda: events.append("menu_start"))
    monkeypatch.setattr(bot.menu_cache, "stop", lambda: events.append("menu_stop"))

    start_thread = threading.Thread(
        target=lambda: (bot.start(), start_finished.set()),
        name="tg-start-gap",
    )
    start_thread.start()
    assert at_activation_gap.wait(1)

    stop_thread = threading.Thread(
        target=lambda: (bot.stop(), stop_finished.set()),
        name="tg-stop-gap",
    )
    stop_thread.start()
    for _ in range(100):
        if "close" in events:
            break
        time.sleep(0.005)
    assert "close" in events
    assert not stop_finished.is_set()

    release_start.set()
    start_thread.join(1)
    stop_thread.join(1)
    assert start_finished.is_set()
    assert stop_finished.is_set()
    assert events == ["close", "menu_stop"]
    assert bot._running is False
    assert bot._stopping is False


def test_concurrent_stop_waiters_keep_start_closed_until_all_return(monkeypatch):
    owner_waiting = threading.Event()
    release_owner = threading.Event()
    follower_leaving = threading.Event()
    release_follower = threading.Event()
    events: list[str] = []

    def wait_idle(timeout=None):
        owner_waiting.set()
        release_owner.wait(2)
        return True

    original_leave = bot._leave_stop_cycle

    def gated_leave(cycle):
        if threading.current_thread().name == "stop-follower":
            follower_leaving.set()
            release_follower.wait(2)
        original_leave(cycle)

    monkeypatch.setattr(ui, "close_session", lambda: events.append("close"))
    monkeypatch.setattr(ui, "wait_session_idle", wait_idle)
    monkeypatch.setattr(bot.menu_cache, "stop", lambda: events.append("menu_stop"))
    monkeypatch.setattr(bot, "_leave_stop_cycle", gated_leave)
    monkeypatch.setattr(ui, "activate_session", lambda: events.append("activate"))
    monkeypatch.setattr(bot, "_drop_pending_updates", lambda: events.append("drop"))
    monkeypatch.setattr(ui, "delete_my_commands", lambda: events.append("delete"))
    monkeypatch.setattr(ui, "set_my_commands", lambda _commands: events.append("set"))
    monkeypatch.setattr(ui, "install_notify_handler", lambda: events.append("notify"))
    monkeypatch.setattr(bot.menu_cache, "start", lambda: events.append("menu_start"))

    owner = threading.Thread(target=bot.stop, name="stop-owner")
    owner.start()
    assert owner_waiting.wait(1)
    follower = threading.Thread(target=bot.stop, name="stop-follower")
    follower.start()
    for _ in range(100):
        with bot._lifecycle_lock:
            waiters = bot._stop_waiters
        if waiters == 2:
            break
        time.sleep(0.005)
    assert waiters == 2

    # Both the active owner and an already-completed-but-not-returned follower
    # keep start closed.
    bot.start()
    assert "activate" not in events
    release_owner.set()
    assert follower_leaving.wait(1)
    owner.join(1)
    assert not owner.is_alive()
    assert follower.is_alive()
    with bot._lifecycle_lock:
        assert bot._stopping is True
        assert bot._stop_waiters == 1
    bot.start()
    assert "activate" not in events
    assert events.count("menu_stop") == 1

    release_follower.set()
    follower.join(1)
    assert not follower.is_alive()
    with bot._lifecycle_lock:
        assert bot._stopping is False
        assert bot._stop_waiters == 0

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
            self.name = name

        def start(self):
            pass

    monkeypatch.setattr(bot.threading, "Thread", FakeThread)
    bot.start()
    assert events[-6:] == ["activate", "drop", "delete", "set", "notify", "menu_start"]
    assert events.count("menu_stop") == 1


@pytest.mark.asyncio
async def test_stop_async_cancellation_waits_for_owned_stop_before_propagating(monkeypatch):
    from src.tests import conftest

    wait_entered = threading.Event()
    release_wait = threading.Event()
    events: list[str] = []

    def wait_idle(timeout=None):
        events.append("wait_enter")
        wait_entered.set()
        release_wait.wait(2)
        events.append("wait_exit")
        return True

    monkeypatch.setattr(asyncio, "to_thread", conftest._ORIG_TO_THREAD)
    monkeypatch.setattr(ui, "close_session", lambda: events.append("close"))
    monkeypatch.setattr(ui, "wait_session_idle", wait_idle)
    monkeypatch.setattr(bot.menu_cache, "stop", lambda: events.append("menu_stop"))

    task = asyncio.create_task(bot.stop_async())
    for _ in range(100):
        if wait_entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert wait_entered.is_set()
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    assert "menu_stop" not in events

    release_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["close", "wait_enter", "wait_exit", "menu_stop"]
    with bot._lifecycle_lock:
        assert bot._stopping is False
        assert bot._stop_cycle is None
        assert bot._stop_waiters == 0
