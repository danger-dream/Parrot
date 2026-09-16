"""Formal-instance feedback: concise effective status and availability-first pages."""
from __future__ import annotations

from dataclasses import replace
import re

import pytest
from starlette.requests import Request

import server
from src import config, model_metadata, state_db
from src.channel import registry
from src.management_control.models import ModelFilters, ModelKind, ModelSourceRef, ModelSourceType
from src.telegram import states, ui
from src.telegram.menus import model_center_catalog as catalog, model_center_menu as menu
from src.tests.test_management_mapping_support import domain_client


@pytest.fixture
def real_catalog(domain_client, monkeypatch):
    client, runtime, admin, *_ = domain_client
    state_db.init()
    monkeypatch.setattr(model_metadata, "resolve_binding", lambda *_args, **_kwargs: None)

    def seed(cfg):
        cfg["oauth"]["mockMode"] = True
        cfg["modelMapping"] = {"global": {"sonnet-alias": "claude-sonnet-4"}}
        cfg["channels"] = [
            {"name": "A", "type": "api", "enabled": True, "protocol": "anthropic",
             "baseUrl": "https://a.invalid", "apiKey": "fixture-only", "providerId": "anthropic",
             "models": ["a-global-off", "b-shared", *[f"live-{i:02d}" for i in range(12)]]},
            {"name": "B", "type": "api", "enabled": True, "protocol": "anthropic",
             "baseUrl": "https://b.invalid", "apiKey": "fixture-only", "models": ["b-shared"]},
            {"name": "off", "type": "api", "enabled": False, "protocol": "anthropic",
             "baseUrl": "https://off.invalid", "apiKey": "fixture-only", "models": ["c-channel-off"]},
        ]
        for channel in cfg["channels"]:
            channel["models"] = [{"alias": model, "real": model} for model in channel["models"]]
        cfg["oauthAccounts"] = [{
            "provider": "cursor", "type": "cursor", "subject": "list-order",
            "email": "cursor@example.test", "label": "Cursor fixture", "enabled": True,
            "access_token": "fixture-only", "models": ["claude-sonnet-4"],
            "cursor_model_catalog": {"models": [{"id": "claude-sonnet-4"}]},
            "cursor_disabled_models": ["claude-sonnet-4"],
        }]
        cfg["modelCenter"] = {
            "schemaVersion": 1, "disabledModels": ["a-global-off"],
            "hiddenModels": ["live-00"], "apiSourceDisabledModels": {"api:A": ["b-shared"]},
        }
    config.update(seed)
    registry.rebuild_from_config()
    control = runtime.control_owner().models
    monkeypatch.setattr(menu, "_CONTROL", control)
    monkeypatch.setattr(ui, "is_admin", lambda chat: chat == 42)
    monkeypatch.setattr(ui, "edit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui, "answer_cb", lambda *_args, **_kwargs: None)
    menu.reset_for_tests()
    states.clear_all()
    try:
        yield client, control, admin
    finally:
        menu.reset_for_tests()
        states.clear_all()
        state_db.close()


def _ids(text):
    return re.findall(r"^\d+\. \S+ <code>([^<]+)</code>", text, re.MULTILINE)


def test_effective_availability_sort_precedes_pagination_and_agrees_between_api_and_tg(real_catalog):
    client, control, admin = real_catalog
    expected = ["b-shared", *[f"live-{i:02d}" for i in range(12)],
                "a-global-off", "c-channel-off", "claude-sonnet-4"]
    page = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,)))
    assert [row.model_id for row in page.items] == expected
    assert [row.available_in() for row in page.items] == [True] * 13 + [False] * 3
    assert page.total == 16
    assert page.items[1].visible is False  # Hidden is still callable, not unavailable.

    seen = []
    for number in (1, 2):
        response = client.get("/api/management/v1/models", headers=admin,
                              params={"type": "chat", "page": number, "pageSize": 8})
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["total"] == 16 and body["meta"]["revision"] == page.revision
        menu._session(42).page = number
        text, _kb = menu.render(42)
        current = [row["modelId"] for row in body["data"]]
        assert _ids(text) == current == expected[(number - 1) * 8:number * 8]
        seen.extend(current)
    assert seen == expected and len(set(seen)) == 16
    text, _kb = menu.render(42)
    assert "16. 🚫 <code>claude-sonnet-4</code> · 来源停用" in text
    assert "14. 🚫 <code>a-global-off</code> · 停用" in text
    assert "15. 🚫 <code>c-channel-off</code> · 渠道停用" in text
    assert "当前无可用来源" not in text and "展示开关" not in text
    menu._session(42).page = 1
    text, _kb = menu.render(42)
    assert "1. ✅ <code>b-shared</code>\n" in text
    assert "2. ✅ <code>live-00</code> · 已隐藏" in text


