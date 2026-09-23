"""Atomic Management API account/create/replace mutation use cases."""

from __future__ import annotations

import copy
import hashlib
import hmac
from typing import Iterable

from src.management_auth.principal import AuthMethod, Capability
from src.management_control.context import ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .contracts import candidate_revision, credential_fingerprint, revision
from .models import (
    CompleteOAuthLoginCommand,
    CreateOAuthAccountCommand,
    OAuthMutationResult,
    OAuthProvider,
    UpdateOAuthAccountCommand,
)


class OAuthReplaceRequired(ManagementError):
    """Identity conflict carrying a typed one-shot plan outside exception text."""

    def __init__(self, account_id: str, plan_token: str) -> None:
        super().__init__(
            ManagementErrorCode.IDENTITY_CONFLICT,
            fields=(ErrorField("accountId", "EXACT_IDENTITY", account_id),),
        )
        self.account_id = account_id
        self.plan_token = plan_token


class OAuthAccountMutationControlMixin:
    @staticmethod
    def _raise_conditional_status(result: dict) -> None:
        status = result.get("status")
        if status == "revision_conflict":
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if status in {"identity_conflict", "resource_conflict"}:
            raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
        if status == "missing":
            raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def _replace_conflict(
        self,
        context: ManagementContext,
        *,
        account_id: str,
        old_account: dict,
        entry: dict,
        flow_binding: tuple[str, str] | None = None,
    ) -> None:
        payload = {
            "account_id": account_id,
            "old_account": copy.deepcopy(old_account),
            "old_revision": revision(old_account),
            "candidate_identity": self.backend.account_id(entry),
            "candidate_revision": candidate_revision(entry),
            "credential_fingerprint": credential_fingerprint(entry),
            # Commit always uses this exact exchange/import observation.  The
            # bearer plan remains bounded, process-local, actor-bound and 10m TTL.
            "entry": copy.deepcopy(entry),
        }
        if flow_binding is not None:
            payload["flow_id"] = flow_binding[0]
            payload["flow_secret_verifier"] = hashlib.sha256(
                flow_binding[1].encode("utf-8"),
            ).digest()
        token, _ = self._replace_plans.create(
            actor_subject_id=context.actor.subject_id,
            kind="replace",
            revision=payload["old_revision"],
            payload=payload,
        )
        raise OAuthReplaceRequired(account_id, token)

    def _commit_replace_plan(
        self,
        context: ManagementContext,
        token: str,
        candidate: dict | None = None,
        flow_binding: tuple[str, str] | None = None,
    ) -> OAuthMutationResult:
        plan = self._replace_plans.inspect(
            token, actor_subject_id=context.actor.subject_id, kind="replace",
        )
        payload = plan.payload
        bound_flow_id = payload.get("flow_id")
        if bound_flow_id is not None:
            if flow_binding is None or flow_binding[0] != bound_flow_id:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if flow_binding[1]:
                supplied = hashlib.sha256(flow_binding[1].encode("utf-8")).digest()
                if not hmac.compare_digest(supplied, payload["flow_secret_verifier"]):
                    raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        bound_entry = payload["entry"]
        self._ensure_legacy_identity_safe(bound_entry)
        if candidate is not None and (
            self.backend.account_id(candidate) != payload["candidate_identity"]
            or candidate_revision(candidate) != payload["candidate_revision"]
            or credential_fingerprint(candidate) != payload["credential_fingerprint"]
        ):
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        account_id = str(payload["account_id"])
        current = self._account(account_id)
        if revision(current) != payload["old_revision"]:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self._replace_plans.consume(
            token, actor_subject_id=context.actor.subject_id, kind="replace",
        )
        result = self.backend.replace_exact_identity_conditional(
            account_id, copy.deepcopy(bound_entry), copy.deepcopy(payload["old_account"]),
        )
        self._raise_conditional_status(result)
        if result.get("status") != "replaced":
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        post_save = self._post_save_account_effects(account_id, bound_entry)
        account_id = post_save.get("account_id", account_id)
        # Usage/quota evaluation may synchronously change enabled state, so the
        # response revision must describe the post-effect account, not the
        # replacement helper's pre-effect snapshot.
        saved = self._account(account_id)
        return OAuthMutationResult(account_id, revision(saved), "replaced", post_save=post_save)

    def _create_entry(
        self,
        context: ManagementContext,
        entry: dict,
        *,
        replace_plan_token: str | None = None,
        flow_binding: tuple[str, str] | None = None,
    ) -> OAuthMutationResult:
        if entry.get("provider") == "zhipu":
            entry = self.backend.zhipu_normalize_credential(entry)
        fields_required = () if entry.get("provider") == "zhipu" else ("uid", "access_token", "refresh_token") if entry.get("provider") == "workbuddy" else ("email", "access_token", "refresh_token")
        required = [field for field in fields_required if not entry.get(field)]
        if required:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=[ErrorField(field, "REQUIRED", "Field is required") for field in required],
            )
        self._ensure_legacy_identity_safe(entry)
        if replace_plan_token:
            return self._commit_replace_plan(context, replace_plan_token, entry)
        existing = self.backend.find_exact_identity(entry)
        if existing is not None:
            self._replace_conflict(
                context,
                account_id=existing[0],
                old_account=existing[1],
                entry=entry,
                flow_binding=flow_binding,
            )
        result = self.backend.add_account_if_absent(copy.deepcopy(entry))
        if result.get("status") != "added":
            # A competing exact add won.  Return a fresh replace plan rather than
            # degrading the explicit-replace contract to an implicit upsert.
            existing = self.backend.find_exact_identity(entry)
            if existing is not None:
                self._replace_conflict(
                    context,
                    account_id=existing[0],
                    old_account=existing[1],
                    entry=entry,
                    flow_binding=flow_binding,
                )
            raise ManagementError(ManagementErrorCode.IDENTITY_CONFLICT)
        account_id = str(result.get("account_key") or self.backend.account_id(entry))
        post_save = self._post_save_account_effects(account_id, entry)
        account_id = post_save.get("account_id", account_id)
        return OAuthMutationResult(
            account_id=account_id,
            revision=revision(self._account(account_id)),
            status="created", post_save=post_save,
        )

    def create_account(
        self, context: ManagementContext, command: CreateOAuthAccountCommand,
    ) -> OAuthMutationResult:
        self._require(context, Capability.SECRETS_WRITE)
        try:
            try:
                entry = self._flows.credential_entry(command.credential)
            except ManagementError:
                raise
            except Exception as exc:
                raise ManagementError(
                    ManagementErrorCode.UPSTREAM_ERROR, retryable=True,
                ) from exc
            result = self._create_entry(
                context, entry, replace_plan_token=command.replace_plan_token,
            )
        except BaseException:
            self._audit(context, "oauth.account.create", "oauthAccounts", "failed")
            raise
        self._audit(context, f"oauth.account.{result.status}", result.account_id)
        return result

    def complete_login_flow(
        self,
        context: ManagementContext,
        flow_id: str,
        flow_secret: str,
        command: CompleteOAuthLoginCommand,
    ) -> OAuthMutationResult:
        self._require(context, Capability.SECRETS_WRITE)
        try:
            if flow_id.startswith(("wbflow_", "zhflow_")):
                if command.completed is not True:
                    raise ManagementError(ManagementErrorCode.INVALID_REQUEST)
                device = self._flows.zhipu if flow_id.startswith("zhflow_") else self._flows.workbuddy
                saved = device.completed_result(context.actor.subject_id, flow_id, flow_secret)
                if saved is not None:
                    return saved
                with device.lease(context.actor.subject_id, flow_id, flow_secret) as plan, \
                        device.saving(context.actor.subject_id, flow_id, flow_secret):
                    entry = device.ready(plan)
                    if flow_id.startswith("zhflow_") and not plan.payload.get("source_account_id"):
                        entry = self.backend.zhipu_login_entry(entry)
                    if flow_id.startswith("zhflow_") and plan.payload.get("source_account_id"):
                        account_id, status = self.backend.zhipu_bind_project(
                            plan.payload["source_account_id"], plan.payload["source_account"], entry)
                        post_save = self._post_save_account_effects(account_id, self._account(account_id))
                        account_id = post_save.get("account_id", account_id)
                        result = OAuthMutationResult(account_id, revision(self._account(account_id)), status, post_save=post_save)
                    elif command.replace_plan_token:
                        result = self._commit_replace_plan(
                            context, command.replace_plan_token, candidate=entry,
                            flow_binding=(flow_id, flow_secret),
                        )
                    else:
                        try:
                            result = self._create_entry(context, entry, flow_binding=(flow_id, flow_secret))
                        except OAuthReplaceRequired as conflict:
                            # WorkBuddy browser login explicitly authorizes saving
                            # this exact identity. Keep the existing CAS replacement
                            # path and its settings preservation; never implicit upsert.
                            result = self._commit_replace_plan(
                                context, conflict.plan_token, candidate=entry,
                                flow_binding=(flow_id, flow_secret),
                            )
                    device.finish(context.actor.subject_id, flow_id, flow_secret, completed=True,
                                  result=result, preview=device.preview(plan.payload))
                    plan.payload.clear()
            elif command.replace_plan_token:
                result = self._commit_replace_plan(
                    context,
                    command.replace_plan_token,
                    flow_binding=(flow_id, flow_secret),
                )
            else:
                completed = self._flows.complete(
                    context.actor.subject_id, flow_id, flow_secret, command,
                )
                result = self._create_entry(
                    context,
                    completed.entry,
                    flow_binding=(flow_id, flow_secret),
                )
        except BaseException:
            self._audit(context, "oauth.login.complete", "oauthLogin", "failed")
            raise
        self._audit(context, "oauth.login.complete", result.account_id)
        return result

    def update_account(
        self,
        context: ManagementContext,
        account_id: str,
        command: UpdateOAuthAccountCommand,
        *,
        expected_revision: str | None = None,
    ):
        self._require(context, Capability.WRITE)
        try:
            current = copy.deepcopy(self._account(account_id))
            if expected_revision and expected_revision != revision(current):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            outcome = self.backend.update_account_conditional(
                account_id,
                current,
                display_name=command.display_name,
                enabled=command.enabled,
                max_concurrent=command.max_concurrent,
            )
            self._raise_conditional_status(outcome)
            result = self.get_account(context, account_id)
        except BaseException:
            self._audit(context, "oauth.account.update", account_id, "failed")
            raise
        self._audit(context, "oauth.account.update", account_id)
        return result

    def delete_account(
        self,
        context: ManagementContext,
        account_id: str,
        *,
        expected_revision: str | None = None,
    ) -> None:
        self._require(context, Capability.DESTRUCTIVE)
        if context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
            # Telegram's v0.31.13 adapter deliberately keeps the historical
            # alias-resolving, unconditional delete path.  Management API
            # requests continue through the atomic snapshot CAS below.
            self._legacy_account(account_id)
            self.backend.delete_account(account_id)
            self._audit(context, "oauth.account.delete", account_id)
            return
        try:
            account = copy.deepcopy(self._account(account_id))
            if expected_revision is not None and expected_revision != revision(account):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            outcome = self.backend.delete_account_conditional(account_id, account)
            self._raise_conditional_status(outcome)
        except BaseException:
            self._audit(context, "oauth.account.delete", account_id, "failed")
            raise
        self._audit(context, "oauth.account.delete", account_id)

    def reorder_accounts(
        self,
        context: ManagementContext,
        account_ids: Iterable[str],
        *,
        expected_revision: str | None = None,
    ) -> str:
        self._require(context, Capability.WRITE)
        try:
            configured = [
                self.backend.account_id(account)
                for account in self.backend.list_accounts()
            ]
            order = list(account_ids)
            if (
                len(configured) != len(set(configured))
                or len(order) != len(configured)
                or len(order) != len(set(order))
                or set(order) != set(configured)
            ):
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            current_revision = revision(configured)
            if expected_revision is not None and expected_revision != current_revision:
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            outcome = self.backend.reorder_accounts_conditional(configured, order)
            self._raise_conditional_status(outcome)
        except BaseException:
            self._audit(context, "oauth.account.reorder", "oauthAccounts", "failed")
            raise
        self._audit(context, "oauth.account.reorder", "oauthAccounts")
        return revision(order)
