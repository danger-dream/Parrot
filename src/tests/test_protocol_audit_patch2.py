"""协议审计 Patch 2 回归测试 — 流式状态机一致性。

覆盖 02-bug-findings.md：
  - #13 stream_c2r._response_skeleton 包含 spec required 全部字段
       (tools/metadata/tool_choice/temperature/top_p/parallel_tool_calls/
        reasoning/text/truncation 等)
  - #16 stream_c2r._MessageItem content_index 用累计计数
       (refusal 和 text 的 index 不撞)
  - #17 stream_c2r._close_function_call 的 done 事件带 name 字段
  - #41 stream_c2r.close() 在 terminal_error 路径也写 store
  - #43 stream_r2c._mk_chunk include_usage=true 时所有 chunk 都带 usage 字段
       (中间 chunk usage:null，末帧才有真值)
"""

from __future__ import annotations

# 测试隔离
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
    """解析 responses SSE -> [(event, data_dict), ...]"""
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


# ───────── #13 Response skeleton 完整 ─────────


def test_bug13_response_created_skeleton_required_fields(m):
    stream_c2r = m["stream_c2r"]
    request_body = {
        "model": "gpt-4",
        "tools": [{"type": "function", "name": "get_weather"}],
        "tool_choice": "auto",
        "temperature": 0.5,
        "top_p": 0.9,
        "parallel_tool_calls": False,
        "metadata": {"k": "v"},
        "reasoning": {"effort": "high"},
        "text": {"format": {"type": "text"}},
        "truncation": "auto",
        "instructions": "be helpful",
    }
    tr = stream_c2r.StreamTranslator(model="gpt-4", request_body=request_body)
    chunks = list(tr.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    created = next(e for e in events if e[0] == "response.created")
    resp = created[1]["response"]
    # spec: Response required fields
    for f in ("id", "object", "created_at", "status", "error", "incomplete_details",
              "model", "tools", "output", "parallel_tool_calls", "metadata",
              "tool_choice", "temperature", "top_p", "reasoning", "text",
              "truncation"):
        assert f in resp, f"required field {f} missing in skeleton"
    # 透传字段值
    assert resp["tools"] == request_body["tools"]
    assert resp["tool_choice"] == "auto"
    assert resp["temperature"] == 0.5
    assert resp["top_p"] == 0.9
    assert resp["parallel_tool_calls"] is False
    assert resp["metadata"] == {"k": "v"}
    assert resp["reasoning"] == {"effort": "high"}
    assert resp["text"] == {"format": {"type": "text"}}
    assert resp["truncation"] == "auto"
    assert resp["instructions"] == "be helpful"


def test_bug13_response_skeleton_defaults_when_no_request_body(m):
    """向后兼容：不传 request_body 时使用 sensible defaults。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="gpt-4")  # 不传 request_body
    chunks = list(tr.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    created = next(e for e in events if e[0] == "response.created")
    resp = created[1]["response"]
    assert resp["tools"] == []
    assert resp["parallel_tool_calls"] is True
    assert resp["metadata"] == {}
    assert resp["tool_choice"] == "auto"
    assert resp["temperature"] == 1
    assert resp["top_p"] == 1
    assert resp["truncation"] == "disabled"
    assert resp["text"] == {"format": {"type": "text"}}
    assert resp["reasoning"] == {"effort": None, "summary": None}


# ───────── #16 content_index 累计 ─────────


def test_bug16_refusal_then_text_content_index_distinct(m):
    """先 refusal 后 text：refusal 用 0，text 用 1（不能撞车）。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"refusal":"NO"}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    refusal_indices = [e[1]["content_index"] for e in events
                       if e[0] == "response.refusal.delta"]
    text_indices = [e[1]["content_index"] for e in events
                    if e[0] == "response.output_text.delta"]
    assert refusal_indices, "no refusal.delta emitted"
    assert text_indices, "no output_text.delta emitted"
    # 关键断言：两类 index 不冲突
    assert set(refusal_indices).isdisjoint(set(text_indices)), \
        f"refusal {refusal_indices} 与 text {text_indices} content_index 撞车"


