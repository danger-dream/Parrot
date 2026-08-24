"""Backend-neutral state recovery and availability contracts.

SQLite quarantine/checkpoint implementation assertions were retired; verified
snapshot generation selection and legacy backup/rebuild behavior remain.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.state_migration import inspect_with_recovery
from src.state_store import RUNTIME_DOMAINS, StateStore


def _runtime(value):
    payload = {domain: {} for domain in RUNTIME_DOMAINS}
    payload["channel_errors"] = {"key": {"value": value}}
    return payload


def test_recovery_never_downgrades_below_higher_verified_backup(tmp_path):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    StateStore.write_snapshot(runtime, "runtime", 2, _runtime("main"))
    StateStore.write_snapshot(runtime + ".bak", "runtime", 7, _runtime("backup"))
    store = StateStore(runtime, durable); store.start()
    assert store.health()["generation"]["runtime"] == 7
    assert store.get("channel_errors", "key")["value"] == "backup"


def test_verified_backup_load_can_heal_corrupt_main_on_next_flush(tmp_path):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    StateStore.write_snapshot(runtime, "runtime", 1, _runtime("old"))
    StateStore.write_snapshot(runtime, "runtime", 2, _runtime("backup-source"))
    Path(runtime).write_bytes(b"corrupt-main")
    store = StateStore(runtime, durable); store.start()
    store._mutate("channel_errors", lambda d: d.__setitem__("new", {"value": "healed"}))
    store.flush("runtime", strict=True); store.close()
    generation, payload = StateStore.read_snapshot(runtime, "runtime")
    assert generation == 2 and payload["channel_errors"]["new"]["value"] == "healed"


def test_corrupt_legacy_uses_newest_healthy_historical_backup_without_modifying_it(tmp_path):
    source = tmp_path / "state.db"; source.write_bytes(b"corrupt")
    root = tmp_path / "backups"; root.mkdir()
    backup = root / "state-db-pre-release.db"
    conn = sqlite3.connect(backup)
    conn.execute("CREATE TABLE channel_errors(channel_key TEXT, model TEXT, error_count INTEGER)")
    conn.execute("INSERT INTO channel_errors VALUES('healthy','m',4)"); conn.commit(); conn.close()
    before = backup.read_bytes(); report = inspect_with_recovery(str(source), str(tmp_path))
    assert report["status"] == "backup"
    assert report["state"]["channel_errors"]["healthy\x1fm"]["error_count"] == 4
    assert backup.read_bytes() == before and source.read_bytes() == b"corrupt"


def test_corrupt_legacy_without_backup_rebuilds_empty_and_retains_evidence(tmp_path):
    source = tmp_path / "state.db"; source.write_bytes(b"bad")
    report = inspect_with_recovery(str(source), str(tmp_path))
    assert report["status"] == "rebuilt-empty"
    assert report["state"] == {}
    assert report["corrupt_source"] == str(source)
    assert report["corrupt_reason"]
    assert source.read_bytes() == b"bad"
