from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from src.management_api.routers.auxiliary_support import get_bound_auxiliary_controls
from src.management_api.routers.media_settings import router as media_router
from src.management_api.routers.status_alerts import router as status_router
from src.management_api.routers.translation import router as translation_router
from src.management_api.routers.updates import router as updates_router
from src.management_auth import AuthMethod
from src.management_control import BoundedAuditSink, OperationRegistry, OperationStore
from src.management_control.auxiliary import AuxiliaryControls
from src.management_control.auxiliary.media import (
    AntigravityMediaControl,
    ImageControl,
    XaiMediaControl,
)
from src.management_control.auxiliary.status_alerts import StatusAlertControl
from src.management_control.auxiliary.translation import (
    TRANSLATION_LANGUAGES,
    TranslationChannel,
    TranslationControl,
)
from src.management_control.auxiliary.updates import STAGE_IDLE, STAGE_STAGED, UpdateControl
from src.tests.test_management_api_foundation import bearer, build_app, create_session


class FakeConfig:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = copy.deepcopy(value)
        self.updates = 0

    def get(self) -> dict[str, Any]:
        return self.value

    def update(self, mutator):
        self.updates += 1
        mutator(self.value)
        return self.value


class FakeTranslation:
    def __init__(self, config: FakeConfig) -> None:
        self.config = config
        self.defaults = {
            "enabled": False,
            "model": "",
            "fallbackModel": "",
            "targetLanguage": "English",
            "prompt": "",
            "timeoutSeconds": 10,
            "maxHistoryMessages": 20,
            "cacheTtlDays": 3,
            "cachePreloadCount": 100,
            "failureAlertThreshold": 10,
            "memoryCacheMaxMb": 100,
            "memoryCacheTtlSeconds": 7200,
            "translateSystemMessages": False,
            "scope": {"models": [], "channels": []},
            "modelOverrides": {},
        }
        self.default_prompt = "Translate to {target_language}."
        self.clear_calls = 0
        self.test_calls: list[str] = []

    def settings(self):
        result = copy.deepcopy(self.defaults)
        result.update(copy.deepcopy(self.config.value.get("translation") or {}))
        return result

    def validate_ready(self, settings, *, require_enabled):
        return (bool(settings.get("model")), "model missing" if not settings.get("model") else "")

    def cache_count(self):
        return 3

    def cache_stats(self):
        return {"memoryEntries": 2, "memoryBytes": 1024, "hits": 5, "misses": 1}

    def clear_cache(self):
        self.clear_calls += 1
        return 3

    async def test_text(self, text):
        self.test_calls.append(text)
        return {
            "ok": True,
            "cached": False,
            "targetLanguage": "English",
            "original": text,
            "translated": "translated",
        }

    def available_models(self):
        return ["model-a", "model-b"]

    def available_channels(self):
        return [TranslationChannel(id="api:one", type="api", display_name="One")]


class FakeStatus:
    def __init__(self) -> None:
        self.active = {
            "claude": [{
                "id": "inc-1",
                "name": "Incident one",
                "impact": "major",
                "status": "investigating",
                "created_at": "2026-01-02T03:04:05Z",
                "updated_at": "2026-01-02T03:05:05Z",
                "shortlink": "https://status.example/inc-1",
            }],
            "openai": [],
            "cloudflare": [],
        }
        self.muted: list[dict[str, Any]] = []
        self.refreshes: list[str] = []
        self.forgotten: list[str] = []

    def snapshot_active(self):
        return copy.deepcopy(self.active)

    def list_muted(self):
        return copy.deepcopy(self.muted)

    def forget_provider(self, provider):
        self.forgotten.append(provider)
        self.active[provider] = []

    def refresh_provider(self, provider, *, raise_on_error=False):
        self.refreshes.append(provider)

    def list_recent(self, provider, limit, *, raise_on_error=False):
        return copy.deepcopy(self.active.get(provider, []))[:limit]

    def mute(self, provider, incident_id, name=""):
        self.muted.append({
            "provider": provider,
            "incident_id": incident_id,
            "name": name,
            "muted_at": 1000.0,
        })
        self.active[provider] = [row for row in self.active.get(provider, []) if row["id"] != incident_id]

    def unmute(self, provider, incident_id):
        self.muted = [row for row in self.muted if row["incident_id"] != incident_id]

    def provider_tag(self, provider):
        return provider

    def provider_label(self, provider):
        return provider.title()

    def impact_icon(self, impact):
        return "!"

    def status_icon(self, status):
        return "?"


