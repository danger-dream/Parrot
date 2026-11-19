"""Strict camelCase schemas for outbound prompt rewrite rules."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from .base import StrictSchema


class _StrictRequest(StrictSchema):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, strict=True,
    )


PromptRuleProtocol = Literal["anthropic", "chat", "responses"]
PromptRuleTarget = Literal["all", "system", "user_text", "assistant_text", "tool_description", "tool_result_text"]
PromptRuleActionType = Literal["replace", "wrap", "prepend_system", "append_system"]
PromptRuleMode = Literal["literal", "regex"]


class PromptRuleMatchData(StrictSchema):
    models: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    apiKeys: list[str] = Field(default_factory=list)
    protocols: list[PromptRuleProtocol] = Field(default_factory=list)


class PromptRuleActionData(StrictSchema):
    type: PromptRuleActionType
    mode: PromptRuleMode = "literal"
    pattern: str = ""
    patterns: list[str] = Field(default_factory=list)
    replacement: str = ""
    prefix: str = ""
    suffix: str = ""
    skipIfWrapped: bool = True
    text: str = ""
    caseSensitive: bool = False


class PromptRuleData(StrictSchema):
    id: str
    name: str
    enabled: bool = True
    match: PromptRuleMatchData
    targets: list[PromptRuleTarget]
    action: PromptRuleActionData
    stopOnMatch: bool = False


class PromptRulesData(StrictSchema):
    enabled: bool
    rules: list[PromptRuleData]
    revision: str


class PromptRuleMatchInput(_StrictRequest):
    models: list[str] = Field(default_factory=list, max_length=200)
    channels: list[str] = Field(default_factory=list, max_length=200)
    providers: list[str] = Field(default_factory=list, max_length=200)
    apiKeys: list[str] = Field(default_factory=list, max_length=200)
    protocols: list[PromptRuleProtocol] = Field(default_factory=list, max_length=8)


class PromptRuleActionInput(_StrictRequest):
    type: PromptRuleActionType
    mode: PromptRuleMode = "literal"
    pattern: str = Field(default="", max_length=1024)
    patterns: list[str] = Field(default_factory=list, max_length=100)
    replacement: str = Field(default="", max_length=16384)
    prefix: str = Field(default="", max_length=256)
    suffix: str = Field(default="", max_length=256)
    skipIfWrapped: bool = True
    text: str = Field(default="", max_length=16384)
    caseSensitive: bool = False


class PromptRuleCreateRequest(_StrictRequest):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(default="", max_length=120)
    enabled: bool = True
    match: PromptRuleMatchInput = Field(default_factory=PromptRuleMatchInput)
    targets: list[PromptRuleTarget] = Field(default_factory=lambda: ["all"], min_length=1, max_length=16)
    action: PromptRuleActionInput
    stopOnMatch: bool = False


class PromptRuleUpdateRequest(_StrictRequest):
    name: str = Field(default="", max_length=120)
    enabled: bool = True
    match: PromptRuleMatchInput = Field(default_factory=PromptRuleMatchInput)
    targets: list[PromptRuleTarget] = Field(default_factory=lambda: ["all"], min_length=1, max_length=16)
    action: PromptRuleActionInput
    stopOnMatch: bool = False


class PromptRulesSettingsPatch(_StrictRequest):
    enabled: bool


class PromptRulesReorderRequest(_StrictRequest):
    ruleIds: list[str] = Field(max_length=200)


class PromptRulesTestSamples(_StrictRequest):
    system: str = Field(default="", max_length=65536)
    user_text: str = Field(default="", max_length=65536)
    assistant_text: str = Field(default="", max_length=65536)
    tool_description: str = Field(default="", max_length=65536)
    tool_result_text: str = Field(default="", max_length=65536)


class PromptRulesTestRequest(_StrictRequest):
    model: str = Field(default="", max_length=256)
    channelKey: str = Field(default="", max_length=512)
    provider: str = Field(default="", max_length=128)
    apiKeyName: str = Field(default="", max_length=128)
    protocol: PromptRuleProtocol = "responses"
    samples: PromptRulesTestSamples = Field(default_factory=PromptRulesTestSamples)
    draftRule: PromptRuleCreateRequest | None = None


class PromptRuleHitData(StrictSchema):
    id: str
    name: str
    actionType: PromptRuleActionType
    replacements: int
    targets: list[str]


class PromptRulesTestSamplesData(StrictSchema):
    system: str
    user_text: str
    assistant_text: str
    tool_description: str
    tool_result_text: str


class PromptRulesTestData(StrictSchema):
    enabled: bool
    changed: bool
    totalReplacements: int
    matchedRuleIds: list[str]
    hits: list[PromptRuleHitData]
    before: PromptRulesTestSamplesData
    after: PromptRulesTestSamplesData
