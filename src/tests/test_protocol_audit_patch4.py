"""协议审计 Patch 4 回归测试 — 字段补齐。

覆盖 02-bug-findings.md：
  - #4 image_url.file_id 双向透传
  - #5 input_file.file_url + detail 双向透传
  - #11 verbosity ↔ text.verbosity 双向
  - #12 reasoning.summary 双向
  - #28 annotations 双向
  - #29 stream_c2r output_text.delta/done 始终带 logprobs:[]
  - #35 stream_r2c 处理 response.output_text.annotation.added
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


# ───────── #4 image_url.file_id 双向 ─────────


def test_bug4_image_file_id_responses_to_chat(m):
    """responses InputImageContent.file_id 应回写 chat image_url.file_id"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{
        "role": "user",
        "content": [{"type": "input_image", "file_id": "file_xyz",
                      "detail": "high"}],
    }]}
    out = responses_to_chat.translate_request(body)
    msg = out["messages"][0]
    parts = msg["content"]
    assert isinstance(parts, list)
    img = parts[0]
    assert img["type"] == "image_url"
    iu = img["image_url"]
    assert iu.get("file_id") == "file_xyz"
    assert iu.get("detail") == "high"


# ───────── #5 input_file.file_url + detail 双向 ─────────


def test_bug5_input_file_file_url_chat_to_responses(m):
    """chat file part 的 file_url + detail 应透传到 responses input_file"""
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{
        "role": "user",
        "content": [{"type": "file", "file": {"file_url": "https://x/y.pdf",
                                                "filename": "y.pdf"}}],
    }]}
    out = chat_to_responses.translate_request(body)
    parts = out["input"][0]["content"]
    fp = parts[0]
    assert fp["type"] == "input_file"
    assert fp.get("file_url") == "https://x/y.pdf"
    assert fp.get("filename") == "y.pdf"


