from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ._isolation import isolate

isolate()

from src import config, cursor_reconcile, log_db, oauth_manager  # noqa: E402
from src.cursor_bridge import runtime as cursor_runtime  # noqa: E402
from src.oauth import cursor as cursor_provider  # noqa: E402


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
    actual_model: str = "claude-fable-5-thinking-xhigh",
    input_tokens: int = 100,
    output_tokens: int = 10,
):
    log_db.insert_pending(
        request_id,
        "127.0.0.1",
        "test-key",
        "claude-fable-5",
        False,
        1,
        0,
        {},
        {},
        ingress_protocol="chat",
        created_at=created_at,
    )
    attempt = log_db.record_retry_attempt(
        request_id,
        1,
        "oauth:cursor:test-subject",
        "oauth",
        "claude-fable-5",
        created_at,
        upstream_protocol="openai-chat",
    )
    log_db.mark_retry_attempt_dispatch(attempt, {"model": actual_model})
    if conversation_id:
        assert log_db.set_retry_attempt_cursor_conversation_id(
            attempt, conversation_id,
        )
    log_db.finish_success(
        request_id,
        "oauth:cursor:test-subject",
        "oauth",
        "claude-fable-5",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        response_body="{}",
        usage_observed=True,
        upstream_protocol="openai-chat",
    )
    log_db.update_retry_attempt(attempt, outcome="success", ended_at=created_at + 1)
    return attempt


def _event(
    conversation_id: str,
    *,
    timestamp: float,
    key: str = "event-1",
    model: str = "claude-fable-5-thinking-xhigh",
    input_tokens: int = 2,
    output_tokens: int = 20,
    cache_creation_tokens: int = 1_000,
    cache_read_tokens: int = 9_000,
    cost_ticks: int = 123_456_789,
):
    return {
        "event_key": key,
        "conversation_id": conversation_id,
        "timestamp_ms": int(timestamp * 1000),
        "model": model,
        "kind": "USAGE_EVENT_KIND_INCLUDED_IN_ULTRA",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cost_ticks": cost_ticks,
        "request_units": 2.0,
    }


