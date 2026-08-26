from __future__ import annotations

import datetime
import ipaddress
import ssl
import threading
import time
from collections import deque

import pytest
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded

from src import network
from src.cursor_bridge import agent_pb2, h2stream, models
from src.cursor_bridge.client import CursorClient
from src.cursor_bridge.connect import frame_connect_message
from src.cursor_bridge.constants import CONNECT_END_STREAM_FLAG
from src.cursor_bridge.errors import CursorError, CursorTimeoutError
from src.cursor_bridge.h2stream import CursorH2Stream, H2Error, cursor_headers, unary_rpc
from src.cursor_bridge.session import CursorSession
from src.cursor_bridge.tool_dispatch import PendingExec
from src.oauth import cursor as cursor_provider


_CURSOR_THREAD_NAMES = {"cursor-h2-reader", "cursor-session", "cursor-heartbeat"}


def _wait_no_cursor_threads(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t.name in _CURSOR_THREAD_NAMES]
        if not alive:
            return
        time.sleep(0.01)
    assert not [t for t in threading.enumerate() if t.name in _CURSOR_THREAD_NAMES]


class ScriptedH2Stream:
    """Socket-like routed stream backed by a real server-side h2 state machine."""

    def __init__(self, *, status: int = 200, response: bytes = b"reply",
                 mode: str = "unary", alpn: str = "h2", poll_timeouts: int = 0) -> None:
        self.status = status
        self.response = response
        self.mode = mode
        self.alpn = alpn
        self.timeout = 0.5
        self.poll_timeouts = poll_timeouts
        self.closed = False
        self.close_count = 0
        self.server_hostname = ""
        self.headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.data_frame_sizes: list[int] = []
        self._responded = False
        self._incoming: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._server = H2Connection(config=H2Configuration(client_side=False, header_encoding="utf-8"))
        self._server.initiate_connection()
        self._queue(self._server.data_to_send())

    def start_tls(self, _context, *, server_hostname: str):
        self.server_hostname = server_hostname
        if self.mode == "tls_error":
            raise ssl.SSLError("scripted TLS failure")
        return self

    def selected_alpn_protocol(self) -> str | None:
        return self.alpn

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = 0.5 if timeout is None else timeout

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        events = self._server.receive_data(data)
        for event in events:
            if isinstance(event, RequestReceived):
                self.headers = list(event.headers)
                if self.mode == "session":
                    self._server.send_headers(event.stream_id, [(":status", str(self.status))])
            elif isinstance(event, DataReceived):
                self.body.extend(event.data)
                self.data_frame_sizes.append(len(event.data))
                self._server.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                if self.mode == "session" and self.response and not self._responded:
                    self._respond(event.stream_id, end_stream=True)
            elif isinstance(event, StreamEnded) and self.mode != "session":
                if self.mode == "eof":
                    self.closed = True
                elif self.mode == "protocol_error":
                    self._queue(b"not-http2")
                elif self.mode != "stall":
                    self._server.send_headers(event.stream_id, [(":status", str(self.status))])
                    self._respond(event.stream_id, end_stream=True)
        self._queue(self._server.data_to_send())

    def _respond(self, stream_id: int, *, end_stream: bool) -> None:
        self._responded = True
        if self.response:
            midpoint = max(1, len(self.response) // 2)
            pieces = [self.response[:midpoint], self.response[midpoint:]]
            pieces = [piece for piece in pieces if piece]
            for index, piece in enumerate(pieces):
                self._server.send_data(
                    stream_id, piece, end_stream=end_stream and index == len(pieces) - 1,
                )
        elif end_stream:
            self._server.end_stream(stream_id)

    def _queue(self, data: bytes) -> None:
        if not data:
            return
        with self._condition:
            self._incoming.append(data)
            self._condition.notify_all()

    def recv(self, _size: int) -> bytes:
        with self._condition:
            if self.poll_timeouts:
                self.poll_timeouts -= 1
                raise TimeoutError("scripted poll timeout")
            deadline = time.monotonic() + self.timeout
            while not self._incoming and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._condition.wait(remaining)
            if self._incoming:
                return self._incoming.popleft()
            return b""

    def close(self) -> None:
        with self._condition:
            self.close_count += 1
            self.closed = True
            self._condition.notify_all()


@pytest.mark.parametrize("route_kind", ["direct", "socks5", "ss2022"])
def test_routed_connectors_feed_real_h2_and_preserve_context(monkeypatch, route_kind):
    routed = ScriptedH2Stream(response=route_kind.encode())
    calls = []

    def open_route(host, port, **kwargs):
        calls.append((host, port, kwargs))
        return routed

    monkeypatch.setattr(network, "open_sync_stream", open_route)
    result = unary_rpc(
        "/test.Unary", "token", b"request", account_key="acct",
        channel_key="oauth:acct", model="cursor-model", purpose="oauth_cursor",
    )
    assert result == route_kind.encode()
    assert len(calls) == 1
    host, port, context = calls[0]
    assert (host, port) == ("api2.cursor.sh", 443)
    assert context == {
        "timeout": 15.0,
        "proxy_purpose": "oauth_cursor",
        "proxy_channel": "oauth:acct",
        "proxy_model": "cursor-model",
    }
    assert routed.body == b"request"
    assert dict(routed.headers)[":path"] == "/test.Unary"
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def test_unary_h2_chunks_flow_control_and_closes_once(monkeypatch):
    routed = ScriptedH2Stream(response=b"response")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    body = b"x" * 70_000
    assert unary_rpc("/test.Flow", "token", body, timeout_s=1) == b"response"
    assert routed.body == body
    assert max(routed.data_frame_sizes) <= 16_384
    assert len(routed.data_frame_sizes) >= 5
    assert routed.close_count == 1
    _wait_no_cursor_threads()


@pytest.mark.parametrize(
    ("mode", "status", "expected"),
    [
        ("unary", 503, CursorError),
        ("stall", 200, CursorTimeoutError),
        ("eof", 200, CursorError),
        ("protocol_error", 200, CursorError),
    ],
)
def test_unary_error_paths_close_once_and_leave_no_reader(monkeypatch, mode, status, expected):
    routed = ScriptedH2Stream(mode=mode, status=status)
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    with pytest.raises(expected):
        unary_rpc("/test.Error", "token", b"body", timeout_s=0.08)
    assert routed.close_count == 1
    _wait_no_cursor_threads()


@pytest.mark.parametrize(
    ("routed", "expected"),
    [
        (ScriptedH2Stream(mode="tls_error"), ssl.SSLError),
        (ScriptedH2Stream(alpn="http/1.1"), H2Error),
    ],
)
def test_open_tls_and_alpn_failure_close_raw_stream(monkeypatch, routed, expected):
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    stream = CursorH2Stream()
    with pytest.raises(expected):
        stream.open(cursor_headers(path="/x", access_token="t", content_type="application/proto"))
    stream.close()
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def test_h2_recv_poll_close_and_double_close(monkeypatch):
    routed = ScriptedH2Stream(mode="stall")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    stream = CursorH2Stream()
    stream.open(cursor_headers(path="/poll", access_token="t", content_type="application/proto"))
    assert stream.read(timeout=0.02) is None
    stream.close()
    stream.close()
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def test_h2_reader_survives_poll_timeout_and_reads_following_data(monkeypatch):
    routed = ScriptedH2Stream(response=b"after-timeout", poll_timeouts=1)
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)

    assert unary_rpc("/test.Poll", "token", b"request", timeout_s=1) == b"after-timeout"
    assert routed.poll_timeouts == 0
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def _session_response(text: str = "hello") -> bytes:
    message = agent_pb2.AgentServerMessage(
        interaction_update=agent_pb2.InteractionUpdate(
            text_delta=agent_pb2.TextDeltaUpdate(text=text),
        )
    )
    return (
        frame_connect_message(message.SerializeToString())
        + frame_connect_message(b"{}", flags=CONNECT_END_STREAM_FLAG)
    )


