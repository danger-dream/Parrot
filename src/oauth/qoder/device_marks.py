"""IDE MarkValueWithHashSeed: XXH64(seed=0), with a decimal detection mark.

No hardware is sampled by generated profiles. Use the official missing-detector
branch (90/0), not a claimed successful physical-machine detection.
"""
import struct

_MASK = (1 << 64) - 1
_P1, _P2 = 11400714785074694791, 14029467366897019727
_P3, _P4, _P5 = 1609587929392839161, 9650029242287828579, 2870177450012600261
VERSION = 2


def _rot(value, count):
    value &= _MASK
    return ((value << count) | (value >> (64 - count))) & _MASK


def _round(value, word):
    return (_rot(value + word * _P2, 31) * _P1) & _MASK


def xxhash64(raw: bytes) -> int:
    offset = 0
    if len(raw) >= 32:
        lanes = [(_P1 + _P2) & _MASK, _P2, 0, (-_P1) & _MASK]
        while offset + 32 <= len(raw):
            lanes = [_round(a, b) for a, b in zip(lanes, struct.unpack_from('<4Q', raw, offset))]
            offset += 32
        value = sum(_rot(v, n) for v, n in zip(lanes, (1, 7, 12, 18))) & _MASK
        for lane in lanes:
            value = ((value ^ _round(0, lane)) * _P1 + _P4) & _MASK
    else:
        value = _P5
    value = (value + len(raw)) & _MASK
    while offset + 8 <= len(raw):
        value = (_rot(value ^ _round(0, struct.unpack_from('<Q', raw, offset)[0]), 27) * _P1 + _P4) & _MASK
        offset += 8
    if offset + 4 <= len(raw):
        value = (_rot(value ^ (struct.unpack_from('<I', raw, offset)[0] * _P1), 23) * _P2 + _P3) & _MASK
        offset += 4
    for byte in raw[offset:]:
        value = (_rot(value ^ (byte * _P5), 11) * _P1) & _MASK
    value = ((value ^ (value >> 33)) * _P2) & _MASK
    value = ((value ^ (value >> 29)) * _P3) & _MASK
    return value ^ (value >> 32)


def mark(value: int, seed: str) -> str:
    digest = f'{xxhash64(seed.encode()):016x}'
    return digest[:8] + f'{value:02d}' + digest[8:]


def missing_detection(token: str) -> dict:
    machine_type = mark(90, token)
    return {'type': machine_type, 'code': mark(0, machine_type), 'format_version': VERSION}
