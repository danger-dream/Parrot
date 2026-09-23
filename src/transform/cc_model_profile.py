"""Claude Code 2.1.280 model defaults, not an account authorization catalog.

Source: v280's embedded model catalog (max_output_tokens / capabilities),
Jyr / vCt / L_ capability gates, and the Fable/Opus/Haiku wire captures.
Lookup never rewrites the user's wire model. Unknown compatible models receive
only the established standard-Anthropic max_tokens fallback, no CC capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CCModelProfile:
    max_tokens: int
    thinking: str | None = None
    effort: bool = False
    context_management: bool = False


_PROFILES = {
    "claude-3-5-haiku": CCModelProfile(8192),
    "claude-3-5-sonnet": CCModelProfile(8192),
    "claude-3-7-sonnet": CCModelProfile(32000),
    "claude-haiku-4-5": CCModelProfile(32000, "enabled", context_management=True),
    "claude-sonnet-4-0": CCModelProfile(32000, "enabled", context_management=True),
    "claude-sonnet-4-5": CCModelProfile(32000, "enabled", context_management=True),
    "claude-sonnet-4-6": CCModelProfile(32000, "adaptive", True, True),
    "claude-sonnet-5": CCModelProfile(64000, "adaptive", True, True),
    "claude-opus-4-0": CCModelProfile(32000, "enabled", context_management=True),
    "claude-opus-4-1": CCModelProfile(32000, "enabled", context_management=True),
    # L_ explicitly permits effort on Opus 4.5, but vCt rejects adaptive.
    "claude-opus-4-5": CCModelProfile(32000, "enabled", True, True),
    "claude-opus-4-6": CCModelProfile(64000, "adaptive", True, True),
    "claude-opus-4-7": CCModelProfile(64000, "adaptive", True, True),
    "claude-opus-4-8": CCModelProfile(64000, "adaptive", True, True),
    "claude-opus-5": CCModelProfile(64000, "adaptive", True, True),
    "claude-opus-5-5": CCModelProfile(128000, "adaptive", True, True),
    "claude-fable-5": CCModelProfile(64000, "adaptive", True, True),
    "claude-fable-5-1": CCModelProfile(64000, "adaptive", True, True),
    "claude-mythos-5": CCModelProfile(64000, "adaptive", True, True),
    "claude-mythos-5-1": CCModelProfile(64000, "adaptive", True, True),
}
_ALIASES = {
    "claude-sonnet-4": "claude-sonnet-4-0",
    "claude-opus-4": "claude-opus-4-0",
    "claude-fable-5.1": "claude-fable-5-1",
    "claude-opus-5.5": "claude-opus-5-5",
    "claude-mythos-5.1": "claude-mythos-5-1",
}


def canonical_model(model) -> str:
    value = re.sub(r"-\d{8}$", "", str(model or "").strip().lower())
    return _ALIASES.get(value, value)


def model_profile(model) -> CCModelProfile | None:
    return _PROFILES.get(canonical_model(model))


def default_thinking(profile: CCModelProfile | None, body: dict, max_tokens):
    """Do not synthesize thinking that conflicts with explicit request controls."""
    if profile is None or profile.thinking is None:
        return None
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in {"any", "tool"}:
        return None
    if any(key in body for key in ("temperature", "top_p", "top_k")):
        return None
    if profile.thinking == "adaptive":
        return {"type": "adaptive", "display": "omitted"}
    # enabled thinking requires at least 1024 budget tokens, strictly below the
    # output limit. Preserve small explicit max_tokens rather than increasing it.
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 1024:
        return {"budget_tokens": max_tokens - 1, "type": "enabled", "display": "omitted"}
    return None
