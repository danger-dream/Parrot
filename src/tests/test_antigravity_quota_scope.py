"""Isolated AG scope regressions. All upstreams are deterministic local stubs."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_package_a import _patch_failover_runtime, _google


@pytest.fixture(autouse=True)
def clean_scope_cooldowns():
    from src import cooldown, state_db
    state_db.init()
    key = "oauth:antigravity:state@gmail.com:proj-state"
    cooldown.clear(key, notify_recovered=False)
    yield
    cooldown.clear(key, notify_recovered=False)


def _summary(ag, bucket="3p-weekly"):
    # Synthetic Argon fixture is deliberately NOT an observed upstream bucket.
    return ag.parse_quota_summary({"groups": [
        {"displayName": "Gemini Models", "buckets": [{
            "bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": .8,
        }]},
        {"displayName": "Unknown independent group", "buckets": [{
            "bucketId": bucket, "window": "weekly", "remainingFraction": 0,
        }]},
    ]})


def _usage(ag, **block):
    return {"seven_day": {"utilization": 100}, "antigravity": {
        "known": False, "quota_groups": _summary(ag), **block,
    }}


@pytest.mark.parametrize("bucket", ["3p-weekly", "synthetic-argon-weekly"])
def test_independent_exhaustion_keeps_account_and_other_models(m, monkeypatch, bucket):
    _setup(m)
    ag, om = m["ag"], m["oauth_manager"]
    ak = _add_ag_account(om, models=["gemini-3.8-flash-high", "gemini-4-argon-medium"])
    monkeypatch.setattr(ag, "load_code_assist_sync", lambda *a, **k: {"paidTier": {
        "availableCredits": [{"creditType": "GOOGLE_ONE_AI", "creditAmount": "100", "minimumCreditAmountForUsage": "1"}],
    }})
    monkeypatch.setattr(ag, "fetch_quota_summary_sync", lambda *a, **k: _summary(ag, bucket))
    usage = ag.fetch_usage_sync("synthetic-token")
    assert usage["seven_day"]["utilization"] == 100  # still useful display
    assert om.evaluate_and_toggle_by_usage(ak, usage, threshold=100)["action"] == "kept_enabled"
    account = om.get_account(ak)
    channel = m["AntigravityOAuthChannel"](account)
    assert account["enabled"]
    assert channel.supports_model("gemini-3.8-flash-high") == "gemini-3.8-flash-high"
    assert channel.supports_model("gemini-4-argon-medium") == "gemini-4-argon-medium"
    from src import cooldown
    assert not cooldown.is_blocked(channel.key, "gemini-3.8-flash-high")
    assert not cooldown.is_blocked(channel.key, "gemini-4-argon-medium")


def test_unmapped_amount_disabled_and_incomplete_buckets_are_retained(m):
    _setup(m)
    groups = m["ag"].parse_quota_summary({"groups": [{"buckets": [
        {"bucketId": "amount", "remainingAmount": "0", "disabled": True},
        {"bucketId": "unknown", "window": "future"},
        {"bucketId": "invalid", "remainingFraction": float("nan")},
    ]}]})
    assert groups[0]["buckets"][0]["remaining_amount"] == 0
    assert groups[0]["buckets"][0]["disabled"] is True
    assert len(groups[0]["buckets"]) == 3
    assert "remaining_fraction" not in groups[0]["buckets"][2]
    ak = _add_ag_account(m["oauth_manager"])
    assert m["oauth_manager"].evaluate_and_toggle_by_usage(ak, {"antigravity": {"quota_groups": groups}})["action"] == "kept_enabled"


@pytest.mark.parametrize("missing", ["empty", "error", "stale", "no_fraction"])
def test_legacy_window_gate_requires_fresh_complete_evidence(m, missing):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_quota", "blocked": ["7d"]})
    usage = _usage(m["ag"], known=True, available=True)
    if missing == "empty":
        usage["antigravity"]["quota_groups"] = []
    elif missing == "error":
        usage["antigravity"]["quota_error"] = {"kind": "server_error"}
    elif missing == "stale":
        usage["antigravity"]["quota_groups_stale"] = True
    else:
        usage["antigravity"]["quota_groups"] = [{"buckets": [{"window": "weekly"}]}]
    assert om.evaluate_and_toggle_by_usage(ak, usage)["action"] == "quota_unknown_keep_disabled"
    good = _usage(m["ag"])
    assert om.evaluate_and_toggle_by_usage(ak, good, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(ak, good)["action"] == "resumed"


def test_legacy_mixed_gates_credits_and_unknown_refusals_not_erased(m):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_quota", "blocked": ["Credits", "7d"]})
    assert om.evaluate_and_toggle_by_usage(ak, _usage(m["ag"]))["action"] == "quota_unknown_keep_disabled"
    exhausted = {"antigravity": {"known": True, "available": False}}
    om.evaluate_and_toggle_by_usage(ak, exhausted)
    assert set(om.get_account(ak)["quota_observation"]["blocked"]) == {"Credits", "7d"}
    assert om.evaluate_and_toggle_by_usage(ak, {"antigravity": {"known": True, "available": True}})["action"] == "quota_unknown_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(ak, _usage(m["ag"], known=True, available=True))["action"] == "resumed"
    om.set_disabled_by_quota(ak, None)  # legacy untyped refusal: scope cannot be reconstructed
    assert om.evaluate_and_toggle_by_usage(ak, _usage(m["ag"], known=True, available=True))["action"] == "quota_unknown_keep_disabled"


@pytest.mark.parametrize("reason", ["user", "auth_error"])
def test_account_manual_and_auth_gates_survive_all_quota_signals(m, reason):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om, enabled=False, disabled_reason=reason)
    for available in (True, False):
        assert om.evaluate_and_toggle_by_usage(ak, _usage(m["ag"], known=True, available=available))["action"] == f"noop_{reason}"
    from src.antigravity.quota import apply_request_429
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    apply_request_429(channel.state_key, "argon", _google("300s", "INSUFFICIENT_G1_CREDITS_BALANCE"),
                      account_key=ak, expected_state_key=channel.state_key)
    assert om.get_account(ak)["disabled_reason"] == reason


def test_legacy_recovery_cas_cannot_erase_new_account_refusal(m, monkeypatch):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_quota", "blocked": ["7d"]})
    enable = om.set_enabled
    def race(*args, **kwargs):
        om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_account_quota", "reason": "INSUFFICIENT_G1_CREDITS_BALANCE"})
        return enable(*args, **kwargs)
    monkeypatch.setattr(om, "set_enabled", race)
    assert om.evaluate_and_toggle_by_usage(ak, _usage(m["ag"]))["action"] == "quota_observation_conflict"
    assert om.get_account(ak)["quota_observation"]["source"] == "antigravity_account_quota"


@pytest.mark.asyncio
async def test_usage_started_before_refusal_cannot_recover_but_later_credits_can(m, monkeypatch):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    async def fetch(*a, **k):
        om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_account_quota", "reason": "INSUFFICIENT_G1_CREDITS_BALANCE"})
        return _usage(m["ag"], known=True, available=True)
    monkeypatch.setattr(m["ag"], "fetch_usage", fetch)
    old = await om.fetch_usage(ak)
    assert om.evaluate_and_toggle_by_usage(ak, old)["action"] == "quota_observation_conflict"
    async def later(*a, **k): return _usage(m["ag"], known=True, available=True)
    monkeypatch.setattr(m["ag"], "fetch_usage", later)
    fresh = await om.fetch_usage(ak)
    assert om.evaluate_and_toggle_by_usage(ak, fresh, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(ak, fresh)["action"] == "resumed"


@pytest.mark.parametrize("reason,delay", [("RATE_LIMIT_EXCEEDED", "300s"), ("QUOTA_EXHAUSTED", "3600s"), ("QUOTA_EXHAUSTED", None)])
@pytest.mark.asyncio
async def test_full_failover_only_pauses_resolved_model_not_metadata_family(m, monkeypatch, reason, delay):
    _setup(m)
    from src import failover, scheduler, cooldown
    from src.protocols.runtime import AttemptResult
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=["gemini-4-argon-medium", "gemini-4-argon-high", "gemini-3.8-flash-high"])
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    get = m["config"].get
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(m["config"], "get", get)
    raw = json.loads(_google(delay, reason))
    raw["error"]["details"][1]["metadata"] = {"model": "gemini", "private": "do-not-copy"}
    raw = json.dumps(raw)
    calls = []
    async def attempt(channel, model, *a, **k):
        calls.append(model)
        if len(calls) == 1:
            return AttemptResult(outcome="http_error", http_status=429, error_detail=raw, full_response_text=raw)
        return AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))
    monkeypatch.setattr(failover, "_try_channel", attempt)
    route = scheduler.ScheduleResult([(ch, "gemini-4-argon-medium"), (ch, "gemini-3.8-flash-high")], None, False)
    response = await failover.run_failover(route, {"model": "gemini-4-argon-medium"}, "scope", "key", "ip", False, time.time(), ingress_protocol="responses")
    assert response.status_code == 200
    assert calls == ["gemini-4-argon-medium", "gemini-3.8-flash-high"]
    assert om.get_account(ak)["enabled"]
    assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    assert not cooldown.is_blocked(ch.key, "gemini-4-argon-high")
    assert not cooldown.is_blocked(ch.key, "gemini-3.8-flash-high")
    state = cooldown.get_state(ch.key, "gemini-4-argon-medium")
    assert 0 < state["cooldown_until"] - int(time.time() * 1000) <= 3600_000
    assert "do-not-copy" not in state["last_error_message"]
    classified = failover._structured_attempt_error(AttemptResult(outcome="http_error", http_status=429, error_detail=raw), 1, ch)
    assert classified["classification"] == "rate_limit_error"


def _reset(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return copy.deepcopy(self.payload)


async def _discover(m, monkeypatch, ak, payloads):
    om, ag = m["oauth_manager"], m["ag"]
    monkeypatch.setattr(om, "mock_mode_enabled", lambda: False)
    async def token(*a, **k): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    def request(method, url, **kwargs):
        endpoint = "prod" if url.startswith(ag.api_base_url()) else "daily"
        data = payloads[endpoint]
        if isinstance(data, Exception): raise data
        return Response(data)
    monkeypatch.setattr(ag, "request_sync", request)
    return await om.refresh_account_models(ak)


def _catalog_payload(fraction=0, reset=None):
    quota = {"remainingFraction": fraction}
    if reset is not None: quota["resetTime"] = reset
    return {"models": {"gemini-4-argon-medium": {"displayName": "Argon", "quotaInfo": quota},
                        "gemini-3.8-flash-high": {"quotaInfo": {"remainingFraction": .8}},
                        "gemini-4-argon-high": {}}}


@pytest.mark.asyncio
async def test_catalog_roundtrip_sources_pause_recover_and_do_not_change_host(m, monkeypatch):
    _setup(m)
    from src import cooldown
    om, ag, cfg = m["oauth_manager"], m["ag"], m["config"]
    ak = _add_ag_account(om, models=["gemini-4-argon-medium"], disabledModels=["gemini-4-argon-high"])
    payloads = {"prod": _catalog_payload(.7, _reset()), "daily": _catalog_payload(0, _reset())}
    payloads["prod"]["models"]["gemini-4-argon-medium"]["displayName"] = "Prod Name"
    payloads["daily"]["models"]["gemini-4-argon-medium"]["quotaInfo"]["secret"] = "must-not-persist"
    result = await _discover(m, monkeypatch, ak, payloads)
    assert result["action"] == "updated"
    account = om.get_account(ak)
    catalog = account["account_model_catalog"]
    disk = json.loads(Path(cfg.CONFIG_PATH).read_text())["oauthAccounts"][0]
    assert disk["account_model_catalog"] == catalog
    earlier = copy.deepcopy(account)
    for record in earlier["account_model_catalog"]["models"]:
        for source in record["antigravitySources"]:
            source["observedAt"] -= 10_000
    assert om._normalize_model_refresh_result(ak, earlier, {"action": "updated"})["catalog_changed"] is False
    # Exercise the real disk loader/normalizer as well as the config write.
    monkeypatch.setattr(cfg, "_cache", None)
    assert om.get_account(ak)["account_model_catalog"] == catalog
    row = catalog["models"][0]
    assert row["name"] == "Argon"
    assert [s["endpoint"] for s in row["antigravitySources"]] == ["prod", "daily"]
    assert [s["quotaInfo"]["remainingFraction"] for s in row["antigravitySources"]] == [.7, 0]
    assert "must-not-persist" not in json.dumps(catalog)
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    assert ch.base_url == ag.daily_api_base_url()
    assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    assert not cooldown.is_blocked(ch.key, "gemini-3.8-flash-high")
    assert not cooldown.is_blocked(ch.key, "gemini-4-argon-high")
    assert om.get_account(ak)["enabled"]
    assert om.get_account(ak)["disabledModels"] == ["gemini-4-argon-high"]
    payloads["daily"] = _catalog_payload(.6, _reset())
    await _discover(m, monkeypatch, ak, payloads)
    assert not cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    assert om.get_account(ak)["disabledModels"] == ["gemini-4-argon-high"]
    # A permanent/manual freeze can retain an old diagnostic marker; it is
    # still not a pause owned by this bounded catalog policy.
    cooldown.record_error(ch.key, "gemini-4-argon-medium", json.dumps({"error": {
        "code": "antigravity_catalog_quota", "baseUrl": ch.base_url,
    }}), cooldown_until=-1)
    await _discover(m, monkeypatch, ak, payloads)
    assert cooldown.get_state(ch.key, "gemini-4-argon-medium")["cooldown_until"] == -1


@pytest.mark.asyncio
async def test_catalog_absence_failure_old_data_and_real_429_never_prove_recovery(m, monkeypatch):
    _setup(m)
    from src import cooldown
    from src.antigravity.quota import apply_fresh_catalog, apply_request_429
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=["gemini-4-argon-medium"])
    payloads = {"prod": _catalog_payload(.8), "daily": _catalog_payload(0, _reset())}
    await _discover(m, monkeypatch, ak, payloads)
    account = om.get_account(ak)
    ch = m["AntigravityOAuthChannel"](account)
    for data in ({"models": {"gemini-4-argon-medium": {}}}, RuntimeError("offline")):
        payloads["daily"] = data
        await _discover(m, monkeypatch, ak, payloads)
        assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    old = copy.deepcopy(account["account_model_catalog"])
    for source in old["models"][0]["antigravitySources"]:
        source["observedAt"] -= 600_000
        source["quotaInfo"]["remainingFraction"] = .9
    apply_fresh_catalog(account, old, expected_state_key=ch.state_key)
    assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    apply_request_429(ch.state_key, "gemini-4-argon-medium", _google("300s"), account_key=ak, expected_state_key=ch.state_key)
    payloads["daily"] = _catalog_payload(.8, _reset())
    await _discover(m, monkeypatch, ak, payloads)
    assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    assert "antigravity_model_429" in cooldown.get_state(ch.key, "gemini-4-argon-medium")["last_error_message"]
    cooldown.record_error(ch.key, "gemini-4-argon-medium", "operator freeze", cooldown_until=-1)
    for fraction in (0, .8):
        payloads["daily"] = _catalog_payload(fraction, _reset())
        await _discover(m, monkeypatch, ak, payloads)
        assert cooldown.get_state(ch.key, "gemini-4-argon-medium")["last_error_message"] == "operator freeze"


@pytest.mark.parametrize("reset", [None, "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "bad"])
@pytest.mark.asyncio
async def test_catalog_unbounded_or_past_reset_is_display_only(m, monkeypatch, reset):
    _setup(m)
    from src import cooldown
    ak = _add_ag_account(m["oauth_manager"])
    await _discover(m, monkeypatch, ak, {"prod": _catalog_payload(0, reset), "daily": _catalog_payload(0, reset)})
    assert not cooldown.is_blocked("oauth:" + ak, "gemini-4-argon-medium")


@pytest.mark.asyncio
async def test_prod_quota_does_not_gate_daily_or_change_explicit_route(m, monkeypatch):
    _setup(m)
    from src import cooldown
    om, ag = m["oauth_manager"], m["ag"]
    ak = _add_ag_account(om)
    payloads = {"prod": _catalog_payload(0, _reset()), "daily": _catalog_payload(.8)}
    await _discover(m, monkeypatch, ak, payloads)
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    assert ch.base_url == ag.daily_api_base_url()
    assert not cooldown.is_blocked(ch.key, "gemini-4-argon-medium")
    m["config"].update(lambda cfg: cfg["oauthAccounts"][0].update(request_base_url=ag.api_base_url()))
    await _discover(m, monkeypatch, ak, payloads)
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    assert ch.base_url == ag.api_base_url()
    assert cooldown.is_blocked(ch.key, "gemini-4-argon-medium")


@pytest.mark.parametrize("raw", [None, [], "bad", {"remainingFraction": True}, {"remainingFraction": float("inf")}, {"remainingFraction": -1}, {"remainingFraction": 2}, {"resetTime": "2026-01-01"}])
def test_model_quota_metadata_validation(raw):
    from src.antigravity.quota import normalize_quota_info
    assert normalize_quota_info(raw) == {}


def test_cached_threshold_path_never_recovers_legacy_gate(m):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    om.set_disabled_by_quota(ak, None, observation={"source": "antigravity_quota", "blocked": ["7d"]})
    usage = _usage(m["ag"], known=True, available=True)
    m["state_db"].quota_save(ak, om.flatten_usage(usage), email="state@gmail.com")
    assert om.evaluate_and_toggle_by_cached_quota(ak, threshold=100)["action"] == "quota_stale_keep_disabled"
    assert om.get_account(ak)["enabled"] is False


@pytest.mark.parametrize("reason", ["QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE"])
def test_late_rejection_cannot_affect_replacement_generation(m, reason):
    _setup(m)
    from src import cooldown
    from src.antigravity.quota import apply_request_429
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    old = m["AntigravityOAuthChannel"](om.get_account(ak))
    om.delete_account(ak)
    assert _add_ag_account(om) == ak
    apply_request_429(old.state_key, "argon", _google("300s", reason), account_key=ak, expected_state_key=old.state_key)
    assert om.get_account(ak)["enabled"] is True
    assert not cooldown.is_blocked("oauth:" + ak, "argon")


@pytest.mark.asyncio
async def test_full_failover_explicit_credits_disable_and_next_account(m, monkeypatch):
    _setup(m)
    from src import failover, scheduler, cooldown
    from src.protocols.runtime import AttemptResult
    om = m["oauth_manager"]
    first = _add_ag_account(om, models=["argon"])
    second = _add_ag_account(om, email="second@fake.invalid", project="second", models=["argon"])
    channels = [m["AntigravityOAuthChannel"](om.get_account(key)) for key in (first, second)]
    get = m["config"].get
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(m["config"], "get", get)
    calls = []
    async def attempt(ch, *a, **k):
        calls.append(ch.account_key)
        if len(calls) == 1:
            raw = _google("300s", "INSUFFICIENT_G1_CREDITS_BALANCE")
            return AttemptResult(outcome="http_error", http_status=429, error_detail=raw, full_response_text=raw)
        return AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))
    monkeypatch.setattr(failover, "_try_channel", attempt)
    route = scheduler.ScheduleResult([(ch, "argon") for ch in channels], None, False)
    response = await failover.run_failover(route, {"model": "argon"}, "credits", "key", "ip", False, time.time(), ingress_protocol="responses")
    assert response.status_code == 200 and calls == [first, second]
    assert om.get_account(first)["disabled_reason"] == "quota"
    assert om.get_account(first)["quota_observation"]["source"] == "antigravity_account_quota"
    assert om.get_account(second)["enabled"] is True
    assert not cooldown.is_blocked(channels[0].key, "argon")  # not a generic permanent ladder


@pytest.mark.asyncio
async def test_short_retry_budget_and_stream_commit_stay_bounded(m, monkeypatch):
    _setup(m)
    from src import failover, scheduler, cooldown
    from src.protocols.runtime import AttemptResult
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=["argon"])
    ch = m["AntigravityOAuthChannel"](om.get_account(ak))
    get = m["config"].get
    m["config"].update(lambda cfg: cfg.update(retry={"transient": {"enabled": True, "maxExtraAttempts": 2}}))
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(m["config"], "get", get)
    calls, sleeps = [], []
    async def attempt(*a, **k):
        calls.append(1)
        return AttemptResult(outcome="http_error", http_status=429, error_detail=_google("1.5s"), retry_after_seconds=1.5)
    async def sleep(delay): sleeps.append(delay)
    monkeypatch.setattr(failover, "_try_channel", attempt)
    monkeypatch.setattr(failover.asyncio, "sleep", sleep)
    route = scheduler.ScheduleResult([(ch, "argon")], None, False)
    await failover.run_failover(route, {"model": "argon"}, "short", "key", "ip", False, time.time(), ingress_protocol="responses")
    assert len(calls) == 3 and sleeps == [1.5, 1.5]
    assert cooldown.is_blocked(ch.key, "argon")
    assert om.get_account(ak)["enabled"] is True
    # An in-flight success must not remove a newer rejection's pause.
    assert cooldown.clear_on_success(ch.key, "argon") is False
    until = cooldown.get_state(ch.key, "argon")["cooldown_until"]
    monkeypatch.setattr(cooldown, "_now_ms", lambda: until + 1)
    assert not cooldown.is_blocked(ch.key, "argon")
    async def committed(*a, **k):
        calls.append(1)
        return AttemptResult(outcome="http_error", http_status=429, error_detail=_google("300s"),
                             stream_started=True, response=JSONResponse({"error": "already committed"}))
    monkeypatch.setattr(failover, "_try_channel", committed)
    await failover.run_failover(route, {"model": "argon"}, "stream", "key", "ip", True, time.time(), ingress_protocol="responses")
    assert len(calls) == 4 and sleeps == [1.5, 1.5]


@pytest.mark.parametrize("delay", [True, float("inf"), float("nan"), -1, "-2s", "huge", 10 ** 500])
def test_untrusted_retry_delays_are_rejected(delay):
    from src.providers.antigravity_errors import parse_google_duration
    assert parse_google_duration(delay) is None


def test_reason_in_message_or_model_metadata_is_not_account_scope():
    from src.providers.antigravity_errors import parse_antigravity_429
    payload = {"error": {"message": "INSUFFICIENT_G1_CREDITS_BALANCE", "details": [
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED",
         "metadata": {"reason": "INSUFFICIENT_G1_CREDITS_BALANCE", "model": "all-models"}},
    ]}}
    assert parse_antigravity_429(payload)["account_quota_exhausted"] is False
