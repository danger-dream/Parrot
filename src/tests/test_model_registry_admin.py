"""Unified naming administration: management API and the Telegram model-center page."""
from __future__ import annotations

import pytest

from src import config, state_db
from src.channel import registry
from src.model_registry import runtime as model_registry
from src.model_registry.offers import Offer
from src.telegram import ui
from src.telegram.menus import model_center_menu as menu
from src.tests.test_management_mapping_support import domain_client  # noqa: F401  (fixture)

BASE = "/api/management/v1/models/registry"


def _api(name, models):
    return {"name": name, "type": "api", "enabled": True, "protocol": "openai-chat",
            "baseUrl": "https://upstream.invalid", "apiKey": "isolated-test",
            "models": [{"real": model, "alias": model} for model in models]}


@pytest.fixture
def enabled(domain_client, request):  # noqa: F811
    from src.openai.channel.registration import register_factories
    state_db.init()
    request.addfinalizer(state_db.close)
    register_factories()

    def seed(cfg):
        cfg["channels"] = [_api("Official", ["MiniMax-M2.7"]), _api("Reseller", ["minimax-m2.7", "glm-5.3"])]
        cfg["modelRegistry"] = {"enabled": True}
        cfg.setdefault("modelCenter", {})["disabledModels"] = ["minimax-m2.7"]
    config.update(seed)
    registry.rebuild_from_config()
    yield domain_client
    config.update(lambda cfg: cfg.update(modelRegistry={"enabled": False}))
    registry.rebuild_from_config()


def test_registry_api_reads_and_writes_rules_under_revision(enabled):
    client, _runtime, admin, read_only, denied = enabled
    assert client.get(BASE, headers=denied).status_code == 403
    read = client.get(BASE, headers=read_only)
    assert read.status_code == 200, read.text
    data = read.json()["data"]
    assert (data["enabled"], data["rules"], data["defaultEfforts"]) == (True, [], {})
    body = {"rules": [{"type": "merge", "model": "MiniMax-M2.7", "into": "minimax-m2.7-legacy"}]}
    assert client.put(BASE + "/rules", headers=read_only, json=body).status_code == 403
    missing = client.put(BASE + "/rules", headers=admin, json=body)
    assert missing.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    stale = client.put(BASE + "/rules", headers={**admin, "If-Match": "rev_stale"}, json=body)
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    bad = client.put(BASE + "/rules", headers={**admin, "If-Match": data["revision"]},
                     json={"rules": [{"type": "merge", "model": "x"}]})
    assert bad.status_code == 422
    saved = client.put(BASE + "/rules", headers={**admin, "If-Match": data["revision"]}, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] != data["revision"]
    after = client.get(BASE, headers=admin).json()["data"]
    assert after["rules"] == [{"type": "merge", "model": "minimax-m2.7", "into": "minimax-m2.7-legacy",
                               "source": None, "upstreamModel": None, "pin": None}]
    assert after["revision"] == saved.json()["data"]["revision"]
    # A rename keeps the model's global switch.
    assert config.get()["modelCenter"]["disabledModels"] == ["minimax-m2.7-legacy"]


def test_default_effort_api_validates_model_and_level(enabled):
    client, _runtime, admin, _read_only, _denied = enabled
    revision = client.get(BASE, headers=admin).json()["data"]["revision"]
    url = BASE + "/models/{}/default-effort"
    assert client.put(url.format("glm-5.3"), headers={**admin, "If-Match": revision},
                      json={"effort": "huge"}).status_code == 422
    unknown = client.put(url.format("no-such-model"), headers={**admin, "If-Match": revision}, json={"effort": "low"})
    assert unknown.status_code == 404
    saved = client.put(url.format("glm-5.3"), headers={**admin, "If-Match": revision}, json={"effort": "low"})
    assert saved.status_code == 200, saved.text
    assert client.get(BASE, headers=admin).json()["data"]["defaultEfforts"] == {"glm-5.3": "low"}
    cleared = client.put(url.format("glm-5.3"), headers={**admin, "If-Match": saved.json()["data"]["revision"]},
                         json={"effort": None})
    assert cleared.status_code == 200
    assert client.get(BASE, headers=admin).json()["data"]["defaultEfforts"] == {}


