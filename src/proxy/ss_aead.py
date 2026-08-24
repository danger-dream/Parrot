"""Shadowsocks SIP004 AEAD TCP client.

Supports: chacha20-ietf-poly1305, aes-256-gcm, aes-128-gcm

Spec: https://shadowsocks.org/doc/aead.html
TCP client only (outbound connect proxy).  No UDP / SIP003 plugins.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .ss_common import (
    AEAD_CIPHERS,
    Reader,
    SSAEADError,
    Writer,
    encode_addr,
)


def evp_bytes_to_key(password: bytes, key_len: int) -> bytes:
    """OpenSSL EVP_BytesToKey(MD5, no salt) used by classic Shadowsocks."""
    out = b""
    prev = b""
    while len(out) < key_len:
        prev = hashlib.md5(prev + password).digest()
        out += prev
    return out[:key_len]


def derive_aead_subkey(master: bytes, salt: bytes, key_len: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA1(),
        length=key_len,
        salt=salt,
        info=b"ss-subkey",
    ).derive(master)


class SSAEADConnection:
    """Async SIP004 AEAD TCP tunnel. connect() establishes the encrypted channel."""

    __slots__ = (
        "_spec", "_master", "_shost", "_sport", "_timing",
        "_reader", "_writer", "_raw_r", "_raw_w", "_resp_ok",
    )

    def __init__(self, cipher: str, password: str, server: str, port: int,
                 timing=None):
        if cipher not in AEAD_CIPHERS:
            raise ValueError(f"unsupported cipher: {cipher}")
        self._spec = AEAD_CIPHERS[cipher]
        self._master = evp_bytes_to_key(
            str(password).encode("utf-8"), self._spec.key_size,
        )
        self._shost = server
        self._sport = port
        self._timing = timing
        self._reader: Optional[Reader] = None
        self._writer: Optional[Writer] = None
        self._raw_r: Optional[asyncio.StreamReader] = None
        self._raw_w: Optional[asyncio.StreamWriter] = None
        self._resp_ok = False

    def _session(self, salt: bytes):
        sk = derive_aead_subkey(self._master, salt, self._spec.key_size)
        return self._spec.make_aead(sk), bytearray(12)

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
            salt = os.urandom(self._spec.salt_size)
            aead, nonce = self._session(salt)
            rw.write(salt)
            writer = Writer(rw, aead, nonce)
            await writer.write(encode_addr(host, port) + initial_payload)
            self._writer = writer
            self._raw_r = rr
        except BaseException:
            rw.close()
            try:
                await rw.wait_closed()
            except Exception:
                pass
            raise

    async def _init_reader(self) -> None:
        if self._raw_r is None:
            raise SSAEADError("not connected")
        try:
            salt = await asyncio.wait_for(
                self._raw_r.readexactly(self._spec.salt_size), timeout=10)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError) as e:
            raise SSAEADError(f"no response from SS server: {e}") from e
        try:
            aead, nonce = self._session(salt)
        except Exception as e:
            raise SSAEADError(f"response key derive failed: {e}") from e
        self._reader = Reader(self._raw_r, aead, nonce)
        self._resp_ok = True

    async def write(self, data: bytes) -> None:
        if not self._writer:
            raise RuntimeError("not connected")
        await self._writer.write(data)

    async def read(self, n: int = -1) -> bytes:
        if not self._resp_ok:
            await self._init_reader()
        if self._reader is None:
            raise SSAEADError("response reader not initialized")
        return await self._reader.read(n)

    async def readall(self) -> bytes:
        if not self._resp_ok:
            await self._init_reader()
        if self._reader is None:
            raise SSAEADError("response reader not initialized")
        return await self._reader.readall()

    async def close(self) -> None:
        if self._raw_w:
            self._raw_w.close()
            try:
                await self._raw_w.wait_closed()
            except Exception:
                pass
