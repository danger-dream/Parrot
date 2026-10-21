"""Windows profiles are generated locally and use the observed Windows wire format."""
import base64
import copy
import json

import pytest

from src.oauth.qoder import device_blocks, device_identity, device_profile, device_windows, umid_protocol


def test_windows_complete_profile_is_generated_and_preserved():
    first = device_profile.generate(platform="windows-x64")
    second = device_profile.generate(platform="windows-x64")
    decoded = device_blocks.decode_blocks(device_profile.validate(first))
    assert [len(fields) for fields in decoded] == [2, 100, 5, 3]
    assert decoded[0][0] == b"" and len(decoded[0][1]) == 32
    assert decoded[2] == [b""] * 5 and decoded[3] == [b"", b"{}", b""]
    assert first["machine_os"] == "x86_64_win32"
    assert first["umid_route"] == device_profile.WINDOWS_UMID_ROUTE
    assert device_profile.upgrade(first) == first
    assert device_profile.summary({"qoder_device_profile": first})["platform_id"] == "windows-x64"
    assert device_profile.fingerprint(first) != device_profile.fingerprint(second)
    assert decoded[0][1] != device_blocks.decode_blocks(device_profile.validate(second))[0][1]
    for key, value in (("platform", "linux-x64"), ("platform", "macos-arm64"),
                       ("version", device_profile.MACOS_VERSION), ("umid_route", device_profile.UMID_ROUTE)):
        changed = {**first, key: value}
        with pytest.raises(ValueError):
            device_profile.validate(changed)


def test_windows_fields_are_self_consistent_without_copied_host_inventory(monkeypatch):
    monkeypatch.setattr(device_windows.time, "time", lambda: 1780000000.9999999)
    first, second = device_windows.generate_fields(), device_windows.generate_fields()
    assert len(first) == 100
    assert first[:9] == [b"1005", b"1780000001000", b"0", b"1", b"4", b"1.0.70.3226",
                         b"Windows", b"11.22631.48", b"runtime-info-win32-x64.exe"]
    assert first[11] == first[16] == first[17] == first[45]
    assert first[23] == first[46] == first[96]
    assert all(first[index] != second[index] for index in (11, 12, 14, 15, 19, 20, 22, 23, 75, 80, 84))
    for index, (tag, data) in device_windows.TAGS.items():
        value = json.loads(first[index])
        assert value["Tag"] == tag
        assert isinstance(value["data"], type(data))
        if index != 26:
            assert value["data"] == data
    device_windows.validate_fields(first)


@pytest.mark.parametrize("index,value", [
    (0, b"1002"), (2, b"1"), (5, b"1.5.0"), (6, b"Linux"), (8, b"runtime-info-linux-x64"),
    (11, b"not-a-mac"), (16, b"02FFFFFFFFFF"), (19, b"real-hostname"), (22, b"invalid"),
    (23, b"not-a-uuid"), (28, b"MemTotal"), (34, b"0"), (46, b"not-a-uuid"),
    (51, b'{"Tag":"GETAP_INFO","data":[{"MAC":"copied"}]}'),
    (52, b'{"Tag":"MAINPROC_INFO","data":[]}'), (53, b"copied-software"), (83, b"[]"),
])
def test_windows_rejects_mixed_platform_and_unapproved_inventory(index, value):
    fields = copy.deepcopy(device_windows.generate_fields())
    fields[index] = value
    with pytest.raises(ValueError):
        device_windows.validate_fields(fields)


def test_windows_profile_rejects_unapproved_auxiliary_state():
    profile = device_profile.generate(platform="windows-x64")
    decoded = device_blocks.decode_blocks(device_profile.validate(profile))
    decoded[0][0] = b"copied-ticket"
    profile["blocks"] = base64.b64encode(b"".join(device_blocks.encode_block(command, values)
        for command, values in zip(device_blocks.COMMANDS, decoded))).decode()
    with pytest.raises(ValueError):
        device_profile.validate(profile)


def test_windows_issuer_uses_plaintext_443_and_initialization_environment(monkeypatch):
    profile = device_profile.generate(platform="windows-x64")
    blocks = device_profile.validate(profile)
    events = []
    class Stream:
        def settimeout(self, timeout): pass
        def start_tls(self, *args, **kwargs): pytest.fail("Windows native transport is not TLS")
        def sendall(self, wire):
            expected = b"/repPc.json?e=1005&pn=runtime-info-win32-x64.exe&os=6&sv=1.0.0&pv=NA&pt=1"
            assert wire.startswith(b"POST " + expected + b" HTTP/1.0\r\n")
            assert b"Content-Type: text/plain;charset=UTF-8\r\n" in wire
            events.append("send")
        def close(self): events.append("close")
    def connect(host, port, **kwargs):
        assert (host, port) == ("issuer.invalid", 443)
        assert kwargs["proxy_provider"] == "qoder"
        assert kwargs["proxy_channel"] == "oauth:windows-fixture"
        events.append("connect")
        return Stream()
    monkeypatch.setattr(device_identity.common, "_network_disabled", lambda: False)
    monkeypatch.setattr(device_identity.network, "open_sync_stream", connect)
    monkeypatch.setattr(device_identity.umid_http, "read_response", lambda *a: b"fixture")
    monkeypatch.setattr(umid_protocol, "parse_response", lambda raw: "fixture-token")
    assert device_identity._fetch("issuer.invalid", blocks, "windows-fixture") == "fixture-token"
    assert events == ["connect", "send", "close"]
