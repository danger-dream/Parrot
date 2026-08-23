from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ._isolation import isolate

isolate()

from src import state_db, status_monitor  # noqa: E402
from src.telegram import ui  # noqa: E402
from src.telegram.menus import (  # noqa: E402
    channel_menu,
    load_balancing_menu,
    logs_menu,
    mapping_menu,
    oauth_defaults_menu,
    oauth_menu,
    stats_menu,
    status_alert_menu,
)


PROVIDERS = ("claude", "openai", "xai", "cursor", "antigravity")


def test_provider_and_family_helpers_emit_custom_icons():
    for provider in PROVIDERS:
        button = ui.provider_button("Provider", "cb", provider)
        assert button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id(provider)
        assert ui.provider_custom_emoji_id(provider) in ui.provider_tag(provider)
    assert ui.provider_button("✏ Claude", "cb", "claude")["text"] == "Claude"
    assert ui.provider_button("🖼 GPT 图片", "cb", "openai")["text"] == "GPT 图片"
    assert ui.provider_button("✅ Claude", "cb", "claude")["text"] == "✅ Claude"
    assert ui.provider_button("1. a@x.com", "cb", "xai")["text"] == "1. a@x.com"

    assert ui.family_tag("anthropic") == (
        f"{ui.provider_custom_emoji_html('claude')} Anthropic"
    )
    assert ui.family_tag("openai") == (
        f"{ui.provider_custom_emoji_html('openai')} OpenAI、"
        f"{ui.provider_custom_emoji_html('xai')} Grok、"
        f"{ui.provider_custom_emoji_html('cursor')} Cursor、"
        f"{ui.provider_custom_emoji_html('antigravity')} Antigravity"
    )
    openai_button = ui.family_button("openai", "family", suffix=" 协议")
    assert openai_button["text"] == "OpenAI、Grok、Cursor、Antigravity 协议"
    assert openai_button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("openai")


def test_oauth_and_status_buttons_use_provider_custom_icons(monkeypatch):
    state_db.init()
    status_monitor._ensure_schema()
    captured = {}
    monkeypatch.setattr(ui, "answer_cb", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ui,
        "edit",
        lambda *_args, **kwargs: captured.update({
            "reply_markup": kwargs.get("reply_markup"),
        }),
    )
    oauth_menu.on_add_menu(1, 2, "cb")
    buttons = [
        button for row in captured["reply_markup"]["inline_keyboard"] for button in row
    ]
    expected = {
        "oa:login": "claude",
        "oa:set_json": "claude",
        "oa:login:openai": "openai",
        "oa:set_rt:openai": "openai",
        "oa:login:xai": "xai",
        "oa:set_rt:xai": "xai",
        "oa:login:cursor": "cursor",
        "oa:login:antigravity": "antigravity",
    }
    for callback, provider in expected.items():
        button = next(item for item in buttons if item.get("callback_data") == callback)
        expected_id = ui.provider_custom_emoji_id(provider)
        if expected_id:
            assert button["icon_custom_emoji_id"] == expected_id
        else:
            assert "icon_custom_emoji_id" not in button

    _text, keyboard = status_alert_menu._main_text_and_kb()
    status_buttons = [
        button for row in keyboard["inline_keyboard"] for button in row
    ]
    for callback, provider in (
        ("stat:toggle_tgt:claude", "claude"),
        ("stat:toggle_tgt:openai", "openai"),
    ):
        button = next(
            item for item in status_buttons if item.get("callback_data") == callback
        )
        assert button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id(provider)


def test_protocol_and_mapping_surfaces_use_rich_families_and_button_icons():
    for protocol, provider in (
        ("anthropic", "claude"),
        ("openai-chat", "openai"),
        ("openai-responses", "openai"),
    ):
        body = channel_menu._protocol_body_label(protocol)
        assert ui.provider_custom_emoji_id(provider) in body
        button = channel_menu._protocol_button(protocol, "cb")
        assert button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id(provider)

    assert ui.provider_custom_emoji_id("cursor") in mapping_menu._line_body_label(
        "openai-chat"
    )
    assert ui.provider_custom_emoji_id("cursor") in (
        oauth_defaults_menu._ingress_body_label("openai-responses")
    )
    defaults = oauth_defaults_menu._overview_kb()["inline_keyboard"]
    buttons = [button for row in defaults for button in row]
    assert any(
        button.get("icon_custom_emoji_id") == ui.provider_custom_emoji_id("claude")
        for button in buttons
    )
    assert any(
        button.get("icon_custom_emoji_id") == ui.provider_custom_emoji_id("openai")
        for button in buttons
    )


def test_stats_and_recent_logs_render_rich_provider_identity(monkeypatch):
    family = stats_menu._render_key_family_split("key", 2, 3)
    assert ui.provider_custom_emoji_id("claude") in family
    assert ui.provider_custom_emoji_id("openai") in family
    assert ui.provider_custom_emoji_id("xai") in family
    assert ui.provider_custom_emoji_id("cursor") in family

    row = {
        "status": "pending",
        "requested_model": "composer-2.5",
        "final_channel_key": "oauth:cursor:user",
        "retry_count": 0,
        "affinity_hit": 0,
    }
    body = ui.fmt_log_entry_body(row)
    assert ui.provider_custom_emoji_id("cursor") in body

    monkeypatch.setattr(
        logs_menu,
        "_filter_options",
        lambda kind: ["oauth:cursor:user"] if kind == "channel" else [],
    )
    base = logs_menu._list_state(1)
    keyboard = logs_menu._filter_menu_kb("channel", base, base)
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    channel_button = next(
        button for button in buttons if str(button.get("callback_data", "")).startswith(
            "logs:ftoggle:channel:"
        )
    )
    assert channel_button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id(
        "cursor"
    )


def test_all_runtime_menus_avoid_plain_provider_icons_and_slash_joining():
    menu_dir = Path(__file__).resolve().parents[1] / "telegram" / "menus"
    forbidden = (
        "ui.provider_icon(",
        "🅾️/",
        "/𝕏",
        "provider_custom_emoji_html('openai')}/",
        'provider_custom_emoji_html("openai")}/',
    )
    failures: list[str] = []
    for path in sorted(menu_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                failures.append(f"{path.name}: {token}")
    assert failures == []


def test_load_balancing_priority_uses_unified_model_and_channel_axes():
    from src import config

    previous = config.get().get("channelSelection")
    try:
        config.update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))
        text, keyboard = load_balancing_menu._main_text_and_kb()
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        callbacks = {button.get("callback_data") for button in buttons}
        assert "lb:models:1" in callbacks
        assert "lb:channels" in callbacks
        assert not any(str(value).startswith("lb:fam:") for value in callbacks)
        assert "模型专属顺序 &gt; 统一渠道/账户顺序" in text
        for provider in PROVIDERS:
            channel = SimpleNamespace(type="oauth", key=f"oauth:{provider}:identity")
            assert ui.provider_custom_emoji_id(provider) in (
                load_balancing_menu._channel_icon(channel)
            )
        api_channel = SimpleNamespace(type="api", key="api:test")
        assert load_balancing_menu._channel_icon(api_channel) == "📡"
        assert load_balancing_menu._channel_icon(
            api_channel, model_context=True,
        ) == "🤖"
    finally:
        config.update(lambda cfg: cfg.__setitem__("channelSelection", previous))
