"""Executable strict traces for OAuth login, overwrite, import and invalid flows."""
from __future__ import annotations

import concurrent.futures
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src import oauth_manager
from src.oauth import antigravity as antigravity_provider
from src.oauth import cursor as cursor_provider
from src.oauth import openai as openai_provider
from src.oauth import xai as xai_provider
from src.telegram import states, ui
from src.telegram.menus import oauth_menu as om
from src.tests.test_tg_contract_oauth_support import FAKE_NOW, FakeEnv, actual, cases_for, check_trace

CASES = cases_for("TG-OA-04", "TG-OA-05", "TG-OA-06")


@dataclass
class Candidate:
    email: str
    refresh_token: str
    source: str
    def as_state_item(self):
        return {"email": self.email, "refresh_token": self.refresh_token, "source": self.source}


def _patch_random(monkeypatch):
    monkeypatch.setattr(om.secrets, "token_urlsafe", lambda size: f"fake-nonce-{size}")
    monkeypatch.setattr(om.secrets, "token_hex", lambda size: f"fakehex{size}")


def _patch_save(env, monkeypatch, *, duplicate=False):
    def add(entry):
        env.cfg["oauthAccounts"].append(entry)
        env.events.append(["add_identity", oauth_manager.get_account_key(entry)])
        return {"status": "added"}
    monkeypatch.setattr(oauth_manager, "find_exact_identity", lambda entry: (env.key(), env.cfg["oauthAccounts"][0]) if duplicate and env.cfg["oauthAccounts"] else None)
    monkeypatch.setattr(oauth_manager, "add_account_if_identity_absent", add)
    monkeypatch.setattr(om, "_foreground_account_model_sync", lambda *a, **k: env.events.append(["model_sync", a[1], k.get("provider")]))
    monkeypatch.setattr(om, "_fetch_and_save_usage_sync", lambda *a, **k: {})
    monkeypatch.setattr(om, "_evaluate_quota_action", lambda *a, **k: None)


def _start_provider(env, monkeypatch, provider, *, twice=False, failure=False):
    _patch_random(monkeypatch)
    if provider == "claude":
        monkeypatch.setattr(oauth_manager, "pkce_generate", lambda: ("fake-verifier", "fake-challenge"))
        monkeypatch.setattr(oauth_manager, "build_login_url", lambda challenge, state: f"https://fake.invalid/claude?challenge={challenge}&state={state}")
        fn = om.on_login_start
    elif provider == "openai":
        monkeypatch.setattr(openai_provider, "pkce_generate", lambda: ("fake-verifier", "fake-challenge"))
        monkeypatch.setattr(openai_provider, "build_login_url", lambda challenge, state: f"https://fake.invalid/openai?challenge={challenge}&state={state}")
        fn = om.on_login_openai_start
    elif provider == "xai":
        monkeypatch.setattr(xai_provider, "pkce_generate", lambda: ("fake-verifier", "fake-challenge"))
        if failure:
            monkeypatch.setattr(xai_provider, "discover_sync", lambda: (_ for _ in ()).throw(RuntimeError("fake discovery failure")))
        else:
            monkeypatch.setattr(xai_provider, "discover_sync", lambda: {"authorization_endpoint": "https://fake.invalid/xai-auth", "token_endpoint": "https://fake.invalid/xai-token"})
        monkeypatch.setattr(xai_provider, "build_login_url", lambda challenge, state, **kw: f"https://fake.invalid/xai?challenge={challenge}&state={state}")
        monkeypatch.setattr(xai_provider, "redirect_uri", lambda: "http://localhost/fake-xai")
        fn = om.on_login_xai_start
    elif provider == "antigravity":
        monkeypatch.setattr(antigravity_provider, "build_login_url", lambda state: f"https://fake.invalid/ag?state={state}")
        monkeypatch.setattr(antigravity_provider, "token_url", lambda: "https://fake.invalid/ag-token")
        monkeypatch.setattr(antigravity_provider, "redirect_uri", lambda: "http://localhost/fake-ag")
        fn = om.on_login_antigravity_start
    else:
        params = SimpleNamespace(uuid="fake-cursor-uuid", verifier="fake-cursor-verifier", login_url="https://fake.invalid/cursor-login")
        monkeypatch.setattr(cursor_provider, "generate_login", lambda: (_ for _ in ()).throw(RuntimeError("fake login failure")) if failure else params)
        fn = om.on_login_cursor_start
    fn(42, 100, f"cb-{provider}-start")
    if twice and not failure:
        fn(42, 100, f"cb-{provider}-regen")


