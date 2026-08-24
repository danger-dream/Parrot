from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from ._isolation import isolate

isolate()

from src import compact_rescue, config, model_mapping, model_metadata, token_counter  # noqa: E402


class _DummyChannel:
    protocol = "openai-responses"


class _DummyScheduleResult:
    candidates = [(_DummyChannel(), "gpt-5.5")]
    saturated = []


class _FakeEncoding:
    name = "o200k_base"

    @staticmethod
    def encode(text: str):
        # Deterministic token-like result; importantly distinct from bytes/3.
        return range((len(text) + 1) // 2)


def _install_fake_tiktoken(monkeypatch, request):
    calls: list[str] = []

    def encoding_for_model(model: str):
        calls.append(model)
        return _FakeEncoding()

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(
        encoding_for_model=encoding_for_model,
        get_encoding=lambda name: _FakeEncoding(),
    ))
    token_counter._encoding_for_model.cache_clear()
    request.addfinalizer(token_counter._encoding_for_model.cache_clear)
    return calls


def test_model_metadata_safe_limit_and_independent_compression_model():
    config.update(lambda c: c.update({
        "modelBindings": {"defaults": {}, "scoped": {}},
        "compressionModel": "",
        "modelMetadata": {},
    }))
    model_metadata.set_binding("gpt-5.5", "openai/gpt-5.5", source="test")
    model_metadata.set_compression_model("gpt-5.5")

    metadata = model_metadata.get_metadata("gpt-5.5")
    assert model_metadata.context_window("gpt-5.5") == metadata["contextWindow"]
    assert model_metadata.compact_trigger_tokens("gpt-5.5") == metadata["compactTriggerTokens"]
    expected_limit = min(
        metadata["compactTriggerTokens"],
        metadata["contextWindow"] - 20000 - 20000,
    )
    assert model_metadata.safe_prompt_limit("gpt-5.5") == expected_limit
    assert model_metadata.can_fit_for_compact("gpt-5.5", expected_limit)
    assert not model_metadata.can_fit_for_compact("gpt-5.5", expected_limit + 1)
    assert model_metadata.get_compression_model() == "gpt-5.5"
    assert "inputPricePer1M" not in metadata
    assert isinstance(metadata["cost"], dict)

    # Compact selection no longer mutates metadata/binding records.
    model_metadata.set_compression_model("deepseek-4v-pro")
    assert model_metadata.get_compression_model() == "deepseek-4v-pro"
    assert model_metadata.resolve_binding("gpt-5.5").target == "openai/gpt-5.5"


def test_model_metadata_default_reasoning_must_be_supported():
    config.update(lambda c: c.__setitem__("modelMetadata", {}))
    try:
        model_metadata.set_metadata("bad", {"reasoning": "low,high", "default_reasoning": "xhigh"})
    except ValueError as exc:
        assert "default reasoning effort" in str(exc)
    else:
        raise AssertionError("expected invalid default reasoning effort")


@pytest.fixture
def _restore_model_mapping(request):
    before = copy.deepcopy(config.get().get("modelMapping"))
    request.addfinalizer(
        lambda: config.update(
            lambda c: c.__setitem__("modelMapping", copy.deepcopy(before))
        )
    )


def test_global_model_mapping_overrides_legacy_ingress_mapping(_restore_model_mapping):
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


def test_strict_global_map_excludes_legacy_ingress_aliases(_restore_model_mapping):
    config.update(
        lambda c: c.__setitem__(
            "modelMapping",
            {
                "global": {"shared": "real-shared"},
                "anthropic": {"claude": "claude-real"},
                "openai-chat": {"chat-only": "chat-real"},
            },
        )
    )

    assert model_mapping.get_global_map() == {"shared": "real-shared"}
    # 统一管理 UI 仍能看到旧条目并迁移/删除；这不代表它们会被 /v1/models 暴露。
    assert model_mapping.get_ingress_map(model_mapping.GLOBAL_MAPPING_LINE) == {
        "claude": "claude-real",
        "chat-only": "chat-real",
        "shared": "real-shared",
    }


