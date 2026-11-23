"""ZCode 3.19.1 ProviderEndpointRoutingService: schema, security, LKG and sign-before-route."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src import config, oauth_manager as om
from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
from src.oauth.zhipu import common, endpoint_routing as er, runtime, signing
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_zhipu_provider import credential
from src.tests.zhipu_pat_fixtures import pat
from src.transports.http import HttpStreamRequest, open_stream


def test_config_headers_exact_ysa_subset_and_sanitization():
    source = {
        **common.identity_headers(),
        "X-Device-Mid": "  dev-uuid-1  ",
        "Authorization": "Bearer secret",
        "x-api-key": "secret.key",
        "X-Extra": "drop-me",
    }
    headers = er.build_config_headers("3.19.1", source, api_key_id="  key-123  ")
    assert set(headers.keys()) == set(er.SOURCE_HEADER_MAP.values()) | {"x-api-key-id", "Accept"}
    assert headers["User-Agent"] == "ZCode/3.19.1"
    assert headers["X-ZCode-App-Version"] == "3.19.1"
    assert headers["X-Device-Mid"] == "dev-uuid-1"
    assert headers["x-api-key-id"] == "key-123"
    assert headers["Accept"] == "application/json"
    assert "X-ZCode-Agent" not in headers
    assert "Authorization" not in headers and "x-api-key" not in headers

    omitted = er.build_config_headers("3.19.1", common.identity_headers(), api_key_id="bad\nkey")
    assert "x-api-key-id" not in omitted
    assert "X-Device-Mid" not in omitted


def test_parse_mapping_valid_ultra_and_normalization():
    body = json.dumps({
        "code": 0,
        "extraRoot": True,
        "data": {
            "otherField": 123,
            "proxyEndpoint": {
                "mapping": [
                    {
                        "from": "https://Open.BigModel.CN:443/api/anthropic/v1/messages/",
                        "to": "https://zcode.z.ai/api/v1/ultra/",
                        "zcodeAuthorizationHeader": "  x-zcode-authorization  ",
                    },
                    {
                        "from": "https://api.z.ai/api/anthropic/v1/messages",
                        "to": "https://zcode.z.ai/api/v1/ultra-zai/v1/messages",
                        "zcodeAuthorizationHeader": "X-ZCode-Authorization",
                    },
                    {
                        "from": "https://open.bigmodel.cn/api/other",
                        "to": "https://proxy.example.com/custom",
                        "zcodeAuthorizationHeader": "   ",
                    },
                ],
            },
        },
    }).encode()
    mapping = er.parse_routing_mapping(body, "https://zcode.z.ai")
    assert mapping == {
        "https://open.bigmodel.cn:443/api/anthropic/v1/messages": (True, "https://zcode.z.ai/api/v1/ultra"),
        "https://api.z.ai:443/api/anthropic/v1/messages": (True, "https://zcode.z.ai/api/v1/ultra-zai/v1/messages"),
        "https://open.bigmodel.cn:443/api/other": (False, "https://proxy.example.com/custom"),
    }
    assert er.parse_routing_mapping(b'{"code":0,"data":{"feature":true}}', "https://zcode.z.ai") == {}


@pytest.mark.parametrize("payload", [
    b"not-json",
    b'{"code":200,"data":{}}',
    b'{"code":"0","data":{}}',
    b'{"code":false,"data":{}}',
    b'{"code":0,"data":{"proxyEndpoint":null}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[],"extra":1}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://b.test/p","extra":1}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"http://a.test/p","to":"https://b.test/p"}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://user@a.test/p","to":"https://b.test/p"}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p?q=1","to":"https://b.test/p"}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://b.test/p#h"}]}}}',
    # Unsafe authorization header name or non-ultra target rejects entire snapshot.
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://zcode.z.ai/api/v1/ultra","zcodeAuthorizationHeader":"Authorization"}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://evil.test/api/v1/ultra","zcodeAuthorizationHeader":"X-ZCode-Authorization"}]}}}',
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://zcode.z.ai/api/v1/ultraviolet","zcodeAuthorizationHeader":"X-ZCode-Authorization"}]}}}',
    # Duplicate normalized from rejects entire snapshot.
    b'{"code":0,"data":{"proxyEndpoint":{"mapping":[{"from":"https://a.test/p","to":"https://b.test/1"},{"from":"https://A.TEST:443/p/","to":"https://b.test/2"}]}}}',
])
def test_parse_mapping_rejects_invalid_or_unsafe_snapshots(payload):
    with pytest.raises(ValueError):
        er.parse_routing_mapping(payload, "https://zcode.z.ai")


@pytest.mark.parametrize("label", ["from", "to"])
@pytest.mark.parametrize("suffix", [
    "../oauth/token", "%2e%2e/oauth/token", ".%2E/oauth/token", "%2E./oauth/token",
    "%2e%2e%2foauth/token", "%2F..%2Foauth/token", "..\\oauth\\token", "%2e%2e%5Coauth/token",
    "./anthropic/v1/messages", "%2e/anthropic/v1/messages",
])
def test_mapping_rejects_ambiguous_path_segments(label, suffix):
    entry = {"from": "https://open.bigmodel.cn/api/anthropic/v1/messages",
             "to": "https://zcode.z.ai/api/v1/ultra/anthropic/v1/messages",
             "zcodeAuthorizationHeader": "X-ZCode-Authorization"}
    entry[label] = "https://zcode.z.ai/api/v1/ultra/" + suffix
    raw = json.dumps({"code": 0, "data": {"proxyEndpoint": {"mapping": [entry]}}}).encode()
    with pytest.raises(ValueError, match=f"agent_configs_unsafe_{label}_url"):
        er.parse_routing_mapping(raw, "https://zcode.z.ai")


@pytest.mark.parametrize("cached", [False, True])
async def test_ambiguous_mapping_never_dispatches_identity_outside_allowed_path(monkeypatch, cached):
    source = "https://open.bigmodel.cn/api/anthropic/v1/messages"
    target = "https://zcode.z.ai/api/v1/ultra/anthropic/v1/messages"
    router = er.EndpointRouter(account_key="fixture", platform_origin="https://zcode.z.ai", app_version=common.VERSION)
    if cached:
        router.snapshot = (0, {"https://open.bigmodel.cn:443/api/anthropic/v1/messages": (True, target)})
    snapshot = router.snapshot
    async def fetch():
        return json.dumps({"code": 0, "data": {"proxyEndpoint": {"mapping": [{
            "from": source, "to": "https://zcode.z.ai/api/v1/ultra/../oauth/token",
            "zcodeAuthorizationHeader": "X-ZCode-Authorization",
        }]}}}).encode()
    monkeypatch.setattr(router, "_fetch_raw", fetch)
    route = await router.resolve(source, zcode_token="fixture-platform")
    request = httpx.Request("POST", route.url, headers=route.headers)
    assert router.snapshot is snapshot and router.retry_after > 0
    if cached:
        assert route.routed and str(request.url) == target
        assert request.headers["x-zcode-authorization"] == "Bearer fixture-platform"
    else:
        assert not route.routed and str(request.url) == source
        assert "x-zcode-authorization" not in request.headers


def test_parse_mapping_rejects_over_256_entries():
    items = [{"from": f"https://a.test/{i}", "to": f"https://b.test/{i}"} for i in range(257)]
    with pytest.raises(ValueError):
        er.parse_routing_mapping(json.dumps({"code": 0, "data": {"proxyEndpoint": {"mapping": items}}}).encode(), "https://zcode.z.ai")


async def test_exact_match_query_preservation_jwt_optional_ttl_cooldown_lkg_and_singleflight(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(er.time, "monotonic", lambda: clock[0])
    router = er.EndpointRouter(
        account_key="acc-1",
        platform_origin="https://zcode.z.ai",
        app_version="3.19.1",
        device_id="dev-1",
        api_key_id="kid-1",
    )
    replies = [
        json.dumps({"code": 0, "data": {"proxyEndpoint": {"mapping": [
            {"from": "https://open.bigmodel.cn/api/anthropic/v1/messages",
             "to": "https://zcode.z.ai/api/v1/ultra",
             "zcodeAuthorizationHeader": "X-ZCode-Authorization"},
            {"from": "https://open.bigmodel.cn/api/anthropic/v1",
             "to": "https://zcode.z.ai/api/v1/ultra-prefix-must-not-match"},
        ]}}}).encode(),
        RuntimeError("upstream down"),
        json.dumps({"code": 0, "data": {"proxyEndpoint": {"mapping": []}}}).encode(),
    ]
    calls = 0
    gate = asyncio.Event()

    async def fake_fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        item = replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(router, "_fetch_raw", fake_fetch)

    # Concurrent calls coalesce into a single in-flight fetch.
    t1 = asyncio.create_task(router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages?beta=1", zcode_token="jwt-1"))
    t2 = asyncio.create_task(router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="Bearer jwt-2"))
    await asyncio.sleep(0)
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert calls == 1
    assert r1 == er.RouteDecision(True, "https://zcode.z.ai/api/v1/ultra?beta=1", {"X-ZCode-Authorization": "Bearer jwt-1"})
    assert r2 == er.RouteDecision(True, "https://zcode.z.ai/api/v1/ultra", {"X-ZCode-Authorization": "Bearer jwt-2"})

    # Missing JWT still rewrites URL without injecting header.
    r_no_jwt = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="")
    assert r_no_jwt == er.RouteDecision(True, "https://zcode.z.ai/api/v1/ultra", {})

    # Prefix rule must NOT match a deeper path.
    r_prefix = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/models", zcode_token="jwt-1")
    assert r_prefix == er.RouteDecision(False, "https://open.bigmodel.cn/api/anthropic/v1/models", {})
    assert calls == 1  # Still within 300s TTL.

    # After 300s TTL, refresh fails -> 30s cooldown AND retains LKG snapshot.
    clock[0] += 301.0
    r_lkg = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="jwt-3")
    assert calls == 2
    assert r_lkg == er.RouteDecision(True, "https://zcode.z.ai/api/v1/ultra", {"X-ZCode-Authorization": "Bearer jwt-3"})

    # Within 30s cooldown, no fetch attempt; LKG continues serving.
    clock[0] += 10.0
    r_cool = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="jwt-3")
    assert calls == 2 and r_cool.routed is True

    # After 30s cooldown, refresh succeeds with empty mapping -> stops routing.
    clock[0] += 25.0
    r_after = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="jwt-3")
    assert calls == 3
    assert r_after == er.RouteDecision(False, "https://open.bigmodel.cn/api/anthropic/v1/messages", {})


def test_account_isolation_and_forget():
    er.clear_all()
    r1 = er.for_account("acc-a", api_key_id="kid-a", device_id="dev-a", network_revision="rev1")
    r2 = er.for_account("acc-b", api_key_id="kid-b", device_id="dev-b", network_revision="rev1")
    assert r1 is not r2
    assert er.for_account("acc-a", api_key_id="kid-a", device_id="dev-a", network_revision="rev1") is r1
    # Changing api_key_id rebuilds the account's router.
    r1_new = er.for_account("acc-a", api_key_id="kid-a2", device_id="dev-a", network_revision="rev1")
    assert r1_new is not r1
    runtime.forget("acc-a")
    assert er.for_account("acc-a", api_key_id="kid-a2", device_id="dev-a", network_revision="rev1") is not r1_new
    er.clear_all()


async def test_loopback_sign_before_route_verify_replay_and_team_headers(monkeypatch):
    private = Ed25519PrivateKey.generate()
    pat_token = pat("key-ultra")
    state = {"configs": [], "handshakes": [], "ultra": [], "reject": 2, "errors": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def reply(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            try:
                assert self.path == "/api/v1/agent/configs"
                lower = {k.lower(): v for k, v in self.headers.items()}
                state["configs"].append(lower)
                assert lower["user-agent"] == "ZCode/3.19.1"
                assert lower["x-zcode-app-version"] == "3.19.1"
                assert lower["x-device-mid"] == "dev-loopback-1"
                assert lower["x-api-key-id"] == "key-ultra"
                assert lower["accept"] == "application/json"
                assert "authorization" not in lower and "x-api-key" not in lower and "x-zcode-agent" not in lower
                self.reply(200, {"code": 0, "data": {"proxyEndpoint": {"mapping": [{
                    "from": f"{base}/api/anthropic/v1/messages",
                    "to": f"{base}/api/v1/ultra",
                    "zcodeAuthorizationHeader": "X-ZCode-Authorization",
                }]}}})
            except BaseException as exc:
                state["errors"].append(exc)
                self.reply(500, {})
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
            try:
                if self.path == "/api/paas/c1f3a7e2/v2/client":
                    state["handshakes"].append(dict(self.headers))
                    key_id, secret = signing.credential(pat_token)
                    assert body["accessToken"] == pat_token
                    expected = hmac.digest(signing.derive(secret, "getSignKey_hmac"),
                        f"get_sign_key\n{key_id}\n{body['ts']}\n{body['nonce']}".encode(), "sha256")
                    assert hmac.compare_digest(base64.b64decode(body["sig"]), expected)
                    der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                    iv = b"012345678901"
                    enc = AESGCM(signing.derive(secret, "ed25519_priv")).encrypt(iv, base64.b64encode(der), key_id.encode())
                    self.reply(200, {"code": 200, "data": {"privateCipher": base64.b64encode(iv + enc).decode()}})
                    return
                assert self.path == "/api/v1/ultra"
                lower = {k.lower(): v for k, v in self.headers.items()}
                state["ultra"].append(lower)
                assert lower["x-zcode-authorization"] == "Bearer platform-jwt"
                assert lower["authorization"] == "Bearer " + pat_token
                assert lower["x-api-key"] == pat_token
                assert lower["bigmodel-organization"] == "org-1"
                assert lower["bigmodel-project"] == "proj-1"
                assert lower["bigmodel-target-type"] == "TEAM"
                if "x-client-sig" in lower:
                    ts, nonce, sid = lower["x-client-ts"], lower["x-client-nonce"], lower["x-session-id"]
                    private.public_key().verify(base64.b64decode(lower["x-client-sig"]),
                        f"key-ultra\n{ts}\n3.19.1\n{sid}\n{nonce}".encode())
                if state["reject"] > 0:
                    state["reject"] -= 1
                    self.reply(401, {"error": {"reason": "VERIFY_SIGNATURE_INVALID"}})
                else:
                    self.reply(200, {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]})
            except BaseException as exc:
                state["errors"].append(exc)
                self.reply(500, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(common, "require_network", lambda: None)
    try:
        router = er.EndpointRouter(
            account_key="team-acc",
            platform_origin=base,
            app_version="3.19.1",
            device_id="dev-loopback-1",
            api_key_id="key-ultra",
            allow_insecure=True,
        )
        logical_url = f"{base}/api/anthropic/v1/messages"
        decision = await router.resolve(logical_url, zcode_token="platform-jwt")
        assert decision.routed is True
        assert decision.url == f"{base}/api/v1/ultra"
        base_headers = {
            **common.model_headers(),
            "Host": "open.bigmodel.cn",
            "x-api-key": pat_token,
            "Authorization": "Bearer " + pat_token,
            "x-session-id": "sess-loopback",
            "Bigmodel-Target-Type": "TEAM",
            "bigmodel-organization": "org-1",
            "bigmodel-project": "proj-1",
        }
        routed_headers = er.apply_routed_headers(base_headers, decision.headers)
        assert "Host" not in routed_headers
        req = HttpStreamRequest("POST", decision.url, routed_headers, b'{"messages":[]}', 2, 5)
        signer = signing.Signer(pat_token, base, "team-acc", allow_insecure=True)
        async with httpx.AsyncClient(trust_env=False) as client:
            async with signer.wrap(req, lambda r: open_stream(client, r), signing_url=logical_url) as resp:
                assert resp.status_code == 200
        assert len(state["configs"]) == 1
        assert len(state["handshakes"]) == 2
        assert len(state["ultra"]) == 3
        assert "x-client-sig" in state["ultra"][0]
        assert "x-client-sig" in state["ultra"][1]
        assert "x-client-sig" not in state["ultra"][2]
        assert not state["errors"], state["errors"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def test_channel_build_upstream_request_applies_routing_and_signs_logical_origin(monkeypatch):
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    a = credential("oauth", plan_scope="team", organization_id="org-t", project_id="proj-t",
                   zcode_token="jwt-channel", project_key_id="kid-channel",
                   models=["GLM-5.4"], account_model_catalog={"models": [{"id": "glm-5.4", "reasoningEfforts": ["low", "high", "max"]}]})
    config.update(lambda c: c.update(oauthAccounts=[a], channels=[], network={"routing": {"default": "direct"}}))
    ch = ZhipuOAuthChannel(a)
    pat_token = pat("kid-channel")
    from src.oauth.zhipu import project_tokens
    monkeypatch.setattr(om, "ensure_channel_token", lambda _ch: asyncio.sleep(0, result=pat_token))
    monkeypatch.setattr(project_tokens, "cached_token", lambda _acc: pat_token)
    monkeypatch.setattr(er.c, "require_network", lambda: None)

    async def fake_resolve(self, url, *, zcode_token=""):
        assert url == "https://open.bigmodel.cn/api/anthropic/v1/messages"
        assert self.api_key_id == "kid-channel"
        assert self.device_id == om.get_account(ch.account_key)["zcode_device_id"]
        assert zcode_token == "jwt-channel"
        return er.RouteDecision(True, "https://zcode.z.ai/api/v1/ultra", {"X-ZCode-Authorization": "Bearer jwt-channel"})

    monkeypatch.setattr(er.EndpointRouter, "resolve", fake_resolve)
    try:
        upstream = await ch.build_upstream_request({"model": "glm-5.4", "messages": [{"role": "user", "content": "hi"}]}, "glm-5.4")
        assert upstream.url == "https://zcode.z.ai/api/v1/ultra"
        assert upstream.headers["X-ZCode-Authorization"] == "Bearer jwt-channel"
        assert upstream.headers["bigmodel-organization"] == "org-t"
        assert upstream.headers["bigmodel-project"] == "proj-t"
        assert "X-Device-Mid" not in upstream.headers

        # Verify signing checks logical_url (open.bigmodel.cn), NOT rewritten zcode.z.ai.
        signed_called = []
        async def fake_headers(self, request):
            signed_called.append(request.url)
            return {**request.headers, "X-Client-Sig": "sig-ok"}
        monkeypatch.setattr(signing.Signer, "headers", fake_headers)

        seen_req = []
        class DummyCtx:
            def __init__(self, r):
                seen_req.append(r)
            async def __aenter__(self):
                return httpx.Response(200, json={"ok": True})
            async def __aexit__(self, *args):
                return None

        req = HttpStreamRequest("POST", upstream.url, upstream.headers, upstream.body, 2, 5)
        async with upstream.stream_context_hook(req, DummyCtx) as resp:
            assert resp.status_code == 200
        assert signed_called == ["https://zcode.z.ai/api/v1/ultra"]
        assert seen_req[0].headers["X-Client-Sig"] == "sig-ok"
        assert seen_req[0].headers["X-ZCode-Authorization"] == "Bearer jwt-channel"
    finally:
        runtime.forget(ch.account_key)
        config.update(lambda c: (c.clear(), c.update(before)))


@pytest.mark.parametrize("mode", ["redirect", "oversized"])
async def test_loopback_rejects_redirect_and_oversized_configs_fail_open(monkeypatch, mode):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def do_GET(self):
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"{base}/other")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b" " * (er.MAX_RESPONSE_BYTES + 128)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(common, "require_network", lambda: None)
    try:
        router = er.EndpointRouter(
            account_key="fail-open-acc",
            platform_origin=base,
            app_version="3.19.1",
            allow_insecure=True,
        )
        decision = await router.resolve(f"{base}/api/anthropic/v1/messages", zcode_token="jwt")
        assert decision == er.RouteDecision(False, f"{base}/api/anthropic/v1/messages", {})
        assert router.snapshot is None and router.retry_after > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def test_singleflight_leader_cancellation_does_not_cancel_waiter(monkeypatch):
    router = er.EndpointRouter(
        account_key="cancel-acc",
        platform_origin="https://zcode.z.ai",
        app_version="3.19.1",
    )
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def slow_fetch():
        entered.set()
        await blocker.wait()
        return b'{"code":0,"data":{}}'

    monkeypatch.setattr(router, "_fetch_raw", slow_fetch)
    leader = asyncio.create_task(router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="jwt"))
    await entered.wait()
    waiter = asyncio.create_task(router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages", zcode_token="jwt"))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    # Unrelated waiter must complete fail-open instead of receiving leader's CancelledError.
    result = await waiter
    assert result == er.RouteDecision(False, "https://open.bigmodel.cn/api/anthropic/v1/messages", {})
    assert router.flight is None


def test_public_safety_redacts_zcode_authorization_header_variants():
    from src.management_control.public_safety import redact_known_fields
    redacted = redact_known_fields({
        "X-ZCode-Authorization": "Bearer secret-jwt-1",
        "X-Zcode-Authorization": "Bearer secret-jwt-2",
        "x-zcode-authorization": "Bearer secret-jwt-3",
        "x-bigmodel-authorization": "secret-biz",
        "x-zcode-query-source": "main_turn",
    })
    assert redacted["X-ZCode-Authorization"] == "[REDACTED]"
    assert redacted["X-Zcode-Authorization"] == "[REDACTED]"
    assert redacted["x-zcode-authorization"] == "[REDACTED]"
    assert redacted["x-bigmodel-authorization"] == "[REDACTED]"
    assert redacted["x-zcode-query-source"] == "main_turn"


async def test_api_key_mode_omits_x_api_key_id_on_endpoint_routing(monkeypatch):
    m = fake._import_modules()
    fake._setup(m)
    before = copy.deepcopy(config.get())
    a = credential("api_key", model_key="handfilled.secret",
                   models=["GLM-5.4"], account_model_catalog={"models": [{"id": "glm-5.4"}]})
    config.update(lambda c: c.update(oauthAccounts=[a], channels=[], network={"routing": {"default": "direct"}}))
    ch = ZhipuOAuthChannel(a)
    seen_key_ids = []

    async def fake_resolve(self, url, *, zcode_token=""):
        seen_key_ids.append((self.api_key_id, zcode_token))
        return er.RouteDecision(False, url, {})

    monkeypatch.setattr(er.EndpointRouter, "resolve", fake_resolve)
    try:
        await ch.build_upstream_request({"model": "glm-5.4", "messages": [{"role": "user", "content": "hi"}]}, "glm-5.4")
        assert seen_key_ids == [(None, "")]
    finally:
        runtime.forget(ch.account_key)
        config.update(lambda c: (c.clear(), c.update(before)))



