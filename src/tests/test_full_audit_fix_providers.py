"""P-01..P-06: synthetic credentials, real lifecycle/threads, isolated transport."""
from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from contextlib import suppress

import httpx
import pytest

from src import affinity, channel_state, config, cooldown, notifier, oauth_errors
from src import oauth_manager as om, oauth_model_discovery as discovery
from src import provider_usage as pu, scorer, state_db
from src.channel import registry
from src.providers import antigravity_codec, remote_image


PROVIDERS = ["claude", "openai", "xai", "antigravity", "cursor"]


@pytest.fixture
def private(monkeypatch):
    before = copy.deepcopy(config.get())
    state_db.init(); scorer.init(); cooldown.init(); affinity.init(); affinity.client_init()
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    monkeypatch.setattr(om, "_pending_refreshes", {})
    monkeypatch.setattr(om, "_profile_sync", lambda *a, **kw: {})
    monkeypatch.setattr(notifier, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(notifier, "notify_event", lambda *a, **kw: None)
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], oauth={"mockMode": True}))
    registry.rebuild_from_config()
    yield
    config.update(lambda c: (c.clear(), c.update(before)))
    registry.rebuild_from_config()


def add(provider="claude", *, fresh=False):
    suffix = uuid.uuid4().hex
    entry = {"provider": provider, "email": suffix + "@example.test",
             "access_token": "fixture-old-at", "refresh_token": "fixture-old-rt",
             "expired": "2099-01-01T00:00:00Z" if fresh else "2000-01-01T00:00:00Z",
             "models": ["fixture-model"], "enabled": True}
    if provider == "openai": entry["workspace_id"] = entry["chatgpt_account_id"] = suffix
    if provider in {"xai", "cursor"}: entry["subject"] = suffix
    if provider == "antigravity": entry["project_id"] = suffix
    om.add_account(entry)
    return om.get_account_key(entry)


def set_refresh(monkeypatch, provider, fn):
    if provider == "claude":
        monkeypatch.setattr(om, "_do_refresh_mock", fn)
    else:
        monkeypatch.setattr(getattr(om, provider + "_provider"), "refresh_sync", fn)


def replacement(key):
    value = copy.deepcopy(om.get_account(key))
    value.update(access_token="fixture-login-at", refresh_token="fixture-login-rt", expired="2099-01-01T00:00:00Z")
    return value


def run_refresh_thread(key, force):
    results, errors = [], []
    def run():
        try:
            results.append(om._refresh_sync_locked(key, force))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results, errors


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("late_failure", [False, True])
def test_providers_p01_relogin_wins_inflight_refresh(private, monkeypatch, provider, force, late_failure):
    key = add(provider)
    expected = om.account_state_key(om.get_account(key))
    entered, release = threading.Event(), threading.Event()
    def refresh(rt, **kwargs):
        assert rt == "fixture-old-rt"
        entered.set()
        assert release.wait(5)
        if late_failure:
            request = httpx.Request("POST", "https://example.test/token")
            raise httpx.HTTPStatusError("fixture old authorization revoked", request=request,
                response=httpx.Response(401, request=request, json={"error": "invalid_grant"}))
        return {"access_token": "fixture-late-at", "refresh_token": "fixture-late-rt", "expires_in": 3600}
    set_refresh(monkeypatch, provider, refresh)
    thread, results, errors = run_refresh_thread(key, force)
    try:
        assert entered.wait(5)
        assert om.replace_exact_identity(key, replacement(key))["status"] == "replaced"
        assert om.account_state_key(om.get_account(key)) == expected
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert results == ["fixture-login-at"]
    assert om.get_account(key)["refresh_token"] == "fixture-login-rt"
    assert not om._has_pending_refresh(expected)


