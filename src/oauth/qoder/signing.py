"""Qoder's COSY request envelope and its body codec, as its desktop client sends them."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import time
import uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives import padding as sym_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import common

# Qoder's embedded 1024-bit RSA key: it wraps the per-request AES key.
_RSA_KEY = serialization.load_pem_public_key(b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----""")

_STANDARD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_QODER = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!$"
_ENCODE = str.maketrans(_STANDARD, _QODER)
_DECODE = str.maketrans(_QODER, _STANDARD)


def _swap_thirds(value: str) -> str:
    third = len(value) // 3
    return value[-third:] + value[third:len(value) - third] + value[:third] if third else value


def encode_body(text: str) -> str:
    """Base64 through the client's alphabet ('$' pads), first and last thirds swapped."""
    return _swap_thirds(base64.b64encode(text.encode("utf-8")).decode("ascii").translate(_ENCODE))


def decode_body(wire: str) -> str:
    return base64.b64decode(_swap_thirds(wire).translate(_DECODE)).decode("utf-8")


def machine_os() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "i386": "x86", "i686": "x86", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine, machine)
    system = {"darwin": "darwin", "windows": "win32"}.get(platform.system().lower(), platform.system().lower())
    return f"{arch}_{system}"


def user_blob(account: dict) -> tuple[str, str]:
    """The account as the envelope carries it: AES-128-CBC under a fresh key that is its own IV."""
    raw = json.dumps({"uid": account.get("uid") or "", "aid": "", "name": account.get("nickname") or "",
                      "email": account.get("email") or "", "security_oauth_token": account.get("access_token") or ""},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = uuid.uuid4().hex[:16].encode("ascii")
    padder = sym_padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    info = encryptor.update(padder.update(raw) + padder.finalize()) + encryptor.finalize()
    wrapped = _RSA_KEY.encrypt(key, padding.PKCS1v15())
    return base64.b64encode(info).decode("ascii"), base64.b64encode(wrapped).decode("ascii")


def cosy_headers(url: str, account: dict, wire_body: str, *, device: dict, ts: int | None = None) -> dict[str, str]:
    """Sign one call whose body, in its wire form, is wire_body."""
    machine = common.text(account.get("machine_id"), "machine_id", required=True, maximum=64)
    uid = common.text(account.get("uid"), "uid", required=True)
    common.text(account.get("access_token"), "access_token", required=True, maximum=32768)
    if device.get("Cosy-MachineId") != machine or any(not device.get(k) for k in (
            "Cosy-MachineToken", "Cosy-MachineType", "Cosy-MachineCode", "Cosy-MachineOS")):
        raise common.QoderError("sign", kind="invalid_device_identity")
    ts = int(time.time()) if ts is None else ts
    info, key = user_blob(account)
    payload = base64.b64encode(json.dumps(
        {"version": "v1", "requestId": str(uuid.uuid4()), "info": info, "cosyVersion": common.COSY_VERSION, "ideVersion": ""},
        separators=(",", ":"),
    ).encode()).decode("ascii")
    path = urlparse(url).path
    if path.startswith("/algo"):
        path = path[5:]
    signature = hashlib.md5(f"{payload}\n{key}\n{ts}\n{wire_body}\n{path}".encode("utf-8")).hexdigest()
    return {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "Authorization": f"Bearer COSY.{payload}.{signature}",
        "Cosy-Business-Product": "app",
        "Cosy-Business-Type": "agent",
        "Cosy-ClientType": "0",
        "Cosy-Data-Policy": "DISAGREE",
        "Cosy-Date": str(ts),
        "Cosy-Key": key,
        **device,
        "Cosy-Scene": "app",
        "Cosy-User": uid,
        "Cosy-Version": common.COSY_VERSION,
        "Login-Version": "v2",
    }