def _run_oa04(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    _patch_random(monkeypatch)
    op = case["entry"]["scenario"]
    steps = []
    if op == "add_menu_cancel":
        states.set_state(42, "oa_openai_code", {"state": "stale"})
        om.on_add_menu(42, 100, "cb-add")
        steps.append(env.state_snapshot("add_menu"))
        om.on_add_claude(42, 100, "cb-claude")
        om.on_add_openai(42, 100, "cb-openai")
        return actual(case, env, state_steps=steps)
    if op == "start_provider":
        _start_provider(env, monkeypatch, case["entry"]["provider"], twice=case["entry"].get("regen", False), failure=case["entry"].get("failure", False))
        return actual(case, env, state_steps=[env.state_snapshot("start")])
    if op == "absent_provider_entries":
        results = {
            callback: om.handle_callback(42, 100, f"cb-{callback}", callback)
            for callback in ("oa:login:claude:regen", "oa:login:cursor:regen", "oa:login:cursor:code")
        }
        return actual(case, env, final=env.final(dispatchResults=results, sourceFact="v0.31.13 has no corresponding TG callback branch"))
    if op == "claude_code":
        states.set_state(42, "oa_login_code", {"code_verifier": "fake-verifier", "state": "fake-state"})
        if case["entry"].get("expired"):
            states.pop_state(42)
        exchange_calls: list[tuple[str, str, str]] = []

        def exchange_code(code: str, code_verifier: str, state: str) -> dict:
            exchange_calls.append((code, code_verifier, state))
            if case["entry"].get("failure"):
                raise RuntimeError("fake exchange failure")
            return {
                "access_token": "fake-access",
                "refresh_token": "fake-refresh",
                "expires_in": 3600,
            }

        monkeypatch.setattr(oauth_manager, "exchange_code", exchange_code)
        async def profile(_): return {"account": {"email": "claude-login@fake.invalid"}}
        monkeypatch.setattr(oauth_manager, "fetch_profile", profile)
        monkeypatch.setattr(oauth_manager, "extract_claude_plan_info", lambda p: {})
        monkeypatch.setattr(oauth_manager, "claude_plan_label", lambda a: "Fake Plan")
        _patch_save(env, monkeypatch)
        submitted_text = case["entry"].get("text", "fake-code#fake-state")
        om.on_login_code_input(42, submitted_text)
        expected_calls = [] if case["entry"].get("expired") or not (submitted_text or "").strip() else [
            ("fake-code", "fake-verifier", "fake-state")
        ]
        assert exchange_calls == expected_calls
        return actual(case, env, state_steps=[env.state_snapshot("code")])
    if op == "json_input":
        states.set_state(42, "oa_set_json", {})
        _patch_save(env, monkeypatch)
        om.on_set_json_input(42, case["entry"]["text"])
        return actual(case, env, state_steps=[env.state_snapshot("json")])
    if op == "code_provider":
        provider = case["entry"]["provider"]
        action = {"openai": "oa_openai_code", "xai": "oa_xai_code", "antigravity": "oa_antigravity_code"}[provider]
        states.set_state(42, action, {"code_verifier": "fake-verifier", "state": "fake-state", "token_endpoint": "https://fake.invalid/token", "redirect_uri": "http://localhost/fake"})
        _patch_save(env, monkeypatch)
        provider_calls = []
        if provider == "openai":
            def exchange_openai_code(
                code: str,
                code_verifier: str,
                *,
                redirect_uri: str | None = None,
            ) -> dict:
                provider_calls.append((code, code_verifier, redirect_uri))
                if case["entry"].get("failure"):
                    raise RuntimeError("fake exchange failure")
                return {
                    "id_token": "fake-id",
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                }

            monkeypatch.setattr(openai_provider, "exchange_code_sync", exchange_openai_code)
            monkeypatch.setattr(om, "_finish_openai_add", lambda chat, token, source: env.events.append(["finish", "openai", source, sorted(token)]))
            fn = om.on_login_openai_code_input
            expected_call = ("fake-code", "fake-verifier", None)
        elif provider == "xai":
            def exchange_xai_code(
                code: str,
                code_verifier: str,
                *,
                redirect_uri: str | None = None,
                token_endpoint: str | None = None,
            ) -> dict:
                provider_calls.append((code, code_verifier, redirect_uri, token_endpoint))
                if case["entry"].get("failure"):
                    raise RuntimeError("fake exchange failure")
                return {
                    "email": "xai@fake.invalid",
                    "subject": "fake-sub",
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                }

            monkeypatch.setattr(xai_provider, "exchange_code_sync", exchange_xai_code)
            monkeypatch.setattr(om, "_finish_xai_add", lambda chat, token, source: env.events.append(["finish", "xai", source, sorted(token)]))
            fn = om.on_login_xai_code_input
            expected_call = (
                "fake-code",
                "fake-verifier",
                "http://localhost/fake",
                "https://fake.invalid/token",
            )
        else:
            def complete_antigravity_login(
                code: str,
                *,
                redirect_uri: str | None = None,
                token_endpoint: str | None = None,
            ) -> dict:
                provider_calls.append((code, redirect_uri, token_endpoint))
                if case["entry"].get("failure"):
                    raise RuntimeError("fake exchange failure")
                return {
                    "email": "ag@fake.invalid",
                    "project_id": "fake-project",
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                }

            monkeypatch.setattr(antigravity_provider, "complete_login_sync", complete_antigravity_login)
            monkeypatch.setattr(om, "_finish_antigravity_add", lambda chat, token, source: env.events.append(["finish", "antigravity", source, sorted(token)]))
            fn = om.on_login_antigravity_code_input
            expected_call = (
                "fake-code",
                "http://localhost/fake",
                "https://fake.invalid/token",
            )
        submitted_text = case["entry"].get(
            "text", "http://localhost/fake?code=fake-code&state=fake-state",
        )
        fn(42, submitted_text)
        expected_calls = [] if "state=wrong-state" in submitted_text else [expected_call]
        assert provider_calls == expected_calls, (
            f"{provider} provider parameters drifted: {provider_calls!r}"
        )
        return actual(case, env, state_steps=[env.state_snapshot("code")])
    if op == "refresh_token_input":
        provider = case["entry"]["provider"]
        action = "oa_openai_rt" if provider == "openai" else "oa_xai_rt"
        states.set_state(42, action, {})
        module = openai_provider if provider == "openai" else xai_provider
        monkeypatch.setattr(module, "refresh_sync", lambda rt: (_ for _ in ()).throw(RuntimeError("fake refresh failure")) if case["entry"].get("failure") else {"access_token": "fake-access", "id_token": "fake-id"})
        monkeypatch.setattr(om, "_finish_openai_add" if provider == "openai" else "_finish_xai_add", lambda chat, token, source: env.events.append(["finish_rt", provider, source, sorted(token)]))
        (om.on_set_rt_openai_input if provider == "openai" else om.on_set_rt_xai_input)(42, case["entry"].get("text", "fake-refresh-token-value-123456"))
        return actual(case, env, state_steps=[env.state_snapshot("refresh_token")])
    if op == "cursor_done":
        _start_provider(env, monkeypatch, "cursor")
        status = case["entry"]["status"]
        poll_calls: list[tuple[str, str]] = []
        profile_calls: list[tuple[str, str, float]] = []
        tokens = SimpleNamespace(
            access_token="fake-cursor-access",
            refresh_token="fake-cursor-refresh",
            expires_at_ms=1_800_000_000_000,
        )

        def poll_cursor_login(login_uuid: str, verifier: str):
            poll_calls.append((login_uuid, verifier))
            if status == "pending":
                raise cursor_provider.CursorAuthPending("fake pending")
            if status == "failure":
                raise RuntimeError("fake poll failure")
            return tokens

        monkeypatch.setattr(cursor_provider, "poll_login_once", poll_cursor_login)
        if status == "expired":
            state = states.get_state(42); state["data"]["created_at"] = FAKE_NOW - 901
        elif status == "missing":
            states.pop_state(42)
        elif status == "success":
            monkeypatch.setattr(cursor_provider, "subject_from_access_token", lambda token: "fake-cursor-subject")

            def fetch_cursor_profile(
                access_token: str,
                *,
                account_key: str = "",
                timeout: float = 20.0,
            ) -> dict:
                profile_calls.append((access_token, account_key, timeout))
                return {
                    "email": "cursor-login@fake.invalid",
                    "name": "Fake Cursor",
                    "id": "fake-profile",
                    "email_verified": True,
                }

            monkeypatch.setattr(cursor_provider, "fetch_profile_sync", fetch_cursor_profile)
            monkeypatch.setattr(cursor_provider, "fetch_usage_sync", lambda token: {"cursor": {"plan_name": "Fake Pro", "subscription_status": "active"}})
            _patch_save(env, monkeypatch)
            monkeypatch.setattr(om, "_save_usage_to_quota_cache", lambda *a, **k: env.events.append(["cursor_usage_saved", a[0]]) or None)
        om.on_login_cursor_done(42, 100, "cb-cursor-done")
        expected_poll_calls = [] if status in {"missing", "expired"} else [
            ("fake-cursor-uuid", "fake-cursor-verifier")
        ]
        assert poll_calls == expected_poll_calls, (
            f"cursor provider parameters drifted: {poll_calls!r}"
        )
        expected_profile_calls = [
            ("fake-cursor-access", "cursor:fake-cursor-subject", 20.0)
        ] if status == "success" else []
        assert profile_calls == expected_profile_calls, (
            f"cursor profile parameters drifted: {profile_calls!r}"
        )
        return actual(case, env, state_steps=[env.state_snapshot("done")])
    raise AssertionError(op)


def _overwrite_entry(env, provider="openai"):
    account = env.account(provider, 1)
    if provider == "openai": account["workspace_name"] = "Fake Team"
    return account


def _run_oa05(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    _patch_random(monkeypatch)
    provider = case["entry"].get("provider", "openai")
    existing = env.account(provider, 1, access_token="fake-old-access", models=["kept-model"], maxConcurrent=4)
    env.cfg["oauthAccounts"] = [existing]
    incoming = _overwrite_entry(env, provider); incoming["access_token"] = "fake-new-access"
    _patch_save(env, monkeypatch, duplicate=True)
    compared = []
    original_compare = om.secrets.compare_digest
    monkeypatch.setattr(om.secrets, "compare_digest", lambda left, right: (compared.append([left, right]), original_compare(left, right))[1])
    if case["entry"].get("replaceStatus"):
        monkeypatch.setattr(oauth_manager, "replace_exact_identity", lambda target, entry: {"status": case["entry"]["replaceStatus"]})
    om._persist_new_or_stage_overwrite(42, incoming, source="fake login", message_id=100)
    steps = [env.state_snapshot("staged")]
    op = case["entry"]["scenario"]
    nonce = "fake-nonce-12"
    if op == "cancel":
        om.on_oauth_overwrite_cancel(42, 100, "cb-cancel", nonce)
    elif op == "confirm":
        om.on_oauth_overwrite_confirm(42, 100, "cb-confirm", nonce)
        if case["entry"].get("repeat"):
            om.on_oauth_overwrite_confirm(42, 100, "cb-repeat", nonce)
    elif op == "wrong_nonce":
        om.on_oauth_overwrite_confirm(42, 100, "cb-wrong", "wrong-nonce")
    elif op == "wrong_chat":
        om.on_oauth_overwrite_confirm(43, 100, "cb-wrong-chat", nonce)
    elif op == "expired":
        states._states[42]["ts"] = FAKE_NOW - 601
        om.on_oauth_overwrite_confirm(42, 100, "cb-expired", nonce)
    steps.append(env.state_snapshot(op))
    return actual(case, env, state_steps=steps, final=env.final(compareDigest=compared, targetKey=env.key()))


def _patch_delete(env, monkeypatch, *, fail_second=False):
    calls = {"n": 0}
    def delete(key):
        calls["n"] += 1
        if fail_second and calls["n"] == 2:
            raise RuntimeError("fake delete failure")
        env.cfg["oauthAccounts"] = [a for a in env.cfg["oauthAccounts"] if oauth_manager.get_account_key(a) != key]
        env.events.append(["delete", key])
    monkeypatch.setattr(oauth_manager, "delete_account", delete)


def _run_oa06(case, monkeypatch):
    env = FakeEnv(case, monkeypatch)
    _patch_random(monkeypatch)
    op = case["entry"]["scenario"]
    steps = []
    if op == "import_start":
        om.on_import_openai_start(42, 100, "cb-import", case["entry"]["kind"])
        return actual(case, env, state_steps=[env.state_snapshot("start")])
    if op in {"import_text", "import_document"}:
        kind = case["entry"].get("kind", "sub2api")
        states.set_state(42, "oa_openai_import", {"kind": kind})
        items = [Candidate("one@fake.invalid", "fake-rt-one", "fake-source"), Candidate("two@fake.invalid", "fake-rt-two", "fake-source")]
        monkeypatch.setattr(om, "parse_openai_import_payload", lambda *a, **k: items)
        if case["entry"].get("failure"):
            monkeypatch.setattr(om, "parse_openai_import_payload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fake parse failure")))
        if op == "import_text":
            om.on_import_openai_text_input(42, "{\"fake\":true}")
        else:
            class FakeDownloadResponse:
                content = b"{}"
                def raise_for_status(self): return None
            class FakeDownloadSession:
                def get(self, url):
                    env.events.append(["telegram_file_transport", "fake-session"])
                    return FakeDownloadResponse()
            monkeypatch.setattr(
                ui, "_session_lease", lambda: nullcontext(FakeDownloadSession())
            )
            msg = {"document": {"file_id": case["entry"].get("fileId", "fake-file"), "file_name": "fake.json"}}
            om.on_import_openai_document_input(42, msg)
        return actual(case, env, state_steps=[env.state_snapshot(op)])
    if op == "import_cancel":
        states.set_state(42, "oa_openai_import_confirm", {"kind": "cpa", "items": [{"email": "one@fake.invalid", "refresh_token": "fake-rt"}]})
        om.on_import_openai_cancel(42, 100, "cb-cancel")
        return actual(case, env, state_steps=[env.state_snapshot("cancel")])
    if op.startswith("import_exec"):
        states.set_state(42, "oa_openai_import_confirm", {"kind": "sub2api", "items": [{"email": "one@fake.invalid", "refresh_token": "fake-rt"}]})
        duplicate = case["entry"].get("duplicate", False)
        entry = env.account("openai", 1, email="one@fake.invalid")
        key = oauth_manager.get_account_key(entry)
        staged = {"new": [] if duplicate else [{"account_key": key, "entry": entry, "meta": {}}], "duplicate": [{"account_key": key, "entry": entry, "meta": {}}] if duplicate else [], "failed": []}
        if duplicate:
            env.cfg["oauthAccounts"] = [env.account("openai", 1, email="one@fake.invalid", access_token="fake-old-access", models=["kept-model"], maxConcurrent=3)]
        monkeypatch.setattr(om, "_stage_openai_import_candidates", lambda items: staged)
        monkeypatch.setattr(oauth_manager, "add_account_if_identity_absent", lambda value: (env.cfg["oauthAccounts"].append(value), env.events.append(["import_add", oauth_manager.get_account_key(value)]), {"status": "added"})[-1])
        monkeypatch.setattr(om, "_fetch_and_save_usage_sync", lambda *a, **k: {})
        monkeypatch.setattr(om, "_evaluate_quota_action", lambda *a, **k: None)
        def sync_future(account_key):
            future = concurrent.futures.Future(); future.set_result({"action": "updated", "models": 8}); return future
        monkeypatch.setattr(oauth_manager, "start_account_model_refresh", sync_future)
        om.on_import_openai_exec(42, 100, "cb-exec")
        steps.append(env.state_snapshot("exec"))
        if duplicate:
            nonce = "fake-nonce-12"
            if case["entry"].get("wrong"):
                nonce = "wrong"
            om.on_import_openai_overwrite(42, 100, "cb-overwrite", nonce, confirm=case["entry"].get("confirm", False))
            steps.append(env.state_snapshot("overwrite"))
        return actual(case, env, state_steps=steps)
    if op == "sync_result_matrix":
        for result in (
            {"added": ["a"], "replaced": [], "failed": [], "model_sync": {"success": 1, "failed": 0, "background": 0}},
            {"added": [], "replaced": ["b"], "failed": [["c", "fake failure"]], "model_sync": {"success": 0, "failed": 1, "background": 1}},
        ):
            om._render_openai_import_result(42, 100, "Sub2API", result)
        return actual(case, env)
    if op.startswith("invalid_"):
        env.cfg["oauthAccounts"] = [
            env.account("claude", 1, enabled=False, disabled_reason="auth_error"),
            env.account("openai", 2, enabled=False, disabled_reason="auth_error"),
            env.account("xai", 3),
        ]
        _patch_delete(env, monkeypatch, fail_second=case["entry"].get("failure", False))
        om.on_invalid_remove_start(42, 100, "cb-invalid")
        steps.append(env.state_snapshot("list"))
        if op == "invalid_selected":
            short = ui.register_code(oauth_manager.get_account_key(env.cfg["oauthAccounts"][0]))
            om.on_invalid_remove_toggle(42, 100, "cb-toggle", short)
            om.on_invalid_remove_exec(42, 100, "cb-remove", all_items=False)
        elif op == "invalid_all":
            om.on_invalid_remove_exec(42, 100, "cb-remove-all", all_items=True)
        elif op == "invalid_none":
            om.on_invalid_remove_exec(42, 100, "cb-remove-none", all_items=False)
        steps.append(env.state_snapshot(op))
        return actual(case, env, state_steps=steps)
    raise AssertionError(op)


