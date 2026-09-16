"""Precise billing values: real response numbers, fixture identity, no live I/O."""
from __future__ import annotations

import copy
import json

import pytest

from src import oauth_manager as om
from src.oauth.workbuddy import billing, common, runtime
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_workbuddy_lifecycle import account, observation
from src.tests.test_workbuddy_provider import _package, _page, credential


def precise_package(prefix="CycleCapacity", **overrides):
    # Actual response values: independently truncated integers do not reconcile,
    # while the corresponding *Precise fields do (500 = 412.34000006 + 87.65999994).
    raw = _package(**{prefix + "Size": 500, prefix + "Remain": 412, prefix + "Used": 87,
                      prefix + "SizePrecise": "500", prefix + "RemainPrecise": "412.34000006",
                      prefix + "UsedPrecise": "87.65999994"})
    return dict(raw, **overrides)


@pytest.mark.parametrize("realm", ["cn", "global"])
@pytest.mark.parametrize("prefix", ["CycleCapacity", "Capacity"])
def test_precise_values_override_truncated_fields_without_rounding_before_validation(realm, prefix):
    raw = precise_package(prefix)
    before = copy.deepcopy(raw)
    result = billing.normalize_package(raw, realm)
    assert result["reliable"] is True and result["basis"] == prefix
    assert result["capacity"] == 500
    assert result["remaining"] == pytest.approx(412.34000006)
    assert result["used"] == pytest.approx(87.65999994)
    assert result["remaining"] + result["used"] == pytest.approx(500)
    assert raw == before


def test_precise_only_cycle_fields_still_take_precedence_over_lifetime():
    raw = precise_package()
    for suffix in ("Size", "Remain", "Used"):
        del raw["CycleCapacity" + suffix]
    result = billing.normalize_package(raw, "cn")
    assert result["basis"] == "CycleCapacity" and result["reliable"]
    assert result["remaining"] == pytest.approx(412.34000006)


def test_missing_precise_fields_fall_back_only_within_the_selected_cycle():
    result = billing.normalize_package(_package(CycleCapacitySize=10, CycleCapacityUsed=4,
        CycleCapacityRemain=0, CycleCapacityRemainPrecise="6"), "cn")
    assert result["reliable"] and (result["capacity"], result["remaining"], result["used"]) == (10, 6, 4)
    partial = billing.normalize_package(_package(CycleCapacityRemainPrecise="0.004"), "cn")
    assert partial["basis"] == "CycleCapacity" and partial["remaining"] == .004
    assert partial["capacity"] is None and partial["used"] is None


@pytest.mark.parametrize("prefix", ["CycleCapacity", "Capacity"])
def test_explicit_precise_zero_does_not_fall_back_to_positive_legacy_fields(prefix):
    result = billing.normalize_package(precise_package(prefix, **{
        prefix + "SizePrecise": "0", prefix + "RemainPrecise": "0", prefix + "UsedPrecise": "0",
    }), "cn")
    assert result["reliable"]
    assert (result["capacity"], result["remaining"], result["used"]) == (0, 0, 0)


@pytest.mark.parametrize("suffix", ["Size", "Remain", "Used"])
@pytest.mark.parametrize("invalid", [None, "", "broken", "NaN", "Infinity", -1, True, {}, "9007199254740994"])
def test_present_but_invalid_precise_value_never_silently_falls_back(suffix, invalid):
    result = billing.normalize_package(precise_package(**{"CycleCapacity" + suffix + "Precise": invalid}), "cn")
    assert result["reliable"] is False
    assert result[{"Size": "capacity", "Remain": "remaining", "Used": "used"}[suffix]] is None


def test_missing_used_can_be_derived_from_precise_capacity_and_remaining():
    raw = precise_package()
    del raw["CycleCapacityUsed"]
    del raw["CycleCapacityUsedPrecise"]
    result = billing.normalize_package(raw, "cn")
    assert result["reliable"] and result["used"] == pytest.approx(87.65999994)


def test_precise_inconsistency_is_not_hidden_by_two_decimal_rounding():
    result = billing.normalize_package(precise_package(CycleCapacitySizePrecise="500",
        CycleCapacityRemainPrecise="412.341", CycleCapacityUsedPrecise="87.658"), "cn")
    assert round(result["remaining"], 2) + round(result["used"], 2) == 500
    assert not result["reliable"]


