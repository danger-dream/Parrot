"""协议审计 Patch 5 回归测试 — 错误规范。

覆盖 02-bug-findings.md：
  - #7 stream_r2c chat error 帧透传 code/type/param
  - #8 stream_c2r response.failed.error.code 映射到 ResponseErrorCode enum
  - #18 stream_c2r 处理 chat delta.function_call legacy
  - #10 responses_to_chat assistant message 全空时 skip 或占位
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


# ───────── #7 stream_r2c chat error 帧透传 ─────────


def test_bug7_chat_error_frame_carries_code_param_type(m):
    """上游 responses error 事件带 code/param/type 应透传到下游 chat error 帧。"""
    stream_r2c = m["stream_r2c"]
    tr = stream_r2c.StreamTranslator(model="x")
    payload = {"type": "rate_limit_error", "message": "too many",
                "code": "rate_limit_exceeded", "param": "messages"}
    chunks = []
    chunks.extend(tr.feed(b'event: error\ndata: ' + json.dumps(payload).encode() + b'\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    parsed = _parse_chat_sse(raw)
    error_frames = [d for kind, d in parsed if kind == "data" and "error" in d]
    assert error_frames, f"没有 error 帧: {parsed}"
    err = error_frames[0]["error"]
    assert err.get("message") == "too many"
    # 02-bug-findings #7: code/param/type 必须透传
    assert err.get("code") == "rate_limit_exceeded"
    assert err.get("param") == "messages"
    assert err.get("type") == "rate_limit_error"


# ───────── #8 stream_c2r response.failed.error.code 映射 enum ─────────


def test_bug8_response_failed_error_code_in_enum(m):
    """chat 上游 error.type=rate_limit_error 应被映射到 spec ResponseErrorCode enum。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(
        b'data: {"error":{"message":"slow down","type":"rate_limit_error"}}\n\n'
    ))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    failed = next(e for e in events if e[0] == "response.failed")
    err = failed[1]["response"]["error"]
    # spec: ResponseError.code in {server_error, rate_limit_exceeded, ...}
    assert err.get("code") == "rate_limit_exceeded", \
        f"rate_limit_error 应映射到 rate_limit_exceeded: {err}"
    assert err.get("message") == "slow down"


def test_bug8_unknown_error_type_falls_back_to_server_error(m):
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(
        b'data: {"error":{"message":"oops","type":"made_up_error_type"}}\n\n'
    ))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    failed = next(e for e in events if e[0] == "response.failed")
    err = failed[1]["response"]["error"]
    assert err.get("code") == "server_error", f"未知 error.type 应回落 server_error: {err}"


def test_bug8_upstream_supplies_valid_code_passthrough(m):
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(
        b'data: {"error":{"message":"invalid","code":"invalid_prompt"}}\n\n'
    ))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    failed = next(e for e in events if e[0] == "response.failed")
    err = failed[1]["response"]["error"]
    assert err.get("code") == "invalid_prompt"


# ───────── #18 stream_c2r legacy delta.function_call ─────────


def test_bug18_legacy_function_call_in_chat_stream(m):
    """chat 上游用旧 delta.function_call 字段而非 tool_calls。"""
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(
        b'data: {"choices":[{"delta":{"function_call":{"name":"get_w","arguments":""}}}]}\n\n'
    ))
    chunks.extend(tr.feed(
        b'data: {"choices":[{"delta":{"function_call":{"arguments":"{\\"a\\":1}"}},"finish_reason":"function_call"}]}\n\n'
    ))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    types = [e[0] for e in events]
    # 应该开 function_call item
    added = [e for e in events if e[0] == "response.output_item.added"]
    assert any(e[1]["item"].get("type") == "function_call" for e in added), \
        f"legacy function_call 应被翻译为 function_call item: {types}"
    # 应该 emit arguments 事件
    deltas = [e for e in events if e[0] == "response.function_call_arguments.delta"]
    assert deltas, f"应该 emit arguments delta: {types}"
    # 应正确累积 args
    args_buf = "".join(d[1]["delta"] for d in deltas)
    assert args_buf == '{"a":1}'


# ───────── #10 assistant 全空消息处理 ─────────


def test_bug10_empty_assistant_message_is_skipped(m):
    """responses input 中 assistant message 既无 text 也无 refusal 也无 tool_calls：
    应被 skip 或 content="" 占位（不应 content=None 单飞）。
    """
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [
        {"role": "user", "content": "hi"},
        {"type": "message", "role": "assistant", "content": []},  # 全空
        {"role": "user", "content": "again"},
    ]}
    out = responses_to_chat.translate_request(body)
    msgs = out["messages"]
    # assistant 完全空：应被 skip
    asst = [m for m in msgs if m.get("role") == "assistant"]
    if asst:
        # 如果保留，content 必须不为 None（否则上游 400）
        for a in asst:
            assert a.get("content") is not None or a.get("tool_calls"), \
                f"assistant 既无 content 又无 tool_calls 不应保留: {a}"


def test_bug10_assistant_with_only_refusal_keeps_msg(m):
    """assistant 只有 refusal 时仍应保留 message 并带 refusal 字段。"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [
        {"role": "user", "content": "do illegal"},
        {"type": "message", "role": "assistant",
          "content": [{"type": "refusal", "refusal": "I can't"}]},
    ]}
    out = responses_to_chat.translate_request(body)
    asst = [m for m in out["messages"] if m.get("role") == "assistant"]
    assert asst
    assert asst[0].get("refusal") == "I can't"
