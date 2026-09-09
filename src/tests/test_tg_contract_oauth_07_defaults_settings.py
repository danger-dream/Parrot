"""Strict OAuth account-model, default-model and settings traces plus coverage gates."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from copy import deepcopy

import pytest

from src import model_metadata, oauth_manager
from src.telegram import states, ui
from src.telegram.menus import oauth_account_models_menu as oam
from src.telegram.menus import oauth_defaults_menu as odm
from src.telegram.menus import oauth_menu as om
from src.tests.tg_contract import assert_capability_coverage
from src.tests.test_tg_contract_oauth_support import (
    ASSIGNED_IDS, SEGMENT, FakeEnv, actual, cases_for, check_trace, load_jsonl,
)

CASES = cases_for("TG-OA-07", "TG-ODM-01", "TG-OA-SET-01")


def _model_env(env, monkeypatch, *, provider="openai", count=14):
    account = env.account(provider, 1, models=[f"{provider}-model-{index:02d}" for index in range(1, count + 1)])
    account["disabledModels"] = [account["models"][-1]]
    env.cfg["oauthAccounts"] = [account]

    def selection(value):
        item = value if isinstance(value, dict) else oauth_manager.get_account(value)
        return {
            "models": list(item.get("models") or []),
            "disabled_models": set(item.get("disabledModels") or []),
            "source": "upstream:fake-catalog", "synced_at": "2030-01-01T00:00:00Z",
            "fallback": False, "error": item.get("modelSyncError", ""),
        }
    monkeypatch.setattr(oauth_manager, "account_model_selection", selection)
    monkeypatch.setattr(oauth_manager, "account_disabled_models", lambda value: selection(value)["disabled_models"])
    def binding(model):
        return model_metadata.MetadataBinding(
            client_visible_model=model, target=f"fake/{model}", provider_id="fake",
            catalog_model_id=model, scope_key="oauth:fake", outbound_model=model,
            source="account", authority="account-upstream", metadata={
                "name": f"Metadata {model}", "description": "Fake catalog description",
                "contextWindow": 128000, "maxOutputTokens": 8192,
                "reasoningEfforts": ["low", "high"], "inputModalities": ["text", "image"],
                "outputModalities": ["text"], "serviceTiers": [{"id": "ultrafast", "name": "Ultra Fast"}],
            },
        )
    monkeypatch.setattr(oam, "_effective_map", lambda key, models: {model: binding(model) for model in models})
    monkeypatch.setattr(oam, "_effective_binding", lambda key, model: binding(model))

    def set_one(key, model, disabled):
        item = oauth_manager.get_account(key)
        values = set(item.get("disabledModels") or [])
        values.add(model) if disabled else values.discard(model)
        item["disabledModels"] = sorted(values)
        env.events.append(["set_model_disabled", key, model, disabled])
        return disabled

    def set_many(key, selected, visible_models=None):
        item = oauth_manager.get_account(key)
        visible = set(visible_models or [])
        hidden = set(item.get("disabledModels") or []) - visible
        item["disabledModels"] = sorted(hidden | set(selected))
        env.events.append(["set_models_disabled", key, sorted(selected), sorted(visible)])
        return set(item["disabledModels"])

    monkeypatch.setattr(oauth_manager, "set_account_model_disabled", set_one)
    monkeypatch.setattr(oauth_manager, "set_account_disabled_models", set_many)
    return account


def _cursor_records(count=14):
    return [{
        "id": f"cursor-model-{index:02d}", "name": f"Cursor Model {index:02d}",
        "context_window": 128_000, "context_window_max_mode": 200_000,
        "reasoning": index % 2 == 0, "supports_images": index % 3 == 0,
        "reasoning_efforts": ["low", "high"] if index % 2 == 0 else [],
    } for index in range(1, count + 1)]


def _patch_cursor(env, monkeypatch, account):
    records = _cursor_records()
    account["models"] = [r["id"] for r in records]
    account["disabledModels"] = [records[-1]["id"]]
    monkeypatch.setattr(om, "_cursor_model_records", lambda acc: deepcopy(records))
    monkeypatch.setattr(oauth_manager, "cursor_disabled_models", lambda acc: set(acc.get("disabledModels") or []))
    monkeypatch.setattr(oauth_manager, "set_cursor_disabled_models", lambda key, selected, **kw: (
        oauth_manager.get_account(key).__setitem__("disabledModels", sorted(selected)),
        env.events.append(["cursor_disabled", key, sorted(selected)]), set(selected),
    )[-1])
    monkeypatch.setattr(oauth_manager, "cursor_max_context_default", lambda acc, model: model in set((acc if isinstance(acc, dict) else oauth_manager.get_account(acc)).get("cursorMaxContextDefault") or []))
    def maxctx(key, model, enabled):
        acc = oauth_manager.get_account(key); values = set(acc.get("cursorMaxContextDefault") or [])
        values.add(model) if enabled else values.discard(model); acc["cursorMaxContextDefault"] = sorted(values)
        env.events.append(["cursor_maxctx", key, model, enabled]); return enabled
    monkeypatch.setattr(oauth_manager, "set_cursor_max_context_default", maxctx)
    return records


def _run_oa07(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    op = case["entry"]["scenario"]
    steps = []
    if op.startswith("oam_"):
        account = _model_env(env, monkeypatch, provider=case["entry"].get("provider", "openai"))
        key = oauth_manager.get_account_key(account); short = ui.register_code(key)
        if op == "oam_list_pages_status":
            env.cooldowns[:] = [
                {"channel_key": f"oauth:{key}", "model": account["models"][1], "cooldown_until": 1_800_000_000_000},
                {"channel_key": f"oauth:{key}", "model": account["models"][2], "cooldown_until": -1},
            ]
            oam.handle_callback(42, 100, "cb-list", f"oam:list:{short}:1:3:quota")
            oam.handle_callback(42, 100, "cb-page", f"oam:list:{short}:2:3:quota")
            oam.handle_callback(42, 100, "cb-noop", "oam:noop")
        elif op == "oam_detail_toggle_clear":
            model = account["models"][1]; ref = ui.register_code(model)
            env.cooldowns[:] = [{"channel_key": f"oauth:{key}", "model": model, "cooldown_until": -1}]
            for kind in ("detail", "toggle", "clear", "toggle"):
                oam.handle_callback(42, 100, f"cb-{kind}", f"oam:{kind}:{short}:{ref}:1:2:available")
        elif op == "oam_bulk_full":
            for callback in (
                f"oam:bulk:{short}:2:3:invalid", f"oam:bsel:{short}:2:2:3:invalid",
                f"oam:ball:{short}:2:3:invalid", f"oam:binv:{short}:2:3:invalid",
                f"oam:bclear:{short}:2:3:invalid", f"oam:bsel:{short}:4:2:3:invalid",
                f"oam:bsave:{short}:2:3:invalid",
            ):
                oam.handle_callback(42, 100, f"cb-{callback}", callback); steps.append(env.state_snapshot(callback))
        elif op == "oam_bulk_cancel_expired":
            oam.handle_callback(42, 100, "cb-expired", "oam:bulk:deadbeef:1:1:all")
            oam.handle_callback(42, 100, "cb-open", f"oam:bulk:{short}:1:1:all")
            oam.handle_callback(42, 100, "cb-cancel", f"oam:bcancel:{short}:1:1:all")
        elif op == "oam_sync":
            async def refresh(key): env.events.append(["refresh_models", key]); return {"action": "updated", "models": 14}
            monkeypatch.setattr(oauth_manager, "refresh_account_models", refresh)
            monkeypatch.setattr(oam.menu_cache, "begin_view", lambda *a: 7)
            monkeypatch.setattr(oam.menu_cache, "is_current_view", lambda *a: True)
            monkeypatch.setattr(oam.menu_cache, "run_if_current", lambda *a: a[-1]() or True)
            oam.handle_callback(42, 100, "cb-sync", f"oam:sync:{short}:1:1:all")
        else: raise AssertionError(op)
        return actual(case, env, state_steps=steps, final=env.final(accountKey=key))
    account = _model_env(env, monkeypatch, provider="cursor")
    records = _patch_cursor(env, monkeypatch, account)
    key = oauth_manager.get_account_key(account); short = ui.register_code(key)
    if op == "cursor_list_detail_maxctx":
        om.on_cursor_models(42, 100, "cb-list", f"{short}:2")
        ref = om._cursor_model_ref(key, records[7]["id"])
        om.on_cursor_model_detail(42, 100, "cb-detail", f"{ref}:2")
        om.on_cursor_max_context_toggle(42, 100, "cb-maxctx", f"{ref}:2")
    elif op == "cursor_bulk_full":
        for callback in (
            f"oa:cursor_disable:{short}:2", f"oa:cursor_dis_sel:{short}:2",
            f"oa:cursor_dis_all:{short}", f"oa:cursor_dis_clear:{short}",
            f"oa:cursor_dis_sel:{short}:3", f"oa:cursor_dis_save:{short}",
        ):
            om.handle_callback(42, 100, f"cb-{callback}", callback); steps.append(env.state_snapshot(callback))
    elif op == "cursor_bulk_cancel_illegal":
        om.handle_callback(42, 100, "cb-expired", "oa:cursor_dis_sel:deadbeef:bad")
        om.handle_callback(42, 100, "cb-open", f"oa:cursor_disable:{short}:1")
        om.handle_callback(42, 100, "cb-illegal", f"oa:cursor_dis_sel:{short}:99")
        om.handle_callback(42, 100, "cb-cancel", f"oa:cursor_dis_cancel:{short}")
    else: raise AssertionError(op)
    return actual(case, env, state_steps=steps, final=env.final(accountKey=key))


def _defaults_config(env):
    env.cfg.update({
        "oauthDefaultModels": ["claude-old", "shared-old"],
        "openaiOAuth": {"defaultModels": ["openai-old", "shared-old"]},
        "xaiOAuth": {"defaultModels": ["xai-old", "shared-old"]},
        "antigravityOAuth": {"defaultModels": ["ag-old", "shared-old"]},
    })


def _run_odm(case, monkeypatch):
    env = FakeEnv(case, monkeypatch); _defaults_config(env)
    op = case["entry"]["scenario"]; steps = []
    if op == "overview":
        odm.handle_callback(42, 100, "cb-show", "odm:show")
        return actual(case, env)
    if op == "family_edit":
        family = case["entry"]["family"]
        monkeypatch.setattr(odm, "_has_live_endpoint", lambda fam: False)
        monkeypatch.setattr(odm, "_static_models", lambda fam: [f"{fam}-model-{i:02d}" for i in range(1, 15)])
        odm.handle_callback(42, 100, "cb-edit", f"odm:edit:{family}")
        return actual(case, env, state_steps=[env.state_snapshot("edit")])
    if op == "discovery_flow":
        family = "xai"
        async def ensure_token(key): return "fake-access-token"
        async def discover(url, token):
            if case["entry"].get("failure"):
                raise odm.ModelsDiscoveryError("fake catalog failure")
            return [f"grok-text-{index:02d}" for index in range(1, 15)] + ["grok-imagine-image"]
        monkeypatch.setattr(oauth_manager, "ensure_valid_token", ensure_token)
        monkeypatch.setattr(odm, "discover_models", discover)
        monkeypatch.setattr(odm, "_first_enabled_account_key", lambda provider: "xai:fake@invalid:subject")
        monkeypatch.setattr(odm.time, "time_ns", lambda: 1_700_000_000_000_000_000)
        monkeypatch.setattr(odm, "_spawn_async_task", lambda factory, name="": asyncio.run(factory()))
        odm.handle_callback(42, 100, "cb-discover", f"odm:edit:{family}")
        return actual(case, env, state_steps=[env.state_snapshot("discovery")])
    if op == "select_flow":
        data = {"family": "openai", "existing_models": ["openai-model-01"], "selected_models": ["openai-model-01"]}
        odm._enter_select(42, 100, data, [f"openai-model-{i:02d}" for i in range(1, 15)], source="live")
        for callback in ("odm:p:1", "odm:t:13:1", "odm:all", "odm:inv", "odm:manual", "odm:backsel", "odm:noop"):
            odm.handle_callback(42, 100, f"cb-{callback}", callback); steps.append(env.state_snapshot(callback))
        return actual(case, env, state_steps=steps)
    if op == "retry_and_expired":
        odm.handle_callback(42, 100, "cb-expired", "odm:retry")
        states.set_state(42, "odm_model_select", {"family": "xai", "existing_models": [], "selected_models": [], "discovered_models": ["xai-a"], "models_source": "live", "discovery_retry_available": True})
        monkeypatch.setattr(odm, "_start_discovery", lambda chat, mid, data: env.events.append(["retry_discovery", data["family"]]))
        odm.handle_callback(42, 100, "cb-retry", "odm:retry")
        return actual(case, env, state_steps=[env.state_snapshot("retry")])
    if op == "manual_input":
        family = case["entry"].get("family", "anthropic")
        states.set_state(42, f"odm_edit:{family}", {"family": family, "existing_models": odm._read_list(family)})
        odm.handle_text_state(42, f"odm_edit:{family}", case["entry"]["text"])
        return actual(case, env, state_steps=[env.state_snapshot("manual")])
    if op == "reference_confirm":
        env.cfg["apiKeys"] = {"fake-key": {"allowedModels": ["shared-old"]}, "keep-other": {"allowedModels": ["shared-old", "other"]}}
        env.cfg["modelMapping"] = {"anthropic": {"alias": "shared-old"}}
        env.cfg["ingressDefaultModel"] = {"anthropic": "shared-old"}
        odm._apply_new_models(42, "anthropic", ["claude-new"], cb_id="cb-apply")
        steps.append(env.state_snapshot("confirm"))
        callback = next(button["callback_data"] for call in env.capture.calls if call["method"] == "sendMessage" for row in call["payload"]["reply_markup"]["inline_keyboard"] for button in row if button["callback_data"].endswith(case["entry"]["mode"]))
        odm.handle_callback(42, 100, "cb-commit", callback)
        return actual(case, env, state_steps=steps)
    if op == "commit_invalid":
        odm.handle_callback(42, 100, "cb-bad-mode", "odm:commit:deadbeef:other")
        odm.handle_callback(42, 100, "cb-expired", "odm:commit:deadbeef:keep")
        odm.handle_callback(42, 100, "cb-unknown", "odm:wat")
        return actual(case, env)
    raise AssertionError(op)


def _run_settings(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    env.cfg.update({
        "oauthDefaultModels": ["claude-default"], "openaiOAuth": {"defaultModels": ["openai-default"]},
        "xaiOAuth": {"defaultModels": ["xai-default"], "imageModels": ["grok-image"], "videoModels": ["grok-video"]},
        "antigravityOAuth": {"defaultModels": ["ag-default"], "imageModels": ["ag-image"]},
        "images": {"enabled": True},
    })
    op = case["entry"]["scenario"]; steps = []
    if op == "settings_and_toggles":
        for callback in ("oa:settings", "oa:usage_mode:toggle", "oa:cch_toggle", "oa:progress_bar:toggle"):
            om.handle_callback(42, 100, f"cb-{callback}", callback)
    elif op == "quota_page_toggle":
        for callback in ("oa:quota", "oa:quota_toggle", "oa:quota_toggle"):
            om.handle_callback(42, 100, f"cb-{callback}", callback)
    elif op.startswith("interval_"):
        om.handle_callback(42, 100, "cb-start", "oa:edit:quota_interval"); steps.append(env.state_snapshot("start"))
        om.handle_text_state(42, "oa_quota_interval", case["entry"]["text"]); steps.append(env.state_snapshot("input"))
    elif op.startswith("threshold_"):
        om.handle_callback(42, 100, "cb-start", "oa:edit:quota_threshold"); steps.append(env.state_snapshot("start"))
        om.handle_text_state(42, "oa_quota_threshold", case["entry"]["text"]); steps.append(env.state_snapshot("input"))
    elif op == "state_expired":
        om.handle_text_state(42, "oa_emax", "3")
        handled = {action: om.handle_text_state(42, action, "1") for action in ("oa_quota_interval", "oa_quota_threshold", "unknown")}
        return actual(case, env, final=env.final(handled=handled))
    else: raise AssertionError(op)
    return actual(case, env, state_steps=steps)


RUNNERS = {"TG-OA-07": _run_oa07, "TG-ODM-01": _run_odm, "TG-OA-SET-01": _run_settings}


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["caseId"])
def test_oauth_07_defaults_settings_strict_trace(case, monkeypatch):
    observed = RUNNERS[case["capabilityId"]](case, monkeypatch)
    expected = deepcopy(case)
    if case["caseId"] == "TG-ODM-01.overview":
        # Preserve the immutable v0.31.13 characterization artifact. This feature
        # intentionally changes only the explanatory overview text; buttons,
        # callbacks, payload shape and all side effects still compare strictly.
        old_text = (
            "仅当某个 OAuth 账户没有可用的实时/LKG 目录时，才作为该账户的无状态兜底；账户故障不会反向修改此列表。\n"
            "Cursor 仍按账号自动同步，不在这里改。"
        )
        new_text = (
            "用于 /v1/models 的普通 OAuth 展示，仅列出同 Provider 启用账户实际支持的默认 ID；清空后该 Provider 不贡献展示项。\n"
            "同时保留账户没有实时/LKG 目录时的无状态兜底用途；不限制已有账户目录中非默认模型的显式调用，账户故障不会反向修改此列表。\n"
            "Cursor 和 WorkBuddy 保留账号原生目录，不在这里改。"
        )
        payload = expected["tgApi"][1]["payload"]
        assert payload["text"].count(old_text) == 1
        payload["text"] = payload["text"].replace(old_text, new_text)
    check_trace(expected, observed)


def _source_callback_families():
    families = set()
    for function in (om.handle_callback, oam.handle_callback, odm.handle_callback):
        for value in re.findall(r'["\']((?:oa|oam|odm):[^"\']*)["\']', inspect.getsource(function)):
            families.add(value + "*" if value.endswith(":") else value)
    return families


def test_segment_schema_unique_ids_capabilities_callback_and_state_bidirectional_coverage():
    cases = load_jsonl(SEGMENT)
    assert_capability_coverage(ASSIGNED_IDS, cases)
    assert len({case["caseId"] for case in cases}) == len(cases)
    serialized = json.dumps(cases, ensure_ascii=False)
    assert "mauth:" not in serialized
    frozen_callbacks = {item for case in cases for item in case["entry"].get("callbackFamilies", [])}
    frozen_states = {item for case in cases for item in case["entry"].get("stateFamilies", [])}
    assert frozen_callbacks == _source_callback_families()
    assert frozen_states == {
        "oa_login_code", "oa_set_json", "oa_openai_code", "oa_openai_rt",
        "oa_xai_code", "oa_xai_rt", "oa_antigravity_code", "oa_openai_import",
        "oa_emax", "oa_quota_interval", "oa_quota_threshold", "oa_cursor_login",
        "oa_oauth_overwrite_confirm", "oa_openai_import_confirm",
        "oa_openai_import_overwrite_confirm", "oa_invalid_remove", "oa_sort",
        "oa_cursor_disable", "oam_bulk_disable", "odm_discovery", "odm_model_select",
        "odm_edit:anthropic|openai|xai|antigravity",
    }


def test_every_fixture_case_has_an_executable_runner_and_no_xim_assignment():
    cases = load_jsonl(SEGMENT)
    assert set(RUNNERS) | {"TG-OA-01", "TG-OA-02", "TG-OA-03", "TG-OA-04", "TG-OA-05", "TG-OA-06"} == ASSIGNED_IDS
    assert all(case["capabilityId"] != "TG-XIM-01" for case in cases)
