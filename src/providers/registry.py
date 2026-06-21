"""Provider adapter selection.

Selection is intentionally lightweight and mirrors today's Channel classes:
- Anthropic API with cc_mimicry=false → standard
- Anthropic API with cc_mimicry=true  → CC mimicry
- Anthropic OAuth                      → Anthropic OAuth
- OpenAI API channels                  → OpenAI API
- OpenAI OAuth/Codex channels          → OpenAI Codex
"""

from __future__ import annotations

from typing import Optional

from .base import (
    AnthropicOAuthAdapter,
    AnthropicStandardAdapter,
    CcMimicryAdapter,
    OpenAIApiAdapter,
    OpenAICodexAdapter,
    ProviderAdapter,
    ProviderAttemptContext,
)

_ANTHROPIC_STANDARD = AnthropicStandardAdapter()
_CC_MIMICRY = CcMimicryAdapter()
_ANTHROPIC_OAUTH = AnthropicOAuthAdapter()
_OPENAI_API = OpenAIApiAdapter()
_OPENAI_CODEX = OpenAICodexAdapter()


def adapter_for_channel(channel) -> ProviderAdapter:
    protocol = getattr(channel, "protocol", "anthropic")
    ch_type = getattr(channel, "type", "api")

    if protocol.startswith("openai-"):
        return _OPENAI_CODEX if ch_type == "oauth" else _OPENAI_API

    if ch_type == "oauth":
        return _ANTHROPIC_OAUTH

    if bool(getattr(channel, "cc_mimicry", False)):
        return _CC_MIMICRY

    return _ANTHROPIC_STANDARD


def capabilities_for_channel(channel):
    return adapter_for_channel(channel).capabilities


def filter_request_payload(channel, payload: dict, *, protocol: str, bridge: bool = False) -> dict:
    return adapter_for_channel(channel).filter_request_payload(
        payload,
        protocol=protocol,
        bridge=bridge,
    )


async def restore_response_bytes(
    channel,
    chunk: bytes,
    *,
    dynamic_map: Optional[dict] = None,
    translator_ctx: Optional[dict] = None,
) -> bytes:
    ctx = ProviderAttemptContext(
        channel=channel,
        dynamic_map=dynamic_map,
        translator_ctx=translator_ctx,
    )
    return await adapter_for_channel(channel).restore_response_bytes(chunk, ctx)
