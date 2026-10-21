"""Qoder wire adaptation: signed/encoded requests, normalized failures and its SSE envelope."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import replace
from http import HTTPStatus

from ..oauth.qoder import common, signing, device_identity, routes

QUEUED = "10605"
# Codes Qoder's client counts as a plan's usage or credits used up.
QUOTA_CODES = frozenset({"110", "112", "113", "114", "115", "116", "117", "118", "119", "122"})
# Refusals of this request, not of the account.
REQUEST_CODES = frozenset({"406", "416", "430", "80411", "80412", "100400", "100401", "100403"})
LOGIN_EXPIRED = "105"
BUSY_WAIT = 30


def _count(value) -> int:
    number = common.number(value.strip() if isinstance(value, str) else value)
    return int(number) if number else 0


def _json(text):
    try:
        return json.loads(text.strip().lstrip("\ufeff"))
    except (ValueError, AttributeError):
        return None


def login_expired(code: str, message) -> bool:
    return code == LOGIN_EXPIRED or (isinstance(message, str) and message.strip().lower() == "login expired")


def failure(status: int, text: str) -> dict:
    """Qoder's own code says what a failure is, as its client reads it.

    Only an unqualified HTTP 401 or 105 / "login expired" triggers OAuth
    recovery. Device/permission refusals must not spend a one-use refresh token.
    A queued model (10605, often a 403) is busy, never an authentication failure.
    """
    if status < 400 or status > 599:
        status = 502
    message = str(text or "").strip()
    code = ""
    queue = None
    from ..management_control.public_safety import redact_known_fields
    body = _json(message)
    if isinstance(body, dict):
        body = redact_known_fields(body)
        if isinstance(body.get("error"), dict):
            body = {**body, **body["error"]}
        outer = "" if body.get("code") is None else str(body.get("code"))
        if outer in {"", "401", "403"} and isinstance(body.get("message"), str):
            inner = _json(body["message"])
            if isinstance(inner, dict) and str(inner.get("code")) == QUEUED:
                body = inner
        code = "" if body.get("code") is None else str(body.get("code"))
        if body.get("message"):
            message = str(body["message"])
        details = body.get("details")
        details = _json(details) if isinstance(details, str) else details
        if isinstance(details, dict) and isinstance(details.get("error"), dict) and details["error"].get("message"):
            message += ": " + str(details["error"]["message"])
        queue = body.get("queue") if isinstance(body.get("queue"), dict) else None
        if queue is None and isinstance(body.get("message"), str):
            candidate = _json(body["message"])
            if isinstance(candidate, dict) and ({"isQueued", "serviceAvailable", "retryAfterSeconds"} & candidate.keys()):
                queue = candidate
    if not message:
        try:
            message = HTTPStatus(status).phrase
        except ValueError:
            message = f"HTTP {status}"
    # Upstream free text may echo credentials. Keep classification from it, but
    # emit only our own bounded message and an observed numeric business code.
    expired = login_expired(code, message)
    quota_message = "quota" in message.lower()
    code = code if re.fullmatch(r"[0-9]{1,8}", code) else ""
    message = "upstream rejected this request"
    if code == QUEUED or (queue or {}).get("isQueued") is True or (queue or {}).get("serviceAvailable") is False:
        wait = (queue or {}).get("retryAfterSeconds")
        wait = int(-(-wait // 1)) if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0 else BUSY_WAIT
        model = ""
        return {"status": 503, "retry_after": wait, "error": {"type": "overloaded_error", "code": QUEUED,
                "message": f"Qoder is busy{model}; try again in {wait}s"}}
    if code in QUOTA_CODES:
        return {"status": 429, "error": {"type": "insufficient_quota", "code": code, "message": "Qoder usage limit reached: " + message}}
    if code in REQUEST_CODES:
        return {"status": 400, "error": {"type": "invalid_request_error", "code": code, "param": "messages",
                "message": f"Qoder refused the request ({code}): {message}"}}
    if expired or (status == 401 and code in {"", "401"}):
        return {"status": 401, "error": {"type": "authentication_error", "code": code or str(status),
                "message": "Qoder sign-in was refused: " + message}}
    if status in (401, 403):
        return {"status": 403, "error": {"type": "permission_error", "code": code, "message": "Qoder refused the account: " + message}}
    if status == 429 or quota_message:
        return {"status": 429, "error": {"type": "rate_limit_error", "code": code or "429", "message": "Qoder usage limit reached: " + message}}
    return {"status": status, "error": {"type": "upstream_error", "code": code or str(status), "message": message}}


class QoderWire:
    """The physical send: encode and sign the plaintext body, normalize an HTTP failure."""

    def __init__(self, request, factory, account: dict, account_key: str = ""):
        self.request, self.factory, self.account = request, factory, account
        self.account_key = account_key
        self.ctx = None
        self.route = None

    async def __aenter__(self):
        content = self.request.content
        plain = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else str(content or "")
        wire = signing.encode_body(plain)
        headers = {k: v for k, v in self.request.headers.items()
                   if not k.lower().startswith("cosy-") and k.lower() not in {"authorization", "content-length", "login-version"}}
        with routes.bind(self.route):
            device = await asyncio.to_thread(device_identity.headers, self.account, account_key=self.account_key)
        headers.update(signing.cosy_headers(self.request.url, self.account, wire, device=device))
        headers["Accept"] = "text/event-stream"
        self.ctx = self.factory(replace(self.request, headers=headers, content=wire.encode("ascii")))
        response = await self.ctx.__aenter__()
        try:
            if response.status_code >= 400:
                raw = await response.aread()
                mapped = failure(response.status_code, raw.decode("utf-8", errors="replace"))
                response.status_code = mapped["status"]
                response._content = json.dumps({"error": mapped["error"]}, ensure_ascii=False).encode()
                response.headers["content-type"] = "application/json"
                response.headers["content-length"] = str(len(response._content))
                if mapped.get("retry_after"):
                    response.headers["retry-after"] = str(mapped["retry_after"])
            return response
        except BaseException:
            await self.ctx.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        return await self.ctx.__aexit__(*args) if self.ctx is not None else None


# ---- tool calls Qoder writes into its text ---------------------------------------

CALL_OPEN = "<tool_call>"
CALL_CLOSE = "</tool_call>"
_FUNC = re.compile(r"<function=([^>]+)>([\s\S]*?)</function>")
_PARAM = re.compile(r"<parameter=([^>]+)>([\s\S]*?)</parameter>")
CUT_SHORT = "Qoder's tool call, written as text, was cut short; it is not sent on as the answer"
UNREADABLE = "Qoder's tool call, written as text, can't be read; it is not sent on as the answer"


def parse_call(value: str) -> tuple[str, str] | None:
    """One tool-call block: JSON {name, arguments}, or the XML function/parameter form."""
    try:
        parsed = json.loads(value.strip())
        if (isinstance(parsed, dict) and isinstance(parsed.get("name"), str) and parsed["name"].strip()
                and isinstance(parsed.get("arguments"), dict)):
            return parsed["name"].strip(), json.dumps(parsed["arguments"], ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        pass
    match = _FUNC.search(value)
    if not match or not match.group(1).strip():
        return None
    arguments = {}
    for param in _PARAM.finditer(match.group(2)):
        key = param.group(1).strip()
        if not key:
            continue
        raw = param.group(2).strip()
        try:
            arguments[key] = json.loads(raw)
        except ValueError:
            arguments[key] = raw
    return match.group(1).strip(), json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _quoted(text: str) -> bool:
    """The text ends inside a code fence or inline code span: a <tool_call> there is prose."""
    if text.count("```") % 2:
        return True
    line = text[text.rfind("\n") + 1:].replace("```", "")
    return line.count("`") % 2 == 1


def _callish(value: str) -> bool:
    return value.lstrip().startswith("{") or "<function=" in value


class Splitter:
    """Hold text until a whole tool-call block has come, then give it as a call.

    A block on its way to being a call that can't be read, or that the stream
    ends inside, is an error, never the answer, so the turn can be retried.
    """

    def __init__(self):
        self.text_buf = ""
        self.call_buf = ""
        self.in_call = False
        self.saw_tool = False
        self.sent = ""

    def _text(self, out, value):
        self.sent += value
        out.append(("text", value))

    def feed(self, value: str) -> list[tuple[str, object]]:
        out = []
        while value:
            if not self.in_call:
                combined, self.text_buf = self.text_buf + value, ""
                index = combined.find(CALL_OPEN)
                if index < 0:
                    keep = next((n for n in range(len(CALL_OPEN) - 1, 0, -1) if combined.endswith(CALL_OPEN[:n])), 0)
                    if keep < len(combined):
                        self._text(out, combined[:len(combined) - keep])
                    self.text_buf = combined[len(combined) - keep:]
                    break
                if index > 0:
                    self._text(out, combined[:index])
                value = combined[index + len(CALL_OPEN):]
                self.in_call = True
                continue
            combined, self.call_buf = self.call_buf + value, ""
            index = combined.find(CALL_CLOSE)
            if index < 0:
                self.call_buf = combined
                break
            body = combined[:index]
            call = parse_call(body)
            if call and not _quoted(self.sent):
                self.saw_tool = True
                out.append(("call", call))
            elif _callish(body) and not _quoted(self.sent):
                self.in_call = False
                out.append(("error", UNREADABLE))
                return out
            else:
                self._text(out, CALL_OPEN + body + CALL_CLOSE)
            value = combined[index + len(CALL_CLOSE):]
            self.in_call = False
        return out

    def flush(self) -> list[tuple[str, object]]:
        if self.in_call:
            body, self.in_call, self.call_buf = self.call_buf, False, ""
            if _callish(body) and not _quoted(self.sent):
                call = parse_call(body)
                if not call:
                    return [("error", CUT_SHORT)]
                self.saw_tool = True
                return [("call", call)]
            self.sent += CALL_OPEN + body
            return [("text", CALL_OPEN + body)]
        if self.text_buf:
            value, self.text_buf = self.text_buf, ""
            self.sent += value
            return [("text", value)]
        return []


class QoderStream:
    """Per-attempt decoder: Qoder's {statusCodeValue, body} SSE envelope to Chat Completions SSE."""

    def __init__(self, model: str):
        self.model = model
        self.id = "chatcmpl-" + uuid.uuid4().hex
        self.created = int(time.time())
        self.buffer = b""
        self.done = False
        self.began = False
        self.saw_output = False
        self.splitter = Splitter()
        self.index = -1
        self.native = {}
        self.native_parts = {}
        self.error_mapping = None
        self.event_name = ""
        self.event_data = []
        self.finish_reason = ""
        self.usage = None

    def _chunk(self, delta: dict, finish_reason=None, **extra) -> bytes:
        value = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra}
        return b"data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

    def _open(self, out):
        if not self.began:
            self.began = True
            out.append(self._chunk({"role": "assistant", "content": ""}))

    def _error(self, error: dict) -> bytes:
        self.done, self.buffer = True, b""
        if self.error_mapping is None:
            self.error_mapping = {"status": 502, "error": error}
        return b"data: " + json.dumps({"error": error}, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

    def _pieces(self, out, pieces) -> bool:
        for kind, value in pieces:
            if kind == "error":
                out.append(self._error({"type": "upstream_error", "code": "qoder_tool_call_unreadable", "message": value}))
                return False
            self._open(out)
            self.saw_output = True
            if kind == "text":
                out.append(self._chunk({"content": value}))
            else:
                self.index += 1
                name, arguments = value
                out.append(self._chunk({"tool_calls": [{"index": self.index, "id": "call_" + uuid.uuid4().hex,
                                                        "type": "function", "function": {"name": name, "arguments": arguments}}]}))
        return True

    def _terminal(self, out):
        for name, arguments in self.native_parts.values():
            if not name or not isinstance(_json(arguments or "{}"), dict):
                out.append(self._error({"type": "upstream_error", "code": "qoder_tool_call_unreadable", "message": CUT_SHORT}))
                return
        if not self._pieces(out, self.splitter.flush()):
            return
        if not self.saw_output:
            out.append(self._error({"type": "upstream_error", "code": "qoder_empty_answer", "message": "Qoder ended without an answer"}))
            return
        stop = "tool_calls" if self.splitter.saw_tool else "length" if self.finish_reason == "length" else "stop"
        out.append(self._chunk({}, stop, **({"usage": self.usage} if self.usage else {})))
        out.append(b"data: [DONE]\n\n")
        self.done, self.buffer = True, b""

    def _failed(self, out, status: int, text: str) -> bool:
        mapped = failure(status, text)
        self.error_mapping = mapped
        error = dict(mapped["error"])
        if mapped.get("retry_after"):
            error["retry_after"] = mapped["retry_after"]
        out.append(self._error(error))
        return False

    def _envelope(self, out, envelope, raw: str) -> bool:
        inner = envelope.get("body") if isinstance(envelope.get("body"), str) else ""
        if "body" not in envelope:
            inner = raw
        status = envelope.get("statusCodeValue")
        if status is not None and status != 200:
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 502
            return self._failed(out, status, inner or raw)
        if envelope.get("error") or (envelope.get("code") is not None and str(envelope["code"]) not in {"0", "200"}):
            return self._failed(out, 502, inner or raw)
        if inner.strip() == "[DONE]":
            return True
        chunk = _json(inner)
        if not isinstance(chunk, dict):
            return self._failed(out, 502, "Malformed Qoder SSE body")
        if "choices" not in chunk and (chunk.get("error") or (chunk.get("code") is not None and str(chunk["code"]) not in {"0", "200"})):
            # A sign-in refused inside a 200 envelope (as Qoder's Go clients also read it).
            return self._failed(out, 502, inner)
        choice = (chunk.get("choices") or [{}])[0] if isinstance(chunk.get("choices"), list) else {}
        delta = choice.get("delta") if isinstance(choice, dict) and isinstance(choice.get("delta"), dict) else {}
        if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            self._open(out)
            self.saw_output = True
            out.append(self._chunk({"reasoning_content": delta["reasoning_content"]}))
        if isinstance(delta.get("content"), str) and delta["content"]:
            if not self._pieces(out, self.splitter.feed(delta["content"])):
                return False
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            position = call.get("index", 0)
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            if not isinstance(position, int) or position < 0:
                return self._failed(out, 502, "Invalid Qoder tool index")
            prior = self.native_parts.setdefault(position, ["", ""])
            prior[0] += function.get("name") or ""
            prior[1] += function.get("arguments") or ""
            if position not in self.native:
                if not self._pieces(out, self.splitter.flush()):
                    return False
                self.splitter.saw_tool = True
                self._open(out)
                self.saw_output = True
                self.index += 1
                self.native[position] = self.index
                out.append(self._chunk({"tool_calls": [{
                    "index": self.index, "id": call.get("id") or "call_" + uuid.uuid4().hex, "type": "function",
                    "function": {"name": function.get("name") or "", "arguments": function.get("arguments") or ""}}]}))
            elif function:
                out.append(self._chunk({"tool_calls": [{"index": self.native[position], "function": {k: v for k, v in function.items() if k in {"name", "arguments"}}}]}))
        if isinstance(choice, dict) and choice.get("finish_reason"):
            self.finish_reason = str(choice["finish_reason"])
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            prompt, completion = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
            self.usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
            cached = _count((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) if isinstance(usage.get("prompt_tokens_details"), dict) else 0
            if cached:
                self.usage["prompt_tokens_details"] = {"cached_tokens": cached}
            reasoning = _count((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) if isinstance(usage.get("completion_tokens_details"), dict) else 0
            if reasoning:
                self.usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
        return True

    def _event(self, out):
        raw = "\n".join(self.event_data)
        event, self.event_name, self.event_data = self.event_name, "", []
        if event == "finish":
            if "forceFinish" in raw:
                self._failed(out, 504, "Qoder forced finish")
            else:
                self._terminal(out)
            return
        if event == "error":
            self._failed(out, 502, raw)
        elif not raw:
            return
        elif raw.strip() == "[DONE]":
            pass  # Official callback ignores this marker; consume trailing usage/errors.
        else:
            envelope = _json(raw)
            if not isinstance(envelope, dict):
                self._failed(out, 502, "Malformed Qoder SSE event")
            else:
                self._envelope(out, envelope, raw)

    def feed(self, chunk: bytes) -> bytes:
        if self.done:
            return b""
        self.buffer += chunk
        if len(self.buffer) + sum(map(len, self.event_data)) > 8 * 1024 * 1024:
            return self._error({"type": "upstream_error", "code": "qoder_malformed", "message": "Qoder stream event too large"})
        out = []
        while not self.done and b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.rstrip(b"\r").lstrip(b"\xef\xbb\xbf")
            if not line:
                self._event(out)
            elif line.startswith(b"event:"):
                self.event_name = line[6:].decode("utf-8", errors="replace").strip()
            elif line.startswith(b"data:"):
                self.event_data.append(line[5:].decode("utf-8", errors="replace").lstrip(" "))
        return b"".join(out)

    def finish(self) -> bytes:
        if self.done:
            return b""
        out = []
        if self.buffer.strip() or self.event_data:
            return self._error({"type": "upstream_error", "code": "qoder_incomplete", "message": "Qoder stream ended inside an event"})
        # Official reader accepts a clean EOF; flush still rejects incomplete tools
        # and an empty answer. Malformed residual events above are not clean EOF.
        self._terminal(out)
        return b"".join(out)
