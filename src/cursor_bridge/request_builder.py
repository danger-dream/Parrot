"""Build Cursor AgentRunRequest bytes from OpenAI messages/tools."""

from __future__ import annotations

import uuid
from typing import Any

from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from . import agent_pb2
from .constants import (
    MAX_EFFECTIVE_PROMPT_BYTES,
    MCP_INSTRUCTIONS,
    MCP_SERVER_NAME,
    MCP_TOOL_PREFIX,
)
from .openai_messages import ConversationTurn


def strip_mcp_prefix(name: str) -> str:
    return name[len(MCP_TOOL_PREFIX) :] if name.startswith(MCP_TOOL_PREFIX) else name


def json_to_value_bytes(value: Any) -> bytes:
    message = ParseDict(value if value is not None else {}, Value())
    return message.SerializeToString()


def build_mcp_tools(tools: list[dict[str, Any]]) -> list[agent_pb2.McpToolDefinition]:
    definitions: list[agent_pb2.McpToolDefinition] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = str(fn.get("name") or "")
        if not name:
            continue
        parameters = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
        definitions.append(
            agent_pb2.McpToolDefinition(
                name=f"{MCP_TOOL_PREFIX}{name}",
                description=str(fn.get("description") or ""),
                provider_identifier=MCP_SERVER_NAME,
                tool_name=name,
                input_schema=json_to_value_bytes(parameters),
            )
        )
    return definitions


def enabled_tool_names(mcp_tools: list[agent_pb2.McpToolDefinition]) -> set[str]:
    return {strip_mcp_prefix(tool.tool_name or tool.name) for tool in mcp_tools if strip_mcp_prefix(tool.tool_name or tool.name)}


def fold_turns_into_system_prompt(system_prompt: str, turns: list[ConversationTurn]) -> str:
    if not turns:
        return system_prompt
    parts: list[str] = []
    for turn in turns:
        if turn.is_compaction:
            parts.append(f"<context>\n{turn.user_text}\n</context>")
        else:
            text = f"User: {turn.user_text}"
            if turn.assistant_text:
                text += f"\nAssistant: {turn.assistant_text}"
            parts.append(text)
    full = f"{system_prompt}\n\nPrevious conversation context:\n" + "\n\n".join(parts)
    if len(full.encode("utf-8")) <= MAX_EFFECTIVE_PROMPT_BYTES:
        return full

    prefix = f"{system_prompt}\n\nPrevious conversation context (oldest turns truncated):\n"
    budget = MAX_EFFECTIVE_PROMPT_BYTES - len(prefix.encode("utf-8"))
    kept: set[int] = set()
    for idx, turn in enumerate(turns):
        if not turn.is_compaction:
            continue
        size = len(parts[idx].encode("utf-8")) + 2
        if size <= budget:
            budget -= size
            kept.add(idx)
    for idx in range(len(turns) - 1, -1, -1):
        if turns[idx].is_compaction:
            continue
        size = len(parts[idx].encode("utf-8")) + 2
        if size <= budget:
            budget -= size
            kept.add(idx)
    if not kept:
        return system_prompt
    selected = [parts[i] for i in sorted(kept)]
    return prefix + "\n\n".join(selected)


def build_request_context(
    mcp_tools: list[agent_pb2.McpToolDefinition],
    cloud_rule: str | None,
) -> agent_pb2.RequestContext:
    ctx = agent_pb2.RequestContext()
    ctx.tools.extend(mcp_tools)
    if mcp_tools:
        ctx.mcp_instructions.append(
            agent_pb2.McpInstructions(server_name=MCP_SERVER_NAME, instructions=MCP_INSTRUCTIONS)
        )
    if cloud_rule:
        ctx.cloud_rule = cloud_rule
    return ctx


def decode_checkpoint(raw: bytes | None) -> agent_pb2.ConversationStateStructure | None:
    if not raw:
        return None
    state = agent_pb2.ConversationStateStructure()
    try:
        state.ParseFromString(raw)
    except Exception:
        return None
    return state


def build_run_request_bytes(
    *,
    model_id: str,
    system_prompt: str,
    user_text: str,
    turns: list[ConversationTurn],
    conversation_id: str,
    checkpoint: bytes | None,
    mcp_tools: list[agent_pb2.McpToolDefinition],
    long_context: bool = False,
    max_mode: bool = False,
) -> bytes:
    decoded = decode_checkpoint(checkpoint)
    effective_system = system_prompt
    conversation_state = agent_pb2.ConversationStateStructure()
    if decoded is not None:
        conversation_state.CopyFrom(decoded)
    else:
        if turns:
            effective_system = fold_turns_into_system_prompt(system_prompt, turns)

    has_max_suffix = model_id.endswith("-max")
    is_max_mode = max_mode or has_max_suffix
    cursor_model_id = model_id[:-4] if has_max_suffix else model_id

    user_message = agent_pb2.UserMessage(text=user_text, message_id=str(uuid.uuid4()))
    request_context = build_request_context(mcp_tools, effective_system or None)
    action = agent_pb2.ConversationAction(
        user_message_action=agent_pb2.UserMessageAction(
            user_message=user_message,
            request_context=request_context,
        )
    )
    requested = agent_pb2.RequestedModel(model_id=cursor_model_id, max_mode=is_max_mode)
    if long_context:
        requested.parameters.append(
            agent_pb2.RequestedModel_ModelParameterbytes(id="long_context", value="true")
        )
    model_details = agent_pb2.ModelDetails(
        model_id=cursor_model_id,
        display_model_id=cursor_model_id,
        display_name=cursor_model_id,
        display_name_short=cursor_model_id,
        max_mode=is_max_mode,
    )
    run_request = agent_pb2.AgentRunRequest(
        conversation_state=conversation_state,
        action=action,
        model_details=model_details,
        requested_model=requested,
        conversation_id=conversation_id,
    )
    return agent_pb2.AgentClientMessage(run_request=run_request).SerializeToString()
