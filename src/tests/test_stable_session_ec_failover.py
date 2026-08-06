from __future__ import annotations

import json
import os as _ap_os
import sys as _ap_sys
import time
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    from src import affinity, failover, fingerprint, scheduler, state_db
    from src.openai import handler
    from src.protocols import runtime
    return {
        "affinity": affinity,
        "failover": failover,
        "fingerprint": fingerprint,
        "handler": handler,
        "runtime": runtime,
        "scheduler": scheduler,
        "state_db": state_db,
    }


def _setup_affinity(m):
    m["state_db"].init()
    m["affinity"].delete_all()
    m["affinity"].client_delete_all()


class _Channel:
    def __init__(self, key: str, *, ch_type: str = "api", protocol: str = "openai-responses"):
        self.key = key
        self.type = ch_type
        self.protocol = protocol
        self.provider = "openai"
        self.enabled = True
        self.disabled_reason = None
        self.upstream_stream_only = False

    def supports_model(self, requested_model: str):
        return requested_model if requested_model == "gpt-5.5" else None


def _responses_body() -> dict:
    return {
        "model": "gpt-5.5",
        "stream": False,
        "prompt_cache_key": "client-visible-cache-key",
        "input": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "portable summary"}],
                "content": [{"type": "reasoning_text", "text": "portable content"}],
                "encrypted_content": "opaque-owner-only-ec",
            },
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "use tool"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
            {"type": "encrypted_content", "data": "opaque-block"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
    }


def test_stable_session_affinity_crosses_tool_transcript_and_is_isolated(m):
    handler = m["handler"]
    fingerprint = m["fingerprint"]
    headers = {
        "session-id": "stable-session-raw-value",
        "x-claude-code-session-id": "claude-parent-must-not-win",
        "x-claude-code-agent-id": "claude-agent-must-not-win",
    }
    first = _responses_body()
    second = _responses_body()
    second["input"] = [
        {"type": "function_call_output", "call_id": "different_call", "output": "different"},
        {"type": "message", "role": "user", "content": "a completely different tool turn"},
    ]

    first_key, _ = handler._openai_http_affinity_keys(
        headers, first, api_key_name="tenant-a", client_ip="192.0.2.10",
        model="gpt-5.5", ingress_protocol="responses",
    )
    second_key, _ = handler._openai_http_affinity_keys(
        headers, second, api_key_name="tenant-a", client_ip="198.51.100.20",
        model="gpt-5.5", ingress_protocol="responses",
    )
    assert first_key == second_key
    assert first_key.startswith("openai-session-v1:")
    assert "stable-session-raw-value" not in first_key
    assert "client-visible-cache-key" not in first_key

    # session-id wins over an explicit prompt_cache_key, while each isolation
    # dimension changes the hash.
    expected = fingerprint.stable_openai_affinity_key(
        "tenant-a", "responses", "gpt-5.5", "session-id", "stable-session-raw-value",
    )
    assert first_key == expected
    assert first_key != fingerprint.stable_openai_affinity_key(
        "tenant-b", "responses", "gpt-5.5", "session-id", "stable-session-raw-value",
    )
    assert first_key != fingerprint.stable_openai_affinity_key(
        "tenant-a", "chat", "gpt-5.5", "session-id", "stable-session-raw-value",
    )
    assert first_key != fingerprint.stable_openai_affinity_key(
        "tenant-a", "responses", "gpt-6", "session-id", "stable-session-raw-value",
    )


def test_claude_code_session_is_stable_affinity_across_tool_transcripts(m):
    handler = m["handler"]
    fingerprint = m["fingerprint"]
    claude_session_id = "123e4567-e89b-12d3-a456-426614174000"
    headers = {"x-claude-code-session-id": claude_session_id}
    early_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
    ]}
    tool_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "calling tool"},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "continue"},
    ]}

    early_key, _ = handler._openai_http_affinity_keys(
        headers, early_body, api_key_name="tenant-a", client_ip="192.0.2.10",
        model="gpt-5.5", ingress_protocol="chat",
    )
    tool_key, _ = handler._openai_http_affinity_keys(
        headers, tool_body, api_key_name="tenant-a", client_ip="198.51.100.20",
        model="gpt-5.5", ingress_protocol="chat",
    )

    expected = fingerprint.stable_openai_affinity_key(
        "tenant-a", "chat", "gpt-5.5", "claude-code-session-id",
        claude_session_id,
    )
    assert early_key == tool_key == expected
    assert early_key.startswith("openai-session-v1:")
    assert claude_session_id not in early_key


