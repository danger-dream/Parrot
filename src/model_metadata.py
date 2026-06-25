"""Global model metadata management.

Metadata is keyed by the final model name, independent of channel/account or
provider protocol.  It is used by routing guards, compact rescue, cost display,
and the Telegram management UI.
"""

from __future__ import annotations

import re
from typing import Any

from . import config

SUMMARY_OUTPUT_RESERVE_TOKENS = 20_000
DEFAULT_COMPACT_BUFFER_TOKENS = 20_000

_INT_KEYS = {
    "contextWindow",
    "maxOutputTokens",
}
_FLOAT_KEYS = {
    "inputPricePer1M",
    "outputPricePer1M",
    "cacheReadPricePer1M",
    "cacheWritePricePer1M",
}
_BOOL_KEYS = {
    "vision",
    "compressionModel",
}
_LIST_KEYS = {
    "reasoningEfforts",
}
_STR_KEYS = {
    "defaultReasoningEffort",
}

_FIELD_ALIASES = {
    "context_window": "contextWindow",
    "context_window_tokens": "contextWindow",
    "context": "contextWindow",
    "max_context": "contextWindow",
    "max_context_tokens": "contextWindow",
    "contextLength": "contextWindow",
    "context_length": "contextWindow",
    "maxOutput": "maxOutputTokens",
    "max_output": "maxOutputTokens",
    "max_output_tokens": "maxOutputTokens",
    "maxTokens": "maxOutputTokens",
    "max_tokens": "maxOutputTokens",
    "canVision": "vision",
    "can_vision": "vision",
    "image": "vision",
    "images": "vision",
    "visionSupport": "vision",
    "supportsImages": "vision",
    "input_price": "inputPricePer1M",
    "input_price_per_1m": "inputPricePer1M",
    "output_price": "outputPricePer1M",
    "output_price_per_1m": "outputPricePer1M",
    "cache_read_price": "cacheReadPricePer1M",
    "cache_read_price_per_1m": "cacheReadPricePer1M",
    "cache_write_price": "cacheWritePricePer1M",
    "cache_write_price_per_1m": "cacheWritePricePer1M",
    "cache_output_price": "cacheWritePricePer1M",
    "cache_output_price_per_1m": "cacheWritePricePer1M",
    "compact": "compressionModel",
    "compression": "compressionModel",
    "compression_model": "compressionModel",
    "isCompressionModel": "compressionModel",
    "reasoning": "reasoningEfforts",
    "reasoning_efforts": "reasoningEfforts",
    "reasoningEffort": "reasoningEfforts",
    "thinking": "reasoningEfforts",
    "thinking_efforts": "reasoningEfforts",
    "thinkingEfforts": "reasoningEfforts",
    "efforts": "reasoningEfforts",
    "support_reasoning": "reasoningEfforts",
    "supported_reasoning": "reasoningEfforts",
    "supported_reasoning_efforts": "reasoningEfforts",
    "default_reasoning": "defaultReasoningEffort",
    "default_reasoning_effort": "defaultReasoningEffort",
    "defaultReasoning": "defaultReasoningEffort",
    "default_thinking": "defaultReasoningEffort",
    "default_thinking_effort": "defaultReasoningEffort",
    "thinking_default": "defaultReasoningEffort",
}


def normalize_model_name(model: Any) -> str:
    return str(model or "").strip()


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(float(str(value).replace(",", "").strip()))
    except Exception:
        return None
    return n if n > 0 else None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(str(value).replace(",", "").strip())
    except Exception:
        return None
    return n if n >= 0 else None


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "支持", "启用", "设为"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "不支持", "关闭", "禁用"}:
        return False
    return None


