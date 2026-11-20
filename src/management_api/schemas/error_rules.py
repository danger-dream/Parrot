"""Strict camelCase schemas for upstream error handling rules."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import ConfigDict, Field, JsonValue

from .base import StrictSchema


class _StrictRequest(StrictSchema):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, strict=True,
    )


ErrorRuleCategory = Literal[
    "rate_limit", "quota_exhausted", "plan_excluded", "account_blocked",
    "request_invalid", "content_blocked", "upstream_busy", "observe",
]
ErrorRulePhase = Literal["http", "stream", "transport"]
ErrorRuleField = Literal["status", "code", "message", "body", "provider", "channel", "channelType", "model", "phase", "path"]
ErrorRuleOp = Literal[
    "eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", "regex",
    "exists", "not_exists", "gt", "gte", "lt", "lte",
]
ErrorRuleStepType = Literal["retry", "pause", "deprioritize", "disable", "notify"]
ErrorRuleFinal = Literal["next", "respond", "pass"]
ErrorRulePauseScope = Literal["model", "account"]
ErrorRulePauseMode = Literal["fixed", "message_time", "retry_after", "usage_window", "boundary", "ladder"]
ErrorRuleThresholdMode = Literal["consecutive", "window"]
ErrorRuleThresholdScope = Literal["model", "account", "rule"]
ErrorRuleLayer = Literal["draft", "user", "preset"]


# ── rule shape (responses) ──────────────────────────────────────────


class ErrorRuleCondData(StrictSchema):
    kind: Literal["cond"]
    field: ErrorRuleField
    op: ErrorRuleOp
    path: str = ""
    values: list[str] = Field(default_factory=list)


class ErrorRuleGroupData(StrictSchema):
    kind: Literal["group"]
    mode: Literal["all", "any"]
    items: list[Annotated[Union[ErrorRuleCondData, "ErrorRuleGroupData"], Field(discriminator="kind")]]


class ErrorRuleStepData(StrictSchema):
    type: ErrorRuleStepType
    delaySeconds: int | None = None
    times: int | None = None
    scope: ErrorRulePauseScope | None = None
    mode: ErrorRulePauseMode | None = None
    seconds: int | None = None
    pattern: str | None = None
    timezone: str | None = None
    boundaryHours: int | None = None
    fallbackSeconds: int | None = None
    reason: str | None = None
    repeatHours: int | None = None


class ErrorRuleThresholdData(StrictSchema):
    count: int
    mode: ErrorRuleThresholdMode
    windowMinutes: int
    scope: ErrorRuleThresholdScope
    steps: list[ErrorRuleStepData]


class ErrorRuleResponseData(StrictSchema):
    status: int = 0
    message: str = ""
    retryAfter: bool = False


class ErrorRuleData(StrictSchema):
    id: str
    name: str
    enabled: bool = True
    note: str = ""
    when: ErrorRuleGroupData
    category: ErrorRuleCategory
    steps: list[ErrorRuleStepData]
    final: ErrorRuleFinal
    thresholds: list[ErrorRuleThresholdData]
    response: ErrorRuleResponseData
    group: str = ""


class ErrorRuleHitStatData(StrictSchema):
    count: int
    lastAt: float | None = None


class ErrorRuleHandlingData(StrictSchema):
    steps: list[ErrorRuleStepData]
    final: ErrorRuleFinal
    thresholds: list[ErrorRuleThresholdData]
    response: ErrorRuleResponseData


class ErrorRuleCategoryData(StrictSchema):
    id: ErrorRuleCategory
    label: str
    description: str
    handling: ErrorRuleHandlingData


class ErrorRuleBuiltinData(StrictSchema):
    id: str
    name: str
    description: str
    setting: str
    link: str = ""


class ErrorRuleProviderData(StrictSchema):
    id: str
    channelCount: int


class ErrorRulesData(StrictSchema):
    enabled: bool
    rules: list[ErrorRuleData]
    presets: list[ErrorRuleData]
    builtins: list[ErrorRuleBuiltinData]
    categories: list[ErrorRuleCategoryData]
    providers: list[ErrorRuleProviderData]
    channelProviders: dict[str, str]
    stats: dict[str, ErrorRuleHitStatData]
    statsWindowDays: int
    revision: str


# ── rule shape (requests) ───────────────────────────────────────────


class ErrorRuleCondInput(_StrictRequest):
    kind: Literal["cond"]
    field: ErrorRuleField
    op: ErrorRuleOp
    path: str = Field(default="", max_length=200)
    values: list[str] = Field(default_factory=list, max_length=100)


class ErrorRuleGroupInput(_StrictRequest):
    kind: Literal["group"]
    mode: Literal["all", "any"] = "all"
    items: list[Annotated[Union[ErrorRuleCondInput, "ErrorRuleGroupInput"], Field(discriminator="kind")]] = Field(
        default_factory=list, max_length=40)


class ErrorRuleStepInput(_StrictRequest):
    type: ErrorRuleStepType
    delaySeconds: int | None = Field(default=None, ge=0, le=300)
    times: int | None = Field(default=None, ge=1, le=5)
    scope: ErrorRulePauseScope | None = None
    mode: ErrorRulePauseMode | None = None
    seconds: int | None = Field(default=None, ge=1, le=30 * 86400)
    pattern: str | None = Field(default=None, max_length=512)
    timezone: str | None = Field(default=None, max_length=6)
    boundaryHours: int | None = Field(default=None, ge=0, le=168)
    fallbackSeconds: int | None = Field(default=None, ge=0, le=30 * 86400)
    reason: str | None = Field(default=None, max_length=40)
    repeatHours: int | None = Field(default=None, ge=1, le=168)


class ErrorRuleThresholdInput(_StrictRequest):
    count: int = Field(ge=2, le=1000)
    mode: ErrorRuleThresholdMode = "consecutive"
    windowMinutes: int = Field(default=10, ge=1, le=7 * 24 * 60)
    scope: ErrorRuleThresholdScope = "model"
    steps: list[ErrorRuleStepInput] = Field(max_length=10)


class ErrorRuleResponseInput(_StrictRequest):
    status: int = Field(default=0, ge=0, le=599)
    message: str = Field(default="", max_length=500)
    retryAfter: bool = False


class ErrorRuleInput(_StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    note: str = Field(default="", max_length=500)
    when: ErrorRuleGroupInput
    category: ErrorRuleCategory
    steps: list[ErrorRuleStepInput] | None = Field(default=None, max_length=10)
    final: ErrorRuleFinal | None = None
    thresholds: list[ErrorRuleThresholdInput] | None = Field(default=None, max_length=5)
    response: ErrorRuleResponseInput | None = None

    def engine_payload(self) -> dict[str, Any]:
        """Omitted handling falls back to the category's recommended handling."""
        return self.model_dump(mode="python", exclude_none=True)


