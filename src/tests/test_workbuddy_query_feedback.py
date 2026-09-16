"""Empty-resource and Telegram query regressions; fixture credentials/network only."""
from __future__ import annotations

import copy
import json
import time

import httpx
import pytest

from src import config, oauth_manager as om, state_db
from src.oauth.workbuddy import billing, common, runtime
from src.telegram import ui
from src.telegram.menus import oauth_menu as menu, workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account, observation
from src.tests.test_workbuddy_provider import _package, _page, credential
from src.tests.test_workbuddy_telegram import tg


@pytest.mark.parametrize("realm", ["cn", "global"])
@pytest.mark.parametrize("rows", [None, []])
def test_explicit_empty_packages_are_success_not_a_zero_balance(monkeypatch, realm, rows):
    data = _page(rows, 0)
    data["Response"]["Data"]["TotalDosage"] = 0
    monkeypatch.setattr(common, "request", lambda *a, **k: data)
    entry = credential(realm=realm, domain="www.codebuddy.cn" if realm == "cn" else "www.codebuddy.ai",
                       workbuddy_client_profile="cli" if realm == "cn" else "ide")
    result = billing.fetch_personal_sync(entry)
    assert result["status"] == "empty" and result["complete"] is True
    assert result["packages"] == [] and result["errors"] == {}
    assert not result["credits"]["reliable"]
    assert all(result["credits"][key] is None for key in ("remaining", "capacity", "used", "used_percent"))


@pytest.mark.parametrize("data", [
    {"TotalCount": 0}, {"Accounts": None}, {"TotalCount": 1, "Accounts": None},
    {"TotalCount": False, "Accounts": None}, {"TotalCount": "invalid", "Accounts": None},
    {"TotalCount": 0, "Accounts": {}}, {"TotalCount": 0, "Accounts": [None]},
])
def test_null_without_explicit_zero_or_malformed_accounts_remains_an_error(monkeypatch, data):
    monkeypatch.setattr(common, "request", lambda *a, **k: {"Response": {"Data": data}})
    result = billing.fetch_personal_sync(credential())
    assert result["status"] == "unknown" and result["complete"] is False
    assert result["errors"]["credits"]["kind"] == "invalid_packages"
    assert result["credits"]["remaining"] is None


def test_international_live_empty_envelope_through_wire_and_payment_query(monkeypatch):
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(config, "get", lambda: {"oauth": {"mockMode": False}})
    paths = []
    def wire(request):
        assert request.url.host == "www.codebuddy.ai"
        assert request.method == "POST" and "X-Refresh-Token" not in request.headers
        paths.append(request.url.path)
        if request.url.path == "/v2/billing/meter/get-user-resource":
            return httpx.Response(200, json={"code": 0, "data": {"Response": {"Data": {
                "TotalCount": 0, "TotalDosage": 0, "Accounts": None}}}})
        assert request.url.path == "/v2/billing/meter/get-payment-type"
        return httpx.Response(200, json={"code": 0, "data": {"paymentType": "free"}})
    monkeypatch.setattr(common.network, "sync_client", lambda **k: httpx.Client(transport=httpx.MockTransport(wire)))
    entry = credential(realm="global", domain="www.codebuddy.ai", workbuddy_client_profile="ide")
    before = copy.deepcopy(entry)
    block = billing.fetch_usage_sync(entry["access_token"], account=entry)["workbuddy"]
    assert block["status"] == "empty" and block["complete"] is True and block["errors"] == {}
    assert block["payment_type"] == "free" and block["checkin"] == {"supported": False}
    assert block["credits"]["remaining"] is None and not block.get("last_success_at")
    assert len(paths) == 2 and entry == before


@pytest.mark.parametrize("realm", ["cn", "global"])
def test_empty_drops_old_credit_fallback_but_unknown_retains_it(monkeypatch, realm):
    old = {"realm": realm, "scope": "personal", "last_success_at": 100,
           "credits": {"reliable": True, "remaining": 320}, "packages": [{"name": "old package"}]}
    row = {"raw_data": json.dumps({"workbuddy": old})}
    monkeypatch.setattr(common, "request", lambda *a, **k: _page(None, 0))
    block = billing.fetch_personal_sync(credential())
    block.update(realm=realm, scope="personal", fetched_at=200)
    value = runtime.preserve_snapshot("fixture", {"workbuddy": block}, row)["workbuddy"]
    assert value["status"] == "empty" and value["packages"] == []
    assert value["credits"]["remaining"] is None
    assert not value.get("last_success_credits") and not value.get("last_success_packages")
    assert not value.get("last_success_at")
    block.update(status="unknown", complete=False, errors={"credits": {"kind": "network"}})
    failed = runtime.preserve_snapshot("fixture", {"workbuddy": block}, row)["workbuddy"]
    assert failed["last_success_credits"]["remaining"] == 320
    assert failed["last_success_packages"] == old["packages"] and failed["last_success_at"] == 100


def test_empty_packages_neither_disable_account_nor_resume_existing_quota(account, monkeypatch):
    monkeypatch.setattr(common, "request", lambda *a, **k: _page(None, 0))
    block = billing.fetch_personal_sync(om.get_account(account))
    usage = observation(account, **block)
    assert om.evaluate_and_toggle_by_usage(account, usage, fresh=True)["action"] == "noop_unknown"
    assert om.get_account(account)["enabled"]
    om.set_disabled_by_quota(account, None)
    assert om.evaluate_and_toggle_by_usage(account, usage, fresh=True)["action"] == "noop_unknown"
    assert om.get_account(account)["disabled_reason"] == "quota"


