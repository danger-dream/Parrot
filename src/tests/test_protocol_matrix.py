"""Protocol matrix tests for Phase 7."""

from __future__ import annotations

import pytest

from src.protocols.matrix import (
    DEFAULT_MATRIX,
    ChannelCapabilities,
    ProtocolGuardError,
    canonical_ingress_protocol,
)


def test_canonical_ingress_protocol_names():
    assert canonical_ingress_protocol("anthropic") == "anthropic"
    assert canonical_ingress_protocol("chat") == "openai-chat"
    assert canonical_ingress_protocol("responses") == "openai-responses"


def test_matrix_allows_existing_native_routes():
    assert DEFAULT_MATRIX.plan("anthropic", "anthropic").cost == 0
    assert DEFAULT_MATRIX.plan("chat", "openai-chat").cost == 0
    assert DEFAULT_MATRIX.plan("responses", "openai-responses").cost == 0


def test_matrix_allows_native_state_only_on_capable_responses_channels():
    from src.protocols.matrix import extract_request_features

    conversation = {"conversation": "conv_1", "input": "hi"}
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-responses",
        features=extract_request_features("responses", conversation),
    ).cost == 0

    codex_caps = ChannelCapabilities(
        protocol="openai-responses",
        native_state=frozenset({"encrypted_reasoning_replay", "prompt_cache_key"}),
    )
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses",
            "openai-responses",
            features=extract_request_features("responses", conversation),
            capabilities=codex_caps,
        )


def test_matrix_routes_file_id_only_to_openai_targets_with_file_capability():
    from src.protocols.matrix import extract_request_features

    chat_file = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"file_id": "file_img"}},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "chat",
        "openai-chat",
        features=extract_request_features("chat", chat_file),
    ).cost == 0
    assert DEFAULT_MATRIX.plan(
        "chat",
        "openai-responses",
        features=extract_request_features("chat", chat_file),
    ).required_transforms == ["chat_to_responses"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat",
            "openai-responses",
            features=extract_request_features("chat", chat_file),
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset()),
        )

    responses_file = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_file", "file_id": "file_doc"},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-responses",
        features=extract_request_features("responses", responses_file),
    ).cost == 0
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses",
            "openai-responses",
            features=extract_request_features("responses", responses_file),
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset()),
        )


def test_matrix_allows_existing_openai_variant_bridge_routes():
    c2r = DEFAULT_MATRIX.plan("chat", "openai-responses")
    assert c2r.cost == 1
    assert c2r.required_transforms == ["chat_to_responses"]

    r2c = DEFAULT_MATRIX.plan("responses", "openai-chat")
    assert r2c.cost == 1
    assert r2c.required_transforms == ["responses_to_chat"]


def test_matrix_routes_chat_refusal_to_responses_but_guards_for_anthropic():
    from src.protocols.matrix import extract_request_features

    body = {"messages": [{"role": "assistant", "content": [
        {"type": "refusal", "refusal": "I can't help with that."},
    ]}]}

    assert DEFAULT_MATRIX.plan(
        "chat", "openai-responses", features=extract_request_features("chat", body),
    ).required_transforms == ["chat_to_responses"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat", "anthropic", features=extract_request_features("chat", body),
        )


def test_matrix_guards_chat_audio_history_and_unknown_content_parts():
    from src.protocols.matrix import extract_request_features

    audio_history = {"messages": [{"role": "assistant", "content": None, "audio": {"id": "audio_1"}}]}
    unknown_part = {"messages": [{"role": "user", "content": [
        {"type": "input_video", "video_url": "https://example.com/v.mp4"},
    ]}]}

    for body in (audio_history, unknown_part):
        for upstream in ("openai-responses", "anthropic"):
            with pytest.raises(ProtocolGuardError):
                DEFAULT_MATRIX.plan(
                    "chat", upstream, features=extract_request_features("chat", body),
                )


