"""API 渠道兼容策略的归一化与模型范围匹配。"""

from __future__ import annotations

from typing import Any


AUTO_MODE = "auto"
FORCE_MODE = "force"
VALID_MODES = {AUTO_MODE, FORCE_MODE}


def normalize_mode(value: Any) -> str:
    """未知/缺省值一律回落到自动透传，保持旧配置行为。"""
    mode = str(value or AUTO_MODE).strip().lower()
    return mode if mode in VALID_MODES else AUTO_MODE


def normalize_models(value: Any) -> list[str]:
    """返回去重后的真实上游模型名；空列表在强制模式下表示全部模型。"""
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        out.append(model)
    return out


def forced_for_model(mode: Any, models: Any, resolved_model: str) -> bool:
    """策略是否要求 Parrot 为这个真实上游模型主动开启能力。"""
    if normalize_mode(mode) != FORCE_MODE:
        return False
    selected = normalize_models(models)
    return not selected or str(resolved_model or "") in selected


def apply_forced_openai_fast_mode(
    channel: Any,
    payload: dict,
    resolved_model: str,
) -> bool:
    """按最终上游渠道策略补齐 OpenAI Fast wire 属性。"""
    check = getattr(channel, "forces_fast_mode", None)
    forced = bool(callable(check) and check(resolved_model))
    if forced:
        payload["service_tier"] = "priority"
    return forced


def apply_reasoning_effort_capability(
    payload: dict,
    reasoning_efforts: Any,
    *,
    protocol: str,
) -> bool:
    """At a concrete provider/model boundary, adapt explicit unsupported max.

    A non-empty ``reasoning_efforts`` list is authoritative model metadata.  We
    only map ``max`` when that list explicitly omits max and includes xhigh.
    Empty/missing metadata is unknown and therefore preserves the request.
    """
    if not isinstance(reasoning_efforts, (list, tuple, set)):
        return False
    supported = {
        str(value or "").strip().lower() for value in reasoning_efforts
        if str(value or "").strip()
    }
    if not supported or "max" in supported or "xhigh" not in supported:
        return False

    if protocol == "openai-chat":
        if str(payload.get("reasoning_effort") or "").strip().lower() != "max":
            return False
        payload["reasoning_effort"] = "xhigh"
        return True

    if protocol == "openai-responses":
        reasoning = payload.get("reasoning")
        if not isinstance(reasoning, dict):
            return False
        if str(reasoning.get("effort") or "").strip().lower() != "max":
            return False
        reasoning["effort"] = "xhigh"
        return True

    return False
