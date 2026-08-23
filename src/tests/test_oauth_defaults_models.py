from __future__ import annotations

import asyncio
import copy

import pytest

from src import config, state_db
from src.models_discovery import ModelsDiscoveryError
from src.telegram import states, ui
from src.telegram.menus import oauth_defaults_menu

_MUTATED_TOP_KEYS = (
    "oauthDefaultModels",
    "oauthAccounts",
    "apiKeys",
    "modelMapping",
    "ingressDefaultModel",
)


@pytest.fixture(autouse=True)
def _restore_oauth_defaults_config():
    """本文件会改家族默认模型，测完必须还原，避免污染后续 Channel 回落测试。"""
    before = config.get()
    snapshot = {key: copy.deepcopy(before.get(key)) for key in _MUTATED_TOP_KEYS}
    openai_models = copy.deepcopy((before.get("openaiOAuth") or {}).get("defaultModels"))
    xai_models = copy.deepcopy((before.get("xaiOAuth") or {}).get("defaultModels"))
    antigravity_models = copy.deepcopy((before.get("antigravityOAuth") or {}).get("defaultModels"))
    yield

    def restore(cfg):
        for key, value in snapshot.items():
            cfg[key] = copy.deepcopy(value)
        cfg.setdefault("openaiOAuth", {})["defaultModels"] = copy.deepcopy(openai_models)
        cfg.setdefault("xaiOAuth", {})["defaultModels"] = copy.deepcopy(xai_models)
        cfg.setdefault("antigravityOAuth", {})["defaultModels"] = copy.deepcopy(antigravity_models)

    config.update(restore)


def _reset():
    state_db.init()
    states.clear_all()
    def clear(cfg):
        cfg["oauthDefaultModels"] = ["claude-old", "claude-keep"]
        cfg.setdefault("openaiOAuth", {})["defaultModels"] = ["gpt-keep"]
        cfg.setdefault("xaiOAuth", {})["defaultModels"] = ["grok-4.5", "grok-local"]
        cfg.setdefault("antigravityOAuth", {})["defaultModels"] = ["gemini-3.7-flash-high"]
        cfg["oauthAccounts"] = []
        cfg["apiKeys"] = {}
        cfg["modelMapping"] = {"global": {}}
        cfg["ingressDefaultModel"] = {}
    config.update(clear)


def _patch_ui(monkeypatch):
    edits, sends, answers, results = [], [], [], []
    monkeypatch.setattr(oauth_defaults_menu.ui, "edit", lambda *a, **k: edits.append((a, k)) or {"ok": True})
    monkeypatch.setattr(oauth_defaults_menu.ui, "send", lambda *a, **k: sends.append((a, k)) or {"ok": True})
    monkeypatch.setattr(oauth_defaults_menu.ui, "answer_cb", lambda *a, **k: answers.append((a, k)))
    monkeypatch.setattr(oauth_defaults_menu.ui, "send_result", lambda *a, **k: results.append((a, k)))
    return edits, sends, answers, results


