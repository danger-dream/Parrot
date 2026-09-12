"""#31: real ASGI ingress/failover/WorkBuddy channels, mock upstream only.

Launch via src/tests/isolated_pytest.py. No server lifespan, live credentials,
OAuth refresh, upstream network, or Telegram services are used.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

from src import config, cooldown, notifier, oauth_manager, scorer, upstream
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
from src.oauth.workbuddy import auth
from src.providers.workbuddy_codec import WorkBuddyStream
from src.providers.workbuddy_errors import UNAPPROVED_CHANNEL_MESSAGE
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_workbuddy_channel import ev, frames, request_body


MODEL = "glm-fixture"
REJECTION = {
    "code": 11128,
    "msg": UNAPPROVED_CHANNEL_MESSAGE,
    "requestId": "fixture-request-id",
    "displayMsg": {"zh": "请求被安全策略拦截，请稍后重试或联系支持。"},
    # Arbitrary vendor extensions are not safe downstream diagnostics.
    "debug": "fixture-private-detail",
}
PATHS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "anthropic": "/v1/messages"}


@pytest.fixture
async def pool(monkeypatch):
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    # A valid fixture token exercises the real token lookup, while refresh is
    # forbidden even with recovery.oauthRefresh enabled (not hidden by env).
    monkeypatch.delenv("PARROT_NO_REFRESH", raising=False)
    def forbidden_refresh(*args, **kwargs):
        raise AssertionError("request rejection must never refresh OAuth")
    monkeypatch.setattr(auth, "refresh_sync", forbidden_refresh)
    monkeypatch.setattr(oauth_manager, "force_refresh", forbidden_refresh)
    monkeypatch.setattr(notifier, "notify_event", lambda *args, **kwargs: None)
    async def no_notify(*args, **kwargs):
        pass
    monkeypatch.setattr(notifier, "throttled_notify_event", no_notify)
    entries = []
    for i in range(7):
        entry = auth.normalize_credential({"realm": "cn", "uid": f"reject-{uuid.uuid4().hex}-{i}",
            "access_token": f"fixture-access-{i}", "refresh_token": "fixture-refresh",
            "expired": auth.utc_text(time.time() + 7200)})
        entry.update(models=[MODEL, "deepseek-fixture"], account_model_catalog={"schema": 1,
            "models": [{"id": MODEL}, {"id": "deepseek-fixture"}]})
        entries.append(entry)
    config.update(lambda c: c.update(oauthAccounts=entries, channels=[],
        network={"routing": {"default": "direct"}},
        timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 10},
        protocolBridge={"enabled": True}, concurrency={"queueWaitSeconds": 1},
        retry={"recovery": {"oauthRefresh": True}}))
    fake._install_keys(m, fake._default_key())
    channels = [WorkBuddyOAuthChannel(entry) for entry in entries]
    fake._install_channels(m, channels)
    p = SimpleNamespace(m=m, channels=channels, requests=[], response_factory=None)
    def wire(req):
        assert str(req.url) == "https://copilot.tencent.com/v2/chat/completions"
        assert req.headers["authorization"].startswith("Bearer fixture-access-")
        p.requests.append(req)
        return p.response_factory(req)
    upstream.set_client(httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    import server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        p.client = client
        yield p
    await upstream.close_client()
    config.update(lambda c: (c.clear(), c.update(before)))
    fake._install_channels(m, [])


def health_state(p):
    return {ch.key: (cooldown.get_state(ch.key, MODEL), scorer.get_stats(ch.key, MODEL)) for ch in p.channels}


async def call(p, ingress="chat", stream=False):
    return await p.client.post(PATHS[ingress], headers={"Authorization": "Bearer ccp-test"},
                               json=request_body(ingress, stream))


def latest(p):
    conn = p.m["log_db"]._get_conn()
    row = dict(conn.execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 1").fetchone())
    chain = [dict(r) for r in conn.execute(
        "SELECT * FROM retry_chain WHERE request_id=? ORDER BY attempt_order", (row["request_id"],))]
    return row, chain


def assert_rejection_logged_without_health_effects(p, before):
    row, chain = latest(p)
    assert row["status"] == "error" and row["http_status"] == 400
    assert len(chain) == 1 and chain[0]["outcome"] == "request_invalid"
    assert UNAPPROVED_CHANNEL_MESSAGE in row["error_message"]
    assert health_state(p) == before
    assert all(ch.enabled and not ch.disabled_reason for ch in p.channels)


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing-grace"])
async def test_http400_short_circuit_preserves_health_and_next_request_works(pool, ingress, stream, existing):
    p = pool
    if existing:
        # Setup only: main evidence always runs real HTTP requests/finalization.
        for ch in p.channels:
            cooldown.record_error(ch.key, MODEL, "earlier unrelated failure")
            scorer.record_failure(ch.key, MODEL, connect_ms=5)
    before = health_state(p)
    accounts_before = copy.deepcopy(config.get()["oauthAccounts"])
    p.response_factory = lambda req: httpx.Response(400, json=REJECTION)
    # More than default grace/ladder failure counts: none may accumulate.
    for _ in range(10):
        n = len(p.requests)
        response = await call(p, ingress, stream)
        assert response.status_code == 400 and "application/json" in response.headers["content-type"]
        error = response.json()["error"]
        assert error["code"] == "11128" and error["message"] == UNAPPROVED_CHANNEL_MESSAGE
        assert "fixture-private-detail" not in response.text
        assert len(p.requests) == n + 1
        assert_rejection_logged_without_health_effects(p, before)
    assert config.get()["oauthAccounts"] == accounts_before
    p.response_factory = lambda req: httpx.Response(200, stream=fake.ChunkedByteStream(frames()),
        headers={"content-type": "text/event-stream"})
    n = len(p.requests)
    response = await call(p, ingress, stream)
    assert response.status_code == 200 and len(p.requests) == n + 1
    assert "WorkBuddy" in response.text
    assert latest(p)[0]["status"] == "success"


@pytest.mark.parametrize("ingress", PATHS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("position", ["first", "fragmented", "after_content", "same_batch"])
async def test_sse_rejection_never_cools_or_fakes_success(pool, ingress, stream, position):
    p = pool
    error = ev(REJECTION)
    content = ev({"id": "wb-fixture", "choices": [{"index": 0, "delta": {"content": "partial-content"}}]})
    chunks = {"first": [error], "fragmented": [error[i:i+9] for i in range(0, len(error), 9)],
              "after_content": [content, error], "same_batch": [content + error]}[position]
    source = fake.TerminalThenHangByteStream(chunks)
    p.response_factory = lambda req: httpx.Response(200, stream=source, headers={"content-type": "text/event-stream"})
    before = health_state(p)
    response = await asyncio.wait_for(call(p, ingress, stream), 3)
    committed = stream and position in {"after_content", "same_batch"}
    assert response.status_code == (200 if committed else 400), response.text
    assert '11128' in response.text and UNAPPROVED_CHANNEL_MESSAGE in response.text
    assert "fixture-private-detail" not in response.text
    assert "response.completed" not in response.text and "message_stop" not in response.text
    assert '"finish_reason":"stop"' not in response.text and "[DONE]" not in response.text
    if committed:
        assert "partial-content" in response.text
        assert '"error"' in response.text
    assert len(p.requests) == 1 and source.closed.is_set()
    assert_rejection_logged_without_health_effects(p, before)
    p.response_factory = lambda req: httpx.Response(200, stream=fake.ChunkedByteStream(frames()),
        headers={"content-type": "text/event-stream"})
    response = await call(p, ingress, stream)
    assert response.status_code == 200 and "WorkBuddy" in response.text
    assert len(p.requests) == 2 and latest(p)[0]["status"] == "success"


@pytest.mark.parametrize("status,payload,outcome,cooled,downstream", [
    (400, {"code":11128,"msg":"different unknown policy"}, "http_error", True, 503),
    (400, {"code":11129,"msg":UNAPPROVED_CHANNEL_MESSAGE}, "http_error", True, 503),
    (400, {"code":11128}, "http_error", True, 503),
    (400, {"msg":UNAPPROVED_CHANNEL_MESSAGE}, "http_error", True, 503),
    (400, {"error":{"code":"model_not_found","message":"not supported by this account"}}, "http_error", True, 503),
    (401, REJECTION, "http_auth_error", False, 401),
    (403, REJECTION, "http_auth_error", False, 403),
    (429, REJECTION, "http_error", True, 429),
    (402, REJECTION, "http_error", True, 402),
    (404, REJECTION, "http_error", False, 404),
    (500, REJECTION, "http_error", True, 503),
])
async def test_unknown_and_authoritative_http_paths_are_preserved(pool, monkeypatch, status, payload, outcome, cooled, downstream):
    p = pool
    # 401 retains existing recovery routing, tested elsewhere; no real refresh here.
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    config.update(lambda c: c.update(oauthGraceCount=0))
    p.response_factory = lambda req: httpx.Response(status, json=payload)
    response = await call(p)
    assert response.status_code == downstream, response.text
    row, chain = latest(p)
    assert len(p.requests) == len(chain) == row["retry_count"] == 7
    assert {r["outcome"] for r in chain} == {outcome}
    assert sum(cooldown.is_blocked(ch.key, MODEL) for ch in p.channels) == (7 if cooled else 0)
    assert all(scorer.get_stats(ch.key, MODEL)["total_requests"] == 1 for ch in p.channels)


@pytest.mark.parametrize("transport", ["http400", "sse"])
async def test_other_provider_same_code_and_text_is_not_workbuddy(pool, transport):
    p = pool
    # Real API channels, not a WorkBuddy class with a forged identity.
    channels = [fake._make_openai_channel(f"other-{i}", "https://other.example", protocol="openai-chat",
                                         alias=MODEL, real=MODEL) for i in range(2)]
    fake._install_channels(p.m, channels)
    requests = []
    def wire(req):
        requests.append(req)
        if transport == "sse":
            payload = {"error": {"type": "invalid_request_error", "code": "11128",
                                 "message": UNAPPROVED_CHANNEL_MESSAGE}}
            return httpx.Response(200, stream=fake.ChunkedByteStream([ev(payload)]),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(400, json=REJECTION)
    await upstream.close_client()
    upstream.set_client(httpx.AsyncClient(transport=httpx.MockTransport(wire)))
    response = await call(p, stream=(transport == "sse"))
    assert response.status_code == 503 and len(requests) == 2
    assert {r["outcome"] for r in latest(p)[1]} == {
        "upstream_error_json" if transport == "sse" else "http_error"}
    assert all(cooldown.is_blocked(ch.key, MODEL) for ch in channels)


@pytest.mark.parametrize("code", [11128, "11128"])
@pytest.mark.parametrize("envelope", [False, True])
async def test_exact_error_envelopes(pool, code, envelope):
    p = pool
    payload = {"code":code, "message":UNAPPROVED_CHANNEL_MESSAGE}
    if envelope:
        payload = {"error":payload}
    p.response_factory = lambda req: httpx.Response(400, json=payload)
    response = await call(p)
    assert response.status_code == 400 and response.json()["error"]["code"] == "11128"
    assert len(p.requests) == 1


async def test_rejection_does_not_clear_expired_or_other_model_permanent_history(pool):
    p = pool
    for ch in p.channels:
        cooldown.record_error(ch.key, MODEL, "earlier failure", cooldown_until=int(time.time()*1000)-1)
        cooldown.record_error(ch.key, "deepseek-fixture", "operator freeze", cooldown_until=-1)
    before = p.m["state_db"].error_load_all()
    p.response_factory = lambda req: httpx.Response(400, json=REJECTION)
    response = await call(p)
    assert response.status_code == 400 and len(p.requests) == 1
    assert p.m["state_db"].error_load_all() == before


def test_decoder_does_not_infer_request_rejection_from_code_alone():
    for payload in [{"code":11128}, {"code":11128,"msg":"other error"},
                    {"code":11128.0,"msg":UNAPPROVED_CHANNEL_MESSAGE},
                    {"code":11128,"msg":UNAPPROVED_CHANNEL_MESSAGE+" untrusted suffix"}]:
        decoder = WorkBuddyStream()
        output = decoder.feed(ev(payload))
        assert b'"error"' in output and decoder.request_rejection is None
    decoder = WorkBuddyStream()
    output = decoder.feed(ev(REJECTION) + ev({"choices":[{"delta":{"content":"ignored"}}]}))
    assert decoder.request_rejection == ("11128", UNAPPROVED_CHANNEL_MESSAGE)
    assert b"ignored" not in output and decoder.feed(b"data: [DONE]\n\n") == b""
