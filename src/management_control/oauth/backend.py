"""Authoritative adapters from OAuth controls to the existing domain modules."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Iterable

from src import affinity, config, cooldown, load_balancing, log_db, oauth_manager, state_db
from src.cursor_bridge import catalog as cursor_model_catalog
from src.oauth import antigravity as antigravity_provider
from src.oauth import cursor as cursor_provider
from src.oauth import openai as openai_provider
from src.oauth import xai as xai_provider
from src.oauth.openai_import import OpenAIImportCandidate, parse_openai_import_payload


class OAuthBackend:
    """Small, patch-friendly wrapper over the repository's existing OAuth APIs."""

    def __init__(self, *, settings_control=None) -> None:
        if settings_control is None:
            from ..system.settings import DEFAULT_SETTINGS_CONTROL
            settings_control = DEFAULT_SETTINGS_CONTROL
        self._settings_control = settings_control

    def config_snapshot(self) -> dict:
        return copy.deepcopy(config.get())

    def list_accounts(self) -> list[dict]:
        return oauth_manager.list_accounts()

    def get_account(self, account_id: str) -> dict | None:
        return oauth_manager.get_account(account_id)

    def get_account_exact(self, account_id: str) -> dict | None:
        """Management IDs accept only exact canonical equality, never TG aliases."""
        matches = [
            account for account in oauth_manager.list_accounts()
            if oauth_manager.get_account_key(account) == account_id
        ]
        return matches[0] if len(matches) == 1 else None

    def account_id(self, account: dict) -> str:
        return oauth_manager.get_account_key(account)

    def provider_of(self, account_or_id: dict | str) -> str:
        return oauth_manager.provider_of(account_or_id)

    def resolve_account_id(self, value: str | None) -> str | None:
        return oauth_manager.resolve_account_key(value)

    def resolve_account_id_or_none(self, value: str | None) -> str | None:
        try:
            return oauth_manager.resolve_account_key(value)
        except oauth_manager.AmbiguousOAuthAccountKey:
            return None

    def account_email(self, account_id: str) -> str:
        return oauth_manager.account_key_to_email(account_id)

    def find_exact_identity(self, entry: dict) -> tuple[str, dict] | None:
        return oauth_manager.find_exact_identity(entry)

    def add_account_if_absent(self, entry: dict) -> dict:
        return oauth_manager.add_account_if_identity_absent(entry)

    def commit_import_conditional(
        self,
        expected_accounts: list[dict],
        candidates: tuple[dict, ...],
        choices: dict[str, str],
    ) -> dict:
        """Delegate one-candidate import CAS/publication to the OAuth domain."""
        return oauth_manager.commit_import_batch_if_unchanged(
            expected_accounts, candidates, choices,
        )

    def replace_exact_identity(self, account_id: str, entry: dict) -> dict:
        return oauth_manager.replace_exact_identity(account_id, entry)

    def replace_exact_identity_conditional(
        self, account_id: str, entry: dict, expected_account: dict,
    ) -> dict:
        return oauth_manager.replace_exact_identity(
            account_id, entry, expected_account=expected_account,
        )

    def delete_account(self, account_id: str) -> None:
        oauth_manager.delete_account(account_id)

    def delete_account_conditional(self, account_id: str, expected_account: dict) -> dict:
        return oauth_manager.delete_account_if_unchanged(account_id, expected_account)

    def delete_invalid_accounts_conditional(
        self,
        expected_accounts: tuple[tuple[str, dict], ...],
    ) -> dict:
        """Delegate one-candidate config/LB delete and post-publish cleanup."""
        return oauth_manager.delete_invalid_accounts_batch_if_unchanged(
            expected_accounts,
        )

    def update_account_conditional(
        self,
        account_id: str,
        expected_account: dict,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        max_concurrent: int | None = None,
    ) -> dict:
        def mutate(account: dict) -> None:
            if display_name is not None:
                if display_name:
                    account["label"] = display_name
                else:
                    account.pop("label", None)
            if enabled is not None:
                account["enabled"] = bool(enabled)
                account["disabled_reason"] = None if enabled else "user"
                account["disabled_until"] = None
                account.pop("quota_observation", None)
            if max_concurrent is not None:
                account["maxConcurrent"] = max(0, int(max_concurrent or 0))

        return oauth_manager.mutate_account_if_unchanged(
            account_id, expected_account, mutate,
        )

    def update_account_models_conditional(
        self,
        account_id: str,
        expected_account: dict,
        *,
        visible_models: set[str],
        disabled_models: set[str],
    ) -> dict:
        def mutate(account: dict) -> None:
            hidden = oauth_manager.account_disabled_models(account) - visible_models
            field = (
                "cursor_disabled_models"
                if oauth_manager.provider_of(account) == "cursor"
                else "disabledModels"
            )
            account[field] = sorted(hidden | disabled_models)

        return oauth_manager.mutate_account_if_unchanged(
            account_id, expected_account, mutate,
        )

    def update_cursor_model_setting_conditional(
        self,
        account_id: str,
        expected_account: dict,
        *,
        model_id: str,
        enabled: bool,
    ) -> dict:
        def mutate(account: dict) -> None:
            disabled = oauth_manager.cursor_max_context_disabled_models(account)
            if enabled:
                disabled.discard(model_id)
            else:
                disabled.add(model_id)
            account["cursor_max_context_disabled_models"] = sorted(disabled)
            account.pop("cursor_max_context_models", None)

        return oauth_manager.mutate_account_if_unchanged(
            account_id, expected_account, mutate,
        )

    def set_enabled(
        self,
        account_id: str,
        enabled: bool,
        *,
        reason: str | None = None,
    ) -> None:
        oauth_manager.set_enabled(account_id, enabled, reason=reason)

    def update_max_concurrent(self, account_id: str, value: int) -> None:
        oauth_manager.update_max_concurrent(account_id, value)

    async def force_refresh(self, account_id: str) -> str:
        return await oauth_manager.force_refresh(account_id)

    async def fetch_usage(self, account_id: str) -> dict:
        return await oauth_manager.fetch_usage(account_id)

    async def fetch_usage_snapshot(self, account_id: str) -> dict:
        # The authoritative domain function always performs a real refresh and
        # accepts only timeout keywords.  Keep this wrapper signature strict so
        # tests cannot hide a production-only unexpected ``force`` TypeError.
        return await oauth_manager.fetch_usage_snapshot(account_id)

    def flatten_usage(self, usage: dict) -> dict:
        return oauth_manager.flatten_usage(usage)

    def usage_from_quota_row(self, row: dict) -> dict:
        return oauth_manager.usage_from_quota_row(row)

    def preserve_antigravity_summary(self, account_id: str, usage: dict) -> dict:
        return oauth_manager.preserve_antigravity_cached_summary(account_id, usage)

    def preserve_openai_reset_details(self, account_id: str, usage: dict) -> dict:
        return oauth_manager.preserve_openai_reset_credit_details(account_id, usage)

    def evaluate_quota(self, account_id: str, usage: dict) -> dict | None:
        return oauth_manager.evaluate_and_toggle_by_usage(account_id, usage)

    def evaluate_cached_quota(self, account_id: str) -> dict | None:
        return oauth_manager.evaluate_and_toggle_by_cached_quota(account_id)

    def quota_load(self, account_id: str) -> dict | None:
        return state_db.quota_load(account_id)

    def quota_save(self, account_id: str, usage: dict, *, email: str | None = None) -> None:
        state_db.quota_save(account_id, usage, email=email)

    def now_ms(self) -> int:
        return state_db.now_ms()

    def cooldown_entries(self) -> list[dict]:
        return cooldown.active_entries()

    def cooldown_state(self, channel_key: str, model_id: str) -> dict | None:
        return cooldown.get_state(channel_key, model_id)

    def clear_errors(self, account_id: str, model_id: str | None = None) -> None:
        cooldown.clear(f"oauth:{account_id}", model_id)

    def clear_all_errors(self) -> int:
        channel_keys = sorted({
            entry["channel_key"]
            for entry in cooldown.active_entries()
            if str(entry.get("channel_key") or "").startswith("oauth:")
        })
        for channel_key in channel_keys:
            cooldown.clear(channel_key, model=None)
        return len(channel_keys)

    def clear_affinity(self, account_id: str) -> None:
        channel_key = f"oauth:{account_id}"
        affinity.delete_by_channel(channel_key)
        affinity.client_delete_by_channel(channel_key)

    def account_model_selection(self, account_or_id: dict | str) -> dict:
        return oauth_manager.account_model_selection(account_or_id)

    def account_disabled_models(self, account_or_id: dict | str) -> set[str]:
        return oauth_manager.account_disabled_models(account_or_id)

    def set_account_model_disabled(self, account_id: str, model_id: str, disabled: bool) -> bool:
        return oauth_manager.set_account_model_disabled(account_id, model_id, disabled)

    def set_account_disabled_models(
        self,
        account_id: str,
        models: Iterable[str],
        *,
        visible_models: Iterable[str] | None = None,
    ) -> set[str]:
        return oauth_manager.set_account_disabled_models(
            account_id, models, visible_models=visible_models,
        )

    def cursor_disabled_models(self, account_or_id: dict | str) -> set[str]:
        return oauth_manager.cursor_disabled_models(account_or_id)

    def set_cursor_disabled_models(
        self,
        account_id: str,
        models: Iterable[str],
        *,
        visible_models: Iterable[str] | None = None,
    ) -> set[str]:
        return oauth_manager.set_cursor_disabled_models(
            account_id, models, visible_models=visible_models,
        )

    def cursor_max_context_default(self, account_or_id: dict | str, model_id: str) -> bool:
        return oauth_manager.cursor_max_context_default(account_or_id, model_id)

    def set_cursor_max_context_default(self, account_id: str, model_id: str, enabled: bool) -> bool:
        return oauth_manager.set_cursor_max_context_default(account_id, model_id, enabled)

    async def refresh_account_models(self, account_id: str) -> dict:
        return await oauth_manager.refresh_account_models(account_id)

    def start_account_model_refresh(self, account_id: str):
        return oauth_manager.start_account_model_refresh(account_id)

    def reset_quota(self, account_id: str) -> dict:
        return oauth_manager.reset_quota(account_id)

    async def redeem_openai_reset_credit(self, account_id: str, idempotency_key: str) -> dict:
        return await oauth_manager.redeem_openai_rate_limit_reset_credit(
            account_id, idempotency_key=idempotency_key,
        )

    def reorder_accounts_conditional(
        self, expected_account_ids: list[str], account_ids: list[str],
    ) -> dict:
        return oauth_manager.reorder_accounts_if_unchanged(
            expected_account_ids, account_ids,
        )

    def reorder_accounts(self, account_ids: list[str]) -> None:
        wanted = list(account_ids)

        def mutate(cfg: dict) -> None:
            accounts = cfg.get("oauthAccounts", [])
            by_id = {oauth_manager.get_account_key(account): account for account in accounts}
            cfg["oauthAccounts"] = [by_id[item] for item in wanted]

        config.update(mutate)

    def reorder_accounts_preserving_unlisted(self, account_ids: list[str]) -> None:
        order = {account_id: index for index, account_id in enumerate(account_ids)}

        def mutate(cfg: dict) -> None:
            accounts = list(cfg.get("oauthAccounts") or [])
            ordered = [
                account for account in accounts
                if oauth_manager.get_account_key(account) in order
            ]
            ordered.sort(
                key=lambda account: order.get(
                    oauth_manager.get_account_key(account), 10**9,
                )
            )
            rest = [
                account for account in accounts
                if oauth_manager.get_account_key(account) not in order
            ]
            cfg["oauthAccounts"] = ordered + rest

        config.update(mutate)

    def update_account_display_name(self, account_id: str, display_name: str | None) -> None:
        def mutate(cfg: dict) -> None:
            for account in cfg.get("oauthAccounts", []):
                if oauth_manager.get_account_key(account) != account_id:
                    continue
                if display_name:
                    account["label"] = display_name
                else:
                    account.pop("label", None)
                return
            raise ValueError("OAuth account not found")

        config.update(mutate)

    @staticmethod
    def _settings_from(cfg: dict) -> tuple[bool, int, float, str]:
        quota = cfg.get("quotaMonitor") or {}
        raw_mode = str(cfg.get("cchMode") or "disabled").strip().lower()
        # Frozen TG treats every historical/non-dynamic value (including
        # ``static``) as disabled.  Keep that fallback at the API boundary.
        mode = raw_mode if raw_mode in {"disabled", "dynamic"} else "disabled"
        return (
            bool(quota.get("enabled", False)),
            int(quota.get("intervalSeconds", 60) or 60),
            float(quota.get("disableThresholdPercent", 95) or 95),
            mode,
        )

    def get_settings(self) -> tuple[bool, int, float, str]:
        return self._settings_from(config.get())

    def _quota_mutator(self, quota_enabled, interval_seconds, threshold_percent):
        return self._settings_control.quota_monitor_mutator({
            key: value for key, value in (
                ("enabled", quota_enabled),
                ("intervalSeconds", interval_seconds),
                ("thresholdPercent", threshold_percent),
            ) if value is not None
        })

    def update_settings_conditional(
        self,
        expected: tuple[bool, int, float, str],
        *,
        quota_enabled: bool | None = None,
        interval_seconds: int | None = None,
        threshold_percent: float | None = None,
        cch_mode: str | None = None,
    ) -> dict:
        result = {"status": "revision_conflict"}
        mutate_quota = self._quota_mutator(quota_enabled, interval_seconds, threshold_percent)

        def mutate(cfg: dict) -> None:
            if self._settings_from(cfg) != expected:
                return
            mutate_quota(cfg)
            if cch_mode is not None:
                cfg["cchMode"] = cch_mode
            result["status"] = "updated"

        config.update(mutate, skip_if_unchanged=True)
        return result

    def update_settings(
        self,
        *,
        quota_enabled: bool | None = None,
        interval_seconds: int | None = None,
        threshold_percent: float | None = None,
        cch_mode: str | None = None,
    ) -> None:
        mutate_quota = self._quota_mutator(quota_enabled, interval_seconds, threshold_percent)

        def mutate(cfg: dict) -> None:
            mutate_quota(cfg)
            if cch_mode is not None:
                cfg["cchMode"] = cch_mode

        config.update(mutate)

    @staticmethod
    def _preferences_from(cfg: dict) -> tuple[str, bool]:
        raw_mode = str(cfg.get("oauthUsageDisplayMode") or "used").strip().lower()
        mode = raw_mode if raw_mode in {"used", "remaining"} else "used"
        return mode, bool(cfg.get("quotaProgressBar", True))

    def get_preferences(self) -> tuple[str, bool]:
        return self._preferences_from(config.get())

    def update_preferences_conditional(
        self,
        expected: tuple[str, bool],
        *,
        usage_display_mode: str | None = None,
        quota_progress_bar: bool | None = None,
    ) -> dict:
        result = {"status": "revision_conflict"}

        def mutate(cfg: dict) -> None:
            if self._preferences_from(cfg) != expected:
                return
            if usage_display_mode is not None:
                cfg["oauthUsageDisplayMode"] = usage_display_mode
            if quota_progress_bar is not None:
                cfg["quotaProgressBar"] = bool(quota_progress_bar)
            result["status"] = "updated"

        config.update(mutate, skip_if_unchanged=True)
        return result

    def update_preferences(
        self,
        *,
        usage_display_mode: str | None = None,
        quota_progress_bar: bool | None = None,
    ) -> None:
        def mutate(cfg: dict) -> None:
            if usage_display_mode is not None:
                cfg["oauthUsageDisplayMode"] = usage_display_mode
            if quota_progress_bar is not None:
                cfg["quotaProgressBar"] = bool(quota_progress_bar)

        config.update(mutate)

    def parse_import(
        self, kind: str, payload, *, filename: str = "",
    ) -> list[OpenAIImportCandidate]:
        return parse_openai_import_payload(kind, payload, filename=filename)

    def cursor_catalog_records(self, account: dict) -> list[dict]:
        return cursor_model_catalog.catalog_records(account)

    def cursor_catalog_record(self, account: dict, model_id: str) -> dict | None:
        return cursor_model_catalog.find_record(account, model_id)

    def channel_model_stats(self, *args, **kwargs):
        return log_db.channel_model_stats(*args, **kwargs)

    def tokens_for_channel(self, *args, **kwargs):
        return log_db.tokens_for_channel(*args, **kwargs)

    def tokens_for_channel_models(self, *args, **kwargs):
        return log_db.tokens_for_channel_models(*args, **kwargs)

    def load_balancing_initialized(self) -> bool:
        return load_balancing.is_initialized()

    def sync_channel_added(self, channel_key: str, family: str) -> None:
        load_balancing.sync_channel_added(channel_key, family)

    async def enrich_openai_reset_credit_details(self, *args, **kwargs):
        return await oauth_manager.enrich_openai_reset_credit_details(*args, **kwargs)

    async def fetch_openai_reset_credits(self, *args, **kwargs):
        return await oauth_manager.fetch_openai_rate_limit_reset_credits(*args, **kwargs)

    def ensure_openai_metadata_fresh_sync(self, *args, **kwargs):
        return oauth_manager.ensure_openai_metadata_fresh_sync(*args, **kwargs)

    async def ensure_openai_metadata_fresh(self, *args, **kwargs):
        return await oauth_manager.ensure_openai_metadata_fresh(*args, **kwargs)

    async def refresh_cursor_models(self, *args, **kwargs):
        return await oauth_manager.refresh_cursor_models(*args, **kwargs)

    def fable_display_from_quota_row(self, *args, **kwargs):
        return oauth_manager.fable_display_from_quota_row(*args, **kwargs)

    def claude_plan_label(self, *args, **kwargs):
        return oauth_manager.claude_plan_label(*args, **kwargs)

    def claude_fable_models(self, *args, **kwargs):
        return oauth_manager.claude_fable_models(*args, **kwargs)

    def extract_utils_percent(self, *args, **kwargs):
        return oauth_manager.extract_utils_percent(*args, **kwargs)

    def model_sync_foreground_timeout_seconds(self) -> float:
        return float(oauth_manager.OAUTH_MODEL_SYNC_FOREGROUND_TIMEOUT_SECONDS)

    @staticmethod
    def is_cursor_auth_pending(exc: BaseException) -> bool:
        return isinstance(exc, cursor_provider.CursorAuthPending)

    # Provider calls remain separate so flows can preserve provider-specific
    # protocol behavior while keeping adapters out of provider modules. Resolve
    # attributes at call time so the repository's fake-provider tests keep working.
    def claude_pkce_generate(self): return oauth_manager.pkce_generate()
    def claude_build_login_url(self, *args, **kwargs): return oauth_manager.build_login_url(*args, **kwargs)
    def claude_exchange_code(self, *args, **kwargs): return oauth_manager.exchange_code(*args, **kwargs)
    async def claude_fetch_profile(self, *args, **kwargs): return await oauth_manager.fetch_profile(*args, **kwargs)
    def claude_extract_plan(self, *args, **kwargs): return oauth_manager.extract_claude_plan_info(*args, **kwargs)
    def openai_pkce_generate(self): return openai_provider.pkce_generate()
    def openai_build_login_url(self, *args, **kwargs): return openai_provider.build_login_url(*args, **kwargs)
    def openai_exchange_code(self, *args, **kwargs): return openai_provider.exchange_code_sync(*args, **kwargs)
    def openai_refresh(self, *args, **kwargs): return openai_provider.refresh_sync(*args, **kwargs)
    def openai_decode_id_token(self, *args, **kwargs): return openai_provider.decode_id_token(*args, **kwargs)
    def openai_extract_user_info(self, *args, **kwargs): return openai_provider.extract_user_info(*args, **kwargs)
    def xai_pkce_generate(self): return xai_provider.pkce_generate()
    def xai_discover(self): return xai_provider.discover_sync()
    def xai_build_login_url(self, *args, **kwargs): return xai_provider.build_login_url(*args, **kwargs)
    def xai_exchange_code(self, *args, **kwargs): return xai_provider.exchange_code_sync(*args, **kwargs)
    def xai_refresh(self, *args, **kwargs): return xai_provider.refresh_sync(*args, **kwargs)
    def xai_decode_id_token(self, *args, **kwargs): return xai_provider.decode_id_token(*args, **kwargs)
    def xai_extract_user_info(self, *args, **kwargs): return xai_provider.extract_user_info(*args, **kwargs)
    def xai_authorization_url(self): return xai_provider.authorization_url()
    def xai_token_url(self): return xai_provider.token_url()
    def xai_redirect_uri(self): return xai_provider.redirect_uri()
    def xai_api_base_url(self): return xai_provider.api_base_url()
    def cursor_generate_login(self): return cursor_provider.generate_login()
    def cursor_poll_login(self, *args, **kwargs): return cursor_provider.poll_login_once(*args, **kwargs)
    def cursor_subject(self, *args, **kwargs): return cursor_provider.subject_from_access_token(*args, **kwargs)
    def cursor_profile(self, *args, **kwargs): return cursor_provider.fetch_profile_sync(*args, **kwargs)
    def cursor_usage(self, *args, **kwargs): return cursor_provider.fetch_usage_sync(*args, **kwargs)
    def cursor_label(self, *args, **kwargs): return cursor_provider.account_label(*args, **kwargs)
    def antigravity_build_login_url(self, *args, **kwargs): return antigravity_provider.build_login_url(*args, **kwargs)
    def antigravity_complete_login(self, *args, **kwargs): return antigravity_provider.complete_login_sync(*args, **kwargs)
    def antigravity_parse_callback(self, *args, **kwargs): return antigravity_provider.parse_callback_url(*args, **kwargs)
    def antigravity_token_url(self): return antigravity_provider.token_url()
    def antigravity_redirect_uri(self): return antigravity_provider.redirect_uri()
    def antigravity_api_base_url(self): return antigravity_provider.api_base_url()

    def workbuddy_start_login(self, *, realm="cn", client_profile=None):
        from src.oauth.workbuddy import start_login_sync
        return start_login_sync(realm=realm, client_profile=client_profile)

    def workbuddy_poll_login(self, payload):
        from src.oauth.workbuddy import poll_login_sync
        return poll_login_sync(payload)

    def workbuddy_effects_enabled(self):
        from src.oauth.workbuddy import effects_allowed
        return effects_allowed()

    def workbuddy_refresh_enabled(self):
        from src.oauth.workbuddy import refresh_allowed
        return refresh_allowed()

    def workbuddy_snapshot(self, account_id: str):
        from src.oauth.workbuddy.runtime import public_snapshot
        return public_snapshot(self.get_account_exact(account_id), self.quota_load(account_id))

    def workbuddy_inspect_action(self, account_id, action, **kwargs):
        from src.oauth.workbuddy.actions import inspect
        return inspect(account_id, action, **kwargs)

    def workbuddy_execute_action(self, account_id, action, **kwargs):
        from src.oauth.workbuddy.actions import execute
        return execute(account_id, action, **kwargs)

    def workbuddy_reconcile_pending(self, account_id):
        from src.oauth.workbuddy.actions import reconcile_pending
        return reconcile_pending(account_id)

    def workbuddy_action_history(self, account_id, *, limit=50):
        from src.oauth.workbuddy.actions import history
        return history(account_id, limit=limit)

    def workbuddy_credential_generation(self, account):
        from src.oauth.workbuddy.runtime import credential_fingerprint
        return credential_fingerprint(account)

    def workbuddy_update_settings(self, account_id, current, auto_checkin):
        return oauth_manager.mutate_account_if_unchanged(account_id, current,
            lambda account: account.update(workbuddy_auto_checkin=auto_checkin))
