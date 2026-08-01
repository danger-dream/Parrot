"""Claude Code compact rescue helpers.

Claude Code's reactive compact request is itself a normal Anthropic Messages
request: it appends a final user message asking the model to summarize the full
conversation.  When the routed model has a smaller context window than Claude
Code assumes (for example gpt-5.5 behind a Claude-facing endpoint), that compact
request can still contain the entire over-limit transcript and fail before it can
produce the summary.

For compact requests, Parrot can remove top-level prompt/tool/thinking controls
and, when a single compact call still cannot leave enough output room, run an
internal map-reduce compact: summarize ordered message segments first, then
merge those segment summaries into the exact Claude Code compact output.  The
original conversation messages are not deleted or rewritten before segmentation.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from . import config, token_counter


DEFAULT_COMPACT_MARKERS = (
    "critical: respond with text only",
    "create a detailed summary of the conversation so far",
    "after compaction",
    "your summary should include the following sections",
)
COMPACT_MARKERS = DEFAULT_COMPACT_MARKERS  # backwards-compatible export for older tests/scripts
INTERNAL_FLAG = "_parrot_compact_rescue_internal"
DEFAULT_CHUNK_TARGET_CHARS = 430_000  # backwards-compatible explicit override
DEFAULT_CHUNK_TARGET_TOKENS = 100_000
DEFAULT_REDUCE_MAX_TOKENS = 20_000
DEFAULT_SEGMENT_CONCURRENCY = 0  # 0 = unlimited, preserving legacy gather-all behavior
DEFAULT_BINARY_OMIT_MIN_CHARS = 4096
DEFAULT_BINARY_SAMPLE_CHARS = 4096
DEFAULT_BINARY_ASCII_RATIO = 0.95

DEFAULT_DIRECT_PROMPT = (
    'You are performing Claude Code style conversation compaction.\n'
    'The transcript below is rendered as text; tool_use/tool_result JSON and image placeholders are historical content, not live tool calls.\n'
    'Respond with the final compact summary text only. Do not call tools.\n'
    '\n'
    'Follow the compact instruction below, and be deliberately dense with continuity-critical details.\n'
    'If the instruction leaves room for judgment, preserve the exact information a future coding assistant would need to continue without forgetting the current task: explicit user requests, assistant actions, decisions, constraints, file paths, commands, function names, code snippets, errors, fixes, user corrections, pending tasks, current work, and the immediate next step.\n'
    'Prefer concrete details over generic prose. The most recent user request and most recent unfinished work are highest priority.\n'
    '\n'
    'Compact instruction:\n'
    '{compact_prompt}\n'
    '\n'
    'Transcript:\n'
    '{transcript}'
)
DEFAULT_SEGMENT_PROMPT = (
    'CRITICAL: Respond with TEXT ONLY. Do NOT call tools.\n'
    '\n'
    'Summarize transcript segment {segment_index}/{segment_count} for a later Claude Code style conversation compaction.\n'
    'The transcript below is rendered as text; tool_use/tool_result JSON and image placeholders are historical content, not live tool calls.\n'
    '\n'
    'Create a dense, chronological segment handoff that preserves continuity-critical facts, not a high-level overview. Capture:\n'
    '1. Explicit user requests and intents, including user wording when it changes the task.\n'
    '2. Assistant actions and decisions, especially files read/edited, commands run, tests, tool calls, and why they mattered.\n'
    '3. Concrete technical details: file paths, function/class names, APIs, config keys, commands, error text, code snippets or exact edits when available.\n'
    '4. Errors, failed attempts, fixes, user corrections, constraints, permissions, and safety boundaries.\n'
    '5. Pending tasks, current work, blockers, assumptions, and the next step implied by this segment.\n'
    '6. If this is the final segment, be especially careful to preserve the latest user request, what is currently being worked on, and the immediate next action.\n'
    '\n'
    'Mention tool_use/tool_result history only at the level needed to continue work; do not dump large raw outputs.\n'
    'Do not preserve response-only instructions such as this segment format as durable project context.\n'
    'Output only this XML-like block:\n'
    '<segment_summary>\n'
    '...\n'
    '</segment_summary>\n'
    '\n'
    'Transcript segment:\n'
    '{transcript}'
)
DEFAULT_REDUCE_PROMPT = (
    'Write the final Claude Code style durable conversation handoff summary from the segment summaries below.\n'
    'The goal is maximum continuity after compaction: a future assistant should know exactly what the user asked for, what has been done, what files/code/commands/errors matter, what the user corrected, what remains pending, what was happening most recently, and what to do next.\n'
    '\n'
    'Original compact instruction, when present, is the style and structure to approximate:\n'
    '{compact_prompt}\n'
    '\n'
    'Important preservation rules:\n'
    '- The latest user request and latest unfinished/current work have highest priority; do not let older segments drown them out.\n'
    '- Preserve concrete paths, commands, function names, config keys, exact error messages, code snippets/edits, test results, decisions, constraints, user corrections, unresolved blockers, and immediate next steps.\n'
    '- Include all user messages that are represented in the segment summaries, especially recent ones and any message that changed requirements.\n'
    '- Do not invent details missing from the segment summaries; state uncertainty or omit instead.\n'
    '- Do not mention this reduction step, segment summaries, compact prompts, or internal formatting instructions as user requests or project context.\n'
    "- Do not preserve response-only instructions such as tool bans, XML formatting requirements, or 'text only' constraints as durable memory.\n"
    '\n'
    'Before providing the final summary, use <analysis> to check chronological coverage, missing current-work details, and whether the next step follows directly from the most recent request.\n'
    'Output exactly two top-level XML-like blocks: <analysis>...</analysis> then <summary>...</summary>.\n'
    'Inside <summary>, use these numbered sections and make each section specific:\n'
    '1. Primary Request and Intent\n'
    '2. Key Technical Concepts\n'
    '3. Files and Code Sections\n'
    '4. Errors and fixes\n'
    '5. Problem Solving\n'
    '6. All user messages\n'
    '7. Pending Tasks\n'
    '8. Current Work\n'
    '9. Optional Next Step\n'
    '\n'
    'Durable context excerpts:\n'
    '{summaries}\n'
    '\n'
    'CRITICAL CURRENT-STATE CHECKPOINT (authoritative latest segment):\n'
    '{latest_summary}\n'
    'The checkpoint is intentionally repeated from the final segment. Its latest user request, current work, pending tasks, and immediate next step MUST control final sections 7-9 even when older segments are much longer. Do not mention this repetition or checkpoint in the final summary.'
)


def _positive_int(value: Any, default: int) -> int:
    try:
        n = int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default
    return n if n > 0 else default


def _non_negative_int(value: Any, default: int) -> int:
    try:
        n = int(float(str(value).replace(",", "").strip()))
    except Exception:
        return default
    return n if n >= 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        n = float(str(value).replace(",", "").strip())
    except Exception:
        return default
    return n if n > 0 else default


def _prompt_template(root: dict[str, Any], name: str, default: str) -> str:
    prompts = root.get("prompts") if isinstance(root.get("prompts"), dict) else {}
    value = prompts.get(name) if isinstance(prompts, dict) else None
    return value if isinstance(value, str) and value.strip() else default


def _markers(root: dict[str, Any]) -> tuple[str, ...]:
    raw = root.get("markers")
    if not isinstance(raw, list):
        return DEFAULT_COMPACT_MARKERS
    items = tuple(str(x).strip().lower() for x in raw if str(x).strip())
    return items or DEFAULT_COMPACT_MARKERS


def _render_template(template: str, values: dict[str, Any]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def settings() -> dict[str, Any]:
    cfg = config.get()
    root = cfg.get("compactRescue") or {}
    if not isinstance(root, dict):
        root = {}
    return {
        "enabled": root.get("enabled") is not False,
        "markers": _markers(root),
        "chunkTargetTokens": _positive_int(root.get("chunkTargetTokens"), DEFAULT_CHUNK_TARGET_TOKENS),
        "reduceMaxTokens": _positive_int(root.get("reduceMaxTokens"), DEFAULT_REDUCE_MAX_TOKENS),
        "summaryReserveTokens": _positive_int(root.get("summaryReserveTokens"), DEFAULT_REDUCE_MAX_TOKENS),
        "safetyBufferTokens": _positive_int(root.get("safetyBufferTokens"), DEFAULT_REDUCE_MAX_TOKENS),
        "segmentConcurrency": _non_negative_int(root.get("segmentConcurrency"), DEFAULT_SEGMENT_CONCURRENCY),
        "binaryOmitMinChars": _positive_int(root.get("binaryOmitMinChars"), DEFAULT_BINARY_OMIT_MIN_CHARS),
        "binarySampleChars": _positive_int(root.get("binarySampleChars"), DEFAULT_BINARY_SAMPLE_CHARS),
        "binaryAsciiRatio": _positive_float(root.get("binaryAsciiRatio"), DEFAULT_BINARY_ASCII_RATIO),
        "directPrompt": _prompt_template(root, "direct", DEFAULT_DIRECT_PROMPT),
        "segmentPrompt": _prompt_template(root, "segment", DEFAULT_SEGMENT_PROMPT),
        "reducePrompt": _prompt_template(root, "reduce", DEFAULT_REDUCE_PROMPT),
    }


def enabled() -> bool:
    return bool(settings()["enabled"])


def compact_markers() -> tuple[str, ...]:
    return tuple(settings()["markers"])


def chunk_target_tokens() -> int:
    return settings()["chunkTargetTokens"]


def reduce_max_tokens() -> int:
    return settings()["reduceMaxTokens"]


def summary_reserve_tokens_default() -> int:
    return settings()["summaryReserveTokens"]


def safety_buffer_tokens() -> int:
    return settings()["safetyBufferTokens"]


def segment_concurrency() -> int:
    return settings()["segmentConcurrency"]


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                typ = item.get("type")
                if typ == "text":
                    parts.append(str(item.get("text") or ""))
                elif typ == "tool_result":
                    parts.append(_text_from_content(item.get("content")))
                elif typ == "tool_use":
                    parts.append(str(item.get("name") or "tool_use"))
                    parts.append(json.dumps(item.get("input") or {}, ensure_ascii=False, default=str))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


def is_claude_code_compact_request(body: dict[str, Any] | None) -> bool:
    """Return True for Claude Code's auto/reactive compact prompt."""
    if not enabled() or not isinstance(body, dict) or body.get(INTERNAL_FLAG):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False

    # The prompt text is more stable than headers through proxies/retries.
    tail_text = "\n".join(_text_from_content(m.get("content")) for m in messages[-4:] if isinstance(m, dict))
    low = tail_text.lower()
    return all(marker in low for marker in compact_markers())


