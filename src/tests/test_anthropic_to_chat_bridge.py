"""Anthropic → OpenAI Chat bridge tests (Phase 8 first path)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.transform import anthropic_to_chat
from src.openai.transform.guard import GuardError
from src.protocols.matrix import DEFAULT_MATRIX, ProtocolGuardError, extract_request_features


def test_translate_request_text_tools_and_tool_history():
    body = {
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "need tool"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
                {"type": "text", "text": "continue"},
            ]},
        ],
        "tools": [{"name": "lookup", "description": "Lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup", "disable_parallel_tool_use": True},
        "stop_sequences": ["END"],
        "max_tokens": 100,
        "temperature": 0.2,
    }

    out = anthropic_to_chat.translate_request(body)

    assert out["stream"] is False
    assert out["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assistant = out["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "need tool"
    assert assistant["tool_calls"][0]["id"] == "toolu_1"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"q":"x"}'
    assert out["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "result"}
    assert out["messages"][4] == {"role": "user", "content": "continue"}
    assert out["tools"][0]["type"] == "function"
    assert out["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert out["parallel_tool_calls"] is False
    assert out["stop"] == ["END"]
    assert out["max_tokens"] == 100
    assert out["temperature"] == 0.2


def test_translate_request_preserves_parallel_tool_calls():
    body = {
        "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            {"type": "tool_use", "id": "toolu_2", "name": "search", "input": {"q": "y"}},
        ]}],
        "tools": [
            {"name": "lookup", "input_schema": {"type": "object"}},
            {"name": "search", "input_schema": {"type": "object"}},
        ],
    }

    out = anthropic_to_chat.translate_request(body)

    calls = out["messages"][0]["tool_calls"]
    assert [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
        ("toolu_1", "lookup", {"q": "x"}),
        ("toolu_2", "search", {"q": "y"}),
    ]


def test_translate_request_rejects_non_object_tool_use_input():
    with pytest.raises(GuardError) as exc_info:
        anthropic_to_chat.translate_request({
            "messages": [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": "bad"},
            ]}],
        })
    assert "tool_use.input must be an object" in exc_info.value.message


def test_translate_request_preserves_user_images():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}

    out = anthropic_to_chat.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]}]


def test_translate_request_preserves_user_documents():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {
            "type": "document",
            "title": "brief.pdf",
            "context": "customer contract",
            "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQ="},
        },
    ]}]}

    out = anthropic_to_chat.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "read"},
        {"type": "text", "text": "Document context: customer contract"},
        {"type": "file", "file": {
            "file_data": "JVBERi0xLjQ=",
            "filename": "brief.pdf",
        }},
    ]}]


def test_translate_request_strips_anthropic_cache_control_blocks():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
    ]}]}

    out = anthropic_to_chat.translate_request(body)

    assert out["messages"] == [{"role": "user", "content": "cached"}]
    assert "cache_control" not in json.dumps(out)


def test_translate_request_maps_reasoning_effort_and_service_tier():
    out = anthropic_to_chat.translate_request({
        "messages": [{"role": "user", "content": "think"}],
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "output_config": {"effort": "max"},
        "service_tier": "standard_only",
    }, target_model="gpt-5")

    assert out["reasoning_effort"] == "xhigh"
    assert out["service_tier"] == "default"

    adaptive = anthropic_to_chat.translate_request({
        "messages": [{"role": "user", "content": "think"}],
        "thinking": {"type": "adaptive"},
        "service_tier": "auto",
    }, target_model="gpt-5-codex")
    assert adaptive["reasoning_effort"] == "xhigh"
    assert adaptive["service_tier"] == "auto"


def test_translate_request_guards_unmappable_reasoning_controls():
    disabled = anthropic_to_chat.translate_request({
        "messages": [],
        "thinking": {"type": "disabled"},
    }, target_model="gpt-5")
    assert "reasoning_effort" not in disabled
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({
            "messages": [],
            "thinking": {"type": "enabled"},
        }, target_model="gpt-4o")


def test_translate_request_allows_stream_but_guards_stateful_thinking_and_non_user_images():
    streamed = anthropic_to_chat.translate_request({"stream": True, "messages": []})
    assert streamed["stream"] is True
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "assistant", "content": [{"type": "redacted_thinking", "data": "opaque"}]}]})
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "assistant", "content": [{"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}]}]})
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "assistant", "content": [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}}]}]})
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}]}]})
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "user", "content": [{"type": "document", "citations": {"enabled": True}, "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}}]}]})
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({"messages": [{"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [{"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}],
        }]}]})
    errored_tool = anthropic_to_chat.translate_request({"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "failed",
        "is_error": True,
    }]}]})
    assert errored_tool["messages"] == [{"role": "tool", "tool_call_id": "toolu_1", "content": "failed"}]
    with pytest.raises(GuardError):
        anthropic_to_chat.translate_request({
            "messages": [],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        })
    stripped = anthropic_to_chat.translate_request({"messages": [], "top_k": 5, "service_tier": "turbo"})
    assert "top_k" not in stripped
    assert "service_tier" not in stripped


def test_translate_response_to_anthropic_message():
    chat = {
        "id": "chatcmpl_1",
        "model": "gpt-x",
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    }

    out = anthropic_to_chat.translate_response(chat, model="alias-model")

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


def test_translate_response_preserves_chat_refusal_as_text():
    chat = {
        "id": "chatcmpl_refusal",
        "model": "gpt-x",
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": None, "refusal": "I cannot help."},
        }],
        "usage": {},
    }

    out = anthropic_to_chat.translate_response(chat, model="alias-model")

    assert out["content"] == [{"type": "text", "text": "I cannot help."}]
    assert out["stop_reason"] == "end_turn"


def test_matrix_allows_safe_anthropic_to_openai_chat_and_guards_unsafe_cases():
    safe = {"stream": False, "messages": [{"role": "user", "content": "hi"}]}
    plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", safe))
    assert plan.required_transforms == ["anthropic_to_chat"]

    stream = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
    stream_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", stream))
    assert stream_plan.required_transforms == ["anthropic_to_chat"]

    image = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}
    image_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", image))
    assert image_plan.required_transforms == ["anthropic_to_chat"]

    document = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
    ]}]}
    doc_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", document))
    assert doc_plan.required_transforms == ["anthropic_to_chat"]

    stop = {"messages": [{"role": "user", "content": "hi"}], "stop_sequences": ["END"]}
    stop_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", stop))
    assert stop_plan.required_transforms == ["anthropic_to_chat"]

    reasoning = {"messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "enabled", "budget_tokens": 2048}}
    reasoning_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", reasoning))
    assert reasoning_plan.required_transforms == ["anthropic_to_chat"]

    service_tier = {"messages": [{"role": "user", "content": "hi"}], "service_tier": "standard_only"}
    service_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", service_tier))
    assert service_plan.required_transforms == ["anthropic_to_chat"]

    assistant_image = {"messages": [{"role": "assistant", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", assistant_image))

    url_document = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}},
    ]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", url_document))

    cited_document = {"messages": [{"role": "user", "content": [
        {"type": "document", "citations": {"enabled": True}, "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
    ]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", cited_document))

    tool_result_image = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": [{"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}],
    }]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", tool_result_image))

    tool_result_error = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "failed",
        "is_error": True,
    }]}]}
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-chat", features=extract_request_features("anthropic", tool_result_error),
    ).required_transforms == ["anthropic_to_chat"]

    hosted_tool = {"messages": [], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", hosted_tool))

    unsupported_option = {"messages": [{"role": "user", "content": "hi"}], "top_k": 5}
    option_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", unsupported_option))
    assert option_plan.required_transforms == ["anthropic_to_chat"]

    disabled_thinking = {"messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "disabled"}}
    disabled_plan = DEFAULT_MATRIX.plan("anthropic", "openai-chat", features=extract_request_features("anthropic", disabled_thinking))
    assert disabled_plan.required_transforms == ["anthropic_to_chat"]


def test_openai_api_channel_builds_anthropic_to_chat_request():
    ch = OpenAIApiChannel({
        "name": "oa",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-chat",
        "models": [{"alias": "claude-alias", "real": "gpt-5"}],
    })
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "_api_key_name": "internal",
    }

    req = asyncio.run(ch.build_upstream_request(body, "gpt-5", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert req.url == "https://api.example.com/v1/chat/completions"
    assert payload["model"] == "gpt-5"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["reasoning_effort"] == "low"
    assert "_api_key_name" not in payload
    assert req.translator_ctx["response_translator"] == "anthropic_to_chat"


def test_openai_api_channel_maps_anthropic_cache_to_openai_prompt_cache():
    ch = OpenAIApiChannel({
        "name": "oa",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-chat",
        "models": [{"alias": "claude-alias", "real": "gpt-5.5"}],
    })
    body = {
        "system": "stable expensive instructions",
        "messages": [{"role": "user", "content": "dynamic tail"}],
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
        "_parrot_api_key_name": "k1",
        "_parrot_client_ip": "127.0.0.1",
    }

    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.5", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert payload["prompt_cache_key"].startswith("parrot:cache:v1:a2o:")
    assert payload["prompt_cache_retention"] == "24h"
    assert "cache_control" not in payload


def test_deepseek_anthropic_bridge_disables_default_thinking_and_allows_forced_tool():
    ch = OpenAIApiChannel({
        "name": "DeepSeek",
        "baseUrl": "https://api.deepseek.com",
        "apiKey": "sk-test",
        "protocol": "openai-chat",
        "models": [{"alias": "deepseek-v4-flash", "real": "deepseek-v4-flash"}],
    })
    body = {
        "messages": [{"role": "user", "content": "call tool"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup"},
    }

    req = asyncio.run(ch.build_upstream_request(body, "deepseek-v4-flash", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert payload["thinking"] == {"type": "disabled"}
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}


def test_deepseek_explicit_thinking_rejects_forced_tool_choice():
    ch = OpenAIApiChannel({
        "name": "DeepSeek",
        "baseUrl": "https://api.deepseek.com",
        "apiKey": "sk-test",
        "protocol": "openai-chat",
        "models": [{"alias": "deepseek-v4-flash", "real": "deepseek-v4-flash"}],
    })
    body = {
        "messages": [{"role": "user", "content": "call tool"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "lookup"},
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    }

    with pytest.raises(GuardError) as exc_info:
        asyncio.run(ch.build_upstream_request(body, "deepseek-v4-flash", ingress_protocol="anthropic"))
    assert "DeepSeek thinking mode does not support" in exc_info.value.message


def test_openai_api_channel_filters_translated_anthropic_to_chat_payload(monkeypatch):
    ch = OpenAIApiChannel({
        "name": "oa",
        "baseUrl": "https://api.example.com",
        "apiKey": "sk-test",
        "protocol": "openai-chat",
        "models": [{"alias": "claude-alias", "real": "gpt-5"}],
    })

    def fake_translate_request(body, *, target_model=None):
        return {
            "model": target_model or "gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "context_management": {"edits": []},
            "previous_response_id": "resp_bad",
            "_api_key_name": "internal",
        }

    monkeypatch.setattr(anthropic_to_chat, "translate_request", fake_translate_request)

    req = asyncio.run(ch.build_upstream_request(
        {"messages": [{"role": "user", "content": "hi"}]},
        "gpt-5",
        ingress_protocol="anthropic",
    ))
    payload = json.loads(req.body)

    assert payload == {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]}


def test_failover_non_stream_translator_returns_anthropic_message():
    from src import failover

    chat = {
        "id": "chatcmpl_2",
        "model": "gpt-real",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 0}},
    }

    out = failover._apply_non_stream_response_translator(
        chat,
        {"response_translator": "anthropic_to_chat", "model_for_response": "gpt-real"},
    )

    assert out["type"] == "message"
    assert out["role"] == "assistant"
    assert out["content"] == [{"type": "text", "text": "ok"}]
    assert out["stop_reason"] == "end_turn"
