from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src import config, state_db
from src.state_store import RUNTIME_DOMAINS, StateStore, SnapshotError


def _payload(kind: str, key: str, value: int):
    from src.state_store import DURABLE_DOMAINS
    domains = RUNTIME_DOMAINS if kind == "runtime" else DURABLE_DOMAINS
    result = {domain: {} for domain in domains}
    result["performance_stats" if kind == "runtime" else "xai_video_jobs"] = {key: {"v": value}}
    return result


def test_missing_legacy_with_verified_runtime_preserves_partial_and_repairs_manifest(tmp_path, monkeypatch):
    runtime, durable = tmp_path / "runtime.json", tmp_path / "durable.json"
    StateStore.write_snapshot(str(runtime), "runtime", 7, _payload("runtime", "keep", 7))
    cfg = {"stateDbPath": str(tmp_path / "absent.db"), "runtimeStatePath": "runtime.json",
           "durableStatePath": "durable.json"}
    state_db.close(); monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "get", lambda: cfg)
    state_db.init()
    assert state_db.get_store().get("performance_stats", "keep") == {"v": 7}
    assert not durable.exists()
    manifest = json.loads((tmp_path / "state-migration.json").read_text())
    assert manifest["status"] == "snapshot-only-missing-source"
    assert manifest["snapshot_generations"] == {"runtime": 7}
    state_db.close()


def test_corrupt_changed_legacy_and_healthy_backup_never_overwrite_json(tmp_path, monkeypatch):
    import sqlite3
    runtime, durable = tmp_path / "runtime.json", tmp_path / "durable.json"
    StateStore.write_snapshot(str(runtime), "runtime", 11, _payload("runtime", "new", 11))
    StateStore.write_snapshot(str(durable), "durable", 12, _payload("durable", "new", 12))
    source = tmp_path / "state.db"; source.write_bytes(b"corrupt")
    backups = tmp_path / "backups"; backups.mkdir(); old = backups / "old.state.db"
    conn = sqlite3.connect(old); conn.execute("CREATE TABLE performance_stats(channel_key TEXT,model TEXT,value INTEGER)")
    conn.execute("INSERT INTO performance_stats VALUES('old','m',1)"); conn.commit(); conn.close()
    cfg = {"stateDbPath": str(source), "runtimeStatePath": "runtime.json", "durableStatePath": "durable.json"}
    state_db.close(); monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "get", lambda: cfg)
    state_db.init()
    assert state_db.get_store().get("performance_stats", "new") == {"v": 11}
    assert state_db.get_store().get("performance_stats", "old\x1fm") is None
    assert not (tmp_path / "state-migration.json").exists()
    assert state_db.migration_report()["status"] == "backup"
    state_db.close()


