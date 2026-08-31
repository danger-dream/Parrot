"""Finalize policy tests for protocol runtime attempts."""

from __future__ import annotations

import sqlite3

import pytest

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

    def clear_on_success(self, channel_key, model):
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


def test_explicit_cooldown_deadline_is_forwarded_only_for_cooldown_plan():
    class ExplicitCooldown(_Cooldown):
        def record_error(self, channel_key, model, error_detail=None, *, cooldown_until=None):
            self.errors.append((channel_key, model, error_detail, cooldown_until))

    cd = ExplicitCooldown()
    finalize.apply_error_health_effects(
        finalize.error_plan("http_error"),
        scorer=_Scorer(), cooldown=cd, channel_key="api:a", model="m",
        error_detail="rate limited", cooldown_until=1_700_000_005_000,
    )
    assert cd.errors == [("api:a", "m", "rate limited", 1_700_000_005_000)]

    finalize.apply_error_health_effects(
        finalize.error_plan("request_invalid"),
        scorer=_Scorer(), cooldown=cd, channel_key="api:b", model="m",
        cooldown_until=1_700_000_005_000,
    )
    assert len(cd.errors) == 1


def test_runtime_error_plan_matches_attempt_health_policy():
    guard = finalize.error_plan("candidate_guard", failure_policy="runtime")
    assert guard.log_error is True
    assert guard.record_failure is False
    assert guard.record_cooldown_error is False

    invalid = finalize.error_plan("request_invalid", failure_policy="runtime")
    assert invalid.record_failure is False
    assert invalid.record_cooldown_error is False

    upstream = finalize.error_plan("stream_upstream_error", failure_policy="runtime")
    assert upstream.record_failure is True
    assert upstream.record_cooldown_error is True


def test_post_commit_stream_errors_record_failure_except_health_neutral_outcomes():
    plan = finalize.error_plan("candidate_guard", failure_policy="post_commit_stream")

    assert plan.terminal == "error"
    assert plan.log_error is True
    assert plan.record_failure is True
    assert plan.record_cooldown_error is False

    for outcome in ("request_invalid", "connection_lifecycle"):
        neutral = finalize.error_plan(outcome, failure_policy="post_commit_stream")
        assert neutral.terminal == "error"
        assert neutral.record_failure is False
        assert neutral.record_cooldown_error is False


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


def test_success_health_effects_isolate_sqlite_failures_and_continue(caplog):
    class LockedScorer(_Scorer):
        def record_success(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    cd = _Cooldown()
    finalize._last_warning_at.clear()
    caplog.set_level("WARNING")
    finalize.apply_success_health_effects(
        finalize.success_plan(),
        scorer=LockedScorer(),
        cooldown=cd,
        channel_key="api:ch",
        model="m",
    )

    assert cd.cleared == [("api:ch", "m")]
    assert "response preserved" in caplog.text


def test_health_effects_do_not_hide_programming_errors():
    class BrokenScorer(_Scorer):
        def record_success(self, *_args, **_kwargs):
            raise ValueError("programming bug")

    with pytest.raises(ValueError, match="programming bug"):
        finalize.apply_success_health_effects(
            finalize.success_plan(),
            scorer=BrokenScorer(),
            cooldown=_Cooldown(),
            channel_key="api:ch",
            model="m",
        )


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.ProgrammingError("closed connection"),
        sqlite3.IntegrityError("constraint failed"),
        sqlite3.OperationalError("no such table: channel_errors"),
    ],
)
def test_health_effects_do_not_hide_sqlite_programming_or_schema_errors(exc):
    class BrokenScorer(_Scorer):
        def record_success(self, *_args, **_kwargs):
            raise exc

    with pytest.raises(type(exc), match=str(exc)):
        finalize.apply_success_health_effects(
            finalize.success_plan(),
            scorer=BrokenScorer(),
            cooldown=_Cooldown(),
            channel_key="api:ch",
            model="m",
        )


def test_error_health_effects_isolate_sqlite_failures_and_continue(caplog):
    class LockedCooldown(_Cooldown):
        def record_error(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    sc = _Scorer()
    finalize._last_warning_at.clear()
    caplog.set_level("WARNING")
    finalize.apply_error_health_effects(
        finalize.error_plan("upstream_error", failure_policy="post_commit_stream"),
        scorer=sc,
        cooldown=LockedCooldown(),
        channel_key="api:ch",
        model="m",
        error_detail="failed",
        connect_ms=12,
    )

    assert sc.failures == [{
        "channel_key": "api:ch",
        "model": "m",
        "connect_ms": 12,
    }]
    assert "response preserved" in caplog.text


def test_health_effect_sqlite_warnings_are_rate_limited(caplog):
    class LockedScorer(_Scorer):
        def record_success(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    finalize._last_warning_at.clear()
    caplog.set_level("WARNING")
    for _ in range(3):
        finalize.apply_success_health_effects(
            finalize.success_plan(),
            scorer=LockedScorer(),
            cooldown=_Cooldown(),
            channel_key="api:ch",
            model="m",
        )
    warnings = [r for r in caplog.records if "response preserved" in r.message]
    assert len(warnings) == 1


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
