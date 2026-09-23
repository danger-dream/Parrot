"""Automatic onboarding/claim policy, with durable retries and manual consumption."""
import copy
import time
from types import SimpleNamespace

import pytest
from src import oauth_manager as om, notifier
from src.oauth.zhipu import actions, common
from src.management_control.oauth.menu_bridge import telegram_context
from src.tests.test_zhipu_provider import account_env
from src.tests.test_zhipu_management import ctl, reset_env
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_callback_login import callback_env, callback


@pytest.mark.parametrize("unsent", [True, False])
def test_auto_setup_retries_unsent_but_only_reconciles_uncertain_create(callback_env, monkeypatch, unsent):
    control, _, _ = callback_env
    wire = common.request
    posts = []
    failed = [True]
    def request(url, **kw):
        if url.endswith("/api_keys"):
            if kw.get("method", "GET") == "POST":
                posts.append(url)
                if failed[0]:
                    raise common.ZhipuError("key_create", "timeout", request_not_sent=unsent,
                        timeout_phase="connect" if unsent else "read")
                return {"apiKey": "created-id", "name": "zcode-api-key"}
            return []
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    ctx = telegram_context(42)
    flow = control.start_zhipu_callback_login(ctx, site="bigmodel")
    poll = control.submit_zhipu_callback(ctx, flow.flow_id, flow.flow_secret, callback(flow))
    saved = control._flows.zhipu.completed_result(ctx.actor.subject_id, flow.flow_id, flow.flow_secret)
    assert saved.post_save["key_action"]["status"] == ("not_submitted" if unsent else "unknown")
    assert not om.get_account(poll.account_id).get("model_key")
    failed[0] = False
    resumed = control.initialize_zhipu_account(ctx, poll.account_id)
    if unsent:
        resumed.post_save["model_sync_future"].result(10)
        assert len(posts) == 2 and om.get_account(poll.account_id)["model_key"] == "created-id.existing-secret"
    else:
        assert len(posts) == 1 and not om.get_account(poll.account_id).get("model_key")
        assert resumed.post_save["key_action"]["status"] == "unknown"


def test_automatic_claim_notifies_once_and_cooldown_prevents_post(reset_env, monkeypatch):
    _, key, state, _ = reset_env
    notifications = []
    monkeypatch.setattr(notifier, "notify", lambda text, **kw: notifications.append(text))
    snapshot = actions.status(om.get_account(key), key)
    result = actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert result["status"] == "succeeded"
    actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    posts = [(url, kw) for url, kw in state["calls"] if kw.get("method") == "POST"]
    assert len(posts) == 1 and posts[0][0].endswith("/opportunity")
    assert len(notifications) == 1 and "未自动使用" in notifications[0]
    assert om.get_account(key)["zhipu_reset_status"]["data"]


def test_automatic_claim_honors_server_next_attempt(reset_env, monkeypatch):
    _, key, state, _ = reset_env
    now = [time.time()]
    monkeypatch.setattr(actions, "time", SimpleNamespace(time=lambda:now[0], sleep=lambda _:None))
    wire = common.request
    posts = []
    next_try_at = (now[0]+3600)*1000
    def request(url, **kw):
        if url.endswith("/opportunity"):
            posts.append(kw)
            return {"code":3301,"data":{"granted":False,"next_try_at":next_try_at}}
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    snapshot = actions.status(om.get_account(key), key)
    result = actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert result["next_try_at"] == next_try_at
    now[0] += 601
    actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert len(posts) == 1
    now[0] += 3600
    actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert len(posts) == 2


def test_uncertain_claim_reuses_same_idempotency_key_after_cooldown(reset_env, monkeypatch):
    _, key, state, _ = reset_env
    now = [time.time()]
    monkeypatch.setattr(actions, "time", SimpleNamespace(time=lambda:now[0], sleep=lambda _:None))
    wire = common.request
    keys = []
    def request(url, **kw):
        if url.endswith("/opportunity"):
            keys.append(kw["body"]["idempotency_key"])
            raise common.ZhipuError("reset_opportunity", "timeout", timeout_phase="read")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    snapshot = actions.status(om.get_account(key), key)
    first = actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert first["status"] == "unknown"
    now[0] += 601
    actions.auto_claim(key, copy.deepcopy(om.get_account(key)), snapshot)
    assert len(keys) == 2 and keys[0] == keys[1]


def test_both_cards_or_paused_accounts_do_not_auto_claim(reset_env):
    _, key, state, _ = reset_env
    account = copy.deepcopy(om.get_account(key))
    both = {"available_five_hour_resets":[{"expire_at":1e14}], "available_week_resets":[{"expire_at":1e14}]}
    assert actions.auto_claim(key, account, both) is None
    account.update(enabled=False, disabled_reason="user")
    assert actions.auto_claim(key, account, {}) is None
    account.update(credential_mode="api_key", enabled=True, disabled_reason=None)
    assert actions.auto_claim(key, account, {}) is None
    assert not state["calls"]
