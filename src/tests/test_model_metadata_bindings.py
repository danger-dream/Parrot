from __future__ import annotations

import copy

from ._isolation import isolate

isolate()

from src import config, model_metadata, model_pricing  # noqa: E402
from src.telegram.menus import mapping_menu  # noqa: E402


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
    assert metadata["cost"] == raw["cost"]
    assert model_pricing.catalog_model("gpt-5.4") is None
    assert model_pricing.canonical_official_model("gpt-5.4") == "openai/gpt-5.4"
    assert model_pricing.canonical_official_model("GPT-5.4") == "openai/gpt-5.4"
    assert model_pricing.canonical_official_model("gpt-5") != "anthropic/gpt-5"


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
    labels = [row[0]["text"] for row in mapping_menu._overview_kb()["inline_keyboard"]]
    assert labels == ["🔁 模型映射", "🧾 模型元数据", "🗜 压缩模型设置", "◀ 返回主菜单"]
    metadata_labels = [text for text, _ in _callbacks(mapping_menu._metadata_kb())]
    assert "🔄 自动同步元数据" in metadata_labels
    assert "➕ 添加专属绑定" in metadata_labels


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
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )

    assert mapping_menu.handle_callback(1, 2, "cb", "map:meta_scope:0")
    callback = _callback_with_prefix(rendered[-1]["reply_markup"], "map:meta_models:")
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
    assert "模型元数据绑定详情" in rendered[-1]["text"]


def test_metadata_sync_callback_reports_user_understandable_counts(monkeypatch):
    rendered: list[dict] = []
    monkeypatch.setattr(mapping_menu.ui, "answer_cb", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        mapping_menu.ui, "edit",
        lambda chat, message, text, **kwargs: rendered.append({"text": text, **kwargs}),
    )
    monkeypatch.setattr(model_metadata, "auto_sync_metadata", lambda: {
        "scanned": 4, "created": ["a"], "updated": ["b"],
        "unchanged": ["c"], "unmatched": ["d"], "success": 2,
    })
    assert mapping_menu.handle_callback(1, 2, "cb", "map:meta_sync")
    text = rendered[-1]["text"]
    assert "扫描去重模型：<b>4</b>" in text
    assert "新增默认绑定：<b>1</b>" in text
    assert "更新默认绑定：<b>1</b>" in text
    assert "未匹配：<b>1</b>" in text
