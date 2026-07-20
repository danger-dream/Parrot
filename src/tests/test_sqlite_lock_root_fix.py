from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import pytest

from src import affinity, channel_state, config, scorer, state_db
from src.openai import store


LEGACY_SCHEMA = """
CREATE TABLE openai_response_store (
  response_id TEXT PRIMARY KEY,
  parent_id TEXT,
  api_key_name TEXT,
  model TEXT,
  channel_key TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  input_items TEXT NOT NULL,
  output_items TEXT NOT NULL
)
"""


@pytest.fixture
def isolated_store(monkeypatch):
    # Keep even this test-specific store below the fail-closed DATA_DIR created
    # by isolated_pytest.py; a relative dbPath must never fall back to BASE_DIR.
    root = Path(config.DATA_DIR) / f"sqlite-root-fix-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    cfg = {
        "stateDbPath": "state.db",
        "openai": {"store": {
            "enabled": True,
            "dbPath": "responses.db",
            "ttlMinutes": 60,
            "cleanupIntervalSeconds": 300,
            "cleanupBatchSize": 100,
            "cleanupBatchBytes": 8 * 1024 * 1024,
            "cleanupMaxBatches": 100,
            "cleanupTimeBudgetSeconds": 10,
        }},
    }
    monkeypatch.setattr(config, "DATA_DIR", str(root))
    monkeypatch.setattr(config, "get", lambda: cfg)
    store._reset_for_test(reinitialize=True)
    store.init()
    try:
        yield root, cfg
    finally:
        store._reset_for_test(reinitialize=True)
        shutil.rmtree(root, ignore_errors=True)


def _save(response_id: str, parent_id: str | None = None) -> None:
    store.save(
        response_id, parent_id,
        api_key_name="key-a", model="gpt-test", channel_key="api:test",
        input_items=[{"id": response_id, "type": "message"}], output_items=[],
    )


def test_config_defaults_keep_history_out_of_state_db():
    defaults = config.DEFAULT_CONFIG
    store_defaults = defaults["openai"]["store"]
    assert store_defaults["dbPath"] != defaults["stateDbPath"]
    assert store_defaults["cleanupBatchSize"] == 100
    assert store_defaults["cleanupBatchBytes"] == 8 * 1024 * 1024
    assert store_defaults["cleanupMaxBatches"] == 100
    assert store_defaults["cleanupTimeBudgetSeconds"] == 10


def test_store_init_rolls_back_failed_schema_setup(monkeypatch, tmp_path):
    class BrokenConnection:
        def __init__(self):
            self.rolled_back = False

        def executescript(self, _sql):
            raise sqlite3.OperationalError("schema is locked")

        def rollback(self):
            self.rolled_back = True

    conn = BrokenConnection()
    monkeypatch.setattr(store, "_resolve_db_path", lambda: str(tmp_path / "store.db"))
    monkeypatch.setattr(
        store, "_resolve_legacy_db_path", lambda: str(tmp_path / "state.db"),
    )
    monkeypatch.setattr(store, "_get_conn", lambda: conn)
    monkeypatch.setattr(store, "_initialized", False)
    monkeypatch.setattr(store, "_db_path", None)
    monkeypatch.setattr(store, "_legacy_db_path", None)
    with pytest.raises(sqlite3.OperationalError, match="schema is locked"):
        store.init()
    assert conn.rolled_back is True


def test_default_store_path_is_data_relative_and_separate(isolated_store):
    root, cfg = isolated_store
    cfg["openai"]["store"].pop("dbPath")
    store._reset_for_test(reinitialize=True)
    store.init()

    assert Path(store._db_path).parent == root
    assert Path(store._db_path).name == "openai_response_store.db"
    assert Path(store._db_path).resolve() != Path(store._legacy_db_path).resolve()

    # Deep-merged defaults still look configured at runtime.  Even if an old
    # stateDbPath happens to use the new default name, do not rejoin that DB.
    cfg["openai"]["store"]["dbPath"] = "openai_response_store.db"
    cfg["stateDbPath"] = "openai_response_store.db"
    store._reset_for_test(reinitialize=True)
    store.init()
    assert Path(store._db_path).name == "openai_response_store.v2.db"
    assert Path(store._db_path).resolve() != Path(store._legacy_db_path).resolve()


def test_explicit_store_path_cannot_rejoin_state_db(isolated_store):
    _root, cfg = isolated_store
    cfg["openai"]["store"]["dbPath"] = "state.db"
    store._reset_for_test(reinitialize=True)
    with pytest.raises(RuntimeError, match="must differ"):
        store.init()


def test_hardlinked_store_path_cannot_rejoin_state_db(isolated_store):
    root, cfg = isolated_store
    state_path = root / "state.db"
    explicit_path = root / "store-hardlink.db"
    state_path.touch()
    explicit_path.hardlink_to(state_path)
    cfg["openai"]["store"]["dbPath"] = explicit_path.name
    store._reset_for_test(reinitialize=True)
    with pytest.raises(RuntimeError, match="must differ"):
        store.init()


def test_relative_store_path_cannot_escape_data_dir(isolated_store):
    _root, cfg = isolated_store
    cfg["openai"]["store"]["dbPath"] = "../escaped.db"
    store._reset_for_test(reinitialize=True)
    with pytest.raises(RuntimeError, match="must stay within DATA_DIR"):
        store.init()


def test_relative_store_path_cannot_escape_through_symlink(isolated_store):
    root, cfg = isolated_store
    outside = root.parent / f"outside-{uuid.uuid4().hex}"
    outside.mkdir()
    (root / "escape-link").symlink_to(outside, target_is_directory=True)
    cfg["openai"]["store"]["dbPath"] = "escape-link/store.db"
    store._reset_for_test(reinitialize=True)
    try:
        with pytest.raises(RuntimeError, match="must stay within DATA_DIR"):
            store.init()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_legacy_fallback_is_read_only_and_new_database_wins(isolated_store):
    root, _cfg = isolated_store
    legacy = root / "state.db"
    now = time.time()
    with sqlite3.connect(legacy) as conn:
        conn.execute(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO openai_response_store VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-id", None, "key-a", "old-model", "api:old", now,
                now + 3600, json.dumps([{"source": "legacy"}]), "[]",
            ),
        )

    legacy_row = store.lookup("legacy-id", api_key_name="key-a")
    assert legacy_row.model == "old-model"
    assert legacy_row.input_items == [{"source": "legacy"}]

    # A newly persisted child may continue a legacy parent without migration.
    _save("new-id", parent_id="legacy-id")
    expanded = store.expand_history("new-id", api_key_name="key-a")
    assert expanded == [{"source": "legacy"}, {"id": "new-id", "type": "message"}]

    # Same id in the new DB must shadow legacy, and all new writes stay there.
    _save("legacy-id")
    assert store.lookup("legacy-id", api_key_name="key-a").model == "gpt-test"
    with sqlite3.connect(legacy) as conn:
        assert conn.execute(
            "SELECT 1 FROM openai_response_store WHERE response_id='new-id'"
        ).fetchone() is None
    with sqlite3.connect(root / "responses.db") as conn:
        assert conn.execute(
            "SELECT 1 FROM openai_response_store WHERE response_id='new-id'"
        ).fetchone() is not None


def test_legacy_fallback_preserves_expired_and_forbidden_semantics(isolated_store):
    root, _cfg = isolated_store
    now = time.time()
    with sqlite3.connect(root / "state.db") as conn:
        conn.execute(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO openai_response_store VALUES (?, NULL, ?, 'old', '', ?, ?, '[]', '[]')",
            [
                ("legacy-expired", "key-a", now - 20, now - 10),
                ("legacy-forbidden", "key-b", now, now + 3600),
            ],
        )
    with pytest.raises(store.ResponseExpired):
        store.lookup("legacy-expired", api_key_name="key-a")
    with pytest.raises(store.ResponseForbidden):
        store.lookup("legacy-forbidden", api_key_name="key-a")


