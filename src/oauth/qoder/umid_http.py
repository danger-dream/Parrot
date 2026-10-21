"""Bounded issuer HTTP response framing (native request uses HTTP/1.0 close).

Chunked/other transfer encodings are not used by the observed issuer protocol;
reject them explicitly rather than treating framing bytes as a ticket envelope.
"""
import re
import time

MAX_BODY = 65536
MAX_HEAD = 8192


def read_response(stream, deadline: float) -> bytes:
    raw = bytearray()
    head = None
    expected = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        stream.settimeout(remaining)
        chunk = stream.recv(4096)
        if not chunk:
            break
        raw.extend(chunk)
        if head is None:
            boundary = raw.find(b"\r\n\r\n")
            if boundary < 0:
                if len(raw) > MAX_HEAD:
                    raise ValueError("Oversize issuer headers")
                continue
            if boundary > MAX_HEAD:
                raise ValueError("Oversize issuer headers")
            head, body = bytes(raw[:boundary]), raw[boundary + 4:]
            lines = head.split(b"\r\n")
            if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?", lines[0]):
                raise ValueError("Invalid issuer HTTP status")
            lengths = []
            for line in lines[1:]:
                name, sep, value = line.partition(b":")
                if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise ValueError("Invalid issuer HTTP header")
                if name.lower() == b"transfer-encoding":
                    raise ValueError("Unsupported issuer transfer encoding")
                if name.lower() == b"content-length":
                    value = value.strip()
                    if not re.fullmatch(rb"[0-9]+", value) or len(value) > 8:
                        raise ValueError("Invalid issuer content length")
                    lengths.append(int(value))
            if len(lengths) > 1 or (lengths and lengths[0] > MAX_BODY):
                raise ValueError("Invalid issuer content length")
            expected = lengths[0] if lengths else None
            raw = bytearray(body)
        if len(raw) > MAX_BODY or (expected is not None and len(raw) > expected):
            raise ValueError("Oversize issuer body")
    if head is None or (expected is not None and len(raw) != expected):
        raise ValueError("Truncated issuer response")
    return bytes(raw)
