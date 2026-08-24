"""Shared Shadowsocks TCP framing, URL parsing, and connection factory.

Used by both SIP022 (SS2022) and SIP004 AEAD clients.  TCP client only.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import struct
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, unquote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

# ── constants ────────────────────────────────────────────────────

AEAD_OVERHEAD = 16
MAX_PAYLOAD_SIZE = 0x3FFF  # 16383

ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04


# ── errors ───────────────────────────────────────────────────────

class SSError(Exception):
    """Proxy-level Shadowsocks protocol / handshake error."""


class SS2022Error(SSError):
    """Raised for SS2022 handshake / protocol errors."""


class SSAEADError(SSError):
    """Raised for SIP004 AEAD protocol errors."""


# ── cipher registry ──────────────────────────────────────────────

@dataclass(frozen=True)
class CipherSpec:
    key_size: int
    make_aead: type
    salt_size: int = 0

    def __post_init__(self) -> None:
        if not self.salt_size:
            object.__setattr__(self, "salt_size", self.key_size)


SS2022_CIPHERS: dict[str, CipherSpec] = {
    "2022-blake3-aes-128-gcm": CipherSpec(16, AESGCM),
    "2022-blake3-aes-256-gcm": CipherSpec(32, AESGCM),
    "2022-blake3-chacha20-poly1305": CipherSpec(32, ChaCha20Poly1305),
}

AEAD_CIPHERS: dict[str, CipherSpec] = {
    "chacha20-ietf-poly1305": CipherSpec(32, ChaCha20Poly1305),
    "aes-256-gcm": CipherSpec(32, AESGCM),
    "aes-128-gcm": CipherSpec(16, AESGCM),
}

ALL_SS_CIPHERS: dict[str, CipherSpec] = {**SS2022_CIPHERS, **AEAD_CIPHERS}


def is_ss_aead_cipher(cipher: str) -> bool:
    return cipher in AEAD_CIPHERS


def is_supported_ss_cipher(cipher: str) -> bool:
    return cipher in ALL_SS_CIPHERS


def ss_family_label(cipher: str) -> str:
    return "SS AEAD" if is_ss_aead_cipher(cipher) else "SS2022"


# ── framing helpers ──────────────────────────────────────────────

def inc_nonce(n: bytearray) -> None:
    for i in range(len(n)):
        n[i] = (n[i] + 1) & 0xFF
        if n[i] != 0:
            return


def encode_addr(host: str, port: int) -> bytes:
    try:
        a = ipaddress.ip_address(host)
        if a.version == 4:
            return struct.pack("!B", ATYP_IPV4) + a.packed + struct.pack("!H", port)
        return struct.pack("!B", ATYP_IPV6) + a.packed + struct.pack("!H", port)
    except ValueError:
        enc = host.encode("idna")
        return struct.pack("!BB", ATYP_DOMAIN, len(enc)) + enc + struct.pack("!H", port)


class Writer:
    __slots__ = ("_w", "_aead", "_nonce", "_mps")

    def __init__(self, w, aead, nonce: bytearray, mps: int = MAX_PAYLOAD_SIZE):
        self._w = w
        self._aead = aead
        self._nonce = nonce
        self._mps = mps

    def _enc(self, pt: bytes) -> bytes:
        ct = self._aead.encrypt(bytes(self._nonce), pt, None)
        inc_nonce(self._nonce)
        return ct

    async def write(self, data: bytes) -> None:
        off = 0
        while off < len(data):
            sz = min(len(data) - off, self._mps)
            chunk = data[off:off + sz]
            self._w.write(self._enc(struct.pack("!H", sz)) + self._enc(chunk))
            await self._w.drain()
            off += sz


class Reader:
    __slots__ = ("_r", "_aead", "_nonce", "_buf")

    def __init__(self, r, aead, nonce: bytearray):
        self._r = r
        self._aead = aead
        self._nonce = nonce
        self._buf = b""

    def _dec(self, ct: bytes) -> bytes:
        pt = self._aead.decrypt(bytes(self._nonce), ct, None)
        inc_nonce(self._nonce)
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
        if self._buf:
            if n < 0:
                r, self._buf = self._buf, b""
            else:
                r, self._buf = self._buf[:n], self._buf[n:]
            return r
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


# ── URL parsing ──────────────────────────────────────────────────

def _try_b64_utf8(text: str) -> Optional[str]:
    padded = text + "=" * (-len(text) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded).decode("utf-8")
        except Exception:
            continue
    return None


def _decode_ss_userinfo(userinfo: str) -> tuple[str, str]:
    decoded = _try_b64_utf8(userinfo)
    if decoded is not None and ":" in decoded:
        method, password = decoded.split(":", 1)
        if method.strip() in ALL_SS_CIPHERS:
            return method.strip(), password
    raw = unquote(userinfo)
    if ":" not in raw:
        raise ValueError("userinfo must be method:password")
    method, password = raw.split(":", 1)
    return unquote(method).strip(), unquote(password)


def _parse_hostport(hostport: str) -> tuple[str, int]:
    if hostport.startswith("["):
        end = hostport.index("]")
        host = hostport[1:end]
        port = int(hostport[end + 2:])
        return host, port
    hp = hostport.rsplit(":", 1)
    if len(hp) != 2:
        raise ValueError("missing host:port")
    return hp[0], int(hp[1])


def parse_ss_url(url: str) -> dict:
    """Parse ``ss://`` URI into {cipher, password, server, port, name}.

    Supported formats:
      ss://<base64(method:password)>@host:port#name
      ss://<base64(method:password)>@host:port?plugin=...#name
      ss://method:password@host:port#name
      ss://<base64(method:password@host:port)>#name

    SIP003 plugins are rejected.  Cipher must be a supported SS2022 or
    SIP004 AEAD method.
    """
    s = url.strip()
    if not s.lower().startswith("ss://"):
        raise ValueError("not an ss:// URL")
    body = s[5:]
    name = ""
    if "#" in body:
        body, name = body.rsplit("#", 1)
        name = unquote(name).strip()
    if "?" in body:
        body, query = body.split("?", 1)
        plugin = (parse_qs(query, keep_blank_values=True).get("plugin") or [""])[0]
        if str(plugin).strip():
            raise ValueError("SIP003 plugins are not supported")

    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        cipher, password = _decode_ss_userinfo(userinfo)
    else:
        decoded = _try_b64_utf8(body)
        if decoded is None or "@" not in decoded:
            raise ValueError("missing @ in ss:// URL")
        userinfo, hostport = decoded.rsplit("@", 1)
        if ":" not in userinfo:
            raise ValueError("userinfo must be method:password")
        cipher, password = userinfo.split(":", 1)
        cipher = cipher.strip()

    host, port = _parse_hostport(hostport)
    if cipher not in ALL_SS_CIPHERS:
        raise ValueError(f"unsupported cipher: {cipher}")
    return {
        "cipher": cipher,
        "password": password,
        "server": host,
        "port": port,
        "name": name,
    }


def create_ss_connection(cipher: str, password: str, server: str, port: int,
                         timing=None):
    """Return an SS2022 or SIP004 AEAD TCP client for *cipher*."""
    if cipher in SS2022_CIPHERS:
        from .ss2022 import SS2022Connection
        return SS2022Connection(cipher, password, server, port, timing=timing)
    if cipher in AEAD_CIPHERS:
        from .ss_aead import SSAEADConnection
        return SSAEADConnection(cipher, password, server, port, timing=timing)
    raise ValueError(f"unsupported cipher: {cipher}")
