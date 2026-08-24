from __future__ import annotations

import copy

from ._isolation import isolate

isolate()

from src import config, model_metadata, model_pricing  # noqa: E402
from src.telegram.menus import main as main_menu, mapping_menu  # noqa: E402


def _set_binding_config(*, defaults=None, scoped=None, compression="", legacy=None):
    config.update(lambda cfg: cfg.update({
        "modelBindings": {
            "defaults": copy.deepcopy(defaults or {}),
            "scoped": copy.deepcopy(scoped or {}),
        },
        "compressionModel": compression,
        "modelMetadata": copy.deepcopy(legacy or {}),
    }))


def test_catalog_exposes_exact_metadata_and_complete_cost_entry():
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    raw = model_pricing.catalog_model("openai/gpt-5.4")
    metadata = model_pricing.catalog_metadata("openai/gpt-5.4")
    assert raw is not None and metadata is not None
    assert metadata["contextWindow"] == raw["limit"]["context"]
    assert metadata["maxOutputTokens"] == raw["limit"]["output"]
    context_thresholds = [
        tier["tier"]["size"] for tier in raw["cost"]["tiers"]
        if tier.get("tier", {}).get("type") == "context"
    ]
    assert metadata["compactTriggerTokens"] == min(context_thresholds)
    assert metadata["cost"] == raw["cost"]
    assert model_pricing.catalog_model("gpt-5.4") is None
    descriptor = next(item for item in model_pricing.catalog_models() if item["key"] == "openai/gpt-5.4")
    assert descriptor["id"] == "gpt-5.4"
    assert descriptor["provider_id"] == "openai"
    assert descriptor["provider_name"]
    assert model_pricing.canonical_official_model("gpt-5.4") == "openai/gpt-5.4"
    assert model_pricing.canonical_official_model("GPT-5.4") == "openai/gpt-5.4"
    assert model_pricing.canonical_official_model("gpt-5") != "anthropic/gpt-5"


def test_catalog_derives_compact_trigger_when_context_tier_is_absent(monkeypatch):
    raw = {
        "id": "demo-model",
        "name": "Demo Model",
        "limit": {"context": 1_000_000, "output": 128_000},
        "cost": {"input": 1, "output": 2},
    }
    monkeypatch.setattr(
        model_pricing, "catalog_model", lambda _key: copy.deepcopy(raw),
    )

    metadata = model_pricing.catalog_metadata("demo/demo-model")

    assert metadata is not None
    assert metadata["compactTriggerTokens"] == 697_600


def test_effective_binding_priority_and_api_alias_outbound_guard():
    _set_binding_config(
        defaults={
            "client-alias": {"target": "openai/gpt-5.4", "source": "auto"},
        },
        scoped={
            "api:Vendor": {
                "client-alias": {
                    "target": "xai/grok-4.5",
                    "outboundModel": "Vendor-Real-Model",
                    "source": "manual",
                },
            },
        },
    )
    default = model_metadata.resolve_binding("client-alias")
    scoped = model_metadata.resolve_binding(
        "client-alias", scope_key="api:Vendor", outbound_model="Vendor-Real-Model",
    )
    stale_scoped = model_metadata.resolve_binding(
        "client-alias", scope_key="api:Vendor", outbound_model="Changed-Real-Model",
    )
    assert default is not None and default.target == "openai/gpt-5.4"
    assert scoped is not None and scoped.target == "xai/grok-4.5"
    assert scoped.outbound_model == "Vendor-Real-Model"
    assert stale_scoped is not None and stale_scoped.target == default.target


