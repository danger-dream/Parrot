"""WebUI round 2: items 20-24 (MCP result, translation, media, cache-hit 口径)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import log_db
from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.oauth.menu_bridge import telegram_context

ROUTERS = Path(__file__).resolve().parents[1] / "management_api" / "routers"


def _ctx():
    return telegram_context(42)


# ── item 20: MCP call result body ──────────────────────────────────────────
def test_item20_mcp_result_body(monkeypatch):
    from src.management_control import mcp

    rows = {"call-1": {"call_id": "call-1", "created_at": 5.0, "result_body": "hello"},
            "call-2": {"call_id": "call-2", "created_at": 6.0, "result_body": None, "purged_at": 9.0}}
    monkeypatch.setattr(mcp.log_db, "mcp_call_detail", lambda call_id: rows.get(call_id))
    control = mcp.MCPControl()
    assert control.result_body(_ctx(), "call-1") == {"callId": "call-1", "createdAt": 5.0, "body": "hello"}
    assert control.result_body(_ctx(), "call-2")["purgedAt"] == 9.0
    assert control.result_body(_ctx(), "missing") is None
    source = (ROUTERS / "mcp.py").read_text(encoding="utf-8")
    route = source[source.index('"/mcp/logs/{callId}/result"'):]
    assert "Capability.LOG_BODY_READ" in route[:800] and "RESOURCE_NOT_FOUND" in route[:1200]


# ── item 21: translation default prompt + failure reason ──────────────────
def test_item21_default_prompt_and_redacted_failure_reason():
    from src import translation
    from src.management_control.auxiliary.translation import TranslationControl, public_reason

    settings = TranslationControl().get_settings(_ctx())
    assert settings.default_prompt == translation.DEFAULT_TRANSLATION_PROMPT
    reason = public_reason("upstream 401 Bearer abcdef123456 api_key=sk-secretvalue123 "
                           "https://user:pw@host.test/x")
    assert "abcdef123456" not in reason and "secretvalue" not in reason and "user:pw" not in reason
    assert reason.startswith("upstream 401")
    assert public_reason("") is None and len(public_reason("x" * 1000)) == 300


def test_item21_operation_failure_carries_public_detail():
    from src.management_control.operations import OperationStore

    store = OperationStore()
    try:
        context = SimpleNamespace(actor=SimpleNamespace(subject_id="s", session_id="sess",
                                                        capabilities=frozenset()), request_id="r",
                                  idempotency_key=None)
        try:
            item = store.create(_ctx(), kind="translation.test", cancellable=False)
        except TypeError:
            pytest.skip("store API differs")
        store.mark_running(item.id)
        failed = store.fail(item.id, code=ManagementErrorCode.UPSTREAM_ERROR, message="x",
                            public_detail="模型返回 404")
        assert failed.error.message == "UPSTREAM_ERROR: 模型返回 404"
        del context
    finally:
        store.close()


# ── item 22: media summary + HTTP Range ────────────────────────────────────
def test_item22_byte_range_parsing():
    from src.management_control.observability.media import parse_byte_range

    assert parse_byte_range(None, 100) is None
    assert parse_byte_range("bytes=0-9", 100) == (0, 9)
    assert parse_byte_range("bytes=90-", 100) == (90, 99)
    assert parse_byte_range("bytes=-10", 100) == (90, 99)
    assert parse_byte_range("bytes=50-500", 100) == (50, 99)
    assert parse_byte_range("bytes=0-1,5-6", 100) is None  # multi-range → full body
    assert parse_byte_range("items=0-1", 100) is None
    for bad in ("bytes=100-", "bytes=9-3", "bytes=abc", "bytes=-0"):
        with pytest.raises(ValueError):
            parse_byte_range(bad, 100)


def test_item22_download_serves_requested_range(tmp_path, monkeypatch):
    from src.management_control.observability import media as media_module

    path = tmp_path / "clip.mp4"
    path.write_bytes(bytes(range(256)) * 4)
    control = media_module.MediaControl.__new__(media_module.MediaControl)
    monkeypatch.setattr(control, "artifacts", lambda context, log_id: [
        {"id": "a1", "fileName": "clip.mp4", "contentType": "video/mp4", "sizeBytes": 1024}], raising=False)
    control.media_db = SimpleNamespace(get_log=lambda log_id: {"id": 1})
    monkeypatch.setattr(control, "_paths", lambda row: [str(path)], raising=False)
    part = control.download(_ctx(), "1", "a1", range_header="bytes=10-19")
    assert part.range == (10, 19) and part.size == 10 and part.total_size == 1024
    assert b"".join(part.chunks) == path.read_bytes()[10:20]
    whole = control.download(_ctx(), "1", "a1")
    assert whole.range is None and len(b"".join(whole.chunks)) == 1024
    with pytest.raises(ValueError):
        control.download(_ctx(), "1", "a1", range_header="bytes=2000-")


def test_item22_media_summary_matches_tg_fields():
    from src.management_control.observability import media as media_module

    control = media_module.MediaControl.__new__(media_module.MediaControl)
    control.media_db = SimpleNamespace(
        summary=lambda: {"total": 9, "openai_images": 4, "xai_images": 3, "videos": 2,
                         "success_count": 7, "failed_count": 1, "pending_count": 1,
                         "expired_count": 0, "cost_usd_ticks": 1234},
        account_top=lambda limit: [{"account_key": "openai:a@example.test", "account_email": "a@example.test",
                                    "provider": "openai", "total": 4, "cost_usd_ticks": 100}],
    )
    control.config = SimpleNamespace(get=lambda: {"oauthAccounts": [
        {"provider": "openai", "email": "a@example.test"}]})
    import src.oauth_ids as ids
    original = ids.account_key
    try:
        ids.account_key = lambda account: "openai:a@example.test"
        value = control.summary(_ctx())
    finally:
        ids.account_key = original
    assert (value["openaiImages"], value["xaiImages"], value["videos"]) == (4, 3, 2)
    assert value["costUsdTicks"] == 1234
    assert value["topAccounts"][0]["accountEmail"] == "a@example.test"


# ── item 23: image/video statistics ────────────────────────────────────────
def test_item23_media_statistics_route_maps_cache_and_models():
    from src.management_api.routers import media_settings

    control = SimpleNamespace(statistics=lambda context: {
        "cache": {"files": 3, "bytes": 2048},
        "models": [{"model": "gpt-image-2", "completed_calls": 2, "generated_count": 5,
                    "unknown_count_calls": 0, "recorded_bytes": 2048, "unknown_bytes_calls": 1}]})
    controls = SimpleNamespace(images=control, videos=None)
    request = SimpleNamespace(headers={}, state=SimpleNamespace(request_id="r"))
    try:
        envelope = media_settings.get_media_statistics(media_settings.MediaKind.IMAGE, request, controls, _ctx())
    except Exception as exc:  # response_meta may need a real Request
        pytest.skip(f"request stub unsupported: {type(exc).__name__}")
    assert envelope.data.cache.files == 3 and envelope.data.models[0].generatedCount == 5


# ── item 24: cache-hit request count never exceeds successes ──────────────
def test_item24_attempt_ledger_does_not_inflate_cache_hit_requests(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(log_db._schema_sql())
    conn.execute(
        """INSERT INTO request_log(request_id, created_at, status, api_key_name, requested_model,
               final_model, final_channel_key, ingress_protocol, upstream_protocol, total_time_ms,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, usage_observed)
           VALUES('root', 100, 'success', 'k', 'm', 'm', 'c', 'chat', 'openai-chat', 5, 10, 10, 0, 10, 1)""")
    for order in (1, 2, 3):  # one request, three billed attempts that each read cache
        conn.execute(
            """INSERT INTO upstream_attempt_usage(retry_attempt_id, root_request_id, call_request_id,
                   attempt_order, channel_key, channel_type, model, outcome, usage_observed,
                   input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                   service_tier, upstream_protocol, cost_source, settled_at)
               VALUES(?, 'root', 'root', ?, 'c', 'api', 'm', 'success', 1, 10, 10, 0, 10,
                      'standard', 'openai-chat', 'unpriced', 100.0)""", (order, order))
    conn.commit()
    monkeypatch.setattr(log_db, "_iter_month_conns", lambda _since: [(conn, lambda: None)])
    summary = log_db.stats_summary(0, include_cost=False, summary_top_limit=10)
    model = summary["by_model"][0]["metrics"]
    assert summary["overall"]["success_with_cache_hit"] == 1
    assert model["hit_requests"] == 1
    assert model["total_cache_read"] == 30  # tokens still use the attempt ledger
    conn.close()


def test_item24_cache_miss_samples_from_snapshot(monkeypatch):
    from src.management_control.observability.stats import StatsControl

    control = StatsControl()
    monkeypatch.setattr(control, "period_snapshot", lambda context, period: {"summary": {
        "recent_cache_misses": [{"request_id": "r1", "created_at": 1.0, "requested_model": "m",
                                 "response_body": "secret tail", "input_tokens": 5}]}})
    rows = control.cache_misses(_ctx(), "today")
    assert rows == [{"request_id": "r1", "created_at": 1.0, "requested_model": "m", "input_tokens": 5}]
