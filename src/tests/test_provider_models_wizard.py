from __future__ import annotations

import asyncio
import httpx
import pytest

from src import config, state_db
from src.channel import registry
from src.models_discovery import ModelsDiscoveryError, derive_custom_models_url, discover_models
from src.providers.catalog import PROVIDER_CATALOG, get_preset
from src.openai.channel.api_channel import OpenAIApiChannel
from src.telegram import states
from src.telegram.menus import channel_menu, channel_wizard


def _factory(handler):
    transport = httpx.MockTransport(handler)
    def make(**kwargs):
        kwargs.pop("proxy_purpose", None)
        return httpx.AsyncClient(transport=transport, **kwargs)
    return make


def test_catalog_exact_endpoints_and_shape():
    assert [b.id for b in PROVIDER_CATALOG] == [
        "zhipu", "kimi", "deepseek", "openai", "anthropic", "iflytek",
        "alibaba-bailian", "volcengine-ark", "tencent-cloud", "jd-cloud",
        "baidu-qianfan", "xiaomi-mimo", "opencode-go", "ctyun-xirang",
        "ollama-cloud", "openrouter", "minimax", "siliconflow",
    ]
    coding = get_preset("zhipu", "coding-cn")
    assert coding.models_url == "https://open.bigmodel.cn/api/v1/models"
    assert coding.protocols["openai-chat"] == "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
    assert coding.cc_mimicry is True
    kimi = get_preset("kimi", "code")
    assert kimi.models_url is None
    assert kimi.static_models == ("k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed")
    assert "api.kimi.com" in kimi.protocols["anthropic"]
    deepseek = get_preset("deepseek", "standard")
    assert deepseek.models_url == "https://api.deepseek.com/models"
    assert set(deepseek.protocols) == {"anthropic", "openai-chat", "openai-responses"}

    assert len(get_preset("openai", "global").protocols) == 2
    assert get_preset("openai", "eu").protocols["openai-responses"] == "https://eu.api.openai.com/v1/responses"
    assert get_preset("anthropic", "api").models_auth == "anthropic-x-api-key"
    assert get_preset("alibaba-bailian", "api-sg").models_parser == "dashscope-output-model"
    assert get_preset("iflytek", "astron-coding").protocols["openai-responses"].endswith("/v1/responses")
    assert get_preset("tencent-cloud", "tokenhub-global").models_url == "https://tokenhub-intl.tencentmaas.com/v1/models"
    assert get_preset("baidu-qianfan", "token-team").models_url is None
    assert get_preset("xiaomi-mimo", "token-eu").protocols["anthropic"].startswith("https://token-plan-ams")
    assert set(get_preset("opencode-go", "responses").protocols) == {"openai-responses"}
    assert get_preset("ollama-cloud", "cloud").protocols["openai-responses"] == "https://ollama.com/v1/responses"
    assert get_preset("minimax", "token-cn").cc_mimicry is True
    assert "openai-responses" not in get_preset("siliconflow", "api-global").protocols

    brand_ids = [brand.id for brand in PROVIDER_CATALOG]
    assert len(brand_ids) == len(set(brand_ids))
    for brand in PROVIDER_CATALOG:
        preset_ids = [preset.id for preset in brand.presets]
        assert preset_ids and len(preset_ids) == len(set(preset_ids))
        for preset in brand.presets:
            assert preset.protocols
            assert set(preset.protocols) <= {"anthropic", "openai-chat", "openai-responses"}
            assert all(url.startswith("https://") for url in preset.protocols.values())
            if preset.models_url:
                assert preset.models_url.startswith("https://")


def test_custom_models_url_derivation():
    assert derive_custom_models_url("https://x.test") == "https://x.test/v1/models"
    assert derive_custom_models_url("https://x.test/v1") == "https://x.test/v1/models"
    assert derive_custom_models_url("https://x.test", "/v1/chat/completions") == "https://x.test/v1/models"
    assert derive_custom_models_url("https://x.test/api", "/v1/messages") == "https://x.test/api/v1/models"


