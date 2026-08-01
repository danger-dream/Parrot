from __future__ import annotations

import json

from src import compact_rescue, model_metadata


def _compact_prompt() -> str:
    return """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
These MUST be preserved verbatim in the summary so they continue to apply after compaction.

Your summary should include the following sections:
1. Primary Request and Intent
"""


def test_compact_rescue_default_knobs_match_current_hardcoded_behavior(monkeypatch):
    cfg = dict(compact_rescue.config.get())
    cfg["compactRescue"] = compact_rescue.config.DEFAULT_CONFIG["compactRescue"]
    cfg["modelBindings"] = {"defaults": {}, "scoped": {}}
    cfg["modelMetadata"] = {}
    monkeypatch.setattr(compact_rescue.config, "get", lambda: cfg)
    monkeypatch.setattr(model_metadata.config, "get", lambda: cfg)
    assert compact_rescue.chunk_target_tokens() == 100000
    assert compact_rescue.reduce_max_tokens() == 20000
    assert model_metadata.summary_reserve_tokens("unknown-model") == 20000
    assert model_metadata.safe_prompt_limit("unknown-model") is None
    default_prompts = compact_rescue.config.DEFAULT_CONFIG["compactRescue"]["prompts"]
    assert default_prompts["direct"].strip()
    assert default_prompts["segment"].strip()
    assert default_prompts["reduce"].strip()

    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": _compact_prompt()}]}]}
    assert compact_rescue.is_claude_code_compact_request(body) is True

    segment = compact_rescue.build_segment_summary_body(body, [{"role": "user", "content": "hello"}], segment_index=1, segment_count=1)
    segment_text = segment["messages"][0]["content"][0]["text"]
    assert "CRITICAL: Respond with TEXT ONLY. Do NOT call tools." in segment_text
    assert "<segment_summary>" in segment_text

    reduce_body = compact_rescue.build_reduce_summary_body(body, ["<segment_summary>A</segment_summary>"])
    reduce_text = reduce_body["messages"][0]["content"][0]["text"]
    assert "Output exactly two top-level XML-like blocks" in reduce_text
    assert "Primary Request and Intent" in reduce_text
    assert "latest user request and latest unfinished/current work have highest priority" in reduce_text
    assert _compact_prompt() in reduce_text


def test_compact_rescue_custom_config_overrides_prompts_and_knobs(monkeypatch):
    original_get = compact_rescue.config.get
    cfg = original_get()
    custom = dict(cfg)
    custom["compactRescue"] = {
        "enabled": True,
        "markers": ["custom compact marker"],
        "chunkTargetTokens": 1234,
        "reduceMaxTokens": 567,
        "summaryReserveTokens": 345,
        "safetyBufferTokens": 234,
        "segmentConcurrency": 2,
        "binaryOmitMinChars": 10,
        "binarySampleChars": 8,
        "binaryAsciiRatio": 0.8,
        "prompts": {
            "direct": "DIRECT {compact_prompt} :: {transcript}",
            "segment": "SEG {segment_index}/{segment_count} :: {transcript}",
            "reduce": "REDUCE :: {summaries}",
        },
    }
    monkeypatch.setattr(compact_rescue.config, "get", lambda: custom)
    monkeypatch.setattr(model_metadata.config, "get", lambda: custom)

    assert compact_rescue.chunk_target_tokens() == 1234
    assert compact_rescue.reduce_max_tokens() == 567
    assert compact_rescue.segment_concurrency() == 2
    assert model_metadata.summary_reserve_tokens("unknown-model") == 345
    assert model_metadata.compact_buffer_tokens() == 234
    assert compact_rescue.is_claude_code_compact_request({"messages": [{"role": "user", "content": "custom compact marker"}]}) is True
    assert compact_rescue.is_claude_code_compact_request({"messages": [{"role": "user", "content": _compact_prompt()}]}) is False

    body = {"messages": [{"role": "user", "content": "custom compact marker"}]}
    direct = compact_rescue.build_direct_summary_body(body, model="m", max_tokens=10)
    assert direct["messages"][0]["content"][0]["text"].startswith("DIRECT")
    segment = compact_rescue.build_segment_summary_body(body, [{"role": "user", "content": "hello"}], segment_index=3, segment_count=4)
    assert segment["messages"][0]["content"][0]["text"].startswith("SEG 3/4")
    reduce_body = compact_rescue.build_reduce_summary_body(body, ["A", "B"])
    assert reduce_body["messages"][0]["content"][0]["text"] == "REDUCE :: ## Segment 1\nA\n\n## Segment 2\nB"


