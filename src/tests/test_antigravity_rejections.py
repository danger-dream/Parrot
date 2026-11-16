"""Synthetic protocol fixtures, not new observations of Google policy-ban reasons."""
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from src.providers.antigravity_errors import parse_antigravity_error, parse_antigravity_429
from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_package_a import _patch_failover_runtime, Parsed


def body(reason=None, status="RESOURCE_EXHAUSTED", **extra):
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]
    if reason:
        details.append({"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                        "domain": "cloudcode-pa.googleapis.com", "metadata": {
                            "validation_url": "https://private.invalid/token", "prompt": "private prompt"}})
    return {"error": {"status": status, "details": details, **extra}}


@pytest.mark.parametrize("raw", ["quota exhausted Codex Hermes", {"error": {"reason": "QUOTA_EXHAUSTED"}},
    body("UNOBSERVED_REASON", message="QUOTA_EXHAUSTED"), body(message="INSUFFICIENT_G1_CREDITS_BALANCE")])
def test_unknown_is_not_quota_or_retry_evidence(raw):
    from src.protocols.runtime import AttemptResult, retryable_transient_error_kind
    signal = parse_antigravity_429(raw)
    assert signal["classification"] == "unknown"
    assert not signal["quota_exhausted"] and not signal["account_quota_exhausted"]
    text = raw if isinstance(raw, str) else json.dumps(raw)
    assert retryable_transient_error_kind(SimpleNamespace(provider="antigravity"),
        AttemptResult(outcome="http_error", http_status=429, error_detail=text)) is None


@pytest.mark.parametrize("status,kind", [("INVALID_ARGUMENT", "request_rejected"),
    ("OUT_OF_RANGE", "request_rejected"),
    ("PERMISSION_DENIED", "account_rejected"), ("UNAUTHENTICATED", "account_rejected")])
@pytest.mark.parametrize("reason", ["QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE", "RATE_LIMIT_EXCEEDED"])
def test_protocol_rejection_wins_over_quota(status, kind, reason):
    signal = parse_antigravity_429(body(reason, status, message="quota exhausted"))
    assert signal["classification"] == kind
    assert not signal["quota_exhausted"]


@pytest.mark.parametrize("code", [400, 422, 403, 401])
def test_numeric_code_does_not_establish_request_scope(code):
    signal = parse_antigravity_429(body("QUOTA_EXHAUSTED", "", code=code))
    assert signal["quota_exhausted"] is (code in {400, 422})
    assert not signal["stop_failover"]


@pytest.mark.parametrize("http_status", [403, 429])
@pytest.mark.parametrize("reason", [None, "QUOTA_EXHAUSTED", "INSUFFICIENT_G1_CREDITS_BALANCE"])
def test_status_only_challenge_precedes_quota(http_status, reason):
    raw = body(reason, "VALIDATION_REQUIRED") if reason else {"error": {"status": "VALIDATION_REQUIRED"}}
    signal = parse_antigravity_error(raw, http_status=http_status)
    assert signal["classification"] == "validation_required"
    assert signal["reason"] == "VALIDATION_REQUIRED"
    assert not signal["stop_failover"]
    assert not signal["quota_exhausted"] and not signal["account_quota_exhausted"]
    assert signal["diagnostic"]["http_status"] == http_status


@pytest.mark.parametrize("http_status", [400, 422])
@pytest.mark.parametrize("raw", [None, "", "gateway configuration error", "INVALID_ARGUMENT", {},
    {"error": {"message": "request rejected"}}])
def test_bare_transport_rejection_has_no_request_scope(http_status, raw):
    signal = parse_antigravity_error(raw, http_status=http_status)
    assert signal["classification"] == "unknown"
    assert not signal["stop_failover"]
    assert not signal["quota_exhausted"] and not signal["account_quota_exhausted"]


