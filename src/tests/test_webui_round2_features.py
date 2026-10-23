"""WebUI round 2: new/extended contracts for items 7-12 (no network)."""
from __future__ import annotations

import json

import pytest

from src.management_control import ManagementError, ManagementErrorCode
from src.management_control.oauth import OAuthProvider
from src.management_control.oauth.menu_bridge import telegram_context
from src.tests.management_oauth_fakes import build_control

OPENAI_ID = "openai:admin@example.test:workspace-1"


def _ctx():
    return telegram_context(42)


def _claude(backend, account_id="claude:ok@example.test"):
    backend.accounts.append({"_id": account_id, "provider": "claude", "type": "claude",
                             "email": "ok@example.test", "access_token": "a", "refresh_token": "r",
                             "enabled": True, "disabled_reason": None, "models": ["claude-x"]})
    return account_id


# ── item 7: Claude official reset plan → execute ───────────────────────────
def test_item7_claude_reset_plan_execute_is_one_shot(monkeypatch):
    control, backend = build_control()
    account = _claude(backend)
    observation = {"organization_uuid": "org", "generation": 3,
                   "cedar_ember": {"next_grant_id": "grant-1", "eligible": True}}
    redeemed = []

    async def status(account_id, program):
        return observation

    async def redeem(account_id, program, **kwargs):
        redeemed.append((account_id, program, kwargs.get("grant_id")))
        return {"redeemed": True}

    monkeypatch.setattr(backend, "claude_reset_status", status, raising=False)
    monkeypatch.setattr(backend, "claude_reset_eligibility", lambda obs, program: None, raising=False)
    monkeypatch.setattr(backend, "claude_reset_redeem", redeem, raising=False)
    plan = control.plan_claude_reset_action(_ctx(), account, "cedar_ember")
    assert plan["available"] is True and plan["grant_id"] == "grant-1" and plan["plan_token"]
    assert redeemed == []  # planning never consumes
    result = control.execute_claude_reset_action(_ctx(), account, plan["plan_token"])
    assert result["program"] == "cedar_ember" and result["redeemed"] is True
    assert redeemed == [(account, "cedar_ember", "grant-1")]
    with pytest.raises(ManagementError):  # one-shot
        control.execute_claude_reset_action(_ctx(), account, plan["plan_token"])


def test_item7_claude_reset_validation_and_ineligible(monkeypatch):
    control, backend = build_control()
    account = _claude(backend)
    with pytest.raises(ManagementError) as bad:
        control.plan_claude_reset_action(_ctx(), account, "nope")
    assert bad.value.code is ManagementErrorCode.VALIDATION_FAILED

    async def status(account_id, program):
        return {"organization_uuid": "org", "generation": 1}

    monkeypatch.setattr(backend, "claude_reset_status", status, raising=False)
    monkeypatch.setattr(backend, "claude_reset_eligibility", lambda obs, p: "not_eligible", raising=False)
    plan = control.plan_claude_reset_action(_ctx(), account, "juniper_tide")
    assert plan["available"] is False and plan["reason"] == "not_eligible" and plan["plan_token"] is None
    with pytest.raises(ManagementError) as other:
        control.plan_claude_reset_action(_ctx(), OPENAI_ID, "cedar_ember")
    assert other.value.code is ManagementErrorCode.UNSUPPORTED_VALUE


def test_item7_claude_reset_upstream_failure_maps_to_502(monkeypatch):
    control, backend = build_control()
    account = _claude(backend)

    async def status(account_id, program):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(backend, "claude_reset_status", status, raising=False)
    with pytest.raises(ManagementError) as caught:
        control.plan_claude_reset_action(_ctx(), account, "cedar_ember")
    assert caught.value.code is ManagementErrorCode.UPSTREAM_ERROR


# ── item 8: OpenAI reset cards + allowCredits ─────────────────────────────
def test_item8_reset_credit_list_from_cache_and_selected_card(monkeypatch):
    control, backend = build_control()
    backend.quota[OPENAI_ID]["raw_data"] = json.dumps({"openai": {"rate_limit_reset_credit_details": {
        "available_count": 2, "data": [{"id": "card-a", "status": "available", "reset_type": "weekly"},
                                       {"id": "card-b", "status": "available"}, {"status": "no-id"}]}}})
    view = control.list_openai_reset_credits(_ctx(), OPENAI_ID)
    assert view["source"] == "cache" and view["available_count"] == 2
    assert [card["credit_id"] for card in view["cards"]] == ["card-a", "card-b"]

    plan = control.plan_quota_reset(_ctx(), OPENAI_ID, credit_id="card-b")
    assert plan.credit_id == "card-b"
    calls = []

    async def redeem(account_id, idempotency_key, credit_id=None):
        calls.append(credit_id)
        return {"status": "redeemed"}

    monkeypatch.setattr(backend, "redeem_openai_reset_credit", redeem)
    control.reset_quota(_ctx(), OPENAI_ID, plan.plan_token)
    assert calls == ["card-b"]


