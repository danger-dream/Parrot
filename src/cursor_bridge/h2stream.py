"""Minimal HTTP/2 client for Cursor Connect RPCs."""

from __future__ import annotations

import socket
import ssl
import threading
import time
import uuid
from collections.abc import Iterable
from queue import Empty, Queue

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    ResponseReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)
from h2.exceptions import StreamClosedError

from .constants import (
    CONNECT_TIMEOUT_S,
    CONNECT_USER_AGENT,
    CURSOR_API_HOST,
    CURSOR_CLIENT_TYPE,
    CURSOR_CLIENT_VERSION,
    SOCKET_RECV_POLL_S,
    UNARY_RPC_TIMEOUT_S,
)
from .errors import CursorTimeoutError, classify_cursor_failure


def _traceparent() -> str:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return f"00-{trace_id}-{span_id}-01"


def cursor_headers(
    *,
    path: str,
    access_token: str,
    content_type: str,
    extra: list[tuple[str, str]] | None = None,
    request_id: str | None = None,
) -> list[tuple[str, str]]:
    """Headers that match pi-cursor's official-CLI camouflage set."""

    request_id = request_id or str(uuid.uuid4())
    trace = _traceparent()
    headers = [
        (":method", "POST"),
        (":authority", CURSOR_API_HOST),
        (":scheme", "https"),
        (":path", path),
        ("content-type", content_type),
        ("user-agent", CONNECT_USER_AGENT),
        ("authorization", f"Bearer {access_token}"),
        ("x-ghost-mode", "true"),
        ("x-cursor-client-version", CURSOR_CLIENT_VERSION),
        ("x-cursor-client-type", CURSOR_CLIENT_TYPE),
        ("x-request-id", request_id),
        ("x-original-request-id", request_id),
        ("traceparent", trace),
        ("backend-traceparent", trace),
        ("connect-protocol-version", "1"),
        ("te", "trailers"),
    ]
    if extra:
        headers.extend(extra)
    return headers


def cursor_http_headers(
    access_token: str,
    *,
    content_type: str | None = "application/json",
    request_id: str | None = None,
) -> dict[str, str]:
    """Same camouflage set for HTTP/1.1 auth and dashboard JSON calls."""

    request_id = request_id or str(uuid.uuid4())
    headers = {
        "user-agent": CONNECT_USER_AGENT,
        "x-ghost-mode": "true",
        "x-cursor-client-version": CURSOR_CLIENT_VERSION,
        "x-cursor-client-type": CURSOR_CLIENT_TYPE,
        "x-request-id": request_id,
        "x-original-request-id": request_id,
        "connect-protocol-version": "1",
    }
    if access_token:
        headers["authorization"] = f"Bearer {access_token}"
    if content_type:
        headers["content-type"] = content_type
    return headers


