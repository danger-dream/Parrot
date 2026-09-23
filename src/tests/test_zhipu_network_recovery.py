"""Network failures must preserve login and distinguish unsent/create/copy outcomes."""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from src import config, network, oauth_manager as om, state_db
from src.oauth.zhipu import actions, auth, common, runtime
from src.oauth.zhipu.common import request as real_request
from src.telegram import ui, states
from src.telegram.menus import zhipu_oauth_menu as zh
from src.tests.test_workbuddy_lifecycle import context
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.test_zhipu_management import ctl, reset_env
from src.tests.test_zhipu_onboarding import onboarding
from src.tests.test_zhipu_callback_login import callback_env, callback


@pytest.mark.parametrize("phase,exception,unsent", [
    ("tls", httpx.ConnectTimeout, True), ("connect", httpx.ConnectError, True),
    ("write", httpx.WriteTimeout, False), ("read", httpx.ReadTimeout, False),
    ("pool", httpx.PoolTimeout, True),
])
def test_wire_failure_has_actual_route_and_submission_facts(monkeypatch, capsys, phase, exception, unsent):
    monkeypatch.setattr(common, "require_network", lambda: None)
    calls = []
    def handler(request):
        calls.append(request)
        trace = request.extensions["trace"]
        event = {"tls": "connection.start_tls.started", "connect": "connection.connect_tcp.started",
                 "write": "http11.send_request_headers.started", "read": "http11.receive_response_headers.started"}.get(phase)
        if phase == "read":
            trace("http11.send_request_headers.started", {})
        if event:
            trace(event, {})
        raise exception("secret-material-must-not-be-logged")
    def client(**kw):
        return network._install_sync_route_failover([
            ("misaka-lax", httpx.Client(transport=httpx.MockTransport(handler), timeout=kw["timeout"]))])
    monkeypatch.setattr(network, "sync_client", client)
    with pytest.raises(common.ZhipuError) as caught:
        real_request("https://bigmodel.cn/api/secret-path", method="POST", body={"token": "private"}, stage="key_create")
    exc = caught.value
    assert exc.request_not_sent is unsent and exc.proxy_route == "misaka-lax"
    assert exc.target_host == "bigmodel.cn" and exc.stage == "key_create"
    assert exc.network_phase == (phase if phase != "pool" else "connect")
    output = capsys.readouterr().out
    assert "secret-material" not in output and "secret-path" not in output and "private" not in output
    assert len(calls) == 1


def test_actual_proxy_fallback_is_observed_not_inferred(monkeypatch):
    monkeypatch.setattr(common, "require_network", lambda: None)
    def failed(request):
        raise httpx.ConnectError("unavailable")
    def failed_tls(request):
        request.extensions["trace"]("connection.start_tls.started", {})
        raise httpx.ConnectTimeout("TLS")
    def client(**kw):
        return network._install_sync_route_failover([
            ("us", httpx.Client(transport=httpx.MockTransport(failed))),
            ("direct", httpx.Client(transport=httpx.MockTransport(failed_tls)))])
    monkeypatch.setattr(network, "sync_client", client)
    with pytest.raises(common.ZhipuError) as caught:
        real_request("https://bigmodel.cn/api/test", method="POST", stage="key_create")
    assert caught.value.proxy_route == "direct" and caught.value.fallback_used
    assert caught.value.network_phase == "tls" and caught.value.request_not_sent


@pytest.mark.parametrize("not_sent,expected", [(True, "not_submitted"), (False, "unknown")])
def test_create_transport_result_controls_replay_boundary(reset_env, monkeypatch, not_sent, expected):
    control, key, state, _ = reset_env
    wire = common.request
    posts = []
    def request(url, **kw):
        if kw.get("method") == "POST":
            posts.append(url)
            raise common.ZhipuError("key_create", "timeout", timeout_phase="connect" if not_sent else "read",
                request_not_sent=not_sent, network_phase="tls" if not_sent else "read", target_host="bigmodel.cn", proxy_route="us")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == expected and result["request_not_sent"] is not_sent
    again = control.plan_zhipu_action(context(), key, "create_key")
    assert again["resume_only"] is (not not_sent)
    control.execute_zhipu_action_now(context(), key, again["plan_token"])
    assert len(posts) == (2 if not_sent else 1)


