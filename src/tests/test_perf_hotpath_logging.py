"""Focused regressions and measurements for async request-log hot paths."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from starlette.websockets import WebSocketState

from src.channel.base import build_dispatch_metadata
from src.protocols.runtime import AttemptResult
from src.scheduler import ScheduleResult

from src.tests import conftest as test_conftest
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_protocol_fake_upstreams import channel_slots_enabled


_import_modules = fake._import_modules


async def test_http_stream_precommit_log_metrics(monkeypatch, m, capsys):
    """Measure commits, loop-visible commits, and re-parses of one wire body.

    The test suite normally executes ``asyncio.to_thread`` inline.  A context
    marker therefore records the production offload boundary without making
    SQLite's thread-local connection cache add worker-initialization commits.
    """

    fake._setup(m)
    router = fake.MockRouter()
    captured: dict[str, bytes] = {}

    def handler(req: httpx.Request):
        captured["wire_body"] = req.content
        payload = json.loads(req.content)
        assert payload["model"] == "gpt-real"
        return fake._chat_sse_response("hot path")

    router.register("https://perf-hotpath.example", handler)
    fake._install_channels(m, [
        fake._make_openai_channel(
            "perf-hotpath",
            "https://perf-hotpath.example",
            protocol="openai-chat",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    offload_depth = contextvars.ContextVar("perf_log_offload_depth", default=0)

    async def tracked_to_thread(function, /, *args, **kwargs):
        token = offload_depth.set(offload_depth.get() + 1)
        try:
            return function(*args, **kwargs)
        finally:
            offload_depth.reset(token)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)

    commits = {"total": 0, "event_loop": 0}
    conn = m["log_db"]._get_conn()

    def trace_sql(statement: str) -> None:
        if statement.strip().upper() != "COMMIT":
            return
        commits["total"] += 1
        if offload_depth.get() == 0:
            commits["event_loop"] += 1

    conn.set_trace_callback(trace_sql)

    parses = {"wire_body": 0}
    original_update_fast = m["log_db"].update_pending_fast_mode_from_upstream
    original_outbound_payload = m["log_db"]._outbound_payload

    def tracked_update_fast(request_id, upstream_body, upstream_headers=None, **kwargs):
        metadata = kwargs.get("dispatch_metadata")
        if metadata is None and isinstance(upstream_body, (bytes, bytearray, str)):
            parses["wire_body"] += 1
        return original_update_fast(
            request_id,
            upstream_body,
            upstream_headers,
            **kwargs,
        )

    def tracked_outbound_payload(request_body):
        # This focused request has exactly one outbound payload.  The dispatch
        # logger calls this helper once for tier and once for model before the
        # in-memory transport handler receives the same bytes.
        if isinstance(request_body, (bytes, bytearray, str)):
            parses["wire_body"] += 1
        return original_outbound_payload(request_body)

    monkeypatch.setattr(m["log_db"], "update_pending_fast_mode_from_upstream", tracked_update_fast)
    monkeypatch.setattr(m["log_db"], "_outbound_payload", tracked_outbound_payload)

    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    response, client, _route = await fake._call_anthropic_core(m, router, body)

    # run_failover has crossed the first-visible-event gate when it returns the
    # StreamingResponse; terminal generator writes are intentionally excluded.
    measured = {
        "event_loop_db_commits": commits["event_loop"],
        "transactions": commits["total"],
        "wire_body_json_parses": parses["wire_body"],
    }
    print("PERF_HOTPATH_METRICS=" + json.dumps(measured, sort_keys=True))

    text = await fake._consume_streaming_to_string(response)
    await client.aclose()
    conn.set_trace_callback(None)

    assert response.status_code == 200
    assert "hot path" in text
    assert measured == {
        "event_loop_db_commits": 0,
        # The seven HTTP visibility points are intentionally retained; unlike
        # the adjacent WS dispatch facts, they span real lifecycle boundaries.
        "transactions": 7,
        "wire_body_json_parses": 0,
    }


async def test_http_stream_all_log_writes_run_outside_event_loop(monkeypatch, m):
    """Exercise the real shared default executor rather than the inline test shim."""

    fake._setup(m)
    router = fake.MockRouter()
    router.register(
        "https://perf-worker.example",
        lambda _request: fake._chat_sse_response("worker path"),
    )
    fake._install_channels(m, [
        fake._make_openai_channel(
            "perf-worker",
            "https://perf-worker.example",
            protocol="openai-chat",
            alias="sonnet",
            real="gpt-real",
        ),
    ])

    loop_thread = threading.get_ident()
    write_threads: list[tuple[str, int]] = []
    for name in (
        "insert_pending",
        "record_retry_attempt",
        "update_pending",
        "update_pending_fast_mode_from_upstream",
        "record_proxy_attempt",
        "mark_retry_attempt_dispatch",
        "update_proxy_attempt",
        "update_retry_attempt",
        "finish_success",
        "finish_error",
    ):
        original = getattr(m["log_db"], name)

        def track(*args, __name=name, __original=original, **kwargs):
            write_threads.append((__name, threading.get_ident()))
            return __original(*args, **kwargs)

        monkeypatch.setattr(m["log_db"], name, track)

    monkeypatch.setattr(asyncio, "to_thread", test_conftest._ORIG_TO_THREAD)
    body = {
        "model": "sonnet",
        "stream": True,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }
    response, client, _route = await fake._call_anthropic_core(m, router, body)
    text = await fake._consume_streaming_to_string(response)
    await client.aclose()

    assert "worker path" in text
    assert write_threads
    assert all(thread_id != loop_thread for _name, thread_id in write_threads), write_threads


@pytest.mark.parametrize(
    ("transport", "saturated", "blocked_write"),
    [
        ("http", False, "record_retry_attempt"),
        ("http", False, "update_pending"),
        ("http", False, "update_retry_attempt"),
        ("http", True, "record_retry_attempt"),
        ("ws", False, "record_retry_attempt"),
        ("ws", False, "update_pending"),
        ("ws", True, "record_retry_attempt"),
    ],
)
async def test_channel_slot_release_survives_real_executor_log_cancellation(
    monkeypatch, m, transport, saturated, blocked_write, channel_slots_enabled,
):
    """Post-acquire writes, cancellation terminals, and release are ordered once."""

    fake._setup(m)
    channel = fake._make_openai_channel(
        f"cancel-{transport}-{blocked_write}-{int(saturated)}",
        "https://cancel-slot.example",
        protocol="openai-responses",
        alias="model",
        real="real-model",
        extra={"maxConcurrent": 1},
    )
    fake._install_channels(m, [channel])
    concurrency = m["failover"].concurrency
    with concurrency._slots_guard:
        concurrency._slots.clear()

    request_id = f"cancel-{transport}-{blocked_write}-{int(saturated)}-{uuid.uuid4().hex}"
    body = {"model": "model", "input": "hello", "stream": False}
    request_handle = m["log_db"].insert_pending(
        request_id,
        "1.2.3.4",
        "key",
        "model",
        transport == "ws",
        1,
        0,
        {},
        body,
        ingress_protocol=("responses_ws" if transport == "ws" else "responses"),
    )

    entered = threading.Event()
    allow_worker = threading.Event()
    worker_done = threading.Event()
    worker_threads: list[int] = []
    loop_thread = threading.get_ident()
    retry_outcomes: list[str | None] = []
    request_terminals: list[tuple[str, int | None]] = []
    release_calls: list[str] = []

    original_record_retry = m["log_db"].record_retry_attempt
    original_update_pending = m["log_db"].update_pending
    original_update_retry = m["log_db"].update_retry_attempt
    original_finish_error = m["log_db"].finish_error
    original_release = concurrency.release

    def maybe_block(name, func, *args, **kwargs):
        if name == blocked_write:
            worker_threads.append(threading.get_ident())
            entered.set()
            try:
                assert allow_worker.wait(timeout=5)
                return func(*args, **kwargs)
            finally:
                worker_done.set()
        return func(*args, **kwargs)

    def record_retry(*args, **kwargs):
        return maybe_block("record_retry_attempt", original_record_retry, *args, **kwargs)

    def update_pending(*args, **kwargs):
        return maybe_block("update_pending", original_update_pending, *args, **kwargs)

    def update_retry(*args, **kwargs):
        retry_outcomes.append(kwargs.get("outcome"))
        return maybe_block("update_retry_attempt", original_update_retry, *args, **kwargs)

    def finish_error(*args, **kwargs):
        request_terminals.append((kwargs.get("status", "error"), kwargs.get("http_status")))
        return original_finish_error(*args, **kwargs)

    def release(channel_key: str) -> None:
        release_calls.append(channel_key)
        original_release(channel_key)

    monkeypatch.setattr(m["log_db"], "record_retry_attempt", record_retry)
    monkeypatch.setattr(m["log_db"], "update_pending", update_pending)
    monkeypatch.setattr(m["log_db"], "update_retry_attempt", update_retry)
    monkeypatch.setattr(m["log_db"], "finish_error", finish_error)
    monkeypatch.setattr(concurrency, "release", release)
    monkeypatch.setattr(asyncio, "to_thread", test_conftest._ORIG_TO_THREAD)

    route = ScheduleResult(
        candidates=[] if saturated else [(channel, "real-model")],
        saturated=[(channel, "real-model")] if saturated else [],
        affinity_hit=False,
        fp_query=None,
        client_key="client:cancel",
    )

    if transport == "http":
        monkeypatch.setattr(
            m["failover"], "_pick_non_direct_proxy_name",
            lambda *_args: "proxy-a" if blocked_write == "update_pending" else None,
        )
        monkeypatch.setattr(
            m["failover"], "_should_use_responses_upstream_ws",
            lambda *_args, **_kwargs: False,
        )

        async def failed_attempt(*_args, **_kwargs):
            return AttemptResult(
                success=False,
                outcome="connect_error",
                error_detail="injected pre-transition failure",
            )

        monkeypatch.setattr(m["failover"], "_try_channel", failed_attempt)
        request = m["failover"].run_failover(
            route,
            body,
            request_id,
            "key",
            "1.2.3.4",
            is_stream=False,
            start_time=time.time(),
            ingress_protocol="responses",
        )
    else:
        monkeypatch.setattr(
            m["responses_ws"], "_pick_non_direct_proxy_name",
            lambda *_args: "proxy-a" if blocked_write == "update_pending" else None,
        )

        async def must_not_reach_upstream(*_args, **_kwargs):
            raise AssertionError("cancellation point was not reached before dispatch")

        monkeypatch.setattr(m["responses_ws"], "_try_ws_channel", must_not_reach_upstream)
        request = m["responses_ws"]._run_ws_failover(
            SimpleNamespace(application_state=WebSocketState.CONNECTED),
            first_obj={"type": "response.create", **body},
            schedule_result=route,
            body=body,
            request_id=request_id,
            api_key_name="key",
            client_ip="1.2.3.4",
            start_time=time.time(),
            start_monotonic=time.monotonic(),
            fp_query=None,
        )

    task = asyncio.create_task(request)
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        slot = next(
            row for row in concurrency.snapshot()
            if row["channel_key"] == channel.key
        )
        assert slot["in_flight"] == 1
        assert worker_done.is_set() is False

        task.cancel()
        await asyncio.sleep(0)
        # The cancelled waiter still owns the thread result and its DB handle.
        assert task.done() is False
        assert release_calls == []
        slot = next(
            row for row in concurrency.snapshot()
            if row["channel_key"] == channel.key
        )
        assert slot["in_flight"] == 1

        allow_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        allow_worker.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert worker_done.is_set()
    assert worker_threads and all(thread != loop_thread for thread in worker_threads)
    assert release_calls == [channel.key]
    assert request_terminals == [("cancelled", 499)]
    assert retry_outcomes == [
        "connect_error" if blocked_write == "update_retry_attempt" else "cancelled"
    ]

    conn = m["log_db"]._get_conn_for_ref(request_handle.db)
    request_row = conn.execute(
        "SELECT status, http_status FROM request_log WHERE request_id=?",
        (request_id,),
    ).fetchone()
    retry_rows = conn.execute(
        "SELECT outcome, ended_at FROM retry_chain WHERE request_id=? ORDER BY attempt_order",
        (request_id,),
    ).fetchall()
    assert tuple(request_row) == ("cancelled", 499)
    assert len(retry_rows) == 1
    assert retry_rows[0]["outcome"] == retry_outcomes[0]
    assert retry_rows[0]["ended_at"] is not None
    slot = next(
        row for row in concurrency.snapshot()
        if row["channel_key"] == channel.key
    )
    assert slot["in_flight"] == 0


@pytest.mark.parametrize(
    ("payload", "protocol", "headers"),
    [
        ({"model": "claude", "speed": "fast"}, "anthropic", {}),
        ({"model": "gpt", "service_tier": "priority"}, "openai-chat", {}),
        ({"response": {"model": "gpt-r"}}, "openai-responses", {}),
        ({"model": "claude"}, "anthropic", {"Anthropic-Beta": "fast-mode-2026-02-01"}),
        ({"model": "gpt", "service_tier": 42}, "openai-responses", {}),
    ],
)
def test_dispatch_metadata_matches_legacy_extractors(m, payload, protocol, headers):
    metadata = build_dispatch_metadata(payload, protocol, headers)
    assert metadata.outbound_model_id == m["log_db"]._outbound_model_id(payload)
    assert metadata.outbound_service_tier == m["log_db"]._outbound_service_tier(
        payload, protocol,
    )
    assert metadata.fast_mode is m["log_db"].extract_fast_mode(payload, headers=headers)


def test_ws_dispatch_transaction_matches_legacy_state_and_commits_once(m):
    fake._setup(m)
    log_db = m["log_db"]
    suffix = uuid.uuid4().hex

    def create_attempt(prefix: str):
        request = log_db.insert_pending(
            f"{prefix}-{suffix}", "1.2.3.4", "key", "alias", True,
            1, 0, {}, {"model": "alias"}, ingress_protocol="responses_ws",
        )
        attempt = log_db.record_retry_attempt(
            request, 1, "api:test", "api", "resolved", time.time(),
            upstream_protocol="openai-responses", client_visible_model="alias",
        )
        return request, attempt

    payload = {"model": "wire-model", "service_tier": "priority"}
    metadata = build_dispatch_metadata(payload, "openai-responses")
    merged_request, merged_attempt = create_attempt("merged")
    legacy_request, legacy_attempt = create_attempt("legacy")
    conn = log_db._get_conn_for_ref(merged_request.db)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    log_db.record_upstream_dispatch(
        merged_request,
        merged_attempt,
        payload,
        dispatch_metadata=metadata,
        dispatched_at=123.5,
    )
    merged_statements = list(statements)
    statements.clear()
    log_db.update_pending_fast_mode_from_upstream(legacy_request, payload)
    log_db.mark_retry_attempt_dispatch(
        legacy_attempt, payload, dispatched_at=123.5,
    )
    legacy_statements = list(statements)
    conn.set_trace_callback(None)

    def relevant_state(request, attempt):
        request_row = conn.execute(
            "SELECT fast_mode FROM request_log WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        attempt_row = conn.execute(
            """SELECT outbound_service_tier, dispatched_at,
                      binding_provider_id, binding_model_id, binding_pricing_key,
                      binding_source, binding_json, binding_version, binding_revision
                 FROM retry_chain WHERE id=?""",
            (attempt.row_id,),
        ).fetchone()
        return tuple(request_row), tuple(attempt_row)

    assert relevant_state(merged_request, merged_attempt) == relevant_state(
        legacy_request, legacy_attempt,
    )
    assert sum(sql.strip().upper() == "COMMIT" for sql in merged_statements) == 1
    assert sum(sql.strip().upper() == "COMMIT" for sql in legacy_statements) == 2
    merged_sql = "\n".join(merged_statements).upper()
    assert merged_sql.index("UPDATE REQUEST_LOG") < merged_sql.index("UPDATE RETRY_CHAIN")
    assert merged_sql.rindex("UPDATE RETRY_CHAIN") < merged_sql.rindex("COMMIT")
    assert merged_request.db == merged_attempt.db


def test_ws_dispatch_merge_preserves_best_effort_dispatch_failure(monkeypatch, m):
    fake._setup(m)
    log_db = m["log_db"]
    request = log_db.insert_pending(
        f"dispatch-failure-{uuid.uuid4().hex}", "1.2.3.4", "key", "alias",
        True, 1, 0, {}, {"model": "alias"}, ingress_protocol="responses_ws",
    )
    attempt = log_db.record_retry_attempt(
        request, 1, "api:test", "api", "resolved", time.time(),
        upstream_protocol="openai-responses", client_visible_model="alias",
    )
    metadata = build_dispatch_metadata(
        {"model": "wire-model", "service_tier": "priority"},
        "openai-responses",
    )

    def fail_binding(**_kwargs):
        raise RuntimeError("injected binding failure")

    monkeypatch.setattr(log_db.model_pricing, "build_pricing_binding", fail_binding)
    assert log_db.record_upstream_dispatch(
        request, attempt, dispatch_metadata=metadata,
    ) is True

    conn = log_db._get_conn_for_ref(request.db)
    request_row = conn.execute(
        "SELECT fast_mode FROM request_log WHERE request_id=?",
        (request.request_id,),
    ).fetchone()
    attempt_row = conn.execute(
        "SELECT dispatched_at FROM retry_chain WHERE id=?", (attempt.row_id,),
    ).fetchone()
    # This matches the old sequence: the required badge write commits, a failed
    # best-effort dispatch diagnostic does not block the upstream send.
    assert request_row["fast_mode"] == 1
    assert attempt_row["dispatched_at"] is None
