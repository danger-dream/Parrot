from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ._isolation import isolate

isolate()

from src import config, notifier, state_db, status_monitor  # noqa: E402
from src.channel import registry  # noqa: E402
from src.telegram import ui  # noqa: E402
from src.telegram.menus import (  # noqa: E402
    channel_menu,
    model_center_menu,
    load_balancing_menu,
    logs_menu,
    mapping_menu,
    oauth_menu,
    stats_menu,
    status_alert_menu,
)


EXPECTED_CUSTOM_EMOJI = {
    "openai": "6141162084857031383",
    "claude": "6140995813788099525",
    "anthropic": "6140995813788099525",
    "antigravity": "6077644693984779782",
    "cursor": "6062261319426390107",
    "ollama-cloud": "6138524734419116492",
    "workbuddy": "6120617435214132136",
    "xai": "6138882363460952713",
    "kimi": "6140905172798284383",
    "deepseek": "6138914554240836667",
    "zhipu": "6140727700454645813",
    "minimax": "6141114311935796161",
    "alibaba-bailian": "6138926816372465673",
    "tencent-cloud": "6140662000339918826",
    "jd-cloud": "6138855790498291964",
    "volcengine-ark": "6141018834812806707",
    "baidu-qianfan": "6138964148228204290",
    "xiaomi-mimo": "6138428226503975259",
    "ctyun-xirang": "6138918874977936421",
    "openrouter": "6140767025175209650",
}
CONFIG_CUSTOM_EMOJI = {
    provider: emoji_id for provider, emoji_id in EXPECTED_CUSTOM_EMOJI.items()
    if provider != "anthropic"
}
PROVIDERS = tuple(EXPECTED_CUSTOM_EMOJI)
API_PROVIDERS = tuple(
    provider for provider in PROVIDERS
    if provider not in {"claude", "antigravity", "cursor", "workbuddy", "xai"}
)


def test_exact_custom_emoji_map_defaults_example_ui_and_notifier_are_consistent(monkeypatch):
    example = json.loads(
        (Path(__file__).resolve().parents[2] / "config.example.json").read_text(
            encoding="utf-8",
        ),
    )
    assert ui.PROVIDER_CUSTOM_EMOJI == EXPECTED_CUSTOM_EMOJI
    assert notifier._PROVIDER_CUSTOM_EMOJI == EXPECTED_CUSTOM_EMOJI
    assert config.DEFAULT_CONFIG["telegramUi"]["providerCustomEmoji"] == CONFIG_CUSTOM_EMOJI
    assert example["telegramUi"]["providerCustomEmoji"] == CONFIG_CUSTOM_EMOJI
    assert len(CONFIG_CUSTOM_EMOJI) == 19
    assert {key: ui.PROVIDER_CUSTOM_EMOJI[key] for key in CONFIG_CUSTOM_EMOJI} == CONFIG_CUSTOM_EMOJI
    assert ui.PROVIDER_CUSTOM_EMOJI["anthropic"] == CONFIG_CUSTOM_EMOJI["claude"]
    assert ui.provider_custom_emoji_id("claude") == EXPECTED_CUSTOM_EMOJI["claude"]
    assert ui.provider_custom_emoji_id("anthropic") == EXPECTED_CUSTOM_EMOJI["claude"]

    overrides = {"openai": "override-id", "claude": "legacy-override-id"}
    monkeypatch.setattr(
        ui, "_telegram_ui_provider_table",
        lambda name: overrides if name == "providerCustomEmoji" else {},
    )
    monkeypatch.setattr(
        notifier, "_telegram_ui_provider_table",
        lambda name: overrides if name == "providerCustomEmoji" else {},
    )
    assert ui.provider_custom_emoji_id("openai") == "override-id"
    assert ui.provider_custom_emoji_id("anthropic") == "legacy-override-id"
    assert 'emoji-id="override-id"' in notifier.provider_tag("openai")
    assert 'emoji-id="legacy-override-id"' in notifier.provider_tag("anthropic")

    overrides.clear()
    overrides["anthropic"] = "canonical-override-id"
    assert ui.provider_custom_emoji_id("anthropic") == "canonical-override-id"
    assert 'emoji-id="canonical-override-id"' in notifier.provider_tag("anthropic")


