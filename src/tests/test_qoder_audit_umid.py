"""Independent native audit vectors + framing and explicit profile migration regressions."""
import base64
import copy
import time
import zlib

import pytest
from cryptography.hazmat.primitives import padding

from src import config, oauth_manager as om, state_db
from src.oauth.qoder import device_blocks as blocks, device_profile as profiles, device_marks as marks
from src.oauth.qoder import umid_protocol as protocol, umid_http, device_identity as identity, common
from src.tests.test_qoder_devices import isolated, account


@pytest.mark.parametrize('value,seed,expected', [
    (90, '', 'ef46db379051d8e999'), (0, '', 'ef46db370051d8e999'),
    (13, 'OFFLINE-TOKEN', '144e326c1327ed8b90'), (84, 'OFFLINE-TOKEN', '144e326c8427ed8b90'),
    (90, 'P1gA' + 'x' * 84, '77fc3b2090ce854741'),
])
def test_mark_matches_official_machine_code_vectors(value, seed, expected):
    assert marks.mark(value, seed) == expected


def legacy_profile():
    p = profiles.generate()
    blob = profiles.validate(p)
    offset = 0
    for _ in range(3):
        offset += int.from_bytes(blob[offset + 2:offset + 6], 'big')
    p['blocks'] = base64.b64encode(blob[:offset] + blocks.encode_block(0x1D, [b''] * 5, key=b'1234')).decode()
    p['version'] = profiles.LEGACY_VERSION
    return p


def test_native_auxiliary_vector_and_block0_semantics():
    assert blocks.encode_block(0x1D, [b'', b'####', b''], key=b'\0' * 4).hex() == 'cfcd0000001b1d00000000555347ddeb686ba652ca5137616d29da'
    p = profiles.generate()
    decoded = blocks.decode_blocks(profiles.validate(p))
    assert [len(f) for f in decoded] == [1, 43, 5, 3]
    old = legacy_profile()
    new = profiles.upgrade(old)
    before, after = profiles.validate(old), profiles.validate(new)
    offset = 0
    for _ in range(3):
        offset += int.from_bytes(before[offset + 2:offset + 6], 'big')
    assert before[:offset] == after[:offset]  # block0 ciphertext and all identity bytes unchanged
    assert before[offset + 7:offset + 11] == after[offset + 7:offset + 11]
    assert old['machine_id'] == new['machine_id'] and old['created_at'] == new['created_at']


def test_compression_uses_native_level6(monkeypatch):
    blob = profiles.validate(profiles.generate())
    seed = b'1234'
    monkeypatch.setattr(protocol.secrets, 'token_bytes', lambda n: seed)
    monkeypatch.setattr(protocol.secrets, 'randbelow', lambda n: 0)
    wire = base64.b64decode(protocol.build_payload(blob))
    unpad = padding.PKCS7(128).unpadder()
    compressed = unpad.update(protocol._crypt(wire[1:], protocol._key(1), decrypt=True)) + unpad.finalize()
    packet = b'\xcf\xcd\0\x2b' + seed + (len(blob) + 12).to_bytes(4, 'big') + bytes(v ^ seed[i % 4] for i, v in enumerate(blob))
    assert compressed == zlib.compress(packet, 6) and compressed[:2] == b'\x78\x9c'


class Stream:
    def __init__(self, wire):
        self.wire = wire
    def settimeout(self, value):
        assert 0 < value <= 15
    def recv(self, size):
        part, self.wire = self.wire[:7], self.wire[7:]
        return part


@pytest.mark.parametrize('wire', [
    b'HTTP/1.1 200 OK\r\nContent-Length: 19\r\n\r\n{}',
    b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n{}',
    b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 2\r\n\r\n{}',
    b'NOTHTTP 200 OK\r\n\r\n{}',
    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n',
    b'HTTP/1.1 200 OK\r\nX:' + b'a' * 8192 + b'\r\n\r\n{}',
    b'HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n',
])
def test_invalid_http_framing_never_reaches_ticket_parser(wire):
    with pytest.raises(ValueError):
        umid_http.read_response(Stream(wire), time.monotonic() + 15)