def test_matrix_allows_responses_allowed_tools_to_chat_and_anthropic():
    from src.protocols.matrix import extract_request_features

    body = {
        "input": "hi",
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "auto",
            "tools": [{"type": "function", "name": "lookup"}],
        },
    }

    r2c = DEFAULT_MATRIX.plan(
        "responses", "openai-chat",
        features=extract_request_features("responses", body),
    )
    assert r2c.required_transforms == ["responses_to_chat"]

    r2a = DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features("responses", body),
    )
    assert r2a.required_transforms == ["responses_to_anthropic"]


def test_matrix_rejects_responses_allowed_tools_with_hosted_nested_tool_to_chat():
    from src.protocols.matrix import extract_request_features

    body = {
        "input": "hi",
        "tool_choice": {
            "type": "allowed_tools",
            "mode": "auto",
            "tools": [{"type": "web_search", "name": "search"}],
        },
    }

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "openai-chat",
            features=extract_request_features("responses", body),
        )


def test_matrix_allows_codex_responses_namespace_passthrough_only_when_native():
    from src.protocols.matrix import extract_request_features
    from src.providers.capabilities import OPENAI_API_CAPABILITIES, OPENAI_CODEX_CAPABILITIES

    body = {
        "input": "hi",
        "tools": [{"type": "namespace", "name": "codex_app", "tools": []}],
    }
    features = extract_request_features("responses", body)

    assert features.hosted_tool_label == "namespace"
    assert features.hosted_tool_labels == ("namespace",)
    assert "namespace" in OPENAI_CODEX_CAPABILITIES.native_state
    assert "namespace" not in OPENAI_API_CAPABILITIES.native_state

    codex_caps = ChannelCapabilities(
        protocol="openai-responses",
        native_state=OPENAI_CODEX_CAPABILITIES.native_state,
    )
    assert DEFAULT_MATRIX.plan(
        "responses", "openai-responses", features=features, capabilities=codex_caps,
    ).cost == 0

    with pytest.raises(ProtocolGuardError) as hosted_exc:
        DEFAULT_MATRIX.plan(
            "responses", "openai-responses", features=features,
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"hosted_tools"})),
        )
    assert "namespace" in hosted_exc.value.reason

    with pytest.raises(ProtocolGuardError) as api_exc:
        DEFAULT_MATRIX.plan(
            "responses", "openai-responses", features=features,
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=OPENAI_API_CAPABILITIES.native_state),
        )
    assert "namespace" in api_exc.value.reason


def test_matrix_keeps_codex_native_passthrough_labels_independent():
    from src.protocols.matrix import extract_request_features

    body = {
        "input": "hi",
        "tools": [
            {"type": "tool_search"},
            {"type": "namespace", "name": "codex_app", "tools": []},
        ],
    }
    features = extract_request_features("responses", body)

    assert features.hosted_tool_label == "tool_search"
    assert features.hosted_tool_labels == ("tool_search", "namespace")

    with pytest.raises(ProtocolGuardError) as namespace_exc:
        DEFAULT_MATRIX.plan(
            "responses", "openai-responses", features=features,
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"tool_search"})),
        )
    assert "namespace" in namespace_exc.value.reason

    with pytest.raises(ProtocolGuardError) as tool_search_exc:
        DEFAULT_MATRIX.plan(
            "responses", "openai-responses", features=features,
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"namespace"})),
        )
    assert "tool_search" in tool_search_exc.value.reason

    assert DEFAULT_MATRIX.plan(
        "responses", "openai-responses", features=features,
        capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"tool_search", "namespace"})),
    ).cost == 0


def test_matrix_rejects_mixed_codex_namespace_and_unsupported_hosted_tool():
    from src.protocols.matrix import extract_request_features

    body = {
        "input": "hi",
        "tools": [
            {"type": "namespace", "name": "codex_app", "tools": []},
            {"type": "web_search_preview"},
        ],
    }
    features = extract_request_features("responses", body)

    assert features.hosted_tool_label == "web_search_preview"
    assert features.hosted_tool_labels == ("web_search_preview",)

    with pytest.raises(ProtocolGuardError) as exc:
        DEFAULT_MATRIX.plan(
            "responses", "openai-responses", features=features,
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"namespace"})),
        )
    assert "web_search_preview" in exc.value.reason


