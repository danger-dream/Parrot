"""Exact AG Argon tier policy. No guessed default, budget conversion or host route."""
from __future__ import annotations

from ..openai.transform.guard import GuardError

BASE = "gemini-4-argon"
TIERS = ("low", "medium", "high")
VARIANTS = {f"{BASE}-{tier}": tier for tier in TIERS}


def is_argon(model: str) -> bool:
    return model == BASE or model in VARIANTS


def _fail(message: str) -> None:
    raise GuardError(400, "invalid_request_error", message, param="reasoning", scope="candidate")


def intent(body: dict, ingress: str) -> tuple[str | None, bool]:
    """Only explicit supported levels select a variant; summary output is separate.

    Numeric thinking budgets have no verified Argon tier mapping. Reject rather
    than silently converting (or increasing) a caller's token budget.
    """
    effort = None
    summary = None
    if ingress == "anthropic":
        thinking = body.get("thinking")
        if thinking is not None and not isinstance(thinking, dict):
            _fail("Argon thinking must be an object")
        thinking = thinking or {}
        if thinking.get("budget_tokens") is not None:
            _fail("Argon cannot safely map thinking.budget_tokens; use an explicit -low/-medium/-high model or output_config.effort without a numeric budget")
        mode = thinking.get("type")
        if mode not in (None, "enabled", "adaptive", "disabled"):
            _fail("Unsupported Argon thinking type")
        if mode == "disabled":
            _fail("Argon fixed-thinking variants cannot promise disabled reasoning; omit thinking to suppress returned summaries")
        output = body.get("output_config") or {}
        if not isinstance(output, dict):
            _fail("Argon output_config must be an object")
        effort = output.get("effort")
        wants_summary = mode in {"enabled", "adaptive"}
    elif ingress == "chat":
        effort = body.get("reasoning_effort")
        summary = body.get("reasoning_summary")
        wants_summary = summary in {"auto", "concise", "detailed"} if isinstance(summary, str) else False
    else:
        reasoning = body.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, dict):
            _fail("Argon reasoning must be an object")
        reasoning = reasoning or {}
        if reasoning.get("budget_tokens") is not None:
            _fail("Argon does not support numeric reasoning budgets; choose low, medium or high")
        effort, summary = reasoning.get("effort"), reasoning.get("summary")
        wants_summary = summary in {"auto", "concise", "detailed"} if isinstance(summary, str) else False
    if effort is not None and (not isinstance(effort, str) or effort not in TIERS):
        _fail("Argon supports only explicit low, medium or high effort; no automatic fallback is performed")
    if summary is not None and (not isinstance(summary, str) or summary not in {"auto", "concise", "detailed", "none"}):
        _fail("Argon reasoning summary must be auto, concise, detailed or none")
    return effort, wants_summary


def resolve(model: str, selected: list[str], body: dict, ingress: str) -> str | None:
    if not is_argon(model):
        return model if model in selected else None
    if not any(is_argon(item) for item in selected):
        return None
    level, _ = intent(body, ingress)
    if model in VARIANTS:
        if level is not None and level != VARIANTS[model]:
            _fail(f"Argon model {model} conflicts with effort {level}; select the matching model and effort explicitly")
        return model if model in selected else None
    if level is None:
        if BASE in selected:
            return BASE
        _fail("Bare gemini-4-argon requires explicit low/medium/high effort or an exact selected variant model; no default tier is chosen")
    target = f"{BASE}-{level}"
    if target not in selected:
        _fail(f"Argon variant {target} is not selected, enabled and available at this account's generation endpoint")
    return target


def thinking_config(model: str, body: dict, ingress: str = "responses") -> dict:
    level, summary = intent(body, ingress)
    fixed = VARIANTS.get(model)
    if fixed and level is not None and fixed != level:
        _fail(f"Argon model {model} conflicts with effort {level}")
    result = {"includeThoughts": summary}
    if fixed or level:
        result["thinkingLevel"] = fixed or level
    return result
