"""Provider/runtime operations needed by byte-frozen OAuth adapters.

These are transport-neutral control entry points.  They intentionally preserve
provider-native return values because Telegram owns the existing progress and
error rendering while the typed HTTP use cases use higher-level methods.
"""

from __future__ import annotations

from src.management_auth.principal import Capability
from src.management_control.context import ManagementContext


class OAuthLegacyOperationsControlMixin:
    def start_account_model_refresh(self, account_id: str):
        return self.backend.start_account_model_refresh(account_id)

    def model_sync_foreground_timeout_seconds(self) -> float:
        return self.backend.model_sync_foreground_timeout_seconds()

    def is_cursor_auth_pending(self, exc: BaseException) -> bool:
        return self.backend.is_cursor_auth_pending(exc)

    async def refresh_account_models_raw(self, account_id: str) -> dict:
        return await self.backend.refresh_account_models(account_id)

    async def refresh_account_models_for_telegram(
        self, context: ManagementContext, account_id: str,
    ) -> dict:
        """Preserve the TG async renderer while authorizing and auditing its actor."""
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        try:
            result = await self.backend.refresh_account_models(account_id)
        except BaseException:
            self._audit(context, "oauth.models.sync", account_id, "failed")
            raise
        self._audit(context, "oauth.models.sync", account_id)
        return result

    async def fetch_usage_raw(self, account_id: str) -> dict:
        return await self.backend.fetch_usage(account_id)

    async def enrich_openai_reset_credit_details_raw(self, *args, **kwargs):
        return await self.backend.enrich_openai_reset_credit_details(*args, **kwargs)

    def preserve_openai_reset_credit_details_raw(self, account_id: str, usage: dict) -> dict:
        return self.backend.preserve_openai_reset_details(account_id, usage)

    async def fetch_openai_reset_credits_raw(self, *args, **kwargs):
        return await self.backend.fetch_openai_reset_credits(*args, **kwargs)

    def evaluate_cached_quota_raw(self, account_id: str) -> dict | None:
        return self.backend.evaluate_cached_quota(account_id)

    async def force_refresh_raw(self, account_id: str) -> str:
        return await self.backend.force_refresh(account_id)

    def ensure_openai_metadata_fresh_sync_raw(self, *args, **kwargs):
        return self.backend.ensure_openai_metadata_fresh_sync(*args, **kwargs)

    async def ensure_openai_metadata_fresh_raw(self, *args, **kwargs):
        return await self.backend.ensure_openai_metadata_fresh(*args, **kwargs)

    async def refresh_cursor_models_raw(self, *args, **kwargs):
        return await self.backend.refresh_cursor_models(*args, **kwargs)

    def usage_from_quota_row(self, row: dict) -> dict:
        return self.backend.usage_from_quota_row(row)

    def fable_display_from_quota_row(self, *args, **kwargs):
        return self.backend.fable_display_from_quota_row(*args, **kwargs)

    def claude_plan_label(self, *args, **kwargs):
        return self.backend.claude_plan_label(*args, **kwargs)

    def claude_fable_models(self, *args, **kwargs):
        return self.backend.claude_fable_models(*args, **kwargs)

    def extract_utils_percent(self, *args, **kwargs):
        return self.backend.extract_utils_percent(*args, **kwargs)

    def claude_pkce_generate(self):
        return self.backend.claude_pkce_generate()

    def claude_build_login_url(self, *args, **kwargs):
        return self.backend.claude_build_login_url(*args, **kwargs)

    def claude_exchange_code(self, *args, **kwargs):
        return self.backend.claude_exchange_code(*args, **kwargs)

    async def claude_fetch_profile(self, *args, **kwargs):
        return await self.backend.claude_fetch_profile(*args, **kwargs)

    def claude_extract_plan(self, *args, **kwargs):
        return self.backend.claude_extract_plan(*args, **kwargs)

    def cursor_generate_login(self):
        return self.backend.cursor_generate_login()

    def cursor_poll_login(self, *args, **kwargs):
        return self.backend.cursor_poll_login(*args, **kwargs)

    def cursor_subject(self, *args, **kwargs):
        return self.backend.cursor_subject(*args, **kwargs)

    def cursor_profile(self, *args, **kwargs):
        return self.backend.cursor_profile(*args, **kwargs)

    def cursor_usage(self, *args, **kwargs):
        return self.backend.cursor_usage(*args, **kwargs)

    def cursor_label(self, *args, **kwargs):
        return self.backend.cursor_label(*args, **kwargs)

    def openai_pkce_generate(self):
        return self.backend.openai_pkce_generate()

    def openai_build_login_url(self, *args, **kwargs):
        return self.backend.openai_build_login_url(*args, **kwargs)

    def openai_validate_callback_url(self, *args, **kwargs):
        return self.backend.openai_validate_callback_url(*args, **kwargs)

    def openai_exchange_code(self, *args, **kwargs):
        return self.backend.openai_exchange_code(*args, **kwargs)

    def openai_refresh(self, *args, **kwargs):
        return self.backend.openai_refresh(*args, **kwargs)

    def openai_decode_id_token(self, *args, **kwargs):
        return self.backend.openai_decode_id_token(*args, **kwargs)

    def openai_extract_user_info(self, *args, **kwargs):
        return self.backend.openai_extract_user_info(*args, **kwargs)

    def xai_pkce_generate(self):
        return self.backend.xai_pkce_generate()

    def xai_discover(self):
        return self.backend.xai_discover()

    def xai_build_login_url(self, *args, **kwargs):
        return self.backend.xai_build_login_url(*args, **kwargs)

    def xai_exchange_code(self, *args, **kwargs):
        return self.backend.xai_exchange_code(*args, **kwargs)

    def xai_refresh(self, *args, **kwargs):
        return self.backend.xai_refresh(*args, **kwargs)

    def xai_decode_id_token(self, *args, **kwargs):
        return self.backend.xai_decode_id_token(*args, **kwargs)

    def xai_extract_user_info(self, *args, **kwargs):
        return self.backend.xai_extract_user_info(*args, **kwargs)

    def xai_authorization_url(self):
        return self.backend.xai_authorization_url()

    def xai_token_url(self):
        return self.backend.xai_token_url()

    def xai_redirect_uri(self):
        return self.backend.xai_redirect_uri()

    def xai_api_base_url(self):
        return self.backend.xai_api_base_url()

    def antigravity_build_login_url(self, *args, **kwargs):
        return self.backend.antigravity_build_login_url(*args, **kwargs)

    def antigravity_complete_login(self, *args, **kwargs):
        return self.backend.antigravity_complete_login(*args, **kwargs)

    def antigravity_parse_callback(self, *args, **kwargs):
        return self.backend.antigravity_parse_callback(*args, **kwargs)

    def antigravity_token_url(self):
        return self.backend.antigravity_token_url()

    def antigravity_redirect_uri(self):
        return self.backend.antigravity_redirect_uri()

    def antigravity_api_base_url(self):
        return self.backend.antigravity_api_base_url()