def test_matrix_allows_codex_namespace_allowed_tools_choice_when_native():
    from src.protocols.matrix import extract_request_features

    body = {
        "input": "hi",
        "tool_choice": {
            "type": "allowed_tools",
            "tools": [{"type": "namespace", "name": "codex_app"}],
        },
    }
    features = extract_request_features("responses", body)

    assert features.hosted_tool_label == "tool_choice:allowed_tools:namespace"
    assert features.hosted_tool_labels == ("tool_choice:allowed_tools:namespace",)

    assert DEFAULT_MATRIX.plan(
        "responses", "openai-responses", features=features,
        capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset({"namespace"})),
    ).cost == 0


def test_matrix_allows_phase8_cross_family_bridges():
    a2c = DEFAULT_MATRIX.plan("anthropic", "openai-chat")
    assert a2c.cost == 1
    assert a2c.required_transforms == ["anthropic_to_chat"]

    a2r = DEFAULT_MATRIX.plan("anthropic", "openai-responses")
    assert a2r.cost == 1
    assert a2r.required_transforms == ["anthropic_to_responses"]

    c2a = DEFAULT_MATRIX.plan("chat", "anthropic")
    assert c2a.cost == 1
    assert c2a.required_transforms == ["chat_to_anthropic"]

    r2a = DEFAULT_MATRIX.plan("responses", "anthropic")
    assert r2a.cost == 1
    assert r2a.required_transforms == ["responses_to_anthropic"]


def test_matrix_allows_cross_family_request_controls_and_guards_content_state():
    from src.protocols.matrix import extract_request_features

    plan = DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features("responses", {"stream": True, "input": "hi"}),
    )
    assert plan.required_transforms == ["responses_to_anthropic"]

    plan = DEFAULT_MATRIX.plan(
        "chat", "anthropic",
        features=extract_request_features("chat", {"response_format": {"type": "json_schema"}, "messages": [], "store": True}),
    )
    assert plan.required_transforms == ["chat_to_anthropic"]


def test_matrix_routes_chat_custom_tool_call_history_by_input_shape():
    from src.protocols.matrix import extract_request_features

    safe_body = {"messages": [{"role": "assistant", "tool_calls": [{
        "id": "call_1",
        "type": "custom",
        "custom": {"name": "shell", "input": {"cmd": "pwd"}},
    }]}]}
    assert DEFAULT_MATRIX.plan(
        "chat", "anthropic", features=extract_request_features("chat", safe_body),
    ).required_transforms == ["chat_to_anthropic"]
    assert DEFAULT_MATRIX.plan(
        "chat", "openai-responses", features=extract_request_features("chat", safe_body),
    ).required_transforms == ["chat_to_responses"]

    raw_body = {"messages": [{"role": "assistant", "tool_calls": [{
        "id": "call_1",
        "type": "custom",
        "custom": {"name": "shell", "input": "raw input"},
    }]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat", "anthropic", features=extract_request_features("chat", raw_body),
        )
    assert DEFAULT_MATRIX.plan(
        "chat", "openai-responses", features=extract_request_features("chat", raw_body),
    ).required_transforms == ["chat_to_responses"]


def test_matrix_allows_chat_multi_candidate_native_only():
    from src.protocols.matrix import extract_request_features

    body = {"messages": [{"role": "user", "content": "hi"}], "n": 2}
    assert DEFAULT_MATRIX.plan(
        "chat",
        "openai-chat",
        features=extract_request_features("chat", body),
    ).cost == 0
    for upstream in ("openai-responses", "anthropic"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "chat",
                upstream,
                features=extract_request_features("chat", body),
            )


def test_matrix_allows_audio_native_only():
    from src.protocols.matrix import extract_request_features

    chat_audio_output = {
        "messages": [{"role": "user", "content": "say hi"}],
        "modalities": ["text", "audio"],
        "audio": {"voice": "alloy", "format": "wav"},
    }
    assert DEFAULT_MATRIX.plan(
        "chat",
        "openai-chat",
        features=extract_request_features("chat", chat_audio_output),
    ).cost == 0
    for upstream in ("openai-responses", "anthropic"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "chat",
                upstream,
                features=extract_request_features("chat", chat_audio_output),
            )

    chat_audio_input = {
        "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
        ]}],
    }
    assert DEFAULT_MATRIX.plan(
        "chat",
        "openai-responses",
        features=extract_request_features("chat", chat_audio_input),
    ).required_transforms == ["chat_to_responses"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat",
            "openai-responses",
            features=extract_request_features("chat", chat_audio_input),
            capabilities=ChannelCapabilities(protocol="openai-responses", native_state=frozenset()),
        )
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat",
            "anthropic",
            features=extract_request_features("chat", chat_audio_input),
        )

    responses_audio = {
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AAAA"}}]}],
    }
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-responses",
        features=extract_request_features("responses", responses_audio),
    ).cost == 0
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-chat",
        features=extract_request_features("responses", responses_audio),
    ).required_transforms == ["responses_to_chat"]
    codex_caps = ChannelCapabilities(
        protocol="openai-responses",
        native_state=frozenset({"encrypted_reasoning_replay", "prompt_cache_key"}),
    )
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses",
            "openai-responses",
            features=extract_request_features("responses", responses_audio),
            capabilities=codex_caps,
        )


