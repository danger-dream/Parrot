"""Chat Completions ⇄ Responses 的请求/响应翻译（chat → responses 方向）。

方向说明：
  - 下游入口：`/v1/chat/completions`（`ingress="chat"`）
  - 上游协议：`openai-responses`（打 `/v1/responses`）
  - 请求：`translate_request(chat_body)` → responses body
  - 响应：`translate_response(responses_json, model=...)` → chat.completion JSON

MS-3 不接 `previous_response_id`；若 chat 下游真的同时又切到 responses 上游，
没有 Store 辅助也无需考虑（MS-5 再补）。

reasoning 字段在 MS-3 做**最小化**处理：
  - translate_response：仅在有 `reasoning` summary 时映射到非官方
    `message.reasoning_content`，完整桥接在 MS-6。
  - translate_request：不插 reasoning item（chat 请求里不会带 reasoning_content）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional


# ─── id 生成 ──────────────────────────────────────────────────────

def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


# ═══════════════════════════════════════════════════════════════
# 请求：chat → responses
# ═══════════════════════════════════════════════════════════════


def translate_request(body: dict) -> dict:
    """把 Chat 请求翻译为 Responses 请求。

    调用方需在此之前完成 CapabilityGuard 的检查（参见 guard.guard_chat_to_responses）。
    """
    payload: dict[str, Any] = {
        "model": body["model"],
        "input": _messages_to_input_items(body.get("messages") or []),
    }

    # 透传字段
    for k in ("stream", "temperature", "top_p", "parallel_tool_calls", "user"):
        if k in body:
            payload[k] = body[k]

    # stream_options.include_usage 在 responses 里不存在（usage 总在 response.completed），丢弃即可

    # max_tokens 映射（首选 max_completion_tokens，其次旧 max_tokens）
    if "max_completion_tokens" in body:
        payload["max_output_tokens"] = body["max_completion_tokens"]
    elif "max_tokens" in body:
        payload["max_output_tokens"] = body["max_tokens"]

    # response_format → text.format；两边结构同构（type:text/json_object/json_schema）
    if "response_format" in body:
        payload.setdefault("text", {})["format"] = body["response_format"]

    # reasoning_effort → reasoning.effort
    if "reasoning_effort" in body:
        payload.setdefault("reasoning", {})["effort"] = body["reasoning_effort"]
    # 02-bug-findings #12: reasoning_summary（非官方 chat 字段，DeepSeek 等生态使用）
    # → reasoning.summary（spec ReasoningParam.summary 取值 auto/concise/detailed）
    if "reasoning_summary" in body:
        payload.setdefault("reasoning", {})["summary"] = body["reasoning_summary"]
    # 02-bug-findings #11: chat verbosity ↔ responses text.verbosity（同 enum low/medium/high）
    if "verbosity" in body:
        payload.setdefault("text", {})["verbosity"] = body["verbosity"]

    # tools 扁平化
    if body.get("tools"):
        payload["tools"] = [_flatten_tool(t) for t in body["tools"]]

    # tool_choice：string 直通；{type:function, function:{name}} → {type:function, name}
    if "tool_choice" in body:
        payload["tool_choice"] = _translate_tool_choice_c2r(body["tool_choice"])

    # 透传兼容字段
    for k in ("metadata", "service_tier", "safety_identifier",
              "prompt_cache_key", "prompt_cache_retention", "store"):
        if k in body:
            payload[k] = body[k]

    return payload


def _messages_to_input_items(messages: list) -> list:
    """messages[] → Responses input items 列表。"""
    items: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            # tool 响应 → function_call_output
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or "",
                "output": _stringify_tool_content(msg.get("content")),
            })
            continue
        # spec: ChatCompletionRequestFunctionMessage (deprecated legacy)
        # 02-bug-findings #15: role="function" 老协议原代码会落入 default 分支
        # 被当 user 处理后发给 responses 上游会 400。应映射为 function_call_output。
        # legacy 协议没有 tool_call_id 字段，用 name 充当 call_id（与老协议下
        # assistant.function_call.name 同名占位，允许下游原话联走）。
        if role == "function":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("name") or "",
                "output": _stringify_tool_content(msg.get("content")),
            })
            continue
        if role == "assistant":
            # 非官方 reasoning_content（DeepSeek 等 chat 生态）→ reasoning item，
            # 保持与上游产出时同构，避免历史 reasoning 丢失。drop 模式不映射。
            from .common import reasoning_passthrough_enabled
            reasoning_text = msg.get("reasoning_content")
            if (isinstance(reasoning_text, str) and reasoning_text
                    and reasoning_passthrough_enabled()):
                items.append({
                    "type": "reasoning",
                    "id": _gen_id("rs_"),
                    "summary": [{"type": "summary_text", "text": reasoning_text}],
                })
            # 可能同时有 content 和 tool_calls；refusal 罕见但处理
            content = msg.get("content")
            if isinstance(content, str) and content:
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                })
            elif isinstance(content, list) and content:
                # chat 的 parts content 在 assistant 上罕见，简化为文本拼接
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text",
                                 "text": _stringify_tool_content(content),
                                 "annotations": []}],
                })
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                call_id = tc.get("id") or _gen_id("call_")
                # 02-bug-findings #27: type=custom 的 tool_call 翻译为
                # responses CustomToolCall {type:custom_tool_call, name, input}
                # 之前一律当 function 处理，导致 responses 上游 schema 不对。
                if tc.get("type") == "custom":
                    c = tc.get("custom") or {}
                    items.append({
                        "type": "custom_tool_call",
                        "id": f"ctc_{call_id}",
                        "call_id": call_id,
                        "name": c.get("name") or "",
                        "input": c.get("input") or "",
                        "status": "completed",
                    })
                    continue
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "id": f"fc_{call_id}",   # 合成稳定 fc_ 前缀 id
                    "call_id": call_id,
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                    "status": "completed",
                })
            if msg.get("refusal"):
                items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "refusal", "refusal": msg["refusal"]}],
                })
            continue

        # system / developer / user
        mapped_role = role or "user"
        if mapped_role == "system":
            mapped_role = "developer"  # Responses 推荐 developer
        items.append({
            "type": "message", "role": mapped_role,
            "content": _content_chat_to_responses(msg.get("content", "")),
        })
    return items


def _content_chat_to_responses(content) -> list:
    """messages[i].content（string 或 parts）→ Responses message content 列表。"""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": ""}]
    out: list[dict] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            out.append({"type": "input_text", "text": p.get("text", "")})
        elif t == "image_url":
            iu = p.get("image_url")
            if isinstance(iu, dict):
                image = {
                    "type": "input_image",
                    "image_url": iu.get("url", ""),
                    "detail": iu.get("detail") or "auto",
                }
                if iu.get("file_id"):
                    image["file_id"] = iu["file_id"]
            else:
                image = {"type": "input_image", "image_url": iu or "", "detail": "auto"}
            out.append(image)
        elif t == "input_audio":
            out.append({"type": "input_audio", "input_audio": p.get("input_audio") or {}})
        elif t == "file":
            f = p.get("file") or {}
            entry: dict = {"type": "input_file"}
            # 02-bug-findings #5: file_url + detail 双向透传
            for k in ("file_id", "file_data", "filename", "file_url", "detail"):
                if k in f:
                    entry[k] = f[k]
            out.append(entry)
        else:
            # 未知 part 类型：保守丢弃，避免上游 schema 错误
            pass
    if not out:
        # 防止 Responses 拒收空 content
        out.append({"type": "input_text", "text": ""})
    return out


def _stringify_tool_content(content) -> str:
    """tool / function_call_output 的 content 归一成字符串（responses 要求 string）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    # 其他：dump 成 JSON 字符串
    import json as _json
    try:
        return _json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _flatten_tool(t: dict) -> dict:
    """chat-style tool → responses-style flat tool。

    支持 type=function 与 type=custom；其他 type 保守返回原 dict。
    """
    if not isinstance(t, dict):
        return t
    if t.get("type") == "function":
        fn = t.get("function") or {}
        out: dict = {"type": "function"}
        for k in ("name", "description", "parameters", "strict"):
            if k in fn:
                out[k] = fn[k]
        # 02-bug-findings #30: spec FunctionTool required: type, name, strict, parameters
        # 上游不传 strict / parameters 时本代码以前不写，responses 上游会 400。
        out.setdefault("strict", False)
        out.setdefault("parameters", {})
        return out
    if t.get("type") == "custom":
        # 02-bug-findings #26: chat 侧 CustomToolChatCompletions
        # {type:custom, custom:{name, description?, format?}}
        # 展开为 responses 侧 CustomTool {type:custom, name, description?, format?}
        c = t.get("custom") or {}
        out_c: dict = {"type": "custom"}
        for k in ("name", "description", "format"):
            if k in c:
                out_c[k] = c[k]
        return out_c
    # 其他 type：保守返回原 dict（guard 已拦非 function/custom 的 built-in）
    return dict(t)


