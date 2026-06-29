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
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "result x"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_2", "content": "result y"}]},
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

    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "remember"}]},
        {"role": "user", "content": [{"type": "text", "text": "remember"}]},
    ]


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
        "tools": [{"type": "custom", "name": "shell"}],
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
        }]},
        {"role": "user", "content": [{"type": "text", "text": "continue"}]},
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


def test_translate_request_strips_responses_only_controls_and_tool_declarations():
    out = responses_to_anthropic.translate_request({
        "model": "m",
        "background": False,
        "reasoning": {"effort": "high"},
        "text": {"format": {"type": "json_schema"}},
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [{"type": "custom", "name": "shell"}],
        "tool_choice": {"type": "custom", "name": "shell"},
    })

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "tools" not in out
    assert "tool_choice" not in out
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
    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
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

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
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

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
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
