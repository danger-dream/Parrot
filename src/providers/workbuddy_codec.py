"""Per-attempt WorkBuddy SSE framing and small wire-shape normalization."""
from __future__ import annotations

import json

from ..protocols.sse import split_sse_events
from .workbuddy_errors import (
    request_rejection_info, error_payload, QUOTA_CODES,
    ACCOUNT_RESTRICTED_CODE, ACCOUNT_RESTRICTED_MESSAGE,
)


class WorkBuddyStream:
    def __init__(self, *, single_choice=False):
        self.single_choice = single_choice
        self.buffer = b""
        self.done = False
        self.saw_choice = False
        self.saw_output = False
        self.tool_ids = {}  # choice -> {upstream id: tool index}
        self.tool_indexes = {}
        self.finished = set()
        self.output_choices = set()
        self.argument_prefix = {}  # At most the initial '{}' ambiguity per call.
        # Trusted per-attempt fact; set only after exact vendor-code/message match.
        self.request_rejection: tuple[str, str] | None = None
        self.account_restricted = False

    @staticmethod
    def _emit(value):
        return b"data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

    def _error(self, code="upstream_malformed"):
        self.done = True
        self.buffer = b""
        quota = str(code) in QUOTA_CODES
        if str(code) == ACCOUNT_RESTRICTED_CODE:
            self.account_restricted = True
            return self._emit({"error": {"type": "permission_error", "code": ACCOUNT_RESTRICTED_CODE,
                                        "message": ACCOUNT_RESTRICTED_MESSAGE}})
        return self._emit({"error": {"type": "insufficient_quota" if quota else "upstream_error", "code": str(code),
            "message": f"WorkBuddy account quota exhausted (code {code})" if quota else
                       "WorkBuddy upstream returned an error or malformed stream"}})

    def _flush_arguments(self, ci):
        tools = []
        for (choice, index), prefix in list(self.argument_prefix.items()):
            if choice == ci and prefix:
                tools.append({"index": index, "function": {"arguments": prefix}})
                self.argument_prefix[(choice, index)] = None
        return tools

    def finish(self) -> bytes:
        if self.done:
            return b""
        if self.buffer.strip() or not self.saw_output or not self.finished:
            return self._error()
        return b""  # A complete finish frame may legally precede HTTP EOF.

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
            event_error = any(line.startswith(b"event:") and line[6:].strip() == b"error" for line in event.splitlines())
            data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
            if not data:
                if event_error:
                    result.append(self._error())
                    break
                # Heartbeats do not become fake choices or a fake terminal.
                result.append(event + b"\n\n")
                continue
            if data == b"[DONE]":
                if not self.saw_output:
                    result.append(self._error())
                else:
                    for ci in sorted(self.tool_indexes):
                        tools = self._flush_arguments(ci)
                        if tools:
                            result.append(self._emit({"object": "chat.completion.chunk", "choices": [{"index": ci, "delta": {"tool_calls": tools}, "finish_reason": None}]}))
                    self.done, self.buffer = True, b""
                    result.append(b"data: [DONE]\n\n")
                break
            try:
                obj = json.loads(data)
                if not isinstance(obj, dict):
                    raise ValueError()
                error = obj.get("error")
                if isinstance(error, dict) or error or obj.get("type") == "error" or event_error or obj.get("code") not in (None, 0):
                    rejection = request_rejection_info(obj)
                    if rejection is not None:
                        self.request_rejection = rejection
                        self.done, self.buffer = True, b""
                        result.append(self._emit({"error": {
                            "type": "invalid_request_error", "code": rejection[0],
                            "message": rejection[1],
                        }}))
                        break
                    code = error_payload(obj).get("code")
                    # Codes carry classification, arbitrary msg/body never does.
                    numeric = isinstance(code, str) and code.isascii() and code.isdigit() and len(code) <= 8
                    safe = code if isinstance(code, int) or numeric or code in {"context_length_exceeded", "rate_limit_exceeded", "insufficient_quota", "invalid_token"} else "upstream_error"
                    result.append(self._error(safe))
                    break
                choices = obj.get("choices")
                if choices is None and isinstance(obj.get("usage"), dict):
                    choices = []
                if not isinstance(choices, list):
                    raise ValueError()
                out = {key: obj[key] for key in ("id", "created", "model", "system_fingerprint", "service_tier", "usage") if key in obj}
                out["object"] = "chat.completion.chunk"
                normalized = []
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise ValueError()
                    ci = choice.get("index", 0)
                    if type(ci) is not int or not 0 <= ci < 1000:
                        raise ValueError()
                    if self.single_choice and (ci != 0 or len(choices) != 1):
                        raise ValueError()
                    delta = choice.get("delta", choice.get("message")) or {}
                    if not isinstance(delta, dict):
                        raise ValueError()
                    clean = {key: delta[key] for key in ("role", "content", "reasoning", "reasoning_content", "extra_fields", "refusal", "tool_calls") if delta.get(key) not in (None, "", [])}
                    for key in ("reasoning", "reasoning_content", "extra_fields"):
                        if key in delta and delta[key] is not None:
                            clean[key] = delta[key]
                    if "extra_fields" in clean and not isinstance(clean["extra_fields"], dict):
                        raise ValueError()
                    if isinstance(clean.get("extra_fields"), dict) and "signature" in clean["extra_fields"] and not isinstance(clean["extra_fields"]["signature"], str):
                        raise ValueError()
                    for key in ("role", "content", "reasoning", "reasoning_content", "refusal"):
                        if key in clean and not isinstance(clean[key], str):
                            raise ValueError()
                    if delta.get("function_call") and any((delta["function_call"] or {}).values()):
                        raise ValueError()  # Legacy function_call is not a supported request path.
                    tools = clean.get("tool_calls", [])
                    if not isinstance(tools, list):
                        raise ValueError()
                    ids = self.tool_ids.setdefault(ci, {})
                    indexes = self.tool_indexes.setdefault(ci, set())
                    for tool in tools:
                        if not isinstance(tool, dict):
                            raise ValueError()
                        tid = tool.get("id")
                        if tid is not None and not isinstance(tid, str):
                            raise ValueError()
                        index = tool.get("index")
                        if index is None:
                            index = ids.get(tid) if tid else None
                            if index is None:
                                if not tid and len(indexes) > 1:
                                    raise ValueError()  # No identity: cannot guess which parallel call owns it.
                                index = max(indexes, default=-1) + 1 if tid else (next(iter(indexes)) if len(indexes) == 1 else 0)
                        if type(index) is not int or not 0 <= index < 1000:
                            raise ValueError()
                        tool["index"] = index
                        indexes.add(index)
                        if tid:
                            if tid in ids:
                                if ids[tid] != index:
                                    raise ValueError()
                                tool.pop("id", None)
                            else:
                                ids[tid] = index
                        if tool.get("type") in (None, ""):
                            tool["type"] = "function"
                        function = tool.get("function") or {}
                        if not isinstance(function, dict) or any(not isinstance(v, str) for k, v in function.items() if k in {"name", "arguments"} and v is not None):
                            raise ValueError()
                        piece = function.get("arguments")
                        key = (ci, index)
                        prefix = self.argument_prefix.get(key, "")
                        if isinstance(piece, str) and prefix is not None:
                            prefix += piece
                            if prefix in ("", "{", "{}"):
                                self.argument_prefix[key] = prefix
                                function.pop("arguments", None)
                            else:
                                function["arguments"] = prefix[2:] if prefix.startswith("{}{") else prefix
                                self.argument_prefix[key] = None
                    if choice.get("finish_reason"):
                        pending = self._flush_arguments(ci)
                        if pending:
                            clean.setdefault("tool_calls", []).extend(pending)
                    item = {"index": ci, "delta": clean, "finish_reason": choice.get("finish_reason") or None}
                    if item["finish_reason"] is not None and not isinstance(item["finish_reason"], str):
                        raise ValueError()
                    if "logprobs" in choice:
                        item["logprobs"] = choice["logprobs"]
                    normalized.append(item)
                    self.saw_choice = True
                    if any(clean.get(k) for k in ("content", "reasoning", "reasoning_content", "refusal", "tool_calls")):
                        self.saw_output = True
                        self.output_choices.add(ci)
                    if item["finish_reason"]:
                        if ci not in self.output_choices:
                            raise ValueError()
                        self.finished.add(ci)
                out["choices"] = normalized
                result.append(self._emit(out))
            except (ValueError, TypeError, AttributeError):
                result.append(self._error())
                break
        return b"".join(result)