def test_auto_sync_uses_canonical_exact_dedupes_visible_models_and_preserves_scoped():
    _set_binding_config(scoped={
        "api:A": {
            "gpt-5.4": {
                "target": "xai/grok-4.5", "outboundModel": "real-a", "source": "manual",
            },
        },
    })
    items = [
        model_metadata.ModelInventoryItem("api:A", "api", "A", "gpt-5.4", "real-a"),
        model_metadata.ModelInventoryItem("oauth:openai:user", "oauth", "User", "gpt-5.4", "gpt-5.4"),
        model_metadata.ModelInventoryItem("api:B", "api", "B", "friendly-alias", "gpt-5.4"),
    ]
    result = model_metadata.auto_sync_metadata(items)
    assert result == {
        "scanned": 2,
        "created": ["gpt-5.4"],
        "updated": [],
        "unchanged": [],
        "unmatched": ["friendly-alias"],
        "success": 1,
    }
    assert model_metadata.resolve_binding("gpt-5.4").target == "openai/gpt-5.4"
    assert model_metadata.resolve_binding(
        "gpt-5.4", scope_key="api:A", outbound_model="real-a",
    ).target == "xai/grok-4.5"


def test_legacy_exact_metadata_and_compression_migrate_without_fuzzy_guess():
    _set_binding_config(legacy={
        "gpt-5.4": {"contextWindow": 1, "compressionModel": True},
        "friendly-gpt": {"contextWindow": 999999},
    })
    result = model_metadata.migrate_legacy_config()
    cfg = config.get()
    assert result == {"bindings": 1, "compression": 1}
    assert cfg["compressionModel"] == "gpt-5.4"
    assert cfg["modelBindings"]["defaults"]["gpt-5.4"]["target"] == "openai/gpt-5.4"
    assert "friendly-gpt" not in cfg["modelBindings"]["defaults"]
    # Runtime limits are catalog values, not the old hand-entered value.
    assert model_metadata.context_window("gpt-5.4") > 1
    assert model_metadata.delete_binding("gpt-5.4") is True
    # The completed migration marker prevents deleted legacy config from
    # resurrecting a default binding through read-through compatibility.
    assert model_metadata.resolve_binding("gpt-5.4") is None
    assert model_metadata.get_compression_model() == "gpt-5.4"


def test_pricing_binding_requires_effective_metadata_binding_and_freezes_scoped_tariff():
    _set_binding_config()
    unbound = model_pricing.build_pricing_binding(
        channel_key="api:A", channel_type="api",
        upstream_protocol="openai-responses",
        outbound_model_id="gpt-5.4", client_visible_model="alias",
    )
    assert unbound.tariff is None
    assert unbound.pricing_key is None
    assert unbound.binding_source == "unbound"

    model_metadata.set_binding(
        "alias", "openai/gpt-5.4", scope_key="api:A",
        outbound_model="gpt-5.4", source="test",
    )
    frozen = model_pricing.build_pricing_binding(
        channel_key="api:A", channel_type="api",
        upstream_protocol="openai-responses",
        outbound_model_id="gpt-5.4", client_visible_model="alias",
    )
    model_metadata.set_binding(
        "alias", "xai/grok-4.5", scope_key="api:A",
        outbound_model="gpt-5.4", source="test",
    )
    next_binding = model_pricing.build_pricing_binding(
        channel_key="api:A", channel_type="api",
        upstream_protocol="openai-responses",
        outbound_model_id="gpt-5.4", client_visible_model="alias",
    )
    assert frozen.pricing_key == "openai/gpt-5.4" and frozen.tariff is not None
    assert next_binding.pricing_key == "xai/grok-4.5" and next_binding.tariff is not None
    assert frozen.binding_json != next_binding.binding_json


