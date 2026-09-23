"""Current TG-OA-03 Claude reset traces: real callbacks and confirmation control.

Only provider status/redeem boundaries are fake here, as in the archived OAuth
trace harness. test_claude_reset.py separately exercises the same chain through
real backend/persistence with network-only fakes. No golden-update path.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import itertools

from src.management_control.oauth.control import OAuthControl
from src.management_control.oauth import plans
from src.telegram import ui
from src.telegram.menus import claude_reset_menu as cm
from src.telegram.menus import oauth_menu as om
from src.tests.test_tg_contract_oauth_support import FAKE_NOW, FakeEnv, actual


SCENARIOS = {
    "claude_reset_cedar_success_duplicate": "cedar_ember",
    "claude_reset_juniper_unconfirmed_duplicate": "juniper_tide",
    "claude_reset_stage_and_expiry_guard": "cedar_ember",
}


def run_claude_reset(case, monkeypatch):
    scenario = case["entry"]["scenario"]
    program = SCENARIOS[scenario]
    env = FakeEnv(case, monkeypatch)
    key = env.key()
    observed = deepcopy(case["initialRuntime"]["resetStatus"])
    sequence = itertools.count(1)
    monkeypatch.setattr(plans.secrets, "token_urlsafe", lambda size: f"fake-claude-plan-{next(sequence):02d}-{size}")
    now = [datetime.fromtimestamp(FAKE_NOW, timezone.utc)]
    control = OAuthControl(clock=lambda: now[0])
    monkeypatch.setattr(cm, "control", control)
    monkeypatch.setattr(om, "oauth_control", control)
    calls = []

    async def status(account_id, selected_program):
        assert account_id == key
        env.events.append(["claude_reset_status", account_id, selected_program])
        return deepcopy(observed)

    async def redeem(account_id, selected_program, **kwargs):
        assert account_id == key
        assert kwargs["expected"] == observed["generation"]
        assert kwargs["organization_uuid"] == "org-contract"
        assert kwargs["grant_id"] == ("g_contract" if selected_program == "cedar_ember" else None)
        assert kwargs["operation_id"]
        calls.append({"account_id": account_id, "program": selected_program, **kwargs})
        env.events.append(["claude_reset_redeem", deepcopy(calls[-1])])
        return deepcopy(case["initialRuntime"]["resetResponse"])

    monkeypatch.setattr(control.backend, "claude_reset_status", status)
    monkeypatch.setattr(control.backend, "claude_reset_redeem", redeem)
    handled, stages = [], []
    families = set()

    def dispatch(label, callback):
        family = callback.split(":", 2)[1]
        families.add(f"oa:{family}:*")
        assert len(callback.encode()) <= 64
        result = om.handle_callback(42, 100, f"cb-{label}", callback)
        assert result is True
        handled.append({"stage": label, "callback": callback, "handled": result})
        stages.append({"after": label, "providerClaims": len(calls)})

    def button(prefix, *, contains=""):
        edits = [call for call in env.capture.calls if call["method"] == "editMessageText"]
        buttons = [b for row in edits[-1]["payload"]["reply_markup"]["inline_keyboard"] for b in row]
        return next(b["callback_data"] for b in buttons
                    if b["callback_data"].startswith(prefix) and contains in b["text"])

    if scenario == "claude_reset_stage_and_expiry_guard":
        dispatch("invalid-confirm", f"oa:claude_reset_confirm:{env.short()}:2:quota")
        assert not calls
    dispatch("ask", f"oa:claude_reset_ask:{env.short()}:2:quota")
    assert not calls
    chosen = button("oa:claude_reset_confirm:", contains="周额度重置卡" if program == "cedar_ember" else "5h重置")
    if scenario == "claude_reset_stage_and_expiry_guard":
        dispatch("bypass-final-confirm", chosen.replace("claude_reset_confirm:", "claude_reset_execute:", 1))
        assert not calls
    dispatch("confirm", chosen)
    assert not calls
    final = button("oa:claude_reset_execute:")
    if scenario == "claude_reset_stage_and_expiry_guard":
        from datetime import timedelta
        now[0] += timedelta(seconds=601)
        dispatch("expired-execute", final)
        assert not calls
    else:
        dispatch("execute", final)
        assert len(calls) == 1 and calls[0]["program"] == program
        dispatch("duplicate-execute", final)
        assert len(calls) == 1
    # Coverage declarations are checked against callbacks ACTUALLY dispatched,
    # not satisfied by listing newly added names in fixture metadata alone.
    assert families == set(case["entry"]["callbackFamilies"])
    assert case["entry"]["stateFamilies"] == []
    return actual(case, env, state_steps=stages,
                  final=env.final(accountKey=key, providerClaims=calls, callbacks=handled))
