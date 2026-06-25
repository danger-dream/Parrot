"""Prompt token estimation helpers.

Parrot only needs a local, deterministic estimate for routing guards and compact
chunking.  Prefer ``tiktoken`` when it is installed; keep a conservative byte
fallback so the proxy can still start in partially upgraded environments.  The
fallback is intentionally token-like (UTF-8 bytes), never raw character length.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any


def _normalized_tiktoken_model_name(model: str | None) -> str | None:
    """Map Parrot model names to tokenizer-known OpenAI model names.

    Parrot model names are aliases/final names such as ``gpt`` or ``gpt-5.5``
    and may be decorated by channel/account labels.  tiktoken does not know most
    of those strings, so detect the family ourselves instead of passing raw names
    through and relying on KeyError.
    """
    lower = str(model or "").strip().lower()
    if not lower:
        return None
    if "gpt-5" in lower:
        return "gpt-5"
    if "gpt-4.1" in lower:
        return "gpt-4.1"
    if "gpt-4o" in lower:
        return "gpt-4o"
    if "gpt-4" in lower:
        return "gpt-4"
    if "gpt-3.5" in lower or "gpt-3" in lower:
        return "gpt-3.5-turbo"
    # The common local alias is just "gpt"; do not let this catch gpt-4/5.
    if re.search(r"(?<![a-z0-9])gpt(?!-)(?:$|[^a-z0-9])", lower):
        return "gpt-5"
    if re.search(r"(?<![a-z0-9])o1", lower):
        return "o1"
    if re.search(r"(?<![a-z0-9])o3", lower):
        return "o3"
    if re.search(r"(?<![a-z0-9])o4", lower):
        return "o4-mini"
    return None


@lru_cache(maxsize=64)
def _encoding_for_model(model: str | None):
    """Return a tiktoken encoder for prompt-size estimation.

    Parrot model names can be aliases or non-OpenAI ids (deepseek/glm/etc.).
    For unknown names we intentionally fall back to GPT-5's tokenizer instead of
    byte-length heuristics; this keeps the unit as tokens and avoids byte-count
    inflation.
    """
    normalized = _normalized_tiktoken_model_name(model) or "gpt-5"
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None
    try:
        return tiktoken.encoding_for_model(normalized)
    except Exception:
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:
            return None


def count_text_tokens(text: Any, *, model: str | None = None) -> int:
    """Count tokens for text with tiktoken when possible.

    The fallback uses UTF-8 byte length / 3, rounded up.  That is not exact, but
    it tracks payload byte size and is safer than character length for CJK/JSON.
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    enc = _encoding_for_model(model)
    if enc is not None:
        try:
            return int(len(enc.encode(text)))
        except Exception:
            pass
    return max(1, int(math.ceil(len(text.encode("utf-8", errors="replace")) / 3)))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _add(segments: list[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        s = value.strip()
        if s:
            segments.append(s)
        return
    if isinstance(value, (int, float, bool)):
        segments.append(str(value))
        return
    segments.append(_json_text(value))


def _is_data_url(text: Any) -> bool:
    return isinstance(text, str) and text.strip().lower().startswith("data:")


def _collect_image_part(part: dict[str, Any], segments: list[str]) -> None:
    """Count image blocks as image placeholders, never raw base64 bytes.

    Claude/ OpenAI multimodal payloads often contain base64 image data in
    tool_result history.  Provider context accounting does not tokenize that
    base64 as ordinary prompt text, so feeding it to tiktoken can over-count by
    hundreds of thousands of tokens.
    """
    _add(segments, "image")
    source = part.get("source") if isinstance(part.get("source"), dict) else {}
    if isinstance(source, dict):
        _add(segments, source.get("type"))
        _add(segments, source.get("media_type"))
        url = source.get("url")
        if url and not _is_data_url(url):
            _add(segments, url)
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url")
        if url and not _is_data_url(url):
            _add(segments, url)
        _add(segments, image_url.get("detail"))
    elif image_url and not _is_data_url(image_url):
        _add(segments, image_url)


def _collect_content(content: Any, segments: list[str]) -> None:
    if content is None:
        return
    if isinstance(content, str):
        _add(segments, content)
        return
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                typ = str(part.get("type") or "")
                if typ in {"text", "input_text", "output_text"}:
                    _add(segments, part.get("text"))
                elif typ in {"image", "image_url", "input_image"}:
                    _collect_image_part(part, segments)
                elif typ in {"input_audio", "output_audio", "audio"}:
                    _add(segments, part.get("id"))
                elif typ == "tool_result":
                    _add(segments, part.get("name"))
                    _add(segments, part.get("tool_use_id"))
                    _collect_content(part.get("content"), segments)
                elif typ == "tool_use":
                    _add(segments, part.get("id"))
                    _add(segments, part.get("name"))
                    _add(segments, part.get("input"))
                else:
                    _add(segments, part)
            else:
                _add(segments, part)
        return
    _add(segments, content)


def _collect_messages(messages: Any, segments: list[str]) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            _add(segments, message)
            continue
        _add(segments, message.get("role"))
        _add(segments, message.get("name"))
        _collect_content(message.get("content"), segments)
        _add(segments, message.get("tool_calls"))
        _add(segments, message.get("function_call"))


def _collect_tools(tools: Any, segments: list[str]) -> None:
    if tools is None:
        return
    items = tools if isinstance(tools, list) else [tools]
    for tool in items:
        if not isinstance(tool, dict):
            _add(segments, tool)
            continue
        _add(segments, tool.get("type"))
        _add(segments, tool.get("name"))
        _add(segments, tool.get("description"))
        _add(segments, tool.get("input_schema"))
        fn = tool.get("function")
        if isinstance(fn, dict):
            _add(segments, fn.get("name"))
            _add(segments, fn.get("description"))
            _add(segments, fn.get("parameters"))


def request_prompt_segments(body: dict[str, Any] | None) -> list[str]:
    if not isinstance(body, dict):
        return []
    segments: list[str] = []
    _collect_content(body.get("system"), segments)
    _collect_messages(body.get("messages"), segments)
    input_value = body.get("input")
    if isinstance(input_value, list):
        _collect_messages(input_value, segments)
    else:
        _collect_content(input_value, segments)
    _add(segments, body.get("prompt"))
    _collect_tools(body.get("tools"), segments)
    _add(segments, body.get("functions"))
    _add(segments, body.get("tool_choice"))
    _add(segments, body.get("response_format"))
    return segments


def count_request_tokens(body: dict[str, Any] | None, *, model: str | None = None) -> int:
    """Estimate prompt tokens for Anthropic/OpenAI style request bodies."""
    segments = request_prompt_segments(body)
    if not segments:
        return 0
    return count_text_tokens("\n".join(segments), model=model)


def count_message_tokens(message: Any, *, model: str | None = None) -> int:
    segments: list[str] = []
    if isinstance(message, dict):
        _add(segments, message.get("role"))
        _add(segments, message.get("name"))
        _collect_content(message.get("content"), segments)
        _add(segments, message.get("tool_calls"))
        _add(segments, message.get("function_call"))
    else:
        _add(segments, message)
    if not segments:
        return 0
    return count_text_tokens("\n".join(segments), model=model)