def test_cursor_client_to_session_to_h2_success_preserves_route_context(monkeypatch):
    routed = ScriptedH2Stream(mode="session", response=_session_response())
    calls = []

    def open_route(host, port, **kwargs):
        calls.append(kwargs)
        return routed

    monkeypatch.setattr(network, "open_sync_stream", open_route)
    client = CursorClient(
        "token", account_key="account-1", channel_key="oauth:account-1",
        max_retries=0, request_timeout_s=2,
    )
    result = client.chat_completions(
        model="cursor-model", messages=[{"role": "user", "content": "hi"}], stream=False,
    )
    assert result["choices"][0]["message"]["content"] == "hello"
    assert calls == [{
        "timeout": 15.0,
        "proxy_purpose": "oauth_cursor",
        "proxy_channel": "oauth:account-1",
        "proxy_model": "cursor-model",
    }]
    assert dict(routed.headers)[":path"].endswith("AgentService/Run")
    assert routed.close_count == 1
    client.close()
    _wait_no_cursor_threads()


def _new_session() -> CursorSession:
    return CursorSession(
        access_token="token", request_bytes=b"request", blob_store={}, mcp_tools=[],
        enabled_tools=set(), cloud_rule=None, account_key="acct",
        channel_key="oauth:acct", model="model",
    )


