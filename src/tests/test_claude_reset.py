"""Claude official resets: real persistence/control/TG, fake network only."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from src import config, oauth_manager as om, state_db
from src.oauth import claude_reset as cr
from src.management_control.oauth.control import OAuthControl
from src.management_control.oauth.menu_bridge import telegram_context
from src.management_control.errors import ManagementError
from src.management_auth.principal import Capability


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(cr.queries, "MIN_INTERVAL", 0)
    cr.queries._accounts.clear()
    state_db.init()
    config.update(lambda cfg: cfg.update(oauthAccounts=[], oauth={"mockMode": True},
                                         quotaMonitor={"disableThresholdPercent": 95}))
    entry = {"provider": "claude", "email": "reset@example.test", "access_token": "fake-at",
             "refresh_token": "fake-rt", "expired": "2999-01-01T00:00:00Z",
             "models": ["claude-fable-5.1", "claude-opus-5"],
             "claude_organization_uuid": "org-actual"}
    om.add_account(entry)
    key = "claude:reset@example.test"
    state_db.quota_delete(key)
    state_db.error_delete(f"oauth:{key}")
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(om.notifier, "notify_event", lambda *a, **k: None)
    usage = {
        "five_hour": {"utilization": 0, "resets_at": "2030-01-01T00:00:00Z"},
        "seven_day": {"utilization": 0, "resets_at": "2030-01-07T00:00:00Z"},
        "extra_usage": {"used_credits": 1234, "monthly_limit": 10000, "utilization": 12.34},
        "cedar_ember": {"eligible": True, "at_limit": True, "next_grant_id": "g_one",
            "weekly_resets_at": "2030-01-07T00:00:00Z", "grants": [{
                "id": "g_one", "label": "测试卡", "resets_left": 1, "resets_total": 1,
                "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z",
                "paused": False, "usable_now": True, "use_requires_limit": True,
                "clears": ["seven_day", "seven_day_oauth_apps"], "blocking": [],
            }]},
        "juniper_tide": {"eligible": True, "in_experiment": True, "arm": "reset",
            "available": True, "resets_per_week": 1, "weekly_resets_at": "2030-01-07T00:00:00Z"},
    }
    e = SimpleNamespace(key=key, entry=entry, usage=usage, gets=[], posts=[],
                        response={"result": "reset", "reason": None, "resets_left": 0,
                                  "cleared": ["seven_day", "seven_day_oauth_apps"],
                                  "weekly_resets_at": "2030-01-07T00:00:00Z"},
                        org="org-actual", post_effect=None, full_error=False)
    def get(url, **kwargs):
        e.gets.append((url, kwargs))
        if url == om.OAUTH_PROFILE_URL:
            data = {"account": {"uuid": "account-NOT-organization"}, "organization": {"uuid": e.org}}
        else:
            if e.full_error and "?" not in url:
                raise httpx.ReadTimeout("fake usage timeout")
            data = copy.deepcopy(e.usage)
            if "skip_spend=1" in url:
                data.pop("extra_usage", None)
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))
    def post(url, **kwargs):
        e.posts.append((url, kwargs))
        if e.post_effect:
            e.post_effect()
        return httpx.Response(200, json=e.response, request=httpx.Request("POST", url))
    monkeypatch.setattr(cr.network, "get_sync", get)
    monkeypatch.setattr(cr.network, "post_sync", post)
    e.control = OAuthControl()
    e.ctx = telegram_context(42)
    e.get = get
    return e


def final_plan(e, program="cedar_ember", grant_id=None):
    plan = e.control.plan_claude_reset(e.ctx, e.key, program, grant_id=grant_id)
    assert plan["available"], plan
    return e.control.confirm_claude_reset(e.ctx, e.key, plan["plan_token"])["plan_token"]


def execute(e, program="cedar_ember", grant_id=None):
    return e.control.execute_claude_reset(e.ctx, e.key, final_plan(e, program, grant_id=grant_id))


@pytest.mark.parametrize("program", cr.PROGRAMS)
def test_wire_confirmed_only_and_duplicate_capability(env, program):
    token = final_plan(env, program)
    assert not env.posts
    out = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert out["result"] == "reset"
    url, call = env.posts[0]
    assert url == "https://api.anthropic.com/api/organizations/org-actual/reset_rate_limits"
    body = call["json"]
    assert body["program"] == program
    if program == "juniper_tide":
        assert body == {"program": "juniper_tide"}
    else:
        assert set(body) == {"program", "grant_id", "request_id"}
        assert body["grant_id"] == "g_one"
        assert 1 <= len(body["request_id"]) <= 64
    assert call["timeout"] == 25
    expected_headers = [
        ("Accept", "application/json, text/plain, */*"),
        ("User-Agent", "claude-cli/2.1.321 (external, cli)"),
        ("Authorization", "Bearer fake-at"),
        ("anthropic-beta", "oauth-2025-04-20"),
        ("Content-Type", "application/json"),
        ("Accept-Encoding", "gzip, deflate, br"),
    ]
    assert list(call["headers"].items()) == expected_headers
    assert list(env.gets[0][1]["headers"].items()) == expected_headers
    wire_req = httpx.Request("POST", url, json=body, headers=call["headers"])
    if program == "juniper_tide":
        assert wire_req.content == b'{"program":"juniper_tide"}'
    else:
        assert wire_req.content == (
            f'{{"program":"cedar_ember","grant_id":"g_one","request_id":"{body["request_id"]}"}}'.encode("ascii")
        )
    raw_names = [k.decode("ascii").lower() for k, _ in wire_req.headers.raw]
    assert raw_names == [
        "host", "accept", "user-agent", "authorization",
        "anthropic-beta", "content-type", "accept-encoding", "content-length",
    ]
    with pytest.raises(ManagementError):
        env.control.execute_claude_reset(env.ctx, env.key, token)
    assert len(env.posts) == 1
    assert env.gets[-1][0] == om.OAUTH_USAGE_URL  # NOT skip_spend for recovery.
    assert out["cleared"] == ["seven_day", "seven_day_oauth_apps"]
    assert out["weekly_resets_at"] == "2030-01-07T00:00:00Z"


def test_status_preserves_spend_window_and_blocks(env):
    state_db.quota_save(env.key, om.flatten_usage(env.usage))
    before = state_db.quota_load(env.key)
    result = asyncio.run(cr.status(env.key))
    after = state_db.quota_load(env.key)
    assert result["cedar_ember"]["next_grant_id"] == "g_one"
    for key in ("extra_used", "extra_limit", "extra_util", "five_hour_util", "fetched_at"):
        assert after[key] == before[key]
    raw = json.loads(after["raw_data"])
    assert raw["extra_usage"]["used_credits"] == 1234
    assert raw["juniper_tide"]["available"] is True
    assert env.gets[0][0].endswith("?cedar_ember=1&skip_spend=1")
    assert env.gets[0][1]["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    ordinary = asyncio.run(om.fetch_usage(env.key))
    flat = om.flatten_usage(ordinary)
    assert json.loads(flat["raw_data"])["cedar_ember"]["grants"]
    assert flat["extra_used"] == 12.34
    assert env.gets[-1][0] == om.OAUTH_USAGE_URL


@pytest.mark.parametrize("change,reason", [
    ({"eligible": False, "ineligible_reason": "tier"}, "tier"),
    ({"next_grant_id": "wrong", "grants": []}, "no_grant"),
    ({"cooldown_until": "2030-01-01T00:00:00Z"}, "cooldown"),
    ({"at_limit": False}, "not_limited"),
])
def test_status_eligibility(env, change, reason):
    env.usage["cedar_ember"].update(change)
    result = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember")
    assert not result["available"] and result["reason"] == reason
    assert not env.posts


@pytest.mark.parametrize("change,reason", [
    ({"paused": True}, "paused"), ({"usable_now": False}, "unavailable"),
    ({"ends_at": "2020-01-01T00:00:00Z"}, "expired"),
    ({"resets_left": 0}, "already_used"),
    ({"blocking": ["seven_day"]}, "blocked_seven_day"),
    ({"blocking": ["five_hour"]}, "blocked_five_hour"),
    ({"gives_back_weekly": True, "blocking": ["seven_day"]}, "fable_no_weekly_usage"),
    ({"gives_back_weekly": True, "unavailable_reason": "blocked_seven_day"}, "fable_no_weekly_usage"),
    ({"unavailable_reason": "cannot_clear"}, "cannot_clear"),
])
def test_grant_rechecked_after_confirmation(env, change, reason):
    token = final_plan(env)
    env.usage["cedar_ember"]["grants"][0].update(change)
    result = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert result["reason"] == reason and not env.posts


def test_next_grant_change_never_silently_spends_new_card(env):
    # Case 1: confirmed g_one becomes used while new g_two becomes next_grant_id -> must reject, never spend g_two.
    token = final_plan(env)
    block = env.usage["cedar_ember"]
    block["grants"].append({**block["grants"][0], "id": "g_two"})
    block["grants"][0]["resets_left"] = 0
    block["next_grant_id"] = "g_two"
    out = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert out["result"] == "ineligible" and out["reason"] == "already_used" and not env.posts

    # Case 2: confirmed g_two (still usable) while next_grant_id points elsewhere -> spends g_two, never substitutes.
    block["grants"][0]["resets_left"] = 1
    block["next_grant_id"] = "g_one"
    token_two = final_plan(env, grant_id="g_two")
    out_two = env.control.execute_claude_reset(env.ctx, env.key, token_two)
    assert out_two["result"] == "reset"
    assert len(env.posts) == 1 and env.posts[0][1]["json"]["grant_id"] == "g_two"


@pytest.mark.parametrize("program,field,value", [
    ("juniper_tide", "available", False), ("juniper_tide", "arm", "control"),
    ("juniper_tide", "in_experiment", False), ("juniper_tide", "eligible", False),
])
def test_juniper_server_gates(env, program, field, value):
    env.usage[program][field] = value
    assert not env.control.plan_claude_reset(env.ctx, env.key, program)["available"]
    assert not env.posts


def test_org_is_profile_organization_not_account_uuid(env):
    om.delete_account(env.key)
    om.add_account({k: v for k, v in env.entry.items() if k != "claude_organization_uuid"})
    final_plan(env)
    assert om.get_account(env.key)["claude_organization_uuid"] == "org-actual"
    assert any(url == om.OAUTH_PROFILE_URL for url, _ in env.gets)
    assert om.extract_claude_plan_info({"account": {"uuid": "wrong"}, "organization": {"uuid": "right"}})["claude_organization_uuid"] == "right"


def test_no_org_no_confirm_no_post(env):
    om.delete_account(env.key)
    om.add_account({k: v for k, v in env.entry.items() if k != "claude_organization_uuid"})
    env.org = None
    result = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember")
    assert result["reason"] == "organization_missing"
    assert not env.posts


def test_management_permissions_and_two_stage_gate(env):
    ctx = replace(env.ctx, actor=replace(env.ctx.actor, capabilities=frozenset({Capability.READ})))
    with pytest.raises(ManagementError):
        env.control.plan_claude_reset(ctx, env.key, "cedar_ember")
    plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember")
    with pytest.raises(ManagementError):
        env.control.execute_claude_reset(env.ctx, env.key, plan["plan_token"])
    with pytest.raises(ManagementError):
        env.control.confirm_claude_reset(telegram_context(43), env.key, plan["plan_token"])
    assert not env.posts


def test_cedar_pending_explicit_retry_same_id_within_600s(env):
    def timeout():
        raise httpx.ReadTimeout("fake lost response")
    env.post_effect = timeout
    assert execute(env)["result"] == "unconfirmed"
    env.post_effect = None
    result = execute(env)
    assert result["result"] == "reset"
    assert len(env.posts) == 2
    assert env.posts[0][1]["json"]["request_id"] == env.posts[1][1]["json"]["request_id"]
    assert any("?cedar_ember=1" in url for url, _ in env.gets)


def test_pending_cedar_after_600s_cannot_assume_new_claim(env):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout("lost"))
    execute(env)
    expected = om.account_state_key(om.get_account(env.key))
    key = cr._journal_key(expected, "cedar_ember", "org-actual")
    record = state_db.claude_reset_operation_load(key)
    record["created_at"] -= 601
    state_db.claude_reset_operation_save(key, record)
    assert execute(env)["result"] == "unconfirmed"
    assert len(env.posts) == 1


def test_juniper_pending_never_auto_or_manually_replayed_same_week(env):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout("lost"))
    first = execute(env, "juniper_tide")
    assert first["result"] == "unconfirmed"
    cr._locks.clear()  # Restart-like: durable journal, not a process-local lock.
    second = execute(env, "juniper_tide")
    assert second["reason"] == "pending_requires_reconciliation"
    assert len(env.posts) == 1
    assert env.posts[0][1]["json"] == {"program": "juniper_tide"}


@pytest.mark.parametrize("result,reason", [("cooldown", "cooldown"), ("unavailable", "reset_unconfirmed"),
                                           ("ineligible", "expired"), ("not_limited", "not_limited")])
def test_server_reasons_are_preserved_and_no_force_enable(env, result, reason):
    om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    env.response = {"result": result, "reason": reason, "cooldown_until": "2030-01-01T00:00:00Z"}
    out = execute(env)
    assert out["reason"] == reason and out["cooldown_until"] == env.response["cooldown_until"]
    assert om.get_account(env.key)["disabled_reason"] == "quota"


@pytest.mark.parametrize("mode,expected", [("over", "still_over_quota"), ("failed", "refresh_failed_keep_disabled"),
                                           ("empty", "quota_unknown_keep_disabled"), ("fresh", "resumed")])
def test_success_requires_fresh_full_usage(env, mode, expected):
    om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    if mode == "over":
        env.usage["seven_day"]["utilization"] = 100
    elif mode == "failed":
        env.full_error = True
    elif mode == "empty":
        old = copy.deepcopy(env.usage)
        old["five_hour"]["utilization"] = 100
        state_db.quota_save(env.key, om.flatten_usage(old))
        env.usage.pop("five_hour")
    out = execute(env)
    assert out["quota_action"]["action"] == expected
    assert bool(om.get_account(env.key)["enabled"]) is (mode == "fresh")
    if mode == "empty":
        assert state_db.quota_load(env.key)["five_hour_util"] == 100


def test_delete_readd_before_confirmation_rejected(env):
    token = final_plan(env)
    om.delete_account(env.key)
    om.add_account(env.entry)
    with pytest.raises(ManagementError):
        env.control.execute_claude_reset(env.ctx, env.key, token)
    assert not env.posts


def test_delete_readd_in_flight_cannot_write_or_enable_replacement(env):
    def replace_account():
        om.delete_account(env.key)
        om.add_account(env.entry)
        om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    env.post_effect = replace_account
    out = execute(env)
    assert out["quota_action"]["action"] == "noop_stale"
    assert om.get_account(env.key)["disabled_reason"] == "quota"
    assert state_db.quota_load(env.key) is None


def test_new_limit_in_flight_not_overwritten(env):
    om.set_disabled_by_quota(env.key, "2030-01-01T00:00:00Z")
    env.post_effect = lambda: om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    out = execute(env)
    assert out["quota_action"]["action"] == "new_observation_keep_disabled"
    assert om.get_account(env.key)["disabled_reason"] == "quota"


def test_tg_actual_confirm_and_execute_chain(env, monkeypatch):
    from src.telegram.menus import oauth_menu as menu, claude_reset_menu as cm
    from src.telegram import ui
    monkeypatch.setattr(menu, "oauth_control", env.control)
    monkeypatch.setattr(cm, "control", env.control)
    edits, receipts, sent = [], [], []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(ui, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(menu, "_edit_cached_detail", lambda *a, **k: receipts.append((a, k)))
    short = ui.register_code(env.key)
    assert menu.handle_callback(42, 123, "ask", f"oa:claude_reset_ask:{short}")
    assert not env.posts
    assert "消耗周额度份额" in edits[-1][0][2] and "不改变每周自然重置日期" in edits[-1][0][2]
    def buttons():
        return [b for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    confirm = next(b["callback_data"] for b in buttons() if "周额度重置卡" in b["text"])
    menu.handle_callback(42, 123, "confirm", confirm)
    assert not env.posts and "最终确认" in edits[-1][0][2]
    final = next(b["callback_data"] for b in buttons() if "最终确认" in b["text"])
    menu.handle_callback(42, 123, "execute", final)
    assert len(env.posts) == 1
    assert "seven_day_oauth_apps" in receipts[-1][1]["prefix"]
    menu.handle_callback(42, 123, "duplicate", final)
    assert len(env.posts) == 1 and sent
    rendered = cm.cached_block(state_db.quota_load(env.key))
    assert "测试卡" in rendered and "5h重置" in rendered


def test_anytime_card_can_reset_without_wall(env):
    env.usage['cedar_ember']['at_limit'] = False
    env.usage['cedar_ember']['grants'][0]['use_requires_limit'] = False
    assert execute(env)['result'] == 'reset'


def test_pending_juniper_survives_real_state_reload(env):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout('lost'))
    execute(env, 'juniper_tide')
    assert state_db.get_store().close()
    state_db.init()
    cr._locks.clear()
    assert execute(env, 'juniper_tide')['reason'] == 'pending_requires_reconciliation'
    assert len(env.posts) == 1


def test_two_confirmed_operations_concurrent_only_dispatch_once(env):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    first, second = final_plan(env), final_plan(env)
    started, release = threading.Event(), threading.Event()
    def wait():
        started.set()
        assert release.wait(3)
    env.post_effect = wait
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(env.control.execute_claude_reset, env.ctx, env.key, first)
        assert started.wait(3)
        try:
            other = env.control.execute_claude_reset(env.ctx, env.key, second)
            assert other['result'] == 'pending'
        finally:
            release.set()
        assert pending.result()['result'] == 'reset'
    assert len(env.posts) == 1


def test_durable_intent_failure_cannot_dispatch(env, monkeypatch):
    def fail(*a, **k):
        raise OSError('fake storage failure')
    monkeypatch.setattr(state_db, 'claude_reset_operation_save', fail)
    with pytest.raises(OSError):
        execute(env)
    assert not env.posts


def test_new_model_restriction_in_flight_not_cleared(env):
    def new_error():
        state_db.error_save(f'oauth:{env.key}', 'claude-opus-5', 1, 2_000_000_000_000, 'new limit')
    env.post_effect = new_error
    out = execute(env)
    assert out['quota_action']['action'] == 'new_observation_keep_disabled'
    assert state_db.error_load(f'oauth:{env.key}', 'claude-opus-5')['last_error_message'] == 'new limit'


def test_fable_scope_remains_blocked_after_account_reset(env):
    from src import cooldown
    env.usage['limits'] = [{'kind': 'weekly_scoped', 'is_active': True,
        'scope': {'model': {'display_name': 'Fable'}}, 'utilization': 100,
        'resets_at': '2030-01-07T00:00:00Z'}]
    out = execute(env)
    assert om.get_account(env.key)['enabled'] is True
    assert out['quota_action']['any_over'] is True
    assert cooldown.is_blocked(f'oauth:{env.key}', 'claude-fable-5.1')
    assert not cooldown.is_blocked(f'oauth:{env.key}', 'claude-opus-5')


def test_already_used_is_not_a_recovery_receipt(env):
    om.set_disabled_by_quota(env.key, '2030-01-07T00:00:00Z')
    env.response['result'] = 'already_used'
    env.usage['seven_day']['utilization'] = 100
    out = execute(env)
    assert out['result'] == 'already_used'
    assert out['quota_action']['action'] == 'still_over_quota'
    assert not om.get_account(env.key)['enabled']


def test_new_passive_limit_during_post_is_not_overwritten(env):
    env.post_effect = lambda: state_db.quota_patch_passive(env.key, {'five_hour_util': 100})
    out = execute(env)
    assert out['quota_action']['action'] == 'new_observation_keep_disabled'
    assert state_db.quota_load(env.key)['five_hour_util'] == 100


@pytest.mark.parametrize('code,result', [(401, 'auth_error'), (403, 'auth_error'), (429, 'rate_limited'), (500, 'unconfirmed')])
def test_http_failure_only_retries_one_explicit_401(env, monkeypatch, code, result):
    refreshes = []
    async def refresh(*args, **kwargs):
        refreshes.append(True)
        return 'fresh-test-token'
    monkeypatch.setattr(om, 'force_refresh', refresh)
    def post(url, **kwargs):
        env.posts.append((url, kwargs))
        return httpx.Response(code, json={'reason': 'upstream_reason', 'cooldown_until': '2030-01-01T00:00:00Z'},
                              request=httpx.Request('POST', url))
    monkeypatch.setattr(cr.network, 'post_sync', post)
    out = execute(env, 'juniper_tide')
    assert out['result'] == result and out['reason'] == 'upstream_reason'
    assert len(env.posts) == (2 if code == 401 else 1)
    assert len(refreshes) == int(code == 401)
    if code == 401:
        assert env.posts[0][1]['json'] == env.posts[1][1]['json']


def test_legacy_generation_is_pinned_before_durable_effect(env):
    config.update(lambda cfg: cfg['oauthAccounts'][0].pop('generationId', None))
    execute(env)
    assert om.get_account(env.key)['generationId']


def test_cedar_pending_changed_allowance_reconciles_without_claiming_success(env):
    env.post_effect = lambda: (_ for _ in ()).throw(httpx.ReadTimeout('lost'))
    execute(env)
    token = final_plan(env)
    env.usage['cedar_ember']['grants'][0]['resets_left'] = 0
    out = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert out['reason'] == 'allowance_changed_not_attributed'
    assert out['result'] == 'unconfirmed' and len(env.posts) == 1


def test_tg_claude_detail_keeps_local_reset_and_adds_official_entry(env, monkeypatch):
    from src.telegram.menus import oauth_menu as menu
    om.set_disabled_by_quota(env.key, '2030-01-07T00:00:00Z')
    state_db.quota_save(env.key, om.flatten_usage(env.usage))
    monkeypatch.setattr(menu, 'oauth_control', env.control)
    text, markup = menu._detail_text_and_kb(env.key, refresh_quota=False, actor_chat_id=42)
    labels = [button['text'] for row in markup['inline_keyboard'] for button in row]
    assert any('清本地配额禁用' in label for label in labels)
    assert any('官方额度重置/状态' in label for label in labels)
    assert '测试卡' in text and '消耗周额度份额' in text


@pytest.mark.parametrize("grant,expected_kind", [
    ({"gives_back_weekly": True, "type": "weekly", "clears": ["seven_day_overage_included"]}, "fable"),
    ({"gives_back_weekly": False, "type": "session", "clears": ["five_hour", "seven_day"]}, "session"),
    ({"type": "weekly", "clears": ["five_hour"]}, "weekly"),
    ({"type": "full", "clears": ["seven_day"]}, "full"),
    ({"type": None, "clears": ["five_hour"]}, "session"),
    ({"clears": ["seven_day_overage_included"]}, "weekly"),
    ({"clears": ["five_hour", "seven_day"]}, "full"),
    ({"clears": []}, "full"),
])
def test_grant_kind_matches_321_ye_rules(grant, expected_kind):
    assert cr.grant_kind(grant) == expected_kind


def test_multi_card_selection_fable_fallback_and_explicit_grant_id(env):
    base_card = copy.deepcopy(env.usage["cedar_ember"]["grants"][0])
    weekly_blocked = {
        **base_card,
        "id": "g_weekly",
        "label": "普通周卡",
        "type": "weekly",
        "gives_back_weekly": False,
        "usable_now": False,
        "unavailable_reason": "cannot_clear",
        "clears": ["seven_day"],
    }
    fable_usable = {
        **base_card,
        "id": "g_fable",
        "label": "Fable 返还卡",
        "type": "weekly",
        "gives_back_weekly": True,
        "usable_now": True,
        "unavailable_reason": None,
        "clears": ["seven_day_overage_included"],
    }
    session_usable = {
        **base_card,
        "id": "g_session",
        "label": "5h 会话卡",
        "type": "session",
        "gives_back_weekly": False,
        "usable_now": True,
        "unavailable_reason": None,
        "clears": ["five_hour"],
    }
    env.usage["cedar_ember"].update(
        next_grant_id="g_weekly",
        exhausted=["seven_day_overage_included"],
        grants=[weekly_blocked, session_usable, fable_usable],
    )
    env.response["cleared"] = ["seven_day_overage_included"]

    # Default selection skips unavailable next_grant_id and prioritizes fable because seven_day_overage_included is exhausted.
    default_plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember")
    assert default_plan["available"] is True
    assert default_plan["grant_id"] == "g_fable"

    # Explicit selection of unavailable card returns its exact reason without creating a planToken.
    blocked_plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember", grant_id="g_weekly")
    assert blocked_plan["available"] is False
    assert blocked_plan["reason"] == "cannot_clear"
    assert blocked_plan.get("plan_token") is None

    # Unknown grant_id is rejected cleanly.
    unknown_plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember", grant_id="g_missing")
    assert unknown_plan["available"] is False
    assert unknown_plan["reason"] == "unknown_grant"

    # Explicit selection of session card hits 60s query cache (no extra GET) and binds one-shot plan strictly to g_session.
    gets_before = len(env.gets)
    session_plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember", grant_id="g_session")
    assert len(env.gets) == gets_before
    assert session_plan["available"] is True and session_plan["grant_id"] == "g_session"

    # Execute Fable plan -> redeems g_fable, never g_session or g_weekly.
    fable_final = env.control.confirm_claude_reset(env.ctx, env.key, default_plan["plan_token"])
    assert fable_final["grant_id"] == "g_fable"
    assert fable_final["grant_kind"] == "fable" and fable_final["gives_back_weekly"] is True
    out = env.control.execute_claude_reset(env.ctx, env.key, fable_final["plan_token"])
    assert out["result"] == "reset"
    assert len(env.posts) == 1 and env.posts[0][1]["json"]["grant_id"] == "g_fable"


def test_tg_multi_card_buttons_fable_reasons_and_confirm_copy(env, monkeypatch):
    from src.telegram.menus import oauth_menu as menu, claude_reset_menu as cm
    from src.telegram import ui
    monkeypatch.setattr(menu, "oauth_control", env.control)
    monkeypatch.setattr(cm, "control", env.control)
    edits, receipts = [], []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(menu, "_edit_cached_detail", lambda *a, **k: receipts.append((a, k)))

    base = copy.deepcopy(env.usage["cedar_ember"]["grants"][0])
    fable_blocked = {
        **base,
        "id": "g_fable_blocked",
        "label": "Fable一卡",
        "type": "weekly",
        "gives_back_weekly": True,
        "usable_now": False,
        "unavailable_reason": "blocked_seven_day",
        "clears": ["seven_day_overage_included"],
        "blocking": ["seven_day"],
    }
    fable_ready = {
        **base,
        "id": "g_fable_ready",
        "label": "Fable二卡",
        "type": "weekly",
        "gives_back_weekly": True,
        "usable_now": True,
        "unavailable_reason": None,
        "clears": ["seven_day_overage_included"],
        "blocking": [],
    }
    weekly_ready = {
        **base,
        "id": "g_weekly_ready",
        "label": "周卡",
        "type": "weekly",
        "gives_back_weekly": False,
        "usable_now": True,
        "unavailable_reason": None,
        "clears": ["seven_day", "seven_day_cowork", "seven_day_omelette"],
        "blocking": [],
    }
    env.usage["cedar_ember"].update(
        next_grant_id="g_fable_blocked",
        exhausted=["seven_day_overage_included"],
        grants=[fable_blocked, fable_ready, weekly_ready],
    )
    env.response["cleared"] = ["seven_day_overage_included"]

    short = ui.register_code(env.key)
    gets_before = len(env.gets)
    assert menu.handle_callback(42, 123, "ask", f"oa:claude_reset_ask:{short}")
    # Only 2 status GETs (cedar_ember + juniper_tide); extra per-card plan hits 60s cache!
    assert len(env.gets) == gets_before + 2
    ask_text = edits[-1][0][2]
    assert "Fable 重置卡 · 退回周额度" in ask_text
    assert "清 Fable 用量并退回 7 天周池" in ask_text
    assert "本周还没有可退回的 Fable 用量" in ask_text
    assert "Cowork 7 天" in ask_text and "Omelette 7 天" in ask_text

    buttons = [b for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    fable_btn = next(b for b in buttons if "Fable 重置卡·退回周额度（Fable二卡）" in b["text"])
    weekly_btn = next(b for b in buttons if "周额度重置卡（周卡）" in b["text"])
    assert not any("Fable一卡" in b["text"] for b in buttons)
    assert fable_btn["callback_data"] != weekly_btn["callback_data"]

    menu.handle_callback(42, 123, "confirm", fable_btn["callback_data"])
    confirm_text = edits[-1][0][2]
    assert "Fable 重置卡 · 退回周额度" in confirm_text
    assert "g_fable_ready" in confirm_text
    assert "退回 7 天周池" in confirm_text

    final_cb = next(
        b["callback_data"]
        for row in edits[-1][1]["reply_markup"]["inline_keyboard"]
        for b in row
        if "最终确认" in b["text"]
    )
    menu.handle_callback(42, 123, "execute", final_cb)
    assert len(env.posts) == 1
    assert env.posts[0][1]["json"]["grant_id"] == "g_fable_ready"
    assert "Fable / 周子限额 7 天" in receipts[-1][1]["prefix"]


def test_webui_plan_select_and_execute_wire_parity_with_real_321_grants(env):
    """End-to-end mocked simulation against real Pro two-card cedar_ember shape."""
    env.usage["cedar_ember"] = {
        "eligible": True,
        "ineligible_reason": None,
        "at_limit": False,
        "exhausted": [],
        "next_grant_id": "launch-2610-fable-week",
        "weekly_resets_at": "2030-11-17T12:00:00Z",
        "cooldown_until": None,
        "grants": [
            {
                "id": "launch-2610-fable-week",
                "label": "New model launch: one Fable reset for Pro and Team Standard, launch week only",
                "type": "full",
                "gives_back_weekly": True,
                "resets_total": 1,
                "resets_left": 1,
                "starts_at": "2020-11-09T18:00:00Z",
                "ends_at": "2030-11-17T07:59:00Z",
                "clears": ["five_hour", "seven_day_overage_included"],
                "paused": False,
                "usable_now": True,
                "unavailable_reason": None,
                "use_requires_limit": False,
                "percent_used": {"five_hour": 0, "seven_day_overage_included": 0},
                "blocking": [],
            },
            {
                "id": "full-reset-2611",
                "label": "One full limit reset (November 2026)",
                "type": "full",
                "gives_back_weekly": False,
                "resets_total": 1,
                "resets_left": 1,
                "starts_at": "2020-11-10T21:30:00Z",
                "ends_at": "2030-12-10T22:00:00Z",
                "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
                "paused": False,
                "usable_now": True,
                "unavailable_reason": None,
                "use_requires_limit": False,
                "percent_used": {"five_hour": 0, "seven_day": 14, "seven_day_overage_included": 0},
                "blocking": [],
            },
        ],
    }
    p_default = env.control.plan_claude_reset_action(env.ctx, env.key, "cedar_ember")
    assert p_default["available"] is True
    assert p_default["grant_id"] == "launch-2610-fable-week"
    assert len(env.gets) == 1 and not env.posts

    # User clicks the second card in WebUI modal -> hits 60s cache, zero extra GETs
    p_second = env.control.plan_claude_reset_action(env.ctx, env.key, "cedar_ember", grant_id="full-reset-2611")
    assert p_second["available"] is True
    assert p_second["grant_id"] == "full-reset-2611"
    assert len(env.gets) == 1 and not env.posts

    # Mock execute: verifies fresh status GET + exact POST headers/body + post-reset plain usage GET
    env.response["cleared"] = ["five_hour", "seven_day", "seven_day_overage_included"]
    out = env.control.execute_claude_reset_action(env.ctx, env.key, p_second["plan_token"])
    assert out["result"] == "reset"
    assert out["grant_id"] == "full-reset-2611"
    assert len(env.posts) == 1
    url, post_call = env.posts[0]
    assert url == "https://api.anthropic.com/api/organizations/org-actual/reset_rate_limits"
    assert post_call["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert post_call["headers"]["User-Agent"] == "claude-cli/2.1.321 (external, cli)"
    assert list(post_call["json"]) == ["program", "grant_id", "request_id"]
    assert post_call["json"]["grant_id"] == "full-reset-2611"


def test_background_token_refresh_quota_or_model_sync_does_not_invalidate_plan(env):
    plan = env.control.plan_claude_reset(env.ctx, env.key, "cedar_ember")
    assert plan["available"] is True

    # Simulate background token refresh, model sync, and quota monitor updates before confirm/execute.
    om._save_token_fields(env.key, {
        "access_token": "rotated-background-at",
        "refresh_token": "rotated-background-rt",
        "expired": "2999-12-31T00:00:00Z",
        "last_refresh": "2026-11-12T06:00:00Z",
    })
    om.set_disabled_by_quota(env.key, "2030-01-07T00:00:00Z")
    config.update(lambda cfg: cfg["oauthAccounts"][0].update(
        last_model_sync="2026-11-12T06:00:05Z",
        models=["claude-fable-5-2", "claude-opus-5-5"],
    ))

    confirmed = env.control.confirm_claude_reset(env.ctx, env.key, plan["plan_token"])
    out = env.control.execute_claude_reset(env.ctx, env.key, confirmed["plan_token"])
    assert out["result"] == "reset"
    assert len(env.posts) == 1
    assert env.posts[0][1]["headers"]["Authorization"] == "Bearer rotated-background-at"


def test_execute_precheck_transport_timeout_falls_back_to_fresh_cache(env, monkeypatch):
    token = final_plan(env, "cedar_ember")
    orig_get = env.get
    status_calls = []

    def flaky_get(url, **kwargs):
        if "cedar_ember=1" in url:
            status_calls.append(url)
            raise httpx.ReadTimeout("transient proxy timeout on precheck GET")
        return orig_get(url, **kwargs)

    monkeypatch.setattr(cr.network, "get_sync", flaky_get)
    out = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert len(status_calls) == 1
    assert out["result"] == "reset"
    assert len(env.posts) == 1


def test_tg_execute_cooldown_reports_explicit_non_consumed_warning(env, monkeypatch):
    from src.telegram.menus import oauth_menu as menu, claude_reset_menu as cm
    from src.telegram import ui
    monkeypatch.setattr(menu, "oauth_control", env.control)
    monkeypatch.setattr(cm, "control", env.control)
    edits, receipts, sent = [], [], []
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(ui, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(menu, "_edit_cached_detail", lambda *a, **k: receipts.append((a, k)))

    # Case 1: Fresh (<60s) panel cache exists when /api/oauth/usage hits 429 -> falls back to panel cache and succeeds!
    short = ui.register_code(env.key)
    assert menu.handle_callback(42, 123, "ask", f"oa:claude_reset_ask:{short}")
    buttons = [b for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    confirm_cb = next(b["callback_data"] for b in buttons if "周额度重置卡" in b["text"])
    menu.handle_callback(42, 123, "confirm", confirm_cb)
    final_cb = next(
        b["callback_data"]
        for row in edits[-1][1]["reply_markup"]["inline_keyboard"]
        for b in row
        if "最终确认" in b["text"]
    )
    monkeypatch.setattr(
        cr.network, "get_sync",
        lambda url, **kw: httpx.Response(429, headers={"Retry-After": "60"}, request=httpx.Request("GET", url)),
    )
    menu.handle_callback(42, 123, "execute", final_cb)
    assert len(env.posts) == 1
    assert not sent

    # Case 2: No fresh cache available and account is in CoolingDown -> sends explicit non-consumed cooldown message with formatted timestamp
    monkeypatch.setattr(cr.network, "get_sync", env.get)
    cr.queries._accounts.clear()
    assert menu.handle_callback(42, 123, "ask", f"oa:claude_reset_ask:{short}")
    buttons = [b for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    confirm_cb = next(b["callback_data"] for b in buttons if "周额度重置卡" in b["text"])
    menu.handle_callback(42, 123, "confirm", confirm_cb)
    final_cb = next(
        b["callback_data"]
        for row in edits[-1][1]["reply_markup"]["inline_keyboard"]
        for b in row
        if "最终确认" in b["text"]
    )
    state_key = om.account_state_key(om.get_account(env.key))
    cr.queries.invalidate(state_key)
    monkeypatch.setattr(
        cr.network, "get_sync",
        lambda url, **kw: httpx.Response(429, headers={"Retry-After": "60"}, request=httpx.Request("GET", url)),
    )
    # Trigger CoolingDown state and then execute without cache
    env.control.refresh_claude_reset_status(env.ctx, env.key, "juniper_tide")
    menu.handle_callback(42, 123, "execute", final_cb)
    assert len(env.posts) == 1
    assert sent and "限流冷却中（冷却至 20" in sent[-1][1] and "未向官方发送重置消耗请求" in sent[-1][1]


def test_plain_usage_with_null_reset_keys_never_wipes_persisted_cards_and_redeem_decrements_db(env, monkeypatch):
    # Step 1: Query reset status so cards are persisted in state_db
    plan = env.control.plan_claude_reset_action(env.ctx, env.key, "cedar_ember")
    assert plan["available"] is True
    raw_after_plan = json.loads(state_db.quota_load(env.key)["raw_data"])
    assert isinstance(raw_after_plan.get("cedar_ember"), dict)
    assert raw_after_plan["cedar_ember"]["grants"][0]["resets_left"] == 1

    # Step 2: Simulate real Anthropic plain /api/oauth/usage returning `"cedar_ember": None, "juniper_tide": None`
    # after in-memory query cache is cleared (e.g. restart or token rotation).
    cr.queries._accounts.clear()
    orig_get = env.get

    def real_plain_usage_get(url, **kwargs):
        resp = orig_get(url, **kwargs)
        if "?" not in url:
            body = resp.json()
            body["cedar_ember"] = None
            body["juniper_tide"] = None
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        return resp

    monkeypatch.setattr(cr.network, "get_sync", real_plain_usage_get)
    usage = asyncio.run(om.fetch_usage(env.key))
    state_db.quota_save(env.key, om.flatten_usage(usage))

    # Cards must remain intact in both returned usage and state_db!
    assert isinstance(usage.get("cedar_ember"), dict)
    raw_after_poll = json.loads(state_db.quota_load(env.key)["raw_data"])
    assert isinstance(raw_after_poll.get("cedar_ember"), dict)
    assert raw_after_poll["cedar_ember"]["grants"][0]["resets_left"] == 1

    # Step 3: Redeem the card -> state_db must immediately reflect resets_left=0 and usable_now=False
    out = env.control.execute_claude_reset_action(env.ctx, env.key, plan["plan_token"])
    assert out["result"] == "reset"
    raw_after_redeem = json.loads(state_db.quota_load(env.key)["raw_data"])
    assert raw_after_redeem["cedar_ember"]["grants"][0]["resets_left"] == 0
    assert raw_after_redeem["cedar_ember"]["grants"][0]["usable_now"] is False