def test_item8_credit_id_is_openai_only():
    control, backend = build_control()
    account = _claude(backend)
    with pytest.raises(ManagementError) as caught:
        control.plan_quota_reset(_ctx(), account, credit_id="card")
    assert caught.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    with pytest.raises(ManagementError):
        control.list_openai_reset_credits(_ctx(), account)


def test_item8_allow_credits_read_and_write(monkeypatch):
    from src.management_control.oauth import UpdateOAuthAccountCommand

    control, backend = build_control()
    evaluated = []
    monkeypatch.setattr(backend, "evaluate_cached_quota", lambda account_id: evaluated.append(account_id), raising=False)
    control.update_account(_ctx(), OPENAI_ID, UpdateOAuthAccountCommand(allow_credits=True))
    assert backend.get_account(OPENAI_ID)["allowCredits"] is True and evaluated == []
    control.update_account(_ctx(), OPENAI_ID, UpdateOAuthAccountCommand(allow_credits=False))
    assert backend.get_account(OPENAI_ID)["allowCredits"] is False and evaluated == [OPENAI_ID]
    assert control.get_account(_ctx(), OPENAI_ID).allow_credits is False
    with pytest.raises(ManagementError) as caught:
        control.update_account(_ctx(), "claude:invalid@example.test", UpdateOAuthAccountCommand(allow_credits=True))
    assert caught.value.code is ManagementErrorCode.UNSUPPORTED_VALUE


# ── item 9: relogin for every provider / proxy selection on add ───────────
def _complete_claude(control, flow):
    from urllib.parse import parse_qs, urlparse
    from src.management_control.oauth import CompleteOAuthLoginCommand
    state = parse_qs(urlparse(flow.auth_url or "").query)["state"][0]
    return control.complete_login_flow(_ctx(), flow.flow_id, flow.flow_secret,
                                       CompleteOAuthLoginCommand(code="provider-code", state=state))


def test_item9_relogin_supported_for_browser_providers():
    control, backend = build_control()
    target = "claude:claude-flow@example.test"
    backend.accounts.append({"_id": target, "provider": "claude", "type": "claude",
                             "email": "claude-flow@example.test", "access_token": "old", "refresh_token": "old",
                             "enabled": False, "disabled_reason": "auth_error", "models": []})
    before = len(backend.accounts)
    flow = control.start_login_flow(_ctx(), OAuthProvider.CLAUDE, relogin_account_id=target)
    assert flow.flow_id.startswith("oflow_")
    result = _complete_claude(control, flow)
    assert result.account_id == target and len(backend.accounts) == before
    assert backend.get_account(target)["access_token"] != "old"
    # A different identity can never overwrite the account being re-logged-in.
    mismatch = control.start_login_flow(_ctx(), OAuthProvider.CLAUDE, relogin_account_id="claude:invalid@example.test")
    with pytest.raises(ManagementError) as conflict:
        _complete_claude(control, mismatch)
    assert conflict.value.code is ManagementErrorCode.IDENTITY_CONFLICT
    with pytest.raises(ManagementError) as healthy:  # relogin only for auth-failed accounts
        control.start_login_flow(_ctx(), OAuthProvider.OPENAI, relogin_account_id=OPENAI_ID)
    assert healthy.value.code is ManagementErrorCode.INVALID_OPERATION_STATE


def test_item9_proxy_target_validation():
    control, _backend = build_control()
    with pytest.raises(ManagementError) as caught:
        control.start_login_flow(_ctx(), OAuthProvider.CLAUDE, proxy_target="no-such-proxy")
    assert caught.value.code is ManagementErrorCode.VALIDATION_FAILED
    from src.management_api.schemas.oauth import StartOAuthLoginFlowRequest
    with pytest.raises(ValueError):
        StartOAuthLoginFlowRequest(provider="claude", proxyTarget="p", reloginAccountId="claude:x")


# ── item 10: usage sections ────────────────────────────────────────────────
def test_item10_usage_sections_cover_tg_details():
    from src.management_control.oauth.usage_sections import build_usage_sections

    class Backend:
        @staticmethod
        def fable_display_from_quota_row(row):
            return None, None

    claude = build_usage_sections("claude", {
        "five_hour_util": 10, "seven_day_util": 20, "sonnet_util": 30, "opus_util": 40,
        "five_hour_reset": "2026-01-01T00:00:00Z", "extra_limit": 100, "extra_used": 25, "extra_util": 25,
        "raw_data": json.dumps({"cedar_ember": {"eligible": True, "grants": [{}]},
                                "juniper_tide": {"available": False, "ineligible_reason": "cooldown"}}),
    }, Backend())
    by_id = {section["id"]: section for section in claude}
    labels = [item["label"] for item in by_id["windows"]["items"]]
    assert labels == ["5 小时", "7 天", "Sonnet 7 天", "Opus 7 天"]
    assert by_id["windows"]["items"][0]["remaining_percent"] == 90
    assert by_id["windows"]["items"][0]["resets_at"] == "2026-01-01T00:00:00Z"
    extra = next(section for section in claude if any(i["label"] == "额外用量" for i in section["items"]))
    assert extra["items"][0]["limit"] == 100 and extra["items"][0]["used"] == 25
    cursor = build_usage_sections("cursor", {"raw_data": json.dumps({"cursor": {
        "limit_cents": 2000, "total_spend_cents": 500, "remaining_cents": 1500, "total_utilization": 25,
        "auto_percent_used": 10, "api_percent_used": 5}})}, Backend())
    cursor_items = [item for section in cursor for item in section["items"]]
    assert cursor_items[0]["unit"] == "usd" and cursor_items[0]["limit"] == 20.0
    assert {"Cursor Models / Auto", "Other Models / API"} <= {item["label"] for item in cursor_items}
    assert build_usage_sections("xai", {}, Backend()) == ()


