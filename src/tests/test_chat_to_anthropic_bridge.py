"""OpenAI Chat → Anthropic bridge tests (Phase 8 second path)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.channel.api_channel import ApiChannel
from src.openai.transform import chat_to_anthropic, common
from src.openai.transform.guard import GuardError
from src.protocols.matrix import DEFAULT_MATRIX, ProtocolGuardError, extract_request_features


def test_translate_request_text_images_tools_and_tool_history():
    body = {
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "developer", "content": [{"type": "text", "text": "Be concise."}]},
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "need tool", "tool_calls": [{
                "id": "call.1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }]},
            {"role": "tool", "tool_call_id": "call.1", "content": "result"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "description": "Lookup", "parameters": {"type": "object"},
        }}],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        "max_completion_tokens": 100,
        "temperature": 0.2,
        "stop": ["END"],
        "parallel_tool_calls": False,
        "user": "u1",
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["stream"] is False
    assert out["system"] == [
        {"type": "text", "text": "You are helpful."},
        {"type": "text", "text": "Be concise."},
    ]
    assert out["messages"][0]["role"] == "user"
    assert out["messages"][0]["content"][0] == {"type": "text", "text": "look"}
    assert out["messages"][0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
    assistant = out["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "need tool"}
    assert assistant["content"][1] == {
        "type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"},
    }
    assert out["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "result"}],
    }
    assert out["tools"] == [{"name": "lookup", "input_schema": {"type": "object"}, "description": "Lookup"}]
    assert out["tool_choice"] == {"type": "tool", "name": "lookup", "disable_parallel_tool_use": True}
    assert out["max_tokens"] == 100
    assert out["temperature"] == 0.2
    assert out["stop_sequences"] == ["END"]
    assert out["metadata"] == {"user_id": "u1"}


def test_translate_request_maps_openai_prompt_cache_hints_to_anthropic_cache_control():
    out = chat_to_anthropic.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "prompt_cache_key": "stable-key",
        "prompt_cache_retention": "24h",
    })

    cache_control = {"type": "ephemeral", "ttl": "1h"}
    assert out["cache_control"] == cache_control
    assert out["messages"][0]["content"][-1]["cache_control"] == cache_control


def test_translate_request_adds_anthropic_block_breakpoints_and_skips_deferred_tools():
    cache_control = {"type": "ephemeral", "ttl": "1h"}
    body = {
        "messages": [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "resident", "parameters": {"type": "object"}},
                "defer_loading": False,
            },
            {
                "type": "function",
                "function": {"name": "deferred", "parameters": {"type": "object"}},
                "defer_loading": True,
            },
        ],
        "prompt_cache_key": "stable-key",
        "prompt_cache_retention": "24h",
    }

    out = chat_to_anthropic.translate_request(body)

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


def test_translate_request_does_not_put_tool_breakpoint_when_all_tools_are_deferred():
    out = chat_to_anthropic.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "deferred_1", "parameters": {"type": "object"}},
                "defer_loading": True,
            },
            {
                "type": "function",
                "function": {"name": "deferred_2", "parameters": {"type": "object"}},
                "defer_loading": True,
            },
        ],
        "prompt_cache_key": "stable-key",
    })

    assert all(tool["defer_loading"] is True for tool in out["tools"])
    assert all("cache_control" not in tool for tool in out["tools"])


def test_translate_request_preserves_url_image():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]}]}

    out = chat_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]


def test_translate_request_preserves_tool_result_image_content():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "inspect", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": [
                {"type": "text", "text": "see attached"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
        ],
    }

    out = chat_to_anthropic.translate_request(body)

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
            ],
        }]},
    ]


def test_translate_request_guards_non_base64_image_data_url():
    with pytest.raises(GuardError, match="image_url data URL must be base64 encoded"):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/svg+xml,<svg></svg>"}},
        ]}]})

    with pytest.raises(GuardError, match="image_url data URL must be base64 encoded"):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
        ]}]})


def test_translate_request_converts_file_data_to_document():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {"type": "file", "file": {
            "filename": "case.pdf",
            "file_data": "data:application/pdf;base64,JVBERi0xLjQ=",
        }},
    ]}]}

    out = chat_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
            "title": "case.pdf",
        },
    ]}]


def test_translate_request_preserves_parallel_tool_calls():
    body = {
        "messages": [{"role": "assistant", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"x"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "search", "arguments": '{"q":"y"}'}},
        ]}],
        "tools": [
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}},
        ],
    }

    out = chat_to_anthropic.translate_request(body)

    blocks = out["messages"][0]["content"]
    assert blocks == [
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
        {"type": "tool_use", "id": "call_2", "name": "search", "input": {"q": "y"}},
    ]


def test_translate_request_preserves_safe_custom_tool_call_history():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "custom", "custom": {"name": "shell", "input": {"cmd": "pwd"}}},
                {"id": "call_2", "type": "custom", "custom": {"name": "shell", "input": '{"cmd":"ls"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["messages"] == [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
            {"type": "tool_use", "id": "call_2", "name": "shell", "input": {"cmd": "ls"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}]},
    ]


def test_translate_request_rejects_raw_custom_tool_call_input():
    with pytest.raises(GuardError, match="custom tool_call input must be a JSON object"):
        chat_to_anthropic.translate_request({"messages": [{"role": "assistant", "tool_calls": [{
            "id": "call_1",
            "type": "custom",
            "custom": {"name": "shell", "input": "raw input"},
        }]}]})


def test_translate_request_converts_legacy_functions_and_function_call():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "functions": [{
            "name": "lookup",
            "description": "Lookup things",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }],
        "function_call": {"name": "lookup"},
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["tools"] == [{
        "name": "lookup",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "description": "Lookup things",
    }]
    assert out["tool_choice"] == {"type": "tool", "name": "lookup"}


def test_translate_request_converts_allowed_tools_to_filtered_anthropic_tools():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}},
        ],
        "tool_choice": {
            "type": "allowed_tools",
            "allowed_tools": {
                "mode": "required",
                "tools": [{"type": "function", "function": {"name": "search"}}],
            },
        },
        "parallel_tool_calls": False,
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["tools"] == [{"name": "search", "input_schema": {"type": "object"}}]
    assert out["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}


def test_translate_request_guards_allowed_tools_missing_definition():
    with pytest.raises(GuardError, match="does not match any declared function tool"):
        chat_to_anthropic.translate_request({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": {
                "type": "allowed_tools",
                "allowed_tools": {
                    "mode": "auto",
                    "tools": [{"type": "function", "function": {"name": "missing"}}],
                },
            },
        })


def test_translate_request_guards_allowed_tools_non_function_entries():
    with pytest.raises(GuardError, match="only supports function tools"):
        chat_to_anthropic.translate_request({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
            "tool_choice": {
                "type": "allowed_tools",
                "allowed_tools": {
                    "mode": "auto",
                    "tools": [{"type": "web_search_preview", "name": "web"}],
                },
            },
        })


def test_translate_request_prefers_modern_tool_choice_over_legacy_function_call():
    out = chat_to_anthropic.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "functions": [{"name": "legacy_lookup"}],
        "tool_choice": "auto",
        "function_call": {"name": "legacy_lookup"},
    })

    assert out["tool_choice"] == {"type": "auto"}
    assert {tool["name"] for tool in out["tools"]} == {"lookup", "legacy_lookup"}


def test_translate_request_allows_noop_text_response_format_and_text_modality():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "text"},
        "modalities": ["text"],
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "response_format" not in out
    assert "modalities" not in out


def test_translate_request_strips_openai_only_chat_controls():
    out = chat_to_anthropic.translate_request({
        "reasoning_effort": "high",
        "response_format": {"type": "json_schema"},
        "modalities": ["text"],
        "frequency_penalty": 0.2,
        "presence_penalty": 0.2,
        "messages": [{"role": "assistant", "reasoning_content": "hidden"}],
        "tools": [{"type": "web_search_preview"}],
    })

    assert out["messages"] == [{"role": "assistant", "content": [{"type": "text", "text": ""}]}]
    assert "tools" not in out
    assert "thinking" not in out
    assert "response_format" not in out
    assert "modalities" not in out


def test_translate_request_uses_anthropic_output_whitelist_for_chat_controls():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 32,
        "temperature": 0.2,
        "top_p": 0.9,
        "stop": ["END"],
        "seed": 123,
        "prediction": {"type": "content", "content": "hi"},
        "response_format": {"type": "json_schema"},
        "modalities": ["text"],
        "frequency_penalty": 0.2,
        "presence_penalty": 0.2,
        "logprobs": True,
        "top_logprobs": 3,
        "logit_bias": {"123": 10},
        "store": True,
        "prompt_cache_key": "cache-key",
        "prompt_cache_retention": "24h",
        "web_search_options": {"search_context_size": "low"},
        "verbosity": "high",
    }

    out = chat_to_anthropic.translate_request(body)

    assert set(out) <= common.ANTHROPIC_BRIDGE_REQ_ALLOWED
    assert out["messages"] == [{"role": "user", "content": [{
        "type": "text",
        "text": "hi",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]}]
    assert out["stop_sequences"] == ["END"]


def test_anthropic_bridge_allowlist_is_protocol_subset_not_source_validator():
    assert common.ANTHROPIC_BRIDGE_REQ_ALLOWED < common.ANTHROPIC_MESSAGES_REQ_ALLOWED
    # These official Anthropic fields require native/provider/state adapters and
    # must not pass through the generic OpenAI-family bridge by accident.
    for field in ("thinking", "output_config", "context_management", "container", "mcp_servers", "top_k"):
        assert field in common.ANTHROPIC_MESSAGES_REQ_ALLOWED
        assert field not in common.ANTHROPIC_BRIDGE_REQ_ALLOWED


def test_translate_request_guards_chat_content_that_would_be_lost():
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"n": 2, "messages": [{"role": "user", "content": "hi"}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"modalities": ["text", "audio"], "messages": [{"role": "user", "content": "hi"}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "assistant", "content": None, "audio": {"id": "audio_1"}}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"file_id": "file_img"}}]}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "file_1"}}]}]})
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_url": "https://example.com/a.pdf"}}]}]})


@pytest.mark.parametrize("field,value", [
    ("verbosity", "high"),
    ("web_search_options", {"search_context_size": "low"}),
    ("service_tier", "flex"),
    ("prompt_cache_key", "cache-key"),
    ("prompt_cache_retention", "24h"),
    ("logit_bias", {"123": 10}),
    ("store", True),
])
def test_translate_request_strips_chat_options_without_anthropic_equivalent(field, value):
    body = {"messages": [{"role": "user", "content": "hi"}], field: value}
    out = chat_to_anthropic.translate_request(body)

    block = {"type": "text", "text": "hi"}
    if field in {"prompt_cache_key", "prompt_cache_retention"}:
        block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    assert out["messages"] == [{"role": "user", "content": [block]}]
    if field == "service_tier":
        assert "service_tier" not in out
    else:
        assert field not in out


def test_translate_request_maps_or_strips_safe_openai_control_fields():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "seed": 123,
        "prediction": {"type": "content", "content": "hi"},
        "service_tier": "default",
        "safety_identifier": "safe-user-1",
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["service_tier"] == "standard_only"
    assert out["metadata"] == {"user_id": "safe-user-1"}
    assert "seed" not in out
    assert "prediction" not in out


def test_translate_request_maps_openai_service_tier_auto():
    out = chat_to_anthropic.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "auto",
    })

    assert out["service_tier"] == "auto"


def test_translate_request_allows_internal_prompt_cache_hints():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "prompt_cache_key": "internal-cache-key",
        "prompt_cache_retention": "24h",
        "_client_body_fields": ["messages"],
        "_internal_injected_fields": ["prompt_cache_key", "prompt_cache_retention"],
    }

    out = chat_to_anthropic.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [{
        "type": "text",
        "text": "hi",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }]}]
    assert "prompt_cache_key" not in out


def test_translate_request_rejects_invalid_tool_call_arguments():
    body = {"messages": [{"role": "assistant", "tool_calls": [{
        "id": "call.bad", "type": "function",
        "function": {"name": "lookup", "arguments": "not json"},
    }]}]}
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request(body)

    scalar = {"messages": [{"role": "assistant", "tool_calls": [{
        "id": "call.bad", "type": "function",
        "function": {"name": "lookup", "arguments": "[]"},
    }]}]}
    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request(scalar)


def test_translate_response_to_chat_completion():
    anthropic = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-x",
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 6,
            "output_tokens": 3,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 4,
        },
    }

    out = chat_to_anthropic.translate_response(anthropic, model="alias-model")

    assert out["object"] == "chat.completion"
    assert out["model"] == "alias-model"
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {
        "role": "assistant",
        "content": "hello",
        "tool_calls": [{
            "id": "toolu_1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
        }],
    }
    assert out["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


def test_matrix_allows_safe_chat_to_anthropic_and_guards_unsafe_cases():
    safe = {"stream": False, "messages": [{"role": "user", "content": "hi"}]}
    plan = DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", safe))
    assert plan.required_transforms == ["chat_to_anthropic"]

    stream = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    plan = DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", stream))
    assert plan.required_transforms == ["chat_to_anthropic"]

    controls = {"response_format": {"type": "json_schema"}, "messages": [{"role": "user", "content": "hi"}], "store": True}
    control_plan = DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", controls))
    assert control_plan.required_transforms == ["chat_to_anthropic"]

    assistant_image = {"messages": [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", assistant_image))

    file_id_image = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"file_id": "file_img"}}]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", file_id_image))

    tool_image = {"messages": [{"role": "tool", "tool_call_id": "call_1", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}
    assert DEFAULT_MATRIX.plan(
        "chat", "anthropic", features=extract_request_features("chat", tool_image),
    ).required_transforms == ["chat_to_anthropic"]

    with pytest.raises(GuardError):
        chat_to_anthropic.translate_request({"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]})


def test_anthropic_api_channel_builds_chat_to_anthropic_request():
    ch = ApiChannel({
        "name": "anth",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "anthropic",
        "cc_mimicry": False,
        "models": [{"alias": "gpt-alias", "real": "claude-real"}],
    })
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}

    req = asyncio.run(ch.build_upstream_request(body, "claude-real", ingress_protocol="chat"))
    payload = json.loads(req.body)

    assert req.url == "https://api.example.com/v1/messages"
    assert payload["model"] == "claude-real"
    assert payload["max_tokens"] == 10
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"][0]["type"] == "text"
    assert payload["messages"][0]["content"][0]["text"] == "hi"
    assert req.translator_ctx["response_translator"] == "chat_to_anthropic"


def test_anthropic_api_channel_native_request_uses_provider_allowlist():
    ch = ApiChannel({
        "name": "anth",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "anthropic",
        "cc_mimicry": False,
        "models": [{"alias": "claude", "real": "claude-real"}],
    })
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
        "service_tier": "auto",
        "container": {"id": "ctr_1"},
        "mcp_servers": [{"name": "tools"}],
        "prompt_cache_key": "openai-only",
        "response_format": {"type": "json_schema"},
        "_api_key_name": "internal",
    }

    req = asyncio.run(ch.build_upstream_request(body, "claude-real", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert payload["model"] == "claude-real"
    assert payload["service_tier"] == "auto"
    assert payload["container"] == {"id": "ctr_1"}
    assert payload["mcp_servers"] == [{"name": "tools"}]
    assert "prompt_cache_key" not in payload
    assert "response_format" not in payload
    assert "_api_key_name" not in payload
    assert req.translator_ctx is None


def test_failover_non_stream_translator_returns_chat_completion():
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
        {"response_translator": "chat_to_anthropic", "model_for_response": "claude-real"},
    )

    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "ok"}
    assert out["choices"][0]["finish_reason"] == "stop"
