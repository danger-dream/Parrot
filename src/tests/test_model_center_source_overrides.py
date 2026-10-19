"""Explicit source enablement over global defaults; Control/API/TG persistence."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from src import config, model_state, oauth_manager as om, state_db
from src.channel import registry
from src.management_control.models import ModelSourceRef, ModelSourceType, ModelStateField
from src.oauth_ids import account_key
from src.telegram import ui
from src.telegram.menus import model_center_menu as menu, model_center_catalog as views
from src.tests.test_management_mapping_support import domain_client
from src.tests.test_workbuddy_lifecycle import account


@pytest.fixture
def center(domain_client, monkeypatch, request):
    client, runtime, admin, _, _ = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)
    entries = [dict(provider="workbuddy", type="workbuddy", realm=realm, uid="fixture-" + realm,
                    workbuddy_client_profile="cli" if realm == "cn" else "workbuddy-cli",
                    label=realm, models=["hy3", "other"], enabled=True, disabledModels=[])
               for realm in ("cn", "global")]
    config.update(lambda cfg: cfg.update(
        oauthAccounts=entries, channels=[dict(name="paid", type="api", providerId="claude",
            protocol="anthropic", enabled=True, apiKey="fixture", baseUrl="https://upstream.invalid",
            models=[{"real": "hy3", "alias": "hy3"}])],
        modelCenter={"disabledModels": ["hy3", "other"], "hiddenModels": [], "apiSourceDisabledModels": {}},
    ))
    registry.rebuild_from_config()
    owner = runtime.control_owner()
    monkeypatch.setattr(menu, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat: chat == 42)
    monkeypatch.setattr(views.usage, "peek", lambda *a, **k: SimpleNamespace(value={}, error=None))
    edits = []
    monkeypatch.setattr(ui, "edit", lambda _chat, _msg, text, reply_markup=None, **k: edits.append((text, reply_markup)))
    monkeypatch.setattr(ui, "answer_cb", lambda *a, **k: None)
    menu.reset_for_tests()
    request.addfinalizer(menu.reset_for_tests)
    return client, admin, owner, entries, edits


def query(center, source=None, status=None):
    client, admin, *_ = center
    params = {"type": "chat", "text": "hy3"}
    if source:
        params.update(sourceType=source.type.value, sourceId=source.id)
    if status:
        params["status"] = status
    response = client.get("/api/management/v1/models", params=params, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def set_state(center, source, enabled):
    client, admin, *_ = center
    revision = query(center)["meta"]["revision"]
    scope = {"type": source.type.value, "id": source.id} if source else {"type": "global"}
    response = client.patch("/api/management/v1/models/actions/state", headers={**admin, "If-Match": revision},
        json={"scope": scope, "selection": {"mode": "ids", "modelIds": ["hy3"]}, "target": {"enabled": enabled}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("source_kind", ["oauth", "api"])
def test_only_selected_source_overrides_default_and_global_off_clears_it(center, source_kind):
    _, _, _, entries, _ = center
    source = (ModelSourceRef(ModelSourceType.OAUTH, account_key(entries[1])) if source_kind == "oauth"
              else ModelSourceRef(ModelSourceType.API, "api:paid"))
    before = copy.deepcopy(config.get())
    assert query(center, source, "enabled")["data"] == []
    assert len(query(center, source, "disabled")["data"]) == 1
    result = set_state(center, source, True)
    assert result["data"]["items"][0]["status"] == "updated"
    shared = query(center)["data"][0]
    assert shared["globalEnabled"] is False
    for item in shared["sources"]:
        expected = item["id"] == source.id
        assert item["enabledOverride"] is expected
        assert item["effectiveRoutable"] is expected
    assert len(query(center, source, "enabled")["data"]) == 1
    assert not query(center, source, "disabled")["data"]
    assert len(query(center, status="enabled")["data"]) == 1
    assert not query(center, status="disabled")["data"]
    assert config.get()["oauthAccounts"] == before["oauthAccounts"]
    assert config.get()["channels"] == before["channels"]
    assert config.get()["modelCenter"]["disabledModels"] == ["hy3", "other"]
    assert set_state(center, source, True)["data"]["items"][0]["status"] == "unchanged"
    config.update(lambda cfg: cfg["channels"].append(dict(cfg["channels"][0], name="new")))
    assert not model_state.is_global_enabled("hy3", source_key="api:new")
    assert set_state(center, None, False)["data"]["items"][0]["status"] == "updated"
    assert not model_state.state_snapshot()["sourceEnabledModels"]
    assert all(not item["effectiveRoutable"] for item in query(center)["data"][0]["sources"])
    assert set_state(center, None, False)["data"]["items"][0]["status"] == "unchanged"


def test_source_disable_clears_override_and_hiding_does_not(center):
    source = ModelSourceRef(ModelSourceType.OAUTH, account_key(center[3][1]))
    set_state(center, source, True)
    config.update(lambda cfg: model_state.set_visible_in_config(cfg, ["hy3"], False))
    key = "oauth:" + source.id
    assert model_state.is_global_enabled("hy3", source_key=key)
    assert not model_state.is_discovery_visible("hy3", source_key=key)
    set_state(center, source, False)
    assert not model_state.has_source_enable_override(key, "hy3")
    assert not model_state.is_source_enabled(key, "hy3")
    assert "hy3" in config.get()["oauthAccounts"][1]["disabledModels"]
    assert "hy3" not in config.get()["oauthAccounts"][0]["disabledModels"]


def test_real_telegram_button_enables_only_source_and_exposes_global_reset(center):
    _, _, _, entries, edits = center
    source = ModelSourceRef(ModelSourceType.OAUTH, account_key(entries[1]))
    session = menu._session(42)
    session.source, session.source_label, session.source_provider = source, "global", "workbuddy"
    resource = query(center)["data"][0]["resourceKey"]
    text, kb = menu._detail_render(42, resource)
    assert "继承全局停用" in text and "全局默认：<code>停用</code>" in text
    enable = next(b for row in kb["inline_keyboard"] for b in row if b["text"] == "仅启用此来源（覆盖全局停用）")
    assert menu.handle_callback(42, 100, "enable", enable["callback_data"])
    text, kb = edits[-1]
    assert "本来源启用（覆盖全局停用）" in text
    assert "显式启用" in text and "当前可用：<code>是</code>" in text
    assert not model_state.is_global_enabled("hy3")
    listing, _ = menu._model_list_render(42)
    assert "✅ <code>hy3</code> · 覆盖全局停用" in listing
    session.source = ModelSourceRef(ModelSourceType.OAUTH, account_key(entries[0]))
    listing, _ = menu._model_list_render(42)
    assert "🚫 <code>hy3</code> · 全局停用" in listing
    session.source = source
    reset = next(b for row in kb["inline_keyboard"] for b in row if "清除所有来源例外" in b["text"])
    assert menu.handle_callback(42, 100, "reset", reset["callback_data"])
    assert not model_state.state_snapshot()["sourceEnabledModels"]
    assert "继承全局停用" in edits[-1][0]


def test_global_batch_disable_clears_source_exceptions(center):
    source = ModelSourceRef(ModelSourceType.OAUTH, account_key(center[3][1]))
    set_state(center, source, True)
    session = menu._session(42)
    session.selected = ["hy3"]
    listed = query(center)
    session.selected_resources = {"hy3": listed["data"][0]["resourceKey"]}
    revision = listed["meta"]["revision"]
    button = menu._batch_button(42, menu._ctx(42), ModelStateField.ENABLED, revision)
    assert "部分来源启用" in button["text"]
    assert menu.handle_callback(42, 100, "batch-off", button["callback_data"])
    assert not model_state.state_snapshot()["sourceEnabledModels"]
    assert not query(center, status="enabled")["data"]


def test_account_disable_still_blocks_explicit_source(center):
    source = ModelSourceRef(ModelSourceType.OAUTH, account_key(center[3][1]))
    set_state(center, source, True)
    config.update(lambda cfg: cfg["oauthAccounts"][1].update(enabled=False))
    item = next(row for row in query(center, source)["data"][0]["sources"] if row["id"] == source.id)
    assert item["enabledOverride"] is True
    assert item["effectiveRoutable"] is False
    assert not query(center, source, "enabled")["data"]


def test_api_rename_and_remove_preserve_exact_override_ownership():
    cfg = {"modelCenter": {"sourceEnabledModels": {"api:old": ["hy3"], "api:other": ["hy3"]}}}
    model_state.rename_api_source_in_config(cfg, "api:old", "api:new")
    assert model_state.has_source_enable_override("api:new", "hy3", cfg)
    assert not model_state.has_source_enable_override("api:old", "hy3", cfg)
    model_state.remove_api_source_in_config(cfg, "api:new")
    assert not model_state.has_source_enable_override("api:new", "hy3", cfg)
    assert model_state.has_source_enable_override("api:other", "hy3", cfg)


def test_oauth_delete_does_not_leave_override_for_recreated_account(account):
    entry = copy.deepcopy(om.get_account(account))
    key = "oauth:" + account
    config.update(lambda cfg: model_state.set_source_enable_override_in_config(cfg, key, ["hy3"], True))
    om.delete_account(account)
    assert not model_state.has_source_enable_override(key, "hy3")
    om.add_account(entry)
    assert not model_state.has_source_enable_override(key, "hy3")


def test_oauth_identity_reference_migration_moves_only_its_override():
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    model_state.set_source_enable_override_in_config(cfg, "oauth:claude:old", ["hy3"], True)
    model_state.set_source_enable_override_in_config(cfg, "oauth:claude:other", ["hy3"], True)
    om._rename_priority_orders_in_config(cfg, "oauth:claude:old", "oauth:claude:new", "anthropic")
    assert model_state.has_source_enable_override("oauth:claude:new", "hy3", cfg)
    assert not model_state.has_source_enable_override("oauth:claude:old", "hy3", cfg)
    assert model_state.has_source_enable_override("oauth:claude:other", "hy3", cfg)