def test_created_key_copy_retry_uses_saved_id_not_another_post(reset_env, monkeypatch):
    control, key, state, _ = reset_env
    wire = common.request
    failed = [True]
    def request(url, **kw):
        if "/copy/" in url and failed[0]:
            raise common.ZhipuError("key_copy", "timeout", timeout_phase="connect", request_not_sent=True)
        if url.endswith("/api_keys") and kw.get("method", "GET") == "GET" and not failed[0]:
            pytest.fail("saved Key ID must resume copy directly")
        return wire(url, **kw)
    monkeypatch.setattr(common, "request", request)
    monkeypatch.setattr(control, "_post_save_zhipu_effects", lambda *a, **k: {"account_id": key})
    plan = control.plan_zhipu_action(context(), key, "create_key")
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == "key_pending" and "new-id" not in str(result)
    failed[0] = False
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert plan["resume_only"]
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert result["status"] == "succeeded" and om.get_account(key)["model_key"] == "new-id.new-secret"
    assert len([kw for _, kw in state["calls"] if kw.get("method") == "POST"]) == 1


def test_legacy_connect_failure_is_recoverable_but_read_failure_is_not():
    base = {"action": "create_key", "status": "unknown", "http_status": 0,
            "error_kind": "timeout", "error_stage": "request", "timeout_phase": "connect"}
    assert actions._known_unsent_creation(base)
    for patch in ({"timeout_phase": "read"}, {"action": "use"}, {"error_stage": "key_copy"}, {"key_id": "existing"}):
        assert not actions._known_unsent_creation(dict(base, **patch))


def test_fresh_login_initializes_in_shared_control_without_tg_repair(callback_env):
    control, observed, _ = callback_env
    from src.management_control.oauth.menu_bridge import telegram_context
    flow = control.start_zhipu_callback_login(telegram_context(42), site="bigmodel")
    poll = control.submit_zhipu_callback(telegram_context(42), flow.flow_id, flow.flow_secret, callback(flow))
    result = control._flows.zhipu.completed_result(telegram_context(42).actor.subject_id, flow.flow_id, flow.flow_secret)
    result.post_save["model_sync_future"].result(10)
    saved = om.get_account(poll.account_id)
    assert saved["project_id"] == "project" and saved["model_key"] == "existing-id.existing-secret"
    assert saved["zhipu_reset_status"]["data"]["available_week_resets"] == []
    assert result.post_save["usage"] and om.account_model_selection(saved)["effective_models"]
    assert sum("client/configs" in url for url, _ in observed) == 1
    assert sum("/api_keys/copy/" in url for url, _ in observed) == 1


def test_card_network_failure_does_not_discard_previous_snapshot_or_key(reset_env, monkeypatch):
    control, key, _, _ = reset_env
    control.get_zhipu(context(), key, reset_status=True)
    before = copy.deepcopy(om.get_account(key))
    def failed(*a, **k):raise common.ZhipuError("reset_status", "network", target_host="zcode.z.ai", proxy_route="us")
    monkeypatch.setattr(actions, "status", failed)
    with pytest.raises(common.ZhipuError):
        control.get_zhipu(context(), key, reset_status=True)
    saved = om.get_account(key)
    assert saved["zhipu_reset_status"]["data"] == before["zhipu_reset_status"]["data"]
    assert saved["zhipu_reset_status"]["error"]["proxy_route"] == "us"
    assert saved["model_key"] == before["model_key"] and saved["enabled"]


def test_card_cache_survives_unrelated_catalog_write(reset_env, monkeypatch):
    control, key, _, _ = reset_env
    original = actions.status
    def status(account, account_key):
        om.mutate_account_if_unchanged(key, om.get_account(key), lambda a:a.update(models=["GLM-5.3"]))
        return original(account, account_key)
    monkeypatch.setattr(actions, "status", status)
    control.get_zhipu(context(), key, reset_status=True)
    assert om.get_account(key)["zhipu_reset_status"]["data"]["available_five_hour_resets"]


def test_action_failure_http_schema_keeps_diagnostics_without_key_id():
    from src.management_api.schemas.oauth_zhipu import ZhipuActionData
    from src.management_control.oauth.contracts import public_value
    row={"action":"create_key","status":"key_pending","created_at":1,"key_id":"private-id",
         "error_kind":"timeout","error_stage":"key_copy","timeout_phase":"connect",
         "request_not_sent":True,"network_phase":"tls","target_host":"bigmodel.cn","proxy_route":"us","fallback_used":False}
    value=ZhipuActionData(**public_value(actions.public(row),camel_case_keys=True)).model_dump()
    assert value["networkPhase"]=="tls" and value["errorStage"]=="key_copy"
    assert "private-id" not in json.dumps(value)


def test_card_timestamps_and_history_are_human_readable(reset_env, monkeypatch):
    control,key,state,_=reset_env
    monkeypatch.setattr(zh,"control",control)
    messages=[]
    monkeypatch.setattr(ui,"edit",lambda chat,mid,text,**kw:messages.append((text,kw)))
    zh._reset_page(42,500,key)
    rendered=str(messages[-1])
    assert "到期 20" in rendered and str(state["calls"][0][1].get("account_key")) not in rendered
    assert "create_key" not in rendered
    states.pop_state(42)