def test_compact_selection_is_independent_and_limits_use_effective_binding():
    _set_binding_config(
        defaults={"compact-alias": {"target": "openai/gpt-5.2-pro", "source": "auto"}},
        scoped={
            "oauth:xai:user": {
                "compact-alias": {
                    "target": "xai/grok-4.5", "outboundModel": "grok-real", "source": "manual",
                },
            },
        },
        compression="compact-alias",
    )
    assert model_metadata.get_compression_model() == "compact-alias"
    default_limit = model_metadata.context_window("compact-alias")
    scoped_limit = model_metadata.context_window(
        "compact-alias", scope_key="oauth:xai:user", outbound_model="grok-real",
    )
    assert default_limit == model_pricing.catalog_metadata("openai/gpt-5.2-pro")["contextWindow"]
    assert scoped_limit == model_pricing.catalog_metadata("xai/grok-4.5")["contextWindow"]
    assert scoped_limit != default_limit
    default_metadata = model_pricing.catalog_metadata("openai/gpt-5.2-pro")
    assert default_metadata is not None
    default_trigger = (
        (default_metadata["contextWindow"] - default_metadata["maxOutputTokens"]) * 4
    ) // 5
    assert model_metadata.compact_trigger_tokens("compact-alias") == default_trigger
    assert model_metadata.safe_prompt_limit("compact-alias") == default_trigger
    assert model_metadata.can_fit_for_compact("compact-alias", default_trigger)
    assert not model_metadata.can_fit_for_compact("compact-alias", default_trigger + 1)
    scoped_trigger = model_metadata.compact_trigger_tokens(
        "compact-alias", scope_key="oauth:xai:user", outbound_model="grok-real",
    )
    assert scoped_trigger == model_pricing.catalog_metadata("xai/grok-4.5")["compactTriggerTokens"]
    assert model_metadata.safe_prompt_limit(
        "compact-alias", scope_key="oauth:xai:user", outbound_model="grok-real",
    ) == scoped_trigger
    assert model_metadata.summary_reserve_tokens(
        "compact-alias", scope_key="oauth:xai:user", outbound_model="grok-real",
    ) <= model_metadata.max_output_tokens(
        "compact-alias", scope_key="oauth:xai:user", outbound_model="grok-real",
    )


def _callbacks(markup: dict) -> list[tuple[str, str]]:
    return [
        (button["text"], button["callback_data"])
        for row in markup["inline_keyboard"] for button in row
    ]


def _callback_with_prefix(markup: dict, prefix: str) -> str:
    return next(callback for _, callback in _callbacks(markup) if callback.startswith(prefix))


def test_model_management_has_exact_four_primary_buttons():
    rows = mapping_menu._overview_kb()["inline_keyboard"]
    assert [[button["text"] for button in row] for row in rows] == [
        ["🔁 模型映射", "🧾 模型元数据"],
        ["🗜 压缩模型", "◀ 返回主菜单"],
    ]
    metadata_labels = [text for text, _ in _callbacks(mapping_menu._metadata_kb())]
    assert "🔄 自动同步" in metadata_labels
    assert "➕ 新增专属" in metadata_labels
    assert "🔎 搜索元数据" not in metadata_labels
    assert metadata_labels[-2:] == ["🏠 返回主菜单", "◀ 返回模型管理"]


def test_scoped_binding_telegram_callback_flow(monkeypatch):
    _set_binding_config()
    item = model_metadata.ModelInventoryItem(
        "api:Vendor", "api", "Vendor", "client-alias", "Vendor-Real",
    )
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: [item])
    monkeypatch.setattr(
        model_pricing, "catalog_providers",
        lambda: [{"id": "openai", "name": "OpenAI"}],
    )
    monkeypatch.setattr(
        model_pricing, "catalog_provider_models",
        lambda provider: [{"key": "openai/gpt-5.4", "id": "gpt-5.4", "name": "GPT-5.4"}],
    )
    monkeypatch.setattr(model_pricing, "catalog_models", lambda: [{
        "key": "openai/gpt-5.4", "id": "gpt-5.4", "name": "GPT-5.4",
        "provider_id": "openai", "provider_name": "OpenAI",
    }])
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )

    assert mapping_menu.handle_callback(1, 2, "cb", "map:meta_scope:0")
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_models:")
    mapping_menu.handle_callback(1, 2, "cb", callback)
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_candidates:")
    mapping_menu.handle_callback(1, 2, "cb", callback)
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_providers:")
    mapping_menu.handle_callback(1, 2, "cb", callback)
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_catalog:")
    mapping_menu.handle_callback(1, 2, "cb", callback)
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_save:")
    mapping_menu.handle_callback(1, 2, "cb", callback)

    binding = model_metadata.resolve_binding(
        "client-alias", scope_key="api:Vendor", outbound_model="Vendor-Real",
    )
    assert binding is not None and binding.target == "openai/gpt-5.4"
    assert "模型元数据详情" in rendered[-1]["text"]
    assert "模型限制" in rendered[-1]["text"]
    assert "模型能力" in rendered[-1]["text"]
    assert "标准价格" in rendered[-1]["text"]


