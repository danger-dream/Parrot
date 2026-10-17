"""Shared request/background refresh failure policy, separate from notification rendering."""
from . import oauth_errors, oauth_manager
from .notifications.events import Effect


def apply(account_key, exc, *, expected_state_key=None, provider=None):
    error = oauth_errors.describe_oauth_error(exc, provider=provider or oauth_manager.provider_of(account_key), operation="refresh_token")
    effect = Effect("unchanged", "not_disabled")
    with oauth_manager.account_generation_guard(expected_state_key) as current:
        if not current:
            return error, None
        if error.auth_error:
            try:
                oauth_manager.set_enabled(account_key, False, reason="auth_error")
                # A setter invocation is not proof. Check the committed exact target.
                account = oauth_manager.get_account(account_key)
                if account and account.get("enabled") is False and account.get("disabled_reason") == "auth_error":
                    effect = Effect("confirmed", "account_paused")
                else:
                    effect = Effect("unknown", "disable_failed")
            except Exception:
                effect = Effect("failed", "disable_failed")
    return error, effect
