"""Backend-neutral state transaction business guarantees.

The former file asserted SQLite connection rollback internals. StateStore now
expresses the same observable contract through prepare-write-publish snapshots.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.state_store import StateStore


def test_failed_durable_transaction_preserves_memory_generation_and_disk(tmp_path, monkeypatch):
    runtime, durable = str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")
    store = StateStore(runtime, durable); store.start()
    store._mutate("app_update_state", lambda d: d.__setitem__("repo", {"repo": "repo", "value": 1}))
    old_bytes = Path(durable).read_bytes(); old_health = store.health()
    real = store.write_snapshot
    monkeypatch.setattr(store, "write_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic commit failure")))
    with pytest.raises(OSError, match="synthetic commit failure"):
        store._mutate("app_update_state", lambda d: d.__setitem__("repo", {"repo": "repo", "value": 2}))
    assert store.get("app_update_state", "repo")["value"] == 1
    assert store.health()["generation"]["durable"] == old_health["generation"]["durable"]
    assert store.health()["dirty"]["durable"] is False
    assert Path(durable).read_bytes() == old_bytes
    monkeypatch.setattr(store, "write_snapshot", real); store.close()


def test_operation_exception_rolls_back_runtime_candidate(tmp_path):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json")); store.start()
    store._mutate("channel_errors", lambda d: d.__setitem__("old", {"error_count": 1}))
    generation = store.health()["generation"]["runtime"]
    def broken(bucket):
        bucket["new"] = {"error_count": 2}
        raise RuntimeError("business failure")
    with pytest.raises(RuntimeError, match="business failure"):
        store._mutate("channel_errors", broken)
    assert store.get("channel_errors", "new") is None
    assert store.get("channel_errors", "old") == {"error_count": 1}
    assert store.health()["generation"]["runtime"] == generation
