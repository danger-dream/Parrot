"""runtime-info block codec. No host samples or host identifiers.

Linux empty-state encodings stay byte-identical; macOS 1A carries new random state.
"""
from __future__ import annotations

import secrets
import struct

from .device_block_1a import transform as transform_1a

MAGIC = b"\xcf\xcd"
COMMANDS = (0x1A, 0x1B, 0x1C, 0x1D)
CONSTANTS = {
    0x1A: bytes.fromhex("976f6530463abf1cae105287d38524ce60cf9fea"),
    0x1C: bytes.fromhex("37b6a36e52cb5137a9652ad956d056afe86b47dd"),
}
KEY_B = struct.unpack("<4I", bytes.fromhex("2f7b991297bd4c89cb5ea6c4652f53e2"))
KEY_D = struct.unpack("<4I", bytes.fromhex("1d754b2d8ebaa59647dd524ba36ea9a5"))
MAX_SIZE = 65536


def tlv_pack(fields: list[bytes]) -> bytes:
    if any(not isinstance(v, bytes) or len(v) > 65535 for v in fields):
        raise ValueError("Invalid device field")
    return b"".join(struct.pack(">H", len(v)) + v for v in fields)


def tlv_unpack(raw: bytes) -> list[bytes]:
    fields, offset = [], 0
    while offset < len(raw):
        if offset + 2 > len(raw):
            raise ValueError("Truncated device field")
        size = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if offset + size > len(raw):
            raise ValueError("Truncated device field")
        fields.append(raw[offset:offset + size])
        offset += size
    return fields


def _transform(raw: bytes, command: int, *, inverse=False) -> bytes:
    result = bytearray()
    for offset in range(0, len(raw), 16):
        w = list(struct.unpack("<4I", raw[offset:offset + 16]))
        if command == 0x1B:
            if inverse:
                u = [w[i] ^ KEY_B[i] for i in range(4)]
                u = [u[3], u[0], u[1], u[2]]
                w = [(u[i] >> 17 | u[(i + 2) % 4] << 15) & 0xffffffff for i in range(4)]
            else:
                u = [(w[i] << 17 | w[(i + 2) % 4] >> 15) & 0xffffffff for i in range(4)]
                w = [u[(i + 1) % 4] ^ KEY_B[i] for i in range(4)]
        elif command == 0x1D:
            if inverse:
                w = [((w[i] >> 14 | w[(i + 2) % 4] << 18) & 0xffffffff) ^ KEY_D[i] for i in range(4)]
            else:
                u = [w[i] ^ KEY_D[i] for i in range(4)]
                w = [(u[i] << 14 | u[(i + 2) % 4] >> 18) & 0xffffffff for i in range(4)]
        else:
            raise ValueError("Unknown device command")
        result.extend(struct.pack("<4I", *w))
    return bytes(result)


def encode_block(command: int, fields: list[bytes], *, key: bytes | None = None) -> bytes:
    key = secrets.token_bytes(4) if key is None else key
    if len(key) != 4 or command not in COMMANDS:
        raise ValueError("Invalid device block")
    raw = tlv_pack(fields)
    if command == 0x1A and raw != b"\0" * 2:
        pad = 20 - len(raw) % 20
        transformed = transform_1a(raw + bytes([pad]) * pad)
    elif command in CONSTANTS:
        if raw != b"\0" * (2 if command == 0x1A else 10):
            raise ValueError("Unsupported constant device block")
        transformed = CONSTANTS[command]
    else:
        pad = 16 - len(raw) % 16
        transformed = _transform(raw + bytes([pad]) * pad, command)
    cipher = bytes(v ^ key[(i + 2) % 4] for i, v in enumerate(transformed))
    content = bytes([command]) + key + cipher
    return MAGIC + struct.pack(">I", len(content) + 6) + content


def decode_blocks(blob: bytes) -> list[list[bytes]]:
    if not isinstance(blob, bytes) or not 12 <= len(blob) <= MAX_SIZE:
        raise ValueError("Invalid device blocks length")
    result, offset = [], 0
    for command in COMMANDS:
        if offset + 11 > len(blob) or blob[offset:offset + 2] != MAGIC:
            raise ValueError("Invalid device block header")
        size = int.from_bytes(blob[offset + 2:offset + 6], "big")
        if size < 27 or offset + size > len(blob) or blob[offset + 6] != command:
            raise ValueError("Invalid device block length or order")
        key, cipher = blob[offset + 7:offset + 11], blob[offset + 11:offset + size]
        raw = bytes(v ^ key[(i + 2) % 4] for i, v in enumerate(cipher))
        if command == 0x1A and raw != CONSTANTS[command]:
            raw = transform_1a(raw, inverse=True)
            pad = raw[-1]
            if not 1 <= pad <= 20 or raw[-pad:] != bytes([pad]) * pad:
                raise ValueError("Invalid device block padding")
            raw = raw[:-pad]
        elif command in CONSTANTS:
            if raw != CONSTANTS[command]:
                raise ValueError("Invalid constant device block")
            raw = b"\0" * (2 if command == 0x1A else 10)
        else:
            if len(raw) % 16:
                raise ValueError("Invalid device block alignment")
            raw = _transform(raw, command, inverse=True)
            pad = raw[-1]
            if not 1 <= pad <= 16 or raw[-pad:] != bytes([pad]) * pad:
                raise ValueError("Invalid device block padding")
            raw = raw[:-pad]
        result.append(tlv_unpack(raw))
        offset += size
    fields = result[1]
    macos = len(fields) == 44 and fields[5:7] == [b"1.1.2", b"MacOS"]
    windows = len(fields) == 100 and fields[5:7] == [b"1.0.70.3226", b"Windows"]
    if offset != len(blob) or not (len(fields) == 43 or macos or windows):
        raise ValueError("Invalid device block fields or trailing data")
    return result