def test_matrix_allows_locally_resolvable_item_reference_to_chat_and_anthropic():
    from src.protocols.matrix import extract_request_features

    local_ref = {
        "input": [
            {"type": "message", "id": "msg_1", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "item_reference", "id": "msg_1"},
        ],
    }
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-chat",
        features=extract_request_features("responses", local_ref),
    ).required_transforms == ["responses_to_chat"]
    assert DEFAULT_MATRIX.plan(
        "responses",
        "anthropic",
        features=extract_request_features("responses", local_ref),
    ).required_transforms == ["responses_to_anthropic"]

    anchored_ref = {
        "previous_response_id": "resp_prev",
        "input": [{"type": "item_reference", "id": "msg_prev"}],
    }
    assert DEFAULT_MATRIX.plan(
        "responses",
        "openai-chat",
        features=extract_request_features("responses", anchored_ref),
    ).required_transforms == ["responses_to_chat"]
    assert DEFAULT_MATRIX.plan(
        "responses",
        "anthropic",
        features=extract_request_features("responses", anchored_ref),
    ).required_transforms == ["responses_to_anthropic"]

    unresolved_ref = {"input": [{"type": "item_reference", "id": "missing"}]}
    for upstream in ("openai-chat", "anthropic"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "responses",
                upstream,
                features=extract_request_features("responses", unresolved_ref),
            )


def test_matrix_routes_responses_text_instruction_items_and_guards_unsafe_instruction_items():
    from src.protocols.matrix import extract_request_features

    text_instructions = {"instructions": [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "policy"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "background"}]},
    ], "input": "hi"}

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", text_instructions),
    ).required_transforms == ["responses_to_anthropic"]
    assert DEFAULT_MATRIX.plan(
        "responses", "openai-chat", features=extract_request_features("responses", text_instructions),
    ).required_transforms == ["responses_to_chat"]

    unsafe_content = {"instructions": [
        {"type": "message", "role": "user", "content": [{"type": "input_file", "file_url": "https://example.com/a.pdf"}]},
    ], "input": "hi"}
    for upstream in ("anthropic", "openai-chat"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "responses", upstream, features=extract_request_features("responses", unsafe_content),
            )

    unsafe_item = {"instructions": [
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
    ], "input": "hi"}
    for upstream in ("anthropic", "openai-chat"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "responses", upstream, features=extract_request_features("responses", unsafe_item),
            )


def test_matrix_allows_noop_text_format_cross_family():
    from src.protocols.matrix import extract_request_features

    chat_plan = DEFAULT_MATRIX.plan(
        "chat", "anthropic",
        features=extract_request_features("chat", {"response_format": {"type": "text"}, "messages": []}),
    )
    assert chat_plan.required_transforms == ["chat_to_anthropic"]

    resp_plan = DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features("responses", {"text": {"format": {"type": "text"}}, "input": "hi"}),
    )
    assert resp_plan.required_transforms == ["responses_to_anthropic"]


