from __future__ import annotations

import json

from src import config, log_db


def test_log_store_bodies_false_keeps_summary_and_attempt_accounting() -> None:
    previous_store_bodies = config.get().get("logStoreBodies", True)
    try:
        config.update(lambda cfg: cfg.__setitem__("logStoreBodies", False))
        log_db.init()
        handle = log_db.insert_pending(
            "body-storage-disabled",
            "127.0.0.1",
            "test-key",
            "grok-4.5",
            True,
            msg_count=1,
            tool_count=0,
            request_headers={"Authorization": "Bearer secret"},
            request_body={"model": "grok-4.5", "input": "private prompt"},
            ingress_protocol="responses",
        )
        attempt = log_db.record_retry_attempt(
            handle,
            1,
            "oauth:xai:test@example.com",
            "oauth",
            "grok-4.5",
            1.0,
            upstream_protocol="openai-responses",
        )
        log_db.mark_retry_attempt_dispatch(
            attempt,
            {"model": "grok-4.5", "service_tier": "priority"},
        )
        response_body = json.dumps(
            {
                "model": "grok-4.5",
                "service_tier": "priority",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 3,
                    "cost_in_usd_ticks": 123456,
                },
            }
        )
        log_db.finish_success(
            handle,
            "oauth:xai:test@example.com",
            "oauth",
            "grok-4.5",
            input_tokens=11,
            output_tokens=7,
            cache_creation_tokens=0,
            cache_read_tokens=3,
            total_ms=25,
            response_body=response_body,
            upstream_protocol="openai-responses",
            usage_observed=True,
        )

        conn = log_db._get_conn_for_ref(handle.db)
        detail = conn.execute(
            "SELECT request_headers, request_body, response_body "
            "FROM request_detail WHERE request_id=?",
            (handle.request_id,),
        ).fetchone()
        assert detail is not None
        assert tuple(detail) == (None, None, None)

        summary = conn.execute(
            "SELECT status, input_tokens, output_tokens, cache_read_tokens, actual_service_tier "
            "FROM request_log WHERE request_id=?",
            (handle.request_id,),
        ).fetchone()
        assert tuple(summary) == ("success", 11, 7, 3, "priority")

        ledger = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, service_tier, cost_ticks "
            "FROM upstream_attempt_usage WHERE root_request_id=?",
            (handle.request_id,),
        ).fetchone()
        assert ledger is not None
        assert tuple(ledger) == (11, 7, 3, "priority", 123456)
    finally:
        config.update(
            lambda cfg: cfg.__setitem__("logStoreBodies", previous_store_bodies)
        )
