"""SIP004 AEAD client: KDF, URL parse, loopback protocol, connector path."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import ipaddress
import os
import socket
import ssl
import struct
import threading
import time

import httpx
import pytest

from src.proxy.connector import (
    SS2022Connector,
    connector_from_config,
    parse_proxy_url,
)
from src.proxy.ss2022 import (
    HEADER_TYPE_CLIENT,
    HEADER_TYPE_SERVER,
    SS2022Connection,
    _decode_key,
    _derive_session_key,
    create_ss_connection,
    is_ss_aead_cipher,
    parse_ss_url,
    ss_family_label,
)
from src.proxy.ss_aead import (
    SSAEADConnection,
    SSAEADError,
    derive_aead_subkey,
    evp_bytes_to_key,
)
from src.proxy.ss_common import (
    AEAD_CIPHERS,
    AEAD_OVERHEAD,
    ATYP_DOMAIN,
    ATYP_IPV4,
    Reader,
    Writer,
    encode_addr,
    inc_nonce,
)
from src.telegram.menus import proxy_menu


AEAD_METHODS = (
    "chacha20-ietf-poly1305",
    "aes-256-gcm",
    "aes-128-gcm",
)


# ── KDF ──────────────────────────────────────────────────────────

def test_evp_bytes_to_key_known_vectors():
    assert evp_bytes_to_key(b"password", 16) == bytes.fromhex(
        "5f4dcc3b5aa765d61d8327deb882cf99"
    )
    assert evp_bytes_to_key(b"password", 32) == bytes.fromhex(
        "5f4dcc3b5aa765d61d8327deb882cf992b95990a9151374abd8ff8c5a7a0fe08"
    )


def test_ss_subkey_is_hkdf_sha1_with_fixed_info():
    master = evp_bytes_to_key(b"secret", 32)
    salt = bytes(range(32))
    sub = derive_aead_subkey(master, salt, 32)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    expect = HKDF(
        algorithm=hashes.SHA1(), length=32, salt=salt, info=b"ss-subkey",
    ).derive(master)
    assert sub == expect
    assert len(sub) == 32


# ── URL / factory ────────────────────────────────────────────────

def test_parse_plaintext_aead_userinfo():
    info = parse_ss_url("ss://chacha20-ietf-poly1305:s3cret@127.0.0.1:8388#jp-aead")
    assert info["cipher"] == "chacha20-ietf-poly1305"
    assert info["password"] == "s3cret"
    assert info["server"] == "127.0.0.1"
    assert info["port"] == 8388
    assert info["name"] == "jp-aead"


def test_parse_percent_encoded_password():
    info = parse_ss_url("ss://aes-256-gcm:p%2Fss%40word@127.0.0.1:443#x")
    assert info["cipher"] == "aes-256-gcm"
    assert info["password"] == "p/ss@word"


def test_parse_legacy_all_in_one_base64():
    blob = base64.urlsafe_b64encode(b"aes-128-gcm:hello@10.0.0.2:9000").decode().rstrip("=")
    info = parse_ss_url(f"ss://{blob}#legacy")
    assert info["cipher"] == "aes-128-gcm"
    assert info["password"] == "hello"
    assert info["server"] == "10.0.0.2"
    assert info["port"] == 9000
    assert info["name"] == "legacy"


def test_parse_ss2022_base64_userinfo_still_works():
    key = bytes(range(32))
    password = base64.urlsafe_b64encode(key).decode().rstrip("=")
    userinfo = base64.urlsafe_b64encode(
        f"2022-blake3-aes-256-gcm:{password}".encode()
    ).decode().rstrip("=")
    cfg = parse_proxy_url(f"ss://{userinfo}@127.0.0.1:8388#jp-main")
    assert cfg["type"] == "ss2022"
    assert cfg["cipher"] == "2022-blake3-aes-256-gcm"
    assert cfg["password"] == password
    conn = connector_from_config("jp-main", cfg)
    assert conn.type == "ss2022"
    assert ss_family_label(conn.cipher) == "SS2022"


def test_parse_plugin_rejected():
    with pytest.raises(ValueError, match="SIP003"):
        parse_ss_url(
            "ss://chacha20-ietf-poly1305:pw@127.0.0.1:8388"
            "?plugin=obfs-local%3Bobfs%3Dhttp#x"
        )


def test_parse_unsupported_ciphers_rejected():
    for method in ("rc4-md5", "aes-256-cfb", "xchacha20-ietf-poly1305", "chacha20-poly1305"):
        with pytest.raises(ValueError, match="unsupported cipher"):
            parse_ss_url(f"ss://{method}:pw@127.0.0.1:8388")


def test_create_ss_connection_dispatch():
    aead = create_ss_connection("aes-128-gcm", "pw", "127.0.0.1", 1)
    assert isinstance(aead, SSAEADConnection)
    key = base64.urlsafe_b64encode(b"x" * 16).decode()
    ss2022 = create_ss_connection("2022-blake3-aes-128-gcm", key, "127.0.0.1", 1)
    assert isinstance(ss2022, SS2022Connection)
    with pytest.raises(ValueError, match="unsupported cipher"):
        create_ss_connection("rc4-md5", "pw", "127.0.0.1", 1)


def test_connector_rejects_unknown_cipher():
    with pytest.raises(ValueError, match="unsupported cipher"):
        SS2022Connector("bad", "127.0.0.1", 1, "rc4-md5", "pw")


def test_connector_display_and_menu_label():
    c = SS2022Connector("n1", "127.0.0.1", 8388, "chacha20-ietf-poly1305", "pw")
    assert "SS AEAD" in c.display()
    assert is_ss_aead_cipher(c.cipher)
    line = proxy_menu._proxy_detail_line(c)
    assert "SS AEAD" in line
    assert "chacha20-ietf-poly1305" in line
    key = base64.urlsafe_b64encode(b"y" * 16).decode()
    c2 = SS2022Connector("n2", "127.0.0.1", 8388, "2022-blake3-aes-128-gcm", key)
    assert "SS2022" in c2.display()
    assert "SS AEAD" not in proxy_menu._proxy_detail_line(c2)


# ── loopback servers ─────────────────────────────────────────────

def _parse_socks_addr(buf: bytes) -> tuple[str, int, bytes]:
    atyp = buf[0]
    if atyp != ATYP_IPV4:
        raise ValueError(f"test server only handles IPv4, got {atyp}")
    host = socket.inet_ntoa(buf[1:5])
    port = struct.unpack("!H", buf[5:7])[0]
    return host, port, buf[7:]


class _SSReadAdapter:
    def __init__(self, reader):
        self._reader = reader

    async def read(self, n=65536):
        return await self._reader.read(n)


class _SSWriteAdapter:
    def __init__(self, writer, raw_w=None):
        self._writer = writer
        self._raw_w = raw_w
        self._pending = b""

    def write(self, data):
        self._pending = data

    async def drain(self):
        data, self._pending = self._pending, b""
        if data:
            await self._writer.write(data)

    def close(self):
        if self._raw_w is not None:
            self._raw_w.close()


async def _pump(a_r, a_w, b_r, b_w) -> None:
    async def one(src, dst):
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        try:
            dst.close()
        except Exception:
            pass

    await asyncio.gather(one(a_r, b_w), one(b_r, a_w), return_exceptions=True)


async def _handle_aead_client(reader, writer, cipher: str, password: str) -> None:
    spec = AEAD_CIPHERS[cipher]
    master = evp_bytes_to_key(password.encode("utf-8"), spec.key_size)
    client_salt = await reader.readexactly(spec.salt_size)
    c_aead = spec.make_aead(derive_aead_subkey(master, client_salt, spec.key_size))
    c_nonce = bytearray(12)

    def dec(ct: bytes) -> bytes:
        pt = c_aead.decrypt(bytes(c_nonce), ct, None)
        inc_nonce(c_nonce)
        return pt

    async def read_chunk() -> bytes:
        lc = await reader.readexactly(2 + AEAD_OVERHEAD)
        plen = struct.unpack("!H", dec(lc))[0]
        return dec(await reader.readexactly(plen + AEAD_OVERHEAD))

    first = await read_chunk()
    host, port, rest = _parse_socks_addr(first)
    target_r, target_w = await asyncio.open_connection(host, port)

    server_salt = os.urandom(spec.salt_size)
    s_aead = spec.make_aead(derive_aead_subkey(master, server_salt, spec.key_size))
    s_nonce = bytearray(12)
    writer.write(server_salt)
    await writer.drain()
    ss_writer = Writer(writer, s_aead, s_nonce)
    ss_reader = Reader(reader, c_aead, c_nonce)
    if rest:
        target_w.write(rest)
        await target_w.drain()
    await _pump(
        _SSReadAdapter(ss_reader),
        _SSWriteAdapter(ss_writer, writer),
        target_r,
        target_w,
    )


async def _handle_ss2022_client(reader, writer, cipher: str, password: str) -> None:
    from src.proxy.ss2022 import CIPHERS
    spec = CIPHERS[cipher]
    ks = spec.key_size
    psk = _decode_key(password)
    client_salt = await reader.readexactly(ks)
    c_aead = spec.make_aead(_derive_session_key(psk, client_salt, ks))
    c_nonce = bytearray(12)

    def dec(ct: bytes) -> bytes:
        pt = c_aead.decrypt(bytes(c_nonce), ct, None)
        inc_nonce(c_nonce)
        return pt

    fixed = dec(await reader.readexactly(11 + AEAD_OVERHEAD))
    assert fixed[0] == HEADER_TYPE_CLIENT
    vlen = struct.unpack("!H", fixed[9:11])[0]
    var = dec(await reader.readexactly(vlen + AEAD_OVERHEAD))
    host, port, after = _parse_socks_addr(var)
    pad_len = struct.unpack("!H", after[:2])[0]
    rest = after[2 + pad_len:]
    target_r, target_w = await asyncio.open_connection(host, port)

    server_salt = os.urandom(ks)
    s_aead = spec.make_aead(_derive_session_key(psk, server_salt, ks))
    s_nonce = bytearray(12)

    def enc(pt: bytes) -> bytes:
        ct = s_aead.encrypt(bytes(s_nonce), pt, None)
        inc_nonce(s_nonce)
        return ct

    hdr = struct.pack("!BQ", HEADER_TYPE_SERVER, int(time.time())) + client_salt + struct.pack("!H", 0)
    writer.write(server_salt + enc(hdr))
    await writer.drain()
    ss_writer = Writer(writer, s_aead, s_nonce)
    ss_reader = Reader(reader, c_aead, c_nonce)
    if rest:
        target_w.write(rest)
        await target_w.drain()
    await _pump(
        _SSReadAdapter(ss_reader),
        _SSWriteAdapter(ss_writer, writer),
        target_r,
        target_w,
    )


async def _start_http_origin() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        await reader.read(4096)
        body = b"ok-ss"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_ss_proxy(handler) -> tuple[asyncio.AbstractServer, int]:
    async def accept(reader, writer):
        try:
            await handler(reader, writer)
        except Exception as exc:
            print(f"[ss-test-proxy] {type(exc).__name__}: {exc}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.parametrize("cipher", AEAD_METHODS)
async def test_aead_raw_http_all_ciphers(cipher):
    origin, origin_port = await _start_http_origin()
    password = "test-pass"
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_aead_client(r, w, cipher, password)
    )
    conn = SSAEADConnection(cipher, password, "127.0.0.1", proxy_port)
    try:
        await conn.connect("127.0.0.1", origin_port)
        await conn.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        data = await asyncio.wait_for(conn.readall(), timeout=5)
        assert b"ok-ss" in data
    finally:
        await conn.close()
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def test_aead_httpx_through_connector():
    origin, origin_port = await _start_http_origin()
    password = "httpx-pass"
    cipher = "chacha20-ietf-poly1305"
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_aead_client(r, w, cipher, password)
    )
    connector = SS2022Connector("aead-httpx", "127.0.0.1", proxy_port, cipher, password)
    assert connector.config_dict()["type"] == "ss2022"
    try:
        async with connector.create_httpx_client() as client:
            resp = await client.get(f"http://127.0.0.1:{origin_port}/")
            assert resp.status_code == 200
            assert resp.text == "ok-ss"
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def test_ss2022_httpx_regression():
    origin, origin_port = await _start_http_origin()
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("ss2022-httpx", "127.0.0.1", proxy_port, cipher, password)
    try:
        async with connector.create_httpx_client() as client:
            resp = await client.get(f"http://127.0.0.1:{origin_port}/")
            assert resp.status_code == 200
            assert resp.text == "ok-ss"
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def _run_in_test_thread(func):
    outcome = []
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def run():
        try:
            outcome.append((True, func()))
        except BaseException as exc:
            outcome.append((False, exc))
        finally:
            loop.call_soon_threadsafe(done.set)

    thread = threading.Thread(
        target=run, name="ss2022-test-caller", daemon=True,
    )
    thread.start()
    await asyncio.wait_for(done.wait(), timeout=10)
    thread.join(1)
    assert not thread.is_alive(), "sync SS2022 test caller did not exit"
    ok, value = outcome[0]
    if not ok:
        raise value
    return value


async def test_ss2022_sync_httpx_tunnel_and_worker_close():
    origin, origin_port = await _start_http_origin()
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("offline-ss", "127.0.0.1", proxy_port, cipher, password)

    def request():
        client = connector.create_sync_httpx_client()
        try:
            response = client.get(f"http://127.0.0.1:{origin_port}/")
            assert isinstance(response, httpx.Response)
            assert response.status_code == 200
            assert response.text == "ok-ss"
        finally:
            client.close()
            client.close()

    try:
        await _run_in_test_thread(request)
        assert not any(
            thread.name == "ss2022-sync-worker" and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def test_ss2022_sync_read_timeout_and_connect_error():
    async def slow_origin(reader, writer):
        await reader.read(4096)
        await asyncio.sleep(1)
        writer.close()

    origin = await asyncio.start_server(slow_origin, "127.0.0.1", 0)
    origin_port = origin.sockets[0].getsockname()[1]
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("offline-ss", "127.0.0.1", proxy_port, cipher, password)

    def timeout_request():
        with connector.create_sync_httpx_client(
            timeout=httpx.Timeout(2, read=0.05)
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                client.get(f"http://127.0.0.1:{origin_port}/")

    try:
        await _run_in_test_thread(timeout_request)
        bad = SS2022Connector("offline-ss", "127.0.0.1", 1, cipher, password)

        def bad_request():
            with bad.create_sync_httpx_client(timeout=0.1) as client:
                client.get(f"http://127.0.0.1:{origin_port}/")

        with pytest.raises(httpx.ConnectError):
            await _run_in_test_thread(bad_request)
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def _start_h2_tls_origin(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import RequestReceived, StreamEnded

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_alpn_protocols(["h2"])

    async def handle(reader, writer):
        conn = H2Connection(config=H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        await writer.drain()
        try:
            while True:
                data = await reader.read(65535)
                if not data:
                    return
                events = conn.receive_data(data)
                for event in events:
                    if isinstance(event, RequestReceived):
                        conn.send_headers(event.stream_id, [
                            (":status", "200"),
                            ("content-length", "5"),
                        ])
                    if isinstance(event, StreamEnded):
                        conn.send_data(event.stream_id, b"ok-h2", end_stream=True)
                writer.write(conn.data_to_send())
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


async def test_ss2022_sync_tls_and_http2(tmp_path):
    origin, origin_port = await _start_h2_tls_origin(tmp_path)
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("offline-ss", "127.0.0.1", proxy_port, cipher, password)

    def rejected_certificate_request():
        with connector.create_sync_httpx_client(http2=True) as client:
            client.get(f"https://127.0.0.1:{origin_port}/")

    def request():
        with connector.create_sync_httpx_client(http2=True, verify=False) as client:
            response = client.get(f"https://127.0.0.1:{origin_port}/")
            assert response.http_version == "HTTP/2"
            assert response.text == "ok-h2"

    try:
        with pytest.raises(httpx.ConnectError):
            await _run_in_test_thread(rejected_certificate_request)
        await _run_in_test_thread(request)
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def test_ss2022_sync_raw_http_and_eight_client_concurrency_cleanup():
    baseline_workers = sum(
        t.name == "ss2022-sync-worker" and t.is_alive()
        for t in threading.enumerate()
    )
    baseline_fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    origin, origin_port = await _start_http_origin()
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("offline-ss", "127.0.0.1", proxy_port, cipher, password)

    def raw_request():
        stream = connector.open_sync_stream("127.0.0.1", origin_port, timeout=2)
        try:
            stream.sendall(b"GET / HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n")
            data = bytearray()
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            assert b"ok-ss" in data
        finally:
            stream.close()
            stream.close()

    def one_client():
        with connector.create_sync_httpx_client(timeout=2) as client:
            assert client.get(f"http://127.0.0.1:{origin_port}/").text == "ok-ss"

    try:
        await _run_in_test_thread(raw_request)
        await asyncio.gather(*(_run_in_test_thread(one_client) for _ in range(8)))
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()

    deadline = asyncio.get_running_loop().time() + 2
    while True:
        workers = sum(
            t.name == "ss2022-sync-worker" and t.is_alive()
            for t in threading.enumerate()
        )
        current_fds = len(os.listdir("/proc/self/fd")) if baseline_fds is not None else None
        if workers == baseline_workers and (current_fds is None or current_fds <= baseline_fds + 2):
            break
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"resources did not return to baseline: workers={workers}, fds={current_fds}")
        await asyncio.sleep(0.01)


def test_encode_addr_ipv4_roundtrip():
    raw = encode_addr("127.0.0.1", 8080)
    host, port, rest = _parse_socks_addr(raw)
    assert host == "127.0.0.1"
    assert port == 8080
    assert rest == b""


def test_encode_addr_domain():
    raw = encode_addr("api.example.com", 443)
    assert raw[0] == ATYP_DOMAIN
    n = raw[1]
    assert raw[2:2 + n] == b"api.example.com"
    assert struct.unpack("!H", raw[2 + n:4 + n])[0] == 443


async def test_aead_wrong_password_fails_closed():
    origin, origin_port = await _start_http_origin()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_aead_client(r, w, "aes-128-gcm", "right-pass")
    )
    conn = SSAEADConnection("aes-128-gcm", "wrong-pass", "127.0.0.1", proxy_port)
    try:
        await conn.connect("127.0.0.1", origin_port)
        await conn.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        with pytest.raises(SSAEADError):
            await asyncio.wait_for(conn.readall(), timeout=3)
    finally:
        await conn.close()
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()


async def test_ss2022_single_sync_client_reuses_keepalive_connection_and_cleans_resources():
    baseline_workers = sum(
        t.name == "ss2022-sync-worker" and t.is_alive()
        for t in threading.enumerate()
    )
    baseline_fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    accepts = 0
    requests = 0

    async def keepalive_origin(reader, writer):
        nonlocal accepts, requests
        accepts += 1
        try:
            while True:
                try:
                    headers = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                if not headers:
                    return
                requests += 1
                body = f"request-{requests}".encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    origin = await asyncio.start_server(keepalive_origin, "127.0.0.1", 0)
    origin_port = origin.sockets[0].getsockname()[1]
    cipher = "2022-blake3-aes-128-gcm"
    password = base64.urlsafe_b64encode(os.urandom(16)).decode()
    proxy, proxy_port = await _start_ss_proxy(
        lambda r, w: _handle_ss2022_client(r, w, cipher, password)
    )
    connector = SS2022Connector("offline-ss", "127.0.0.1", proxy_port, cipher, password)

    def two_requests_one_client():
        client = connector.create_sync_httpx_client(timeout=2)
        try:
            first = client.get(f"http://127.0.0.1:{origin_port}/one")
            second = client.get(f"http://127.0.0.1:{origin_port}/two")
            assert first.text == "request-1"
            assert second.text == "request-2"
        finally:
            client.close()
            client.close()

    try:
        await _run_in_test_thread(two_requests_one_client)
        assert accepts == 1
        assert requests == 2
    finally:
        proxy.close()
        origin.close()
        await proxy.wait_closed()
        await origin.wait_closed()

    deadline = asyncio.get_running_loop().time() + 2
    while True:
        workers = sum(
            t.name == "ss2022-sync-worker" and t.is_alive()
            for t in threading.enumerate()
        )
        current_fds = len(os.listdir("/proc/self/fd")) if baseline_fds is not None else None
        if workers == baseline_workers and (current_fds is None or current_fds <= baseline_fds + 2):
            break
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(
                f"keepalive resources did not return: workers={workers}, fds={current_fds}"
            )
        await asyncio.sleep(0.01)
