import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from src.state_migration import TABLES, read_legacy_state
from src.state_store import SnapshotError, StateStore


def paths(tmp_path):
    return str(tmp_path / "runtime-cache.json"), str(tmp_path / "durable-state.json")


def test_both_snapshots_round_trip_permissions_and_copies(tmp_path):
    runtime, durable = paths(tmp_path)
    store = StateStore(runtime, durable); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("a", {"value": 1}))
    store._mutate("xai_video_jobs", lambda d: d.__setitem__("j", {"request_id": "j"}))
    store.flush("runtime", strict=True); store.close()
    assert stat.S_IMODE(os.stat(runtime).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(durable).st_mode) == 0o600
    restored = StateStore(runtime, durable); restored.start()
    row = restored.get("performance_stats", "a"); row["value"] = 9
    assert restored.get("performance_stats", "a") == {"value": 1}
    assert restored.get("xai_video_jobs", "j") == {"request_id": "j"}


def test_runtime_failure_keeps_memory_dirty_but_durable_raises(tmp_path, monkeypatch):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    real = store.write_snapshot
    def fail(path, kind, generation, payload): raise OSError("injected")
    monkeypatch.setattr(store, "write_snapshot", fail)
    store._mutate("performance_stats", lambda d: d.__setitem__("a", {"v": 1}))
    assert store.flush("runtime") is False
    assert store.health()["dirty"]["runtime"] is True
    assert store.get("performance_stats", "a") == {"v": 1}
    with pytest.raises(OSError):
        store._mutate("xai_video_jobs", lambda d: d.__setitem__("j", {"v": 1}))
    assert store.health()["dirty"]["durable"] is False
    assert store.get("xai_video_jobs", "j") is None
    monkeypatch.setattr(store, "write_snapshot", real)


def test_generation_write_during_flush_remains_dirty(tmp_path, monkeypatch):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("a", {"v": 1}))
    entered = threading.Event(); release = threading.Event(); real = store.write_snapshot
    def blocked(*args): entered.set(); release.wait(5); return real(*args)
    monkeypatch.setattr(store, "write_snapshot", blocked)
    thread = threading.Thread(target=lambda: store.flush("runtime", strict=True)); thread.start()
    assert entered.wait(2)
    mutation = threading.Thread(target=lambda: store._mutate("performance_stats", lambda d: d.__setitem__("b", {"v": 2})))
    mutation.start()
    assert mutation.is_alive()  # snapshot capture/install owns the per-kind lock
    release.set(); thread.join(5); mutation.join(5)
    assert store.health()["dirty"]["runtime"] is True
    store.flush("runtime", strict=True); store.close()
    restarted = StateStore(runtime, durable); restarted.start()
    assert {"a", "b"} == set(restarted.items("performance_stats"))


def test_concurrent_updates_do_not_lose_rows(tmp_path):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    threads = [threading.Thread(target=lambda n=n: [store._mutate("performance_stats", lambda d, k=f"{n}-{i}": d.__setitem__(k, {"k": k})) for i in range(50)]) for n in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(store.values("performance_stats")) == 400


@pytest.mark.parametrize("bad", [b'{"schema":', b'\x17\x03\x03\x00\x13garbage'])
def test_corrupt_main_recovers_verified_backup(tmp_path, bad):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("old", {"v": 1})); store.flush("runtime", strict=True)
    store._mutate("performance_stats", lambda d: d.__setitem__("new", {"v": 2})); store.flush("runtime", strict=True)
    store.close(); Path(runtime).write_bytes(bad)
    restored = StateStore(runtime, durable); restored.start()
    assert restored.get("performance_stats", "old") == {"v": 1}
    assert restored.get("performance_stats", "new") is None


def test_bad_durable_without_backup_fails_closed(tmp_path):
    runtime, durable = paths(tmp_path); Path(durable).write_bytes(b'\x17\x03\x03\x00\x13')
    with pytest.raises(SnapshotError): StateStore(runtime, durable).start()


def test_replace_failure_preserves_last_good_snapshot(tmp_path, monkeypatch):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("old", {"v": 1})); store.flush("runtime", strict=True)
    real_replace = os.replace
    def fail(src, dst):
        if dst == runtime: raise OSError("replace injection")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", fail)
    store._mutate("performance_stats", lambda d: d.__setitem__("new", {"v": 2}))
    assert store.flush("runtime") is False
    generation, payload = StateStore.read_snapshot(runtime, "runtime")
    assert "old" in payload["performance_stats"] and "new" not in payload["performance_stats"]


def test_legacy_all_tables_equivalent_and_read_only(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    for table, keys in TABLES.items():
        cols = ",".join(f'"{k}" TEXT' for k in keys) + ', "payload" TEXT'
        conn.execute(f'CREATE TABLE "{table}" ({cols})')
        vals = tuple(f"{table}-{k}" for k in keys) + (f"payload-{table}",)
        conn.execute(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in vals)})', vals)
    conn.commit(); conn.close(); before = db.read_bytes()
    migrated = read_legacy_state(str(db)); migrated2 = read_legacy_state(str(db))
    assert set(migrated) == set(TABLES)
    assert migrated == migrated2
    for table, rows in migrated.items():
        assert next(iter(rows.values()))["payload"] == f"payload-{table}"
    assert db.read_bytes() == before


def test_existing_json_prevents_legacy_overwrite(tmp_path):
    runtime, durable = paths(tmp_path); store = StateStore(runtime, durable); store.start()
    store._mutate("performance_stats", lambda d: d.__setitem__("json", {"v": 1})); store.flush("runtime", strict=True)
    # Idempotence is represented by authoritative snapshot loading: supplied legacy data
    # only fills an empty domain and therefore cannot overwrite this row.
    store.close()
    other = StateStore(runtime, durable); other.start(migrated={"performance_stats": {"legacy": {"v": 2}}})
    assert other.get("performance_stats", "json") == {"v": 1}
    assert other.get("performance_stats", "legacy") is None
