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
        for key in ("image_models", "video_models", "videos"):
            cfg.pop(key, None)
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


def test_oauth_settings_does_not_duplicate_model_center_media_entries(m):
    recorder = _setup(m)

    m["oauth_menu"].on_settings(42, 100, "cb-settings")
    message = recorder.last("editMessageText")
    assert message is not None
    assert "模型目录与媒体设置已统一归位到模型中心" in message["text"]
    assert "备用模型" not in message["text"]
    assert "默认模型" not in message["text"]
    assert "🎨 <b>媒体能力</b>" not in message["text"]
    assert "Grok Imagine:" not in message["text"]

    callbacks = {button["text"]: button["callback_data"] for button in _buttons(message)}
    assert "GPT 图片" not in callbacks and "Grok 图片" not in callbacks
    assert callbacks["📈 配额监控"] == "oa:quota"
    assert callbacks["◀ 返回OAuth账户"] == "menu:oauth"


def test_grok_imagine_legacy_menu_redirects_to_independent_panel_without_writes(m):
    from copy import deepcopy
    recorder = _setup(m)
    menu = m['xai_imagine_menu']
    before = deepcopy(m['config'].get())
    menu.show(42, 100, 'cb-show')
    text = recorder.last('editMessageText')['text']
    assert '模型中心 · 视频' in text and '任务 TTL：10800 秒' in text and '请求超时：180 秒' in text
    for callback, action in [('xim:edit:image', 'xim_edit_image_models'), ('xim:edit:video', 'xim_edit_video_models'),
                             ('xim:edit:ttl', 'xim_edit_job_ttl'), ('xim:edit:timeout', 'xim_edit_request_timeout')]:
        menu.handle_callback(42, 100, 'old', callback)
        assert m['states'].get_state(42) is None
        m['states'].set_state(42, action)
        assert menu.handle_text_state(42, action, 'must-not-save')
        assert m['states'].get_state(42) is None
    assert m['config'].get() == before


def test_bot_redirects_old_grok_callbacks_without_replaying_text_write(m):
    recorder = _setup(m)
    bot = m["bot"]

    bot._handle_callback({
        "id": "cb-show",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "xim:show",
    })
    assert "模型中心 · 视频" in recorder.last("editMessageText")["text"]

    before = m["config"].get()["xaiOAuth"]["mediaRequestTimeoutSeconds"]
    bot._handle_callback({
        "id": "cb-timeout",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "xim:edit:timeout",
    })
    assert "模型中心 · 视频" in recorder.last("editMessageText")["text"]
    assert m["states"].get_state(42) is None

    # Old write intent is never replayed: a following text message is ordinary input.
    bot._handle_message({"chat": {"id": 42}, "text": "5m"})
    assert m["config"].get()["xaiOAuth"]["mediaRequestTimeoutSeconds"] == before
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
