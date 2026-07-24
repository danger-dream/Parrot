"""渠道兼容策略：自动透传、按真实模型强制 1M/Fast、最终字段剔除。"""

from __future__ import annotations

import json
import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()

import pytest

from src import failover
from src.channel.api_channel import ApiChannel
from src.openai.channel.api_channel import OpenAIApiChannel
from src.transform import cc_mimicry


def _anthropic_channel(**overrides) -> ApiChannel:
    entry = {
        "name": "anthropic-compat",
        "type": "api",
        "baseUrl": "https://example.com",
        "apiKey": "sk-test",
        "protocol": "anthropic",
        "models": [
            {"real": "claude-fable-5", "alias": "fable"},
            {"real": "claude-haiku-4-5-20251001", "alias": "haiku"},
        ],
        "cc_mimicry": True,
        "enabled": True,
    }
    entry.update(overrides)
    return ApiChannel(entry)


def _openai_channel(protocol: str = "openai-chat", **overrides) -> OpenAIApiChannel:
    entry = {
        "name": "openai-compat",
        "type": "api",
        "baseUrl": "https://example.com/v1",
        "apiKey": "sk-test",
        "protocol": protocol,
        "models": [
            {"real": "gpt-fast", "alias": "fast"},
            {"real": "gpt-normal", "alias": "normal"},
        ],
        "enabled": True,
    }
    entry.update(overrides)
    return OpenAIApiChannel(entry)


def _anthropic_body(**overrides) -> dict:
    body = {
        "model": "fable",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "stream": False,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_anthropic_auto_is_passthrough_and_unknown_model_1m_is_not_filtered():
    ch = _anthropic_channel(context1mMode="auto")

    plain = await ch.build_upstream_request(
        _anthropic_body(), "claude-fable-5", ingress_protocol="anthropic",
    )
    assert cc_mimicry.CONTEXT_1M_BETA not in plain.headers["anthropic-beta"].split(",")

    explicit = await ch.build_upstream_request(
        _anthropic_body(
            **{cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY: [cc_mimicry.CONTEXT_1M_BETA]}
        ),
        "claude-fable-5",
        ingress_protocol="anthropic",
    )
    assert cc_mimicry.CONTEXT_1M_BETA in explicit.headers["anthropic-beta"].split(",")


@pytest.mark.asyncio
async def test_anthropic_force_1m_and_fast_respect_real_model_scope():
    ch = _anthropic_channel(
        context1mMode="force",
        context1mModels=["claude-fable-5"],
        fastMode="force",
        fastModels=["claude-fable-5"],
    )

    forced = await ch.build_upstream_request(
        _anthropic_body(), "claude-fable-5", ingress_protocol="anthropic",
    )
    forced_payload = json.loads(forced.body)
    forced_betas = forced.headers["anthropic-beta"].split(",")
    assert cc_mimicry.CONTEXT_1M_BETA in forced_betas
    assert cc_mimicry.FAST_MODE_BETA in forced_betas
    assert forced_payload["speed"] == "fast"

    automatic = await ch.build_upstream_request(
        _anthropic_body(model="haiku"),
        "claude-haiku-4-5-20251001",
        ingress_protocol="anthropic",
    )
    automatic_payload = json.loads(automatic.body)
    automatic_betas = automatic.headers["anthropic-beta"].split(",")
    assert cc_mimicry.CONTEXT_1M_BETA not in automatic_betas
    assert cc_mimicry.FAST_MODE_BETA not in automatic_betas
    assert "speed" not in automatic_payload


@pytest.mark.asyncio
async def test_anthropic_force_all_models_and_auto_fast_header_passthrough():
    forced_all = _anthropic_channel(context1mMode="force", context1mModels=[])
    req = await forced_all.build_upstream_request(
        _anthropic_body(model="haiku"),
        "claude-haiku-4-5-20251001",
        ingress_protocol="anthropic",
    )
    assert cc_mimicry.CONTEXT_1M_BETA in req.headers["anthropic-beta"].split(",")

    auto_fast = _anthropic_channel(fastMode="auto")
    req = await auto_fast.build_upstream_request(
        _anthropic_body(
            **{cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY: [cc_mimicry.FAST_MODE_BETA]}
        ),
        "claude-fable-5",
        ingress_protocol="anthropic",
    )
    assert cc_mimicry.FAST_MODE_BETA in req.headers["anthropic-beta"].split(",")
    assert json.loads(req.body)["speed"] == "fast"


@pytest.mark.asyncio
async def test_anthropic_standard_path_applies_force_and_omit_consistently():
    ch = _anthropic_channel(
        cc_mimicry=False,
        context1mMode="force",
        context1mModels=["claude-fable-5"],
        fastMode="force",
        fastModels=["claude-fable-5"],
        omitTemperature=True,
        omitThinking=True,
    )
    req = await ch.build_upstream_request(
        _anthropic_body(
            temperature=0.4,
            thinking={"type": "enabled", "budget_tokens": 1024},
            **{
                cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY: [
                    "interleaved-thinking-2025-05-14"
                ]
            },
        ),
        "claude-fable-5",
        ingress_protocol="anthropic",
    )
    payload = json.loads(req.body)
    betas = req.headers["anthropic-beta"].split(",")
    assert "temperature" not in payload
    assert "thinking" not in payload
    assert "interleaved-thinking-2025-05-14" not in betas
    assert payload["speed"] == "fast"
    assert cc_mimicry.FAST_MODE_BETA in betas
    assert cc_mimicry.CONTEXT_1M_BETA in betas


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
async def test_openai_force_fast_uses_priority_and_respects_model_scope(protocol: str):
    ch = _openai_channel(
        protocol,
        fastMode="force",
        fastModels=["gpt-fast"],
    )
    if protocol == "openai-chat":
        body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}
        ingress = "chat"
    else:
        body = {"model": "fast", "input": "hi"}
        ingress = "responses"

    forced = await ch.build_upstream_request(body, "gpt-fast", ingress_protocol=ingress)
    assert json.loads(forced.body)["service_tier"] == "priority"

    automatic = await ch.build_upstream_request(body, "gpt-normal", ingress_protocol=ingress)
    assert "service_tier" not in json.loads(automatic.body)


@pytest.mark.asyncio
async def test_openai_omit_fields_runs_after_provider_compatibility():
    ch = _openai_channel(
        "openai-chat",
        omitTemperature=True,
        omitThinking=True,
    )
    req = await ch.build_upstream_request(
        {
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "thinking": {"type": "enabled"},
            "_parrot_allow_openai_thinking": True,
        },
        "gpt-fast",
        ingress_protocol="chat",
    )
    payload = json.loads(req.body)
    assert "temperature" not in payload
    assert "thinking" not in payload


def test_forced_1m_disables_the_opposite_direction_fallback():
    forced = _anthropic_channel(
        context1mMode="force",
        context1mModels=["claude-fable-5"],
    )
    assert failover._channel_forces_context_1m(forced, "claude-fable-5")
    assert not failover._channel_forces_context_1m(forced, "claude-haiku-4-5-20251001")

    automatic = _anthropic_channel(context1mMode="auto")
    assert not failover._channel_forces_context_1m(automatic, "claude-fable-5")
