"""Business envelopes are decoded outside the raw-response signature owner."""
from __future__ import annotations
import json
import re

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


def stream_phase(*, iterator_created, emitted_boundary=False, status=0, error_chunk=False, content_type=""):
    """3.15.3 zHs order; HTTP rejection wins over an SSE content-type."""
    if not iterator_created:
        return "request_setup"
    if emitted_boundary or 200 <= status < 300:
        return "response_body"
    if 300 <= status < 600:
        return "request_setup"
    if error_chunk or "text/event-stream" in content_type.lower():
        return "response_body"
    return "request_setup"


def _error_values(value):
    """Read error identity only, never request IDs or arbitrary metadata."""
    if isinstance(value, (str, int, float)):
        return [str(value).lower()]
    if not isinstance(value, dict):
        return []
    records = [value]
    for name in ("error", "data"):
        if isinstance(value.get(name), dict):
            records.append(value[name])
    fields = ("error", "code", "error_code", "providerCode", "providerErrorCode", "type",
              "msg", "message", "reason", "providerMessage", "providerErrorMessage")
    return [str(record[field]).lower() for record in records for field in fields
            if isinstance(record.get(field), (str, int, float))]


def quota_rejection(value):
    """Quota, entitlement and throttling are not proof of a rejected model key."""
    return any(
        re.search(r"(?<!\d)(?:1310|1311|3002|3008|3009|3010)(?!\d)", text)
        or any(word in text for word in (
            "quota", "rate_limit", "rate limit", "usage_limit", "usage limit",
            "entitlement", "额度", "配额", "限流", "使用上限"))
        for text in _error_values(value)
    )


def other_auth_rejection(value):
    return quota_rejection(value) or any(
        "verify_" in text or re.search(r"(?<!\d)3007(?!\d)", text) is not None
        or "captcha" in text or "验证码" in text
        for text in _error_values(value)
    )


def replayable_rejection(result):
    observed = (getattr(result, "translator_ctx", None) or {}).get("zhipu_auth") or {}
    return bool(observed.get("resolved") and observed.get("status") == 401
        and observed.get("phase") == "request_setup" and not observed.get("security")
        and not result.stream_started and not observed.get("output_committed")
        and not observed.get("emitted_boundary") and result.http_status == 401)


class BusinessContext:
    def __init__(self, inner, observation=None):
        self.inner = inner
        self.observation = observation

    async def __aenter__(self):
        response = await self.inner.__aenter__()
        try:
            if self.observation is not None:
                self.observation.update(status=response.status_code,
                    phase=stream_phase(iterator_created=True, status=response.status_code,
                                       content_type=response.headers.get("content-type", "")))
                if response.status_code == 401:
                    raw = await response.aread()
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        value = raw.decode("utf-8", errors="replace")
                    self.observation["security"] = other_auth_rejection(value)
                    self.observation["quota"] = quota_rejection(value)
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