def test_session_heartbeat_is_sent_and_close_is_interruptible(monkeypatch):
    from src.cursor_bridge import session as session_module

    routed = ScriptedH2Stream(mode="session", response=b"")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    monkeypatch.setattr(session_module, "HEARTBEAT_INTERVAL_S", 0.02)
    session = _new_session()
    deadline = time.monotonic() + 1
    while len(routed.data_frame_sizes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(routed.data_frame_sizes) >= 2  # initial Run frame plus heartbeat
    started = time.monotonic()
    session.close()
    session.close()
    assert time.monotonic() - started < 0.5
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def test_session_cancel_sends_action_and_stops_all_threads(monkeypatch):
    routed = ScriptedH2Stream(mode="session", response=b"")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    session = _new_session()
    deadline = time.monotonic() + 1
    while not routed.body and time.monotonic() < deadline:
        time.sleep(0.01)
    before = len(routed.body)
    session.cancel()
    assert len(routed.body) > before
    done = session.next(timeout=0.2)
    assert done.type == "done" and done.error == "session closed"
    assert routed.close_count == 1
    _wait_no_cursor_threads()


def test_session_pending_tool_close_emits_one_done_and_preserves_reason(monkeypatch):
    routed = ScriptedH2Stream(mode="session", response=b"")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: routed)
    session = _new_session()
    deadline = time.monotonic() + 1
    while not routed.body and time.monotonic() < deadline:
        time.sleep(0.01)
    assert routed.body
    session.pending_execs.append(PendingExec(
        exec_id="exec", exec_msg_id=1, tool_call_id="call", tool_name="tool",
        decoded_args="{}",
    ))
    routed.close()
    done = session.next(timeout=1)
    assert done.type == "done"
    assert done.error == "session closed with pending tool calls"
    session.close()
    assert session.events.empty()
    _wait_no_cursor_threads()


def test_session_start_failure_and_inactivity_timeout_cleanup(monkeypatch):
    from src.cursor_bridge import session as session_module

    failed = ScriptedH2Stream(mode="tls_error")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: failed)
    session = _new_session()
    done = session.next(timeout=1)
    assert done.type == "done" and "TLS failure" in (done.error or "")
    session.close()
    assert failed.close_count == 1
    _wait_no_cursor_threads()

    stalled = ScriptedH2Stream(mode="stall")
    monkeypatch.setattr(network, "open_sync_stream", lambda *args, **kwargs: stalled)
    monkeypatch.setattr(session_module, "INACTIVITY_THINKING_S", 0.03)
    session = _new_session()
    done = session.next(timeout=1)
    assert done.type == "done" and done.retry_hint == "timeout"
    session.close()
    assert stalled.close_count == 1
    _wait_no_cursor_threads()


