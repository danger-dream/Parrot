"""Transport-neutral OAuth management use cases shared by API and Telegram."""

from __future__ import annotations

import asyncio
import copy
import secrets
from concurrent.futures import Executor
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from src import model_names
from src.model_pricing import TICKS_PER_USD
from src.management_auth.policy import CapabilityDenied, authorize
from src.management_auth.principal import AuthMethod, Capability
from src.management_control.context import AuditSink, ManagementContext, audit_record
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.management_control.operations import ManagementOperation, OperationStore

from .account_mutations import OAuthAccountMutationControlMixin
from .account_orchestration import OAuthAccountOrchestrationControlMixin
from .backend import OAuthBackend
from .compat import OAuthCompatibilityControlMixin
from .contracts import (
    audit_failures,
    invalid_account,
    public_value,
    revision as _revision,
    sanitize_text,
    utc_datetime,
)
from .flows import OAuthFlowService
from .imports import OAuthImportControlMixin
from .legacy_ops import OAuthLegacyOperationsControlMixin
from .models import (
    CchMode,
    CompleteOAuthLoginCommand,
    OAuthAccountDetail,
    OAuthAccountFilter,
    OAuthAccountPage,
    OAuthAccountSort,
    OAuthAccountSummary,
    OAuthDeletionPlan,
    OAuthLocalStats,
    OAuthLoginFlow,
    OAuthModel,
    OAuthModelPage,
    OAuthMutationResult,
    OAuthProvider,
    OAuthQuotaResetPlan,
    OAuthRuntimeError,
    OAuthSettings,
    OAuthUsageDisplayMode,
    OAuthUsageWindow,
    PageMeta,
    PageSpec,
    TelegramOAuthPreferences,
)
from .plans import OneShotPlanStore
from .queries import OAuthQueryControlMixin
from .workbuddy_control import WorkBuddyControlMixin


def _page(items: list, spec: PageSpec) -> tuple[list, PageMeta]:
    if spec.page < 1 or spec.page_size < 1 or spec.page_size > 200:
        raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
    start = (spec.page - 1) * spec.page_size
    total = len(items)
    return items[start : start + spec.page_size], PageMeta(
        page=spec.page,
        page_size=spec.page_size,
        total=total,
        has_next=start + spec.page_size < total,
    )


