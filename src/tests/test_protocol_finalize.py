"""Finalize policy tests for protocol runtime attempts."""

from __future__ import annotations

from src.protocols import finalize


class _Scorer:
    def __init__(self):
        self.successes = []
        self.failures = []

    def record_success(self, channel_key, model, *, connect_ms=None, first_byte_ms=None, total_ms=None):
        self.successes.append({
            "channel_key": channel_key,
            "model": model,
            "connect_ms": connect_ms,
            "first_byte_ms": first_byte_ms,
            "total_ms": total_ms,
        })

    def record_failure(self, channel_key, model, *, connect_ms=None):
        self.failures.append({
            "channel_key": channel_key,
            "model": model,
            "connect_ms": connect_ms,
        })


class _Cooldown:
    def __init__(self):
        self.cleared = []
        self.errors = []

    def clear(self, channel_key, model):
        self.cleared.append((channel_key, model))

    def record_error(self, channel_key, model, error_detail=None):
        self.errors.append((channel_key, model, error_detail))


def test_success_plan_records_success_clears_cooldown_and_writes_affinity():
    plan = finalize.success_plan(cache_reasoning_replay=True)

    assert plan.terminal == "success"
    assert plan.log_success is True
    assert plan.record_success is True
    assert plan.clear_cooldown is True
    assert plan.write_affinity is True
    assert plan.cache_reasoning_replay is True
    assert plan.log_error is False
    assert plan.record_failure is False
    assert plan.record_cooldown_error is False


def test_runtime_error_plan_matches_attempt_health_policy():
    guard = finalize.error_plan("candidate_guard", failure_policy="runtime")
    assert guard.log_error is True
    assert guard.record_failure is False
    assert guard.record_cooldown_error is False

    invalid = finalize.error_plan("request_invalid", failure_policy="runtime")
    assert invalid.record_failure is True
    assert invalid.record_cooldown_error is False

    upstream = finalize.error_plan("stream_upstream_error", failure_policy="runtime")
    assert upstream.record_failure is True
    assert upstream.record_cooldown_error is True


def test_post_commit_stream_errors_always_record_failed_attempt():
    plan = finalize.error_plan("candidate_guard", failure_policy="post_commit_stream")

    assert plan.terminal == "error"
    assert plan.log_error is True
    assert plan.record_failure is True
    assert plan.record_cooldown_error is False


def test_cooldown_only_error_policy_preserves_oauth_ws_legacy_behavior():
    transform = finalize.error_plan("transform_error", failure_policy="cooldown_only")
    assert transform.record_failure is False
    assert transform.record_cooldown_error is False

    timeout = finalize.error_plan("idle_timeout", failure_policy="cooldown_only")
    assert timeout.record_failure is True
    assert timeout.record_cooldown_error is True


def test_client_cancelled_plan_preserves_terminal_upstream_state():
    in_flight = finalize.client_cancelled_plan()
    assert in_flight.terminal == "client_cancelled"
    assert in_flight.log_error is True
    assert in_flight.record_failure is False
    assert in_flight.record_cooldown_error is False

    after_success = finalize.client_cancelled_plan(saw_stream_end=True)
    assert after_success.terminal == "success"
    assert after_success.log_success is True
    assert after_success.record_success is True

    after_error = finalize.client_cancelled_plan(saw_stream_error=True)
    assert after_error.terminal == "error"
    assert after_error.log_error is True
    assert after_error.record_failure is True
    assert after_error.record_cooldown_error is True


def test_apply_success_health_effects_is_dependency_injected():
    sc = _Scorer()
    cd = _Cooldown()
    plan = finalize.success_plan()

    finalize.apply_success_health_effects(
        plan,
        scorer=sc,
        cooldown=cd,
        channel_key="api:ch",
        model="m",
        connect_ms=10,
        first_byte_ms=20,
        total_ms=30,
    )

    assert sc.successes == [{
        "channel_key": "api:ch",
        "model": "m",
        "connect_ms": 10,
        "first_byte_ms": 20,
        "total_ms": 30,
    }]
    assert cd.cleared == [("api:ch", "m")]
    assert sc.failures == []
    assert cd.errors == []


def test_apply_error_health_effects_respects_plan_flags():
    sc = _Scorer()
    cd = _Cooldown()
    plan = finalize.error_plan("candidate_guard", failure_policy="runtime")

    finalize.apply_error_health_effects(
        plan,
        scorer=sc,
        cooldown=cd,
        channel_key="api:ch",
        model="m",
        error_detail="guarded",
        connect_ms=10,
    )

    assert sc.failures == []
    assert cd.errors == []

    committed = finalize.error_plan("stream_upstream_error", failure_policy="post_commit_stream")
    finalize.apply_error_health_effects(
        committed,
        scorer=sc,
        cooldown=cd,
        channel_key="api:ch",
        model="m",
        error_detail="boom",
        connect_ms=11,
    )

    assert sc.failures == [{"channel_key": "api:ch", "model": "m", "connect_ms": 11}]
    assert cd.errors == [("api:ch", "m", "boom")]
