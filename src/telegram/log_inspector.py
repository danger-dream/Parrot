"""Structured request/response log inspector for Telegram UI.

This module turns raw request_detail.request_body / response_body blobs into
human-sized "message units".  The menu layer can then render one selected unit in
text and put the nearby units in inline buttons, instead of dumping the whole
blob into the chat.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional


Item = dict[str, Any]


SORT_KEYS = ("original", "reverse", "size", "type")
SORT_LABELS = {
    "original": "原始",
    "reverse": "倒序",
    "size": "大小",
    "type": "类型",
}

KIND_LABELS = {
    "assistant": "助手消息",
    "user": "用户消息",
    "system": "系统消息",
    "developer": "开发者消息",
    "tool": "工具消息",
    "instructions": "指令消息",
    "params": "请求参数",
    "metadata": "元数据",
    "tools": "工具定义",
    "tool_choice": "工具选择",
    "tool_call": "工具调用",
    "tool_result": "工具结果",
    "reasoning": "思考内容",
    "usage": "用量统计",
    "error": "错误信息",
    "finish": "结束原因",
    "refusal": "拒绝内容",
    "message": "消息",
    "input": "输入内容",
    "output": "输出内容",
    "response": "响应元信息",
    "request": "请求内容",
    "event": "原始事件",
    "raw_request": "原始请求",
    "raw_response": "原始响应",
}

SUMMARY_LABELS = {
    "function_call_output": "函数调用结果",
    "custom_tool_call_output": "自定义工具结果",
    "mcp_tool_call_output": "MCP 工具结果",
    "function_call": "函数调用",
    "custom_tool_call": "自定义工具调用",
    "mcp_tool_call": "MCP 工具调用",
    "tool_call": "工具调用",
    "tool_result": "工具结果",
    "output_text": "输出文本",
    "input_text": "输入文本",
    "reasoning_summary_text": "思考摘要",
    "reasoning": "思考内容",
    "assistant": "助手消息",
    "user": "用户消息",
    "system": "系统消息",
    "developer": "开发者消息",
    "usage": "用量统计",
    "metadata": "元数据",
    "params": "请求参数",
    "tools": "工具定义",
    "text": "文本",
}

KIND_SHORT_LABELS = {
    "assistant": "助手",
    "user": "用户",
    "system": "系统",
    "developer": "开发",
    "instructions": "指令",
    "params": "参数",
    "metadata": "元数据",
    "tools": "工具定义",
    "tool_choice": "工具选择",
    "tool_call": "调用",
    "tool_result": "结果",
    "reasoning": "思考",
    "usage": "用量",
    "error": "错误",
    "finish": "结束",
    "refusal": "拒绝",
    "message": "消息",
    "response": "响应",
    "event": "事件",
}

SUMMARY_SHORT_LABELS = {
    "函数调用结果": "函数结果",
    "自定义工具结果": "自定义结果",
    "MCP 工具结果": "MCP结果",
    "函数调用": "函数",
    "自定义工具调用": "自定义",
    "MCP 工具调用": "MCP",
    "工具调用": "调用",
    "工具结果": "结果",
    "输出文本": "输出",
    "输入文本": "输入",
    "思考摘要": "思考摘要",
    "思考内容": "思考",
    "助手消息": "助手",
    "用户消息": "用户",
    "系统消息": "系统",
    "开发者消息": "开发",
    "用量统计": "用量",
    "请求参数": "参数",
    "工具定义": "工具定义",
}

_ENCRYPTED_KEYS = {"encrypted_content"}
_ENCRYPTED_PREVIEW_CHARS = 48


_TEXTY_BLOCK_TYPES = {
    "text", "input_text", "output_text", "thinking", "reasoning", "summary_text",
}
_TOOL_CALL_TYPES = {
    "tool_use", "server_tool_use", "mcp_tool_use", "function_call",
    "custom_tool_call", "mcp_tool_call", "tool_call",
}
_TOOL_RESULT_TYPES = {
    "tool_result", "function_call_output", "custom_tool_call_output",
    "mcp_tool_call_output", "input_tool_result",
}


# ─── Common formatting ────────────────────────────────────────────────

def fmt_size(n: int | None) -> str:
    try:
        n = int(n or 0)
    except Exception:
        n = 0
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def kind_label(kind: str | None) -> str:
    k = str(kind or "")
    return KIND_LABELS.get(k, k or "消息")


def summary_label(summary: str | None) -> str:
    s = str(summary or "")
    if not s:
        return s
    for raw, label in sorted(SUMMARY_LABELS.items(), key=lambda kv: len(kv[0]), reverse=True):
        s = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(raw)}(?![A-Za-z0-9_])", label, s)
    return s


def kind_short_label(kind: str | None) -> str:
    k = str(kind or "")
    return KIND_SHORT_LABELS.get(k, kind_label(k))


def summary_short_label(summary: str | None) -> str:
    s = summary_label(summary)
    if not s:
        return s
    for raw, label in sorted(SUMMARY_SHORT_LABELS.items(), key=lambda kv: len(kv[0]), reverse=True):
        s = s.replace(raw, label)
    # 常见“类型 · 类型”冗余，比如“用户消息 · 输入文本”，按钮里保留后者即可。
    parts = [p.strip() for p in s.split("·") if p.strip()]
    if len(parts) >= 2 and parts[0] in {"助手", "用户", "系统", "开发", "思考"}:
        parts = parts[1:]
    return " · ".join(parts)


def _short_encrypted(value: str) -> str:
    s = str(value or "")
    if len(s) <= _ENCRYPTED_PREVIEW_CHARS + 12:
        return s
    omitted = len(s) - _ENCRYPTED_PREVIEW_CHARS
    return f"{s[:_ENCRYPTED_PREVIEW_CHARS]}…（encrypted_content 已省略 {omitted} 字符）"


def _sanitize_for_display(obj: Any) -> Any:
    """Trim unreadable encrypted payloads before displaying/dumping JSON."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k) in _ENCRYPTED_KEYS and isinstance(v, str):
                out[k] = _short_encrypted(v)
            else:
                out[k] = _sanitize_for_display(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_for_display(x) for x in obj]
    return obj


