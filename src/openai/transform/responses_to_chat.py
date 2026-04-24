"""Chat Completions ⇄ Responses 的请求/响应翻译（responses → chat 方向）。

方向说明：
  - 下游入口：`/v1/responses`（`ingress="responses"`）
  - 上游协议：`openai-chat`（打 `/v1/chat/completions`）
  - 请求：`translate_request(responses_body)` → chat body
  - 响应：`translate_response(chat_json, model=...)` → responses JSON

MS-3 约束：
  - `previous_response_id` 未接 Store → guard 阶段已拒绝，本模块不读 Store
  - `conversation` 未支持 → guard 已拒绝
  - built-in tools / input 含内置 call items → guard 已拒绝
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


# ═══════════════════════════════════════════════════════════════
# 请求：responses → chat
# ═══════════════════════════════════════════════════════════════


def translate_request(body: dict, *, api_key_name: str = "") -> dict:
    """Responses 请求翻译为 Chat 请求。

    前置：guard.guard_responses_to_chat 已做跨变体死角检查。
    若 body 含 `previous_response_id`，会向 openai.store 展开历史。
    Store 查询异常（NotFound/Expired/Forbidden）通过 GuardError 抛出，由
    failover / handler 映射成 4xx。
    """
    input_items = _resolve_input(body, api_key_name=api_key_name)
    messages = _input_items_to_messages(input_items)

    # instructions → 首条 system（在任何历史之前）
    if body.get("instructions"):
        messages.insert(0, {"role": "system", "content": body["instructions"]})

    payload: dict[str, Any] = {"model": body["model"], "messages": messages}

    for k in ("stream", "temperature", "top_p", "parallel_tool_calls", "user"):
        if k in body:
            payload[k] = body[k]

    if "max_output_tokens" in body:
        payload["max_completion_tokens"] = body["max_output_tokens"]

    text_cfg = body.get("text") or {}
    fmt = text_cfg.get("format") if isinstance(text_cfg, dict) else None
    if fmt:
        payload["response_format"] = fmt

    reasoning = body.get("reasoning") or {}
    eff = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if eff:
        payload["reasoning_effort"] = eff
    # 02-bug-findings #12: reasoning.summary → 非官方 chat 字段 reasoning_summary
    summary = reasoning.get("summary") if isinstance(reasoning, dict) else None
    if summary:
        payload["reasoning_summary"] = summary

    # 02-bug-findings #11: text.verbosity ↔ chat verbosity（同 enum low/medium/high）
    if isinstance(text_cfg, dict) and text_cfg.get("verbosity"):
        payload["verbosity"] = text_cfg["verbosity"]

    if body.get("tools"):
        payload["tools"] = [_nest_tool(t) for t in body["tools"]]

    if "tool_choice" in body:
        payload["tool_choice"] = _translate_tool_choice_r2c(body["tool_choice"])

    for k in ("metadata", "service_tier", "safety_identifier",
              "prompt_cache_key", "prompt_cache_retention", "store"):
        if k in body:
            payload[k] = body[k]

    return payload


# ─── 辅助 ────────────────────────────────────────────────────────


def _resolve_input(body: dict, *, api_key_name: str = "") -> list:
    """把 body.input 统一为 list[item]；若带 previous_response_id 则先展开历史。

    Store 查询异常映射为 GuardError，由调用方转成 4xx 短路。
    """
    prev_id = body.get("previous_response_id")
    history: list = []
    if prev_id:
        from . import guard as _guard
        from .. import store as _store
        if not _store.is_enabled():
            raise _guard.GuardError(
                400, "invalid_request_error",
                "previous_response_id requires openai.store.enabled=true",
                param="previous_response_id",
            )
        try:
            history = _store.expand_history(str(prev_id), api_key_name=api_key_name)
        except _store.ResponseNotFound:
            raise _guard.GuardError(
                404, "not_found_error",
                f"previous_response_id '{prev_id}' not found (or already expired)",
                param="previous_response_id",
            )
        except _store.ResponseExpired:
            raise _guard.GuardError(
                410, "not_found_error",
                f"previous_response_id '{prev_id}' has expired",
                param="previous_response_id",
            )
        except _store.ResponseForbidden:
            raise _guard.GuardError(
                403, "permission_error",
                f"previous_response_id '{prev_id}' does not belong to this API key",
                param="previous_response_id",
            )

    cur = body.get("input")
    if isinstance(cur, str):
        cur_items: list = [{"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": cur}]}]
    elif isinstance(cur, list):
        cur_items = list(cur)
    else:
        cur_items = []

    return list(history) + cur_items


def resolve_current_input_items(body: dict) -> list:
    """仅返回"本次请求"的 input items（不包含 previous_response_id 展开的历史）。

    用于 Store 写入：`input_items` 字段存的是当前请求的输入，`output_items`
    存本次响应。下次请求带 previous_response_id 时，expand_history 会沿
    parent_id 链递归拼出完整历史。
    """
    cur = body.get("input")
    if isinstance(cur, str):
        return [{"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": cur}]}]
    if isinstance(cur, list):
        return list(cur)
    return []


def _input_items_to_messages(items: list) -> list:
    """input items → chat messages[]；function_call items 聚合到前一条 assistant。

    reasoning 处理（passthrough 模式）：
      - 历史 reasoning item 的 summary_text / reasoning_text 拼接后，
        写到紧随的 assistant message 的 `reasoning_content` 字段
      - 所有 assistant 消息都会带上 `reasoning_content`（无内容时为空串）：
        DeepSeek v4 thinking mode 要求每条 assistant 都带这个非官方字段，
        其它 chat 上游会忽略该字段，兼容无影响
      - drop 模式不写 reasoning_content（用户主动关闭桥接时不再增加字段）
    """
    from .common import reasoning_passthrough_enabled
    bridge = reasoning_passthrough_enabled()

    messages: list[dict] = []
    pending_assistant: Optional[dict] = None
    # 收集紧随下一条 assistant 之前的 reasoning 文本；遇到 user/system 跳轮时丢弃
    pending_reasoning: list[str] = []

    def _pop_reasoning() -> str:
        text = "\n\n".join(s for s in pending_reasoning if s)
        pending_reasoning.clear()
        return text

    def _flush():
        nonlocal pending_assistant
        if pending_assistant is not None:
            if bridge:
                pending_assistant.setdefault("reasoning_content", _pop_reasoning())
            messages.append(pending_assistant)
            pending_assistant = None

    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        # spec: EasyInputMessage.type optional (const "message")
        # 02-bug-findings #1: 裸消息 {role,content} 在原代码里完全丢失。
        # type 缺失但存在合法 role 时，视同 message 处理。
        if t is None and item.get("role") in ("user", "assistant", "system", "developer"):
            t = "message"

        if t == "message":
            _flush()
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"   # chat 只认 system/user/assistant/tool
            content_parts = item.get("content") or []
            if role == "assistant":
                # Responses 的 assistant message 的 content 只可能是 output_text / refusal；
                # 回喂 chat 上游时要把 refusal parts 单独提取到 message.refusal 字段，
                # 避免信息被折叠成空 text 丢失
                refusal_texts: list[str] = []
                non_refusal_parts: list = []
                for p in content_parts:
                    if isinstance(p, dict) and p.get("type") == "refusal":
                        r = p.get("refusal")
                        if isinstance(r, str) and r:
                            refusal_texts.append(r)
                    else:
                        non_refusal_parts.append(p)
                # 纯 refusal（无文本/图片 parts）→ content 必须是 null，
                # 否则严格上游（如官方 gpt-*）会因 assistant.content 为空列表而 400
                msg_out: dict = {
                    "role": role,
                    "content": (_content_responses_to_chat(non_refusal_parts)
                                if non_refusal_parts else None),
                }
                if refusal_texts:
                    msg_out["refusal"] = "\n".join(refusal_texts)
                # 02-bug-findings #10: assistant 全空（无 text/refusal/reasoning）→ skip
                # 避免向严格上游发出 content=None && tool_calls 缺失的 assistant message。
                # （pending_assistant 路径有 tool_calls 由 _flush 处理；这里只管直接走
                #  message 分支的情况）
                bridge_text = _pop_reasoning() if bridge else None
                has_content = msg_out["content"] is not None
                has_refusal = bool(refusal_texts)
                has_reasoning = bool(bridge_text)
                if not (has_content or has_refusal or has_reasoning):
                    # 完全空 → skip
                    continue
                if bridge:
                    msg_out["reasoning_content"] = bridge_text or ""
                messages.append(msg_out)
            else:
                # 非 assistant 的 message（user/system/tool）跳轮新上下文，
                # 丢弃残留的 reasoning（避免给下一次 assistant 带上上轮的思考）
                pending_reasoning.clear()
                messages.append({
                    "role": role,
                    "content": _content_responses_to_chat(content_parts),
                })

        elif t == "function_call":
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": None, "tool_calls": []}
            pending_assistant.setdefault("tool_calls", []).append({
                "id": item.get("call_id") or _gen_id("call_"),
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "",
                },
            })

        elif t == "function_call_output":
            _flush()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": item.get("output") or "",
            })

        elif t == "custom_tool_call":
            # 02-bug-findings #27 反向：responses CustomToolCall
            # → chat assistant.tool_calls type=custom
            if pending_assistant is None:
                pending_assistant = {"role": "assistant", "content": None, "tool_calls": []}
            pending_assistant.setdefault("tool_calls", []).append({
                "id": item.get("call_id") or _gen_id("call_"),
                "type": "custom",
                "custom": {
                    "name": item.get("name") or "",
                    "input": item.get("input") or "",
                },
            })

        elif t == "custom_tool_call_output":
            # CustomToolCallOutput 与 function_call_output 在 chat 端落到同一种 tool message
            _flush()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": item.get("output") or "",
            })

        elif t == "reasoning":
            # passthrough 模式：聚合 summary_text / reasoning_text 给下一条 assistant 用
            # drop 模式：全部忽略
            if bridge:
                for s in item.get("summary") or []:
                    if isinstance(s, dict) and s.get("type") == "summary_text":
                        txt = s.get("text")
                        if isinstance(txt, str) and txt:
                            pending_reasoning.append(txt)
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "reasoning_text":
                        txt = c.get("text")
                        if isinstance(txt, str) and txt:
                            pending_reasoning.append(txt)

        elif t in (
            "web_search_call", "file_search_call", "computer_call",
            "image_generation_call", "code_interpreter_call",
            "mcp_call", "mcp_list_tools", "mcp_approval_request",
            "mcp_approval_response", "local_shell_call", "local_shell_call_output",
            "item_reference",
        ):
            # guard 已拦；防御性 skip
            pass

    _flush()
    return messages


def _content_responses_to_chat(content) -> Any:
    """Responses message content[] → chat parts 或 string。"""
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    out: list[dict] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if pt in ("input_text", "output_text"):
            out.append({"type": "text", "text": p.get("text", "")})
        elif pt == "input_image":
            url = p.get("image_url") or ""
            detail = p.get("detail") or "auto"
            iu: dict = {"url": url, "detail": detail}
            # 02-bug-findings #4: file_id 双向透传
            if p.get("file_id"):
                iu["file_id"] = p["file_id"]
            out.append({"type": "image_url", "image_url": iu})
        elif pt == "input_file":
            f: dict = {}
            # 02-bug-findings #5: file_url + detail 双向透传
            for k in ("file_id", "file_data", "filename", "file_url", "detail"):
                if k in p:
                    f[k] = p[k]
            out.append({"type": "file", "file": f})
        elif pt == "input_audio":
            out.append({"type": "input_audio", "input_audio": p.get("input_audio") or {}})
        elif pt == "refusal":
            # chat 里没有 refusal part；用空 text 占位，refusal 字段由 translate_response 单独带
            out.append({"type": "text", "text": ""})
    # 只有一条 text → 简化为字符串，兼容旧客户端
    if len(out) == 1 and out[0].get("type") == "text":
        return out[0]["text"]
    return out


def _nest_tool(t: dict) -> dict:
    if not isinstance(t, dict):
        return t
    if t.get("type") == "function":
        fn: dict = {}
        for k in ("name", "description", "parameters", "strict"):
            if k in t:
                fn[k] = t[k]
        return {"type": "function", "function": fn}
    if t.get("type") == "custom":
        # 02-bug-findings #26 反向：responses CustomTool {type:custom, name, ...}
        # → chat CustomToolChatCompletions {type:custom, custom:{name, ...}}
        nested = {}
        for k in ("name", "description", "format"):
            if k in t:
                nested[k] = t[k]
        return {"type": "custom", "custom": nested}
    return dict(t)


def _translate_tool_choice_r2c(tc):
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "function":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
        if ttype == "custom":
            # 02-bug-findings #23: responses {type:custom, name} → chat {type:custom, custom:{name}}
            return {"type": "custom", "custom": {"name": tc.get("name", "")}}
        if ttype == "allowed_tools":
            # 02-bug-findings #24: responses {type:allowed_tools, mode, tools}
            # → chat {type:allowed_tools, allowed_tools:{mode, tools}}
            nested: dict = {}
            if "mode" in tc:
                nested["mode"] = tc["mode"]
            if "tools" in tc:
                nested["tools"] = [_nest_tool(x) if isinstance(x, dict) else x
                                   for x in (tc["tools"] or [])]
            return {"type": "allowed_tools", "allowed_tools": nested}
    return tc


# ═══════════════════════════════════════════════════════════════
# 响应：chat.completion JSON → responses JSON
# ═══════════════════════════════════════════════════════════════


def translate_response(chat: dict, *, model: str,
                       previous_response_id: Optional[str] = None,
                       api_key_name: Optional[str] = None,
                       channel_key: Optional[str] = None,
                       current_input_items: Optional[list] = None) -> dict:
    """Chat 非流式 JSON → Responses 非流式 JSON。

    当 `current_input_items` 非 None 且 `api_key_name` 非空时，把本次响应
    存入 openai.store（供下次 previous_response_id 续接使用）。
    """
    choices = chat.get("choices") or [{}]
    choice0 = choices[0] if choices else {}
    msg = choice0.get("message") or {}
    finish_reason = choice0.get("finish_reason")

    output_items: list[dict] = []

    # reasoning_content（非官方字段）→ reasoning item；
    # drop 模式丢弃文本（usage.reasoning_tokens 仍透传）
    reasoning_text = msg.get("reasoning_content")
    if isinstance(reasoning_text, str) and reasoning_text:
        from .common import reasoning_passthrough_enabled
        if reasoning_passthrough_enabled():
            output_items.append({
                "type": "reasoning",
                "id": _gen_id("rs_"),
                "summary": [{"type": "summary_text", "text": reasoning_text}],
            })

    # content text → message item
    content = msg.get("content")
    if isinstance(content, str) and content:
        # 02-bug-findings #28: 把 chat assistant.annotations 回填到 output_text.annotations
        ann_list = msg.get("annotations") if isinstance(msg.get("annotations"), list) else []
        output_items.append({
            "type": "message",
            "id": _gen_id("msg_"),
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": content,
                          "annotations": list(ann_list)}],
        })

    # refusal → message with refusal part
    refusal = msg.get("refusal")
    if isinstance(refusal, str) and refusal:
        output_items.append({
            "type": "message",
            "id": _gen_id("msg_"),
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "refusal", "refusal": refusal}],
        })

    # tool_calls → function_call items
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        output_items.append({
            "type": "function_call",
            "id": _gen_id("fc_"),
            "call_id": tc.get("id") or _gen_id("call_"),
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "",
            "status": "completed",
        })

    status, incomplete = _finish_reason_to_status(finish_reason, bool(msg.get("tool_calls")))

    output_text = "".join(
        c["text"] for it in output_items if it.get("type") == "message"
        for c in (it.get("content") or []) if c.get("type") == "output_text"
    )

    resp_id = _gen_id("resp_")
    resp: dict = {
        "id": resp_id,
        "object": "response",
        "created_at": int(chat.get("created") or time.time()),
        "status": status,
        "error": None,
        "incomplete_details": incomplete,
        "model": model,
        "previous_response_id": previous_response_id,
        "output": output_items,
        "output_text": output_text,
        "usage": _usage_chat_to_resps(chat.get("usage") or {}),
    }

    # 写 Store：只在 responses 入口 + chat 上游 + 有 api_key_name 时触发。
    # Store 失败不中断主响应（客户端已拿到结果），但要走节流告警：
    # 下一次带 previous_response_id 的请求会 404，必须让运维能及时看到。
    if current_input_items is not None and api_key_name:
        try:
            from .. import store as _store
            if _store.is_enabled():
                _store.save(
                    response_id=resp_id,
                    parent_id=previous_response_id,
                    api_key_name=api_key_name,
                    model=model,
                    channel_key=channel_key,
                    input_items=list(current_input_items),
                    output_items=output_items,
                )
        except Exception as exc:
            import traceback as _tb
            _tb.print_exc()
            from ... import notifier as _notifier
            ek = _notifier.escape_html
            _notifier.throttled_notify_event_sync(
                "openai_store_save_failed",
                f"openai_store_save_failed:{api_key_name}",
                "❌ <b>OpenAI Store 写入失败</b>（非流式）\n"
                f"API Key: <code>{ek(api_key_name)}</code>\n"
                f"模型: <code>{ek(model)}</code> · 渠道: <code>{ek(channel_key or '?')}</code>\n"
                f"resp_id: <code>{ek(resp_id)}</code>\n"
                f"原因: <code>{ek(str(exc))[:300]}</code>\n"
                "⚠ 下一次带该 previous_response_id 的请求会 404；"
                "请检查 state.db 读写权限与磁盘空间。",
            )

    return resp


def _finish_reason_to_status(finish_reason: Optional[str],
                             has_tool_calls: bool) -> tuple[str, Optional[dict]]:
    if finish_reason in (None, "stop"):
        return ("completed", None)
    if finish_reason == "tool_calls" or finish_reason == "function_call":
        return ("completed", None)
    if finish_reason == "length":
        return ("incomplete", {"reason": "max_output_tokens"})
    if finish_reason == "content_filter":
        return ("incomplete", {"reason": "content_filter"})
    # 其他：保守归 completed
    return ("completed", None)


def _usage_chat_to_resps(u: dict) -> dict:
    # 02-bug-findings #9: details fields must always be written.
    # Unified through common.build_response_usage (avoid hand-copy in 4 places).
    from .common import build_response_usage
    prompt_tokens = int(u.get("prompt_tokens", 0) or 0)
    completion_tokens = int(u.get("completion_tokens", 0) or 0)
    total_tokens = int(u.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    prompt_details = u.get("prompt_tokens_details") or {}
    completion_details = u.get("completion_tokens_details") or {}
    cached = int(prompt_details.get("cached_tokens", 0) or 0)
    reasoning = int(completion_details.get("reasoning_tokens", 0) or 0)
    return build_response_usage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total_tokens,
    )
