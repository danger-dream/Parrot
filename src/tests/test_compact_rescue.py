from __future__ import annotations

from src.compact_rescue import is_claude_code_compact_request, prepare_compact_rescue_body


def _compact_prompt() -> str:
    return """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
These MUST be preserved verbatim in the summary so they continue to apply after compaction.

Your summary should include the following sections:

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
"""


def test_compact_rescue_strips_only_top_level_system_and_tool_definitions():
    body = {
        "model": "gpt-5.5",
        "system": [{"type": "text", "text": "top level system"}],
        "tools": [{"name": "Bash", "input_schema": {}}],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "max_tokens": 20000,
        "messages": [
            {"role": "system", "content": "mid conversation reminder"},
            {"role": "user", "content": [{"type": "text", "text": "please inspect the file"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "/tmp/a"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": [{"type": "text", "text": "x" * 10000}]},
                {"type": "text", "text": _compact_prompt()},
            ]},
        ],
    }

    assert is_claude_code_compact_request(body) is True
    rescued, meta = prepare_compact_rescue_body(body)

    assert meta is not None
    assert "system" not in rescued
    assert "tools" not in rescued
    assert "tool_choice" not in rescued
    assert "thinking" not in rescued
    assert "output_config" not in rescued
    assert "context_management" not in rescued
    assert rescued["max_tokens"] == 20000
    assert meta["removed_top_level_system"] is True
    assert meta["removed_tool_definitions"] == 1
    assert meta["removed_tool_choice"] is True
    assert meta["removed_thinking"] is True
    assert meta["removed_output_config"] is True
    assert meta["removed_context_management"] is True
    assert meta["original_max_tokens"] == 20000
    assert meta["preserved_max_tokens"] == 20000
    assert meta["original_max_output_tokens"] is None
    assert meta["preserved_max_output_tokens"] is None

    # Conversation messages are preserved verbatim, including role=system and
    # historical assistant tool_use / user tool_result blocks.
    assert rescued["messages"] == body["messages"]
    assert rescued["messages"][0]["role"] == "system"
    assert rescued["messages"][2]["content"][0]["type"] == "tool_use"
    assert rescued["messages"][3]["content"][0]["type"] == "tool_result"
    assert "x" * 1000 in str(rescued)


def test_non_compact_request_is_left_unchanged():
    body = {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}

    rescued, meta = prepare_compact_rescue_body(body)

    assert rescued is body
    assert meta is None
