"""The account's own Qoder model list, read as Qoder's client asks for it."""
from __future__ import annotations

import re
from collections import Counter

from . import common, signing, device_identity
from ...safe_diagnostics import diagnose

EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")
# "auto" and "default" route inside Qoder and are not one model.
_ROUTERS = {"", "auto", "default"}


def _positive_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def windows_of(raw: dict) -> tuple[list[int], int]:
    """The context windows an entry offers, smallest first, and Qoder's default among them."""
    windows: list[int] = []
    default = 0
    config = raw.get("context_config") or raw.get("contextConfig")
    if isinstance(config, dict):
        for entry in config.values():
            if not isinstance(entry, dict):
                continue
            tokens = _positive_int(entry.get("token_count"))
            if not tokens:
                continue
            if tokens not in windows:
                windows.append(tokens)
            if entry.get("is_default") in (True, 1, "1"):
                default = tokens
    if not windows:
        values = raw.get("available_context_windows") or raw.get("availableContextWindows")
        for value in values if isinstance(values, list) else []:
            if _positive_int(value) and value not in windows:
                windows.append(value)
        default = _positive_int(raw.get("default_context_window") or raw.get("defaultContextWindow"))
        if default not in windows:
            default = 0
    return sorted(windows), default


def _rate(raw: dict) -> tuple[float | None, float, bool]:
    """Credits per request as a multiple (price_factor), the price struck through, and free."""
    def num(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    price = num(raw.get("price_factor", raw.get("priceFactor")))
    if price is None:
        return None, 0.0, raw.get("is_free", raw.get("isFree")) is True
    promotion = raw.get("promotion") or raw.get("prommotion") or {}
    promotion = promotion if isinstance(promotion, dict) else {}
    before = num(promotion.get("before_promotion_price_factor", promotion.get("beforePromotionPriceFactor")))
    discount = num(promotion.get("discount_factor", promotion.get("discountFactor"))) or 0.0
    discounted = promotion.get("active") is True and before is not None and before > 0
    rate = max(price, 0.0)
    was = max(num(raw.get("original_price_factor")) or 0.0, num(raw.get("originalPriceFactor")) or 0.0)
    free = price == 0 and not discounted
    if discounted:
        was = before
        if rate == 0:
            rate = before * discount
    return rate, (was if was > rate else 0.0), free


def model_record(raw: dict, scene: str) -> dict:
    key = common.text(raw.get("key"), "model id", required=True)
    windows, default_window = windows_of(raw)
    thinks = raw.get("is_reasoning") is True
    always = False
    efforts: list[str] = []
    default_effort = ""
    thinking = raw.get("thinking_config")
    if isinstance(thinking, dict):
        enabled = thinking.get("enabled")
        thinks = enabled is not None
        always = thinks and thinking.get("disabled") is None
        if thinks and isinstance(enabled, dict):
            for name, entry in (enabled.get("efforts") or {}).items():
                if isinstance(name, str) and name:
                    efforts.append(name)
                    if isinstance(entry, dict) and entry.get("is_default"):
                        default_effort = name
        rank = {name: index for index, name in enumerate(EFFORT_ORDER)}
        efforts.sort(key=lambda name: (rank.get(name, len(rank)), name))
    elif isinstance(raw.get("reasoning_efforts"), list):
        efforts = [name for name in raw["reasoning_efforts"] if isinstance(name, str) and name]
    rate, was, free = _rate(raw)
    record = {
        "id": key, "name": common.text(raw.get("display_name") or key, "model name"),
        "source": f"qoder:{scene}",
        "inputModalities": ["text", "image"] if raw.get("is_vl") is True else ["text"],
        "outputModalities": ["text"],
        "reasoning": thinks,
        "reasoningEfforts": efforts if thinks else [],
        "canDisableThinking": not always,
        "qoderAlwaysThinks": always,
        "qoderWindows": windows,
        "qoderDefaultWindow": default_window,
        "qoderMaxInputTokens": _positive_int(raw.get("max_input_tokens")),
        "qoderModelSource": common.text(raw.get("source"), "model source"),
        "qoderRate": rate, "qoderRateWas": was, "qoderFree": free,
        # Sent back verbatim as model_config on every request for this model.
        "qoderConfig": raw,
    }
    context = windows[-1] if windows else record["qoderMaxInputTokens"]
    if context:
        record["contextWindow"] = context
    if default_effort and thinks:
        record["defaultReasoningEffort"] = default_effort
    return record


def records_from_listing(listing) -> list[dict]:
    """The app scene's enabled models in its order, then any model only chat still lists, after the one it follows there."""
    if not isinstance(listing, dict):
        raise common.QoderError("models", kind="invalid_response")
    app = [item for item in listing.get("app") or [] if isinstance(item, dict)]
    chat = [item for item in listing.get("chat") or [] if isinstance(item, dict)]
    ordered = [(item, "app") for item in app]
    after = -1
    for item in chat:
        index = next((i for i, (seen, _scene) in enumerate(ordered) if seen.get("key") == item.get("key")), -1)
        if index >= 0:
            after = index
            continue
        after += 1
        ordered.insert(after, (item, "chat"))
    records = []
    for item, scene in ordered:
        key = str(item.get("key") or "").strip()
        if item.get("enable") is not True or key in _ROUTERS or any(r["id"] == key for r in records):
            continue
        try:
            records.append(model_record(item, scene))
        except ValueError:
            continue
    return records


def credit_label(record: dict) -> str:
    rate = record.get("qoderRate")
    free = record.get("qoderFree") is True
    if not free and (not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0):
        return ""
    label = "免费" if free else f"×{rate:g}"
    was = record.get("qoderRateWas")
    if isinstance(was, (int, float)) and not isinstance(was, bool) and was > (rate or 0):
        label += f"（原×{was:g}）"
    return label


def model_aliases(records: list[dict]) -> dict[str, str]:
    names = {}
    for record in records:
        key = str(record.get("id") or "").strip()
        name = str(record.get("name") or "").strip()
        slug = re.sub(r"[^a-z0-9.]+", "-", name.casefold()).strip("-.")
        if key and name != key and slug:
            names[key] = "qoder-" + slug
    counts = Counter(names.values())
    return {key: alias if counts[alias] == 1 else alias + "-" + re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")
            for key, alias in names.items()}


@diagnose("qoder", "models", "model_list")
def fetch_models_sync(account: dict, *, account_key: str = "", timeout: float = 15.0) -> list[dict]:
    url = common.site_of(account)["api"] + common.MODELS_PATH
    def prepare():
        device = device_identity.headers(account, account_key=account_key)
        return signing.cosy_headers(url, account, "", device=device)
    status, data = common.request(url, prepare_headers=prepare, account_key=account_key,
                                  timeout=timeout, stage="models")
    if status != 200:
        raise common.QoderError("models", status=status, code=common.error_code(data))
    records = records_from_listing(data)
    if not records:
        raise common.QoderError("models", status=status, kind="empty_catalog")
    return records