def _translate_tool_choice_c2r(tc):
    """chat tool_choice → responses tool_choice。

    支持的形态：
      - string ("auto" / "none" / "required")
      - {type:function, function:{name}} → {type:function, name}
      - 02-bug-findings #23: {type:custom, custom:{name}} → {type:custom, name}
      - 02-bug-findings #24: {type:allowed_tools, allowed_tools:{mode, tools}}
        → {type:allowed_tools, mode, tools}
    """
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "function":
            fn = tc.get("function") or {}
            return {"type": "function", "name": fn.get("name", "")}
        if ttype == "custom":
            c = tc.get("custom") or {}
            out = {"type": "custom"}
            if "name" in c:
                out["name"] = c["name"]
            elif "name" in tc:  # 容忍已经是 flat 形态
                out["name"] = tc["name"]
            return out
        if ttype == "allowed_tools":
            nested = tc.get("allowed_tools") or {}
            out2 = {"type": "allowed_tools"}
            mode = nested.get("mode") or tc.get("mode")
            if mode is not None:
                out2["mode"] = mode
            tools = nested.get("tools") or tc.get("tools")
            if tools is not None:
                # tools 内每项也按 _flatten_tool 拍平
                out2["tools"] = [_flatten_tool(x) if isinstance(x, dict) else x
                                 for x in tools]
            return out2
    return tc