def test_expand_history_cycle_and_depth_are_bounded(isolated_store):
    _save("chain-a")
    _save("chain-b", parent_id="chain-a")
    _save("chain-c", parent_id="chain-b")
    conn = store._get_conn()
    conn.execute(
        "UPDATE openai_response_store SET parent_id='chain-c' WHERE response_id='chain-a'"
    )
    conn.commit()

    bounded = store.expand_history("chain-c", api_key_name="key-a", max_depth=2)
    assert [item["id"] for item in bounded] == ["chain-b", "chain-c"]
    cyclic = store.expand_history("chain-c", api_key_name="key-a", max_depth=50)
    assert [item["id"] for item in cyclic] == ["chain-a", "chain-b", "chain-c"]


def test_legacy_missing_database_or_table_is_a_clean_miss(isolated_store):
    root, _cfg = isolated_store
    assert not (root / "state.db").exists()
    with pytest.raises(store.ResponseNotFound):
        store.lookup("missing", api_key_name="key-a")
    # A state DB from a version predating this table is also a clean miss.
    with sqlite3.connect(root / "state.db") as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(store.ResponseNotFound):
        store.lookup("missing", api_key_name="key-a")


def test_legacy_precheck_only_treats_enoent_as_miss(isolated_store, monkeypatch):
    root, _cfg = isolated_store
    legacy_path = root / "state.db"
    legacy_path.mkdir()
    with pytest.raises(sqlite3.OperationalError, match="not a regular file"):
        store.lookup("legacy-directory", api_key_name="key-a")

    legacy_path.rmdir()
    real_stat = store.os.stat

    def denied(path, *args, **kwargs):
        if str(path) == str(legacy_path):
            raise PermissionError(13, "permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(store.os, "stat", denied)
    with pytest.raises(PermissionError, match="permission denied"):
        store.lookup("legacy-denied", api_key_name="key-a")


def test_legacy_sqlite_failure_is_not_rewritten_as_404(isolated_store, monkeypatch):
    class LockedLegacy:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_get_legacy_conn", lambda: LockedLegacy())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store.lookup("legacy-busy", api_key_name="key-a")

    from src.openai.transform import guard, responses_to_chat
    with pytest.raises(guard.GuardError) as raised:
        responses_to_chat.translate_request(
            {
                "model": "gpt-test",
                "previous_response_id": "legacy-busy",
                "input": "continue",
            },
            api_key_name="key-a",
        )
    assert raised.value.status == 503
    assert raised.value.err_type == "server_error"


def test_cleanup_expired_is_bounded_and_commits_each_batch(isolated_store):
    for i in range(7):
        _save(f"expired-{i}")
    conn = store._get_conn()
    conn.execute("UPDATE openai_response_store SET expires_at=?", (time.time() - 1,))
    conn.commit()

    assert store.cleanup_expired(batch_size=2, max_batches=2) == 4
    assert conn.execute("SELECT count(*) FROM openai_response_store").fetchone()[0] == 3
    assert store.cleanup_expired(batch_size=2, max_batches=10) == 3
    assert conn.execute("SELECT count(*) FROM openai_response_store").fetchone()[0] == 0


def test_cleanup_respects_payload_bytes_and_still_deletes_oversized_row(isolated_store):
    payload = "x" * 2048
    for i in range(3):
        store.save(
            f"large-{i}", None, api_key_name="key-a", model="gpt-test",
            channel_key="api:test", input_items=[{"payload": payload}],
            output_items=[], ttl_seconds=-1,
        )
    assert store.cleanup_expired(
        batch_size=100, max_batches=2, batch_bytes=1024, time_budget_seconds=10,
    ) == 2
    assert store.cleanup_expired(
        batch_size=100, max_batches=2, batch_bytes=1024, time_budget_seconds=10,
    ) == 1


def test_cleanup_releases_store_lock_between_batches(isolated_store, monkeypatch):
    for i in range(3):
        store.save(
            f"yield-{i}", None, api_key_name="key-a", model="gpt-test",
            channel_key="api:test", input_items=[], output_items=[], ttl_seconds=-1,
        )
    writer_errors: list[BaseException] = []
    invoked = False

    def yield_to_writer(_seconds: float) -> None:
        nonlocal invoked
        if invoked:
            return
        invoked = True

        def writer() -> None:
            try:
                _save("save-during-cleanup")
            except BaseException as exc:
                writer_errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive(), "cleanup retained Store lock between batches"

    monkeypatch.setattr(store.time, "sleep", yield_to_writer)
    assert store.cleanup_expired(batch_size=1, max_batches=2) == 2
    assert invoked is True
    assert writer_errors == []
    assert store.lookup("save-during-cleanup", api_key_name="key-a").response_id == "save-during-cleanup"


def test_cleanup_default_catches_up_ten_thousand_small_rows(isolated_store):
    conn = store._get_conn()
    now = time.time()
    payload = json.dumps([{"text": "x" * 128}])
    conn.executemany(
        """INSERT INTO openai_response_store
           (response_id, parent_id, api_key_name, model, channel_key,
            created_at, expires_at, input_items, output_items)
           VALUES (?, NULL, 'key-a', 'gpt-test', 'api:test', ?, ?, ?, '[]')""",
        [(f"bulk-{i}", now - 10, now - 1, payload) for i in range(10_000)],
    )
    conn.commit()

    assert store.cleanup_expired() == 10_000
    assert conn.execute("SELECT count(*) FROM openai_response_store").fetchone()[0] == 0


def test_store_thread_local_connections_handle_concurrent_writes(isolated_store):
    errors: list[BaseException] = []
    workers = 6
    writes_per_worker = 20
    barrier = threading.Barrier(workers)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for i in range(writes_per_worker):
                response_id = f"thread-{worker_id}-{i}"
                _save(response_id)
                assert store.lookup(response_id, api_key_name="key-a").response_id == response_id
        except BaseException as exc:  # collected and asserted in the test thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert store._get_conn().execute(
        "SELECT count(*) FROM openai_response_store"
    ).fetchone()[0] == workers * writes_per_worker


def test_state_db_write_lock_does_not_block_openai_store(isolated_store):
    root, _cfg = isolated_store
    legacy = sqlite3.connect(root / "state.db", timeout=0.1)
    legacy.execute("CREATE TABLE state_only (id INTEGER)")
    legacy.commit()
    legacy.execute("BEGIN IMMEDIATE")
    try:
        # This used to target state.db and would block/fail behind the writer.
        _save("independent-write")
    finally:
        legacy.rollback()
        legacy.close()
    assert store.lookup("independent-write", api_key_name="key-a").response_id == "independent-write"


