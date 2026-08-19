import asyncio
import signal

import uvicorn

from src import drain
import server as parrot_server


def test_drain_waits_for_active_lease_then_finishes():
    async def scenario():
        drain.reset_for_tests()
        lease = await drain.enter("test")
        drain.begin("unit-test")
        waiter = asyncio.create_task(drain.wait_for_zero(1))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert drain.active_count() == 1
        await lease.aclose()
        assert await waiter is True
        assert drain.active_count() == 0

    asyncio.run(scenario())


def test_drain_wait_timeout_when_active_request_remains():
    async def scenario():
        drain.reset_for_tests()
        lease = await drain.enter("test")
        try:
            drain.begin("unit-test")
            assert await drain.wait_for_zero(0.01) is False
            assert drain.active_count() == 1
        finally:
            await lease.aclose()

    asyncio.run(scenario())


def test_drain_reject_response_shape():
    drain.reset_for_tests()
    drain.begin("unit-test")
    resp = drain.reject_response()
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "5"
    assert drain.status_snapshot()["draining"] is True


def test_drain_aware_server_signal_waits_before_should_exit():
    async def scenario():
        drain.reset_for_tests()
        srv = parrot_server._DrainAwareServer(  # noqa: SLF001 - intentional white-box test
            uvicorn.Config(parrot_server.app, host="127.0.0.1", port=0, log_level="critical")
        )
        srv._drain_loop = asyncio.get_running_loop()  # noqa: SLF001
        lease = await drain.enter("test-signal")
        srv.handle_exit(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert drain.is_draining() is True
        assert srv.should_exit is False
        await lease.aclose()
        await asyncio.wait_for(srv._drain_shutdown_task, timeout=1)  # noqa: SLF001
        assert srv.should_exit is True

    asyncio.run(scenario())


def test_shutdown_closes_state_db_without_checkpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        parrot_server.state_db,
        "checkpoint",
        lambda **kwargs: calls.append(("checkpoint", kwargs)) or (0, 8, 8),
    )
    monkeypatch.setattr(
        parrot_server.state_db,
        "close",
        lambda: calls.append(("close", {})),
    )

    assert parrot_server._finalize_state_db() is True
    assert calls == [("close", {})]


def test_shutdown_reports_close_failure(monkeypatch):
    def fail_close():
        raise RuntimeError("close failed")

    monkeypatch.setattr(parrot_server.state_db, "close", fail_close)

    assert parrot_server._finalize_state_db() is False


def test_recovery_restart_preserves_corrupt_db_without_close(monkeypatch):
    calls = []
    parrot_server.state_db._reset_recovery_state_for_tests()
    parrot_server.state_db.request_recovery_restart("file is not a database")
    monkeypatch.setattr(
        parrot_server.state_db,
        "checkpoint",
        lambda **kwargs: calls.append(("checkpoint", kwargs)),
    )
    monkeypatch.setattr(
        parrot_server.state_db,
        "close",
        lambda: calls.append(("close", {})),
    )
    try:
        assert parrot_server._finalize_state_db() is True
        assert calls == []
    finally:
        parrot_server.state_db._reset_recovery_state_for_tests()


def test_state_db_health_loop_requests_graceful_recovery_restart(monkeypatch):
    calls = []
    parrot_server.state_db._reset_recovery_state_for_tests()

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(parrot_server.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        parrot_server.state_db,
        "runtime_corruption_reason",
        lambda: "file is not a database",
    )
    monkeypatch.setattr(
        parrot_server.notifier,
        "notify_event",
        lambda event, text: calls.append(("notify", event, text)),
    )
    monkeypatch.setattr(
        parrot_server.drain,
        "begin",
        lambda reason: calls.append(("drain", reason)),
    )
    monkeypatch.setattr(
        parrot_server.os,
        "kill",
        lambda pid, sig: calls.append(("kill", pid, sig)),
    )
    try:
        asyncio.run(parrot_server._state_db_health_loop())
        assert parrot_server.state_db.recovery_restart_requested() is True
        assert calls[0][0:2] == ("notify", "database_recovery")
        assert ("drain", "state_db_corruption") in calls
        assert ("kill", parrot_server.os.getpid(), signal.SIGTERM) in calls
    finally:
        parrot_server.state_db._reset_recovery_state_for_tests()
