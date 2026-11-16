"""Account catalog fields shared by discovery and dispatch."""
from __future__ import annotations

import re

_MODEL_ENUM = re.compile(r"MODEL_[A-Z0-9_]+\Z")


def disabled(model: str, record: dict) -> bool:
    return record.get("disabled") is True or model.endswith("-disabled")


def model_enum(value: object) -> str:
    return value if isinstance(value, str) and _MODEL_ENUM.fullmatch(value) else ""


def thinking_level(value: object) -> str | int | None:
    if type(value) is int and 0 <= value <= 0x7FFFFFFF:
        return {1: "LOW", 2: "MEDIUM", 3: "HIGH"}.get(value, value)
    if isinstance(value, str) and value in {"LOW", "MEDIUM", "HIGH", "MINIMAL"}:
        return value
    return None