@pytest.mark.parametrize("status", [401, 429, 503])
@pytest.mark.parametrize("provider", PROVIDERS)
def test_providers_refresh_unchanged_credentials_keep_original_exception(private, monkeypatch, provider, status):
    key = add(provider)
    request = httpx.Request("POST", "https://example.test/token")
    error = httpx.HTTPStatusError("fixture failure", request=request,
        response=httpx.Response(status, request=request, json={"error": "invalid_grant" if status == 401 else "busy"}))
    def refresh(*args, **kwargs): raise error
    set_refresh(monkeypatch, provider, refresh)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        om._refresh_sync_locked(key, True)
    assert caught.value is error
    assert not om._pending_refreshes
    info = oauth_errors.describe_oauth_error(error, provider=provider, operation="refresh_token")
    assert info.auth_error is (status == 401)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_providers_p02_failed_write_retries_save_even_if_old_at_fresh(private, monkeypatch, provider):
    key = add(provider, fresh=True)
    calls = []
    def refresh(rt, **kwargs):
        calls.append(rt)
        assert len(calls) == 1, "pre-rotation RT must never be replayed"
        return {"access_token": "fixture-new-at", "refresh_token": "fixture-new-rt", "expires_in": 3600}
    set_refresh(monkeypatch, provider, refresh)
    with monkeypatch.context() as patch:
        patch.setattr(config, "_write_atomic", lambda *_: (_ for _ in ()).throw(OSError("fixture disk full")))
        for _ in range(2):
            with pytest.raises(oauth_errors.OAuthRefreshStateError) as caught:
                om._refresh_sync_locked(key, True)
            info = oauth_errors.describe_oauth_error(caught.value, provider=provider, operation="refresh_token")
            assert info.retryable and not info.auth_error
            assert caught.value.kind == "save_failed_retry_save"
        assert om.get_account(key)["refresh_token"] == "fixture-old-rt"
    assert asyncio.run(om.ensure_valid_token(key)) == "fixture-new-at"
    assert om.get_account(key)["refresh_token"] == "fixture-new-rt"
    assert calls == ["fixture-old-rt"]
    assert not om._has_pending_refresh(om.account_state_key(om.get_account(key)))


def fail_one_save(monkeypatch, key, *, expires=3600):
    calls = []
    def refresh(rt, **kwargs):
        calls.append(rt)
        return {"access_token": "fixture-new-at", "refresh_token": "fixture-new-rt", "expires_in": expires}
    set_refresh(monkeypatch, om.provider_of(key), refresh)
    with monkeypatch.context() as patch:
        patch.setattr(config, "_write_atomic", lambda *_: (_ for _ in ()).throw(OSError("fixture write error")))
        with pytest.raises(oauth_errors.OAuthRefreshStateError):
            om._refresh_sync_locked(key, True)
    return calls


def test_providers_p01_p02_new_login_supersedes_pending(private, monkeypatch):
    key = add(fresh=True)
    calls = fail_one_save(monkeypatch, key)
    assert om.replace_exact_identity(key, replacement(key))["status"] == "replaced"
    assert asyncio.run(om.ensure_valid_token(key)) == "fixture-login-at"
    assert om.get_account(key)["refresh_token"] == "fixture-login-rt"
    assert calls == ["fixture-old-rt"]
    assert not om._has_pending_refresh(om.account_state_key(om.get_account(key)))


@pytest.mark.parametrize("batch", [False, True])
def test_providers_p01_p02_delete_readd_retires_pending(private, monkeypatch, batch):
    key = add(fresh=True)
    saved = copy.deepcopy(om.get_account(key))
    state = om.account_state_key(saved)
    calls = fail_one_save(monkeypatch, key)
    if batch:
        om.set_enabled(key, False, reason="auth_error")
        expected = copy.deepcopy(om.get_account(key))
        assert om.delete_invalid_accounts_batch_if_unchanged([(key, expected)])["status"] == "deleted"
    else:
        om.delete_account(key)
    assert not om._has_pending_refresh(state)
    om.add_account(saved)
    assert om.account_state_key(om.get_account(key)) != state
    assert asyncio.run(om.ensure_valid_token(key)) == "fixture-old-at"
    assert calls == ["fixture-old-rt"]