def test_metadata_sync_callback_refreshes_saves_then_uses_local_catalog(monkeypatch):
    rendered: list[dict] = []
    events: list[str] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )
    monkeypatch.setattr(
        model_pricing, "refresh_remote_catalog_sync",
        lambda: events.append("refresh") or True,
    )
    monkeypatch.setattr(
        model_pricing, "reload_local_catalog",
        lambda: events.append("reload-local") or True,
    )
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", lambda: (
        events.append("sync") or {
            "scanned": 4, "created": ["a"], "updated": ["b"],
            "unchanged": ["c"], "unmatched": ["d"], "success": 2,
        }
    ))
    monkeypatch.setattr(
        mapping_menu, "_start_metadata_sync_worker", lambda target: target(),
    )
    mapping_menu._METADATA_SYNC_RUNNING = False

    assert mapping_menu.handle_callback(1, 2, "cb", "map:meta_sync")
    assert events == ["refresh", "reload-local", "sync"]
    text = rendered[-1]["text"]
    assert "models.dev：<b>已更新本地目录</b>" in text
    assert "扫描模型 <b>4</b> 个" in text
    assert "新增默认元数据 <b>1</b> 个" in text
    assert "更新默认元数据 <b>1</b> 个" in text
    assert "未找到官方同名模型 <b>1</b> 个" in text
    rows = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2]
    assert rows[-1][0]["text"] == "🏠 返回主菜单"
    assert rows[-1][1]["text"] == "◀ 返回模型元数据"


def test_metadata_sync_ignores_remote_failure_and_uses_saved_local_catalog(monkeypatch):
    events: list[str] = []

    def fail_refresh():
        events.append("refresh-failed")
        raise OSError("offline")

    monkeypatch.setattr(model_pricing, "refresh_remote_catalog_sync", fail_refresh)
    monkeypatch.setattr(
        model_pricing, "reload_local_catalog",
        lambda: events.append("reload-local") or True,
    )
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", lambda: (
        events.append("sync") or {
            "scanned": 1, "created": [], "updated": [],
            "unchanged": ["gpt-5.4"], "unmatched": [], "success": 0,
        }
    ))

    result = mapping_menu._perform_metadata_sync()

    assert events == ["refresh-failed", "reload-local", "sync"]
    assert result["catalog"] == "local"
    assert "models.dev：<b>使用本地目录</b>" in mapping_menu._sync_result_text(result)


def test_metadata_home_uses_stable_two_by_three_grid_and_preserves_page():
    _set_binding_config(defaults={
        f"alias-{index}": {"target": "openai/gpt-5.4", "source": "auto"}
        for index in range(1, 8)
    })

    first = mapping_menu._metadata_kb(mapping_menu._META_DEFAULT, 0)["inline_keyboard"]
    assert [len(row) for row in first] == [2, 2, 2, 2, 3, 2, 2]
    item_callbacks = [
        button["callback_data"]
        for row in first[1:4] for button in row
    ]
    assert len(item_callbacks) == 6
    assert all(callback.startswith("map:meta_item:") for callback in item_callbacks)
    assert first[4][0]["text"] == "◁ 上一页"
    assert first[4][1]["text"] == "1/2"
    assert first[4][2]["text"] == "下一页 ➡"
    assert [button["text"] for button in first[-2]] == ["🔄 自动同步", "➕ 新增专属"]
    assert [button["text"] for button in first[-1]] == ["🏠 返回主菜单", "◀ 返回模型管理"]

    second = mapping_menu._metadata_kb(mapping_menu._META_DEFAULT, 1)["inline_keyboard"]
    assert [len(row) for row in second] == [2, 1, 3, 2, 2]
    assert second[2][0]["text"] == "⬅ 上一页"
    assert second[2][1]["text"] == "2/2"
    assert second[2][2]["text"] == "下一页 ▷"
    assert "默认元数据</b> · 第 2/2 页 · 共 7 条" in mapping_menu._metadata_text(
        mapping_menu._META_DEFAULT, 1,
    )


