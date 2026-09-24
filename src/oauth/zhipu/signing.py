"""ZCode V4 signing at raw HTTP response boundary, before business error mapping."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
from dataclasses import replace
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import common as c

SIGNATURE_HEADERS = frozenset({"x-client-ts", "x-client-version", "x-client-sig", "x-client-nonce",
                               "x-app-id", "x-client-pow", "x-client-sign-verified"})
VERIFY_REASONS = {"VERIFY_SIGNATURE_INVALID", "VERIFY_APIKEY_EXPIRED"}


def derive(secret, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"WD_CLIENT_SIGN_KDF_SALT", info=info.encode()).derive(secret.encode())


def credential(key):
    parts = key.split(".")
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise c.ZhipuError("signing", "invalid-config")
    return tuple(p.strip() for p in parts)


def origin(url):
    p = urlsplit(url)
    return p.scheme + "://" + p.netloc


def refreshable(status, data):
    if status != 401 or not isinstance(data, dict):
        return False
    values = [data.get("msg"), data.get("reason")]
    for name, field in (("data", "reason"), ("error", "reason"), ("error", "message")):
        if isinstance(data.get(name), dict):
            values.append(data[name].get(field))
    return any(isinstance(v, str) and v in VERIFY_REASONS for v in values)


async def proof_of_work(key_id, session, ts):
    seed = hashlib.sha256(f"{key_id}\nzcode\n{session}\n{ts}".encode()).hexdigest()[:32]
    prefix = secrets.token_hex(12)
    for counter in range(2**32):
        attempt = prefix + f"{counter:08x}"
        if hashlib.sha256(f"{seed}\n{attempt}".encode()).digest()[0] == 0:
            return attempt
        if counter % 256 == 0:
            await asyncio.sleep(0)  # Cancellation remains observable even in PoW.
    raise c.ZhipuError("signing", "cryptography")


class Signer:
    def __init__(self, key, provider_origin, account_key, *, allow_insecure=False):
        if urlsplit(provider_origin).scheme != "https" and not allow_insecure:
            raise c.ZhipuError("signing", "invalid-config")
        self.key, self.origin, self.account_key = key, provider_origin, account_key
        self.private_key = None
        self.epoch = 0
        self.disposed = False
        self.bypass = False
        self.gate_value = False
        self.gate_until = 0
        self.lock = threading.RLock()
        self.flights = {}

    def dispose(self):
        with self.lock:
            self.disposed = True
            self.invalidate()

    def invalidate(self):
        with self.lock:
            self.epoch += 1
            self.private_key = None

    def check(self):
        if self.disposed:
            raise c.ZhipuError("signing", "disposed")

    async def singleflight(self, name, operation):
        with self.lock:
            future = self.flights.get(name)
            leader = future is None
            if leader:
                future = concurrent.futures.Future()
                self.flights[name] = future
        if not leader:
            return await asyncio.shield(asyncio.wrap_future(future))
        try:
            result = await operation()
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                if self.flights.get(name) is future:
                    self.flights.pop(name, None)

    async def json_request(self, url, *, headers, body=None, timeout):
        # Management/signing side requests have their own URL-based proxy/noProxy
        # resolution; never reuse a business-origin proxy decision for the gate.
        from ... import network
        c.require_network()
        try:
            async with network.async_client(timeout=timeout, follow_redirects=False,
                                            proxy_purpose="oauth_zhipu", proxy_channel="oauth:" + self.account_key) as client:
                async with client.stream("GET" if body is None else "POST", url, headers=headers,
                                         **({"json": body} if body is not None else {})) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > c.MAX_BYTES:
                            raise c.ZhipuError("handshake", "protocol", fail_open=True)
                    if not 200 <= response.status_code < 300:
                        raise c.ZhipuError("handshake", "server", status=response.status_code, fail_open=True)
                    return json.loads(raw)
        except c.ZhipuError:
            raise
        except Exception:
            raise c.ZhipuError("handshake", "network", fail_open=True) from None

    async def gate(self):
        self.check()
        if self.gate_until > time.monotonic():
            return self.gate_value
        async def load():
            try:
                data = await self.json_request(c.PLATFORM_ORIGIN + "/api/v1/agent/configs",
                    headers={**c.identity_headers(), "x-api-key": self.key}, timeout=15)
                if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
                    return False
                gate = data["data"].get("codingPlanSignature")
                self.gate_value = isinstance(gate, dict) and gate.get("enable") is True
                self.gate_until = time.monotonic() + 3600
                return self.gate_value
            except c.ZhipuError as exc:
                if exc.kind == "disabled":
                    raise
                return False  # Unavailable is deliberately not cached.
        return await self.singleflight("gate", load)

    async def ensure_private_key(self):
        self.check()
        key_id, secret = credential(self.key)
        with self.lock:
            epoch = self.epoch
            if self.private_key is not None:
                return self.private_key
        async def load():
            nonce, ts = secrets.token_hex(16), str(int(time.time() * 1000))
            sig = base64.b64encode(hmac.digest(derive(secret, "getSignKey_hmac"), f"get_sign_key\n{key_id}\n{ts}\n{nonce}".encode(), "sha256")).decode()
            data = await self.json_request(self.origin + "/api/paas/c1f3a7e2/v2/client",
                headers={"Authorization": self.key, "Content-Type": "application/json"},
                body={"apiKey": self.key, "nonce": nonce, "ts": ts, "sig": sig}, timeout=10)
            buffers = []
            try:
                if not isinstance(data, dict) or data.get("code") != 200:
                    raise ValueError("handshake envelope")
                encrypted = base64.b64decode(data["data"]["privateCipher"], validate=True)
                aes = bytearray(derive(secret, "ed25519_priv")); buffers.append(aes)
                plain = bytearray(AESGCM(bytes(aes)).decrypt(encrypted[:12], encrypted[12:], key_id.encode())); buffers.append(plain)
                der = bytearray(base64.b64decode(plain, validate=True)); buffers.append(der)
                key = serialization.load_der_private_key(bytes(der), password=None)
                if not isinstance(key, Ed25519PrivateKey):
                    raise ValueError("wrong key type")
            except Exception:
                raise c.ZhipuError("handshake", "cryptography", fail_open=True) from None
            finally:
                for buf in buffers:
                    buf[:] = b"\0" * len(buf)
            self.check()
            with self.lock:
                if self.epoch != epoch:
                    raise c.ZhipuError("signing", "disposed")
                self.private_key = key
            return key
        return await self.singleflight(("handshake", epoch), load)

    async def headers(self, request):
        self.check()
        key_id, _secret = credential(self.key)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in SIGNATURE_HEADERS}
        lower = {k.lower(): v for k, v in headers.items()}
        session = lower.get("x-session-id")
        if not session:
            raise c.ZhipuError("signing", "invalid-config")
        key = await self.ensure_private_key()
        ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(16)
        version = lower.get("x-zcode-app-version") or c.VERSION
        try:
            pow_value = await proof_of_work(key_id, session, ts)
            sig = base64.b64encode(key.sign(f"{key_id}\n{ts}\n{version}\n{session}\n{nonce}".encode())).decode()
        except Exception:
            raise c.ZhipuError("signing", "cryptography") from None
        headers.update({"X-Client-Ts": ts, "X-Client-Version": version, "X-Client-Sig": sig,
                        "X-Client-Nonce": nonce, "X-App-Id": "zcode", "X-Client-Pow": pow_value})
        return headers

    def wrap(self, request, factory):
        return SignedContext(self, request, factory)


class SignedContext:
    def __init__(self, signer, request, factory):
        self.signer, self.request, self.factory = signer, request, factory
        self.ctx = None

    async def close(self, *args):
        ctx, self.ctx = self.ctx, None
        if ctx is not None:
            return await ctx.__aexit__(*args)

    async def __aenter__(self):
        signer, request = self.signer, self.request
        signer.check()
        enabled = origin(request.url) == signer.origin and not signer.bypass and await signer.gate()
        for attempt in range(3):
            signed = enabled and attempt < 2 and not signer.bypass
            headers = {k: v for k, v in request.headers.items() if k.lower() not in SIGNATURE_HEADERS}
            if signed:
                try:
                    headers = await signer.headers(request)
                except c.ZhipuError as exc:
                    if not exc.fail_open_eligible:
                        raise
                    signed = False
            # The original immutable body and attribution headers survive replay.
            self.ctx = self.factory(replace(request, headers=headers))
            try:
                response = await self.ctx.__aenter__()
                if not signed or response.status_code != 401:
                    return response
                raw = await response.aread()
                try:
                    rejected = refreshable(401, json.loads(raw))
                except (ValueError, UnicodeError):
                    rejected = False
                if not rejected:
                    return response
                await self.close(None, None, None)
                signer.invalidate()
                if attempt == 1:
                    signer.bypass = True
            except BaseException:
                await self.close(None, None, None)
                raise
        raise c.ZhipuError("signing", "invalid-state")

    async def __aexit__(self, *args):
        return await self.close(*args)


_cache = {}
_cache_lock = threading.RLock()


def for_account(account, account_key, *, network_revision=""):
    key = (c.fingerprint(account), c.MODEL_ORIGINS[c.site_of(account)], c.VERSION, network_revision)
    with _cache_lock:
        cached = _cache.get(account_key)
        if cached and cached[0] == key:
            return cached[1]
        if cached:
            cached[1].dispose()
        signer = Signer(account["model_key"], key[1], account_key)
        _cache[account_key] = key, signer
        return signer


def forget(account_key):
    with _cache_lock:
        cached = _cache.pop(account_key, None)
        if cached:
            cached[1].dispose()