def _maybe_pretty_json_text(text: str) -> str:
    s = str(text or "")
    if not s:
        return s
    try:
        obj = json.loads(s)
    except Exception:
        return s
    return _json_dumps(_sanitize_for_display(obj))


def _compact_ws(text: str, limit: int = 42) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _json_dumps(obj: Any, *, compact: bool = False) -> str:
    try:
        obj = _sanitize_for_display(obj)
        if compact:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def _load_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "")
    try:
        return json.loads(text)
    except Exception:
        return None


def _text_from_content(content: Any) -> str:
    content = _sanitize_for_display(content)
    if content is None:
        return ""
    if isinstance(content, str):
        return _maybe_pretty_json_text(content)
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                parts.append(_json_dumps(part))
                continue
            typ = str(part.get("type") or "")
            if typ in ("text", "input_text", "output_text"):
                parts.append(str(part.get("text") or ""))
            elif typ in ("thinking", "reasoning"):
                parts.append(str(part.get("thinking") or part.get("text") or part.get("summary") or ""))
            elif typ in ("image", "input_image", "image_url"):
                url = part.get("url") or part.get("image_url") or part.get("file_id") or ""
                parts.append(f"[image] {url}".strip())
            else:
                parts.append(_json_dumps(part))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        for k in ("text", "input", "output", "content", "arguments", "partial_json"):
            if isinstance(content.get(k), str):
                return _maybe_pretty_json_text(content[k])
        return _json_dumps(content)
    return str(content)


def _tool_name(obj: dict) -> str:
    for key in ("name", "tool_name", "server_name"):
        if obj.get(key):
            return str(obj[key])
    fn = obj.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return ""


def _kind_for_block(role: str, typ: str, obj: dict | None = None) -> str:
    typ = typ or "text"
    if typ in _TOOL_CALL_TYPES:
        return "tool_call"
    if typ in _TOOL_RESULT_TYPES or role == "tool":
        return "tool_result"
    if typ in ("thinking", "reasoning", "reasoning_summary", "summary_text"):
        return "reasoning"
    if role:
        return role
    return typ


def _add_item(items: list[Item], *, kind: str, text: Any, summary: str = "",
              raw: Any = None, title: str = "", meta: Optional[dict] = None) -> None:
    kind = str(kind or "message")
    text = _sanitize_for_display(text)
    raw = _sanitize_for_display(raw)
    body = _text_from_content(text)
    if kind in {"tool_call", "tool_result", "params", "metadata", "tools", "usage", "error"}:
        body = _maybe_pretty_json_text(body)
    raw_text = body if raw is None else (_maybe_pretty_json_text(raw) if isinstance(raw, str) else _json_dumps(raw))
    if not body and raw is not None:
        body = raw_text
    size = len(body.encode("utf-8", errors="replace"))
    seq = len(items) + 1
    summary = summary or _compact_ws(body, 36) or "空"
    items.append({
        "seq": seq,
        "kind": str(kind or "message"),
        "title": title or str(kind or "message"),
        "summary": summary,
        "text": body,
        "raw": raw_text,
        "size": size,
        "meta": meta or {},
    })


