"""Independent review: real ingress/build/signing/transport/result/failover.

Only synthetic management/HTTP transports are replaced.  In particular restore
Signer.headers (the inherited fixtures stub it), and use the real executor.
Run only via src/tests/isolated_pytest.py.
"""
import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src import config, network, oauth_manager as om
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import signing
from src.tests import conftest, test_protocol_fake_upstreams as fake
from src.tests.test_zhipu_channel import env, body, call
from src.tests.test_zhipu_pat_recovery import oauth_env
from src.tests.test_zhipu_provider import credential

_REAL_SIGNER_HEADERS = signing.Signer.headers


@pytest.fixture
def review_env(oauth_env, monkeypatch):
    environment, issued = oauth_env
    monkeypatch.setattr(signing.Signer, "headers", _REAL_SIGNER_HEADERS)
    monkeypatch.setattr(asyncio, "to_thread", conftest._ORIG_TO_THREAD)
    monkeypatch.delenv("DISABLE_OAUTH_NETWORK_CALLS", raising=False)
    config.update(lambda cfg: cfg.setdefault("oauth", {}).update(mockMode=False))
    private = Ed25519PrivateKey.generate()
    der = private.private_bytes(serialization.Encoding.DER,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
    handshakes = []

    def handshake(request):
        assert request.url.path == "/api/paas/c1f3a7e2/v2/client"
        value = json.loads(request.content)
        handshakes.append(value)
        token = value.get("accessToken") or value["apiKey"]
        key_id, secret = signing.credential(token)
        iv = b"0123456789ab"
        encrypted = iv + AESGCM(signing.derive(secret, "ed25519_priv")).encrypt(
            iv, base64.b64encode(der), key_id.encode())
        return httpx.Response(200, json={"code": 200, "data": {
            "privateCipher": base64.b64encode(encrypted).decode()}})

    monkeypatch.setattr(network, "async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(handshake)))
    yield environment, issued, handshakes


def add_backup(environment):
    account = credential(model_key="backup.secret", models=["GLM-5.3"])
    om.add_account(account)
    backup = ZhipuOAuthChannel(om.get_account(om.get_account_key(account)))
    fake._install_channels(environment[0], [environment[1], backup])
    return backup


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, error=None):
        self.error = error or httpx.ReadError("accepted headers then interrupted body")

    async def __aiter__(self):
        raise self.error
        yield b""  # pragma: no cover


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("failure", ["malformed_json", "empty_json", "broken_non_json", "empty_sse", "broken_sse",
                                     "timeout_non_json", "timeout_sse", "unexpected_sse"])
async def test_review_accepted_response_never_reaches_backup(review_env, ingress, failure):
    environment, issued, handshakes = review_env
    add_backup(environment)
    stream = failure.endswith("sse")
    calls = []

    def wire(request):
        calls.append(request)
        if len(calls) > 1:
            return fake._anthropic_sse_response("DUPLICATE") if stream else fake._anthropic_response("DUPLICATE")
        if failure == "malformed_json":
            return httpx.Response(200, content=b"{", headers={"content-type": "application/json"})
        if failure == "empty_json":
            return httpx.Response(200, content=b"", headers={"content-type": "application/json"})
        if failure in {"broken_non_json", "timeout_non_json"}:
            error = httpx.ReadTimeout("accepted body timeout") if failure.startswith("timeout") else None
            return httpx.Response(200, stream=BrokenBody(error), headers={"content-type": "text/plain"})
        if failure == "empty_sse":
            return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})
        error = (httpx.ReadTimeout("accepted body timeout") if failure == "timeout_sse" else
                 ValueError("unexpected stream read failure") if failure == "unexpected_sse" else None)
        return httpx.Response(200, stream=BrokenBody(error), headers={"content-type": "text/event-stream"})

    _, text, requests = await call(environment, ingress, body(ingress, stream), wire)
    assert len(requests) == 1, [(r.headers["x-api-key"], r.url.path) for r in requests]
    assert "DUPLICATE" not in text


@pytest.mark.parametrize("reason", ["quota", "1310", "rate_limit", "usage_limit"])
async def test_review_quota_401_does_not_disable_account(review_env, reason):
    environment, issued, _ = review_env
    _, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error": {"code": reason, "message": reason}}))
    assert len(issued) == 1 and len(requests) == 1
    assert om.get_account(environment[1].account_key)["enabled"]

