"""协议审计 Patch 3 回归测试 — tool / tool_choice 完整支持。

覆盖 02-bug-findings.md：
  - #21 guard._BUILTIN_TOOL_TYPES 名单补全（web_search/computer/apply_patch/...）
  - #23 tool_choice = {type:custom, name} ↔ {type:custom, custom:{name}} 双向
  - #24 tool_choice = {type:allowed_tools, mode, tools} 双向
  - #25 guard 拦截 tool_choice = hosted/MCP/allowed_tools/custom 时的请求
  - #26 chat custom tool {type:custom, custom:{name,...}} 展开为 responses 端
       {type:custom, name, ...}
  - #27 assistant.tool_calls type=custom 翻译为 custom_tool_call item
  - #30 FunctionTool.strict 必填字段补默认值

加上 stream_c2r 的 custom_tool_call 流式状态机（Patch 3.4）。
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


# ───────── #21 built-in tool 白名单补全 ─────────


def test_bug21_web_search_v2_recognized(m):
    """web_search（v2）已在白名单：guard 应给"不支持"友好错误而非"未知 type"。"""
    g = m["guard"]
    body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "web_search"}]}
    with pytest.raises(g.GuardError) as ei:
        g.guard_responses_to_chat(body)
    # 错误消息应包含 "not supported" 等友好语，不应是 "unsupported"/"unknown"
    msg = ei.value.message.lower()
    assert "not supported" in msg or "built-in" in msg, f"友好错误信息：{ei.value.message}"


def test_bug21_computer_recognized(m):
    g = m["guard"]
    for ttype in ("computer", "computer_use", "apply_patch",
                   "function_shell", "web_search_2025_08_26",
                   "web_search_preview_2025_03_11"):
        body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
                "tools": [{"type": ttype}]}
        with pytest.raises(g.GuardError) as ei:
            g.guard_responses_to_chat(body)
        msg = ei.value.message.lower()
        assert "not supported" in msg or "built-in" in msg, \
            f"{ttype}: {ei.value.message}"


# ───────── #23 tool_choice custom 双向 ─────────


def test_bug23_tool_choice_custom_c2r(m):
    """chat 侧 {type:custom, custom:{name}} → responses 侧 {type:custom, name}"""
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "custom", "custom": {"name": "my_dsl"}}}
    out = chat_to_responses.translate_request(body)
    tc = out["tool_choice"]
    assert tc.get("type") == "custom"
    assert tc.get("name") == "my_dsl"
    assert "custom" not in tc, "responses 端不该有嵌套 custom 字段"


def test_bug23_tool_choice_custom_r2c(m):
    """responses 侧 {type:custom, name} → chat 侧 {type:custom, custom:{name}}"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "custom", "name": "my_dsl"}}
    out = responses_to_chat.translate_request(body)
    tc = out["tool_choice"]
    assert tc.get("type") == "custom"
    assert tc.get("custom", {}).get("name") == "my_dsl"


# ───────── #24 tool_choice allowed_tools 双向 ─────────


def test_bug24_tool_choice_allowed_tools_c2r(m):
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "allowed_tools",
                             "allowed_tools": {
                                 "mode": "auto",
                                 "tools": [{"type": "function", "function": {"name": "f1"}}]
                             }}}
    out = chat_to_responses.translate_request(body)
    tc = out["tool_choice"]
    assert tc.get("type") == "allowed_tools"
    # responses 端形态：{type:allowed_tools, mode, tools}
    assert tc.get("mode") == "auto"
    assert isinstance(tc.get("tools"), list)


def test_bug24_tool_choice_allowed_tools_r2c(m):
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "allowed_tools", "mode": "auto",
                             "tools": [{"type": "function", "name": "f1"}]}}
    out = responses_to_chat.translate_request(body)
    tc = out["tool_choice"]
    assert tc.get("type") == "allowed_tools"
    # chat 端形态：{type:allowed_tools, allowed_tools:{mode, tools}}
    nested = tc.get("allowed_tools") or {}
    assert nested.get("mode") == "auto"
    assert isinstance(nested.get("tools"), list)


# ───────── #25 guard 拦 tool_choice hosted/mcp/... ─────────


def test_bug25_guard_rejects_hosted_tool_choice(m):
    g = m["guard"]
    for tc in ({"type": "file_search"}, {"type": "web_search_preview"},
                {"type": "computer_use_preview"}, {"type": "code_interpreter"},
                {"type": "image_generation"}, {"type": "mcp", "server_label": "x"}):
        body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
                "tool_choice": tc}
        with pytest.raises(g.GuardError):
            g.guard_responses_to_chat(body)


# ───────── #26 chat custom tool 展开 ─────────


def test_bug26_chat_custom_tool_flattens_for_responses(m):
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "custom", "custom": {
                "name": "my_dsl",
                "description": "a custom tool",
                "format": {"type": "text"},
            }}]}
    out = chat_to_responses.translate_request(body)
    tool = out["tools"][0]
    # responses 端 CustomTool: {type:custom, name, description?, format?}
    assert tool.get("type") == "custom"
    assert tool.get("name") == "my_dsl"
    assert tool.get("description") == "a custom tool"
    assert tool.get("format") == {"type": "text"}
    assert "custom" not in tool, "responses 端不应再有嵌套 custom"


