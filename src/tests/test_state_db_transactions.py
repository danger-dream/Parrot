"""channel_errors 写事务故障后的 rollback 与连接淘汰回归。"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading

import pytest

from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import state_db

    return {"state_db": state_db}


class _FaultConnection:
    def __init__(
        self,
        real,
        *,
        commit_failures: int = 0,
        fail_rollback: bool = False,
        execute_failures: int = 0,
    ):
        self.real = real
        self.commit_failures = commit_failures
        self.fail_rollback = fail_rollback
        self.execute_failures = execute_failures
        self.rollback_calls = 0
        self.close_calls = 0
        self.closed = False

    def execute(self, *args, **kwargs):
        if self.execute_failures:
            self.execute_failures -= 1
            raise sqlite3.OperationalError("synthetic execute failure")
        return self.real.execute(*args, **kwargs)

    def commit(self):
        if self.commit_failures:
            self.commit_failures -= 1
            raise sqlite3.OperationalError("synthetic commit failure")
        return self.real.commit()

    def rollback(self):
        self.rollback_calls += 1
        if self.fail_rollback:
            raise sqlite3.OperationalError("synthetic rollback failure")
        return self.real.rollback()

    def close(self):
        self.close_calls += 1
        self.closed = True
        return self.real.close()

    def __getattr__(self, name):
        return getattr(self.real, name)


def _independent_error_row(state_db, channel_key, model):
    conn = sqlite3.connect(state_db._db_path)
    try:
        return conn.execute(
            "SELECT error_count, cooldown_until, last_error_message "
            "FROM channel_errors WHERE channel_key=? AND model=?",
            (channel_key, model),
        ).fetchone()
    finally:
        conn.close()


def _restore_local_connection(state_db, proxy):
    current = getattr(state_db._local, "conn", None)
    if current is proxy:
        try:
            proxy.real.rollback()
        except Exception:
            pass
        if proxy.closed:
            state_db._local.conn = None
        else:
            state_db._local.conn = proxy.real


@pytest.mark.parametrize("operation", ["error_save", "error_delete"])
def test_channel_error_commit_failure_rolls_back_and_cannot_leak_later(m, operation):
    state_db = m["state_db"]
    state_db.init()
    channel_key = f"txn-commit-failure:{operation}"
    model = "gpt-test"
    state_db.error_delete(channel_key, model)
    if operation == "error_delete":
        state_db.error_save(channel_key, model, 4, -1, "seed")
        assert _independent_error_row(state_db, channel_key, model) is not None

    real = state_db._get_conn()
    proxy = _FaultConnection(real, commit_failures=1)
    state_db._local.conn = proxy
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            if operation == "error_save":
                state_db.error_save(channel_key, model, 7, -1, "must-not-leak")
            else:
                state_db.error_delete(channel_key, model)

        assert proxy.rollback_calls == 1
        assert proxy.in_transaction is False
        expected_present = operation == "error_delete"
        assert (_independent_error_row(state_db, channel_key, model) is not None) is expected_present

        # A later unrelated successful commit on the same thread-local connection
        # must not carry the supposedly failed channel_errors DML with it.
        state_db.schema_meta_set(f"txn-probe:{operation}", "committed")
        assert (_independent_error_row(state_db, channel_key, model) is not None) is expected_present
    finally:
        _restore_local_connection(state_db, proxy)
        state_db.error_delete(channel_key, model)


def test_channel_error_execute_failure_also_rolls_back_and_reraises(m):
    state_db = m["state_db"]
    state_db.init()
    real = state_db._get_conn()
    proxy = _FaultConnection(real, execute_failures=1)
    state_db._local.conn = proxy
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic execute failure"):
            state_db.error_save("txn-execute-failure", "gpt-test", 1, -1, "no-write")
        assert proxy.rollback_calls == 1
        assert proxy.in_transaction is False
    finally:
        _restore_local_connection(state_db, proxy)


def test_rollback_failure_discards_thread_connection_and_next_call_recovers(m):
    state_db = m["state_db"]
    state_db.init()
    channel_key = "txn-rollback-failure"
    model = "gpt-test"
    state_db.error_delete(channel_key, model)

    real = state_db._get_conn()
    proxy = _FaultConnection(real, commit_failures=1, fail_rollback=True)
    state_db._local.conn = proxy
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            state_db.error_save(channel_key, model, 3, -1, "discard-me")

        assert proxy.rollback_calls == 1
        assert proxy.close_calls == 1
        assert proxy.closed is True
        assert getattr(state_db._local, "conn", None) is None
        assert _independent_error_row(state_db, channel_key, model) is None

        state_db.error_save(channel_key, model, 5, -1, "new-connection")
        replacement = state_db._get_conn()
        assert replacement is not proxy and replacement is not real
        row = _independent_error_row(state_db, channel_key, model)
        assert row is not None and row[0] == 5 and row[2] == "new-connection"
    finally:
        _restore_local_connection(state_db, proxy)
        if getattr(state_db._local, "conn", None) is None:
            state_db._get_conn()
        state_db.error_delete(channel_key, model)


def test_state_db_uses_rollback_journal_and_checkpoint_is_safe_noop(m):
    state_db = m["state_db"]
    state_db.init()
    state_db.schema_meta_set("checkpoint-probe", "ready")

    mode = state_db._get_conn().execute("PRAGMA journal_mode").fetchone()[0]
    result = state_db.checkpoint(mode="FULL", strict=True)

    assert str(mode).lower() == "delete"
    assert result == (0, 0, 0)


def test_worker_connections_only_verify_delete_mode(m):
    state_db = m["state_db"]
    state_db.init()
    modes = []
    errors = []

    def worker():
        try:
            conn = state_db._get_conn()
            modes.append(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower())
            state_db.close()
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(16)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    assert errors == []
    assert modes == ["delete"] * 16
    assert not os.path.exists(state_db._db_path + "-wal")


def test_online_backup_is_consistent_and_integrity_checked(m, tmp_path):
    state_db = m["state_db"]
    state_db.init()
    state_db.schema_meta_set("online-backup-probe", "preserved")
    destination = tmp_path / "state-online-backup.db"

    result = state_db.online_backup(str(destination), verify=True)

    assert result == str(destination)
    assert destination.is_file()
    conn = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='online-backup-probe'"
        ).fetchone() == ("preserved",)
    finally:
        conn.close()
