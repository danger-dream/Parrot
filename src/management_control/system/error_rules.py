"""CAS-safe management control for upstream error handling rules."""

from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from src import config as config_module
from src import error_rules as engine
from src.management_auth import Capability
from src.management_control.context import AuditSink, ManagementContext
from src.management_control.dependency_mapping import dependency_result
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.models.common import DomainControl, stable_revision

STATS_WINDOW_SECONDS = 7 * 86400
_HTTP_STATUS_RE = re.compile(r"^HTTP (\d{3})\b")
_VOLATILE_RE = re.compile(r"\b[0-9a-f]{12,}\b|\d+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ErrorRulesSnapshot:
    enabled: bool
    rules: tuple[dict[str, Any], ...]
    presets: tuple[dict[str, Any], ...]
    revision: str


def _provider_from_key(channel_key: str) -> str:
    parts = str(channel_key or "").split(":")
    return parts[1].lower() if len(parts) > 2 and parts[0] == "oauth" else ""


def _channel_label(channel_key: str) -> str:
    channel = _channel(channel_key)
    name = str(getattr(channel, "display_name", "") or getattr(channel, "name", "") or "") if channel else ""
    return name or channel_key


def _channel(channel_key: str):
    if not channel_key:
        return None
    from src.channel import registry
    return registry.get_channel(channel_key)


class ErrorRulesControl(DomainControl):
    def __init__(self, *, config=config_module, audit_sink: AuditSink | None = None, log_db=None) -> None:
        super().__init__(audit_sink=audit_sink)
        self.config = config
        self._log_db = log_db

    _dependency = staticmethod(dependency_result)

    @property
    def log_db(self):
        if self._log_db is None:
            from src import log_db
            return log_db
        return self._log_db

    # ── snapshot ────────────────────────────────────────────────────

    @classmethod
    def _projection(cls, cfg: dict[str, Any]) -> dict[str, Any]:
        # Shows what the runtime applies: a hand-broken rule is skipped, never the whole list.
        try:
            return engine.normalize_settings(cfg.get("errorRules"), tolerant=True)
        except engine.ErrorRuleValidationError:
            return copy.deepcopy(engine.DEFAULT_SETTINGS)

    @classmethod
    def _snapshot_from(cls, cfg: dict[str, Any]) -> ErrorRulesSnapshot:
        public = cls._projection(cfg)
        return ErrorRulesSnapshot(
            enabled=bool(public["enabled"]),
            rules=tuple(public["rules"]),
            presets=tuple(engine.preset_rules(public)),
            revision=stable_revision(public),
        )

    @staticmethod
    def _validation_error(exc: Exception) -> ManagementError:
        return ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField("errorRules", "INVALID_VALUE", str(exc)),),
        )

    def _update_config(self, mutate) -> None:
        try:
            self.config.update(mutate)
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True) from exc

    def _mutate(self, expected_revision: str | None, change) -> ErrorRulesSnapshot:
        result: dict[str, ErrorRulesSnapshot] = {}

        def mutate(cfg: dict[str, Any]) -> None:
            current = self._snapshot_from(cfg)
            self._check_revision(expected_revision, current.revision)
            try:
                public = engine.normalize_settings(cfg.get("errorRules"))
            except engine.ErrorRuleValidationError as exc:
                # Saving would silently drop the broken rule; make the operator fix it first.
                raise self._validation_error(ValueError(
                    f"config.json 里的错误处理规则有问题，先修正或删除它再保存：{exc}")) from exc
            change(public)
            try:
                cfg["errorRules"] = engine.normalize_settings(public)
            except engine.ErrorRuleValidationError as exc:
                raise self._validation_error(exc) from exc
            result["snapshot"] = self._snapshot_from(cfg)

        self._update_config(mutate)
        return result["snapshot"]

    @staticmethod
    def builtins() -> list[dict[str, str]]:
        return [dict(item) for item in engine.BUILTINS]

    @staticmethod
    def categories() -> list[dict[str, Any]]:
        return engine.categories_public()

    @staticmethod
    def providers() -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Provider ids as the engine sees them, with channel counts, and each channel's provider."""
        from src.channel import registry
        counts: dict[str, int] = {}
        by_channel: dict[str, str] = {}
        for channel in registry.all_channels():
            provider = engine.provider_of(channel)
            if not provider:
                continue
            by_channel[channel.key] = provider
            counts[provider] = counts.get(provider, 0) + 1
        def walk(node: dict[str, Any]) -> None:
            for item in node["items"]:
                if item["kind"] == "group":
                    walk(item)
                elif item["field"] == "provider":
                    for provider in item["values"]:
                        counts.setdefault(provider, 0)

        for preset in engine.PRESETS:
            walk(preset["when"])
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"id": pid, "channelCount": n} for pid, n in ordered], by_channel

    def stats(self) -> dict[str, dict[str, Any]]:
        try:
            return self.log_db.error_rule_hit_stats(time.time() - STATS_WINDOW_SECONDS)
        except Exception:
            return {}

    def get(self, context: ManagementContext) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.READ)
        return self._snapshot_from(self._dependency(self.config.get))

    # ── writes ──────────────────────────────────────────────────────

    def set_enabled(self, context: ManagementContext, *, enabled: bool,
                    expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        if not isinstance(enabled, bool):
            raise self._validation_error(ValueError("enabled must be boolean"))
        snap = self._mutate(expected_revision, lambda public: public.__setitem__("enabled", enabled))
        self._audit(context, "error_rules.update_settings", "errorRules", "updated")
        return snap

    def _normalized(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return engine.normalize_rule(payload)
        except engine.ErrorRuleValidationError as exc:
            raise self._validation_error(exc) from exc

    def create_rule(self, context: ManagementContext, rule_payload: dict[str, Any], *,
                    expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        payload = dict(rule_payload)
        payload["id"] = f"rule-{uuid.uuid4().hex[:8]}"
        normalized = self._normalized(payload)

        def change(public: dict[str, Any]) -> None:
            while normalized["id"] in {r["id"] for r in public["rules"]}:
                normalized["id"] = f"rule-{uuid.uuid4().hex[:8]}"
            public["rules"].append(normalized)

        snap = self._mutate(expected_revision, change)
        self._audit(context, "error_rules.create_rule", f"errorRules:{normalized['id']}", "created")
        return snap

    def update_rule(self, context: ManagementContext, rule_id: str, rule_payload: dict[str, Any], *,
                    expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        target = str(rule_id or "").strip()
        payload = dict(rule_payload)
        payload["id"] = target
        normalized = self._normalized(payload)

        def change(public: dict[str, Any]) -> None:
            for index, existing in enumerate(public["rules"]):
                if existing["id"] == target:
                    public["rules"][index] = normalized
                    return
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

        snap = self._mutate(expected_revision, change)
        self._audit(context, "error_rules.update_rule", f"errorRules:{target}", "updated")
        return snap

    def delete_rule(self, context: ManagementContext, rule_id: str, *,
                    expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.DESTRUCTIVE)
        target = str(rule_id or "").strip()

        def change(public: dict[str, Any]) -> None:
            remaining = [r for r in public["rules"] if r["id"] != target]
            if len(remaining) == len(public["rules"]):
                raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
            public["rules"] = remaining

        snap = self._mutate(expected_revision, change)
        self._audit(context, "error_rules.delete_rule", f"errorRules:{target}", "deleted")
        return snap

    def reorder_rules(self, context: ManagementContext, rule_ids: list[str], *,
                      expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.WRITE)

        def change(public: dict[str, Any]) -> None:
            by_id = {r["id"]: r for r in public["rules"]}
            if len(rule_ids) != len(by_id) or set(rule_ids) != set(by_id):
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField("ruleIds", "MISMATCHED_IDS", "ruleIds must contain every existing rule id exactly once"),),
                )
            public["rules"] = [by_id[rid] for rid in rule_ids]

        snap = self._mutate(expected_revision, change)
        self._audit(context, "error_rules.reorder_rules", "errorRules", "reordered")
        return snap

    def set_preset_enabled(self, context: ManagementContext, preset_id: str, *, enabled: bool,
                           expected_revision: str | None = None) -> ErrorRulesSnapshot:
        self._authorize(context, Capability.WRITE)
        if preset_id not in engine.PRESET_IDS:
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if not isinstance(enabled, bool):
            raise self._validation_error(ValueError("enabled must be boolean"))
        snap = self._mutate(expected_revision,
                            lambda public: public["presets"].__setitem__(preset_id, {"enabled": enabled}))
        self._audit(context, "error_rules.update_preset", f"errorRules:{preset_id}", "updated")
        return snap

    # ── reads for the page ──────────────────────────────────────────

    def test(self, context: ManagementContext, *, channel_key: str = "", provider: str = "", model: str = "",
             phase: str = "http", status: int | None = None, body: str = "",
             draft_rule: dict[str, Any] | None = None) -> dict[str, Any]:
        self._authorize(context, Capability.READ)
        draft = None
        if draft_rule is not None:
            payload = dict(draft_rule)
            payload["id"] = "draft-preview"
            draft = self._normalized(payload)
        channel = _channel(channel_key)
        obs = engine.observation_from_parts(
            phase=phase if phase in engine.PHASES else "http", status=status, outcome="http_error",
            provider=(provider or (engine.provider_of(channel) if channel else _provider_from_key(channel_key))),
            channel_key=channel_key, model=model, body=body,
            channel_type=str(getattr(channel, "type", "") or ("oauth" if channel_key.startswith("oauth:") else "api" if channel_key else "")),
        )
        settings = self._projection(self._dependency(self.config.get))
        progress = None
        if channel is not None:
            from src import channel_state, error_rule_effects
            effect_key = channel_state.effect_key(channel)
            progress = lambda found: error_rule_effects.progress(found, effect_key, model)  # noqa: E731
        return engine.explain(obs, settings, draft=draft, channel=channel, channel_label=_channel_label,
                              progress=progress)

    def recent_errors(self, context: ManagementContext, *, hours: int = 24, limit: int = 60) -> dict[str, Any]:
        """Failed upstream attempts grouped by provider, status, code and message shape."""
        self._authorize(context, Capability.READ)
        hours = max(1, min(int(hours), 24 * 7))
        since = time.time() - hours * 3600
        try:
            rows = self.log_db.recent_upstream_failures(since, limit=3000)
        except Exception as exc:
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True) from exc
        settings = self._projection(self._dependency(self.config.get))
        names = {r["id"]: (r["name"], "user") for r in settings["rules"]}
        names.update({p["id"]: (p["name"], "preset") for p in engine.PRESETS})
        groups: dict[str, dict[str, Any]] = {}
        channels: dict[str, Any] = {}
        for row in rows:
            key = str(row.get("channel_key") or "")
            if key not in channels:
                channels[key] = _channel(key)
            channel = channels[key]
            detail = str(row.get("error_detail") or "")
            m = _HTTP_STATUS_RE.match(detail)
            status = int(m.group(1)) if m else None
            outcome = str(row.get("outcome") or "")
            phase = engine.phase_of(outcome, status)
            provider = engine.provider_of(channel) if channel is not None else _provider_from_key(key)
            obs = engine.observation_from_parts(
                phase=phase, status=status, outcome=outcome, provider=provider, channel_key=key,
                model=str(row.get("model") or ""), body=detail,
                channel_type=str(row.get("channel_type") or ""),
            )
            shape = re.sub(r"\s+", " ", _VOLATILE_RE.sub("#", obs.message or outcome))[:160]
            gid = hashlib.sha256(f"{provider}|{status}|{outcome if status is None else ''}|{obs.codes[:1]}|{shape}".encode()).hexdigest()[:16]
            group = groups.get(gid)
            if group is None:
                handled = row.get("error_rule_id")
                name, layer = names.get(handled, (None, None)) if handled else (None, None)
                current = engine.explain(obs, settings, channel=channel)["matched"]
                group = groups[gid] = {
                    "id": gid, "provider": provider, "phase": phase, "status": status, "outcome": outcome,
                    "codes": list(obs.codes[:4]), "message": obs.message[:400], "sample": detail[:2000],
                    "count": 0, "firstAt": row.get("started_at"), "lastAt": row.get("started_at"),
                    "channels": [], "models": [], "channelType": str(row.get("channel_type") or ""),
                    "handledBy": ({"layer": layer or "user", "ruleId": handled, "ruleName": name or handled,
                                   "category": row.get("error_category")} if handled else None),
                    "currentRule": current,
                }
            group["count"] += 1
            group["firstAt"] = row.get("started_at")
            if key and key not in group["channels"] and len(group["channels"]) < 6:
                group["channels"].append(key)
            model = str(row.get("model") or "")
            if model and model not in group["models"] and len(group["models"]) < 6:
                group["models"].append(model)
        ordered = sorted(groups.values(), key=lambda g: g["lastAt"] or 0, reverse=True)
        return {"hours": hours, "total": len(rows), "truncated": len(rows) >= 3000, "groups": ordered[:max(1, min(int(limit), 200))]}