def _strip_top_level_controls(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    original_json = json.dumps(body, ensure_ascii=False, default=str)
    new_body = copy.deepcopy(body)
    meta = {
        "original_chars": len(original_json),
        "removed_top_level_system": "system" in new_body,
        "removed_tool_definitions": len(new_body.get("tools") or []) if isinstance(new_body.get("tools"), list) else 0,
        "removed_tool_choice": "tool_choice" in new_body,
        "removed_thinking": "thinking" in new_body,
        "removed_output_config": "output_config" in new_body,
        "removed_context_management": "context_management" in new_body,
        "original_max_tokens": new_body.get("max_tokens"),
        "original_max_output_tokens": new_body.get("max_output_tokens"),
    }
    for key in ("system", "tools", "tool_choice", "thinking", "output_config", "context_management"):
        new_body.pop(key, None)
    return new_body, meta


def prepare_compact_rescue_body(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a compact-rescued body plus metadata, or original body + None.

    Minimal policy: remove only top-level system_prompt, top-level tool
    definitions, and top-level thinking controls. Preserve messages verbatim.
    """
    if not is_claude_code_compact_request(body):
        return body, None

    new_body, meta = _strip_top_level_controls(body)
    rescued_json = json.dumps(new_body, ensure_ascii=False, default=str)
    messages = new_body.get("messages") if isinstance(new_body.get("messages"), list) else []
    meta.update({
        "rescued_chars": len(rescued_json),
        "message_count": len(messages),
        "preserved_max_tokens": new_body.get("max_tokens"),
        "preserved_max_output_tokens": new_body.get("max_output_tokens"),
    })
    new_body["_parrot_compact_rescue"] = meta
    return new_body, meta


def sanitized_compact_base(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return compact-safe body base with no top-level controls."""
    out, meta = _strip_top_level_controls(body)
    out[INTERNAL_FLAG] = True
    out.pop("_parrot_compact_rescue", None)
    return out, meta



def _message_json_chars(message: Any) -> int:
    return len(json.dumps(message, ensure_ascii=False, default=str))


def _content_blocks(message: Any) -> list[Any]:
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return content
    return []


def _tool_use_ids(message: Any) -> list[str]:
    ids: list[str] = []
    for block in _content_blocks(message):
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
            ids.append(str(block.get("id")))
    return ids


def _tool_result_ids(message: Any) -> list[str]:
    ids: list[str] = []
    for block in _content_blocks(message):
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
            ids.append(str(block.get("tool_use_id")))
    return ids


def _compact_prompt_start(text: str) -> int | None:
    """Return the compact-instruction start offset when all markers match."""
    low = str(text or "").lower()
    markers = compact_markers()
    positions = [low.find(marker) for marker in markers]
    if not positions or any(pos < 0 for pos in positions):
        return None
    return min(positions)


def _without_compact_instruction(message: dict[str, Any]) -> dict[str, Any] | None:
    """Strip compact instructions while preserving user text in the same turn.

    Claude Code can append the compact instruction as a second text block in
    the user's latest request. Dropping that whole message loses the current
    task. Standalone compact messages still disappear completely.
    """
    content = message.get("content")
    if isinstance(content, str):
        start = _compact_prompt_start(content)
        if start is None:
            return message
        prefix = content[:start].rstrip()
        if not prefix:
            return None
        out = copy.deepcopy(message)
        out["content"] = prefix
        return out

    if not isinstance(content, list):
        return None

    # Locate the text block where the compact instruction begins. In normal
    # Claude Code requests this is a dedicated block immediately after the
    # actual user text. The fallback supports custom marker sets as well.
    marker_block = -1
    marker_offset = -1
    markers = compact_markers()
    for block_idx, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        block_text = str(block.get("text") or "")
        start = _compact_prompt_start(block_text)
        if start is not None:
            marker_block, marker_offset = block_idx, start
            break
        low = block_text.lower()
        positions = [low.find(marker) for marker in markers if low.find(marker) >= 0]
        if positions:
            marker_block, marker_offset = block_idx, min(positions)
            break

    if marker_block < 0:
        # The full message matched but markers were split across unusual block
        # boundaries. Preserve blocks before the first marker-bearing text
        # block when possible; otherwise retain the old safe behavior.
        return None

    kept = copy.deepcopy(content[:marker_block])
    marker = content[marker_block]
    prefix = str(marker.get("text") or "")[:marker_offset].rstrip()
    if prefix:
        prefix_block = copy.deepcopy(marker)
        prefix_block["text"] = prefix
        kept.append(prefix_block)
    # Non-text blocks after the compact text remain historical context; later
    # text blocks are compact-instruction continuation and are intentionally
    # omitted.
    kept.extend(
        copy.deepcopy(block)
        for block in content[marker_block + 1:]
        if not (isinstance(block, dict) and block.get("type") == "text")
    )
    if not kept:
        return None
    out = copy.deepcopy(message)
    out["content"] = kept
    return out


def _history_without_compact_prompt(messages: list[Any]) -> list[Any]:
    if not messages:
        return []
    # Claude Code's compact prompt is normally near the tail, but another
    # system reminder may be appended after it. Strip only the compact
    # instruction; preserve any real user request sharing that message.
    for idx in range(len(messages) - 1, max(-1, len(messages) - 8), -1):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _text_from_content(msg.get("content"))
        if _compact_prompt_start(text) is None:
            continue
        replacement = _without_compact_instruction(msg)
        out = list(messages[:idx])
        if replacement is not None:
            out.append(replacement)
        out.extend(messages[idx + 1:])
        return out
    return messages


def split_messages_for_compact(
    messages: list[Any],
    *,
    target_tokens: int | None = None,
    target_chars: int | None = None,
    model: str | None = None,
) -> list[list[Any]]:
    """Split messages into ordered chunks by token budget and tool boundary.

    Boundaries are only allowed when all tool_use ids seen in the current chunk
    have corresponding tool_result ids. The final Claude Code compact prompt is
    not part of the history being summarized; reduce uses it separately.

    ``target_chars`` is kept only for old tests/scripts that pass it explicitly;
    production defaults to 100k estimated tokens per segment.
    """
    messages = _history_without_compact_prompt(messages)
    if not messages:
        return []
    chunks: list[list[Any]] = []
    start = 0
    cur_tokens = 0
    cur_chars = 0
    if target_chars is None and target_tokens is None:
        target_tokens = chunk_target_tokens()
    use_tokens = target_chars is None and target_tokens is not None and int(target_tokens) > 0
    token_limit = int(target_tokens or 0)
    char_limit = int(target_chars or DEFAULT_CHUNK_TARGET_CHARS)
    pending_tool_ids: set[str] = set()
    for idx, msg in enumerate(messages):
        msg_chars = _message_json_chars(msg)
        msg_tokens = token_counter.count_message_tokens(msg, model=model) if use_tokens else 0
        for tid in _tool_result_ids(msg):
            pending_tool_ids.discard(tid)
        for tid in _tool_use_ids(msg):
            pending_tool_ids.add(tid)
        cur_chars += msg_chars
        cur_tokens += msg_tokens
        over_limit = cur_tokens >= token_limit if use_tokens else cur_chars >= char_limit
        if over_limit and not pending_tool_ids and idx + 1 < len(messages):
            chunks.append(copy.deepcopy(messages[start: idx + 1]))
            start = idx + 1
            cur_chars = 0
            cur_tokens = 0
    if start < len(messages):
        chunks.append(copy.deepcopy(messages[start:]))
    return chunks


def _compact_source_prompt(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _text_from_content(msg.get("content"))
        start = _compact_prompt_start(text)
        if start is not None:
            return text[start:]
    return ""


def _is_probably_base64_blob(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.lower().startswith("data:") and ";base64," in text[:200].lower():
        return True
    s = settings()
    if len(text) < int(s["binaryOmitMinChars"]):
        return False
    # Base64 image/tool blobs are long and mostly ASCII alphabet/+//=.
    sample = text[:int(s["binarySampleChars"])]
    allowed = sum(1 for ch in sample if ch.isalnum() or ch in "+/=_-\n\r")
    return allowed / max(1, len(sample)) > float(s["binaryAsciiRatio"])


def _sanitize_for_summary_render(value: Any) -> Any:
    """Remove non-text binary payloads before compact summarization.

    Compact rescue is a text summarization task.  Tool results may contain image
    blocks with huge base64 payloads; sending those bytes as text both distorts
    token estimation and may exceed downstream context.  Preserve enough
    metadata for continuity, but never include raw base64/image data.
    """
    if isinstance(value, list):
        return [_sanitize_for_summary_render(item) for item in value]
    if isinstance(value, dict):
        typ = str(value.get("type") or "").lower()
        if typ in {"image", "image_url", "input_image"}:
            source = value.get("source") if isinstance(value.get("source"), dict) else {}
            image_url = value.get("image_url") if isinstance(value.get("image_url"), dict) else {}
            return {
                "type": typ or "image",
                "media_type": source.get("media_type") or value.get("media_type"),
                "source_type": source.get("type"),
                "detail": image_url.get("detail") or value.get("detail"),
                "omitted": "image/base64 payload omitted for text compaction",
            }
        out: dict[str, Any] = {}
        for key, item in value.items():
            low_key = str(key).lower()
            if low_key == "data" and _is_probably_base64_blob(item):
                out[key] = f"<omitted base64 data: {len(str(item))} chars>"
            elif low_key == "url" and _is_probably_base64_blob(item):
                out[key] = f"<omitted data url: {len(str(item))} chars>"
            else:
                out[key] = _sanitize_for_summary_render(item)
        return out
    if _is_probably_base64_blob(value):
        return f"<omitted base64/data payload: {len(str(value))} chars>"
    return value


def _render_messages_for_summary(messages: list[Any]) -> str:
    """Render messages as plain transcript text for internal summarization.

    Internal compact calls must not send structured tool_use/tool_result/image
    blocks as protocol messages.  Rendering preserves textual history while
    replacing binary image payloads with small placeholders.
    """
    parts: list[str] = []
    for idx, msg in enumerate(messages, start=1):
        if isinstance(msg, dict):
            role = str(msg.get("role") or "unknown")
            content = _sanitize_for_summary_render(msg.get("content"))
            rendered = json.dumps(content, ensure_ascii=False, default=str)
        else:
            role = "unknown"
            rendered = json.dumps(_sanitize_for_summary_render(msg), ensure_ascii=False, default=str)
        parts.append(f"--- message {idx} role={role} ---\n{rendered}")
    return "\n\n".join(parts)


def build_direct_summary_body(
    original_body: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    base, _meta = sanitized_compact_base(original_body)
    base["stream"] = False
    base["model"] = model
    base["max_tokens"] = max_tokens
    base.pop("max_output_tokens", None)
    history = _history_without_compact_prompt(original_body.get("messages") or [])
    transcript = _render_messages_for_summary(history)
    compact_prompt = _compact_source_prompt(original_body.get("messages") or [])
    base["messages"] = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": _render_template(settings()["directPrompt"], {
                "compact_prompt": compact_prompt,
                "transcript": transcript,
            }),
        }],
    }]
    return base