def test_registry_writes_are_refused_while_the_registry_is_off(domain_client):  # noqa: F811
    client, _runtime, admin, _read_only, _denied = domain_client
    data = client.get(BASE, headers=admin).json()["data"]
    assert data["enabled"] is False
    refused = client.put(BASE + "/rules", headers={**admin, "If-Match": data["revision"]}, json={"rules": []})
    assert refused.json()["error"]["code"] == "UNSUPPORTED_VALUE"


def _buttons(keyboard):
    return [button for row in keyboard["inline_keyboard"] for button in row]


def _press(text_part, keyboard):
    button = next(item for item in _buttons(keyboard) if text_part in item["text"])
    calls.clear()
    assert menu.handle_callback(42, 100, "cb", button["callback_data"])
    edit = next(data for method, data in calls if method == "editMessageText")
    return edit["text"], edit["reply_markup"]


calls: list = []


def test_telegram_page_settles_a_pending_id_with_an_assign_rule(enabled, monkeypatch):
    _client, runtime, _admin, _read_only, _denied = enabled
    monkeypatch.setattr(menu, "_CONTROL", runtime.control_owner().models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, dict(data or {}))) or {
        "ok": True, "result": {"message_id": 100}})
    monkeypatch.setattr(model_registry, "collect", lambda cfg: [
        Offer("oauth:wb", "workbuddy", "hy3", "Hy3", basis="name"),
        Offer("oauth:wb", "workbuddy", "hy3-x", "Hy3", basis="name")])
    registry.rebuild_from_config()
    menu.reset_for_tests()
    text, keyboard = menu.render(42)
    entry = next(item for item in _buttons(keyboard) if "统一命名" in item["text"])
    assert entry["text"].endswith("（待处理 1）")
    text, keyboard = _press("统一命名", keyboard)
    assert "hy3-x" in text and "workbuddy-hy3-x" in text
    text, keyboard = _press("待确认 1", keyboard)
    assert {"归入 hy3", "保持 workbuddy-hy3-x", "自定义名称"} <= {item["text"] for item in _buttons(keyboard)}
    text, keyboard = _press("归入 hy3", keyboard)
    assert config.get()["modelRegistry"]["rules"] == [
        {"type": "assign", "source": "oauth:wb", "wire": "hy3-x", "model": "hy3"}]
    registry.rebuild_from_config()
    hy3 = model_registry.current().models["hy3"]
    assert sorted(offer.wire for offer in hy3.offers) == ["hy3", "hy3-x"] and not hy3.pending
    text, keyboard = menu.render(42)
    assert next(item for item in _buttons(keyboard) if "统一命名" in item["text"])["text"] == "🧩 统一命名"


def test_telegram_detail_page_sets_a_models_default_effort(enabled, monkeypatch):
    from src.telegram.menus import model_center_catalog as catalog
    _client, runtime, _admin, _read_only, _denied = enabled
    monkeypatch.setattr(menu, "_CONTROL", runtime.control_owner().models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, dict(data or {}))) or {
        "ok": True, "result": {"message_id": 100}})
    monkeypatch.setattr(model_registry, "collect", lambda cfg: [
        Offer("api:Reseller", "api", "glm-5.3", "glm-5.3", efforts=("low", "high", "max"))])
    registry.rebuild_from_config()
    menu.reset_for_tests()
    view = next(item for item in menu._CONTROL.list_models(menu._ctx(42)).items if item.model_id == "glm-5.3")
    _text, keyboard = catalog._detail_render(42, view.resource_key)
    text, keyboard = _press("默认强度：跟随全局（high）", keyboard)
    assert {"low", "high", "max"} <= {item["text"] for item in _buttons(keyboard)}
    text, keyboard = _press("low", keyboard)
    assert config.get()["modelRegistry"]["models"]["glm-5.3"] == {"defaultEffort": "low"}
    assert "✓ low" in {item["text"] for item in _buttons(keyboard)}


