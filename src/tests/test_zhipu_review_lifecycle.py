"""Independent 3.15.3 lifecycle review: offline scheduling/caller regressions."""
from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from src import config, oauth_manager as om, state_db
from src.oauth.zhipu import auth, actions, catalog, common as c, project_tokens as pt, runtime
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.test_zhipu_management import ctl, reset_env
from src.tests.test_workbuddy_lifecycle import context
from src.tests.zhipu_pat_fixtures import envelope, pat


def saved_account(**changes):
    account = credential("oauth", organization_id="org", project_id="project", model_key="", **changes)
    om.add_account(account)
    key = om.get_account_key(account)
    return key, copy.deepcopy(om.get_account(key))


def test_peer_business_rotation_must_not_adopt_concurrent_user_pause(account_env, monkeypatch):
    """The normal token entrance must reject a pause that happened during its IO."""
    key, snapshot = saved_account(site="zai", project_key_id="fixture", zhipu_oauth_token="source")
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    exchanges = []

    def wire(url, **kw):
        if url.endswith("/api/auth/z/login"):
            exchanges.append(kw["body"])
            return {"access_token": "renewed-business"}
        if url.endswith("getCustomerInfo"):
            if kw["headers"]["Authorization"] == "fixture-biz":
                raise c.ZhipuError("projects", status=401)
            return {"organizations": []}
        assert url.endswith("/access_tokens")
        if kw["headers"]["Authorization"] == "Bearer fixture-biz":
            # A management request renews the same source while this request
            # still has the old business credential, then the user pauses it.
            auth.project_choices(copy.deepcopy(snapshot), account_key=key)
            om.set_enabled(key, False)
            raise c.ZhipuError("project_token", status=401)
        return envelope()

    monkeypatch.setattr(c, "request", wire)
    from src.tests import conftest
    monkeypatch.setattr(asyncio, "to_thread", conftest._ORIG_TO_THREAD)
    with pytest.raises(c.ZhipuError, match="stale_generation|scope_invalidated"):
        asyncio.run(om.ensure_valid_token(key))
    assert exchanges == [{"token": "source"}]
    assert om.get_account(key)["disabled_reason"] == "user"


