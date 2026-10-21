"""Offline device generation, UMID wire/cache, and identity send boundaries."""
import base64
import copy
import json
import struct
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import padding

from src import config, network, state_db
from src.oauth import add_context
from src.oauth.qoder import common, device_blocks as blocks, device_profile as profiles
from src.oauth.qoder import device_identity as identity, umid_protocol as protocol, signing, catalog, billing
from src.proxy import manager


def account():
    p = profiles.generate()
    return {"realm": "global", "uid": p["machine_id"], "machine_id": p["machine_id"],
            "qoder_device_profile": p, "access_token": "oauth", "device_token": "device"}


def response(token="issued-ticket", *, compressed=False):
    raw = json.dumps({"eeid": token}).encode()
    if compressed:
        raw = zlib.compress(raw)
    pad = padding.PKCS7(128).padder()
    wire = protocol._crypt(pad.update(raw) + pad.finalize(), protocol._key(1))
    return json.dumps({"ec": 200, "dt": base64.b64encode(b"\1" + wire).decode()}).encode()


def test_generator_and_four_block_roundtrip_without_host_samples():
    a, b = profiles.generate(), profiles.generate()
    assert a["machine_id"] != b["machine_id"] and a["blocks"] != b["blocks"]
    blob = profiles.validate(a)
    decoded = blocks.decode_blocks(blob)
    rebuilt = b"".join(blocks.encode_block(cmd, fields) for cmd, fields in zip(blocks.COMMANDS, decoded))
    assert blocks.decode_blocks(rebuilt) == decoded
    assert decoded[1][11] == decoded[1][12] == decoded[1][15]
    assert decoded[1][22] == decoded[1][23] and decoded[1][21] == b"Parrot\n"
    assert decoded[0] == [b""] and decoded[2] == [b""] * 5 and decoded[3] == [b"", b"####", b""]
    for bad in [blob[:-1], blob + b"x", b"bad" + blob, blob[:2] + struct.pack(">I", 2**32 - 1) + blob[6:]]:
        with pytest.raises(ValueError):
            blocks.decode_blocks(bad)
    with pytest.raises(ValueError):
        blocks.tlv_unpack(b"\0\5ab")


@pytest.mark.parametrize("command", [0x1B, 0x1D])
@pytest.mark.parametrize("size", [0, 1, 14, 16, 31, 128, 1024])
def test_block_variable_length_roundtrip(command, size):
    fields = [bytes(i % 256 for i in range(size))]
    encoded = blocks.encode_block(command, fields, key=b"1234")
    cipher = encoded[11:]
    transformed = bytes(v ^ b"1234"[(i + 2) % 4] for i, v in enumerate(cipher))
    plain = blocks._transform(transformed, command, inverse=True)
    assert blocks.tlv_unpack(plain[:-plain[-1]]) == fields


def test_umid_request_unpack_and_response_validation():
    blob = profiles.validate(profiles.generate())
    body = base64.b64decode(protocol.build_payload(blob))
    plain = protocol._crypt(body[1:], protocol._key(body[0]), decrypt=True)
    unpad = padding.PKCS7(128).unpadder()
    raw = zlib.decompress(unpad.update(plain) + unpad.finalize())
    assert raw[:4] == b"\xcf\xcd\0\x2b" and int.from_bytes(raw[8:12], "big") == len(raw)
    seed = raw[4:8]
    assert bytes(v ^ seed[i % 4] for i, v in enumerate(raw[12:])) == blob
    assert protocol.parse_response(response()) == "issued-ticket"
    assert protocol.parse_response(response(compressed=True)) == "issued-ticket"
    for raw in [b"{}", b'{"ec":403}', b'{"ec":200,"dt":"AA=="}', response("bad\nheader"), b"x" * 65537]:
        with pytest.raises((ValueError, KeyError)):
            protocol.parse_response(raw)


@pytest.fixture
def isolated(monkeypatch):
    before = copy.deepcopy(config.get())
    config.update(lambda c: c.update(network={"proxies": {
        "one": {"type": "socks5", "url": "socks5://127.0.0.1:18001"},
        "two": {"type": "socks5", "url": "socks5://127.0.0.1:18002"}},
        "routing": {"default": "one", "directFallback": True, "accounts": {}}}))
    state_db.init()
    identity._pending.clear()
    yield
    identity._pending.clear()
    config.update(lambda c: (c.clear(), c.update(before)))