def test_global_mapping_delete_cascades_same_alias_from_legacy_lines(
    _restore_model_mapping,
):
    config.update(
        lambda c: c.__setitem__(
            "modelMapping",
            {
                "global": {"stale": "global-real", "keep": "keep-real"},
                "anthropic": {"stale": "anthropic-real", "a": "a-real"},
                "openai-chat": {"stale": "chat-real"},
                "openai-responses": {"stale": "responses-real"},
            },
        )
    )

    assert model_mapping.remove_mapping(model_mapping.GLOBAL_MAPPING_LINE, "stale")
    root = config.get()["modelMapping"]
    assert all("stale" not in (root.get(line) or {}) for line in (
        model_mapping.GLOBAL_MAPPING_LINE, *model_mapping.INGRESS_LINES,
    ))
    assert root["global"]["keep"] == "keep-real"
    assert root["anthropic"]["a"] == "a-real"


def test_setting_global_mapping_removes_legacy_duplicate(_restore_model_mapping):
    config.update(
        lambda c: c.__setitem__(
            "modelMapping",
            {
                "anthropic": {"move": "old-anthropic"},
                "openai-chat": {"move": "old-chat"},
            },
        )
    )

    model_mapping.set_mapping(model_mapping.GLOBAL_MAPPING_LINE, "move", "new-global")
    root = config.get()["modelMapping"]
    assert root["global"]["move"] == "new-global"
    assert "move" not in root["anthropic"]
    assert "move" not in root["openai-chat"]


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


def test_tiktoken_model_name_normalization_for_parrot_names(monkeypatch, request):
    calls = _install_fake_tiktoken(monkeypatch, request)
    assert token_counter._normalized_tiktoken_model_name("gpt") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("gpt-5.5") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("channel/account:gpt-5.5") == "gpt-5"
    assert token_counter._normalized_tiktoken_model_name("gpt-4o") == "gpt-4o"
    assert token_counter._encoding_for_model("gpt-5.5").name == "o200k_base"
    assert token_counter._normalized_tiktoken_model_name("deepseek-v4-pro") is None
    assert token_counter._encoding_for_model("deepseek-v4-pro").name == "o200k_base"
    assert calls == ["gpt-5", "gpt-5"]


def test_unknown_models_fallback_to_gpt5_tiktoken_not_bytes(monkeypatch, request):
    calls = _install_fake_tiktoken(monkeypatch, request)
    text = "用户请求：请继续。" * 1000
    token_count = token_counter.count_text_tokens(text, model="deepseek-v4-pro")
    byte_estimate = (len(text.encode("utf-8")) + 2) // 3
    assert token_count < byte_estimate
    assert token_count == token_counter.count_text_tokens(text, model="gpt-5")
    assert calls == ["gpt-5", "gpt-5"]


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


def test_bound_compression_model_respects_compact_trigger_for_replayed_shape():
    config.update(lambda c: c.update({
        "modelBindings": {
            "defaults": {"gpt-5.5": {"target": "openai/gpt-5.5", "source": "test"}},
            "scoped": {},
        },
        "compressionModel": "gpt-5.5",
    }))
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "用户请求：请继续。" * 55_000}],
    }
    direct_body, _ = compact_rescue.sanitized_compact_base(body)
    direct_body["model"] = "gpt-5.5"
    direct_body["stream"] = False
    direct_body["max_tokens"] = model_metadata.summary_reserve_tokens("gpt-5.5")
    prompt_tokens = token_counter.count_request_tokens(direct_body, model="gpt-5.5")
    assert prompt_tokens < model_metadata.context_window("gpt-5.5")
    assert prompt_tokens > model_metadata.compact_trigger_tokens("gpt-5.5")
    assert not model_metadata.can_fit_for_compact("gpt-5.5", prompt_tokens)


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


