"""统一渠道默认 + 模型专属 priority 调度与 Telegram 编辑器测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import affinity, concurrency, config, load_balancing, scheduler, state_db
    from src.channel import registry
    from src.openai.channel.registration import register_factories
    from src.telegram import states, ui
    from src.telegram.menus import load_balancing_menu
    return {
        "affinity": affinity,
        "concurrency": concurrency,
        "config": config,
        "load_balancing": load_balancing,
        "scheduler": scheduler,
        "state_db": state_db,
        "registry": registry,
        "register_factories": register_factories,
        "states": states,
        "ui": ui,
        "lb_menu": load_balancing_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def by(self, method):
        return [data for called, data in self.calls if called == method]

    def last(self, method):
        values = self.by(method)
        return values[-1] if values else None

    def clear(self):
        self.calls.clear()


def _install_recorder(m):
    recorder = ApiRecorder()
    m["ui"].api = recorder
    return recorder


def _setup(m):
    m["state_db"].init()
    m["states"].clear_all()
    m["register_factories"]()

    def mutate(cfg):
        cfg["oauthAccounts"] = []
        cfg["channels"] = []
        cfg["channelSelection"] = "smart"
        cfg["loadBalancing"] = {
            "initialized": False,
            "channelPriorityOrder": [],
            "modelPriorityOrders": {},
            "priorityOrders": {"anthropic": [], "openai": []},
        }
        cfg.setdefault("scoring", {})["explorationRate"] = 0.0

    m["config"].update(mutate)
    m["registry"].rebuild_from_config()
    m["affinity"]._initialized = False
    m["affinity"]._client_initialized = False
    m["affinity"].init()
    m["affinity"].client_init()
    m["concurrency"]._slots.clear()


def _add_api(m, name, models=("m",), protocol="anthropic"):
    m["registry"].add_api_channel({
        "name": name,
        "baseUrl": "https://example.com",
        "apiKey": "sk-testkey12345",
        "protocol": protocol,
        "models": [{"real": model, "alias": model} for model in models],
        "enabled": True,
    })
    m["registry"].rebuild_from_config()


def _candidate_keys(result):
    return [channel.key for channel, _model in result.candidates]


def _schedule(m, model, ip="1.1.1.1"):
    return m["scheduler"].schedule(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]},
        "key",
        ip,
    )


def test_model_priority_overrides_unified_channel_priority_and_affinity_still_wins(m):
    _setup(m)
    _add_api(m, "anth-a", models=("m", "n"), protocol="anthropic")
    _add_api(m, "open-b", models=("m", "n"), protocol="openai-chat")
    _add_api(m, "anth-c", models=("m", "n"), protocol="anthropic")

    m["load_balancing"].save_channel_order([
        "api:anth-a", "api:open-b", "api:anth-c",
    ])
    m["load_balancing"].save_model_order("m", [
        "api:anth-c", "api:open-b", "api:anth-a",
    ])
    m["config"].update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))

    assert _candidate_keys(_schedule(m, "m")) == [
        "api:anth-c", "api:open-b", "api:anth-a",
    ]
    assert _candidate_keys(_schedule(m, "n")) == [
        "api:anth-a", "api:open-b", "api:anth-c",
    ]

    # Model order is the priority layer; affinity remains the separate final layer.
    client_key = m["affinity"].make_client_key("key", "1.1.1.1", "m")
    m["affinity"].client_upsert(client_key, "api:open-b", "m")
    result = _schedule(m, "m")
    assert _candidate_keys(result) == [
        "api:open-b", "api:anth-c", "api:anth-a",
    ]
    assert result.affinity_hit is True


def test_unlisted_model_channel_appends_using_unified_default(m):
    _setup(m)
    _add_api(m, "a", models=("m",), protocol="anthropic")
    _add_api(m, "b", models=("m",), protocol="openai-chat")
    _add_api(m, "c", models=("m",), protocol="anthropic")
    m["load_balancing"].save_channel_order(["api:c", "api:b", "api:a"])
    m["load_balancing"].save_model_order("m", ["api:a"])
    m["config"].update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))

    # Explicit model item first; missing candidates follow unified channel order.
    assert _candidate_keys(_schedule(m, "m")) == [
        "api:a", "api:c", "api:b",
    ]


def test_legacy_family_orders_migrate_to_one_stable_channel_order(m):
    _setup(m)
    _add_api(m, "a1", protocol="anthropic")
    _add_api(m, "o1", protocol="openai-chat")
    _add_api(m, "a2", protocol="anthropic")
    _add_api(m, "o2", protocol="openai-chat")
    m["load_balancing"].save_family_order("anthropic", ["api:a2", "api:a1"])
    m["load_balancing"].save_family_order("openai", ["api:o2", "api:o1"])

    migrated = m["load_balancing"].initialize_priority_orders()
    # Rank 0 from both families, then rank 1; registry order breaks equal ranks.
    assert migrated == ["api:a2", "api:o2", "api:a1", "api:o1"]
    cfg = m["config"].get()["loadBalancing"]
    assert cfg["channelPriorityOrder"] == migrated
    assert cfg["priorityOrders"]["anthropic"] == ["api:a2", "api:a1"]
    assert cfg["priorityOrders"]["openai"] == ["api:o2", "api:o1"]


def test_priority_main_menu_uses_model_and_channel_axes_not_families(m):
    _setup(m)
    m["config"].update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))
    text, keyboard = m["lb_menu"]._main_text_and_kb()
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    callbacks = {button.get("callback_data") for button in buttons}
    labels = {button.get("text") for button in buttons}

    assert "lb:models:1" in callbacks
    assert "lb:channels" in callbacks
    assert "🤖 按模型调整优先级" in labels
    assert "🔀 按渠道/账户调整优先级" in labels
    assert not any(str(value).startswith("lb:fam:") for value in callbacks)
    assert "Anthropic 协议" not in text
    assert "OpenAI、Grok、Cursor 协议" not in text
    assert "模型专属顺序 &gt; 统一渠道/账户顺序" in text


def test_unified_channel_editor_keeps_existing_move_reset_and_save_controls(m):
    _setup(m)
    recorder = _install_recorder(m)
    _add_api(m, "anth", protocol="anthropic")
    _add_api(m, "open", protocol="openai-chat")

    m["lb_menu"]._start_channels(42, 100, "cb")
    edit = recorder.last("editMessageText")
    assert edit and "按渠道/账户调整优先级" in edit["text"]
    assert "api:anth" not in edit["text"]  # human display names, not internal keys
    labels = [
        button["text"]
        for row in edit["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    for expected in ("🔝 置顶", "🔚 置底", "⬆ 上移", "⬇ 下移", "还原", "保存设置"):
        assert expected in labels

    m["lb_menu"]._toggle_select(42, 100, "cb", "2")
    m["lb_menu"]._move(42, 100, "cb", "top")
    state = m["states"].get_state(42)
    assert state["data"]["draft"][0] == "api:open"
    m["lb_menu"]._reset(42, 100, "cb")
    state = m["states"].get_state(42)
    assert state["data"]["draft"] == ["api:anth", "api:open"]

    m["lb_menu"]._toggle_select(42, 100, "cb", "2")
    m["lb_menu"]._move(42, 100, "cb", "top")
    m["lb_menu"]._save(42, 100, "cb")
    assert m["config"].get()["loadBalancing"]["channelPriorityOrder"] == [
        "api:open", "api:anth",
    ]


def test_model_list_is_six_per_page_and_displays_effective_orders(m):
    _setup(m)
    for index in range(7):
        _add_api(
            m,
            f"ch{index}",
            models=(f"model-{index}", "shared"),
            protocol="anthropic" if index % 2 == 0 else "openai-chat",
        )
    m["load_balancing"].save_channel_order([
        f"api:ch{index}" for index in reversed(range(7))
    ])

    text, keyboard = m["lb_menu"]._models_text_and_kb(1)
    assert "共 <b>8</b> 个模型 · 第 <b>1/2</b> 页" in text
    assert text.count("渠道（默认）：") == 6
    details = [
        button for row in keyboard["inline_keyboard"] for button in row
        if str(button.get("callback_data") or "").startswith("lb:model:")
    ]
    assert [button["text"] for button in details] == [
        "📄 #1", "📄 #2", "📄 #3", "📄 #4", "📄 #5", "📄 #6",
    ]
    assert any(
        button.get("callback_data") == "lb:model_bulk"
        for row in keyboard["inline_keyboard"] for button in row
    )


def test_batch_model_selection_has_no_pagination_and_save_filters_union(m):
    _setup(m)
    recorder = _install_recorder(m)
    _add_api(m, "both", models=("m1", "m2"), protocol="anthropic")
    _add_api(m, "only1", models=("m1",), protocol="openai-chat")
    _add_api(m, "only2", models=("m2",), protocol="anthropic")
    for index in range(6):
        _add_api(m, f"extra{index}", models=(f"x{index}",), protocol="anthropic")
    assert len(m["lb_menu"]._client_models()) == 8

    m["lb_menu"]._start_model_bulk(42, 100, "cb")
    edit = recorder.last("editMessageText")
    assert edit and "本页一次展示全部模型，不分页" in edit["text"]
    model_buttons = [
        button for row in edit["reply_markup"]["inline_keyboard"] for button in row
        if str(button.get("callback_data") or "").startswith("lb:model_pick:")
    ]
    assert len(model_buttons) == 8
    assert not any(
        "page" in str(button.get("callback_data") or "")
        for button in model_buttons
    )

    for model in ("m1", "m2"):
        m["lb_menu"]._toggle_bulk_model(
            42, 100, "cb", m["lb_menu"]._model_code(model),
        )
    m["lb_menu"]._confirm_model_bulk(42, 100, "cb")
    state = m["states"].get_state(42)
    assert state["action"] == "lb_edit"
    assert state["data"]["kind"] == "models_batch"
    assert set(state["data"]["draft"]) == {
        "api:both", "api:only1", "api:only2",
    }

    # User's union draft can contain channels unsupported by one selected model.
    data = state["data"]
    data["draft"] = ["api:only2", "api:only1", "api:both"]
    m["lb_menu"]._store_edit_state(42, data)
    m["lb_menu"]._save(42, 100, "cb")
    orders = m["config"].get()["loadBalancing"]["modelPriorityOrders"]
    assert orders["m1"] == ["api:only1", "api:both"]
    assert orders["m2"] == ["api:only2", "api:both"]


def test_single_model_clear_restores_unified_default(m):
    _setup(m)
    recorder = _install_recorder(m)
    _add_api(m, "a", models=("m",), protocol="anthropic")
    _add_api(m, "b", models=("m",), protocol="openai-chat")
    m["load_balancing"].save_channel_order(["api:a", "api:b"])
    m["load_balancing"].save_model_order("m", ["api:b", "api:a"])

    m["lb_menu"]._start_model(
        42, 100, "cb", m["lb_menu"]._model_code("m"), 1,
    )
    assert m["states"].get_state(42)["data"]["draft"] == ["api:b", "api:a"]
    m["lb_menu"]._clear_single_model(42, 100, "cb")
    assert not m["load_balancing"].has_model_priority("m")
    assert m["states"].get_state(42)["data"]["draft"] == ["api:a", "api:b"]
    assert "统一渠道/账户顺序" in recorder.last("editMessageText")["text"]


def test_channel_remove_and_rename_sync_unified_and_all_model_orders(m):
    _setup(m)
    _add_api(m, "a", models=("m1", "m2"), protocol="anthropic")
    _add_api(m, "b", models=("m1", "m2"), protocol="openai-chat")
    m["load_balancing"].save_channel_order(["api:b", "api:a"])
    m["load_balancing"].save_model_orders({
        "m1": ["api:a", "api:b"],
        "m2": ["api:b", "api:a"],
    })

    m["registry"].update_api_channel("b", {"name": "renamed"})
    lb = m["config"].get()["loadBalancing"]
    assert lb["channelPriorityOrder"] == ["api:renamed", "api:a"]
    assert lb["modelPriorityOrders"]["m1"] == ["api:a", "api:renamed"]
    assert lb["modelPriorityOrders"]["m2"] == ["api:renamed", "api:a"]

    m["registry"].delete_api_channel("renamed")
    lb = m["config"].get()["loadBalancing"]
    assert lb["channelPriorityOrder"] == ["api:a"]
    assert lb["modelPriorityOrders"]["m1"] == ["api:a"]
    assert lb["modelPriorityOrders"]["m2"] == ["api:a"]


def test_priority_sorts_available_and_saturated_separately(m):
    _setup(m)
    _add_api(m, "a")
    _add_api(m, "b")
    m["registry"].update_api_channel("a", {"maxConcurrent": 1})
    m["load_balancing"].save_channel_order(["api:a", "api:b"])
    m["config"].update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))

    assert asyncio.run(m["concurrency"].try_acquire("api:a")) is True
    try:
        result = _schedule(m, "m")
        assert _candidate_keys(result) == ["api:b"]
        assert [channel.key for channel, _model in result.saturated] == ["api:a"]
    finally:
        m["concurrency"].release("api:a")


def test_model_codes_survive_restart(m):
    _setup(m)
    _add_api(m, "a", models=("m",))
    code = m["lb_menu"]._model_code("m")
    with m["ui"]._code_lock:
        m["ui"]._code_to_name.clear()
    assert m["lb_menu"]._resolve_model_code(code) == "m"


def test_complete_order_input_updates_draft_without_saving(m):
    _setup(m)
    recorder = _install_recorder(m)
    _add_api(m, "a")
    _add_api(m, "b")
    m["lb_menu"]._start_channels(42, 100, "cb")
    m["lb_menu"]._order_input_start(42, 100, "cb")
    assert m["lb_menu"].handle_text_state(42, "lb_order_input", "2,1")
    state = m["states"].get_state(42)
    assert state["action"] == "lb_edit"
    assert state["data"]["draft"] == ["api:b", "api:a"]
    assert not m["config"].get()["loadBalancing"]["channelPriorityOrder"]
    assert "尚未保存" in recorder.last("sendMessage")["text"]


def main():
    modules = _import_modules()
    original = json.loads(json.dumps(modules["config"].get()))
    tests = [
        test_model_priority_overrides_unified_channel_priority_and_affinity_still_wins,
        test_unlisted_model_channel_appends_using_unified_default,
        test_legacy_family_orders_migrate_to_one_stable_channel_order,
        test_priority_main_menu_uses_model_and_channel_axes_not_families,
        test_unified_channel_editor_keeps_existing_move_reset_and_save_controls,
        test_model_list_is_six_per_page_and_displays_effective_orders,
        test_batch_model_selection_has_no_pagination_and_save_filters_union,
        test_single_model_clear_restores_unified_default,
        test_channel_remove_and_rename_sync_unified_and_all_model_orders,
        test_priority_sorts_available_and_saturated_separately,
        test_model_codes_survive_restart,
        test_complete_order_input_updates_draft_without_saving,
    ]
    passed = 0
    try:
        for test in tests:
            try:
                test(modules)
                passed += 1
            except Exception as exc:
                print(f"  [FAIL] {test.__name__}: {exc}")
                import traceback
                traceback.print_exc()
    finally:
        modules["config"].update(lambda cfg: (cfg.clear(), cfg.update(original)))
        modules["states"].clear_all()
    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
