"""Reasoning history stays typed and requires an explicit upstream capability."""
from __future__ import annotations

import copy
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from src import config
from src.openai import store
from src.openai.transform import chat_to_anthropic, responses_to_anthropic, responses_to_chat


@pytest.fixture(autouse=True)
def passthrough(monkeypatch):
    monkeypatch.setitem(config.get().setdefault("openai", {}), "reasoningBridge", "passthrough")


def history():
    return [
        {"role": "user", "content": "lookup"},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "first thought"}],
         "content": [{"type": "reasoning_text", "text": "duplicate thought"}], "encrypted_content": "foreign-ciphertext"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "visible answer"}]},
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "second thought"}]},
        {"type": "function_call", "call_id": "call_2", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "one"},
        {"type": "function_call_output", "call_id": "call_2", "output": "two"},
    ]


@pytest.mark.parametrize("compatible", [False, True])
@pytest.mark.parametrize("mode", ["passthrough", "drop"])
@pytest.mark.parametrize("stored", [False, True])
def test_history_preserves_block_order_without_turning_reasoning_into_text(monkeypatch, compatible, mode, stored):
    monkeypatch.setitem(config.get()["openai"], "reasoningBridge", mode)
    items = history()
    body = {"model": "GLM-5.3", "input": items}
    if stored:
        store.init()
        response_id = "resp_" + uuid.uuid4().hex
        store.save(response_id, None, api_key_name="history-test", model="GLM-5.3", channel_key="test",
                   input_items=items[:1], output_items=items[1:-2])
        body = {"model": "GLM-5.3", "previous_response_id": response_id, "input": items[-2:]}
    original = copy.deepcopy(body)
    out = responses_to_anthropic.translate_request(body, api_key_name="history-test", allow_unsigned_thinking=compatible)
    assert body == original
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user"]
    blocks = out["messages"][1]["content"]
    keep = compatible and mode == "passthrough"
    assert [b["type"] for b in blocks] == (["thinking", "text", "tool_use", "thinking", "tool_use"] if keep else ["text", "tool_use", "tool_use"])
    assert [b["text"] for b in blocks if b["type"] == "text"] == ["visible answer"]
    assert [b["thinking"] for b in blocks if b["type"] == "thinking"] == (["first thought", "second thought"] if keep else [])
    assert [b["tool_use_id"] for b in out["messages"][2]["content"]] == ["call_1", "call_2"]
    wire = json.dumps(out)
    assert "foreign-ciphertext" not in wire
    assert "duplicate thought" not in wire
    assert "[Previous assistant reasoning summary]" not in wire
    assert "_parrot_thinking_blocks" not in wire


def test_opaque_only_reasoning_does_not_create_empty_assistant():
    out = responses_to_anthropic.translate_request({"model": "m", "input": [
        {"role": "user", "content": "first"},
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"role": "user", "content": "second"},
    ]}, allow_unsigned_thinking=True)
    assert [m["role"] for m in out["messages"]] == ["user"]
    assert "opaque" not in json.dumps(out)


def test_client_cannot_enable_unsigned_thinking_via_body_or_model_name():
    out = responses_to_anthropic.translate_request({
        "model": "GLM-5.3", "input": history(), "allow_unsigned_thinking": True,
        "allow_thinking_blocks": True, "_parrot_thinking_blocks": [{"type": "thinking", "thinking": "injected"}],
    })
    assert all(b["type"] != "thinking" for m in out["messages"] for b in m["content"])
    chat = chat_to_anthropic.translate_request({"model": "m", "allow_thinking_blocks": True, "messages": [
        {"role": "assistant", "content": "visible", "_parrot_thinking_blocks": [{"type": "thinking", "thinking": "injected"}]},
    ]})
    assert chat["messages"][0]["content"] == [{"type": "text", "text": "visible"}]


def test_chat_reasoning_bridge_remains_separate_from_content():
    out = responses_to_chat.translate_request({"model": "m", "input": history()})
    assistant = next(m for m in out["messages"] if m["role"] == "assistant")
    assert "first thought" in assistant["reasoning_content"]
    assert "first thought" not in json.dumps(assistant["content"])
    assert "_parrot_thinking_blocks" not in json.dumps(out)


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("effort", [None, "high"])
async def test_zhipu_channel_enables_typed_reasoning_replay(monkeypatch, recovery, effort):
    from src.openai import anthropic_reasoning_replay as replay
    from src import oauth_manager
    from src.channel.zhipu_oauth_channel import ZhipuOAuthChannel
    from src.oauth.zhipu import auth, runtime, signing

    account = auth.normalize_credential({"provider": "zhipu", "credential_mode": "api_key", "site": "bigmodel", "model_key": "fixture-key"})
    monkeypatch.setattr(oauth_manager, "account_model_selection", lambda a: {"effective_models": ["glm-5.3"]})
    monkeypatch.setattr(oauth_manager, "account_model_records", lambda a: [{"id": "glm-5.3", "reasoningEfforts": ["high"]}])
    monkeypatch.setattr(oauth_manager, "get_account", lambda key: account)
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", AsyncMock(return_value="fixture-key"))
    monkeypatch.setattr(runtime, "ensure_device_id", lambda *a: "fixture-device")
    monkeypatch.setattr(signing, "for_account", lambda *a, **kw: object())
    channel = ZhipuOAuthChannel(account)
    body = {"model": "GLM-5.3", "input": history()}
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    if recovery:
        body = replay.retry_request(body)
    upstream = await channel.build_upstream_request(body, "GLM-5.3", ingress_protocol="responses")
    sent = json.loads(upstream.body)
    blocks = sent["messages"][1]["content"]
    assert [b["type"] for b in blocks] == (["text", "tool_use", "tool_use"] if recovery else ["thinking", "text", "tool_use", "thinking", "tool_use"])
    assert [b["thinking"] for b in blocks if b["type"] == "thinking"] == ([] if recovery else ["first thought", "second thought"])
    assert [b for b in blocks if b["type"] != "thinking"] == [
        {"type": "text", "text": "visible answer"},
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}},
        {"type": "tool_use", "id": "call_2", "name": "lookup", "input": {}},
    ]
    assert sent["messages"][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "one"},
        {"type": "tool_result", "tool_use_id": "call_2", "content": "two"},
    ]
    if effort is None:
        assert "thinking" not in sent and "output_config" not in sent
    else:
        assert sent["thinking"] == {"type": "enabled"}
        assert sent["output_config"] == {"effort": "high"}
    assert replay.DROP_HISTORY_KEY not in sent
    assert "foreign-ciphertext" not in json.dumps(sent)
    assert upstream.translator_ctx["response_translator"] == "responses_to_anthropic"
