"""Directory-key thinking tiers observed on the Hub 2.32.1 generation path."""
from __future__ import annotations

from ..openai.transform.guard import GuardError
from .catalog import thinking_level

FAMILIES = {
    "gemini-4-carbon": ("low", "medium", "high"),
    "gemini-4-hydrogen": ("low", "medium", "high", "xhigh"),
}


def family(model: str) -> str | None:
    return next((base for base, tiers in FAMILIES.items()
                 if model == base or model in {f"{base}-{tier}" for tier in tiers}), None)


def _fail(message: str) -> None:
    raise GuardError(400, "invalid_request_error", message, param="reasoning", scope="candidate")


def intent(model: str, body: dict, ingress: str) -> tuple[str | None, bool]:
    base = family(model)
    if base is None:
        _fail("Unknown Antigravity fixed-thinking model")
    summary = None
    if ingress == "anthropic":
        thinking = body.get("thinking")
        if thinking is not None and not isinstance(thinking, dict):
            _fail("thinking must be an object")
        thinking = thinking or {}
        if thinking.get("budget_tokens") is not None:
            _fail(f"{base} uses directory-key tiers, not numeric thinking budgets")
        mode = thinking.get("type")
        if mode not in (None, "enabled", "adaptive", "disabled"):
            _fail("Unsupported thinking type")
        if mode == "disabled":
            _fail(f"{base} fixed-thinking tiers cannot promise disabled reasoning")
        output = body.get("output_config") or {}
        if not isinstance(output, dict):
            _fail("output_config must be an object")
        effort = output.get("effort")
    elif ingress == "chat":
        effort, summary = body.get("reasoning_effort"), body.get("reasoning_summary")
    else:
        reasoning = body.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, dict):
            _fail("reasoning must be an object")
        reasoning = reasoning or {}
        if reasoning.get("budget_tokens") is not None:
            _fail(f"{base} uses directory-key tiers, not numeric thinking budgets")
        effort, summary = reasoning.get("effort"), reasoning.get("summary")
    if effort is not None and (not isinstance(effort, str) or effort not in FAMILIES[base]):
        _fail(f"{base} supports only explicit {'/'.join(FAMILIES[base])} effort; no fallback is performed")
    if summary is not None and (not isinstance(summary, str) or summary not in {"auto", "concise", "detailed", "none"}):
        _fail("reasoning summary must be auto, concise, detailed or none")
    return effort, summary != "none"


def resolve(model: str, selected: list[str], body: dict, ingress: str, *, allow_tier_alias: bool = True) -> str | None:
    if not allow_tier_alias and model not in selected:
        return None
    base = family(model)
    if base is None:
        return model if model in selected else None
    if not any(family(item) == base for item in selected):
        return None
    effort, _ = intent(model, body, ingress)
    if model != base:
        if effort is not None and model != f"{base}-{effort}":
            _fail(f"Model {model} conflicts with effort {effort}")
        return model if model in selected else None
    if not allow_tier_alias:
        return model
    if effort is None:
        if base in selected:
            return base
        _fail(f"Bare {base} requires an explicit effort or an exact selected variant")
    target = f"{base}-{effort}"
    if target not in selected:
        _fail(f"Variant {target} is not selected, enabled and available at this account's generation endpoint")
    return target


def thinking_config(model: str, body: dict, ingress: str = "responses", *, metadata: dict | None = None) -> dict:
    effort, summary = intent(model, body, ingress)
    base = family(model)
    if model != base and effort is not None and model != f"{base}-{effort}":
        _fail(f"Model {model} conflicts with effort {effort}")
    result = {"includeThoughts": summary}
    level = thinking_level((metadata or {}).get("thinkingLevel"))
    if level is not None:
        result["thinkingLevel"] = level
    return result
