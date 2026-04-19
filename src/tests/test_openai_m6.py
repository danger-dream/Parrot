"""MS-6 Reasoning Bridge 配置开关测试。

覆盖：两种模式（passthrough / drop）× 两个方向（c2r / r2c）× 两种路径
（非流式 / 流式）共 8 组合。drop 模式下 reasoning 文本被丢弃，
但 usage.reasoning_tokens 在 drop 模式下仍被透传（计费信息不失）。

运行：./venv/bin/python -m src.tests.test_openai_m6
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, state_db, log_db
    from src.openai.transform import (
        chat_to_responses, responses_to_chat, stream_r2c, stream_c2r, common,
    )
    return {
        "config": config, "state_db": state_db, "log_db": log_db,
        "chat_to_responses": chat_to_responses,
        "responses_to_chat": responses_to_chat,
        "stream_r2c": stream_r2c, "stream_c2r": stream_c2r,
        "common": common,
    }


def _set_bridge(m, mode: str) -> None:
    def _mut(c):
        c.setdefault("openai", {})["reasoningBridge"] = mode
    m["config"].update(_mut)


def _run(tr, chunks):
    out = []
    for c in chunks:
        out.extend(tr.feed(c))
    out.extend(tr.close())
    return out


def _parse_chat_frames(frames):
    text = b"".join(frames).decode("utf-8", errors="replace")
    objs = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                objs.append(json.loads(data))
            except Exception:
                pass
    return objs


def _parse_responses_events(frames):
    text = b"".join(frames).decode("utf-8", errors="replace")
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = ""
        data_str = ""
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            out.append((ev, json.loads(data_str)))
        except Exception:
            pass
    return out


# ─── 非流式 ──────────────────────────────────────────────────────


def test_c2r_nonstream_passthrough(m):
    _set_bridge(m, "passthrough")
    resp = {
        "id": "resp_1", "status": "completed", "created_at": 1, "model": "x",
        "output": [
            {"type": "reasoning", "id": "rs_1",
             "summary": [{"type": "summary_text", "text": "because math"}]},
            {"type": "message", "id": "m", "role": "assistant",
             "content": [{"type": "output_text", "text": "done", "annotations": []}]},
        ],
        "output_text": "done",
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                  "output_tokens_details": {"reasoning_tokens": 2}},
    }
    out = m["chat_to_responses"].translate_response(resp, model="x")
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "because math"
    assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    print("  [PASS] c2r 非流式 passthrough: reasoning_content + reasoning_tokens")


def test_c2r_nonstream_drop(m):
    _set_bridge(m, "drop")
    try:
        resp = {
            "id": "resp_1", "status": "completed", "created_at": 1, "model": "x",
            "output": [
                {"type": "reasoning", "id": "rs_1",
                 "summary": [{"type": "summary_text", "text": "because math"}]},
                {"type": "message", "id": "m", "role": "assistant",
                 "content": [{"type": "output_text", "text": "done", "annotations": []}]},
            ],
            "output_text": "done",
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                      "output_tokens_details": {"reasoning_tokens": 2}},
        }
        out = m["chat_to_responses"].translate_response(resp, model="x")
        msg = out["choices"][0]["message"]
        assert "reasoning_content" not in msg, f"drop 模式应不含 reasoning_content: {msg}"
        # reasoning_tokens 仍透传
        assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] c2r 非流式 drop: 丢弃 reasoning_content，保留 reasoning_tokens")


def test_r2c_nonstream_passthrough(m):
    _set_bridge(m, "passthrough")
    chat = {
        "id": "c", "object": "chat.completion", "created": 1, "model": "x",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": "done",
                                 "reasoning_content": "thinking"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                  "completion_tokens_details": {"reasoning_tokens": 1}},
    }
    out = m["responses_to_chat"].translate_response(chat, model="x")
    types = [it["type"] for it in out["output"]]
    assert "reasoning" in types
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 1
    print("  [PASS] r2c 非流式 passthrough: reasoning item + reasoning_tokens")


def test_r2c_nonstream_drop(m):
    _set_bridge(m, "drop")
    try:
        chat = {
            "id": "c", "object": "chat.completion", "created": 1, "model": "x",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "done",
                                     "reasoning_content": "thinking"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                      "completion_tokens_details": {"reasoning_tokens": 1}},
        }
        out = m["responses_to_chat"].translate_response(chat, model="x")
        types = [it["type"] for it in out["output"]]
        assert "reasoning" not in types, f"drop 模式不应有 reasoning item：{types}"
        assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 1
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] r2c 非流式 drop: 无 reasoning item，保留 reasoning_tokens")


# ─── 流式 ────────────────────────────────────────────────────────


def test_r2c_stream_passthrough(m):
    _set_bridge(m, "passthrough")
    tr = m["stream_r2c"].StreamTranslator(model="x", include_usage=False)
    events = [
        b'event: response.reasoning_summary_text.delta\ndata: {"delta":"thinking"}\n\n',
        b'event: response.output_text.delta\ndata: {"delta":"ok"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r","status":"completed","output":[]}}\n\n',
    ]
    frames = _run(tr, events)
    objs = _parse_chat_frames(frames)
    has_rc = any(o.get("choices") and o["choices"][0]["delta"].get("reasoning_content")
                 for o in objs)
    assert has_rc, "passthrough 模式流式应包含 delta.reasoning_content"
    print("  [PASS] r2c 流式 passthrough: 含 delta.reasoning_content")


def test_r2c_stream_drop(m):
    _set_bridge(m, "drop")
    try:
        tr = m["stream_r2c"].StreamTranslator(model="x", include_usage=False)
        events = [
            b'event: response.reasoning_summary_text.delta\ndata: {"delta":"thinking"}\n\n',
            b'event: response.output_text.delta\ndata: {"delta":"ok"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r","status":"completed","output":[]}}\n\n',
        ]
        frames = _run(tr, events)
        objs = _parse_chat_frames(frames)
        has_rc = any(o.get("choices") and o["choices"][0]["delta"].get("reasoning_content")
                     for o in objs)
        assert not has_rc, "drop 模式流式不应包含 delta.reasoning_content"
        # 但 content 仍然有
        has_content = any(o.get("choices") and o["choices"][0]["delta"].get("content")
                           for o in objs)
        assert has_content
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] r2c 流式 drop: 无 delta.reasoning_content，content 正常")


def test_c2r_stream_passthrough(m):
    _set_bridge(m, "passthrough")
    tr = m["stream_c2r"].StreamTranslator(model="x")
    chunks = [
        b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning_content":"thinking"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run(tr, chunks)
    events = _parse_responses_events(frames)
    names = [n for n, _ in events]
    assert "response.reasoning_summary_text.delta" in names
    print("  [PASS] c2r 流式 passthrough: 生成 reasoning_summary_text.delta")


def test_c2r_stream_drop(m):
    _set_bridge(m, "drop")
    try:
        tr = m["stream_c2r"].StreamTranslator(model="x")
        chunks = [
            b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning_content":"thinking"},"finish_reason":null}]}\n\n',
            b'data: {"id":"c","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        frames = _run(tr, chunks)
        events = _parse_responses_events(frames)
        names = [n for n, _ in events]
        assert "response.reasoning_summary_text.delta" not in names, (
            f"drop 模式不应 emit reasoning 事件: {names}"
        )
        # 应该没有 reasoning item（因为 reasoning_content 被丢弃）
        completed = [p for n, p in events if n == "response.completed"][0]
        types = [it["type"] for it in completed["response"]["output"]]
        assert "reasoning" not in types, f"drop 模式 completed.output 不应含 reasoning: {types}"
        assert "message" in types
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] c2r 流式 drop: 无 reasoning events，无 reasoning item")


# ─── 非法值回落 ──────────────────────────────────────────────────


def test_invalid_mode_falls_back(m):
    _set_bridge(m, "nonsense-mode-xxx")
    try:
        assert m["common"].reasoning_bridge_mode() == "passthrough"
        assert m["common"].reasoning_passthrough_enabled() is True
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] 非法 mode 值回落为 passthrough")


# ─── 驱动 ────────────────────────────────────────────────────────


def main() -> int:
    m = _import_modules()
    m["state_db"].init()
    m["log_db"].init()
    orig_cfg = m["config"].get().copy()

    tests = [
        test_c2r_nonstream_passthrough,
        test_c2r_nonstream_drop,
        test_r2c_nonstream_passthrough,
        test_r2c_nonstream_drop,
        test_r2c_stream_passthrough,
        test_r2c_stream_drop,
        test_c2r_stream_passthrough,
        test_c2r_stream_drop,
        test_invalid_mode_falls_back,
    ]
    passed = 0
    try:
        print("── MS-6 Reasoning Bridge Config ────────────")
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig_cfg)
        m["config"].update(_restore)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