class H2Error(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CursorH2Stream:
    """One bidirectional HTTP/2 stream plus its parent connection."""

    def __init__(
        self,
        host: str = CURSOR_API_HOST,
        port: int = 443,
        timeout_s: float = CONNECT_TIMEOUT_S,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._sock: ssl.SSLSocket | None = None
        self._conn: H2Connection | None = None
        self._stream_id: int | None = None
        self._lock = threading.RLock()
        self._incoming: Queue[tuple[str, bytes]] = Queue()
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._status: int | None = None
        self._pending_out = bytearray()
        self._pending_end = False

    @property
    def status(self) -> int | None:
        return self._status

    def open(self, headers: list[tuple[str, str]], initial_body: bytes = b"", *, end_stream: bool = False) -> None:
        context = ssl.create_default_context()
        context.set_alpn_protocols(["h2"])
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        sock = context.wrap_socket(raw, server_hostname=self.host)
        if sock.selected_alpn_protocol() != "h2":
            sock.close()
            raise H2Error(f"ALPN negotiated {sock.selected_alpn_protocol()!r}, expected h2")
        sock.settimeout(SOCKET_RECV_POLL_S)
        config = H2Configuration(client_side=True, header_encoding="utf-8")
        conn = H2Connection(config=config)
        conn.initiate_connection()
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(stream_id, headers, end_stream=end_stream and not initial_body)
        sock.sendall(conn.data_to_send())
        self._sock = sock
        self._conn = conn
        self._stream_id = stream_id
        self._reader = threading.Thread(target=self._read_loop, name="cursor-h2-reader", daemon=True)
        self._reader.start()
        if initial_body or (end_stream and initial_body):
            self.write(initial_body, end_stream=end_stream)
        elif end_stream:
            self.write(b"", end_stream=True)

    def write(self, data: bytes, *, end_stream: bool = False) -> None:
        if self._closed.is_set() or self._conn is None or self._stream_id is None or self._sock is None:
            return
        with self._lock:
            if data:
                self._pending_out.extend(data)
            if end_stream:
                self._pending_end = True
            try:
                self._flush_outbound_locked()
            except (OSError, StreamClosedError) as exc:
                self._incoming.put(("error", str(exc).encode("utf-8")))
                self.close()

    def read(self, timeout: float | None = None) -> tuple[str, bytes] | None:
        try:
            return self._incoming.get(timeout=timeout)
        except Empty:
            return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        sock = self._sock
        conn = self._conn
        if sock is not None and conn is not None:
            try:
                with self._lock:
                    conn.close_connection()
                    self._sendall_locked(conn.data_to_send())
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._incoming.put(("closed", b""))

    def _sendall_locked(self, payload: bytes) -> None:
        if not payload or self._sock is None:
            return
        self._sock.settimeout(self.timeout_s)
        try:
            self._sock.sendall(payload)
        finally:
            self._sock.settimeout(SOCKET_RECV_POLL_S)

    def _flush_outbound_locked(self) -> None:
        conn = self._conn
        stream_id = self._stream_id
        if conn is None or stream_id is None or self._sock is None:
            return
        remaining = bytes(self._pending_out)
        ended = False
        while remaining:
            window = conn.local_flow_control_window(stream_id)
            max_size = conn.max_outbound_frame_size
            chunk_size = min(window, max_size, len(remaining))
            if chunk_size <= 0:
                break
            chunk = remaining[:chunk_size]
            remaining = remaining[chunk_size:]
            finish = self._pending_end and not remaining
            conn.send_data(stream_id, chunk, end_stream=finish)
            if finish:
                ended = True
        self._pending_out = bytearray(remaining)
        if self._pending_end and not remaining and not ended:
            conn.end_stream(stream_id)
            ended = True
        if ended:
            self._pending_end = False
        self._sendall_locked(conn.data_to_send())

    def _read_loop(self) -> None:
        assert self._sock is not None
        assert self._conn is not None
        while not self._closed.is_set():
            try:
                chunk = self._sock.recv(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                try:
                    events = self._conn.receive_data(chunk)
                except Exception as exc:  # noqa: BLE001 - transport errors become stream errors
                    self._incoming.put(("error", str(exc).encode("utf-8")))
                    break
                for event in events:
                    if isinstance(event, ResponseReceived):
                        for name, value in event.headers:
                            if name == ":status":
                                try:
                                    self._status = int(value)
                                except ValueError:
                                    self._status = None
                    elif isinstance(event, DataReceived):
                        self._incoming.put(("data", event.data))
                        self._conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, StreamEnded):
                        self._incoming.put(("end", b""))
                    elif isinstance(event, (StreamReset, ConnectionTerminated)):
                        self._incoming.put(("error", b"http2 stream reset"))
                    elif isinstance(event, WindowUpdated):
                        try:
                            self._flush_outbound_locked()
                        except (OSError, StreamClosedError) as exc:
                            self._incoming.put(("error", str(exc).encode("utf-8")))
                            self._closed.set()
                            break
                try:
                    pending = self._conn.data_to_send()
                    if pending:
                        self._sendall_locked(pending)
                except OSError:
                    break
        self._incoming.put(("closed", b""))

    def collect_unary(self, timeout_s: float = UNARY_RPC_TIMEOUT_S) -> bytes:
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout_s
        ended = False
        while time.monotonic() < deadline:
            item = self.read(timeout=max(0.05, deadline - time.monotonic()))
            if item is None:
                continue
            kind, payload = item
            if kind == "data":
                chunks.append(payload)
            elif kind in {"end", "closed"}:
                ended = True
                break
            elif kind == "error":
                raise H2Error(payload.decode("utf-8", errors="replace"), status=self._status)
        if not ended:
            raise CursorTimeoutError(f"Cursor unary RPC timed out after {timeout_s:.0f}s")
        if self._status not in (None, 200):
            raise H2Error(f"unary RPC HTTP {self._status}", status=self._status)
        return b"".join(chunks)


def unary_rpc(path: str, access_token: str, body: bytes, *, timeout_s: float = UNARY_RPC_TIMEOUT_S) -> bytes:
    stream = CursorH2Stream(timeout_s=min(timeout_s, CONNECT_TIMEOUT_S))
    try:
        stream.open(
            cursor_headers(path=path, access_token=access_token, content_type="application/proto"),
            body,
            end_stream=True,
        )
        return stream.collect_unary(timeout_s=timeout_s)
    except H2Error as exc:
        raise classify_cursor_failure(str(exc), http_status=exc.status) from exc
    finally:
        stream.close()


def iter_until_closed(stream: CursorH2Stream) -> Iterable[tuple[str, bytes]]:
    while True:
        item = stream.read(timeout=1.0)
        if item is None:
            if stream._closed.is_set():
                return
            continue
        yield item
        if item[0] in {"end", "closed", "error"}:
            return
