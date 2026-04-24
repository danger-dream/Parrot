"""OpenAI 家族 usage 解析的缓存语义对齐测试（v0.8.0 修复）。

背景：
    OpenAI 上游（Chat / Responses）返回的 `prompt_tokens` / `input_tokens`
    是**含缓存命中**的总 prompt 大小，`cached_tokens` 是其中被缓存命中的那
    部分。而 Anthropic 的 `input_tokens` 指**未命中缓存的新 token**，两套
    语义相反。

    Parrot 的 log_db 以 Anthropic 4 键（input/output/cache_creation/cache_read）
    作为统一存储语义，因此 OpenAI 归一路径需要做一次扣减：
        input_tokens = max(0, prompt_tokens - cached_tokens)

    这样展示层的公式 `↑ = input + cache_creation + cache_read` 才能对两种
    协议都得出正确的总 prompt 大小。

覆盖：
    1. OpenAI Chat 非流式 (`extract_usage_chat_json`)
    2. OpenAI Chat SSE  (`ChatSSEUsageTracker`)
    3. OpenAI Responses 非流式 (`extract_usage_responses_json`)
    4. OpenAI Responses SSE (`ResponsesSSEUsageTracker`)
    5. Anthropic 路径回归（不受修复影响，语义保持原样）

运行：
    ./venv/bin/python -m pytest src/tests/test_openai_cache_usage_semantics.py -v
"""

from __future__ import annotations

import json

from src.upstream import (
    ChatSSEUsageTracker,
    ResponsesSSEUsageTracker,
    extract_usage_chat_json,
    extract_usage_responses_json,
    extract_usage_from_json as extract_usage_anthropic_json,
)


# ══════════════════════════════════════════════════════════════════════
# 1. OpenAI Chat 非流式
# ══════════════════════════════════════════════════════════════════════


def test_chat_json_subtracts_cached_from_prompt():
    """Chat 非流式 usage：prompt_tokens 含 cached_tokens，归一后应扣除。"""
    resp = {
        "usage": {
            "prompt_tokens": 48427,
            "completion_tokens": 1852,
            "total_tokens": 50279,
            "prompt_tokens_details": {"cached_tokens": 47232},
        }
    }
    out = extract_usage_chat_json(resp)
    # prompt_tokens - cached = 48427 - 47232 = 1195（真正算钱的新 token）
    assert out["input_tokens"] == 1195, out
    assert out["cache_read"] == 47232, out
    assert out["output_tokens"] == 1852, out
    assert out["cache_creation"] == 0, out
    # 统一语义下：input + cache_read 应当等于原始 prompt_tokens
    assert out["input_tokens"] + out["cache_read"] == 48427


def test_chat_json_no_cache_hit_unchanged():
    """无缓存命中时，input_tokens 直接等于 prompt_tokens。"""
    resp = {
        "usage": {
            "prompt_tokens": 26047,
            "completion_tokens": 243,
            "total_tokens": 26290,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
    }
    out = extract_usage_chat_json(resp)
    assert out["input_tokens"] == 26047
    assert out["cache_read"] == 0


def test_chat_json_missing_usage_returns_zero():
    """usage 缺失时返回 4 键 0 值，不抛异常。"""
    out = extract_usage_chat_json({})
    assert out == {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}


def test_chat_json_cached_greater_than_prompt_clamped():
    """异常情况：cached > prompt，结果被 clamp 到 0，不出现负数。"""
    resp = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 500},  # 畸形数据
        }
    }
    out = extract_usage_chat_json(resp)
    assert out["input_tokens"] == 0, out  # max(0, 100-500)
    assert out["cache_read"] == 500


# ══════════════════════════════════════════════════════════════════════
# 2. OpenAI Chat SSE
# ══════════════════════════════════════════════════════════════════════