def test_compression_picker_uses_six_item_grid_and_stable_pager(monkeypatch):
    _set_binding_config(compression="model-1")
    items = [
        model_metadata.ModelInventoryItem(
            "api:Vendor", "api", "Vendor", f"model-{index}", f"real-{index}",
        )
        for index in range(1, 8)
    ]
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: items)
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )

    mapping_menu._show_compression(1, 2, "cb", 0)
    first = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in first] == [2, 2, 2, 3, 2]
    assert sum(
        button["callback_data"].startswith("map:compact_pick:")
        for row in first[:3] for button in row
    ) == 6
    assert [button["text"] for button in first[3]] == [
        "◁ 上一页", "1/2", "下一页 ➡",
    ]
    assert [button["text"] for button in first[-1]] == [
        "清除压缩模型", "返回模型管理",
    ]

    mapping_menu._show_compression(1, 2, "cb", 1)
    second = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in second] == [1, 3, 2]
    assert second[0][0]["text"] == "7. model-7"
    assert [button["text"] for button in second[1]] == [
        "⬅ 上一页", "2/2", "下一页 ▷",
    ]
    assert "第 2/2 页 · 共 7 个" in rendered[-1]["text"]

    callback = second[0][0]["callback_data"]
    assert mapping_menu.handle_callback(1, 2, "cb", callback)
    assert model_metadata.get_compression_model() == "model-7"
    selected = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert selected[0][0]["text"] == "✅ 7. model-7"
    assert selected[1][1]["text"] == "2/2"


def test_scope_picker_uses_oauth_api_tabs_and_six_item_grid(monkeypatch):
    _set_binding_config()
    items = [
        model_metadata.ModelInventoryItem(
            f"api:Vendor-{index}", "api", f"Vendor {index}",
            f"model-{index}", f"real-{index}",
        )
        for index in range(1, 8)
    ]
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: items)
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )

    assert mapping_menu.handle_callback(1, 2, "cb", "map:meta_scope:a:0")
    rows = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2, 2, 3, 2]
    assert [button["text"] for button in rows[0]] == ["OAuth 账户 · 0", "API 渠道 · 7 ✓"]
    assert all(
        button["callback_data"].startswith("map:meta_models:")
        for row in rows[1:4] for button in row
    )
    assert rows[-1][0]["text"] == "🏠 返回主菜单"
    assert rows[-1][1]["text"] == "◀ 返回模型元数据"


def test_same_name_candidates_put_canonical_official_model_first(monkeypatch):
    _set_binding_config()
    item = model_metadata.ModelInventoryItem(
        "api:Router", "api", "Router", "gpt-demo", "openai/gpt-demo",
    )
    monkeypatch.setattr(model_metadata, "inventory_items", lambda: [item])
    monkeypatch.setattr(model_pricing, "canonical_official_model", lambda model: (
        "openai/gpt-demo" if str(model).lower().endswith("gpt-demo") else None
    ))
    monkeypatch.setattr(model_pricing, "catalog_models", lambda: [
        {"key": "azure/gpt-demo", "id": "gpt-demo", "name": "GPT Demo", "provider_id": "azure", "provider_name": "Azure"},
        {"key": "openrouter/openai/gpt-demo", "id": "openai/gpt-demo", "name": "GPT Demo", "provider_id": "openrouter", "provider_name": "OpenRouter"},
        {"key": "openai/gpt-demo", "id": "gpt-demo", "name": "GPT Demo", "provider_id": "openai", "provider_name": "OpenAI"},
    ])
    monkeypatch.setattr(model_pricing, "catalog_metadata", lambda key: {
        "name": "GPT Demo",
        "contextWindow": 1_000_000,
        "maxOutputTokens": 128_000,
        "compactTriggerTokens": 272_000,
        "cost": {"input": 5, "output": 30},
    })
    monkeypatch.setattr(model_pricing, "catalog_model", lambda key: {
        "name": "GPT Demo", "cost": {"input": 5, "output": 30},
    })
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )
    code = mapping_menu._binding_tag(
        scope="api:Router", model="gpt-demo", outbound="openai/gpt-demo",
        flow="add", scope_kind="a", scope_page=0, model_page=0,
    )

    mapping_menu._show_candidate_picker(1, 2, "cb", code)
    text = rendered[-1]["text"]
    assert text.index("⭐ OpenAI 官方") < text.index("OpenRouter")
    rows = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 1, 3, 2, 2]
    assert rows[0][0]["text"] == "⭐ 1. OpenAI"
    assert rows[0][1]["text"] == "2. OpenRouter"
    assert [button["text"] for button in rows[-2]] == ["🔎 选择其他模型", "🏢 浏览提供商"]

    mapping_menu.handle_callback(1, 2, "cb", rows[0][0]["callback_data"])
    binding = model_metadata.resolve_binding(
        "gpt-demo", scope_key="api:Router", outbound_model="openai/gpt-demo",
    )
    assert binding is not None and binding.target == "openai/gpt-demo"
    assert "模型元数据详情" in rendered[-1]["text"]
    assert "压缩阈值：272.0K Tokens" in rendered[-1]["text"]


