"""Shadowsocks 2022 (AEAD-2022) TCP client – async connect proxy.

Supports: 2022-blake3-aes-128-gcm, 2022-blake3-aes-256-gcm,
          2022-blake3-chacha20-poly1305

Also re-exports SIP004 AEAD helpers used by the shared SS connector:
          chacha20-ietf-poly1305, aes-256-gcm, aes-128-gcm

Reference impl: github.com/metacubex/sing-shadowsocks2
Spec: github.com/Shadowsocks-NET/shadowsocks-specs

TCP client only (outbound connect proxy).  No EIH / UDP.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import struct
import time
from typing import Optional

import blake3

from .ss_common import (
    AEAD_OVERHEAD,
    SS2022_CIPHERS,
    CipherSpec,
    Reader,
    SS2022Error,
    SSAEADError,
    SSError,
    Writer,
    create_ss_connection,
    encode_addr,
    inc_nonce,
    is_ss_aead_cipher,
    is_supported_ss_cipher,
    parse_ss_url,
    ss_family_label,
)

# ── constants ────────────────────────────────────────────────────

HEADER_TYPE_CLIENT = 0
HEADER_TYPE_SERVER = 1
MAX_PADDING_LENGTH = 900

CIPHERS: dict[str, CipherSpec] = SS2022_CIPHERS


# ── helpers ──────────────────────────────────────────────────────

def _derive_session_key(psk: bytes, salt: bytes, key_len: int) -> bytes:
    h = blake3.blake3(psk + salt, derive_key_context="shadowsocks 2022 session subkey")
    return h.digest(length=key_len)


def _decode_key(text: str) -> bytes:
    raw = str(text or "").strip()
    padded = raw + "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return base64.b64decode(padded)


# ── SS2022 connection ────────────────────────────────────────────

class SS2022Connection:
    """Async SS2022 TCP tunnel. connect() establishes the encrypted channel."""

    __slots__ = (
        "_spec", "_psk", "_shost", "_sport", "_timing",
        "_reader", "_writer", "_raw_r", "_raw_w", "_salt", "_resp_ok",
    )

    def __init__(self, cipher: str, password: str, server: str, port: int,
                 timing=None):
        if cipher not in CIPHERS:
            raise ValueError(f"unsupported cipher: {cipher}")
        self._spec = CIPHERS[cipher]
        self._psk = _decode_key(password)
        if len(self._psk) != self._spec.key_size:
            raise ValueError(f"key length {len(self._psk)} != {self._spec.key_size}")
        self._shost = server
        self._sport = port
        self._timing = timing
        self._reader: Optional[Reader] = None
        self._writer: Optional[Writer] = None
        self._raw_r: Optional[asyncio.StreamReader] = None
        self._raw_w: Optional[asyncio.StreamWriter] = None
        self._salt: Optional[bytes] = None
        self._resp_ok = False

    async def connect(self, host: str, port: int, *,
                      initial_payload: bytes = b"",
                      timeout: float = 8.0) -> None:
        tcp_started = time.monotonic()
        try:
            rr, rw = await asyncio.wait_for(
                asyncio.open_connection(self._shost, self._sport), timeout=timeout)
        finally:
            if self._timing is not None:
                self._timing.record_proxy_tcp(tcp_started, time.monotonic())
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
                inc_nonce(nonce)
                return ct

            addr = encode_addr(host, port)
            pad_len = random.randint(1, MAX_PADDING_LENGTH) if len(initial_payload) < MAX_PADDING_LENGTH else 0
            var = addr + struct.pack("!H", pad_len) + os.urandom(pad_len) + initial_payload
            fixed = struct.pack("!BQH", HEADER_TYPE_CLIENT, int(time.time()), len(var))

            rw.write(salt + enc(fixed) + enc(var))
            await rw.drain()
            self._writer = Writer(rw, aead, nonce)

            self._raw_r = rr
        except BaseException:
            rw.close()
            try:
                await rw.wait_closed()
            except Exception:
                # Preserve the connect/handshake cause; the writer is already
                # closing and no bridge owner exists yet.
                pass
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
            inc_nonce(nonce)
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

        self._reader = Reader(raw_r, aead, nonce)
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


__all__ = [
    "CIPHERS",
    "SS2022Connection",
    "SS2022Error",
    "SSAEADError",
    "SSError",
    "create_ss_connection",
    "is_ss_aead_cipher",
    "is_supported_ss_cipher",
    "parse_ss_url",
    "ss_family_label",
]
