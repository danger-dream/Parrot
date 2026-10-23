"""Schemas for Translation Management API operations."""

from __future__ import annotations

from pydantic import Field, JsonValue, model_validator

from .base import StrictSchema


class TranslationScopeSchema(StrictSchema):
    models: list[str] = Field(max_length=500)
    channels: list[str] = Field(max_length=500)


class TranslationModelOverrideSchema(StrictSchema):
    body: dict[str, JsonValue] = Field(default_factory=dict)


class TranslationSettingsData(StrictSchema):
    enabled: bool
    model: str
    fallbackModel: str
    targetLanguage: str
    timeoutSeconds: int
    maxHistoryMessages: int
    cacheTtlDays: int
    cachePreloadCount: int
    failureAlertThreshold: int
    memoryCacheMaxMb: int
    memoryCacheTtlSeconds: int
    translateSystemMessages: bool
    scope: TranslationScopeSchema
    modelOverrides: dict[str, TranslationModelOverrideSchema]
    prompt: str
    revision: str
    # Read-only: the built-in prompt applied when ``prompt`` is empty.
    defaultPrompt: str = ""


class TranslationSettingsPatch(StrictSchema):
    enabled: bool = False
    model: str = Field(default="", max_length=256)
    fallbackModel: str = Field(default="", max_length=256)
    targetLanguage: str = Field(default="English", min_length=1, max_length=128)
    timeoutSeconds: int = Field(default=10, ge=1, le=60)
    maxHistoryMessages: int = Field(default=20, ge=1, le=200)
    cacheTtlDays: int = Field(default=3, ge=1, le=30)
    cachePreloadCount: int = Field(default=100, ge=0, le=1000)
    failureAlertThreshold: int = Field(default=10, ge=0, le=100)
    memoryCacheMaxMb: int = Field(default=100, ge=0, le=1024)
    memoryCacheTtlSeconds: int = Field(default=7200, ge=0, le=86400)
    translateSystemMessages: bool = False
    scope: TranslationScopeSchema | None = None
    modelOverrides: dict[str, TranslationModelOverrideSchema] | None = None
    prompt: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def reject_null_non_reset_fields(self):
        for field in self.model_fields_set - {"prompt"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} must not be null")
        return self


class TranslationCacheData(StrictSchema):
    entries: int
    memoryEntries: int
    memoryBytes: int
    hits: int
    misses: int
    revision: str


class TranslationCacheClearedData(StrictSchema):
    clearedEntries: int


class TranslationTestRequest(StrictSchema):
    text: str = Field(min_length=1, max_length=20000)


class TranslationLanguageData(StrictSchema):
    id: str
    displayName: str


class TranslationLanguagesData(StrictSchema):
    items: list[TranslationLanguageData]
    total: int