RUNNERS = {"TG-OA-04": _run_oa04, "TG-OA-05": _run_oa05, "TG-OA-06": _run_oa06}


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["caseId"])
def test_oauth_04_06_strict_trace(case, monkeypatch):
    observed = RUNNERS[case["capabilityId"]](case, monkeypatch)
    expected = case
    if case["caseId"] in {"TG-OA-04.add_menu_cancel", "TG-OA-06.import_cancel"}:
        # The v0.31.13 recording stays immutable. This explicitly approved
        # feature delta adds two WorkBuddy rows and one Zhipu row, without masking
        # any actual output, state changes, existing labels, or old row order.
        from copy import deepcopy
        expected = deepcopy(case)
        payload = expected["tgApi"][1]["payload"]
        assert payload["text"].startswith("<b>新增 OAuth 账户</b>")
        payload["text"] += '、<tg-emoji emoji-id="6120617435214132136">✉</tg-emoji> WorkBuddy'
        rows = payload["reply_markup"]["inline_keyboard"]
        assert len(rows) == 12 and rows[-2][0]["callback_data"] == "menu:oauth"
        rows[-2:-2] = [
            [{"text": "WorkBuddy 中国区登录", "callback_data": "oa:wb:login", "icon_custom_emoji_id": "6120617435214132136"}],
            [{"text": "WorkBuddy 国际区登录", "callback_data": "oa:wb:login:global", "icon_custom_emoji_id": "6120617435214132136"}],
            [{"text": "智谱 / Z.ai（Key / OAuth）", "callback_data": "oa:zh:add", "icon_custom_emoji_id": "6140727700454645813"}],
        ]
    check_trace(expected, observed)


