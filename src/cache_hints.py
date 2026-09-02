"""Cross-protocol prompt-cache hint helpers.

Parrot accepts several protocol dialects whose cache knobs are not named the
same way:

- Anthropic Messages uses top-level/block-level ``cache_control``.
- OpenAI-family APIs route prompt cache via ``prompt_cache_key`` and optionally
  ``prompt_cache_retention``.

This module keeps the mapping deterministic and content-safe: generated cache
keys are hashes of stable prompt prefixes, never raw prompt text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


_CACHE_KEY_PREFIX = "parrot:cache:v1"
_SESSION_ID_RE = re.compile(r"session[_-]?id['\"=:\s]+([A-Za-z0-9_.:-]{8,})", re.IGNORECASE)
_ANTHROPIC_EPHEMERAL_1H = {"type": "ephemeral", "ttl": "1h"}
_ANTHROPIC_EPHEMERAL_5M = {"type": "ephemeral"}


def _canon(value: Any) -> Any:
    """Stable, cache-control-insensitive canonical value for hashing."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in sorted(value.keys(), key=str):
            if k in {"cache_control"}:
                continue
            if isinstance(k, str) and k.startswith("_parrot_"):
                continue
            out[str(k)] = _canon(value[k])
        return out
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def _json_hash(value: Any) -> str:
    raw = json.dumps(_canon(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _session_id_from_value(value: Any) -> str | None:
    """Extract a conversation/session id from common Claude Code metadata shapes.

    Claude Code sends a stable per-session UUID in ``metadata.user_id`` as a
    JSON string.  Prefer that explicit boundary over hashing growing history;
    never put the raw value into the upstream key, only into the hash material.
    """
    if isinstance(value, dict):
        for key in (
            "session_id", "sessionId", "claude_session_id", "claudeSessionId",
            "claude_code_session_id", "claudeCodeSessionId",
        ):
            sid = _clean_str(value.get(key))
            if sid:
                return sid
        for key in ("user_id", "user", "metadata"):
            sid = _session_id_from_value(value.get(key))
            if sid:
                return sid
        return None

    text = _clean_str(value)
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            sid = _session_id_from_value(obj)
            if sid:
                return sid
    match = _SESSION_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def anthropic_session_id(body: dict[str, Any] | None) -> str | None:
    """Return a stable Anthropic/Claude conversation id when the client exposes one."""
    if not isinstance(body, dict):
        return None
    # Internal header-derived hint from server.py.  This is stripped by provider
    # allowlists and never forwarded upstream as-is.
    sid = _clean_str(body.get("_parrot_claude_code_session_id"))
    if sid:
        return sid
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        sid = _session_id_from_value(metadata)
        if sid:
            return sid
    return None


def _iter_content_blocks(content: Any):
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _inject_anthropic_message_cache_control(
    message: dict[str, Any],
    cache_control: dict[str, Any],
) -> dict[str, Any]:
    """Return one message with a breakpoint on its final content block."""
    message = dict(message)
    content = message.get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": dict(cache_control)}
        message["content"] = blocks
    elif isinstance(content, str):
        message["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": dict(cache_control),
        }]
    return message


def _cache_controls_in_value(value: Any):
    if isinstance(value, dict):
        cache_control = value.get("cache_control")
        if isinstance(cache_control, dict):
            yield cache_control
        for key in ("content", "source"):
            yield from _cache_controls_in_value(value.get(key))
    elif isinstance(value, list):
        for item in value:
            yield from _cache_controls_in_value(item)


def _anthropic_block_cache_controls(payload: dict[str, Any]):
    yield from _cache_controls_in_value(payload.get("system"))
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
            yield tool["cache_control"]
    for message in payload.get("messages") or []:
        if isinstance(message, dict):
            yield from _cache_controls_in_value(message.get("content"))


def _anthropic_prompt_order_cache_controls(payload: dict[str, Any]):
    """Yield block cache controls in Anthropic's validation order.

    Anthropic validates mixed TTLs across the whole prompt in this order:
    ``tools`` -> ``system`` -> ``messages``.  Do not recurse into arbitrary tool
    schemas because user JSON Schema examples are not prompt cache controls.
    """
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
            yield tool["cache_control"]
    yield from _cache_controls_in_value(payload.get("system"))
    for message in payload.get("messages") or []:
        if isinstance(message, dict):
            yield from _cache_controls_in_value(message.get("content"))


def promote_anthropic_cache_ttls_for_order(payload: dict[str, Any]) -> int:
    """Promote illegal earlier 5m breakpoints to 1h without reordering content.

    Anthropic rejects a 1h cache breakpoint that appears after a 5m breakpoint.
    Omitted ``ttl`` means the default 5m.  If an invalid mixed sequence exists,
    every 5m breakpoint before the final 1h breakpoint is promoted to 1h; 5m
    breakpoints after that point remain unchanged.  Valid and single-TTL prompts
    are untouched.

    The three prompt sections are copied only when a repair is necessary.  This
    preserves transform callers' no-input-mutation contract while avoiding a
    deep copy on the normal path.  Returns the number of promoted breakpoints.
    """
    if not isinstance(payload, dict):
        return 0

    controls = list(_anthropic_prompt_order_cache_controls(payload))
    last_long = -1
    for index, control in enumerate(controls):
        if (
            str(control.get("type") or "").strip().lower() == "ephemeral"
            and str(control.get("ttl") or "").strip().lower() == "1h"
        ):
            last_long = index
    if last_long <= 0:
        return 0

    def is_five_minute(control: dict[str, Any]) -> bool:
        if str(control.get("type") or "").strip().lower() != "ephemeral":
            return False
        return "ttl" not in control or str(control.get("ttl") or "").strip().lower() == "5m"

    if not any(is_five_minute(control) for control in controls[:last_long]):
        return 0

    for section in ("tools", "system", "messages"):
        if section in payload:
            payload[section] = copy.deepcopy(payload[section])

    promoted = 0
    controls = list(_anthropic_prompt_order_cache_controls(payload))
    for control in controls[:last_long]:
        if is_five_minute(control):
            control["ttl"] = "1h"
            promoted += 1
    return promoted


def apply_anthropic_tools_cache_breakpoint(
    tools: Any,
    *,
    cache_control: dict[str, Any] | None = None,
) -> bool:
    """Put one tool breakpoint on the last persistent Anthropic tool.

    A deferred tool is loaded on demand and must not become the cache boundary.
    Existing explicit tool breakpoints are preserved; when every tool is
    deferred there is no valid tool breakpoint to add.
    """
    if not isinstance(tools, list) or not tools:
        return False
    if any(
        isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict)
        for tool in tools
    ):
        return False
    for index in range(len(tools) - 1, -1, -1):
        tool = tools[index]
        if not isinstance(tool, dict) or tool.get("defer_loading") is True:
            continue
        tools[index] = {
            **tool,
            "cache_control": dict(cache_control or _ANTHROPIC_EPHEMERAL_1H),
        }
        return True
    return False


