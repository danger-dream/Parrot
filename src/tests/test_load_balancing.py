"""负载均衡 / priority 调度测试。"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys
from types import SimpleNamespace


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
        return [d for m, d in self.calls if m == method]
    def last(self, method):
        arr = self.by(method)
        return arr[-1] if arr else None
    def clear(self):
        self.calls.clear()


def _install(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _setup(m):
    m["state_db"].init()
    m["states"].clear_all()
    m["register_factories"]()
    def _mut(c):
        c["oauthAccounts"] = []
        c["channels"] = []
        c["channelSelection"] = "smart"
        c["loadBalancing"] = {"initialized": False, "priorityOrders": {"anthropic": [], "openai": []}}
        c.setdefault("scoring", {})["explorationRate"] = 0.0
    m["config"].update(_mut)
    m["registry"].rebuild_from_config()
    m["affinity"]._initialized = False
    m["affinity"]._client_initialized = False
    m["affinity"].init()
    m["affinity"].client_init()
    m["concurrency"]._slots.clear()


def _add_api(m, name, model="m", protocol="anthropic"):
    m["registry"].add_api_channel({
        "name": name,
        "baseUrl": "https://example.com",
        "apiKey": "sk-testkey12345",
        "protocol": protocol,
        "models": [{"real": model, "alias": model}],
        "enabled": True,
    })
    m["registry"].rebuild_from_config()


def _add_oauth(m, email, provider="claude", model="m"):
    from src import oauth_manager
    oauth_manager.add_account({
        "email": email,
        "provider": provider,
        "access_token": "at",
        "refresh_token": "rt",
        "expired": "2099-01-01T00:00:00Z",
        "last_refresh": "2026-01-01T00:00:00Z",
        "type": "claude",
        "enabled": True,
        "disabled_reason": None,
        "models": [model],
    })
    m["registry"].rebuild_from_config()


def test_priority_sort_and_affinity(m):
    _setup(m)
    _add_api(m, "a")
    _add_api(m, "b")
    _add_api(m, "c")
    m["load_balancing"].save_family_order("anthropic", ["api:c", "api:b", "api:a"])
    m["config"].update(lambda c: c.__setitem__("channelSelection", "priority"))

    res = m["scheduler"].schedule({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "k", "1.1.1.1")
    assert [ch.key for ch, _ in res.candidates] == ["api:c", "api:b", "api:a"]

    client_key = m["affinity"].make_client_key("k", "1.1.1.1", "m")
    m["affinity"].client_upsert(client_key, "api:b", "m")
    res2 = m["scheduler"].schedule({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "k", "1.1.1.1")
    assert [ch.key for ch, _ in res2.candidates][:3] == ["api:b", "api:c", "api:a"]
    assert res2.affinity_hit is True
    print("  [PASS] priority order + affinity wins")


def test_normalize_and_init(m):
    _setup(m)
    _add_api(m, "a")
    _add_api(m, "b")
    m["load_balancing"].save_family_order("anthropic", ["api:b"])
    assert m["load_balancing"].normalize_order_for_family("anthropic", ["api:a", "api:b", "api:c"]) == ["api:b", "api:a", "api:c"]
    m["load_balancing"].set_mode("priority")
    cfg = m["config"].get()
    assert cfg["channelSelection"] == "priority"
    assert cfg["loadBalancing"]["initialized"] is True
    print("  [PASS] normalize + priority init")


def test_load_balancing_uses_custom_icons_without_slash_joining(m):
    _setup(m)
    m["config"].update(lambda cfg: cfg.__setitem__("channelSelection", "priority"))
    lb = m["lb_menu"]
    ui = m["ui"]

    _text, keyboard = lb._main_text_and_kb()
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    anthropic = next(button for button in buttons if button.get("callback_data") == "lb:fam:anthropic")
    openai = next(button for button in buttons if button.get("callback_data") == "lb:fam:openai")
    assert anthropic["text"] == "Anthropic 协议"
    assert anthropic["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("claude")
    assert openai["text"] == "OpenAI、Grok、Cursor 协议"
    assert "/" not in openai["text"]
    assert openai["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("openai")

    rich_openai = lb._family_label("openai")
    assert rich_openai == (
        f"{ui.provider_custom_emoji_html('openai')} OpenAI、"
        f"{ui.provider_custom_emoji_html('xai')} Grok、"
        f"{ui.provider_custom_emoji_html('cursor')} Cursor 协议"
    )
    assert "🅾️/𝕏" not in rich_openai
    assert lb._family_label("anthropic") == (
        f"{ui.provider_custom_emoji_html('claude')} Anthropic 协议"
    )

    for provider in ("openai", "xai", "cursor", "claude"):
        channel = SimpleNamespace(type="oauth", key=f"oauth:{provider}:identity")
        assert ui.provider_custom_emoji_id(provider) in lb._channel_icon(channel)


def test_button_rows_and_preview(m):
    _setup(m)
    rec = _install(m)
    for i in range(1, 12):
        _add_api(m, f"ch{i}")
    lb = m["lb_menu"]
    assert lb._split_number_rows(11) == [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11]]

    lb._start_family(42, 100, "cb", "anthropic")
    edit = rec.last("editMessageText")
    assert edit and "Anthropic" in edit["text"]
    # 选中 3 → 按钮显示 "3 ✅"
    rec.clear()
    lb._toggle_select(42, 100, "cb", "3")
    edit = rec.last("editMessageText")
    texts = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert "3 ✅" in texts

    # 批量输入后只预览，不直接保存
    m["states"].set_state(42, "lb_bulk_input", {"family": "anthropic", "draft": [f"api:ch{i}" for i in range(1, 12)]})
    rec.clear()
    lb.handle_text_state(42, "lb_bulk_input", "2,1,3,4,5,6,7,8,9,10,11")
    send = rec.last("sendMessage")
    assert send and "尚未保存" in send["text"]
    st = m["states"].get_state(42)
    assert st["action"] == "lb_edit"
    assert st["data"]["draft"][:3] == ["api:ch2", "api:ch1", "api:ch3"]
    # 批量设置只更新草稿，需点保存设置才写配置
    saved = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert saved != st["data"]["draft"]
    print("  [PASS] smart number rows + selected label + bulk preview")


def test_add_delete_sync_priority(m):
    _setup(m)
    _add_api(m, "api0")
    m["load_balancing"].initialize_priority_orders()

    _add_oauth(m, "u@example.com", provider="claude")
    order = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert "oauth:claude:u@example.com" in order
    from src import oauth_manager
    oauth_manager.delete_account("claude:u@example.com")
    order2 = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert "oauth:claude:u@example.com" not in order2

    _add_api(m, "rename-me")
    order3 = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert order3[-1] == "api:rename-me"
    m["registry"].update_api_channel("rename-me", {"name": "renamed"})
    order4 = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert "api:rename-me" not in order4
    assert "api:renamed" in order4
    m["registry"].delete_api_channel("renamed")
    order5 = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert "api:renamed" not in order5
    print("  [PASS] add/delete/rename sync priorityOrders")


def test_protocol_patch_without_family_change_keeps_priority(m):
    _setup(m)
    _add_api(m, "a")
    _add_api(m, "b")
    m["load_balancing"].save_family_order("anthropic", ["api:b", "api:a"])

    # 菜单保存编辑时可能把当前 protocol 原样带回；family 未变时不能把渠道挪到队尾。
    m["registry"].update_api_channel("b", {
        "protocol": "anthropic",
        "baseUrl": "https://example.org",
    })
    order = m["config"].get()["loadBalancing"]["priorityOrders"]["anthropic"]
    assert order == ["api:b", "api:a"]
    print("  [PASS] unchanged protocol patch keeps priority position")


def test_rename_and_protocol_change_cleans_old_family(m):
    _setup(m)
    _add_api(m, "x", protocol="anthropic")
    _add_api(m, "y", protocol="anthropic")
    m["load_balancing"].initialize_priority_orders()

    m["registry"].update_api_channel("x", {
        "name": "z",
        "protocol": "openai-chat",
        "baseUrl": "https://example.org",
        "apiPath": "/v1/chat/completions",
    })
    orders = m["config"].get()["loadBalancing"]["priorityOrders"]
    assert orders["anthropic"] == ["api:y"]
    assert orders["openai"] == ["api:z"]
    print("  [PASS] rename + protocol change cleans cross-family priorityOrders")


def test_priority_sorts_available_and_saturated_separately(m):
    _setup(m)
    _add_api(m, "a")
    _add_api(m, "b")
    m["registry"].update_api_channel("a", {"maxConcurrent": 1})
    m["load_balancing"].save_family_order("anthropic", ["api:a", "api:b"])
    m["config"].update(lambda c: c.__setitem__("channelSelection", "priority"))

    # 当前策略：priority 分别排序 available / saturated；高优先级满并发时，
    # 低优先级空闲渠道先进入 candidates，高优先级作为 saturated 排队备选。
    assert asyncio.run(m["concurrency"].try_acquire("api:a")) is True
    try:
        res = m["scheduler"].schedule({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "k", "1.1.1.1")
        assert [ch.key for ch, _ in res.candidates] == ["api:b"]
        assert [ch.key for ch, _ in res.saturated] == ["api:a"]
    finally:
        m["concurrency"].release("api:a")
    print("  [PASS] saturated high-priority channel stays queued while available lower priority runs")


def main():
    m = _import_modules()
    orig_cfg = json.loads(json.dumps(m["config"].get()))
    tests = [
        test_priority_sort_and_affinity,
        test_normalize_and_init,
        test_button_rows_and_preview,
        test_add_delete_sync_priority,
        test_protocol_patch_without_family_change_keeps_priority,
        test_rename_and_protocol_change_cleans_old_family,
        test_priority_sorts_available_and_saturated_separately,
    ]
    passed = 0
    try:
        for t in tests:
            try:
                t(m); passed += 1
            except Exception as exc:
                print(f"  [FAIL] {t.__name__}: {exc}")
                import traceback; traceback.print_exc()
    finally:
        m["config"].update(lambda c: (c.clear(), c.update(orig_cfg)))
        m["states"].clear_all()
    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
