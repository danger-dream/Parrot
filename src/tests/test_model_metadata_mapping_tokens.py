from __future__ import annotations

from ._isolation import isolate

isolate()

from src import compact_rescue, config, model_mapping, model_metadata, token_counter  # noqa: E402


class _DummyChannel:
    protocol = "openai-responses"


class _DummyScheduleResult:
    candidates = [(_DummyChannel(), "gpt-5.5")]
    saturated = []


def test_model_metadata_safe_limit_and_single_compression_model():
    config.update(lambda c: c.__setitem__("modelMetadata", {}))

    model_metadata.set_metadata(
        "gpt-5.5",
        {
            "context": "273000",
            "max_output": "64000",
            "vision": "false",
            "input_price": "1.25",
            "compression": "true",
            "reasoning": " LOW， high 、XHIGH; max ",
            "default_reasoning": "xHIGH",
        },
    )
    assert model_metadata.context_window("gpt-5.5") == 273000
    # Claude Code-compatible reserve: min(max_output, 20k) + 20k buffer.
    assert model_metadata.safe_prompt_limit("gpt-5.5") == 273000 - 20000 - 20000
    assert model_metadata.get_compression_model() == "gpt-5.5"
    meta = model_metadata.get_metadata("gpt-5.5")
    assert meta["reasoningEfforts"] == ["low", "high", "xhigh", "max"]
    assert meta["defaultReasoningEffort"] == "xhigh"

    model_metadata.set_metadata("deepseek-4v-pro", {"context": 1_000_000, "compression": True})
    assert model_metadata.get_compression_model() == "deepseek-4v-pro"
    assert model_metadata.get_metadata("gpt-5.5").get("compressionModel") is False


def test_model_metadata_default_reasoning_must_be_supported():
    config.update(lambda c: c.__setitem__("modelMetadata", {}))
    try:
        model_metadata.set_metadata("bad", {"reasoning": "low,high", "default_reasoning": "xhigh"})
    except ValueError as exc:
        assert "default reasoning effort" in str(exc)
    else:
        raise AssertionError("expected invalid default reasoning effort")


def test_global_model_mapping_overrides_legacy_ingress_mapping():
    config.update(
        lambda c: c.__setitem__(
            "modelMapping",
            {
                "anthropic": {"alias": "legacy-real"},
                "global": {"alias": "global-real", "any": "model"},
            },
        )
    )

    body = {"model": "alias"}
    assert model_mapping.apply_mapping(body, "anthropic") == ("alias", "global-real")
    assert body["model"] == "global-real"

    body = {"model": "any"}
    assert model_mapping.apply_mapping(body, "openai-chat") == ("any", "model")
    assert body["model"] == "model"


def test_token_counter_counts_payload_tokens_not_raw_message_count():
    body = {
        "system": "你是测试助手",
        "messages": [
            {"role": "user", "content": "你好" * 100},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ],
        "tools": [{"name": "read", "description": "读取文件", "input_schema": {"type": "object"}}],
    }
    count = token_counter.count_request_tokens(body, model="gpt-5.5")
    assert count > len(body["messages"])


def test_token_counter_counts_responses_input_string():
    count = token_counter.count_request_tokens({"input": "hello " * 100}, model="gpt-5.5")
    assert count > 10


def test_tiktoken_model_name_normalization_for_parrot_names():
    assert token_counter._normalized_tiktoken_model_name("gpt") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("gpt-5.5") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("channel/account:gpt-5.5") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("gpt-4o") == "gpt-4o"
    assert token_counter._encoding_for_model("gpt-5.5").name == "o200k_base"
    assert token_counter._normalized_tiktoken_model_name("deepseek-v4-pro") is None
    assert token_counter._encoding_for_model("deepseek-v4-pro").name == "o200k_base"


def test_unknown_models_fallback_to_gpt5_tiktoken_not_bytes():
    text = "用户请求：请继续。" * 1000
    token_count = token_counter.count_text_tokens(text, model="deepseek-v4-pro")
    byte_estimate = (len(text.encode("utf-8")) + 2) // 3
    assert token_count < byte_estimate
    assert token_count == token_counter.count_text_tokens(text, model="gpt-5")