def test_overview_buttons_use_provider_custom_icons():
    kb = oauth_defaults_menu._overview_kb()["inline_keyboard"]
    assert [b["text"] for b in kb[0]] == ["Claude", "OpenAI", "Grok"]
    assert kb[0][0]["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("claude")
    assert kb[0][1]["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("openai")
    assert kb[0][2]["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("xai")
    assert [b["text"] for b in kb[1]] == ["Antigravity"]
    assert kb[1][0]["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("antigravity")
    assert kb[1][0]["icon_custom_emoji_id"] == "6077644693984779782"
    assert kb[2][0]["callback_data"] == "oa:settings"


def test_account_settings_summary_lists_antigravity_catalog():
    from src.telegram.menus import oauth_menu

    _reset()
    assert oauth_menu._default_models_for_settings("antigravity") == ["gemini-3.7-flash-high"]
    assert "claude-old" not in oauth_menu._default_models_for_settings("antigravity")
    text, _kb = oauth_menu._settings_text_and_kb()
    ag_at = text.index("Antigravity")
    assert "1 个" in text[ag_at:ag_at + 40]
    assert "Antigravity 出图:" in text
    acc = {
        "provider": "antigravity",
        "email": "ag@example.com",
        "project_id": "proj-1",
    }
    assert oauth_menu._antigravity_catalog_counts(acc)[0] == 1


def test_claude_skips_live_and_uses_static(monkeypatch):
    _reset()
    edits, *_ = _patch_ui(monkeypatch)
    async def unexpected(*a, **k):
        raise AssertionError("claude has no live models endpoint")
    monkeypatch.setattr(oauth_defaults_menu, "discover_models", unexpected)
    oauth_defaults_menu._start_edit(7, 99, "cb", "anthropic")
    state = states.get_state(7)
    assert state["action"] == "odm_model_select"
    assert state["data"]["models_source"] == "static"
    assert "claude-old" in state["data"]["discovered_models"]
    assert "claude-keep" in state["data"]["selected_models"]
    factory = oauth_defaults_menu._static_models("anthropic")
    for mid in factory:
        if mid not in ("claude-old", "claude-keep"):
            assert mid not in state["data"]["selected_models"]
    assert all("正在发现模型" not in str(call[0]) for call in edits)
    assert "内置参考模型" in edits[-1][0][2]
    callbacks = [b["callback_data"] for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "odm:ok" in callbacks and "odm:manual" in callbacks
    assert "odm:retry" not in callbacks


def test_grok_live_failure_falls_back_to_static(monkeypatch):
    _reset()
    edits, *_ = _patch_ui(monkeypatch)
    monkeypatch.setattr(oauth_defaults_menu, "_SYNC_SPAWN", True)
    async def fail(*a, **k):
        raise ModelsDiscoveryError("safe failure")
    monkeypatch.setattr(oauth_defaults_menu, "discover_models", fail)
    oauth_defaults_menu._start_edit(7, 99, "cb", "xai")
    state = states.get_state(7)
    assert state["action"] == "odm_model_select"
    assert state["data"]["models_source"] == "static"
    assert state["data"]["discovery_error"] == "没有可用的 Grok 账户用于拉取模型"
    assert state["data"]["discovery_retry_available"] is True
    assert "grok-4.5" in state["data"]["discovered_models"]
    assert "可能不是最新版本" in edits[-1][0][2]
    callbacks = [b["callback_data"] for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "odm:retry" in callbacks and "odm:manual" in callbacks


def test_grok_live_success_filters_imagine_and_prechecks(monkeypatch):
    _reset()
    edits, *_ = _patch_ui(monkeypatch)
    monkeypatch.setattr(oauth_defaults_menu, "_SYNC_SPAWN", True)
    monkeypatch.setattr(oauth_defaults_menu, "_first_enabled_account_key", lambda provider: "xai:demo")
    async def token(account_key):
        return "tok"
    monkeypatch.setattr("src.oauth_manager.ensure_valid_token", token)
    async def live(*a, **k):
        return ["grok-4.5", "grok-imagine-image", "grok-4.5-fast"]
    monkeypatch.setattr(oauth_defaults_menu, "discover_models", live)
    oauth_defaults_menu._start_edit(7, 99, "cb", "xai")
    state = states.get_state(7)
    assert state["action"] == "odm_model_select"
    assert state["data"]["models_source"] == "live"
    assert state["data"]["discovered_models"] == ["grok-4.5", "grok-4.5-fast", "grok-local"]
    assert state["data"]["selected_models"] == ["grok-4.5", "grok-local"]
    assert "已从上游获取 3 个模型" in edits[-1][0][2]
    labels = [b["text"] for row in edits[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert any("⬜ grok-4.5-fast · 新" in text for text in labels)


def test_confirm_saves_selected_without_reference_prompt(monkeypatch):
    _reset()
    _patch_ui(monkeypatch)
    states.set_state(7, "odm_model_select", {
        "family": "anthropic",
        "existing_models": ["claude-old", "claude-keep"],
        "discovered_models": ["claude-keep", "claude-new", "claude-old"],
        "selected_models": ["claude-keep", "claude-new"],
    })
    oauth_defaults_menu._model_confirm(7, 99, "cb")
    assert config.get()["oauthDefaultModels"] == ["claude-keep", "claude-new"]
    assert states.get_state(7) is None


def test_delete_with_refs_opens_confirm(monkeypatch):
    _reset()
    def seed(cfg):
        cfg["oauthDefaultModels"] = ["claude-a", "claude-b"]
        cfg["ingressDefaultModel"] = {"anthropic": "claude-b"}
    config.update(seed)
    _patch_ui(monkeypatch)
    sends = []
    monkeypatch.setattr(oauth_defaults_menu.ui, "send", lambda *a, **k: sends.append((a, k)))
    states.set_state(7, "odm_model_select", {
        "family": "anthropic",
        "existing_models": ["claude-a", "claude-b"],
        "discovered_models": ["claude-a", "claude-b"],
        "selected_models": ["claude-a"],
    })
    oauth_defaults_menu._model_confirm(7, 99, "cb")
    assert config.get()["oauthDefaultModels"] == ["claude-a", "claude-b"]
    assert "确认保存" in sends[-1][0][1]
    callbacks = [b["callback_data"] for row in sends[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert any(cb.startswith("odm:commit:") and cb.endswith(":keep") for cb in callbacks)
    assert any(cb.startswith("odm:commit:") and cb.endswith(":clean") for cb in callbacks)


def test_catalog_show_invalidates_inflight_discovery(monkeypatch):
    _reset()
    factories = []
    monkeypatch.setattr(oauth_defaults_menu, "_spawn_async_task", lambda factory, name="": factories.append(factory))
    _patch_ui(monkeypatch)
    data = {"family": "xai", "existing_models": ["grok-4.5"], "selected_models": ["grok-4.5"]}
    oauth_defaults_menu._start_discovery(7, 99, data)
    assert states.get_state(7)["action"] == "odm_discovery"
    oauth_defaults_menu.show(7, 99, "cb")
    assert states.get_state(7) is None
    async def late(*a, **k):
        return ["late-model"]
    monkeypatch.setattr(oauth_defaults_menu, "discover_models", late)
    monkeypatch.setattr(oauth_defaults_menu, "_first_enabled_account_key", lambda provider: "xai:demo")
    async def token(account_key):
        return "tok"
    monkeypatch.setattr("src.oauth_manager.ensure_valid_token", token)
    asyncio.run(factories[0]())
    assert states.get_state(7) is None