def test_singleflight_followers_share_success_across_business_rotation(account_env, monkeypatch):
    key, snapshot = saved_account(site="zai", project_key_id="fixture", zhipu_oauth_token="source")
    entered, waiter, release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiter.set()
            return super().result(timeout)

    monkeypatch.setattr(pt, "Future", ObservedFuture)

    def wire(url, **kw):
        calls.append(url)
        if url.endswith("/api/auth/z/login"):
            return {"access_token": "renewed-business"}
        if url.endswith("/models"):
            return {"data": [{"id": "GLM-5.3"}]}
        if "client/configs" in url:
            return {"configs": {}}
        assert url.endswith("/access_tokens")
        if kw["headers"]["Authorization"] == "Bearer fixture-biz":
            entered.set()
            assert release.wait(3)
            raise c.ZhipuError("project_token", status=401)
        return envelope()

    monkeypatch.setattr(c, "request", wire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(om._refresh_sync_locked, key, False)
        try:
            assert entered.wait(3)
            follower = pool.submit(catalog.fetch_models, copy.deepcopy(snapshot), account_key=key)
            assert waiter.wait(3)
        finally:
            release.set()
        assert leader.result(3) == pat()
        assert [row["upstreamId"] for row in follower.result(3)] == ["GLM-5.3"]
    assert sum(url.endswith("/api/auth/z/login") for url in calls) == 1
    assert sum(url.endswith("/access_tokens") for url in calls) == 2


def test_completed_flight_cannot_return_a_just_rejected_pat(account_env, monkeypatch):
    key, snapshot = saved_account(project_key_id="fixture")
    published, release = threading.Event(), threading.Event()
    issued = []

    class PausedCompletion(Future):
        def set_result(self, value):
            super().set_result(value)
            if len(issued) == 1:
                published.set()
                assert release.wait(3)

    monkeypatch.setattr(pt, "Future", PausedCompletion)

    def wire(url, **kw):
        assert url.endswith("/access_tokens")
        issued.append(True)
        return envelope(accessToken=pat(tag=str(len(issued))))

    monkeypatch.setattr(c, "request", wire)
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(auth.resolve_model_key, copy.deepcopy(snapshot), account_key=key)
        try:
            assert published.wait(3)
            renewed = auth.resolve_model_key(copy.deepcopy(snapshot), account_key=key,
                rejected_fingerprint=pt.token_fingerprint(pat(tag="1")))
        finally:
            release.set()
        # The original consumer was also paused until after the rejection;
        # its return-time check must use the live replacement, not PAT 1.
        assert leader.result(3) == pat(tag="2")
    assert renewed == pat(tag="2")
    assert len(issued) == 2


def test_peer_business_rotation_cannot_reauthorize_key_create_after_pause(ctl, monkeypatch):
    """Real confirmed action: a pre-dispatch pause must cancel the Key POST."""
    control, _ = ctl
    key, snapshot = saved_account(site="zai", zhipu_oauth_token="source")
    created = []
    monkeypatch.setattr(control, "_post_save_zhipu_effects", lambda *a, **kw: {})
    monkeypatch.setattr(control, "_zhipu_initialization_summary", lambda effects: {})

    def wire(url, **kw):
        if url.endswith("/api/auth/z/login"):
            return {"access_token": "renewed-business"}
        if url.endswith("getCustomerInfo"):
            if kw["headers"]["Authorization"] == "fixture-biz":
                raise c.ZhipuError("projects", status=401)
            return {"organizations": []}
        if url.endswith("/access_tokens"):
            return envelope()
        assert url.endswith("/api_keys")
        if kw.get("method") == "POST":
            created.append(kw)
            return {"name": "zcode-api-key", "apiKey": "fixture"}
        if kw["headers"]["Authorization"] == "Bearer fixture-biz":
            auth.project_choices(copy.deepcopy(snapshot), account_key=key)
            om.set_enabled(key, False)
            raise c.ZhipuError("key_list", status=401)
        return []

    monkeypatch.setattr(c, "request", wire)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    result = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert not created, "Key POST dispatched after user pause but before durable dispatch commit"
    assert result["status"] == "not_submitted"


def test_created_key_then_pat_timeout_is_durably_resumed_without_duplicate(reset_env, monkeypatch):
    control, key, state, _ = reset_env
    wire = c.request
    failures = [True]
    monkeypatch.setattr(control, "_post_save_zhipu_effects", lambda *a, **kw: {})
    monkeypatch.setattr(control, "_zhipu_initialization_summary", lambda effects: {})

    def fail_pat_once(url, **kw):
        if url.endswith("/access_tokens") and failures:
            failures.pop()
            raise c.ZhipuError("project_token", "timeout", timeout_phase="read")
        return wire(url, **kw)

    monkeypatch.setattr(c, "request", fail_pat_once)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    first = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    assert first["status"] == "key_pending"
    history = state_db.zhipu_action_history(actions.owner(om.get_account(key)))
    assert len(history) == 1 and history[0]["key_id"] == "new-id" and history[0]["submitted"]
    plan = control.plan_zhipu_action(context(), key, "create_key")
    assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "succeeded"
    assert sum(url.endswith("/api_keys") and kw.get("method") == "POST" for url, kw in state["calls"]) == 1
    assert om.get_account(key)["project_key_id"] == "new-id"
    assert not om.get_account(key)["model_key"]


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_key_and_pat_network_io_do_not_hold_config_or_cache_lock(ctl, monkeypatch, site):
    control, _ = ctl
    key, _ = saved_account(site=site, zhipu_oauth_token="source" if site == "zai" else "")
    checks, threads = [], []
    renewed = [False]
    monkeypatch.setattr(control, "_post_save_zhipu_effects", lambda *a, **kw: {})
    monkeypatch.setattr(control, "_zhipu_initialization_summary", lambda effects: {})

    def wire(url, **kw):
        entered = threading.Event()
        def inspect_locks():
            with config.serialized_updates(), pt._lock:
                entered.set()
        thread = threading.Thread(target=inspect_locks, daemon=True)
        threads.append(thread)
        thread.start()
        checks.append((url, entered.wait(1)))
        if url.endswith("/api/auth/z/login"):
            renewed[0] = True
            return {"access_token": "renewed-business"}
        if site == "zai" and not renewed[0]:
            raise c.ZhipuError("key_list", status=401)
        if url.endswith("/access_tokens"):
            return envelope()
        assert url.endswith("/api_keys")
        return {"name": "zcode-api-key", "apiKey": "fixture"} if kw.get("method") == "POST" else []

    monkeypatch.setattr(c, "request", wire)
    try:
        plan = control.plan_zhipu_action(context(), key, "create_key")
        outcome = control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    finally:
        for thread in threads:
            thread.join(2)
    assert outcome["status"] == "succeeded"
    assert checks and all(ok for _, ok in checks), checks
    assert not any(thread.is_alive() for thread in threads)


def test_different_accounts_and_projects_never_share_pat(account_env, monkeypatch):
    accounts = [saved_account(site=site, subject=subject, project_key_id="fixture")
        for site, subject in [("bigmodel", "first"), ("bigmodel", "second"), ("zai", "first")]]
    base = credential("oauth", subject="first", organization_id="org", project_id="other-project",
        model_key="", project_key_id="fixture")
    om.add_account(base)
    other_key = om.get_account_key(base)
    accounts.append((other_key, copy.deepcopy(om.get_account(other_key))))
    calls = []
    def wire(url, **kw):
        assert url.endswith("/access_tokens")
        calls.append((url, kw["account_key"]))
        return envelope(accessToken=pat(tag=str(len(calls))))
    monkeypatch.setattr(c, "request", wire)
    values = [auth.resolve_model_key(snapshot, account_key=key) for key, snapshot in accounts]
    assert len(set(values)) == 4 and len(calls) == 4
    for (key, snapshot), token in zip(accounts, values):
        assert auth.resolve_model_key(snapshot, account_key=key) == token
    assert len(calls) == 4


def test_deleted_then_readded_account_rejects_old_pat_completion(account_env, monkeypatch):
    key, snapshot = saved_account(project_key_id="fixture")
    def wire(url, **kw):
        assert url.endswith("/access_tokens")
        om.delete_account_if_unchanged(key, snapshot)
        om.add_account(copy.deepcopy(snapshot))
        assert om.get_account(key)["generationId"] != snapshot["generationId"]
        return envelope()
    monkeypatch.setattr(c, "request", wire)
    with pytest.raises(c.ZhipuError, match="scope_invalidated"):
        auth.resolve_model_key(snapshot, account_key=key)
    assert pt.cached_token(om.get_account(key)) is None
    assert pt.scope(snapshot) not in pt._entries


@pytest.mark.parametrize("failure", ["initial", "dispatch", "completion"])
def test_durable_write_rejection_never_grants_or_repeats_key_post(reset_env, monkeypatch, failure):
    control, key, state, _ = reset_env
    save = state_db.zhipu_action_save
    rejected = []
    monkeypatch.setattr(control, "_post_save_zhipu_effects", lambda *a, **kw: {})
    monkeypatch.setattr(control, "_zhipu_initialization_summary", lambda effects: {})
    def reject_once(record_key, row, *, expected):
        target = (expected is None if failure == "initial" else
            row.get("submitted") and not row.get("key_id") if failure == "dispatch" else
            row.get("status") == "succeeded")
        if target and not rejected:
            rejected.append(True)
            return False
        return save(record_key, row, expected=expected)
    monkeypatch.setattr(state_db, "zhipu_action_save", reject_once)
    plan = control.plan_zhipu_action(context(), key, "create_key")
    if failure == "dispatch":
        assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "not_submitted"
    else:
        with pytest.raises(c.ZhipuError):
            control.execute_zhipu_action_now(context(), key, plan["plan_token"])
    posts = lambda: sum(url.endswith("/api_keys") and kw.get("method") == "POST" for url, kw in state["calls"])
    assert rejected and posts() == int(failure == "completion")
    if failure == "completion":
        rows = state_db.zhipu_action_history(actions.owner(om.get_account(key)))
        assert len(rows) == 1 and rows[0]["key_id"] == "new-id" and rows[0]["submitted"]
        assert rows[0]["status"] == "pending"
        plan = control.plan_zhipu_action(context(), key, "create_key")
        # Existing fail-closed semantics: completion remains uncertain, not
        # silently treated as permission to repeat a remote create.
        assert control.execute_zhipu_action_now(context(), key, plan["plan_token"])["status"] == "pending"
        assert posts() == 1


@pytest.mark.parametrize("change", ["same", "label", "pause", "login", "business_only",
                                   "project", "location", "delete_readd", "forget", "expired"])
def test_rotated_flight_waiter_validates_current_scope_without_adopting_state(account_env, monkeypatch, change):
    key, snapshot = saved_account(site="zai", project_key_id="fixture", zhipu_oauth_token="source")
    now = [1000.0]
    monkeypatch.setattr(pt, "time", SimpleNamespace(time=lambda: now[0]))
    entered, joined, ready = threading.Event(), threading.Event(), threading.Event()
    release_wire, release_waiter = threading.Event(), threading.Event()
    calls = []
    follower_snapshot = copy.deepcopy(snapshot)

    class GatedWaiter(Future):
        def result(self, timeout=None):
            joined.set()
            result = super().result(timeout)
            ready.set()
            assert release_waiter.wait(3)
            return result

    monkeypatch.setattr(pt, "Future", GatedWaiter)

    def wire(url, **kw):
        calls.append(url)
        if url.endswith("/api/auth/z/login"):
            return {"access_token": "renewed-business"}
        assert url.endswith("/access_tokens")
        if kw["headers"]["Authorization"] == "Bearer fixture-biz":
            entered.set()
            assert release_wire.wait(3)
            raise c.ZhipuError("project_token", status=401)
        return envelope()

    monkeypatch.setattr(c, "request", wire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(auth.resolve_model_key, copy.deepcopy(snapshot), account_key=key)
        try:
            assert entered.wait(3)
            follower = pool.submit(runtime.refresh_locked, follower_snapshot, key, False)
            assert joined.wait(3)
            release_wire.set()
            assert ready.wait(3)
            assert leader.result(3) == pat()
            current = copy.deepcopy(om.get_account(key))
            if change == "label":
                om.mutate_account_if_unchanged(key, current, lambda a: a.update(label="changed-by-user"))
            elif change == "pause":
                om.set_enabled(key, False)
            elif change == "login":
                om.replace_exact_identity(key, dict(current, access_token="new-login", zhipu_oauth_token="new-source"), expected_account=current)
            elif change == "business_only":
                om.mutate_account_if_unchanged(key, current, lambda a: a.update(access_token="unrelated-business"))
            elif change == "project":
                config.update(lambda cfg: next(a for a in cfg["oauthAccounts"] if a["generationId"] == current["generationId"]).update(project_id="other-project"))
            elif change == "location":
                om.mutate_account_if_unchanged(key, current, lambda a: a.update(project_key_id="other-key"))
            elif change == "delete_readd":
                om.delete_account_if_unchanged(key, current)
                om.add_account(current)
            elif change == "forget":
                pt.forget(key)
            elif change == "expired":
                now[0] += 3601
        finally:
            release_wire.set()
            release_waiter.set()
        if change in {"same", "label"}:
            assert follower.result(3) == pat()
            assert follower_snapshot["access_token"] == "renewed-business"
            assert om.get_account(key)["access_token"] == "renewed-business"
        else:
            with pytest.raises(c.ZhipuError, match="scope_invalidated|stale_generation"):
                follower.result(3)
    assert follower_snapshot["label"] == snapshot["label"]
    assert follower_snapshot["enabled"] == snapshot["enabled"]
    if change == "label":
        assert om.get_account(key)["label"] == "changed-by-user"
    if change == "pause":
        assert om.get_account(key)["disabled_reason"] == "user"
    if change in {"login", "business_only"}:
        assert om.get_account(key)["access_token"] == ("new-login" if change == "login" else "unrelated-business")
    assert sum(url.endswith("/api/auth/z/login") for url in calls) == 1
    assert sum(url.endswith("/access_tokens") for url in calls) == 2


def test_old_completion_does_not_clear_new_pending_flight(account_env, monkeypatch):
    key, snapshot = saved_account(project_key_id="fixture")
    first_done, release_first = threading.Event(), threading.Event()
    second_entered, release_second, joined = threading.Event(), threading.Event(), threading.Event()
    issued = []

    class GatedCompletion(Future):
        def set_result(self, result):
            super().set_result(result)
            if len(issued) == 1:
                first_done.set()
                assert release_first.wait(3)
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    monkeypatch.setattr(pt, "Future", GatedCompletion)

    def wire(url, **kw):
        assert url.endswith("/access_tokens")
        issued.append(True)
        tag = str(len(issued))
        if tag == "2":
            second_entered.set()
            assert release_second.wait(3)
        return envelope(accessToken=pat(tag=tag))

    monkeypatch.setattr(c, "request", wire)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(auth.resolve_model_key, copy.deepcopy(snapshot), account_key=key)
        try:
            assert first_done.wait(3)
            second = pool.submit(auth.resolve_model_key, copy.deepcopy(snapshot), account_key=key,
                rejected_fingerprint=pt.token_fingerprint(pat(tag="1")))
            assert second_entered.wait(3)
            with pt._lock:
                new_pending = pt._entries[pt.scope(snapshot)]["pending"]
            release_first.set()
            with pytest.raises(c.ZhipuError, match="scope_invalidated"):
                first.result(3)
            with pt._lock:
                assert pt._entries[pt.scope(snapshot)]["pending"] is new_pending
            follower = pool.submit(auth.resolve_model_key, copy.deepcopy(snapshot), account_key=key)
            assert joined.wait(3)
        finally:
            release_first.set()
            release_second.set()
        assert second.result(3) == follower.result(3) == pat(tag="2")
    assert len(issued) == 2


def test_reissued_rejected_pat_fails_without_unbounded_issuance(account_env, monkeypatch):
    key, snapshot = saved_account(project_key_id="fixture")
    issued = []
    def wire(url, **kw):
        assert url.endswith("/access_tokens")
        issued.append(True)
        return envelope()
    monkeypatch.setattr(c, "request", wire)
    assert auth.resolve_model_key(copy.deepcopy(snapshot), account_key=key) == pat()
    with pytest.raises(c.ZhipuError, match="scope_invalidated"):
        auth.resolve_model_key(copy.deepcopy(snapshot), account_key=key,
            rejected_fingerprint=pt.token_fingerprint(pat()))
    assert len(issued) == 2
    assert pt.cached_token(snapshot) is None
