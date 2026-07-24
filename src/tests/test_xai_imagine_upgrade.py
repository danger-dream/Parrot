"""Upgrade compatibility checks for xAI Imagine state.

These tests start a fresh Python process against a database shaped like an older
Parrot installation.  The subprocess boundary ensures the real startup path is
exercised without sharing config/state module globals with the rest of pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def test_old_state_db_is_upgraded_in_place_without_losing_existing_rows(tmp_path):
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    state_path = data_dir / "state.db"

    # This intentionally omits every new Imagine field and allowVideos, matching
    # a deployment that upgrades without editing config.json first.
    config_path.write_text(
        json.dumps(
            {
                "listen": {"host": "127.0.0.1", "port": 0},
                "apiKeys": {
                    "legacy-client": {
                        "key": "ccp-legacy-client",
                        "enabled": True,
                        "allowedModels": ["legacy-model"],
                        "allowImages": True,
                    }
                },
                "oauthAccounts": [],
                "channels": [],
                "stateDbPath": "state.db",
                "logDir": "logs",
                "telegram": {"botToken": "", "adminIds": []},
                "xaiOAuth": {
                    "defaultModels": ["legacy-grok-model"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    script = r'''
import json
import os
import sqlite3

config_path = os.environ["ANTHROPIC_PROXY_CONFIG"]
state_path = os.path.join(os.environ["ANTHROPIC_PROXY_DATA_DIR"], "state.db")

# One real pre-existing runtime table and row are enough to prove that startup
# upgrades in place rather than replacing the database.
conn = sqlite3.connect(state_path)
conn.execute("""
CREATE TABLE performance_stats (
  channel_key TEXT NOT NULL,
  model TEXT NOT NULL,
  total_requests INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  recent_requests INTEGER DEFAULT 0,
  recent_success_count INTEGER DEFAULT 0,
  avg_connect_ms REAL DEFAULT 0,
  avg_first_byte_ms REAL DEFAULT 0,
  avg_total_ms REAL DEFAULT 0,
  last_updated INTEGER NOT NULL,
  PRIMARY KEY (channel_key, model)
)
""")
conn.execute(
    "INSERT INTO performance_stats "
    "(channel_key, model, total_requests, success_count, last_updated) "
    "VALUES (?, ?, ?, ?, ?)",
    ("legacy-channel", "legacy-model", 9, 8, 123456),
)
conn.commit()
conn.close()

from src import config, state_db

state_db.init()

conn = sqlite3.connect(state_path)
row = conn.execute(
    "SELECT total_requests, success_count, last_updated "
    "FROM performance_stats WHERE channel_key=? AND model=?",
    ("legacy-channel", "legacy-model"),
).fetchone()
assert row == (9, 8, 123456), row

table = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='xai_video_jobs'"
).fetchone()
assert table == ("xai_video_jobs",), table
columns = [row[1] for row in conn.execute("PRAGMA table_info(xai_video_jobs)")]
assert columns == [
    "request_id", "channel_key", "api_key_name", "model", "created_at", "expires_at"
], columns
conn.close()

loaded = config.get()
legacy_key = loaded["apiKeys"]["legacy-client"]
assert legacy_key["allowImages"] is True
assert legacy_key["allowVideos"] is False
assert legacy_key["allowedModels"] == ["legacy-model"]
assert loaded["xaiOAuth"]["defaultModels"] == ["legacy-grok-model"]
assert loaded["xaiOAuth"]["imageModels"] == [
    "grok-imagine-image", "grok-imagine-image-quality"
]
assert loaded["xaiOAuth"]["videoModels"] == [
    "grok-imagine-video", "grok-imagine-video-1.5"
]

# The same guarantees must be durable on disk, not only present in memory.
with open(config_path, "r", encoding="utf-8") as f:
    saved = json.load(f)
assert saved["apiKeys"]["legacy-client"] == legacy_key
assert saved["xaiOAuth"]["defaultModels"] == ["legacy-grok-model"]
'''

    env = os.environ.copy()
    env.update(
        {
            "ANTHROPIC_PROXY_DATA_DIR": str(data_dir),
            "ANTHROPIC_PROXY_CONFIG": str(config_path),
            "DISABLE_OAUTH_NETWORK_CALLS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout
