"""The 160-bit 1A permutation shared by runtime-info 1.1.2 and 1.5.0."""
from __future__ import annotations

from functools import lru_cache
import struct

KEY = (0x396c669e, 0x1cb6334f, 0x8e5b19a7, 0xc72d8cd3, 0xe396c669)
MASK = 0xffffffff


def _forward(w):
    w0, w1, w2, w3, w4 = w
    c = (w0 << 7 | w3 >> 25) & MASK
    t = [(w1 << 7 | w4 >> 25) & MASK, (w2 << 7 | w0 >> 25) & MASK,
         (w3 << 7 | w1 >> 25) & MASK, (w4 << 7 | w2 >> 25) & MASK]
    a = (t[2] >> 3 | c << 29) & MASK
    r = [(t[0] << 29 | t[3] >> 3) & MASK, (t[1] << 29 | c >> 3) & MASK,
         (t[2] << 29 | t[0] >> 3) & MASK, (t[3] << 29 | t[1] >> 3) & MASK]
    s = [(r[0] << 19 | r[3] >> 13) & MASK, (r[1] << 19 | a >> 13) & MASK,
         (r[2] << 19 | r[0] >> 13) & MASK, (r[3] << 19 | r[1] >> 13) & MASK,
         (r[2] >> 13 | a << 19) & MASK]
    return [value ^ key for value, key in zip(s, KEY)]


@lru_cache(maxsize=1)
def _inverse_map():
    result = []
    for source in range(160):
        words = [0] * 5
        words[source // 32] = 1 << (source % 32)
        encoded = _forward(words)
        target = sum((value ^ key) << (32 * index) for index, (value, key) in enumerate(zip(encoded, KEY)))
        if target.bit_count() != 1:
            raise ValueError("Invalid 1A permutation")
        result.append(target.bit_length() - 1)
    if len(set(result)) != 160:
        raise ValueError("Invalid 1A inverse")
    return result


def transform(raw: bytes, *, inverse=False) -> bytes:
    if not raw or len(raw) % 20:
        raise ValueError("Invalid 1A alignment")
    result = bytearray()
    for offset in range(0, len(raw), 20):
        words = struct.unpack("<5I", raw[offset:offset + 20])
        if inverse:
            value = sum((word ^ key) << (32 * index) for index, (word, key) in enumerate(zip(words, KEY)))
            decoded = sum(((value >> target) & 1) << source for source, target in enumerate(_inverse_map()))
            result.extend(decoded.to_bytes(20, "little"))
        else:
            result.extend(struct.pack("<5I", *_forward(words)))
    return bytes(result)
