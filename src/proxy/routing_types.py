"""Stable upstream identities for routing, independent of accounts and wire protocol.

Legacy purpose remains separate: adding a type override must not silently change
any route that has no override. In particular, a custom API speaking OpenAI wire
format is not evidence that its provider is OpenAI.
"""
from __future__ import annotations

from collections.abc import Mapping

PROVIDER_ROUTES = (
    ("openai", "OpenAI"),
    ("xai", "Grok"),
    ("cursor", "Cursor"),
    ("antigravity", "Antigravity"),
    ("workbuddy", "WorkBuddy"),
    ("zhipu", "z.ai / 智谱"),
    ("claude", "Anthropic"),
)
PROVIDER_KEYS = frozenset(key for key, _ in PROVIDER_ROUTES)
_ALIASES = {"anthropic": "claude", "grok": "xai", "z.ai": "zhipu", "zai": "zhipu"}
_PURPOSE_PROVIDERS = {
    "oauth_anthropic": "claude", "oauth_openai": "openai",
    "oauth_xai": "xai", "oauth_cursor": "cursor", "oauth_antigravity": "antigravity",
    "oauth_workbuddy": "workbuddy", "oauth_zhipu": "zhipu",
    "core_openai": "openai", "core_claude": "claude",
}
# These are new purpose names replacing ambiguous ones. Keep their old fallback
# exactly; older distinct purposes (oauth_xai/cursor/antigravity) stay untouched.
_LEGACY_PURPOSES = {
    "oauth_workbuddy": "oauth_openai", "oauth_zhipu": "oauth_openai",
    "core_openai": "core_monitor", "core_claude": "core_monitor",
}


def normalize_provider(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    raw = _ALIASES.get(raw, raw)
    return raw if raw in PROVIDER_KEYS else ""


def provider_for_context(*, provider: str = "", channel_key: str = "",
                         account_key: str = "", purpose: str = "",
                         api_providers: Mapping[str, str] | None = None) -> str:
    explicit = normalize_provider(provider)
    if explicit:
        return explicit
    if channel_key.startswith("api:"):
        # Never infer a third-party API provider from its protocol/model/URL.
        return (api_providers or {}).get(channel_key, "")
    key = channel_key.removeprefix("oauth:") if channel_key.startswith("oauth:") else account_key
    if key:
        known = normalize_provider(key.split(":", 1)[0])
        if known:
            return known
    return _PURPOSE_PROVIDERS.get(purpose, "")


def provider_for_channel(channel) -> str:
    return provider_for_context(
        provider=(getattr(channel, "provider_id", "") if getattr(channel, "type", "") == "api"
                  else getattr(channel, "provider", "")),
        channel_key=str(getattr(channel, "key", "") or ""),
        account_key=str(getattr(channel, "account_key", "") or ""),
    )


def legacy_purpose(purpose: str) -> str:
    return _LEGACY_PURPOSES.get(purpose, purpose)
