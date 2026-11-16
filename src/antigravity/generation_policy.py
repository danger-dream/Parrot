"""Provider-owned defaults for Antigravity text generation."""
from __future__ import annotations

DEFAULT_MAX_OUTPUT_TOKENS = 16384
SHORT_IDENTITY = (
    "<identity>\n"
    "You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.\n"
    "You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.\n"
    "</identity>"
)


def max_output_tokens(metadata: dict) -> int:
    value = metadata.get("maxOutputTokens")
    return value if type(value) is int and value > 0 else DEFAULT_MAX_OUTPUT_TOKENS


def prepend_identity(request: dict) -> None:
    system = request.setdefault("systemInstruction", {})
    parts = list(system.get("parts") or [])
    first = parts[0] if parts else None
    if isinstance(first, dict) and isinstance(first.get("text"), str):
        if first["text"].startswith(SHORT_IDENTITY):
            return
        parts[0] = {**first, "text": SHORT_IDENTITY + "\n\n" + first["text"]}
    else:
        parts.insert(0, {"text": SHORT_IDENTITY})
    request["systemInstruction"] = {**system, "parts": parts}
