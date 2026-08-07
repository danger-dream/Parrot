"""Protocol routing matrix.

Phase 7 replaced the scheduler's hard-coded family filter with an explicit
matrix. Phase 8 opens the safe cross-family subset and keeps unsafe/provider
stateful features behind capability guards that produce concrete guard reasons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import config


# Kept in sync with src.openai.transform.guard. Duplicating the small sets here
# keeps the protocol runtime layer independent from the OpenAI transform package
# while still letting scheduler-level capability checks avoid doomed attempts.
_RESPONSES_BUILTIN_TOOL_TYPES = frozenset({
    "web_search_preview", "file_search", "computer_use_preview",
    "code_interpreter", "image_generation", "mcp", "local_shell",
    "web_search", "web_search_2025_08_26", "web_search_preview_2025_03_11",
    "computer", "computer_use", "apply_patch", "function_shell",
})
_RESPONSES_NON_CHAT_TOOL_CHOICE_TYPES = frozenset({
    "file_search", "web_search_preview", "web_search",
    "web_search_2025_08_26", "web_search_preview_2025_03_11",
    "computer_use_preview", "computer", "computer_use",
    "code_interpreter", "image_generation", "mcp", "apply_patch", "function_shell",
})
_RESPONSES_NATIVE_PASSTHROUGH_TOOL_TYPES = frozenset({"tool_search", "namespace"})
_RESPONSES_BUILTIN_INPUT_ITEM_TYPES = frozenset({
    "web_search_call", "file_search_call", "computer_call",
    "image_generation_call", "code_interpreter_call", "mcp_call",
    "mcp_list_tools", "mcp_approval_request", "mcp_approval_response",
    "local_shell_call", "local_shell_call_output",
})

_OPENAI_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_OPENAI_SERVICE_TIERS = frozenset({"auto", "default", "flex", "priority"})


def _protocol_bridge_cfg() -> dict[str, Any]:
    root = config.get().get("protocolBridge") or {}
    return root if isinstance(root, dict) else {}


def _anthropic_to_openai_cfg() -> dict[str, Any]:
    root = _protocol_bridge_cfg().get("anthropicToOpenAI") or {}
    return root if isinstance(root, dict) else {}


def _reasoning_cfg() -> dict[str, Any]:
    root = _anthropic_to_openai_cfg().get("reasoning") or {}
    return root if isinstance(root, dict) else {}


def _valid_effort(value: Any, default: str) -> str:
    effort = str(value or "").strip().lower()
    return effort if effort in _OPENAI_REASONING_EFFORTS else default


def _tier_mapping(section: str) -> dict[str, Any]:
    root = _protocol_bridge_cfg().get("serviceTier") or {}
    if not isinstance(root, dict):
        return {}
    mapping = root.get(section) or {}
    return mapping if isinstance(mapping, dict) else {}
_ANTHROPIC_LOCAL_WEB_TOOL_TYPES = frozenset({
    "web_search_20250305",
    "web_search_20260209",
    "web_search_20260318",
    "web_fetch_20250910",
    "web_fetch_20260209",
    "web_fetch_20260309",
    "web_fetch_20260318",
})


@dataclass(frozen=True)
class RoutePlan:
    ingress_protocol: str
    upstream_protocol: str
    conversion_path: list[str] = field(default_factory=list)
    cost: int = 0
    warnings: list[str] = field(default_factory=list)
    required_transforms: list[str] = field(default_factory=list)


class ProtocolGuardError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RequestFeatures:
    stream: bool = False
    wants_multi_candidate: bool = False
    has_tools: bool = False
    has_tool_results: bool = False
    unsupported_tool_result_label: str | None = None
    anthropic_tool_result_responses_unsupported_label: str | None = None
    has_reasoning: bool = False
    has_images: bool = False
    has_audio: bool = False
    chat_audio_output_label: str | None = None
    chat_audio_history_label: str | None = None
    wants_background: bool = False
    has_files: bool = False
    stateful_file_reference_label: str | None = None
    unsupported_file_label: str | None = None
    responses_file_chat_unsupported_label: str | None = None
    responses_file_anthropic_unsupported_label: str | None = None
    anthropic_document_chat_unsupported_label: str | None = None
    anthropic_document_responses_unsupported_label: str | None = None
    openai_image_anthropic_unsupported_label: str | None = None
    responses_image_anthropic_unsupported_label: str | None = None
    responses_tool_output_chat_unsupported_label: str | None = None
    responses_tool_output_anthropic_unsupported_label: str | None = None
    responses_instructions_unsupported_label: str | None = None
    chat_content_responses_unsupported_label: str | None = None
    chat_content_anthropic_unsupported_label: str | None = None
    chat_tool_call_anthropic_unsupported_label: str | None = None
    has_unsupported_image_position: bool = False
    unsupported_image_label: str | None = None
    has_encrypted_reasoning: bool = False
    has_custom_tools: bool = False
    custom_tool_label: str | None = None
    has_hosted_tools: bool = False
    hosted_tool_label: str | None = None
    hosted_tool_labels: tuple[str, ...] = ()
    has_stateful_input_items: bool = False
    stateful_input_item_label: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ChannelCapabilities:
    protocol: str
    transports: frozenset[str] = frozenset()
    native_state: frozenset[str] = frozenset()
    raw: dict[str, Any] | None = None


def canonical_ingress_protocol(ingress_protocol: str) -> str:
    if ingress_protocol == "chat":
        return "openai-chat"
    if ingress_protocol == "responses":
        return "openai-responses"
    return ingress_protocol or "anthropic"


def protocol_family(protocol: str) -> str:
    return "anthropic" if protocol == "anthropic" else "openai"


def _tool_list_has_hosted_or_non_function(ingress_protocol: str, body: dict[str, Any]) -> bool:
    if body.get("container") is not None or body.get("mcp_servers") is not None:
        return True
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        typ = tool.get("type")
        if ingress_protocol == "anthropic":
            # Anthropic function tools normally omit type; Parrot can emulate
            # web_search/web_fetch server tools locally through AnySearch when
            # crossing to OpenAI-family upstreams.
            if typ not in (None, "function") and typ not in _ANTHROPIC_LOCAL_WEB_TOOL_TYPES:
                return True
            continue
        if ingress_protocol == "chat":
            if typ not in (None, "function"):
                return True
            continue
        # Responses: function/custom are user-defined. Hosted/built-in/unknown
        # tools have state Chat/Anthropic cannot safely represent.
        if typ not in (None, "function", "custom"):
            return True
    return False


def _tool_choice_is_hosted_or_non_function(ingress_protocol: str, body: dict[str, Any]) -> bool:
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return False
    typ = choice.get("type")
    if ingress_protocol == "anthropic":
        return typ not in ("auto", "none", "any", "tool")
    if ingress_protocol == "chat":
        if typ == "allowed_tools":
            nested = choice.get("allowed_tools")
            tools = nested.get("tools") if isinstance(nested, dict) else None
            if not isinstance(tools, list) or not tools:
                return True
            return any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools)
        return typ not in (None, "function")
    if typ in _RESPONSES_NON_CHAT_TOOL_CHOICE_TYPES:
        return True
    # Responses function/custom/allowed_tools/auto-ish choices can be handled by the existing
    # Responses↔Chat translator; unknown hosted shapes stay guarded.
    return typ not in (None, "auto", "none", "function", "custom", "allowed_tools")


def _append_unique(labels: list[str], label: str) -> None:
    if label not in labels:
        labels.append(label)


def _responses_tool_type_label(tool_type: Any, *, prefix: str = "") -> str | None:
    if tool_type in (None, "function", "custom"):
        return None
    return f"{prefix}{tool_type}"


def _responses_tool_labels_from_tool_list(tools: Any, *, prefix: str = "") -> tuple[str, ...]:
    if not isinstance(tools, list):
        return ()
    labels: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        label = _responses_tool_type_label(tool.get("type"), prefix=prefix)
        if label:
            _append_unique(labels, str(label))
    return tuple(labels)


def _responses_native_input_labels(body: dict[str, Any]) -> tuple[str, ...]:
    # Codex uses `tool_search_call` / `tool_search_output` history items for
    # client-executed tool discovery.  `tool_search_output.tools[]` can carry
    # namespace specs that the model may call in later turns, so the input
    # history can require both native states even when the current top-level
    # tools list is empty.
    labels: list[str] = []
    for item in _responses_input_like_items(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in ("tool_search_call", "tool_search_output"):
            _append_unique(labels, "tool_search")
            if typ == "tool_search_output":
                for label in _responses_tool_labels_from_tool_list(item.get("tools")):
                    _append_unique(labels, label)
        if typ in ("function_call", "custom_tool_call") and item.get("namespace"):
            _append_unique(labels, "namespace")
    return tuple(labels)


def _hosted_tool_labels(ingress_protocol: str, body: dict[str, Any]) -> tuple[str, ...]:
    # For Responses, Codex OAuth supports native passthrough tool states such
    # as `tool_search` and `namespace`, but not generic hosted tools such as
    # web_search or file_search.  Scan the full request and retain every hosted
    # / native-passthrough requirement so a provider's broad `hosted_tools`
    # capability cannot mask a missing Codex-native `namespace`/`tool_search`,
    # and vice versa.
    if ingress_protocol == "responses":
        labels: list[str] = []
        if body.get("container") is not None:
            _append_unique(labels, "container")
        if body.get("mcp_servers") is not None:
            _append_unique(labels, "mcp_servers")
        for label in _responses_tool_labels_from_tool_list(body.get("tools") or []):
            _append_unique(labels, label)

        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            typ = choice.get("type")
            if typ == "allowed_tools":
                for label in _responses_tool_labels_from_tool_list(
                    choice.get("tools"), prefix="tool_choice:allowed_tools:",
                ):
                    _append_unique(labels, label)
            elif typ in _RESPONSES_NON_CHAT_TOOL_CHOICE_TYPES:
                _append_unique(labels, f"tool_choice:{typ}")
            elif typ not in (None, "auto", "none", "function", "custom"):
                _append_unique(labels, f"tool_choice:{typ}")

        for label in _responses_native_input_labels(body):
            _append_unique(labels, label)
        return tuple(labels)

    if body.get("container") is not None:
        return ("container",)
    if body.get("mcp_servers") is not None:
        return ("mcp_servers",)
    tools = body.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            typ = tool.get("type")
            if ingress_protocol == "anthropic" and typ not in (None, "function"):
                if typ in _ANTHROPIC_LOCAL_WEB_TOOL_TYPES:
                    continue
                return (str(typ),)
            if ingress_protocol == "chat" and typ not in (None, "function"):
                return (str(typ),)
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        typ = choice.get("type")
        if ingress_protocol == "anthropic" and typ not in ("auto", "none", "any", "tool"):
            return (f"tool_choice:{typ}",)
        if ingress_protocol == "chat" and typ not in (None, "function"):
            if typ == "allowed_tools":
                nested = choice.get("allowed_tools")
                tools = nested.get("tools") if isinstance(nested, dict) else None
                if not isinstance(tools, list) or not tools:
                    return ("tool_choice:allowed_tools",)
                for tool in tools:
                    if not isinstance(tool, dict):
                        return ("tool_choice:allowed_tools:invalid",)
                    nested_typ = tool.get("type")
                    if nested_typ != "function":
                        return (f"tool_choice:allowed_tools:{nested_typ}",)
                return ()
            return (f"tool_choice:{typ}",)
    return ()

def _hosted_tool_label(ingress_protocol: str, body: dict[str, Any]) -> str | None:
    labels = _hosted_tool_labels(ingress_protocol, body)
    return labels[0] if labels else None


def _responses_input_like_items(body: dict[str, Any]) -> list[Any]:
    items: list[Any] = []
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        items.extend(instructions)
    inp = body.get("input")
    if isinstance(inp, list):
        items.extend(inp)
    return items


def _responses_item_reference_unresolved_label(body: dict[str, Any]) -> str | None:
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


def _responses_stateful_input_item_label(body: dict[str, Any]) -> str | None:
    if body.get("conversation"):
        return "conversation"
    unresolved_item_reference = _responses_item_reference_unresolved_label(body)
    if unresolved_item_reference:
        return unresolved_item_reference
    items = _responses_input_like_items(body)
    for item in items:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in _RESPONSES_BUILTIN_INPUT_ITEM_TYPES:
            return str(typ)
    return None


def _responses_custom_tool_label(body: dict[str, Any]) -> str | None:
    """Return the first Responses custom-tool shape that needs replay/guarding.

    Freeform custom declarations have no equivalent Anthropic JSON-schema tool.
    Historical custom calls remain safe when their input is an object that can
    become tool_use.input.
    """
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "custom":
            return "custom_tool_declaration"
    for item in _responses_input_like_items(body):
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ == "custom_tool_call":
            if _responses_custom_tool_call_input_object(item.get("input")) is None:
                return "custom_tool_call.input"
        if typ == "custom_tool_call_output":
            unsupported = _responses_custom_tool_output_anthropic_unsupported_label(item.get("output"))
            if unsupported:
                return unsupported
    return None


def _responses_custom_tool_call_input_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _responses_custom_tool_output_anthropic_unsupported_label(output: Any) -> str | None:
    if output is None or isinstance(output, str):
        return None
    if not isinstance(output, list):
        return None
    for part in output:
        if isinstance(part, str):
            continue
        if not isinstance(part, dict):
            return f"custom_tool_call_output:{type(part).__name__}"
        typ = part.get("type")
        if typ in ("input_text", "output_text", "text"):
            continue
        if typ == "input_image":
            image_label = _responses_image_part_anthropic_unsupported_label(part)
            if image_label:
                return f"custom_tool_call_output:{image_label}"
            continue
        if typ == "input_file":
            file_label = _responses_file_part_anthropic_unsupported_label(part)
            if file_label:
                return f"custom_tool_call_output:{file_label}"
            continue
        return f"custom_tool_call_output:{typ or 'object'}"
    return None


def _anthropic_tool_result_unsupported_label(block: dict[str, Any]) -> str | None:
    # OpenAI Chat tool messages and Responses function_call_output input do not
    # have a separate Anthropic-style is_error marker.  Preserve the textual
    # payload and drop only the marker; this keeps Claude Code read-only tool
    # failures from killing the whole bridge while retaining the error text.
    content = block.get("content")
    if content is None or isinstance(content, str):
        return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "tool_reference"):
                continue
            if isinstance(item, dict):
                return f"tool_result:{item.get('type') or 'object'}"
            return f"tool_result:{type(item).__name__}"
        return None
    return f"tool_result:{type(content).__name__}"


def _anthropic_image_unsupported_label(block: dict[str, Any]) -> str | None:
    source = block.get("source")
    if not isinstance(source, dict):
        return "image.source"
    st = source.get("type")
    if st == "base64":
        data = source.get("data")
        return None if isinstance(data, str) and data else "image.base64"
    if st == "url":
        url = source.get("url")
        return None if isinstance(url, str) and url else "image.url"
    return f"image.{st or 'source'}"


def _anthropic_tool_result_responses_unsupported_label(block: dict[str, Any]) -> str | None:
    content = block.get("content")
    if content is None or isinstance(content, str):
        return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                return f"tool_result:{type(item).__name__}"
            typ = item.get("type")
            if typ in ("text", "tool_reference"):
                continue
            if typ == "image":
                image_label = _anthropic_image_unsupported_label(item)
                if image_label:
                    return f"tool_result:{image_label}"
                continue
            if typ == "document":
                doc_label = _anthropic_document_unsupported_label(item, allow_url=True)
                if doc_label:
                    return f"tool_result:{doc_label}"
                continue
            return f"tool_result:{typ or 'object'}"
        return None
    return f"tool_result:{type(content).__name__}"


def _responses_has_encrypted_reasoning_input(body: dict[str, Any]) -> bool:
    """Return whether the request carries encrypted reasoning history.

    `include: ["reasoning.encrypted_content"]` by itself is only a response
    projection hint.  Chat fallback never understands encrypted reasoning and
    drops this field in the translator; Anthropic fallback still rejects real
    encrypted reasoning history because that bridge has no equivalent replay
    mechanism.
    """
    for item in _responses_input_like_items(body):
        if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("encrypted_content"):
            return True
    return False


def _responses_instructions_unsupported_label(body: dict[str, Any]) -> str | None:
    instructions = body.get("instructions")
    if instructions is None or isinstance(instructions, str):
        return None
    if not isinstance(instructions, list):
        return type(instructions).__name__
    for item in instructions:
        if not isinstance(item, dict):
            return type(item).__name__
        typ = item.get("type")
        if typ not in (None, "message"):
            return str(typ or "object")
        role = item.get("role") or "system"
        if role not in ("system", "developer", "user", "assistant"):
            return f"role:{role}"
        content = item.get("content")
        if isinstance(content, str) or content is None:
            continue
        if not isinstance(content, list):
            return "content"
        for part in content:
            if isinstance(part, str):
                continue
            if not isinstance(part, dict):
                return f"content:{type(part).__name__}"
            ptyp = part.get("type")
            if ptyp not in ("input_text", "output_text", "text", "refusal"):
                return f"content:{ptyp or 'object'}"
    return None


def _resolve_anthropic_reasoning_effort(body: dict[str, Any]) -> str | None:
    cfg = _reasoning_cfg()
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        raw_effort = output_config.get("effort")
        effort = str(raw_effort).strip().lower() if isinstance(raw_effort, str) else ""
        if effort == "max":
            return _valid_effort(cfg.get("maxEffort"), "xhigh")
        if effort in _OPENAI_REASONING_EFFORTS:
            return effort
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return None
    typ = str(thinking.get("type") or "").strip().lower()
    if typ == "adaptive":
        return _valid_effort(cfg.get("adaptiveEffort"), "xhigh")
    if typ != "enabled":
        return None
    budget_raw = thinking.get("budget_tokens")
    try:
        budget = int(budget_raw) if budget_raw is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is None:
        return _valid_effort(cfg.get("defaultEnabledEffort"), "high")
    thresholds = cfg.get("budgetThresholds")
    if isinstance(thresholds, list):
        for item in thresholds:
            if not isinstance(item, dict):
                continue
            effort = _valid_effort(item.get("effort"), "")
            if not effort:
                continue
            if "lt" not in item:
                return effort
            try:
                if budget < int(float(str(item.get("lt")).replace(",", "").strip())):
                    return effort
            except Exception:
                continue
    if budget < 4_000:
        return "low"
    if budget < 16_000:
        return "medium"
    return "high"


def _anthropic_top_level_reasoning_is_mappable(body: dict[str, Any]) -> bool:
    has_reasoning_control = body.get("thinking") is not None or body.get("output_config") is not None
    return bool(has_reasoning_control and _resolve_anthropic_reasoning_effort(body))


def _anthropic_thinking_is_disabled(body: dict[str, Any]) -> bool:
    thinking = body.get("thinking")
    return isinstance(thinking, dict) and str(thinking.get("type") or "").strip().lower() == "disabled" and body.get("output_config") is None


def _anthropic_context_management_is_ignorable(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if any(k not in {"edits"} for k in value.keys()):
        return False
    edits = value.get("edits")
    if edits in (None, []):
        return True
    if not isinstance(edits, list):
        return False
    for edit in edits:
        if not isinstance(edit, dict):
            return False
        if not str(edit.get("type") or "").startswith("clear_thinking_"):
            return False
    return True


def _anthropic_service_tier_is_mappable(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    tier = value.strip().lower()
    if not tier:
        return True
    mapping = _tier_mapping("anthropicToOpenAI")
    if tier in mapping:
        return True
    return tier in {"auto", "standard_only"} or tier in _OPENAI_SERVICE_TIERS


def _openai_service_tier_is_mappable_to_anthropic(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    tier = value.strip().lower()
    if not tier:
        return True
    mapping = _tier_mapping("openaiToAnthropic")
    if tier in mapping:
        return True
    return tier in {"auto", "default", "standard_only"}


def _file_data_supported(file_data: Any) -> bool:
    if not isinstance(file_data, str) or not file_data:
        return False
    if file_data.startswith("data:"):
        header, sep, encoded = file_data.partition(",")
        return bool(sep and ";base64" in header and encoded)
    return True


def _chat_file_part_unsupported_label(part: dict[str, Any]) -> str | None:
    file_obj = part.get("file")
    if not isinstance(file_obj, dict):
        return "file"
    if file_obj.get("file_id") is not None:
        return "file_id"
    if file_obj.get("file_url") is not None:
        return "file_url"
    if not _file_data_supported(file_obj.get("file_data")):
        return "file_data"
    return None


def _responses_file_part_chat_unsupported_label(part: dict[str, Any]) -> str | None:
    if part.get("file_url") is not None:
        return "file_url"
    if part.get("file_id") is not None:
        return None
    if not _file_data_supported(part.get("file_data")):
        return "file_data"
    return None


def _responses_file_part_anthropic_unsupported_label(part: dict[str, Any]) -> str | None:
    if part.get("file_id") is not None:
        return "file_id"
    file_data = part.get("file_data")
    file_url = part.get("file_url")
    has_file_data = isinstance(file_data, str) and bool(file_data)
    has_file_url = isinstance(file_url, str) and bool(file_url)
    if file_url is not None and not has_file_url:
        return "file_url"
    if file_data is not None and not has_file_data:
        return "file_data"
    if has_file_data and has_file_url:
        return "file_data+file_url"
    if has_file_data:
        return None if _file_data_supported(file_data) else "file_data"
    if has_file_url:
        return None
    return "file_data"


def _file_reference_label(ingress_protocol: str, part: dict[str, Any]) -> str | None:
    typ = part.get("type")
    if ingress_protocol == "chat":
        if typ == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, dict) and image_url.get("file_id") is not None:
                return "image_url.file_id"
        if typ == "file":
            file_obj = part.get("file")
            if isinstance(file_obj, dict) and file_obj.get("file_id") is not None:
                return "file.file_id"
        return None
    if ingress_protocol == "responses":
        if typ == "input_image" and part.get("file_id") is not None:
            return "input_image.file_id"
        if typ == "input_file" and part.get("file_id") is not None:
            return "input_file.file_id"
        return None
    return None


def _responses_image_part_anthropic_unsupported_label(part: dict[str, Any]) -> str | None:
    if part.get("file_id") is not None:
        return "input_image.file_id"
    url = part.get("image_url")
    if not isinstance(url, str) or not url:
        return "input_image.image_url"
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        if not sep or ";base64" not in header or not data:
            return "input_image.data_url"
    return None


def _chat_image_part_anthropic_unsupported_label(part: dict[str, Any]) -> str | None:
    image_url = part.get("image_url")
    if isinstance(image_url, dict) and image_url.get("file_id") is not None:
        return "image_url.file_id"
    return None


def _chat_content_responses_unsupported_label(part: Any, *, role: Any) -> str | None:
    if isinstance(part, str):
        return None
    if not isinstance(part, dict):
        return type(part).__name__
    typ = part.get("type")
    if typ in ("text", "image_url", "file", "input_audio"):
        return None
    if typ == "refusal":
        return None if role == "assistant" else f"{role or 'unknown'}:refusal"
    return f"{role or 'unknown'}:{typ or 'object'}"


def _chat_content_anthropic_unsupported_label(part: Any, *, role: Any) -> str | None:
    if isinstance(part, str):
        return None
    if not isinstance(part, dict):
        return type(part).__name__
    typ = part.get("type")
    if typ in ("text", "image_url", "file", "input_audio"):
        return None
    return f"{role or 'unknown'}:{typ or 'object'}"


def _chat_custom_tool_input_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _chat_tool_call_anthropic_unsupported_label(tool_call: Any) -> str | None:
    if not isinstance(tool_call, dict):
        return f"tool_calls:{type(tool_call).__name__}"
    typ = tool_call.get("type")
    if typ == "function":
        return None
    if typ == "custom":
        custom = tool_call.get("custom") if isinstance(tool_call.get("custom"), dict) else {}
        if _chat_custom_tool_input_object(custom.get("input")) is None:
            return "custom_tool_call.input"
        return None
    return f"tool_calls:{typ or 'object'}"


def _document_citations_enabled(block: dict[str, Any]) -> bool:
    citations = block.get("citations")
    if citations is None:
        return False
    if isinstance(citations, dict):
        return citations.get("enabled") is True
    return bool(citations)


def _anthropic_document_unsupported_label(block: dict[str, Any], *, allow_url: bool) -> str | None:
    if _document_citations_enabled(block):
        return "document.citations"
    source = block.get("source")
    if not isinstance(source, dict):
        return "document.source"
    st = source.get("type")
    if st == "base64":
        data = source.get("data")
        return None if isinstance(data, str) and data else "document.base64"
    if st == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            return "document.url"
        return None if allow_url else "document.url"
    if st == "file":
        return "document.file"
    return f"document.{st or 'source'}"


def extract_request_features(ingress_protocol: str, body: dict | None) -> RequestFeatures:
    body = body if isinstance(body, dict) else {}
    wants_multi_candidate = (
        ingress_protocol == "chat"
        and isinstance(body.get("n"), int)
        and int(body.get("n") or 0) > 1
    )
    has_tools = bool(body.get("tools"))
    has_tool_results = False
    unsupported_tool_result_label: str | None = None
    anthropic_tool_result_responses_unsupported_label: str | None = None
    has_reasoning = bool(body.get("reasoning") or body.get("reasoning_effort"))
    has_images = False
    has_unsupported_image_position = False
    unsupported_image_label: str | None = None
    chat_audio_output_label: str | None = None
    chat_audio_history_label: str | None = None
    has_audio = False
    if ingress_protocol == "chat":
        modalities = body.get("modalities")
        if body.get("audio") is not None:
            has_audio = True
            chat_audio_output_label = "audio"
        if isinstance(modalities, list) and "audio" in modalities:
            has_audio = True
            chat_audio_output_label = chat_audio_output_label or "modalities.audio"
    wants_background = ingress_protocol == "responses" and body.get("background") is True
    has_files = False
    stateful_file_reference_label: str | None = None
    unsupported_file_label: str | None = None
    responses_file_chat_unsupported_label: str | None = None
    responses_file_anthropic_unsupported_label: str | None = None
    anthropic_document_chat_unsupported_label: str | None = None
    anthropic_document_responses_unsupported_label: str | None = None
    openai_image_anthropic_unsupported_label: str | None = None
    responses_image_anthropic_unsupported_label: str | None = None
    responses_tool_output_chat_unsupported_label: str | None = None
    responses_tool_output_anthropic_unsupported_label: str | None = None
    responses_instructions_unsupported_label: str | None = None
    chat_content_responses_unsupported_label: str | None = None
    chat_content_anthropic_unsupported_label: str | None = None
    chat_tool_call_anthropic_unsupported_label: str | None = None
    hosted_tool_labels = _hosted_tool_labels(ingress_protocol, body)
    hosted_tool_label = hosted_tool_labels[0] if hosted_tool_labels else None
    has_hosted_tools = bool(hosted_tool_labels)
    custom_tool_label: str | None = None
    has_custom_tools = False
    has_encrypted_reasoning = False
    stateful_input_item_label: str | None = None
    has_stateful_input_items = False
    if ingress_protocol == "responses":
        stateful_input_item_label = _responses_stateful_input_item_label(body)
        has_stateful_input_items = stateful_input_item_label is not None
        custom_tool_label = _responses_custom_tool_label(body)
        has_custom_tools = custom_tool_label is not None
        has_encrypted_reasoning = _responses_has_encrypted_reasoning_input(body)
        responses_instructions_unsupported_label = _responses_instructions_unsupported_label(body)
    if ingress_protocol == "anthropic":
        if (
            (body.get("thinking") is not None or body.get("output_config") is not None)
            and not _anthropic_thinking_is_disabled(body)
            and not _anthropic_top_level_reasoning_is_mappable(body)
        ):
            has_reasoning = True
        system = body.get("system")
        system_blocks = system if isinstance(system, list) else []
        if any(isinstance(b, dict) and b.get("type") == "image" for b in system_blocks):
            has_images = True
            has_unsupported_image_position = True
            unsupported_image_label = "system:image"
        if any(isinstance(b, dict) and b.get("type") == "document" for b in system_blocks):
            has_files = True
            anthropic_document_chat_unsupported_label = anthropic_document_chat_unsupported_label or "system:document"
            anthropic_document_responses_unsupported_label = anthropic_document_responses_unsupported_label or "system:document"
        if body.get("context_management") is not None and not _anthropic_context_management_is_ignorable(body.get("context_management")):
            has_reasoning = True
        for msg in body.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    has_tool_results = True
                    unsupported_tool_result_label = unsupported_tool_result_label or _anthropic_tool_result_unsupported_label(b)
                    anthropic_tool_result_responses_unsupported_label = (
                        anthropic_tool_result_responses_unsupported_label
                        or _anthropic_tool_result_responses_unsupported_label(b)
                    )
            if any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in blocks):
                has_reasoning = True
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "image":
                    has_images = True
                    if role != "user":
                        has_unsupported_image_position = True
                        unsupported_image_label = unsupported_image_label or f"{role or 'unknown'}:image"
                if isinstance(b, dict) and b.get("type") == "document":
                    has_files = True
                    if role != "user":
                        label = f"{role or 'unknown'}:document"
                        anthropic_document_chat_unsupported_label = anthropic_document_chat_unsupported_label or label
                        anthropic_document_responses_unsupported_label = anthropic_document_responses_unsupported_label or label
                    else:
                        chat_label = _anthropic_document_unsupported_label(b, allow_url=False)
                        responses_label = _anthropic_document_unsupported_label(b, allow_url=True)
                        anthropic_document_chat_unsupported_label = anthropic_document_chat_unsupported_label or chat_label
                        anthropic_document_responses_unsupported_label = anthropic_document_responses_unsupported_label or responses_label
    else:
        messages = body.get("messages") if ingress_protocol == "chat" else body.get("input")
        items = messages if isinstance(messages, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            typ = item.get("type")
            if role == "tool" or typ in ("function_call_output", "custom_tool_call_output"):
                has_tool_results = True
            if ingress_protocol == "responses" and typ in ("function_call_output", "custom_tool_call_output"):
                output = item.get("output")
                output_parts = output if isinstance(output, list) else []
                output_label = str(typ)
                check_anthropic_tool_output = typ == "function_call_output"
                for op in output_parts:
                    if isinstance(op, str):
                        continue
                    if not isinstance(op, dict):
                        responses_tool_output_chat_unsupported_label = (
                            responses_tool_output_chat_unsupported_label
                            or f"{output_label}:{type(op).__name__}"
                        )
                        if check_anthropic_tool_output:
                            responses_tool_output_anthropic_unsupported_label = (
                                responses_tool_output_anthropic_unsupported_label
                                or f"{output_label}:{type(op).__name__}"
                            )
                        continue
                    optyp = op.get("type")
                    if optyp == "input_image":
                        has_images = True
                        if op.get("file_id") is not None:
                            stateful_file_reference_label = (
                                stateful_file_reference_label or f"{output_label}:input_image.file_id"
                            )
                        responses_tool_output_chat_unsupported_label = (
                            responses_tool_output_chat_unsupported_label
                            or f"{output_label}:input_image"
                        )
                        if check_anthropic_tool_output:
                            image_label = _responses_image_part_anthropic_unsupported_label(op)
                            if image_label:
                                responses_tool_output_anthropic_unsupported_label = (
                                    responses_tool_output_anthropic_unsupported_label
                                    or f"{output_label}:{image_label}"
                                )
                    elif optyp == "input_audio":
                        has_audio = True
                        responses_tool_output_chat_unsupported_label = (
                            responses_tool_output_chat_unsupported_label
                            or f"{output_label}:input_audio"
                        )
                        if check_anthropic_tool_output:
                            responses_tool_output_anthropic_unsupported_label = (
                                responses_tool_output_anthropic_unsupported_label
                                or f"{output_label}:input_audio"
                            )
                    elif optyp in ("input_file", "file"):
                        has_files = True
                        if optyp == "input_file" and op.get("file_id") is not None:
                            stateful_file_reference_label = (
                                stateful_file_reference_label or f"{output_label}:input_file.file_id"
                            )
                        responses_tool_output_chat_unsupported_label = (
                            responses_tool_output_chat_unsupported_label
                            or f"{output_label}:{optyp}"
                        )
                        if check_anthropic_tool_output:
                            if optyp == "input_file":
                                file_label = _responses_file_part_anthropic_unsupported_label(op)
                                if file_label:
                                    responses_tool_output_anthropic_unsupported_label = (
                                        responses_tool_output_anthropic_unsupported_label
                                        or f"{output_label}:{file_label}"
                                    )
                            else:
                                responses_tool_output_anthropic_unsupported_label = (
                                    responses_tool_output_anthropic_unsupported_label
                                    or f"{output_label}:file"
                                )
                    elif optyp not in ("input_text", "output_text", "text"):
                        responses_tool_output_chat_unsupported_label = (
                            responses_tool_output_chat_unsupported_label
                            or f"{output_label}:{optyp}"
                        )
                        if check_anthropic_tool_output:
                            responses_tool_output_anthropic_unsupported_label = (
                                responses_tool_output_anthropic_unsupported_label
                                or f"{output_label}:{optyp}"
                            )
            if item.get("reasoning_content") or typ == "reasoning":
                has_reasoning = True
                if isinstance(item.get("encrypted_content"), str) and item.get("encrypted_content"):
                    has_encrypted_reasoning = True
            if ingress_protocol == "chat" and item.get("audio") is not None:
                has_audio = True
                chat_audio_history_label = chat_audio_history_label or "assistant.audio"
            if ingress_protocol == "chat" and role == "assistant":
                for tool_call in item.get("tool_calls") or []:
                    chat_tool_call_anthropic_unsupported_label = (
                        chat_tool_call_anthropic_unsupported_label
                        or _chat_tool_call_anthropic_unsupported_label(tool_call)
                    )
            content = item.get("content")
            parts = content if isinstance(content, list) else []
            if any(isinstance(p, dict) and p.get("type") == "image_url" for p in parts):
                has_images = True
            if any(isinstance(p, dict) and p.get("type") == "input_audio" for p in parts):
                has_audio = True
            for p in parts:
                if ingress_protocol == "chat":
                    chat_content_responses_unsupported_label = (
                        chat_content_responses_unsupported_label
                        or _chat_content_responses_unsupported_label(p, role=role)
                    )
                    chat_content_anthropic_unsupported_label = (
                        chat_content_anthropic_unsupported_label
                        or _chat_content_anthropic_unsupported_label(p, role=role)
                    )
                if not isinstance(p, dict):
                    continue
                ptyp = p.get("type")
                if ingress_protocol == "chat" and ptyp == "image_url" and role not in ("user", "tool"):
                    has_unsupported_image_position = True
                    unsupported_image_label = unsupported_image_label or f"{role or 'unknown'}:image_url"
                if ingress_protocol == "chat" and ptyp == "image_url":
                    stateful_file_reference_label = (
                        stateful_file_reference_label
                        or _file_reference_label(ingress_protocol, p)
                    )
                    image_label = _chat_image_part_anthropic_unsupported_label(p)
                    if image_label:
                        openai_image_anthropic_unsupported_label = (
                            openai_image_anthropic_unsupported_label or image_label
                        )
                if ingress_protocol == "responses" and ptyp == "input_image":
                    has_images = True
                    if role != "user":
                        has_unsupported_image_position = True
                        unsupported_image_label = unsupported_image_label or f"{role or 'unknown'}:input_image"
                    else:
                        image_label = _responses_image_part_anthropic_unsupported_label(p)
                        if image_label:
                            responses_image_anthropic_unsupported_label = (
                                responses_image_anthropic_unsupported_label or image_label
                            )
                if ptyp in ("file", "input_file"):
                    has_files = True
                    stateful_file_reference_label = (
                        stateful_file_reference_label
                        or _file_reference_label(ingress_protocol, p)
                    )
                    if ingress_protocol == "chat" and ptyp == "file":
                        if unsupported_file_label is None:
                            unsupported_file_label = _chat_file_part_unsupported_label(p)
                    elif ingress_protocol == "responses" and ptyp == "input_file":
                        if responses_file_chat_unsupported_label is None:
                            responses_file_chat_unsupported_label = _responses_file_part_chat_unsupported_label(p)
                        if responses_file_anthropic_unsupported_label is None:
                            responses_file_anthropic_unsupported_label = _responses_file_part_anthropic_unsupported_label(p)
                    elif unsupported_file_label is None:
                        unsupported_file_label = str(ptyp)
            if typ in ("input_file", "file"):
                has_files = True
                if ingress_protocol == "responses" and typ == "input_file" and item.get("file_id") is not None:
                    stateful_file_reference_label = stateful_file_reference_label or "input_file.file_id"
                if ingress_protocol == "responses" and typ == "input_file":
                    unsupported_file_label = unsupported_file_label or "input_file"
                elif unsupported_file_label is None:
                    unsupported_file_label = str(typ)
    return RequestFeatures(
        stream=bool(body.get("stream")),
        wants_multi_candidate=wants_multi_candidate,
        has_tools=has_tools,
        has_tool_results=has_tool_results,
        unsupported_tool_result_label=unsupported_tool_result_label,
        anthropic_tool_result_responses_unsupported_label=anthropic_tool_result_responses_unsupported_label,
        has_reasoning=has_reasoning,
        has_images=has_images,
        has_audio=has_audio,
        chat_audio_output_label=chat_audio_output_label,
        chat_audio_history_label=chat_audio_history_label,
        wants_background=wants_background,
        has_files=has_files,
        stateful_file_reference_label=stateful_file_reference_label,
        unsupported_file_label=unsupported_file_label,
        responses_file_chat_unsupported_label=responses_file_chat_unsupported_label,
        responses_file_anthropic_unsupported_label=responses_file_anthropic_unsupported_label,
        anthropic_document_chat_unsupported_label=anthropic_document_chat_unsupported_label,
        anthropic_document_responses_unsupported_label=anthropic_document_responses_unsupported_label,
        openai_image_anthropic_unsupported_label=openai_image_anthropic_unsupported_label,
        responses_image_anthropic_unsupported_label=responses_image_anthropic_unsupported_label,
        responses_tool_output_chat_unsupported_label=responses_tool_output_chat_unsupported_label,
        responses_tool_output_anthropic_unsupported_label=responses_tool_output_anthropic_unsupported_label,
        responses_instructions_unsupported_label=responses_instructions_unsupported_label,
        chat_content_responses_unsupported_label=chat_content_responses_unsupported_label,
        chat_content_anthropic_unsupported_label=chat_content_anthropic_unsupported_label,
        chat_tool_call_anthropic_unsupported_label=chat_tool_call_anthropic_unsupported_label,
        has_unsupported_image_position=has_unsupported_image_position,
        unsupported_image_label=unsupported_image_label,
        has_encrypted_reasoning=has_encrypted_reasoning,
        has_custom_tools=has_custom_tools,
        custom_tool_label=custom_tool_label,
        has_hosted_tools=has_hosted_tools,
        hosted_tool_label=hosted_tool_label,
        hosted_tool_labels=hosted_tool_labels,
        has_stateful_input_items=has_stateful_input_items,
        stateful_input_item_label=stateful_input_item_label,
        raw=body,
    )


def capabilities_for_channel(channel) -> ChannelCapabilities:
    protocol = getattr(channel, "protocol", "anthropic")
    ch_type = getattr(channel, "type", "api")
    provider = getattr(channel, "provider", "")
    transports: set[str] = {"http-sse", "http-json"}
    if bool(getattr(channel, "upstream_stream_only", False)):
        transports.discard("http-json")
    native_state: set[str] = set()
    if protocol == "openai-chat":
        native_state.update({"multi_candidate", "file_id", "audio"})
    elif protocol == "openai-responses":
        if ch_type == "oauth" and provider == "xai":
            native_state.update({
                "encrypted_reasoning_replay",
                "prompt_cache_key",
                "web_search",
            })
        elif ch_type == "oauth":
            native_state.update({
                "encrypted_reasoning_replay",
                "prompt_cache_key",
                "item_reference",
                "custom_tool_history",
                "tool_search",
                "namespace",
            })
        else:
            native_state.update({
                "conversation",
                "item_reference",
                "hosted_tools",
                "custom_tool_history",
                "encrypted_reasoning_replay",
                "file_id",
                "previous_response_id",
                "background",
                "audio",
            })
    if protocol == "openai-responses" and ch_type == "oauth" and provider != "xai":
        transports.add("ws")
    return ChannelCapabilities(
        protocol=protocol,
        transports=frozenset(transports),
        native_state=frozenset(native_state),
    )


def _label_suffix(label: str | None) -> str:
    return f": {label}" if label else ""


def _native_state_key_for_label(label: str | None) -> str | None:
    if not label:
        return None
    if label == "reasoning.encrypted_content":
        return "encrypted_reasoning_replay"
    if label == "conversation":
        return "conversation"
    if label == "item_reference":
        return "item_reference"
    if label in _RESPONSES_BUILTIN_INPUT_ITEM_TYPES:
        return "hosted_tools"
    return label


def _responses_hosted_tool_supported(label: str | None, native: frozenset[str]) -> bool:
    # Codex native passthrough tools are not generic hosted/server-side tools.
    # xAI similarly supports a narrow hosted tool subset (currently web_search)
    # without supporting arbitrary OpenAI hosted tools such as file_search.
    # Do not let a provider's broad `hosted_tools` capability satisfy explicit
    # native requirements; and allow narrow explicit tool support without granting
    # all hosted tools.
    if not label:
        return False
    for tool_type in _RESPONSES_NATIVE_PASSTHROUGH_TOOL_TYPES:
        if label in {tool_type, f"tool_choice:{tool_type}", f"tool_choice:allowed_tools:{tool_type}"}:
            return tool_type in native
    if label in {"web_search", "tool_choice:web_search", "tool_choice:allowed_tools:web_search"}:
        return "web_search" in native or "hosted_tools" in native
    if label in native:
        return True
    for prefix in ("tool_choice:", "tool_choice:allowed_tools:"):
        if label.startswith(prefix) and label[len(prefix):] in native:
            return True
    if "hosted_tools" in native:
        return True
    return False


def _responses_native_unsupported_label(
    f: RequestFeatures,
    capabilities: ChannelCapabilities | None,
) -> str | None:
    native = capabilities.native_state if capabilities is not None else frozenset()
    if f.has_hosted_tools:
        hosted_tool_labels = f.hosted_tool_labels or ((f.hosted_tool_label,) if f.hosted_tool_label else ())
        for hosted_tool_label in hosted_tool_labels:
            if not _responses_hosted_tool_supported(hosted_tool_label, native):
                return hosted_tool_label
        if not hosted_tool_labels:
            return "hosted_tools"
    if f.has_custom_tools and "custom_tool_history" not in native:
        return f.custom_tool_label or "custom_tool_history"
    if f.has_encrypted_reasoning and "encrypted_reasoning_replay" not in native:
        return "reasoning.encrypted_content"
    if f.has_audio and "audio" not in native:
        return "audio"
    if f.wants_background and "background" not in native:
        return "background"
    if f.stateful_file_reference_label and "file_id" not in native:
        return f.stateful_file_reference_label
    if f.has_stateful_input_items:
        required = _native_state_key_for_label(f.stateful_input_item_label)
        if required and required not in native:
            return f.stateful_input_item_label or required
    return None


def _default_capabilities_for_protocol(protocol: str) -> ChannelCapabilities:
    if protocol == "openai-chat":
        return ChannelCapabilities(
            protocol=protocol,
            native_state=frozenset({"multi_candidate", "file_id", "audio"}),
        )
    if protocol == "openai-responses":
        return ChannelCapabilities(
            protocol=protocol,
            native_state=frozenset({
                "conversation",
                "item_reference",
                "hosted_tools",
                "custom_tool_history",
                "encrypted_reasoning_replay",
                "file_id",
                "previous_response_id",
                "background",
                "audio",
            }),
        )
    return ChannelCapabilities(protocol=protocol)


class ProtocolMatrix:
    def plan(
        self,
        ingress_protocol: str,
        upstream_protocol: str,
        features: RequestFeatures | None = None,
        capabilities: ChannelCapabilities | None = None,
    ) -> RoutePlan:
        ingress = canonical_ingress_protocol(ingress_protocol)
        upstream = upstream_protocol or "anthropic"
        capabilities = capabilities or _default_capabilities_for_protocol(upstream)
        f = features or RequestFeatures()

        ingress_family = protocol_family(ingress)
        upstream_family = protocol_family(upstream)
        if upstream_family == "openai" and f.stateful_file_reference_label and "file_id" not in capabilities.native_state:
            raise ProtocolGuardError(
                "OpenAI target route does not support file_id-backed content"
                + _label_suffix(f.stateful_file_reference_label)
            )
        if upstream == "openai-responses" and f.has_audio and "audio" not in capabilities.native_state:
            raise ProtocolGuardError(
                "OpenAI Responses target route does not support audio content"
            )
        if ingress_family != upstream_family:
            if ingress == "anthropic" and upstream == "openai-chat":
                if f.has_hosted_tools:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Chat built-in/server tools are not enabled yet"
                        + _label_suffix(f.hosted_tool_label)
                    )
                if f.has_unsupported_image_position:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Chat image input is only enabled for user messages"
                        + _label_suffix(f.unsupported_image_label)
                    )
                if f.anthropic_document_chat_unsupported_label:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Chat document input is not safely convertible"
                        + _label_suffix(f.anthropic_document_chat_unsupported_label)
                    )
                if f.has_reasoning:
                    raise ProtocolGuardError("Anthropic→OpenAI Chat thinking/reasoning is not enabled yet")
                if f.unsupported_tool_result_label:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Chat tool_result content is not safely convertible"
                        + _label_suffix(f.unsupported_tool_result_label)
                    )
                return RoutePlan(
                    ingress_protocol=ingress,
                    upstream_protocol=upstream,
                    conversion_path=[ingress, upstream],
                    cost=1,
                    required_transforms=["anthropic_to_chat"],
                )
            if ingress == "anthropic" and upstream == "openai-responses":
                if f.has_hosted_tools:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Responses built-in/server tools are not enabled yet"
                        + _label_suffix(f.hosted_tool_label)
                    )
                if f.has_reasoning:
                    raise ProtocolGuardError("Anthropic→OpenAI Responses thinking/reasoning is not enabled yet")
                if f.has_unsupported_image_position:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Responses image input is only enabled for user messages"
                        + _label_suffix(f.unsupported_image_label)
                    )
                if f.anthropic_document_responses_unsupported_label:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Responses document input is not safely convertible"
                        + _label_suffix(f.anthropic_document_responses_unsupported_label)
                    )
                if f.anthropic_tool_result_responses_unsupported_label:
                    raise ProtocolGuardError(
                        "Anthropic→OpenAI Responses tool_result content is not safely convertible"
                        + _label_suffix(f.anthropic_tool_result_responses_unsupported_label)
                    )
                return RoutePlan(
                    ingress_protocol=ingress,
                    upstream_protocol=upstream,
                    conversion_path=[ingress, upstream],
                    cost=1,
                    required_transforms=["anthropic_to_responses"],
                )
            if ingress == "openai-chat" and upstream == "anthropic":
                if f.wants_multi_candidate:
                    raise ProtocolGuardError("OpenAI Chat→Anthropic multi-candidate n>1 aggregation is not enabled yet")
                if f.has_audio:
                    raise ProtocolGuardError("OpenAI Chat→Anthropic audio input/history is not enabled yet")
                if f.chat_content_anthropic_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Anthropic content part is not safely convertible"
                        + _label_suffix(f.chat_content_anthropic_unsupported_label)
                    )
                if f.chat_tool_call_anthropic_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Anthropic tool_call history is not safely convertible"
                        + _label_suffix(f.chat_tool_call_anthropic_unsupported_label)
                    )
                if f.has_unsupported_image_position:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Anthropic image input is only enabled for user messages"
                        + _label_suffix(f.unsupported_image_label)
                    )
                if f.openai_image_anthropic_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Anthropic image input is not safely convertible"
                        + _label_suffix(f.openai_image_anthropic_unsupported_label)
                    )
                if f.unsupported_file_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Anthropic file/document input is not safely convertible"
                        + _label_suffix(f.unsupported_file_label)
                    )
                return RoutePlan(
                    ingress_protocol=ingress,
                    upstream_protocol=upstream,
                    conversion_path=[ingress, upstream],
                    cost=1,
                    required_transforms=["chat_to_anthropic"],
                )
            if ingress == "openai-responses" and upstream == "anthropic":
                if f.responses_instructions_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic instructions are not safely convertible"
                        + _label_suffix(f.responses_instructions_unsupported_label)
                    )
                if f.has_encrypted_reasoning:
                    raise ProtocolGuardError("OpenAI Responses→Anthropic include reasoning.encrypted_content / encrypted reasoning replay is not enabled yet")
                if f.has_stateful_input_items:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic stateful input items are not enabled yet"
                        + _label_suffix(f.stateful_input_item_label)
                    )
                if f.has_audio:
                    raise ProtocolGuardError("OpenAI Responses→Anthropic audio input is not enabled yet")
                if f.wants_background:
                    raise ProtocolGuardError("OpenAI Responses→Anthropic background async state is not enabled yet")
                if f.has_unsupported_image_position:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic image input is only enabled for user messages"
                        + _label_suffix(f.unsupported_image_label)
                    )
                if f.responses_image_anthropic_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic image input is not safely convertible"
                        + _label_suffix(f.responses_image_anthropic_unsupported_label)
                    )
                if f.responses_tool_output_anthropic_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic function_call_output content is not safely convertible"
                        + _label_suffix(f.responses_tool_output_anthropic_unsupported_label)
                    )
                file_label = f.responses_file_anthropic_unsupported_label or f.unsupported_file_label
                if file_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic file/document input is not safely convertible"
                        + _label_suffix(file_label)
                    )
                if f.has_custom_tools:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Anthropic custom tools/calls are not enabled yet"
                        + _label_suffix(f.custom_tool_label)
                    )
                return RoutePlan(
                    ingress_protocol=ingress,
                    upstream_protocol=upstream,
                    conversion_path=[ingress, upstream],
                    cost=1,
                    required_transforms=["responses_to_anthropic"],
                )
            raise ProtocolGuardError(
                f"cross-family route not enabled yet: ingress={ingress} upstream={upstream}"
            )

        if ingress == upstream:
            if ingress == "openai-responses":
                unsupported = _responses_native_unsupported_label(f, capabilities)
                if unsupported:
                    raise ProtocolGuardError(
                        "OpenAI Responses native route does not support requested server-side state"
                        + _label_suffix(unsupported)
                    )
            return RoutePlan(ingress_protocol=ingress, upstream_protocol=upstream)

        if ingress_family == "openai" and upstream_family == "openai":
            if ingress == "openai-chat" and upstream == "openai-responses":
                if f.wants_multi_candidate:
                    raise ProtocolGuardError("OpenAI Chat→Responses multi-candidate n>1 aggregation is not enabled yet")
                audio_label = f.chat_audio_output_label or f.chat_audio_history_label
                if audio_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Responses audio output/history is not enabled yet"
                        + _label_suffix(audio_label)
                    )
                if f.chat_content_responses_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Chat→Responses content part is not safely convertible"
                        + _label_suffix(f.chat_content_responses_unsupported_label)
                    )
                transform = "chat_to_responses"
            elif ingress == "openai-responses" and upstream == "openai-chat":
                if f.responses_instructions_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Chat instructions are not safely convertible"
                        + _label_suffix(f.responses_instructions_unsupported_label)
                    )
                if f.has_hosted_tools or f.has_stateful_input_items:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Chat hosted/stateful tools are not enabled yet"
                        + _label_suffix(f.hosted_tool_label or f.stateful_input_item_label)
                    )
                if f.wants_background:
                    raise ProtocolGuardError("OpenAI Responses→Chat background async state is not enabled yet")
                if f.responses_tool_output_chat_unsupported_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Chat function_call_output content is not safely convertible"
                        + _label_suffix(f.responses_tool_output_chat_unsupported_label)
                    )
                file_label = f.responses_file_chat_unsupported_label or f.unsupported_file_label
                if file_label:
                    raise ProtocolGuardError(
                        "OpenAI Responses→Chat file/document input is not safely convertible"
                        + _label_suffix(file_label)
                    )
                transform = "responses_to_chat"
            else:
                transform = "openai_variant_bridge"
            return RoutePlan(
                ingress_protocol=ingress,
                upstream_protocol=upstream,
                conversion_path=[ingress, upstream],
                cost=1,
                required_transforms=[transform],
            )

        raise ProtocolGuardError(f"unsupported protocol route: ingress={ingress} upstream={upstream}")


DEFAULT_MATRIX = ProtocolMatrix()