@pytest.mark.parametrize('head', [b'HTTP/1.0 200 OK\r\n\r\n', b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n'])
def test_bounded_http_framing_accepts_valid_modes(head):
    assert umid_http.read_response(Stream(head + b'{}'), time.monotonic() + 15) == b'{}'


def test_ticket_marks_version_and_binding(isolated, monkeypatch):
    a = account()
    monkeypatch.setattr(identity, '_fetch', lambda *args: 'OFFLINE-TOKEN')
    h = identity.headers(a, account_key='a')
    assert h['Cosy-MachineType'] == marks.mark(90, 'OFFLINE-TOKEN')
    assert h['Cosy-MachineCode'] == marks.mark(0, h['Cosy-MachineType'])
    key = identity._binding(a, 'a')[0]
    assert state_db.qoder_identity_load(key)['format_version'] == 2


def test_legacy_profile_cannot_issue_until_confirmed_reauthorization(isolated, monkeypatch):
    from src.tests.test_workbuddy_lifecycle import context
    from src.management_control.oauth import OAuthProvider
    a = account()
    old = legacy_profile()
    a.update(provider="qoder", type="qoder", machine_id=old['machine_id'], qoder_device_profile=old)
    config.update(lambda c: c.update(oauthAccounts=[a]))
    key = om.get_account_key(a)
    before = copy.deepcopy(om.get_account(key))
    with pytest.raises(common.QoderError, match='device_migration_required'):
        identity.headers(before, account_key=key)
    # Explicit draft upgrades only auxiliary bytes, not the bound account.
    from src.management_control.oauth import OAuthControl
    ctl = OAuthControl()
    f = ctl.start_login_flow(context(), OAuthProvider.QODER, relogin_account_id=key)
    assert f.device_preview['migration'] and om.get_account(key) == before
    draft = ctl._flows.qoder.store.inspect_parts(f.flow_id, f.flow_secret, actor_subject_id=context().actor.subject_id, kind='qoder-login').payload['profile']
    incoming = dict(before, qoder_device_profile=draft, access_token='new-token')
    assert om.replace_exact_identity(key, incoming, expected_account=before)['status'] != 'replaced'
    assert om.get_account(key) == before
    assert om.replace_exact_identity(key, incoming, expected_account=before, authenticated_login=True)['status'] == 'replaced'
    assert om.get_account(key)['machine_id'] == before['machine_id']


def test_old_random_ticket_format_is_not_reused(isolated, monkeypatch):
    a = account()
    key = identity._binding(a, 'a')[0]
    state_db.qoder_identity_save(key, {'token': 'old', 'type': 'a' * 18, 'code': 'b' * 18, 'expires': time.time() + 999})
    monkeypatch.setattr(identity, '_fetch', lambda *a: 'new-ticket')
    assert identity.headers(a, account_key='a')['Cosy-MachineToken'] == 'new-ticket'


def test_online_probe_defaults_offline_and_sends_at_most_once(monkeypatch):
    import importlib.util
    import socket
    from pathlib import Path
    from src.tests.test_qoder_devices import response
    path = Path(__file__).parents[2] / 'scripts' / 'qoder_umid_probe.py'
    spec = importlib.util.spec_from_file_location('test_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('dry run cannot connect'))
    assert probe.probe()['executed'] is False
    sent = []
    class Issuer(Stream):
        def sendall(self, raw):
            sent.append(raw)
        def close(self):
            pass
    result = probe.probe(execute=True, open_stream=lambda *a, **kw: Issuer(b'HTTP/1.0 200 OK\r\n\r\n' + response()))
    assert len(sent) == 1 and result['issued'] and 'issued-ticket' not in str(result)
