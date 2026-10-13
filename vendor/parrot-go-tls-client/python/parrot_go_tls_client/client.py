"""Bounded, cancellable native h2 streams. Protocol parsing belongs to callers.

ClientHello/initial h2 settings target LS 2.22.0; dynamic equivalence and the
CloudCode business-host fingerprint are NOT established by the sample comparison.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path


class _PyBuffer(ctypes.Structure):
    """Stable Py_buffer layout; exports bytes/contiguous buffers without copying.

    Acquiring/releasing an export uses the Python C API with the GIL held, but is
    O(1). The potentially large copy happens only in ParrotStartRaw via CDLL.
    """
    _fields_ = [("buf", ctypes.c_void_p), ("obj", ctypes.c_void_p),
                ("len", ctypes.c_ssize_t), ("itemsize", ctypes.c_ssize_t),
                ("readonly", ctypes.c_int), ("ndim", ctypes.c_int),
                ("format", ctypes.c_char_p), ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
                ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
                ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)), ("internal", ctypes.c_void_p)]


_get_buffer = ctypes.pythonapi.PyObject_GetBuffer
_get_buffer.argtypes = [ctypes.py_object, ctypes.POINTER(_PyBuffer), ctypes.c_int]
_get_buffer.restype = ctypes.c_int
_release_buffer = ctypes.pythonapi.PyBuffer_Release
_release_buffer.argtypes = [ctypes.POINTER(_PyBuffer)]
_release_buffer.restype = None


class NativeError(RuntimeError):
    def __init__(self, event: dict):
        self.event = event
        self.code = event.get("code", "protocol")
        self.phase = event.get("phase", "setup")
        self.dispatched = bool(event.get("dispatched"))
        super().__init__(event.get("message") or self.code)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    duration_ms: int = 0
    error: str = ""
    headers_raw: list[tuple[bytes, bytes]] = field(default_factory=list)

    def json(self):
        return json.loads(self.body)


class NativeRequest:
    """Exactly one polling consumer; cancel/close may be called from another thread."""
    def __init__(self, client: Client, handle: int):
        self.client = client
        self.handle = handle
        self._close_lock = threading.Lock()
        self._terminal: dict | None = None
        self.dispatched = False

    def _remember(self, event: dict) -> dict:
        # Caller holds _close_lock. Never expose a mutable cached terminal.
        self.dispatched |= bool(event.get("dispatched"))
        result = dict(event)
        result["dispatched"] = self.dispatched
        return result

    def poll(self, wait_ms: int = 0) -> dict:
        with self._close_lock:
            if self._terminal is not None:
                return self._remember(self._terminal)
        # Do not hold the lock during a blocking poll: close/cancel must wake IO.
        event = self.client._call("ParrotPoll", self.handle, wait_ms)
        with self._close_lock:
            if self._terminal is not None:
                return self._remember(self._terminal)
            return self._remember(event)

    def cancel(self) -> None:
        self.client._lib.ParrotCancel(self.handle)

    def close(self) -> dict:
        with self._close_lock:
            if self._terminal is None:
                self.cancel()
                self._terminal = self._remember(self.client._call("ParrotCloseRequest", self.handle))
                with self.client._lock:
                    self.client._requests.pop(self.handle, None)
            return self._remember(self._terminal)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Client:
    """An explicitly owned handle, no global native connection cache.

    Each request owns its h2 connection (no invisible retry or redirect).
    Bodies are raw/compressed bytes: httpx adapters perform decoding once.
    """
    def __init__(self, lib_path: str | None = None, *, proxy: str = "",
                 proxy_auth: tuple[str, str] | None = None, insecure: bool = False,
                 ca_pem: str = ""):
        path = lib_path or os.environ.get("PARROT_GO_TLS_LIBRARY") or str(
            Path(__file__).with_name("libparrot_go_tls_client.so")
        )
        self._config = dict(lib_path=path, proxy=proxy, proxy_auth=proxy_auth,
                            insecure=insecure, ca_pem=ca_pem)
        self._lib = ctypes.CDLL(path)
        if not all(hasattr(self._lib, name) for name in ("ParrotBuildInfo", "ParrotStartRaw")):
            raise OSError("Go TLS native ABI 3 required (missing build-info/raw-body entry point)")
        signatures = {
            "ParrotBuildInfo": [],
            "ParrotNewClient": [ctypes.c_char_p],
            "ParrotStart": [ctypes.c_ulonglong, ctypes.c_char_p],
            "ParrotStartRaw": [ctypes.c_ulonglong, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t],
            "ParrotPoll": [ctypes.c_ulonglong, ctypes.c_int],
            "ParrotCloseRequest": [ctypes.c_ulonglong],
        }
        for name, args in signatures.items():
            fn = getattr(self._lib, name)
            fn.argtypes = args
            fn.restype = ctypes.c_void_p
        for name in ("ParrotCancel", "ParrotCloseClient"):
            fn = getattr(self._lib, name)
            fn.argtypes = [ctypes.c_ulonglong]
            fn.restype = None
        self._lib.ParrotFree.argtypes = [ctypes.c_void_p]
        self._lib.ParrotFree.restype = None
        self.native_info = self._call("ParrotBuildInfo")
        if self.native_info.get("abi") != 3 or self.native_info.get("version") != "0.2.1":
            raise OSError("Go TLS wrapper/native version or ABI mismatch")
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._requests: dict[int, NativeRequest] = {}
        self._closed = False
        self._handle = self._call("ParrotNewClient", json.dumps({
            "proxy": proxy, "proxy_auth": proxy_auth, "insecure": insecure,
            "ca_pem": ca_pem,
        }).encode())["id"]

    def _call(self, name: str, *args) -> dict:
        ptr = getattr(self._lib, name)(*args)
        if not ptr:
            raise RuntimeError(f"{name} returned null")
        try:
            result = json.loads(ctypes.string_at(ptr))
        finally:
            self._lib.ParrotFree(ptr)
        if result.get("kind") == "headers":
            # ABI 2 always supplies raw pairs, even for an empty header block.
            result["headers_raw"] = [tuple(base64.b64decode(v, validate=True) for v in pair)
                                     for pair in result["headers_b64"]]
        return result

    def start(self, method: str, url: str, *, headers=None, body: bytes = b"",
              timeout: float | None = None, connect_timeout: float | None = 15,
              read_timeout: float | None = None, write_timeout: float | None = 30,
              dial_ips: list[str] | None = None, require_resolved: bool = False) -> NativeRequest:
        """Start using small JSON metadata plus a borrowed contiguous body buffer.

        Go owns a complete copy on return; immutable bytes need no Python copy.
        Callers supplying mutable buffers must not modify them until this call
        returns. Client.close still waits for registration of any concurrent start.
        """
        items = list(headers.items()) if hasattr(headers, "items") else list(headers or [])
        def header_bytes(value):
            if isinstance(value, bytes):
                return value
            if isinstance(value, str):
                return value.encode("utf-8")  # Compatibility with the original string interface.
            raise TypeError("headers must contain bytes or str pairs")
        encoded_headers = [[base64.b64encode(header_bytes(v)).decode("ascii") for v in pair]
                           for pair in items]
        raw = json.dumps({
            "method": method, "url": url, "headers_b64": encoded_headers,
            "timeout_s": timeout or 0, "connect_timeout_s": -1 if connect_timeout is None else max(1e-9, connect_timeout),
            "read_timeout_s": read_timeout or 0, "write_timeout_s": write_timeout or 0,
            "dial_ips": dial_ips, "require_resolved": require_resolved,
        }).encode()
        exported = _PyBuffer()
        _get_buffer(b"" if body is None else body, ctypes.byref(exported), 0)  # PyBUF_SIMPLE
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("native client closed")
                # CDLL, not PyDLL: the body copy must not hold the Python GIL.
                # The export pins memory until Go has copied it into owned []byte.
                result = self._call("ParrotStartRaw", self._handle, raw, exported.buf, exported.len)
                if "id" not in result:
                    raise NativeError(result)
                stream = NativeRequest(self, result["id"])
                self._requests[stream.handle] = stream
                return stream
        finally:
            _release_buffer(ctypes.byref(exported))

    def request(self, method: str, url: str, *, headers=None, body: bytes = b"",
                timeout: float = 60, max_body_bytes: int = 128 << 20, **kwargs) -> Response:
        # Compatibility for the original convenience API; streaming callers use start().
        overrides = {key: kwargs.pop(key) for key in tuple(kwargs) if key in self._config}
        if overrides:
            config = {**self._config, **overrides}
            with Client(**config) as client:
                return client.request(method, url, headers=headers, body=body, timeout=timeout,
                                      max_body_bytes=max_body_bytes, **kwargs)
        with self.start(method, url, headers=headers, body=body, timeout=timeout, **kwargs) as stream:
            status, response_headers, raw_headers, chunks, size = 0, {}, [], [], 0
            while True:
                event = stream.poll(1000)
                kind = event["kind"]
                if kind == "error":
                    raise NativeError(event)
                if kind == "headers":
                    status, response_headers = event["status"], dict(event.get("headers", []))
                    raw_headers = event["headers_raw"]
                elif kind == "data":
                    chunk = base64.b64decode(event["data"])
                    size += len(chunk)
                    if size > max_body_bytes:
                        raise ValueError("response exceeded max_body_bytes")
                    chunks.append(chunk)
                elif kind == "eof":
                    return Response(status, response_headers, b"".join(chunks), event.get("duration_ms", 0),
                                    headers_raw=raw_headers)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, body: bytes = b"", **kwargs):
        return self.request("POST", url, body=body, **kwargs)

    def close(self):
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                streams = tuple(self._requests.values())
            # Cancel all first; then collect each final fact before native client
            # deletion. Registry entries are removed, not kept as global tombstones.
            for stream in streams:
                stream.cancel()
            try:
                for stream in streams:
                    stream.close()
            finally:
                self._lib.ParrotCloseClient(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