def test_catalog_search_input_returns_two_column_results_and_clears_state(monkeypatch):
    _set_binding_config()
    monkeypatch.setattr(model_pricing, "catalog_models", lambda: [
        {"key": "openai/gpt-demo", "id": "gpt-demo", "name": "GPT Demo", "provider_id": "openai", "provider_name": "OpenAI"},
        {"key": "openrouter/openai/gpt-demo", "id": "openai/gpt-demo", "name": "GPT Demo", "provider_id": "openrouter", "provider_name": "OpenRouter"},
    ])
    monkeypatch.setattr(model_pricing, "catalog_metadata", lambda key: {
        "contextWindow": 1_000_000, "maxOutputTokens": 128_000,
        "cost": {"input": 5, "output": 30},
    })
    monkeypatch.setattr(model_pricing, "canonical_official_model", lambda model: (
        "openai/gpt-demo" if str(model).lower() == "gpt-demo" else None
    ))
    edited: list[dict] = []
    sent: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: edited.append({"text": text, **kwargs}),
    )
    monkeypatch.setattr(
        mapping_menu.ui, "send",
        lambda chat, text, **kwargs: sent.append({"text": text, **kwargs}),
    )
    code = mapping_menu._binding_tag(
        scope="api:Router", model="gpt-demo", outbound="openai/gpt-demo",
        flow="add", scope_kind="a", scope_page=0, model_page=0,
    )

    mapping_menu._start_catalog_search(1, 2, "cb", code)
    state = mapping_menu.states.get_state(1)
    assert state and state["action"] == f"meta_catalog_search:{code}"
    assert mapping_menu.handle_text_state(1, state["action"], "gpt demo")
    assert mapping_menu.states.get_state(1) is None
    rows = sent[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 3, 2, 2]
    assert all(button["callback_data"].startswith("map:meta_save:") for button in rows[0])


def test_metadata_main_navigation_clears_pending_catalog_search(monkeypatch):
    called: list[tuple] = []
    mapping_menu.states.set_state(99, "meta_catalog_search:deadbeef")
    monkeypatch.setattr(
        main_menu, "handle_back",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    assert mapping_menu.handle_callback(99, 2, "cb", "map:meta_main")
    assert mapping_menu.states.get_state(99) is None
    assert called and called[0][0] == (99, 2, "cb")


def test_metadata_detail_returns_to_original_category_and_page(monkeypatch):
    _set_binding_config(defaults={
        f"alias-{index}": {"target": "openai/gpt-5.4", "source": "auto"}
        for index in range(1, 8)
    })
    page = mapping_menu._metadata_kb(mapping_menu._META_DEFAULT, 1)
    callback = _callback_with_prefix(page, "map:meta_item:")
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )

    mapping_menu.handle_callback(1, 2, "cb", callback)
    rows = rendered[-1]["reply_markup"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2]
    assert rows[-1][0]["text"] == "🏠 返回主菜单"
    assert rows[-1][1]["callback_data"] == "map:meta_view:d:1"


