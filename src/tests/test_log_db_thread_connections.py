"""Thread-local log-DB connections must not outlive their threads through the registry."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from src import log_db
from src.management_control import stats_cache


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_write_conn_owner", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    monkeypatch.setattr(log_db, "_retired_log_paths", set())
    yield tmp_path
    for connections in log_db._write_conn_registry.values():
        for conn in connections:
            conn.close()


def _open_in_thread() -> sqlite3.Connection:
    box: list[sqlite3.Connection] = []
    worker = threading.Thread(target=lambda: box.append(log_db._get_conn()))
    worker.start()
    worker.join()
    return box[0]


def test_connections_of_finished_threads_are_closed_on_next_open(isolated_log):
    finished = [_open_in_thread() for _ in range(5)]
    newest = _open_in_thread()

    path = log_db._current_db_path()[0]
    assert log_db._write_conn_registry[path] == [newest]
    assert set(log_db._write_conn_owner) == {id(newest)}
    for conn in finished:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_live_thread_keeps_its_connection(isolated_log):
    mine = log_db._get_conn()
    _open_in_thread()
    _open_in_thread()
    path = log_db._current_db_path()[0]
    assert mine in log_db._write_conn_registry[path]
    assert mine.execute("SELECT 1").fetchone()[0] == 1
    assert log_db._get_conn() is mine


def test_stats_cache_refreshes_reuse_pool_threads(monkeypatch):
    names: set[str] = set()
    cache = stats_cache.StatsCache(fresh_seconds=0.0)
    monkeypatch.setattr(cache, "enabled", True)
    for i in range(6):
        done = threading.Event()

        def loader(i=i, done=done):
            names.add(threading.current_thread().name)
            done.set()
            return i

        cache._start_background(("k", i), loader)
        assert done.wait(5)
    assert names and len(names) <= 2
    assert all(name.startswith("management-stats-refresh") for name in names)