def apply_anthropic_block_cache_breakpoints(
    payload: dict[str, Any],
    *,
    max_breakpoints: int = 4,
    default_cache_control: dict[str, Any] | None = None,
) -> None:
    """Fill missing Anthropic cache sections without exceeding four blocks.

    Breakpoint priority follows the prompt prefix order: final persistent tool,
    final system block, final message, then the second-to-last user turn. An
    existing breakpoint suppresses auto-injection only in its own section, not
    in unrelated sections. Existing client controls are never moved or removed.
    """
    if not isinstance(payload, dict) or max_breakpoints <= 0:
        return

    generated_control = dict(default_cache_control or _ANTHROPIC_EPHEMERAL_1H)
    existing = list(_anthropic_block_cache_controls(payload))
    remaining = max_breakpoints - len(existing)
    if remaining <= 0:
        return

    tools = payload.get("tools")
    tool_controls = [
        tool["cache_control"]
        for tool in tools or []
        if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict)
    ]
    if not tool_controls and apply_anthropic_tools_cache_breakpoint(
        tools, cache_control=generated_control,
    ):
        remaining -= 1
        tool_controls = list(_anthropic_block_cache_controls({"tools": tools}))
    if remaining <= 0:
        return
    short_ttl_seen = any(control.get("ttl") != "1h" for control in tool_controls)

    system = payload.get("system")
    system_controls = list(_cache_controls_in_value(system))
    if not system_controls and isinstance(system, list) and system and isinstance(system[-1], dict):
        cache_control = _ANTHROPIC_EPHEMERAL_5M if short_ttl_seen else generated_control
        blocks = list(system)
        blocks[-1] = {**blocks[-1], "cache_control": dict(cache_control)}
        payload["system"] = blocks
        system = blocks
        system_controls = [dict(cache_control)]
        remaining -= 1
    if remaining <= 0:
        return
    short_ttl_seen = short_ttl_seen or any(
        control.get("ttl") != "1h" for control in system_controls
    )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    message_controls = [
        control
        for message in messages
        if isinstance(message, dict)
        for control in _cache_controls_in_value(message.get("content"))
    ]
    if message_controls:
        return

    cache_control = _ANTHROPIC_EPHEMERAL_5M if short_ttl_seen else generated_control
    target_indices: list[int] = []
    if isinstance(messages[-1], dict):
        target_indices.append(len(messages) - 1)
    if len(messages) >= 4:
        user_count = 0
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if isinstance(message, dict) and message.get("role") == "user":
                user_count += 1
                if user_count == 2:
                    target_indices.append(index)
                    break

    messages = list(messages)
    for index in target_indices:
        if remaining <= 0:
            break
        updated = _inject_anthropic_message_cache_control(
            messages[index], cache_control,
        )
        if _has_block_cache_control(updated.get("content")):
            messages[index] = updated
            remaining -= 1
    payload["messages"] = messages


