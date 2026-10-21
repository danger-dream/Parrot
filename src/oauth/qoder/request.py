"""A Chat Completions payload written as Qoder's agent_chat_generation request."""
from __future__ import annotations

import json
import math
import time
import uuid

from . import common

QODER_SYSTEM = "You are a Qoder agent. Use the instructions below and the tools available to you to assist the user."
EFFORT_RANK = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_MAX_TOKENS = 32000


def _image(part: dict) -> dict | None:
    value = part.get("image_url")
    url = value if isinstance(value, str) else value.get("url") if isinstance(value, dict) else None
    return {"type": "image_url", "image_url": {"url": url}} if isinstance(url, str) and url else None


def blocks_of(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text") is not None:
            out.append({"type": "text", "text": str(part["text"])})
        elif part.get("type") == "image_url":
            image = _image(part)
            if image:
                out.append(image)
    return out


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(block["text"] for block in blocks_of(content) if block["type"] == "text")


def messages_of(messages: list) -> list[dict]:
    """Text and image blocks; tool results as tool turns, the images a tool returned in the next user turn."""
    out: list[dict] = []
    names: dict[str, str] = {}
    seen: list[dict] = []

    def show_seen():
        if seen:
            out.append({"role": "user", "content": list(seen)})
            seen.clear()

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        if role not in ("user", "tool"):
            show_seen()
        content = message.get("content")
        if role == "tool":
            text = text_of(content)
            images = [image for image in (_image(p) for p in content if isinstance(p, dict) and p.get("type") == "image_url") if image] if isinstance(content, list) else []
            call_id = str(message.get("tool_call_id") or "")
            if images:
                source = "tool call " + call_id
                name = names.get(call_id) or message.get("name")
                if name:
                    source = f"{name} ({source})"
                seen.extend([{"type": "text", "text": f"[From the result of {source}:]"}, *images])
                note = ("[The tool returned an image; it follows in the next message.]" if len(images) == 1
                        else f"[The tool returned {len(images)} images; they follow in the next message.]")
                text = text + "\n\n" + note if text.strip() else text + note
            out.append({"role": "tool", "tool_call_id": call_id, "content": text})
            continue
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_id = call.get("id") or "call_" + uuid.uuid4().hex
                names[call_id] = function.get("name")
                arguments = function.get("arguments")
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": function.get("name"),
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))}})
            out.append({"role": "assistant", "content": text_of(content), "tool_calls": calls})
            continue
        blocks = blocks_of(content)
        if role == "assistant":
            blocks = [block for block in blocks if block["type"] == "text"]
        if not blocks:
            continue
        if role == "user" and seen:
            blocks = [*seen, *blocks]
            seen.clear()
        out.append({"role": role, "content": blocks})
    show_seen()
    return out


def fit_effort(want: str, levels: list[str]) -> str:
    if want == "ultra" and want not in levels:
        want = "max"
    if not levels or want in levels or want not in EFFORT_RANK:
        return want
    at = EFFORT_RANK.index(want)
    best, distance = want, len(EFFORT_RANK)
    for level in levels:
        if level not in EFFORT_RANK or level == "none":
            continue
        index = EFFORT_RANK.index(level)
        gap = abs(index - at)
        if gap < distance or (gap == distance and index > at):
            best, distance = level, gap
    return best


def effort_for(asked, record: dict) -> tuple[bool, str]:
    """Whether to think and at which of the model's own levels ("" for none)."""
    want = str(asked or "").lower()
    if want == "off":
        want = "none"
    if want not in EFFORT_RANK:
        want = ""
    efforts = list(record.get("reasoningEfforts") or [])
    always = record.get("qoderAlwaysThinks") is True
    if not record.get("reasoning") or (want == "none" and not always):
        return False, ""
    if not efforts:
        return True, ""
    if want == "":
        return True, str(record.get("defaultReasoningEffort") or "")
    if want == "none":
        return True, efforts[0]
    return True, fit_effort(want, efforts)


def window_for(payload: dict, record: dict, reply: int = 0) -> int:
    """The default window while the request fits it, else the smallest that holds it, else the largest."""
    windows = [w for w in record.get("qoderWindows") or [] if isinstance(w, int) and w > 0]
    context = int(record.get("contextWindow") or 0)
    if not windows:
        return context
    base = int(record.get("qoderDefaultWindow") or 0) or int(record.get("qoderMaxInputTokens") or 0) or context
    size = len(json.dumps({"m": payload.get("messages") or [], "t": payload.get("tools") or []},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    need = math.ceil(size / 3) + max(0, reply)
    if base > 0 and need <= base:
        return base
    return next((w for w in windows if w >= need), windows[-1])


def body_for(payload: dict, record: dict) -> dict:
    messages = payload.get("messages") or []
    system = "\n\n".join(text for text in (text_of(m.get("content")) for m in messages
                         if isinstance(m, dict) and m.get("role") in ("system", "developer")) if text)
    choice = payload.get("tool_choice")
    tools = [] if choice == "none" else [
        tool for tool in payload.get("tools") or []
        if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict) and tool["function"].get("name")
    ]
    text = QODER_SYSTEM + "\n\n" + system if system else QODER_SYSTEM
    if (choice == "required" or (isinstance(choice, dict) and choice.get("type") == "function")) and tools:
        named = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
        text += f"\nYou must call the {named} function in this response." if named else "\nYou must call an available function in this response."
    system_block = {"type": "text", "text": text}
    thinking, effort = effort_for(payload.get("reasoning_effort"), record)
    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens") or DEFAULT_MAX_TOKENS
    parameters = {"enable_thinking": thinking, "max_tokens": int(max_tokens)}
    if payload.get("temperature") is not None:
        parameters["temperature"] = payload["temperature"]
    if effort:
        parameters["reasoning_effort"] = effort
    window = window_for(payload, record, parameters["max_tokens"])
    if window > 0:
        parameters["context_length"] = window
    body = {
        "parameters": parameters,
        "business": {"product": "app", "version": common.COSY_VERSION, "type": "agent", "id": uuid.uuid4().hex,
                     "name": "session", "begin_at": int(time.time() * 1000), "stage": "start"},
        "agent_id": "agent_common",
        "task_id": "common",
        "session_type": "app",
        "model_config": record.get("qoderConfig") or {},
        "system": [system_block],
        "messages": [{"role": "system", "content": [system_block]}, *messages_of(messages)],
    }
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": tool["function"]["name"], "description": tool["function"].get("description") or "",
            "parameters": tool["function"].get("parameters") or {"type": "object", "properties": {}}}} for tool in tools]
    return body
