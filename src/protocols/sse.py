"""Shared, byte-oriented Server-Sent Events framing helpers.

SSE permits both LF (``\n\n``) and CRLF (``\r\n\r\n``) blank-line event
separators.  HTTP clients expose wire bytes unchanged, so protocol adapters must
not assume that CRLF has already been normalized for them.
"""

from __future__ import annotations


def _next_event_separator(buf: bytes) -> tuple[int, int]:
    """Return ``(offset, length)`` of the earliest complete SSE separator.

    ``(-1, 0)`` means that *buf* does not yet contain a complete event.  Keeping
    this byte-oriented avoids corrupting a partial UTF-8 sequence while callers
    buffer incremental network reads.
    """
    lf_at = buf.find(b"\n\n")
    crlf_at = buf.find(b"\r\n\r\n")
    if lf_at < 0 and crlf_at < 0:
        return -1, 0
    if crlf_at < 0 or (lf_at >= 0 and lf_at < crlf_at):
        return lf_at, 2
    return crlf_at, 4


def split_sse_events(buf: bytes) -> tuple[bytes, list[bytes]]:
    """Split all complete SSE event blocks from an incremental byte buffer.

    Returned blocks exclude their terminating blank line and retain their
    original internal line endings.  The remaining bytes are an incomplete tail
    to prepend to the next network chunk.
    """
    events: list[bytes] = []
    while True:
        separator_at, separator_len = _next_event_separator(buf)
        if separator_at < 0:
            break
        events.append(buf[:separator_at])
        buf = buf[separator_at + separator_len:]
    return buf, events
