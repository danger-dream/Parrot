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


def final_plan(e, program="cedar_ember"):
    plan = e.control.plan_claude_reset(e.ctx, e.key, program)
    assert plan["available"], plan
    return e.control.confirm_claude_reset(e.ctx, e.key, plan["plan_token"])["plan_token"]


def execute(e, program="cedar_ember"):
    return e.control.execute_claude_reset(e.ctx, e.key, final_plan(e, program))


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
    assert call["headers"]["User-Agent"] == "claude-cli/2.1.282 (external, sdk-cli)"
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
    ({"next_grant_id": "wrong"}, "unknown_grant"),
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
    ({"resets_left": 0}, "already_used"), ({"blocking": ["seven_day"]}, "blocking"),
])
def test_grant_rechecked_after_confirmation(env, change, reason):
    token = final_plan(env)
    env.usage["cedar_ember"]["grants"][0].update(change)
    result = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert result["reason"] == reason and not env.posts


def test_next_grant_change_never_silently_spends_new_card(env):
    token = final_plan(env)
    block = env.usage["cedar_ember"]
    block["grants"].append({**block["grants"][0], "id": "g_two"})
    block["next_grant_id"] = "g_two"
    out = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert out["reason"] == "not_next_grant" and not env.posts


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
    assert env.gets[0][0] == om.OAUTH_PROFILE_URL
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
