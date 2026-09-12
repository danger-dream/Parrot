"""Per-attempt WorkBuddy SSE framing and small wire-shape normalization."""
from __future__ import annotations

import json

from ..protocols.sse import split_sse_events
from .workbuddy_errors import unapproved_channel_error_info


class WorkBuddyStream:
    def __init__(self):
        self.buffer = b""
        self.done = False
        self.saw_choice = False
        # Trusted per-attempt fact; set only after exact vendor-code/message match.
        self.request_rejection: tuple[str, str] | None = None

    @staticmethod
    def _emit(value):
        return b"data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

    def _error(self, code="upstream_malformed"):
        self.done = True
        self.buffer = b""
        return self._emit({"error": {"type": "upstream_error", "code": str(code),
            "message": "WorkBuddy upstream returned an error or malformed stream"}})

    def feed(self, chunk: bytes) -> bytes:
        if self.done:
            return b""
        self.buffer += chunk
        self.buffer, events = split_sse_events(self.buffer)
        if len(self.buffer) > 2 * 1024 * 1024:
            return self._error()
        result = []
        for event in events:
            if len(event) > 2 * 1024 * 1024:
                result.append(self._error())
                break
            data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
            if not data:
                # Heartbeats do not become fake choices or a fake terminal.
                result.append(event + b"\n\n")
                continue
            if data == b"[DONE]":
                if not self.saw_choice:
                    result.append(self._error())
                else:
                    self.done, self.buffer = True, b""
                    result.append(b"data: [DONE]\n\n")
                break
            try:
                obj = json.loads(data)
                if not isinstance(obj, dict):
                    raise ValueError()
                error = obj.get("error")
                if error or obj.get("type") == "error" or b"event: error" in event.splitlines() or obj.get("code") not in (None, 0):
                    rejection = unapproved_channel_error_info(obj)
                    if rejection is not None:
                        self.request_rejection = rejection
                        self.done, self.buffer = True, b""
                        result.append(self._emit({"error": {
                            "type": "invalid_request_error", "code": rejection[0],
                            "message": rejection[1],
                        }}))
                        break
                    code = error.get("code") if isinstance(error, dict) else obj.get("code")
                    # Codes carry classification, arbitrary msg/body never does.
                    safe = code if isinstance(code, int) or code in {"context_length_exceeded", "rate_limit_exceeded", "insufficient_quota", "invalid_token"} else "upstream_error"
                    result.append(self._error(safe))
                    break
                choices = obj.get("choices")
                if not isinstance(choices, list):
                    raise ValueError()
                out = {key: obj[key] for key in ("id", "created", "model", "system_fingerprint", "service_tier", "usage") if key in obj}
                out["object"] = "chat.completion.chunk"
                normalized = []
                for choice in choices:
                    if not isinstance(choice, dict) or choice.get("index", 0) != 0 or len(choices) != 1:
                        raise ValueError()
                    delta = choice.get("delta", choice.get("message")) or {}
                    if not isinstance(delta, dict):
                        raise ValueError()
                    clean = {key: delta[key] for key in ("role", "content", "reasoning_content", "refusal", "tool_calls") if delta.get(key) not in (None, "", [])}
                    for key in ("role", "content", "reasoning_content", "refusal"):
                        if key in clean and not isinstance(clean[key], str):
                            raise ValueError()
                    if delta.get("function_call") and any((delta["function_call"] or {}).values()):
                        raise ValueError()  # Legacy function_call is not a supported request path.
                    tools = clean.get("tool_calls", [])
                    if not isinstance(tools, list):
                        raise ValueError()
                    for tool in tools:
                        if not isinstance(tool, dict) or type(tool.get("index", 0)) is not int or not 0 <= tool.get("index", 0) < 1000:
                            raise ValueError()
                        function = tool.get("function") or {}
                        if not isinstance(function, dict) or any(not isinstance(v, str) for k, v in function.items() if k in {"name", "arguments"} and v is not None):
                            raise ValueError()
                    item = {"index": 0, "delta": clean, "finish_reason": choice.get("finish_reason") or None}
                    if item["finish_reason"] is not None and not isinstance(item["finish_reason"], str):
                        raise ValueError()
                    if "logprobs" in choice:
                        item["logprobs"] = choice["logprobs"]
                    normalized.append(item)
                    self.saw_choice = True
                out["choices"] = normalized
                result.append(self._emit(out))
            except (ValueError, TypeError, AttributeError):
                result.append(self._error())
                break
        return b"".join(result)
