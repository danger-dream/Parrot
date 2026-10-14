"""3.15.3 PAT contracts, process cache and account/login races (offline only)."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, oauth_manager as om
from src.oauth.zhipu import auth, common as c, project_tokens as pt, runtime, signing
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.zhipu_pat_fixtures import pat, envelope


def account(**changes):
    return credential("oauth", organization_id="org", project_id="project", **changes)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pt, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(pt.random, "random", lambda: .5)
    return now


def material(tag="one", expires=10000, seconds=1000):
    return envelope(accessToken=pat(tag=tag), expiresIn=seconds, expiresAt=expires)


def install(monkeypatch, replies):
    calls = []
    def wire(url, **kw):
        calls.append((url, kw))
        if url.endswith("/api_keys"):
            return [{"apiKey": "fixture", "name": "zcode-api-key"}]
        assert url.endswith("/access_tokens"), url
        result = replies.pop(0)
        if callable(result):
            result = result()
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(c, "request", wire)
    return calls


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("create", [False, True])
def test_family_scope_wire_and_durable_callbacks(monkeypatch, site, team, create):
    a = account(site=site, plan_scope="team" if team else "personal")
    name = "zcode-team-api-key" if team else "zcode-api-key"
    calls, events = [], []
    def wire(url, **kw):
        calls.append((url, kw))
        headers = kw["headers"]
        if url.endswith("getCustomerInfo"):
            assert headers == {"Authorization": "fixture-biz"}
            return {"organizations": []}
        if url.endswith("/access_tokens"):
            assert url.endswith("/organization/org/projects/project/api_keys/id%2Fx/access_tokens")
            assert headers == {"Authorization": "Bearer fixture-biz", "Content-Type": "application/json"}
            assert kw["method"] == "POST" and kw["body"] == {"clientType":"zcode", "clientVersion":"3.15.3"}
            return envelope("id/x")
        assert headers["Authorization"] == ("Bearer " if site == "zai" and not team else "") + "fixture-biz"
        assert (headers.get("bigmodel-organization"), headers.get("bigmodel-project")) == (("org", "project") if team else (None, None))
        if kw.get("method") == "POST":
            assert events[-1] == "authorized"
            assert type(kw["body"]["usageScene"]) is int
            assert kw["body"] == {"name":name, "usageScene":1, **({"keyType":2} if team else {})}
            return {"apiKey":"id/x", "name":name, "keyType":2}
        wrong = [{"apiKey":"wrong", "name":"zcode-api-key" if team else "other", "keyType":2},
                 {"apiKey":"wrong-type", "name":name if team else "other", "keyType":"2"}]
        return wrong if create else wrong + [{"apiKey":"id/x", "name":name, "keyType":2}]
    monkeypatch.setattr(c, "request", wire)
    assert auth.project_choices(a) == []
    assert auth.resolve_model_key(a, create=create, before_create=lambda: events.append("authorized"),
        on_key=lambda key, made: events.append((key, made)), on_stage=events.append) == pat("id/x")
    assert ("id/x", create) in events and events[-1] == "project_token"
    assert auth.resolve_model_key(a, create=True) == pat("id/x")
    assert sum(kw.get("method") == "POST" and url.endswith("/api_keys") for url, kw in calls) == int(create)
    assert not any("/copy/" in url for url, _ in calls)


def test_cache_min_start_time_advance_refresh_and_five_second_deferral(monkeypatch, clock):
    a = account()
    def delayed():
        clock[0] += 20
        return material(seconds=1000, expires=1900)
    calls = install(monkeypatch, [delayed, c.ZhipuError("project_token", "timeout"), material("two")])
    assert auth.resolve_model_key(a) == pat(tag="one")
    value = pt._entries[pt.scope(a)]["value"]
    assert value["expires"] == 1900000 and value["refresh"] == 1750000
    clock[0] = 1749
    assert auth.resolve_model_key(a) == pat(tag="one") and len(calls) == 2
    clock[0] = 1750
    assert auth.resolve_model_key(a) == pat(tag="one") and len(calls) == 3
    assert value["refresh"] == 1755000
    clock[0] = 1754
    assert auth.resolve_model_key(a) == pat(tag="one") and len(calls) == 3
    clock[0] = 1755
    assert auth.resolve_model_key(a) == pat(tag="two") and len(calls) == 4


def test_relative_expiry_uses_request_start_not_delayed_response(monkeypatch, clock):
    def delayed():
        clock[0] += 50
        return material(seconds=200)
    install(monkeypatch, [delayed])
    a = account()
    auth.resolve_model_key(a)
    assert pt._entries[pt.scope(a)]["value"]["expires"] == 1200000


@pytest.mark.parametrize("reason", ["expired", "rejected", "schema", "disabled"])
def test_never_fallback_expired_rejected_or_terminal(monkeypatch, clock, reason):
    a = account()
    replies = [material(seconds=200)]
    install(monkeypatch, replies)
    token = auth.resolve_model_key(a)
    clock[0] = 1200 if reason == "expired" else 1050
    replies.append(material(expires=0) if reason == "schema" else
                   envelope(enable=False) if reason == "disabled" else c.ZhipuError("project_token", status=503))
    with pytest.raises(c.ZhipuError):
        auth.resolve_model_key(a, rejected_fingerprint=pt.token_fingerprint(token) if reason == "rejected" else None)
    assert pt.scope(a) not in pt._entries


def test_rejected_fingerprint_only_invalidates_matching_token(monkeypatch, clock):
    a = account()
    calls = install(monkeypatch, [material(), material("two")])
    one = auth.resolve_model_key(a)
    assert auth.resolve_model_key(a, rejected_fingerprint="stale") == one and len(calls) == 2
    two = auth.resolve_model_key(a, rejected_fingerprint=pt.token_fingerprint(one))
    assert two != one and len(calls) == 3
    assert auth.resolve_model_key(a, rejected_fingerprint=pt.token_fingerprint(one)) == two and len(calls) == 3


@pytest.mark.parametrize("change", [{"enable":False}, {"enable":0}, {"tokenType":"bearer"}, {"accessToken":" "},
    {"expiresIn":0}, {"expiresIn":True}, {"expiresAt":float("inf")}, {"expiresAt":"9000"}, {"expiresAt":999}])
def test_pat_schema_fail_closed(monkeypatch, clock, change):
    install(monkeypatch, [envelope(**change)])
    with pytest.raises(c.ZhipuError):
        auth.resolve_model_key(account())


@pytest.mark.parametrize("code", [0, 200, "0", "200"])
def test_pat_schema_default_enable_and_trim(monkeypatch, clock, code):
    data = material()
    data["code"] = code
    data["data"]["accessToken"] = "  " + pat() + "  "
    install(monkeypatch, [data])
    assert auth.resolve_model_key(account()) == pat()


def test_singleflight_waiters_share_one_issuance(monkeypatch, clock):
    entered, release = threading.Event(), threading.Event()
    def delayed():
        entered.set()
        assert release.wait(3)
        return material()
    calls = install(monkeypatch, [delayed])
    a = account()
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(auth.resolve_model_key, copy.deepcopy(a)) for _ in range(8)]
        assert entered.wait(3)
        release.set()
        assert {job.result(3) for job in jobs} == {pat(tag="one")}
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["delete", "replace", "scope", "clear"])
def test_inflight_reply_cannot_repopulate_retired_scope(account_env, monkeypatch, clock, change):
    a = account(); om.add_account(a); key = om.get_account_key(a)
    snapshot = copy.deepcopy(om.get_account(key))
    def reply():
        if change == "clear":
            pt.forget(key)
        elif change == "delete":
            om.delete_account_if_unchanged(key, snapshot)
        else:
            fields = {"access_token":"new-login"} if change == "replace" else {"plan_scope":"team"}
            om.mutate_account_if_unchanged(key, snapshot, lambda value: value.update(fields))
        return material()
    install(monkeypatch, [reply])
    with pytest.raises(c.ZhipuError, match="scope_invalidated"):
        auth.resolve_model_key(snapshot, account_key=key)
    assert pt.scope(snapshot) not in pt._entries


def test_404_deletes_persisted_location_fails_once_then_relists(account_env, monkeypatch, clock):
    a = account(project_key_id="gone", model_key=""); om.add_account(a); key = om.get_account_key(a)
    calls = install(monkeypatch, [c.ZhipuError("project_token", status=404), material()])
    with pytest.raises(c.ZhipuError):
        runtime.refresh_locked(copy.deepcopy(om.get_account(key)), key, False)
    assert len(calls) == 1 and not om.get_account(key).get("project_key_id")
    runtime.refresh_locked(copy.deepcopy(om.get_account(key)), key, False)
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["access_tokens", "api_keys", "access_tokens"]
    assert om.get_account(key)["project_key_id"] == "fixture"


async def test_normal_ensure_checks_ttl_no_pat_on_disk_no_entitlement_reads(account_env, monkeypatch, clock):
    a = account(); om.add_account(a); key = om.get_account_key(a)
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    calls = install(monkeypatch, [material(seconds=500), material("two")])
    monkeypatch.setattr(auth, "entitlement", lambda *a, **k: pytest.fail("not a PAT refresh dependency"))
    assert await om.ensure_valid_token(key) == pat(tag="one")
    disk = Path(config.path()).read_bytes()
    for _ in range(3):
        assert await om.ensure_valid_token(key) == pat(tag="one")
    assert disk == Path(config.path()).read_bytes() and pat(tag="one").encode() not in disk
    assert not om.get_account(key)["model_key"] and om.get_account(key)["project_key_id"] == "fixture"
    clock[0] = 1350
    assert await om.ensure_valid_token(key) == pat(tag="two") and len(calls) == 3


@pytest.mark.parametrize("value", ["fixture.secret", pat()])
async def test_handfilled_never_renews(account_env, monkeypatch, value):
    a = credential(model_key=value, zhipu_oauth_token="discard", access_token="discard")
    assert not a.get("access_token") and not a.get("zhipu_oauth_token")
    om.add_account(a); key = om.get_account_key(a)
    monkeypatch.setattr(c, "request", lambda *a, **k: pytest.fail("no management credential"))
    assert await om.ensure_valid_token(key) == value
    assert await om.force_refresh(key) == value


@pytest.mark.parametrize("changed", [False, True])
def test_zai_source_exchange_saved_with_cas(account_env, monkeypatch, changed):
    a = account(site="zai", zhipu_oauth_token="original-oauth", zcode_token="platform")
    om.add_account(a); key = om.get_account_key(a)
    snapshot = copy.deepcopy(om.get_account(key)); calls = []
    def wire(url, **kw):
        calls.append((url, kw))
        if url.endswith("/api/auth/z/login"):
            assert kw["body"] == {"token":"original-oauth"} and not kw.get("headers")
            if changed:
                om.mutate_account_if_unchanged(key, snapshot, lambda v: v.update(access_token="new-login", zhipu_oauth_token="new-source"))
            return {"access_token":"renewed-business"}
        if kw["headers"]["Authorization"] == "fixture-biz":
            raise c.ZhipuError("projects", status=401)
        assert kw["headers"]["Authorization"] == "renewed-business"
        return {"organizations": []}
    monkeypatch.setattr(c, "request", wire)
    if changed:
        with pytest.raises(c.ZhipuError, match="stale_generation"):
            auth.project_choices(snapshot, account_key=key)
        assert om.get_account(key)["access_token"] == "new-login"
    else:
        assert auth.project_choices(snapshot, account_key=key) == []
        assert om.get_account(key)["access_token"] == "renewed-business"
        assert om.get_account(key)["zcode_token"] == "platform"
        assert auth.project_choices(copy.deepcopy(om.get_account(key)), account_key=key) == []
    assert sum(url.endswith("/api/auth/z/login") for url, _ in calls) == 1


@pytest.mark.parametrize("has_source", [False, True])
def test_business_401_bounded_and_old_login_not_invented(monkeypatch, has_source):
    a = account(site="zai", zhipu_oauth_token="original" if has_source else "")
    calls = []
    def wire(url, **kw):
        calls.append(url)
        if url.endswith("/api/auth/z/login"):
            return {"access_token":"second"}
        raise c.ZhipuError("subscription", status=401)
    monkeypatch.setattr(c, "request", wire)
    with pytest.raises(c.ZhipuError):
        auth.entitlement(a)
    assert len(calls) == (3 if has_source else 1)


def test_login_source_is_sensitive_serialized_and_not_an_identity(account_env, monkeypatch):
    from src.management_control.public_safety import redact_known_fields
    monkeypatch.setattr(c, "request", lambda *a, **k: {"access_token":"business"})
    a = auth.credential_from_login({"zai":{"access_token":"original"}, "token":"platform",
                                    "user":{"user_id":"user", "name":"User"}}, "zai")
    assert a["zhipu_oauth_token"] == "original" and a["access_token"] == "business"
    assert c.identity(a) == c.identity(dict(a, zhipu_oauth_token="different"))
    assert c.fingerprint(a) != c.fingerprint(dict(a, zhipu_oauth_token="different"))
    om.add_account(a); key = om.get_account_key(a)
    assert om.get_account(key)["zhipu_oauth_token"] == "original"
    assert json.loads(Path(config.path()).read_text())["oauthAccounts"][0]["zhipu_oauth_token"] == "original"
    assert "original" not in json.dumps(redact_known_fields(a))
    old_import = dict(a); old_import.pop("zhipu_oauth_token")
    om.add_account(old_import)
    assert not om.get_account(key).get("zhipu_oauth_token")


def test_rejection_during_pending_refresh_cannot_fallback_to_old_token(monkeypatch, clock):
    a = account()
    entered, release = threading.Event(), threading.Event()
    def delayed_failure():
        entered.set()
        assert release.wait(3)
        raise c.ZhipuError("project_token", "timeout")
    install(monkeypatch, [material(seconds=200), delayed_failure])
    old = auth.resolve_model_key(a)
    clock[0] = 1050
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(auth.resolve_model_key, copy.deepcopy(a))
        assert entered.wait(3)
        rejected = pool.submit(auth.resolve_model_key, copy.deepcopy(a), rejected_fingerprint=pt.token_fingerprint(old))
        # Observe the actual rejection before releasing the failed IO.
        import time as real_time
        deadline = real_time.monotonic() + 3
        while pt._entries[pt.scope(a)].get("value") is not None:
            assert real_time.monotonic() < deadline
            real_time.sleep(.001)
        release.set()
        for future in (refresh, rejected):
            with pytest.raises(c.ZhipuError):
                future.result(3)
    assert pt.scope(a) not in pt._entries


def test_zai_concurrent_business_rejections_exchange_once(account_env, monkeypatch):
    a = account(site="zai", zhipu_oauth_token="source")
    om.add_account(a); key = om.get_account_key(a)
    snapshot = copy.deepcopy(om.get_account(key))
    barrier = threading.Barrier(2)
    exchanges = []
    def wire(url, **kw):
        if url.endswith("/api/auth/z/login"):
            exchanges.append(kw["body"])
            return {"access_token":"second"}
        if kw["headers"]["Authorization"] == "fixture-biz":
            barrier.wait(3)
            raise c.ZhipuError("projects", status=401)
        assert kw["headers"]["Authorization"] == "second"
        return {"organizations": []}
    monkeypatch.setattr(c, "request", wire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(auth.project_choices, copy.deepcopy(snapshot), account_key=key) for _ in range(2)]
        assert [job.result(3) for job in jobs] == [[], []]
    assert exchanges == [{"token":"source"}]


def test_business_rejection_cannot_borrow_unrelated_replacement(account_env, monkeypatch):
    a = account(site="zai", zhipu_oauth_token="source")
    om.add_account(a); key = om.get_account_key(a)
    snapshot = copy.deepcopy(om.get_account(key))
    def wire(url, **kw):
        assert not url.endswith("/api/auth/z/login")
        om.mutate_account_if_unchanged(key, snapshot, lambda v: v.update(access_token="unrelated-replacement"))
        raise c.ZhipuError("projects", status=401)
    monkeypatch.setattr(c, "request", wire)
    with pytest.raises(c.ZhipuError, match="stale_generation"):
        auth.project_choices(snapshot, account_key=key)
    assert om.get_account(key)["access_token"] == "unrelated-replacement"


def test_quota_normal_path_refreshes_expiring_pat_preserves_management_headers(account_env, monkeypatch, clock):
    a = account(project_key_id="fixture", model_key="")
    om.add_account(a); key = om.get_account_key(a)
    issued, quota_tokens = [], []
    def wire(url, **kw):
        if url.endswith("/access_tokens"):
            issued.append(True)
            return material(str(len(issued)), seconds=200)
        if "/quota/limit" in url:
            quota_tokens.append(kw["headers"]["authorization"])
            return {"limits": []}
        if url.endswith("/subscription/list"):
            assert kw["headers"]["Authorization"] == "fixture-biz"
            return []
        assert url.endswith("/mcp/usage")
        assert kw["headers"]["Authorization"] == "Bearer fixture-platform"
        assert kw["headers"]["X-Bigmodel-Authorization"] == "Bearer fixture-biz"
        return {}
    monkeypatch.setattr(c, "request", wire)
    runtime.fetch_usage_sync(copy.deepcopy(om.get_account(key)), key)
    clock[0] = 1050
    runtime.fetch_usage_sync(copy.deepcopy(om.get_account(key)), key)
    assert len(issued) == 2 and quota_tokens == [pat(tag="1"), pat(tag="2")]


def test_entire_pat_chain_exchanges_business_at_most_once(monkeypatch):
    a = account(site="zai", zhipu_oauth_token="source")
    exchanges = []
    def wire(url, **kw):
        if url.endswith("/api/auth/z/login"):
            exchanges.append(True)
            return {"access_token":"renewed"}
        if kw["headers"]["Authorization"] == "Bearer fixture-biz" or url.endswith("/access_tokens"):
            raise c.ZhipuError("request", status=401)
        return [{"apiKey":"fixture", "name":"zcode-api-key"}]
    monkeypatch.setattr(c, "request", wire)
    with pytest.raises(c.ZhipuError):
        auth.resolve_model_key(a)
    assert exchanges == [True]


async def test_no_refresh_switch_never_falls_back_to_oauth_legacy_key(account_env, monkeypatch):
    a = account(); om.add_account(a); key = om.get_account_key(a)
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(c, "request", lambda *a, **kw: pytest.fail("operator forbids renewal"))
    with pytest.raises(c.ZhipuError, match="disabled"):
        await om.ensure_valid_token(key)