def test_aggregation_precedes_display_rounding(monkeypatch):
    rows = [_package(i, CapacitySizePrecise="1", CapacityRemainPrecise="0.004",
                     CapacityUsedPrecise="0.996") for i in range(3)]
    monkeypatch.setattr(common, "request", lambda *a, **k: _page(rows, 3))
    result = billing.fetch_personal_sync(credential())
    assert result["complete"] and result["credits"]["reliable"]
    assert result["credits"]["remaining"] == pytest.approx(.012)
    assert result["credits"]["used"] == pytest.approx(2.988)
    assert wb._num(result["credits"]["remaining"]) == "0.01"
    assert wb._num(result["credits"]["used"]) == "2.99"


@pytest.mark.parametrize("remaining,used,expected", [("0.004", "0.996", "kept_enabled"), ("0", "1", "disabled")])
def test_quota_decisions_use_precise_balance_not_two_decimal_display(account, monkeypatch, remaining, used, expected):
    raw = _package(CapacitySizePrecise="1", CapacityRemainPrecise=remaining, CapacityUsedPrecise=used)
    monkeypatch.setattr(common, "request", lambda *a, **k: _page([raw], 1))
    block = billing.fetch_personal_sync(om.get_account(account))
    assert wb._num(block["credits"]["remaining"]) == "0.00"
    result = om.evaluate_and_toggle_by_usage(account, observation(account, **block), fresh=True)
    assert result["action"] == expected
    assert om.get_account(account)["enabled"] is (expected == "kept_enabled")


@pytest.mark.parametrize("mode,display", [("remaining", "2,512.34"), ("used", "87.66")])
def test_precise_response_clears_old_snapshot_and_renders_success_with_two_decimals(monkeypatch, mode, display):
    rows = [precise_package()]
    for i, amount in enumerate([1500, 100, 100, 100, 100, 100, 100], 1):
        rows.append(_package(i, CapacitySize=amount, CapacityRemain=amount, CapacityUsed=0))
    paths = []

    def wire(acc, path, **kwargs):
        paths.append(path)
        if path == "/v2/billing/meter/get-user-resource":
            return _page(rows, 8)
        if path == "/v2/billing/meter/checkin-activity-status":
            return {"active": True, "todayCheckedIn": True}
        assert path == "/v2/billing/meter/get-payment-type"
        return {"paymentType": "free"}

    monkeypatch.setattr(common, "request", wire)
    monkeypatch.setattr(wb.oauth_control, "config_snapshot", lambda: {"oauthUsageDisplayMode": mode})
    entry = credential()
    usage = billing.fetch_usage_sync(entry["access_token"], account=entry)
    old = {"raw_data": json.dumps({"workbuddy": {"realm": "cn", "scope": "personal", "last_success_at": 1,
        "credits": {"remaining": 2299, "capacity": 2300, "used": 1, "reliable": True}}})}
    result = runtime.preserve_snapshot("fixture", usage, old)
    block = result["workbuddy"]
    assert block["complete"] and block["status"] == "known" and block["errors"] == {}
    assert block["credits"]["remaining"] == pytest.approx(2512.34000006)
    assert not block.get("last_success_credits") and block["last_success_at"] == block["fetched_at"]
    snap = runtime.public_snapshot(entry, {"raw_data": json.dumps(result)})
    text = wb.query_result_text(snap) + "\n" + wb.usage_block("fixture", detail=True, snapshot=snap)
    assert "✅ 已更新积分与活动" in text and f"（{display} / 2,600.00）" in text
    assert "总量 500.00 · 已用 87.66 · 剩余 412.34" in text
    assert all(value not in text for value in ("数据不完整", "旧快照", "34000006", "65999994"))
    assert len(paths) == 3 and all("daily-checkin" not in path for path in paths)


@pytest.mark.parametrize("value,expected", [(412.34000006, "412.34"), (87.65999994, "87.66"),
    (1.2, "1.20"), (500, "500.00"), (0, "0.00"), (1234.5678, "1,234.57"), (None, "未知"), (True, "未知")])
def test_credit_display_has_exactly_two_decimals_and_preserves_unknown(value, expected):
    assert wb._num(value) == expected