def test_discovery_stable_dedupe_auth_and_redirect_refusal():
    seen = {}
    def ok(request):
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}, {"id": ""}]})
    models = asyncio.run(discover_models("https://x.test/v1/models", "secret-key", client_factory=_factory(ok)))
    assert models == ["b", "a"] and seen["authorization"] == "Bearer secret-key"

    def anthropic(request):
        seen["x-api-key"] = request.headers.get("x-api-key")
        seen["anthropic-version"] = request.headers.get("anthropic-version")
        return httpx.Response(200, json={"data": [{"id": "claude-test"}]})
    models = asyncio.run(discover_models(
        "https://api.anthropic.com/v1/models?limit=1000", "anthropic-secret",
        auth="anthropic-x-api-key", client_factory=_factory(anthropic),
    ))
    assert models == ["claude-test"]
    assert seen["x-api-key"] == "anthropic-secret"
    assert seen["anthropic-version"] == "2023-06-01"

    def dashscope(request):
        return httpx.Response(200, json={
            "success": True,
            "output": {"models": [{"model": "qwen-a"}, {"model": "qwen-b"}, {"model": "qwen-a"}]},
        })
    models = asyncio.run(discover_models(
        "https://dashscope.aliyuncs.com/api/v1/models", "dashscope-secret",
        parser="dashscope-output-model", client_factory=_factory(dashscope),
    ))
    assert models == ["qwen-a", "qwen-b"]

    def redirect(request):
        return httpx.Response(302, headers={"location": "https://other.test/models"})
    with pytest.raises(ModelsDiscoveryError, match="重定向") as exc:
        asyncio.run(discover_models("https://x.test/models", "never-leak", client_factory=_factory(redirect)))
    assert "never-leak" not in str(exc.value)


def test_discovery_safe_errors_and_bounded_body():
    secret = "top-secret"
    def html(request):
        return httpx.Response(500, text=f"<html>{secret}</html>")
    with pytest.raises(ModelsDiscoveryError) as exc:
        asyncio.run(discover_models("https://x.test/models", secret, client_factory=_factory(html)))
    assert secret not in str(exc.value) and "html" not in str(exc.value).lower()

    def bad_schema(request): return httpx.Response(200, json={"models": [secret]})
    with pytest.raises(ModelsDiscoveryError, match="格式"):
        asyncio.run(discover_models("https://x.test/models", secret, client_factory=_factory(bad_schema)))


def test_registry_provider_identity_and_runtime_compatibility():
    state_db.init()
    def clear(c): c["channels"] = []
    config.update(clear); registry.rebuild_from_config()
    registry.add_api_channel({"name": "known", "baseUrl": "https://x.test", "apiKey": "key",
        "protocol": "anthropic", "models": [{"real": "m", "alias": "m"}],
        "providerId": "future-brand", "providerPresetId": "future-preset"})
    entry = config.get()["channels"][0]
    assert entry["providerId"] == "future-brand" and entry["providerPresetId"] == "future-preset"
    ch = registry.get_channel("api:known")
    assert ch.provider_id == "future-brand" and ch.provider_preset_id == "future-preset" and ch.enabled
    registry.update_api_channel("known", {"name": "renamed", "apiKey": "new-key"})
    renamed = config.get()["channels"][0]
    assert renamed["providerId"] == "future-brand" and renamed["providerPresetId"] == "future-preset"

    old = OpenAIApiChannel({"name": "old", "baseUrl": "https://old.test", "apiKey": "key",
        "protocol": "openai-chat", "models": [{"real": "m", "alias": "m"}]})
    assert old.provider_id is None and old.provider_preset_id is None