def test_claude_code_agent_is_effective_stable_affinity_identity(m):
    handler = m["handler"]
    fingerprint = m["fingerprint"]
    parent = "123e4567-e89b-12d3-a456-426614174000"
    other_parent = "223e4567-e89b-12d3-a456-426614174000"
    agent_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    agent_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    early_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
    ]}
    tool_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "calling tool"},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "continue"},
    ]}

    def _key(session_id, agent_id, body, client_ip):
        effective, _ = handler._openai_http_affinity_keys(
            {
                "x-claude-code-session-id": session_id,
                "x-claude-code-agent-id": agent_id,
            },
            body,
            api_key_name="tenant-a",
            client_ip=client_ip,
            model="gpt-5.5",
            ingress_protocol="chat",
        )
        return effective

    first_a = _key(parent, agent_a, early_body, "192.0.2.10")
    next_a = _key(parent, agent_a, tool_body, "198.51.100.20")
    first_b = _key(parent, agent_b, early_body, "192.0.2.10")
    switched_back_a = _key(parent, agent_a, early_body, "203.0.113.30")
    same_agent_other_parent = _key(other_parent, agent_a, early_body, "203.0.113.40")

    expected_a = fingerprint.stable_openai_affinity_key(
        "tenant-a", "chat", "gpt-5.5", "claude-code-agent-id", agent_a,
    )
    assert first_a == next_a == switched_back_a == same_agent_other_parent == expected_a
    assert first_a != first_b
    assert first_a.startswith("openai-session-v1:")
    assert parent not in first_a and agent_a not in first_a

    orphan_effective, orphan_legacy = handler._openai_http_affinity_keys(
        {"x-claude-code-agent-id": agent_a},
        tool_body,
        api_key_name="tenant-a",
        client_ip="192.0.2.10",
        model="gpt-5.5",
        ingress_protocol="chat",
    )
    assert orphan_effective == orphan_legacy


def test_explicit_prompt_cache_key_then_legacy_fallback_and_exact_bridge(m):
    _setup_affinity(m)
    handler = m["handler"]
    affinity = m["affinity"]

    explicit_body = {"model": "gpt-5.5", "input": "hi", "prompt_cache_key": "explicit-pck"}
    stable, legacy = handler._openai_http_affinity_keys(
        {
            "x-claude-code-session-id": "claude-parent-must-not-win",
            "x-claude-code-agent-id": "claude-agent-must-not-win",
        },
        explicit_body,
        api_key_name="tenant", client_ip="203.0.113.2",
        model="gpt-5.5", ingress_protocol="responses",
    )
    assert stable == m["fingerprint"].stable_openai_affinity_key(
        "tenant", "responses", "gpt-5.5", "prompt_cache_key", "explicit-pck",
    )
    assert legacy is None
    assert "explicit-pck" not in stable

    chat_body = {"model": "gpt-5.5", "messages": [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]}
    effective, legacy = handler._openai_http_affinity_keys(
        {}, chat_body, api_key_name="tenant", client_ip="203.0.113.2",
        model="gpt-5.5", ingress_protocol="chat",
    )
    assert effective == legacy

    stable = m["fingerprint"].stable_openai_affinity_key(
        "tenant", "chat", "gpt-5.5", "session-id", "session-migrating",
    )
    affinity.upsert(legacy, "api:legacy-owner", "gpt-5.5", prompt_cache_key="old-pck")
    affinity.client_upsert(
        affinity.make_client_key("tenant", "203.0.113.2", "gpt-5.5"),
        "api:soft-not-owner", "gpt-5.5",
    )
    assert handler._bridge_legacy_openai_affinity(stable, legacy) is True
    assert affinity.get(stable)["channel_key"] == "api:legacy-owner"

    unbound = m["fingerprint"].stable_openai_affinity_key(
        "tenant", "chat", "gpt-5.5", "session-id", "no-exact-transcript",
    )
    assert handler._bridge_legacy_openai_affinity(unbound, None) is False
    assert affinity.get(unbound) is None


