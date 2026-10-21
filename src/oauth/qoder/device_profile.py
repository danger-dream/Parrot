"""Versioned, locally generated account device profiles; tickets live separately."""
from __future__ import annotations

import base64
import copy
import hashlib
import re
import secrets
import time
import uuid

from .device_blocks import COMMANDS, decode_blocks, encode_block
from . import device_platforms, device_macos, device_windows

VERSION = "linux-generated-v2"
LEGACY_VERSION = "linux-generated-v1"
UMID_ROUTE = "runtime-info-1.5.0-default-v1"
MACOS_VERSION = "macos-generated-v1"
MACOS_UMID_ROUTE = "runtime-info-1.1.2-macos-default-v1"
WINDOWS_VERSION = "windows-generated-v1"
WINDOWS_UMID_ROUTE = "runtime-info-1.0.0-windows-default-v1"
CURRENT_VERSIONS = (VERSION, MACOS_VERSION, WINDOWS_VERSION)


def auxiliary(command: int) -> list[bytes]:
    return [b""] if command == 0x1A else [b"", b"####", b""] if command == 0x1D else [b""] * 5


def _generate_macos(machine_id: str | None, platform: str) -> dict:
    fields = device_macos.generate_fields()
    auxiliary_fields = {0x1A: [b"", secrets.token_hex(16).encode("ascii")],
                        0x1C: [b""] * 5, 0x1D: [b"", b"{}", b""]}
    blocks = b"".join(encode_block(cmd, fields if cmd == 0x1B else auxiliary_fields[cmd]) for cmd in COMMANDS)
    profile = {"version": MACOS_VERSION, "machine_id": machine_id or str(uuid.uuid4()),
               "machine_os": device_platforms.resolve(platform)["machine_os"], "platform": platform,
               "umid_route": MACOS_UMID_ROUTE, "blocks": base64.b64encode(blocks).decode(),
               "created_at": int(time.time())}
    validate(profile)
    return profile


def _generate_windows(machine_id: str | None, platform: str) -> dict:
    fields = device_windows.generate_fields()
    auxiliary_fields = {0x1A: [b"", secrets.token_hex(16).encode("ascii")],
                        0x1C: [b""] * 5, 0x1D: [b"", b"{}", b""]}
    blocks = b"".join(encode_block(cmd, fields if cmd == 0x1B else auxiliary_fields[cmd]) for cmd in COMMANDS)
    profile = {"version": WINDOWS_VERSION, "machine_id": machine_id or str(uuid.uuid4()),
               "machine_os": device_platforms.resolve(platform)["machine_os"], "platform": platform,
               "umid_route": WINDOWS_UMID_ROUTE, "blocks": base64.b64encode(blocks).decode(),
               "created_at": int(time.time())}
    validate(profile)
    return profile


def generate(machine_id: str | None = None, *, platform: str = device_platforms.DEFAULT) -> dict:
    """Generate only local data. Does not sample hardware or contact any service."""
    selected = device_platforms.resolve(platform)
    if platform == "macos-arm64":
        return _generate_macos(machine_id, platform)
    if platform == "windows-x64":
        return _generate_windows(machine_id, platform)
    mac = "02" + secrets.token_hex(5)
    device_uuid = str(uuid.uuid4())
    hostname = "parrot-" + secrets.token_hex(6)
    fields = [""] * 43
    values = {
        0: "1000", 1: str(int(time.time() * 1000)), 2: "0", 3: "1", 4: "4",
        5: "1.5.0", 6: "Linux", 7: "Linux version 6.1.0 (Parrot generated profile)",
        8: selected["process"], 11: mac, 12: mac, 14: mac[:8], 15: mac,
        16: "Parrot\n1.0\n", 19: hostname, 21: "Parrot\n", 22: device_uuid,
        23: device_uuid, 24: "Generated device\n", 25: "Parrot\n",
        28: "MemTotal: 8388608 kB\nMemFree: 4194304 kB\n", 32: "",
    }
    for key, value in values.items():
        fields[key] = value
    # Empty auxiliary state, not a captured machine's auxiliary block.
    blocks = [encode_block(cmd, [v.encode() for v in fields] if cmd == 0x1B else auxiliary(cmd))
              for cmd in COMMANDS]
    profile = {"version": VERSION, "machine_id": machine_id or str(uuid.uuid4()),
               "machine_os": selected["machine_os"], "umid_route": UMID_ROUTE,
               "blocks": base64.b64encode(b"".join(blocks)).decode(),
               "created_at": int(time.time())}
    if platform != device_platforms.DEFAULT:
        profile["platform"] = platform
    validate(profile)
    return profile


