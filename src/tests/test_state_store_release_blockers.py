from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import config, state_db
from src.state_migration import inspect_legacy, inspect_with_recovery
from src.state_store import RUNTIME_DOMAINS, StateStore, validate_distinct_paths


def _store(tmp_path):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"))
    store.start(); return store


def test_durable_prepare_write_publish_has_no_ghost_commit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store._mutate("xai_video_jobs", lambda d: d.__setitem__("old", {"v": 1}))
    before = store.health()["generation"]["durable"]
    real = store.write_snapshot
    monkeypatch.setattr(store, "write_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store._mutate("xai_video_jobs", lambda d: d.__setitem__("ghost", {"v": 2}))
    assert store.get("xai_video_jobs", "ghost") is None
    assert store.health()["generation"]["durable"] == before
    assert store.health()["dirty"]["durable"] is False
    monkeypatch.setattr(store, "write_snapshot", real); store.close()
    restored = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")); restored.start()
    assert restored.get("xai_video_jobs", "old") == {"v": 1}
    assert restored.get("xai_video_jobs", "ghost") is None


@pytest.mark.parametrize("bad", [object(), float("nan"), float("inf")])
def test_runtime_rejects_non_json_at_mutation_boundary(tmp_path, bad):
    store = _store(tmp_path); generation = store.health()["generation"]["runtime"]
    with pytest.raises((TypeError, ValueError)):
        store._mutate("performance_stats", lambda d: d.__setitem__("bad", {"v": bad}))
    assert store.get("performance_stats", "bad") is None
    assert store.health()["generation"]["runtime"] == generation


def test_load_selects_highest_verified_main_or_backup(tmp_path):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    payload = {domain: {} for domain in RUNTIME_DOMAINS}; payload["performance_stats"] = {"high": {"v": 2}}
    low = {domain: {} for domain in RUNTIME_DOMAINS}; low["performance_stats"] = {"low": {"v": 1}}
    StateStore.write_snapshot(runtime + ".bak", "runtime", 9, payload)
    StateStore.write_snapshot(runtime, "runtime", 3, low)
    store = StateStore(runtime, durable); store.start()
    assert store.health()["generation"]["runtime"] == 9
    assert store.get("performance_stats", "high") == {"v": 2}


def test_close_barrier_rejects_late_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path); store._mutate("performance_stats", lambda d: d.__setitem__("a", {"v": 1}))
    entered, release = threading.Event(), threading.Event(); real = store.write_snapshot
    def blocked(*args, **kwargs):
        if args[1] == "runtime": entered.set(); release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(store, "write_snapshot", blocked)
    closer = threading.Thread(target=store.close); closer.start(); assert entered.wait(2)
    with pytest.raises(RuntimeError, match="not accepting"):
        store._mutate("performance_stats", lambda d: d.__setitem__("late", {"v": 2}))
    release.set(); closer.join(5); assert not closer.is_alive()
    restored = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")); restored.start()
    assert restored.get("performance_stats", "a") == {"v": 1}
    assert restored.get("performance_stats", "late") is None


def test_process_lock_contention_and_release(tmp_path):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    owner = StateStore(runtime, durable); owner.start()
    code = "from src.state_store import StateStore; s=StateStore(%r,%r); s.start()" % (runtime, durable)
    env = dict(os.environ); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    failed = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert failed.returncode != 0 and "another process owns" in failed.stderr
    owner.close()
    succeeded = subprocess.run([sys.executable, "-c", code + "; s.close()"], env=env,
                               capture_output=True, text=True)
    assert succeeded.returncode == 0, succeeded.stderr


def test_path_aliases_hardlinks_and_special_files_rejected(tmp_path):
    legacy = tmp_path / "state.db"; legacy.write_bytes(b"x")
    hard = tmp_path / "runtime.json"; os.link(legacy, hard)
    with pytest.raises(ValueError, match="runtimeStatePath.*stateDbPath"):
        validate_distinct_paths({"runtimeStatePath": str(hard), "stateDbPath": str(legacy),
                                 "durableStatePath": str(tmp_path / "durable.json")})
    with pytest.raises(ValueError, match="not a regular file"):
        StateStore(str(tmp_path), str(tmp_path / "durable.json"))


def test_wal_only_schema_row_and_response_are_migrated_without_source_change(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db); conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE performance_stats(channel_key TEXT, model TEXT, value INTEGER)")
    conn.execute("CREATE TABLE openai_response_store(response_id TEXT,parent_id TEXT,api_key_name TEXT,model TEXT,channel_key TEXT,created_at REAL,expires_at REAL,input_items TEXT,output_items TEXT)")
    conn.commit()
    # Seal the main schema, then place a later table and both rows only in WAL.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("CREATE TABLE app_self_update(id INTEGER, stage TEXT)")
    conn.execute("INSERT INTO app_self_update VALUES(1,'docker-ready')")
    conn.execute("INSERT INTO performance_stats VALUES('wal','m',7)")
    conn.execute("INSERT INTO openai_response_store VALUES('resp',NULL,'k','m','c',1,2,'[]','[]')")
    conn.commit()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("state.db")}
    report = inspect_legacy(str(db))
    assert report["state"]["performance_stats"]["wal\x1fm"]["value"] == 7
    assert report["state"]["app_self_update"]["1"]["stage"] == "docker-ready"
    assert report["responses"][0]["response_id"] == "resp"
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("state.db")}
    assert before == after
    conn.close()


def _legacy_db(path: Path, value: int):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS performance_stats(channel_key TEXT, model TEXT, value INTEGER)")
    conn.execute("DELETE FROM performance_stats"); conn.execute("INSERT INTO performance_stats VALUES('source','m',?)", (value,))
    conn.commit(); conn.close()


def test_rollback_old_db_change_is_reimported_and_manifest_interruption_retries(tmp_path, monkeypatch):
    db = tmp_path / "state.db"; _legacy_db(db, 1)
    cfg = {"stateDbPath": "state.db", "runtimeStatePath": "runtime.json", "durableStatePath": "durable.json"}
    state_db.close()
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "get", lambda: cfg)
    state_db.init(); assert state_db.perf_load("source", "m")["value"] == 1; state_db.close()
    _legacy_db(db, 2)  # old-version rollback writes its authoritative SQLite state
    real_manifest = state_db._write_manifest
    monkeypatch.setattr(state_db, "_write_manifest", lambda *a: (_ for _ in ()).throw(OSError("power loss")))
    with pytest.raises(OSError, match="power loss"): state_db.init()
    monkeypatch.setattr(state_db, "_write_manifest", real_manifest)
    state_db.init(); assert state_db.perf_load("source", "m")["value"] == 2
    manifest = json.loads((tmp_path / "state-migration.json").read_text())
    assert manifest["snapshot_generations"]["runtime"] >= 3
    state_db.close()


def test_corrupt_source_uses_healthy_historical_backup_or_rebuilds(tmp_path):
    source = tmp_path / "state.db"; source.write_bytes(b"not sqlite")
    backups = tmp_path / "backups"; backups.mkdir(); healthy = backups / "20260101.state.db"
    _legacy_db(healthy, 8)
    report = inspect_with_recovery(str(source), str(tmp_path))
    assert report["status"] == "backup"
    assert report["state"]["performance_stats"]["source\x1fm"]["value"] == 8
    healthy.unlink()
    report = inspect_with_recovery(str(source), str(tmp_path))
    assert report["status"] == "rebuilt-empty" and report["corrupt_source"] == str(source)
