"""Executable strict traces for OAuth list, usage and runtime actions."""
from __future__ import annotations

from copy import deepcopy
import uuid

import pytest

from src import cooldown, oauth_manager
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as om
from src.tests.test_tg_contract_oauth_support import (
    DeferredThread, FakeEnv, actual, cases_for, check_trace,
)

CASES = cases_for("TG-OA-01", "TG-OA-02", "TG-OA-03")


def _patch_actions(env, monkeypatch):
    def set_enabled(key, enabled, reason=None):
        account = oauth_manager.get_account(key)
        account["enabled"] = enabled
        account["disabled_reason"] = None if enabled else (reason or "user")
        env.events.append(["set_enabled", key, enabled, reason])
        return True

    def delete_account(key):
        before = len(env.cfg["oauthAccounts"])
        env.cfg["oauthAccounts"] = [a for a in env.cfg["oauthAccounts"] if oauth_manager.get_account_key(a) != key]
        env.events.append(["delete_account", key, before - len(env.cfg["oauthAccounts"])])

    monkeypatch.setattr(oauth_manager, "set_enabled", set_enabled)
    monkeypatch.setattr(oauth_manager, "delete_account", delete_account)
    monkeypatch.setattr(oauth_manager, "update_max_concurrent", lambda key, value: (
        env.cfg["oauthAccounts"][0].__setitem__("maxConcurrent", value),
        env.events.append(["max_concurrent", key, value]),
    )[-1])


