"""Raw-shape OAuth queries used by frozen adapters.

The Management API consumes typed DTO use cases.  Telegram's byte-frozen
renderers still need existing dict shapes, so these transport-neutral query
methods centralize domain access without copying presentation logic.
"""

from __future__ import annotations


class OAuthQueryControlMixin:
    def config_snapshot(self) -> dict:
        return self.backend.config_snapshot()

    def account_entries_snapshot(self) -> list[dict]:
        return self.backend.list_accounts()

    def account_snapshot(self, account_id: str) -> dict | None:
        return self.backend.get_account(account_id)

    def resolve_account_id_snapshot(self, value: str | None) -> str | None:
        return self.backend.resolve_account_id(value)

    def resolve_account_id_or_none(self, value: str | None) -> str | None:
        return self.backend.resolve_account_id_or_none(value)

    def account_id_from_entry(self, account: dict) -> str:
        return self.backend.account_id(account)

    def account_email_snapshot(self, account_id: str) -> str:
        return self.backend.account_email(account_id)

    def provider_of_snapshot(self, account_or_id: dict | str) -> str:
        return self.backend.provider_of(account_or_id)

    def find_exact_identity_snapshot(self, entry: dict) -> tuple[str, dict] | None:
        return self.backend.find_exact_identity(entry)

    def quota_snapshot(self, account_id: str) -> dict | None:
        return self.backend.quota_load(account_id)

    def workbuddy_snapshot(self, account_id: str) -> dict:
        """Redacted local view for Telegram; never refreshes or performs an action."""
        return self.backend.workbuddy_snapshot(account_id)

    def workbuddy_refresh_enabled_snapshot(self) -> bool:
        return self.backend.workbuddy_refresh_enabled()

    def now_ms(self) -> int:
        return self.backend.now_ms()

    def cooldown_entries_snapshot(self) -> list[dict]:
        return self.backend.cooldown_entries()

    def cooldown_state_snapshot(self, account_id: str, model_id: str) -> dict | None:
        return self.backend.cooldown_state(f"oauth:{account_id}", model_id)

    def account_model_selection_snapshot(self, account_or_id: dict | str) -> dict:
        return self.backend.account_model_selection(account_or_id)

    def account_disabled_models_snapshot(self, account_or_id: dict | str) -> set[str]:
        return self.backend.account_disabled_models(account_or_id)

    def cursor_disabled_models_snapshot(self, account_or_id: dict | str) -> set[str]:
        return self.backend.cursor_disabled_models(account_or_id)

    def cursor_max_context_default_snapshot(self, account_or_id: dict | str, model_id: str) -> bool:
        return self.backend.cursor_max_context_default(account_or_id, model_id)

    def cursor_catalog_records_snapshot(self, account: dict) -> list[dict]:
        return self.backend.cursor_catalog_records(account)

    def cursor_catalog_record_snapshot(self, account: dict, model_id: str) -> dict | None:
        return self.backend.cursor_catalog_record(account, model_id)

    def tokens_for_channel_snapshot(self, *args, **kwargs):
        return self.backend.tokens_for_channel(*args, **kwargs)

    def tokens_for_channel_models_snapshot(self, *args, **kwargs):
        return self.backend.tokens_for_channel_models(*args, **kwargs)

    def channel_model_stats_snapshot(self, *args, **kwargs):
        return self.backend.channel_model_stats(*args, **kwargs)

    def load_balancing_initialized(self) -> bool:
        return self.backend.load_balancing_initialized()