def test_matrix_allows_file_data_documents_to_anthropic_and_guards_references():
    from src.protocols.matrix import extract_request_features

    chat_body = {"messages": [{"role": "user", "content": [
        {"type": "file", "file": {"filename": "case.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="}},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "chat", "anthropic", features=extract_request_features("chat", chat_body),
    ).required_transforms == ["chat_to_anthropic"]

    resp_body = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_file", "filename": "case.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", resp_body),
    ).required_transforms == ["responses_to_anthropic"]

    resp_url_body = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_file", "filename": "remote.pdf", "file_url": "https://example.com/remote.pdf"},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", resp_url_body),
    ).required_transforms == ["responses_to_anthropic"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "openai-chat", features=extract_request_features("responses", resp_url_body),
        )

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "chat", "anthropic",
            features=extract_request_features(
                "chat",
                {"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "file_1"}}]}]},
            ),
        )

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "anthropic",
            features=extract_request_features("responses", {"input": [{"type": "input_file", "file_id": "file_1"}]}),
        )
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "anthropic",
            features=extract_request_features("responses", {"input": [{"type": "input_file", "file_url": "https://example.com/a.pdf"}]}),
        )


def test_matrix_routes_responses_tool_output_attachments_by_target_capability():
    from src.protocols.matrix import extract_request_features

    safe_tool_output = {"input": [{"type": "function_call_output", "call_id": "call_1", "output": [
        {"type": "input_text", "text": "see attached"},
        {"type": "input_image", "image_url": "https://example.com/a.png"},
        {"type": "input_file", "filename": "case.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjQ="},
    ]}]}

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", safe_tool_output),
    ).required_transforms == ["responses_to_anthropic"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "openai-chat", features=extract_request_features("responses", safe_tool_output),
        )

    file_url_output = {"input": [{"type": "function_call_output", "call_id": "call_1", "output": [
        {"type": "input_file", "file_url": "https://example.com/case.pdf"},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", file_url_output),
    ).required_transforms == ["responses_to_anthropic"]
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "openai-chat", features=extract_request_features("responses", file_url_output),
        )


def test_matrix_routes_responses_custom_tool_output_attachments_by_target_capability():
    from src.protocols.matrix import extract_request_features

    custom_tool_output = {"input": [
        {"type": "custom_tool_call", "call_id": "call_1", "name": "shell", "input": {"cmd": "pwd"}},
        {"type": "custom_tool_call_output", "call_id": "call_1", "output": [
            {"type": "input_text", "text": "see attached"},
            {"type": "input_file", "filename": "result.txt", "file_data": "data:text/plain;base64,b2s="},
        ]},
    ]}

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", custom_tool_output),
    ).required_transforms == ["responses_to_anthropic"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "openai-chat", features=extract_request_features("responses", custom_tool_output),
        )


def test_matrix_routes_anthropic_tool_result_attachments_by_target_capability():
    from src.protocols.matrix import extract_request_features

    safe_tool_result = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": [
            {"type": "text", "text": "see attached"},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
            {"type": "document", "source": {"type": "url", "url": "https://example.com/remote.pdf"}},
        ],
    }]}]}

    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", safe_tool_result),
    ).required_transforms == ["anthropic_to_responses"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "anthropic", "openai-chat", features=extract_request_features("anthropic", safe_tool_result),
        )

    error_tool_result = {"messages": [{"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "failed",
        "is_error": True,
    }]}]}
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", error_tool_result),
    ).required_transforms == ["anthropic_to_responses"]