def test_challenge_precedence_and_safe_diagnostics():
    raw = body("VALIDATION_REQUIRED", "PERMISSION_DENIED", message="private prompt")
    raw["error"]["details"].append({"@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "INSUFFICIENT_G1_CREDITS_BALANCE"})
    signal = parse_antigravity_error(raw, http_status=403)
    assert signal["classification"] == "validation_required"
    assert not signal["stop_failover"] and not signal["account_quota_exhausted"]
    diagnostic = signal["diagnostic"]
    assert diagnostic["http_status"] == 403 and diagnostic["retry_info_seconds"] == 1
    assert diagnostic["error_info"][0]["domain"] == "cloudcode-pa.googleapis.com"
    assert "private" not in json.dumps(diagnostic) and "metadata" not in json.dumps(diagnostic)


@pytest.mark.parametrize("reason,status,expected", [
    ("VALIDATION_REQUIRED", "PERMISSION_DENIED", False),
    ("QUOTA_EXHAUSTED", "INVALID_ARGUMENT", False),
    ("UNOBSERVED_REASON", "RESOURCE_EXHAUSTED", False),
    ("QUOTA_EXHAUSTED", "RESOURCE_EXHAUSTED", True),
    ("INSUFFICIENT_G1_CREDITS_BALANCE", "RESOURCE_EXHAUSTED", False),
])
def test_credits_requires_structured_quota(monkeypatch, reason, status, expected):
    from src import oauth_manager
    from src.antigravity.quota import can_retry_429_with_credits
    monkeypatch.setattr(oauth_manager, "antigravity_credits_usable", lambda _: True)
    assert can_retry_429_with_credits("fake", body(reason, status)) is expected
    assert not can_retry_429_with_credits("fake", body(reason, status), already_used_credits=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status,http_status,stop", [
    ("QUOTA_EXHAUSTED", "INVALID_ARGUMENT", 429, True),
    ("VALIDATION_REQUIRED", "PERMISSION_DENIED", 403, False),
    (None, "VALIDATION_REQUIRED", 403, False),
    (None, "VALIDATION_REQUIRED", 429, False),
    ("UNOBSERVED_REASON", "RESOURCE_EXHAUSTED", 429, False),
])
async def test_text_request_boundary_and_account_failover(m, monkeypatch, reason, status, http_status, stop):
    _setup(m)
    from src import failover, scheduler, cooldown
    from src.protocols.runtime import AttemptResult
    om = m["oauth_manager"]
    keys = [_add_ag_account(om, email=f"rejection{i}@fake.invalid", project=f"r{i}", models=["argon"]) for i in range(2)]
    channels = [m["AntigravityOAuthChannel"](om.get_account(key)) for key in keys]
    get = m["config"].get
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(m["config"], "get", get)
    monkeypatch.setattr(om, "antigravity_credits_usable", lambda _: True)
    async def forbidden(*a, **k): pytest.fail("no refresh or blind retry")
    monkeypatch.setattr(om, "force_refresh", forbidden)
    monkeypatch.setattr(failover.asyncio, "sleep", forbidden)
    calls = []
    raw = json.dumps(body(reason, status, message="quota exhausted private prompt"))
    async def attempt(ch, *a, **k):
        calls.append(ch.key)
        if len(calls) == 1:
            return AttemptResult(outcome="http_error", http_status=http_status, error_detail=raw, full_response_text=raw)
        return AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))
    # Even a generic overload label must not grant unknown Google 429 retries.
    original_kind = failover.retryable_transient_error_kind
    monkeypatch.setattr(failover, "retryable_transient_error_kind", lambda ch, result:
        "claudeOverloaded" if reason == "UNOBSERVED_REASON" else original_kind(ch, result))
    monkeypatch.setattr(failover, "_try_channel", attempt)
    route = scheduler.ScheduleResult([(ch, "argon") for ch in channels], None, False)
    response = await failover.run_failover(route, {"model": "argon"}, "reject", "key", "ip", False, time.time(), ingress_protocol="responses")
    assert len(calls) == (1 if stop else 2)
    assert response.status_code == (http_status if stop else 200)
    assert all(om.get_account(key)["enabled"] for key in keys)
    if not stop:
        state = cooldown.get_state(channels[0].key, "argon")
        assert state["cooldown_until"] > 0
        assert "private" not in state["last_error_message"]
        assert not cooldown.is_blocked(channels[1].key, "argon")
    else:
        assert not cooldown.is_blocked(channels[0].key, "argon")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,status,expected_calls", [
    ("QUOTA_EXHAUSTED", "INVALID_ARGUMENT", 1),
    ("VALIDATION_REQUIRED", "PERMISSION_DENIED", 2),
    ("UNOBSERVED_REASON", "RESOURCE_EXHAUSTED", 2),
])
async def test_image_rejection_boundary(monkeypatch, reason, status, expected_calls):
    from src.openai import images_runtime as rt
    from src import oauth_manager
    import httpx
    sources = [SimpleNamespace(key=f"oauth:antigravity:reject{i}", provider="antigravity", model="m", upstream="m",
        state_key=None, available_for=lambda _: True) for i in range(2)]
    monkeypatch.setattr(rt.image_catalog, "sources", lambda: sources)
    monkeypatch.setattr(rt.registry, "get_channel", lambda _: None)
    monkeypatch.setattr(rt.auth, "channel_allowed", lambda *a: True)
    monkeypatch.setattr(rt.cooldown, "is_blocked", lambda *a: False)
    monkeypatch.setattr(rt.cooldown, "get_state", lambda *a: {})
    monkeypatch.setattr(rt.cooldown, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(rt.antigravity_images, "validate_request", lambda *a, **k: None)
    monkeypatch.setattr(oauth_manager, "antigravity_credits_usable", lambda _: True)
    async def noop(*a, **k): return None
    async def acquire(*a): return True
    monkeypatch.setattr(rt.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(rt.concurrency, "release", lambda *a: None)
    monkeypatch.setattr(rt.imagine, "_finish_media_log", noop)
    monkeypatch.setattr(rt.imagine, "_start_media_log", noop)
    calls = []
    async def send(*a, **k):
        calls.append(1)
        return httpx.Response(403 if reason == "VALIDATION_REQUIRED" else 429,
                              json=body(reason, status, message="private prompt https://private.invalid/token"))
    monkeypatch.setattr(rt, "_send", send)
    request = SimpleNamespace(headers={}, client=None)
    response = await rt.execute(Parsed(model="m"), request=request, action="generate", key_name="key", cfg={})
    assert len(calls) == expected_calls
    assert "private" not in response.body.decode()
    assert json.loads(response.body)["error"]["diagnostic"]["status"] == status

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code,http_status', [
    ('google', 'INVALID_ARGUMENT', 200), ('empty', 'no_images', 200),
    ('invalid', 'image_result_error', 200), ('timeout', 'timeout', None),
    ('network', 'transport_error', None), ('http', 'upstream_http_500', 500),
])
async def test_image_public_diagnostic(monkeypatch, mode, code, http_status):
    from src.openai import images_runtime as rt
    from src.antigravity.images import normalize_response
    import httpx
    source = SimpleNamespace(key='oauth:antigravity:diag', provider='antigravity', model='m', upstream='m',
        state_key=None, available_for=lambda _: True)
    monkeypatch.setattr(rt.image_catalog, 'sources', lambda: [source])
    monkeypatch.setattr(rt.registry, 'get_channel', lambda _: None)
    monkeypatch.setattr(rt.auth, 'channel_allowed', lambda *a: True)
    monkeypatch.setattr(rt.cooldown, 'is_blocked', lambda *a: False)
    monkeypatch.setattr(rt.cooldown, 'get_state', lambda *a: {})
    monkeypatch.setattr(rt.cooldown, 'clear_on_success', lambda *a: None)
    monkeypatch.setattr(rt.antigravity_images, 'validate_request', lambda *a, **k: None)
    async def noop(*a, **k): pass
    async def acquire(*a): return True
    monkeypatch.setattr(rt.concurrency, 'try_acquire', acquire)
    monkeypatch.setattr(rt.concurrency, 'release', lambda *a: None)
    monkeypatch.setattr(rt.imagine, '_finish_media_log', noop)
    monkeypatch.setattr(rt.imagine, '_start_media_log', noop)
    calls = []
    async def send(*a, **k):
        calls.append(1)
        if mode == 'google': normalize_response(body(status='INVALID_ARGUMENT', message='private URL'), model='m')
        if mode == 'empty': normalize_response({}, model='m')
        if mode == 'timeout': raise httpx.ReadTimeout('private URL')
        if mode == 'network': raise httpx.ConnectError('private URL')
        return httpx.Response(500 if mode == 'http' else 200, content='private URL')
    monkeypatch.setattr(rt, '_send', send)
    response = await rt.execute(Parsed(model='m'), request=SimpleNamespace(headers={}, client=None), action='generate', key_name='k', cfg={})
    error = json.loads(response.body)['error']
    assert error['code'] == code and len(calls) == 1
    assert error['diagnostic'].get('http_status') == http_status
    assert 'private' not in error['message']
    assert ('rejected' in error['message']) is (mode == 'google')
