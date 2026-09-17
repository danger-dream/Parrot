"""Telegram ↔ Management API integration over one real lifecycle Control graph."""
from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import server
import copy

from src import config, state_db
from src.channel import registry
from src.management_api.routers.media_settings import router as media_settings_router
from src.management_control import OperationRegistry, OperationStore
from src.management_control.composition import build_management_controls
from src.management_control.oauth.models import PageSpec
from src.management_control.models import (
    ModelFilters,
    ModelKind,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelStatus,
)
from src.telegram import bot as tgbot
from src.telegram import states, ui
from src.telegram.menus import model_center_menu as menu
from src.tests.test_management_mapping_support import domain_client


def _buttons(keyboard):
    return [button for row in keyboard["inline_keyboard"] for button in row]


def _button(keyboard, text):
    from src.telegram.menus.model_center_icons import label_with_icon
    return next(button for button in _buttons(keyboard) if button["text"] in {text, label_with_icon(text)})


def _catalog() -> None:
    config.update(lambda current: current.__setitem__("channels", [{
        "name": "tg-integration", "type": "api", "enabled": True,
        "protocol": "anthropic", "providerId": "anthropic",
        "baseUrl": "https://upstream.invalid", "apiKey": "isolated-test",
        "models": [{"real": "upstream-one", "alias": "model-one"}],
    }]))
    # Production startup/reload rebuilds this inventory view. The isolated
    # in-process API fixture deliberately does not start server lifespan.
    registry.rebuild_from_config()


def _state_body(field: str, value: bool) -> dict:
    return {
        "scope": {"type": "global"},
        "selection": {"mode": "ids", "modelIds": ["model-one"]},
        "target": {field: value},
    }