def _to_effort_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = re.split(r"[,，、;；\n]+", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        effort = re.sub(r"\s+", "", str(item)).lower()
        if not effort or effort in seen:
            continue
        seen.add(effort)
        out.append(effort)
    return out


def _to_effort(value: Any) -> str | None:
    items = _to_effort_list(value)
    return items[0] if items else None


def _canonical_key(key: Any) -> str:
    text = str(key or "").strip()
    return _FIELD_ALIASES.get(text, text)


def normalize_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        ck = _canonical_key(key)
        if ck in _INT_KEYS:
            n = _to_int(value)
            if n is not None:
                out[ck] = n
        elif ck in _FLOAT_KEYS:
            n = _to_float(value)
            if n is not None:
                out[ck] = n
        elif ck in _BOOL_KEYS:
            b = _to_bool(value)
            if b is not None:
                out[ck] = b
        elif ck in _LIST_KEYS:
            vals = _to_effort_list(value)
            if vals:
                out[ck] = vals
        elif ck in _STR_KEYS:
            val = _to_effort(value)
            if val:
                out[ck] = val
    return out


def _validate_metadata(meta: dict[str, Any]) -> None:
    efforts = meta.get("reasoningEfforts")
    default = meta.get("defaultReasoningEffort")
    if default:
        if not isinstance(efforts, list) or not efforts:
            raise ValueError("default reasoning effort requires reasoning efforts first")
        if default not in efforts:
            raise ValueError(f"default reasoning effort must be one of: {', '.join(efforts)}")


def all_metadata() -> dict[str, dict[str, Any]]:
    cfg = config.get()
    root = cfg.get("modelMetadata") or {}
    if not isinstance(root, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for model, meta in root.items():
        name = normalize_model_name(model)
        normalized = normalize_metadata(meta if isinstance(meta, dict) else {})
        if name and normalized:
            out[name] = normalized
    return out


def get_metadata(model: Any) -> dict[str, Any]:
    name = normalize_model_name(model)
    if not name:
        return {}
    return dict(all_metadata().get(name) or {})


def list_models() -> list[str]:
    return sorted(all_metadata().keys())


def set_metadata(model: str, meta: dict[str, Any]) -> None:
    name = normalize_model_name(model)
    if not name:
        raise ValueError("model name is required")
    normalized = normalize_metadata(meta)
    if not normalized:
        raise ValueError("metadata is empty or invalid")
    merged_for_validation = get_metadata(name)
    merged_for_validation.update(normalized)
    _validate_metadata(merged_for_validation)

    def _mutate(cfg: dict) -> None:
        root = cfg.setdefault("modelMetadata", {})
        if not isinstance(root, dict):
            root = {}
            cfg["modelMetadata"] = root
        # Only one compression model is active at a time.  This keeps runtime
        # selection deterministic and matches the Telegram "set as" wording.
        if normalized.get("compressionModel") is True:
            for item in root.values():
                if isinstance(item, dict):
                    item["compressionModel"] = False
        current = root.get(name) if isinstance(root.get(name), dict) else {}
        current = dict(current or {})
        current.update(normalized)
        _validate_metadata(current)
        root[name] = current

    config.update(_mutate)


def delete_metadata(model: str) -> bool:
    name = normalize_model_name(model)
    if not name:
        return False
    removed = [False]

    def _mutate(cfg: dict) -> None:
        root = cfg.get("modelMetadata") or {}
        if isinstance(root, dict) and name in root:
            del root[name]
            removed[0] = True

    config.update(_mutate)
    return removed[0]


def set_compression_model(model: str) -> None:
    meta = get_metadata(model)
    if not meta:
        meta = {}
    meta["compressionModel"] = True
    set_metadata(model, meta)


def clear_compression_model(model: str) -> bool:
    name = normalize_model_name(model)
    if not name:
        return False
    changed = [False]

    def _mutate(cfg: dict) -> None:
        root = cfg.get("modelMetadata") or {}
        item = root.get(name) if isinstance(root, dict) else None
        if isinstance(item, dict) and item.get("compressionModel"):
            item["compressionModel"] = False
            changed[0] = True

    config.update(_mutate)
    return changed[0]


def get_compression_model() -> str | None:
    for model, meta in all_metadata().items():
        if meta.get("compressionModel") is True:
            return model
    return None


def context_window(model: Any) -> int | None:
    return _to_int(get_metadata(model).get("contextWindow"))


def max_output_tokens(model: Any) -> int | None:
    return _to_int(get_metadata(model).get("maxOutputTokens"))


def _compact_rescue_int(key: str, default: int) -> int:
    root = config.get().get("compactRescue") or {}
    if not isinstance(root, dict):
        return default
    try:
        n = int(float(str(root.get(key)).replace(",", "").strip()))
    except Exception:
        return default
    return n if n > 0 else default


def summary_reserve_tokens(model: Any) -> int:
    reserve = _compact_rescue_int("summaryReserveTokens", SUMMARY_OUTPUT_RESERVE_TOKENS)
    max_out = max_output_tokens(model)
    if max_out is None:
        return reserve
    return max(1, min(max_out, reserve))


def compact_buffer_tokens() -> int:
    return _compact_rescue_int("safetyBufferTokens", DEFAULT_COMPACT_BUFFER_TOKENS)


def safe_prompt_limit(model: Any, *, buffer_tokens: int | None = None) -> int | None:
    window = context_window(model)
    if window is None:
        return None
    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    limit = window - summary_reserve_tokens(model) - buffer
    return max(0, limit)


def required_context_for_compact(
    prompt_tokens: int,
    model: Any,
    *,
    buffer_tokens: int | None = None,
) -> int:
    buffer = compact_buffer_tokens() if buffer_tokens is None else max(0, int(buffer_tokens))
    return max(0, int(prompt_tokens)) + summary_reserve_tokens(model) + buffer


def can_fit_for_compact(
    model: Any,
    prompt_tokens: int,
    *,
    buffer_tokens: int | None = None,
) -> bool:
    window = context_window(model)
    if window is None:
        return False
    return window >= required_context_for_compact(prompt_tokens, model, buffer_tokens=buffer_tokens)