def _run_oa01(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    op = case["entry"]["scenario"]
    steps = []
    if op.startswith("list_"):
        env.seed_accounts(count=9)
        # Deliberately create every filter state in the same provider-rich list.
        env.cfg["oauthAccounts"][1].update(enabled=False, disabled_reason="user")
        env.cfg["oauthAccounts"][2].update(enabled=False, disabled_reason="quota")
        env.cfg["oauthAccounts"][3].update(enabled=False, disabled_reason="auth_error")
        filt = op.removeprefix("list_")
        data = "menu:oauth" if filt == "all" else f"oa:page:1:{filt}"
        om.handle_callback(42, 100, "cb-list", data)
        return actual(case, env, final=env.final(filter=filt, accountKeys=[oauth_manager.get_account_key(a) for a in env.cfg["oauthAccounts"]]))
    if op == "page_noop_and_parser":
        om.handle_callback(42, 100, "cb-noop", "oa:page:noop")
        matrix = {
            raw: list(om._parse_page_filter(raw, default_page=2, default_filter="available"))
            for raw in ("", "noop", "3", "4:quota", "bad:invalid", "-2", "7:unknown")
        }
        keyboards = {
            "small": om._build_pagination_row(2, 3, "quota"),
            "large": om._build_pagination_row(7, 14, "invalid"),
        }
        return actual(case, env, final=env.final(parser=matrix, keyboards=keyboards))
    if op == "detail_provider_matrix":
        env.seed_accounts()
        monthly = {"total": 3, "success_count": 2, "error_count": 1, "input": 1000, "output": 200, "cache_creation": 100, "cache_read": 400, "avg_tps": 12.5, "max_tps": 20.0, "min_tps": 4.0, "total_cost": 1.25}
        monkeypatch.setattr(om, "_account_period_stats", lambda *a, **k: monthly)
        rendered = {}
        for index, account in enumerate(env.cfg["oauthAccounts"]):
            key = oauth_manager.get_account_key(account)
            env.quota[key] = {"fetched_at": int(1_700_000_000_000), "five_hour_util": 12 + index, "five_hour_reset": "2030-01-02T00:00:00Z", "seven_day_util": 45 + index, "seven_day_reset": "2030-01-08T00:00:00Z", "raw_data": "{}"}
            text, keyboard = om._detail_text_and_kb(key, page=2, filter_key="available", refresh_quota=False, month_snapshot={}, model_stats=[])
            rendered[oauth_manager.provider_of(account)] = {"text": text, "keyboard": keyboard}
        return actual(case, env, final=env.final(rendered=rendered))
    if op == "sort_full_flow":
        env.seed_accounts(providers=["claude"], count=5)
        for callback in (
            "oa:sort:2:available", "oa:sort_sel:2", "oa:sort_mv:top",
            "oa:sort_mv:down", "oa:sort_mv:bottom", "oa:sort_mv:up",
            "oa:sort_reset", "oa:sort_sel:5", "oa:sort_save",
        ):
            om.handle_callback(42, 100, f"cb-{callback}", callback)
            steps.append(env.state_snapshot(callback))
        return actual(case, env, state_steps=steps, final=env.final(order=[a["email"] for a in env.cfg["oauthAccounts"]]))
    if op == "sort_cancel_and_illegal":
        env.seed_accounts(providers=["claude"], count=5)
        om.handle_callback(42, 100, "cb-sort-start", "oa:sort:2:quota")
        for callback in ("oa:sort_sel:x", "oa:sort_sel:9", "oa:sort_mv:sideways", "oa:sort_mv:top", "oa:sort_cancel"):
            om.handle_callback(42, 100, f"cb-{callback}", callback)
            steps.append(env.state_snapshot(callback))
        return actual(case, env, state_steps=steps)
    if op == "sort_expired":
        env.seed_accounts(providers=["claude"], count=5)
        om.handle_callback(42, 100, "cb-expired", "oa:sort_save")
        return actual(case, env, state_steps=[env.state_snapshot("expired_save")])
    raise AssertionError(op)


def _run_oa02(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    op = case["entry"]["scenario"]
    if op == "usage_exact_format":
        values = {}
        for mode in ("used", "remaining"):
            env.cfg["oauthUsageDisplayMode"] = mode
            for progress in (True, False):
                env.cfg["quotaProgressBar"] = progress
                values[f"{mode}:{progress}"] = {
                    "html": om._format_usage_line_html("5h", 12.5, "2030-01-02T00:00:00Z"),
                    "text": om._format_usage_line_text("7d", 87.4, "2030-01-08T00:00:00Z"),
                    "bar": om._usage_progress_bar(55),
                    "indent": "\u00a0" * 7,
                }
        return actual(case, env, final=env.final(values=values))
    if op == "provider_usage_blocks":
        env.seed_accounts()
        blocks = {}
        for index, account in enumerate(env.cfg["oauthAccounts"]):
            key = oauth_manager.get_account_key(account)
            provider = oauth_manager.provider_of(account)
            env.quota[key] = {
                "fetched_at": 1_700_000_000_000,
                "five_hour_util": 10 + index, "five_hour_reset": "2030-01-02T00:00:00Z",
                "seven_day_util": 20 + index, "seven_day_reset": "2030-01-08T00:00:00Z",
                "raw_data": "{}",
            }
            blocks[provider] = om._format_usage_block(key)
        return actual(case, env, final=env.final(blocks=blocks))
    env.seed_accounts(providers=[case["entry"].get("provider", "claude")], count=1)
    _patch_actions(env, monkeypatch)
    key, short = env.key(), env.short()
    if op == "refresh_token":
        async def force_refresh(account_key):
            env.events.append(["force_refresh", account_key])
            if case["entry"].get("failure"):
                raise RuntimeError("fake refresh rejected")
            return "fake-new-access"
        monkeypatch.setattr(oauth_manager, "force_refresh", force_refresh)
        async def cursor_models(*args, **kwargs):
            return {"action": "updated", "models": 8}
        monkeypatch.setattr(oauth_manager, "refresh_cursor_models", cursor_models)
        monkeypatch.setattr(om, "_fetch_and_save_usage_sync", lambda *a, **k: {"five_hour_util": 11})
        monkeypatch.setattr(om, "_evaluate_quota_action", lambda *a, **k: {"action": "kept_enabled"})
        om.on_refresh_token(42, 100, "cb-refresh-token", short, page=2, filter_key="quota")
        return actual(case, env, final=env.final(accountKey=key))
    if op == "refresh_usage":
        usage = case["entry"].get("usage") or {"five_hour_util": 15, "seven_day_util": 33}
        error = RuntimeError("fake usage rejected") if case["entry"].get("failure") else None
        monkeypatch.setattr(om, "_fetch_and_save_usage_result_sync", lambda *a, **k: {"usage": None if error else usage, "error": error, "reset_credit_error": None})
        monkeypatch.setattr(om, "_evaluate_quota_action", lambda *a, **k: case["entry"].get("quotaAction"))
        async def metadata(*a, **k): return {"action": "updated", "models": 8, "fields": {"plan_type": "Fake Pro"}}
        monkeypatch.setattr(oauth_manager, "ensure_openai_metadata_fresh", metadata)
        monkeypatch.setattr(oauth_manager, "refresh_cursor_models", metadata)
        om.on_refresh_usage(42, 100, "cb-refresh-usage", short, page=3, filter_key="invalid")
        return actual(case, env, final=env.final(accountKey=key))
    if op == "refresh_all_nonblocking_singleflight":
        monkeypatch.setattr(om, "_refreshable_account_keys_for_ui", lambda accounts, *, explicit=False: [key])
        monkeypatch.setattr(om.threading, "Thread", DeferredThread)
        om._BACKGROUND_REFRESH_INFLIGHT.clear()
        om.on_refresh_all(42, 100, "cb-all", page=2, filter_key="available")
        pending_after_return = len(DeferredThread.pending)
        # Characterize the worker's per-account single-flight branch without waiting.
        tick = [1_700_000_000.0]
        def advancing_time():
            tick[0] += 121.0
            return tick[0]
        monkeypatch.setattr(om.time, "time", advancing_time)
        monkeypatch.setattr(om.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(om, "_quota_cache_has_usage_signal", lambda row: False)
        monkeypatch.setattr(om, "_fetch_and_save_usage_result_sync", lambda *a, **k: env.events.append(["unexpected_provider_call"]))
        om._BACKGROUND_REFRESH_INFLIGHT.add(key)
        om._run_oauth_update_panel(42, -1, [key], show_transition_hint=False)
        om._BACKGROUND_REFRESH_INFLIGHT.clear()
        return actual(case, env, final=env.final(pendingWorkers=pending_after_return, providerCallsBeforeReturn=0, threadEvents=DeferredThread.events))
    if op == "refresh_all_empty":
        env.cfg["oauthAccounts"] = []
        om.on_refresh_all(42, 100, "cb-empty")
        return actual(case, env)
    raise AssertionError(op)


def _run_oa03(case, monkeypatch):
    if case["entry"]["scenario"].startswith("claude_reset_"):
        from src.tests.tg_contract.claude_reset_runner import run_claude_reset
        return run_claude_reset(case, monkeypatch)
    env = FakeEnv(case, monkeypatch)
    env.seed_accounts(providers=[case["entry"].get("provider", "claude")], count=1)
    _patch_actions(env, monkeypatch)
    key, short = env.key(), env.short()
    op = case["entry"]["scenario"]
    steps = []
    if op == "toggle_cycle":
        om.on_toggle(42, 100, "cb-off", short, 2, "available")
        om.on_toggle(42, 100, "cb-on", short, 2, "available")
    elif op == "clear_runtime":
        env.cooldowns[:] = [{"channel_key": f"oauth:{key}", "model": "m1", "cooldown_until": -1}]
        om.on_clear_errors(42, 100, "cb-errors", short)
        om.on_clear_affinity(42, 100, "cb-affinity", short)
    elif op == "clear_all":
        env.cfg["oauthAccounts"].append(env.account("openai", 2))
        monkeypatch.setattr(cooldown, "clear", env.cooldown_clear)
        om.on_clear_all_errors(42, 100, "cb-clear-all", page=2, filter_key="quota")
    elif op == "delete_ask_exec":
        om.on_delete_ask(42, 100, "cb-ask", short, 2, "invalid")
        om.on_delete_exec(42, 100, "cb-exec", short, 2, "invalid")
    elif op == "delete_failure":
        monkeypatch.setattr(oauth_manager, "delete_account", lambda key: (_ for _ in ()).throw(RuntimeError("fake registry failure")))
        om.on_delete_exec(42, 100, "cb-exec", short)
    elif op.startswith("max_"):
        om.on_edit_max_concurrent(42, 100, "cb-max", short, 2, "quota")
        steps.append(env.state_snapshot("start"))
        if op == "max_expired":
            states.pop_state(42)
        if op == "max_failure":
            monkeypatch.setattr(oauth_manager, "update_max_concurrent", lambda *a: (_ for _ in ()).throw(RuntimeError("fake config failure")))
        om.on_edit_max_concurrent_input(42, case["entry"].get("text", "3"))
        steps.append(env.state_snapshot("input"))
    elif op == "quota_non_openai_noop":
        om.on_reset_quota_ask(42, 100, "cb-reset", short)
    elif op == "quota_local_outcomes":
        outcomes = ["reset", "already_enabled", "cleared_runtime_state", "reset_failed", "state_conflict", "invalid_state", "noop_user", "noop_auth_error", "other"]
        for outcome in outcomes:
            monkeypatch.setattr(oauth_manager, "reset_quota", lambda key, value=outcome: {"action": value, "required_state_cleared": False})
            om.on_reset_quota(42, 100, f"cb-{outcome}", short)
    elif op == "quota_openai_confirmation":
        monkeypatch.setattr(om, "_fetch_and_save_usage_sync", lambda *a, **k: {"reset_credit_count": 2, "reset_credit_details": {"available_count": 2}})
        monkeypatch.setattr(om, "_openai_reset_credit_count_from_usage", lambda usage: 2)
        monkeypatch.setattr(om.uuid, "uuid4", lambda: uuid.UUID("00000000-0000-0000-0000-000000000123"))
        om.on_reset_quota_ask(42, 100, "cb-ask", short, 2, "quota")
        confirm = next(call["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] for call in env.capture.calls if call["method"] == "editMessageText")
        om.handle_callback(42, 100, "cb-confirm", confirm)
    elif op == "quota_openai_invalid_direct":
        om.on_reset_quota(42, 100, "cb-direct", short)
    elif op == "quota_openai_execute_outcomes":
        for outcome in ("reset", "alreadyRedeemed", "nothingToReset", "noCredit", "unknown"):
            token = ui.register_code(f"{key}|fake-idempotency-{outcome}|execute")
            async def redeem(account_key, idempotency_key, value=outcome):
                env.events.append(["redeem_reset_credit", account_key, idempotency_key, value])
                return {"outcome": value, "available_count": 1, "quota_action": {"action": "resumed"}}
            monkeypatch.setattr(oauth_manager, "redeem_openai_rate_limit_reset_credit", redeem)
            om.on_reset_quota(42, 100, f"cb-{outcome}", token)
    elif op == "quota_openai_execute_failure":
        token = ui.register_code(f"{key}|fake-idempotency-failure|execute")
        async def redeem_failure(*args, **kwargs):
            raise RuntimeError("fake official reset failure")
        monkeypatch.setattr(oauth_manager, "redeem_openai_rate_limit_reset_credit", redeem_failure)
        om.on_reset_quota(42, 100, "cb-failure", token)
    else:
        raise AssertionError(op)
    return actual(case, env, state_steps=steps, final=env.final(accountKey=key))


RUNNERS = {"TG-OA-01": _run_oa01, "TG-OA-02": _run_oa02, "TG-OA-03": _run_oa03}


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["caseId"])
def test_oauth_01_03_strict_trace(case, monkeypatch):
    check_trace(case, RUNNERS[case["capabilityId"]](case, monkeypatch))
