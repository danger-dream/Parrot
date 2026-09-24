"""Zhipu joins the existing weekly OAuth estimate; no separate quota formula."""
from datetime import datetime, timedelta, timezone

import pytest

from src import config, oauth_manager as om, state_db
from src.telegram import menu_cache
from src.telegram.menus import oauth_menu as menu
from src.tests.test_zhipu_provider import account_env, credential


@pytest.fixture
def projection(account_env):
    menu_cache.WINDOW_STATS.clear()
    yield
    menu_cache.WINDOW_STATS.clear()


def week_stats():
    return {"total": 1, "success_count": 1, "error_count": 0,
            "input": 400, "output": 200, "cache_creation": 100, "cache_read": 300,
            "cost_ticks": 15_000_000_000, "costed_success": 1, "unpriced_success": 0}


@pytest.mark.parametrize("mode,site", [("oauth", "bigmodel"), ("api_key", "zai")])
def test_zhipu_account_card_projects_only_matching_week(projection, mode, site):
    account = credential(mode, site, models=["GLM-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    reset = datetime.now(timezone.utc) + timedelta(days=5)
    state_db.quota_save(key, {"fetched_at": state_db.now_ms(), "five_hour_util": 50,
                            "seven_day_util": 25, "seven_day_reset": reset.isoformat(), "raw_data": "{}"})
    week = week_stats()
    menu_cache.WINDOW_STATS.store(menu._window_stats_cache_key(key, "7d"), week)
    menu_cache.WINDOW_STATS.store(menu._window_stats_cache_key(key, "5h"),
                                 dict(week, input=900_000, cost_ticks=1_000_000_000_000))
    month = dict(week, input=1_000_000, cost_ticks=120_000_000_000)
    before = dict(state_db.quota_load(key))
    card = menu._format_account_block(om.get_account(key),
                                     month_snapshot={"by_channel": {"oauth:" + key: month}})
    assert "💵 自然月 $12.00 · 周额度预测：4.0K · $6.00" in card
    assert menu._weekly_quota_projection(key) == {"tokens": 4000, "cost_text": "$6.00"}
    weekly = next(spec for spec in menu._oauth_window_specs([om.get_account(key)])
                  if spec[0] == menu._window_stats_cache_key(key, "7d"))
    assert weekly[2] == pytest.approx((reset - timedelta(days=7)).timestamp())
    assert state_db.quota_load(key) == before


@pytest.mark.parametrize("used", [None, 0, -1, 101, float("nan")])
def test_unknown_or_invalid_weekly_usage_does_not_invent_forecast(projection, used):
    account = credential()
    om.add_account(account)
    key = om.get_account_key(account)
    menu_cache.WINDOW_STATS.store(menu._window_stats_cache_key(key, "7d"), week_stats())
    assert menu._weekly_quota_projection(key, {"seven_day_util": used, "five_hour_util": 50}) is None


def test_missing_week_stats_and_unpriced_usage_follow_existing_policy(projection):
    account = credential()
    om.add_account(account)
    key = om.get_account_key(account)
    row = {"seven_day_util": 25}
    assert menu._weekly_quota_projection(key, row) is None
    menu_cache.WINDOW_STATS.store(menu._window_stats_cache_key(key, "7d"),
                                 dict(week_stats(), cost_ticks=0, costed_success=0))
    assert menu._weekly_quota_projection(key, row) == {"tokens": 4000, "cost_text": None}
    menu_cache.WINDOW_STATS.store(menu._window_stats_cache_key(key, "7d"), week_stats())
    config.update(lambda c: c.setdefault("pricing", {}).update(enabled=False))
    assert menu._weekly_quota_projection(key, row) == {"tokens": 4000, "cost_text": None}