def test_matrix_allows_anthropic_documents_to_openai_safe_subset():
    from src.protocols.matrix import extract_request_features

    base64_document = {"messages": [{"role": "user", "content": [
        {"type": "document", "title": "case.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
    ]}]}
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-chat", features=extract_request_features("anthropic", base64_document),
    ).required_transforms == ["anthropic_to_chat"]
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", base64_document),
    ).required_transforms == ["anthropic_to_responses"]

    url_document = {"messages": [{"role": "user", "content": [
        {"type": "document", "title": "case.pdf", "source": {"type": "url", "url": "https://example.com/case.pdf"}},
    ]}]}
    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "anthropic", "openai-chat", features=extract_request_features("anthropic", url_document),
        )
    assert DEFAULT_MATRIX.plan(
        "anthropic", "openai-responses", features=extract_request_features("anthropic", url_document),
    ).required_transforms == ["anthropic_to_responses"]

    cited_document = {"messages": [{"role": "user", "content": [
        {"type": "document", "citations": {"enabled": True}, "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}},
    ]}]}
    for upstream in ("openai-chat", "openai-responses"):
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "anthropic", upstream, features=extract_request_features("anthropic", cited_document),
            )


def test_matrix_guards_responses_stateful_file_audio_and_allows_request_controls():
    from src.protocols.matrix import extract_request_features

    control_bodies = [
        {"background": False, "input": "hi"},
        {"max_tool_calls": 1, "input": "hi"},
        {"input": "hi", "prompt": {"id": "pmpt_1"}},
        {"input": "hi", "truncation": "auto"},
        {"input": "hi", "include": ["unsupported.include"]},
        {"input": "hi", "include": ["reasoning.encrypted_content"]},
    ]

    for body in control_bodies:
        assert DEFAULT_MATRIX.plan(
            "responses", "anthropic",
            features=extract_request_features("responses", body),
        ).required_transforms == ["responses_to_anthropic"]

    unsafe_bodies = [
        {"conversation": "conv_1", "input": "hi"},
        {"background": True, "input": "hi"},
        {"input": [{"type": "item_reference", "id": "item_1"}]},
        {"input": [{"type": "reasoning", "encrypted_content": "gAAAA"}]},
        {"input": [{"type": "input_file", "file_id": "file_1"}]},
    ]

    for body in unsafe_bodies:
        with pytest.raises(ProtocolGuardError):
            DEFAULT_MATRIX.plan(
                "responses", "anthropic",
                features=extract_request_features("responses", body),
            )


@pytest.mark.parametrize("body", [
    {"messages": [], "verbosity": "high"},
    {"messages": [], "web_search_options": {"search_context_size": "low"}},
    {"messages": [], "service_tier": "flex"},
    {"messages": [], "prompt_cache_key": "cache-key"},
    {"messages": [], "prompt_cache_retention": "24h"},
    {"messages": [], "logit_bias": {"123": 10}},
    {"messages": [], "store": True},
])
def test_matrix_allows_chat_options_without_anthropic_equivalent(body):
    from src.protocols.matrix import extract_request_features

    plan = DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", body))
    assert plan.required_transforms == ["chat_to_anthropic"]


@pytest.mark.parametrize("body", [
    {"input": "hi", "service_tier": "flex"},
    {"input": "hi", "prompt_cache_key": "cache-key"},
    {"input": "hi", "prompt_cache_retention": "24h"},
])
def test_matrix_allows_responses_options_without_anthropic_equivalent(body):
    from src.protocols.matrix import extract_request_features

    plan = DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", body))
    assert plan.required_transforms == ["responses_to_anthropic"]


@pytest.mark.parametrize("body", [
    {"messages": [], "seed": 123},
    {"messages": [], "prediction": {"type": "content", "content": "hi"}},
    {"messages": [], "safety_identifier": "user-1"},
    {"messages": [], "service_tier": "auto"},
    {"messages": [], "service_tier": "default"},
])
def test_matrix_allows_safe_chat_control_fields_to_anthropic(body):
    from src.protocols.matrix import extract_request_features

    plan = DEFAULT_MATRIX.plan("chat", "anthropic", features=extract_request_features("chat", body))

    assert plan.required_transforms == ["chat_to_anthropic"]


@pytest.mark.parametrize("body", [
    {"input": "hi", "safety_identifier": "user-1"},
    {"input": "hi", "service_tier": "auto"},
    {"input": "hi", "service_tier": "default"},
])
def test_matrix_allows_safe_responses_control_fields_to_anthropic(body):
    from src.protocols.matrix import extract_request_features

    plan = DEFAULT_MATRIX.plan("responses", "anthropic", features=extract_request_features("responses", body))

    assert plan.required_transforms == ["responses_to_anthropic"]