def _has_block_cache_control(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("cache_control"), dict):
            return True
        # Tool schemas can be arbitrarily deep; only recurse through known
        # Anthropic wrapper fields to avoid treating user JSON schema examples as
        # cache-control hints.
        for key in ("content", "source"):
            if _has_block_cache_control(value.get(key)):
                return True
    elif isinstance(value, list):
        return any(_has_block_cache_control(item) for item in value)
    return False


def has_anthropic_cache_control(body: dict[str, Any] | None) -> bool:
    """Whether an Anthropic-shaped request contains explicit cache controls."""
    if not isinstance(body, dict):
        return False
    if isinstance(body.get("cache_control"), dict):
        return True
    if _has_block_cache_control(body.get("system")):
        return True
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and _has_block_cache_control(msg.get("content")):
            return True
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
            return True
    return False


def top_level_cache_control(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    cc = body.get("cache_control")
    if not isinstance(cc, dict):
        return None
    typ = cc.get("type")
    if typ != "ephemeral":
        # Anthropic currently only supports ephemeral; let upstream validate
        # unknown future values by preserving the object shape rather than
        # inventing a fallback.
        return dict(cc)
    out = {"type": "ephemeral"}
    ttl = cc.get("ttl")
    if isinstance(ttl, str) and ttl:
        out["ttl"] = ttl
    return out


def _cache_ttl(body: dict[str, Any] | None) -> str | None:
    cc = top_level_cache_control(body)
    if isinstance(cc, dict):
        ttl = cc.get("ttl")
        if isinstance(ttl, str) and ttl:
            return ttl
    # If the user only placed block-level cache controls, use the longest TTL
    # present as the cross-protocol retention hint.
    ttls: list[str] = []

    def visit(v: Any) -> None:
        if isinstance(v, dict):
            c = v.get("cache_control")
            if isinstance(c, dict) and isinstance(c.get("ttl"), str):
                ttls.append(c["ttl"])
            for key in ("system", "messages", "tools", "content"):
                if key in v:
                    visit(v[key])
        elif isinstance(v, list):
            for item in v:
                visit(item)

    visit(body or {})
    if "1h" in ttls:
        return "1h"
    return ttls[0] if ttls else None


def _model_prefers_24h_retention(model: str | None) -> bool:
    name = str(model or "").lower()
    return name.startswith("gpt-5.5") or name.startswith("gpt-5.5-pro")


def openai_retention_from_anthropic(body: dict[str, Any] | None, *, model: str | None = None) -> str | None:
    """Best-effort Anthropic TTL → OpenAI retention mapping.

    OpenAI's prompt cache is automatic; retention is a routing/storage policy.
    Anthropic 1h is best approximated by OpenAI 24h.  For GPT-5.5+ OpenAI docs
    say only 24h is supported, so set it explicitly.
    """
    ttl = _cache_ttl(body)
    if ttl == "1h" or _model_prefers_24h_retention(model):
        return "24h"
    return None


def _anthropic_stable_prefix(body: dict[str, Any]) -> dict[str, Any]:
    """Extract a stable cache-routing prefix from an Anthropic request.

    Exclude the latest user turn so multi-turn conversations keep the same key
    while their dynamic tail changes.  Keep tools/system and older history,
    because those are exactly the reusable expensive prefix.
    """
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    prefix_messages = list(messages)
    if prefix_messages and isinstance(prefix_messages[-1], dict) and prefix_messages[-1].get("role") == "user":
        prefix_messages = prefix_messages[:-1]
    material: dict[str, Any] = {}
    if body.get("tools"):
        material["tools"] = body.get("tools")
    if body.get("system"):
        material["system"] = body.get("system")
    if prefix_messages:
        material["messages"] = prefix_messages
    return material


def stable_prompt_cache_key_from_anthropic(
    body: dict[str, Any] | None,
    *,
    model: str | None = None,
    api_key_name: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    if not isinstance(body, dict):
        return None
    model_s = model or body.get("model") or ""
    api_key_s = api_key_name or body.get("_parrot_api_key_name") or ""
    client_ip_s = client_ip or body.get("_parrot_client_ip") or ""

    session_id = anthropic_session_id(body)
    if session_id:
        material = {
            "family": "anthropic-to-openai-session",
            "v": 2,
            "model": model_s,
            "api_key_name": api_key_s,
            "client_ip": client_ip_s,
            "session_id": session_id,
        }
        return f"{_CACHE_KEY_PREFIX}:a2o-session:{_json_hash(material)}"

    prefix = _anthropic_stable_prefix(body)
    if not prefix:
        # With only a single dynamic user message and no static prefix there is
        # nothing useful to route together.
        return None
    material = {
        "family": "anthropic-to-openai",
        "model": model_s,
        "api_key_name": api_key_s,
        "client_ip": client_ip_s,
        "prefix": prefix,
    }
    return f"{_CACHE_KEY_PREFIX}:a2o:{_json_hash(material)}"


def apply_anthropic_cache_to_openai_payload(
    source_body: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    model: str | None = None,
    api_key_name: str | None = None,
    client_ip: str | None = None,
    force: bool = True,
) -> None:
    """Mutate an OpenAI-family payload with cache routing hints.

    ``force=True`` means Anthropic-originated requests get a stable key even if
    the client forgot cache_control.  This mirrors OpenAI handler's native
    autoPromptCacheKey behaviour and prevents expensive long prefixes from
    missing cache solely because the ingress protocol was Anthropic.
    """
    if not isinstance(payload, dict):
        return
    if not force and not has_anthropic_cache_control(source_body):
        return
    if not str(payload.get("prompt_cache_key") or "").strip():
        key = stable_prompt_cache_key_from_anthropic(
            source_body,
            model=model,
            api_key_name=api_key_name,
            client_ip=client_ip,
        )
        if key:
            payload["prompt_cache_key"] = key
    if not payload.get("prompt_cache_retention"):
        retention = openai_retention_from_anthropic(source_body, model=model)
        if retention:
            payload["prompt_cache_retention"] = retention


def anthropic_cache_control_from_openai(body: dict[str, Any] | None) -> dict[str, Any] | None:
    """Best-effort OpenAI prompt cache hint → Anthropic cache_control."""
    if not isinstance(body, dict):
        return None
    if not (body.get("prompt_cache_key") or body.get("prompt_cache_retention")):
        return None
    cc: dict[str, Any] = {"type": "ephemeral"}
    # Anthropic cannot do 24h; 1h is the strongest available approximation.
    if body.get("prompt_cache_retention") == "24h":
        cc["ttl"] = "1h"
    return cc


def apply_openai_cache_to_anthropic_payload(source_body: dict[str, Any] | None, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    cc = anthropic_cache_control_from_openai(source_body)
    if not cc:
        return
    if not payload.get("cache_control"):
        payload["cache_control"] = cc
    apply_anthropic_block_cache_breakpoints(payload)