def _make_chat_sse_usage_frame(prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> bytes:
    """构造带 usage 的 Chat SSE 尾帧。"""
    evt = {
        "id": "chatcmpl-xxx",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }
    return (f"data: {json.dumps(evt)}\n\n".encode("utf-8") + b"data: [DONE]\n\n")


def test_chat_sse_tracker_subtracts_cached():
    """Chat SSE tracker：usage 帧里 prompt_tokens 扣除 cached_tokens 后写入 input。"""
    tracker = ChatSSEUsageTracker()
    tracker.feed(_make_chat_sse_usage_frame(prompt_tokens=48427, cached_tokens=47232, completion_tokens=1852))
    assert tracker.usage["input_tokens"] == 1195
    assert tracker.usage["cache_read"] == 47232
    assert tracker.usage["output_tokens"] == 1852
    assert tracker.usage["cache_creation"] == 0
    assert tracker.saw_stream_end is True


def test_chat_sse_tracker_zero_cache():
    """SSE 无缓存命中，input_tokens 等于 prompt_tokens。"""
    tracker = ChatSSEUsageTracker()
    tracker.feed(_make_chat_sse_usage_frame(prompt_tokens=1000, cached_tokens=0, completion_tokens=100))
    assert tracker.usage["input_tokens"] == 1000
    assert tracker.usage["cache_read"] == 0


# ══════════════════════════════════════════════════════════════════════
# 3. OpenAI Responses 非流式
# ══════════════════════════════════════════════════════════════════════


def test_responses_json_subtracts_cached_from_input():
    """Responses 非流式 usage：input_tokens 是含 cache 的总数，应扣除 cached。"""
    resp = {
        "usage": {
            "input_tokens": 50000,
            "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 45000},
        }
    }
    out = extract_usage_responses_json(resp)
    assert out["input_tokens"] == 5000, out  # 50000 - 45000
    assert out["cache_read"] == 45000
    assert out["output_tokens"] == 500
    assert out["cache_creation"] == 0
    assert out["input_tokens"] + out["cache_read"] == 50000


def test_responses_json_no_cache():
    resp = {
        "usage": {
            "input_tokens": 3000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0},
        }
    }
    out = extract_usage_responses_json(resp)
    assert out["input_tokens"] == 3000
    assert out["cache_read"] == 0


# ══════════════════════════════════════════════════════════════════════
# 4. OpenAI Responses SSE
# ══════════════════════════════════════════════════════════════════════


def _make_responses_sse_completed_frame(input_tokens: int, cached_tokens: int, output_tokens: int) -> bytes:
    """构造 Responses `response.completed` 事件帧。"""
    payload = {
        "response": {
            "id": "resp_xxx",
            "status": "completed",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_tokens_details": {"cached_tokens": cached_tokens},
            },
        }
    }
    return (
        b"event: response.completed\n"
        + f"data: {json.dumps(payload)}\n\n".encode("utf-8")
    )


def test_responses_sse_tracker_subtracts_cached():
    tracker = ResponsesSSEUsageTracker()
    tracker.feed(_make_responses_sse_completed_frame(input_tokens=50000, cached_tokens=45000, output_tokens=500))
    assert tracker.usage["input_tokens"] == 5000
    assert tracker.usage["cache_read"] == 45000
    assert tracker.usage["output_tokens"] == 500
    assert tracker.usage["cache_creation"] == 0
    assert tracker.saw_stream_end is True


def test_responses_sse_tracker_zero_cache():
    tracker = ResponsesSSEUsageTracker()
    tracker.feed(_make_responses_sse_completed_frame(input_tokens=2000, cached_tokens=0, output_tokens=50))
    assert tracker.usage["input_tokens"] == 2000
    assert tracker.usage["cache_read"] == 0


# ══════════════════════════════════════════════════════════════════════
# 5. Anthropic 路径回归 —— 不受修复影响
# ══════════════════════════════════════════════════════════════════════


def test_anthropic_json_input_tokens_unchanged():
    """Anthropic 协议里 input_tokens 本来就不含 cache，解析器不做额外扣减。"""
    resp = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": 500,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 46355,
        }
    }
    out = extract_usage_anthropic_json(resp)
    # Anthropic 的 input_tokens 保持原值不变；这是历史 Claude OAuth 典型特征
    assert out["input_tokens"] == 1
    assert out["cache_read"] == 46355
    assert out["cache_creation"] == 100
    assert out["output_tokens"] == 500


# ══════════════════════════════════════════════════════════════════════
# 6. 端到端一致性断言
# ══════════════════════════════════════════════════════════════════════


def test_display_formula_consistent_across_protocols():
    """两套协议归一后，`↑ = input + cache_creation + cache_read` 都应等于总 prompt。"""
    # OpenAI Chat: 总 prompt = 48427
    chat_out = extract_usage_chat_json({
        "usage": {
            "prompt_tokens": 48427,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 47232},
        }
    })
    chat_up = chat_out["input_tokens"] + chat_out["cache_creation"] + chat_out["cache_read"]
    assert chat_up == 48427, f"OpenAI Chat 总 prompt 不对: {chat_up}"

    # OpenAI Responses: 总 prompt = 50000
    resp_out = extract_usage_responses_json({
        "usage": {
            "input_tokens": 50000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 45000},
        }
    })
    resp_up = resp_out["input_tokens"] + resp_out["cache_creation"] + resp_out["cache_read"]
    assert resp_up == 50000, f"OpenAI Responses 总 prompt 不对: {resp_up}"

    # Anthropic: 总 prompt = input(1) + cache_creation(100) + cache_read(46355) = 46456
    anth_out = extract_usage_anthropic_json({
        "usage": {
            "input_tokens": 1,
            "output_tokens": 100,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 46355,
        }
    })
    anth_up = anth_out["input_tokens"] + anth_out["cache_creation"] + anth_out["cache_read"]
    assert anth_up == 46456, f"Anthropic 总 prompt 不对: {anth_up}"