def test_legacy_claude_override_survives_real_default_merge_for_anthropic_alias(monkeypatch):
    merged = config._deep_merge_defaults(config.DEFAULT_CONFIG, {
        "telegramUi": {
            "providerCustomEmoji": {"claude": "persisted-custom-id"},
        },
    })
    table = merged["telegramUi"]["providerCustomEmoji"]
    assert table["claude"] == "persisted-custom-id"
    assert "anthropic" not in table
    monkeypatch.setattr(
        ui, "_telegram_ui_provider_table",
        lambda name: table if name == "providerCustomEmoji" else {},
    )
    monkeypatch.setattr(
        notifier, "_telegram_ui_provider_table",
        lambda name: table if name == "providerCustomEmoji" else {},
    )
    assert ui.provider_custom_emoji_id("anthropic") == "persisted-custom-id"
    assert 'emoji-id="persisted-custom-id"' in ui.provider_tag("anthropic")
    assert 'emoji-id="persisted-custom-id"' in notifier.provider_tag("anthropic")


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


def test_workbuddy_custom_icon_is_consistent_in_defaults_messages_and_buttons(monkeypatch):
    expected = "6120617435214132136"
    assert config.DEFAULT_CONFIG["telegramUi"]["providerCustomEmoji"]["workbuddy"] == expected
    monkeypatch.setattr(ui, "_telegram_ui_provider_table", lambda name: {})
    monkeypatch.setattr(notifier, "_telegram_ui_provider_table", lambda name: {})
    assert ui.provider_custom_emoji_id("workbuddy") == expected
    assert ui.provider_custom_emoji_html("workbuddy") == f'<tg-emoji emoji-id="{expected}">✉</tg-emoji>'
    assert notifier.provider_tag("workbuddy") == ui.provider_tag("workbuddy")
    assert ui.provider_button("WorkBuddy", "oa:wb:login", "workbuddy")["icon_custom_emoji_id"] == expected
    assert "WorkBuddy" not in ui.family_label("openai")


def test_api_channel_provider_helper_uses_exact_registry_provider_without_guessing(monkeypatch):
    calls = []
    channels = {
        "api:known": SimpleNamespace(provider_id="deepseek"),
        "api:custom": SimpleNamespace(provider_id="iflytek"),
        "api:unset": SimpleNamespace(provider_id=None),
    }

    def get_channel(key):
        calls.append(key)
        return channels.get(key)

    monkeypatch.setattr(registry, "get_channel", get_channel)
    assert ui.channel_provider("api:known") == "deepseek"
    assert ui.channel_provider_custom_emoji_id("api:known") == EXPECTED_CUSTOM_EMOJI["deepseek"]
    assert EXPECTED_CUSTOM_EMOJI["deepseek"] in ui.channel_provider_custom_emoji_html("api:known")
    assert ui.channel_provider("api:custom") == "iflytek"
    assert ui.channel_provider_custom_emoji_id("api:custom") == ""
    assert ui.channel_provider("api:unset") == ""
    assert ui.channel_provider("api:missing") == ""
    assert calls == [
        "api:known", "api:known", "api:known", "api:custom", "api:custom",
        "api:unset", "api:missing",
    ]


