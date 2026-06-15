from __future__ import annotations

from src.oauth.openai import normalize_wham_usage


def test_wham_weekly_shape_remains_5h_and_7d():
    usage = normalize_wham_usage({
        "plan_type": "team",
        "rate_limit": {
            "primary_window": {
                "used_percent": 1,
                "limit_window_seconds": 5 * 3600,
                "reset_after_seconds": 3600,
            },
            "secondary_window": {
                "used_percent": 4,
                "limit_window_seconds": 7 * 86400,
                "reset_after_seconds": 7 * 86400,
            },
        },
        "credits": {},
    })

    assert usage["five_hour"]["utilization"] == 1.0
    assert usage["seven_day"]["utilization"] == 4.0
    assert usage["openai"]["thirty_day"] == {}


def test_wham_single_monthly_window_is_exposed_as_30d_not_7d():
    usage = normalize_wham_usage({
        "plan_type": "team",
        "rate_limit": {
            "primary_window": {
                "used_percent": 8,
                "limit_window_seconds": 2628000,
                "reset_after_seconds": 2628000,
            },
        },
        "credits": {},
    })

    assert usage["five_hour"] == {}
    assert usage["seven_day"] == {}
    assert usage["openai"]["thirty_day"]["utilization"] == 8.0
    assert usage["openai"]["thirty_day"]["window_seconds"] == 2628000
