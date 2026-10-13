"""OpenAI Responses ↔ Anthropic Messages non-stream bridge.

Phase 8 fourth path: Responses ingress → Anthropic upstream, non-stream only.
This module intentionally composes the already-tested Responses↔Chat and
Chat↔Anthropic translators instead of duplicating the whole mapping table.

Compatibility policy preserves input/function-call/tool-result content, maps
supported format/reasoning/tool controls, and strips only unsupported request
hints. Opaque state or content parts that would be corrupted are rejected.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ... import cache_hints
from .. import anthropic_reasoning_replay as native_replay
from . import chat_to_anthropic, common, guard, responses_to_chat


def _fail(message: str, *, param: str | None = None) -> None:
    raise guard.GuardError(400, "invalid_request_error", message, param=param)


_TOOL_NAME_BAD = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_NAME_MAX = 64
_CUSTOM_RAW_KEY = "__parrot_raw_input"

@dataclass(frozen=True)
class ToolWireIdentity:
    kind: str
    namespace: str | None
    child_name: str

@dataclass
class NamespaceToolMap:
    """Per-request reversible Responses identity to Anthropic flat-name plan."""
    by_flat_name: dict[str, ToolWireIdentity] = field(default_factory=dict)
    by_identity: dict[ToolWireIdentity, str] = field(default_factory=dict)
    raw_custom_flats: set[str] = field(default_factory=set)

    def reserve_direct(self, kind: str, name: str) -> None:
        identity = ToolWireIdentity(kind, None, name)
        if name in self.by_flat_name or identity in self.by_identity:
            _fail(f"duplicate or colliding Responses tool declaration {name!r}", param="tools")
        self.by_flat_name[name] = identity
        self.by_identity[identity] = name

    def flat_name(self, kind: str, namespace: str, child_name: str) -> str:
        identity = ToolWireIdentity(kind, namespace, child_name)
        if identity in self.by_identity:
            return self.by_identity[identity]
        raw = f"{namespace}__{child_name}"
        base = _TOOL_NAME_BAD.sub("_", raw).strip("_") or "namespaced_tool"
        digest = hashlib.sha256(f"{kind}\0{namespace}\0{child_name}".encode()).hexdigest()[:10]
        if len(base) > _TOOL_NAME_MAX:
            base = f"{base[:_TOOL_NAME_MAX - 12]}__{digest}"
        candidate = base
        if candidate in self.by_flat_name:
            candidate = f"{base[:_TOOL_NAME_MAX - 12]}__{digest}"
        suffix = 2
        while candidate in self.by_flat_name:
            tail = f"_{suffix}"
            candidate = f"{base[:_TOOL_NAME_MAX-len(tail)]}{tail}"
            suffix += 1
        self.by_flat_name[candidate] = identity
        self.by_identity[identity] = candidate
        return candidate

    def identity_for_flat(self, name: str) -> ToolWireIdentity | None:
        return self.by_flat_name.get(name)

def _flatten_response_tools(tools: Any, plan: NamespaceToolMap) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    # Reserve every direct name first, including names that historical namespace
    # calls must avoid even when that namespace child is no longer declared.
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        typ = tool.get("type")
        if typ in (None, "function", "custom"):
            name = str(tool.get("name") or "")
            if not name:
                _fail("Responses tools require a non-empty name", param="tools")
            plan.reserve_direct("custom" if typ == "custom" else "function", name)
    flattened: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        typ = tool.get("type")
        if typ in (None, "function"):
            flattened.append(copy.deepcopy(tool)); continue
        if typ == "custom":
            plan.raw_custom_flats.add(str(tool["name"]))
            flattened.append(_wrap_custom_tool(tool)); continue
        if typ != "namespace":
            continue
        namespace, children = str(tool.get("name") or ""), tool.get("tools")
        if not namespace or not isinstance(children, list):
            _fail("Responses namespace tools require a non-empty name and tools array", param="tools")
        seen: set[ToolWireIdentity] = set()
        for child in children:
            if not isinstance(child, dict):
                _fail("Responses namespace children must be tool objects", param="tools")
            kind, child_name = str(child.get("type") or "function"), str(child.get("name") or "")
            if not child_name:
                _fail("Responses namespace children require a non-empty name", param="tools")
            identity = ToolWireIdentity(kind, namespace, child_name)
            if identity in seen or identity in plan.by_identity:
                _fail(f"duplicate Responses namespace tool {namespace}.{child_name}", param="tools")
            seen.add(identity)
            if kind not in ("function", "custom"):
                _fail("Responses namespace children must be function or custom tools", param="tools")
            flat = _wrap_custom_tool(child) if kind == "custom" else copy.deepcopy(child)
            flat["type"] = "function"; flat["name"] = plan.flat_name(kind, namespace, child_name)
            if kind == "custom":
                plan.raw_custom_flats.add(flat["name"])
            flattened.append(flat)
    return flattened

def _wrap_custom_tool(tool: dict) -> dict:
    fmt = tool.get("format")
    if fmt is not None and (not isinstance(fmt, dict) or fmt.get("type") != "text"):
        _fail("custom tool grammar cannot be enforced by Anthropic JSON-schema tools", param="tools")
    return {"type": "function", "name": tool.get("name"),
            "description": str(tool.get("description") or "") + " Return the tool's raw text in __parrot_raw_input.",
            "strict": True,
            "parameters": {"type": "object", "properties": {_CUSTOM_RAW_KEY: {"type": "string"}},
                           "required": [_CUSTOM_RAW_KEY], "additionalProperties": False}}


def _map_namespaced_history(items: list, plan: NamespaceToolMap) -> list:
    out: list[Any] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in ("function_call", "custom_tool_call"):
            out.append(item); continue
        namespace = item.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            out.append(item); continue
        normalized = copy.deepcopy(item)
        kind = "custom" if item.get("type") == "custom_tool_call" else "function"
        normalized["name"] = plan.flat_name(kind, namespace, str(item.get("name") or ""))
        normalized.pop("namespace", None); out.append(normalized)
    return out

def _map_tool_choice(choice: Any, plan: NamespaceToolMap) -> Any:
    if not isinstance(choice, dict):
        return choice
    def selected_name(item: dict) -> str:
        namespace = item.get("namespace")
        kind = item.get("type") or "function"
        name = item.get("name")
        if kind not in ("function", "custom") or not isinstance(name, str) or not name:
            _fail("tool_choice must select a named function or custom tool", param="tool_choice")
        if namespace is not None:
            identity = ToolWireIdentity(kind, namespace, name)
            if identity not in plan.by_identity:
                _fail("namespaced tool_choice does not match a declared tool", param="tool_choice")
            return plan.by_identity[identity]
        identity = ToolWireIdentity(kind, None, name)
        if identity not in plan.by_identity:
            _fail("tool_choice does not match any declared function tool", param="tool_choice")
        return plan.by_identity[identity]
    if choice.get("type") in ("function", "custom"):
        return {"type": "function", "name": selected_name(choice)}
    if choice.get("type") != "allowed_tools":
        return choice
    selected = choice.get("tools")
    if not isinstance(selected, list) or not selected:
        _fail("allowed_tools must select at least one declared tool", param="tool_choice")
    names = []
    for item in selected:
        if not isinstance(item, dict) or item.get("type") == "namespace":
            _fail("namespaced Responses allowed_tools must identify individual tools", param="tool_choice")
        names.append(selected_name(item))
    return {"type": "allowed_tools", "mode": choice.get("mode", "auto"),
            "tools": [{"type": "function", "name": name} for name in names]}


def _map_output_controls(body: dict, payload: dict, *, target_model: str | None = None) -> None:
    text = body.get("text")
    fmt = text.get("format") if isinstance(text, dict) else None
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        if not isinstance(fmt.get("schema"), dict):
            _fail("text.format.json_schema requires a schema object", param="text.format")
        payload["output_config"] = {"format": {"type": "json_schema", "schema": fmt["schema"]}}
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if effort is None:
        return
    if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        _fail("unsupported reasoning.effort for Anthropic bridge", param="reasoning.effort")
    from ...transform.cc_model_profile import canonical_model, model_profile
    model = canonical_model(target_model or body.get("model"))
    profile = model_profile(model)
    # Claude 3.7 Sonnet supports manual thinking; older 3.x/Haiku 3 models
    # don't. A family-wide 'claude-3' match sent invalid thinking upstream.
    legacy = model == "claude-3-7-sonnet" or (profile is not None and profile.thinking == "enabled")
    if model.startswith("claude-3-") and not legacy:
        return
    if effort == "none":
        payload["thinking"] = {"type": "disabled"}
        return
    modern = bool(re.search(r"claude-(?:opus|sonnet)-4-(?:6|[7-9])(?:\b|[-_])", model)
                  or re.search(r"claude-(?:opus|sonnet|fable|mythos)-[5-9](?:\b|[-_])", model))
    if legacy:
        # Manual thinking is the supported alternative before Claude 4.6.
        # A legal budget is at least 1024 and strictly below max_tokens.
        limit = payload.get("max_tokens", 4096)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 1024:
            depth = {"minimal": 1024, "low": 1024, "medium": 2048,
                     "high": 4096, "xhigh": 8192, "max": 8192}[effort]
            payload["thinking"] = {"type": "enabled", "budget_tokens": min(depth, limit - 1)}
        return
    # The shared 4.6 adaptive mode does not imply shared effort levels.
    # Sonnet 4.6 must not inherit Opus 4.6's max fallback for xhigh.
    if model == "claude-sonnet-4-6" and effort in ("xhigh", "max"):
        claude_effort = "high"
    elif effort == "xhigh":
        claude_effort = "max" if model == "claude-opus-4-6" else (
            "xhigh" if model.startswith(("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5")) else "high"
        )
    else:
        claude_effort = "low" if effort == "minimal" else effort
    payload.setdefault("output_config", {})["effort"] = claude_effort
    if modern:
        payload["thinking"] = {"type": "adaptive"}

def _reconcile_thinking_controls(payload: dict) -> None:
    thinking = payload.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") not in ("enabled", "adaptive"):
        return
    # Claude manual thinking disallows temperature changes and forced tool use.
    # Forced tool selection is a harder constraint than a qualitative reasoning
    # budget, so keep the tool requirement and fall back to no explicit thinking.
    choice = payload.get("tool_choice") or {}
    if isinstance(choice, dict) and choice.get("type") in ("tool", "any"):
        payload.pop("thinking", None)
        if thinking.get("type") == "enabled" and isinstance(payload.get("output_config"), dict):
            payload["output_config"].pop("effort", None)
        return
    payload.pop("temperature", None)
    top_p = payload.get("top_p")
    if isinstance(top_p, (int, float)) and top_p < 0.95:
        payload.pop("top_p", None)

def restore_output_item(item: dict, plan: NamespaceToolMap | None) -> dict:
    if plan is None or not isinstance(item, dict) or item.get("type") not in ("function_call", "custom_tool_call"):
        return item
    identity = plan.identity_for_flat(str(item.get("name") or ""))
    if identity is None:
        return item
    out = copy.deepcopy(item); out["name"] = identity.child_name
    if identity.namespace is not None: out["namespace"] = identity.namespace
    else: out.pop("namespace", None)
    if identity.kind == "custom":
        out["type"] = "custom_tool_call"
        if "arguments" in out:
            args = out.pop("arguments")
            decoded = common.parse_json_object(args)
            if isinstance(decoded, dict) and isinstance(decoded.get(_CUSTOM_RAW_KEY), str):
                out["input"] = decoded[_CUSTOM_RAW_KEY]
            elif str(item.get("name") or "") in plan.raw_custom_flats:
                raise ValueError("Claude custom tool input was not a raw-text wrapper")
            else:
                out["input"] = args
    else: out["type"] = "function_call"
    return out

def _custom_tool_label(body: dict) -> str | None:
    # Text custom declarations are wrapped as JSON-schema tools. Historical
    # custom_tool_call items are conversation/tool state and must not be dropped.
    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "custom_tool_call" and not isinstance(item.get("input"), (str, dict)):
            return "custom_tool_call.input"
        if typ == "custom_tool_call_output":
            try:
                _guard_function_call_output_content(item.get("output"))
            except guard.GuardError:
                raise
    return None


def _item_reference_unresolved_label(body: dict) -> str | None:
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, dict) and item.get("type") == "item_reference":
                return "item_reference"
    inp = body.get("input")
    items = inp if isinstance(inp, list) else []
    known_ids: set[str] = set()
    has_history_anchor = bool(body.get("previous_response_id"))
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "item_reference":
            ref_id = item.get("id")
            if not isinstance(ref_id, str) or not ref_id:
                return "item_reference"
            if ref_id in known_ids or has_history_anchor:
                continue
            return "item_reference"
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            known_ids.add(item_id)
    return None


def _stateful_input_item_label(body: dict) -> str | None:
    unresolved_ref = _item_reference_unresolved_label(body)
    if unresolved_ref:
        return unresolved_ref
    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in {
            "file_search_call", "computer_call",
            "image_generation_call", "code_interpreter_call",
            "mcp_call", "mcp_list_tools", "mcp_approval_request",
            "mcp_approval_response", "local_shell_call", "local_shell_call_output",
        }:
            return str(typ)
    return None


def _guard_input_file_part(part: dict, *, param: str = "input") -> None:
    if part.get("file_id") is not None:
        _fail("file_id-backed files cannot be converted to Anthropic documents without file retrieval", param=param)
    file_data = part.get("file_data")
    file_url = part.get("file_url")
    has_file_data = isinstance(file_data, str) and bool(file_data)
    has_file_url = isinstance(file_url, str) and bool(file_url)
    if file_data is not None and not has_file_data:
        _fail("Responses input_file.file_data must be a non-empty string for Anthropic document conversion", param=param)
    if file_url is not None and not has_file_url:
        _fail("Responses input_file.file_url must be a non-empty string for Anthropic document conversion", param=param)
    if has_file_data and has_file_url:
        _fail("Responses input_file cannot contain both file_data and file_url for Anthropic document conversion", param=param)
    if not has_file_data and not has_file_url:
        _fail("Responses input_file requires non-empty file_data or file_url for Anthropic document conversion", param=param)
    if has_file_data and file_data.startswith("data:"):
        header, sep, encoded = file_data.partition(",")
        if not sep or ";base64" not in header or not encoded:
            _fail("Responses input_file.file_data data URL must be base64 encoded", param=param)


def _guard_input_image_part(part: dict, *, param: str = "input") -> None:
    if part.get("file_id") is not None:
        _fail("file_id-backed images cannot be converted to Anthropic images without file retrieval", param=param)
    url = part.get("image_url")
    if not isinstance(url, str) or not url:
        _fail("Responses input_image requires non-empty image_url for Anthropic image conversion", param=param)
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        if not sep or ";base64" not in header or not data:
            _fail("Responses input_image.image_url data URL must be base64 encoded", param=param)


def _guard_function_call_output_content(output: Any) -> None:
    if output is None or isinstance(output, str):
        return
    if not isinstance(output, list):
        return
    for part in output:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            _fail("Responses function_call_output output parts must be objects", param="input")
        typ = part.get("type")
        if typ in ("input_text", "output_text", "text"):
            continue
        if typ == "input_image":
            _guard_input_image_part(part, param="input")
            continue
        if typ == "input_file":
            _guard_input_file_part(part, param="input")
            continue
        if typ == "input_audio":
            _fail("audio tool output is not supported on Responses→Anthropic bridge yet", param="input")
        _fail(
            f"Responses function_call_output output part {typ!r} cannot be safely converted to Anthropic tool_result yet",
            param="input",
        )


def guard_request(body: dict, *, store_enabled: bool = True) -> None:
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    # Supported reasoning/text.format controls are translated below; unsupported
    # projection/cache hints fall back through the bridge output allowlist.
    # include-only reasoning.encrypted_content is a projection hint: Claude
    # cannot produce or replay OpenAI ciphertext. Strip opaque reasoning history
    # before the intermediate Chat conversion rather than rejecting visible turns.
    # `conversation` is different: it names server-side state that this bridge
    # cannot load or replay, so align direct translator calls with the real
    # Responses ingress guard and reject non-null values instead of pretending
    # the conversation context was applied.
    if body.get("conversation"):
        _fail("conversation resource is not supported on Responses→Anthropic bridge", param="conversation")
    if body.get("background") is True:
        _fail("background async response is not supported on Responses→Anthropic bridge", param="background")
    from ...protocols.matrix import _hosted_tool_labels
    from ...local_web_tools import is_openai_web_search_tool_type
    for label in _hosted_tool_labels("responses", body):
        kind = label.split(":")[-1]
        if kind not in ("namespace", "custom") and not is_openai_web_search_tool_type(kind):
            raise guard.GuardError(400, "invalid_request_error",
                f"Responses {label} requires a native-capable candidate", param="tools", scope="candidate")
    custom_label = _custom_tool_label(body)
    if custom_label:
        _fail(
            f"Responses {custom_label} cannot be safely converted to Anthropic tool history yet",
            param="input",
        )
    stateful_label = _stateful_input_item_label(body)
    if stateful_label:
        _fail(
            f"Responses {stateful_label} history item cannot be safely converted to Anthropic Messages",
            param="input",
        )

    # Do not reuse the stricter Responses→Chat guard here: it is intentionally
    # designed for a strict OpenAI Chat upstream.  For Anthropic fallback we let
    # responses_to_chat skip/strip Responses-only state items, while
    # previous_response_id Store errors are still raised by _resolve_input().
    _ = store_enabled

    for item in _current_input_items_for_guard(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        role = item.get("role")
        if typ == "reasoning":
            continue
        if typ == "function_call_output":
            _guard_function_call_output_content(item.get("output"))
        if typ == "input_image" and role != "user":
            _fail("Responses input_image is only supported in user messages on Anthropic bridge", param="input")
        if typ in ("input_file", "file"):
            _guard_input_file_part(item, param="input")
            _fail("Responses input_file is only supported inside user message content on Anthropic bridge", param="input")
        content = item.get("content")
        parts = content if isinstance(content, list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            pt = part.get("type")
            if pt == "input_image" and role != "user":
                _fail("Responses input_image is only supported in user messages on Anthropic bridge", param="input")
            if pt == "input_image":
                _guard_input_image_part(part, param="input")
            if pt in ("input_file", "file"):
                _guard_input_file_part(part, param="input")
            if pt == "input_audio":
                _fail("audio input is not supported on Responses→Anthropic bridge yet", param="input")


def _current_input_items_for_guard(body: dict) -> list:
    items: list = []
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        items.extend(instructions)
    cur = body.get("input")
    if isinstance(cur, str):
        items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": cur}]})
        return items
    if isinstance(cur, list):
        items.extend(cur)
    return items


def resolve_current_input_items(body: dict) -> list:
    return responses_to_chat.resolve_current_input_items(body)


def _function_output_part_to_chat_tool_part(part: dict[str, Any]) -> dict[str, Any]:
    typ = part.get("type")
    if typ in ("input_text", "output_text", "text"):
        return {"type": "text", "text": str(part.get("text") or "")}
    if typ == "input_image":
        _guard_input_image_part(part, param="input")
        image_url: dict[str, Any] = {"url": part.get("image_url") or ""}
        if part.get("detail"):
            image_url["detail"] = part.get("detail")
        return {"type": "image_url", "image_url": image_url}
    if typ == "input_file":
        _guard_input_file_part(part, param="input")
        file_obj: dict[str, Any] = {}
        if part.get("file_data") is not None:
            file_obj["file_data"] = part.get("file_data")
        if part.get("file_url") is not None:
            file_obj["file_url"] = part.get("file_url")
        if part.get("filename"):
            file_obj["filename"] = part.get("filename")
        return {"type": "file", "file": file_obj}
    if typ == "input_audio":
        _fail("audio tool output is not supported on Responses→Anthropic bridge yet", param="input")
    _fail(
        f"Responses function_call_output output part {typ!r} cannot be safely converted to Anthropic tool_result yet",
        param="input",
    )
    return {"type": "text", "text": ""}


def _function_call_output_attachment_content(output: Any) -> tuple[Any, bool]:
    if not isinstance(output, list):
        return output, False
    has_attachment = False
    content: list[dict[str, Any]] = []
    for part in output:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            _fail("Responses function_call_output output parts must be objects", param="input")
        typ = part.get("type")
        if typ in ("input_image", "input_file"):
            has_attachment = True
        content.append(_function_output_part_to_chat_tool_part(part))
    return content, has_attachment


def _function_call_output_attachment_replacements(input_items: list) -> list[tuple[str, Any]]:
    replacements: list[tuple[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        content, has_attachment = _function_call_output_attachment_content(item.get("output"))
        if has_attachment:
            replacements.append((str(item.get("call_id") or ""), content))
    return replacements


def _reasoning_history_blocks(item: dict, *, allow_unsigned_thinking: bool) -> list[dict]:
    if not common.reasoning_passthrough_enabled():
        return []
    if not allow_unsigned_thinking:
        block = native_replay.decode(item)
        return [block] if block is not None else []
    # Readable summaries are not Claude signatures or ordinary assistant text.
    readable: list[str] = []
    for field, kind in (("summary", "summary_text"), ("content", "reasoning_text")):
        for part in item.get(field) or []:
            if isinstance(part, dict) and part.get("type") == kind and isinstance(part.get("text"), str) and part["text"]:
                readable.append(part["text"])
        if readable:
            break
    if not readable:
        return []
    return [{"type": "thinking", "thinking": "\n\n".join(readable)}]


def repair_tool_thinking(payload: dict, input_items: list, *, allow_unsigned_thinking: bool = False) -> None:
    if allow_unsigned_thinking:
        return
    # Inspect missing history while expanded Store items are still available,
    # even before CC injects default thinking. The final channel pass then sees
    # the cleaned tool turn and can omit manual thinking without rereading Store.
    history = [item for item in input_items if isinstance(item, dict) and item.get("role") not in ("system", "developer")]
    if not history or history[-1].get("type") not in ("function_call_output", "custom_tool_call_output"):
        return
    latest: list[dict] = []
    current: list[dict] = []
    for item in history:
        if item.get("type") in ("reasoning", "function_call", "custom_tool_call", "web_search_call") or item.get("role") == "assistant":
            current.append(item)
            latest = current
        else:
            current = []
    missing_native = any(native_replay.decode(item) is None for item in latest if item.get("type") == "reasoning")
    messages = payload.get("messages") or []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        blocks = message.get("content") or []
        calls = {block.get("id") for block in blocks if block.get("type") == "tool_use"}
        results = {block.get("tool_use_id") for following in messages[index + 1:]
                   for block in following.get("content") or [] if block.get("type") == "tool_result"}
        if calls & results and (missing_native or not blocks or blocks[0].get("type") not in ("thinking", "redacted_thinking")):
            native_replay.drop_thinking_history(payload)
        return


def _normalize_custom_tool_history(input_items: list, plan: NamespaceToolMap) -> list:
    """Map safe Responses custom tool history to function-call history.

    Anthropic tool_use.input is an object. Wrap freeform text in one string
    property; keep object-shaped historical inputs unchanged.
    """
    out: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        typ = item.get("type")
        if typ == "custom_tool_call":
            raw = item.get("input")
            if isinstance(raw, str) and str(item.get("name") or "") in plan.raw_custom_flats:
                input_obj = {_CUSTOM_RAW_KEY: raw}
            else:
                input_obj = common.parse_json_object(raw)
                if input_obj is None and isinstance(raw, str):
                    input_obj = {_CUSTOM_RAW_KEY: raw}
            if input_obj is None:
                _fail("custom_tool_call.input must be text or JSON object", param="input")
            normalized = copy.deepcopy(item)
            normalized["type"] = "function_call"
            normalized["arguments"] = json.dumps(input_obj, ensure_ascii=False, separators=(",", ":"))
            normalized.pop("input", None)
            out.append(normalized)
            continue
        if typ == "custom_tool_call_output":
            normalized = copy.deepcopy(item)
            normalized["type"] = "function_call_output"
            out.append(normalized)
            continue
        out.append(item)
    return out


def _preserve_function_call_output_attachments(chat_payload: dict, input_items: list) -> None:
    replacements = _function_call_output_attachment_replacements(input_items)
    if not replacements:
        return

    by_call_id: dict[str, list[Any]] = {}
    for call_id, content in replacements:
        by_call_id.setdefault(call_id, []).append(content)

    replaced = 0
    messages = chat_payload.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            call_id = str(msg.get("tool_call_id") or "")
            queue = by_call_id.get(call_id)
            if not queue:
                continue
            msg["content"] = queue.pop()
            replaced += 1

    if replaced != len(replacements):
        _fail("Responses function_call_output attachments could not be preserved on Anthropic bridge", param="input")


def _preserve_deferred_tool_loading(chat_payload: dict, response_tools: Any) -> None:
    """Carry Responses defer_loading through the intermediate Chat tool shape."""
    chat_tools = chat_payload.get("tools")
    if not isinstance(chat_tools, list) or not isinstance(response_tools, list):
        return
    deferred_by_name = {
        str(tool.get("name") or ""): tool["defer_loading"]
        for tool in response_tools
        if (
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and isinstance(tool.get("defer_loading"), bool)
            and str(tool.get("name") or "")
        )
    }
    for target in chat_tools:
        if not isinstance(target, dict):
            continue
        function = target.get("function")
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        if name in deferred_by_name:
            target["defer_loading"] = deferred_by_name[name]


def translate_request(
    body: dict, *, api_key_name: str = "", store_enabled: bool = True,
    namespace_tool_map: NamespaceToolMap | None = None,
    target_model: str | None = None,
    allow_unsigned_thinking: bool = False,
) -> dict:
    guard_request(body, store_enabled=store_enabled)
    bridge_body = dict(body)
    plan = namespace_tool_map if namespace_tool_map is not None else NamespaceToolMap()
    # Do not let the intermediate Responses→Chat payload reintroduce cache hints
    # as if they were user-supplied Chat fields; translate them once after
    # composition instead.
    bridge_body.pop("prompt_cache_key", None)
    bridge_body.pop("prompt_cache_retention", None)
    from ... import local_web_tools
    hosted_search = [t for t in body.get("tools") or [] if isinstance(t, dict) and local_web_tools.is_openai_web_search_tool_type(t.get("type"))]
    flattened_tools = _flatten_response_tools(bridge_body.get("tools"), plan)
    if isinstance(bridge_body.get("tools"), list):
        bridge_body["tools"] = flattened_tools
    choice = bridge_body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") not in (None, "function", "custom", "allowed_tools"):
        bridge_body.pop("tool_choice", None)
    else:
        bridge_body["tool_choice"] = _map_tool_choice(choice, plan)
    input_items = responses_to_chat.resolve_input_items(bridge_body, api_key_name=api_key_name)
    input_items = responses_to_chat.bridge_history_items(input_items)
    try:
        guard_request({**body, "input": input_items}, store_enabled=store_enabled)
    except guard.GuardError as exc:
        raise guard.GuardError(exc.status, exc.err_type, exc.message, param=exc.param, scope="candidate") from exc
    input_items = _map_namespaced_history(input_items, plan)
    input_items = _normalize_custom_tool_history(input_items, plan)
    bridge_body["_parrot_preserve_native_search"] = True
    without_history = bool(body.get(native_replay.DROP_HISTORY_KEY))
    chat_payload = responses_to_chat.translate_request_from_input_items(
        bridge_body, input_items,
        reasoning_blocks=lambda item: [] if without_history else _reasoning_history_blocks(item, allow_unsigned_thinking=allow_unsigned_thinking),
    )
    _preserve_deferred_tool_loading(chat_payload, flattened_tools)
    _preserve_function_call_output_attachments(chat_payload, input_items)
    # chat_to_anthropic runs its own guard too; this is intentional because it
    # catches fields introduced by the Responses→Chat mapping (response_format,
    # reasoning_effort, etc.) before anything reaches Anthropic upstream.
    payload = chat_to_anthropic.translate_request(
        chat_payload, allow_file_url_documents=True, allow_thinking_blocks=True,
    )
    from ... import search_hosted_codec
    for message in payload.get("messages") or []:
        for block in message.get("content") or []:
            if isinstance(block, dict) and search_hosted_codec.is_anthropic_search(block):
                block.pop(search_hosted_codec.SOURCE, None)
    from ...search_native_tools import to_anthropic
    for tool in hosted_search:
        payload.setdefault("tools", []).append(to_anthropic(tool))
    if isinstance(choice, dict) and local_web_tools.is_openai_web_search_tool_type(choice.get("type")):
        payload["tool_choice"] = {"type": "tool", "name": "web_search"}
    cache_hints.apply_openai_cache_to_anthropic_payload(body, payload)
    # Native controls are generated here, not passed through from Chat input.
    _map_output_controls(body, payload, target_model=target_model)
    _reconcile_thinking_controls(payload)
    if without_history:
        native_replay.drop_thinking_history(payload, allow_unsigned_thinking=allow_unsigned_thinking)
    else:
        repair_tool_thinking(payload, input_items, allow_unsigned_thinking=allow_unsigned_thinking)
    return common.filter_anthropic_bridge_payload(payload)


def translate_response(
    message: dict,
    *,
    model: str = "",
    previous_response_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
    channel_key: Optional[str] = None,
    current_input_items: Optional[list] = None,
    namespace_tool_map: NamespaceToolMap | None = None,
    allow_unsigned_thinking: bool = False,
) -> dict:
    from ... import search_hosted_codec
    hosted = search_hosted_codec.anthropic_to_responses(message.get("content") or [])
    chat_obj = chat_to_anthropic.translate_response(message, model=model)
    if message.get("stop_reason") == "pause_turn":
        chat_obj["choices"][0]["finish_reason"] = "pause_turn"  # internal composition marker only
    keep_reasoning = common.reasoning_passthrough_enabled()
    preserve_native = keep_reasoning and not allow_unsigned_thinking
    def ordered_output(items: list[dict]) -> list[dict]:
        # Chat flattens all text ahead of its tool_calls. Reapply the original
        # Anthropic block positions before writing the resulting response/store.
        calls = iter(item for item in items if item.get("type") in ("function_call", "custom_tool_call"))
        hosted_by_id = {str(item.get("id")): item for item in hosted}
        ordered: list[dict] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    ordered.append({
                        "type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
                        "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    })
            elif kind in ("thinking", "redacted_thinking"):
                text = block.get("thinking") if kind == "thinking" else None
                readable = keep_reasoning and isinstance(text, str) and bool(text)
                carrier = native_replay.encode(block) if preserve_native else None
                if readable or carrier:
                    item = {"type": "reasoning", "id": f"rs_{uuid.uuid4().hex[:24]}",
                            "summary": [{"type": "summary_text", "text": text}] if readable else []}
                    if carrier:
                        item["encrypted_content"] = carrier
                    ordered.append(item)
            elif kind == "tool_use":
                call = next(calls, None)
                if call is not None:
                    ordered.append(call)
            elif kind == "server_tool_use" and block.get("name") == "web_search":
                item = hosted_by_id.pop(str(block.get("id")), None)
                if item is not None:
                    ordered.append(item)
        return ordered

    return responses_to_chat.translate_response(
        chat_obj,
        model=model,
        previous_response_id=previous_response_id,
        api_key_name=api_key_name,
        channel_key=channel_key,
        current_input_items=current_input_items,
        output_ordering=ordered_output,
        output_item_transform=(
            (lambda item: restore_output_item(item, namespace_tool_map))
            if namespace_tool_map is not None else None
        ),
    )