def test_oauth_effective_metadata_scoped_default_upstream_and_vision_false(monkeypatch):
    scope = "oauth:openai:a@example.com:ws"
    upstream = {
        "id": "account-model", "name": "Upstream display",
        "description": "upstream description", "contextWindow": 872_000,
        "maxOutputTokens": 90_000, "supportsImages": True,
        "inputModalities": ["text", "image"], "reasoningEfforts": ["high"],
    }
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [{
            "provider": "openai", "email": "a@example.com", "workspace_id": "ws",
            "models": ["account-model"],
            "account_model_catalog": {"models": [upstream]},
        }],
        "modelBindings": {
            "defaults": {"account-model": {"target": "demo/default", "source": "manual"}},
            "scoped": {scope: {"account-model": {
                "target": "demo/scoped", "source": "manual",
                "outboundModel": "account-model",
            }}},
        },
        "modelMetadata": {},
    }))
    metadata = {
        "demo/default": {"contextWindow": 400_000, "vision": True},
        "demo/scoped": {
            "contextWindow": 200_000, "maxOutputTokens": 20_000,
            "vision": False, "reasoningEfforts": ["low", "medium"],
        },
    }
    monkeypatch.setattr(model_pricing, "catalog_metadata", lambda target: metadata.get(target))

    scoped = model_metadata.resolve_binding(
        "account-model", scope_key=scope, outbound_model="account-model",
    )
    assert scoped is not None and scoped.target == "demo/scoped"
    assert scoped.metadata["contextWindow"] == 200_000
    assert scoped.metadata["maxOutputTokens"] == 20_000
    assert scoped.metadata["vision"] is False
    assert scoped.metadata["supportsImages"] is False
    assert scoped.metadata["inputModalities"] == ["text"]
    assert scoped.metadata["reasoningEfforts"] == ["low", "medium"]
    assert scoped.metadata["description"] == "upstream description"

    config.update(lambda cfg: cfg["modelBindings"]["scoped"].clear())
    default = model_metadata.resolve_binding(
        "account-model", scope_key=scope, outbound_model="account-model",
    )
    assert default is not None and default.target == "demo/default"
    assert default.metadata["contextWindow"] == 400_000

    config.update(lambda cfg: cfg["modelBindings"]["defaults"].clear())
    native = model_metadata.resolve_binding(
        "account-model", scope_key=scope, outbound_model="account-model",
    )
    assert native is not None and native.authority == "account-upstream"
    assert native.metadata["contextWindow"] == 872_000
    assert native.metadata["vision"] is True


def test_oauth_default_fallback_binding_never_uses_stale_account_catalog(monkeypatch):
    scope = "oauth:openai:a@example.com:ws"
    config.update(lambda cfg: cfg.update({
        "oauthAccounts": [{
            "provider": "openai", "email": "a@example.com", "workspace_id": "ws",
            "models": [],
            "account_model_catalog": {"models": [{
                "id": "fallback-model", "contextWindow": 872_000,
                "supportsImages": True,
            }]},
        }],
        "modelBindings": {
            "defaults": {"fallback-model": {"target": "demo/default", "source": "manual"}},
            "scoped": {},
        },
        "modelMetadata": {},
    }))
    monkeypatch.setattr(
        model_pricing, "catalog_metadata",
        lambda target: {"contextWindow": 400_000, "vision": False}
        if target == "demo/default" else None,
    )
    bound = model_metadata.resolve_binding(
        "fallback-model", scope_key=scope, outbound_model="fallback-model",
    )
    assert bound is not None and bound.metadata == {
        "contextWindow": 400_000, "vision": False,
        "supportsImages": False,
    }
    config.update(lambda cfg: cfg["modelBindings"]["defaults"].clear())
    assert model_metadata.resolve_binding(
        "fallback-model", scope_key=scope, outbound_model="fallback-model",
    ) is None