def test_real_control_source_picker_keeps_exact_multi_provider_identity_after_reorder(
    domain_client, monkeypatch, request,
):
    _client, runtime, _admin, _read_only, _denied = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)

    def seed(current: dict) -> None:
        current["channels"] = [{
            "name": "API Human", "type": "api", "enabled": True,
            "protocol": "anthropic", "providerId": "anthropic",
            "baseUrl": "https://api-human.invalid", "apiKey": "isolated-api",
            "models": [{"real": "api-wire", "alias": "api-only"}],
        }]
        current["oauthAccounts"] = [
            {
                "provider": "cursor", "type": "cursor", "subject": "cursor-a",
                "email": "cursor-a@example.test", "label": "Team",
                "models": ["cursor-a-only"],
                "cursor_model_catalog": {"models": [{"id": "cursor-a-only"}]},
                "enabled": True,
            },
            {
                "provider": "cursor", "type": "cursor", "subject": "cursor-b",
                "email": "cursor-b@example.test", "label": "Team",
                "models": ["cursor-b-only"],
                "cursor_model_catalog": {"models": [{"id": "cursor-b-only"}]},
                "enabled": True,
            },
            {
                "provider": "xai", "type": "xai", "subject": "grok-x",
                "email": "grok@example.test", "label": "Grok Lab",
                "models": [f"grok-only-{index}" for index in range(1, 8)],
                "enabled": True,
            },
            {
                "provider": "openai", "type": "openai",
                "email": "openai@example.test", "label": "OpenAI Ops",
                "workspace_id": "workspace-readable", "models": [],
                "enabled": True,
            },
            {
                "provider": "antigravity", "type": "antigravity",
                "email": "ag@example.test", "label": "AG Lab",
                "project_id": "project-readable", "models": [],
                "enabled": True,
            },
            {
                "provider": "workbuddy", "type": "workbuddy", "realm": "cn",
                "uid": "7009a407-312c-464e-8e69-67ba00000001",
                "label": "18213053121", "workbuddy_client_profile": "cli",
                "models": ["workbuddy-cn-only"], "enabled": True,
            },
            {
                "provider": "workbuddy", "type": "workbuddy", "realm": "global",
                "uid": "03ebab31-2aef-4000-8000-000000000002",
                "email": "soarsky0204@gmail.com", "workbuddy_client_profile": "ide",
                "models": ["workbuddy-global-only"], "enabled": True,
            },
        ]
        current["modelMapping"] = {"global": {}}

    config.update(seed)
    # Isolated in-process registry build only; no real service/upstream is used.
    registry.rebuild_from_config()
    owner = runtime.control_owner()
    monkeypatch.setattr(menu, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    summaries = {
        str(item.account_id): item
        for item in owner.models.oauth.list_accounts(
            menu._ctx(42), page=PageSpec(page=1, page_size=200),
        ).items
    }
    global_workbuddy = summaries[
        "workbuddy:global:03ebab31-2aef-4000-8000-000000000002:p"
    ]
    assert global_workbuddy.display_name == "soarsky0204@gmail.com"
    assert global_workbuddy.identity == global_workbuddy.display_name
    get_account_calls = []

    def reject_full_account_detail(*args, **kwargs):
        get_account_calls.append((args, kwargs))
        raise AssertionError("source picker must not load full OAuth account details")

    monkeypatch.setattr(owner.models.oauth, "get_account", reject_full_account_detail)
    edits: list[tuple[str, dict]] = []
    answers: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        ui, "edit", lambda _chat, _message, text, reply_markup=None, parse_mode="HTML":
            edits.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        ui, "answer_cb", lambda _cb, text=None, show_alert=False:
            answers.append((text, show_alert)),
    )
    menu.reset_for_tests()
    states.clear_all()
    try:
        menu.handle_callback(42, 100, "picker", "mc:source")
        picker = edits[-1][1]
        cursor_b = _button(
            picker, "Cursor · Team · cursor-b@example.test",
        )["callback_data"]
        api_source = _button(
            picker, "Claude · API Human",
        )["callback_data"]
        workbuddy_cn = _button(
            picker, "WorkBuddy · 18213053121",
        )["callback_data"]
        workbuddy_global = _button(
            picker, "WorkBuddy · soarsky0204@gmail.com",
        )["callback_data"]
        assert _button(picker, "OpenAI · OpenAI Ops · openai@example.test")
        assert _button(picker, "Antigravity · AG Lab · ag@example.test")
        picker_text = "\n".join(button["text"] for button in _buttons(picker))
        assert "cn:" not in picker_text and "global:" not in picker_text
        assert "7009a407" not in picker_text and "03ebab31" not in picker_text
        assert "cursor:" not in picker_text and "xai:" not in picker_text

        # Change both source list orders after rendering. Frozen callbacks still
        # carry canonical IDs and must not infer by provider, label, or position.
        config.update(lambda cfg: (
            cfg["oauthAccounts"].reverse(), cfg["channels"].reverse(),
        ))
        menu.handle_callback(42, 100, "cursor-b", cursor_b)
        text = edits[-1][0]
        state = menu._session(42)
        assert state.source is not None and state.source.id.endswith("cursor-b")
        assert state.source_label == "Cursor · Team · cursor-b@example.test"
        assert "模型中心 · 对话 · 1 个" in text and "cursor-b-only" in text
        assert "cursor-a-only" not in text and "grok-only-" not in text

        menu.handle_callback(42, 100, "workbuddy-cn", workbuddy_cn)
        text = edits[-1][0]
        state = menu._session(42)
        assert state.source == ModelSourceRef(
            ModelSourceType.OAUTH,
            "workbuddy:cn:7009a407-312c-464e-8e69-67ba00000001:p",
        )
        assert state.source_label == "WorkBuddy · 18213053121"
        assert "workbuddy-cn-only" in text and "workbuddy-global-only" not in text
        assert f"来源：{ui.provider_custom_emoji_html('workbuddy')} <code>WorkBuddy · 18213053121</code>" in text
        assert "7009a407" not in text and "cn:" not in text

        menu.handle_callback(
            42, 100, "workbuddy-detail", _button(edits[-1][1], "1")["callback_data"],
        )
        detail_text = edits[-1][0]
        assert "<b>WorkBuddy · 18213053121</b>" in detail_text
        assert "7009a407" not in detail_text and "cn:" not in detail_text

        menu.handle_callback(42, 100, "workbuddy-global", workbuddy_global)
        text = edits[-1][0]
        assert menu._session(42).source == ModelSourceRef(
            ModelSourceType.OAUTH,
            "workbuddy:global:03ebab31-2aef-4000-8000-000000000002:p",
        )
        assert "workbuddy-global-only" in text and "workbuddy-cn-only" not in text
        assert f"来源：{ui.provider_custom_emoji_html('workbuddy')} <code>WorkBuddy · soarsky0204@gmail.com</code>" in text
        assert "03ebab31" not in text and "global:" not in text
        menu.handle_callback(
            42, 100, "workbuddy-global-detail",
            _button(edits[-1][1], "1")["callback_data"],
        )
        detail_text = edits[-1][0]
        assert "<b>WorkBuddy · soarsky0204@gmail.com</b>" in detail_text
        assert "03ebab31" not in detail_text and "global:" not in detail_text

        menu.handle_callback(42, 100, "all", "mc:source")
        menu.handle_callback(42, 100, "api", api_source)
        assert menu._session(42).source == ModelSourceRef(
            ModelSourceType.API, "api:API Human",
        )
        assert "api-only" in edits[-1][0]
        assert get_account_calls == []
        assert not any(show_alert for _text, show_alert in answers)
    finally:
        menu.reset_for_tests()
        states.clear_all()