def test_cross_process_state_lock_does_not_block_openai_store(isolated_store):
    root, _cfg = isolated_store
    state_path = root / "state.db"
    script = (
        "import sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1], timeout=1)\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS process_lock (id INTEGER)')\n"
        "conn.commit()\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "print('LOCKED', flush=True)\n"
        "sys.stdin.readline()\n"
        "conn.rollback()\n"
        "conn.close()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(state_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "LOCKED"
        started = time.monotonic()
        _save("cross-process-independent")
        assert time.monotonic() - started < 1.0
    finally:
        if proc.stdin is not None:
            proc.stdin.write("release\n")
            proc.stdin.flush()
        proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""


def test_scorer_sqlite_failure_keeps_memory_and_is_rate_limited(monkeypatch, caplog):
    scorer._stats.clear()
    scorer._last_persist_warning_at = 0.0
    calls = 0

    def fail_sqlite(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scorer.state_db, "perf_save", fail_sqlite)
    caplog.set_level("WARNING")
    scorer.record_success("api:locked", "model", 10, 20, 30)
    scorer.record_failure("api:locked", "model", 15)

    stats = scorer.get_stats("api:locked", "model")
    assert calls == 2
    assert stats is not None and stats["total_requests"] == 2
    assert stats["success_count"] == 1
    warnings = [r for r in caplog.records if "keeping in-memory score" in r.message]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.ProgrammingError("closed connection"),
        sqlite3.IntegrityError("constraint failed"),
        sqlite3.OperationalError("no such table: performance_stats"),
    ],
)
def test_scorer_does_not_swallow_sqlite_programming_or_schema_errors(monkeypatch, exc):
    scorer._stats.clear()

    def programmer_error(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(scorer.state_db, "perf_save", programmer_error)
    with pytest.raises(type(exc), match=str(exc)):
        scorer.record_success("api:bug", "model", 10, 20, 30)
    # Memory is still updated before persistence is attempted.
    assert scorer.get_stats("api:bug", "model")["total_requests"] == 1


def test_affinity_concurrent_upserts_publish_in_commit_order(monkeypatch):
    affinity._entries.clear()
    persisted: dict[str, str] = {}
    first_persisted = threading.Event()
    release_first = threading.Event()

    def persist(fingerprint, channel_key, model, **_kwargs):
        persisted[fingerprint] = model
        if model == "model-a":
            first_persisted.set()
            assert release_first.wait(timeout=2)

    monkeypatch.setattr(affinity.state_db, "affinity_upsert", persist)
    first = threading.Thread(
        target=affinity.upsert, args=("fp-order", "api:a", "model-a"),
    )
    second = threading.Thread(
        target=affinity.upsert, args=("fp-order", "api:b", "model-b"),
    )
    first.start()
    assert first_persisted.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert persisted["fp-order"] == "model-b"
    assert affinity.snapshot()["fp-order"]["model"] == "model-b"


def test_client_affinity_concurrent_upserts_publish_in_commit_order(monkeypatch):
    affinity._client_entries.clear()
    persisted: dict[str, str] = {}
    first_persisted = threading.Event()
    release_first = threading.Event()

    def persist(client_key, channel_key, model, **_kwargs):
        persisted[client_key] = model
        if model == "model-a":
            first_persisted.set()
            assert release_first.wait(timeout=2)

    monkeypatch.setattr(affinity.state_db, "client_affinity_upsert", persist)
    first = threading.Thread(
        target=affinity.client_upsert, args=("client-order", "api:a", "model-a"),
    )
    second = threading.Thread(
        target=affinity.client_upsert, args=("client-order", "api:b", "model-b"),
    )
    first.start()
    assert first_persisted.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert persisted["client-order"] == "model-b"
    assert affinity.client_snapshot()["client-order"]["model"] == "model-b"


def test_scheduler_affinity_touch_survives_locked_state_db(monkeypatch):
    from types import SimpleNamespace
    from src import scheduler

    affinity._entries.clear()
    old_last_used = state_db.now_ms() - 1000
    affinity._entries["fp-hit"] = {
        "channel_key": "api:bound",
        "model": "model",
        "last_used": old_last_used,
        "prompt_cache_key": None,
    }

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(affinity.state_db, "affinity_touch", locked)
    candidates = [
        (SimpleNamespace(key="api:other"), "model"),
        (SimpleNamespace(key="api:bound"), "model"),
    ]
    ordered, hit = scheduler._apply_affinity(candidates, "fp-hit", None)

    assert hit is True
    assert ordered[0][0].key == "api:bound"
    assert affinity.snapshot()["fp-hit"]["last_used"] == old_last_used


def test_affinity_availability_failure_skips_memory_update(monkeypatch):
    affinity._entries.clear()
    affinity._client_entries.clear()

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(affinity.state_db, "affinity_upsert", locked)
    monkeypatch.setattr(affinity.state_db, "client_affinity_upsert", locked)
    affinity.upsert("fp-locked", "api:test", "model")
    affinity.client_upsert("client-locked", "api:test", "model")
    assert affinity.get("fp-locked") is None
    assert affinity.client_get("client-locked") is None


def test_affinity_availability_failure_skips_touch_delete_and_rename(monkeypatch):
    affinity._entries.clear()
    affinity._client_entries.clear()
    affinity._entries["fp"] = {
        "channel_key": "api:old", "model": "model", "last_used": 1,
        "prompt_cache_key": None,
    }
    affinity._client_entries["client"] = {
        "channel_key": "api:old", "model": "model", "last_used": 1,
    }

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    for name in (
        "affinity_touch", "affinity_delete", "affinity_rename_channel",
        "client_affinity_delete", "client_affinity_rename_channel",
    ):
        monkeypatch.setattr(affinity.state_db, name, locked)

    affinity.touch("fp")
    affinity.delete("fp")
    affinity.rename_channel("api:old", "api:new")
    affinity.client_delete("client")
    affinity.client_rename_channel("api:old", "api:new")

    assert affinity.snapshot()["fp"] == {
        "channel_key": "api:old", "model": "model", "last_used": 1,
        "prompt_cache_key": None,
    }
    assert affinity.client_snapshot()["client"] == {
        "channel_key": "api:old", "model": "model", "last_used": 1,
    }


@pytest.mark.parametrize(
    "exc",
    [sqlite3.ProgrammingError("closed connection"), sqlite3.IntegrityError("constraint failed")],
)
def test_affinity_does_not_swallow_programming_errors(monkeypatch, exc):
    affinity._entries.clear()

    def fail(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(affinity.state_db, "affinity_upsert", fail)
    with pytest.raises(type(exc), match=str(exc)):
        affinity.upsert("fp-bug", "api:test", "model")
    assert affinity.get("fp-bug") is None


@pytest.mark.parametrize(
    "writer,args",
    [
        (state_db.affinity_upsert, ("fp", "api:test", "model")),
        (state_db.affinity_touch, ("fp",)),
        (state_db.affinity_delete, ("fp",)),
        (state_db.affinity_cleanup, (1,)),
        (state_db.client_affinity_upsert, ("client", "api:test", "model")),
        (state_db.client_affinity_delete, ("client",)),
        (state_db.client_affinity_cleanup, (1,)),
    ],
)
def test_affinity_state_writes_roll_back_failed_transaction(monkeypatch, writer, args):
    class LockedConnection:
        def __init__(self):
            self.rolled_back = False

        def execute(self, sql, *_args, **_kwargs):
            if str(sql).startswith("PRAGMA busy_timeout"):
                return self
            raise sqlite3.OperationalError("database is locked")

        def fetchone(self):
            return (5_000,)

        def rollback(self):
            self.rolled_back = True

    conn = LockedConnection()
    monkeypatch.setattr(state_db, "_get_conn", lambda: conn)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        writer(*args)
    assert conn.rolled_back is True


def test_perf_save_rolls_back_failed_sqlite_write(monkeypatch):
    class LockedConnection:
        def __init__(self):
            self.rolled_back = False

        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            self.rolled_back = True

    conn = LockedConnection()
    monkeypatch.setattr(state_db, "_get_conn", lambda: conn)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        state_db.perf_save("api:locked", "model", {})
    assert conn.rolled_back is True


@pytest.mark.parametrize(
    "writer",
    [
        lambda: state_db.schema_meta_set("key", "value"),
        lambda: state_db.network_check_save({"key": "network"}),
        lambda: state_db.network_check_delete("network"),
        lambda: state_db.network_check_delete_stale(set()),
        lambda: state_db.quota_save("openai:account", {}),
        lambda: state_db.quota_delete("openai:account"),
        lambda: state_db.quota_patch_passive(
            "claude:account", {"five_hour_util": 1},
        ),
        lambda: state_db.quota_save_openai_snapshot(
            "openai:account",
            {"fetched_at": 1},
            {
                "five_hour_util": 1,
                "five_hour_reset_sec": 1,
                "seven_day_util": 2,
                "seven_day_reset_sec": 2,
            },
        ),
    ],
)
def test_auxiliary_state_writes_roll_back_failed_transaction(monkeypatch, writer):
    class LockedConnection:
        def __init__(self):
            self.rolled_back = False

        def execute(self, sql, *_args, **_kwargs):
            if str(sql).startswith("PRAGMA busy_timeout"):
                return self
            raise sqlite3.OperationalError("database is locked")

        def fetchone(self):
            return (5_000,)

        def rollback(self):
            self.rolled_back = True

    conn = LockedConnection()
    monkeypatch.setattr(state_db, "_get_conn", lambda: conn)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        writer()
    assert conn.rolled_back is True


def test_state_db_init_rolls_back_failed_schema_setup(monkeypatch, tmp_path):
    class BrokenConnection:
        def __init__(self):
            self.rolled_back = False

        def executescript(self, _sql):
            raise sqlite3.OperationalError("schema is locked")

        def rollback(self):
            self.rolled_back = True

    conn = BrokenConnection()
    monkeypatch.setattr(state_db, "_resolve_db_path", lambda: str(tmp_path / "state.db"))
    monkeypatch.setattr(state_db, "_get_conn", lambda: conn)
    monkeypatch.setattr(state_db, "_initialized", False)
    with pytest.raises(sqlite3.OperationalError, match="schema is locked"):
        state_db.init()
    assert conn.rolled_back is True


@pytest.mark.parametrize(
    "writer",
    [
        lambda key: state_db.quota_save(key, {"fetched_at": 1}),
        lambda key: state_db.quota_patch_passive(key, {"five_hour_util": 1}),
        lambda key: state_db.quota_save_openai_snapshot(
            key,
            {"fetched_at": 1},
            {
                "five_hour_util": 1,
                "five_hour_reset_sec": 1,
                "seven_day_util": 2,
                "seven_day_reset_sec": 2,
            },
        ),
    ],
)
def test_quota_cache_response_path_writes_fail_fast_under_state_lock(writer):
    state_db.init()
    key = f"openai:quota-lock-{uuid.uuid4().hex}"
    locker = sqlite3.connect(state_db._db_path, timeout=0.05)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer(key)
    finally:
        locker.rollback()
        locker.close()
    assert time.monotonic() - started < 1.0


def test_late_quota_writes_after_rename_follow_current_account_generation():
    state_db.init()
    suffix = uuid.uuid4().hex
    old_key = f"openai:old-{suffix}@example.test"
    new_key = f"openai:new-{suffix}@example.test:workspace-{suffix}"
    state_db.quota_save(old_key, {"fetched_at": 1, "five_hour_util": 1})
    with channel_state.mutation_lock:
        state_db.quota_rename_account_key(
            old_key, new_key, email=f"new-{suffix}@example.test",
        )
        channel_state._install_alias(f"oauth:{old_key}", f"oauth:{new_key}")

    # Simulate three responses that started before the identity rename.
    state_db.quota_save(
        old_key, {"fetched_at": 2, "five_hour_util": 11},
    )
    state_db.quota_patch_passive(old_key, {"seven_day_util": 22})
    state_db.quota_save_openai_snapshot(
        old_key,
        {
            "fetched_at": 3,
            "primary_used_pct": 33,
            "primary_reset_sec": 60,
            "primary_window_min": 300,
        },
        {
            "five_hour_util": 33,
            "five_hour_reset_sec": 60,
            "seven_day_util": 44,
            "seven_day_reset_sec": 120,
        },
    )

    matching = {
        row["account_key"]: row
        for row in state_db.quota_load_all()
        if suffix in row["account_key"]
    }
    assert set(matching) == {new_key}
    assert matching[new_key]["email"] == f"new-{suffix}@example.test"
    assert matching[new_key]["codex_primary_used_pct"] == 33
    assert matching[new_key]["five_hour_util"] == 33
    assert matching[new_key]["seven_day_util"] == 44


def test_runtime_oauth_identity_rename_keeps_all_db_and_memory_mirrors_aligned():
    from src import cooldown, oauth_manager

    state_db.init()
    scorer.init()
    cooldown.init()
    affinity.init()
    affinity.client_init()

    suffix = uuid.uuid4().hex
    old_account = f"openai:old-{suffix}"
    new_account = f"openai:new-{suffix}"
    old_channel = f"oauth:{old_account}"
    new_channel = f"oauth:{new_account}"
    model = "gate-model"
    destination_model = "destination-only-model"

    scorer.record_success(old_channel, model, 11, 22, 33)
    scorer.record_failure(new_channel, destination_model, 99)
    old_score = scorer.get_stats(old_channel, model)
    destination_score = scorer.get_stats(new_channel, destination_model)
    cooldown.record_error(
        old_channel, model, "old wins", cooldown_until=state_db.now_ms() + 60_000,
    )
    cooldown.record_error(
        new_channel, model, "new conflict", cooldown_until=state_db.now_ms() + 120_000,
    )
    cooldown.record_error(
        new_channel, destination_model, "destination survives",
        cooldown_until=state_db.now_ms() + 120_000,
    )
    old_cooldown = cooldown.get_state(old_channel, model)
    destination_cooldown = cooldown.get_state(new_channel, destination_model)
    affinity.upsert(f"fp-old-{suffix}", old_channel, model)
    affinity.upsert(f"fp-new-{suffix}", new_channel, model)
    affinity.client_upsert(f"client-old-{suffix}", old_channel, model)
    affinity.client_upsert(f"client-new-{suffix}", new_channel, model)
    state_db.quota_save(
        old_account, {"fetched_at": 200, "raw_data": '{"source":"old"}'},
        email=f"old-{suffix}@example.test",
    )
    state_db.quota_save(
        new_account, {"fetched_at": 100, "raw_data": '{"source":"new"}'},
        email=f"new-{suffix}@example.test",
    )

    oauth_manager._rename_runtime_oauth_identity(
        old_account,
        new_account,
        email=f"new-{suffix}@example.test",
        config_mutator=lambda cfg: None,
        rollback_mutator=lambda cfg: None,
    )

    assert scorer.get_stats(old_channel, model) is None
    assert scorer.get_stats(new_channel, model) == old_score
    assert scorer.get_stats(new_channel, destination_model) == destination_score
    assert cooldown.get_state(old_channel, model) is None
    assert cooldown.get_state(new_channel, model) == old_cooldown
    assert cooldown.get_state(new_channel, destination_model) == destination_cooldown
    assert affinity.get(f"fp-old-{suffix}")["channel_key"] == new_channel
    assert affinity.get(f"fp-new-{suffix}")["channel_key"] == new_channel
    assert affinity.client_get(f"client-old-{suffix}")["channel_key"] == new_channel
    assert affinity.client_get(f"client-new-{suffix}")["channel_key"] == new_channel
    assert state_db.quota_load(old_account) is None
    assert state_db.quota_load(new_account)["raw_data"] == '{"source":"old"}'

    conn = sqlite3.connect(state_db._db_path)
    try:
        for table in ("performance_stats", "channel_errors", "cache_affinities", "client_affinities"):
            assert conn.execute(
                f"SELECT count(*) FROM {table} WHERE channel_key=?", (old_channel,),
            ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT last_error_message FROM channel_errors WHERE channel_key=? AND model=?",
            (new_channel, model),
        ).fetchone()[0] == "old wins"
    finally:
        conn.close()

    # A retry after all old rows have moved is a no-op, not a destructive
    # delete of destination-only models.
    oauth_manager._rename_runtime_oauth_identity(
        old_account,
        new_account,
        email=f"new-{suffix}@example.test",
        config_mutator=lambda cfg: None,
        rollback_mutator=lambda cfg: None,
    )
    assert scorer.get_stats(new_channel, destination_model) == destination_score
    assert cooldown.get_state(new_channel, destination_model) == destination_cooldown


def test_migration_channel_rename_merges_channel_error_primary_key_conflicts():
    state_db.init()
    suffix = uuid.uuid4().hex
    old_channel = f"oauth:old-{suffix}"
    new_channel = f"oauth:new-{suffix}"
    model = "gate-model"
    state_db.error_save(old_channel, model, 7, -1, "old wins")
    state_db.error_save(new_channel, model, 2, 123, "new conflict")

    state_db._commit_write(
        lambda conn: state_db._rename_channel_key_no_commit(
            conn, old_channel, new_channel,
        )
    )

    conn = sqlite3.connect(state_db._db_path)
    try:
        rows = conn.execute(
            "SELECT channel_key, error_count, last_error_message FROM channel_errors "
            "WHERE channel_key IN (?, ?) AND model=?",
            (old_channel, new_channel, model),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(new_channel, 7, "old wins")]


def test_refresh_identity_and_priority_publish_in_one_reload_snapshot():
    from src import cooldown, oauth_manager
    from src.channel import registry

    state_db.init()
    scorer.init()
    cooldown.init()
    affinity.init()
    affinity.client_init()
    suffix = uuid.uuid4().hex
    email = f"reload-{suffix}@example.test"
    account = {
        "email": email,
        "provider": "openai",
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "id_token": "h.p.s",
        "chatgpt_account_id": f"acct-old-{suffix}",
        "models": ["gpt-test"],
        "enabled": True,
    }
    old_account = oauth_manager._canonical_key(account)
    updated = dict(account)
    updated["chatgpt_account_id"] = f"acct-new-{suffix}"
    new_account = oauth_manager._canonical_key(updated)
    assert old_account != new_account
    old_channel = f"oauth:{old_account}"
    new_channel = f"oauth:{new_account}"

    def seed_config(cfg):
        cfg["oauthAccounts"] = [dict(account)]
        cfg.setdefault("loadBalancing", {}).setdefault("priorityOrders", {})["openai"] = [old_channel]

    config.update(seed_config)
    registry.rebuild_from_config()
    scorer.record_success(old_channel, "gpt-test", 1, 2, 3)
    cooldown.record_error(
        old_channel, "gpt-test", "keep me",
        cooldown_until=state_db.now_ms() + 60_000,
    )
    affinity.upsert(f"fp-reload-{suffix}", old_channel, "gpt-test")

    snapshots: list[tuple[list[str], list[str]]] = []

    def reload_callback(cfg):
        snapshots.append((
            [oauth_manager._canonical_key(a) for a in cfg.get("oauthAccounts", [])],
            list(cfg.get("loadBalancing", {}).get("priorityOrders", {}).get("openai", [])),
        ))
        registry.rebuild_from_config()

    config.on_reload(reload_callback)
    try:
        oauth_manager._save_token_fields(
            old_account, {"chatgpt_account_id": f"acct-new-{suffix}"},
        )
    finally:
        config._reload_callbacks.remove(reload_callback)

    assert snapshots == [([new_account], [new_channel])]
    assert registry.get_channel(new_channel) is not None
    assert registry.get_channel(old_channel) is None
    assert scorer.get_stats(new_channel, "gpt-test") is not None
    assert cooldown.get_state(new_channel, "gpt-test") is not None
    assert affinity.get(f"fp-reload-{suffix}")["channel_key"] == new_channel
    with pytest.raises(ValueError, match="restart before reusing"):
        oauth_manager.add_account(dict(account))

    config.update(
        lambda cfg: cfg.setdefault("oauthAccounts", []).append(dict(account))
    )
    registry.rebuild_from_config()
    assert registry.get_channel(old_channel) is None
    oauth_manager.delete_account(old_account)
    assert cooldown.get_state(new_channel, "gpt-test") is not None
    assert [
        oauth_manager._canonical_key(item)
        for item in config.get().get("oauthAccounts", [])
    ] == [new_account]


def test_api_channel_rename_preserves_state_with_real_reload_callback():
    from src import cooldown
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    old_name = f"old-{suffix}"
    new_name = f"new-{suffix}"
    old_key = f"api:{old_name}"
    new_key = f"api:{new_name}"

    def seed(cfg):
        cfg["channels"] = [{
            "name": old_name,
            "type": "api",
            "baseUrl": "http://127.0.0.1:9",
            "apiKey": "***",
            "protocol": "anthropic",
            "models": [{"real": "model", "alias": "model"}],
            "cc_mimicry": True,
            "enabled": True,
        }]
        cfg.setdefault("loadBalancing", {})["initialized"] = True
        cfg["loadBalancing"]["priorityOrders"] = {
            "anthropic": [old_key], "openai": [],
        }

    config.update(seed)
    registry.rebuild_from_config()
    scorer.record_success(old_key, "model", 1, 2, 3)
    cooldown.record_error(
        old_key, "model", "keep", cooldown_until=state_db.now_ms() + 60_000,
    )
    affinity.upsert(f"fp-api-{suffix}", old_key, "model")
    affinity.client_upsert(f"client-api-{suffix}", old_key, "model")
    snapshots = []

    def callback(cfg):
        snapshots.append((
            [entry.get("name") for entry in cfg.get("channels", [])],
            list(cfg.get("loadBalancing", {}).get("priorityOrders", {}).get("anthropic", [])),
        ))
        registry.rebuild_from_config()

    config.on_reload(callback)
    try:
        registry.update_api_channel(old_name, {"name": new_name})
    finally:
        config._reload_callbacks.remove(callback)

    assert snapshots == [([new_name], [new_key])]
    assert scorer.get_stats(old_key, "model") is None
    assert scorer.get_stats(new_key, "model") is not None
    assert cooldown.get_state(new_key, "model") is not None
    assert affinity.get(f"fp-api-{suffix}")["channel_key"] == new_key
    assert affinity.client_get(f"client-api-{suffix}")["channel_key"] == new_key

    # A request that started on the old generation may finish after rename;
    # every late state write must still land on the destination generation.
    scorer.record_success(old_key, "model", 4, 5, 6)
    cooldown.record_error(old_key, "late-model", "late")
    affinity.upsert(f"fp-late-{suffix}", old_key, "model")
    affinity.client_upsert(f"client-late-{suffix}", old_key, "model")
    assert scorer.get_stats(new_key, "model")["total_requests"] == 2
    assert cooldown.get_state(new_key, "late-model") is not None
    assert affinity.get(f"fp-late-{suffix}")["channel_key"] == new_key
    assert affinity.client_get(f"client-late-{suffix}")["channel_key"] == new_key

    # Maintenance cleanup must use the raw stale key rather than resolving the
    # request alias and accidentally clearing the live destination cooldown.
    conn = sqlite3.connect(state_db._db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO channel_errors
               (channel_key, model, error_count, cooldown_until, last_error_message, last_error_at)
               VALUES (?,?,?,?,?,?)""",
            (old_key, "orphan", 1, state_db.now_ms() + 60_000, "orphan", state_db.now_ms()),
        )
        conn.commit()
    finally:
        conn.close()
    registry._sync_state_db_with_channels()
    assert cooldown.get_state(new_key, "late-model") is not None
    conn = sqlite3.connect(state_db._db_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM channel_errors WHERE channel_key=?", (old_key,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    with pytest.raises(ValueError, match="restart before reusing"):
        registry.add_api_channel({
            "name": old_name,
            "baseUrl": "http://127.0.0.1:9",
            "apiKey": "***",
            "protocol": "anthropic",
            "models": [{"real": "model", "alias": "model"}],
        })

    raw_old = {
        "name": old_name, "type": "api", "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***", "protocol": "anthropic",
        "models": [{"real": "model", "alias": "model"}],
        "cc_mimicry": True, "enabled": True,
    }
    config.update(lambda cfg: cfg.setdefault("channels", []).append(raw_old))
    registry.rebuild_from_config()
    assert registry.get_channel(old_key) is None
    assert registry.delete_api_channel(old_name)
    assert cooldown.get_state(new_key, "late-model") is not None


def test_oauth_rename_state_failure_rolls_config_back_without_losing_old_state(monkeypatch):
    from src import cooldown, oauth_manager
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    old_account = {
        "email": f"rollback-{suffix}@example.test",
        "provider": "openai",
        "access_token": "***",
        "refresh_token": "***",
        "id_token": "***",
        "chatgpt_account_id": f"old-{suffix}",
        "models": ["model"],
        "enabled": True,
    }
    old_key = oauth_manager._canonical_key(old_account)
    updated = dict(old_account, chatgpt_account_id=f"new-{suffix}")
    new_key = oauth_manager._canonical_key(updated)
    old_channel = f"oauth:{old_key}"

    def seed(cfg):
        cfg["oauthAccounts"] = [dict(old_account)]
        cfg.setdefault("loadBalancing", {})["priorityOrders"] = {
            "anthropic": [], "openai": [old_channel],
        }

    config.update(seed)
    registry.rebuild_from_config()
    scorer.record_success(old_channel, "model", 1, 2, 3)
    cooldown.record_error(
        old_channel, "model", "keep", cooldown_until=state_db.now_ms() + 60_000,
    )
    affinity.upsert(f"fp-rollback-{suffix}", old_channel, "model")
    snapshots = []

    def callback(cfg):
        snapshots.append([oauth_manager._canonical_key(a) for a in cfg.get("oauthAccounts", [])])
        registry.rebuild_from_config()

    config.on_reload(callback)
    monkeypatch.setattr(
        state_db,
        "rename_runtime_channel_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            oauth_manager._save_token_fields(
                old_key, {"chatgpt_account_id": f"new-{suffix}"},
            )
    finally:
        config._reload_callbacks.remove(callback)

    assert snapshots == [[new_key], [old_key]]
    assert [oauth_manager._canonical_key(a) for a in config.get()["oauthAccounts"]] == [old_key]
    assert scorer.get_stats(old_channel, "model") is not None
    assert cooldown.get_state(old_channel, "model") is not None
    assert affinity.get(f"fp-rollback-{suffix}")["channel_key"] == old_channel


def test_post_commit_publisher_failure_installs_alias_and_attempts_remaining_publishers(monkeypatch):
    from src import channel_state, cooldown, oauth_manager

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    old_account = f"openai:publish-old-{suffix}"
    new_account = f"openai:publish-new-{suffix}"
    old_channel = f"oauth:{old_account}"
    new_channel = f"oauth:{new_account}"
    scorer.record_success(old_channel, "model", 1, 2, 3)
    cooldown.record_error(
        old_channel, "model", "keep", cooldown_until=state_db.now_ms() + 60_000,
    )
    affinity.upsert(f"fp-publish-{suffix}", old_channel, "model")
    affinity.client_upsert(f"client-publish-{suffix}", old_channel, "model")
    real_scorer_rename = scorer.rename_channel

    def broken_scorer(*args, **kwargs):
        raise RuntimeError("publisher bug")

    monkeypatch.setattr(scorer, "rename_channel", broken_scorer)
    with pytest.raises(ExceptionGroup, match="memory publication failed"):
        oauth_manager._rename_runtime_oauth_identity(
            old_account,
            new_account,
            email=f"publish-{suffix}@example.test",
            config_mutator=lambda cfg: None,
            rollback_mutator=lambda cfg: None,
        )

    assert channel_state.resolve(old_channel) == new_channel
    assert cooldown.get_state(new_channel, "model") is not None
    assert affinity.get(f"fp-publish-{suffix}")["channel_key"] == new_channel
    assert affinity.client_get(f"client-publish-{suffix}")["channel_key"] == new_channel
    monkeypatch.setattr(scorer, "rename_channel", real_scorer_rename)
    scorer.record_success(old_channel, "late", 4, 5, 6)
    assert scorer.get_stats(new_channel, "late") is not None
    assert scorer.get_stats(old_channel, "late") is None


def test_oauth_rename_config_write_failure_does_not_move_runtime_state(monkeypatch):
    from src import oauth_manager

    state_db.init(); scorer.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    account = {
        "email": f"write-fail-{suffix}@example.test",
        "provider": "openai",
        "access_token": "***",
        "refresh_token": "***",
        "chatgpt_account_id": f"old-{suffix}",
        "models": ["model"],
    }
    old_key = oauth_manager._canonical_key(account)
    old_channel = f"oauth:{old_key}"
    config.update(lambda cfg: cfg.__setitem__("oauthAccounts", [dict(account)]))
    scorer.record_success(old_channel, "model", 1, 2, 3)
    affinity.upsert(f"fp-write-fail-{suffix}", old_channel, "model")
    monkeypatch.setattr(
        config, "_write_atomic",
        lambda candidate: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        oauth_manager._save_token_fields(
            old_key, {"chatgpt_account_id": f"new-{suffix}"},
        )

    assert [oauth_manager._canonical_key(a) for a in config.get()["oauthAccounts"]] == [old_key]
    assert scorer.get_stats(old_channel, "model") is not None
    assert affinity.get(f"fp-write-fail-{suffix}")["channel_key"] == old_channel


def test_oauth_refresh_rejects_duplicate_destination_identity():
    from src import oauth_manager

    state_db.init(); scorer.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    email = f"duplicate-{suffix}@example.test"
    old_account = {
        "email": email, "provider": "openai", "access_token": "***old",
        "refresh_token": "***old", "chatgpt_account_id": f"old-{suffix}",
        "models": ["model"],
    }
    destination = {
        "email": email, "provider": "openai", "access_token": "***new",
        "refresh_token": "***new", "chatgpt_account_id": f"new-{suffix}",
        "models": ["model"],
    }
    old_key = oauth_manager._canonical_key(old_account)
    destination_key = oauth_manager._canonical_key(destination)
    config.update(
        lambda cfg: cfg.__setitem__(
            "oauthAccounts", [dict(old_account), dict(destination)],
        )
    )
    scorer.record_success(f"oauth:{old_key}", "model", 1, 2, 3)

    with pytest.raises(ValueError, match="identity already exists"):
        oauth_manager._save_token_fields(
            old_key, {"chatgpt_account_id": f"new-{suffix}"},
        )

    assert [
        oauth_manager._canonical_key(account)
        for account in config.get()["oauthAccounts"]
    ] == [old_key, destination_key]
    assert scorer.get_stats(f"oauth:{old_key}", "model") is not None


def test_concurrent_reload_waits_until_oauth_state_rename_is_published(monkeypatch):
    from src import oauth_manager
    from src.channel import registry

    state_db.init(); scorer.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    account = {
        "email": f"concurrent-{suffix}@example.test",
        "provider": "openai",
        "access_token": "***",
        "refresh_token": "***",
        "chatgpt_account_id": f"old-{suffix}",
        "models": ["model"],
    }
    old_key = oauth_manager._canonical_key(account)
    updated = dict(account, chatgpt_account_id=f"new-{suffix}")
    new_key = oauth_manager._canonical_key(updated)
    old_channel = f"oauth:{old_key}"
    new_channel = f"oauth:{new_key}"
    config.update(lambda cfg: cfg.__setitem__("oauthAccounts", [dict(account)]))
    registry.rebuild_from_config()
    scorer.record_success(old_channel, "model", 1, 2, 3)
    affinity.upsert(f"fp-concurrent-{suffix}", old_channel, "model")

    callback = lambda cfg: registry.rebuild_from_config()
    config.on_reload(callback)
    entered = threading.Event()
    allow_commit = threading.Event()
    second_done = threading.Event()
    errors: list[BaseException] = []
    real_rename = state_db.rename_runtime_channel_state

    def paused_rename(*args, **kwargs):
        entered.set()
        assert allow_commit.wait(5)
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(state_db, "rename_runtime_channel_state", paused_rename)

    def refresh_worker():
        try:
            oauth_manager._save_token_fields(
                old_key, {"chatgpt_account_id": f"new-{suffix}"},
            )
        except BaseException as exc:
            errors.append(exc)

    def reload_worker():
        try:
            config.update(lambda cfg: cfg.__setitem__("concurrentReloadProbe", suffix))
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_done.set()

    first = threading.Thread(target=refresh_worker)
    second = threading.Thread(target=reload_worker)
    try:
        first.start()
        assert entered.wait(5)
        second.start()
        # The second config lifecycle must wait until config and all mirrored
        # state for the rename have published together.
        time.sleep(0.05)
        assert not second_done.is_set()
        allow_commit.set()
        first.join(5); second.join(5)
    finally:
        allow_commit.set()
        first.join(5); second.join(5)
        config._reload_callbacks.remove(callback)

    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert scorer.get_stats(old_channel, "model") is None
    assert scorer.get_stats(new_channel, "model") is not None
    assert affinity.get(f"fp-concurrent-{suffix}")["channel_key"] == new_channel
    assert registry.get_channel(new_channel) is not None


def test_paused_old_registry_rebuild_cannot_overwrite_completed_rename(monkeypatch):
    from src import cooldown
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    old_name = f"cas-old-{suffix}"
    new_name = f"cas-new-{suffix}"
    old_key = f"api:{old_name}"
    new_key = f"api:{new_name}"

    def seed(cfg):
        cfg["channels"] = [{
            "name": old_name, "type": "api", "baseUrl": "http://127.0.0.1:9",
            "apiKey": "***", "protocol": "anthropic",
            "models": [{"real": "model", "alias": "model"}],
            "cc_mimicry": True, "enabled": True,
        }]

    config.update(seed)
    registry.rebuild_from_config()
    scorer.record_success(old_key, "model", 1, 2, 3)
    affinity.upsert(f"fp-cas-{suffix}", old_key, "model")
    entered = threading.Event(); allow = threading.Event(); rename_done = threading.Event()
    errors: list[BaseException] = []
    real_cls = registry.ApiChannel

    class PausingApiChannel(real_cls):
        def __init__(self, entry):
            entered.set()
            assert allow.wait(5)
            super().__init__(entry)

    monkeypatch.setattr(registry, "ApiChannel", PausingApiChannel)

    def old_rebuild():
        try:
            registry.rebuild_from_config()
        except BaseException as exc:
            errors.append(exc)

    def rename():
        try:
            registry.update_api_channel(old_name, {"name": new_name})
        except BaseException as exc:
            errors.append(exc)
        finally:
            rename_done.set()

    first = threading.Thread(target=old_rebuild)
    second = threading.Thread(target=rename)
    first.start(); assert entered.wait(5)
    second.start(); time.sleep(0.05)
    assert not rename_done.is_set()
    allow.set(); first.join(5); second.join(5)

    assert not errors
    assert registry.get_channel(old_key) is None
    assert registry.get_channel(new_key) is not None
    assert scorer.get_stats(new_key, "model") is not None
    assert affinity.get(f"fp-cas-{suffix}")["channel_key"] == new_key


def test_api_delete_holds_serialized_lifecycle_through_cascade(monkeypatch):
    from src.channel import registry

    state_db.init(); scorer.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    name = f"delete-race-{suffix}"
    key = f"api:{name}"
    entry = {
        "name": name, "type": "api", "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***", "protocol": "anthropic",
        "models": [{"real": "model", "alias": "model"}],
        "cc_mimicry": True, "enabled": True,
    }
    config.update(lambda cfg: cfg.__setitem__("channels", [dict(entry)]))
    registry.rebuild_from_config(); scorer.record_success(key, "old", 1, 2, 3)
    entered = threading.Event(); allow = threading.Event(); add_done = threading.Event()
    results = []; errors: list[BaseException] = []
    real_clear = scorer.clear_stats

    def paused_clear(channel_key=None, model=None):
        if channel_key == key:
            entered.set(); assert allow.wait(5)
        return real_clear(channel_key, model)

    monkeypatch.setattr(scorer, "clear_stats", paused_clear)

    def delete_worker():
        try:
            results.append(registry.delete_api_channel(name))
        except BaseException as exc:
            errors.append(exc)

    def add_worker():
        try:
            registry.add_api_channel(dict(entry))
            scorer.record_success(key, "new", 4, 5, 6)
        except BaseException as exc:
            errors.append(exc)
        finally:
            add_done.set()

    first = threading.Thread(target=delete_worker)
    second = threading.Thread(target=add_worker)
    first.start(); assert entered.wait(5)
    second.start(); time.sleep(0.05)
    assert not add_done.is_set()
    allow.set(); first.join(5); second.join(5)

    assert results == [True]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "restart before reusing" in str(errors[0])
    assert registry.get_channel(key) is None
    assert scorer.get_stats(key, "old") is None
    assert scorer.get_stats(key, "new") is None


def test_registry_stale_cleanup_clears_scorer_and_cooldown_memory_and_db(monkeypatch):
    from src import cooldown
    from src.channel import registry

    state_db.init()
    scorer.init()
    cooldown.init()
    stale = f"api:stale-{uuid.uuid4().hex}"
    scorer.record_success(stale, "model", 1, 2, 3)
    cooldown.record_error(
        stale, "model", "stale", cooldown_until=state_db.now_ms() + 60_000,
    )
    memory_only = f"api:memory-only-{uuid.uuid4().hex}"
    real_perf_save = state_db.perf_save
    monkeypatch.setattr(
        state_db, "perf_save",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    scorer.record_success(memory_only, "model", 1, 2, 3)
    monkeypatch.setattr(state_db, "perf_save", real_perf_save)
    partial = f"api:partial-memory-{uuid.uuid4().hex}"
    with scorer._lock:
        scorer._stats[(partial, "model")] = {"total_requests": 1}
    assert scorer.get_stats(stale, "model") is not None
    assert scorer.get_stats(memory_only, "model") is not None
    assert partial in scorer.channel_keys()
    assert cooldown.get_state(stale, "model") is not None

    registry._sync_state_db_with_channels()

    assert scorer.get_stats(stale, "model") is None
    assert scorer.get_stats(memory_only, "model") is None
    assert partial not in scorer.channel_keys()
    assert cooldown.get_state(stale, "model") is None
    conn = sqlite3.connect(state_db._db_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM performance_stats WHERE channel_key=?", (stale,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM channel_errors WHERE channel_key=?", (stale,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_scorer_rename_persists_before_publishing_memory(monkeypatch):
    old_channel = "api:score-old"
    new_channel = "api:score-new"
    scorer._stats.clear()
    scorer._stats[(old_channel, "model")] = {"total_requests": 1}

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scorer.state_db, "perf_rename_channel", locked)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        scorer.rename_channel(old_channel, new_channel)
    assert (old_channel, "model") in scorer._stats
    assert (new_channel, "model") not in scorer._stats


@pytest.mark.parametrize("client", [False, True])
def test_expiry_read_does_not_delete_concurrently_refreshed_affinity(monkeypatch, client):
    key = f"race-{'client' if client else 'fp'}-{uuid.uuid4().hex}"
    old = {
        "channel_key": "api:old", "model": "model", "last_used": 1,
    }
    if not client:
        old["prompt_cache_key"] = None
    entries = affinity._client_entries if client else affinity._entries
    mutation_lock = affinity._client_mutation_lock if client else affinity._mutation_lock
    entries.clear()
    entries[key] = old

    observed_stale = threading.Event()
    real_now_ms = state_db.now_ms

    def controlled_now():
        observed_stale.set()
        return 10_000_000

    monkeypatch.setattr(affinity.state_db, "now_ms", controlled_now)
    monkeypatch.setattr(
        affinity, "_client_ttl_ms" if client else "_ttl_ms", lambda: 100,
    )
    result: list[dict | None] = []
    getter = affinity.client_get if client else affinity.get

    mutation_lock.acquire()
    try:
        thread = threading.Thread(target=lambda: result.append(getter(key)))
        thread.start()
        assert observed_stale.wait(timeout=1)
        refreshed = {
            "channel_key": "api:new", "model": "model", "last_used": 9_999_950,
        }
        if not client:
            refreshed["prompt_cache_key"] = None
        if client:
            state_db.client_affinity_upsert(key, "api:new", "model", last_used=9_999_950)
            with affinity._client_lock:
                entries[key] = refreshed
        else:
            state_db.affinity_upsert(key, "api:new", "model", last_used=9_999_950)
            with affinity._lock:
                entries[key] = refreshed
    finally:
        mutation_lock.release()
    thread.join(timeout=2)
    monkeypatch.setattr(affinity.state_db, "now_ms", real_now_ms)

    assert not thread.is_alive()
    assert result == [refreshed]
    assert entries[key] == refreshed


def test_api_delete_is_one_config_snapshot_and_drops_late_generation_writes():
    from src import channel_state, concurrency, cooldown
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    name = f"delete-{suffix}"
    key = f"api:{name}"
    entry = {
        "name": name,
        "type": "api",
        "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***",
        "protocol": "anthropic",
        "models": [{"real": "model", "alias": "model"}],
        "cc_mimicry": True,
        "enabled": True,
    }

    def seed(cfg):
        cfg["channels"] = [dict(entry)]
        cfg.setdefault("loadBalancing", {})["initialized"] = True
        cfg["loadBalancing"]["priorityOrders"] = {
            "anthropic": [key], "openai": [],
        }

    config.update(seed)
    registry.rebuild_from_config()
    scorer.record_success(key, "model", 1, 2, 3)
    cooldown.record_error(key, "model", "old")
    affinity.upsert(f"fp-{suffix}", key, "model")
    affinity.client_upsert(f"client-{suffix}", key, "model")
    with concurrency._slots_guard:
        concurrency._slots[key] = concurrency.ChannelSlot(
            key=key, max_concurrent=1, in_flight=1,
        )
    snapshots = []

    def callback(cfg):
        snapshots.append((
            [channel.get("name") for channel in cfg.get("channels", [])],
            list(cfg.get("loadBalancing", {}).get("priorityOrders", {}).get("anthropic", [])),
        ))

    config.on_reload(callback)
    try:
        assert registry.delete_api_channel(name)
    finally:
        config._reload_callbacks.remove(callback)

    assert snapshots == [([], [])]
    assert channel_state.is_deleted(key)

    # Simulate completion side effects from a request holding the old Channel.
    scorer.record_success(key, "late", 4, 5, 6)
    scorer.record_failure(key, "late", 4)
    assert cooldown.record_error(key, "late", "late") == {}
    affinity.upsert(f"late-fp-{suffix}", key, "late")
    affinity.client_upsert(f"late-client-{suffix}", key, "late")
    assert scorer.get_stats(key, "late") is None
    assert cooldown.get_state(key, "late") is None
    assert affinity.get(f"late-fp-{suffix}") is None
    assert affinity.client_get(f"late-client-{suffix}") is None
    assert state_db.perf_load(key, "late") is None
    assert state_db.error_load(key, "late") is None
    concurrency.release(key)
    assert channel_state.is_deleted(key)
    with pytest.raises(ValueError, match="restart before reusing"):
        registry.add_api_channel(dict(entry))


def test_legacy_email_delete_cleans_all_accounts_atomically_and_blocks_late_quota():
    from src import channel_state, concurrency, cooldown, oauth_manager

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    email = f"multi-delete-{suffix}@example.test"
    claude = {
        "email": email, "provider": "claude", "access_token": "***",
        "refresh_token": "***", "models": ["claude-test"], "enabled": True,
    }
    openai = {
        "email": email, "provider": "openai", "access_token": "***",
        "refresh_token": "***", "chatgpt_account_id": f"ws-{suffix}",
        "models": ["gpt-test"], "enabled": True,
    }
    account_keys = [oauth_manager._canonical_key(claude), oauth_manager._canonical_key(openai)]
    channel_keys = [f"oauth:{key}" for key in account_keys]

    def seed(cfg):
        cfg["oauthAccounts"] = [dict(claude), dict(openai)]
        cfg.setdefault("loadBalancing", {})["initialized"] = True
        cfg["loadBalancing"]["priorityOrders"] = {
            "anthropic": [channel_keys[0]], "openai": [channel_keys[1]],
        }

    config.update(seed)
    for account_key, channel_key in zip(account_keys, channel_keys):
        scorer.record_success(channel_key, "model", 1, 2, 3)
        cooldown.record_error(channel_key, "model", "old")
        affinity.upsert(f"fp-{account_key}", channel_key, "model")
        affinity.client_upsert(f"client-{account_key}", channel_key, "model")
        state_db.quota_save(account_key, {"fetched_at": 1}, email=email)
        with concurrency._slots_guard:
            concurrency._slots[channel_key] = concurrency.ChannelSlot(
                key=channel_key, max_concurrent=1, in_flight=1,
            )
    snapshots = []

    def callback(cfg):
        snapshots.append((
            len(cfg.get("oauthAccounts", [])),
            dict(cfg.get("loadBalancing", {}).get("priorityOrders", {})),
        ))

    config.on_reload(callback)
    try:
        oauth_manager.delete_account(email)
    finally:
        config._reload_callbacks.remove(callback)

    assert snapshots == [(0, {"anthropic": [], "openai": []})]
    for account_key, channel_key in zip(account_keys, channel_keys):
        assert channel_state.is_deleted(channel_key)
        assert scorer.get_stats(channel_key, "model") is None
        assert cooldown.get_state(channel_key, "model") is None
        assert affinity.get(f"fp-{account_key}") is None
        assert affinity.client_get(f"client-{account_key}") is None
        assert state_db.quota_load(account_key) is None

        scorer.record_success(channel_key, "late", 4, 5, 6)
        cooldown.record_error(channel_key, "late", "late")
        affinity.upsert(f"late-fp-{account_key}", channel_key, "late")
        affinity.client_upsert(f"late-client-{account_key}", channel_key, "late")
        state_db.quota_save(account_key, {"fetched_at": 2}, email=email)
        state_db.quota_patch_passive(account_key, {"five_hour_util": 9}, email=email)
        assert scorer.get_stats(channel_key, "late") is None
        assert cooldown.get_state(channel_key, "late") is None
        assert state_db.quota_load(account_key) is None

    for channel_key in channel_keys:
        concurrency.release(channel_key)
        assert channel_state.is_deleted(channel_key)
    with pytest.raises(ValueError, match="restart before reusing"):
        oauth_manager.add_account(dict(openai))


def test_failed_delete_config_write_restores_generation_reusability(monkeypatch):
    from src import channel_state
    from src.channel import registry

    suffix = uuid.uuid4().hex
    name = f"delete-rollback-{suffix}"
    key = f"api:{name}"
    entry = {
        "name": name, "type": "api", "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***", "protocol": "anthropic", "models": [],
        "cc_mimicry": True, "enabled": True,
    }
    config.update(lambda cfg: cfg.__setitem__("channels", [dict(entry)]))
    monkeypatch.setattr(
        config, "_write_atomic",
        lambda _cfg: (_ for _ in ()).throw(OSError("write failed")),
    )
    with pytest.raises(OSError, match="write failed"):
        registry.delete_api_channel(name)
    assert not channel_state.is_deleted(key)
    assert any(
        channel.get("name") == name
        for channel in config.get().get("channels", [])
    )


def test_delete_without_tracked_slot_keeps_process_lifetime_tombstone():
    from src import channel_state, concurrency, cooldown
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    name = f"delete-no-slot-{suffix}"
    key = f"api:{name}"
    entry = {
        "name": name, "type": "api", "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***", "protocol": "anthropic", "models": [],
        "cc_mimicry": True, "enabled": True,
    }
    config.update(lambda cfg: cfg.__setitem__("channels", [dict(entry)]))
    registry.rebuild_from_config()
    with concurrency._slots_guard:
        concurrency._slots.pop(key, None)

    assert registry.delete_api_channel(name)
    assert channel_state.is_deleted(key)

    # A request may have selected the channel before deletion but not reached
    # concurrency acquire yet (or concurrency may be disabled). Its late side
    # effects must remain rejected even though no slot existed at delete time.
    scorer.record_success(key, "late", 1, 2, 3)
    cooldown.record_error(key, "late", "late")
    affinity.upsert(f"late-no-slot-{suffix}", key, "late")
    assert scorer.get_stats(key, "late") is None
    assert cooldown.get_state(key, "late") is None
    assert affinity.get(f"late-no-slot-{suffix}") is None
    with pytest.raises(ValueError, match="restart before reusing"):
        registry.add_api_channel(dict(entry))


def test_delete_renamed_destination_waits_for_old_generation_to_drain():
    from src import channel_state, concurrency, cooldown
    from src.channel import registry

    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    suffix = uuid.uuid4().hex
    old_name = f"delete-alias-old-{suffix}"
    new_name = f"delete-alias-new-{suffix}"
    old_key = f"api:{old_name}"
    new_key = f"api:{new_name}"
    entry = {
        "name": old_name, "type": "api", "baseUrl": "http://127.0.0.1:9",
        "apiKey": "***", "protocol": "anthropic",
        "models": [{"real": "model", "alias": "model"}],
        "cc_mimicry": True, "enabled": True,
    }
    config.update(lambda cfg: cfg.__setitem__("channels", [dict(entry)]))
    registry.rebuild_from_config()
    with concurrency._slots_guard:
        concurrency._slots[old_key] = concurrency.ChannelSlot(
            key=old_key, max_concurrent=1, in_flight=1,
        )

    registry.update_api_channel(old_name, {"name": new_name})
    assert channel_state.resolve(old_key) == new_key
    assert registry.delete_api_channel(new_name)
    assert channel_state.is_deleted(new_key)

    # The request acquired before rename still reports using old_key. It must
    # resolve to the deleted destination and be ignored until old_key drains.
    scorer.record_success(old_key, "late", 1, 2, 3)
    cooldown.record_error(old_key, "late", "late")
    affinity.upsert(f"late-fp-{suffix}", old_key, "late")
    assert scorer.get_stats(new_key, "late") is None
    assert cooldown.get_state(new_key, "late") is None
    assert affinity.get(f"late-fp-{suffix}") is None

    concurrency.release(old_key)
    assert channel_state.is_deleted(new_key)
    with pytest.raises(ValueError, match="restart before reusing"):
        registry.add_api_channel(dict(entry, name=new_name))