@pytest.mark.parametrize("realm", ["cn", "global"])
@pytest.mark.parametrize("entrypoint", ["activity", "detail", "legacy_credits"])
@pytest.mark.parametrize("outcome", ["empty", "known", "partial", "unknown", "exception"])
def test_query_progress_result_time_and_error_survive_rerender(tg, monkeypatch, realm, entrypoint, outcome):
    oldkey, ctl, ledger, output, old_snapshot, _, _ = tg
    entry = dict(om.get_account(oldkey), realm=realm,
                 domain="www.codebuddy.cn" if realm == "cn" else "www.codebuddy.ai",
                 workbuddy_client_profile="cli" if realm == "cn" else "ide")
    config.update(lambda c: c.update(oauthAccounts=[entry], oauthUsageDisplayMode="remaining"))
    key = om.get_account_key(entry)
    before = copy.deepcopy(om.get_account(key))
    snap = copy.deepcopy(old_snapshot)
    snap.update(realm=realm, fetched_at=int(time.time()*1000)-3600000,
                last_success_at=int(time.time()*1000)-3600000)
    if realm == "global":
        snap["checkin"] = {"supported": False}
    state_db.quota_save(key, {"raw_data": json.dumps({"workbuddy": snap})})
    paths = []
    def wire(acc, path, **kwargs):
        paths.append(path)
        assert path in {"/v2/billing/meter/get-user-resource", "/v2/billing/meter/get-payment-type"}
        if path.endswith("get-payment-type"):
            return {"paymentType": "free"}
        if outcome == "empty":
            return _page(None, 0)
        if outcome == "partial":
            raise common.WorkBuddyError("credits", status=403, code=1001)
        if outcome == "unknown":
            return _page([_package(CapacityRemain=None, CapacityUsed=None)], 1)
        return _page([_package()], 1)
    monkeypatch.setattr(common, "request", wire)
    monkeypatch.setattr(billing, "fetch_checkin_sync", lambda *a, **k: {
        "supported": True, "active": True, "today_checked_in": False, "observed_at": int(time.time()*1000)})
    async def fetch(account_key):
        # The loading page and callback ACK must precede even the first query.
        assert "查询中" in output[-1][0]
        ui.answer_cb.assert_called_with("cb", "查询中…")
        if outcome == "exception":
            raise common.WorkBuddyError("usage", status=503, kind="network")
        return await om.fetch_usage(account_key)  # Real token guard, parser and previous-snapshot preservation.
    monkeypatch.setattr(om, "fetch_usage_snapshot", fetch)
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    nav = (ui.register_code(key), 3, "quota")
    text, _ = wb.render_activity(key, nav, chat_id=42)
    assert "320" in text and "数据更新于" in text and not paths
    def query():
        if entrypoint == "detail":
            menu.on_refresh_usage(42, 900, "cb", nav[0], page=nav[1], filter_key=nav[2])
        else:
            kind = "refresh_activity" if entrypoint == "activity" else "refresh_credits"
            assert menu.handle_callback(42, 900, "cb", wb._cb(kind, nav))
    for _ in range(2):  # An unchanged balance must still visibly transition on every click.
        output.clear()
        ui.answer_cb.reset_mock()
        query()
        assert len(output) == 2 and "查询中" in output[0][0]
        text, kb = output[-1]
        assert "查询结束:" in text and wb._date(time.time()*1000)[:16] in text
        if outcome == "empty":
            assert "✅ 查询完成：当前未查到有效资源包" in text
            assert "320" not in text and "旧快照" not in text and "部分失败" not in text
            assert "剩余 0" not in text and "计费类型: free" in text
        elif outcome == "known":
            assert "✅ 已更新积分" in text and "（6.00 / 10.00）" in text and "320" not in text
        elif outcome == "partial":
            assert "查询部分失败" in text and "积分/资源包" in text and "HTTP 403" in text and "code 1001" in text
            assert "旧快照" in text and "320" in text
        elif outcome == "unknown":
            assert "积分数据不完整" in text and "未知值不视为零" in text and "旧快照" in text
        else:
            assert "❌ 查询失败" in text and "网络请求失败" in text and "HTTP 503" in text
            assert "页面数据未更新" in text and "320" in text and not paths
        if realm == "global":
            assert "未执行签到" not in text and "未申请试用额度" in text
        else:
            assert "未执行签到" in text
        assert kb and ledger["calls"] == 0
        assert om.get_account(key) == before
        assert "fixture-at" not in text and "fixture-rt" not in text


@pytest.mark.parametrize("error,expected", [
    (common.WorkBuddyError("usage", status=401, code=12153), "请重新登录"),
    (TimeoutError("private-token"), "查询超时"),
    (ValueError("private-token"), "数据格式异常"),
    (OSError("private-token"), "本地数据读取或保存失败"),
    (RuntimeError("private-token"), "请稍后重试"),
])
def test_query_exception_feedback_never_echoes_raw_exception(error, expected):
    text = wb.query_result_text({"realm": "global"}, error=error)
    assert expected in text and "private-token" not in text and "未执行签到" not in text


def test_activity_browse_shows_safe_component_errors_without_triggering_query(tg):
    key, _, ledger, _, snap, save, _ = tg
    snap.update(complete=False, errors={"payment_type": {"http_status": 503, "kind": "upstream", "message": "private-token"}})
    save(snap)
    text, _ = wb.render_activity(key, (ui.register_code(key), 1, "all"), chat_id=42)
    assert "计费类型" in text and "HTTP 503" in text and "数据更新于" in text
    assert "private-token" not in text and ledger["calls"] == 0
