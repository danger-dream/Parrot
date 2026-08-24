"""Telegram Grok Imagine 设置与媒体权限 UI 测试。"""

from __future__ import annotations

# 测试隔离：配置、状态和日志都放到临时目录，不触碰正式 config.json。
import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(
    0,
    _ap_os.path.dirname(
        _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))
    ),
)
from src.tests import _isolation

_isolation.isolate()


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def by(self, method: str) -> list[dict]:
        return [data for name, data in self.calls if name == method]

    def last(self, method: str) -> dict | None:
        items = self.by(method)
        return items[-1] if items else None

    def clear(self) -> None:
        self.calls.clear()


def _import_modules():
    from src import config, log_db, state_db
    from src.channel import registry
    from src.telegram import bot, menu_cache, states, ui
    from src.telegram.menus import apikey_menu, oauth_menu, xai_imagine_menu

    return {
        "config": config,
        "log_db": log_db,
        "state_db": state_db,
        "registry": registry,
        "bot": bot,
        "menu_cache": menu_cache,
        "states": states,
        "ui": ui,
        "apikey_menu": apikey_menu,
        "oauth_menu": oauth_menu,
        "xai_imagine_menu": xai_imagine_menu,
    }


def _setup(m) -> ApiRecorder:
    m["state_db"].init()
    m["log_db"].init()
    m["states"].clear_all()
    m["ui"].configure("TOKEN", [42])

    def _reset(cfg: dict) -> None:
        cfg["apiKeys"] = {
            "media-client": {
                "key": "ccp-media-client",
                "enabled": True,
                "allowedModels": ["grok-4.5"],
                "allowImages": True,
                "allowVideos": False,
            },
        }
        cfg.setdefault("images", {})["enabled"] = True
        cfg.setdefault("xaiOAuth", {}).update({
            "defaultModels": ["grok-4.5"],
            "imageModels": [
                "grok-imagine-image",
                "grok-imagine-image-quality",
            ],
            "videoModels": [
                "grok-imagine-video",
                "grok-imagine-video-1.5",
            ],
            "videoJobTtlSeconds": 10800,
            "mediaRequestTimeoutSeconds": 180,
        })

    m["config"].update(_reset)
    since = m["menu_cache"].month_start_ts()
    m["menu_cache"].PERIOD_STATS.store(("period", int(since)), {})
    recorder = ApiRecorder()
    m["ui"].api = recorder
    return recorder


def _buttons(message: dict) -> list[dict]:
    return [
        button
        for row in message["reply_markup"]["inline_keyboard"]
        for button in row
    ]


def test_oauth_settings_exposes_distinct_gpt_and_grok_media_entries(m):
    recorder = _setup(m)

    m["oauth_menu"].on_settings(42, 100, "cb-settings")
    message = recorder.last("editMessageText")
    assert message is not None
    assert "默认模型" in message["text"]
    assert "🎨 <b>媒体能力</b>" in message["text"]
    assert "Grok Imagine: 图片 <b>2</b> · 视频 <b>2</b>" in message["text"]

    callbacks = {button["text"]: button["callback_data"] for button in _buttons(message)}
    assert callbacks["GPT 图片"] == "img:show"
    assert callbacks["Grok 图片"] == "xim:show"


