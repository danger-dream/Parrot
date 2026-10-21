"""Locally generated Windows fields; optional host inventories are left empty."""
from __future__ import annotations

import json
import re
import secrets
import string
import time
import uuid

SDK = b"1.0.70.3226"
PROCESS = b"runtime-info-win32-x64.exe"
TAGS = {
    26: ("RESOLUTION", []), 31: ("MAINSIG_INFO", []), 32: ("MAC_SIMPLE", {}),
    33: ("DISK_ALL", []), 38: ("MON_EDID", []), 39: ("DOMAIN_INFO", []),
    40: ("LGUSER_INFO", []), 51: ("GETAP_INFO", []), 52: ("MAINPROC_INFO", {}),
    54: ("CPU_INFO", {}), 60: ("GPU_INFO", []), 64: ("CP_USER_INFO", []),
    65: ("CPUSER_GROUP_INFO", []), 66: ("ACPOWER_INFO", {}), 68: ("COOLDEV_INFO", []),
    69: ("TEMPERATUREPROBE_INFO", []), 70: ("GETMEMORY_INFO", []), 71: ("EFIVAR", {}),
    73: ("BCEDIT_INFO", {}), 76: ("REG_DISK_INFO", {}), 81: ("REG_COLLECTION_INFO", {}),
    83: ("DISKINF_DEVIOCTOL", []),
}


def _json(tag, data):
    return json.dumps({"Tag": tag, "data": data}, separators=(",", ":")).encode("ascii")


def generate_fields() -> list[bytes]:
    now = time.time()
    milliseconds = str(int(now * 1000))
    mac = "02" + secrets.token_hex(5).upper()
    machine_uuid = str(uuid.uuid4()).upper()
    serial = secrets.token_hex(10).upper()
    product_key = "-".join("".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5)) for _ in range(5))
    fields = [b""] * 100
    values = {
        0: "1005", 1: milliseconds, 2: "0", 3: "1", 4: "4", 5: SDK.decode(), 6: "Windows",
        7: "11.22631.48", 8: PROCESS.decode(), 10: "0", 11: mac,
        12: "PARROT-" + secrets.token_hex(4).upper(), 14: secrets.token_hex(4).upper(),
        15: serial, 16: mac, 17: mac, 18: "0000000000000000",
        19: "PARROT-" + secrets.token_hex(4).upper(), 20: secrets.token_hex(8).upper(),
        21: "Parrot", 22: product_key, 23: machine_uuid, 24: "Generated Windows device", 25: "Parrot",
        27: "0", 28: "8192", 30: "0000", 34: milliseconds + ".000000", 35: milliseconds + ".000000",
        36: milliseconds + ".000000", 37: "64", 41: milliseconds + ".000000", 42: "1", 43: "2",
        45: mac, 46: machine_uuid, 47: serial, 75: str(uuid.uuid4()).upper(),
        80: str(uuid.uuid4()).upper(), 84: str(uuid.uuid4()).upper(), 89: serial,
        93: "Parrot", 94: "Parrot", 95: "Generated Windows device", 96: machine_uuid, 97: serial,
    }
    for index, value in values.items():
        fields[index] = value.encode("ascii")
    for index, (tag, data) in TAGS.items():
        fields[index] = _json(tag, data)
    fields[26] = _json("RESOLUTION", [{"DisplayFrequency": "60", "ScreenPhysicsHeight": "0",
        "ScreenPhysicsWidth": "0", "ScreenPixelHeight": "1080", "ScreenPixelWidth": "1920"}])
    validate_fields(fields)
    return fields


def validate_fields(fields: list[bytes]) -> None:
    fixed = {0: b"1005", 2: b"0", 3: b"1", 4: b"4", 5: SDK, 6: b"Windows", 7: b"11.22631.48",
             8: PROCESS, 10: b"0", 21: b"Parrot", 24: b"Generated Windows device", 25: b"Parrot",
             37: b"64", 42: b"1", 43: b"2", 93: b"Parrot", 94: b"Parrot", 95: b"Generated Windows device"}
    if len(fields) != 100 or any(not isinstance(value, bytes) for value in fields):
        raise ValueError("Invalid Windows device fields")
    if any(fields[index] != value for index, value in fixed.items()):
        raise ValueError("Inconsistent Windows device protocol")
    populated = {*fixed, *TAGS, 1, 11, 12, 14, 15, 16, 17, 18, 19, 20, 22, 23, 27, 28, 30,
                 34, 35, 36, 41, 45, 46, 47, 75, 80, 84, 89, 96, 97}
    if any(value for index, value in enumerate(fields) if index not in populated):
        raise ValueError("Unsupported generated Windows field")
    if not re.fullmatch(rb"02[0-9A-F]{10}", fields[11]) or any(fields[index] != fields[11] for index in (16, 17, 45)):
        raise ValueError("Inconsistent generated Windows MAC")
    if not re.fullmatch(rb"PARROT-[0-9A-F]{8}", fields[19]):
        raise ValueError("Invalid generated Windows hostname")
    if not re.fullmatch(rb"[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}", fields[22]):
        raise ValueError("Invalid generated Windows product-key field")
    if not re.fullmatch(rb"[0-9A-F]{20}", fields[15]) or any(fields[index] != fields[15] for index in (47, 89, 97)):
        raise ValueError("Inconsistent generated Windows serial")
    for index in (23, 46, 75, 80, 84, 96):
        uuid.UUID(fields[index].decode("ascii"))
    if fields[46] != fields[23] or fields[96] != fields[23]:
        raise ValueError("Inconsistent generated Windows UUID")
    if not fields[1].isdigit() or not 0 < int(fields[1]) <= 2**53:
        raise ValueError("Invalid Windows profile timestamp")
    if (not re.fullmatch(rb"PARROT-[0-9A-F]{8}", fields[12])
            or not re.fullmatch(rb"[0-9A-F]{8}", fields[14])
            or not re.fullmatch(rb"[0-9A-F]{16}", fields[20])
            or fields[18] != b"0000000000000000" or fields[27] != b"0" or fields[30] != b"0000"
            or fields[28] != b"8192"):
        raise ValueError("Invalid generated Windows hardware field")
    if (not re.fullmatch(rb"[0-9]+\.[0-9]{6}", fields[41])
            or any(fields[index] != fields[41] for index in (34, 35, 36))
            or fields[41].split(b".")[0] != fields[1]):
        raise ValueError("Inconsistent generated Windows timestamps")
    for index, (tag, empty) in TAGS.items():
        value = json.loads(fields[index])
        if not isinstance(value, dict) or set(value) != {"Tag", "data"} or value["Tag"] != tag or not isinstance(value["data"], type(empty)):
            raise ValueError("Invalid Windows inventory field")
        expected = [{"DisplayFrequency": "60", "ScreenPhysicsHeight": "0", "ScreenPhysicsWidth": "0",
                     "ScreenPixelHeight": "1080", "ScreenPixelWidth": "1920"}] if index == 26 else empty
        if value["data"] != expected:
            raise ValueError("Unsupported generated Windows inventory")