def test_source_filtered_sort_uses_that_source_but_preserves_revision_and_disabled_filter(real_catalog):
    client, control, admin = real_catalog
    source = ModelSourceRef(ModelSourceType.API, "api:A")
    global_page = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,)))
    source_page = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,), source=source))
    assert [row.model_id for row in source_page.items] == [
        *[f"live-{i:02d}" for i in range(12)], "a-global-off", "b-shared",
    ]
    assert source_page.revision == global_page.revision  # Read ordering never changes CAS identity.
    shared = next(row for row in source_page.items if row.model_id == "b-shared")
    assert shared.available_in() and not shared.available_in(source)
    assert shared.available_in(ModelSourceRef(ModelSourceType.GLOBAL, ""))
    response = client.get("/api/management/v1/models", headers=admin, params={
        "type": "chat", "sourceType": "api", "sourceId": "api:A", "status": "disabled",
    })
    assert response.status_code == 200
    assert [row["modelId"] for row in response.json()["data"]] == ["b-shared"]
    # Existing status filters describe switches, not silently redefined availability.
    response = client.get("/api/management/v1/models", headers=admin,
                          params={"type": "chat", "status": "disabled"})
    assert [row["modelId"] for row in response.json()["data"]] == ["a-global-off"]


@pytest.mark.asyncio
async def test_disabled_cursor_stays_manageable_but_not_discovered_and_can_be_reenabled(real_catalog, monkeypatch):
    client, control, admin = real_catalog
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "headers": []})
    monkeypatch.setattr(server.auth, "validate", lambda _headers: ("fixture-client", [], None))
    found = await server.list_models(request)
    ids = {row["id"] for row in found["data"]}
    assert "claude-sonnet-4" not in ids and "sonnet-alias" not in ids
    assert "a-global-off" not in ids and "c-channel-off" not in ids and "live-00" not in ids
    assert "b-shared" in ids and "live-01" in ids

    page = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,)))
    sonnet = next(row for row in page.items if row.model_id == "claude-sonnet-4")
    assert sonnet.global_enabled and not sonnet.available_in()
    source = sonnet.sources[0]
    assert source.provider == "cursor" and not source.source_enabled
    response = client.patch("/api/management/v1/models/actions/state", headers={
        **admin, "If-Match": page.revision,
    }, json={
        "scope": {"type": "oauth", "id": source.id},
        "selection": {"mode": "ids", "modelIds": ["claude-sonnet-4"]},
        "target": {"enabled": True},
    })
    assert response.status_code == 200, response.text
    registry.rebuild_from_config()
    refreshed = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,)))
    assert next(row for row in refreshed.items if row.model_id == "claude-sonnet-4").available_in()
    assert [row.model_id for row in refreshed.items][:2] == ["b-shared", "claude-sonnet-4"]
    ids = {row["id"] for row in (await server.list_models(request))["data"]}
    assert {"claude-sonnet-4", "sonnet-alias"} <= ids
    # Reusing the old state write remains protected after reordering.
    stale = client.patch("/api/management/v1/models/actions/state", headers={
        **admin, "If-Match": page.revision,
    }, json={"scope": {"type": "oauth", "id": source.id},
             "selection": {"mode": "ids", "modelIds": ["claude-sonnet-4"]},
             "target": {"enabled": False}})
    assert stale.status_code == 409


@pytest.mark.parametrize("case,expected", [
    ("live", (True, "")), ("disabled", (False, "停用")),
    ("source_off", (False, "来源停用")), ("account_off", (False, "账户停用")),
    ("no_source", (False, "无来源")), ("mixed_off", (False, "不可用")),
])
def test_concise_list_state_does_not_confuse_global_switch_with_usable_source(real_catalog, case, expected):
    _client, control, _admin = real_catalog
    page = control.list_models(filters=ModelFilters(kinds=(ModelKind.CHAT,)))
    view = next(row for row in page.items if row.model_id == "claude-sonnet-4")
    source = view.sources[0]
    if case == "live": view = replace(view, sources=(replace(source, source_enabled=True, effective_routable=True),))
    elif case == "disabled": view = replace(view, global_enabled=False)
    elif case == "account_off": view = replace(view, sources=(replace(source, container_enabled=False),))
    elif case == "no_source": view = replace(view, sources=())
    elif case == "mixed_off":
        view = replace(view, sources=(source, replace(source, id="other", source_enabled=True, container_enabled=False)))
    assert catalog._list_model_state(view, None) == expected
