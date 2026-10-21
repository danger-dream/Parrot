"""Bounded runtime-info 1.5.0 transport codec using Parrot's cryptography dependency."""
from __future__ import annotations

import base64
import json
import secrets
import struct
import zlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .device_blocks import decode_blocks
from . import device_platforms

MASTER_KEY = b"650uWT4wglfzY43x"
POOL = bytes.fromhex(
    "19d6e5fee84e594a2e73d7e5142e781b910b39a740d014a57fab5c11cd5b8452"
    "a715ce2ce62a76bd3ec1d005d77c9af1c34904cf5b6765f340b31ccab937d149"
    "6d5cb19937517b4962172852e03947b5022a222ce3fc5a707b7509d62c70644b"
    "296df7e8dda507c39b6e644be53e088ee663f2f9a4dddc3b5d1110a6af76dcd6"
    "481031bd92b338b5bcda63eb449dd7aa9b2aecb65bcdede588956ac8e6468009"
    "2cc0f8c760da1b984a5eba711850d433")
MAX_RESPONSE = 65536
PATH = "/repPc.json?e=1000&pn=runtime-info-linux-x64&os=8&sv=1.5.0&pv=NA&pt=1"


def _crypt(raw, key, *, decrypt=False):
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    worker = cipher.decryptor() if decrypt else cipher.encryptor()
    return worker.update(raw) + worker.finalize()


def _key(index):
    if not 1 <= index <= 10:
        raise ValueError("Invalid UMID key index")
    return _crypt(POOL, MASTER_KEY, decrypt=True)[(index - 1) * 16:index * 16]


def build_payload(blocks: bytes) -> str:
    decode_blocks(blocks)
    seed = secrets.token_bytes(4)
    packet = b"\xcf\xcd\x00\x2b" + seed + struct.pack(">I", len(blocks) + 12)
    packet += bytes(v ^ seed[i % 4] for i, v in enumerate(blocks))
    padder = padding.PKCS7(128).padder()
    compressed = zlib.compress(packet, 6)
    index = secrets.randbelow(10) + 1
    encrypted = _crypt(padder.update(compressed) + padder.finalize(), _key(index))
    return base64.b64encode(bytes([index]) + encrypted).decode("ascii")


def _decompress(raw: bytes) -> bytes:
    worker = zlib.decompressobj()
    result = worker.decompress(raw, MAX_RESPONSE + 1)
    if len(result) > MAX_RESPONSE or not worker.eof or worker.unused_data or worker.unconsumed_tail:
        raise ValueError("Invalid UMID compressed response")
    return result


def parse_response(raw: bytes) -> str:
    if len(raw) > MAX_RESPONSE:
        raise ValueError("UMID response too large")
    envelope = json.loads(raw)
    if not isinstance(envelope, dict) or envelope.get("ec") != 200 or not isinstance(envelope.get("dt"), str):
        raise ValueError("UMID refused request")
    wire = base64.b64decode(envelope["dt"], validate=True)
    if len(wire) < 17 or (len(wire) - 1) % 16:
        raise ValueError("Invalid UMID response alignment")
    plain = _crypt(wire[1:], _key(wire[0]), decrypt=True)
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(plain) + unpadder.finalize()
    if not plain.lstrip().startswith(b"{"):
        plain = _decompress(plain)
    result = json.loads(plain)
    token = result.get("eeid") if isinstance(result, dict) else None
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("Invalid UMID ticket")
    return token


def request_bytes(host: str, body: str, *, process: str = "runtime-info-linux-x64") -> bytes:
    selected = device_platforms.for_process(process)
    path = f"/repPc.json?e={selected.get('environment', 1000)}&pn={process}&os={selected['os']}&sv={selected['sdk']}&pv=NA&pt=1"
    if selected["tls"]:
        return (f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
                "Content-Type: text/plain;charset=UTF-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}").encode("ascii")
    # Native wire has this unusual combined UA/content-type line. Preserve it.
    return (f"POST {path} HTTP/1.0\r\nHost: {host}\r\n"
            "Mozilla/5.0 (Windows; U; Windows NT 5.1; zh-CN; rv:1.9.2.3) "
            "Gecko/20100401 Firefox/3.6.3 SG/1.0.0"
            "Content-Type: text/plain;charset=UTF-8\r\n"
            f"Content-Length:{len(body)}\r\nConnection:close\r\n\r\n{body}").encode("ascii")
