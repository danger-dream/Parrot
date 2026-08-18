"""Route Cursor exec/interaction messages. Native tools are always rejected."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Value

from . import agent_pb2
from .connect import frame_connect_message
from .constants import REJECT_REASON
from .request_builder import build_request_context, strip_mcp_prefix

SendFrame = Callable[[bytes], None]


def normalize_tool_call_id(raw: str | None) -> str:
    cleaned = "".join(str(raw or "").split())
    return cleaned or str(uuid.uuid4())


@dataclass
class PendingExec:
    exec_id: str
    exec_msg_id: int
    tool_call_id: str
    tool_name: str
    decoded_args: str


REDIRECTABLE = {
    "shell_args",
    "write_args",
    "delete_args",
    "grep_args",
    "read_args",
    "ls_args",
    "shell_stream_args",
    "fetch_args",
}


def _client_message(inner: agent_pb2.AgentClientMessage) -> bytes:
    return frame_connect_message(inner.SerializeToString())


def send_exec_stream_close(exec_msg_id: int, send: SendFrame) -> None:
    control = agent_pb2.ExecClientControlMessage(
        stream_close=agent_pb2.ExecClientStreamClose(id=exec_msg_id)
    )
    send(_client_message(agent_pb2.AgentClientMessage(exec_client_control_message=control)))


def send_exec_result(
    exec_msg: agent_pb2.ExecServerMessage,
    send: SendFrame,
    **fields: Any,
) -> None:
    result = agent_pb2.ExecClientMessage(id=exec_msg.id, exec_id=exec_msg.exec_id, **fields)
    send(_client_message(agent_pb2.AgentClientMessage(exec_client_message=result)))
    send_exec_stream_close(exec_msg.id, send)


def send_mcp_result(
    exec_msg: agent_pb2.ExecServerMessage,
    send: SendFrame,
    content: str,
    *,
    is_error: bool = False,
) -> None:
    success = agent_pb2.McpSuccess(is_error=is_error)
    success.content.append(
        agent_pb2.McpToolResultContentItem(text=agent_pb2.McpTextContent(text=content))
    )
    send_exec_result(exec_msg, send, mcp_result=agent_pb2.McpResult(success=success))


def send_mcp_result_for_pending(
    exec: PendingExec,
    send: SendFrame,
    content: str,
    *,
    is_error: bool = False,
) -> None:
    fake = agent_pb2.ExecServerMessage(id=exec.exec_msg_id, exec_id=exec.exec_id)
    send_mcp_result(fake, send, content, is_error=is_error)


def decode_mcp_args(raw_map: Any) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in raw_map.items():
        if not isinstance(value, (bytes, bytearray)):
            decoded[key] = value
            continue
        try:
            message = Value()
            message.ParseFromString(bytes(value))
            decoded[key] = MessageToDict(message)
        except Exception:
            decoded[key] = bytes(value).decode("utf-8", errors="replace")
    if decoded.get("filePath") and not decoded.get("path"):
        decoded["path"] = decoded.pop("filePath")
    return decoded


def _reject_shell(exec_msg: agent_pb2.ExecServerMessage, send: SendFrame, reason: str) -> None:
    args = exec_msg.shell_args if exec_msg.WhichOneof("message") == "shell_args" else exec_msg.shell_stream_args
    rejected = agent_pb2.ShellRejected(
        command=args.command,
        working_directory=args.working_directory,
        reason=reason,
        is_readonly=False,
    )
    send_exec_result(exec_msg, send, shell_result=agent_pb2.ShellResult(rejected=rejected))


def handle_exec_message(
    exec_msg: agent_pb2.ExecServerMessage,
    *,
    mcp_tools: list[agent_pb2.McpToolDefinition],
    enabled: set[str],
    cloud_rule: str | None,
    send: SendFrame,
    on_mcp_exec: Callable[[PendingExec], None],
) -> None:
    case = exec_msg.WhichOneof("message")
    if case == "mcp_args":
        args = exec_msg.mcp_args
        decoded = decode_mcp_args(args.args)
        tool_name = strip_mcp_prefix(args.tool_name or args.name or "")
        if tool_name not in enabled:
            send_mcp_result(exec_msg, send, f"Tool '{tool_name}' is not enabled in this session", is_error=True)
            return
        on_mcp_exec(
            PendingExec(
                exec_id=exec_msg.exec_id,
                exec_msg_id=exec_msg.id,
                tool_call_id=normalize_tool_call_id(args.tool_call_id),
                tool_name=tool_name,
                decoded_args=json.dumps(decoded, ensure_ascii=False),
            )
        )
        return

    if case == "request_context_args":
        ctx = build_request_context(mcp_tools, cloud_rule)
        send_exec_result(
            exec_msg,
            send,
            request_context_result=agent_pb2.RequestContextResult(
                success=agent_pb2.RequestContextSuccess(request_context=ctx)
            ),
        )
        return

    if case in REDIRECTABLE:
        if case in {"shell_args", "shell_stream_args"}:
            _reject_shell(exec_msg, send, REJECT_REASON)
        else:
            send_mcp_result(exec_msg, send, REJECT_REASON, is_error=True)
        return

    if case == "background_shell_spawn_args":
        args = exec_msg.background_shell_spawn_args
        rejected = agent_pb2.ShellRejected(
            command=getattr(args, "command", ""),
            working_directory=getattr(args, "working_directory", ""),
            reason=REJECT_REASON,
            is_readonly=False,
        )
        send_exec_result(
            exec_msg,
            send,
            background_shell_spawn_result=agent_pb2.BackgroundShellSpawnResult(rejected=rejected),
        )
        return

    if case == "write_shell_stdin_args":
        send_exec_result(
            exec_msg,
            send,
            write_shell_stdin_result=agent_pb2.WriteShellStdinResult(
                error=agent_pb2.WriteShellStdinError(error=REJECT_REASON)
            ),
        )
        return

    if case == "diagnostics_args":
        send_exec_result(exec_msg, send, diagnostics_result=agent_pb2.DiagnosticsResult())
        return

    send_exec_stream_close(exec_msg.id, send)


def handle_interaction_query(query: agent_pb2.InteractionQuery, send: SendFrame) -> None:
    case = query.WhichOneof("query")
    response = agent_pb2.InteractionResponse(id=query.id)
    if case == "web_search_request_query":
        response.web_search_request_response.rejected.reason = REJECT_REASON
    elif case == "exa_search_request_query":
        response.exa_search_request_response.rejected.reason = REJECT_REASON
    elif case == "exa_fetch_request_query":
        response.exa_fetch_request_response.rejected.reason = REJECT_REASON
    elif case == "ask_question_interaction_query":
        response.ask_question_interaction_response.result.rejected.CopyFrom(agent_pb2.AskQuestionRejected())
    elif case == "switch_mode_request_query":
        response.switch_mode_request_response.CopyFrom(agent_pb2.SwitchModeRequestResponse())
    elif case == "create_plan_request_query":
        response.create_plan_request_response.CopyFrom(agent_pb2.CreatePlanRequestResponse())
    send(_client_message(agent_pb2.AgentClientMessage(interaction_response=response)))


def handle_kv_message(
    kv_msg: agent_pb2.KvServerMessage,
    blob_store: dict[str, bytes],
    send: SendFrame,
) -> None:
    case = kv_msg.WhichOneof("message")
    reply = agent_pb2.KvClientMessage(id=kv_msg.id)
    if case == "get_blob_args":
        key = kv_msg.get_blob_args.blob_id.hex()
        data = blob_store.get(key)
        if data is not None:
            reply.get_blob_result.blob_data = data
        else:
            reply.get_blob_result.CopyFrom(agent_pb2.GetBlobResult())
    elif case == "set_blob_args":
        key = kv_msg.set_blob_args.blob_id.hex()
        blob_store[key] = bytes(kv_msg.set_blob_args.blob_data)
        reply.set_blob_result.CopyFrom(agent_pb2.SetBlobResult())
    else:
        return
    send(_client_message(agent_pb2.AgentClientMessage(kv_client_message=reply)))