def test_all_api_brand_icons_render_in_channel_list_detail_and_model_sources(monkeypatch):
    channels = [SimpleNamespace(
        id=f"api:brand-{provider}", key=f"api:brand-{provider}", type="api",
        display_name=f"brand-{provider}", provider_id=provider,
        protocol="openai-chat", enabled=True, disabled_reason=None,
        base_url="https://example.invalid", api_path=None,
        api_key_masked_hint="****test", cc_mimicry=False,
        omit_temperature=False, omit_thinking=False, max_concurrent=0,
        models=(), affinity_count=0,
        provider_usage=SimpleNamespace(supported=False),
    ) for provider in API_PROVIDERS]
    unknown = SimpleNamespace(**{
        **channels[0].__dict__, "id": "api:brand-iflytek", "key": "api:brand-iflytek",
        "display_name": "brand-iflytek", "provider_id": "iflytek",
    })
    channels.append(unknown)

    monkeypatch.setattr(channel_menu, "_all_channels", lambda: channels)
    monkeypatch.setattr(channel_menu, "_channel_health", lambda _channel: ("✅", "可用"))
    monkeypatch.setattr(channel_menu, "_channel_monthly_lines", lambda *_args: ["📈 本地统计"])
    monkeypatch.setattr(channel_menu, "_usage_summary", lambda _channel: None)
    monkeypatch.setattr(channel_menu, "_usage_detail_lines", lambda _channel: [])
    monkeypatch.setattr(channel_menu, "_channel_model_lines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(channel_menu, "_compat_feature_status", lambda *_args: "关闭")
    monkeypatch.setattr(model_center_menu, "source_callback", lambda *_args, **_kwargs: "mc:test")

    rendered_by_name = {}
    page_count = (len(channels) + channel_menu._PAGE_SIZE - 1) // channel_menu._PAGE_SIZE
    for page in range(1, page_count + 1):
        text, keyboard = channel_menu._list_text_and_kb(
            page=page, snapshot={"by_channel": {}},
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        for channel in channels[(page - 1) * channel_menu._PAGE_SIZE:page * channel_menu._PAGE_SIZE]:
            button = next(item for item in buttons if channel.display_name in item["text"])
            rendered_by_name[channel.display_name] = (text, button)

    for provider in API_PROVIDERS:
        text, button = rendered_by_name[f"brand-{provider}"]
        expected = EXPECTED_CUSTOM_EMOJI[provider]
        assert expected in text
        assert button["icon_custom_emoji_id"] == expected
        assert "✅" in button["text"]
    unknown_text, unknown_button = rendered_by_name["brand-iflytek"]
    assert "icon_custom_emoji_id" not in unknown_button
    unknown_line = next(line for line in unknown_text.splitlines() if "brand-iflytek" in line)
    assert "tg-emoji" not in unknown_line and "✅" in unknown_line

    current = [channels[0]]
    monkeypatch.setattr(channel_menu, "_get_channel", lambda *_args: current[0])
    for channel in channels:
        current[0] = channel
        text, keyboard = channel_menu._detail_text_and_kb(
            channel.display_name, chat_id=7, model_stats=[],
        )
        manage = keyboard["inline_keyboard"][0][0]
        expected = EXPECTED_CUSTOM_EMOJI.get(channel.provider_id)
        if expected:
            assert expected in text
            assert manage["icon_custom_emoji_id"] == expected
        else:
            assert "tg-emoji" not in text.splitlines()[0]
            assert "icon_custom_emoji_id" not in manage
        assert "✅" in text.splitlines()[0]
        assert manage["text"] == "管理模型" and manage["callback_data"] == "mc:test"

    oauth = SimpleNamespace(list_accounts=lambda *_args, **_kwargs: SimpleNamespace(items=()))
    source_channels = SimpleNamespace(list_all=lambda *_args, **_kwargs: tuple(channels))
    monkeypatch.setattr(
        model_center_menu, "_CONTROL",
        SimpleNamespace(
            oauth=oauth, channels=source_channels,
            bind_telegram_actor=lambda chat_id: SimpleNamespace(chat_id=chat_id),
        ),
    )
    model_center_menu.reset_for_tests()
    try:
        _text, keyboard = model_center_menu._source_picker_render(7)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        for channel in channels:
            button = next(item for item in buttons if channel.display_name in item["text"])
            expected = EXPECTED_CUSTOM_EMOJI.get(channel.provider_id)
            if expected:
                assert button["icon_custom_emoji_id"] == expected
            else:
                assert "icon_custom_emoji_id" not in button
    finally:
        model_center_menu.reset_for_tests()


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
        "oa:wb:login": "workbuddy",
        "oa:wb:login:global": "workbuddy",
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
        for provider in ("claude", "openai", "xai", "cursor", "antigravity", "workbuddy"):
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
