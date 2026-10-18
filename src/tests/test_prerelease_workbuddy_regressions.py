"""WorkBuddy policy, production owner wiring and real threaded TG query flow."""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import FastAPI
import server
from src import config
from src.telegram import bot, ui
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_management_server_composition import settings
from src.tests.test_workbuddy_management_api import api
from src.tests.test_workbuddy_telegram import tg
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account

_NATIVE_QUERY_WORKER = wb._start_query_worker


def test_policy_describes_real_login_regions_and_accepted_profiles(api, monkeypatch):
    call, _, ledger, ctl, *_ = api
    response = call("GET", "/oauth/workbuddy/settings")
    assert response.status_code == 200, response.text
    policy = response.json()["data"]
    assert policy["browserLoginRealms"] == ["cn", "global"]
    assert policy["importRealms"] == []
    assert policy["clientProfile"] == "cli"
    assert policy["clientProfilesByRealm"] == {"cn": "cli", "global": "workbuddy-cli"}
    assert policy["supportedClientProfilesByRealm"]["global"] == ["workbuddy-cli", "ide"]
    calls = []
    def start(*, realm="cn", client_profile="cli"):
        calls.append((realm, client_profile))
        return {"realm": realm, "workbuddy_client_profile": client_profile, "status": "pending", "auth_url": "https://login.invalid", "state": "fixture"}
    monkeypatch.setattr(ctl.backend, "workbuddy_start_login", start)
    for realm in policy["browserLoginRealms"]:
        response = call("POST", "/oauth/login-flows", {"provider": "workbuddy", "realm": realm,
                        "clientProfile": policy["clientProfilesByRealm"][realm]})
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert call("POST", "/oauth/login-flows/" + data["flowId"] + "/cancel",
                    {"flowSecret": data["flowSecret"]}).status_code == 204
    assert calls == [("cn", "cli"), ("global", "workbuddy-cli")]
    assert ledger["calls"] == 0
    assert not policy["autoCheckinDefault"] and not policy["autoTrial"]


def test_workbuddy_adapter_binds_and_unbinds_the_runtime_oauth_owner(tmp_path, monkeypatch):
    holder = FastAPI()
    fallback = wb.oauth_control
    monkeypatch.setattr(server, "_telegram_control_defaults", None)
    monkeypatch.setattr(server.config, "management_settings", lambda: settings(tmp_path))
    runtime = server._initialize_management_runtime(holder)
    try:
        owner = runtime.control_owner().oauth
        assert wb.oauth_control is owner
        assert bot.oauth_menu.oauth_control is owner
        assert owner is not fallback
    finally:
        asyncio.run(server._close_management_runtime(holder))
    assert wb.oauth_control is fallback


@pytest.fixture
def native_queries(tg, monkeypatch):
    workers, errors = [], []
    def start(worker):
        def checked():
            try:
                worker()
            except BaseException as exc:
                errors.append(exc)
        thread = _NATIVE_QUERY_WORKER(checked)
        workers.append(thread)
        return thread
    monkeypatch.setattr(wb, "_start_query_worker", start)
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    yield workers
    for thread in workers:
        thread.join(3)
        assert not thread.is_alive(), "query outlived its isolated test state"
    assert not errors


def update(data, ident="cb"):
    return {"callback_query": {"id": ident, "data": data,
            "message": {"chat": {"id": 42}, "message_id": 900}}}


def query_callback(key, entrypoint):
    short = ui.register_code(key)
    return (f"oa:refresh_usage:{short}:1" if entrypoint == "detail" else
            wb._cb("refresh_activity" if entrypoint == "activity" else "refresh_credits", (short, 1, "all")))


@pytest.mark.parametrize("entrypoint", ["activity", "detail", "legacy_credits"])
@pytest.mark.parametrize("failed", [False, True], ids=["success", "network-error"])
def test_slow_query_never_stalls_next_update_or_overwrites_new_view(tg, native_queries, monkeypatch, entrypoint, failed):
    key, ctl, ledger, output, *_ = tg
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    def slow_query(*args):
        entered.set()
        assert release.wait(3)
        if failed:
            raise TimeoutError("fixture network timeout")
        return ctl.workbuddy_snapshot(key)
    monkeypatch.setattr(ctl, "refresh_workbuddy_status_now", slow_query)
    def main(*args):
        ui.edit(42, 900, "NEW MAIN PAGE")
        second.set()
    monkeypatch.setattr(bot.main_menu, "handle_back", main)
    errors = []
    def polling():
        try:
            bot._handle_update(update(query_callback(key, entrypoint)))
            bot._handle_update(update("menu:main", "back"))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=polling)
    thread.start()
    try:
        assert entered.wait(2)
        assert second.wait(0.5), "normal vendor latency blocked the following TG update"
        assert "查询中" in output[0][0] and output[-1][0] == "NEW MAIN PAGE"
    finally:
        release.set()
        thread.join(3)
        for worker in native_queries:
            worker.join(3)
    assert not errors and not thread.is_alive()
    assert len(output) == 2 and output[-1][0] == "NEW MAIN PAGE"
    assert ledger["calls"] == 0


@pytest.mark.parametrize("entrypoint", ["activity", "detail", "legacy_credits"])
@pytest.mark.parametrize("outcome", ["success", "error", "deleted"])
def test_current_query_finishes_visibly_with_result_error_or_deleted_account(tg, native_queries, monkeypatch, entrypoint, outcome):
    key, ctl, ledger, output, *_ = tg
    def query(*args):
        assert "查询中" in output[0][0]
        if outcome == "error":
            raise TimeoutError("fixture network timeout")
        if outcome == "deleted":
            config.update(lambda c: c.update(oauthAccounts=[]))
        return ctl.workbuddy_snapshot(key)
    monkeypatch.setattr(ctl, "refresh_workbuddy_status_now", query)
    bot._handle_update(update(query_callback(key, entrypoint)))
    for worker in native_queries:
        worker.join(3)
        assert not worker.is_alive()
    assert len(output) == 2
    result = output[-1][0]
    if outcome == "success":
        assert "✅ 已更新积分" in result and "查询结束:" in result
    elif outcome == "error":
        assert "❌ 查询失败：查询超时" in result and "查询结束:" in result
    else:
        assert "账户已不存在" in result
    assert "fixture network timeout" not in result and ledger["calls"] == 0
