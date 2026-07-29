"""Cooldown memory/state.db consistency at channel lifecycle boundaries."""

from __future__ import annotations

from src import log_db
from src.channel import registry


def test_client_cancelled_is_a_terminal_non_error_status():
    log_db.init()
    request_id = "cancelled-terminal-status"
    log_db.insert_pending(
        request_id,
        "127.0.0.1",
        "test-key",
        "test-model",
        True,
        1,
        0,
        {},
        {"model": "test-model"},
        ingress_protocol="responses",
    )
    log_db.finish_error(
        request_id,
        "client disconnected",
        http_status=499,
        status="cancelled",
    )

    row = log_db._get_conn().execute(
        "SELECT status, finished_at, error_message FROM request_log WHERE request_id=?",
        (request_id,),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    assert row["error_message"] == "client disconnected"


def test_registry_orphan_cleanup_uses_cooldown_commit_point(monkeypatch):
    monkeypatch.setattr(registry, "_channels", {"api:live": object()})
    monkeypatch.setattr(registry.state_db, "perf_load_all", lambda: [])
    monkeypatch.setattr(
        registry.state_db,
        "error_load_all",
        lambda: [
            {"channel_key": "api:orphan", "model": "m"},
            {"channel_key": "api:live", "model": "m"},
        ],
    )
    monkeypatch.setattr(registry.state_db, "affinity_delete_stale_channels", lambda keys: None)
    monkeypatch.setattr(registry.state_db, "client_affinity_delete_stale_channels", lambda keys: None)

    cleared: list[tuple[str, str | None, bool]] = []

    def clear(channel_key, model=None, *, notify_recovered=True,
              resolve_alias=True):
        cleared.append((channel_key, model, notify_recovered))

    monkeypatch.setattr(registry.cooldown, "clear", clear)
    registry._sync_state_db_with_channels()

    assert cleared == [("api:orphan", None, False)]
