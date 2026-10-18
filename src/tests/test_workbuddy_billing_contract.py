"""Billing wire compatibility; fixture credentials, isolated state, MockTransport only."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest

from src import config, oauth_manager as om, state_db
from src.management_control.oauth import OAuthControl
from src.oauth.workbuddy import actions, billing, common
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account, context, observation
from src.tests.test_workbuddy_provider import _package, _page, credential

# action_env normally replaces this function; integration cases restore the real
# billing implementation while retaining fake token/usage/status boundaries.
_execute_wire = billing.execute_action_sync


@pytest.fixture
def billing_wire(monkeypatch):
    previous = copy.deepcopy(config.get().get("oauth", {}))
    config.update(lambda c: c.update(oauth={**previous, "mockMode": False}))
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    requests, replies = [], []

    def transport(request):
        requests.append(request)
        assert replies, "unexpected network attempt"
        response = replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    monkeypatch.setattr(common.network, "sync_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(transport)))
    yield requests, replies
    config.update(lambda c: c.update(oauth=previous))


@pytest.mark.parametrize("data,award", [
    ({"credit": "1.23456789", "todayCredit": 99, "today_credit": 88}, 1.23456789),
    ({"credit": 0, "todayCredit": 99}, 0),
    ({"todayCredit": 0, "today_credit": 88}, 0),
    ({"today_credit": "0"}, 0),
    ({"todayCredit": 100}, 100),
])
def test_checkin_wire_award_priority_zero_and_legacy_fields(billing_wire, data, award):
    requests, replies = billing_wire
    replies.append({"code": 0, "data": data})
    assert billing.execute_action_sync(credential(), "checkin", account_key="fixture-key") == {
        "status": "succeeded", "awarded_credits": award}
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://www.codebuddy.cn/v2/billing/meter/daily-checkin"
    assert json.loads(request.content) == {} and request.method == "POST"
    assert "x-device-token" not in request.headers


@pytest.mark.parametrize("envelope", [
    {"code": 0, "data": None}, {"code": 0}, {"code": 0, "data": []},
    {"code": 0, "data": {}}, {"code": 0, "data": {"streak_days": 1}},
    {"code": 0, "data": {"credit": None, "todayCredit": 100}},
    {"code": 0, "data": {"credit": "bad", "today_credit": 100}},
    {"code": 0, "data": {"credit": False}},
])
def test_uncertain_wire_result_is_durable_and_only_reconciles(action_env, billing_wire, monkeypatch, envelope):
    key, state = action_env
    requests, replies = billing_wire
    replies.append(envelope)
    monkeypatch.setattr(billing, "execute_action_sync", _execute_wire)
    result = actions.execute(key, "checkin", actor="fixture")
    assert result["status"] == "unknown" and result["phase"] == "response_unknown"
    assert "awarded_credits" not in result
    assert actions.execute(key, "checkin", actor="fixture", retry_failed=True)["status"] == "unknown"
    state["checked"] = True
    result = actions.reconcile_pending(key)[0]
    assert result["status"] == "already_done" and result["reconciled"]
    assert "awarded_credits" not in result and len(requests) == 1


def test_http_read_timeout_is_not_replayed(action_env, billing_wire, monkeypatch):
    key, _ = action_env
    requests, replies = billing_wire
    replies.append(httpx.ReadTimeout("fixture timeout"))
    monkeypatch.setattr(billing, "execute_action_sync", _execute_wire)
    assert actions.execute(key, "checkin", actor="fixture")["status"] == "unknown"
    assert actions.execute(key, "checkin", actor="fixture", retry_failed=True)["status"] == "unknown"
    assert len(requests) == 1


@pytest.mark.parametrize("code,status,label", [
    (1001, "already_done", "已完成"), (1002, "not_eligible", "无签到资格"),
    (1003, "activity_inactive", "活动已结束"),
])
def test_business_results_persist_and_block_manual_and_evening_replay(action_env, billing_wire, monkeypatch, code, status, label):
    key, _ = action_env
    requests, replies = billing_wire
    replies.append({"code": code, "msg": "fixture message must not leak"})
    monkeypatch.setattr(billing, "execute_action_sync", _execute_wire)
    clock = [datetime.now(billing.CN_TIMEZONE).replace(hour=9, minute=5)]

    class Clock:
        @classmethod
        def now(cls, tz):
            return clock[0]

    monkeypatch.setattr(actions, "datetime", Clock)
    config.update(lambda c: c["oauthAccounts"][0].update(workbuddy_auto_checkin=True))
    result = actions.execute(key, "checkin", actor="fixture")
    assert result["status"] == status and result["code"] == code
    assert "awarded_credits" not in result and label in wb.result_text(result)
    assert actions.execute(key, "checkin", actor="fixture", retry_failed=True)["status"] == status
    clock[0] = clock[0].replace(hour=21, minute=5)
    assert actions.auto_checkin_once()[key]["status"] == status
    records = OAuthControl().get_workbuddy_records(context(), key)["items"]
    assert records[0]["status"] == status and len(requests) == 1
    assert "fixture message" not in repr(records)
    # The terminal classification is scoped to this business day, not a
    # permanent eligibility decision about the account or the next activity.
    clock[0] += timedelta(days=1)
    replies.append({"code": 0, "data": {"credit": 0}})
    assert actions.execute(key, "checkin", actor="fixture")["status"] == "succeeded"
    assert len(requests) == 2


@pytest.mark.parametrize("code,status", list(billing.CHECKIN_BUSINESS_STATUSES.items()))
def test_old_rejected_business_codes_are_not_retry_permissions(action_env, monkeypatch, code, status):
    key, state = action_env
    day = actions.business_date()
    ledger_key = actions.action_key(key, "checkin", day)
    state_db.workbuddy_action_begin(ledger_key, {"owner": actions.owner(key), "action": "checkin",
        "business_date": day, "attempt_id": "fixture-attempt", "created_at": 1, "updated_at": 1})
    state_db.workbuddy_action_finish(ledger_key, "fixture-attempt", {"status": "rejected", "code": code})
    result = actions.execute(key, "checkin", actor="fixture", retry_failed=True)
    assert result["status"] == status and result["attempt"] == 1 and state["calls"] == 0


def test_wire_checkin_concurrent_callers_share_one_award(action_env, billing_wire, monkeypatch):
    key, _ = action_env
    requests, replies = billing_wire
    replies.append({"code": 0, "data": {"credit": 0}})
    monkeypatch.setattr(billing, "execute_action_sync", _execute_wire)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: actions.execute(key, "checkin", actor="fixture"), range(4)))
    assert len(requests) == 1 and len({r["id"] for r in results}) == 1
    assert all(r["status"] == "succeeded" and r["awarded_credits"] == 0 for r in results)


@pytest.mark.parametrize("status", ["not_eligible", "activity_inactive"])
def test_activity_ui_does_not_offer_retry_for_terminal_result(monkeypatch, status):
    snap = {"realm": "cn"}
    record = {"action": "checkin", "status": status, "business_date": actions.business_date()}
    monkeypatch.setattr(wb.oauth_control, "workbuddy_snapshot", lambda key: snap)
    monkeypatch.setattr(wb.oauth_control, "get_workbuddy", lambda *a: {"snapshot": snap, "actions": [record]})
    monkeypatch.setattr(wb.oauth_control, "account_snapshot", lambda key: {})
    monkeypatch.setattr(wb.oauth_control, "config_snapshot", lambda: {})
    text, keyboard = wb.render_activity("fixture", ("short", 1, "all"))
    assert "本日不再提交" in text
    assert "立即签到" not in repr(keyboard)


def test_checkin_state_endpoint_order_and_404_only_fallback(monkeypatch):
    calls = []

    def request(account, path, **kwargs):
        calls.append((common.billing_base_url(account), path))
        if path.endswith("checkin-activity-status"):
            raise common.WorkBuddyError("request", status=404)
        return {"active": True, "today_checked_in": False, "today_credit": 0}

    monkeypatch.setattr(common, "request", request)
    assert billing.fetch_checkin_sync(credential())["today_credit"] == 0
    assert calls == [("https://www.codebuddy.cn", "/v2/billing/meter/checkin-activity-status"),
                     ("https://www.codebuddy.cn", "/v2/billing/meter/checkin-status")]


@pytest.mark.parametrize("capacity_type", [4, "4"])
def test_slice_capacity_overrides_lifetime_and_cycle(capacity_type):
    raw = _package(ResourceId="slice", CapacityType=capacity_type, CapacitySizePrecise=1000,
        CapacityRemainPrecise=999, CycleCapacitySizePrecise=100, CycleCapacityRemainPrecise=80,
        SlicePeriodUsageDetails=[{"SlicePeriodCapacitySizePrecise": "30.125", "SlicePeriodCapacityRemainPrecise": "0.000001"},
                                 {"SlicePeriodCapacitySizePrecise": 999, "SlicePeriodCapacityRemainPrecise": 999}])
    before = copy.deepcopy(raw)
    result = billing.normalize_package(raw, "cn")
    assert result["id"] == "slice" and result["basis"] == "SlicePeriodCapacity" and result["unit"] == "credits"
    assert result["capacity"] == 30.125 and result["remaining"] == .000001 and result["reliable"]
    assert result["used"] == pytest.approx(30.124999) and raw == before


@pytest.mark.parametrize("details", [None, [], {}, [None], [{}], [{"SlicePeriodCapacityRemainPrecise": "NaN"}]])
def test_missing_slice_is_unknown_not_lifetime_zero(details):
    result = billing.normalize_package(_package(CapacityType=4, CapacityRemain=0,
        CapacityUsed=10, SlicePeriodUsageDetails=details), "cn")
    assert result["basis"] == "SlicePeriodCapacity" and result["remaining"] is None and not result["reliable"]


@pytest.mark.parametrize("fields,basis,remaining", [
    ({"CapacitySizePrecise": 10, "CapacityRemainPrecise": 0, "CapacityUsedPrecise": 10,
      "CycleCapacitySizePrecise": 99, "CycleCapacityRemainPrecise": 99}, "Capacity", 0),
    ({"CapacitySizePrecise": 10, "CapacityRemainPrecise": 6, "CapacityUsedPrecise": 4,
      "CycleCapacitySizePrecise": 0, "CycleCapacityRemainPrecise": 0}, "Capacity", 6),
    ({"CapacitySizePrecise": 0, "CapacityRemainPrecise": 0, "CycleCapacitySizePrecise": 99,
      "CycleCapacityRemainPrecise": 99}, "CycleCapacity", 99),
    ({"CapacitySizePrecise": 0, "CapacityRemainPrecise": 0, "CapacityUsedPrecise": 0}, "Capacity", 0),
])
def test_declared_capacity_priority_does_not_depend_on_cycle_field_presence(fields, basis, remaining):
    result = billing.normalize_package(_package(**fields), "cn")
    assert result["basis"] == basis and result["remaining"] == remaining and result["reliable"]


def test_slice_precise_zero_preserves_quota_guard(account, monkeypatch):
    row = _package(CapacityType=4, SlicePeriodUsageDetails=[{
        "SlicePeriodCapacitySizePrecise": 30, "SlicePeriodCapacityRemainPrecise": 0}])
    monkeypatch.setattr(common, "request", lambda *a, **kw: _page([row], 1))
    block = billing.fetch_personal_sync(om.get_account(account))
    assert block["credits"]["remaining"] == 0 and block["credits"]["reliable"]
    assert om.evaluate_and_toggle_by_usage(account, observation(account, **block), fresh=True)["action"] == "disabled"


def test_missing_slice_cannot_disable_account_even_if_other_package_is_zero(account, monkeypatch):
    rows = [_package(0, CapacityRemain=0, CapacityUsed=10), _package(1, CapacityType=4)]
    monkeypatch.setattr(common, "request", lambda *a, **kw: _page(rows, 2))
    block = billing.fetch_personal_sync(om.get_account(account))
    assert block["credits"]["remaining"] is None and not block["credits"]["reliable"]
    assert om.evaluate_and_toggle_by_usage(account, observation(account, **block), fresh=True)["action"] == "noop_unknown"
    assert om.get_account(account)["enabled"]


def test_resource_ids_distinguish_identically_sized_pages(monkeypatch):
    monkeypatch.setattr(billing, "PAGE_SIZE", 2)
    pages = [_page([{"ResourceId": str(i), "CapacitySizePrecise": "1", "CapacityRemainPrecise": "0.004"}
                    for i in indexes], 4) for indexes in ([1, 2], [3, 4])]
    monkeypatch.setattr(common, "request", lambda *a, **kw: pages.pop(0))
    block = billing.fetch_personal_sync(credential())
    assert block["complete"] and block["credits"]["reliable"] and not pages
    assert block["credits"]["remaining"] == pytest.approx(.016)


@pytest.mark.parametrize("value", [0, 0.0, "0", "000"])
def test_zero_billing_timestamp_is_unknown_without_changing_zero_credits(value):
    result = billing.normalize_package(_package(CapacityRemain=0, CapacityUsed=10,
        DeductionEndTime=value, CycleEndTime="2026-09-30 23:59:59"), "cn")
    assert result["expires_at"] is None
    assert result["cycle_end"] == "2026-09-30T15:59:59Z"
    assert result["remaining"] == 0 and result["reliable"]


def test_explicit_absolute_epoch_date_is_not_an_unset_numeric_timestamp():
    assert billing._time("1970-01-01T00:00:00Z", "global") == "1970-01-01T00:00:00Z"


@pytest.mark.parametrize("value", [1792281600000, "1792281600000", "2026-10-18T00:00:00Z"])
def test_billing_timestamp_milliseconds_and_iso(value):
    assert billing._time(value, "global") == "2026-10-18T00:00:00Z"
    result = billing.normalize_package(_package(DeductionEndTime=value), "global")
    assert result["expires_at"] == "2026-10-18T00:00:00Z"


@pytest.mark.parametrize("realm,profile,host,prefix", [
    ("cn", "cli", "www.codebuddy.cn", ""),
    ("global", "ide", "www.codebuddy.ai", "/v2"),
])
@pytest.mark.parametrize("depth", [0, 1, 2])
def test_enterprise_profile_path_envelope_and_reset(billing_wire, realm, profile, host, prefix, depth):
    requests, replies = billing_wire
    data = {"limitNum": "100.125", "credit": "0.125", "cycleResetTime": "2026-10-18T00:00:00Z",
            "cycleEndTime": "2020-01-01T00:00:00Z"}
    for _ in range(depth):
        data = {"data": data}
    replies.append({"code": 0, "data": data})
    entry = credential(realm=realm, domain=host, workbuddy_client_profile=profile, enterprise_id="fixture-team")
    result = billing.fetch_enterprise_sync(entry)
    assert result["reliable"] and result["remaining"] == 100 and result["unit"] == "credits"
    assert result["cycle_end"] == "2026-10-18T00:00:00Z"
    assert str(requests[0].url) == f"https://{host}{prefix}/billing/meter/get-enterprise-user-usage"
    assert requests[0].headers["x-enterprise-id"] == "fixture-team"
    assert requests[0].headers["x-tenant-id"] == "fixture-team" and json.loads(requests[0].content) == {}
    assert len(requests) == 1


@pytest.mark.parametrize("data,reliable,remaining", [
    ({"limitNum": 0, "credit": 0}, True, 0),
    ({"limitNum": 0}, False, None), ({"limitNum": "bad", "credit": 0}, False, None),
    ({"limitNum": -1, "credit": 0}, False, None),
    ({}, False, None), ({"data": {}}, False, None),
])
def test_enterprise_missing_or_unlimited_is_not_zero(monkeypatch, data, reliable, remaining):
    monkeypatch.setattr(common, "request", lambda *a, **kw: data)
    result = billing.fetch_enterprise_sync(credential(enterprise_id="fixture-team"))
    assert result["reliable"] is reliable and result["remaining"] == remaining


def test_legacy_global_enterprise_support_is_not_invented(monkeypatch):
    entry = credential(realm="global", domain="www.workbuddy.ai", enterprise_id="fixture-team")
    assert common.is_legacy(entry) and common.billing_base_url(entry) == "https://www.workbuddy.ai"
    monkeypatch.setattr(common, "request", lambda *a, **kw: pytest.fail("legacy scope is unchanged"))
    with pytest.raises(ValueError, match="Unsupported WorkBuddy enterprise scope"):
        billing.fetch_enterprise_sync(entry)


def test_global_trial_existing_path_null_success_and_business_code_unchanged(billing_wire):
    requests, replies = billing_wire
    entry = credential(realm="global", domain="www.workbuddy.ai")
    replies.extend([{"code": 0, "data": None}, {"code": 14051}])
    assert billing.execute_action_sync(entry, "claim_trial") == {"status": "succeeded"}
    assert billing.execute_action_sync(entry, "claim_trial") == {"status": "already_done", "code": 14051}
    assert all(str(r.url) == "https://www.workbuddy.ai/billing/ide/trial" for r in requests)
    assert all(r.headers["user-agent"] == common.LEGACY_USER_AGENT for r in requests)
