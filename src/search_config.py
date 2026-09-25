"""Search backend catalog and effective configuration defaults.

Configuration reads preserve the legacy AnySearch migration without persisting
or exposing credential-bearing settings.
"""
from __future__ import annotations

import copy
import os

from . import config


MODES = ("managed", "passthrough", "disabled")
BACKEND_TYPES = ("anysearch", "tavily", "exa", "brave", "openai", "xai", "anthropic", "zhipu")
KEY_TYPES = frozenset(("anysearch", "tavily", "exa", "brave", "zhipu"))
ACCOUNT_TYPES = frozenset(("openai", "xai", "anthropic", "zhipu"))
EXTRACT_TYPES = frozenset(("anysearch", "tavily", "exa", "openai", "zhipu"))
ENDPOINTS = {
    "anysearch": "https://api.anysearch.com",
    "tavily": "https://api.tavily.com",
    "exa": "https://api.exa.ai",
    "brave": "https://api.search.brave.com",
}
NAMES = {"anysearch": "AnySearch", "tavily": "Tavily", "exa": "Exa", "brave": "Brave",
         "openai": "OpenAI OAuth", "xai": "xAI OAuth", "anthropic": "Anthropic OAuth", "zhipu": "智谱 MCP"}
DEFAULTS = {
    "functionMode": "managed", "hostedMode": "managed", "maxAttempts": 3,
    "timeoutSeconds": 10, "maxResults": 8, "maxToolRounds": 50,
    "maxFetchChars": 50000, "minQueryChars": 2, "maxFetchUrlChars": 2048,
    "requireKnownUrlForFetch": True, "maxConcurrentToolCalls": 0,
    "language": "", "country": "", "freshness": "",
}


def default_backend(kind: str) -> dict:
    return {"id": kind, "type": kind, "name": NAMES[kind], "enabled": True,
            "apiKeys": [], "endpoint": ENDPOINTS.get(kind, ""), "model": "",
            "accountIds": [], "allowDisabledAccounts": False}


def settings() -> dict:
    """Effective config; intentionally private (it contains secrets). Never persist on read."""
    cfg = config.get()
    legacy = cfg.get("anysearch") or {}
    current = cfg.get("search") or {}
    result = copy.deepcopy(DEFAULTS)
    if isinstance(legacy, dict):
        for key in ("timeoutSeconds", "maxResults", "maxFetchChars", "maxToolRounds", "minQueryChars",
                    "maxFetchUrlChars", "requireKnownUrlForFetch", "maxConcurrentToolCalls"):
            if key in legacy:
                result[key] = copy.deepcopy(legacy[key])
        # Explicit old opt-out must not silently start interception on upgrade.
        if legacy.get("enabled") is False:
            result.update(functionMode="passthrough", hostedMode="passthrough")
    if isinstance(current, dict):
        result.update(copy.deepcopy(current))
    if "backends" not in result:
        result["backends"] = [default_backend(kind) for kind in BACKEND_TYPES]
        key = str(legacy.get("apiKey") or os.environ.get("ANYSEARCH_API_KEY", "")).strip()
        result["backends"][0]["apiKeys"] = [key] if key else []
        endpoint = str(legacy.get("endpoint") or ENDPOINTS["anysearch"]).rstrip("/")
        # Known legacy MCP URL migrates to the same origin's documented REST API.
        result["backends"][0]["endpoint"] = endpoint.removesuffix("/mcp")
    return result
