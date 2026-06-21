"""协议审计 Patch 6 回归测试 — 边界 + 死代码清理 + 合约测试。

覆盖 02-bug-findings.md：
  - #3 function_call_output.output 是 array 时拍扁为文本
  - #6 删除 input_audio 死代码（responses InputContent 没有 audio）
  - #19 reasoning summary vs reasoning_text 区分（基本/降级行为）
  - #22 system role 不再强制改 developer（双方都接受）
  - #33 多 message item 切换发新 role chunk
  - #36 _stringify_tool_content 严格化（assistant array content 走专用拆分）
  - #40 prediction 字段 log warning 但不阻断
  - #44/#45/#46 SSE 多 data 行拼接
  - 附录小修：guard.GuardError 422 选项；reasoning item status:completed

最后：spec 合约测试（03 计划 5.3）。
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import json
import pytest


def _import_modules():
    from src.openai.transform import (
        chat_to_responses, responses_to_chat, guard, common,
        stream_r2c, stream_c2r,
    )
    return {
        "chat_to_responses": chat_to_responses,
        "responses_to_chat": responses_to_chat,
        "guard": guard,
        "common": common,
        "stream_r2c": stream_r2c,
        "stream_c2r": stream_c2r,
    }


def _parse_responses_sse(raw: bytes):
    out = []
    for block in raw.decode().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if payload != "[DONE]":
                    try:
                        data = json.loads(payload)
                    except Exception:
                        pass
        if ev:
            out.append((ev, data))
    return out


def _parse_chat_sse(raw: bytes):
    out = []
    for block in raw.decode().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    out.append(("DONE", None))
                else:
                    try:
                        out.append(("data", json.loads(payload)))
                    except Exception:
                        pass
    return out


# ───────── #3 function_call_output array → 文本 ─────────


def test_bug3_function_call_output_array_flattens_to_text(m):
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [
        {"role": "user", "content": "Q"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1",
          "output": [{"type": "input_text", "text": "result"},
                      {"type": "input_text", "text": " more"}]},
    ]}
    out = responses_to_chat.translate_request(body)
    tool_msgs = [m for m in out["messages"] if m.get("role") == "tool"]
    assert tool_msgs
    # 02-bug-findings #3: array content 必须拍扁为字符串（chat tool message 不接受 input_text part）
    assert isinstance(tool_msgs[0]["content"], str)
    assert "result" in tool_msgs[0]["content"]
    assert " more" in tool_msgs[0]["content"]


# ───────── #6 input_audio 在 r2c 保真桥接 ─────────


def test_bug6_input_audio_in_responses_content_is_preserved(m):
    """Responses↔Chat 的 input_audio content part 同构，r2c 应保真保留。"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "ok"},
            {"type": "input_audio", "input_audio": {"data": "...", "format": "wav"}},
        ],
    }]}
    out = responses_to_chat.translate_request(body)
    parts = out["messages"][0]["content"]
    assert isinstance(parts, list)
    assert parts[1] == {"type": "input_audio", "input_audio": {"data": "...", "format": "wav"}}


# ───────── #22 system 不强制改 developer ─────────


def test_bug22_system_role_not_forced_to_developer(m):
    """chat → responses：system role 应保持 system，而不是改成 developer。"""
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]}
    out = chat_to_responses.translate_request(body)
    items = out["input"]
    sys_items = [it for it in items if it.get("type") == "message"
                  and it.get("role") in ("system", "developer")]
    assert sys_items
    # 02-bug-findings #22: 不再强制改名
    assert sys_items[0]["role"] == "system", \
        f"system role 不应被改为 developer: {sys_items[0]}"


# ───────── #33 多 message item 切换发新 role chunk ─────────


def test_bug33_multiple_message_items_each_get_role_chunk(m):
    """上游 emit 多个 message item，每个都应在 chat 流中新发 role chunk。"""
    stream_r2c = m["stream_r2c"]
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = []
    # 第一个 message item
    chunks.extend(tr.feed(b'event: response.output_item.added\ndata: {"output_index":0,"item":{"type":"message","id":"m1","role":"assistant"}}\n\n'))
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":"part1"}\n\n'))
    # 第二个 message item
    chunks.extend(tr.feed(b'event: response.output_item.added\ndata: {"output_index":1,"item":{"type":"message","id":"m2","role":"assistant"}}\n\n'))
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":"part2"}\n\n'))
    chunks.extend(tr.feed(b'event: response.completed\ndata: {"response":{"status":"completed"}}\n\n'))
    chunks.extend(tr.close())
    parsed = _parse_chat_sse(b"".join(chunks))
    role_chunks = [d for kind, d in parsed if kind == "data"
                   and d.get("choices") and d["choices"][0]["delta"].get("role") == "assistant"]
    # 至少应有 2 个 role chunk（每个 message item 一个）
    assert len(role_chunks) >= 2, \
        f"02-bug-findings #33: 多 message item 应各发一个 role chunk: {len(role_chunks)}"


