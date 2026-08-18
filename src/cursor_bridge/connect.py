"""Connect protocol framing used by Cursor's HTTP/2 AgentService."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable

from .constants import CONNECT_END_STREAM_FLAG, MAX_CONNECT_FRAME_SIZE


def frame_connect_message(data: bytes, flags: int = 0) -> bytes:
    return struct.pack("!BI", flags, len(data)) + data


def decode_connect_unary_body(payload: bytes) -> bytes | None:
    if len(payload) < 5:
        return None
    offset = 0
    while offset + 5 <= len(payload):
        flags = payload[offset]
        message_length = struct.unpack_from("!I", payload, offset + 1)[0]
        frame_end = offset + 5 + message_length
        if frame_end > len(payload):
            return None
        if flags & 0b00000001:
            return None
        if (flags & CONNECT_END_STREAM_FLAG) == 0:
            return payload[offset + 5 : frame_end]
        offset = frame_end
    return None


def parse_connect_end_stream(data: bytes) -> str | None:
    try:
        payload = json.loads(data.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Failed to parse Connect end stream"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not error:
        return None
    code = error.get("code") or "unknown"
    message = error.get("message") or "Unknown error"
    return f"Connect error {code}: {message}"


class ConnectFrameParser:
    def __init__(
        self,
        on_message: Callable[[bytes], None],
        on_end_stream: Callable[[bytes], None],
    ) -> None:
        self._on_message = on_message
        self._on_end_stream = on_end_stream
        self._pending = bytearray()

    def feed(self, incoming: bytes) -> None:
        self._pending.extend(incoming)
        while len(self._pending) >= 5:
            flags = self._pending[0]
            msg_len = struct.unpack_from("!I", self._pending, 1)[0]
            if msg_len > MAX_CONNECT_FRAME_SIZE:
                self._pending.clear()
                self._on_end_stream(
                    json.dumps(
                        {
                            "error": {
                                "code": "frame_too_large",
                                "message": f"Frame size {msg_len} exceeds limit",
                            }
                        }
                    ).encode("utf-8")
                )
                return
            if len(self._pending) < 5 + msg_len:
                break
            message = bytes(self._pending[5 : 5 + msg_len])
            del self._pending[: 5 + msg_len]
            if flags & CONNECT_END_STREAM_FLAG:
                self._on_end_stream(message)
            else:
                self._on_message(message)