class FakeUpdates:
    current_version = "0.31.13"

    def __init__(self, config: FakeConfig) -> None:
        self.config = config
        self.release = {
            "latest_version": "0.32.0",
            "latest_name": "Release",
            "latest_body": "changes",
            "latest_published_at": "2026-01-02T03:04:05Z",
            "latest_prerelease": False,
            "latest_url": "https://example.invalid/release",
        }
        self.update_state = {"stage": STAGE_IDLE, "mode": "docker"}
        self.progress = None
        self.stage_calls: list[str] = []
        self.stage_ok = True
        self.activate_calls = 0
        self.cancel_calls = 0

    def cached_release(self):
        return copy.deepcopy(self.release)

    def is_newer(self, version):
        return bool(version and version != self.current_version)

    def check_latest(self):
        return None

    def force_refresh(self):
        return None

    def _ignored(self):
        return self.config.value.setdefault("updateChecker", {}).setdefault("ignoredVersions", [])

    def add_ignored(self, version):
        values = sorted(set(self._ignored()) | {version})
        self.config.update(lambda root: root.setdefault("updateChecker", {}).__setitem__("ignoredVersions", values))

    def remove_ignored(self, version):
        values = [item for item in self._ignored() if item != version]
        self.config.update(lambda root: root.setdefault("updateChecker", {}).__setitem__("ignoredVersions", values))

    def clear_ignored(self):
        self.config.update(lambda root: root.setdefault("updateChecker", {}).__setitem__("ignoredVersions", []))

    def mode(self):
        return "docker"

    def state(self):
        return copy.deepcopy(self.update_state)

    def busy(self):
        return self.update_state["stage"] not in {STAGE_IDLE, "failed", "success", "rolled_back"}

    def backups(self):
        return [
            {"ref": "b2", "version": "0.31.13", "target_tag": "0.32.0", "mode": "docker", "ts": "20260202-030405"},
            {"ref": "b1", "version": "0.31.12", "target_tag": "0.31.13", "mode": "src", "ts": "20260101-010203"},
        ]

    def failure_log(self):
        return "api_token=top-secret\nhealth failed"

    def set_progress(self, callback):
        self.progress = callback

    def stage(self, version, *, chat_id=None, notify_msg_id=None):
        self.stage_calls.append(version)
        if not self.stage_ok:
            return False, "failed with private upstream detail"
        if self.progress:
            self.progress("backing_up", "backup")
            self.progress("pulling", "pull")
        self.update_state = {"stage": STAGE_STAGED, "mode": "docker", "target_tag": version, "message": "ready"}
        if self.progress:
            self.progress("staged", "ready")
        return True, "ready"

    def save_state(self, **fields):
        self.update_state.update(fields)

    def activate(self):
        self.activate_calls += 1
        self.update_state["stage"] = "success"
        return True, "accepted"

    def cancel(self):
        self.cancel_calls += 1
        self.update_state = {"stage": STAGE_IDLE, "mode": "docker"}
        return True, "cancelled"

    def activation_is_terminal(self):
        return True


