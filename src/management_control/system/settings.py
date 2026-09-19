"""Atomic typed system-setting use cases shared by HTTP and Telegram."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Callable, Mapping

from src import config as config_module
from src.management_auth import AuthMethod, Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.dependency_mapping import dependency_result
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, stable_revision

from .models import (
    AffinitySettings,
    ApiKeyConcurrencySettings,
    CchSettings,
    ConcurrencySettings,
    ErrorCooldownSettings,
    NotificationEvents,
    NotificationSettings,
    OpenAiWebSocketSettings,
    QuotaMonitorSettings,
    RetryErrors,
    RetryRecovery,
    RetrySettings,
    RetryTransient,
    ScoringSettings,
    TimeoutSettings,
)


_RETRY_ERRORS = (
    "openaiServerOverloaded", "openaiServerError", "claudeOverloaded", "xaiUnavailable",
)
_RETRY_RECOVERY = (
    "oauthRefresh", "invalidEncryptedContent", "claudeContext1mFallback",
)
_NOTIFICATION_EVENTS = (
    ("channelPermanent", "channel_permanent"),
    ("channelRecovered", "channel_recovered"),
    ("quotaDisabled", "quota_disabled"),
    ("quotaResumed", "quota_resumed"),
    ("quotaCooldown", "quota_cooldown"),
    ("oauthRefreshed", "oauth_refreshed"),
    ("oauthRefreshFailed", "oauth_refresh_failed"),
    ("noChannels", "no_channels"),
    ("openaiStoreSaveFailed", "openai_store_save_failed"),
    ("modelDegraded", "model_degraded"),
    ("statusAlert", "status_alert"),
    ("appUpdate", "app_update"),
    ("networkMonitor", "network_monitor"),
)


def _int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and result < minimum:
        return default
    if maximum is not None and result > maximum:
        return default
    return result


def _float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) and minimum <= result <= maximum else default


def _public(record: Any) -> dict[str, Any]:
    value = asdict(record)
    value.pop("revision", None)
    return value


def _with_revision(cls, **values):
    return cls(**values, revision=stable_revision(values))


class SettingsControl(DomainControl):
    """One authoritative sparse-patch boundary over the serialized config store."""

    def __init__(self, *, config=config_module, audit_sink: AuditSink | None = None) -> None:
        super().__init__(audit_sink=audit_sink)
        self.config = config

    _dependency = staticmethod(dependency_result)

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _keys(value: Mapping[str, Any], allowed: set[str], path: str = "body") -> None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=tuple(ErrorField(f"{path}.{key}" if path else key, "extra_forbidden", "Unknown field") for key in unknown),
            )

    @staticmethod
    def _bool(value: Any, path: str) -> bool:
        if type(value) is not bool:
            raise SettingsControl._validation(path, "bool_type", "A boolean is required")
        return value

    @staticmethod
    def _integer(value: Any, path: str, minimum: int, maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsControl._validation(path, "int_type", "An integer is required")
        if value < minimum or (maximum is not None and value > maximum):
            raise SettingsControl._validation(path, "out_of_range", "Value is outside the supported range")
        return value

    @staticmethod
    def _number(value: Any, path: str, minimum: float, maximum: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsControl._validation(path, "float_type", "A number is required")
        result = float(value)
        if not math.isfinite(result) or result < minimum or result > maximum:
            raise SettingsControl._validation(path, "out_of_range", "Value is outside the supported range")
        return result

    def _retry(self, cfg: Mapping[str, Any]) -> RetrySettings:
        retry = self._mapping(cfg.get("retry"))
        transient = self._mapping(retry.get("transient"))
        recovery = self._mapping(retry.get("recovery"))
        errors = self._mapping(transient.get("errors"))
        delays_raw = transient.get("backoffSeconds")
        delays: list[float] = []
        if isinstance(delays_raw, (list, tuple)) and 1 <= len(delays_raw) <= 5:
            for value in delays_raw:
                parsed = _float(value, -1.0, minimum=0.0, maximum=60.0)
                if parsed < 0:
                    delays = []
                    break
                delays.append(parsed)
        if not delays:
            delays = [0.75, 1.75]
        values = {
            "transient": RetryTransient(
                enabled=bool(transient.get("enabled", True)),
                maxExtraAttempts=_int(transient.get("maxExtraAttempts"), 2, minimum=1, maximum=5),
                backoffSeconds=tuple(delays),
                errors=RetryErrors(**{key: bool(errors.get(key, True)) for key in _RETRY_ERRORS}),
            ),
            "recovery": RetryRecovery(**{key: bool(recovery.get(key, True)) for key in _RETRY_RECOVERY}),
        }
        return _with_revision(RetrySettings, **values)

    def _timeouts(self, cfg: Mapping[str, Any]) -> TimeoutSettings:
        raw = self._mapping(cfg.get("timeouts"))
        values = {
            "connect": _int(raw.get("connect"), 10, minimum=1),
            "firstByte": _int(raw.get("firstByte"), 30, minimum=1),
            "idle": _int(raw.get("idle"), 30, minimum=1),
            "total": _int(raw.get("total"), 600, minimum=1),
        }
        return _with_revision(TimeoutSettings, **values)

    def _error_cooldown(self, cfg: Mapping[str, Any]) -> ErrorCooldownSettings:
        raw_windows = cfg.get("errorWindows")
        windows: list[int] = []
        if isinstance(raw_windows, (list, tuple)) and raw_windows:
            for value in raw_windows:
                parsed = _int(value, -1, minimum=0)
                if parsed < 0:
                    windows = []
                    break
                windows.append(parsed)
        if not windows:
            windows = [1, 3, 5, 10, 15, 0]
        values = {
            "errorWindows": tuple(windows),
            "oauthGraceCount": _int(cfg.get("oauthGraceCount"), 3, minimum=0, maximum=100),
            "ladderMinIntervalSeconds": _int(cfg.get("cooldownLadderMinIntervalSeconds"), 30, minimum=0, maximum=3600),
            "permanentMinAgeSeconds": _int(cfg.get("cooldownPermanentMinAgeSeconds"), 300, minimum=0, maximum=86400),
        }
        return _with_revision(ErrorCooldownSettings, **values)

    def _scoring(self, cfg: Mapping[str, Any]) -> ScoringSettings:
        raw = self._mapping(cfg.get("scoring"))
        values = {
            "emaAlpha": _float(raw.get("emaAlpha"), 0.25, minimum=0, maximum=1),
            "recentWindow": _int(raw.get("recentWindow"), 50, minimum=1, maximum=1000),
            "errorPenaltyFactor": _int(raw.get("errorPenaltyFactor"), 8, minimum=0, maximum=100),
            "explorationRate": _float(raw.get("explorationRate"), 0.2, minimum=0, maximum=1),
        }
        return _with_revision(ScoringSettings, **values)

    def _affinity(self, cfg: Mapping[str, Any]) -> AffinitySettings:
        raw = self._mapping(cfg.get("affinity"))
        return _with_revision(AffinitySettings, ttlMinutes=_int(raw.get("ttlMinutes"), 30, minimum=1, maximum=1440))

    def _cch(self, cfg: Mapping[str, Any]) -> CchSettings:
        mode = str(cfg.get("cchMode") or "disabled")
        return _with_revision(CchSettings, mode=mode if mode in {"disabled", "dynamic"} else "disabled")

    def _concurrency(self, cfg: Mapping[str, Any]) -> ConcurrencySettings:
        raw = self._mapping(cfg.get("concurrency"))
        return _with_revision(
            ConcurrencySettings,
            enabled=bool(raw.get("enabled", True)),
            queueWaitSeconds=_int(raw.get("queueWaitSeconds"), 30, minimum=0),
            defaultMaxConcurrent=_int(raw.get("defaultMaxConcurrent"), 0, minimum=0),
        )

    def _api_key_concurrency(self, cfg: Mapping[str, Any]) -> ApiKeyConcurrencySettings:
        raw = self._mapping(cfg.get("apiKeyConcurrency"))
        return _with_revision(
            ApiKeyConcurrencySettings,
            enabled=bool(raw.get("enabled", True)),
            defaultMaxConcurrent=_int(raw.get("defaultMaxConcurrent"), 5, minimum=0),
            defaultMaxQueue=_int(raw.get("defaultMaxQueue"), 50, minimum=0),
            defaultQueueWaitSeconds=_int(raw.get("defaultQueueWaitSeconds"), 1800, minimum=0),
        )

    def _quota_monitor(self, cfg: Mapping[str, Any]) -> QuotaMonitorSettings:
        raw = self._mapping(cfg.get("quotaMonitor"))
        return _with_revision(
            QuotaMonitorSettings,
            enabled=bool(raw.get("enabled", False)),
            intervalSeconds=_int(raw.get("intervalSeconds"), 60, minimum=10, maximum=86400),
            thresholdPercent=_float(raw.get("disableThresholdPercent"), 95.0, minimum=1, maximum=100),
        )

    def _notifications(self, cfg: Mapping[str, Any]) -> NotificationSettings:
        raw = self._mapping(cfg.get("notifications"))
        events = self._mapping(raw.get("events"))
        values = {
            "enabled": bool(raw.get("enabled", True)),
            "events": NotificationEvents(**{public: bool(events.get(stored, True)) for public, stored in _NOTIFICATION_EVENTS}),
        }
        return _with_revision(NotificationSettings, **values)

    def _websocket(self, cfg: Mapping[str, Any]) -> OpenAiWebSocketSettings:
        raw = self._mapping(cfg.get("openai"))
        return _with_revision(OpenAiWebSocketSettings, responsesUpstreamWsForOAuth=bool(raw.get("responsesUpstreamWsForOAuth", False)))

    _READERS: dict[str, str] = {
        "retry": "_retry", "timeouts": "_timeouts", "error-cooldown": "_error_cooldown",
        "scoring": "_scoring", "affinity": "_affinity", "cch": "_cch",
        "concurrency": "_concurrency", "api-key-concurrency": "_api_key_concurrency",
        "quota-monitor": "_quota_monitor", "notifications": "_notifications",
        "openai-websocket": "_websocket",
    }

    def get(self, context: ManagementContext | None, resource: str):
        self._read(context)
        reader = self._READERS.get(resource)
        if reader is None:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return self._dependency(
            lambda: getattr(self, reader)(self.config.get()),
        )

    def _update(
        self,
        context: ManagementContext | None,
        resource: str,
        patch: Mapping[str, Any],
        expected_revision: str | None,
        mutator: Callable[[dict[str, Any]], None],
    ):
        actual = self._write(context, Capability.WRITE)
        action = f"settings.{resource}.update"
        if not patch:
            self._audit(actual, action, resource, "failed")
            raise self._validation("body", "empty_patch", "At least one field is required")
        if actual.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
            # The frozen Telegram trace is exactly one authoritative config write.
            # Preserve the original exception type/message and do not add a
            # post-commit read, DTO conversion, or audit failure point.
            self.config.update(mutator)
            return None
        dependency_failed = False
        revision_conflict = False
        result = None
        try:
            with self.config.serialized_updates():
                current = self._dependency(
                    lambda: getattr(self, self._READERS[resource])(self.config.get()),
                )
                revision_conflict = (
                    expected_revision is not None
                    and expected_revision != current.revision
                )
                if not revision_conflict:
                    self._dependency(lambda: self.config.update(mutator))
                    result = self._dependency(
                        lambda: getattr(self, self._READERS[resource])(self.config.get()),
                    )
        except Exception:
            dependency_failed = True
        if dependency_failed:
            self._audit(actual, action, resource, "failed")
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
            )
        if revision_conflict:
            self._audit(actual, action, resource, "failed")
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if result is None:
            # A transaction manager that suppresses a body failure is itself an
            # unavailable dependency boundary, not a successful mutation.
            self._audit(actual, action, resource, "failed")
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
            )
        self._audit(actual, action, resource, "succeeded")
        return result

    def update_retry(self, context, patch: Mapping[str, Any], *, expected_revision=None) -> RetrySettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"transient", "recovery"})
        if "transient" in patch and not isinstance(patch["transient"], Mapping):
            raise self._validation("transient", "dict_type", "An object is required")
        if "recovery" in patch and not isinstance(patch["recovery"], Mapping):
            raise self._validation("recovery", "dict_type", "An object is required")
        transient = self._mapping(patch.get("transient")) if "transient" in patch else None
        recovery = self._mapping(patch.get("recovery")) if "recovery" in patch else None
        if transient is not None:
            self._keys(transient, {"enabled", "maxExtraAttempts", "backoffSeconds", "errors"}, "transient")
            if "enabled" in transient: self._bool(transient["enabled"], "transient.enabled")
            if "maxExtraAttempts" in transient: self._integer(transient["maxExtraAttempts"], "transient.maxExtraAttempts", 1, 5)
            if "backoffSeconds" in transient:
                delays = transient["backoffSeconds"]
                if not isinstance(delays, (list, tuple)) or not 1 <= len(delays) <= 5:
                    raise self._validation("transient.backoffSeconds", "length", "One to five values are required")
                for index, value in enumerate(delays): self._number(value, f"transient.backoffSeconds[{index}]", 0, 60)
            if "errors" in transient:
                if not isinstance(transient["errors"], Mapping):
                    raise self._validation("transient.errors", "dict_type", "An object is required")
                errors = self._mapping(transient["errors"])
                self._keys(errors, set(_RETRY_ERRORS), "transient.errors")
                for key, value in errors.items(): self._bool(value, f"transient.errors.{key}")
        if recovery is not None:
            self._keys(recovery, set(_RETRY_RECOVERY), "recovery")
            for key, value in recovery.items(): self._bool(value, f"recovery.{key}")

        def mutate(cfg):
            retry = cfg.setdefault("retry", {})
            if transient is not None:
                target = retry.setdefault("transient", {})
                for key in ("enabled", "maxExtraAttempts"):
                    if key in transient: target[key] = transient[key]
                if "backoffSeconds" in transient: target["backoffSeconds"] = [round(float(v), 3) for v in transient["backoffSeconds"]]
                if "errors" in transient: target.setdefault("errors", {}).update(transient["errors"])
            if recovery is not None: retry.setdefault("recovery", {}).update(recovery)
        return self._update(context, "retry", patch, expected_revision, mutate)

    def update_timeouts(self, context, patch, *, expected_revision=None) -> TimeoutSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"connect", "firstByte", "idle", "total"})
        for key, value in patch.items(): self._integer(value, key, 1)
        return self._update(context, "timeouts", patch, expected_revision, lambda cfg: cfg.setdefault("timeouts", {}).update(patch))

    def update_error_cooldown(self, context, patch, *, expected_revision=None) -> ErrorCooldownSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"errorWindows", "oauthGraceCount", "ladderMinIntervalSeconds", "permanentMinAgeSeconds"})
        if "errorWindows" in patch:
            values = patch["errorWindows"]
            if not isinstance(values, (list, tuple)) or not values:
                raise self._validation("errorWindows", "min_length", "At least one value is required")
            for index, value in enumerate(values): self._integer(value, f"errorWindows[{index}]", 0)
        for key, maximum in (("oauthGraceCount", 100), ("ladderMinIntervalSeconds", 3600), ("permanentMinAgeSeconds", 86400)):
            if key in patch: self._integer(patch[key], key, 0, maximum)
        def mutate(cfg):
            if "errorWindows" in patch: cfg["errorWindows"] = list(patch["errorWindows"])
            if "oauthGraceCount" in patch: cfg["oauthGraceCount"] = patch["oauthGraceCount"]
            if "ladderMinIntervalSeconds" in patch: cfg["cooldownLadderMinIntervalSeconds"] = patch["ladderMinIntervalSeconds"]
            if "permanentMinAgeSeconds" in patch: cfg["cooldownPermanentMinAgeSeconds"] = patch["permanentMinAgeSeconds"]
        return self._update(context, "error-cooldown", patch, expected_revision, mutate)

    def update_scoring(self, context, patch, *, expected_revision=None) -> ScoringSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"emaAlpha", "recentWindow", "errorPenaltyFactor", "explorationRate"})
        if "emaAlpha" in patch: self._number(patch["emaAlpha"], "emaAlpha", 0, 1)
        if "recentWindow" in patch: self._integer(patch["recentWindow"], "recentWindow", 1, 1000)
        if "errorPenaltyFactor" in patch: self._integer(patch["errorPenaltyFactor"], "errorPenaltyFactor", 0, 100)
        if "explorationRate" in patch: self._number(patch["explorationRate"], "explorationRate", 0, 1)
        return self._update(context, "scoring", patch, expected_revision, lambda cfg: cfg.setdefault("scoring", {}).update(patch))

    def update_affinity(self, context, patch, *, expected_revision=None) -> AffinitySettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"ttlMinutes"})
        if "ttlMinutes" in patch: self._integer(patch["ttlMinutes"], "ttlMinutes", 1, 1440)
        return self._update(context, "affinity", patch, expected_revision, lambda cfg: cfg.setdefault("affinity", {}).update(patch))

    def update_cch(self, context, patch, *, expected_revision=None) -> CchSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"mode"})
        if "mode" in patch and patch["mode"] not in {"disabled", "dynamic"}:
            raise self._validation("mode", "unsupported_value", "Unsupported CCH mode")
        return self._update(context, "cch", patch, expected_revision, lambda cfg: cfg.__setitem__("cchMode", patch["mode"]) if "mode" in patch else None)

    def update_concurrency(self, context, patch, *, expected_revision=None) -> ConcurrencySettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"enabled", "queueWaitSeconds", "defaultMaxConcurrent"})
        if "enabled" in patch: self._bool(patch["enabled"], "enabled")
        for key in ("queueWaitSeconds", "defaultMaxConcurrent"):
            if key in patch: self._integer(patch[key], key, 0)
        return self._update(context, "concurrency", patch, expected_revision, lambda cfg: cfg.setdefault("concurrency", {}).update(patch))

    def update_api_key_concurrency(self, context, patch, *, expected_revision=None) -> ApiKeyConcurrencySettings:
        self._write(context, Capability.WRITE)
        allowed = {"enabled", "defaultMaxConcurrent", "defaultMaxQueue", "defaultQueueWaitSeconds"}
        self._keys(patch, allowed)
        if "enabled" in patch: self._bool(patch["enabled"], "enabled")
        for key in allowed - {"enabled"}:
            if key in patch: self._integer(patch[key], key, 0)
        return self._update(context, "api-key-concurrency", patch, expected_revision, lambda cfg: cfg.setdefault("apiKeyConcurrency", {}).update(patch))

    def quota_monitor_mutator(self, patch: Mapping[str, Any]) -> Callable[[dict[str, Any]], None]:
        """One quota validator/mutation, also used by the atomic OAuth+CCH update."""
        self._keys(patch, {"enabled", "intervalSeconds", "thresholdPercent"})
        if "enabled" in patch: self._bool(patch["enabled"], "enabled")
        if "intervalSeconds" in patch: self._integer(patch["intervalSeconds"], "intervalSeconds", 10, 86400)
        if "thresholdPercent" in patch: self._number(patch["thresholdPercent"], "thresholdPercent", 1, 100)
        def mutate(cfg):
            target = cfg.setdefault("quotaMonitor", {})
            if "enabled" in patch: target["enabled"] = patch["enabled"]
            if "intervalSeconds" in patch: target["intervalSeconds"] = patch["intervalSeconds"]
            if "thresholdPercent" in patch:
                target["disableThresholdPercent"] = patch["thresholdPercent"]
                target["resumeThresholdPercent"] = patch["thresholdPercent"]
        return mutate

    def update_quota_monitor(self, context, patch, *, expected_revision=None) -> QuotaMonitorSettings:
        self._write(context, Capability.WRITE)
        return self._update(
            context, "quota-monitor", patch, expected_revision,
            self.quota_monitor_mutator(patch),
        )

    def update_notifications(self, context, patch, *, expected_revision=None) -> NotificationSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"enabled", "events"})
        if "enabled" in patch: self._bool(patch["enabled"], "enabled")
        events = None
        if "events" in patch:
            if not isinstance(patch["events"], Mapping):
                raise self._validation("events", "dict_type", "An object is required")
            events = self._mapping(patch["events"])
            self._keys(events, {public for public, _stored in _NOTIFICATION_EVENTS}, "events")
            for key, value in events.items(): self._bool(value, f"events.{key}")
        def mutate(cfg):
            target = cfg.setdefault("notifications", {})
            if "enabled" in patch: target["enabled"] = patch["enabled"]
            if events is not None:
                stored = target.setdefault("events", {})
                names = dict(_NOTIFICATION_EVENTS)
                stored.update({names[key]: value for key, value in events.items()})
        return self._update(context, "notifications", patch, expected_revision, mutate)

    def update_websocket(self, context, patch, *, expected_revision=None) -> OpenAiWebSocketSettings:
        self._write(context, Capability.WRITE)
        self._keys(patch, {"responsesUpstreamWsForOAuth"})
        if "responsesUpstreamWsForOAuth" in patch: self._bool(patch["responsesUpstreamWsForOAuth"], "responsesUpstreamWsForOAuth")
        def mutate(cfg):
            target = cfg.setdefault("openai", {})
            if "responsesUpstreamWsForOAuth" in patch: target["responsesUpstreamWsForOAuth"] = patch["responsesUpstreamWsForOAuth"]
            target.pop("responsesUpstreamTransport", None)
            target.pop("responsesUpstreamWs", None)
        return self._update(context, "openai-websocket", patch, expected_revision, mutate)


DEFAULT_SETTINGS_CONTROL = SettingsControl()