def test_model_provider_propagates_account_and_never_fakes_failed_catalog(monkeypatch):
    seen = {}

    def list_models(token, **kwargs):
        seen.update(token=token, **kwargs)
        return []

    monkeypatch.setattr(cursor_provider, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(cursor_provider, "list_cursor_models", list_models)
    catalog = cursor_provider.fetch_model_catalog_sync(
        "token", timeout=0.25, account_key="acct",
    )
    assert catalog["models"] == []
    assert seen == {
        "token": "token", "include_hidden": False, "use_model_parameters": True,
        "timeout_s": 0.25, "account_key": "acct", "channel_key": "oauth:acct",
    }

    def failed(*args, **kwargs):
        raise TimeoutError("catalog deadline")

    monkeypatch.setattr(cursor_provider, "list_cursor_models", failed)
    with pytest.raises(TimeoutError, match="catalog deadline"):
        cursor_provider.fetch_model_catalog_sync("token", timeout=0.01, account_key="acct")


def test_model_unary_signature_carries_catalog_context(monkeypatch):
    seen = {}

    def rpc(path, token, body, **kwargs):
        seen.update(path=path, token=token, body=body, **kwargs)
        return b""

    monkeypatch.setattr(models, "unary_rpc", rpc)
    assert models.list_cursor_models(
        "token", timeout_s=0.2, account_key="acct", channel_key="oauth:acct",
    ) == []
    assert seen["account_key"] == "acct"
    assert seen["channel_key"] == "oauth:acct"
    assert seen["purpose"] == "oauth_cursor"
    assert seen["timeout_s"] == 0.2


def _create_server_context(tmp_path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False).sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_alpn_protocols(["h2"])
    return context


def test_direct_connector_real_local_tls_h2(monkeypatch, tmp_path):
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    server_context = _create_server_context(tmp_path)
    captured = {}

    def serve():
        raw, _ = listener.accept()
        with server_context.wrap_socket(raw, server_side=True) as sock:
            captured["alpn"] = sock.selected_alpn_protocol()
            conn = H2Connection(config=H2Configuration(client_side=False, header_encoding="utf-8"))
            conn.initiate_connection()
            sock.sendall(conn.data_to_send())
            body = bytearray()
            while True:
                data = sock.recv(65535)
                if not data:
                    return
                for event in conn.receive_data(data):
                    if isinstance(event, RequestReceived):
                        captured["headers"] = list(event.headers)
                    elif isinstance(event, DataReceived):
                        body.extend(event.data)
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, StreamEnded):
                        captured["body"] = bytes(body)
                        conn.send_headers(event.stream_id, [(":status", "200")])
                        conn.send_data(event.stream_id, b"local-h2", end_stream=True)
                pending = conn.data_to_send()
                if pending:
                    sock.sendall(pending)

    thread = threading.Thread(target=serve, name="cursor-local-h2-origin")
    thread.start()
    real_create_default_context = ssl.create_default_context

    def client_context():
        context = real_create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    monkeypatch.setattr(ssl, "create_default_context", client_context)
    monkeypatch.setattr(network, "_configured_proxy_chain_or_none", lambda **kwargs: None)
    monkeypatch.setattr(network, "active_socks5_url", lambda: None)
    stream = CursorH2Stream(
        host="127.0.0.1", port=port, account_key="acct",
        channel_key="oauth:acct", model="model",
    )
    try:
        stream.open(
            [(":method", "POST"), (":authority", "127.0.0.1"), (":scheme", "https"),
             (":path", "/direct"), ("content-type", "application/proto")],
            b"direct-body", end_stream=True,
        )
        assert stream.collect_unary(timeout_s=1) == b"local-h2"
    finally:
        stream.close()
        listener.close()
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert captured["alpn"] == "h2"
    assert captured["body"] == b"direct-body"
    assert dict(captured["headers"])[":path"] == "/direct"
    _wait_no_cursor_threads()