@pytest.mark.parametrize("metadata", ["request-3007-abc", "trace-1310-abc", "quota-monitor"])
async def test_review_unrelated_metadata_does_not_suppress_plain_401_recovery(review_env, metadata):
    environment, issued, _ = review_env
    attempts = []
    def wire(req):
        attempts.append(req)
        if len(attempts) == 1:
            return httpx.Response(401, json={"error": {"message": "token expired"}, "request_id": metadata,
                "metadata": {"code": "3007", "message": "quota"}, "data": {"request_id": metadata}})
        return fake._anthropic_response("recovered")
    result, text, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert len(issued) == 2 and len(requests) == 2
    assert result.status_code == 200 and "recovered" in text
    assert om.get_account(environment[1].account_key)["enabled"]


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("final_status", [200, 401])
async def test_review_plain_401_actual_signer_recovery(review_env, ingress, stream, final_status):
    environment, issued, handshakes = review_env
    count = []
    def wire(req):
        count.append(req)
        assert req.headers.get("x-client-sig")
        if len(count) == 1 or final_status == 401:
            return httpx.Response(401, json={"error": {"message": "token expired"}})
        return fake._anthropic_sse_response("recovered") if stream else fake._anthropic_response("recovered")
    result, text, requests = await call(environment, ingress, body(ingress, stream), wire)
    assert len(issued) == len(requests) == len(handshakes) == 2
    assert requests[0].content == requests[1].content
    for header in ("x-session-id", "x-query-id", "x-zcode-trace-id"):
        assert requests[0].headers[header] == requests[1].headers[header]
    assert requests[0].headers["x-request-id"] != requests[1].headers["x-request-id"]
    assert requests[0].headers["x-client-nonce"] != requests[1].headers["x-client-nonce"]
    assert om.get_account(environment[1].account_key)["enabled"] is (final_status == 200)
    assert result.status_code == final_status
    if final_status == 200:
        assert "recovered" in text


async def test_review_actual_signer_thinking_plus_pat_eight_sends(review_env):
    environment, issued, handshakes = review_env
    count = []
    def wire(req):
        count.append(req)
        n = len(count)
        if n in (1, 3, 4, 6, 7):
            return httpx.Response(401, json={"reason": "VERIFY_SIGNATURE_INVALID"})
        if n == 2:
            return httpx.Response(400, json={"error": {"message": "Invalid signature in thinking block"}})
        if n == 5:
            return httpx.Response(401, json={"error": {"message": "PAT rejected"}})
        return fake._anthropic_response("eight sends")
    result, text, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert result.status_code == 200 and "eight sends" in text
    assert len(requests) == 8 and len(issued) == 2 and len(handshakes) == 5
    assert [bool(r.headers.get("x-client-sig")) for r in requests] == [True, True, True, True, False, True, True, False]
    for start, end in ((0, 2), (2, 5), (5, 8)):
        assert len({r.content for r in requests[start:end]}) == 1
        assert len({r.headers["x-request-id"] for r in requests[start:end]}) == 1
    for header in ("x-session-id", "x-query-id", "x-zcode-trace-id"):
        assert len({r.headers[header] for r in requests}) == 1


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["overload", "thinking"])
async def test_review_body_errors_do_not_enter_transient_or_thinking_retry(review_env, ingress, stream, failure):
    environment, issued, _ = review_env
    add_backup(environment)
    error = {"code": 2007} if failure == "overload" else {
        "type": "error", "error": {"type": "invalid_request_error", "message": "Invalid signature in thinking block"}}
    def wire(req):
        if stream:
            return httpx.Response(200, content=b"event: error\ndata: " + json.dumps(error).encode() + b"\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=error)
    _, _, requests = await call(environment, ingress, body(ingress, stream), wire)
    assert len(requests) == 1 and len(issued) == 1
    assert om.get_account(environment[1].account_key)["enabled"]


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_review_body_context_error_preserves_public_400(review_env, ingress, stream):
    environment, issued, _ = review_env
    value = {"type": "error", "error": {"type": "invalid_request_error",
        "code": "context_length_exceeded", "message": "prompt is too long"}}
    def wire(req):
        if stream:
            return httpx.Response(200, content=b"event: error\ndata: " + json.dumps(value).encode() + b"\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=value)
    response, text, requests = await call(environment, ingress, body(ingress, stream), wire)
    assert len(requests) == len(issued) == 1
    assert response.status_code == 400, text
    assert "context_length_exceeded" in text


async def test_review_plan_excluded_401_is_not_pat_rejection(review_env):
    environment, issued, _ = review_env
    _, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error": {"code": "1311",
            "message": "Your current subscription plan does not yet include this model"}}))
    assert len(issued) == 1 and len(requests) == 1
    assert om.get_account(environment[1].account_key)["enabled"]


