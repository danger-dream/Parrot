"""Parse OpenAI chat-completions payloads into the Cursor request shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COMPACTION_MARKERS = (
    "The conversation history before this point was compacted into the following summary:",
    "The following is a summary of a branch that this conversation came back from:",
)


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str


@dataclass(frozen=True)
class ConversationTurn:
    user_text: str
    assistant_text: str
    is_compaction: bool = False


@dataclass
class ParsedMessages:
    system_prompt: str
    turns: list[ConversationTurn]
    user_text: str
    tool_results: list[ToolResult] = field(default_factory=list)


def text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(content)


def is_compaction_text(text: str) -> bool:
    return any(text.startswith(marker) for marker in COMPACTION_MARKERS)


def parse_messages(messages: list[dict[str, Any]]) -> ParsedMessages:
    system_prompt = ""
    tool_results: list[ToolResult] = []
    tool_names: dict[str, str] = {}
    user_messages: list[tuple[int, str]] = []
    assistant_messages: list[tuple[int, str]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and isinstance(msg.get("tool_calls"), list):
            for call in msg["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(fn.get("name") or "")
                if call_id:
                    tool_names[call_id] = name

    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            if not system_prompt:
                system_prompt = text_content(msg.get("content"))
        elif role == "user":
            user_messages.append((index, text_content(msg.get("content"))))
        elif role == "assistant":
            assistant_messages.append((index, text_content(msg.get("content"))))
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id:
                tool_results.append(
                    ToolResult(
                        tool_call_id=call_id,
                        name=tool_names.get(call_id, ""),
                        content=text_content(msg.get("content")),
                    )
                )

    user_text = user_messages[-1][1] if user_messages else ""
    turns: list[ConversationTurn] = []
    user_idx = 0
    assistant_idx = 0
    while user_idx < len(user_messages) - 1 and assistant_idx < len(assistant_messages):
        user_pos, user_value = user_messages[user_idx]
        assistant_pos, assistant_value = assistant_messages[assistant_idx]
        if assistant_pos > user_pos:
            turns.append(
                ConversationTurn(
                    user_text=user_value,
                    assistant_text=assistant_value,
                    is_compaction=is_compaction_text(user_value),
                )
            )
            user_idx += 1
            assistant_idx += 1
        else:
            assistant_idx += 1

    return ParsedMessages(
        system_prompt=system_prompt,
        turns=turns,
        user_text=user_text,
        tool_results=tool_results,
    )


def select_tools_for_choice(
    tools: list[dict[str, Any]],
    choice: Any,
) -> list[dict[str, Any]]:
    if choice == "none":
        return []
    if choice in (None, "auto", "required"):
        return tools
    if isinstance(choice, dict) and choice.get("type") == "function":
        wanted = ((choice.get("function") or {}) if isinstance(choice.get("function"), dict) else {}).get("name")
        return [tool for tool in tools if (tool.get("function") or {}).get("name") == wanted]
    return tools


def conversation_fingerprint(messages: list[dict[str, Any]], model: str) -> str:
    """Stable session key so a tool-result follow-up hits the paused bridge."""
    import hashlib
    import json

    trimmed = list(messages)
    while trimmed and trimmed[-1].get("role") == "tool":
        trimmed.pop()
    if trimmed and trimmed[-1].get("role") == "assistant" and trimmed[-1].get("tool_calls"):
        trimmed.pop()
    payload = json.dumps({"model": model, "messages": trimmed}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
