"""OpenAI Responses → Anthropic bridge tests (Phase 8 fourth path)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.channel.api_channel import ApiChannel
from src.openai.transform import common, responses_to_anthropic
from src.openai.transform.guard import GuardError
from src.protocols.matrix import DEFAULT_MATRIX, ProtocolGuardError, extract_request_features


def test_translate_request_composes_responses_input_to_anthropic_messages():
    body = {
        "model": "resp-model",
        "instructions": "You are helpful.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
        ],
        "tools": [{"type": "function", "name": "lookup", "description": "Lookup", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "lookup"},
        "max_output_tokens": 50,
        "temperature": 0.1,
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["stream"] is False
    assert out["system"] == [{"type": "text", "text": "You are helpful."}]
    assert out["max_tokens"] == 50
    assert out["temperature"] == 0.1
    assert out["tools"] == [{"name": "lookup", "input_schema": {"type": "object"}, "description": "Lookup"}]
    assert out["tool_choice"] == {"type": "tool", "name": "lookup"}
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "result"}]},
    ]


def test_translate_request_adds_anthropic_block_breakpoints_and_preserves_deferred_tools():
    cache_control = {"type": "ephemeral", "ttl": "1h"}
    body = {
        "model": "resp-model",
        "instructions": "stable system",
        "input": [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ],
        "tools": [
            {
                "type": "function",
                "name": "resident",
                "parameters": {"type": "object"},
                "defer_loading": False,
            },
            {
                "type": "function",
                "name": "deferred",
                "parameters": {"type": "object"},
                "defer_loading": True,
            },
        ],
        "prompt_cache_key": "stable-key",
        "prompt_cache_retention": "24h",
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["cache_control"] == cache_control
    assert out["system"][-1]["cache_control"] == cache_control
    assert out["tools"][0]["defer_loading"] is False
    assert out["tools"][0]["cache_control"] == cache_control
    assert out["tools"][1]["defer_loading"] is True
    assert "cache_control" not in out["tools"][1]
    assert [
        "cache_control" in message["content"][-1]
        for message in out["messages"]
    ] == [False, False, True, False, True]


def test_translate_request_preserves_easy_input_string_messages_after_tool_history():
    """Hermes may append local fallback messages as EasyInputMessage strings."""
    body = {
        "model": "resp-model",
        "stream": True,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "start"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
            {"role": "assistant", "content": "local max-iteration fallback"},
            {"role": "user", "content": "follow-up"},
        ],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["stream"] is True
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "result"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "local max-iteration fallback"}]},
        {"role": "user", "content": [{"type": "text", "text": "follow-up"}]},
    ]
    assert all(
        part.get("type") != "text" or bool(part.get("text"))
        for message in out["messages"]
        for part in message["content"]
    )


def test_translate_request_preserves_parallel_tool_calls():
    body = {
        "model": "resp-model",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
            {"type": "function_call", "call_id": "call_2", "name": "search", "arguments": '{"q":"y"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "result x"},
            {"type": "function_call_output", "call_id": "call_2", "output": "result y"},
        ],
        "tools": [
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            {"type": "function", "name": "search", "parameters": {"type": "object"}},
        ],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
            {"type": "tool_use", "id": "call_2", "name": "search", "input": {"q": "y"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result x"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "result y"},
        ]},
    ]


def test_translate_request_converts_responses_allowed_tools_to_anthropic_choice():
    body = {
        "model": "resp-model",
        "input": "hi",
        "tools": [
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            {"type": "function", "name": "search", "parameters": {"type": "object"}},
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "search"}],
        },
        "parallel_tool_calls": False,
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["tools"] == [{"name": "search", "input_schema": {"type": "object"}}]
    assert out["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}


def test_translate_request_guards_responses_allowed_tools_missing_definition():
    with pytest.raises(GuardError, match="does not match any declared function tool"):
        responses_to_anthropic.translate_request({
            "model": "resp-model",
            "input": "hi",
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "auto",
                "tools": [{"type": "function", "name": "missing"}],
            },
        })


def test_translate_request_preserves_text_instruction_items():
    body = {
        "model": "resp-model",
        "instructions": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "follow policy"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "background"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "noted"}]},
        ],
        "input": "ping",
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["system"] == [{"type": "text", "text": "follow policy"}]
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "background"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "noted"}]},
        {"role": "user", "content": [{"type": "text", "text": "ping"}]},
    ]


def test_translate_request_resolves_local_item_reference():
    body = {
        "model": "resp-model",
        "input": [
            {"type": "message", "id": "msg_1", "role": "user", "content": [{"type": "input_text", "text": "remember"}]},
            {"type": "item_reference", "id": "msg_1"},
        ],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "remember"},
            {"type": "text", "text": "remember"},
        ],
    }]


def test_translate_request_preserves_safe_custom_tool_history():
    body = {
        "model": "resp-model",
        "input": [
            {"type": "custom_tool_call", "call_id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
            {"type": "custom_tool_call_output", "call_id": "call_1", "output": [
                {"type": "input_text", "text": "ok"},
                {"type": "input_file", "filename": "result.txt", "file_data": "data:text/plain;base64,b2s="},
            ]},
        ],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "text/plain", "data": "b2s="},
                    "title": "result.txt",
                },
            ],
        }]},
    ]
    assert "tools" not in out


def test_translate_request_flattens_namespace_and_maps_history():
    body = {
        "model": "resp-model",
        "input": [
            {"type": "function_call", "call_id": "direct", "name": "lookup", "arguments": "{}"},
            {
                "type": "function_call",
                "call_id": "namespaced",
                "namespace": "db",
                "name": "lookup",
                "arguments": "{\"id\":1}",
            },
            {"type": "function_call_output", "call_id": "direct", "output": "direct result"},
            {"type": "function_call_output", "call_id": "namespaced", "output": "namespace result"},
        ],
        "tools": [
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            {
                "type": "namespace",
                "name": "db",
                "tools": [{
                    "type": "function",
                    "name": "lookup",
                    "description": "Namespaced lookup",
                    "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}},
                }],
            },
        ],
    }

    out = responses_to_anthropic.translate_request(body)

    assert [tool["name"] for tool in out["tools"]] == ["lookup", "db__lookup"]
    assert [block["name"] for block in out["messages"][0]["content"]] == ["lookup", "db__lookup"]
    assert [block["tool_use_id"] for block in out["messages"][1]["content"]] == ["direct", "namespaced"]


def test_translate_request_rejects_guessed_namespaced_tool_choice():
    with pytest.raises(GuardError, match="namespaced Responses allowed_tools"):
        responses_to_anthropic.translate_request({
            "model": "m", "input": "x",
            "tools": [{"type": "namespace", "name": "db", "tools": [
                {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            ]}],
            "tool_choice": {"type": "allowed_tools", "mode": "required", "tools": [
                {"type": "namespace", "name": "db"},
            ]},
        })


def test_translate_request_namespace_generated_name_avoids_real_direct_collision_stably():
    body = {
        "model": "resp-model",
        "input": [{
            "type": "function_call",
            "call_id": "call_1",
            "namespace": "db",
            "name": "lookup",
            "arguments": "{}",
        }],
        "tools": [
            {"type": "function", "name": "db__lookup", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "db", "tools": [
                {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            ]},
        ],
    }

    first = responses_to_anthropic.translate_request(body)
    second = responses_to_anthropic.translate_request(body)
    mapped = first["tools"][1]["name"]

    assert first["tools"][0]["name"] == "db__lookup"
    assert mapped != "db__lookup"
    assert mapped == second["tools"][1]["name"]
    assert first["messages"][0]["content"][0]["name"] == mapped


def test_translate_request_rejects_freeform_custom_tool_declaration():
    with pytest.raises(GuardError, match="freeform custom tool declarations"):
        responses_to_anthropic.translate_request({
            "model": "resp-model",
            "input": "run",
            "tools": [{"type": "custom", "name": "shell", "format": {"type": "text"}}],
        })


def test_translate_request_preserves_responses_image_input():
    body = {
        "model": "resp-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
            ],
        }],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ],
    }]


def test_translate_request_preserves_responses_url_image_input():
    body = {
        "model": "resp-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "https://example.com/a.png", "detail": "low"},
            ],
        }],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
        ],
    }]


def test_translate_request_guards_responses_non_base64_image_data_url():
    with pytest.raises(GuardError, match="image_url data URL must be base64 encoded"):
        responses_to_anthropic.translate_request({
            "model": "resp-model",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/svg+xml,<svg></svg>"}],
            }],
        })


def test_translate_request_preserves_responses_file_data_input():
    body = {
        "model": "resp-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "read"},
                {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
            ],
        }],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "read"},
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                "title": "brief.pdf",
            },
        ],
    }]


def test_translate_request_preserves_responses_file_url_input():
    body = {
        "model": "resp-model",
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "read"},
                {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
            ],
        }],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{
        "role": "user",
        "content": [
            {"type": "text", "text": "read"},
            {
                "type": "document",
                "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                "title": "remote.pdf",
            },
        ],
    }]


def test_translate_request_preserves_function_call_output_attachments():
    body = {
        "model": "resp-model",
        "input": [
            {"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": [
                {"type": "input_text", "text": "see attached"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "high"},
                {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
                {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
            ]},
        ],
        "tools": [{"type": "function", "name": "inspect", "parameters": {"type": "object"}}],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "inspect", "input": {}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [
                {"type": "text", "text": "see attached"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                    "title": "brief.pdf",
                },
                {
                    "type": "document",
                    "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                    "title": "remote.pdf",
                },
            ],
        }]},
    ]


def test_translate_request_preserves_previous_response_id_function_output_attachments(monkeypatch):
    expanded_items = [
        {"type": "function_call", "call_id": "call_1", "name": "inspect", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": [
            {"type": "input_text", "text": "from history"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_file", "filename": "brief.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
            {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
        ]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
    ]

    monkeypatch.setattr(
        responses_to_anthropic.responses_to_chat,
        "resolve_input_items",
        lambda body, *, api_key_name="": list(expanded_items),
    )

    out = responses_to_anthropic.translate_request({
        "model": "resp-model",
        "previous_response_id": "resp_prev",
        "input": "continue",
        "tools": [{"type": "function", "name": "inspect", "parameters": {"type": "object"}}],
    }, api_key_name="key-1")

    assert out["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "inspect", "input": {}},
        ]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [
                {"type": "text", "text": "from history"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                    "title": "brief.pdf",
                },
                {
                    "type": "document",
                    "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                    "title": "remote.pdf",
                },
            ],
        }, {"type": "text", "text": "continue"}]},
    ]


def test_translate_request_allows_noop_text_format():
    body = {
        "model": "resp-model",
        "input": "hi",
        "text": {"format": {"type": "text"}},
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "text" not in out


def test_translate_request_strips_responses_only_controls():
    out = responses_to_anthropic.translate_request({
        "model": "m",
        "background": False,
        "reasoning": {"effort": "high"},
        "text": {"format": {"type": "json_schema"}},
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    })

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "thinking" not in out


def test_translate_request_uses_anthropic_output_whitelist_for_responses_controls():
    body = {
        "model": "m",
        "input": "hi",
        "stream": True,
        "max_output_tokens": 32,
        "temperature": 0.2,
        "top_p": 0.9,
        "background": False,
        "reasoning": {"effort": "high"},
        "text": {"format": {"type": "json_schema"}},
        "prompt_cache_key": "cache-key",
        "prompt_cache_retention": "24h",
        "service_tier": "flex",
        "safety_identifier": "safe-user-1",
    }

    out = responses_to_anthropic.translate_request(body)

    assert set(out) <= common.ANTHROPIC_BRIDGE_REQ_ALLOWED
    assert out["messages"] == [{"role": "user", "content": [{
        "type": "text",
        "text": "hi",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]}]
    assert out["metadata"] == {"user_id": "safe-user-1"}
    assert "service_tier" not in out


def test_translate_request_guards_responses_content_that_would_be_lost():
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "background": True, "input": "hi"})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_file"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_file", "file_data": "AAAA", "file_url": "https://example.com/a.pdf"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "input_file", "file_url": "https://example.com/a.pdf"}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_audio"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "message", "role": "assistant", "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_image", "file_id": "file_img"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_file", "file_id": "file_1"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_audio"}]}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "item_reference", "id": "item_1"}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "custom_tool_call", "call_id": "c1"}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": "raw text"}]})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "conversation": "conv_1", "input": "hi"})
    with pytest.raises(GuardError):
        responses_to_anthropic.translate_request({"model": "m", "input": [{"type": "reasoning", "encrypted_content": "gAAAA"}, {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]})
    include_only = responses_to_anthropic.translate_request({
        "model": "m",
        "input": "hi",
        "include": ["reasoning.encrypted_content"],
    })
    assert include_only["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "include" not in include_only


@pytest.mark.parametrize("field,value", [
    ("service_tier", "flex"),
    ("prompt_cache_key", "cache-key"),
    ("prompt_cache_retention", "24h"),
    ("max_tool_calls", 1),
    ("prompt", {"id": "pmpt_1"}),
    ("truncation", "auto"),
    ("include", ["message.output_text.logprobs"]),
    ("include", ["unsupported.include"]),
])
def test_translate_request_strips_responses_options_without_anthropic_equivalent(field, value):
    body = {"model": "m", "input": "hi", field: value}
    out = responses_to_anthropic.translate_request(body)

    block = {"type": "text", "text": "hi"}
    if field in {"prompt_cache_key", "prompt_cache_retention"}:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    assert out["messages"] == [{"role": "user", "content": [block]}]
    assert field not in out


def test_translate_request_maps_safe_responses_control_fields():
    body = {
        "model": "m",
        "input": "hi",
        "service_tier": "default",
        "safety_identifier": "safe-user-1",
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["service_tier"] == "standard_only"
    assert out["metadata"] == {"user_id": "safe-user-1"}


def test_translate_request_maps_responses_service_tier_auto():
    out = responses_to_anthropic.translate_request({
        "model": "m",
        "input": "hi",
        "service_tier": "auto",
    })

    assert out["service_tier"] == "auto"


def test_translate_request_allows_internal_prompt_cache_hints():
    body = {
        "model": "m",
        "input": "hi",
        "prompt_cache_key": "internal-cache-key",
        "prompt_cache_retention": "24h",
        "_client_body_fields": ["model", "input"],
        "_internal_injected_fields": ["prompt_cache_key", "prompt_cache_retention"],
    }

    out = responses_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [{
        "type": "text",
        "text": "hi",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]}]
    assert "prompt_cache_key" not in out


def test_translate_response_returns_responses_shape_and_tool_call():
    anthropic = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-real",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 6, "output_tokens": 3, "cache_read_input_tokens": 4},
    }

    out = responses_to_anthropic.translate_response(anthropic, model="alias-model")

    assert out["object"] == "response"
    assert out["model"] == "alias-model"
    assert out["status"] == "completed"
    assert out["output_text"] == "hello"
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"][0]["text"] == "hello"
    assert out["output"][1]["type"] == "function_call"
    assert out["output"][1]["call_id"] == "call_1"
    assert out["output"][1]["name"] == "lookup"
    assert out["output"][1]["arguments"] == '{"q":"x"}'
    assert out["usage"] == {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "input_tokens_details": {"cached_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 0},
    }


def test_matrix_allows_safe_responses_to_anthropic_and_guards_unsafe_cases():
    safe = {"stream": False, "input": "hi"}
    plan = DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", safe))
    assert plan.required_transforms == ["responses_to_anthropic"]

    stream = {"stream": True, "input": "hi"}
    plan = DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", stream))
    assert plan.required_transforms == ["responses_to_anthropic"]

    controls = {"text": {"format": {"type": "json_schema"}}, "input": "hi", "background": False}
    control_plan = DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", controls))
    assert control_plan.required_transforms == ["responses_to_anthropic"]

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features("responses", {"input": "hi", "include": ["reasoning.encrypted_content"]}),
    ).required_transforms == ["responses_to_anthropic"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "anthropic",
            features=extract_request_features("responses", {"input": [{"type": "reasoning", "encrypted_content": "gAAAA"}]}),
        )

    assistant_image = {"input": [{"type": "message", "role": "assistant", "content": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", assistant_image))

    tool_output_image = {"input": [{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}]}
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", tool_output_image),
    ).required_transforms == ["responses_to_anthropic"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("responses", "openai-chat", features=extract_request_features("responses", tool_output_image))

    tool_output_file_url = {"input": [{"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_file", "file_url": "https://example.com/a.pdf"}]}]}
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", tool_output_file_url),
    ).required_transforms == ["responses_to_anthropic"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("responses", "openai-chat", features=extract_request_features("responses", tool_output_file_url))

    custom = {"input": [{"type": "custom_tool_call", "call_id": "c1"}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", custom))


def test_anthropic_api_channel_builds_responses_to_anthropic_request():
    ch = ApiChannel({
        "name": "anth",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "anthropic",
        "cc_mimicry": False,
        "models": [{"alias": "resp-alias", "real": "claude-real"}],
    })
    body = {"model": "resp-alias", "input": "hi", "max_output_tokens": 10}

    req = asyncio.run(ch.build_upstream_request(body, "claude-real", ingress_protocol="responses"))
    payload = json.loads(req.body)

    assert req.url == "https://api.example.com/v1/messages"
    assert payload["model"] == "claude-real"
    assert payload["max_tokens"] == 10
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"][0]["type"] == "text"
    assert payload["messages"][0]["content"][0]["text"] == "hi"
    assert req.translator_ctx["response_translator"] == "responses_to_anthropic"
    assert req.translator_ctx["current_input_items"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_failover_non_stream_translator_returns_responses_response():
    from src import failover

    anthropic = {
        "id": "msg_2",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }

    out = failover._apply_non_stream_response_translator(
        anthropic,
        {"response_translator": "responses_to_anthropic", "model_for_response": "claude-real"},
    )

    assert out["object"] == "response"
    assert out["output_text"] == "ok"
    assert out["output"][0]["type"] == "message"


def _sse_objects(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_namespace_plan_restores_non_stream_parallel_direct_and_namespaced():
    plan = responses_to_anthropic.NamespaceToolMap()
    wire = responses_to_anthropic.translate_request({
        "model": "m", "input": "go", "tools": [
            {"type": "function", "name": "db__lookup", "parameters": {"type": "object"}},
            {"type": "namespace", "name": "db", "tools": [
                {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
            ]},
        ],
    }, namespace_tool_map=plan)
    generated = wire["tools"][1]["name"]
    assert generated != "db__lookup" and len(generated) <= 64
    anthropic = {
        "type": "message", "role": "assistant", "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "d", "name": "db__lookup", "input": {}},
            {"type": "tool_use", "id": "n", "name": generated, "input": {"x": 1}},
        ], "usage": {},
    }
    output = responses_to_anthropic.translate_response(
        anthropic, model="m", namespace_tool_map=plan,
    )["output"]
    assert [(x["name"], x.get("namespace")) for x in output] == [
        ("db__lookup", None), ("lookup", "db"),
    ]
    assert generated not in json.dumps(output)


def test_previous_response_history_undeclared_namespace_avoids_current_direct_name(monkeypatch):
    from src.openai import store
    history = [{
        "type": "function_call", "call_id": "old", "namespace": "db",
        "name": "lookup", "arguments": "{}",
    }]
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    monkeypatch.setattr(
        store, "expand_history",
        lambda response_id, api_key_name="": history,
    )
    plan = responses_to_anthropic.NamespaceToolMap()
    wire = responses_to_anthropic.translate_request({
        "model": "m", "previous_response_id": "resp_old", "input": [], "tools": [
            {"type": "function", "name": "db__lookup", "parameters": {"type": "object"}},
        ],
    }, api_key_name="key", namespace_tool_map=plan)
    historical_flat = wire["messages"][0]["content"][0]["name"]
    assert historical_flat != "db__lookup"
    assert plan.identity_for_flat(historical_flat).namespace == "db"


def test_namespace_long_and_sanitized_names_are_bounded_and_reversible():
    plan = responses_to_anthropic.NamespaceToolMap()
    namespace, child = "space." + "n" * 90, "child/" + "x" * 90
    wire = responses_to_anthropic.translate_request({
        "model": "m", "input": "go", "tools": [{
            "type": "namespace", "name": namespace, "tools": [{
                "type": "function", "name": child, "parameters": {"type": "object"},
            }],
        }],
    }, namespace_tool_map=plan)
    flat = wire["tools"][0]["name"]
    assert len(flat) <= 64 and "." not in flat and "/" not in flat
    identity = plan.identity_for_flat(flat)
    assert (identity.namespace, identity.child_name) == (namespace, child)


def test_namespace_stream_restores_only_schema_authorized_locations():
    from src.openai.transform.stream_anthropic_to_responses import StreamTranslator
    plan = responses_to_anthropic.NamespaceToolMap()
    flat = plan.flat_name("function", "db", "lookup")
    tr = StreamTranslator(model="m", namespace_tool_map=plan, created_ts=1)
    chunks = []
    chunks += list(tr.feed((
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"tool_use","id":"call_1","name":"' + flat + '","input":{}}}\n\n'
    ).encode()))
    chunks += list(tr.feed(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":1}"}}\n\n'
    ))
    chunks += list(tr.feed(
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    ))
    chunks += list(tr.close())
    events = _sse_objects(chunks)
    added = next(x for x in events if x["type"] == "response.output_item.added")["item"]
    arg_delta = next(x for x in events if x["type"] == "response.function_call_arguments.delta")
    arg_done = next(x for x in events if x["type"] == "response.function_call_arguments.done")
    done = next(x for x in events if x["type"] == "response.output_item.done")["item"]
    completed = next(x for x in events if x["type"] == "response.completed")["response"]["output"][0]
    assert (added["namespace"], added["name"]) == ("db", "lookup")
    assert (done["namespace"], done["name"]) == ("db", "lookup")
    assert (completed["namespace"], completed["name"]) == ("db", "lookup")
    assert arg_done["name"] == "lookup" and "namespace" not in arg_done
    assert "namespace" not in arg_delta
    assert flat not in json.dumps(events)


def test_namespace_non_stream_store_receives_restored_outward_pair(monkeypatch):
    from src.openai import store
    saved = {}
    monkeypatch.setattr(store, "is_enabled", lambda: True)
    monkeypatch.setattr(store, "save", lambda **kw: saved.update(kw))
    plan = responses_to_anthropic.NamespaceToolMap()
    flat = plan.flat_name("function", "db", "lookup")
    responses_to_anthropic.translate_response({
        "type": "message", "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "c", "name": flat, "input": {}}],
        "usage": {},
    }, model="m", api_key_name="key", current_input_items=[], namespace_tool_map=plan)
    assert saved["output_items"][0]["name"] == "lookup"
    assert saved["output_items"][0]["namespace"] == "db"


def test_namespace_custom_declaration_and_namespaced_selector_are_guarded():
    with pytest.raises(GuardError, match="namespace freeform custom"):
        responses_to_anthropic.translate_request({
            "model": "m", "input": "go", "tools": [{
                "type": "namespace", "name": "shells", "tools": [{
                    "type": "custom", "name": "shell", "format": {"type": "text"},
                }],
            }],
        })


def test_api_channel_namespace_context_drives_runtime_non_stream_restore():
    from src.protocols.runtime import apply_non_stream_response_translator
    ch = ApiChannel({
        "name": "anth-ns", "baseUrl": "https://api.example.com", "apiKey": "sk",
        "protocol": "anthropic", "cc_mimicry": False,
        "models": [{"alias": "m", "real": "real"}],
    })
    req = asyncio.run(ch.build_upstream_request({
        "model": "m", "input": "go", "tools": [{
            "type": "namespace", "name": "db", "tools": [{
                "type": "function", "name": "lookup", "parameters": {"type": "object"},
            }],
        }],
    }, "real", ingress_protocol="responses"))
    flat = json.loads(req.body)["tools"][0]["name"]
    out = apply_non_stream_response_translator({
        "type": "message", "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "c", "name": flat, "input": {}}],
        "usage": {},
    }, req.translator_ctx)
    assert out["output"][0]["name"] == "lookup"
    assert out["output"][0]["namespace"] == "db"


def test_oauth_channel_carries_same_request_namespace_plan(monkeypatch):
    from src.channel.oauth_channel import OAuthChannel
    from src import oauth_manager
    async def token(_key):
        return "token"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    ch = OAuthChannel({"email": "a@example.com", "provider": "anthropic"}, ["m"])
    req = asyncio.run(ch.build_upstream_request({
        "model": "m", "input": "go", "tools": [{
            "type": "namespace", "name": "db", "tools": [{
                "type": "function", "name": "lookup", "parameters": {"type": "object"},
            }],
        }],
    }, "m", ingress_protocol="responses"))
    plan = req.translator_ctx["namespace_tool_map"]
    assert isinstance(plan, responses_to_anthropic.NamespaceToolMap)
    assert any(x.namespace == "db" and x.child_name == "lookup" for x in plan.by_flat_name.values())


def test_namespaced_custom_json_history_is_reversible_but_declaration_remains_guarded():
    plan = responses_to_anthropic.NamespaceToolMap()
    wire = responses_to_anthropic.translate_request({
        "model": "m", "input": [{
            "type": "custom_tool_call", "call_id": "old", "namespace": "shells",
            "name": "shell", "input": {"cmd": "pwd"},
        }],
    }, namespace_tool_map=plan)
    flat = wire["messages"][0]["content"][0]["name"]
    assert flat != "shell"
    restored = responses_to_anthropic.translate_response({
        "type": "message", "role": "assistant", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "c", "name": flat, "input": {"cmd": "ls"}}],
        "usage": {},
    }, model="m", namespace_tool_map=plan)["output"][0]
    assert restored["type"] == "custom_tool_call"
    assert (restored["namespace"], restored["name"]) == ("shells", "shell")
    assert restored["input"] == '{"cmd":"ls"}'