def test_ec_helper_preserves_reasoning_summary_content_and_tool_items(m):
    body = _responses_body()
    stripped, removed = m["runtime"].retry_body_without_encrypted_content(body)
    assert removed == 2
    assert body["input"][0]["encrypted_content"] == "opaque-owner-only-ec"
    assert stripped is not body
    assert [item["type"] for item in stripped["input"]] == [
        "reasoning", "message", "function_call", "function_call_output", "message",
    ]
    reasoning = stripped["input"][0]
    assert "encrypted_content" not in reasoning
    assert reasoning["id"] == "rs_1"
    assert reasoning["summary"][0]["text"] == "portable summary"
    assert reasoning["content"][0]["text"] == "portable content"


def test_scheduler_exposes_exact_owner_and_routes_ec_without_one_portably(m, monkeypatch):
    _setup_affinity(m)
    scheduler = m["scheduler"]
    affinity = m["affinity"]
    owner = _Channel("oauth:owner", ch_type="oauth")
    fallback = _Channel("oauth:fallback", ch_type="oauth")
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [owner, fallback])
    monkeypatch.setattr(
        scheduler.registry, "get_channel",
        lambda key: owner if key == owner.key else (fallback if key == fallback.key else None),
    )
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)
    monkeypatch.setattr(scheduler.config, "get", lambda: {"channelSelection": "order", "affinity": {}})

    client_key = affinity.make_client_key("tenant", "203.0.113.5", "gpt-5.5")
    affinity.client_upsert(client_key, owner.key, "gpt-5.5")
    unbound = scheduler.schedule(
        _responses_body(), "tenant", "203.0.113.5",
        ingress_protocol="responses", fp_query="unbound-stable-fp",
    )
    assert unbound
    assert unbound.bound_channel_key is None
    assert unbound.encrypted_content_count == 2
    assert [ch.key for ch, _ in unbound.candidates] == [owner.key, fallback.key]
    assert unbound.guard_error is None

    affinity.upsert("bound-stable-fp", owner.key, "gpt-5.5")
    routed = scheduler.schedule(
        _responses_body(), "tenant", "203.0.113.5",
        ingress_protocol="responses", fp_query="bound-stable-fp",
    )
    assert routed.bound_channel_key == owner.key
    assert routed.encrypted_content_count == 2
    assert [ch.key for ch, _ in routed.candidates] == [owner.key, fallback.key]


def _patch_run_failover(monkeypatch, failover):
    async def _acquire(_key):
        return True

    monkeypatch.setattr(failover.concurrency, "try_acquire", _acquire)
    monkeypatch.setattr(failover.concurrency, "release", lambda *_: None)
    monkeypatch.setattr(failover, "_pick_non_direct_proxy_name", lambda *_: None)
    monkeypatch.setattr(failover, "_should_use_responses_upstream_ws", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(failover.local_web_tools, "request_declares_supported_tools", lambda *_: False)
    monkeypatch.setattr(failover.local_web_tools, "openai_responses_local_web_active", lambda *_: False)
    monkeypatch.setattr(failover.log_db, "record_retry_attempt", lambda *args, **kwargs: int(args[1]))
    monkeypatch.setattr(failover.log_db, "update_retry_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.log_db, "update_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.log_db, "finish_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.finalize_policy, "apply_error_health_effects", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.quota_errors, "zhipu_1310_reset_ms", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.config, "get", lambda: {
        "timeouts": {"total": 30},
        "retry": {"transient": {"enabled": True, "maxExtraAttempts": 2}},
        "concurrency": {"queueWaitSeconds": 0},
        "affinity": {},
    })


