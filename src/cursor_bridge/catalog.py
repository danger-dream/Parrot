"""Cursor account model catalog and request-time variant resolution.

Cursor's ``AvailableModels`` response is account-specific.  With
``use_model_parameters=true`` it returns canonical client model ids and keeps
expanded Cursor wire ids in ``legacy_slugs``.  Parrot exposes only canonical ids
to downstream clients and resolves one legacy slug per request from reasoning,
thinking, fast, and long-context controls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .constants import DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_TOKENS
from .models import CursorModel

_EFFORT_CANONICAL = {
    "none": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "max": "max",
}
# Cursor's exploded model directory exposes ``max`` as a real reasoning
# effort above ``xhigh`` (for example ``claude-fable-5-thinking-max``).
# Max Context is an independent RequestedModel.max_mode/long_context control
# and must never be inferred from an effort suffix.
_EFFORT_TOKENS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_CURSOR_FIRST_PARTY = frozenset({"composer-2.5", "grok-4.5", "grok-4.6"})


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def normalize_effort(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace(" ", "-")
    return _EFFORT_CANONICAL.get(text)


def _variant_traits(slug: str, base_id: str = "") -> dict[str, Any]:
    """Extract flags from real Cursor slugs regardless of suffix ordering.

    Cursor currently emits all of these shapes, among others::

      claude-fable-5-thinking-medium
      claude-4.6-sonnet-medium-thinking
      cursor-grok-4.5-medium-fast
      grok-4.5-fast-medium
      gpt-5.5-extra-high-fast

    Parsing by repeatedly stripping only the final suffix is therefore unsafe.
    The canonical base sequence is removed first so a base model such as
    ``gpt-5.1-codex-max`` does not manufacture a ``max`` effort.
    """

    parts = [part for part in str(slug or "").lower().split("-") if part]
    base_parts = [part for part in str(base_id or "").lower().split("-") if part]
    if base_parts and len(parts) >= len(base_parts):
        for index in range(len(parts) - len(base_parts) + 1):
            if parts[index:index + len(base_parts)] == base_parts:
                del parts[index:index + len(base_parts)]
                break
    fast = "fast" in parts
    thinking = "thinking" in parts
    effort: str | None = None
    for index, token in enumerate(parts):
        if token == "extra" and index + 1 < len(parts) and parts[index + 1] == "high":
            effort = "xhigh"
        elif token == "high" and index > 0 and parts[index - 1] == "extra":
            continue
        elif token in _EFFORT_TOKENS:
            effort = token
    return {
        "id": str(slug or "").strip(),
        "effort": effort,
        "fast": fast,
        "thinking": thinking,
    }


def _derive_variant_metadata(
    legacy_slugs: Iterable[Any], *, base_id: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    variants = [
        _variant_traits(str(slug), base_id)
        for slug in legacy_slugs if str(slug or "").strip()
    ]
    efforts: list[str] = []
    for item in variants:
        effort = item.get("effort")
        if effort and effort not in efforts:
            efforts.append(effort)
    return variants, efforts


def record_from_model(model: CursorModel) -> dict[str, Any]:
    variants, efforts = _derive_variant_metadata(
        model.legacy_slugs, base_id=model.id,
    )
    return {
        "id": model.id,
        "name": model.name,
        "reasoning": bool(model.reasoning),
        "context_window": _positive_int(model.context_window, DEFAULT_CONTEXT_WINDOW),
        "context_window_max_mode": (
            _positive_int(model.context_window_max_mode, DEFAULT_CONTEXT_WINDOW)
            if model.context_window_max_mode else None
        ),
        "max_tokens": _positive_int(model.max_tokens, DEFAULT_MAX_TOKENS),
        "supports_images": bool(model.supports_images),
        "supports_max_mode": bool(model.supports_max_mode),
        "supports_agent": bool(model.supports_agent),
        "supports_plan_mode": bool(model.supports_plan_mode),
        "is_long_context_only": bool(model.is_long_context_only),
        "default_on": bool(model.default_on),
        "server_model_name": model.server_model_name,
        "tagline": model.tagline,
        "price": model.price,
        "aliases": list(model.id_aliases),
        "legacy_slugs": list(model.legacy_slugs),
        "reasoning_efforts": efforts,
        "variants": variants,
    }


def build_catalog(models: Iterable[CursorModel], *, fetched_at: str | None = None) -> dict[str, Any]:
    records = [record_from_model(model) for model in models]
    records.sort(key=lambda item: str(item.get("id") or ""))
    return {
        "schema": 1,
        "fetched_at": fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": records,
    }


def catalog_records(account_or_catalog: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    source = account_or_catalog or {}
    catalog = source.get("cursor_model_catalog") if isinstance(source, Mapping) else None
    if not isinstance(catalog, Mapping) and isinstance(source, Mapping) and source.get("schema") == 1:
        catalog = source
    values = catalog.get("models") if isinstance(catalog, Mapping) else None
    records: list[dict[str, Any]] = []
    for item in values or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        record = dict(item)
        # Re-derive effort/variant fields from authoritative legacy slugs at
        # every read. This also repairs catalogs written by the historical
        # parser that mistook a ``-max`` reasoning suffix for Max Context.
        if "legacy_slugs" in record:
            variants, efforts = _derive_variant_metadata(
                record.get("legacy_slugs") or [],
                base_id=str(record.get("id") or ""),
            )
            record["variants"] = variants
            record["reasoning_efforts"] = efforts
        records.append(record)
    return records


def find_record(account_or_catalog: Mapping[str, Any] | None, model_id: str) -> dict[str, Any] | None:
    wanted = str(model_id or "").strip()
    for item in catalog_records(account_or_catalog):
        if str(item.get("id") or "").strip() == wanted:
            return item
    return None


def _effort_preferences(effort: str | None) -> tuple[str | None, ...]:
    if effort in (None, ""):
        return (None,)
    if effort == "none":
        return ("none", "minimal", "low", None)
    if effort == "minimal":
        return ("minimal", "none", "low", None)
    if effort == "low":
        return ("low", "none", "minimal", None)
    if effort == "medium":
        return ("medium", None, "low", "high")
    if effort == "high":
        return ("high", "medium", "xhigh", None)
    if effort == "xhigh":
        return ("xhigh", "high", "medium", None)
    if effort == "max":
        # Capability adaptation belongs at the authoritative provider/model
        # boundary. The resolver must not guess xhigh or select Max Mode.
        return ("max",)
    return (effort, None)


def resolve_variant(
    record: Mapping[str, Any] | None,
    *,
    reasoning_effort: Any = None,
    fast: bool = False,
    thinking: bool | None = None,
) -> str:
    """Resolve one canonical model plus request controls to a real Cursor id.

    A request without variant controls deliberately keeps the canonical id so
    Cursor applies the account/team default parameter preset.  With explicit
    controls, exact legacy slugs win and unsupported flags degrade
    independently instead of producing a made-up model name.
    """

    if not isinstance(record, Mapping):
        return ""
    base = str(record.get("id") or "").strip()
    effort = normalize_effort(reasoning_effort)
    variants = [
        dict(item) for item in record.get("variants") or []
        if isinstance(item, Mapping) and item.get("id")
    ]
    if not base or not variants or (effort is None and not fast and thinking is None):
        return base

    supports_fast = any(bool(item.get("fast")) for item in variants)
    supports_thinking = any(bool(item.get("thinking")) for item in variants)
    wanted_fast = bool(fast and supports_fast)
    if thinking is None:
        wanted_thinking = bool(supports_thinking and effort not in (None, "none", "minimal"))
    else:
        wanted_thinking = bool(thinking and supports_thinking)

    effort_order = _effort_preferences(effort)
    # Exact flags first, then independently relax thinking and fast.  Stable
    # list order preserves Cursor's own preference when two slugs are equivalent.
    flag_preferences: list[tuple[bool, bool]] = [(wanted_fast, wanted_thinking)]
    for pair in (
        (wanted_fast, False),
        (False, wanted_thinking),
        (False, False),
    ):
        if pair not in flag_preferences:
            flag_preferences.append(pair)

    for wanted_effort in effort_order:
        for fast_flag, thinking_flag in flag_preferences:
            for item in variants:
                if item.get("effort") != wanted_effort:
                    continue
                if bool(item.get("fast")) != fast_flag:
                    continue
                if bool(item.get("thinking")) != thinking_flag:
                    continue
                return str(item.get("id") or base)
    return base


def metadata_from_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {}
    context = _positive_int(record.get("context_window"), DEFAULT_CONTEXT_WINDOW)
    output = min(_positive_int(record.get("max_tokens"), DEFAULT_MAX_TOKENS), context)
    input_budget = max(1, context - output)
    compact_trigger = max(1, int(input_budget * 0.8))
    efforts = [str(item) for item in record.get("reasoning_efforts") or [] if str(item)]
    result: dict[str, Any] = {
        "name": str(record.get("name") or record.get("id") or "Cursor model"),
        "description": str(record.get("tagline") or ""),
        "family": "cursor",
        # AvailableModels may advertise upstream image support, but the current
        # AgentService bridge does not yet serialize SelectedImage. Effective
        # Parrot metadata must describe the usable intersection, not overclaim.
        "vision": False,
        "cursorUpstreamVision": bool(record.get("supports_images")),
        "reasoning": bool(record.get("reasoning") or efforts),
        "reasoningEfforts": efforts,
        "toolCall": True,
        "structuredOutput": False,
        "temperature": False,
        "modalities": {
            "input": ["text"],
            "output": ["text"],
        },
        "contextWindow": context,
        "maxOutputTokens": output,
        "compactTriggerTokens": compact_trigger,
        "metadataSource": "cursor.AvailableModels",
        "supportsFast": any(bool(item.get("fast")) for item in record.get("variants") or [] if isinstance(item, Mapping)),
        "supportsThinking": any(bool(item.get("thinking")) for item in record.get("variants") or [] if isinstance(item, Mapping)),
        "variants": [str(item.get("id")) for item in record.get("variants") or [] if isinstance(item, Mapping) and item.get("id")],
        "aliases": [str(item) for item in record.get("aliases") or [] if str(item)],
    }
    max_context = record.get("context_window_max_mode")
    if max_context:
        result["contextWindowMaxMode"] = _positive_int(max_context, context)
    if efforts:
        if "medium" in efforts:
            result["defaultReasoningEffort"] = "medium"
        elif "high" in efforts:
            result["defaultReasoningEffort"] = "high"
        else:
            result["defaultReasoningEffort"] = efforts[0]
    return result


def is_cursor_first_party_model(record: Mapping[str, Any] | None, auto_bucket_models: Iterable[str] = ()) -> bool:
    if not isinstance(record, Mapping):
        return False
    base = str(record.get("id") or "").strip()
    if base in _CURSOR_FIRST_PARTY:
        return True
    bucket = {str(item or "").strip() for item in auto_bucket_models if str(item or "").strip()}
    if base in bucket:
        return True
    return any(str(item or "") in bucket for item in record.get("legacy_slugs") or [])