def test_matrix_allows_internal_prompt_cache_hints_to_anthropic():
    from src.protocols.matrix import extract_request_features

    chat_body = {
        "messages": [],
        "prompt_cache_key": "internal",
        "prompt_cache_retention": "24h",
        "_client_body_fields": ["messages"],
    }
    assert DEFAULT_MATRIX.plan(
        "chat", "anthropic", features=extract_request_features("chat", chat_body),
    ).required_transforms == ["chat_to_anthropic"]

    resp_body = {
        "input": "hi",
        "prompt_cache_key": "internal",
        "prompt_cache_retention": "24h",
        "_client_body_fields": ["input"],
    }
    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic", features=extract_request_features("responses", resp_body),
    ).required_transforms == ["responses_to_anthropic"]

    assert DEFAULT_MATRIX.plan(
        "responses", "openai-chat",
        features=extract_request_features(
            "responses",
            {"input": "hi", "include": ["reasoning.encrypted_content"]},
        ),
    ).required_transforms == ["responses_to_chat"]
    assert DEFAULT_MATRIX.plan(
        "responses", "openai-chat",
        features=extract_request_features(
            "responses",
            {"input": [{"type": "reasoning", "encrypted_content": "gAAAA"}]},
        ),
    ).required_transforms == ["responses_to_chat"]

    assert DEFAULT_MATRIX.plan(
        "chat", "openai-responses",
        features=extract_request_features("chat", {"messages": [], "logprobs": True, "top_logprobs": 3}),
    ).required_transforms == ["chat_to_responses"]
    assert DEFAULT_MATRIX.plan(
        "responses", "openai-chat",
        features=extract_request_features(
            "responses",
            {"input": "hi", "include": ["message.output_text.logprobs"], "top_logprobs": 3},
        ),
    ).required_transforms == ["responses_to_chat"]

    for body in (
        {"input": "hi", "max_tool_calls": 1},
        {"input": "hi", "prompt": {"id": "pmpt_1"}},
        {"input": "hi", "truncation": "auto"},
        {"input": "hi", "include": ["unsupported.include"]},
    ):
        for upstream in ("openai-chat", "anthropic"):
            plan = DEFAULT_MATRIX.plan(
                "responses", upstream, features=extract_request_features("responses", body),
            )
            assert plan.required_transforms == [
                "responses_to_chat" if upstream == "openai-chat" else "responses_to_anthropic"
            ]

    for body in (
        {"messages": [], "stop": ["END"]},
        {"messages": [], "seed": 123},
        {"messages": [], "prediction": {"type": "content", "content": "hi"}},
        {"messages": [], "logit_bias": {"123": 10}},
        {"messages": [], "frequency_penalty": 0.2},
        {"messages": [], "presence_penalty": 0.2},
    ):
        plan = DEFAULT_MATRIX.plan(
            "chat", "openai-responses", features=extract_request_features("chat", body),
        )
        assert plan.required_transforms == ["chat_to_responses"]

    for upstream in ("openai-chat", "openai-responses"):
        plan = DEFAULT_MATRIX.plan(
            "anthropic", upstream,
            features=extract_request_features(
                "anthropic",
                {"messages": [], "thinking": {"type": "disabled"}},
            ),
        )
        assert plan.required_transforms == ["anthropic_to_chat" if upstream == "openai-chat" else "anthropic_to_responses"]

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features(
            "responses",
            {"input": "hi", "tools": [{"type": "custom", "name": "shell"}]},
        ),
    ).required_transforms == ["responses_to_anthropic"]

    assert DEFAULT_MATRIX.plan(
        "responses", "anthropic",
        features=extract_request_features(
            "responses",
            {"input": [
                {"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": {"cmd": "pwd"}},
                {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"},
            ]},
        ),
    ).required_transforms == ["responses_to_anthropic"]

    with pytest.raises(ProtocolGuardError):
        DEFAULT_MATRIX.plan(
            "responses", "anthropic",
            features=extract_request_features(
                "responses",
                {"input": [{"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": "raw text"}]},
            ),
        )
