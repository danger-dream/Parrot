"""API Key 自定义密钥流程测试。"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import os
import sys


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, log_db, state_db
    from src.telegram import states, ui
    from src.telegram.menus import apikey_menu
    return {
        "config": config, "log_db": log_db, "state_db": state_db,
        "states": states, "ui": ui, "apikey_menu": apikey_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def by(self, method: str) -> list[dict]:
        return [d for m, d in self.calls if m == method]

    def last(self, method: str) -> dict | None:
        items = self.by(method)
        return items[-1] if items else None

    def clear(self) -> None:
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["config"].update(lambda c: c.__setitem__("apiKeys", {}))
    m["states"].clear_all()


def _install_recorder(m) -> ApiRecorder:
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _button_callbacks(msg: dict) -> list[str]:
    return [
        b["callback_data"]
        for row in msg["reply_markup"]["inline_keyboard"]
        for b in row
        if "callback_data" in b
    ]


def test_validate_custom_key(m):
    ak = m["apikey_menu"]

    assert ak._validate_custom_key("Abc-_.~+/=123", []) is None
    assert "太短" in ak._validate_custom_key("1234567", [])
    assert "太长" in ak._validate_custom_key("a" * 257, [])
    assert "非法字符" in ak._validate_custom_key("abc defghi", [])
    assert "非法字符" in ak._validate_custom_key("abcdefg\nh", [])
    assert "已被其他 key 使用" in ak._validate_custom_key("custom-key-1", ["custom-key-1"])


def test_add_custom_key_flow_writes_config(m):
    _setup(m)
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    states = m["states"]

    ak.on_add(42, 100, "cb-add")
    assert states.get_state(42)["action"] == "ak_add_name"

    ak.on_add_name_input(42, "client-a")
    state = states.get_state(42)
    assert state["action"] == "ak_add_key_input"
    assert state["data"]["name"] == "client-a"
    assert "client-a" not in m["config"].get()["apiKeys"]

    choice = rec.last("sendMessage")
    callbacks = _button_callbacks(choice)
    custom_cb = [x for x in callbacks if x.startswith("ak:add_custom:")][0]

    ak.handle_callback(42, 100, "cb-custom", custom_cb)
    edit = rec.last("editMessageText")
    assert "自定义 key 密钥" in edit["text"]

    ak.on_add_key_input(42, "sk-custom_123+/=")
    keys = m["config"].get()["apiKeys"]
    assert keys["client-a"]["key"] == "sk-custom_123+/="
    assert keys["client-a"]["allowedModels"] == []
    assert keys["client-a"]["allowImages"] is False
    assert keys["client-a"]["allowVideos"] is False
    assert states.get_state(42) is None
    assert "sk-custom_123+/=" in rec.last("sendMessage")["text"]


def test_add_custom_rejects_duplicate_and_keeps_state(m):
    _setup(m)
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    m["config"].update(lambda c: c.__setitem__("apiKeys", {
        "existing": {"key": "already-used-key", "allowedModels": []},
    }))
    m["states"].set_state(42, "ak_add_key_input", {"name": "new-client"})

    ak.on_add_key_input(42, "already-used-key")

    assert "new-client" not in m["config"].get()["apiKeys"]
    assert m["states"].get_state(42)["action"] == "ak_add_key_input"
    assert "已被其他 key 使用" in rec.last("sendMessage")["text"]


def test_rekey_custom_flow_writes_config(m):
    _setup(m)
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    m["config"].update(lambda c: c.__setitem__("apiKeys", {
        "alpha": {
            "key": "old-alpha-key",
            "allowedModels": ["m1"],
            "allowImages": True,
            "allowVideos": True,
        },
        "beta": {"key": "beta-secret", "allowedModels": []},
    }))

    short = ak._short_of("alpha")
    ak.on_rekey_enter(42, 100, "cb-rekey", short)
    assert m["states"].get_state(42)["action"] == "ak_rekey_input"
    assert "自定义新 key" in rec.last("editMessageText")["text"]

    ak.on_rekey_input(42, "new-alpha_key+/=1")
    entry = m["config"].get()["apiKeys"]["alpha"]
    assert entry["key"] == "new-alpha_key+/=1"
    assert entry["allowedModels"] == ["m1"]
    assert entry["allowImages"] is True
    assert entry["allowVideos"] is True
    assert m["states"].get_state(42) is None
    result = rec.last("sendMessage")["text"]
    assert "new-alpha_key+/=1" in result
    assert "下游客户端需要更新" in result


def test_rekey_rejects_other_key_duplicate(m):
    _setup(m)
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    m["config"].update(lambda c: c.__setitem__("apiKeys", {
        "alpha": "old-alpha-key",
        "beta": {"key": "beta-secret", "allowedModels": []},
    }))
    m["states"].set_state(42, "ak_rekey_input", {"name": "alpha", "short": ak._short_of("alpha")})

    ak.on_rekey_input(42, "beta-secret")

    assert m["config"].get()["apiKeys"]["alpha"] == "old-alpha-key"
    assert m["states"].get_state(42)["action"] == "ak_rekey_input"
    assert "已被其他 key 使用" in rec.last("sendMessage")["text"]