# ═══════════════════════════════════════════════════════════════
# 响应：收到 responses JSON → 回 chat.completion 风格
# ═══════════════════════════════════════════════════════════════


def translate_response(resp: dict, *, model: str) -> dict:
    """Responses 非流式 JSON → chat.completion 非流式 JSON。

    不修改入参 resp。
    """
    output = list(resp.get("output") or [])
    content_text = resp.get("output_text") or _gather_output_text(output)
    tool_calls = _gather_function_calls(output)
    refusal = _gather_refusal(output)
    reasoning_text = _gather_reasoning_summary(output)
    # 02-bug-findings #28: 把 output_text.annotations 累计回填到 chat
    # message.annotations（chat ResponseMessage.annotations 与 responses
    # OutputTextContent.annotations 是 1:1 url_citation 等结构，spec-compliant）
    annotations = _gather_annotations(output)

    message: dict[str, Any] = {"role": "assistant", "content": content_text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if refusal:
        message["refusal"] = refusal
    if reasoning_text is not None:
        # 非官方字段：DeepSeek 等生态使用，兼容客户端会忽略
        message["reasoning_content"] = reasoning_text
    if annotations:
        message["annotations"] = annotations

    finish_reason = _status_to_finish_reason(resp, has_tool_calls=bool(tool_calls))

    # id 生成：复用 responses id 的后缀，便于链路排查
    resp_id = resp.get("id") or _gen_id("resp_")
    chat_id = f"chatcmpl-{resp_id.replace('resp_', '')}"

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(resp.get("created_at") or time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
            "logprobs": None,
        }],
        "usage": _usage_resps_to_chat(resp.get("usage") or {}),
    }


# ─── 响应辅助 ────────────────────────────────────────────────────


def _gather_output_text(output: list) -> str:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
    return "".join(parts)


def _gather_annotations(output: list) -> list[dict]:
    """02-bug-findings #28: 收集 output items 中所有 output_text.annotations。"""
    out: list[dict] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                ann = c.get("annotations")
                if isinstance(ann, list):
                    out.extend(a for a in ann if isinstance(a, dict))
    return out


def _gather_function_calls(output: list) -> list[dict]:
    out: list[dict] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id") or item.get("id") or _gen_id("call_")
        out.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": item.get("name") or "",
                "arguments": item.get("arguments") or "",
            },
        })
    return out


def _gather_refusal(output: list) -> Optional[str]:
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "refusal":
                r = c.get("refusal")
                if isinstance(r, str) and r:
                    parts.append(r)
    return "\n".join(parts) if parts else None


def _gather_reasoning_summary(output: list) -> Optional[str]:
    """从 reasoning items 聚合推理文本。

    一个 reasoning item 官方结构：
      - `summary[]`：summary_text（精简摘要，大多数模型仅输出这个）
      - `content[]`：reasoning_text（原文推理，部分模型独立产出）
      - `encrypted_content`：加密原文，chat 侧没有对应字段 → 不处理

    两处都尝试读，按 item 顺序合并；当 `openai.reasoningBridge == "drop"`
    时直接返回 None（usage.reasoning_tokens 不受影响，仍由 _usage_resps_to_chat 透传）。
    """
    from .common import reasoning_passthrough_enabled
    if not reasoning_passthrough_enabled():
        return None
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for s in item.get("summary") or []:
            if isinstance(s, dict) and s.get("type") == "summary_text":
                t = s.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "reasoning_text":
                t = c.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
    return "\n\n".join(parts) if parts else None


def _status_to_finish_reason(resp: dict, *, has_tool_calls: bool) -> str:
    """Responses status → Chat finish_reason。

    官方 ResponseStatus 取值：in_progress / queued / completed / failed
    / incomplete / cancelled。其中 queued / in_progress 理论上不会出现在
    非流式完整响应里（它们是流式中间态）；cancelled 映射到 stop。
    """
    status = resp.get("status")
    incomplete = resp.get("incomplete_details") or {}
    if status == "completed":
        return "tool_calls" if has_tool_calls else "stop"
    if status == "incomplete":
        reason = incomplete.get("reason")
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
        return "stop"
    if status in ("failed", "cancelled"):
        return "stop"
    # in_progress / queued / 未知 → 保守用 stop（若有 tool_calls 则 tool_calls）
    return "tool_calls" if has_tool_calls else "stop"


def _usage_resps_to_chat(u: dict) -> dict:
    # 02-bug-findings #9: details fields must always be written.
    from .common import build_chat_usage
    in_details = u.get("input_tokens_details") or {}
    out_details = u.get("output_tokens_details") or {}
    input_tokens = int(u.get("input_tokens", 0) or 0)
    output_tokens = int(u.get("output_tokens", 0) or 0)
    cached = int(in_details.get("cached_tokens", 0) or 0)
    reasoning = int(out_details.get("reasoning_tokens", 0) or 0)
    total = int(u.get("total_tokens", input_tokens + output_tokens) or 0)
    return build_chat_usage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )
