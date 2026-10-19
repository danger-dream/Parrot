"""Account-native WorkBuddy type/rates through real catalog and Telegram views."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from src import config, oauth_manager as om, oauth_model_discovery, state_db
from src.channel import registry
from src.management_control.models import ModelFilters, ModelKind, ModelSourceRef, ModelSourceType
from src.oauth.workbuddy import auth, catalog, common, runtime
from src.telegram import ui
from src.telegram.menus import model_center_menu as models, model_center_catalog as views
from src.telegram.menus import workbuddy_oauth_menu as wb
from src.tests.test_management_mapping_support import domain_client
from src.tests.test_workbuddy_lifecycle import account


@pytest.mark.parametrize("value,expected", [
    ("x0.00", "0.00"), ("x0.00 credits", "0.00"), ("x0.11 credits", "0.11"),
    ("x1.39", "1.39"), (" ×0.25 ", "0.25"), (0, "0"), (1.5, "1.5"),
    ("x0.000000000000000001", "0.000000000000000001"),
    (None, None), ("", None), (True, None), (False, None), ("free", None),
    ("x-1", None), ("xNaN", None), ("xInfinity", None), (float("nan"), None),
    ({"credits": "x0"}, None), ("x0<script>", None), ("x0.1x", None),
])
def test_credit_multiplier_is_explicit_finite_nonnegative(value, expected):
    assert catalog.credit_multiplier(value) == expected


@pytest.mark.parametrize("realm,profile", [("cn", "cli"), ("global", "workbuddy-cli"), ("global", "ide")])
def test_discovery_preserves_account_rates_without_changing_routing(monkeypatch, realm, profile):
    payload = {"models": [{"id": "free", "credits": "x0.00 credits", "modelId": "free-wire"},
                          {"id": "paid", "credits": "x0.11"}, {"id": "unknown", "credits": ""}],
               "agents": [{"name": "craft" if profile == "ide" else "cli", "models": ["free", "paid", "unknown"]}]}
    monkeypatch.setattr(common, "request", lambda *a, **k: copy.deepcopy(payload))
    account = {"realm": realm, "workbuddy_client_profile": profile}
    result = oauth_model_discovery.discover_workbuddy(account)
    assert result.models == ["free", "paid", "unknown"]
    records = result.catalog["models"]
    assert records[0]["workbuddyCreditMultiplier"] == "0.00"
    assert records[1]["workbuddyCreditMultiplier"] == "0.11"
    assert "workbuddyCreditMultiplier" not in records[2]
    assert records[0]["wireModel"] == ("free-wire" if profile == "ide" else "free")
    assert all("cost" not in record for record in records)


@pytest.mark.parametrize("kind,label", [
    ("personal", "个人"), ("pro", "个人 Pro"), ("ultimate", "企业版"),
    ("exclusive", "专享版"), ("premise", "私有部署"), ("future-type", "类型未知"),
])
def test_official_account_type_survives_save_and_display(account, kind, label):
    before = om.get_account(account)
    entry = auth.normalize_credential({"realm": "cn", "auth": {
        "accessToken": "new-fixture-at", "refreshToken": "new-fixture-rt"},
        "account": {"uid": before["uid"], "type": kind}}, source="login")
    assert entry["type"] == entry["provider"] == "workbuddy"
    assert entry["workbuddy_account_type"] == kind
    assert auth.normalize_credential(entry)["workbuddy_account_type"] == kind
    result = om.replace_exact_identity(account, entry, expected_account=before)
    assert result["status"] == "replaced"
    saved = om.get_account(account)
    assert saved["workbuddy_account_type"] == kind
    assert wb.provider_line(saved).startswith("🏷️ 账户: " + label + " · 中国区")
    assert "官方账户类型: " + kind in wb.provider_line(saved, detail=True)
    assert runtime.public_snapshot(saved, None)["workbuddy_account_type"] == kind


def test_account_type_unknown_is_not_a_paid_tier_or_provider_override():
    entry = {"provider": "workbuddy", "type": "workbuddy", "realm": "global",
             "workbuddy_client_profile": "workbuddy-cli", "uid": "fixture",
             "access_token": "fixture-at", "refresh_token": "fixture-rt"}
    assert "workbuddy_account_type" not in auth.normalize_credential(entry)
    assert "官方账户类型: 未获取" in wb.provider_line(entry, detail=True)
    entry["workbuddy_account_type"] = "<new-type>"
    assert "&lt;new-type&gt;" in wb.provider_line(entry, detail=True)
    assert "类型未知" in wb.provider_line(entry)


def test_actual_model_center_keeps_rates_per_account_and_marks_unknown(domain_client, monkeypatch, request):
    _client, runtime_api, _admin, _read_only, _denied = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)
    accounts = []
    for realm,rate in (("cn", "x0.11 credits"), ("global", "x0.00")):
        account = {"provider": "workbuddy", "type": "workbuddy", "realm": realm,
                   "workbuddy_client_profile": "cli" if realm == "cn" else "workbuddy-cli",
                   "uid": "fixture-" + realm, "label": realm + " account", "enabled": True,
                   "models": ["shared-fixture", "unknown-fixture", "tiny-fixture"]}
        payload = {"models": [{"id": "shared-fixture", "credits": rate},
                              {"id": "unknown-fixture", "credits": ""},
                              {"id": "tiny-fixture", "credits": "x0.000000000000000001"}],
                   "agents": [{"name": "cli", "models": account["models"]}]}
        monkeypatch.setattr(common, "request", lambda *a, _payload=payload, **k: copy.deepcopy(_payload))
        account["account_model_catalog"] = oauth_model_discovery.discover_workbuddy(account).catalog
        accounts.append(account)
    config.update(lambda cfg: cfg.update(oauthAccounts=accounts, channels=[], modelMapping={"global": {}}))
    registry.rebuild_from_config()
    owner = runtime_api.control_owner()
    monkeypatch.setattr(models, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: chat_id == 42)
    monkeypatch.setattr(views.usage, "peek", lambda *a, **k: SimpleNamespace(value={}, error=None))
    models.reset_for_tests()
    request.addfinalizer(models.reset_for_tests)
    ctx = models._ctx(42)
    for account, expected in zip(accounts, ("×0.11", "×0.00（免费）")):
        source = ModelSourceRef(ModelSourceType.OAUTH, om.get_account_key(account))
        state = models._session(42)
        state.source, state.source_label, state.source_provider = source, account["label"], "workbuddy"
        text, kb = models._model_list_render(42)
        assert expected in text and "倍率未知" in text
        assert "×0.000000000000000001" in text
        if account["realm"] == "cn":
            assert "（免费）" not in text
        page = owner.models.list_models(ctx, filters=ModelFilters(kinds=(ModelKind.CHAT,), source=source), page=1, page_size=20)
        shared = next(item for item in page.items if item.model_id == "shared-fixture")
        detail, _ = models._detail_render(42, shared.resource_key)
        assert expected in detail and account["label"] in detail
        assert "（免费）" not in detail if account["realm"] == "cn" else "×0.11" not in detail
        unknown = next(item for item in page.items if item.model_id == "unknown-fixture")
        detail, _ = models._detail_render(42, unknown.resource_key)
        assert "倍率未知" in detail and "免费" not in detail
        assert kb["inline_keyboard"]
    models._session(42).source = None
    text, _ = models._model_list_render(42)
    assert "×0.11" in text and "×0.00（免费）" in text and "随账户不同" in text