def test_providers_p01_delete_during_inflight_never_returns_stale_token(private, monkeypatch):
    key = add()
    saved = copy.deepcopy(om.get_account(key))
    entered, release = threading.Event(), threading.Event()
    def refresh(rt, **kwargs):
        entered.set(); assert release.wait(5)
        return {"access_token": "fixture-retired-at", "refresh_token": "fixture-retired-rt", "expires_in": 3600}
    set_refresh(monkeypatch, "claude", refresh)
    thread, results, errors = run_refresh_thread(key, True)
    try:
        assert entered.wait(5)
        om.delete_account(key)
        saved["expired"] = "2099-01-01T00:00:00Z"
        om.add_account(saved)
    finally:
        release.set(); thread.join(5)
    assert not thread.is_alive() and not results
    assert len(errors) == 1 and "generation was deleted" in str(errors[0])
    assert om.get_account(key)["access_token"] == "fixture-old-at"
    assert not om._pending_refreshes


def test_providers_p01_p02_pending_follows_identity_only_rename(private, monkeypatch):
    email = uuid.uuid4().hex + "@example.test"
    legacy = {"provider": "openai", "email": email, "enabled": True,
              "access_token": "fixture-old-at", "refresh_token": "fixture-old-rt",
              "expired": "2099-01-01T00:00:00Z"}
    config.update(lambda c: c.update(oauthAccounts=[legacy]))
    key = om.get_account_key(legacy)
    registry.rebuild_from_config()
    state = om.account_state_key(om.get_account(key))
    calls = fail_one_save(monkeypatch, key)
    workspace = uuid.uuid4().hex
    assert om._save_token_fields(key, {"chatgpt_account_id": workspace}, expected_state_key=state)
    new_key = f"openai:{email}:{workspace}"
    assert om.account_state_key(om.get_account(new_key)) == state
    assert asyncio.run(om.ensure_valid_token(new_key)) == "fixture-new-at"
    assert om.get_account(new_key)["refresh_token"] == "fixture-new-rt"
    assert calls == ["fixture-old-rt"] and not om._has_pending_refresh(state)


def test_providers_p02_expired_pending_saved_before_renewal(private, monkeypatch):
    key = add(fresh=True)
    fail_one_save(monkeypatch, key, expires=-60)
    seen = []
    def refresh(rt, **kw):
        seen.append(rt)
        assert om.get_account(key)["refresh_token"] == "fixture-new-rt"
        return {"access_token": "fixture-renewed-at", "refresh_token": "fixture-renewed-rt", "expires_in": 3600}
    set_refresh(monkeypatch, "claude", refresh)
    assert asyncio.run(om.ensure_valid_token(key)) == "fixture-renewed-at"
    assert seen == ["fixture-new-rt"]


def test_providers_p02_proactive_saves_pending_without_waiting_old_expiry(private, monkeypatch):
    key = add(fresh=True)
    calls = fail_one_save(monkeypatch, key)
    async def usage(*args, **kwargs): return {}
    monkeypatch.setattr(om, "fetch_usage_snapshot", usage)
    result = asyncio.run(om.proactive_refresh_once())
    assert result[om.get_account(key)["email"]] == "refreshed"
    assert om.get_account(key)["refresh_token"] == "fixture-new-rt"
    assert calls == ["fixture-old-rt"]


