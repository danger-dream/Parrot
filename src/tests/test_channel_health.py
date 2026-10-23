"""Channel health only counts models still in use and names the weak ones."""

from __future__ import annotations

from src.management_control.channel_health import ACTIVE_WINDOW_MS, active_model_rates, rate_health

NOW = 10_000_000_000


def _row(model, requests, successes, age_ms):
    return {"model": model, "recent_requests": requests, "recent_success_count": successes, "last_updated": NOW - age_ms}


def test_stale_windows_do_not_flag_a_channel():
    rows = [
        _row("old-failure", 2, 0, ACTIVE_WINDOW_MS + 1),  # failed days ago, never routed since
        _row("busy", 50, 49, 60_000),
        _row("flaky", 10, 6, 5 * 60_000),
        _row("idle", 0, 0, 0),
    ]
    rates = active_model_rates(rows, now_ms=NOW)
    assert [r["model"] for r in rates] == ["flaky", "busy"]
    assert rates[0] == {"model": "flaky", "successRate": 60.0, "recentRequests": 10, "recentSuccessCount": 6}
    assert rate_health(rates[0]["successRate"]) == "degraded"


def test_rate_tiers():
    assert [rate_health(v) for v in (None, 100, 80, 79.9, 50, 49.9)] == [
        "unknown", "healthy", "healthy", "degraded", "degraded", "unhealthy",
    ]
