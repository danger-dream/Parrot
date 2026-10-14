"""Real loopback HTTP: gate, HMAC/HKDF, AES-GCM, Ed25519, PoW and raw replay."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.oauth.zhipu import signing, common
from src.transports.http import HttpStreamRequest, open_stream


@pytest.fixture
def local_upstream(monkeypatch):
    private = Ed25519PrivateKey.generate()
    state = {"gate": True, "key": "fixture.secret", "reject": 0, "status": 200, "handshakes": 0, "gates": 0, "requests": [], "errors": [], "delay": 0}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def reply(self, code, data, content_type="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw))); self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_GET(self):
            state["gates"] += 1
            try:
                assert self.path == "/api/v1/agent/configs"
                assert self.headers["x-api-key"] in {"fixture.secret", "single-key"}
                assert self.headers["Authorization"] is None
                assert self.headers["User-Agent"] == "ZCode/3.15.3"
                self.reply(200, {"code": 0, "data": {"codingPlanSignature": {"enable": state["gate"]}}})
            except BaseException as exc:
                state["errors"].append(exc); self.reply(500, {})
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
            try:
                if self.path == "/api/paas/c1f3a7e2/v2/client":
                    state["handshakes"] += 1
                    assert self.headers["Authorization"] == state["key"]
                    key_id, secret = signing.credential(state["key"])
                    assert key_id == state.get("expected_key_id", "fixture")
                    field = "accessToken" if len(state["key"].split(".")) == 3 else "apiKey"
                    assert body[field] == state["key"] and ("apiKey" if field == "accessToken" else "accessToken") not in body
                    expected = hmac.digest(signing.derive(secret, "getSignKey_hmac"), f"get_sign_key\n{key_id}\n{body['ts']}\n{body['nonce']}".encode(), "sha256")
                    assert hmac.compare_digest(base64.b64decode(body["sig"]), expected)
                    der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                    iv = b"012345678901"
                    encrypted = AESGCM(signing.derive(secret, "ed25519_priv")).encrypt(iv, base64.b64encode(der), key_id.encode())
                    self.reply(200, {"code": 200, "data": {"privateCipher": base64.b64encode(iv + encrypted).decode()}})
                    return
                headers = dict(self.headers)
                state["requests"].append((raw, {k.lower(): v for k,v in headers.items()}))
                if self.headers.get("X-Client-Sig"):
                    key_id, _ = signing.credential(state["key"])
                    ts, nonce, sid = (self.headers[k] for k in ("X-Client-Ts", "X-Client-Nonce", "X-Session-Id"))
                    private.public_key().verify(base64.b64decode(self.headers["X-Client-Sig"]), f"{key_id}\n{ts}\n3.15.3\n{sid}\n{nonce}".encode())
                    seed = hashlib.sha256(f"{key_id}\nzcode\n{sid}\n{ts}".encode()).hexdigest()[:32]
                    assert hashlib.sha256((seed + "\n" + self.headers["X-Client-Pow"]).encode()).digest()[0] == 0
                if state["delay"]:
                    time.sleep(state["delay"])
                if state["reject"]:
                    state["reject"] -= 1
                    self.reply(401, {"error": {"reason": "VERIFY_SIGNATURE_INVALID"}})
                else:
                    from src.tests import test_protocol_fake_upstreams as fake
                    response = fake._anthropic_sse_response("loopback") if body.get("stream") else fake._anthropic_response("loopback")
                    self.reply(state["status"], response.content, response.headers["content-type"])
            except BaseException as exc:
                state["errors"].append(exc); self.reply(500, {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(common, "PLATFORM_ORIGIN", base)
    monkeypatch.setattr(common, "require_network", lambda: None)
    yield base, state
    server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert not state["errors"], state["errors"]


def request(base):
    return HttpStreamRequest("POST", base + "/api/anthropic/v1/messages", {"x-api-key": "fixture.secret", "x-session-id": "session-fixture",
        "x-request-id": "request-fixture", "x-zcode-trace-id": "trace-fixture", "X-ZCode-App-Version": "3.15.3"}, b'{"messages":[]}', 2, 5)


@pytest.mark.parametrize("rejections,expected", [(0,1),(1,2),(2,3)])
async def test_real_crypto_raw_verify_replay(local_upstream, rejections, expected):
    base, state = local_upstream
    state["reject"] = rejections
    signer = signing.Signer("fixture.secret", base, "fixture", allow_insecure=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
            assert response.status_code == 200
            assert b"loopback" in await response.aread()
    assert len(state["requests"]) == expected
    assert state["handshakes"] == min(expected, 2)
    assert len({row[0] for row in state["requests"]}) == 1
    for field in ("x-request-id", "x-zcode-trace-id", "x-session-id"):
        assert len({row[1][field] for row in state["requests"]}) == 1
    if rejections == 2:
        assert "x-client-sig" not in state["requests"][-1][1] and signer.bypass


@pytest.mark.parametrize("enabled", [True,False])
async def test_single_key_gate_fail_closed(local_upstream, enabled):
    base, state = local_upstream
    state["gate"] = enabled
    signer = signing.Signer("single-key", base, "fixture", allow_insecure=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        context = signer.wrap(request(base), lambda req: open_stream(client, req))
        with pytest.raises(common.ZhipuError, match="invalid-config"):
            async with context:
                pytest.fail("must not send")
        assert not state["requests"] and state["gates"] == 0


async def test_no_403_or_cancel_replay(local_upstream):
    base, state = local_upstream
    signer = signing.Signer("fixture.secret", base, "fixture", allow_insecure=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        state["status"] = 403
        async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
            assert response.status_code == 403
        assert len(state["requests"]) == 1
        state["delay"] = .5
        async def run():
            async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
                await response.aread()
        task = asyncio.create_task(run())
        while len(state["requests"]) < 2:
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.05)
        assert len(state["requests"]) == 2


async def test_concurrent_gate_handshake_coalescing(local_upstream):
    base, state = local_upstream
    signer = signing.Signer("fixture.secret", base, "fixture", allow_insecure=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        async def send():
            async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
                await response.aread()
        await asyncio.gather(*(send() for _ in range(8)))
    assert len(state["requests"]) == 8 and state["handshakes"] == 1 and state["gates"] == 0


async def test_crypto_failure_failclosed_handshake_failure_open(local_upstream, monkeypatch):
    base, state = local_upstream
    signer = signing.Signer("fixture.secret", base, "fixture", allow_insecure=True)
    async def broken(*a):
        raise ValueError("secret must never escape")
    async with httpx.AsyncClient(trust_env=False) as client:
        monkeypatch.setattr(signing, "proof_of_work", broken)
        with pytest.raises(common.ZhipuError, match="cryptography") as exc:
            async with signer.wrap(request(base), lambda req: open_stream(client, req)):
                pass
        assert "secret must" not in str(exc.value) and not state["requests"]
        async def handshake():
            raise common.ZhipuError("handshake", "network", fail_open=True)
        monkeypatch.setattr(signer, "ensure_private_key", handshake)
        async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
            assert response.status_code == 200
        assert len(state["requests"]) == 1 and "x-client-sig" not in state["requests"][0][1]


@pytest.mark.parametrize("key_id", ["fixture", "  fixture  "])
async def test_pat_real_handshake_uses_entire_token_and_access_token_field(local_upstream, key_id):
    from src.tests.zhipu_pat_fixtures import pat
    base, state = local_upstream
    state["key"] = pat(key_id)
    state["expected_key_id"] = key_id
    signer = signing.Signer(state["key"], base, "pat-fixture", allow_insecure=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        async with signer.wrap(request(base), lambda req: open_stream(client, req)) as response:
            assert response.status_code == 200
    assert state["handshakes"] == 1 and state["gates"] == 0


@pytest.mark.parametrize("header,payload", [
    ({"alg":"none"}, {}), ({"typ":"not-jwt"}, {}), ({"sign_type":"OTHER"}, {}),
    ({}, {"token_use":"other"}), ({}, {"api_key":123}), ({}, {"api_key":None}),
    ({}, {"api_key":True}), ({}, {"api_key":[]}), ({}, {"api_key":""}), ({}, {"api_key":" \t\r\n"})])
def test_pat_parser_invalid_configuration_never_legacy_fallback(header, payload):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    value = ".".join((part({"alg":"HS256","typ":"JWT","sign_type":"SIGN", **header}),
        part({"token_use":"project_access","api_key":"id", **payload}), "sig"))
    with pytest.raises(common.ZhipuError, match="invalid-config"):
        signing.credential(value)


def test_pat_empty_signature_fails_closed():
    from src.tests.zhipu_pat_fixtures import pat
    with pytest.raises(common.ZhipuError, match="invalid-config"):
        signing.credential(pat(tag=""))


def test_pat_preserves_nonempty_api_key_and_entire_token():
    from src.tests.zhipu_pat_fixtures import pat
    key_id = " \tfixture\r\n "
    value = pat(key_id)
    assert signing.credential(value) == (key_id, value)
    # The signature segment is opaque to FSr; require nonempty, not JWT verification.
    whitespace_signature = pat(key_id, tag=" ")
    assert signing.credential(whitespace_signature) == (key_id, whitespace_signature)


@pytest.mark.parametrize("segment", [0, 1])
@pytest.mark.parametrize("invalid", ["!", "#", "\x00", "\v", "\u00a0", "é"])
def test_pat_invalid_base64_characters_fail_closed(segment, invalid):
    from src.tests.zhipu_pat_fixtures import pat
    parts = pat().split(".")
    parts[segment] = parts[segment][:8] + invalid + parts[segment][8:]
    with pytest.raises(common.ZhipuError, match="invalid-config"):
        signing.credential(".".join(parts))


@pytest.mark.parametrize("segment", [0, 1])
@pytest.mark.parametrize("variant", ["unpadded", "padded", "ascii_whitespace", "bad_padding", "excess_padding"])
def test_pat_atob_padding_and_whitespace_rules(segment, variant):
    from src.tests.zhipu_pat_fixtures import pat
    parts = pat().split(".")
    raw = base64.urlsafe_b64decode(parts[segment] + "=" * (-len(parts[segment]) % 4))
    # Make a valid JSON document whose base64 needs exactly two '=' characters.
    raw += b" " * ((1 - len(raw)) % 3)
    encoded = base64.urlsafe_b64encode(raw).decode()
    assert encoded.endswith("==")
    parts[segment] = {
        "unpadded": encoded[:-2],
        "padded": encoded,
        "ascii_whitespace": encoded[:4] + " \t\n\r\f" + encoded[4:],
        "bad_padding": encoded[:-1],
        "excess_padding": encoded + "====",
    }[variant]
    token = ".".join(parts)
    if variant in {"bad_padding", "excess_padding"}:
        with pytest.raises(common.ZhipuError, match="invalid-config"):
            signing.credential(token)
    else:
        assert signing.credential(token) == ("fixture", token)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_pat_decoder_uses_textdecoder_utf8_not_json_encoding_autodetection(encoding):
    from src.tests.zhipu_pat_fixtures import pat
    parts = pat().split(".")
    payload = json.dumps({"token_use":"project_access", "api_key":"fixture"})
    parts[1] = base64.urlsafe_b64encode(payload.encode(encoding)).decode().rstrip("=")
    token = ".".join(parts)
    if encoding in {"utf-16", "utf-32"}:
        with pytest.raises(common.ZhipuError, match="invalid-config"):
            signing.credential(token)
    else:
        assert signing.credential(token) == ("fixture", token)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_pat_decoder_rejects_non_json_constants(constant):
    from src.tests.zhipu_pat_fixtures import pat
    parts = pat().split(".")
    payload = '{"token_use":"project_access","api_key":"fixture","extra":' + constant + '}'
    parts[1] = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    with pytest.raises(common.ZhipuError, match="invalid-config"):
        signing.credential(".".join(parts))


def test_pat_decoder_matches_textdecoder_invalid_utf8_replacement():
    from src.tests.zhipu_pat_fixtures import pat
    parts = pat().split(".")
    parts[1] = base64.urlsafe_b64encode(b'{"token_use":"project_access","api_key":"id\xff"}').decode().rstrip("=")
    token = ".".join(parts)
    assert signing.credential(token) == ("id\ufffd", token)


@pytest.mark.parametrize("value", ["fixture.secret", " \tfixture . secret\r\n "])
def test_legacy_two_part_credential_trimming_is_unchanged(value):
    assert signing.credential(value) == ("fixture", "secret")
