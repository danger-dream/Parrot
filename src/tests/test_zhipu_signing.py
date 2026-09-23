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
    state = {"gate": True, "reject": 0, "status": 200, "handshakes": 0, "gates": 0, "requests": [], "errors": [], "delay": 0}
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
                assert self.headers["User-Agent"] == "ZCode/3.14.3"
                self.reply(200, {"code": 0, "data": {"codingPlanSignature": {"enable": state["gate"]}}})
            except BaseException as exc:
                state["errors"].append(exc); self.reply(500, {})
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw)
            try:
                if self.path == "/api/paas/c1f3a7e2/v2/client":
                    state["handshakes"] += 1
                    assert self.headers["Authorization"] == "fixture.secret"
                    expected = hmac.digest(signing.derive("secret", "getSignKey_hmac"), f"get_sign_key\nfixture\n{body['ts']}\n{body['nonce']}".encode(), "sha256")
                    assert hmac.compare_digest(base64.b64decode(body["sig"]), expected)
                    der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                    iv = b"012345678901"
                    encrypted = AESGCM(signing.derive("secret", "ed25519_priv")).encrypt(iv, base64.b64encode(der), b"fixture")
                    self.reply(200, {"code": 200, "data": {"privateCipher": base64.b64encode(iv + encrypted).decode()}})
                    return
                headers = dict(self.headers)
                state["requests"].append((raw, {k.lower(): v for k,v in headers.items()}))
                if self.headers.get("X-Client-Sig"):
                    ts, nonce, sid = (self.headers[k] for k in ("X-Client-Ts", "X-Client-Nonce", "X-Session-Id"))
                    private.public_key().verify(base64.b64decode(self.headers["X-Client-Sig"]), f"fixture\n{ts}\n3.14.3\n{sid}\n{nonce}".encode())
                    seed = hashlib.sha256(f"fixture\nzcode\n{sid}\n{ts}".encode()).hexdigest()[:32]
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
        "x-request-id": "request-fixture", "x-zcode-trace-id": "trace-fixture", "X-ZCode-App-Version": "3.14.3"}, b'{"messages":[]}', 2, 5)


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
        if enabled:
            with pytest.raises(common.ZhipuError, match="invalid-config"):
                async with context:
                    pytest.fail("must not send")
            assert not state["requests"]
        else:
            async with context as response:
                assert response.status_code == 200
            assert len(state["requests"]) == 1


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
    assert len(state["requests"]) == 8 and state["handshakes"] == 1 and state["gates"] == 1


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