def test_telegram_page_decides_one_upstream_id_for_every_account_of_a_provider(enabled, monkeypatch):
    _client, runtime, _admin, _read_only, _denied = enabled
    monkeypatch.setattr(menu, "_CONTROL", runtime.control_owner().models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, dict(data or {}))) or {
        "ok": True, "result": {"message_id": 100}})
    monkeypatch.setattr(model_registry, "collect", lambda cfg: [
        Offer(source, "workbuddy", wire, "Hy3", basis="name")
        for source in ("oauth:wb-a", "oauth:wb-b") for wire in ("hy3", "hy3-x")])
    registry.rebuild_from_config()
    menu.reset_for_tests()
    _text, keyboard = menu.render(42)
    # The entry counts what the page lists: one decision per provider and id.
    assert any(item["text"] == "🧩 统一命名（待处理 1）" for item in _buttons(keyboard))
    text, keyboard = _press("统一命名", keyboard)
    assert "<b>待确认 1</b>" in text and "workbuddy（2 个账号）" in text
    text, keyboard = _press("待确认 1", keyboard)
    _press("保持 workbuddy-hy3-x", keyboard)
    assert config.get()["modelRegistry"]["rules"] == [
        {"type": "assign", "source": source, "wire": "hy3-x", "model": "workbuddy-hy3-x"}
        for source in ("oauth:wb-a", "oauth:wb-b")]


def test_telegram_pending_page_opens_for_antigravity_ids(enabled, monkeypatch):
    _client, runtime, _admin, _read_only, _denied = enabled
    monkeypatch.setattr(menu, "_CONTROL", runtime.control_owner().models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, dict(data or {}))) or {
        "ok": True, "result": {"message_id": 100}})
    monkeypatch.setattr(model_registry, "collect", lambda cfg: [
        Offer("oauth:ag", "antigravity", wire, "Gemini 3.5 Flash Lite", basis="name")
        for wire in ("gemini-2.5-flash", "gemini-2.5-flash-lite")])
    registry.rebuild_from_config()
    menu.reset_for_tests()
    _text, keyboard = menu.render(42)
    _text, keyboard = _press("统一命名", keyboard)
    text, keyboard = _press("待确认 1", keyboard)
    assert "上游 ID：<code>gemini-2.5-flash</code>" in text
    assert not any(method == "answerCallbackQuery" and data.get("show_alert") for method, data in calls)


def test_detail_page_merges_identical_sources_of_one_provider():
    from src.telegram.menus.model_center_sources import _merged_source_blocks
    same = ["上游名：<code>deepseek-v4.1-flash</code>", "💳 WorkBuddy 倍率：×0.11"]
    free = ["上游名：<code>deepseek-v4.1-flash</code>", "💳 WorkBuddy 倍率：×0.00（免费）"]
    lines = _merged_source_blocks([
        ("", "DeepSeek · DeepSeek", ["上游名：<code>deepseek-flash</code>"]),
        ("", "WorkBuddy · 可乐", list(same)), ("", "WorkBuddy · 小枕头", list(same)),
        ("", "WorkBuddy · soarsky0204@gmail.com", free), ("", "Kimi · 可乐", list(same)),
        ("", "已移除的账户", ["x"]), ("", "已移除的账户", ["x"]),
    ])
    titles = [line for line in lines if line.startswith("<b>")]
    assert titles == [
        "<b>DeepSeek · DeepSeek</b>",
        "<b>WorkBuddy · 可乐、小枕头（2 个账号）</b>",
        "<b>WorkBuddy · soarsky0204@gmail.com</b>",
        "<b>Kimi · 可乐</b>",
        "<b>已移除的账户（2 个）</b>",
    ]
    assert lines.count("💳 WorkBuddy 倍率：×0.11") == 2


def test_ranked_sources_merge_only_with_their_neighbours():
    from src.telegram.menus.model_center_sources import _merged_source_blocks
    same = ["x"]
    lines = _merged_source_blocks([
        ("", "WorkBuddy · a", list(same)), ("", "WorkBuddy · b", list(same)),
        ("", "Kimi · k", ["y"]), ("", "WorkBuddy · c", list(same)),
    ], [1, 2, 3, 4])
    assert [line for line in lines if line.startswith("<b>")] == [
        "<b>1–2. WorkBuddy · a、b（2 个账号）</b>", "<b>3. Kimi · k</b>", "<b>4. WorkBuddy · c</b>"]