def test_bug16_text_then_refusal_content_index_distinct(m):
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"refusal":"NO"},"finish_reason":"stop"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    refusal_indices = [e[1]["content_index"] for e in events
                       if e[0] == "response.refusal.delta"]
    text_indices = [e[1]["content_index"] for e in events
                    if e[0] == "response.output_text.delta"]
    assert refusal_indices and text_indices
    assert set(refusal_indices).isdisjoint(set(text_indices))


# ───────── #17 function_call_arguments.done 带 name ─────────


def test_bug17_function_call_arguments_done_has_name(m):
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"sg\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    done_events = [e for e in events if e[0] == "response.function_call_arguments.done"]
    assert done_events, "no function_call_arguments.done"
    for ev, data in done_events:
        # spec: ResponseFunctionCallArgumentsDoneEvent required: name
        assert "name" in data, f"function_call_arguments.done missing name: {data}"
        assert data["name"] == "get_weather"


# ───────── #41 close 错误路径写 store ─────────


def test_bug41_terminal_error_still_saves_store(m, monkeypatch):
    stream_c2r = m["stream_c2r"]
    saved = []

    # 直接 monkeypatch src.openai.store 的两个函数：因为 stream_c2r 用
    # `from .. import store as _store` 拿模块对象，sys.modules.setitem 在
    # 父包 attribute 已绑定时不生效。
    from src.openai import store as _store
    monkeypatch.setattr(_store, "is_enabled", lambda: True)
    monkeypatch.setattr(_store, "save", lambda **kwargs: saved.append(kwargs))

    tr = stream_c2r.StreamTranslator(
        model="x",
        api_key_name="apikey1",
        channel_key="ch1",
        current_input_items=[{"type": "message", "role": "user", "content": "hi"}],
    )
    # 部分输出后上游报 error
    list(tr.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'))
    list(tr.feed(b'data: {"error":{"message":"upstream blew up","type":"server_error"}}\n\n'))
    list(tr.close())
    # 即使 terminal_error 路径，也应该把已有内容写入 store（带 status:failed 标记）
    assert len(saved) == 1, f"store should be saved exactly once on error path, got {len(saved)}"
    payload = saved[0]
    assert payload["api_key_name"] == "apikey1"
    assert payload["response_id"] == tr.state.resp_id
    # output_items 应至少有部分内容
    assert len(payload["output_items"]) >= 1


# ───────── #43 include_usage 中间 chunk 带 usage:null ─────────


def test_bug43_include_usage_intermediate_chunks_have_usage_null(m):
    stream_r2c = m["stream_r2c"]
    tr = stream_r2c.StreamTranslator(model="x", include_usage=True)
    chunks = []
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\n'))
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":" world"}\n\n'))
    chunks.extend(tr.feed(b'event: response.completed\ndata: {"response":{"status":"completed","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'))
    chunks.extend(tr.close())
    parsed = _parse_chat_sse(b"".join(chunks))
    data_chunks = [d for kind, d in parsed if kind == "data"]
    assert data_chunks, "no chat chunks"
    # 中间 chunk 应都带 usage 字段（值为 null）
    intermediate = data_chunks[:-1]  # 最后一帧带真 usage
    last = data_chunks[-1]
    for c in intermediate:
        assert "usage" in c, f"include_usage=true 时 intermediate chunk 应该带 usage 字段（即使为 null）：{c}"
        assert c["usage"] is None, f"intermediate chunk usage 应为 null：{c['usage']}"
    # 最后一帧 usage 真值
    assert last.get("usage") is not None, f"last chunk usage 应为真值：{last}"
    assert last["usage"]["prompt_tokens"] == 3


def test_bug43_include_usage_false_no_usage_field(m):
    """include_usage=false 时 chunk 不带 usage 字段（保持原行为）。"""
    stream_r2c = m["stream_r2c"]
    tr = stream_r2c.StreamTranslator(model="x", include_usage=False)
    chunks = []
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\n'))
    chunks.extend(tr.feed(b'event: response.completed\ndata: {"response":{"status":"completed"}}\n\n'))
    chunks.extend(tr.close())
    parsed = _parse_chat_sse(b"".join(chunks))
    data_chunks = [d for kind, d in parsed if kind == "data"]
    for c in data_chunks:
        assert "usage" not in c, f"include_usage=false 时不应带 usage：{c}"
