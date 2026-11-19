"""CAS-safe management control for outbound prompt rewrite rules."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from src import config as config_module
from src import prompt_rules as engine
from src.management_auth import Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.dependency_mapping import dependency_result
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, stable_revision


@dataclass(frozen=True, slots=True)
class PromptRulesSnapshot:
    enabled: bool
    rules: tuple[dict[str, Any], ...]
    revision: str


class PromptRulesControl(DomainControl):
    def __init__(
        self,
        *,
        config=config_module,
        audit_sink: AuditSink | None = None,
    ) -> None:
        super().__init__(audit_sink=audit_sink)
        self.config = config

    _dependency = staticmethod(dependency_result)

    @classmethod
    def _projection(cls, cfg: dict[str, Any]) -> dict[str, Any]:
        raw = cfg.get("promptRules")
        try:
            return engine.normalize_settings(raw)
        except engine.PromptRuleValidationError:
            return copy.deepcopy(engine.DEFAULT_PROMPT_RULES)

    @classmethod
    def _snapshot_from(cls, cfg: dict[str, Any]) -> PromptRulesSnapshot:
        public = cls._projection(cfg)
        return PromptRulesSnapshot(
            enabled=bool(public["enabled"]),
            rules=tuple(public["rules"]),
            revision=stable_revision(public),
        )

    @staticmethod
    def _validation_error(exc: Exception) -> ManagementError:
        return ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField("promptRules", "INVALID_VALUE", str(exc)),),
        )

    def _update_config(self, mutate) -> None:
        try:
            self.config.update(mutate)
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
            ) from exc

    def get(self, context: ManagementContext) -> PromptRulesSnapshot:
        self._authorize(context, Capability.READ)
        cfg = self._dependency(self.config.get)
        return self._snapshot_from(cfg)

    def set_enabled(
        self,
        context: ManagementContext,
        *,
        enabled: bool,
        expected_revision: str | None = None,
    ) -> PromptRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        if not isinstance(enabled, bool):
            raise self._validation_error(ValueError("enabled must be boolean"))
        result: dict[str, PromptRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            public = self._projection(cfg)
            public["enabled"] = enabled
            cfg["promptRules"] = public
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        snap = result["snapshot"]
        self._audit(context, "prompt_rules.update_settings", "promptRules", "updated")
        return snap

    def create_rule(
        self,
        context: ManagementContext,
        rule_payload: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> PromptRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        payload = dict(rule_payload)
        auto_id = not str(payload.get("id") or "").strip()
        if auto_id:
            payload["id"] = f"rule-{uuid.uuid4().hex[:8]}"
        try:
            normalized = engine.normalize_rule(payload)
        except engine.PromptRuleValidationError as exc:
            raise self._validation_error(exc) from exc

        result: dict[str, PromptRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            nonlocal normalized
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            public = self._projection(cfg)
            existing_ids = {r["id"] for r in public["rules"]}
            if normalized["id"] in existing_ids:
                if auto_id:
                    while normalized["id"] in existing_ids:
                        payload["id"] = f"rule-{uuid.uuid4().hex[:8]}"
                        normalized = engine.normalize_rule(payload)
                else:
                    raise ManagementError(
                        ManagementErrorCode.RESOURCE_CONFLICT,
                        fields=(ErrorField("id", "DUPLICATE_RULE_ID", f"Rule id {normalized['id']!r} already exists"),),
                    )
            public["rules"].append(normalized)
            try:
                cfg["promptRules"] = engine.normalize_settings(public)
            except engine.PromptRuleValidationError as exc:
                raise self._validation_error(exc) from exc
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        snap = result["snapshot"]
        self._audit(context, "prompt_rules.create_rule", f"promptRules:{normalized['id']}", "created")
        return snap

    def update_rule(
        self,
        context: ManagementContext,
        rule_id: str,
        rule_payload: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> PromptRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        target_id = str(rule_id or "").strip()
        if not target_id:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        payload = dict(rule_payload)
        payload["id"] = target_id
        try:
            normalized = engine.normalize_rule(payload)
        except engine.PromptRuleValidationError as exc:
            raise self._validation_error(exc) from exc

        result: dict[str, PromptRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            public = self._projection(cfg)
            replaced = False
            for idx, existing in enumerate(public["rules"]):
                if existing["id"] == target_id:
                    public["rules"][idx] = normalized
                    replaced = True
                    break
            if not replaced:
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            try:
                cfg["promptRules"] = engine.normalize_settings(public)
            except engine.PromptRuleValidationError as exc:
                raise self._validation_error(exc) from exc
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        snap = result["snapshot"]
        self._audit(context, "prompt_rules.update_rule", f"promptRules:{target_id}", "updated")
        return snap

    def delete_rule(
        self,
        context: ManagementContext,
        rule_id: str,
        *,
        expected_revision: str | None = None,
    ) -> PromptRulesSnapshot:
        self._authorize(context, Capability.DESTRUCTIVE)
        target_id = str(rule_id or "").strip()
        if not target_id:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        result: dict[str, PromptRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            public = self._projection(cfg)
            remaining = [r for r in public["rules"] if r["id"] != target_id]
            if len(remaining) == len(public["rules"]):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            public["rules"] = remaining
            cfg["promptRules"] = public
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        snap = result["snapshot"]
        self._audit(context, "prompt_rules.delete_rule", f"promptRules:{target_id}", "deleted")
        return snap

    def reorder_rules(
        self,
        context: ManagementContext,
        rule_ids: list[str],
        *,
        expected_revision: str | None = None,
    ) -> PromptRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        result: dict[str, PromptRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            public = self._projection(cfg)
            by_id = {r["id"]: r for r in public["rules"]}
            if len(rule_ids) != len(by_id) or set(rule_ids) != set(by_id):
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField("ruleIds", "MISMATCHED_IDS", "ruleIds must contain every existing rule id exactly once"),),
                )
            public["rules"] = [by_id[rid] for rid in rule_ids]
            cfg["promptRules"] = public
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        snap = result["snapshot"]
        self._audit(context, "prompt_rules.reorder_rules", "promptRules", "reordered")
        return snap

    def test_rules(
        self,
        context: ManagementContext,
        *,
        samples: dict[str, str],
        model: str = "",
        channel_key: str = "",
        provider: str = "",
        api_key_name: str = "",
        ingress_protocol: str = "responses",
        draft_rule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(context, Capability.READ)
        settings_override: dict[str, Any] | None = None
        if draft_rule is not None:
            draft_payload = dict(draft_rule)
            if not str(draft_payload.get("id") or "").strip():
                draft_payload["id"] = "draft-preview"
            try:
                normalized_draft = engine.normalize_rule(draft_payload)
            except engine.PromptRuleValidationError as exc:
                raise self._validation_error(exc) from exc
            settings_override = {"enabled": True, "rules": [normalized_draft]}
        else:
            cfg = self._dependency(self.config.get)
            settings_override = self._projection(cfg)
        return engine.preview_rules(
            samples=samples,
            requested_model=model,
            resolved_model=model,
            channel_key=channel_key,
            provider=provider,
            api_key_name=api_key_name,
            ingress_protocol=ingress_protocol,
            settings_override=settings_override,
        )
