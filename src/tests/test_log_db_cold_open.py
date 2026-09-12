"""Cold monthly DB opens must serialize WAL negotiation and own failed clients."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from src import log_db
from src.tests.test_proxy_stats_cache import months, _database


def test_cold_open_and_journal_setup_are_inside_existing_write_lock(months, monkeypatch):
    # A pre-existing DELETE-mode file is the problematic upgrade path. Production
    # migration/retention may encounter one; do not pre-warm it into WAL in tests.
    seed = _database(months.current)
    assert seed.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    seed.close()
    real_connect = sqlite3.connect
    statements = []
    opens = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA") and any(x in sql for x in ("journal_mode=", "synchronous=", "busy_timeout=")):
                assert log_db._write_lock._is_owned(), sql
                statements.append(sql)
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        assert log_db._write_lock._is_owned()
        kwargs["factory"] = ObservedConnection
        conn = real_connect(*args, **kwargs)
        opens.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    conn = log_db._get_conn()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert statements == ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=5000"]
    assert log_db._get_conn() is conn and opens == [conn]
    assert log_db._write_conn_registry[str(months.current)] == [conn]


@pytest.mark.parametrize("phase", ["journal", "schema", "migration"])
def test_failed_cold_open_closes_client_and_next_call_can_retry(months, monkeypatch, phase):
    real_connect = sqlite3.connect
    real_migrate = log_db._ensure_migrations
    attempts, closed = [], []
    fail = [True]

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if phase == "journal" and fail[0] and sql == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("injected journal setup failure")
            return super().execute(sql, *args, **kwargs)

        def executescript(self, script, *args, **kwargs):
            if phase == "schema" and fail[0]:
                raise sqlite3.OperationalError("injected schema setup failure")
            return super().executescript(script, *args, **kwargs)

        def close(self):
            closed.append(self)
            super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        conn = real_connect(*args, **kwargs)
        attempts.append(conn)
        return conn

    def migrate(conn):
        if phase == "migration" and fail[0]:
            raise sqlite3.OperationalError("injected migration setup failure")
        return real_migrate(conn)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(log_db, "_ensure_migrations", migrate)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        log_db._get_conn()
    assert len(attempts) == 1 and closed == attempts
    assert str(months.current) not in log_db._local.write_conns
    assert str(months.current) not in log_db._write_conn_registry
    with pytest.raises(sqlite3.ProgrammingError):
        attempts[0].execute("SELECT 1")
    fail[0] = False
    conn = log_db._get_conn()
    assert conn is attempts[1] and conn.execute("SELECT 1").fetchone()[0] == 1


def test_retirement_winning_while_cold_opener_waits_cannot_recreate_file(months, monkeypatch):
    _database(months.current).close()
    after_first_guard = threading.Event()
    proceed = threading.Event()
    errors = []

    class ColdLocal:
        @property
        def write_conns(self):
            # This access follows the optimistic retired-path check and precedes
            # the initialization lock. Let retention win exactly in that gap.
            after_first_guard.set()
            assert proceed.wait(5)
            return {}

    monkeypatch.setattr(log_db, "_local", ColdLocal())

    def open_late():
        try:
            log_db._get_conn()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=open_late)
    with log_db._write_lock:
        thread.start()
        try:
            assert after_first_guard.wait(5)
            months.current.unlink()
            log_db._mark_log_path_retired(str(months.current))
        finally:
            proceed.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], log_db.RetentionPlanError)
    assert not months.current.exists() and not log_db._write_conn_registry
