"""Type-4 CN billing regression: real non-secret fields, fixture identity, no live I/O."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from src.oauth.workbuddy import billing, common, runtime
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_provider import _package, _page, credential


@pytest.fixture
def cn_experience(monkeypatch):
    # Projection of the 2026-10-18 18:02 BJT get-user-resource response.
    # SlicePeriodUsageDetails was ABSENT; the lifetime 500 remaining is NOT
    # this month's remaining balance. Identifiers are fixture-only.
    now = datetime(2026, 10, 18, 10, 2, 31, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(billing, "datetime", Clock)
    return {
        "ResourceId": "fixture-cn-experience", "PackageName": "CodeBuddy个人体验版",
        "CapacityType": 4, "Status": 0,
        "CapacitySize": 500, "CapacityRemain": 500, "CapacityUsed": 0,
        "CapacitySizePrecise": "500", "CapacityRemainPrecise": "500", "CapacityUsedPrecise": "0",
        "CycleCapacitySize": 500, "CycleCapacityRemain": 0, "CycleCapacityUsed": 500,
        "CycleCapacitySizePrecise": "500", "CycleCapacityRemainPrecise": "0", "CycleCapacityUsedPrecise": "500",
        "CycleStartTime": "2026-10-01 00:00:00", "CycleEndTime": "2026-10-31 23:59:59",
        "DeductionStartTime": 1788779655000, "DeductionEndTime": 2049102854000,
        "ExpiredTime": "",
    }


@pytest.mark.parametrize("slice_field", [{}, {"SlicePeriodUsageDetails": None}, {"SlicePeriodUsageDetails": []}])
def test_missing_slice_uses_current_cycle_not_positive_lifetime(cn_experience, slice_field):
    raw = dict(cn_experience, **slice_field)
    before = copy.deepcopy(raw)
    result = billing.normalize_package(raw, "cn")
    assert result["basis"] == "CycleCapacity" and result["reliable"]
    assert (result["capacity"], result["remaining"], result["used"]) == (500, 0, 500)
    assert result["cycle_start"] == "2026-09-30T16:00:00Z"
    assert result["cycle_end"] == "2026-10-31T15:59:59Z"
    assert raw == before


@pytest.mark.parametrize("overrides", [
    {"CycleStartTime": None}, {"CycleStartTime": "broken"},
    {"CycleStartTime": "2026-11-01 00:00:00"},
    {"CycleEndTime": None}, {"CycleEndTime": "broken"},
    {"CycleEndTime": "2026-10-17 23:59:59"},
    {"DeductionEndTime": "2026-10-17 23:59:59"},
    {"Status": 1}, {"Status": 2},
    {"CycleCapacitySizePrecise": None}, {"CycleCapacityRemainPrecise": None},
    {"CycleCapacityRemainPrecise": "NaN"}, {"CycleCapacityRemainPrecise": -1},
    {"CycleCapacityRemainPrecise": "501"}, {"CycleCapacityUsedPrecise": "NaN"},
    {"CycleCapacityUsedPrecise": "499.999"},
])
def test_invalid_or_inactive_cycle_cannot_become_reliable(cn_experience, overrides):
    result = billing.normalize_package(dict(cn_experience, **overrides), "cn")
    assert not result["reliable"]
    assert result["basis"] != "Capacity"


@pytest.mark.parametrize("field", ["CycleCapacitySizePrecise", "CycleCapacityRemainPrecise"])
def test_missing_precise_cycle_is_not_rescued_by_legacy_integer_or_lifetime(cn_experience, field):
    del cn_experience[field]
    result = billing.normalize_package(cn_experience, "cn")
    assert not result["reliable"] and result["remaining"] is None


def test_unzoned_global_cycle_does_not_invent_timezone(cn_experience):
    result = billing.normalize_package(cn_experience, "global")
    assert not result["reliable"] and result["remaining"] is None


def test_cycle_fallback_preserves_precision_and_derives_missing_used(cn_experience):
    cn_experience.update(CycleCapacityRemainPrecise="412.34000006")
    del cn_experience["CycleCapacityUsedPrecise"]
    # The old integer value is deliberately inconsistent: derive from precise
    # size/remain instead of silently reading a truncated/other-period integer.
    result = billing.normalize_package(cn_experience, "cn")
    assert result["reliable"] and result["basis"] == "CycleCapacity"
    assert result["remaining"] == pytest.approx(412.34000006)
    assert result["used"] == pytest.approx(87.65999994)


def test_explicit_zero_cycle_is_known_not_missing(cn_experience):
    cn_experience.update(CycleCapacitySizePrecise="0", CycleCapacityUsedPrecise="0")
    result = billing.normalize_package(cn_experience, "cn")
    assert result["reliable"]
    assert (result["capacity"], result["remaining"], result["used"]) == (0, 0, 0)


def test_valid_slice_remains_authoritative_over_valid_cycle(cn_experience):
    cn_experience["SlicePeriodUsageDetails"] = [
        {"SlicePeriodCapacitySizePrecise": "30.125", "SlicePeriodCapacityRemainPrecise": "0.000001"},
        {"SlicePeriodCapacitySizePrecise": "999", "SlicePeriodCapacityRemainPrecise": "999"},
    ]
    result = billing.normalize_package(cn_experience, "cn")
    assert result["basis"] == "SlicePeriodCapacity" and result["reliable"]
    assert result["capacity"] == 30.125 and result["remaining"] == .000001
    assert result["used"] == pytest.approx(30.124999)


@pytest.mark.parametrize("details", [{}, [None], [{}], [{"SlicePeriodCapacityRemainPrecise": "NaN"}]])
def test_malformed_slice_is_not_hidden_by_valid_cycle(cn_experience, details):
    cn_experience["SlicePeriodUsageDetails"] = details
    result = billing.normalize_package(cn_experience, "cn")
    assert result["basis"] == "SlicePeriodCapacity" and not result["reliable"]
    assert result["remaining"] is None


@pytest.mark.parametrize("mode", ["remaining", "used"])
@pytest.mark.parametrize("broken", [None, "missing_cycle", "expired_cycle", "malformed_slice"])
def test_31_package_usage_and_ui_clear_old_snapshot_only_for_reliable_cycle(cn_experience, monkeypatch, mode, broken):
    if broken == "missing_cycle":
        del cn_experience["CycleCapacityRemainPrecise"]
    elif broken == "expired_cycle":
        cn_experience["CycleEndTime"] = "2026-10-17 23:59:59"
    elif broken == "malformed_slice":
        cn_experience["SlicePeriodUsageDetails"] = [{}]
    # The failing experience package uses the real projection above. Surround
    # it with 30 controlled, reliable 100-credit packages in the observed shape.
    rows = [cn_experience] + [_package(i, CapacitySize=100, CapacityRemain=100, CapacityUsed=0)
                              for i in range(1, 31)]
    calls = []

    def wire(account, path, **kwargs):
        calls.append(path)
        if path == "/v2/billing/meter/get-user-resource":
            assert kwargs["body"]["PageNumber"] == 1
            return _page(rows, 31)
        if path == "/v2/billing/meter/checkin-activity-status":
            return {"active": True, "todayCheckedIn": True}
        assert path == "/v2/billing/meter/get-payment-type"
        return {"paymentType": "free"}

    monkeypatch.setattr(common, "request", wire)
    monkeypatch.setattr(wb.oauth_control, "config_snapshot", lambda: {"oauthUsageDisplayMode": mode})
    entry = credential()
    old = {"raw_data": json.dumps({"workbuddy": {
        "realm": "cn", "scope": "personal", "last_success_at": 1,
        "credits": {"capacity": 3500, "remaining": 3500, "used": 0, "reliable": True},
    }})}
    usage = billing.fetch_usage_sync(entry["access_token"], account=entry)
    result = runtime.preserve_snapshot("fixture", usage, old)
    block = result["workbuddy"]
    assert block["complete"] and block["errors"] == {} and len(block["packages"]) == 31
    assert sum(not p["reliable"] for p in block["packages"]) == (1 if broken else 0)
    snap = runtime.public_snapshot(entry, {"raw_data": json.dumps(result)})
    text = wb.query_result_text(snap) + "\n" + wb.usage_block("fixture", detail=True, snapshot=snap)
    if broken:
        assert block["status"] == "partial" and not block["credits"]["reliable"]
        assert block["last_success_at"] == 1 and block["last_success_credits"]["remaining"] == 3500
        assert "旧快照" in text and "不是本次余额" in text
    else:
        assert block["status"] == "known" and block["credits"]["reliable"]
        assert (block["credits"]["capacity"], block["credits"]["remaining"], block["credits"]["used"]) == (3500, 3000, 500)
        assert block["last_success_at"] == block["fetched_at"] and not block.get("last_success_credits")
        assert "旧快照" not in text and "数据不完整" not in text and "✅ 已更新积分与活动" in text
        assert "总量 500.00 · 已用 500.00 · 剩余 0.00" in text
        assert ("3,000.00 / 3,500.00" if mode == "remaining" else "500.00 / 3,500.00") in text
    assert len(calls) == 3 and not any("daily-checkin" in path for path in calls)