@pytest.mark.parametrize("change", ["replace", "new_pat"])
async def test_review_inflight_401_cannot_disable_peer_credential(review_env, change):
    import copy
    from src.oauth.zhipu import auth
    environment, issued, _ = review_env
    key = environment[1].account_key
    entered, release = asyncio.Event(), asyncio.Event()
    requests_seen = []
    async def wire(req):
        requests_seen.append(req)
        if len(requests_seen) == 1:
            entered.set()
            await release.wait()
            return httpx.Response(401, json={"error": {"message": "old token rejected"}})
        return fake._anthropic_response("peer token")
    task = asyncio.create_task(call(environment, "anthropic", body("anthropic"), wire))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = copy.deepcopy(om.get_account(key))
        if change == "replace":
            await asyncio.to_thread(om.mutate_account_if_unchanged, key, current,
                lambda a: a.update(access_token="new-login"))
        else:
            await asyncio.to_thread(auth.resolve_model_key, current, account_key=key, force=True)
        release.set()
        _, _, requests = await asyncio.wait_for(task, 5)
        assert om.get_account(key)["enabled"]
        assert len(requests) == (2 if change == "new_pat" else 1)
        assert len(issued) == (2 if change == "new_pat" else 1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("extra", [2, 5])
async def test_review_transient_plus_thinking_plus_pat_composed_send_budget(review_env, extra):
    environment, issued, _ = review_env
    config.update(lambda cfg: cfg.update(retry={"transient": {
        "enabled": True, "maxExtraAttempts": extra, "backoffSeconds": [0]}},
        timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 20}))
    sequence = (["verify", "overload"] * extra +
                ["verify", "thinking", "verify", "verify", "pat", "verify", "verify", "ok"])
    requests_seen = []
    def wire(req):
        event = sequence[len(requests_seen)]
        requests_seen.append(req)
        if event == "verify":
            return httpx.Response(401, json={"reason": "VERIFY_SIGNATURE_INVALID"})
        if event == "overload":
            return httpx.Response(529, json={"error": {"type": "overloaded_error", "message": "busy"}})
        if event == "thinking":
            return httpx.Response(400, json={"error": {"message": "Invalid signature in thinking block"}})
        if event == "pat":
            return httpx.Response(401, json={"error": {"message": "token expired"}})
        return fake._anthropic_response("composed recovery")
    result, text, requests = await call(environment, "anthropic", body("anthropic"), wire)
    assert result.status_code == 200 and "composed recovery" in text
    assert len(requests) == 2 * extra + 8 and len(issued) == 2


@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses"])
async def test_review_committed_output_is_not_replayed(review_env, ingress):
    environment, issued, _ = review_env
    add_backup(environment)
    payload = fake._anthropic_sse_response("visible answer").content
    # Remove the successful stop; a real upstream error follows visible text.
    payload = payload[:payload.index(b"event: message_delta")]
    payload += b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
    _, text, requests = await call(environment, ingress, body(ingress, True),
        lambda req: httpx.Response(200, stream=fake.ChunkedByteStream(payload.splitlines(keepends=True)),
                                  headers={"content-type": "text/event-stream"}))
    assert "visible answer" in text
    assert len(requests) == len(issued) == 1


async def test_review_explicit_string_error_quota_is_not_auth_failure(review_env):
    environment, issued, _ = review_env
    _, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error": "quota"}))
    assert len(issued) == len(requests) == 1
    assert om.get_account(environment[1].account_key)["enabled"]


@pytest.mark.parametrize("reason,physical_sends", [("3007", 1), ("VERIFY_SIGNATURE_INVALID", 3)])
async def test_review_terminal_security_policy_is_preserved(review_env, reason, physical_sends):
    environment, issued, _ = review_env
    result, _, requests = await call(environment, "anthropic", body("anthropic"),
        lambda req: httpx.Response(401, json={"error": {"code": reason, "reason": reason, "message": reason}}))
    assert len(issued) == 1 and len(requests) == physical_sends
    assert result.status_code == 401
    current = om.get_account(environment[1].account_key)
    assert not current["enabled"] and current["disabled_reason"] == "auth_error"
