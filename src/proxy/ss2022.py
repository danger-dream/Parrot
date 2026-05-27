"""Shadowsocks 2022 (AEAD-2022) TCP client – async connect proxy.

Supports: 2022-blake3-aes-128-gcm, 2022-blake3-aes-256-gcm,
          2022-blake3-chacha20-poly1305

Reference impl: github.com/metacubex/sing-shadowsocks2
Spec: github.com/Shadowsocks-NET/shadowsocks-specs

TCP client only (outbound connect proxy).  No EIH / UDP.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import random
import struct
import time
from dataclasses import dataclass
from typing import Optional

import blake3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

# ── constants ────────────────────────────────────────────────────

HEADER_TYPE_CLIENT = 0
HEADER_TYPE_SERVER = 1
MAX_PADDING_LENGTH = 900
AEAD_OVERHEAD = 16
MAX_PAYLOAD_SIZE = 0x3FFF  # 16383

ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04

# ── cipher registry ──────────────────────────────────────────────

@dataclass(frozen=True)
class CipherSpec:
    key_size: int
    make_aead: type

CIPHERS: dict[str, CipherSpec] = {
    "2022-blake3-aes-128-gcm": CipherSpec(16, AESGCM),
    "2022-blake3-aes-256-gcm": CipherSpec(32, AESGCM),
    "2022-blake3-chacha20-poly1305": CipherSpec(32, ChaCha20Poly1305),
}

# ── helpers ──────────────────────────────────────────────────────

def _derive_session_key(psk: bytes, salt: bytes, key_len: int) -> bytes:
    h = blake3.blake3(psk + salt, derive_key_context="shadowsocks 2022 session subkey")
    return h.digest(length=key_len)


def _inc_nonce(n: bytearray) -> None:
    for i in range(len(n)):
        n[i] = (n[i] + 1) & 0xFF
        if n[i] != 0:
            return


def _decode_key(text: str) -> bytes:
    raw = str(text or "").strip()
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return base64.b64decode(padded)


def _encode_addr(host: str, port: int) -> bytes:
    try:
        a = ipaddress.ip_address(host)
        if a.version == 4:
            return struct.pack("!B", ATYP_IPV4) + a.packed + struct.pack("!H", port)
        return struct.pack("!B", ATYP_IPV6) + a.packed + struct.pack("!H", port)
    except ValueError:
        enc = host.encode("idna")
        return struct.pack("!BB", ATYP_DOMAIN, len(enc)) + enc + struct.pack("!H", port)


def parse_ss_url(url: str) -> dict:
    """Parse ``ss://`` URI into {cipher, password, server, port, name}.

    Supported formats:
      ss://<base64(method:password)>@host:port#name
      ss://<base64(method:password)>@host:port?plugin=...#name
    """
    from urllib.parse import unquote, urlparse
    s = url.strip()
    if not s.lower().startswith("ss://"):
        raise ValueError("not an ss:// URL")
    # Some URLs have base64 userinfo that may contain '=' padding; urlparse
    # chokes if the fragment contains special chars, so extract fragment first.
    body = s[5:]
    name = ""
    if "#" in body:
        body, name = body.rsplit("#", 1)
        name = unquote(name).strip()
    # Drop query params (plugin opts etc.)
    if "?" in body:
        body = body.split("?", 1)[0]
    # body = base64(method:pass)@host:port   OR   base64_of_entire_userinfo@host:port
    if "@" not in body:
        raise ValueError("missing @ in ss:// URL")
    userinfo, hostport = body.rsplit("@", 1)

    # Decode userinfo
    # Pad base64 if needed
    padded = userinfo + "=" * (-len(userinfo) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        decoded = base64.b64decode(padded).decode("utf-8")
    if ":" not in decoded:
        raise ValueError("userinfo must be method:password")
    cipher, password = decoded.split(":", 1)

    # Parse host:port
    if hostport.startswith("["):
        # IPv6
        end = hostport.index("]")
        host = hostport[1:end]
        port = int(hostport[end + 2:])  # skip ]:
    else:
        hp = hostport.rsplit(":", 1)
        host = hp[0]
        port = int(hp[1])

    return {"cipher": cipher, "password": password, "server": host, "port": port, "name": name}


# ── AEAD stream writer / reader ──────────────────────────────────

class _Writer:
    __slots__ = ("_w", "_aead", "_nonce", "_mps")

    def __init__(self, w: asyncio.StreamWriter, aead, nonce: bytearray,
                 mps: int = MAX_PAYLOAD_SIZE):
        self._w = w
        self._aead = aead
        self._nonce = nonce
        self._mps = mps

    def _enc(self, pt: bytes) -> bytes:
        ct = self._aead.encrypt(bytes(self._nonce), pt, None)
        _inc_nonce(self._nonce)
        return ct

    async def write(self, data: bytes) -> None:
        off = 0
        while off < len(data):
            sz = min(len(data) - off, self._mps)
            chunk = data[off:off + sz]
            self._w.write(self._enc(struct.pack("!H", sz)) + self._enc(chunk))
            await self._w.drain()
            off += sz


class _Reader:
    __slots__ = ("_r", "_aead", "_nonce", "_buf")

    def __init__(self, r: asyncio.StreamReader, aead, nonce: bytearray):
        self._r = r
        self._aead = aead
        self._nonce = nonce
        self._buf = b""

    def _dec(self, ct: bytes) -> bytes:
        pt = self._aead.decrypt(bytes(self._nonce), ct, None)
        _inc_nonce(self._nonce)
        return pt

    async def _chunk(self) -> bytes:
        lc = await self._r.readexactly(2 + AEAD_OVERHEAD)
        plen = struct.unpack("!H", self._dec(lc))[0]
        return self._dec(await self._r.readexactly(plen + AEAD_OVERHEAD))

    async def read(self, n: int = -1) -> bytes:
        """Read up to *n* bytes.  Returns as soon as any data is available
        (like socket.recv), not waiting to fill the full *n*."""
        if n == 0:
            return b""
        # If buffer has data, return immediately (partial is fine)
        if self._buf:
            if n < 0:
                r, self._buf = self._buf, b""
            else:
                r, self._buf = self._buf[:n], self._buf[n:]
            return r
        # Buffer empty: read exactly one encrypted chunk, then return
        try:
            self._buf = await self._chunk()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        if n < 0:
            r, self._buf = self._buf, b""
        else:
            r, self._buf = self._buf[:n], self._buf[n:]
        return r

    async def readall(self) -> bytes:
        parts = []
        if self._buf:
            parts.append(self._buf)
            self._buf = b""
        while True:
            try:
                c = await self._chunk()
                if not c:
                    break
                parts.append(c)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                break
        return b"".join(parts)


# ── SS2022 connection ────────────────────────────────────────────

class SS2022Error(Exception):
    """Raised for SS2022 handshake / protocol errors (→ proxy-level fault)."""


class SS2022Connection:
    """Async SS2022 TCP tunnel. connect() establishes the encrypted channel."""

    __slots__ = (
        "_spec", "_psk", "_shost", "_sport",
        "_reader", "_writer", "_raw_r", "_raw_w", "_salt", "_resp_ok",
    )

    def __init__(self, cipher: str, password: str, server: str, port: int):
        if cipher not in CIPHERS:
            raise ValueError(f"unsupported cipher: {cipher}")
        self._spec = CIPHERS[cipher]
        self._psk = _decode_key(password)
        if len(self._psk) != self._spec.key_size:
            raise ValueError(f"key length {len(self._psk)} != {self._spec.key_size}")
        self._shost = server
        self._sport = port
        self._reader: Optional[_Reader] = None
        self._writer: Optional[_Writer] = None
        self._raw_r: Optional[asyncio.StreamReader] = None
        self._raw_w: Optional[asyncio.StreamWriter] = None
        self._salt: Optional[bytes] = None
        self._resp_ok = False

    async def connect(self, host: str, port: int, *,
                      initial_payload: bytes = b"",
                      timeout: float = 8.0) -> None:
        rr, rw = await asyncio.wait_for(
            asyncio.open_connection(self._shost, self._sport), timeout=timeout)
        self._raw_w = rw
        try:
            ks = self._spec.key_size
            salt = os.urandom(ks)
            self._salt = salt
            sk = _derive_session_key(self._psk, salt, ks)
            aead = self._spec.make_aead(sk)
            nonce = bytearray(12)

            def enc(pt: bytes) -> bytes:
                ct = aead.encrypt(bytes(nonce), pt, None)
                _inc_nonce(nonce)
                return ct

            addr = _encode_addr(host, port)
            pad_len = random.randint(1, MAX_PADDING_LENGTH) if len(initial_payload) < MAX_PADDING_LENGTH else 0
            var = addr + struct.pack("!H", pad_len) + os.urandom(pad_len) + initial_payload
            fixed = struct.pack("!BQH", HEADER_TYPE_CLIENT, int(time.time()), len(var))

            rw.write(salt + enc(fixed) + enc(var))
            await rw.drain()
            self._writer = _Writer(rw, aead, nonce)

            self._raw_r = rr
        except Exception:
            rw.close()
            raise

    async def _parse_resp(self) -> None:
        if self._raw_r is None:
            raise SS2022Error("not connected")
        raw_r = self._raw_r
        ks = self._spec.key_size
        try:
            salt = await asyncio.wait_for(raw_r.readexactly(ks), timeout=10)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError) as e:
            raise SS2022Error(f"no response from SS server: {e}") from e

        sk = _derive_session_key(self._psk, salt, ks)
        aead = self._spec.make_aead(sk)
        nonce = bytearray(12)

        def dec(ct: bytes) -> bytes:
            pt = aead.decrypt(bytes(nonce), ct, None)
            _inc_nonce(nonce)
            return pt

        flen = 1 + 8 + ks + 2
        try:
            fpt = dec(await raw_r.readexactly(flen + AEAD_OVERHEAD))
        except Exception as e:
            raise SS2022Error(f"response header decrypt failed: {e}") from e

        if fpt[0] != HEADER_TYPE_SERVER:
            raise SS2022Error(f"bad header type {fpt[0]}")
        ts = struct.unpack("!Q", fpt[1:9])[0]
        if abs(int(time.time()) - ts) > 30:
            raise SS2022Error(f"timestamp drift {abs(int(time.time()) - ts)}s")
        if fpt[9:9 + ks] != self._salt:
            raise SS2022Error("salt echo mismatch")

        vlen = struct.unpack("!H", fpt[9 + ks:11 + ks])[0]
        init = b""
        if vlen > 0:
            init = dec(await raw_r.readexactly(vlen + AEAD_OVERHEAD))

        self._reader = _Reader(raw_r, aead, nonce)
        if init:
            self._reader._buf = init
        self._resp_ok = True

    async def write(self, data: bytes) -> None:
        if not self._writer:
            raise RuntimeError("not connected")
        await self._writer.write(data)

    async def read(self, n: int = -1) -> bytes:
        if not self._resp_ok:
            await self._parse_resp()
        if self._reader is None:
            raise SS2022Error("response reader not initialized")
        return await self._reader.read(n)

    async def readall(self) -> bytes:
        if not self._resp_ok:
            await self._parse_resp()
        if self._reader is None:
            raise SS2022Error("response reader not initialized")
        return await self._reader.readall()

    async def close(self) -> None:
        if self._raw_w:
            self._raw_w.close()
            try:
                await self._raw_w.wait_closed()
            except Exception:
                pass