def validate(profile: dict) -> bytes:
    if not isinstance(profile, dict) or profile.get("version") not in (*CURRENT_VERSIONS, LEGACY_VERSION):
        raise ValueError("Unsupported Qoder device profile")
    machine = profile.get("machine_id")
    if not isinstance(machine, str) or not machine or len(machine) > 64 or not all(33 <= ord(c) <= 126 for c in machine):
        raise ValueError("Invalid Qoder machine id")
    platform = device_platforms.for_profile(profile)
    selected = device_platforms.resolve(platform)
    if profile["version"] == LEGACY_VERSION and platform != device_platforms.DEFAULT:
        raise ValueError("Unsupported legacy Qoder device platform")
    macos = platform == "macos-arm64"
    windows = platform == "windows-x64"
    expected_route = MACOS_UMID_ROUTE if macos else WINDOWS_UMID_ROUTE if windows else UMID_ROUTE
    if (profile.get("machine_os") != selected["machine_os"] or profile.get("umid_route") != expected_route
            or (profile["version"] == MACOS_VERSION) != macos
            or (profile["version"] == WINDOWS_VERSION) != windows):
        raise ValueError("Unsupported Qoder device platform or route")
    encoded = profile.get("blocks")
    if not isinstance(encoded, str) or len(encoded) > 90000:
        raise ValueError("Invalid Qoder device blocks")
    created = profile.get("created_at")
    if type(created) is not int or not 0 <= created <= 2**53:
        raise ValueError("Invalid Qoder profile creation time")
    blocks = base64.b64decode(encoded, validate=True)
    decoded = decode_blocks(blocks)
    if macos or windows:
        state = decoded[0]
        if (len(state) != 2 or state[0] != b"" or len(state[1]) != 32 or not state[1].isalnum()
                or decoded[2] != [b""] * 5 or decoded[3] != [b"", b"{}", b""]):
            raise ValueError("Unsupported device auxiliary state")
        (device_windows if windows else device_macos).validate_fields(decoded[1])
        return blocks
    if len(decoded[1]) != 43:
        raise ValueError("Invalid Linux device field count")
    if any(decoded[i] != (auxiliary(COMMANDS[i]) if i != 3 or profile["version"] == VERSION else [b""] * 5) for i in (0, 2, 3)):
        raise ValueError("Unsupported Qoder auxiliary state")
    fields = decoded[1]
    if fields[0] != b"1000" or fields[5] != b"1.5.0" or fields[6] != b"Linux" or fields[8] != selected["process"].encode("ascii"):
        raise ValueError("Inconsistent Qoder device protocol")
    if fields[11] != fields[12] or fields[11] != fields[15] or fields[14] != fields[11][:8] or fields[22] != fields[23]:
        raise ValueError("Inconsistent Qoder device identity")
    return blocks


def upgrade(profile: dict) -> dict:
    """Local draft only: preserve all identity bytes and block keys; fix auxiliary 1D."""
    blob = validate(profile)
    result = copy.deepcopy(profile)
    if profile["version"] in CURRENT_VERSIONS:
        return result
    offset = 0
    for _ in range(3):
        offset += int.from_bytes(blob[offset + 2:offset + 6], "big")
    result["blocks"] = base64.b64encode(blob[:offset] + encode_block(
        0x1D, auxiliary(0x1D), key=blob[offset + 7:offset + 11])).decode()
    result["version"] = VERSION
    validate(result)
    return result


def is_confirmed_upgrade(old: dict, new: dict) -> bool:
    return old.get("version") == LEGACY_VERSION and upgrade(old) == new


def fingerprint(profile: dict) -> str:
    blocks = validate(profile)
    return hashlib.sha256(profile["machine_id"].encode() + b"\0" + blocks).hexdigest()


def hardware(profile: dict) -> dict:
    fields = decode_blocks(validate(profile))[1]
    platform = device_platforms.for_profile(profile)
    def text(index):
        return fields[index].decode("utf-8", errors="replace").strip()
    result = {"manufacturer": text(25), "model": text(24), "system": text(7),
              "architecture": "ARM64" if platform.endswith("arm64") else "x86_64",
              "cpu": None, "motherboard": None}
    if platform == "macos-arm64":
        result.update(memory_bytes=int(fields[28]), storage_bytes=int(fields[27]))
    elif platform == "windows-x64":
        result["available_memory_bytes"] = int(fields[28]) * 1024**2
    else:
        memory = re.search(rb"(?m)^MemTotal:\s*(\d+)\s*kB\s*$", fields[28])
        if memory:
            result["memory_bytes"] = int(memory[1]) * 1024
    return result


def summary(account: dict) -> dict:
    profile = account.get("qoder_device_profile")
    if not profile:
        return {"status": "initialization_required", "outbound_ip": None}
    try:
        digest = fingerprint(profile)
        if account.get("machine_id") is not None and account["machine_id"] != profile["machine_id"]:
            raise ValueError("Inconsistent Qoder machine id")
    except (ValueError, TypeError):
        return {"status": "invalid_profile", "outbound_ip": None}
    return {"status": "migration_required" if profile["version"] == LEGACY_VERSION else "bound", "profile_id": digest[:16], "version": profile["version"],
            "platform": profile["machine_os"], "platform_id": device_platforms.for_profile(profile),
            "platform_label": device_platforms.resolve(device_platforms.for_profile(profile))["label"],
            "umid_route": profile["umid_route"],
            "created_at": profile.get("created_at"), "outbound_ip": None,
            "profile_fingerprint": digest, "hardware": hardware(profile)}