def build_segment_summary_body(
    original_body: dict[str, Any],
    segment_messages: list[Any],
    *,
    segment_index: int,
    segment_count: int,
) -> dict[str, Any]:
    base, _meta = sanitized_compact_base(original_body)
    base["stream"] = False
    # Internal segment summaries should reserve a bounded output budget instead
    # of inheriting Claude Code's original compact max_tokens, otherwise
    # input+max_output can still exceed smaller downstream model windows.
    base["max_tokens"] = reduce_max_tokens()
    base.pop("max_output_tokens", None)
    transcript = _render_messages_for_summary(segment_messages)
    base["messages"] = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": _render_template(settings()["segmentPrompt"], {
                "segment_index": segment_index,
                "segment_count": segment_count,
                "transcript": transcript,
            }),
        }],
    }]
    return base


def build_reduce_summary_body(original_body: dict[str, Any], segment_summaries: list[str]) -> dict[str, Any]:
    base, _meta = sanitized_compact_base(original_body)
    base["stream"] = False
    base["max_tokens"] = reduce_max_tokens()
    base.pop("max_output_tokens", None)
    prompt = _compact_source_prompt(original_body.get("messages") or [])
    non_empty_summaries = [summary.strip() for summary in segment_summaries if summary.strip()]
    summaries = "\n\n".join(
        f"## Segment {i + 1}\n{summary}"
        for i, summary in enumerate(non_empty_summaries)
    )
    latest_summary = non_empty_summaries[-1] if non_empty_summaries else ""
    base["messages"] = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": _render_template(settings()["reducePrompt"], {
                "compact_prompt": prompt,
                "summaries": summaries,
                "latest_summary": latest_summary,
            }),
        }],
    }]
    return base


def anthropic_message(text: str, *, model: str | None = None) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model or "compact-rescue",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def extract_anthropic_message_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)
