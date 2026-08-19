"""state.db corruption detection, quarantine and startup recovery tests."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    from src import config, state_db

    return {"config": config, "state_db": state_db}


@pytest.fixture
def recovery_env(m, tmp_path, monkeypatch):
    config = m["config"]
    state_db = m["state_db"]
    original_path = state_db._db_path
    original_initialized = state_db._initialized
    state_db.close()
    state_db._db_path = None
    state_db._initialized = False
    state_db._reset_recovery_state_for_tests()

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "state.db"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(state_db, "_resolve_db_path", lambda: str(db_path))
    try:
        yield state_db, data_dir, db_path
    finally:
        state_db.close()
        state_db._db_path = original_path
        state_db._initialized = original_initialized
        state_db._reset_recovery_state_for_tests()


def _create_sqlite(path, value: str = "healthy") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE recovery_probe (value TEXT NOT NULL)")
        conn.execute("INSERT INTO recovery_probe(value) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def test_valid_database_is_left_untouched(recovery_env):
    state_db, _data_dir, db_path = recovery_env
    _create_sqlite(db_path, "keep")
    before = db_path.read_bytes()

    report = state_db.bootstrap_recover()

    assert report is None
    assert db_path.read_bytes() == before
    assert state_db.bootstrap_recovery_report() is None


def test_corrupt_db_wal_shm_are_preserved_and_verified_backup_restored(recovery_env):
    state_db, data_dir, db_path = recovery_env
    backups = data_dir / "backups"
    backups.mkdir()
    healthy = backups / "docker-0.30.1.state.db"
    _create_sqlite(healthy, "from-backup")
    db_path.write_bytes(b"SQLit" + os.urandom(123))
    (data_dir / "state.db-wal").write_bytes(b"wal-evidence")
    (data_dir / "state.db-shm").write_bytes(b"shm-evidence")

    report = state_db.bootstrap_recover()

    assert report is not None
    assert report["action"] == "restored"
    assert report["restored_from"] == str(healthy)
    quarantine = report["quarantine"]
    assert open(os.path.join(quarantine, "state.db"), "rb").read().startswith(b"SQLit")
    assert open(os.path.join(quarantine, "state.db-wal"), "rb").read() == b"wal-evidence"
    assert open(os.path.join(quarantine, "state.db-shm"), "rb").read() == b"shm-evidence"
    manifest = json.load(open(os.path.join(quarantine, "RECOVERY.json"), encoding="utf-8"))
    assert manifest["action"] == "restored"

    ok, detail = state_db._validate_sqlite_file(str(db_path))
    assert ok is True, detail
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT value FROM recovery_probe").fetchone() == ("from-backup",)
    finally:
        conn.close()


def test_corrupt_db_without_backup_is_quarantined_and_recreated(recovery_env):
    state_db, data_dir, db_path = recovery_env
    db_path.write_bytes(b"not-a-sqlite-database")
    (data_dir / "state.db-wal").write_bytes(b"wal-evidence")

    report = state_db.bootstrap_recover()

    assert report is not None
    assert report["action"] == "recreated"
    assert report["restored_from"] is None
    assert not db_path.exists()
    assert os.path.isfile(os.path.join(report["quarantine"], "state.db"))
    assert os.path.isfile(os.path.join(report["quarantine"], "state.db-wal"))

    state_db.init()
    ok, detail = state_db._validate_sqlite_file(str(db_path))
    assert ok is True, detail
    assert state_db._get_conn().execute(
        "SELECT name FROM sqlite_schema WHERE name='performance_stats'"
    ).fetchone() is not None


def test_invalid_newer_backup_is_skipped_for_older_healthy_backup(recovery_env):
    state_db, data_dir, db_path = recovery_env
    backups = data_dir / "backups"
    backups.mkdir()
    healthy = backups / "older.state.db"
    invalid = backups / "newer.state.db"
    _create_sqlite(healthy, "older-healthy")
    invalid.write_bytes(b"invalid-backup")
    os.utime(healthy, (100, 100))
    os.utime(invalid, (200, 200))
    db_path.write_bytes(b"invalid-live-database")

    report = state_db.bootstrap_recover()

    assert report is not None
    assert report["action"] == "restored"
    assert report["restored_from"] == str(healthy)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT value FROM recovery_probe").fetchone() == ("older-healthy",)
    finally:
        conn.close()


def test_validation_unavailable_never_quarantines_live_database(
    recovery_env, monkeypatch
):
    state_db, _data_dir, db_path = recovery_env
    _create_sqlite(db_path, "must-remain")
    before = db_path.read_bytes()
    monkeypatch.setattr(
        state_db,
        "_validate_sqlite_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("state database validation unavailable: database is locked")
        ),
    )

    with pytest.raises(RuntimeError, match="database is locked"):
        state_db.bootstrap_recover()

    assert db_path.read_bytes() == before
    backups = db_path.parent / "backups"
    assert backups.is_dir()
    assert list(backups.glob("state-db-corrupt-*")) == []


def test_runtime_header_corruption_requests_recovery_restart(recovery_env):
    state_db, _data_dir, db_path = recovery_env
    state_db.init()
    with open(db_path, "r+b") as f:
        f.seek(0)
        f.write(b"broken header!!!")
        f.flush()
        os.fsync(f.fileno())

    reason = state_db.runtime_corruption_reason()
    state_db.request_recovery_restart(reason)

    assert "invalid SQLite header" in reason
    assert state_db.recovery_restart_requested() is True