@pytest.mark.parametrize(
    ("case_id", "control_method", "mutation", "failure_message"),
    (
        (
            "TG-OA-04.openai_code_success",
            "openai_exchange_code",
            "openai",
            "openai provider parameters drifted",
        ),
        (
            "TG-OA-04.xai_code_success",
            "xai_exchange_code",
            "xai",
            "xai provider parameters drifted",
        ),
        (
            "TG-OA-04.antigravity_code_success",
            "antigravity_complete_login",
            "antigravity",
            "antigravity provider parameters drifted",
        ),
        (
            "TG-OA-04.cursor_done_success",
            "cursor_poll_login",
            "cursor_poll",
            "cursor provider parameters drifted",
        ),
        (
            "TG-OA-04.cursor_done_success",
            "cursor_profile",
            "cursor_profile",
            "cursor profile parameters drifted",
        ),
    ),
    ids=("openai", "xai", "antigravity", "cursor-poll", "cursor-profile"),
)
def test_oauth_04_wrong_provider_parameters_are_rejected(
    case_id,
    control_method,
    mutation,
    failure_message,
    monkeypatch,
):
    case = next(item for item in CASES if item["caseId"] == case_id)
    original = getattr(om.oauth_control, control_method)

    if mutation == "openai":
        def mutate_openai(code, code_verifier, *, redirect_uri=None):
            return original(
                "WRONG-CODE",
                "WRONG-VERIFIER",
                redirect_uri="wrong://redirect",
            )

        mutated = mutate_openai
    elif mutation == "xai":
        def mutate_xai(
            code,
            code_verifier,
            *,
            redirect_uri=None,
            token_endpoint=None,
        ):
            return original(
                "WRONG-CODE",
                "WRONG-VERIFIER",
                redirect_uri="wrong://redirect",
                token_endpoint="wrong://token",
            )

        mutated = mutate_xai
    elif mutation == "antigravity":
        def mutate_antigravity(code, *, redirect_uri=None, token_endpoint=None):
            return original(
                "WRONG-CODE",
                redirect_uri="wrong://redirect",
                token_endpoint="wrong://token",
            )

        mutated = mutate_antigravity
    elif mutation == "cursor_poll":
        def mutate_cursor_poll(login_uuid, verifier):
            return original("WRONG-UUID", "WRONG-VERIFIER")

        mutated = mutate_cursor_poll
    else:
        def mutate_cursor_profile(access_token, *, account_key="", timeout=20.0):
            return original(
                "WRONG-ACCESS-TOKEN",
                account_key="cursor:wrong-account-key",
                timeout=timeout,
            )

        mutated = mutate_cursor_profile

    monkeypatch.setattr(om.oauth_control, control_method, mutated)
    with pytest.raises(AssertionError, match=failure_message):
        observed = _run_oa04(case, monkeypatch)
        check_trace(case, observed)
