"""Provider adapter base classes.

Provider adapters isolate upstream/provider quirks from protocol codecs.  Phase 5
starts with response restoration because that ordering is already critical in
Parrot: provider restore must happen before protocol parsing and response
translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from .capabilities import (
    ANTHROPIC_OAUTH_CAPABILITIES,
    ANTHROPIC_STANDARD_CAPABILITIES,
    CC_MIMICRY_CAPABILITIES,
    OPENAI_API_CAPABILITIES,
    OPENAI_CODEX_CAPABILITIES,
    XAI_OAUTH_CAPABILITIES,
    ProviderCapabilities,
)


class RestorableChannel(Protocol):
    async def restore_response(self, chunk: bytes, dynamic_map: Optional[dict] = None) -> bytes: ...


@dataclass(frozen=True)
class ProviderAttemptContext:
    channel: RestorableChannel
    dynamic_map: Optional[dict] = None
    translator_ctx: Optional[dict] = None


class ProviderAdapter:
    name = "provider-base"
    capabilities = ProviderCapabilities(
        adapter_name=name,
        family="unknown",
        protocols=frozenset(),
        transports=frozenset(),
    )

    def filter_request_payload(self, payload: dict, *, protocol: str, bridge: bool = False) -> dict:
        """Apply this provider's target payload allowlist for a protocol.

        Absence of an allowlist means the adapter does not own that payload
        shape; callers receive a shallow copy instead of a validator error.
        """
        if not isinstance(payload, dict):
            return {}
        allow = self.capabilities.request_allowlist(protocol, bridge=bridge)
        if allow is None:
            return dict(payload)
        return {k: v for k, v in payload.items() if k in allow}

    async def restore_response_bytes(self, chunk: bytes, ctx: ProviderAttemptContext) -> bytes:
        """Restore upstream response bytes before protocol decoding.

        The default implementation delegates to the existing Channel method so
        Phase 5 is behaviour-preserving.  Dynamic maps stay per-attempt via the
        context and never move back onto channel instances.
        """
        return await ctx.channel.restore_response(chunk, dynamic_map=ctx.dynamic_map)


class AnthropicStandardAdapter(ProviderAdapter):
    name = "anthropic-standard"
    capabilities = ANTHROPIC_STANDARD_CAPABILITIES


class CcMimicryAdapter(ProviderAdapter):
    name = "cc-mimicry"
    capabilities = CC_MIMICRY_CAPABILITIES


class AnthropicOAuthAdapter(ProviderAdapter):
    name = "anthropic-oauth"
    capabilities = ANTHROPIC_OAUTH_CAPABILITIES


class OpenAIApiAdapter(ProviderAdapter):
    name = "openai-api"
    capabilities = OPENAI_API_CAPABILITIES


class OpenAICodexAdapter(ProviderAdapter):
    name = "openai-codex"
    capabilities = OPENAI_CODEX_CAPABILITIES


class XAIOAuthAdapter(ProviderAdapter):
    name = "xai-oauth"
    capabilities = XAI_OAUTH_CAPABILITIES
