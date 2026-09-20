"""Exercise real v0.32.2 writers, not current DEFAULT_CONFIG/schema fixtures.

Requires the release Git object locally (skips in shallow source distributions).
All source copies, configs, DBs and subprocesses are confined to tmp_path.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

import pytest

RELEASE = "55178591ddfb6b4caf6c530c1a0fa6e7d8951089"
REPO = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("release_data_upgrade_probe.py")


@pytest.fixture
def release_store(tmp_path):
    available = subprocess.run(
        ["git", "cat-file", "-e", f"{RELEASE}:src/log_db.py"], cwd=REPO,
        capture_output=True,
    )
    if available.returncode:
        pytest.skip(f"real release compatibility test needs local Git object {RELEASE}")
    old = tmp_path / "v0.32.2"
    old.mkdir()
    archive = subprocess.check_output(["git", "archive", RELEASE, "src"], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        # Only trusted local Git source, never an external archive or worktree.
        tar.extractall(old, filter="data")
    data = tmp_path / "data"
    data.mkdir()
    raw = {
        "listen": {"host": "127.0.0.1", "port": 0}, "apiKeys": {},
        "oauthAccounts": [], "channels": [], "oauth": {"mockMode": True},
        "telegram": {"botToken": "", "adminIds": []},
        "stateDbPath": str(data / "state.db"),
        "runtimeStatePath": str(data / "runtime-cache.json"),
        "durableStatePath": str(data / "durable-state.json"),
        "logDir": str(data / "logs"), "logStoreBodies": True,
        "images": {"dbPath": str(data / "image_logs.db")},
    }
    config_path = data / "config.json"
    config_path.write_text(json.dumps(raw))

    def run(source, mode):
        env = dict(os.environ, ANTHROPIC_PROXY_DATA_DIR=str(data),
                   ANTHROPIC_PROXY_CONFIG=str(config_path),
                   PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(
            [sys.executable, str(PROBE), mode, str(data)], cwd=source,
            env=env, capture_output=True, text=True, timeout=90,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        line = next(line for line in completed.stdout.splitlines()
                    if line.startswith("UPGRADE_RESULT="))
        result = json.loads(line.split("=", 1)[1])
        print(f"{mode}: {json.dumps(result, ensure_ascii=False, sort_keys=True)}")
        return result

    try:
        yield old, data, run
    finally:
        # The test owns only these two directories, never the checkout or runner root.
        shutil.rmtree(old)
        shutil.rmtree(data)


def test_real_release_data_upgrade_restart_and_rollback(release_store):
    old, data, run = release_store
    baseline = run(old, "seed")
    assert baseline["request_count"] == 3
    assert baseline["media_count"] == 3
    assert baseline["snapshot_version"] == 1
    upgraded = run(REPO, "upgrade")
    assert upgraded["old_rows_preserved"]
    assert upgraded["readonly_history_unchanged"]
    assert upgraded["idempotent_schema"]
    assert upgraded["request_count"] == 4
    assert upgraded["media_count"] == 4
    restarted = run(REPO, "restart")
    assert restarted["request_count"] == 4
    assert restarted["new_state_key"] == "isolated-account"
    assert restarted["mute_present"]
    rolled_back = run(old, "rollback")
    assert rolled_back["new_sql_fields_survive"]
    assert rolled_back["new_state_key_readable"]
    assert rolled_back["unknown_domain_dropped_on_write"]
    round_trip = run(REPO, "round_trip")
    assert round_trip["request_count"] == 5
    assert round_trip["media_count"] == 5
    assert round_trip["rollback_write_readable"]
    assert round_trip["mute_absent_after_rollback"]


def test_real_release_pending_video_can_finish_after_restart(release_store):
    old, data, run = release_store
    run(old, "seed")
    resumed = run(REPO, "resume_video")
    assert resumed["old_video_resumed_in_place"]
    assert resumed["media_count"] == 3
    restarted = run(REPO, "restart_video")
    assert restarted["resumed_video_preserved"]
    assert restarted["media_count"] == 3


@pytest.mark.parametrize("kind,recorded,expected", [
    ("image", None, 9000), ("image", 120, 120),
    ("video", None, None), ("video", 120, 120),
])
def test_duration_migration_preserves_async_request_unknown(kind, recorded, expected):
    """Small current-schema unit case, separate from the real-release fixture above."""
    from src import image_db

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(image_db._SCHEMA)
        conn.execute(
            "INSERT INTO image_call_logs(request_id,created_at,action,status,media_type,"
            "duration_ms,request_duration_ms) VALUES('duration-fixture',1,'generate','success',?,?,?)",
            (kind, 9000, recorded),
        )
        for _ in range(2):
            image_db._ensure_migrations(conn)
            assert conn.execute("SELECT request_duration_ms FROM image_call_logs").fetchone()[0] == expected
    finally:
        conn.close()