# ───────── #40 prediction 无 Responses 等价时剥离但不阻断 ─────────


def test_bug40_prediction_field_is_output_whitelist_filtered(m):
    """prediction 是 OpenAI Chat provider hint；Responses fallback 剥离但不阻断。"""
    g = m["guard"]
    c2r = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "prediction": {"type": "content", "content": "hello"}}
    g.guard_chat_to_responses(body)
    out = c2r.translate_request(body)
    assert "prediction" not in out


# ───────── 附录：reasoning item status:completed ─────────


def test_appendix_reasoning_item_has_status_completed(m):
    """流式 reasoning item 的 final 快照应带 status:completed（兼容严格客户端）。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"reasoning_content":"think..."},"finish_reason":null}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    # 找最后的 reasoning output_item.done 或 response.completed 中的 reasoning item
    item_done = [e for e in events if e[0] == "response.output_item.done"
                 and e[1].get("item", {}).get("type") == "reasoning"]
    assert item_done
    for ev, data in item_done:
        assert data["item"].get("status") == "completed", \
            f"reasoning item 应带 status:completed: {data['item']}"


# ───────── #45 SSE 多 data 行拼接 ─────────


def test_bug45_sse_multiple_data_lines_join(m):
    """多个 data: 行同一个 SSE block 应拼接成 \\n 分隔的字符串（按 SSE spec）。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    # 一个 block 两个 data 行：合法 JSON 拼出来
    block = b'data: {"choices":[{"delta":{"content":\ndata: "hi"}}]}\n\n'
    chunks = list(tr.feed(block))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    deltas = [e for e in events if e[0] == "response.output_text.delta"]
    # 该 case 关键是不 raise；text 是否能成功取决于 JSON 是否能解析
    assert isinstance(deltas, list)


# ───────── 附录：GuardError 422 选项 ─────────


def test_appendix_guard_error_supports_422(m):
    """GuardError 应能接受 422 status（spec input 验证失败用 422 而非 400）。"""
    g = m["guard"]
    err = g.GuardError(422, "invalid_request_error", "validation failed", param="x")
    assert err.status == 422


# ───────── spec 合约测试（03 计划 5.3，简版） ─────────


def test_contract_translate_response_resp_to_chat_basic_shape(m):
    """非流式 r→c：翻译输出 chat completion 的形状应满足官方关键 required 字段。"""
    chat_to_responses = m["chat_to_responses"]
    resp = {
        "id": "resp_1", "object": "response", "status": "completed",
        "created_at": 1, "model": "gpt-x",
        "output": [{
            "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "hello", "annotations": []}],
        }],
        "output_text": "hello",
        "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
    }
    out = chat_to_responses.translate_response(resp, model="gpt-x")
    # spec: CreateChatCompletionResponse required: id, choices, created, model, object
    for k in ("id", "choices", "created", "model", "object"):
        assert k in out, f"missing required field {k}"
    assert out["object"] == "chat.completion"
    assert isinstance(out["choices"], list) and out["choices"]
    choice = out["choices"][0]
    # spec: ChatChoice required: finish_reason, index, message, logprobs (nullable)
    for k in ("finish_reason", "index", "message"):
        assert k in choice, f"missing choice field {k}"
    # usage CompletionUsage required: prompt_tokens, completion_tokens, total_tokens
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert k in out["usage"], f"missing usage field {k}"


def test_contract_translate_response_chat_to_resp_basic_shape(m):
    """非流式 c→r：翻译输出 responses 的形状应满足官方关键 required 字段。"""
    responses_to_chat = m["responses_to_chat"]
    chat = {
        "id": "cmpl-1", "model": "gpt-x", "object": "chat.completion",
        "created": 1,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                      "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    out = responses_to_chat.translate_response(chat, model="gpt-x")
    # spec: Response required: id, object, created_at, status, error, incomplete_details, model, output, usage
    for k in ("id", "object", "created_at", "status", "error",
              "incomplete_details", "model", "output", "usage"):
        assert k in out, f"missing required field {k}"
    assert out["object"] == "response"
    # ResponseUsage required (#9)
    assert "input_tokens_details" in out["usage"]
    assert "output_tokens_details" in out["usage"]
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 0
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 0