def test_grok_imagine_menu_renders_and_persists_existing_config_fields(m):
    recorder = _setup(m)
    menu = m["xai_imagine_menu"]

    menu.show(42, 100, "cb-show")
    message = recorder.last("editMessageText")
    assert message is not None
    assert "🎨 <b>Grok Imagine 设置</b>" in message["text"]
    assert "<b>🖼 图片模型</b> (2)" in message["text"]
    assert "<b>🎬 视频模型</b> (2)" in message["text"]
    assert "视频任务绑定: <code>3 小时</code>（10800s）" in message["text"]
    assert "媒体请求超时: <code>3 分钟</code>（180s）" in message["text"]
    assert "统一统计、费用和任务详情" in message["text"]
    callbacks = {button["callback_data"] for button in _buttons(message)}
    assert {
        "xim:edit:image",
        "xim:edit:video",
        "xim:edit:ttl",
        "xim:edit:timeout",
        "media:logs",
        "oa:settings",
        "menu:main",
    } <= callbacks

    menu.handle_callback(42, 100, "cb-image", "xim:edit:image")
    assert m["states"].get_state(42)["action"] == "xim_edit_image_models"
    assert menu.handle_text_state(
        42,
        "xim_edit_image_models",
        "grok-imagine-image-new, grok-imagine-image-quality\n"
        "grok-imagine-image-new",
    ) is True
    assert m["config"].get()["xaiOAuth"]["imageModels"] == [
        "grok-imagine-image-new",
        "grok-imagine-image-quality",
    ]
    assert m["states"].get_state(42) is None

    menu.handle_callback(42, 100, "cb-video", "xim:edit:video")
    menu.handle_text_state(42, "xim_edit_video_models", "-")
    assert m["config"].get()["xaiOAuth"]["videoModels"] == []

    menu.handle_callback(42, 100, "cb-ttl", "xim:edit:ttl")
    menu.handle_text_state(42, "xim_edit_job_ttl", "4h")
    assert m["config"].get()["xaiOAuth"]["videoJobTtlSeconds"] == 14400

    menu.handle_callback(42, 100, "cb-timeout", "xim:edit:timeout")
    menu.handle_text_state(42, "xim_edit_request_timeout", "240")
    assert m["config"].get()["xaiOAuth"]["mediaRequestTimeoutSeconds"] == 240


def test_bot_routes_grok_imagine_callbacks_and_text_state(m):
    recorder = _setup(m)
    bot = m["bot"]

    bot._handle_callback({
        "id": "cb-show",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "xim:show",
    })
    assert "Grok Imagine 设置" in recorder.last("editMessageText")["text"]

    bot._handle_callback({
        "id": "cb-timeout",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "xim:edit:timeout",
    })
    assert m["states"].get_state(42)["action"] == "xim_edit_request_timeout"

    bot._handle_message({"chat": {"id": 42}, "text": "5m"})
    assert m["config"].get()["xaiOAuth"]["mediaRequestTimeoutSeconds"] == 300
    assert m["states"].get_state(42) is None


def test_apikey_video_toggle_and_media_models_in_whitelist(m):
    recorder = _setup(m)
    menu = m["apikey_menu"]
    registry = m["registry"]
    original_available_models = registry.available_models
    registry.available_models = lambda: ["grok-4.5"]
    try:
        short = menu._short_of("media-client")
        menu.on_view(42, 100, "cb-view", short)
        detail = recorder.last("editMessageText")
        assert "🖼 图片接口: <code>允许</code>" in detail["text"]
        assert "🎬 视频接口: <code>禁止（默认）</code>" in detail["text"]
        callbacks = [button["callback_data"] for button in _buttons(detail)]
        video_callback = next(value for value in callbacks if value.startswith("ak:vid:"))

        assert menu.handle_callback(42, 100, "cb-video", video_callback) is True
        entry = m["config"].get()["apiKeys"]["media-client"]
        assert entry["allowVideos"] is True
        assert entry["allowImages"] is True
        toggled = recorder.last("editMessageText")
        assert "🎬 视频接口: <code>允许</code>" in toggled["text"]

        menu.on_perm_enter(42, 100, "cb-perm", short)
        state = m["states"].get_state(42)
        assert state["action"] == "ak_perm_editing"
        models = state["data"]["models"]
        assert models == [
            "grok-4.5",
            "grok-imagine-image",
            "grok-imagine-image-quality",
            "grok-imagine-video",
            "grok-imagine-video-1.5",
        ]
        perm = recorder.last("editMessageText")
        labels = [button["text"] for button in _buttons(perm)]
        assert "☐ 🖼 grok-imagine-image" in labels
        assert "☐ 🎬 grok-imagine-video" in labels

        image_idx = models.index("grok-imagine-image")
        video_idx = models.index("grok-imagine-video")
        menu.on_perm_toggle(42, 100, "cb-img-model", short, str(image_idx))
        menu.on_perm_toggle(42, 100, "cb-video-model", short, str(video_idx))
        menu.on_perm_save(42, 100, "cb-save", short)
        allowed = set(m["config"].get()["apiKeys"]["media-client"]["allowedModels"])
        assert allowed == {
            "grok-4.5",
            "grok-imagine-image",
            "grok-imagine-video",
        }
    finally:
        registry.available_models = original_available_models