def test_image_base64_is_not_counted_as_text_tokens():
    body = {
        "model": "deepseek-v4-pro",
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * 500_000,
                },
            }],
        }],
    }
    assert token_counter.count_request_tokens(body, model="deepseek-v4-pro") < 100


def test_deepseek_compression_model_can_fit_replayed_compact_shape():
    config.update(
        lambda c: c.__setitem__(
            "modelMetadata",
            {"deepseek-v4-pro": {"contextWindow": 700_000, "maxOutputTokens": 100_000, "compressionModel": True}},
        )
    )
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "用户请求：请继续。" * 55_000}],
    }
    direct_body, _ = compact_rescue.sanitized_compact_base(body)
    direct_body["model"] = "deepseek-v4-pro"
    direct_body["stream"] = False
    direct_body["max_tokens"] = model_metadata.summary_reserve_tokens("deepseek-v4-pro")
    prompt_tokens = token_counter.count_request_tokens(direct_body, model="deepseek-v4-pro")
    assert prompt_tokens < 660_000
    assert model_metadata.can_fit_for_compact("deepseek-v4-pro", prompt_tokens)


def test_compact_split_defaults_to_configured_token_chunks(monkeypatch):
    messages = [
        {"role": "user", "content": f"message {i}"}
        for i in range(5)
    ]

    monkeypatch.setattr(
        compact_rescue.token_counter,
        "count_message_tokens",
        lambda msg, model=None: 60_000,
    )

    config.update(lambda c: c.__setitem__("compactRescue", {"chunkTargetTokens": 120_000}))
    chunks = compact_rescue.split_messages_for_compact(messages)
    assert [len(c) for c in chunks] == [2, 2, 1]

    config.update(lambda c: c.__setitem__("compactRescue", {"chunkTargetTokens": 60_000}))
    chunks = compact_rescue.split_messages_for_compact(messages)
    assert [len(c) for c in chunks] == [1, 1, 1, 1, 1]


def test_compact_split_keeps_explicit_char_override(monkeypatch):
    messages = [
        {"role": "user", "content": "x" * 50},
        {"role": "assistant", "content": "y" * 50},
        {"role": "user", "content": "z" * 50},
    ]
    monkeypatch.setattr(
        compact_rescue.token_counter,
        "count_message_tokens",
        lambda msg, model=None: 1_000_000,
    )

    chunks = compact_rescue.split_messages_for_compact(messages, target_chars=10_000)
    assert len(chunks) == 1


def test_internal_compact_bodies_cap_output_budget():
    original = {
        "model": "gpt-5.5",
        "max_tokens": 64_000,
        "max_output_tokens": 64_000,
        "messages": [{"role": "user", "content": "hello"}],
    }

    segment = compact_rescue.build_segment_summary_body(
        original,
        original["messages"],
        segment_index=1,
        segment_count=1,
    )
    reduce = compact_rescue.build_reduce_summary_body(original, ["summary"])

    assert segment["max_tokens"] == compact_rescue.reduce_max_tokens()
    assert "max_output_tokens" not in segment
    assert reduce["max_tokens"] == compact_rescue.reduce_max_tokens()
    assert "max_output_tokens" not in reduce


def test_context_guard_skips_claude_code_compact_requests():
    from server import _anthropic_to_openai_context_preflight

    config.update(
        lambda c: c.__setitem__(
            "modelMetadata",
            {"gpt-5.5": {"contextWindow": 273_000, "maxOutputTokens": 20_000}},
        )
    )
    compact_prompt = (
        "CRITICAL: Respond with text only. Create a detailed summary of the conversation so far. "
        "Your summary should include the following sections. After compaction continue."
    )
    huge_text = ("用户请求：请继续修改文件 /opt/project/src/main.py，并保留所有错误、路径、命令。\n" * 260_000)
    compact_body = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "user", "content": huge_text},
            {"role": "user", "content": compact_prompt},
        ],
    }
    normal_body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": huge_text}],
    }

    assert compact_rescue.is_claude_code_compact_request(compact_body)
    assert _anthropic_to_openai_context_preflight(compact_body, _DummyScheduleResult()) is None
    assert _anthropic_to_openai_context_preflight(normal_body, _DummyScheduleResult()) is not None
