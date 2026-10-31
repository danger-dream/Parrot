"""Pure per-key model rule data and validation, shared by config/control/runtime.

No configuration loading, inference routing, or management transport imports.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator


class ModelBlockRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    preset: Literal["quota_exhausted", "insufficient_balance", "service_busy", "model_unavailable"] = "quota_exhausted"
    message: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None

    @model_validator(mode="after")
    def clean_message(self):
        if self.message and any(ord(c) < 32 and c not in "\n\t" for c in self.message):
            raise ValueError("message contains control characters")
        return self


Rules = Annotated[list[ModelBlockRule], Field(max_length=100)]
_RULES = TypeAdapter(Rules)


def validate_rules(value) -> list[dict]:
    rules = _RULES.validate_python(value)
    models = [rule.model for rule in rules]
    if len(set(models)) != len(models):
        raise ValueError("modelBlocks must not contain duplicate models")
    return [rule.model_dump() for rule in rules]