@pytest.mark.parametrize("fault", ["candidate_write", "candidate_fsync", "candidate_verify", "main_replace", "main_dir_fsync", "backup_replace", "backup_verify", "backup_chmod", "backup_dir_fsync"])
def test_strict_durable_install_failure_restores_memory_generation_main_and_backup(tmp_path, monkeypatch, fault):
    path = str(tmp_path / "durable.json"); store = StateStore(str(tmp_path / "runtime.json"), path); store.start()
    store._mutate("xai_video_jobs", lambda d: d.__setitem__("one", {"v": 1}))
    store._mutate("xai_video_jobs", lambda d: d.__setitem__("two", {"v": 2}))
    before_main, before_backup = Path(path).read_bytes(), Path(path + ".bak").read_bytes()
    before_health = store.health(); real_replace, real_read = os.replace, StateStore.read_snapshot
    real_open, real_fsync = os.open, os.fsync
    real_chmod, real_sync = os.chmod, StateStore._sync_dir; sync_calls = fsync_calls = 0
    def open_file(candidate, flags, *args):
        if fault == "candidate_write" and str(candidate).endswith(".tmp"): raise OSError("candidate write fault")
        return real_open(candidate, flags, *args)
    def fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fault == "candidate_fsync" and fsync_calls == 1: raise OSError("candidate fsync fault")
        return real_fsync(fd)
    def replace(src, dst):
        if fault == "main_replace" and dst == path and str(src).endswith(".tmp"): raise OSError("main replace fault")
        if fault == "backup_replace" and dst == path + ".bak" and str(src).endswith(".new-bak"): raise OSError("backup replace fault")
        return real_replace(src, dst)
    def read(candidate, kind):
        if fault == "candidate_verify" and str(candidate).endswith(".tmp"): raise OSError("candidate verify fault")
        if fault == "backup_verify" and candidate == path + ".bak": raise OSError("backup verify fault")
        return real_read(candidate, kind)
    def chmod(candidate, mode):
        if fault == "backup_chmod" and candidate == path + ".bak": raise OSError("backup chmod fault")
        return real_chmod(candidate, mode)
    def sync(directory):
        nonlocal sync_calls
        sync_calls += 1
        if fault == "main_dir_fsync" and sync_calls == 1: raise OSError("main dir fsync fault")
        if fault == "backup_dir_fsync" and sync_calls == 2: raise OSError("backup dir fsync fault")
        return real_sync(directory)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_file); patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "replace", replace); patch.setattr(StateStore, "read_snapshot", staticmethod(read))
        patch.setattr(os, "chmod", chmod); patch.setattr(StateStore, "_sync_dir", staticmethod(sync))
        with pytest.raises(OSError, match="fault"):
            store._mutate("xai_video_jobs", lambda d: d.__setitem__("ghost", {"v": 3}))
    assert store.get("xai_video_jobs", "ghost") is None
    assert store.health()["generation"] == before_health["generation"]
    assert store.health()["dirty"] == before_health["dirty"]
    assert Path(path).read_bytes() == before_main and Path(path + ".bak").read_bytes() == before_backup
    store.close()


def test_backup_replacement_late_failure_restores_main_and_backup(tmp_path, monkeypatch):
    path = str(tmp_path / "durable.json")
    StateStore.write_snapshot(path, "durable", 1, _payload("durable", "one", 1))
    StateStore.write_snapshot(path, "durable", 2, _payload("durable", "two", 2))
    before_main, before_backup = Path(path).read_bytes(), Path(path + ".bak").read_bytes()
    real_sync = StateStore._sync_dir; calls = 0
    def fail_second(directory):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("backup directory fsync failed")
        return real_sync(directory)
    monkeypatch.setattr(StateStore, "_sync_dir", staticmethod(fail_second))
    with pytest.raises(OSError, match="backup directory"):
        StateStore.write_snapshot(path, "durable", 3, _payload("durable", "three", 3))
    assert Path(path).read_bytes() == before_main
    assert Path(path + ".bak").read_bytes() == before_backup


