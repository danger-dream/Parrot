"""Locally generated macOS fields matching runtime-info's 1.1.2 layout."""
from __future__ import annotations

import re
import secrets
import string
import time
import uuid


def generate_fields() -> list[bytes]:
    now = str(int(time.time() * 1000))
    mac = "02:" + ":".join(secrets.token_hex(1) for _ in range(5))
    serial = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    fields = [b""] * 44
    values = {
        0: "1000", 1: now, 2: "0", 3: "1", 4: "4", 5: "1.1.2", 6: "MacOS",
        7: "Version 26.2 (Build 25C56)", 8: "runtime-info-darwin-arm64", 11: mac,
        19: "parrot-" + secrets.token_hex(6) + ".local", 20: serial,
        23: str(uuid.uuid4()).upper(), 24: "Mac16,10", 25: "Apple", 26: "1920*1080",
        27: str(256 * 1024**3), 28: str(16 * 1024**3), 34: now, 37: "arm64", 41: now,
    }
    for index, value in values.items():
        fields[index] = value.encode("ascii")
    validate_fields(fields)
    return fields


def validate_fields(fields: list[bytes]) -> None:
    fixed = {0: b"1000", 2: b"0", 3: b"1", 4: b"4", 5: b"1.1.2", 6: b"MacOS",
             8: b"runtime-info-darwin-arm64", 25: b"Apple", 37: b"arm64"}
    populated = {*fixed, 1, 7, 11, 19, 20, 23, 24, 26, 27, 28, 34, 41}
    if len(fields) != 44 or any(not isinstance(value, bytes) for value in fields):
        raise ValueError("Invalid macOS device fields")
    if any(fields[index] != value for index, value in fixed.items()):
        raise ValueError("Inconsistent macOS device protocol")
    if any(value for index, value in enumerate(fields) if index not in populated):
        raise ValueError("Unsupported macOS device field")
    if not re.fullmatch(rb"02(?::[0-9a-f]{2}){5}", fields[11]):
        raise ValueError("Invalid generated macOS MAC")
    if not re.fullmatch(rb"[A-Z0-9]{10}", fields[20]):
        raise ValueError("Invalid generated macOS serial")
    if not re.fullmatch(rb"parrot-[0-9a-f]{12}\.local", fields[19]):
        raise ValueError("Invalid generated macOS hostname")
    uuid.UUID(fields[23].decode("ascii"))
    if any(not fields[index].isdigit() or not 0 < int(fields[index]) <= 2**53
           for index in (1, 27, 28, 34, 41)):
        raise ValueError("Invalid generated macOS numeric field")
    if fields[7] != b"Version 26.2 (Build 25C56)" or fields[24] != b"Mac16,10" or fields[26] != b"1920*1080":
        raise ValueError("Unsupported generated macOS model")