def _param_item(body: dict) -> dict:
    skip = {
        "messages", "input", "system", "instructions", "tools", "tool_choice",
        "metadata", "response_format", "modalities", "audio",
    }
    params = {k: v for k, v in body.items() if k not in skip}
    return params


# ─── Request parsing ─────────────────────────────────────────────────

def parse_request_body(raw: Any) -> list[Item]:
    obj = _load_json(raw)
    if obj is None:
        text = str(raw or "")
        return [_fallback_item("raw_request", text)] if text else []
    if not isinstance(obj, dict):
        return [_fallback_item("request", _json_dumps(obj))]

    items: list[Item] = []

    params = _param_item(obj)
    if params:
        _add_item(items, kind="params", text=_json_dumps(params), raw=params,
                  summary=f"{len(params)} 个参数")
    if obj.get("metadata") is not None:
        _add_item(items, kind="metadata", text=_json_dumps(obj.get("metadata")), raw=obj.get("metadata"),
                  summary="metadata")
    if obj.get("tools") is not None:
        tools = obj.get("tools")
        names: list[str] = []
        if isinstance(tools, list):
            for t in tools[:4]:
                if isinstance(t, dict):
                    names.append(_tool_name(t) or str(t.get("type") or "tool"))
        _add_item(items, kind="tools", text=_json_dumps(tools), raw=tools,
                  summary=f"{len(tools) if isinstance(tools, list) else 1} 个工具" + (f" · {', '.join(names)}" if names else ""))
    if obj.get("tool_choice") is not None:
        _add_item(items, kind="tool_choice", text=_json_dumps(obj.get("tool_choice")), raw=obj.get("tool_choice"),
                  summary="tool_choice")
    if obj.get("system") is not None:
        _add_item(items, kind="system", text=obj.get("system"), raw=obj.get("system"), summary="system")
    if obj.get("instructions") is not None:
        _add_item(items, kind="instructions", text=obj.get("instructions"), raw=obj.get("instructions"), summary="instructions")

    if isinstance(obj.get("messages"), list):
        _parse_messages_list(items, obj["messages"])
    elif obj.get("input") is not None:
        _parse_responses_input(items, obj.get("input"))

    if not items:
        _add_item(items, kind="request", text=_json_dumps(obj), raw=obj, summary="完整请求 JSON")
    return items


def _parse_messages_list(items: list[Item], messages: list) -> None:
    for mi, msg in enumerate(messages, 1):
        if not isinstance(msg, dict):
            _add_item(items, kind="message", text=msg, raw=msg, summary=f"message[{mi}]")
            continue
        role = str(msg.get("role") or "message")
        name = msg.get("name") or msg.get("tool_call_id") or ""
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            for ti, tc in enumerate(tool_calls, 1):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                nm = _tool_name(tc) or _tool_name(fn) or f"tool#{ti}"
                text = fn.get("arguments") if isinstance(fn, dict) else _json_dumps(tc)
                _add_item(items, kind="tool_call", text=text, raw=tc,
                          summary=f"{nm} · args {fmt_size(len(str(text).encode('utf-8', errors='replace')))}",
                          meta={"role": role, "message_index": mi, "tool_name": nm})
        if isinstance(content, list):
            if not content and not tool_calls:
                _add_item(items, kind=role, text="", raw=msg, summary=f"{role} · 空")
            for bi, block in enumerate(content, 1):
                if isinstance(block, dict):
                    typ = str(block.get("type") or "text")
                    kind = _kind_for_block(role, typ, block)
                    nm = _tool_name(block)
                    summary = f"{typ}" + (f" · {nm}" if nm else "")
                    _add_item(items, kind=kind, text=block, raw=block, summary=summary,
                              meta={"role": role, "message_index": mi, "block_index": bi, "type": typ})
                else:
                    _add_item(items, kind=role, text=block, raw=block, summary=f"{role} · part[{bi}]",
                              meta={"role": role, "message_index": mi, "block_index": bi})
        elif content is not None or not tool_calls:
            suffix = f" · {name}" if name else ""
            _add_item(items, kind=("tool_result" if role == "tool" else role), text=content, raw=msg,
                      summary=f"{role}{suffix}", meta={"role": role, "message_index": mi})