def test_two_flushes_serialize_physical_write_and_leave_consistent_generation(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("one", {"v": 1}))
    entered, release = threading.Event(), threading.Event(); calls = []
    real = store.write_snapshot
    def blocked(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set(); assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(store, "write_snapshot", blocked)
    first = threading.Thread(target=lambda: store.flush("runtime", strict=True)); first.start()
    assert entered.wait(2)
    second = threading.Thread(target=lambda: store.flush("runtime", strict=True)); second.start()
    time.sleep(0.1); assert len(calls) == 1
    release.set(); first.join(5); second.join(5)
    assert not first.is_alive() and not second.is_alive() and len(calls) == 1
    health = store.health(); generation, payload = StateStore.read_snapshot(str(tmp_path / "runtime.json"), "runtime")
    assert generation == health["generation"]["runtime"] and health["dirty"]["runtime"] is False
    assert payload["performance_stats"]["one"] == {"v": 1}
    store.close()


def test_startup_backup_selection_self_heals_main(tmp_path):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    StateStore.write_snapshot(runtime, "runtime", 2, _payload("runtime", "low", 2))
    StateStore.write_snapshot(runtime + ".bak", "runtime", 9, _payload("runtime", "high", 9))
    store = StateStore(runtime, durable); store.start()
    assert StateStore.read_snapshot(runtime, "runtime")[0] == 9
    assert store.get("performance_stats", "high") == {"v": 9}
    store.close()


def test_reverse_target_order_process_lock_does_not_deadlock(tmp_path):
    left, right = str(tmp_path / "left.json"), str(tmp_path / "right.json")
    owner = StateStore(left, right); owner.start()
    code = "from src.state_store import StateStore; s=StateStore(%r,%r); s.start()" % (right, left)
    env = dict(os.environ); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and "another process owns StateStore target" in result.stderr
    owner.close()


def test_partial_overlap_process_lock_and_release(tmp_path):
    runtime = str(tmp_path / "shared.json"); d1 = str(tmp_path / "d1.json"); d2 = str(tmp_path / "d2.json")
    owner = StateStore(runtime, d1); owner.start()
    code = "from src.state_store import StateStore; s=StateStore(%r,%r); s.start(); s.close()" % (runtime, d2)
    env = dict(os.environ); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    blocked = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert blocked.returncode != 0 and "another process owns StateStore target" in blocked.stderr
    owner.close()
    assert subprocess.run([sys.executable, "-c", code], env=env, capture_output=True).returncode == 0


@pytest.mark.parametrize("runtime,durable,error", [
    ("state-migration.json", "durable.json", "runtimeStatePath.*migrationManifest"),
    ("runtime.json", "runtime.json.bak", "runtimeBackup.*durableStatePath"),
    ("runtime.json", "runtime.json.lock", "(runtimeLock.*durableStatePath|durableStatePath.*runtimeLock)"),
])
def test_derived_path_collisions(runtime, durable, error, tmp_path):
    with pytest.raises(ValueError, match=error):
        StateStore(str(tmp_path / runtime), str(tmp_path / durable))


def test_legacy_manifest_collision_rejected_before_creation(tmp_path, monkeypatch):
    cfg = {"stateDbPath": "state-migration.json", "runtimeStatePath": "runtime.json", "durableStatePath": "durable.json"}
    state_db.close(); monkeypatch.setattr(config, "DATA_DIR", str(tmp_path)); monkeypatch.setattr(config, "get", lambda: cfg)
    with pytest.raises(ValueError, match="migrationManifest.*stateDbPath"):
        state_db.init()
    assert not (tmp_path / "runtime.json.lock").exists()


def test_snapshot_symlink_and_standalone_hardlink_rejected(tmp_path):
    target = tmp_path / "target"; target.write_text("x")
    symlink = tmp_path / "runtime.json"; symlink.symlink_to(target)
    with pytest.raises(ValueError, match="runtimeStatePath.*symlink"):
        StateStore(str(symlink), str(tmp_path / "durable.json"))
    symlink.unlink(); os.link(target, symlink)
    with pytest.raises(ValueError, match="runtimeStatePath.*multiple hard links"):
        StateStore(str(symlink), str(tmp_path / "durable.json"))


@pytest.mark.parametrize("failure", [PermissionError("denied"), OSError("I/O failure")])
def test_optional_openai_legacy_fingerprint_failure_does_not_block_store(failure, monkeypatch, capsys):
    from src.openai import store as openai_store
    from src import state_migration
    conn = sqlite3.connect(":memory:"); conn.executescript(openai_store._SCHEMA)
    monkeypatch.setattr(state_migration, "source_fingerprint", lambda *_: (_ for _ in ()).throw(failure))
    openai_store._migrate_legacy_responses(conn)
    assert conn.execute("SELECT count(*) FROM store_migrations").fetchone()[0] == 0
    assert "optional legacy response import skipped" in capsys.readouterr().out
    conn.close()


def test_optional_write_timeout_before_init_is_backend_neutral():
    state_db.close()
    with state_db.optional_write_timeout(1):
        assert True
