"""Shared OAuth identity, usage, and credential post-save orchestration."""

from __future__ import annotations

import asyncio
from threading import Thread

from src.management_auth.principal import Capability
from src.management_control.context import ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode


class OAuthAccountOrchestrationControlMixin:
    """Transport-neutral account validation and post-save business effects."""

    def _ensure_legacy_identity_safe(self, entry: dict) -> None:
        """Preserve frozen XAI/Cursor email-fallback identity protection."""
        provider = self.backend.provider_of(entry)
        if provider not in {"xai", "cursor"}:
            return
        email = str(entry.get("email") or "")
        incoming_subject = str(entry.get("subject") or entry.get("sub") or "")
        for account in self.backend.list_accounts():
            if (
                self.backend.provider_of(account) != provider
                or str(account.get("email") or "") != email
            ):
                continue
            old_subject = str(account.get("subject") or account.get("sub") or "")
            if bool(old_subject) != bool(incoming_subject):
                raise ManagementError(
                    ManagementErrorCode.IDENTITY_CONFLICT,
                    f"{provider} legacy email fallback 会改变 canonical identity，请先移除或迁移旧账户",
                    fields=(ErrorField("credential", "LEGACY_IDENTITY_MIGRATION", email),),
                )

    def prepare_account_save(
        self,
        context: ManagementContext,
        entry: dict,
    ) -> tuple[str, dict] | None:
        """Validate identity before a caller stages any overwrite confirmation.

        This preflight does not write or reserve an account. Add/replace retain
        their commit-time guard because the account set can change while waiting.
        """
        self._require(context, Capability.SECRETS_WRITE)
        self._ensure_legacy_identity_safe(entry)
        return self.backend.find_exact_identity(entry)

    @staticmethod
    def _run_coroutine_sync(factory):
        """Run a coroutine from sync Control code, even under a caller event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        result: list[object] = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                result.append(asyncio.run(factory()))
            except BaseException as exc:
                failure.append(exc)

        thread = Thread(target=run, daemon=True, name="oauth-control-async")
        thread.start()
        thread.join()
        if failure:
            raise failure[0]
        return result[0]

    @staticmethod
    def _notify_usage_stage(on_stage, name: str, payload=None) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, payload)
        except Exception:
            pass

    def _save_and_evaluate_usage(
        self,
        account_id: str,
        usage: dict,
        *,
        email: str | None = None,
        tolerate_evaluation_error: bool = False,
    ) -> tuple[dict, dict | None]:
        preserved = self.backend.preserve_antigravity_summary(account_id, usage)
        self.backend.quota_save(
            account_id,
            self.backend.flatten_usage(preserved),
            email=email if email is not None else self.backend.account_email(account_id),
        )
        try:
            quota_action = self.backend.evaluate_quota(account_id, preserved)
        except Exception as exc:
            if not tolerate_evaluation_error:
                raise
            print(f"[oauth-control] quota evaluate failed for {account_id}: {exc}")
            quota_action = None
        return preserved, quota_action

    def _refresh_usage_account(
        self,
        account_id: str,
        *,
        email: str | None = None,
        on_stage=None,
        tolerate_evaluation_error: bool = False,
    ) -> dict:
        """Own fetch, provider enrichment, persistence and quota evaluation."""
        provider = self.backend.provider_of(account_id)
        details = None
        detail_error = None
        if on_stage is None:
            usage = self._run_coroutine_sync(
                lambda: self.backend.fetch_usage_snapshot(account_id)
            )
        else:
            self._notify_usage_stage(on_stage, "usage_start")
            usage = self._run_coroutine_sync(
                lambda: self.backend.fetch_usage(account_id)
            )
            preserved = self.backend.preserve_antigravity_summary(account_id, usage)
            self.backend.quota_save(
                account_id,
                self.backend.flatten_usage(preserved),
                email=email if email is not None else self.backend.account_email(account_id),
            )
            usage = preserved
            self._notify_usage_stage(on_stage, "usage_done", usage)
            if provider == "openai":
                summary = (usage.get("openai") or {}).get("rate_limit_reset_credits")
                if not isinstance(summary, dict):
                    summary = usage.get("rate_limit_reset_credits")
                try:
                    available_count = int(summary.get("available_count")) if isinstance(summary, dict) else None
                except (TypeError, ValueError):
                    available_count = None
                if available_count is None or available_count > 0:
                    self._notify_usage_stage(on_stage, "reset_start", usage)
                try:
                    usage = self._run_coroutine_sync(
                        lambda: self.backend.enrich_openai_reset_credit_details(
                            account_id, usage,
                        )
                    )
                    details = (usage.get("openai") or {}).get("rate_limit_reset_credits")
                    if not isinstance(details, dict):
                        details = usage.get("rate_limit_reset_credits")
                    self._notify_usage_stage(on_stage, "reset_done", details)
                except Exception as exc:
                    detail_error = exc
                    usage = self.backend.preserve_openai_reset_details(account_id, usage)
                    details = (usage.get("openai") or {}).get("rate_limit_reset_credits")
                    if not isinstance(details, dict):
                        details = usage.get("rate_limit_reset_credits")
                    self._notify_usage_stage(on_stage, "reset_error", exc)

        usage, quota_action = self._save_and_evaluate_usage(
            account_id,
            usage,
            email=email,
            tolerate_evaluation_error=tolerate_evaluation_error,
        )
        return {
            "usage": usage,
            "reset_credit_details": details,
            "reset_credit_error": detail_error,
            "quota_action": quota_action,
        }

    def refresh_usage_now(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        email: str | None = None,
        on_stage=None,
    ) -> dict:
        """Synchronous compatibility use case used by Telegram progress UI."""
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        try:
            result = self._refresh_usage_account(
                account_id,
                email=email,
                on_stage=on_stage,
                tolerate_evaluation_error=True,
            )
        except Exception as exc:
            return {"error": exc}
        self._audit(context, "oauth.usage.refresh", account_id)
        return result

    def save_and_evaluate_usage(
        self,
        context: ManagementContext,
        account_id: str,
        usage: dict,
        *,
        email: str | None = None,
    ) -> dict:
        """Persist an already-fetched provider snapshot and evaluate quota once."""
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        saved, quota_action = self._save_and_evaluate_usage(
            account_id,
            usage,
            email=email,
            tolerate_evaluation_error=True,
        )
        self._audit(context, "oauth.usage.snapshot.save", account_id)
        return {"usage": saved, "quota_action": quota_action}

    def _start_post_save_model_sync(self, account_id: str) -> dict:
        effects: dict = {
            "model_sync_future": None,
            "model_selection_before": None,
            "model_sync_error": None,
        }
        try:
            effects["model_selection_before"] = self.backend.account_model_selection(account_id)
            effects["model_sync_future"] = self.backend.start_account_model_refresh(account_id)
        except Exception as exc:
            effects["model_sync_error"] = exc
        return effects

    def start_post_save_model_sync(
        self,
        context: ManagementContext,
        account_id: str,
    ) -> dict:
        """Start the shared model-sync portion for legacy progress renderers."""
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        return self._start_post_save_model_sync(account_id)

    def _post_save_account_effects(
        self,
        account_id: str,
        entry: dict,
        *,
        usage: dict | None = None,
    ) -> dict:
        """Start model sync and run provider quota work after one successful save.

        Failures are observations rather than rollback triggers, matching the
        frozen handlers where the credential save remains committed and the UI
        reports that model/usage refresh can be retried later.
        """
        provider = self.backend.provider_of(entry)
        if provider == "zhipu":
            return self._post_save_zhipu_effects(account_id, entry, usage=usage)
        effects = self._start_post_save_model_sync(account_id)
        effects.update(usage=None, usage_error=None, quota_action=None, enrichment_error=None)

        if provider not in {"openai", "cursor", "workbuddy"}:
            return effects
        try:
            if usage is None:
                refreshed = self._refresh_usage_account(
                    account_id,
                    email=str(entry.get("email") or self.backend.account_email(account_id)),
                    tolerate_evaluation_error=True,
                )
            else:
                saved, quota_action = self._save_and_evaluate_usage(
                    account_id,
                    usage,
                    email=str(entry.get("email") or self.backend.account_email(account_id)),
                    tolerate_evaluation_error=True,
                )
                refreshed = {"usage": saved, "quota_action": quota_action}
            effects.update(
                usage=refreshed.get("usage"),
                quota_action=refreshed.get("quota_action"),
            )
        except Exception as exc:
            effects["usage_error"] = exc
        return effects

    def run_post_save_account_effects(
        self,
        context: ManagementContext,
        account_id: str,
        entry: dict,
        *,
        usage: dict | None = None,
    ) -> dict:
        """Run the shared post-save use case when a renderer defers its start."""
        self._require(context, Capability.SECRETS_WRITE)
        self._legacy_account(account_id)
        return self._post_save_account_effects(account_id, entry, usage=usage)

    def _refresh_usage_worker(
        self,
        account_ids: list[str],
        *,
        continue_on_error: bool = False,
    ) -> dict:
        results = []
        refreshed = failed = 0
        for account_id in account_ids:
            try:
                self._refresh_usage_account(account_id)
            except Exception:
                if not continue_on_error:
                    raise
                failed += 1
                results.append({"accountId": account_id, "status": "failed"})
                continue
            refreshed += 1
            results.append({"accountId": account_id, "status": "refreshed"})
        return {
            "accounts": results,
            "total": len(results),
            "refreshed": refreshed,
            "failed": failed,
        }