def test_tg_eighty_chat_models_page_filter_detail_and_clear_with_real_controls(
    domain_client, monkeypatch, request,
):
    _client, runtime, _admin, _read_only, _denied = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)

    def seed_catalog(current: dict) -> None:
        current["channels"] = [
            {
                "name": "tg-eighty-a",
                "type": "api",
                "enabled": True,
                "protocol": "anthropic",
                "providerId": "anthropic",
                "baseUrl": "https://source-a.invalid",
                "apiKey": "isolated-source-a",
                "models": [
                    {"real": f"wire-a-{index:03d}", "alias": f"model-{index:03d}"}
                    for index in range(1, 41)
                ],
            },
            {
                "name": "tg-eighty-b",
                "type": "api",
                "enabled": True,
                "protocol": "anthropic",
                "providerId": "anthropic",
                "baseUrl": "https://source-b.invalid",
                "apiKey": "isolated-source-b",
                "models": [
                    {"real": f"wire-b-{index:03d}", "alias": f"model-{index:03d}"}
                    for index in range(41, 81)
                ],
            },
        ]
        current["oauthAccounts"] = []
        current["modelMapping"] = {"global": {}}

    config.update(seed_catalog)
    registry.rebuild_from_config()
    owner = runtime.control_owner()
    context = owner.models.bind_telegram_actor(42)

    mappings = owner.mapping.list_mappings(
        context, query=None, sort="alias", page=1, page_size=8,
    )
    owner.mapping.put_mapping(
        context, "quick-forty-two", "model-042",
        expected_revision=mappings.revision,
    )
    initial = owner.models.list_models(
        context,
        filters=ModelFilters(kinds=(ModelKind.CHAT,)),
        page=1,
        page_size=200,
    )
    assert initial.total == 80
    source_a = ModelSourceRef(ModelSourceType.API, "api:tg-eighty-a")
    disabled_ids = tuple(f"model-{index:03d}" for index in range(1, 21))
    owner.models.set_state(
        context,
        scope=source_a,
        selection=ModelSelection(
            mode=ModelSelectionMode.IDS,
            model_ids=disabled_ids,
        ),
        target=ModelStateTarget(ModelStateField.ENABLED, False),
        expected_revision=initial.revision,
    )

    edits: list[tuple[str, dict]] = []
    sends: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(menu, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(
        ui, "edit",
        lambda _chat, _message, text, reply_markup=None, parse_mode="HTML":
            edits.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        ui, "send",
        lambda _chat, text, reply_markup=None, parse_mode="HTML":
            sends.append((text, reply_markup)),
    )
    monkeypatch.setattr(ui, "answer_cb", lambda *_args, **_kwargs: None)
    menu.reset_for_tests()
    states.clear_all()

    def listed_ids(text: str) -> list[str]:
        values: list[str] = []
        for line in text.splitlines():
            prefix, separator, remainder = line.partition(". ")
            if not separator or not prefix.isdigit() or "<code>" not in remainder:
                continue
            values.append(remainder.split("<code>", 1)[1].split("</code>", 1)[0])
        return values

    def submit_query(value: str) -> tuple[str, dict]:
        menu.handle_callback(42, 100, "query", "mc:query")
        assert states.get_state(42)["action"] == "mc_query"
        assert menu.handle_text_state(42, "mc_query", value) is True
        return sends[-1]

    try:
        # The TG adapter pages the real production control result: exactly ten
        # stable pages of eight with no duplicated or missing model identity.
        # Live models lead; the twenty source-disabled entries remain manageable.
        expected_order = [f"model-{index:03d}" for index in range(21, 81)] + list(disabled_ids)
        text, keyboard = menu.render(42)
        all_ids: list[str] = []
        for page_no in range(1, 11):
            page_ids = listed_ids(text)
            assert page_ids == expected_order[(page_no - 1) * 8:page_no * 8]
            assert f"模型中心 · 对话 · 80 个" in text
            assert menu._session(42).page == page_no
            all_ids.extend(page_ids)
            if page_no < 10:
                menu.handle_callback(
                    42, 100, f"page-{page_no + 1}",
                    _button(keyboard, "下一页 ▶")["callback_data"],
                )
                text, keyboard = edits[-1]
        assert all_ids == expected_order
        assert len(all_ids) == len(set(all_ids)) == 80

        # One query box must resolve public name, mapping alias and outbound name
        # through the same real control inventory.
        for needle in ("model-042", "quick-forty-two", "wire-b-042"):
            result_text, _result_keyboard = submit_query(needle)
            assert "模型中心 · 对话 · 1 个" in result_text
            assert listed_ids(result_text) == ["model-042"]
            assert f"查询：<code>{needle}</code>" in result_text
        cleared_text, _cleared_keyboard = submit_query("-")
        assert "模型中心 · 对话 · 80 个" in cleared_text
        assert "查询：<code>未设置</code>" in cleared_text

        # Combine a real API-channel source with source-level disabled state.
        menu.handle_callback(42, 100, "source-picker", "mc:source")
        source_keyboard = edits[-1][1]
        menu.handle_callback(
            42, 100, "source-a",
            _button(source_keyboard, "Claude · tg-eighty-a")["callback_data"],
        )
        menu.handle_callback(42, 100, "status-picker", "mc:status")
        status_keyboard = edits[-1][1]
        menu.handle_callback(
            42, 100, "status-disabled",
            _button(status_keyboard, "停用")["callback_data"],
        )
        filtered_text, filtered_keyboard = submit_query("model")
        assert "模型中心 · 对话 · 20 个" in filtered_text
        assert f"来源：{ui.provider_custom_emoji_html('anthropic')} <code>Claude · tg-eighty-a</code> · 状态：<code>停用</code>" in filtered_text
        assert listed_ids(filtered_text) == list(disabled_ids[:8])

        # Enter page two and one detail, then use the rendered return button.
        # Type/source/query/status/page must all survive the round trip.
        menu.handle_callback(
            42, 100, "filtered-page-2",
            _button(filtered_keyboard, "下一页 ▶")["callback_data"],
        )
        page_two_text, page_two_keyboard = edits[-1]
        assert listed_ids(page_two_text) == list(disabled_ids[8:16])
        expected_session = ("chat", source_a, ModelStatus.DISABLED, "model", 2)
        session = menu._session(42)
        assert (session.tab, session.source, session.status, session.text, session.page) == expected_session
        menu.handle_callback(
            42, 100, "detail-9", _button(page_two_keyboard, "9")["callback_data"],
        )
        detail_text, detail_keyboard = edits[-1]
        assert "<b>model-009</b>" in detail_text
        assert "类型：<code>对话</code>" in detail_text
        assert "上游名：<code>wire-a-009</code>" in detail_text
        assert "此模型：<code>停用</code> · 渠道：<code>启用</code> · 当前可用：<code>否</code>" in detail_text
        session = menu._session(42)
        assert (session.tab, session.source, session.status, session.text, session.page) == expected_session
        menu.handle_callback(
            42, 100, "detail-back",
            _button(detail_keyboard, "返回模型列表")["callback_data"],
        )
        returned_text, _returned_keyboard = edits[-1]
        assert listed_ids(returned_text) == list(disabled_ids[8:16])
        assert "查询：<code>model</code>" in returned_text
        assert f"来源：{ui.provider_custom_emoji_html('anthropic')} <code>Claude · tg-eighty-a</code> · 状态：<code>停用</code>" in returned_text
        session = menu._session(42)
        assert (session.tab, session.source, session.status, session.text, session.page) == expected_session

        # Clear query, status and source through their real TG controls. No stale
        # condition may remain, and pagination returns to the full 80-item page 1.
        query_cleared, _ = submit_query("-")
        assert "模型中心 · 对话 · 20 个" in query_cleared
        menu.handle_callback(42, 100, "status-clear-picker", "mc:status")
        menu.handle_callback(
            42, 100, "status-clear",
            _button(edits[-1][1], "全部状态")["callback_data"],
        )
        menu.handle_callback(42, 100, "source-clear-picker", "mc:source")
        menu.handle_callback(
            42, 100, "source-clear",
            _button(edits[-1][1], "全部来源")["callback_data"],
        )
        final_text, _final_keyboard = edits[-1]
        session = menu._session(42)
        assert (session.tab, session.source, session.status, session.text, session.page) == (
            "chat", None, None, "", 1,
        )
        assert "模型中心 · 对话 · 80 个" in final_text
        assert "查询：<code>未设置</code>" in final_text
        assert "来源：<code>全部来源</code> · 状态：<code>全部状态</code>" in final_text
        assert listed_ids(final_text) == expected_order[:8]
    finally:
        menu.reset_for_tests()
        states.clear_all()


def test_tg_api_state_visibility_alias_share_real_controls_and_survive_rebuild(
    domain_client, monkeypatch, request,
):
    client, runtime, admin, _read_only, _denied = domain_client
    state_db.init()
    request.addfinalizer(state_db.close)
    _catalog()
    owner = runtime.control_owner()
    assert owner.models.mapping is owner.mapping
    assert owner.models.images is owner.auxiliary.images
    assert owner.models.xai_media is owner.auxiliary.xai_media

    edits: list[tuple[str, dict]] = []
    sends: list[tuple[str, dict | None]] = []
    answers: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(menu, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(
        ui, "edit",
        lambda _chat, _message, text, reply_markup=None, parse_mode="HTML":
            edits.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        ui, "send",
        lambda _chat, text, reply_markup=None, parse_mode="HTML":
            sends.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        ui, "send_result",
        lambda _chat, text, **kwargs: sends.append((text, kwargs.get("reply_markup"))),
    )
    monkeypatch.setattr(
        ui, "answer_cb",
        lambda _cb, text=None, show_alert=False: answers.append((text, show_alert)),
    )
    menu.reset_for_tests()
    states.clear_all()

    # Open the real Telegram detail page. Every mutation below is dispatched
    # through a frozen Telegram callback and the same runtime owner used by API.
    list_text, list_kb = menu.render(42)
    assert "model-one" in list_text
    menu.handle_callback(42, 100, "open", _button(list_kb, "1")["callback_data"])
    resource_key = menu._session(42).detail_key
    assert resource_key

    # Telegram disables; API immediately observes the durable model state.
    menu.handle_callback(42, 100, "tg-disable", _button(edits[-1][1], "停用模型")["callback_data"])
    api_detail = client.get(f"/api/management/v1/models/{resource_key}", headers=admin)
    assert api_detail.status_code == 200, api_detail.text
    assert api_detail.json()["data"]["globalEnabled"] is False

    # API explicitly enables; Telegram reads the same current state (no HTTP
    # self-call and no adapter cache).
    listed = client.get("/api/management/v1/models?type=chat", headers=admin).json()
    enabled = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": listed["meta"]["revision"]},
        json=_state_body("enabled", True),
    )
    assert enabled.status_code == 200, enabled.text
    detail_text, detail_kb = menu._detail_render(42, resource_key)
    assert "状态：<code>启用</code>" in detail_text

    # Telegram hides globally even from a source-independent detail callback;
    # API observes visible=false, then changes it back for Telegram to observe.
    menu.handle_callback(42, 100, "tg-hide", _button(detail_kb, "对下游隐藏")["callback_data"])
    api_detail = client.get(f"/api/management/v1/models/{resource_key}", headers=admin)
    assert api_detail.json()["data"]["visible"] is False
    listed = client.get("/api/management/v1/models?type=chat", headers=admin).json()
    visible = client.patch(
        "/api/management/v1/models/actions/state",
        headers={**admin, "If-Match": listed["meta"]["revision"]},
        json=_state_body("visible", True),
    )
    assert visible.status_code == 200, visible.text
    detail_text, _detail_kb = menu._detail_render(42, resource_key)
    assert "下游展示开关：<code>开</code>" in detail_text
    assert "只有可用来源才会贡献模型" in detail_text

    # Seed one alias over the API, perform one atomic Telegram rename through
    # its input state (immediately effective), then observe it over the API.
    mappings = client.get("/api/management/v1/model-mappings", headers=admin).json()
    created = client.put(
        "/api/management/v1/model-mappings/quick-tg",
        headers={**admin, "If-Match": mappings["meta"]["revision"]},
        json={"realModel": "model-one"},
    )
    assert created.status_code == 200, created.text
    session = menu._session(42)
    session.tab = "alias"
    alias_text, alias_kb = menu.render(42)
    assert "quick-tg" in alias_text
    alias_callback = next(
        b["callback_data"] for b in _buttons(alias_kb)
        if b["callback_data"].startswith("mc:a:")
        and (a := menu._thaw(42, b["callback_data"].split(":", 2)[2]))
        and a.name == "alias_open" and a.data["alias"] == "quick-tg"
    )
    menu.handle_callback(42, 100, "alias-open", alias_callback)
    menu.handle_callback(
        42, 100, "alias-name",
        _button(edits[-1][1], "编辑别名名称")["callback_data"],
    )
    assert states.get_state(42)["action"] == "mc_alias_name"
    assert menu.handle_text_state(42, "mc_alias_name", "fast-tg") is True
    assert all("保存" not in button["text"] for button in _buttons(sends[-1][1]))
    mappings = client.get(
        "/api/management/v1/model-mappings?query=fast-tg", headers=admin,
    ).json()
    assert mappings["data"] == [{
        "alias": "fast-tg", "realModel": "model-one",
        "sourceLine": "global", "revision": mappings["data"][0]["revision"],
    }]

    # API performs the inverse direction; Telegram renders the new alias.
    current_revision = mappings["meta"]["revision"]
    renamed = client.patch(
        "/api/management/v1/model-mappings/fast-tg",
        headers={**admin, "If-Match": current_revision},
        json={"alias": "final-api", "realModel": "model-one"},
    )
    assert renamed.status_code == 200, renamed.text
    alias_text, _alias_kb = menu.render(42)
    assert "final-api" in alias_text and "model-one" in alias_text

    # Telegram changes one sparse common metadata field through the actual
    # editor/input pipeline; API sees the effective and override values.
    session.tab = "chat"
    detail_text, detail_kb = menu._detail_render(42, resource_key)
    assert "容量、能力与价格" in detail_text
    menu.handle_callback(
        42, 100, "metadata-targets",
        _button(detail_kb, "调整元数据")["callback_data"],
    )
    menu.handle_callback(
        42, 100, "metadata-common",
        _button(edits[-1][1], "模型通用值")["callback_data"],
    )
    menu.handle_callback(
        42, 100, "metadata-field",
        _button(edits[-1][1], "上下文")["callback_data"],
    )
    assert states.get_state(42)["action"] == "mc_metadata_field"
    assert menu.handle_text_state(42, "mc_metadata_field", "256k") is True
    metadata = client.get(
        "/api/management/v1/model-metadata/model-one", headers=admin,
    )
    assert metadata.status_code == 200, metadata.text
    metadata_data = metadata.json()["data"]
    assert metadata_data["effective"]["contextWindow"] == 256_000
    assert metadata_data["commonOverride"]["contextWindow"] == 256_000

    # API changes the same sparse field; Telegram's next render reflects it.
    patched = client.patch(
        "/api/management/v1/model-metadata/model-one/overrides",
        headers={**admin, "If-Match": metadata_data["revision"]},
        json={"scope": "global", "set": {"contextWindow": 300_000}, "unset": []},
    )
    assert patched.status_code == 200, patched.text
    metadata_text, _metadata_kb = menu._metadata_editor_render(
        42, resource_key, None, "capacity",
    )
    assert "上下文：<code>300,000 tokens</code>（手工" in metadata_text

    # Rebuild a complete production Control graph against the same isolated
    # persistence. It must observe both API-final state and alias without reuse
    # of the first ModelCenterControl instance.
    rebuilt_operations = OperationStore(audit_sink=runtime.audit_sink)
    rebuilt_registry = OperationRegistry(rebuilt_operations)
    try:
        rebuilt = build_management_controls(
            audit_sink=runtime.audit_sink,
            operations=rebuilt_operations,
            operation_registry=rebuilt_registry,
            state_store=runtime.state_store,
        )
        assert rebuilt.models is not owner.models
        context = rebuilt.models.bind_telegram_actor(42)
        page = rebuilt.models.list_models(
            context,
            filters=ModelFilters(kinds=(ModelKind.CHAT,), text="model-one"),
            page=1,
            page_size=8,
        )
        assert len(page.items) == 1
        assert page.items[0].global_enabled is True
        assert page.items[0].visible is True
        assert page.items[0].aliases == ("final-api",)
        mapped = rebuilt.mapping.list_mappings(
            context, query="final-api", sort="alias", page=1, page_size=8,
        )
        assert [(item.alias, item.real_model) for item in mapped.items] == [
            ("final-api", "model-one"),
        ]
        rebuilt_metadata = rebuilt.mapping.get_metadata(
            context, "model-one", scope_id=None,
        )
        assert rebuilt_metadata.effective["contextWindow"] == 300_000
        assert rebuilt_metadata.common_override["contextWindow"] == 300_000
    finally:
        rebuilt_operations.close()
        menu.reset_for_tests()
        states.clear_all()


def test_tg_api_media_share_real_controls_survive_rebuild_and_use_production_binding(
    domain_client, monkeypatch,
):
    client, runtime, admin, _read_only, _denied = domain_client
    client.app.include_router(media_settings_router, prefix="/api/management/v1")

    def seed_media(current: dict) -> None:
        current.setdefault("xaiOAuth", {}).update({
            "imageModels": ["grok-initial-a", "grok-initial-b"],
            "videoModels": ["grok-video-initial"],
            "videoJobTtlSeconds": 10_800,
            "mediaRequestTimeoutSeconds": 180,
        })
        current["antigravityOAuth"] = {"imageModels": ["ag-initial"]}
        current["oauthAccounts"] = [{
            "provider": "antigravity",
            "email": "owner@example.test",
            "project_id": "project",
            "imageModels": ["account-only"],
        }]

    config.update(seed_media)
    owner = runtime.control_owner()
    assert owner.models.xai_media is owner.auxiliary.xai_media
    assert owner.models.antigravity_media is owner.auxiliary.antigravity_media

    # The production binding imports this exact module through bot.py and binds
    # the one runtime-owned ModelCenterControl, rather than constructing a TG copy.
    assert tgbot.model_center_menu is menu
    binding = next(
        item for item in server._telegram_control_bindings(owner)
        if item[0] is menu and item[1] == "_CONTROL"
    )
    assert binding == (menu, "_CONTROL", owner.models)
    before_binding = menu._CONTROL
    server._bind_telegram_management_controls(owner)
    try:
        assert menu._CONTROL is owner.models
    finally:
        server._unbind_telegram_management_controls(owner)
    assert menu._CONTROL is before_binding

    sends: list[tuple[str, dict | None]] = []
    monkeypatch.setattr(menu, "_CONTROL", owner.models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "edit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ui, "send",
        lambda _chat, text, reply_markup=None, parse_mode="HTML":
            sends.append((text, reply_markup)),
    )
    monkeypatch.setattr(
        ui, "send_result",
        lambda _chat, text, **kwargs: sends.append((text, kwargs.get("reply_markup"))),
    )
    monkeypatch.setattr(ui, "answer_cb", lambda *_args, **_kwargs: None)
    menu.reset_for_tests()
    states.clear_all()

    # Telegram no longer edits model names. Old add/edit actions only redirect;
    # model updates go through the real hot Management API and appear immediately.
    _text, keyboard = menu._media_manager_render(42, 'xai', 'image', 1)
    assert not any('添加模型' in b['text'] or '批量编辑' in b['text'] for row in keyboard['inline_keyboard'] for b in row)
    old = menu._freeze(42, 'media_input', mode='add', provider='xai', kind='image')
    before = copy.deepcopy(config.get())
    menu.handle_callback(42, 100, 'old-add', old)
    assert states.get_state(42) is None and config.get() == before
    xai = client.get('/api/management/v1/xai/media-settings', headers=admin)
    xai_data = xai.json()['data']
    api_added = client.post('/api/management/v1/xai/media-models/image',
        headers={**admin, 'If-Match': xai_data['revision']}, json={'modelId': 'api-added'})
    assert api_added.status_code == 200, api_added.text
    assert api_added.json()['data']['models'] == ['grok-initial-a', 'grok-initial-b', 'api-added']
    rendered, _keyboard = menu._media_manager_render(42, 'xai', 'image', 1)
    assert 'api-added' in rendered and 'grok-initial-a' in rendered
    bulk = client.patch('/api/management/v1/images/settings', headers={**admin,
        'If-Match': client.get('/api/management/v1/images/settings', headers=admin).json()['data']['revision']},
        json={'models': {'xai': ['bulk-one', 'bulk-two']}})
    assert bulk.status_code == 200, bulk.text
    assert config.get()['image_models']['xai'] == ['bulk-one', 'bulk-two']
    xai = client.get('/api/management/v1/xai/media-settings', headers=admin)
    # API changes a scalar media setting; Telegram reads the same control graph,
    # not an HTTP self-call or stale adapter state.
    xai_data = xai.json()["data"]
    duration = client.patch(
        "/api/management/v1/xai/media-settings",
        headers={**admin, "If-Match": xai_data["revision"]},
        json={"jobTtlSeconds": 7_200},
    )
    assert duration.status_code == 200, duration.text
    video_text, _video_keyboard = menu._video_settings_render(42)
    assert "任务 TTL：7200 秒" in video_text

    # AG generation entry points are retired, without touching saved account data.
    assert client.get("/api/management/v1/antigravity/media-settings", headers=admin).status_code == 404
    assert client.patch("/api/management/v1/antigravity/media-settings", headers=admin, json={"imageModels": []}).status_code == 404

    # Rebuild a second complete graph over the isolated persisted configuration.
    # All final media values must be visible without reusing the first controls.
    rebuilt_operations = OperationStore(audit_sink=runtime.audit_sink)
    rebuilt_registry = OperationRegistry(rebuilt_operations)
    try:
        rebuilt = build_management_controls(
            audit_sink=runtime.audit_sink,
            operations=rebuilt_operations,
            operation_registry=rebuilt_registry,
            state_store=runtime.state_store,
        )
        assert rebuilt.models is not owner.models
        assert rebuilt.models.xai_media is rebuilt.auxiliary.xai_media
        assert rebuilt.models.antigravity_media is rebuilt.auxiliary.antigravity_media
        context = rebuilt.models.bind_telegram_actor(42)
        rebuilt_xai = rebuilt.models.xai_media.get_settings(context)
        assert rebuilt_xai.image_models == ("bulk-one", "bulk-two")
        assert rebuilt_xai.job_ttl_seconds == 7_200
        rebuilt_ag = rebuilt.models.antigravity_media.get_settings(context)
        assert rebuilt_ag.image_models == ()
        assert rebuilt_ag.account_overrides == ()
    finally:
        rebuilt_operations.close()
        menu.reset_for_tests()
        states.clear_all()
