"""Provider adapter selection.

Selection is intentionally lightweight and mirrors today's Channel classes:
- Anthropic API with cc_mimicry=false → standard
- Anthropic API with cc_mimicry=true  → CC mimicry
- Anthropic OAuth                      → Anthropic OAuth
- OpenAI API channels                  → OpenAI API
- OpenAI OAuth/Codex channels          → OpenAI Codex
- xAI OAuth/Grok channels              → xAI OAuth
- Cursor OAuth/AgentService channels    → Cursor OAuth
- Antigravity OAuth channels           → Antigravity OAuth
"""

from __future__ import annotations

from typing import Optional

from .base import (
    AnthropicOAuthAdapter,
    AnthropicStandardAdapter,
    AntigravityOAuthAdapter,
    CcMimicryAdapter,
    CursorOAuthAdapter,
    OpenAIApiAdapter,
    OpenAICodexAdapter,
    XAIOAuthAdapter,
    ProviderAdapter,
    ProviderAttemptContext,
)

_ANTHROPIC_STANDARD = AnthropicStandardAdapter()
_CC_MIMICRY = CcMimicryAdapter()
_ANTHROPIC_OAUTH = AnthropicOAuthAdapter()
_OPENAI_API = OpenAIApiAdapter()
_CURSOR_OAUTH = CursorOAuthAdapter()
_OPENAI_CODEX = OpenAICodexAdapter()
_XAI_OAUTH = XAIOAuthAdapter()
_ANTIGRAVITY_OAUTH = AntigravityOAuthAdapter()


def adapter_for_channel(channel) -> ProviderAdapter:
    protocol = getattr(channel, "protocol", "anthropic")
    ch_type = getattr(channel, "type", "api")

    if protocol.startswith("openai-"):
        if ch_type == "oauth":
            provider = getattr(channel, "provider", "")
            if provider == "xai":
                return _XAI_OAUTH
            if provider == "cursor":
                return _CURSOR_OAUTH
            if provider == "antigravity":
                return _ANTIGRAVITY_OAUTH
            return _OPENAI_CODEX
        return _OPENAI_API

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