class OAuthControl(
    WorkBuddyControlMixin,
    OAuthAccountOrchestrationControlMixin,
    OAuthAccountMutationControlMixin,
    OAuthImportControlMixin,
    OAuthCompatibilityControlMixin,
    OAuthLegacyOperationsControlMixin,
    OAuthQueryControlMixin,
):
    _value_revision = staticmethod(_revision)

    def __init__(
        self,
        backend: OAuthBackend | None = None,
        *,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.backend = backend or OAuthBackend()
        self._audit_sink = audit_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Explicit executors remain a deterministic unit-test seam. Production
        # operations submit through their runtime-owned OperationStore.
        self._executor = executor
        self._flows = OAuthFlowService(self.backend, clock=self._clock)
        self._replace_plans: OneShotPlanStore[dict] = OneShotPlanStore(
            prefix="oreplace", clock=self._clock,
        )
        self._delete_plans: OneShotPlanStore[dict] = OneShotPlanStore(
            prefix="odelete", clock=self._clock,
        )
        self._quota_plans: OneShotPlanStore[dict] = OneShotPlanStore(
            prefix="oquota", clock=self._clock,
        )
        self._import_plans: OneShotPlanStore[dict] = OneShotPlanStore(
            prefix="oimport", clock=self._clock,
        )
        self._workbuddy_plans: OneShotPlanStore[dict] = OneShotPlanStore(
            prefix="wbaction", ttl_seconds=300, clock=self._clock,
        )

    @staticmethod
    def _require(context: ManagementContext, capability: Capability) -> None:
        try:
            authorize(context.actor, capability)
        except CapabilityDenied as exc:
            raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED) from exc

    def _audit(self, context: ManagementContext, action: str, target: str, result: str = "succeeded") -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                audit_record(
                    context,
                    action=sanitize_text(action),
                    target=sanitize_text(target),
                    result=sanitize_text(result),
                    occurred_at=self._clock(),
                )
            )

    def _account(self, account_id: str) -> dict:
        account = self.backend.get_account_exact(account_id)
        if not isinstance(account, dict):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return account

    def _legacy_account(self, account_id: str) -> dict:
        account = self.backend.get_account(account_id)
        if not isinstance(account, dict):
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        return account

    def find_exact_identity(
        self,
        context: ManagementContext,
        entry: dict,
    ) -> tuple[str, dict] | None:
        self._require(context, Capability.WRITE)
        return self.backend.find_exact_identity(entry)

    def add_account_entry(
        self,
        context: ManagementContext,
        entry: dict,
        *,
        usage: dict | None = None,
        defer_post_save: bool = False,
    ) -> dict:
        self._require(context, Capability.SECRETS_WRITE)
        self._ensure_legacy_identity_safe(entry)
        result = self.backend.add_account_if_absent(copy.deepcopy(entry))
        if result.get("status") == "added":
            account_id = str(result.get("account_key") or self.backend.account_id(entry))
            if not defer_post_save:
                result["_post_save"] = self._post_save_account_effects(
                    account_id, entry, usage=usage,
                )
            self._audit(context, "oauth.account.create", account_id)
        return result

    def replace_account_entry(
        self,
        context: ManagementContext,
        account_id: str,
        entry: dict,
        *,
        usage: dict | None = None,
        defer_post_save: bool = False,
    ) -> dict:
        self._require(context, Capability.SECRETS_WRITE)
        self._ensure_legacy_identity_safe(entry)
        result = self.backend.replace_exact_identity(account_id, copy.deepcopy(entry))
        if result.get("status") == "replaced":
            if not defer_post_save:
                result["_post_save"] = self._post_save_account_effects(
                    account_id, entry, usage=usage,
                )
            self._audit(context, "oauth.account.replace", account_id)
        return result

    def set_account_enabled(
        self,
        context: ManagementContext,
        account_id: str,
        enabled: bool,
        *,
        reason: str | None = None,
    ) -> None:
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        self.backend.set_enabled(account_id, enabled, reason=reason)
        self._audit(context, "oauth.account.enable", account_id)

    def set_account_max_concurrent(
        self,
        context: ManagementContext,
        account_id: str,
        value: int,
    ) -> None:
        self._require(context, Capability.WRITE)
        self._legacy_account(account_id)
        self.backend.update_max_concurrent(account_id, value)
        self._audit(context, "oauth.account.max-concurrent", account_id)

    def reset_quota_now(self, context: ManagementContext, account_id: str) -> dict:
        self._require(context, Capability.DESTRUCTIVE)
        self._legacy_account(account_id)
        result = self.backend.reset_quota(account_id)
        self._audit(context, "oauth.quota.reset", account_id)
        return result

    def redeem_openai_reset_credit_now(
        self,
        context: ManagementContext,
        account_id: str,
        idempotency_key: str,
        credit_id: str | None = None,
    ) -> dict:
        self._require(context, Capability.DESTRUCTIVE)
        self._legacy_account(account_id)
        kwargs = {"credit_id": credit_id} if credit_id else {}
        result = asyncio.run(
            self.backend.redeem_openai_reset_credit(account_id, idempotency_key, **kwargs)
        )
        self._audit(context, "oauth.quota.credit-redeem", account_id)
        return result

    def _summary(self, account: dict) -> OAuthAccountSummary:
        account_id = self.backend.account_id(account)
        provider = OAuthProvider(self.backend.provider_of(account))
        disabled = self.backend.account_disabled_models(account)
        models = self.backend.account_model_selection(account).get("models") or []
        reason = account.get("disabled_reason")
        return OAuthAccountSummary(
            account_id=account_id,
            provider=provider,
            display_name=sanitize_text(account.get("label") or account.get("email") or account_id),
            identity=sanitize_text(account.get("email") or account_id.partition(":")[2]),
            enabled=bool(account.get("enabled", True)),
            disabled_reason=sanitize_text(reason) if reason else None,
            disabled_until=utc_datetime(account.get("disabled_until")),
            max_concurrent=max(0, int(account.get("maxConcurrent") or 0)),
            available=bool(account.get("enabled", True)) and not reason,
            quota_limited=reason == "quota",
            invalid=invalid_account(account),
            model_count=len(models),
            disabled_model_count=len(disabled),
            credential_configured=bool(
                account.get("refresh_token") or account.get("access_token")
            ),
            revision=_revision(account),
        )

    def list_accounts(
        self,
        context: ManagementContext,
        *,
        account_filter: OAuthAccountFilter = OAuthAccountFilter.ALL,
        provider: OAuthProvider | None = None,
        enabled: bool | None = None,
        sort: OAuthAccountSort = OAuthAccountSort.CONFIGURED,
        page: PageSpec = PageSpec(),
    ) -> OAuthAccountPage:
        self._require(context, Capability.READ)
        accounts = self.backend.list_accounts()
        configured_ids = [self.backend.account_id(account) for account in accounts]
        summaries = [self._summary(account) for account in accounts]
        if account_filter is OAuthAccountFilter.AVAILABLE:
            summaries = [item for item in summaries if item.available]
        elif account_filter is OAuthAccountFilter.QUOTA:
            summaries = [item for item in summaries if item.quota_limited]
        elif account_filter is OAuthAccountFilter.INVALID:
            summaries = [item for item in summaries if item.invalid]
        if provider is not None:
            summaries = [item for item in summaries if item.provider is provider]
        if enabled is not None:
            summaries = [item for item in summaries if item.enabled is enabled]
        if sort is OAuthAccountSort.DISPLAY_NAME:
            summaries.sort(key=lambda item: (item.display_name.casefold(), item.account_id))
        elif sort is OAuthAccountSort.PROVIDER:
            summaries.sort(key=lambda item: (item.provider.value, item.display_name.casefold(), item.account_id))
        elif sort is OAuthAccountSort.STATUS:
            summaries.sort(key=lambda item: (not item.available, item.disabled_reason or "", item.account_id))
        visible, meta = _page(summaries, page)
        return OAuthAccountPage(
            items=tuple(visible), meta=meta, revision=_revision(configured_ids),
        )

    def get_account(self, context: ManagementContext, account_id: str) -> OAuthAccountDetail:
        self._require(context, Capability.READ)
        account = self._account(account_id)
        summary = self._summary(account)
        row = self.backend.quota_load(account_id) or {}
        windows = []
        for name, used_key, reset_key in (
            ("fiveHour", "five_hour_util", "five_hour_reset"),
            ("sevenDay", "seven_day_util", "seven_day_reset"),
            ("thirtyDay", "thirty_day_util", "thirty_day_reset"),
        ):
            used = row.get(used_key)
            used_float = float(used) if isinstance(used, (int, float)) else None
            windows.append(
                OAuthUsageWindow(
                    name=name,
                    used_percent=used_float,
                    remaining_percent=max(0.0, 100.0 - used_float) if used_float is not None else None,
                    resets_at=utc_datetime(row.get(reset_key)),
                )
            )
        # Match Telegram's BJT accounting month, even while UTC is in the previous month.
        month_start = self._clock().astimezone(timezone(timedelta(hours=8))).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        try:
            stats = self.backend.tokens_for_channel(f"oauth:{account_id}", month_start.timestamp()) or {}
        except Exception:
            stats = {}
        runtime_errors = []
        for entry in self.backend.cooldown_entries():
            if entry.get("channel_key") != f"oauth:{account_id}":
                continue
            until = entry.get("cooldown_until")
            runtime_errors.append(
                OAuthRuntimeError(
                    model_id=str(entry.get("model")) if entry.get("model") else None,
                    message=(
                        sanitize_text(entry.get("last_error_message"))
                        if entry.get("last_error_message")
                        else None
                    ),
                    cooldown_until=utc_datetime(until),
                    cooldown_permanent=until == -1,
                )
            )
        return OAuthAccountDetail(
            account=summary,
            workspace_id=sanitize_text(account.get("workspace_id") or account.get("project_id") or account.get("subject")) if (account.get("workspace_id") or account.get("project_id") or account.get("subject")) else None,
            workspace_name=sanitize_text(account.get("workspace_name") or account.get("cursor_profile_name")) if (account.get("workspace_name") or account.get("cursor_profile_name")) else None,
            plan_type=sanitize_text(account.get("plan_type")) if account.get("plan_type") else None,
            expires_at=utc_datetime(account.get("expired")),
            usage_windows=tuple(windows),
            local_stats=OAuthLocalStats(
                request_count=int(stats.get("total") or 0),
                input_tokens=int(stats.get("input") or 0),
                output_tokens=int(stats.get("output") or 0),
                cost_usd=(stats["cost_ticks"] / TICKS_PER_USD if stats.get("costed_success") else None),
            ),
            runtime_errors=tuple(runtime_errors),
            credential_configured=bool(account.get("refresh_token") or account.get("access_token")),
            last_model_sync=utc_datetime(account.get("last_model_sync")),
            workbuddy=self.backend.workbuddy_snapshot(account_id) if summary.provider is OAuthProvider.WORKBUDDY else None,
        )

    def reorder_accounts_preserving_unlisted(
        self,
        context: ManagementContext,
        account_ids: Iterable[str],
    ) -> None:
        self._require(context, Capability.WRITE)
        self.backend.reorder_accounts_preserving_unlisted(list(account_ids))
        self._audit(context, "oauth.account.reorder", "oauthAccounts")

    @audit_failures("oauth.login.start", target_arg="provider")
    def start_login_flow(self, context: ManagementContext, provider: OAuthProvider, *, realm=None, client_profile=None) -> OAuthLoginFlow:
        self._require(context, Capability.SECRETS_WRITE)
        if provider is OAuthProvider.WORKBUDDY:
            realm = realm or "cn"
            expected_profile = "ide" if realm == "global" else "cli"
            if realm not in {"cn", "global"} or client_profile not in (None, expected_profile):
                raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE,
                    fields=[ErrorField("clientProfile", "REGION_PROFILE_MISMATCH", "Unsupported WorkBuddy login region/profile")])
            client_profile = expected_profile
        elif realm is not None or client_profile is not None:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        try:
            flow = (self._flows.start(context.actor.subject_id, provider, realm=realm, client_profile=client_profile)
                    if provider is OAuthProvider.WORKBUDDY else self._flows.start(context.actor.subject_id, provider))
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.UPSTREAM_ERROR, retryable=True,
            ) from exc
        self._audit(context, "oauth.login.start", provider.value)
        return flow

    @audit_failures("oauth.login.poll", target="oauthLogin")
    def poll_login_flow(self, context: ManagementContext, flow_id: str, flow_secret: str):
        self._require(context, Capability.SECRETS_WRITE)
        device = self._flows.workbuddy
        poll = device.poll(context.actor.subject_id, flow_id, flow_secret)
        if poll.status == "ready":
            self.complete_login_flow(context, flow_id, flow_secret, CompleteOAuthLoginCommand(completed=True))
            return device.poll(context.actor.subject_id, flow_id, flow_secret)
        return poll

    @audit_failures("oauth.login.cancel", target="oauthLogin")
    def cancel_login_flow(self, context: ManagementContext, flow_id: str, flow_secret: str) -> None:
        self._require(context, Capability.SECRETS_WRITE)
        self._flows.workbuddy.cancel(context.actor.subject_id, flow_id, flow_secret)
        self._audit(context, "oauth.login.cancel", "oauthLogin")

    def list_invalid_accounts(self, context: ManagementContext, *, page: PageSpec) -> OAuthAccountPage:
        return self.list_accounts(context, account_filter=OAuthAccountFilter.INVALID, page=page)

    @audit_failures("oauth.invalid.delete-plan", target="oauthAccounts")
    def plan_invalid_deletion(
        self, context: ManagementContext, account_ids: Iterable[str] | None,
    ) -> OAuthDeletionPlan:
        self._require(context, Capability.DESTRUCTIVE)
        invalid = [
            self.backend.account_id(account)
            for account in self.backend.list_accounts()
            if (account.get("email") or self.backend.provider_of(account) == "workbuddy") and account.get("disabled_reason") == "auth_error"
        ]
        selected = invalid if account_ids is None else list(account_ids)
        if not selected or len(selected) != len(set(selected)) or not set(selected).issubset(invalid):
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        expected_accounts = tuple(
            (item, copy.deepcopy(self._account(item))) for item in selected
        )
        revision = _revision(expected_accounts)
        token, plan = self._delete_plans.create(
            actor_subject_id=context.actor.subject_id,
            kind="invalid-delete",
            revision=revision,
            payload={
                "account_ids": tuple(selected),
                "expected_accounts": expected_accounts,
            },
        )
        return OAuthDeletionPlan(token, tuple(selected), plan.expires_at, revision)

    @audit_failures("oauth.invalid.delete", target="oauthAccounts")
    def delete_invalid_accounts(self, context: ManagementContext, plan_token: str) -> int:
        self._require(context, Capability.DESTRUCTIVE)
        plan = self._delete_plans.consume(
            plan_token, actor_subject_id=context.actor.subject_id, kind="invalid-delete",
        )
        expected_accounts = tuple(plan.payload["expected_accounts"])
        outcome = self.backend.delete_invalid_accounts_conditional(expected_accounts)
        status = outcome.get("status")
        if status == "revision_conflict":
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if status == "missing":
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)
        if status != "deleted":
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        count = int(outcome.get("count") or 0)
        self._audit(context, "oauth.invalid.delete", str(count))
        return count

    @audit_failures("oauth.token.refresh", target_arg="account_id")
    def refresh_token(self, context: ManagementContext, account_id: str) -> OAuthMutationResult:
        self._require(context, Capability.WRITE)
        account = self._account(account_id)
        if self.backend.provider_of(account) == "workbuddy" and not self.backend.workbuddy_refresh_enabled():
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE,
                fields=[ErrorField("instance", "REFRESH_DISABLED", "PARROT_NO_REFRESH=1; no refresh was performed")])
        try:
            asyncio.run(self.backend.force_refresh(account_id))
        except Exception as exc:
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from exc
        self._audit(context, "oauth.token.refresh", account_id)
        return OAuthMutationResult(account_id, _revision(self._account(account_id)), "refreshed")

    def _start_operation(
        self,
        context: ManagementContext,
        store: OperationStore,
        *,
        kind: str,
        worker: Callable[[], object],
    ) -> ManagementOperation:
        operation = store.create(context, kind=kind, cancellable=False)

        def run() -> None:
            try:
                store.mark_running(operation.id)
                result = public_value(worker(), camel_case_keys=True)
                store.succeed(operation.id, result)
                self._audit(context, kind, operation.id, "succeeded")
            except ManagementError as exc:
                store.fail(operation.id, code=exc.code, message=exc.code.value, retryable=exc.retryable)
                self._audit(context, kind, operation.id, "failed")
            except Exception:
                store.fail(
                    operation.id,
                    code=ManagementErrorCode.UPSTREAM_ERROR,
                    message=ManagementErrorCode.UPSTREAM_ERROR.value,
                    retryable=True,
                )
                self._audit(context, kind, operation.id, "failed")

        if self._executor is not None:
            self._executor.submit(run)
        else:
            store.submit(operation.id, run)
        self._audit(context, kind, operation.id, "queued")
        return operation

    @audit_failures("oauth.usage.refresh", target_arg="account_id")
    def refresh_usage(
        self, context: ManagementContext, account_id: str, store: OperationStore,
    ) -> ManagementOperation:
        self._require(context, Capability.WRITE)
        self._account(account_id)
        return self._start_operation(
            context, store, kind="oauth.usage.refresh", worker=lambda: self._refresh_usage_worker([account_id]),
        )

    @audit_failures("oauth.usage.refresh-all", target="oauthAccounts")
    def refresh_all_usage(self, context: ManagementContext, store: OperationStore) -> ManagementOperation:
        self._require(context, Capability.WRITE)
        account_ids = [self.backend.account_id(account) for account in self.backend.list_accounts()]
        return self._start_operation(
            context,
            store,
            kind="oauth.usage.refresh-all",
            worker=lambda: self._refresh_usage_worker(account_ids, continue_on_error=True),
        )

    @audit_failures("oauth.quota.reset-plan", target_arg="account_id")
    def plan_quota_reset(
        self, context: ManagementContext, account_id: str,
    ) -> OAuthQuotaResetPlan:
        self._require(context, Capability.DESTRUCTIVE)
        account = self._account(account_id)
        provider = OAuthProvider(self.backend.provider_of(account))
        if provider in {OAuthProvider.CURSOR, OAuthProvider.XAI, OAuthProvider.ANTIGRAVITY, OAuthProvider.WORKBUDDY}:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        row = copy.deepcopy(self.backend.quota_load(account_id) or {})
        credit_count = row.get("openai_reset_credit_count")
        token, plan = self._quota_plans.create(
            actor_subject_id=context.actor.subject_id,
            kind="quota-reset",
            revision=_revision(account),
            payload={
                "account_id": account_id,
                "provider": provider.value,
                "quota_revision": _revision(row),
                "credit_observation": copy.deepcopy(credit_count),
                "idempotency_key": secrets.token_urlsafe(24),
            },
        )
        result = OAuthQuotaResetPlan(
            token,
            account_id,
            provider,
            int(credit_count) if isinstance(credit_count, (int, float)) else None,
            plan.expires_at,
        )
        self._audit(context, "oauth.quota.reset-plan", account_id)
        return result

    def reset_quota(
        self, context: ManagementContext, account_id: str, plan_token: str,
    ) -> OAuthMutationResult:
        self._require(context, Capability.DESTRUCTIVE)
        try:
            # Exact URL identity and every observation are checked before the
            # one-shot consume and before local/remote quota side effects.
            account = self._account(account_id)
            plan = self._quota_plans.inspect(
                plan_token, actor_subject_id=context.actor.subject_id, kind="quota-reset",
            )
            if str(plan.payload["account_id"]) != account_id:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if _revision(account) != plan.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            row = copy.deepcopy(self.backend.quota_load(account_id) or {})
            if (
                _revision(row) != plan.payload["quota_revision"]
                or row.get("openai_reset_credit_count") != plan.payload["credit_observation"]
            ):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            self._quota_plans.consume(
                plan_token, actor_subject_id=context.actor.subject_id, kind="quota-reset",
            )
            try:
                if plan.payload["provider"] == "openai":
                    asyncio.run(self.backend.redeem_openai_reset_credit(
                        account_id, str(plan.payload["idempotency_key"]),
                    ))
                else:
                    self.backend.reset_quota(account_id)
            except Exception as exc:
                raise ManagementError(
                    ManagementErrorCode.UPSTREAM_ERROR, retryable=True,
                ) from exc
            result = OAuthMutationResult(
                account_id, _revision(self._account(account_id)), "reset",
            )
        except BaseException:
            self._audit(context, "oauth.quota.reset", account_id, "failed")
            raise
        self._audit(context, "oauth.quota.reset", account_id)
        return result

    @audit_failures("oauth.errors.clear", target_arg="account_id")
    def clear_errors(self, context: ManagementContext, account_id: str) -> None:
        self._require(context, Capability.WRITE)
        self._account(account_id)
        self.backend.clear_errors(account_id)
        self._audit(context, "oauth.errors.clear", account_id)

    @audit_failures("oauth.affinity.clear", target_arg="account_id")
    def clear_affinity(self, context: ManagementContext, account_id: str) -> None:
        self._require(context, Capability.WRITE)
        self._account(account_id)
        self.backend.clear_affinity(account_id)
        self._audit(context, "oauth.affinity.clear", account_id)

    @audit_failures("oauth.errors.clear-all", target="oauthAccounts")
    def clear_all_errors(self, context: ManagementContext) -> int:
        self._require(context, Capability.DESTRUCTIVE)
        count = self.backend.clear_all_errors()
        self._audit(context, "oauth.errors.clear-all", "oauthAccounts")
        return count

    def list_models(
        self, context: ManagementContext, account_id: str, *, page: PageSpec,
    ) -> OAuthModelPage:
        self._require(context, Capability.READ)
        account = self._account(account_id)
        selection = self.backend.account_model_selection(account)
        records = {str(item.get("id") or ""): item for item in selection.get("records") or []}
        disabled = set(selection.get("disabled_models") or [])
        models = []
        for model_id in selection.get("models") or []:
            record = records.get(model_id) or {}
            state = self.backend.cooldown_state(f"oauth:{account_id}", model_id) or {}
            until = state.get("cooldown_until")
            models.append(
                OAuthModel(
                    model_id=model_names.public_id(self.backend.provider_of(account), model_id),
                    name=sanitize_text(model_names.display_name(self.backend.provider_of(account), model_id, record.get("name"))),
                    disabled=model_id in disabled,
                    cooldown_until=utc_datetime(until),
                    cooldown_permanent=until == -1,
                    metadata_source=sanitize_text(record.get("metadataSource") or selection.get("source")) if (record.get("metadataSource") or selection.get("source")) else None,
                    max_input_tokens=int(record.get("maxInputTokens") or 0) or None,
                    max_output_tokens=int(record.get("maxOutputTokens") or 0) or None,
                    reasoning_efforts=tuple(str(e) for e in record.get("reasoningEfforts") or []),
                    context_window=int(record.get("contextWindow") or record.get("context_window") or 0) or None,
                    max_context_window=int(record.get("contextWindowMaxMode") or record.get("context_window_max_mode") or 0) or None,
                    service_tier=sanitize_text(record.get("serviceTier") or record.get("service_tier")) if (record.get("serviceTier") or record.get("service_tier")) else None,
                    max_context_default=(
                        self.backend.cursor_max_context_default(account, model_id)
                        if self.backend.provider_of(account) == "cursor"
                        else None
                    ),
                )
            )
        visible, meta = _page(models, page)
        return OAuthModelPage(tuple(visible), meta, _revision(account))

    def update_models(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        model_ids: Iterable[str],
        disabled: bool,
        expected_revision: str | None = None,
    ) -> OAuthModelPage:
        self._require(context, Capability.WRITE)
        try:
            account = copy.deepcopy(self._account(account_id))
            if expected_revision and expected_revision != _revision(account):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            selection = self.backend.account_model_selection(account)
            visible = set(selection.get("models") or [])
            requested_list = [model_names.upstream_id(self.backend.provider_of(account), model_id) for model_id in model_ids]
            fields = [
                ErrorField(f"modelIds[{index}]", "UNKNOWN_MODEL", "Model is not visible")
                for index, model_id in enumerate(requested_list)
                if model_id not in visible
            ]
            if not requested_list or fields:
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED, fields=fields,
                )
            requested = set(requested_list)
            current = set(selection.get("disabled_models") or [])
            wanted = current | requested if disabled else current - requested
            outcome = self.backend.update_account_models_conditional(
                account_id,
                account,
                visible_models=visible,
                disabled_models=wanted,
            )
            self._raise_conditional_status(outcome)
            result = self.list_models(context, account_id, page=PageSpec())
        except BaseException:
            self._audit(context, "oauth.models.update", account_id, "failed")
            raise
        self._audit(context, "oauth.models.update", account_id)
        return result

    def update_model_settings(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        model_id: str,
        max_context_default: bool,
        expected_revision: str | None = None,
    ) -> OAuthModelPage:
        self._require(context, Capability.WRITE)
        try:
            account = copy.deepcopy(self._account(account_id))
            if expected_revision and expected_revision != _revision(account):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            selection = self.backend.account_model_selection(account)
            if self.backend.provider_of(account) != "cursor":
                raise ManagementError(
                    ManagementErrorCode.UNSUPPORTED_VALUE,
                    fields=(ErrorField("modelId", "WRONG_PROVIDER", "Cursor account required"),),
                )
            model_id = model_names.upstream_id(self.backend.provider_of(account), model_id)
            visible = set(selection.get("models") or [])
            record = next((
                item for item in selection.get("records") or []
                if str(item.get("id") or "") == model_id
            ), None)
            normal = int((record or {}).get("contextWindow") or (record or {}).get("context_window") or 0)
            maximum = int((record or {}).get("contextWindowMaxMode") or (record or {}).get("context_window_max_mode") or 0)
            if model_id not in visible or record is None:
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=(ErrorField("modelId", "UNKNOWN_MODEL", "Model is not visible"),),
                )
            if maximum <= normal:
                raise ManagementError(
                    ManagementErrorCode.UNSUPPORTED_VALUE,
                    fields=(ErrorField("modelId", "UNSUPPORTED_TIER", "Model has no Max Context tier"),),
                )
            outcome = self.backend.update_cursor_model_setting_conditional(
                account_id,
                account,
                model_id=model_id,
                enabled=max_context_default,
            )
            self._raise_conditional_status(outcome)
            result = self.list_models(context, account_id, page=PageSpec())
        except BaseException:
            self._audit(context, "oauth.models.settings.update", account_id, "failed")
            raise
        self._audit(context, "oauth.models.settings.update", account_id)
        return result

    @audit_failures("oauth.models.sync", target_arg="account_id")
    def sync_models(
        self, context: ManagementContext, account_id: str, store: OperationStore,
    ) -> ManagementOperation:
        self._require(context, Capability.WRITE)
        self._account(account_id)

        def worker() -> dict:
            result = asyncio.run(self.backend.refresh_account_models(account_id))
            action = str((result or {}).get("action") or "error")
            if action != "updated":
                if action == "timeout":
                    raise ManagementError(
                        ManagementErrorCode.UPSTREAM_TIMEOUT, retryable=True,
                    )
                if action == "network_disabled":
                    raise ManagementError(
                        ManagementErrorCode.DEPENDENCY_UNAVAILABLE, retryable=True,
                    )
                if action == "stale":
                    raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
                raise ManagementError(
                    ManagementErrorCode.UPSTREAM_ERROR, retryable=True,
                )
            return result

        return self._start_operation(context, store, kind="oauth.models.sync", worker=worker)

    def get_settings(self, context: ManagementContext) -> OAuthSettings:
        self._require(context, Capability.READ)
        enabled, interval, threshold, mode, fingerprint_enabled = self.backend.get_settings()
        raw = (enabled, interval, threshold, mode, fingerprint_enabled)
        return OAuthSettings(
            enabled, interval, threshold, CchMode(mode), fingerprint_enabled, _revision(raw),
        )

    def update_settings(
        self,
        context: ManagementContext,
        *,
        quota_enabled: bool | None = None,
        interval_seconds: int | None = None,
        threshold_percent: float | None = None,
        cch_mode: CchMode | None = None,
        antigravity_tls_fingerprint_enabled: bool | None = None,
        expected_revision: str | None = None,
    ) -> OAuthSettings:
        self._require(context, Capability.WRITE)
        if context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
            # Preserve the frozen Telegram setter path, including fake/domain
            # callback behavior.  Management API requests use the atomic CAS
            # path below even when If-Match is omitted.
            current = self.get_settings(context)
            if expected_revision and expected_revision != current.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            if interval_seconds is not None and not 10 <= interval_seconds <= 86400:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            if threshold_percent is not None and not 1 <= threshold_percent <= 100:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            self.backend.update_settings(
                quota_enabled=quota_enabled,
                interval_seconds=interval_seconds,
                threshold_percent=threshold_percent,
                cch_mode=cch_mode.value if cch_mode else None,
                antigravity_tls_fingerprint_enabled=antigravity_tls_fingerprint_enabled,
            )
            self._audit(context, "oauth.settings.update", "oauthSettings")
            return self.get_settings(context)
        try:
            current = self.get_settings(context)
            if expected_revision and expected_revision != current.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            if interval_seconds is not None and not 10 <= interval_seconds <= 86400:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            if threshold_percent is not None and not 1 <= threshold_percent <= 100:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            expected = (
                current.quota_monitor_enabled,
                current.quota_monitor_interval_seconds,
                current.quota_monitor_threshold_percent,
                current.cch_mode.value,
                current.antigravity_tls_fingerprint_enabled,
            )
            outcome = self.backend.update_settings_conditional(
                expected,
                quota_enabled=quota_enabled,
                interval_seconds=interval_seconds,
                threshold_percent=threshold_percent,
                cch_mode=cch_mode.value if cch_mode else None,
                antigravity_tls_fingerprint_enabled=antigravity_tls_fingerprint_enabled,
            )
            self._raise_conditional_status(outcome)
            result = self.get_settings(context)
        except BaseException:
            self._audit(context, "oauth.settings.update", "oauthSettings", "failed")
            raise
        self._audit(context, "oauth.settings.update", "oauthSettings")
        return result

    def get_telegram_preferences(self, context: ManagementContext) -> TelegramOAuthPreferences:
        self._require(context, Capability.READ)
        mode, progress = self.backend.get_preferences()
        return TelegramOAuthPreferences(OAuthUsageDisplayMode(mode), progress, _revision((mode, progress)))

    def update_telegram_preferences(
        self,
        context: ManagementContext,
        *,
        usage_display_mode: OAuthUsageDisplayMode | None = None,
        quota_progress_bar: bool | None = None,
        expected_revision: str | None = None,
    ) -> TelegramOAuthPreferences:
        self._require(context, Capability.WRITE)
        if context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
            current = self.get_telegram_preferences(context)
            if expected_revision and expected_revision != current.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            self.backend.update_preferences(
                usage_display_mode=(
                    usage_display_mode.value if usage_display_mode else None
                ),
                quota_progress_bar=quota_progress_bar,
            )
            self._audit(
                context, "oauth.telegram-preferences.update",
                "telegramOAuthPreferences",
            )
            return self.get_telegram_preferences(context)
        try:
            current = self.get_telegram_preferences(context)
            if expected_revision and expected_revision != current.revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            expected = (
                current.usage_display_mode.value,
                current.quota_progress_bar,
            )
            outcome = self.backend.update_preferences_conditional(
                expected,
                usage_display_mode=usage_display_mode.value if usage_display_mode else None,
                quota_progress_bar=quota_progress_bar,
            )
            self._raise_conditional_status(outcome)
            result = self.get_telegram_preferences(context)
        except BaseException:
            self._audit(
                context, "oauth.telegram-preferences.update",
                "telegramOAuthPreferences", "failed",
            )
            raise
        self._audit(context, "oauth.telegram-preferences.update", "telegramOAuthPreferences")
        return result
