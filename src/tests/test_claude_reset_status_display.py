"""Regular quota reads fill opt-in reset blocks without losing quota/spend."""
import copy
import json

import httpx
import pytest
from src import oauth_manager as om, state_db
from src.oauth import claude_reset as cr
from src.telegram import ui
from src.telegram.menus import claude_reset_menu as menu
from src.tests.test_claude_reset import env


def test_regular_refresh_does_not_fan_out_and_reuses_explicit_status(env, monkeypatch):
    calls = []
    def get(url, **kw):
        calls.append(url)
        program = "cedar_ember" if "?cedar_ember" in url else "juniper_tide" if "?at_wall" in url else None
        value = {program: copy.deepcopy(env.usage[program])} if program else {
            key: copy.deepcopy(value) for key, value in env.usage.items() if key not in cr.PROGRAMS}
        return httpx.Response(200, json=value, request=httpx.Request("GET", url))
    monkeypatch.setattr(cr.network, "get_sync", get)
    for staged in (False, True):
        result = env.control.refresh_usage_now(env.ctx, env.key, on_stage=(lambda *a: None) if staged else None)
        assert "error" not in result
        raw = json.loads(state_db.quota_load(env.key)["raw_data"])
        assert raw["five_hour"] == env.usage["five_hour"]
        assert raw["extra_usage"] == env.usage["extra_usage"]
        assert "cedar_ember" not in raw
    assert len(calls) == 1
    env.control.refresh_claude_reset_status(env.ctx, env.key)
    env.control.refresh_usage_now(env.ctx, env.key)
    raw = json.loads(state_db.quota_load(env.key)["raw_data"])
    assert raw["cedar_ember"] == env.usage["cedar_ember"]
    assert raw["claude_reset_queries"]["cedar_ember"]["state"] == "cached"
    assert len(calls) == 2 and not env.posts


@pytest.mark.parametrize("value,state,word", [({}, "not_provided", "官方未返回"),
    ({"cedar_ember": None}, "not_provided", "官方未返回"),
    ({"cedar_ember": []}, "invalid_response", "格式无法识别")])
def test_missing_or_invalid_status_is_not_invented_zero_cards(env, monkeypatch, value, state, word):
    def get(url, **kw):
        return httpx.Response(200, json=value, request=httpx.Request("GET", url))
    monkeypatch.setattr(cr.network, "get_sync", get)
    result = env.control.refresh_claude_reset_status(env.ctx, env.key)
    assert result["claude_reset_queries"]["cedar_ember"]["state"] == state
    assert cr.eligibility(result, "cedar_ember") == "status_unknown"
    text = menu.cached_block(state_db.quota_load(env.key))
    assert word in text and "0 张 ·" not in text
    assert not env.posts


def test_explicit_query_one_failure_still_displays_other_program_and_incident(env, monkeypatch, capsys):
    edits = []
    monkeypatch.setattr(menu, "control", env.control)
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: edits.append((a, k)))
    original = env.get
    def get(url, **kw):
        if "?cedar_ember" in url:
            raise httpx.ReadTimeout("private-credential-in-raw-exception")
        response = original(url, **kw)
        data = response.json()
        data.pop("cedar_ember", None)
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))
    monkeypatch.setattr(cr.network, "get_sync", get)
    menu.ask(42, 2, "cb", ui.register_code(env.key))
    text = edits[-1][0][2]
    assert "查询失败" in text and "5h重置：可用=True" in text
    assert "private-credential" not in text + capsys.readouterr().out
    assert not env.posts


def test_no_snapshot_says_not_queried_not_unknown():
    text = menu.cached_block(None)
    assert "尚未查询" in text and "状态未知" not in text


