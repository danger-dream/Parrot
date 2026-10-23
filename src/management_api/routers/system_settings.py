"""Typed GET/sparse PATCH Management API routes for P6 system settings."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from src.management_auth import Capability
from src.management_control import ManagementContext, ManagementErrorCode

from ..dependencies import require_capability
from ..error_mapping import management_error_responses
from ..schemas.base import DataEnvelope
from ..schemas.system import (
    AffinitySettingsData, AffinitySettingsPatch,
    ApiKeyConcurrencySettingsData, ApiKeyConcurrencySettingsPatch,
    CchSettingsData, CchSettingsPatch,
    ConcurrencySettingsData, ConcurrencySettingsPatch,
    ErrorCooldownSettingsData, ErrorCooldownSettingsPatch,
    NotificationSettingsData, NotificationSettingsPatch,
    OpenAiWebSocketSettingsData, OpenAiWebSocketSettingsPatch,
    QuotaMonitorSettingsData, QuotaMonitorSettingsPatch,
    RetrySettingsData, RetrySettingsPatch,
    ScoringSettingsData, ScoringSettingsPatch,
    TimeoutSettingsData, TimeoutSettingsPatch,
    WebUiSettingsData, WebUiSettingsPatch,
)
from .system_support import (
    SystemNetworkControls,
    get_bound_system_network_controls,
    reject_unknown_query,
    response_meta,
    success_response,
)


router = APIRouter()
ReadContext = Annotated[ManagementContext, Depends(require_capability(Capability.READ))]
WriteContext = Annotated[ManagementContext, Depends(require_capability(Capability.WRITE))]
Controls = Annotated[SystemNetworkControls, Depends(get_bound_system_network_controls)]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
_ERRORS = (
    ManagementErrorCode.SESSION_REQUIRED, ManagementErrorCode.SESSION_EXPIRED,
    ManagementErrorCode.ORIGIN_DENIED, ManagementErrorCode.CAPABILITY_DENIED,
    ManagementErrorCode.VALIDATION_FAILED, ManagementErrorCode.REVISION_CONFLICT,
    ManagementErrorCode.DEPENDENCY_UNAVAILABLE, ManagementErrorCode.SERVICE_NOT_READY,
)


def _responses(example: dict):
    return {**success_response(200, example), **management_error_responses(*_ERRORS)}


def _data(model, value):
    return model.model_validate(asdict(value))


_RETRY = {
    "transient": {"enabled": True, "maxExtraAttempts": 2, "backoffSeconds": [0.75, 1.75],
                  "errors": {"openaiServerOverloaded": True, "openaiServerError": True, "claudeOverloaded": True, "xaiUnavailable": True}},
    "recovery": {"oauthRefresh": True, "invalidEncryptedContent": True, "claudeContext1mFallback": True},
    "revision": "rev_example",
}
_TIMEOUTS = {"connect": 10, "firstByte": 30, "idle": 120, "total": 600, "revision": "rev_example"}
_ERROR_COOLDOWN = {"errorWindows": [1, 3, 5, 10, 15, 0], "oauthGraceCount": 3, "ladderMinIntervalSeconds": 30, "permanentMinAgeSeconds": 300, "revision": "rev_example"}
_SCORING = {"emaAlpha": 0.25, "recentWindow": 50, "errorPenaltyFactor": 8, "explorationRate": 0.2, "revision": "rev_example"}
_AFFINITY = {"ttlMinutes": 30, "revision": "rev_example"}
_CCH = {"mode": "disabled", "revision": "rev_example"}
_CONCURRENCY = {"enabled": True, "queueWaitSeconds": 30, "defaultMaxConcurrent": 0, "revision": "rev_example"}
_AK_CONCURRENCY = {"enabled": True, "defaultMaxConcurrent": 5, "defaultMaxQueue": 50, "defaultQueueWaitSeconds": 1800, "revision": "rev_example"}
_QUOTA = {"enabled": False, "intervalSeconds": 60, "thresholdPercent": 95, "revision": "rev_example"}
_NOTIFICATIONS = {"enabled": True, "events": {"channelPermanent": True, "channelRecovered": True, "quotaDisabled": True, "quotaResumed": True, "quotaCooldown": True, "oauthRefreshed": True, "oauthRefreshFailed": True, "noChannels": True, "openaiStoreSaveFailed": True, "statusAlert": True, "appUpdate": True, "networkMonitor": True}, "revision": "rev_example"}
_WS = {"responsesUpstreamWsForOAuth": False, "revision": "rev_example"}
_WEB_UI = {"enabled": False, "revision": "rev_example"}


@router.get("/settings/retry", operation_id="getRetrySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[RetrySettingsData], responses=_responses(_RETRY))
def get_retry_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(RetrySettingsData, controls.settings.get(context, "retry")), meta=response_meta(request))


@router.patch("/settings/retry", operation_id="updateRetrySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[RetrySettingsData], responses=_responses(_RETRY))
def update_retry_settings(body: RetrySettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    value = controls.settings.update_retry(context, body.model_dump(exclude_unset=True), expected_revision=if_match)
    return DataEnvelope(data=_data(RetrySettingsData, value), meta=response_meta(request))


@router.get("/settings/timeouts", operation_id="getTimeoutSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[TimeoutSettingsData], responses=_responses(_TIMEOUTS))
def get_timeout_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(TimeoutSettingsData, controls.settings.get(context, "timeouts")), meta=response_meta(request))


@router.patch("/settings/timeouts", operation_id="updateTimeoutSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[TimeoutSettingsData], responses=_responses(_TIMEOUTS))
def update_timeout_settings(body: TimeoutSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(TimeoutSettingsData, controls.settings.update_timeouts(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/error-cooldown", operation_id="getErrorCooldownSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ErrorCooldownSettingsData], responses=_responses(_ERROR_COOLDOWN))
def get_error_cooldown_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(ErrorCooldownSettingsData, controls.settings.get(context, "error-cooldown")), meta=response_meta(request))


@router.patch("/settings/error-cooldown", operation_id="updateErrorCooldownSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ErrorCooldownSettingsData], responses=_responses(_ERROR_COOLDOWN))
def update_error_cooldown_settings(body: ErrorCooldownSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(ErrorCooldownSettingsData, controls.settings.update_error_cooldown(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/scoring", operation_id="getScoringSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ScoringSettingsData], responses=_responses(_SCORING))
def get_scoring_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(ScoringSettingsData, controls.settings.get(context, "scoring")), meta=response_meta(request))


@router.patch("/settings/scoring", operation_id="updateScoringSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ScoringSettingsData], responses=_responses(_SCORING))
def update_scoring_settings(body: ScoringSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(ScoringSettingsData, controls.settings.update_scoring(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/affinity", operation_id="getAffinitySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[AffinitySettingsData], responses=_responses(_AFFINITY))
def get_affinity_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(AffinitySettingsData, controls.settings.get(context, "affinity")), meta=response_meta(request))


@router.patch("/settings/affinity", operation_id="updateAffinitySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[AffinitySettingsData], responses=_responses(_AFFINITY))
def update_affinity_settings(body: AffinitySettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(AffinitySettingsData, controls.settings.update_affinity(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/cch", operation_id="getCchSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[CchSettingsData], responses=_responses(_CCH))
def get_cch_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(CchSettingsData, controls.settings.get(context, "cch")), meta=response_meta(request))


@router.patch("/settings/cch", operation_id="updateCchSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[CchSettingsData], responses=_responses(_CCH))
def update_cch_settings(body: CchSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(CchSettingsData, controls.settings.update_cch(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/concurrency", operation_id="getConcurrencySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ConcurrencySettingsData], responses=_responses(_CONCURRENCY))
def get_concurrency_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(ConcurrencySettingsData, controls.settings.get(context, "concurrency")), meta=response_meta(request))


@router.patch("/settings/concurrency", operation_id="updateConcurrencySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ConcurrencySettingsData], responses=_responses(_CONCURRENCY))
def update_concurrency_settings(body: ConcurrencySettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(ConcurrencySettingsData, controls.settings.update_concurrency(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/api-key-concurrency", operation_id="getApiKeyConcurrencySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ApiKeyConcurrencySettingsData], responses=_responses(_AK_CONCURRENCY))
def get_api_key_concurrency_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(ApiKeyConcurrencySettingsData, controls.settings.get(context, "api-key-concurrency")), meta=response_meta(request))


@router.patch("/settings/api-key-concurrency", operation_id="updateApiKeyConcurrencySettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[ApiKeyConcurrencySettingsData], responses=_responses(_AK_CONCURRENCY))
def update_api_key_concurrency_settings(body: ApiKeyConcurrencySettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(ApiKeyConcurrencySettingsData, controls.settings.update_api_key_concurrency(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/quota-monitor", operation_id="getQuotaMonitorSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[QuotaMonitorSettingsData], responses=_responses(_QUOTA))
def get_quota_monitor_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(QuotaMonitorSettingsData, controls.settings.get(context, "quota-monitor")), meta=response_meta(request))


@router.patch("/settings/quota-monitor", operation_id="updateQuotaMonitorSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[QuotaMonitorSettingsData], responses=_responses(_QUOTA))
def update_quota_monitor_settings(body: QuotaMonitorSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(QuotaMonitorSettingsData, controls.settings.update_quota_monitor(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/notifications", operation_id="getNotificationSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[NotificationSettingsData], responses=_responses(_NOTIFICATIONS))
def get_notification_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(NotificationSettingsData, controls.settings.get(context, "notifications")), meta=response_meta(request))


@router.patch("/settings/notifications", operation_id="updateNotificationSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[NotificationSettingsData], responses=_responses(_NOTIFICATIONS))
def update_notification_settings(body: NotificationSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(NotificationSettingsData, controls.settings.update_notifications(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/openai-websocket", operation_id="getOpenAiWebSocketSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[OpenAiWebSocketSettingsData], responses=_responses(_WS))
def get_openai_websocket_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(OpenAiWebSocketSettingsData, controls.settings.get(context, "openai-websocket")), meta=response_meta(request))


@router.patch("/settings/openai-websocket", operation_id="updateOpenAiWebSocketSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[OpenAiWebSocketSettingsData], responses=_responses(_WS))
def update_openai_websocket_settings(body: OpenAiWebSocketSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(OpenAiWebSocketSettingsData, controls.settings.update_websocket(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))


@router.get("/settings/web-ui", operation_id="getWebUiSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[WebUiSettingsData], responses=_responses(_WEB_UI))
def get_web_ui_settings(request: Request, context: ReadContext, controls: Controls):
    return DataEnvelope(data=_data(WebUiSettingsData, controls.settings.get(context, "web-ui")), meta=response_meta(request))


@router.patch("/settings/web-ui", operation_id="updateWebUiSettings", dependencies=[Depends(reject_unknown_query())], tags=["management-system"], response_model=DataEnvelope[WebUiSettingsData], responses=_responses(_WEB_UI))
def update_web_ui_settings(body: WebUiSettingsPatch, request: Request, context: WriteContext, controls: Controls, if_match: IfMatch = None):
    return DataEnvelope(data=_data(WebUiSettingsData, controls.settings.update_web_ui(context, body.model_dump(exclude_unset=True), expected_revision=if_match)), meta=response_meta(request))