def test_old_log_schema_adds_cursor_columns_before_creating_indexes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE request_log (
          id INTEGER PRIMARY KEY, request_id TEXT, created_at REAL
        );
        CREATE TABLE retry_chain (
          id INTEGER PRIMARY KEY, request_id TEXT, channel_key TEXT
        );
        CREATE TABLE upstream_attempt_usage (
          id INTEGER PRIMARY KEY, retry_attempt_id INTEGER
        );
        CREATE TABLE proxy_chain (
          id INTEGER PRIMARY KEY, request_id TEXT
        );
        CREATE TABLE local_web_log (
          id INTEGER PRIMARY KEY, request_id TEXT, tool_name TEXT, started_at REAL
        );
    """)
    log_db._ensure_migrations(conn)
    attempt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(upstream_attempt_usage)")
    }
    retry_columns = {row[1] for row in conn.execute("PRAGMA table_info(retry_chain)")}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(upstream_attempt_usage)")}
    assert "cursor_event_cache_read_tokens" in attempt_columns
    assert "cursor_conversation_id" in retry_columns
    assert "idx_attempt_cursor_conversation" in indexes
    assert "idx_attempt_cursor_event" in indexes
    conn.close()


def test_bridge_returns_internal_cursor_conversation_header():
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
        assert response.headers[cursor_runtime._CONVERSATION_HEADER] == fake.conversation
        assert fake.discarded
    finally:
        runtime.stop()


def test_cursor_usage_events_fetch_normalizes_cache_and_fractional_cents(monkeypatch):
    config.update(lambda cfg: cfg.setdefault("oauth", {}).__setitem__("mockMode", False))
    token = cursor_provider._mock_jwt("auth0|user_events", int(time.time() * 1000) + 3600_000)

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert request.headers["origin"] == "https://cursor.com"
        assert "user_events%3A%3A" in request.headers["cookie"]
        if body["page"] == 1:
            events = [{
                "conversationId": "conv-1",
                "timestamp": "1787046690324",
                "model": "claude-fable-5-thinking-xhigh",
                "kind": "USAGE_EVENT_KIND_INCLUDED_IN_ULTRA",
                "requestsCosts": 2,
                "chargedCents": 8.167349815368652,
                "isChargeable": True,
                "isHeadless": False,
                "tokenUsage": {
                    "inputTokens": 2,
                    "outputTokens": 743,
                    "cacheWriteTokens": 1949,
                    "cacheReadTokens": 20141,
                },
            }]
        else:
            events = []
        return httpx.Response(200, json={
            "totalUsageEventsCount": 1,
            "usageEventsDisplay": events,
        })

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cursor_provider,
        "_http_client",
        lambda **_kwargs: httpx.Client(transport=transport),
    )
    events = cursor_provider.fetch_usage_events_sync(
        token,
        start_ms=1787046600000,
        end_ms=1787046800000,
        page_size=1000,
    )
    assert len(events) == 1
    event = events[0]
    assert event["input_tokens"] == 2
    assert event["output_tokens"] == 743
    assert event["cache_creation_tokens"] == 1949
    assert event["cache_read_tokens"] == 20141
    assert event["cost_ticks"] == 816_734_982


def test_exact_conversation_reconciliation_overlays_stats_cost_and_detail():
    created = time.time() - 5
    conversation = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _make_attempt("cursor-exact", created_at=created, conversation_id=conversation)
    result = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject",
        [_event(conversation, timestamp=created + 1)],
        since_ts=created - 10,
    )
    assert result["matched"] == 1
    assert result["exact"] == 1

    stats = log_db.tokens_for_channel("oauth:cursor:test-subject", since_ts=created - 10)
    assert stats["input"] == 2
    assert stats["output"] == 20
    assert stats["cache_creation"] == 1000
    assert stats["cache_read"] == 9000
    assert stats["actual_cost_ticks"] == 123_456_789
    assert stats["actual_costed_success"] == 1
    assert stats["unpriced_success"] == 0

    snapshot = log_db.stats_period_snapshot(created - 10)
    channel = snapshot["by_channel"]["oauth:cursor:test-subject"]
    assert channel["input"] == 2
    assert channel["cache_creation"] == 1000
    assert channel["cache_read"] == 9000
    assert channel["actual_cost_ticks"] == 123_456_789
    assert not any(
        row.get("request_id") == "cursor-exact"
        for row in snapshot["summary"]["recent_cache_misses"]
    )
    models = log_db.channel_model_stats(
        "oauth:cursor:test-subject", since_ts=created - 10,
    )
    assert models[0]["cache_read"] == 9000
    assert models[0]["actual_cost_ticks"] == 123_456_789

    detail = log_db.log_detail("cursor-exact")
    assert detail["log"]["cursor_event_reconciled"] is True
    assert detail["log"]["cache_read_tokens"] == 9000
    assert detail["billing_attempts"][0]["cost_source"] == "actual"
    assert detail["billing_attempts"][0]["cost_ticks"] == 123_456_789
    recent = log_db.recent_logs(channel_key="oauth:cursor:test-subject")
    assert recent[0]["cursor_event_reconciled"] is True
    assert recent[0]["cache_read_tokens"] == 9000
    assert recent[0]["cache_creation_tokens"] == 1000

    repeated = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject",
        [_event(conversation, timestamp=created + 1)],
        since_ts=created - 10,
    )
    assert repeated["matched"] == 0


def test_one_official_event_supersedes_partial_tool_turns_without_double_count():
    created = time.time() - 5
    conversation = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    _make_attempt("cursor-tool-1", created_at=created, conversation_id=conversation, input_tokens=50)
    _make_attempt("cursor-tool-2", created_at=created + 1, conversation_id=conversation, input_tokens=60)
    result = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject",
        [_event(conversation, timestamp=created + 2, key="event-tool")],
        since_ts=created - 10,
    )
    assert result["matched"] == 1
    assert result["superseded"] == 1
    stats = log_db.tokens_for_channel("oauth:cursor:test-subject", since_ts=created - 10)
    assert stats["total"] == 2
    assert stats["input"] == 2
    assert stats["cache_creation"] == 1000
    assert stats["cache_read"] == 9000
    assert stats["actual_cost_ticks"] == 123_456_789


def test_late_tool_turn_moves_existing_single_event_after_grace_period():
    created = time.time() - 300
    conversation = "dddddddd-eeee-ffff-0000-111111111111"
    _make_attempt("cursor-late-tool-1", created_at=created, conversation_id=conversation)
    event = _event(conversation, timestamp=created + 1, key="event-late-tool")
    first = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject", [event], since_ts=created - 10,
    )
    assert first["matched"] == 1

    _make_attempt(
        "cursor-late-tool-2", created_at=created + 10,
        conversation_id=conversation,
    )
    log_db._get_conn().execute(
        "UPDATE upstream_attempt_usage SET settled_at=? WHERE root_request_id=?",
        (created + 11, "cursor-late-tool-2"),
    )
    log_db._get_conn().commit()
    second = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject",
        [event],
        since_ts=created - 10,
        tool_settle_seconds=15,
    )
    assert second["moved"] == 1
    assert second["superseded"] == 1
    rows = log_db._get_conn().execute(
        """SELECT root_request_id, cursor_event_key, cursor_event_superseded
             FROM upstream_attempt_usage
            WHERE cursor_conversation_id=? ORDER BY root_request_id""",
        (conversation,),
    ).fetchall()
    states = {row["root_request_id"]: dict(row) for row in rows}
    assert states["cursor-late-tool-1"]["cursor_event_superseded"] == 1
    assert states["cursor-late-tool-1"]["cursor_event_key"] is None
    assert states["cursor-late-tool-2"]["cursor_event_key"] == "event-late-tool"
    stats = log_db.tokens_for_channel(
        "oauth:cursor:test-subject", since_ts=created - 10,
    )
    assert stats["cache_read"] == 9000
    assert stats["actual_cost_ticks"] == 123_456_789


def test_cursor_reconcile_orchestrator_fetches_pending_exact_event(monkeypatch):
    created = time.time() - 5
    conversation = "cccccccc-dddd-eeee-ffff-000000000000"
    _make_attempt("cursor-orchestrated", created_at=created, conversation_id=conversation)
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [{
            "provider": "cursor",
            "type": "cursor",
            "email": "cursor@example.test",
            "subject": "test-subject",
            "sub": "test-subject",
            "access_token": "access",
            "refresh_token": "refresh",
            "expired": "2099-01-01T00:00:00Z",
            "enabled": True,
            "billing_cycle_start": datetime.fromtimestamp(
                created - 60, tz=timezone.utc,
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
        "cursorOAuth": {
            **cfg.get("cursorOAuth", {}),
            "eventSyncEnabled": True,
        },
    }))

    async def valid_token(_account_key):
        return "access"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    monkeypatch.setattr(
        cursor_provider,
        "fetch_usage_events_sync",
        lambda *_args, **_kwargs: [_event(
            conversation, timestamp=created + 1, key="event-orchestrated",
        )],
    )
    result = asyncio.run(cursor_reconcile.sync_account(
        "cursor:test-subject", force=True,
    ))
    assert result["action"] == "reconciled"
    assert result["matched"] == 1
    assert result["exact"] == 1


def test_legacy_reconciliation_requires_unique_timestamp_and_native_model():
    created = time.time() - 5
    _make_attempt("cursor-legacy", created_at=created, conversation_id=None)
    result = log_db.reconcile_cursor_usage_events(
        "cursor:test-subject",
        [_event("legacy-conversation", timestamp=created + 1, key="legacy-event")],
        since_ts=created - 10,
        legacy_match_seconds=5,
    )
    assert result["legacy"] == 1
    row = log_db._get_conn().execute(
        "SELECT cursor_conversation_id, cursor_event_key FROM upstream_attempt_usage "
        "WHERE root_request_id='cursor-legacy'"
    ).fetchone()
    assert row["cursor_conversation_id"] == "legacy-conversation"
    assert row["cursor_event_key"] == "legacy-event"