def test_provider_selection_protocol_filter_static_models_and_pagination(monkeypatch):
    calls = []
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: calls.append((a, k)) or {"ok": True, "result": {"message_id": 9}})
    monkeypatch.setattr(channel_wizard.ui, "answer_cb", lambda *a, **k: None)
    states.clear_all()
    data = {"name": "kimi"}
    # Kimi Code static fallback; preset protocols are the only rendered choices.
    states.set_state(7, "ch_wiz_url", data)
    channel_wizard._apply_preset(7, 99, data, 1, 0)
    state = states.get_state(7)
    assert state["action"] == "ch_wiz_protocol"
    channel_wizard.send_protocol_panel(7, 99)
    callbacks = [b["callback_data"] for row in calls[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "chw:proto:anthropic" in callbacks and "chw:proto:openai-chat" in callbacks
    assert "chw:proto:openai-responses" not in callbacks
    channel_wizard.wiz_on_protocol_select(7, 99, "cb", "openai-responses")
    assert states.get_state(7)["action"] == "ch_wiz_protocol"

    channel_wizard.wiz_on_protocol_select(7, 99, "cb", "anthropic")
    monkeypatch.setattr(channel_menu, "_SYNC_SPAWN", True)
    calls.clear()
    channel_wizard.wiz_on_key_input(7, "static-key")
    state = states.get_state(7)
    assert state["action"] == "ch_wiz_model_select"
    assert state["data"]["discovered_models"][0] == "k3"
    assert state["data"]["models_source"] == "static"
    assert all("正在发现模型" not in str(call[0]) for call in calls)
    assert "内置参考模型" in calls[-1][0][1]
    callbacks = [b["callback_data"] for row in calls[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "chw:manual" in callbacks and "chw:discover_retry" not in callbacks

    state["data"]["discovered_models"] = [f"m{i}" for i in range(23)]
    state["data"]["selected_models"] = []
    kb = channel_wizard.model_kb(state["data"])["inline_keyboard"]
    model_buttons = [b for row in kb for b in row if b.get("callback_data", "").startswith("chw:mt:")]
    assert len(model_buttons) == 10
    state["data"]["models"] = [{"real": f"m{i}", "alias": f"m{i}"} for i in range(23)]
    state["data"]["test_results"] = {}
    test_buttons = [b for row in channel_wizard.test_kb(state["data"])["inline_keyboard"] for b in row
                    if b.get("callback_data", "").startswith("chw:test:")]
    assert len(test_buttons) == 10


@pytest.mark.parametrize("page, expected_count", [(0, 10), (1, 10), (2, 3)])
def test_test_panel_body_and_keyboard_are_paginated(page, expected_count):
    models = [{"real": f"real-{i:02d}-END", "alias": f"alias-{i:02d}-END"} for i in range(23)]
    data = {
        "name": "many-models",
        "models": models,
        "test_page": page,
        "test_results": {
            model["real"]: (i % 2 == 0, i + 1, f"failure-{i:02d}-END")
            for i, model in enumerate(models)
        },
    }

    text = channel_menu._wiz_test_intro(data)
    assert "模型: 23 个" in text
    assert f"第 {page + 1}/3 页" in text
    start = page * 10
    current = range(start, min(start + 10, 23))
    other = set(range(23)) - set(current)
    for i in current:
        assert f"<code>alias-{i:02d}-END</code>" in text
    for i in other:
        assert f"<code>alias-{i:02d}-END</code>" not in text

    buttons = [
        button
        for row in channel_wizard.test_kb(data)["inline_keyboard"]
        for button in row
        if button.get("callback_data", "").startswith("chw:test:")
    ]
    assert len(buttons) == expected_count


def test_test_panel_omits_untested_models_from_body():
    models = [{"real": f"real-{i:02d}", "alias": f"alias-{i:02d}"} for i in range(23)]
    data = {
        "name": "partial-results",
        "models": models,
        "test_page": 1,
        "test_results": {"real-10": (True, 12, None), "real-11": (False, 0, "failed")},
    }
    text = channel_wizard.test_intro(data)
    assert "<code>alias-10</code>" in text
    assert "<code>alias-11</code>" in text
    assert "<code>alias-12</code>" not in text


def test_test_panel_entry_and_refresh_paths_use_paginated_body(monkeypatch):
    models = [f"confirm-{i:02d}-END" for i in range(23)]
    data = {
        "name": "confirm-many",
        "discovered_models": models,
        "selected_models": list(models),
        "models_mode": "discovered",
    }
    states.clear_all()
    states.set_state(70, "ch_wiz_model_select", data)
    edits = []
    sends = []
    monkeypatch.setattr(channel_wizard.ui, "answer_cb", lambda *a, **k: None)
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: sends.append((a, k)))

    channel_wizard.wiz_model_confirm(70, 90, "confirm-cb")
    confirm_text = edits[-1][0][2]
    assert "第 1/3 页" in confirm_text
    assert "<code>confirm-10-END</code>" not in confirm_text

    test_data = states.get_state(70)["data"]
    test_data["test_results"] = {model: (True, i, None) for i, model in enumerate(models)}
    test_data["test_page"] = 2
    channel_menu._wiz_send_test_panel(70, test_data)
    channel_menu._wiz_refresh_test_panel(70, 90, test_data)
    assert "第 3/3 页" in sends[-1][0][1]
    assert "<code>confirm-22-END</code>" in sends[-1][0][1]
    assert sends[-1][0][1] == edits[-1][0][2]


def test_model_and_test_page_callbacks_write_state(monkeypatch):
    states.clear_all()
    edits = []
    answers = []
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(channel_wizard.ui, "answer_cb", lambda *a, **k: answers.append((a, k)))
    original_set_state = states.set_state
    writes = []

    def tracked_set_state(chat_id, action, data=None):
        writes.append((chat_id, action, dict(data or {})))
        return original_set_state(chat_id, action, data)

    original_set_state(71, "ch_wiz_model_select", {
        "discovered_models": [f"m{i}" for i in range(23)],
        "selected_models": [],
        "model_page": 0,
    })
    monkeypatch.setattr(channel_wizard.states, "set_state", tracked_set_state)
    channel_wizard.wiz_model_page(71, 91, "model-cb", 99)
    assert writes[-1][0:2] == (71, "ch_wiz_model_select")
    assert writes[-1][2]["model_page"] == 2
    assert states.get_state(71)["data"]["model_page"] == 2

    original_set_state(72, "ch_wiz_test", {
        "name": "paging",
        "models": [{"real": f"m{i}", "alias": f"m{i}"} for i in range(23)],
        "test_results": {},
        "test_page": 0,
    })
    channel_wizard.wiz_test_page(72, 92, "test-cb", 99)
    assert writes[-1][0:2] == (72, "ch_wiz_test")
    assert writes[-1][2]["test_page"] == 2
    assert states.get_state(72)["data"]["test_page"] == 2
    assert [call[0][0] for call in answers[-2:]] == ["model-cb", "test-cb"]
    assert len(edits) == 2


def test_preset_without_official_models_endpoint_goes_directly_manual(monkeypatch):
    from types import SimpleNamespace
    states.clear_all(); sent = []
    preset = SimpleNamespace(models_url=None, models_auth="bearer",
                             models_parser="openai-data-id", static_models=())
    monkeypatch.setattr(channel_wizard, "get_preset", lambda *a: preset)
    async def unexpected(*a, **k):
        raise AssertionError("provider preset must not derive an undocumented /models URL")
    monkeypatch.setattr(channel_wizard, "discover_models", unexpected)
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: sent.append((a, k)))
    data = {"name": "manual-models", "providerId": "p", "providerPresetId": "q",
            "baseUrl": "https://provider.test", "protocol": "openai-chat"}
    states.set_state(10, "ch_wiz_key", data)
    channel_wizard.wiz_on_key_input(10, "secret")
    state = states.get_state(10)
    assert state["action"] == "ch_wiz_models"
    assert state["data"]["models_source"] == "manual"
    assert state["data"]["discovery_retry_available"] is False
    assert "已直接进入手动输入" in sent[-1][0][1]
    assert "正在发现模型" not in sent[-1][0][1]


def test_network_failure_uses_labeled_static_fallback_with_manual_and_retry(monkeypatch):
    from types import SimpleNamespace
    states.clear_all(); edits = []
    preset = SimpleNamespace(models_url="https://x.test/models", models_auth="bearer",
                             models_parser="openai-data-id", static_models=("fallback-a", "fallback-b"))
    monkeypatch.setattr(channel_wizard, "get_preset", lambda *a: preset)
    async def fail(*a, **k): raise ModelsDiscoveryError("safe failure")
    monkeypatch.setattr(channel_wizard, "discover_models", fail)
    monkeypatch.setattr(channel_menu, "_SYNC_SPAWN", True)
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: None)
    data = {"name": "fallback", "providerId": "p", "providerPresetId": "q",
            "baseUrl": "https://x.test", "apiKey": "secret", "protocol": "anthropic"}
    channel_wizard.start_discovery(9, 1, data)
    state = states.get_state(9)
    assert state["action"] == "ch_wiz_model_select"
    assert state["data"]["discovered_models"] == ["fallback-a", "fallback-b"]
    assert state["data"]["models_source"] == "static"
    assert state["data"]["discovery_error"] == "safe failure"
    assert state["data"]["discovery_retry_available"] is True
    assert "可能不是最新版本" in edits[-1][0][2]
    callbacks = [b["callback_data"] for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "chw:manual" in callbacks and "chw:discover_retry" in callbacks


def test_network_failure_without_static_enters_manual_and_retry_can_succeed(monkeypatch):
    from types import SimpleNamespace
    states.clear_all(); edits = []; answers = []
    preset = SimpleNamespace(models_url="https://x.test/models", models_auth="bearer",
                             models_parser="openai-data-id", static_models=())
    monkeypatch.setattr(channel_wizard, "get_preset", lambda *a: preset)
    outcomes = [ModelsDiscoveryError("HTTP 401"), ["fresh-model"]]
    async def discover(*a, **k):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(channel_wizard, "discover_models", discover)
    monkeypatch.setattr(channel_menu, "_SYNC_SPAWN", True)
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: edits.append((a, k)))
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: None)
    monkeypatch.setattr(channel_wizard.ui, "answer_cb", lambda *a, **k: answers.append((a, k)))
    data = {"name": "manual-fallback", "providerId": "p", "providerPresetId": "q",
            "baseUrl": "https://x.test", "apiKey": "secret", "protocol": "openai-chat"}
    channel_wizard.start_discovery(11, 1, data)
    state = states.get_state(11)
    assert state["action"] == "ch_wiz_models"
    assert state["data"]["models_source"] == "manual"
    assert state["data"]["discovery_retry_available"] is True
    assert "已切换为手动输入" in edits[-1][0][2]

    channel_wizard.wiz_discovery_retry(11, 1, "retry-cb")
    state = states.get_state(11)
    assert state["action"] == "ch_wiz_model_select"
    assert state["data"]["discovered_models"] == ["fresh-model"]
    assert state["data"]["models_source"] == "live"
    assert "discovery_error" not in state["data"]
    assert state["data"]["discovery_retry_available"] is False
    assert answers[-1][0][0] == "retry-cb"


def test_late_discovery_generation_cannot_overwrite(monkeypatch):
    states.clear_all(); factories = []
    monkeypatch.setattr(channel_menu, "_spawn_async_task", lambda factory, name="": factories.append(factory))
    monkeypatch.setattr(channel_wizard.ui, "edit", lambda *a, **k: None)
    monkeypatch.setattr(channel_wizard.ui, "send", lambda *a, **k: None)
    data = {"name": "race", "baseUrl": "https://x.test", "apiKey": "secret", "protocol": "anthropic"}
    channel_wizard.start_discovery(8, 1, data)
    first_generation = states.get_state(8)["data"]["discovery_generation"]
    # 返回 Key/新动作使旧 generation 失效，再执行迟到任务。
    states.set_state(8, "ch_wiz_key", {**data, "discovery_generation": first_generation + 1})
    async def immediate(*a, **k): return ["late-model"]
    monkeypatch.setattr(channel_wizard, "discover_models", immediate)
    asyncio.run(factories[0]())
    state = states.get_state(8)
    assert state["action"] == "ch_wiz_key" and "discovered_models" not in state["data"]