def test_bug5_input_file_file_url_responses_to_chat(m):
    """responses input_file.file_url 回写 chat file.file_url"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{
        "role": "user",
        "content": [{"type": "input_file", "file_url": "https://x/y.pdf",
                      "filename": "y.pdf"}],
    }]}
    out = responses_to_chat.translate_request(body)
    msg = out["messages"][0]
    parts = msg["content"]
    fp = parts[0]
    assert fp["type"] == "file"
    assert fp["file"].get("file_url") == "https://x/y.pdf"
    assert fp["file"].get("filename") == "y.pdf"


# ───────── #11 verbosity 双向 ─────────


def test_bug11_verbosity_chat_to_responses(m):
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "verbosity": "low"}
    out = chat_to_responses.translate_request(body)
    assert out.get("text", {}).get("verbosity") == "low"


def test_bug11_verbosity_responses_to_chat(m):
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
            "text": {"verbosity": "high", "format": {"type": "text"}}}
    out = responses_to_chat.translate_request(body)
    assert out.get("verbosity") == "high"


# ───────── #12 reasoning.summary 双向 ─────────


def test_bug12_reasoning_summary_chat_to_responses(m):
    """非官方 chat 字段 reasoning_summary 透传到 responses reasoning.summary。"""
    # spec 中 chat 没有 reasoning_summary 顶层字段，这是 DeepSeek/部分代理生态用的
    # 桥接字段；本 proxy 走 reasoning_summary -> reasoning.summary 的非官方映射
    # （02-bug-findings #12 标 P2，仅在 reasoning_summary 出现时映射，没有就不动）
    chat_to_responses = m["chat_to_responses"]
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high", "reasoning_summary": "auto"}
    out = chat_to_responses.translate_request(body)
    assert out["reasoning"].get("summary") == "auto"
    assert out["reasoning"].get("effort") == "high"


def test_bug12_reasoning_summary_responses_to_chat(m):
    """responses reasoning.summary 回写 chat 端非官方字段 reasoning_summary。"""
    responses_to_chat = m["responses_to_chat"]
    body = {"model": "x", "input": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "high", "summary": "auto"}}
    out = responses_to_chat.translate_request(body)
    assert out.get("reasoning_summary") == "auto"
    assert out.get("reasoning_effort") == "high"


# ───────── #28 annotations 双向 ─────────


def test_bug28_annotations_responses_to_chat_response(m):
    """responses output_text.annotations[] → chat assistant.annotations[]"""
    responses_to_chat = m["responses_to_chat"]
    chat_resp = {
        "id": "cmpl-1", "model": "x",
        "choices": [{"message": {
            "role": "assistant", "content": "hi",
            "annotations": [{"type": "url_citation", "url": "https://example.com",
                              "title": "example", "start_index": 0, "end_index": 2}],
        }, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    out = responses_to_chat.translate_response(chat_resp, model="x")
    output = out["output"]
    msg = next(it for it in output if it.get("type") == "message")
    text_part = next(p for p in msg["content"] if p.get("type") == "output_text")
    annotations = text_part.get("annotations") or []
    assert annotations, f"annotations 应被回填到 output_text.annotations: {text_part}"
    assert annotations[0].get("type") == "url_citation"


def test_bug28_annotations_chat_to_responses_response(m):
    """responses 端 output_text.annotations[] → chat 端 assistant.annotations[]"""
    chat_to_responses = m["chat_to_responses"]
    resp = {
        "id": "resp_1", "object": "response", "status": "completed",
        "created_at": 1, "model": "x",
        "output": [{
            "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "hi",
                          "annotations": [{"type": "url_citation",
                                            "url": "https://example.com"}]}],
        }],
        "output_text": "hi",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    out = chat_to_responses.translate_response(resp, model="x")
    msg = out["choices"][0]["message"]
    annotations = msg.get("annotations") or []
    assert annotations, f"chat 端 message.annotations 应被回填: {msg}"
    assert annotations[0].get("type") == "url_citation"


# ───────── #29 logprobs:[] 必填 ─────────


def test_bug29_output_text_delta_has_logprobs(m):
    stream_c2r = m["stream_c2r"]
    tr = stream_c2r.StreamTranslator(model="x")
    chunks = list(tr.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))
    chunks.extend(tr.feed(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'))
    chunks.extend(tr.close())
    raw = b"".join(chunks)
    events = _parse_responses_sse(raw)
    deltas = [e for e in events if e[0] == "response.output_text.delta"]
    assert deltas
    for ev, data in deltas:
        # spec: ResponseTextDeltaEvent.logprobs required
        assert "logprobs" in data, f"output_text.delta 必须带 logprobs: {data}"
        assert data["logprobs"] == []
    dones = [e for e in events if e[0] == "response.output_text.done"]
    assert dones
    for ev, data in dones:
        assert "logprobs" in data
        assert data["logprobs"] == []


# ───────── #35 stream_r2c 处理 annotation.added ─────────


def test_bug35_stream_r2c_handles_annotation_added(m):
    """response.output_text.annotation.added 不应被静默丢弃；
    应在最后的 finish_reason chunk 中通过 message.annotations 透出（如 spec 允许的话），
    至少不能 raise 也不能丢弃流。最低要求：保留 annotation 到 close 时的 chat assistant 快照。
    """
    stream_r2c = m["stream_r2c"]
    tr = stream_r2c.StreamTranslator(model="x")
    chunks = []
    chunks.extend(tr.feed(b'event: response.output_text.delta\ndata: {"delta":"hi"}\n\n'))
    annotation = {"type": "url_citation", "url": "https://example.com"}
    chunks.extend(tr.feed(
        b'event: response.output_text.annotation.added\ndata: {"annotation":'
        + json.dumps(annotation).encode() + b'}\n\n'
    ))
    chunks.extend(tr.feed(b'event: response.completed\ndata: {"response":{"status":"completed"}}\n\n'))
    chunks.extend(tr.close())
    # 最低断言：translator 不报错；累积的 chat assistant 快照里能拿到 annotations
    snap = tr.get_downstream_chat_assistant()
    assert "annotations" in snap, f"chat assistant 快照应保留 annotations: {snap}"
    assert snap["annotations"][0]["type"] == "url_citation"