def _parse_responses_input(items: list[Item], inp: Any) -> None:
    if isinstance(inp, str):
        _add_item(items, kind="user", text=inp, raw=inp, summary="input text")
        return
    if not isinstance(inp, list):
        _add_item(items, kind="input", text=inp, raw=inp, summary="input")
        return
    for ii, entry in enumerate(inp, 1):
        if not isinstance(entry, dict):
            _add_item(items, kind="input", text=entry, raw=entry, summary=f"input[{ii}]")
            continue
        typ = str(entry.get("type") or "message")
        role = str(entry.get("role") or "")
        if typ == "message":
            content = entry.get("content")
            if isinstance(content, list):
                for ci, block in enumerate(content, 1):
                    if isinstance(block, dict):
                        bt = str(block.get("type") or "text")
                        kind = _kind_for_block(role or "message", bt, block)
                        _add_item(items, kind=kind, text=block, raw=block, summary=f"{role or 'message'} · {bt}",
                                  meta={"input_index": ii, "content_index": ci, "type": bt})
                    else:
                        _add_item(items, kind=role or "message", text=block, raw=block,
                                  summary=f"{role or 'message'} · content[{ci}]",
                                  meta={"input_index": ii, "content_index": ci})
            else:
                _add_item(items, kind=role or "message", text=content, raw=entry, summary=f"{role or 'message'}")
        else:
            kind = _kind_for_block(role, typ, entry)
            nm = _tool_name(entry)
            _add_item(items, kind=kind, text=entry, raw=entry, summary=f"{typ}" + (f" · {nm}" if nm else ""),
                      meta={"input_index": ii, "type": typ})


# ─── Response parsing ────────────────────────────────────────────────

def parse_response_body(raw: Any) -> list[Item]:
    text = str(raw or "")
    if not text:
        return []
    obj = _load_json(text)
    if isinstance(obj, dict):
        return _parse_response_json(obj)

    events = parse_sse_or_ws_events(text)
    if not events:
        return [_fallback_item("raw_response", text)]

    if any(str(e.get("event") or e.get("type") or "").startswith("response.") for e in events):
        return _parse_responses_events(events)
    if any(isinstance(e.get("data"), dict) and isinstance(e["data"].get("choices"), list) for e in events):
        return _parse_chat_events(events)
    if any(isinstance(e.get("data"), dict) and e["data"].get("type") for e in events):
        return _parse_anthropic_events(events)
    return _parse_raw_events(events)


def parse_sse_or_ws_events(text: str) -> list[dict]:
    """Return normalized event rows: {event, type, data, raw, order}."""
    out: list[dict] = []
    # SSE blocks with blank-line separators.
    if "data:" in text or "event:" in text:
        blocks = re.split(r"\n\s*\n", text.strip())
        for block in blocks:
            if not block.strip():
                continue
            event_name: Optional[str] = None
            data_lines: list[str] = []
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("event:"):
                    event_name = line[6:].strip() or None
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            data_str = "\n".join(data_lines).strip()
            if data_str == "[DONE]":
                out.append({"order": len(out) + 1, "event": event_name or "done", "type": "done", "data": None, "raw": block})
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                data = data_str
            typ = ""
            if isinstance(data, dict):
                typ = str(data.get("type") or "")
            out.append({"order": len(out) + 1, "event": event_name or typ or "data", "type": typ, "data": data, "raw": block})
        if out:
            return out

    # WebSocket log: one JSON object per line.
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            data = json.loads(s)
        except Exception:
            continue
        typ = str(data.get("type") or "") if isinstance(data, dict) else ""
        out.append({"order": len(out) + 1, "event": typ or "json", "type": typ, "data": data, "raw": s})
    return out


def _parse_response_json(obj: dict) -> list[Item]:
    items: list[Item] = []
    if isinstance(obj.get("choices"), list):
        for i, ch in enumerate(obj.get("choices") or [], 1):
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message") or ch.get("delta") or {}
            if isinstance(msg, dict):
                _parse_messages_list(items, [msg])
                if ch.get("finish_reason"):
                    _add_item(items, kind="finish", text=str(ch.get("finish_reason")), raw=ch,
                              summary=f"finish_reason={ch.get('finish_reason')}")
        if isinstance(obj.get("usage"), dict):
            _add_item(items, kind="usage", text=_json_dumps(obj.get("usage")), raw=obj.get("usage"), summary="usage")
        return items or [_fallback_item("response", _json_dumps(obj))]

    if isinstance(obj.get("output"), list):
        _parse_response_output_items(items, obj.get("output") or [])
        if isinstance(obj.get("usage"), dict):
            _add_item(items, kind="usage", text=_json_dumps(obj.get("usage")), raw=obj.get("usage"), summary="usage")
        if isinstance(obj.get("error"), dict):
            _add_item(items, kind="error", text=_json_dumps(obj.get("error")), raw=obj.get("error"), summary=_error_summary(obj.get("error")))
        return items or [_fallback_item("response", _json_dumps(obj))]

    if isinstance(obj.get("content"), list):
        _parse_messages_list(items, [{"role": obj.get("role") or "assistant", "content": obj.get("content")}])
        if isinstance(obj.get("usage"), dict):
            _add_item(items, kind="usage", text=_json_dumps(obj.get("usage")), raw=obj.get("usage"), summary="usage")
        return items or [_fallback_item("response", _json_dumps(obj))]

    if isinstance(obj.get("error"), dict):
        _add_item(items, kind="error", text=_json_dumps(obj.get("error")), raw=obj.get("error"), summary=_error_summary(obj.get("error")))
        return items
    return [_fallback_item("response", _json_dumps(obj))]


