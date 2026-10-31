"""Local, non-retryable per-key text-model denial. Never enters routing/failover."""
from __future__ import annotations

from dataclasses import dataclass

from . import auth, errors, model_mapping, model_names
from .api_key_rules import ModelBlockRule
from .model_registry import runtime as model_registry
from .transform.cc_mimicry import strip_context_1m_model_marker

PRESETS = {
    "quota_exhausted": (402, "The quota for this model has been exhausted."),
    "insufficient_balance": (402, "Insufficient balance to use this model."),
    "service_busy": (403, "This model is busy and unavailable for this API key."),
    "model_unavailable": (403, "This model is unavailable for this API key."),
}


def has_rules(key_name: str) -> bool:
    return any(isinstance(r, dict) and r.get("enabled", True) is True
               for r in (auth.api_key_entry(key_name) or {}).get("modelBlocks", []))


def effective_model(name: str, ingress: str) -> str:
    body = {"model": name}
    model_mapping.apply_mapping(body, ingress)
    stripped = strip_context_1m_model_marker(body["model"])
    if stripped != body["model"]:
        body["model"] = stripped
        model_mapping.apply_mapping(body, ingress)
    return str(body["model"]).strip()


@dataclass(frozen=True)
class Denial:
    status: int
    code: str
    message: str

    def http_response(self, ingress: str):
        build = errors.json_error_response if ingress == "anthropic" else errors.json_error_openai
        response = build(self.status, "permission_error", self.message, code=self.code)
        response.headers["x-should-retry"] = "false"
        return response

    def ws_frame(self) -> dict:
        return {"type": "error", "status": self.status, "retryable": False,
                "headers": {"x-should-retry": "false"},
                "error": {"type": "permission_error", "code": self.code,
                          "message": self.message, "param": "model", "retryable": False},
                "code": self.code, "message": self.message, "param": "model",
                "sequence_number": 0, "error_type": "permission_error"}


def check(key_name: str | None, requested: str, ingress: str, *, effective: str | None = None) -> Denial | None:
    """Compare the request spelling and its one-step effective identity.

    Do NOT map the rule: a rule for A must not deny unrelated B merely because
    the administrator mapped A -> B. A rule for B does cover aliases to B.
    No substring, family expansion, or client-supplied private metadata is used.
    """
    if not isinstance(requested, str) or not requested.strip():
        return None
    rules = [r for r in (auth.api_key_entry(key_name) or {}).get("modelBlocks", [])
             if isinstance(r, dict) and r.get("enabled", True) is True]
    if not rules:
        return None
    resolved = effective if effective is not None else effective_model(requested.strip(), ingress)
    requested_name = strip_context_1m_model_marker(requested.strip())
    # Source aliases can expose a different wire spelling, also with the unified
    # registry enabled. Resolve only the effective request, never the rule or an
    # unrelated global mapping target. This reads identities, not scheduling state.
    from .channel import registry
    source_names: list[tuple[str, str]] = []
    unified = model_registry.current()
    if unified is not None:
        found = model_registry.resolve(unified, resolved)
        offers = unified.models[found.model].offers if found is not None else ()
        source_models = [(registry.get_channel(offer.source), offer.wire) for offer in offers]
    else:
        source_models = [(channel, channel.supports_model(resolved)) for channel in registry.all_channels()]
    for channel, wire in source_models:
        if channel is None or not wire or not channel.enabled or channel.disabled_reason:
            continue
        if auth.channel_allowed(key_name, channel.key):
            source_names.append((getattr(channel, "provider", ""), model_names.for_channel(channel.key, wire)))
    for raw in rules:
        rule = ModelBlockRule.model_validate(raw)
        target = strip_context_1m_model_marker(rule.model)
        if not (target == requested_name or model_registry.permits([target], resolved)
                or any(model_names.canonical_id(provider, target) == wire for provider, wire in source_names)):
            continue
        status, default_message = PRESETS[rule.preset]
        # Match the inference runtime's stdout logging; no upstream failure row,
        # scoring/cooldown event, notification, or outward custom text is logged.
        print(f"[local-policy] 本地策略拦截 local_model_policy_block key={key_name!r} "
              f"requested={requested!r} effective={resolved!r} rule={rule.model!r} "
              f"preset={rule.preset}; no upstream, no retry", flush=True)
        return Denial(status, rule.preset, rule.message or default_message)
    return None