def test_map_reduce_bodies_strip_top_controls_and_preserve_segment_messages():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "u" * 60000}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"path": "/tmp/a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "r" * 60000}]}]},
        {"role": "user", "content": [{"type": "text", "text": "next turn " + ("n" * 60000)}]},
        {"role": "user", "content": [{"type": "text", "text": _compact_prompt()}]},
    ]
    body = {
        "model": "gpt-5.5",
        "stream": True,
        "system": [{"type": "text", "text": "top system"}],
        "tools": [{"name": "Read", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "max_tokens": 20000,
        "messages": messages,
    }

    chunks = compact_rescue.split_messages_for_compact(messages, target_chars=70000)
    assert len(chunks) >= 2

    segment = compact_rescue.build_segment_summary_body(body, chunks[0], segment_index=1, segment_count=len(chunks))
    for key in ("system", "tools", "tool_choice", "thinking", "output_config", "context_management"):
        assert key not in segment
    assert segment[compact_rescue.INTERNAL_FLAG] is True
    assert segment["stream"] is False
    assert segment["max_tokens"] == 20000
    # Existing transcript content is preserved as rendered text, not structured
    # tool protocol blocks, so artificial segment boundaries cannot violate
    # OpenAI function-call/tool-output pairing rules.
    assert len(segment["messages"]) == 1
    rendered_segment = segment["messages"][0]["content"][0]["text"]
    assert "Transcript segment" in rendered_segment
    assert "tool_use" in rendered_segment
    assert "tool_result" in rendered_segment
    assert not any(
        isinstance(block, dict) and block.get("type") in {"tool_use", "tool_result"}
        for message in segment["messages"]
        for block in (message.get("content") or [])
    )

    reduce_body = compact_rescue.build_reduce_summary_body(body, ["<segment_summary>A</segment_summary>", "<segment_summary>B</segment_summary>"])
    for key in ("system", "tools", "tool_choice", "thinking", "output_config", "context_management"):
        assert key not in reduce_body
    assert reduce_body[compact_rescue.INTERNAL_FLAG] is True
    assert reduce_body["stream"] is False
    assert reduce_body["max_tokens"] == 20000
    rendered = json.dumps(reduce_body, ensure_ascii=False)
    assert "Segment 1" in rendered
    assert "Segment 2" in rendered
    # The final reducer now receives the original compact instruction as a
    # style/structure reference while still treating it as non-durable context.
    assert "Original compact instruction" in rendered
    assert "CRITICAL: Respond with TEXT ONLY" in rendered
    assert "latest user request and latest unfinished/current work have highest priority" in rendered
    assert "CRITICAL CURRENT-STATE CHECKPOINT" in rendered
    assert rendered.count("<segment_summary>B</segment_summary>") == 2


def test_mixed_user_and_compact_text_blocks_preserve_latest_request():
    latest_request = "按照你的理解改个T-170我看看"
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "older request"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": latest_request},
                {"type": "text", "text": _compact_prompt()},
            ],
        },
        {"role": "system", "content": "trailing reminder"},
    ]

    history = compact_rescue._history_without_compact_prompt(messages)
    assert len(history) == 3
    assert history[1] == {
        "role": "user",
        "content": [{"type": "text", "text": latest_request}],
    }
    assert history[2]["content"] == "trailing reminder"
    assert compact_rescue._compact_source_prompt(messages) == _compact_prompt()

    body = {"messages": messages}
    direct = compact_rescue.build_direct_summary_body(body, model="m", max_tokens=100)
    prompt = direct["messages"][0]["content"][0]["text"]
    transcript = prompt.split("Transcript:\n", 1)[1]
    assert latest_request in transcript
    assert "older request" in transcript
    assert "trailing reminder" in transcript
    assert "CRITICAL: Respond with TEXT ONLY" not in transcript


def test_mixed_single_string_preserves_prefix_and_isolates_compact_prompt():
    latest_request = "接着审核5.5，把最后来做图纸的修正。"
    messages = [{
        "role": "user",
        "content": latest_request + "\n\n" + _compact_prompt(),
    }]

    history = compact_rescue._history_without_compact_prompt(messages)
    assert history == [{"role": "user", "content": latest_request}]
    assert compact_rescue._compact_source_prompt(messages) == _compact_prompt()


def test_standalone_compact_message_is_removed_but_tool_result_is_preserved():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "result"},
                {"type": "text", "text": _compact_prompt()},
            ],
        },
    ]

    history = compact_rescue._history_without_compact_prompt(messages)
    assert len(history) == 2
    assert history[1]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "result"},
    ]
