"""Scheduler integration tests for ProtocolMatrix Phase 7 shell."""

from __future__ import annotations

from types import SimpleNamespace

from src import scheduler


class FakeChannel(SimpleNamespace):
    enabled = True
    disabled_reason = None

    def supports_model(self, requested_model):
        return self.real if requested_model == self.alias else None


def _ch(key, protocol, alias="m", real="real", type="api"):
    return FakeChannel(key=key, protocol=protocol, alias=alias, real=real, type=type)


def test_filter_candidates_uses_matrix_and_phase8_reachability(monkeypatch):
    channels = [
        _ch("a", "anthropic"),
        _ch("c", "openai-chat"),
        _ch("r", "openai-responses"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    anthropic, _, plans_a, guards_a = scheduler._filter_candidates("m", "anthropic", body={"model": "m", "messages": []})
    # Phase 8 opens safe Anthropic → OpenAI Chat and Responses (non-stream + narrow stream).
    assert [ch.key for ch, _ in anthropic] == ["a", "c", "r"]
    assert ("a", "real") in plans_a
    assert plans_a[("c", "real")].required_transforms == ["anthropic_to_chat"]
    assert plans_a[("r", "real")].required_transforms == ["anthropic_to_responses"]
    assert guards_a == []

    chat, _, plans_chat, guards_chat = scheduler._filter_candidates("m", "chat", body={"model": "m", "messages": []})
    # Phase 8 second path opens safe non-stream OpenAI Chat → Anthropic too.
    assert [ch.key for ch, _ in chat] == ["a", "c", "r"]
    assert plans_chat[("a", "real")].required_transforms == ["chat_to_anthropic"]
    assert plans_chat[("c", "real")].cost == 0
    assert plans_chat[("r", "real")].required_transforms == ["chat_to_responses"]
    assert guards_chat == []

    responses, _, plans_resp, guards_resp = scheduler._filter_candidates("m", "responses", body={"model": "m", "input": "hi"})
    assert [ch.key for ch, _ in responses] == ["a", "c", "r"]
    assert plans_resp[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert plans_resp[("c", "real")].required_transforms == ["responses_to_chat"]
    assert plans_resp[("r", "real")].cost == 0
    assert guards_resp == []



def test_filter_candidates_allows_anthropic_user_image_to_chat(monkeypatch):
    channels = [_ch("c", "openai-chat")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "anthropic", body=body)

    assert [ch.key for ch, _ in available] == ["c"]
    assert saturated == []
    assert plans[("c", "real")].required_transforms == ["anthropic_to_chat"]
    assert guards == []

    result = scheduler.schedule(body, api_key_name="k", client_ip="127.0.0.1", ingress_protocol="anthropic")
    assert result
    assert result.candidates[0][0].key == "c"


def test_filter_candidates_records_guard_reason_for_non_user_anthropic_image(monkeypatch):
    channels = [_ch("c", "openai-chat")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [{"role": "assistant", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "anthropic", body=body)

    assert available == []
    assert saturated == []
    assert plans == {}
    assert guards == ["Anthropic→OpenAI Chat image input is only enabled for user messages: assistant:image"]

    result = scheduler.schedule(body, api_key_name="k", client_ip="127.0.0.1", ingress_protocol="anthropic")
    assert not result
    assert result.guard_error == "Anthropic→OpenAI Chat image input is only enabled for user messages: assistant:image"


def test_responses_custom_tool_keeps_anthropic_fallback_and_native_candidate(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "input": "hi", "tools": [{"type": "custom", "name": "shell"}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "r"]
    assert saturated == []
    assert ("r", "real") in plans
    assert plans[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert guards == []


def test_responses_safe_custom_tool_history_keeps_anthropic_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "input": [
        {"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": {"cmd": "pwd"}},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"},
    ]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "r"]
    assert saturated == []
    assert plans[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert plans[("r", "real")].cost == 0
    assert guards == []


def test_chat_safe_custom_tool_history_keeps_anthropic_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [{"role": "assistant", "tool_calls": [{
        "id": "call_1",
        "type": "custom",
        "custom": {"name": "shell", "input": {"cmd": "pwd"}},
    }]}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "chat", body=body)

    assert [ch.key for ch, _ in available] == ["a", "r"]
    assert saturated == []
    assert plans[("a", "real")].required_transforms == ["chat_to_anthropic"]
    assert plans[("r", "real")].required_transforms == ["chat_to_responses"]
    assert guards == []


def test_chat_raw_custom_tool_history_skips_anthropic_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [{"role": "assistant", "tool_calls": [{
        "id": "call_1",
        "type": "custom",
        "custom": {"name": "shell", "input": "raw input"},
    }]}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "chat", body=body)

    assert [ch.key for ch, _ in available] == ["r"]
    assert saturated == []
    assert plans[("r", "real")].required_transforms == ["chat_to_responses"]
    assert guards == ["OpenAI Chat→Anthropic tool_call history is not safely convertible: custom_tool_call.input"]


def test_responses_allowed_tools_keeps_anthropic_chat_and_native_candidates(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": "hi",
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "auto",
            "tools": [{"type": "function", "name": "lookup"}],
        },
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "c", "r"]
    assert saturated == []
    assert plans[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert plans[("c", "real")].required_transforms == ["responses_to_chat"]
    assert ("r", "real") in plans
    assert guards == []


def test_responses_allowed_tools_with_hosted_nested_tool_keeps_anthropic_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": "hi",
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "auto",
            "tools": [{"type": "web_search", "name": "search"}],
        },
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "r"]
    assert saturated == []
    assert ("r", "real") in plans
    assert plans[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert ("c", "real") not in plans
    assert guards == [
        "OpenAI Responses→Chat hosted/stateful tools are not enabled yet: tool_choice:allowed_tools:web_search",
    ]

def test_responses_tool_search_routes_only_to_explicit_codex_oauth_native(monkeypatch):
    channels = [
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "tool_search", "execution": "client"}],
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["oauth-r"]
    assert saturated == []
    assert plans[("oauth-r", "real")].cost == 0
    assert ("api-r", "real") not in plans
    assert guards == [
        "OpenAI Responses native route does not support requested server-side state: tool_search",
    ]


def test_responses_tool_choice_tool_search_routes_to_codex_oauth(monkeypatch):
    channels = [_ch("oauth-r", "openai-responses", type="oauth")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "tool_search", "execution": "client"}],
        "tool_choice": {"type": "tool_search"},
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["oauth-r"]
    assert saturated == []
    assert plans[("oauth-r", "real")].cost == 0
    assert guards == []


def test_responses_codex_oauth_still_rejects_other_hosted_tools(monkeypatch):
    channels = [
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "tool_search"}, {"type": "web_search"}],
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert available == []
    assert saturated == []
    assert plans == {}
    assert guards == [
        "OpenAI Responses native route does not support requested server-side state: web_search",
        "OpenAI Responses native route does not support requested server-side state: tool_search",
    ]


def test_responses_include_only_encrypted_reasoning_can_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "input": "hi", "include": ["reasoning.encrypted_content"]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "c", "r"]
    assert saturated == []
    assert ("a", "real") in plans
    assert ("c", "real") in plans
    assert ("r", "real") in plans
    assert guards == []


def test_responses_encrypted_reasoning_input_can_fallback_to_chat(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "input": [{"type": "reasoning", "encrypted_content": "gAAAA"}]}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["c", "r"]
    assert saturated == []
    assert ("r", "real") in plans
    assert ("c", "real") in plans
    assert ("a", "real") not in plans
    assert guards == [
        "OpenAI Responses→Anthropic include reasoning.encrypted_content / encrypted reasoning replay is not enabled yet",
    ]


def test_responses_conversation_routes_only_to_native_api_channel(monkeypatch):
    channels = [
        _ch("a", "anthropic"),
        _ch("c", "openai-chat"),
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "conversation": "conv_1", "input": "hi"}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["api-r"]
    assert saturated == []
    assert plans[("api-r", "real")].cost == 0
    assert ("oauth-r", "real") not in plans
    assert guards == [
        "OpenAI Responses→Anthropic stateful input items are not enabled yet: conversation",
        "OpenAI Responses→Chat hosted/stateful tools are not enabled yet: conversation",
        "OpenAI Responses native route does not support requested server-side state: conversation",
    ]


def test_responses_background_true_routes_only_to_native_api_channel(monkeypatch):
    channels = [
        _ch("a", "anthropic"),
        _ch("c", "openai-chat"),
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "background": True, "input": "hi"}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["api-r"]
    assert saturated == []
    assert plans[("api-r", "real")].cost == 0
    assert guards == [
        "OpenAI Responses→Anthropic background async state is not enabled yet",
        "OpenAI Responses→Chat background async state is not enabled yet",
        "OpenAI Responses native route does not support requested server-side state: background",
    ]


def test_responses_local_item_reference_keeps_chat_and_anthropic_fallback(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": [
            {"type": "message", "id": "msg_1", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "item_reference", "id": "msg_1"},
        ],
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["a", "c", "r"]
    assert saturated == []
    assert plans[("a", "real")].required_transforms == ["responses_to_anthropic"]
    assert plans[("c", "real")].required_transforms == ["responses_to_chat"]
    assert plans[("r", "real")].cost == 0
    assert guards == []


def test_chat_multi_candidate_routes_only_to_native_chat(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "n": 2}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "chat", body=body)

    assert [ch.key for ch, _ in available] == ["c"]
    assert saturated == []
    assert plans[("c", "real")].cost == 0
    assert guards == [
        "OpenAI Chat→Anthropic multi-candidate n>1 aggregation is not enabled yet",
        "OpenAI Chat→Responses multi-candidate n>1 aggregation is not enabled yet",
    ]


def test_chat_audio_output_routes_only_to_native_chat(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "alloy", "format": "wav"},
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "chat", body=body)

    assert [ch.key for ch, _ in available] == ["c"]
    assert saturated == []
    assert plans[("c", "real")].cost == 0
    assert guards == [
        "OpenAI Chat→Anthropic audio input/history is not enabled yet",
        "OpenAI Chat→Responses audio output/history is not enabled yet: audio",
    ]


