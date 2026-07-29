"""Provider adapter tests for Protocol Runtime Phase 5."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.providers import registry


class FakeChannel:
    def __init__(self, *, protocol="anthropic", type="api", cc_mimicry=False, provider=""):
        self.protocol = protocol
        self.type = type
        self.cc_mimicry = cc_mimicry
        self.provider = provider
        self.calls = []

    async def restore_response(self, chunk: bytes, dynamic_map=None) -> bytes:
        self.calls.append((chunk, dynamic_map))
        if dynamic_map:
            return chunk + b":" + str(dynamic_map.get("tool", "?")).encode()
        return chunk


def test_adapter_selection_matches_existing_channel_kinds():
    assert registry.adapter_for_channel(FakeChannel(protocol="anthropic", type="api", cc_mimicry=False)).name == "anthropic-standard"
    assert registry.adapter_for_channel(FakeChannel(protocol="anthropic", type="api", cc_mimicry=True)).name == "cc-mimicry"
    assert registry.adapter_for_channel(FakeChannel(protocol="anthropic", type="oauth", cc_mimicry=True)).name == "anthropic-oauth"
    assert registry.adapter_for_channel(FakeChannel(protocol="openai-chat", type="api", cc_mimicry=False)).name == "openai-api"
    assert registry.adapter_for_channel(FakeChannel(protocol="openai-responses", type="oauth", cc_mimicry=False)).name == "openai-codex"
    assert registry.adapter_for_channel(FakeChannel(protocol="openai-responses", type="oauth", provider="xai")).name == "xai-oauth"


def test_provider_capabilities_expose_protocols_and_state_boundaries():
    api = registry.capabilities_for_channel(FakeChannel(protocol="openai-chat", type="api"))
    assert api.adapter_name == "openai-api"
    assert api.protocols == frozenset({"openai-chat", "openai-responses"})
    assert "previous_response_id" in api.native_state
    assert "conversation" in api.native_state
    assert "file_id" in api.native_state
    assert "audio" in api.native_state

    xai = registry.capabilities_for_channel(FakeChannel(protocol="openai-responses", type="oauth", provider="xai"))
    assert xai.adapter_name == "xai-oauth"
    assert xai.protocols == frozenset({"openai-responses"})
    assert "prompt_cache_key" in xai.native_state
    assert "encrypted_reasoning_replay" in xai.native_state
    assert "web_search" in xai.native_state
    assert "tool_search" not in xai.native_state
    assert "namespace" not in xai.native_state
    assert "ws" not in xai.transports

    codex = registry.capabilities_for_channel(FakeChannel(protocol="openai-responses", type="oauth"))
    assert codex.adapter_name == "openai-codex"
    assert codex.protocols == frozenset({"openai-responses"})
    assert "encrypted_reasoning_replay" in codex.native_state
    assert "item_reference" in codex.native_state
    assert "tool_search" in codex.native_state
    assert "hosted_tools" not in codex.native_state
    assert "file_id" not in codex.native_state
    assert "audio" not in codex.native_state
    assert "conversation" in codex.redlines


def test_provider_request_payload_filter_uses_target_allowlists():
    chat = FakeChannel(protocol="openai-chat", type="api")
    filtered = registry.filter_request_payload(
        chat,
        {
            "model": "m",
            "messages": [],
            "previous_response_id": "resp_bad",
            "_api_key_name": "internal",
            "seed": 1,
        },
        protocol="openai-chat",
    )
    assert filtered == {"model": "m", "messages": [], "seed": 1}

    responses = FakeChannel(protocol="openai-responses", type="oauth")
    filtered = registry.filter_request_payload(
        responses,
        {
            "model": "m",
            "input": "hi",
            "client_metadata": {"turn": "1"},
            "background": True,
            "_api_key_name": "internal",
        },
        protocol="openai-responses",
    )
    assert filtered == {"model": "m", "input": "hi", "client_metadata": {"turn": "1"}, "background": True}


def test_legacy_transform_filters_share_provider_allowlists():
    from src.openai.transform import common

    chat = common.filter_chat_passthrough({
        "model": "m",
        "messages": [],
        "seed": 1,
        "previous_response_id": "resp_bad",
        "_api_key_name": "internal",
    })
    assert chat == {"model": "m", "messages": [], "seed": 1}

    responses = common.filter_responses_passthrough({
        "model": "m",
        "input": "hi",
        "client_metadata": {"turn": "1"},
        "_api_key_name": "internal",
    })
    assert responses == {"model": "m", "input": "hi", "client_metadata": {"turn": "1"}}

    bridge = common.filter_anthropic_bridge_payload({
        "model": "claude",
        "messages": [],
        "thinking": {"type": "enabled"},
        "context_management": {"edits": []},
    })
    assert bridge == {"model": "claude", "messages": []}


def test_anthropic_bridge_filter_is_provider_capability_not_source_validator():
    anthropic = FakeChannel(protocol="anthropic", type="api", cc_mimicry=True)
    filtered = registry.filter_request_payload(
        anthropic,
        {
            "model": "claude",
            "messages": [],
            "system": "ok",
            "thinking": {"type": "enabled"},
            "context_management": {"edits": []},
        },
        protocol="anthropic",
        bridge=True,
    )
    assert filtered == {"model": "claude", "messages": [], "system": "ok"}


def test_anthropic_native_filter_keeps_official_fields_and_drops_foreign_hints():
    anthropic = FakeChannel(protocol="anthropic", type="api", cc_mimicry=False)
    filtered = registry.filter_request_payload(
        anthropic,
        {
            "model": "claude",
            "messages": [],
            "service_tier": "auto",
            "speed": "fast",
            "container": {"id": "ctr_1"},
            "mcp_servers": [{"name": "tools"}],
            "_parrot_downstream_betas": ["fast-mode-2026-02-01"],
            "_parrot_wants_fast_mode": True,
            "prompt_cache_key": "openai-only",
            "response_format": {"type": "json_schema"},
            "_api_key_name": "internal",
        },
        protocol="anthropic",
    )

    assert filtered == {
        "model": "claude",
        "messages": [],
        "service_tier": "auto",
        "speed": "fast",
        "container": {"id": "ctr_1"},
        "mcp_servers": [{"name": "tools"}],
        "_parrot_downstream_betas": ["fast-mode-2026-02-01"],
        "_parrot_wants_fast_mode": True,
    }


@pytest.mark.asyncio
async def test_anthropic_api_channel_emits_fast_mode_header_and_body():
    from src.channel.api_channel import ApiChannel
    from src.transform import cc_mimicry

    for cc_enabled in (False, True):
        ch = ApiChannel({
            "name": f"anthropic-fast-{cc_enabled}",
            "baseUrl": "https://example.test",
            "apiKey": "sk-test",
            "models": [],
            "cc_mimicry": cc_enabled,
        })
        req = await ch.build_upstream_request({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY: [cc_mimicry.FAST_MODE_BETA],
            cc_mimicry.PARROT_WANTS_FAST_MODE_KEY: True,
        }, "claude-sonnet-4-6")
        payload = json.loads(req.body.decode("utf-8"))
        assert payload["speed"] == "fast"
        assert cc_mimicry.FAST_MODE_BETA in req.headers["anthropic-beta"].split(",")


@pytest.mark.asyncio
async def test_restore_response_delegates_with_per_attempt_dynamic_map():
    ch = FakeChannel(protocol="anthropic", type="api", cc_mimicry=True)

    out1 = await registry.restore_response_bytes(ch, b"chunk", dynamic_map={"tool": "a"})
    out2 = await registry.restore_response_bytes(ch, b"chunk", dynamic_map={"tool": "b"})

    assert out1 == b"chunk:a"
    assert out2 == b"chunk:b"
    assert ch.calls == [(b"chunk", {"tool": "a"}), (b"chunk", {"tool": "b"})]

class _NoopFeed:
    def __init__(self):
        self.seen = []

    def feed(self, chunk: bytes) -> None:
        self.seen.append(chunk)


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_precommit_restore_receives_translator_ctx(monkeypatch):
    import time
    from src import failover

    calls = []
    translator_ctx = {"response_translator": "anthropic_to_responses", "trace": "ctx-1"}
    dynamic_map = {"tool": "mapped"}

    async def fake_restore(channel, chunk, *, dynamic_map=None, translator_ctx=None):
        calls.append((chunk, dynamic_map, translator_ctx))
        return chunk

    monkeypatch.setattr(failover.provider_registry, "restore_response_bytes", fake_restore)

    first = (
        b"event: response.created\n"
        b"data: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_1\"}}\n\n"
    )
    visible = (
        b"event: response.output_text.delta\n"
        b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n"
    )

    chunks, err = await failover._read_until_first_downstream_chunk(
        _AsyncChunks([visible]),
        SimpleNamespace(protocol="openai-responses"),
        dynamic_map,
        _NoopFeed(),
        _NoopFeed(),
        time.time() + 5,
        1,
        protocol="openai-responses",
        first_chunk=first,
        translator_ctx=translator_ctx,
    )

    assert err is None
    assert chunks == [first, visible]
    assert calls == [
        (first, dynamic_map, translator_ctx),
        (visible, dynamic_map, translator_ctx),
    ]
