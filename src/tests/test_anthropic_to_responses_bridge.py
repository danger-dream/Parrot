"""Anthropic → OpenAI Responses bridge tests (Phase 8 third path)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.transform import anthropic_to_responses
from src.openai.transform.guard import GuardError
from src.protocols.matrix import DEFAULT_MATRIX, ProtocolGuardError, extract_request_features


def test_translate_request_text_image_tools_and_history_lifting():
    body = {
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "need tool"},
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "result"},
                {"type": "text", "text": "continue"},
            ]},
        ],
        "tools": [{"name": "lookup", "description": "Lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup", "disable_parallel_tool_use": True},
        "max_tokens": 100,
        "temperature": 0.2,
    }

    out = anthropic_to_responses.translate_request(body)

    assert out["stream"] is False
    assert out["instructions"] == "You are helpful."
    assert out["max_output_tokens"] == 100
    assert out["temperature"] == 0.2
    assert out["tools"] == [{"type": "function", "name": "lookup", "parameters": {"type": "object"}, "description": "Lookup"}]
    assert out["tool_choice"] == {"type": "function", "name": "lookup"}
    assert out["parallel_tool_calls"] is False

    items = out["input"]
    assert items[0] == {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        ],
    }
    assert items[1] == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "need tool"}]}
    assert items[2] == {
        "type": "function_call", "id": "fc_call_1", "call_id": "call_1",
        "name": "lookup", "arguments": '{"q":"x"}', "status": "completed",
    }
    assert items[3] == {"type": "function_call_output", "call_id": "call_1", "output": "result"}
    assert items[4] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}


def test_translate_request_preserves_url_image_input():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}

    out = anthropic_to_responses.translate_request(body)

    assert out["input"] == [{
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "https://example.com/a.png", "detail": "auto"},
        ],
    }]


def test_translate_request_preserves_user_documents():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {
            "type": "document",
            "title": "brief.pdf",
            "context": "customer contract",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
        },
        {
            "type": "document",
            "title": "remote.pdf",
            "source": {"type": "url", "url": "https://example.com/remote.pdf"},
        },
    ]}]}

    out = anthropic_to_responses.translate_request(body)

    assert out["input"] == [{
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "read"},
            {"type": "input_text", "text": "Document context: customer contract"},
            {"type": "input_file", "file_data": "JVBERi0xLjQ=", "filename": "brief.pdf"},
            {"type": "input_file", "file_url": "https://example.com/remote.pdf", "filename": "remote.pdf"},
        ],
    }]


def test_translate_request_preserves_tool_result_attachments():
    body = {
        "messages": [{"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [
                {"type": "text", "text": "see attached"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {
                    "type": "document",
                    "title": "brief.pdf",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
                },
                {
                    "type": "document",
                    "title": "remote.pdf",
                    "context": "remote contract",
                    "source": {"type": "url", "url": "https://example.com/remote.pdf"},
                },
            ],
        }]}],
    }

    out = anthropic_to_responses.translate_request(body)

    assert out["input"] == [{
        "type": "function_call_output",
        "call_id": "call_1",
        "output": [
            {"type": "input_text", "text": "see attached"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
            {"type": "input_file", "file_data": "JVBERi0xLjQ=", "filename": "brief.pdf"},
            {"type": "input_text", "text": "Document context: remote contract"},
            {"type": "input_file", "file_url": "https://example.com/remote.pdf", "filename": "remote.pdf"},
        ],
    }]


def test_translate_request_strips_anthropic_cache_control_blocks():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
    ]}]}

    out = anthropic_to_responses.translate_request(body)

    assert out["input"] == [{
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "cached"}],
    }]
    assert "cache_control" not in json.dumps(out)


def test_translate_request_preserves_parallel_tool_calls():
    body = {
        "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
            {"type": "tool_use", "id": "call_2", "name": "search", "input": {"q": "y"}},
        ]}],
        "tools": [
            {"name": "lookup", "input_schema": {"type": "object"}},
            {"name": "search", "input_schema": {"type": "object"}},
        ],
    }

    out = anthropic_to_responses.translate_request(body)

    calls = [item for item in out["input"] if item["type"] == "function_call"]
    assert [(c["call_id"], c["name"], json.loads(c["arguments"])) for c in calls] == [
        ("call_1", "lookup", {"q": "x"}),
        ("call_2", "search", {"q": "y"}),
    ]


def test_translate_request_rejects_non_object_tool_use_input():
    with pytest.raises(GuardError) as exc_info:
        anthropic_to_responses.translate_request({
            "messages": [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": ["bad"]},
            ]}],
        })
    assert "tool_use.input must be an object" in exc_info.value.message


def test_translate_request_maps_reasoning_effort_and_service_tier():
    out = anthropic_to_responses.translate_request({
        "messages": [{"role": "user", "content": "think"}],
        "thinking": {"type": "enabled", "budget_tokens": 8000},
        "service_tier": "auto",
    }, target_model="gpt-5")

    assert out["reasoning"] == {"effort": "medium"}
    assert out["service_tier"] == "auto"

    codex = anthropic_to_responses.translate_request({
        "messages": [{"role": "user", "content": "fast"}],
        "output_config": {"effort": "max"},
        "service_tier": "auto",
    }, target_model="gpt-5-codex", codex_oauth=True)
    assert codex["reasoning"] == {"effort": "xhigh"}
    assert codex["service_tier"] == "priority"

    standard_codex = anthropic_to_responses.translate_request({
        "messages": [{"role": "user", "content": "normal"}],
        "service_tier": "standard_only",
    }, target_model="gpt-5-codex", codex_oauth=True)
    assert "service_tier" not in standard_codex


def test_translate_request_guards_unmappable_reasoning_controls():
    disabled = anthropic_to_responses.translate_request({
        "messages": [],
        "thinking": {"type": "disabled"},
    }, target_model="gpt-5")
    assert "reasoning" not in disabled
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({
            "messages": [],
            "thinking": {"type": "enabled"},
        }, target_model="gpt-4o")


def test_translate_request_ignores_claude_code_clear_thinking_context_management():
    out = anthropic_to_responses.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    }, target_model="gpt-5.5")

    assert out["reasoning"] == {"effort": "medium"}
    assert "context_management" not in out


def test_translate_request_allows_claude_code_system_message_role():
    out = anthropic_to_responses.translate_request({
        "messages": [
            {"role": "system", "content": "system in messages"},
            {"role": "user", "content": "hi"},
        ],
    }, target_model="gpt-5.5")

    assert out["input"][0] == {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": "system in messages"}],
    }


def test_translate_request_allows_stream_but_guards_stateful_thinking_and_builtin_tools():
    streamed = anthropic_to_responses.translate_request({"stream": True, "messages": []})
    assert streamed["stream"] is True
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "assistant", "content": [{"type": "redacted_thinking", "data": "opaque"}]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "assistant", "content": [{
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/a.png"},
        }]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "assistant", "content": [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
        }]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "user", "content": [{
            "type": "document",
            "source": {"type": "file", "file_id": "file_1"},
        }]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "user", "content": [{
            "type": "document",
            "citations": {"enabled": True},
            "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
        }]}]})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [{"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": [{"type": "document", "citations": {"enabled": True}, "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}}],
        }]}]})
    errored_tool = anthropic_to_responses.translate_request({"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "failed",
        "is_error": True,
    }]}]})
    assert errored_tool["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "failed"}]
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"context_management": {"clear_function_results": True}, "messages": []})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"output_config": {"type": "json_schema"}, "messages": []})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"container": {"id": "c1"}, "messages": []})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"mcp_servers": [{"name": "mcp"}], "messages": []})
    with pytest.raises(GuardError):
        anthropic_to_responses.translate_request({"messages": [], "tools": [{"type": "web_search_20250305", "name": "web_search"}]})
    stripped = anthropic_to_responses.translate_request({"messages": [], "stop_sequences": ["END"], "top_k": 5, "service_tier": "turbo"})
    assert "stop" not in stripped
    assert "stop_sequences" not in stripped
    assert "top_k" not in stripped
    assert "service_tier" not in stripped


def test_translate_response_to_anthropic_message():
    resp = {
        "id": "resp_1",
        "object": "response",
        "created_at": 123,
        "status": "completed",
        "model": "gpt-real",
        "output": [
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": "hello", "annotations": []},
            ]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}', "status": "completed"},
        ],
        "output_text": "hello",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 3,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }

    out = anthropic_to_responses.translate_response(resp, model="alias-model")

    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["model"] == "alias-model"
    assert out["stop_reason"] == "tool_use"
    assert out["content"] == [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
    ]
    assert out["usage"] == {
        "input_tokens": 6,
        "output_tokens": 3,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 4,
    }


def test_matrix_allows_safe_anthropic_to_responses_and_guards_unsafe_cases():
    safe = {"stream": False, "messages": [{"role": "user", "content": "hi"}]}
    plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", safe))
    assert plan.required_transforms == ["anthropic_to_responses"]

    stream = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    stream_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", stream))
    assert stream_plan.required_transforms == ["anthropic_to_responses"]

    reasoning = {"messages": [{"role": "user", "content": "hi"}], "output_config": {"effort": "max"}}
    reasoning_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", reasoning))
    assert reasoning_plan.required_transforms == ["anthropic_to_responses"]

    service_tier = {"messages": [{"role": "user", "content": "hi"}], "service_tier": "auto"}
    service_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", service_tier))
    assert service_plan.required_transforms == ["anthropic_to_responses"]

    document = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
        {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}},
    ]}]}
    doc_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", document))
    assert doc_plan.required_transforms == ["anthropic_to_responses"]

    thinking = {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", thinking))

    assistant_image = {"messages": [{"role": "assistant", "content": [{
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/a.png"},
    }]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", assistant_image))

    cited_document = {"messages": [{"role": "user", "content": [{
        "type": "document",
        "citations": {"enabled": True},
        "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
    }]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", cited_document))

    tool_result_image = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": [{"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}],
    }]}]}
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", tool_result_image),
    ).required_transforms == ["anthropic_to_responses"]

    tool_result_cited_document = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": [{"type": "document", "citations": {"enabled": True}, "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}}],
    }]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "anthropic", "openai-responses", features=extract_request_features("anthropic", tool_result_cited_document),
        )

    tool_result_error = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "failed",
        "is_error": True,
    }]}]}
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", tool_result_error),
    ).required_transforms == ["anthropic_to_responses"]

    stop = {"messages": [{"role": "user", "content": "hi"}], "stop_sequences": ["END"]}
    stop_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", stop))
    assert stop_plan.required_transforms == ["anthropic_to_responses"]

    unsupported_option = {"messages": [{"role": "user", "content": "hi"}], "top_k": 5}
    option_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", unsupported_option))
    assert option_plan.required_transforms == ["anthropic_to_responses"]

    disabled_thinking = {"messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "disabled"}}
    disabled_plan = DEFAULT_MATRIX.plan("anthropic", "openai-responses", features=extract_request_features("anthropic", disabled_thinking))
    assert disabled_plan.required_transforms == ["anthropic_to_responses"]


def test_openai_api_channel_builds_anthropic_to_responses_request():
    ch = OpenAIApiChannel({
        "name": "resp",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-responses",
        "models": [{"alias": "claude-alias", "real": "gpt-5"}],
    })
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
        "output_config": {"effort": "max"},
        "_api_key_name": "internal",
    }

    req = asyncio.run(ch.build_upstream_request(body, "gpt-5", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert req.url == "https://api.example.com/v1/responses"
    assert payload["model"] == "gpt-5"
    assert payload["max_output_tokens"] == 10
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert "_api_key_name" not in payload
    assert req.translator_ctx["response_translator"] == "anthropic_to_responses"


def test_openai_api_channel_filters_translated_anthropic_to_responses_payload(monkeypatch):
    ch = OpenAIApiChannel({
        "name": "resp",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-responses",
        "models": [{"alias": "claude-alias", "real": "gpt-5"}],
    })

    def fake_translate_request(body, *, target_model=None):
        return {
            "model": target_model or "gpt-5",
            "input": "hi",
            "messages": [{"role": "user", "content": "should not leak"}],
            "response_format": {"type": "json_schema"},
            "container": {"id": "anthropic-only"},
            "_api_key_name": "internal",
        }

    monkeypatch.setattr(anthropic_to_responses, "translate_request", fake_translate_request)

    req = asyncio.run(ch.build_upstream_request(
        {"messages": [{"role": "user", "content": "hi"}]},
        "gpt-5",
        ingress_protocol="anthropic",
    ))
    payload = json.loads(req.body)

    assert payload == {"model": "gpt-5", "input": "hi"}


def test_failover_non_stream_translator_returns_anthropic_message():
    from src import failover

    resp = {
        "id": "resp_2",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 2, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 0}},
    }

    out = failover._apply_non_stream_response_translator(
        resp,
        {"response_translator": "anthropic_to_responses", "model_for_response": "gpt-real"},
    )

    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "ok"}]
    assert out["stop_reason"] == "end_turn"