@pytest.mark.parametrize(
    "first_status,first_detail",
    [
        (403, 'HTTP 403: {"error":{"message":"You have used all credits","code":"credits_exhausted"}}'),
        (429, 'HTTP 429: {"error":{"message":"You exceeded your current quota; check your plan and billing details","code":"insufficient_quota"}}'),
    ],
)
async def test_old_api_failure_falls_back_without_ec_and_rebinds(
    m, monkeypatch, first_status, first_detail,
):
    _setup_affinity(m)
    failover = m["failover"]
    affinity = m["affinity"]
    _patch_run_failover(monkeypatch, failover)
    owner = _Channel("api:old-owner", ch_type="api")
    oauth = _Channel("oauth:new-account", ch_type="oauth")
    stable_fp = "openai-session-v1:test-rebind"
    affinity.upsert(stable_fp, owner.key, "gpt-5.5")
    route = m["scheduler"].ScheduleResult(
        [(owner, "gpt-5.5"), (oauth, "gpt-5.5")], stable_fp, True,
        client_key="client-soft", bound_channel_key=owner.key,
    )
    original = _responses_body()
    attempts = []

    async def _try_channel(ch, _model, attempt_body, *_args, **_kwargs):
        attempts.append((ch.key, attempt_body))
        if ch is owner:
            return m["runtime"].AttemptResult(
                outcome="http_auth_error" if first_status == 403 else "http_error",
                http_status=first_status,
                error_detail=first_detail,
            )
        return m["runtime"].AttemptResult(
            outcome="success", success=True, http_status=200,
            response=JSONResponse({"id": "resp_ok", "output": []}),
        )

    monkeypatch.setattr(failover, "_try_channel", _try_channel)
    response = await failover.run_failover(
        route, original, "request-1", "tenant", "203.0.113.8",
        is_stream=False, start_time=time.time(), ingress_protocol="responses",
        start_monotonic=time.monotonic(),
    )
    assert response.status_code == 200
    assert attempts[0][1] is original
    assert attempts[0][1]["input"][0]["encrypted_content"] == "opaque-owner-only-ec"
    fallback_body = attempts[1][1]
    assert fallback_body is not original
    assert "encrypted_content" not in fallback_body["input"][0]
    assert fallback_body["input"][0]["summary"][0]["text"] == "portable summary"
    assert fallback_body["input"][0]["content"][0]["text"] == "portable content"
    assert [item["type"] for item in fallback_body["input"]] == [
        "reasoning", "message", "function_call", "function_call_output", "message",
    ]
    assert original["input"][0]["encrypted_content"] == "opaque-owner-only-ec"
    assert affinity.get(stable_fp)["channel_key"] == oauth.key


@pytest.mark.parametrize("queued", [False, True])
async def test_outer_started_stream_never_rebinds_regular_or_queued_candidate(
    m, monkeypatch, queued,
):
    """Only the real stream finalizer may move a stable session binding."""
    _setup_affinity(m)
    failover = m["failover"]
    affinity = m["affinity"]
    _patch_run_failover(monkeypatch, failover)
    old_owner = _Channel("api:old-owner")
    stream_channel = _Channel("api:stream-candidate")
    stable_fp = f"stream-outer-{'queued' if queued else 'regular'}"
    affinity.upsert(stable_fp, old_owner.key, "gpt-5.5")

    async def _body_iterator():
        yield b"event: error\ndata: {}\n\n"

    async def _started_stream(*_args, **_kwargs):
        return m["runtime"].AttemptResult(
            outcome="success",
            success=True,
            stream_started=True,
            http_status=200,
            response=StreamingResponse(_body_iterator(), media_type="text/event-stream"),
        )

    monkeypatch.setattr(failover, "_try_channel", _started_stream)
    candidates = [] if queued else [(stream_channel, "gpt-5.5")]
    saturated = [(stream_channel, "gpt-5.5")] if queued else []
    if queued:
        monkeypatch.setattr(failover.config, "get", lambda: {
            "timeouts": {"total": 30},
            "retry": {"transient": {"enabled": True, "maxExtraAttempts": 2}},
            "concurrency": {"queueWaitSeconds": 1},
            "affinity": {},
        })

        async def _acquire(candidate_keys, _timeout):
            return candidate_keys[0]

        monkeypatch.setattr(failover.concurrency, "acquire_from_candidates", _acquire)

    route = m["scheduler"].ScheduleResult(
        candidates, stable_fp, True,
        saturated=saturated, bound_channel_key=old_owner.key,
    )
    response = await failover.run_failover(
        route, {"model": "gpt-5.5", "stream": True, "input": "hi"},
        f"request-stream-outer-{queued}", "tenant", "203.0.113.12",
        is_stream=True, start_time=time.time(), ingress_protocol="responses",
        start_monotonic=time.monotonic(),
    )
    assert response.status_code == 200
    assert affinity.get(stable_fp)["channel_key"] == old_owner.key