def test_v321_card_kinds_windows_and_reasons_displayed_in_chinese():
    usage = {
        "cedar_ember": {
            "eligible": True,
            "at_limit": True,
            "exhausted": ["seven_day_overage_included", "seven_day_cowork", "seven_day_omelette"],
            "next_grant_id": "g_fable",
            "weekly_resets_at": "2030-01-07T00:00:00Z",
            "cooldown_until": None,
            "grants": [
                {
                    "id": "g_fable",
                    "label": "Fable赠卡",
                    "type": "weekly",
                    "gives_back_weekly": True,
                    "resets_left": 1,
                    "resets_total": 1,
                    "starts_at": "2020-01-01T00:00:00Z",
                    "ends_at": "2030-01-01T00:00:00Z",
                    "paused": False,
                    "usable_now": False,
                    "unavailable_reason": "blocked_seven_day",
                    "use_requires_limit": True,
                    "clears": ["seven_day_overage_included"],
                    "blocking": ["seven_day"],
                },
                {
                    "id": "g_session",
                    "label": "会话卡",
                    "type": "session",
                    "gives_back_weekly": False,
                    "resets_left": 1,
                    "resets_total": 1,
                    "starts_at": "2020-01-01T00:00:00Z",
                    "ends_at": "2030-01-01T00:00:00Z",
                    "paused": False,
                    "usable_now": False,
                    "unavailable_reason": "blocked_five_hour",
                    "use_requires_limit": True,
                    "clears": ["five_hour"],
                    "blocking": ["five_hour"],
                },
                {
                    "id": "g_full",
                    "label": "全量卡",
                    "type": "full",
                    "gives_back_weekly": False,
                    "resets_left": 1,
                    "resets_total": 1,
                    "starts_at": "2020-01-01T00:00:00Z",
                    "ends_at": "2030-01-01T00:00:00Z",
                    "paused": False,
                    "usable_now": True,
                    "unavailable_reason": None,
                    "use_requires_limit": True,
                    "clears": ["five_hour", "seven_day"],
                    "blocking": [],
                },
            ],
        },
    }
    text = menu.block(usage)
    assert "Fable / 周子限额 7 天" in text
    assert "Cowork 7 天" in text
    assert "Omelette 7 天" in text
    assert "Fable 重置卡 · 退回周额度" in text
    assert "清 Fable 用量并退回 7 天周池" in text
    assert "本周还没有可退回的 Fable 用量" in text
    assert "5 小时卡" in text
    assert "当前正受 5 小时限额阻止，需等其恢复后才能用" in text
    assert "全量重置卡" in text


def test_quota_save_preserves_reset_blocks_and_populates_card_and_detail_sections(env):
    from src.management_control.oauth import usage_sections

    state_db.quota_save(env.key, om.flatten_usage({
        "five_hour": {"utilization": 10.0, "resets_at": "2030-01-01T05:00:00Z"},
        "seven_day": {"utilization": 14.0, "resets_at": "2030-01-07T00:00:00Z"},
    }))
    expected = om.account_state_key(om.get_account(env.key))
    state_db.quota_patch_claude_reset_status(env.key, {
        "cedar_ember": {
            "eligible": True,
            "at_limit": False,
            "grants": [
                {"id": "launch-2610-fable-week", "type": "full", "gives_back_weekly": True, "resets_left": 1, "resets_total": 1, "paused": False},
                {"id": "full-reset-2611", "type": "full", "gives_back_weekly": False, "resets_left": 1, "resets_total": 1, "paused": False},
            ],
            "weekly_resets_at": "2030-01-07T00:00:00Z",
        },
        "juniper_tide": {"eligible": False, "available": False, "ineligible_reason": "not_at_wall"},
        "claude_reset_queries": {"cedar_ember": {"state": "known", "fetched_at": 1000}},
    }, expected_state_key=expected)

    # Subsequent ordinary /api/oauth/usage refresh (after 60s memory cache expiry)
    # must NOT wipe out persisted cedar_ember / juniper_tide in state_db.
    state_db.quota_save(env.key, om.flatten_usage({
        "five_hour": {"utilization": 25.0, "resets_at": "2030-01-01T05:00:00Z"},
        "seven_day": {"utilization": 20.0, "resets_at": "2030-01-07T00:00:00Z"},
    }))
    row = state_db.quota_load(env.key)
    raw = json.loads(row["raw_data"])
    assert row["five_hour_util"] == 25.0
    assert len(raw["cedar_ember"]["grants"]) == 2
    assert raw["juniper_tide"]["ineligible_reason"] == "not_at_wall"

    plan_facts = env.control._plan_facts("claude", om.get_account(env.key), row)
    assert plan_facts["reset_credit_count"] == 2
    sec = usage_sections._claude_reset(raw)
    assert sec is not None
    assert sec["items"][0]["remaining"] == 2
    assert sec["items"][0]["note"] == "Fable 卡 ×1 · 全量卡 ×1"
    assert sec["items"][0]["resets_at"] is None
    assert sec["items"][1]["note"] == "not_at_wall"