def _parse_response_output_items(items: list[Item], output: list) -> None:
    for oi, item in enumerate(output, 1):
        if not isinstance(item, dict):
            _add_item(items, kind="output", text=item, raw=item, summary=f"output[{oi}]")
            continue
        typ = str(item.get("type") or "output")
        if typ == "message":
            role = str(item.get("role") or "assistant")
            content = item.get("content") or []
            if isinstance(content, list):
                for ci, block in enumerate(content, 1):
                    if isinstance(block, dict):
                        bt = str(block.get("type") or "text")
                        kind = _kind_for_block(role, bt, block)
                        _add_item(items, kind=kind, text=block, raw=block, summary=f"{role} · {bt}",
                                  meta={"output_index": oi, "content_index": ci, "type": bt})
                    else:
                        _add_item(items, kind=role, text=block, raw=block, summary=f"{role} · content[{ci}]",
                                  meta={"output_index": oi, "content_index": ci})
            else:
                _add_item(items, kind=role, text=content, raw=item, summary=role, meta={"output_index": oi})
        elif typ in _TOOL_CALL_TYPES:
            nm = _tool_name(item)
            text = item.get("arguments") or item.get("input") or item
            _add_item(items, kind="tool_call", text=text, raw=item, summary=f"{typ}" + (f" · {nm}" if nm else ""),
                      meta={"output_index": oi, "type": typ, "tool_name": nm})
        elif typ in _TOOL_RESULT_TYPES:
            _add_item(items, kind="tool_result", text=item.get("output") or item, raw=item, summary=typ,
                      meta={"output_index": oi, "type": typ})
        elif typ in ("reasoning", "reasoning_summary"):
            _add_item(items, kind="reasoning", text=item.get("summary") or item.get("text") or item, raw=item, summary=typ,
                      meta={"output_index": oi, "type": typ})
        else:
            _add_item(items, kind=typ, text=item, raw=item, summary=typ, meta={"output_index": oi, "type": typ})


