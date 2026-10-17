"""Shared request/background refresh failure policy, separate from notification rendering."""
from . import oauth_errors, oauth_manager
from .notifications.events import Effect


def apply(account_key, exc, *, expected_state_key=None, provider=None, observation=None):
    error = oauth_errors.describe_oauth_error(exc, provider=provider or oauth_manager.provider_of(account_key), operation="refresh_token")
    effect = Effect("unchanged", "not_disabled")
    # Prefer the lock owner's actual refresh input over a request's older 401.
    observation = getattr(exc, "oauth_refresh_observation", None) or observation
    if observation is not None:
        if expected_state_key is not None and expected_state_key != observation["state_key"]:
            return error, None
        expected_state_key = observation["state_key"]
    with oauth_manager.account_generation_guard(expected_state_key) as current:
        if not current:
            return error, None
        account = oauth_manager.get_account(account_key)
        if observation is not None and (not account or not oauth_manager.refresh_observation_matches(account, observation)):
            return error, None  # Late old failure cannot pause a newer login/rotation.
        if error.code == "workbuddy_save_candidate_expired_relogin" and account:
            # Persist the local loss independently of a user's pause. After a
            # restart, neither a selected old channel nor an explicit refresh
            # may replay the pre-rotation RT. Browser login clears this marker.
            try:
                def mark(value):
                    value["workbuddy_relogin_required"] = True
                    if value.get("disabled_reason") != "user":
                        value.update(enabled=False, disabled_reason="auth_error", disabled_until=None)
                outcome = oauth_manager.mutate_account_if_unchanged(account_key, account, mark)
                effect = Effect("confirmed", "account_paused") if outcome.get("status") == "updated" else Effect("unknown", "disable_failed")
            except Exception:
                effect = Effect("failed", "disable_failed")
            return error, effect
        if account and account.get("disabled_reason") == "user":
            return error, Effect("unchanged", "still_paused")
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
