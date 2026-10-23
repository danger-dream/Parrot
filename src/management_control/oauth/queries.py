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

    def qoder_snapshot(self, account_id: str) -> dict:
        """Redacted local view for Telegram; never refreshes or claims."""
        return self.backend.qoder_snapshot(account_id)

    def _month_channel_stats(self, context, account_id: str, since_ts: float) -> dict:
        """Month token/cost stats for the detail view; API reads share the
        statistics cache (Telegram keeps its exact uncached read)."""
        from src.management_auth import AuthMethod
        from src.management_control.stats_cache import cached
        from .backend import OAuthBackend
        if type(self.backend) is OAuthBackend and context.actor.auth_method is not AuthMethod.TELEGRAM_ADMIN:
            # Single-object detail: a cached per-account read, never the
            # global month snapshot (see test_management_read_performance).
            return cached(("oauth-month", account_id, since_ts),
                          lambda: self.backend.tokens_for_channel(f"oauth:{account_id}", since_ts) or {})
        return self.backend.tokens_for_channel(f"oauth:{account_id}", since_ts) or {}

    def _provider_snapshots(self, provider, account_id: str) -> dict:
        """The account detail's provider-specific redacted views; None for other providers."""
        from .models import OAuthProvider
        from .usage_sections import build_usage_sections, _time
        zhipu = self.backend.zhipu_snapshot(account_id) if provider is OAuthProvider.ZHIPU else None
        row = self.backend.quota_load(account_id) or {}
        try:
            snapshot = (self.backend.workbuddy_snapshot(account_id) if provider is OAuthProvider.WORKBUDDY
                        else self.backend.qoder_snapshot(account_id) if provider is OAuthProvider.QODER else None)
            sections = build_usage_sections(provider.value, row, self.backend, zhipu_snapshot=zhipu,
                                            provider_snapshot=snapshot)
        except Exception:
            sections = ()  # Presentation-only; never fail the account detail.
        account = self.backend.get_account_exact(account_id) if provider is OAuthProvider.OPENAI else None
        return {
            "workbuddy": self.backend.workbuddy_snapshot(account_id) if provider is OAuthProvider.WORKBUDDY else None,
            "zhipu": zhipu,
            "qoder": self.backend.qoder_snapshot(account_id) if provider is OAuthProvider.QODER else None,
            "usage_sections": sections,
            "usage_fetched_at": _time(row.get("fetched_at")),
            "allow_credits": (account or {}).get("allowCredits") is True if provider is OAuthProvider.OPENAI else None,
        }

    def _refresh_enabled_for(self, provider: str) -> bool:
        """Whether this instance may rotate the provider's credentials (PARROT_NO_REFRESH)."""
        if provider == "workbuddy":
            return self.backend.workbuddy_refresh_enabled()
        if provider == "qoder":
            return self.backend.qoder_refresh_enabled()
        return True

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