class FakeMedia:
    def __init__(self, config: FakeConfig, data_dir: Path) -> None:
        self.config = config
        self._data_dir = data_dir
        self.image_defaults = {
            "enabled": True,
            "cacheEnabled": False,
            "mainModel": "gpt-5.4-mini",
            "toolModel": "gpt-image-2",
            "cachePath": "images",
            "cacheRetentionDays": 0,
            "cacheMaxBytes": 1073741824,
            "disabledAccounts": [],
        }
        self.xai_defaults = {
            "imageModels": ["grok-imagine-image"],
            "videoModels": ["grok-imagine-video"],
            "videoJobTtlSeconds": 10800,
            "mediaRequestTimeoutSeconds": 180,
        }
        self.account_key = "openai:user@example.com"
        self.account_email = "user@example.com"
        self.image_cooldown_until: Any = 0

    @property
    def data_dir(self):
        return str(self._data_dir)

    def image_settings(self):
        value = copy.deepcopy(self.image_defaults)
        value.update(copy.deepcopy(self.config.value.get("images") or {}))
        return value

    def image_accounts(self):
        disabled = {
            str(item).strip().lower()
            for item in self.image_settings().get("disabledAccounts", [])
        }
        aliases = {
            self.account_key.lower(),
            f"oauth:{self.account_key}".lower(),
            self.account_email.lower(),
            f"openai:{self.account_email}".lower(),
        }
        return [{
            "account_key": self.account_key,
            "email": self.account_email,
            "enabled": True,
            "image_disabled": bool(disabled & aliases),
            "image_cooldown_until": self.image_cooldown_until,
            "missing_account_id": False,
        }]

    def image_log(self, log_id):
        return None


class AuxiliaryFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.config = FakeConfig({
            "translation": {"model": "model-a"},
            "statusMonitor": {
                "enabled": True,
                "intervalSeconds": 60,
                "targets": ["claude", "openai", "cloudflare"],
                "minImpact": "minor",
            },
            "notifications": {"events": {"status_alert": True}},
            "updateChecker": {
                "enabled": True,
                "includePrerelease": False,
                "autoUpdate": False,
                "intervalSeconds": 3600,
                "ignoredVersions": [],
            },
            "images": {},
            "xaiOAuth": {},
            "antigravityOAuth": {},
        })
        self.translation_gateway = FakeTranslation(self.config)
        self.status_gateway = FakeStatus()
        self.update_gateway = FakeUpdates(self.config)
        self.media_gateway = FakeMedia(self.config, tmp_path)
        self.audit = BoundedAuditSink()
        immediate = lambda task, name: task()
        self.controls = AuxiliaryControls(
            translation=TranslationControl(
                config_gateway=self.config,
                translation_gateway=self.translation_gateway,
                audit_sink=self.audit,
                scheduler=immediate,
            ),
            status_alerts=StatusAlertControl(
                config_gateway=self.config,
                status_gateway=self.status_gateway,
                audit_sink=self.audit,
                scheduler=immediate,
            ),
            updates=UpdateControl(
                config_gateway=self.config,
                update_gateway=self.update_gateway,
                audit_sink=self.audit,
                scheduler=immediate,
                clock=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
            images=ImageControl(
                config_gateway=self.config,
                media_gateway=self.media_gateway,
                audit_sink=self.audit,
            ),
            xai_media=XaiMediaControl(
                config_gateway=self.config,
                media_gateway=self.media_gateway,
                audit_sink=self.audit,
            ),
            antigravity_media=AntigravityMediaControl(
                config_gateway=self.config,
                audit_sink=self.audit,
            ),
        )


def build_auxiliary_app(tmp_path: Path):
    app, runtime, _ = build_app(tmp_path)
    fixture = AuxiliaryFixture(tmp_path)
    fixture.controls.bind_operations(runtime.operations, runtime.operation_registry)
    app.dependency_overrides[get_bound_auxiliary_controls] = lambda: fixture.controls
    for domain_router in (translation_router, status_router, updates_router, media_router):
        app.include_router(domain_router, prefix="/api/management/v1")
    return app, runtime, fixture


__all__ = [
    "AuthMethod",
    "TRANSLATION_LANGUAGES",
    "bearer",
    "build_auxiliary_app",
    "create_session",
]