class ErrorRulesSettingsPatch(_StrictRequest):
    enabled: bool


class ErrorRulesReorderRequest(_StrictRequest):
    ruleIds: list[str] = Field(max_length=200)


class ErrorRulePresetPatch(_StrictRequest):
    enabled: bool


class ErrorRulesTestRequest(_StrictRequest):
    channelKey: str = Field(default="", max_length=512)
    provider: str = Field(default="", max_length=128)
    model: str = Field(default="", max_length=256)
    phase: ErrorRulePhase = "http"
    status: int | None = Field(default=None, ge=100, le=599)
    body: str = Field(default="", max_length=65536)
    draftRule: ErrorRuleInput | None = None


# ── dry run ─────────────────────────────────────────────────────────


class ErrorRuleObservationData(StrictSchema):
    phase: str
    status: int | None = None
    provider: str
    model: str
    codes: list[str]
    message: str
    data: JsonValue = None


class ErrorRuleTraceData(StrictSchema):
    layer: ErrorRuleLayer
    id: str
    name: str
    category: ErrorRuleCategory
    status: Literal["matched", "missed", "skipped"]
    reason: str


class ErrorRuleMatchedData(StrictSchema):
    layer: ErrorRuleLayer
    id: str
    name: str
    category: ErrorRuleCategory


class ErrorRulePlanResponseData(StrictSchema):
    status: int
    message: str
    retryAfter: int | None = None
    ruleId: str


class ErrorRulePlanStepData(ErrorRuleStepData):
    until: int | None = None
    source: str | None = None
    untilText: str | None = None


class ErrorRulePlanThresholdData(StrictSchema):
    count: int
    mode: ErrorRuleThresholdMode
    windowMinutes: int
    scope: ErrorRuleThresholdScope
    steps: list[ErrorRulePlanStepData]
    current: int | None = None


class ErrorRulePlanData(StrictSchema):
    passthrough: bool
    steps: list[ErrorRulePlanStepData]
    final: ErrorRuleFinal
    thresholds: list[ErrorRulePlanThresholdData]
    response: ErrorRulePlanResponseData | None = None


class ErrorRulesTestData(StrictSchema):
    observation: ErrorRuleObservationData
    trace: list[ErrorRuleTraceData]
    matched: ErrorRuleMatchedData | None = None
    plan: ErrorRulePlanData | None = None


class ErrorRuleHandledByData(StrictSchema):
    layer: str
    ruleId: str | None = None
    ruleName: str | None = None
    category: str | None = None


class ErrorRuleRecentGroupData(StrictSchema):
    id: str
    provider: str
    phase: ErrorRulePhase
    status: int | None = None
    outcome: str
    codes: list[str]
    message: str
    sample: str
    count: int
    firstAt: float | None = None
    lastAt: float | None = None
    channels: list[str]
    models: list[str]
    channelType: str = ""
    handledBy: ErrorRuleHandledByData | None = None
    currentRule: ErrorRuleMatchedData | None = None


class ErrorRulesRecentData(StrictSchema):
    hours: int
    total: int
    truncated: bool
    groups: list[ErrorRuleRecentGroupData]


ErrorRuleGroupData.model_rebuild()
ErrorRuleGroupInput.model_rebuild()