async def test_bounded_quota_skips_oauth_refresh_but_generic_403_keeps_it(m, monkeypatch):
    _setup_affinity(m)
    failover = m["failover"]
    _patch_run_failover(monkeypatch, failover)
    oauth_quota = _Channel("oauth:quota", ch_type="oauth")
    fallback = _Channel("api:fallback", ch_type="api")
    refreshes = []

    async def _refresh(account_key):
        refreshes.append(account_key)

    monkeypatch.setattr(failover.oauth_manager, "force_refresh", _refresh)
    calls = 0

    async def _quota_then_success(ch, _model, _body, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if ch is oauth_quota:
            return m["runtime"].AttemptResult(
                outcome="http_auth_error", http_status=403,
                error_detail='HTTP 403: {"error":{"message":"Monthly spending limit reached"}}',
            )
        return m["runtime"].AttemptResult(
            outcome="success", success=True, http_status=200,
            response=JSONResponse({"output": []}),
        )

    monkeypatch.setattr(failover, "_try_channel", _quota_then_success)
    quota_route = m["scheduler"].ScheduleResult(
        [(oauth_quota, "gpt-5.5"), (fallback, "gpt-5.5")], "quota-fp", True,
        bound_channel_key=oauth_quota.key,
    )
    response = await failover.run_failover(
        quota_route, _responses_body(), "request-quota", "tenant", "203.0.113.9",
        is_stream=False, start_time=time.time(), ingress_protocol="responses",
        start_monotonic=time.monotonic(),
    )
    assert response.status_code == 200
    assert refreshes == []
    assert calls == 2

    # A generic OAuth 403 keeps the historical refresh + same-channel retry.
    refreshes.clear()
    generic_calls = 0

    async def _generic_then_success(ch, _model, _body, *_args, **_kwargs):
        nonlocal generic_calls
        generic_calls += 1
        if generic_calls == 1:
            return m["runtime"].AttemptResult(
                outcome="http_auth_error", http_status=403,
                error_detail='HTTP 403: {"error":{"message":"Forbidden"}}',
            )
        return m["runtime"].AttemptResult(
            outcome="success", success=True, http_status=200,
            response=JSONResponse({"output": []}),
        )

    monkeypatch.setattr(failover, "_try_channel", _generic_then_success)
    generic_route = m["scheduler"].ScheduleResult(
        [(oauth_quota, "gpt-5.5")], "generic-fp", True,
        bound_channel_key=oauth_quota.key,
    )
    response = await failover.run_failover(
        generic_route, _responses_body(), "request-generic", "tenant", "203.0.113.9",
        is_stream=False, start_time=time.time(), ingress_protocol="responses",
        start_monotonic=time.monotonic(),
    )
    assert response.status_code == 200
    assert refreshes == [""]
    assert generic_calls == 2


@pytest.mark.parametrize(
    "quota_status,quota_detail,expected_code,expected_message",
    [
        (
            429,
            'HTTP 429: {"error":{"message":"Your account quota is exhausted","code":"quota_exhausted"}}',
            "quota_exhausted",
            "Your account quota is exhausted",
        ),
        (
            429,
            'HTTP 429: {"error":{"message":"Exceeded your current quota for sk-supersecret123; check your plan and billing details","code":"insufficient_quota"}}',
            "insufficient_quota",
            "Exceeded your current quota for [redacted credential]; check your plan and billing details",
        ),
        (
            403,
            'HTTP 403: {"error":{"message":"Billing hard limit reached","code":"billing_hard_limit_reached"}}',
            "billing_hard_limit_reached",
            "Billing hard limit reached",
        ),
        (
            403,
            'HTTP 403: {"error":{"message":"Monthly spending limit reached"}}',
            None,
            "Monthly spending limit reached",
        ),
        (
            403,
            'HTTP 403: {"error":{"message":"Credits exhausted","code":"credits_exhausted"}}',
            "credits_exhausted",
            "Credits exhausted",
        ),
    ],
)
async def test_all_failed_quota_response_is_terminal_and_safe(
    m, monkeypatch, quota_status, quota_detail, expected_code, expected_message,
):
    _setup_affinity(m)
    failover = m["failover"]
    _patch_run_failover(monkeypatch, failover)
    first = _Channel("api:user@example.com", ch_type="api")
    second = _Channel("oauth:other@example.com", ch_type="oauth")
    route = m["scheduler"].ScheduleResult(
        [(first, "gpt-5.5"), (second, "gpt-5.5")], "error-fp", False,
    )
    results = iter([
        m["runtime"].AttemptResult(
            outcome="http_auth_error", http_status=403,
            error_detail='HTTP 403: {"error":{"message":"Forbidden for user@example.com Bearer secret-token"}}',
        ),
        m["runtime"].AttemptResult(
            outcome="http_auth_error" if quota_status == 403 else "http_error",
            http_status=quota_status,
            error_detail=quota_detail,
        ),
    ])

    async def _fail(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(failover, "_try_channel", _fail)
    response = await failover.run_failover(
        route, {"model": "gpt-5.5", "input": "hi"}, "request-error",
        "tenant", "203.0.113.10", is_stream=False, start_time=time.time(),
        ingress_protocol="responses", start_monotonic=time.monotonic(),
    )
    assert response.status_code == 503
    payload = json.loads(response.body)
    error = payload["error"]
    assert {"message", "type", "code", "param", "details"}.issubset(error)
    details = error["details"]
    assert set(details) == {"summary", "root_cause", "attempts"}
    assert details["root_cause"] == {
        "status": quota_status,
        "classification": "quota_exhausted",
        "code": expected_code,
        "message": expected_message,
        "retryable": False,
        "retry_scope": "none",
    }
    assert len(details["attempts"]) == 2
    # Individual attempts retain Parrot's internal candidate-progression semantics.
    assert details["attempts"][1]["retryable"] is True
    assert details["attempts"][1]["retry_scope"] == "next_candidate"
    assert "{" not in details["summary"] and "}" not in details["summary"]
    serialized = json.dumps(details)
    assert "user@example.com" not in serialized
    assert "other@example.com" not in serialized
    assert "secret-token" not in serialized
    assert "sk-supersecret123" not in serialized


async def test_all_failed_generic_rate_limit_remains_request_retryable(m, monkeypatch):
    _setup_affinity(m)
    failover = m["failover"]
    _patch_run_failover(monkeypatch, failover)
    channel = _Channel("api:rate-limited", ch_type="api")
    route = m["scheduler"].ScheduleResult(
        [(channel, "gpt-5.5")], "rate-limit-fp", False,
    )

    async def _fail(*_args, **_kwargs):
        return m["runtime"].AttemptResult(
            outcome="http_error",
            http_status=429,
            error_detail='HTTP 429: {"error":{"message":"Rate limit exceeded","code":"rate_limit_exceeded"}}',
        )

    monkeypatch.setattr(failover, "_try_channel", _fail)
    response = await failover.run_failover(
        route, {"model": "gpt-5.5", "input": "hi"}, "request-rate-limit",
        "tenant", "203.0.113.11", is_stream=False, start_time=time.time(),
        ingress_protocol="responses", start_monotonic=time.monotonic(),
    )
    details = json.loads(response.body)["error"]["details"]
    assert details["root_cause"] == {
        "status": 429,
        "classification": "rate_limit_error",
        "code": "rate_limit_exceeded",
        "message": "Rate limit exceeded",
        "retryable": True,
        "retry_scope": "request",
    }
    assert details["attempts"][0]["retry_scope"] == "next_candidate"