@pytest.mark.asyncio
async def test_anthropic_http_mapping_binds_final_logical_model(monkeypatch):
    import server
    from src import model_pricing
    from starlette.responses import JSONResponse

    external_alias = "grok-4.5"
    logical_model = "routed-logical-model"
    outbound_model = "vendor-outbound-model"
    config.update(lambda cfg: cfg.update({
        "modelMapping": {"global": {external_alias: logical_model}},
        "modelBindings": {
            "defaults": {
                logical_model: {"target": "openai/gpt-5.4", "source": "test"},
            },
            "scoped": {},
        },
        "modelMetadata": {},
    }))
    model_pricing.reset_for_tests()
    model_pricing.initialize()

    class _Request:
        headers = {"x-api-key": "test-key"}
        client = SimpleNamespace(host="127.0.0.1")

        async def body(self):
            return json.dumps({
                # Anthropic uniquely strips this marker and maps a second time.
                "model": f"{external_alias}[1m]",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "ping"}],
            }).encode("utf-8")

    channel = SimpleNamespace(
        key="api:binding", type="api", protocol="openai-responses",
    )
    route = SimpleNamespace(
        candidates=[(channel, outbound_model)], saturated=[],
        affinity_hit=False,
    )
    captured = {}

    monkeypatch.setattr(server.auth, "validate", lambda headers: ("test", [], None))
    monkeypatch.setattr(server.log_db, "insert_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.scheduler, "schedule", lambda *args, **kwargs: route)
    original_safe_prompt_limit = server.model_metadata.safe_prompt_limit

    def _capture_safe_prompt_limit(model, **kwargs):
        captured["preflight_model"] = model
        return original_safe_prompt_limit(model, **kwargs)

    monkeypatch.setattr(
        server.model_metadata, "safe_prompt_limit", _capture_safe_prompt_limit,
    )

    async def _identity_translate(body, **kwargs):
        return body

    async def _capture_failover(_route, body, *args, **kwargs):
        captured["body"] = body
        captured["binding"] = model_pricing.build_pricing_binding(
            channel_key=channel.key,
            channel_type=channel.type,
            upstream_protocol=channel.protocol,
            outbound_model_id=outbound_model,
            client_visible_model=body["_client_visible_model"],
        )
        return JSONResponse({"ok": True})

    monkeypatch.setattr(server.translation, "translate_body", _identity_translate)
    monkeypatch.setattr(server.failover, "run_failover", _capture_failover)

    response = await server.proxy_messages(_Request())
    assert response.status_code == 200
    assert captured["body"]["model"] == logical_model
    assert captured["body"]["_client_visible_model"] == logical_model
    assert captured["preflight_model"] == logical_model
    binding = captured["binding"]
    assert binding.client_visible_model == logical_model
    assert binding.outbound_model_id == outbound_model
    assert binding.binding_source == "metadata_default"
    assert binding.pricing_key == "openai/gpt-5.4"
    assert binding.pricing_key != model_pricing.canonical_official_model(external_alias)
    assert binding.tariff is not None


def test_context_guard_uses_compact_trigger_and_skips_compact_requests(monkeypatch):
    import server

    config.update(lambda c: c.update({
        "modelBindings": {
            "defaults": {"gpt-5.5": {"target": "openai/gpt-5.5", "source": "test"}},
            "scoped": {},
        },
        "modelMetadata": {},
    }))
    trigger = model_metadata.compact_trigger_tokens("gpt-5.5")
    assert trigger is not None and trigger < model_metadata.context_window("gpt-5.5")
    monkeypatch.setattr(
        server.token_counter, "count_request_tokens",
        lambda body, model=None: trigger + 1,
    )
    compact_prompt = (
        "CRITICAL: Respond with text only. Create a detailed summary of the conversation so far. "
        "Your summary should include the following sections. After compaction continue."
    )
    compact_body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": compact_prompt}],
    }
    normal_body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "继续处理当前任务"}],
    }

    assert compact_rescue.is_claude_code_compact_request(compact_body)
    assert server._anthropic_to_openai_context_preflight(
        compact_body, _DummyScheduleResult(),
    ) is None
    assert server._anthropic_to_openai_context_preflight(
        normal_body, _DummyScheduleResult(),
    ) is not None