def test_concurrent_ticket_cache_account_and_route_isolation(isolated, monkeypatch):
    calls = []
    def fetch(host, blob, key):
        calls.append((host, blob, key))
        time.sleep(.01)
        return "issued-" + str(len(calls))
    monkeypatch.setattr(identity, "_fetch", fetch)
    a, b = account(), account()
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: identity.headers(a, account_key="a"), range(16)))
    assert len(calls) == 1 and all(v == values[0] for v in values)
    identity.headers(b, account_key="b")
    assert len(calls) == 2
    identity.headers(a, account_key="other-account")
    assert len(calls) == 3  # even accidental profile duplication cannot share a ticket
    config.update(lambda c: c["network"]["routing"]["accounts"].update({"oauth:a": "two"}))
    identity.headers(a, account_key="a")
    assert len(calls) == 4
    assert values[0]["Cosy-MachineToken"] != values[0]["Cosy-MachineId"]
    assert len(values[0]["Cosy-MachineType"]) == len(values[0]["Cosy-MachineCode"]) == 18


def test_issuer_failure_backoff_and_candidate_save_retry(isolated, monkeypatch):
    a, calls = account(), []
    def failed(*args):
        calls.append(1)
        raise common.QoderError("device_identity", kind="umid_unavailable")
    monkeypatch.setattr(identity, "_fetch", failed)
    for _ in range(2):
        with pytest.raises(common.QoderError):
            identity.headers(a, account_key="a")
    assert calls == [1]
    b = account()
    monkeypatch.setattr(identity, "_fetch", lambda *args: calls.append(2) or "issued")
    save = state_db.qoder_identity_save
    fail = [True]
    def saving(key, row):
        if row.get("token") and fail[0]:
            raise OSError("secret should not leak")
        return save(key, row)
    monkeypatch.setattr(state_db, "qoder_identity_save", saving)
    with pytest.raises(common.QoderError, match="identity_store_unavailable"):
        identity.headers(b, account_key="b")
    fail[0] = False
    assert identity.headers(b, account_key="b")["Cosy-MachineToken"] == "issued"
    assert calls == [1, 2]


def test_actual_byte_stream_uses_attempt_proxy_and_never_falls_back(isolated, monkeypatch):
    monkeypatch.setattr(common, "_network_disabled", lambda: False)
    seen = []
    class Stream:
        def __init__(self):
            self.chunks = [b"HTTP/1.0 200 OK\r\n\r\n" + response(), b""]
        def sendall(self, raw):
            assert raw.startswith(b"POST /repPc.json?")
            assert b"Host: pum.m.alibaba.com" in raw
        def recv(self, size):
            return self.chunks.pop(0)
        def settimeout(self, timeout):
            assert 0 < timeout <= 15
        def close(self):
            pass
    class Connector:
        def config_dict(self):
            return {"type": "socks5", "url": "socks5://127.0.0.1:18002"}
        def open_sync_stream(self, host, port, **kw):
            seen.append((host, port))
            return Stream()
    manager.init()
    get = manager.get_connector
    monkeypatch.setattr(manager, "get_connector", lambda name: Connector() if name == "two" else get(name))
    with add_context.bind(add_context.AddAttempt(target="two", provider="qoder")):
        assert identity.headers(account())["Cosy-MachineToken"] == "issued-ticket"
        chain = network._configured_proxy_chain_or_none(proxy_model="", proxy_channel="", proxy_purpose="oauth_qoder")
        assert [n for n, _ in chain] == ["two"]
    assert seen == [("pum.m.alibaba.com", 443)]
    assert [n for n, _ in network._configured_proxy_chain_or_none(proxy_model="", proxy_channel="", proxy_purpose="oauth_workbuddy")] == ["one", "direct"]


def test_model_signing_and_campaign_share_device_provider(isolated, monkeypatch):
    a, seen = account(), []
    monkeypatch.setattr(identity, "_fetch", lambda *args: "issued")
    device = identity.headers(a, account_key="a")
    def request(url, **kw):
        if kw.get("prepare_headers"):
            kw["headers"] = kw.pop("prepare_headers")()
        seen.append((url, kw))
        if "model/list" in url:
            return 200, {"app": [{"key": "m", "enable": True}]}
        return 200, {"campaigns": []}
    monkeypatch.setattr(common, "request", request)
    assert catalog.fetch_models_sync(a, account_key="a")
    billing.checkin_sync(a, account_key="a")
    assert all(seen[0][1]["headers"][k] == v for k, v in device.items())
    for key in ("Cosy-MachineToken", "Cosy-MachineType"):
        assert seen[1][1]["headers"][key] == device[key]
    assert seen[1][1]["headers"]["Cosy-ClientType"] == "10"  # campaigns, not IDE activity
    assert "Encode" not in seen[0][0] and "body" not in seen[0][1]
    h = seen[0][1]["headers"]
    assert h["Cosy-Version"] == "1.24.2" and h["Cosy-Data-Policy"] == "DISAGREE" and h["Cosy-ClientType"] == "0"
    assert "Cosy-ClientIp" not in h
    old = dict(a); old.pop("qoder_device_profile")
    with pytest.raises(common.QoderError, match="device_initialization_required"):
        identity.headers(old)
    assert old["machine_id"] == a["machine_id"]
