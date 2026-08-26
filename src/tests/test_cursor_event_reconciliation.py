from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ._isolation import isolate

isolate()

from src import config, log_db  # noqa: E402
from src.cursor_bridge import runtime as cursor_runtime  # noqa: E402
from src.oauth import cursor as cursor_provider  # noqa: E402
from src.telegram.menus import logs_menu  # noqa: E402


@dataclass
class _FakeBridgeClient:
    conversation: str = "11111111-2222-3333-4444-555555555555"
    discarded: str = ""

    def chat_completions(self, **_kwargs):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "composer-2.5",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }

    def conversation_id(self, _session_id: str) -> str:
        return self.conversation

    def discard_conversation(self, session_id: str, *, cancel: bool = False) -> None:
        _ = cancel
        self.discarded = session_id

    def close(self) -> None:
        return


def setup_function(_function):
    log_db.init()
    conn = log_db._get_conn()
    for table in ("upstream_attempt_usage", "retry_chain", "request_detail", "request_log"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    config.update(lambda cfg: cfg.setdefault("oauth", {}).__setitem__("mockMode", True))


def _make_attempt(
    request_id: str,
    *,
    created_at: float,
    conversation_id: str | None,
    input_tokens: int = 100,
    output_tokens: int = 10,
):
    log_db.insert_pending(
        request_id, "127.0.0.1", "test-key", "claude-fable-5", False, 1, 0,
        {}, {}, ingress_protocol="chat", created_at=created_at,
    )
    attempt = log_db.record_retry_attempt(
        request_id, 1, "oauth:cursor:test-subject", "oauth", "claude-fable-5",
        created_at, upstream_protocol="openai-chat",
    )
    log_db.mark_retry_attempt_dispatch(
        attempt, {"model": "claude-fable-5-thinking-xhigh"},
    )
    _ = conversation_id  # Same-conversation turns must still remain independent.
    log_db.finish_success(
        request_id, "oauth:cursor:test-subject", "oauth", "claude-fable-5",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_tokens=0, cache_read_tokens=0,
        response_body="{}", usage_observed=True, upstream_protocol="openai-chat",
    )
    log_db.update_retry_attempt(attempt, outcome="success", ended_at=created_at + 1)
    return attempt


def _inject_historical_event(
    request_id: str, *, superseded: bool, event_key: str | None,
) -> None:
    conn = log_db._get_conn()
    conn.execute(
        """UPDATE upstream_attempt_usage SET
               cursor_conversation_id='historical-conversation',
               cursor_event_key=?, cursor_event_input_tokens=2,
               cursor_event_output_tokens=20,
               cursor_event_cache_creation_tokens=1000,
               cursor_event_cache_read_tokens=9000,
               cursor_event_cost_ticks=123456789,
               cursor_event_superseded=?, cursor_event_reconciled_at=?
             WHERE root_request_id=?""",
        (event_key, int(superseded), time.time(), request_id),
    )
    conn.commit()


def test_old_schema_and_old_event_config_remain_compatible():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE request_log (id INTEGER PRIMARY KEY, request_id TEXT, created_at REAL);
        CREATE TABLE retry_chain (id INTEGER PRIMARY KEY, request_id TEXT, channel_key TEXT);
        CREATE TABLE upstream_attempt_usage (id INTEGER PRIMARY KEY, retry_attempt_id INTEGER);
        CREATE TABLE proxy_chain (id INTEGER PRIMARY KEY, request_id TEXT);
        CREATE TABLE local_web_log (
          id INTEGER PRIMARY KEY, request_id TEXT, tool_name TEXT, started_at REAL
        );
    """)
    log_db._ensure_migrations(conn)
    attempt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(upstream_attempt_usage)")
    }
    assert "cursor_event_cache_read_tokens" in attempt_columns
    assert "cursor_event_superseded" in attempt_columns
    conn.close()

    config.update(lambda cfg: cfg.setdefault("cursorOAuth", {}).update({
        "eventSyncEnabled": True,
        "eventSyncSeconds": 1,
        "eventLookbackSeconds": 999,
        "eventToolSettleSeconds": 1,
    }))
    assert config.get()["cursorOAuth"]["eventSyncEnabled"] is True


def test_historical_superseded_and_event_owner_are_ignored_everywhere():
    created = time.time() - 5
    _make_attempt(
        "cursor-superseded", created_at=created,
        conversation_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        input_tokens=100, output_tokens=10,
    )
    _make_attempt(
        "cursor-owner", created_at=created + 1,
        conversation_id="ffffffff-1111-2222-3333-444444444444",
        input_tokens=60, output_tokens=6,
    )
    _inject_historical_event("cursor-superseded", superseded=True, event_key=None)
    _inject_historical_event("cursor-owner", superseded=False, event_key="historical-owner")

    recent = {row["request_id"]: row for row in log_db.recent_logs(limit=10)}
    assert recent["cursor-superseded"]["input_tokens"] == 100
    assert recent["cursor-superseded"]["cache_read_tokens"] == 0
    assert recent["cursor-owner"]["input_tokens"] == 60
    assert recent["cursor-owner"]["cache_read_tokens"] == 0
    assert not recent["cursor-owner"].get("cursor_event_reconciled")

    for request_id, expected_input, expected_output in (
        ("cursor-superseded", 100, 10), ("cursor-owner", 60, 6),
    ):
        detail = log_db.log_detail(request_id)
        assert detail["log"]["input_tokens"] == expected_input
        assert detail["log"]["output_tokens"] == expected_output
        attempt = detail["billing_attempts"][0]
        assert attempt["input_tokens"] == expected_input
        assert attempt["output_tokens"] == expected_output
        assert attempt["cache_creation_tokens"] == 0
        assert attempt["cache_read_tokens"] == 0
        assert attempt["cost_ticks"] != 123_456_789
        assert attempt["cost_source"] != "actual"

    expected = {"input": 160, "output": 16, "cache_creation": 0, "cache_read": 0}
    channel = log_db.tokens_for_channel("oauth:cursor:test-subject", created - 10)
    apikey = log_db.tokens_for_apikey("test-key", created - 10)
    for stats in (channel, apikey):
        for key, value in expected.items():
            assert stats[key] == value
        assert stats["actual_cost_ticks"] == 0

    model = log_db.channel_model_stats(
        "oauth:cursor:test-subject", since_ts=created - 10,
    )[0]
    assert model["input"] == 160
    assert model["output"] == 16
    assert model["cache_creation"] == 0
    assert model["cache_read"] == 0
    assert model["actual_cost_ticks"] == 0

    snapshot = log_db.stats_period_snapshot(created - 10)
    grouped = snapshot["by_channel"]["oauth:cursor:test-subject"]
    assert grouped["input"] == 160
    assert grouped["output"] == 16
    assert grouped["cache_creation"] == 0
    assert grouped["cache_read"] == 0
    assert any(
        row.get("request_id") == "cursor-owner"
        for row in snapshot["summary"]["recent_cache_misses"]
    )


def test_multi_turn_conversation_keeps_each_live_row_independent():
    created = time.time() - 5
    conversation = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    _make_attempt(
        "cursor-turn-1", created_at=created, conversation_id=conversation,
        input_tokens=50, output_tokens=5,
    )
    _make_attempt(
        "cursor-turn-2", created_at=created + 1, conversation_id=conversation,
        input_tokens=70, output_tokens=7,
    )
    fresh = log_db._get_conn().execute(
        """SELECT cursor_conversation_id, cursor_event_key, cursor_event_reconciled_at,
                  cursor_event_superseded
             FROM upstream_attempt_usage WHERE root_request_id=?""",
        ("cursor-turn-2",),
    ).fetchone()
    assert fresh["cursor_conversation_id"] is None
    assert fresh["cursor_event_key"] is None
    assert fresh["cursor_event_reconciled_at"] is None
    assert fresh["cursor_event_superseded"] == 0
    _inject_historical_event("cursor-turn-1", superseded=True, event_key=None)
    _inject_historical_event("cursor-turn-2", superseded=False, event_key="last-owner")

    stats = log_db.tokens_for_channel("oauth:cursor:test-subject", created - 10)
    assert stats["input"] == 120
    assert stats["output"] == 12
    recent = {row["request_id"]: row for row in log_db.recent_logs(limit=10)}
    assert recent["cursor-turn-1"]["input_tokens"] == 50
    assert recent["cursor-turn-2"]["input_tokens"] == 70


def test_event_runtime_fetch_and_reconcile_entrypoints_are_removed():
    root = Path(__file__).resolve().parents[2]
    server_source = (root / "server.py").read_text()
    oauth_menu_source = (root / "src/telegram/menus/oauth_menu.py").read_text()
    public_config = (root / "config.example.json").read_text()
    config_docs = (root / "docs/02-config-schema.md").read_text()
    assert "cursor_reconcile" not in server_source
    assert "cursor_reconcile" not in oauth_menu_source
    assert not (root / "src/cursor_reconcile.py").exists()
    assert not hasattr(cursor_provider, "fetch_usage_events_sync")
    assert not hasattr(log_db, "reconcile_cursor_usage_events")
    assert not hasattr(log_db, "cursor_reconciliation_targets")
    for stale_name in (
        "eventSyncEnabled",
        "eventSyncSeconds",
        "eventLookbackSeconds",
        "eventPageSize",
        "eventMaxPages",
        "eventLegacyMatchSeconds",
        "eventToolSettleSeconds",
    ):
        assert stale_name not in public_config
        assert stale_name not in config_docs


def test_cursor_detail_disclaimer_is_provider_specific():
    created = time.time() - 5
    _make_attempt(
        "cursor-detail", created_at=created,
        conversation_id="cccccccc-dddd-eeee-ffff-000000000000",
    )
    detail = log_db.log_detail("cursor-detail")
    cursor_text = logs_menu._render_detail(detail)
    assert cursor_text.count("准确计价以官方为准") == 1
    assert "Cursor 官方 usage event" not in cursor_text

    detail["log"]["final_channel_key"] = "api:not-cursor"
    non_cursor_text = logs_menu._render_detail(detail)
    assert "准确计价以官方为准" not in non_cursor_text


def test_bridge_generation_still_returns_live_usage_without_event_owner_header():
    runtime = cursor_runtime.CursorBridgeRuntime()
    fake = _FakeBridgeClient()
    try:
        runtime.ensure_started()
        runtime._clients["cursor:test"] = fake
        response = httpx.post(
            runtime.base_url + "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {runtime.bearer_secret}",
                cursor_runtime._ACCOUNT_HEADER: "cursor:test",
            },
            json={
                "model": "composer-2.5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 200
        assert response.json()["usage"]["prompt_tokens"] == 10
        assert "X-Parrot-Cursor-Conversation-Id" not in response.headers
        assert fake.discarded
    finally:
        runtime.stop()