def _parse_responses_events(events: list[dict]) -> list[Item]:
    items: list[Item] = []
    text_parts: dict[tuple[int, int], str] = {}
    reasoning_parts: dict[tuple[int, int], str] = {}
    fc_args: dict[int, str] = {}
    output_items: dict[int, dict] = {}
    first_order: dict[tuple[str, int, int], int] = {}
    usage_obj: Optional[dict] = None
    completed_obj: Optional[dict] = None

    def _evt_name(e: dict) -> str:
        return str(e.get("event") or e.get("type") or "")

    for e in events:
        name = _evt_name(e)
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        typ = str(data.get("type") or name)
        order = int(e.get("order") or 0)
        if typ in ("response.output_item.added", "response.output_item.done"):
            idx = _safe_int(data.get("output_index"), 0)
            item = data.get("item")
            if isinstance(item, dict):
                output_items[idx] = dict(item)
        elif typ == "response.output_text.delta":
            idx = _safe_int(data.get("output_index"), 0)
            cidx = _safe_int(data.get("content_index"), 0)
            key = (idx, cidx)
            text_parts[key] = text_parts.get(key, "") + str(data.get("delta") or "")
            first_order.setdefault(("text", idx, cidx), order)
        elif typ in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta", "response.reasoning.delta"):
            idx = _safe_int(data.get("output_index"), 0)
            cidx = _safe_int(data.get("summary_index") or data.get("content_index"), 0)
            key = (idx, cidx)
            reasoning_parts[key] = reasoning_parts.get(key, "") + str(data.get("delta") or "")
            first_order.setdefault(("reasoning", idx, cidx), order)
        elif typ == "response.function_call_arguments.delta":
            idx = _safe_int(data.get("output_index"), 0)
            fc_args[idx] = fc_args.get(idx, "") + str(data.get("delta") or "")
            first_order.setdefault(("tool", idx, 0), order)
        elif typ in ("response.completed", "response.failed", "response.incomplete"):
            resp = data.get("response")
            if isinstance(resp, dict):
                completed_obj = resp
                if isinstance(resp.get("usage"), dict):
                    usage_obj = resp.get("usage")
                if isinstance(resp.get("output"), list):
                    for idx, item in enumerate(resp.get("output") or []):
                        if isinstance(item, dict):
                            output_items[idx] = dict(item)
        if typ in ("error", "response.failed") or isinstance(data.get("error"), dict) or (isinstance(data.get("response"), dict) and isinstance(data["response"].get("error"), dict)):
            err = data.get("error") or (data.get("response") or {}).get("error") or data
            _add_item(items, kind="error", text=_json_dumps(err), raw=data, summary=_error_summary(err),
                      meta={"event_order": order, "event": typ})

    built: list[tuple[int, Item]] = []
    # Prefer output_item metadata when available; merge accumulated deltas into it.
    temp_items: list[Item] = []
    for idx in sorted(output_items.keys()):
        item = dict(output_items[idx])
        if item.get("type") == "message":
            content = list(item.get("content") or [])
            for (oi, ci), txt in text_parts.items():
                if oi != idx:
                    continue
                if ci < len(content) and isinstance(content[ci], dict):
                    if not content[ci].get("text"):
                        content[ci]["text"] = txt
                else:
                    content.append({"type": "output_text", "text": txt, "annotations": []})
            item["content"] = content
        elif item.get("type") == "function_call" and fc_args.get(idx) and not item.get("arguments"):
            item["arguments"] = fc_args[idx]
        before = len(temp_items)
        _parse_response_output_items(temp_items, [item])
        for it in temp_items[before:]:
            built.append((first_order.get(("text", idx, 0), first_order.get(("tool", idx, 0), idx)), it))

    represented_text_indexes = set(output_items.keys())
    for (idx, cidx), txt in text_parts.items():
        if idx in represented_text_indexes:
            continue
        it: list[Item] = []
        _add_item(it, kind="assistant", text=txt, raw=txt, summary=f"output_text · {fmt_size(len(txt.encode('utf-8', errors='replace')))}",
                  meta={"output_index": idx, "content_index": cidx})
        built.append((first_order.get(("text", idx, cidx), 0), it[0]))
    for (idx, cidx), txt in reasoning_parts.items():
        it = []
        _add_item(it, kind="reasoning", text=txt, raw=txt, summary=f"reasoning · {fmt_size(len(txt.encode('utf-8', errors='replace')))}",
                  meta={"output_index": idx, "content_index": cidx})
        built.append((first_order.get(("reasoning", idx, cidx), 0), it[0]))
    for idx, args in fc_args.items():
        if idx in output_items:
            continue
        it = []
        _add_item(it, kind="tool_call", text=args, raw=args, summary=f"function_call args · {fmt_size(len(args.encode('utf-8', errors='replace')))}",
                  meta={"output_index": idx})
        built.append((first_order.get(("tool", idx, 0), 0), it[0]))

    for _, item in sorted(built, key=lambda x: x[0]):
        item["seq"] = len(items) + 1
        items.append(item)

    if usage_obj is not None:
        _add_item(items, kind="usage", text=_json_dumps(usage_obj), raw=usage_obj, summary=_usage_summary(usage_obj))
    elif completed_obj is not None:
        _add_item(items, kind="response", text=_json_dumps({k: v for k, v in completed_obj.items() if k != "output"}),
                  raw=completed_obj, summary=str(completed_obj.get("status") or "response.completed"))

    if not items:
        return _parse_raw_events(events)
    return _renumber(items)


