"""Shared validation for client-supplied inference/create model identifiers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ExplicitModelError(ValueError):
    message: str
    param: str = "model"

    def __str__(self) -> str:
        return self.message


def require_explicit_model(
    body: Mapping[str, Any],
    *,
    field: str = "model",
    param: str = "model",
) -> str:
    """Return a stripped non-empty model or raise a transport-neutral error.

    ``None`` (including an absent field), empty/blank strings and non-string
    values are all invalid. Callers choose their protocol envelope/close code.
    The function intentionally does not consult ingress defaults, compression
    settings, OAuth fallback lists or provider-internal media models.
    """

    value = body.get(field)
    if value is None:
        raise ExplicitModelError("model is required", param)
    if not isinstance(value, str):
        raise ExplicitModelError("model must be a non-empty string", param)
    normalized = value.strip()
    if not normalized:
        raise ExplicitModelError("model is required", param)
    return normalized
