"""Card summaries and automatic grants share the existing OAuth UI/monitor."""
import copy
import time

import pytest
from src import oauth_manager as om, state_db
from src.oauth.zhipu import common
from src.telegram.menus import oauth_menu as menu
from src.tests.test_zhipu_provider import account_env, credential, quota, window
from src.tests.test_zhipu_management import ctl, reset_env


def test_oauth_card_summary_is_visible_in_list_and_detail(reset_env, monkeypatch):
    control, key, state, _ = reset_env
    from src.tests.test_workbuddy_lifecycle import context
    control.get_zhipu(context(), key, reset_status=True)
    monkeypatch.setattr(menu, "_account_period_stats", lambda *a, **kw: {})
    listed = menu._format_account_block(om.get_account(key))
    detail, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42, model_stats=[], stats_loading=False)
    assert "重置卡: 5小时 1 张 · 周窗 0 张" in listed
    list_lines = listed.splitlines()
    plan_index = next(i for i, line in enumerate(list_lines) if "🏷️ 套餐:" in line)
    assert list_lines[plan_index + 1] == "♻️ 重置卡: 5小时 1 张 · 周窗 0 张"
    assert listed.count("♻️ 重置卡:") == 1
    assert "重置卡: 5小时 1 张 · 周窗 0 张" in detail
    assert "♻️ 官方重置卡" in str(kb)
    # Expired entries stop counting without a network request at render time.
    current = copy.deepcopy(om.get_account(key))
    current["zhipu_reset_status"]["data"]["available_five_hour_resets"][0]["expire_at"] = 1
    om.mutate_account_if_unchanged(key, copy.deepcopy(om.get_account(key)),
        lambda a: a.update(zhipu_reset_status=current["zhipu_reset_status"]))
    assert "重置卡: 5小时 0 张 · 周窗 0 张" in menu._format_account_block(om.get_account(key))
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("failure", [None, "cards", "quota"])
async def test_monitor_refreshes_cards_and_quota_independently(reset_env, monkeypatch, failure):
    _, key, state, _ = reset_env
    wire = common.request
    def request(url, **kw):
        assert kw.get("method", "GET") == "GET" or url.endswith("/opportunity"), "monitor may grant but never create Keys or consume cards"
        if failure == "cards" and url.endswith("/status"):
            raise common.ZhipuError("reset_status", "network")
        if failure == "quota" and "/quota/limit" in url:
            raise common.ZhipuError("quota", "network")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    result = await om.quota_monitor_once()
    account = om.get_account(key)
    if failure == "cards":
        assert account["zhipu_reset_status"]["error"]["kind"] == "network"
    else:
        assert account["zhipu_reset_status"]["data"]["available_five_hour_resets"]
        assert account["zhipu_reset_status"]["fetched_at"] > (time.time()-10)*1000
    if failure == "quota":
        assert next(iter(result.values())).startswith("fetch_failed:")
    else:
        assert state_db.quota_load(key)
        assert next(iter(result.values())).startswith("ok:")


async def test_monitor_key_mode_never_queries_cards(account_env, monkeypatch):
    account = credential("api_key")
    om.add_account(account)
    calls = []
    def request(url, **kw):
        calls.append(url)
        assert "/quota/limit" in url
        return quota(window(0))
    monkeypatch.setattr(common, "request", request)
    result = await om.quota_monitor_once()
    assert len(calls) == 1 and next(iter(result.values())).startswith("ok:")


async def test_monitor_quota_pause_and_resume_keep_working_with_cards(reset_env):
    _, key, state, _ = reset_env
    state["quota"] = quota(window(100), window(100, week=True))
    await om.quota_monitor_once()
    assert om.get_account(key)["disabled_reason"] == "quota"
    state["quota"] = quota(window(0), window(0, week=True))
    result = await om.quota_monitor_once()
    assert next(iter(result.values())) == "resumed"
    assert om.get_account(key)["enabled"]