def _parse_chat_events(events: list[dict]) -> list[Item]:
    content_parts: list[str] = []
    refusal_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    usage_obj: Optional[dict] = None
    finish_reason = ""
    errors: list[dict] = []

    for e in events:
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("error"), dict):
            errors.append(data.get("error") or data)
            continue
        choices = data.get("choices") or []
        if isinstance(choices, list):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta") or ch.get("message") or {}
                if isinstance(delta, dict):
                    if isinstance(delta.get("content"), str):
                        content_parts.append(delta["content"])
                    if isinstance(delta.get("refusal"), str):
                        refusal_parts.append(delta["refusal"])
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = _safe_int(tc.get("index"), len(tool_calls))
                        slot = tool_calls.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id") and not slot.get("id"):
                            slot["id"] = tc["id"]
                        if tc.get("type"):
                            slot["type"] = tc["type"]
                        fn = tc.get("function") or {}
                        if isinstance(fn, dict):
                            if fn.get("name") and not slot["function"].get("name"):
                                slot["function"]["name"] = fn["name"]
                            if isinstance(fn.get("arguments"), str):
                                slot["function"]["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish_reason = str(ch.get("finish_reason"))
        if isinstance(data.get("usage"), dict):
            usage_obj = data.get("usage")

    items: list[Item] = []
    if content_parts:
        text = "".join(content_parts)
        _add_item(items, kind="assistant", text=text, raw=text, summary=f"text · {fmt_size(len(text.encode('utf-8', errors='replace')))}")
    if refusal_parts:
        text = "".join(refusal_parts)
        _add_item(items, kind="refusal", text=text, raw=text, summary=f"refusal · {fmt_size(len(text.encode('utf-8', errors='replace')))}")
    for idx in sorted(tool_calls.keys()):
        tc = tool_calls[idx]
        nm = _tool_name(tc)
        args = ((tc.get("function") or {}).get("arguments") or "") if isinstance(tc.get("function"), dict) else _json_dumps(tc)
        _add_item(items, kind="tool_call", text=args, raw=tc, summary=f"{nm or 'function'} · args {fmt_size(len(args.encode('utf-8', errors='replace')))}")
    for err in errors:
        _add_item(items, kind="error", text=_json_dumps(err), raw=err, summary=_error_summary(err))
    if usage_obj is not None:
        _add_item(items, kind="usage", text=_json_dumps(usage_obj), raw=usage_obj, summary=_usage_summary(usage_obj))
    if finish_reason:
        _add_item(items, kind="finish", text=finish_reason, raw={"finish_reason": finish_reason}, summary=f"finish_reason={finish_reason}")
    return items or _parse_raw_events(events)


def _parse_anthropic_events(events: list[dict]) -> list[Item]:
    blocks: dict[int, dict] = {}
    partial_json: dict[int, str] = {}
    usage_obj: Optional[dict] = None
    errors: list[dict] = []
    message_meta: dict = {}

    for e in events:
        data = e.get("data")
        if not isinstance(data, dict):
            continue
        typ = str(data.get("type") or "")
        if typ == "message_start":
            message_meta = data.get("message") if isinstance(data.get("message"), dict) else {}
            if isinstance(message_meta.get("usage"), dict):
                usage_obj = message_meta.get("usage")
        elif typ == "content_block_start":
            idx = _safe_int(data.get("index"), 0)
            block = dict(data.get("content_block") or {})
            blocks[idx] = block
        elif typ == "content_block_delta":
            idx = _safe_int(data.get("index"), 0)
            delta = data.get("delta") or {}
            dt = str(delta.get("type") or "") if isinstance(delta, dict) else ""
            block = blocks.setdefault(idx, {})
            if dt == "text_delta":
                block["text"] = (block.get("text") or "") + str(delta.get("text") or "")
            elif dt == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + str(delta.get("thinking") or "")
            elif dt == "input_json_delta":
                partial_json[idx] = partial_json.get(idx, "") + str(delta.get("partial_json") or "")
            elif dt == "signature_delta":
                block["signature"] = (block.get("signature") or "") + str(delta.get("signature") or "")
        elif typ == "content_block_stop":
            idx = _safe_int(data.get("index"), 0)
            if idx in partial_json:
                raw = partial_json.pop(idx)
                try:
                    blocks.setdefault(idx, {})["input"] = json.loads(raw) if raw else {}
                except Exception:
                    blocks.setdefault(idx, {})["input"] = {"_raw": raw}
        elif typ == "message_delta":
            if isinstance(data.get("usage"), dict):
                usage_obj = data.get("usage")
        elif typ == "error" or isinstance(data.get("error"), dict):
            errors.append(data.get("error") or data)

    items: list[Item] = []
    for idx in sorted(blocks.keys()):
        b = blocks[idx]
        typ = str(b.get("type") or "text")
        kind = _kind_for_block("assistant", typ, b)
        nm = _tool_name(b)
        _add_item(items, kind=kind, text=b, raw=b, summary=f"{typ}" + (f" · {nm}" if nm else ""),
                  meta={"block_index": idx, "type": typ})
    for err in errors:
        _add_item(items, kind="error", text=_json_dumps(err), raw=err, summary=_error_summary(err))
    if usage_obj is not None:
        _add_item(items, kind="usage", text=_json_dumps(usage_obj), raw=usage_obj, summary=_usage_summary(usage_obj))
    if message_meta and not items:
        _add_item(items, kind="message", text=_json_dumps(message_meta), raw=message_meta, summary="message")
    return items or _parse_raw_events(events)


def _parse_raw_events(events: list[dict]) -> list[Item]:
    items: list[Item] = []
    for e in events:
        name = str(e.get("event") or e.get("type") or "event")
        raw = e.get("raw") or _json_dumps(e.get("data"))
        _add_item(items, kind="event", text=raw, raw=raw, summary=f"{name} · event#{e.get('order')}", meta={"event": name})
    return items


# ─── Filtering / sorting ─────────────────────────────────────────────

def filter_items(items: list[Item], query: str | None = None) -> list[Item]:
    q = (query or "").strip().lower()
    if not q:
        return list(items)
    out: list[Item] = []
    for it in items:
        hay = "\n".join([
            str(it.get("kind") or ""), str(it.get("title") or ""),
            str(it.get("summary") or ""), str(it.get("text") or ""),
            str(it.get("raw") or ""), _json_dumps(it.get("meta") or {}, compact=True),
        ]).lower()
        if q in hay:
            cp = dict(it)
            cp["match_count"] = hay.count(q)
            out.append(cp)
    return out


def sort_items(items: list[Item], sort_key: str = "original") -> list[Item]:
    key = sort_key if sort_key in SORT_KEYS else "original"
    if key == "reverse":
        return sorted(items, key=lambda x: int(x.get("seq") or 0), reverse=True)
    if key == "size":
        return sorted(items, key=lambda x: (int(x.get("size") or 0), int(x.get("seq") or 0)), reverse=True)
    if key == "type":
        return sorted(items, key=lambda x: (str(x.get("kind") or ""), int(x.get("seq") or 0)))
    return sorted(items, key=lambda x: int(x.get("seq") or 0))


def next_sort(sort_key: str | None) -> str:
    cur = sort_key if sort_key in SORT_KEYS else "original"
    i = SORT_KEYS.index(cur)
    return SORT_KEYS[(i + 1) % len(SORT_KEYS)]


def button_label(item: Item, *, selected: bool = False, compact: bool = True) -> str:
    prefix = "✅ " if selected else ""
    seq = int(item.get("seq") or 0)
    kind = str(item.get("kind") or "message")
    summary = summary_short_label(str(item.get("summary") or "")) if compact else summary_label(str(item.get("summary") or ""))
    size = fmt_size(int(item.get("size") or 0))
    kind_text = kind_short_label(kind) if compact else kind_label(kind)
    if compact:
        if kind == "tool_call":
            parts = [p.strip() for p in summary.split("·") if p.strip()]
            if len(parts) >= 2 and parts[0] in {"函数", "调用", "自定义", "MCP"}:
                summary = " · ".join(parts[1:])
        elif kind == "tool_result" and summary in {"函数结果", "自定义结果", "MCP结果", "结果"}:
            summary = ""
        base = f"{prefix}#{seq} {kind_text}" + (f" · {summary}" if summary else "") + f" · {size}"
        return _compact_ws(base, 34)
    base = f"{prefix}#{seq} {kind_text} · {summary} · {size}"
    return _compact_ws(base, 58)


_META_KINDS = {"usage", "finish", "params", "metadata", "tools", "tool_choice", "response"}


def selected_item(items: list[Item], selected_seq: int | None = None) -> Item | None:
    if not items:
        return None
    if selected_seq is not None:
        for it in items:
            try:
                if int(it.get("seq") or 0) == int(selected_seq):
                    return it
            except Exception:
                pass
    # Default to the last real message/content unit, not trailing usage/finish metadata.
    for it in reversed(items):
        if str(it.get("kind") or "") not in _META_KINDS:
            return it
    return items[-1]


def _fallback_item(kind: str, text: str) -> Item:
    return {
        "seq": 1,
        "kind": kind,
        "title": kind,
        "summary": _compact_ws(text, 36) or "raw",
        "text": text,
        "raw": text,
        "size": len(text.encode("utf-8", errors="replace")),
        "meta": {},
    }


def _renumber(items: list[Item]) -> list[Item]:
    for i, it in enumerate(items, 1):
        it["seq"] = i
    return items


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _usage_summary(u: Any) -> str:
    if not isinstance(u, dict):
        return "usage"
    inp = u.get("input_tokens", u.get("prompt_tokens", "?"))
    out = u.get("output_tokens", u.get("completion_tokens", "?"))
    return f"usage ↑{inp} ↓{out}"


def _error_summary(err: Any) -> str:
    if not isinstance(err, dict):
        return _compact_ws(str(err), 42)
    msg = err.get("message") or err.get("reason") or err.get("code") or err.get("type") or _json_dumps(err, compact=True)
    code = err.get("code") or err.get("type") or err.get("error_type")
    if code and str(code) not in str(msg):
        return _compact_ws(f"{code}: {msg}", 42)
    return _compact_ws(str(msg), 42)
