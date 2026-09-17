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


RUNNERS = {"TG-OA-07": _run_oa07, "TG-OA-SET-01": _run_settings}


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["caseId"])
def test_oauth_07_defaults_settings_strict_trace(case, monkeypatch):
    if case["capabilityId"] == "TG-ODM-01":
        pytest.skip("Retired 2026-09-16: OAuth fallback; immutable historical trace only")
    # Explicit reviewed retirement delta, not regenerated golden recordings:
    # remove only the exact retired button/phrase, compare all other fields strictly.
    expected = deepcopy(case)
    buttons = phrases = 0
    for call in expected["tgApi"]:
        payload = call.get("payload", {})
        for row in payload.get("reply_markup", {}).get("inline_keyboard", []):
            retired = {"callback_data": "odm:show", "text": "🧬 默认模型"}
            if retired in row:
                row.remove(retired)
                buttons += 1
        old = "模型目录、备用模型与媒体设置"
        if old in payload.get("text", ""):
            phrases += payload["text"].count(old)
            payload["text"] = payload["text"].replace(old, "模型目录与媒体设置")
    deltas = {
        "TG-OA-07.oam_list_pages_status": (2, 0),
        "TG-OA-07.oam_bulk_full": (1, 0),
        "TG-OA-07.oam_bulk_cancel_expired": (1, 0),
        "TG-OA-07.oam_sync": (2, 0),
        "TG-OA-SET-01.settings_toggles": (0, 4),
    }
    assert (buttons, phrases) == deltas.get(case["caseId"], (0, 0))
    check_trace(expected, RUNNERS[case["capabilityId"]](case, monkeypatch))


def _source_callback_families():
    families = set()
    for function in (om.handle_callback, oam.handle_callback):
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
    retired_callbacks = {item for item in frozen_callbacks if item.startswith("odm:")}
    assert retired_callbacks == {"odm:*"}
    assert frozen_callbacks - retired_callbacks == _source_callback_families()
    assert frozen_states == {
        "oa_login_code", "oa_set_json", "oa_openai_code", "oa_openai_rt",
        "oa_xai_code", "oa_xai_rt", "oa_antigravity_code", "oa_openai_import",
        "oa_emax", "oa_quota_interval", "oa_quota_threshold", "oa_cursor_login",
        "oa_oauth_overwrite_confirm", "oa_openai_import_confirm",
        "oa_openai_import_overwrite_confirm", "oa_invalid_remove", "oa_sort",
        "oa_cursor_disable", "oam_bulk_disable", "odm_discovery", "odm_model_select",
        "odm_edit:anthropic|openai|xai|antigravity",
    }


def test_every_fixture_case_has_a_runner_or_explicit_retirement_and_no_xim_assignment():
    cases = load_jsonl(SEGMENT)
    assert set(RUNNERS) | {"TG-ODM-01", "TG-OA-01", "TG-OA-02", "TG-OA-03", "TG-OA-04", "TG-OA-05", "TG-OA-06"} == ASSIGNED_IDS
    assert all(case["capabilityId"] != "TG-XIM-01" for case in cases)