# ───────── #27 assistant.tool_calls type=custom ─────────


def test_bug27_assistant_custom_tool_calls_translates(m):
    chat_to_responses = m["chat_to_responses"]
    body = {
        "model": "x",
        "messages": [
            {"role": "user", "content": "do thing"},
            {"role": "assistant", "content": None,
              "tool_calls": [{
                  "id": "call_1", "type": "custom",
                  "custom": {"name": "my_dsl", "input": "raw input text"},
              }]},
        ],
    }
    out = chat_to_responses.translate_request(body)
    items = out["input"]
    custom_calls = [it for it in items if isinstance(it, dict)
                    and it.get("type") == "custom_tool_call"]
    assert len(custom_calls) == 1, f"expected 1 custom_tool_call item: {items}"
    cc = custom_calls[0]
    assert cc.get("call_id") == "call_1"
    assert cc.get("name") == "my_dsl"
    assert cc.get("input") == "raw input text"


def test_bug27_responses_custom_tool_call_back_to_chat(m):
    """responses 侧 custom_tool_call item → chat assistant tool_calls type=custom"""
    responses_to_chat = m["responses_to_chat"]
    body = {
        "model": "x",
        "input": [
            {"role": "user", "content": "do thing"},
            {"type": "custom_tool_call", "call_id": "call_1",
              "name": "my_dsl", "input": "raw input text"},
        ],
    }
    out = responses_to_chat.translate_request(body)
    msgs = out["messages"]
    # 应该出现 assistant 消息带 tool_calls type=custom
    asst = [m for m in msgs if m.get("role") == "assistant"]
    assert asst
    tcs = asst[0].get("tool_calls") or []
    assert tcs and tcs[0].get("type") == "custom"
    assert tcs[0].get("custom", {}).get("name") == "my_dsl"
    assert tcs[0].get("custom", {}).get("input") == "raw input text"


def test_bug27_responses_custom_tool_call_output_to_chat(m):
    """responses custom_tool_call_output → chat tool message"""
    responses_to_chat = m["responses_to_chat"]
    body = {
        "model": "x",
        "input": [
            {"role": "user", "content": "do thing"},
            {"type": "custom_tool_call", "call_id": "c1", "name": "f", "input": "x"},
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "result"},
        ],
    }
    out = responses_to_chat.translate_request(body)
    msgs = out["messages"]
    tools = [m for m in msgs if m.get("role") == "tool"]
    assert tools and tools[0].get("tool_call_id") == "c1"
    assert tools[0].get("content") == "result"


# ───────── #30 FunctionTool.strict 必填默认 ─────────


def test_bug30_function_tool_strict_default(m):
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {
                "name": "f", "parameters": {"type": "object"}}}]}
    out = chat_to_responses.translate_request(body)
    tool = out["tools"][0]
    # spec: FunctionTool required: type, name, strict, parameters
    assert "strict" in tool, f"FunctionTool 必须有 strict 字段：{tool}"
    assert tool["strict"] is False  # 默认 False
    assert "parameters" in tool


def test_bug30_function_tool_strict_explicit_passes_through(m):
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {
                "name": "f", "strict": True, "parameters": {"type": "object"}}}]}
    out = chat_to_responses.translate_request(body)
    assert out["tools"][0]["strict"] is True


# ───────── stream_c2r custom_tool_call 状态机 (Patch 3.4) ─────────


def test_custom_tool_call_streaming_emits_input_delta(m):
    """chat 上游 stream 中带 type=custom 的 tool_calls，stream_c2r 应:
    1. open 一个 custom_tool_call item（不是 function_call）
    2. emit response.custom_tool_call_input.delta 事件
    3. 收尾 emit response.custom_tool_call_input.done
    """
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"custom","custom":{"name":"my_dsl","input":""}}]}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"custom":{"input":"raw"}}]}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"custom":{"input":" txt"}}]},"finish_reason":"tool_calls"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    types = [e[0] for e in events]
    assert "response.custom_tool_call_input.delta" in types, \
        f"期望 custom_tool_call_input.delta 事件: {types}"
    assert "response.custom_tool_call_input.done" in types, \
        f"期望 custom_tool_call_input.done 事件: {types}"
    # output_item.added 的 item 应为 custom_tool_call
    added = [e for e in events if e[0] == "response.output_item.added"]
    assert any(e[1]["item"].get("type") == "custom_tool_call" for e in added), \
        f"output_item.added 应有 custom_tool_call: {[e[1]['item'].get('type') for e in added]}"
    # delta 累计
    deltas = [e[1]["delta"] for e in events
              if e[0] == "response.custom_tool_call_input.delta"]
    assert "".join(deltas) == "raw txt"
