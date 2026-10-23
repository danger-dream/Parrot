"""Per-model facts of an API channel as the model center resolves them."""

from __future__ import annotations

from src import model_metadata, model_state
from src.model_registry import runtime as model_registry


def channel_model_facts(source: str, alias: str, real: str) -> dict:
    """Enable state (this channel / model center) and limits of one channel model."""
    model_id = model_registry.model_of(source, real) or alias
    try:
        meta = model_metadata.get_metadata(model_id, scope_key=source, outbound_model=real)
    except Exception:
        meta = {}
    context = meta.get("contextWindow")
    output = meta.get("maxOutputTokens")
    return {
        "disabled": not (model_state.is_source_enabled(source, alias) and model_state.is_source_enabled(source, model_id)),
        "globallyDisabled": not model_state.is_global_enabled(model_id, source_key=source),
        "contextWindow": context if isinstance(context, int) else None,
        "maxOutputTokens": output if isinstance(output, int) else None,
    }