@pytest.mark.parametrize("outcome", ["success", "error"])
@pytest.mark.parametrize("mutation", ["delete", "replace_key"])
def test_providers_p03_worker_and_registry_mutation_share_lock_order(private, monkeypatch, outcome, mutation):
    name = "fixture-usage-" + uuid.uuid4().hex
    registry.add_api_channel({"name": name, "baseUrl": "https://example.test", "apiKey": "fixture-key",
        "protocol": "anthropic", "providerId": "deepseek", "providerPresetId": "standard", "models": []})
    channel = registry.get_channel("api:" + name)
    aid, spec = pu.account_id(channel), pu.spec_for(channel)
    state_db.provider_usage_save_success(aid, spec.adapter, {"source": "fixture"})
    ready, finish_fetch, acquiring = threading.Event(), threading.Event(), threading.Event()
    timeouts, errors = [], []
    real_lock = channel_state.mutation_lock
    class ObservedLock:
        # Instrument the real RLock rather than replacing mutual exclusion.
        # Bounded acquire prevents an old-code regression from wedging pytest.
        def __enter__(self):
            if threading.current_thread().name == "providers-usage-worker":
                acquiring.set()
            if not real_lock.acquire(timeout=3):
                timeouts.append(threading.current_thread().name)
                raise TimeoutError("fixture detected inverted lock order")
            return self
        def __exit__(self, *_): real_lock.release()
        def __getattr__(self, name): return getattr(real_lock, name)
    monkeypatch.setattr(channel_state, "mutation_lock", ObservedLock())
    async def fetch(*args, **kwargs):
        ready.set()
        assert await asyncio.to_thread(finish_fetch.wait, 5)
        if outcome == "error": raise RuntimeError("fixture upstream failure")
        return {"source": "fixture", "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fetch)
    def worker():
        async def run():
            pu._QUEUE = asyncio.Queue()
            await pu._QUEUE.put(pu.RefreshJob(aid, spec, "fixture-key", channel.key, 0))
            task = asyncio.create_task(pu._worker(0))
            try:
                await asyncio.wait_for(pu._QUEUE.join(), 10)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError): await task
        try: asyncio.run(run())
        except BaseException as exc: errors.append(exc)
    thread = threading.Thread(target=worker, name="providers-usage-worker", daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        with channel_state.mutation_lock:
            finish_fetch.set()
            assert acquiring.wait(5)
            if mutation == "delete":
                assert registry.delete_api_channel(name)
            else:
                assert registry.update_api_channel(name, {"apiKey": "fixture-replacement-key"})
    finally:
        finish_fetch.set(); thread.join(12)
    assert not thread.is_alive() and not timeouts and not errors
    assert state_db.provider_usage_load(aid) is None, "late success/error must not resurrect deleted identity"
    assert aid not in pu._RUNTIME
    pu._QUEUE = None


def test_providers_p04_unknown_credits_keep_disable_then_explicit_recovery(private):
    key = add("antigravity", fresh=True)
    assert om.evaluate_and_toggle_by_usage(key, {"antigravity": {"known": True, "available": False}})["action"] == "disabled"
    # Reload from isolated disk to prove evidence is durable, not a runtime flag.
    config.reload()
    low = {"antigravity": {"known": False}, "five_hour": {"utilization": 1}}
    result = om.evaluate_and_toggle_by_usage(key, low)
    assert result["action"] == "quota_unknown_keep_disabled"
    assert om.get_account(key)["quota_observation"]["blocked"] == ["Credits"]
    assert om.get_account(key)["enabled"] is False
    known = {**low, "antigravity": {"known": True, "available": True}}
    assert om.evaluate_and_toggle_by_usage(key, known, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(key, known)["action"] == "resumed"
    assert "quota_observation" not in om.get_account(key)


def test_providers_p04_partial_snapshots_do_not_promote_display_windows_to_gates(private):
    key = add("antigravity", fresh=True)
    initial = {"antigravity": {"known": True, "available": False},
               "five_hour": {"utilization": 99}, "seven_day": {"utilization": 99}}
    assert om.evaluate_and_toggle_by_usage(key, initial)["action"] == "disabled"
    config.reload()
    assert om.get_account(key)["quota_observation"]["blocked"] == ["Credits"]
    assert om.evaluate_and_toggle_by_usage(key, {"five_hour": {"utilization": 1}})["action"] == "quota_unknown_keep_disabled"
    assert om.get_account(key)["enabled"] is False
    first = {"antigravity": {"known": True, "available": True}, "five_hour": {"utilization": 1}}
    assert om.evaluate_and_toggle_by_usage(key, first)["action"] == "resumed"
    config.reload()
    assert "quota_observation" not in om.get_account(key)
    assert om.evaluate_and_toggle_by_usage(key, {"seven_day": {"utilization": 99}})["action"] == "kept_enabled"
    om.set_enabled(key, False, reason="user")
    assert om.evaluate_and_toggle_by_usage(key, initial)["action"] == "noop_user"


@pytest.mark.asyncio
@pytest.mark.parametrize("container,image_type,holder", [
    ("input", "input_image", "https://example.test/real.png"),
    ("messages", "image_url", {"url": "https://example.test/real.png", "detail": "high"}),
])
async def test_providers_p05_only_message_media_parts_are_inlined(monkeypatch, container, image_type, holder):
    calls = []
    async def download(url, **kwargs):
        calls.append(url); return b"fixture-image", "image/png"
    monkeypatch.setattr(remote_image, "download_https_image", download)
    literal = {"type": "image_url", "image_url": {"url": "https://example.test/literal.png"}}
    params = {"type": "object", "properties": {"doc": {"const": literal}}, "examples": [literal]}
    body = {"model": "gemini-3-flash", "tools": [{"type": "function", "name": "save", "parameters": params}],
            "metadata": literal, "text": {"format": {"type": "json_schema", "schema": params}},
            container: [{"role": "user", "content": [
                {"type": image_type, "image_url": holder}, {"type": "text", "text": "hi", "example": literal}]},
                {"type": "function_call", "name": "save", "arguments": literal, "content": [literal]},
                {"type": "function_call_output", "output": [literal]},
                {"role": "tool", "content": [literal]}]}
    original = copy.deepcopy(body)
    actual = await remote_image.inline_remote_images(body)
    assert calls == ["https://example.test/real.png"]
    assert body == original
    converted = actual[container][0]["content"][0]["image_url"]
    assert (converted["url"] if isinstance(converted, dict) else converted).startswith("data:image/png;base64,")
    assert actual["tools"] == original["tools"] and actual["text"] == original["text"]
    assert actual["metadata"] == literal and actual[container][1:] == original[container][1:]
    assert actual[container][0]["content"][1] == original[container][0]["content"][1]
    if container == "input":
        schema_body = {"model": "gemini-3-flash", "input": "hi", "tools": actual["tools"]}
        schema = antigravity_codec.responses_to_gemini(schema_body)["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"]
        assert schema == params


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [[{}], [{"id": None}], [{"id": True}], [{"id": 42}],
                                    [{"id": {"nested": "no"}}], [{"name": ["no"]}], [{"id": "  "}]])
async def test_providers_p06_malformed_catalog_preserves_lkg(private, monkeypatch, records):
    key = add("xai", fresh=True)
    lkg = {"schema": 1, "models": [{"id": "fixture-model"}]}
    config.update(lambda c: c["oauthAccounts"][0].update(account_model_catalog=lkg, last_model_sync="2026-01-01T00:00:00Z"))
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data": records}
    monkeypatch.setattr(discovery.network, "get_sync", lambda *a, **kw: Response())
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    result = await om.refresh_account_models(key)
    assert result["action"] == "error"
    account = om.get_account(key)
    assert account["models"] == ["fixture-model"] and account["account_model_catalog"] == lkg
    assert account["last_model_sync"] == "2026-01-01T00:00:00Z" and account["last_model_sync_error"]


def test_providers_p06_mixed_catalog_ids_and_records_agree(private, monkeypatch):
    key = add("xai", fresh=True)
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data": [{}, {"id": 42}, {"id": " grok-good "}, {"id": None, "name": "grok-name"}]}
    monkeypatch.setattr(discovery.network, "get_sync", lambda *a, **kw: Response())
    result = discovery.discover_xai(om.get_account(key))
    assert result.models == ["grok-good", "grok-name"]
    assert [r["id"] for r in result.catalog["models"]] == result.models
