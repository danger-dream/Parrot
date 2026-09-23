"""Business envelopes are decoded outside the raw-response signature owner."""
from __future__ import annotations
import json

# Documented ZCode business codes; unrelated service bodies remain untouched.
_CODES = {3001: (400, "invalid_request_error"), 3006: (404, "not_found_error"),
          3007: (401, "authentication_error"), 3002: (429, "rate_limit_error"),
          3008: (429, "rate_limit_error"), 3009: (429, "rate_limit_error"), 3010: (429, "rate_limit_error"),
          2007: (503, "overloaded_error")}


def business_error(value):
    if not isinstance(value, dict):
        return None
    error = value.get("error") if isinstance(value.get("error"), dict) else value
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        return None
    if code not in _CODES:
        return None
    status, kind = _CODES[code]
    return status, {"type": "error", "error": {"type": kind, "code": str(code), "message": "Zhipu business error " + str(code)}}


class BusinessContext:
    def __init__(self, inner):
        self.inner = inner

    async def __aenter__(self):
        response = await self.inner.__aenter__()
        try:
            if "application/json" in response.headers.get("content-type", "").lower():
                raw = await response.aread()
                try:
                    mapped = business_error(json.loads(raw))
                except (ValueError, UnicodeError):
                    mapped = None
                if mapped:
                    response.status_code = mapped[0]
                    response._content = json.dumps(mapped[1]).encode()
                    response.headers["content-length"] = str(len(response._content))
            return response
        except BaseException:
            await self.inner.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        return await self.inner.__aexit__(*args)


class BusinessStream:
    """Preserve normal SSE bytes; normalize vendor error frames, never replay."""
    def __init__(self):
        self.buffer = b""

    def feed(self, chunk):
        if not chunk:
            remaining, self.buffer = self.buffer, b""
            return remaining
        self.buffer += chunk
        out = []
        while True:
            end = self.buffer.find(b"\n\n")
            delimiter = 2
            crlf = self.buffer.find(b"\r\n\r\n")
            if crlf >= 0 and (end < 0 or crlf < end):
                end, delimiter = crlf, 4
            if end < 0:
                break
            raw, self.buffer = self.buffer[:end+delimiter], self.buffer[end+delimiter:]
            data = b"\n".join(line[5:].lstrip() for line in raw.splitlines() if line.startswith(b"data:"))
            try:
                mapped = business_error(json.loads(data))
            except (ValueError, UnicodeError):
                mapped = None
            out.append(b"event: error\ndata: " + json.dumps(mapped[1]).encode() + b"\n\n" if mapped else raw)
        # A non-SSE JSON response is restored through the normal JSON path.
        if self.buffer.lstrip().startswith(b"{"):
            remaining, self.buffer = self.buffer, b""
            out.append(remaining)
        return b"".join(out)