def test_detail_follows_priority_order_and_manages_sources_and_priority(enabled, monkeypatch):
    from src.telegram.menus import load_balancing_menu
    from src.telegram.menus import model_center_catalog as catalog
    _client, runtime, _admin, _read_only, _denied = enabled

    def priority(cfg):
        cfg["modelCenter"]["disabledModels"] = []
        cfg["channelSelection"] = "priority"
        cfg["loadBalancing"] = {"initialized": True, "channelPriorityOrder": ["api:Official", "api:Reseller"],
                                "modelPriorityOrders": {"MiniMax-M2.7": ["api:Reseller", "api:Official"]}}
    config.update(priority)
    registry.rebuild_from_config()
    monkeypatch.setattr(menu, "_CONTROL", runtime.control_owner().models)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: int(chat_id) == 42)
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, dict(data or {}))) or {
        "ok": True, "result": {"message_id": 100}})
    menu.reset_for_tests()
    view = next(item for item in menu._CONTROL.list_models(menu._ctx(42)).items if item.model_id == "minimax-m2.7")
    text, keyboard = catalog._detail_render(42, view.resource_key)
    assert "调度：<code>优先级 · 本模型专属顺序</code>" in text
    assert text.index("<b>1. ") < text.index("Reseller") < text.index("<b>2. ") < text.index("Official")

    text, sources_kb = _press("按账户/渠道启停", keyboard)
    assert "1. ✅" in text and "调度第 1 位" in text
    text, _ = _press("停用 1 ·", sources_kb)
    assert "1. 🚫" in text and "2. ✅" in text

    def lb(callback):
        calls.clear()
        assert load_balancing_menu.handle_callback(42, 100, "cb", callback)
        return next(data for method, data in calls if method == "editMessageText")

    text, keyboard = catalog._detail_render(42, view.resource_key)
    text, _ = _press("调整调度优先级", keyboard)
    assert "模型专属优先级" in text
    assert "调度：" in lb("lb:cancel")["text"]
    _press("调整调度优先级", keyboard)
    saved = lb("lb:save")
    assert [button["text"] for row in saved["reply_markup"]["inline_keyboard"] for button in row] == ["返回模型详情"]
    orders = config.get()["loadBalancing"]["modelPriorityOrders"]
    assert orders == {"minimax-m2.7": ["api:Reseller", "api:Official"]}


def test_one_source_serving_a_model_per_effort_is_one_block():
    from src.telegram.menus.model_center_sources import _fold_source_ids
    body = ["本来源模型开关：停用", "上下文：1,000,000 tokens"]
    blocks = [("", "Antigravity · a", list(body)), ("", "Antigravity · a", list(body)),
              ("", "Antigravity · a", list(body)), ("", "Claude · c", ["x"])]
    wires = ["claude-opus-5-5-medium", "claude-opus-5-5-low", "claude-opus-5-5-high", "claude-opus-5-5"]
    refs = [("oauth", "ag:a")] * 3 + [("oauth", "claude:c")]
    folded, ranks = _fold_source_ids("no-such-model", blocks, wires, refs, [2, 2, 2, 3])
    assert ranks == [2, 3] and len(folded) == 2
    assert folded[0][2][0] == ("上游名：<code>claude-opus-5-5-medium</code> / <code>claude-opus-5-5-low</code>"
                               " / <code>claude-opus-5-5-high</code>")
    different = [("", "Antigravity · a", ["x"]), ("", "Antigravity · a", ["y"])]
    folded, _ = _fold_source_ids("m", different, ["a-low", "a-high"], [("oauth", "ag:a")] * 2, [])
    assert [block[2][0] for block in folded] == ["上游名：<code>a-low</code>", "上游名：<code>a-high</code>"]


def test_reasoning_flags_are_not_effort_levels():
    from src import model_metadata
    assert model_metadata.normalize_metadata({"reasoning": True, "thinking": "true"}) == {}
    assert model_metadata.normalize_metadata({"reasoning": ["low", "High"]}) == {"reasoningEfforts": ["low", "high"]}