def test_chat_audio_input_can_bridge_to_api_responses_not_codex(monkeypatch):
    channels = [
        _ch("a", "anthropic"),
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ]}],
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "chat", body=body)

    assert [ch.key for ch, _ in available] == ["api-r"]
    assert saturated == []
    assert plans[("api-r", "real")].required_transforms == ["chat_to_responses"]
    assert guards == [
        "OpenAI Chat→Anthropic audio input/history is not enabled yet",
        "OpenAI Responses target route does not support audio content",
    ]


def test_responses_audio_routes_to_chat_and_api_responses_not_codex(monkeypatch):
    channels = [
        _ch("a", "anthropic"),
        _ch("c", "openai-chat"),
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AAAA"}}]}],
    }
    available, saturated, plans, guards = scheduler._filter_candidates("m", "responses", body=body)

    assert [ch.key for ch, _ in available] == ["c", "api-r"]
    assert saturated == []
    assert plans[("c", "real")].required_transforms == ["responses_to_chat"]
    assert plans[("api-r", "real")].cost == 0
    assert guards == [
        "OpenAI Responses→Anthropic audio input is not enabled yet",
        "OpenAI Responses target route does not support audio content",
    ]


def test_openai_file_id_routes_around_codex_without_retrieval(monkeypatch):
    channels = [
        _ch("oauth-r", "openai-responses", type="oauth"),
        _ch("api-r", "openai-responses", type="api"),
    ]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    chat_body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"file_id": "file_img"}},
    ]}]}
    available, _, plans, guards = scheduler._filter_candidates("m", "chat", body=chat_body)
    assert [ch.key for ch, _ in available] == ["api-r"]
    assert plans[("api-r", "real")].required_transforms == ["chat_to_responses"]
    assert guards == ["OpenAI target route does not support file_id-backed content: image_url.file_id"]

    responses_body = {"model": "m", "input": [{"type": "message", "role": "user", "content": [
        {"type": "input_file", "file_id": "file_doc"},
    ]}]}
    available, _, plans, guards = scheduler._filter_candidates("m", "responses", body=responses_body)
    assert [ch.key for ch, _ in available] == ["api-r"]
    assert plans[("api-r", "real")].cost == 0
    assert guards == ["OpenAI target route does not support file_id-backed content: input_file.file_id"]


def test_anthropic_thinking_disabled_can_fallback_to_openai_candidates(monkeypatch):
    channels = [_ch("a", "anthropic"), _ch("c", "openai-chat"), _ch("r", "openai-responses")]
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: channels)
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)

    body = {"model": "m", "messages": [], "thinking": {"type": "disabled"}}
    available, saturated, plans, guards = scheduler._filter_candidates("m", "anthropic", body=body)

    assert [ch.key for ch, _ in available] == ["a", "c", "r"]
    assert saturated == []
    assert ("a", "real") in plans
    assert ("c", "real") in plans
    assert ("r", "real") in plans
    assert guards == []