def test_item10_account_detail_exposes_usage_sections():
    from src.management_api.schemas.oauth import OAuthUsageSectionData

    control, _backend = build_control()
    detail = control.get_account(_ctx(), OPENAI_ID)
    windows = next(section for section in detail.usage_sections if section["id"] == "windows")
    assert [item["label"] for item in windows["items"]] == ["5 小时", "7 天"]
    assert set(OAuthUsageSectionData.model_fields) == {"id", "title", "items"}


# ── item 11: update state / cached check / re-issued activation plan ──────
def test_item11_update_state_and_reissued_activation_plan():
    from src.management_control.operations import OperationStatus
    from src.tests.test_management_update_lifecycle_s7 import (
        FakeClock, _bound_control, _config, _context, _stage, _terminal,
    )
    from src.tests.management_auxiliary_support import FakeUpdates

    clock = FakeClock()
    gateway = FakeUpdates(_config())
    control, store = _bound_control(gateway, clock)
    try:
        context, old_token, _plan = _stage(control, store)
        state = control.state_view(context)
        assert state["stage"] == "staged" and state["activation_available"] is True
        assert state["target_version"] and state["repository"] and state["revision"]
        cached = control.cached_check(context)
        assert cached is not None
        with pytest.raises(ManagementError):  # If-Match required
            control.issue_activation_plan(context, expected_revision=None)
        with pytest.raises(ManagementError) as stale:
            control.issue_activation_plan(context, expected_revision="rev_wrong")
        assert stale.value.code is ManagementErrorCode.REVISION_CONFLICT
        fresh = control.issue_activation_plan(context, expected_revision=state["revision"])
        assert fresh["plan_token"] != old_token and fresh["target_version"] == state["target_version"]
        with pytest.raises(ManagementError):  # the old token was retired
            control.activate_staged(_context("old"), plan_token=old_token, expected_revision=state["revision"])
        activation = control.activate_staged(_context("new"), plan_token=fresh["plan_token"],
                                             expected_revision=state["revision"])
        assert _terminal(store, _context("new"), activation.id).status is OperationStatus.SUCCEEDED
    finally:
        store.close()


def test_item11_activation_plan_requires_staged_state():
    from src.tests.test_management_update_lifecycle_s7 import FakeClock, _bound_control, _config, _context
    from src.tests.management_auxiliary_support import FakeUpdates

    control, store = _bound_control(FakeUpdates(_config()), FakeClock())
    try:
        state = control.state_view(_context("s"))
        assert state["activation_available"] is False
        with pytest.raises(ManagementError) as caught:
            control.issue_activation_plan(_context("s"), expected_revision=state["revision"])
        assert caught.value.code is ManagementErrorCode.INVALID_OPERATION_STATE
    finally:
        store.close()


# ── item 12: retention plan per-database items ─────────────────────────────
def test_item12_retention_plan_items():
    from src.management_control.observability.retention import RetentionControl

    raw = {"items": [
        {"month": "2026-01", "action": "delete_file", "total_requests": 10, "expired_requests": 10,
         "bundle_bytes": 4096, "active": False},
        {"month": "2026-09", "action": "trim_and_vacuum", "total_requests": 50, "expired_requests": 20,
         "expired_mcp": 3, "bundle_bytes": 8192, "active": False},
        {"month": "2026-10", "total_requests": 1},  # unknown action → omitted, never invented
    ], "preflight": {"compaction": [{"month": "2026-09", "will_compact": True, "required_bytes": 9000}]}}
    items = RetentionControl._public_plan_items(raw)
    assert [item["month"] for item in items] == ["2026-01", "2026-09"]
    assert items[0]["releasableBytes"] == 4096 and items[0]["willCompact"] is False
    assert items[1]["deletedRows"] == 20 and items[1]["deletedCallRows"] == 3
    assert items[1]["releasableBytes"] is None and items[1]["willCompact"] is True
    assert items[1]["compactionRequiredBytes"] == 9000 and items[1]["database"] == "2026-09.db"
    bodies = RetentionControl._public_plan_items({"scope": "bodies", "items": [{"month": "2026-08"}]})
    assert bodies[0]["action"] == "purge_bodies"
