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


@pytest.mark.parametrize("failed", [None, "cedar_ember", "juniper_tide"])
def test_regular_refresh_fetches_each_missing_program_independently(env, monkeypatch, failed):
    calls = []
    def get(url, **kw):
        calls.append(url)
        program = "cedar_ember" if "?cedar_ember" in url else "juniper_tide" if "?at_wall" in url else None
        if program == failed and program:
            return httpx.Response(429, request=httpx.Request("GET", url))
        value = {program: copy.deepcopy(env.usage[program])} if program else {
            key: copy.deepcopy(value) for key, value in env.usage.items() if key not in cr.PROGRAMS}
        return httpx.Response(200, json=value, request=httpx.Request("GET", url))
    monkeypatch.setattr(cr.network, "get_sync", get)
    # Both manual-refresh staging and background snapshot call the same fetch.
    for staged in (False, True):
        calls.clear()
        result = env.control.refresh_usage_now(env.ctx, env.key, on_stage=(lambda *a: None) if staged else None)
        assert "error" not in result
        row = state_db.quota_load(env.key)
        raw = json.loads(row["raw_data"])
        assert raw["five_hour"] == env.usage["five_hour"]
        assert raw["extra_usage"] == env.usage["extra_usage"]
        assert row["extra_used"] == 12.34  # Existing cache uses dollars; raw spend stays 1234 cents.
        for program in cr.PROGRAMS:
            assert raw["claude_reset_queries"][program]["state"] == ("error" if program == failed else "known")
            if program != failed:
                assert raw[program] == env.usage[program]
        text = menu.cached_block(row)
        assert "状态未知" not in text
        assert ("查询失败" in text) == bool(failed)
        if failed:
            assert "HTTP 429" in text
        assert len(calls) == 3 and not env.posts


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
    assert "故障编号" in text and "5h重置：可用=True" in text
    assert "private-credential" not in text + capsys.readouterr().out
    assert not env.posts


def test_no_snapshot_says_not_queried_not_unknown():
    text = menu.cached_block(None)
    assert "尚未查询" in text and "状态未知" not in text
