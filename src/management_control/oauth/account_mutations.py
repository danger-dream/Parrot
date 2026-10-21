"""Atomic Management API account/create/replace mutation use cases."""

from __future__ import annotations

import copy
import hashlib
import hmac
from typing import Iterable

from src.management_auth.principal import AuthMethod, Capability
from src.management_control.context import ManagementContext
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.oauth import add_context
from src.safe_diagnostics import diagnose

from .contracts import audit_failures, candidate_revision, credential_fingerprint, revision
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

    def notification_operation(self, context, account_id, expected_account, action, *, stages=None):
        """Exact single-account management adapter, no global monitor or implicit benefits."""
        import asyncio
        import os
        from src import oauth_manager
        from src.notifications import reporting
        self._require(context, Capability.WRITE)
        if revision(self._account(account_id)) != revision(expected_account):
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        expected = oauth_manager.account_state_key(expected_account)
        observation = reporting.observation(account_id)
        with oauth_manager.exact_operation_target(account_id, expected):
            if action == "refresh_token":
                if os.environ.get("PARROT_NO_REFRESH") == "1":
                    raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
                result = self.refresh_token(context, account_id)
                reporting.refreshed(account_id, source="notification_action", observation=observation)
                return {"status": "succeeded"}
            if action == "sync_models":
                from src.notifications.events import COMPLETED_STAGES
                requested = {stage["name"] for stage in stages or [] if stage["status"] not in COMPLETED_STAGES}
                outcomes = []
                if stages is None or "模型目录" in requested:
                    result = asyncio.run(self.backend.refresh_account_models(account_id))
                    status = result.get("action")
                    outcomes.append({"name": "模型目录", "status": "succeeded" if status in {"updated", "not_modified"} else "unknown" if status == "timeout" else "failed"})
                if "元数据" in requested:
                    from src import model_pricing
                    result = asyncio.run(model_pricing.refresh_metadata_after_model_sync())
                    outcomes.append({"name": "元数据", "status": result.get("status", "unknown")})
                ok = outcomes and all(stage["status"] in COMPLETED_STAGES for stage in outcomes)
                unknown = any(stage["status"] in {"unknown", "pending", "running"} for stage in outcomes)
                return {"status": "succeeded" if ok else "unknown" if unknown else "failed", "stages": outcomes}
            if action != "refresh_usage":
                raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
            # Fetch through the existing backend. Commit/evaluate ONLY under the
            # same-generation lock after I/O; never hold that lock over network.
            with oauth_manager.usage_commit_observation(expected_account):
                usage = asyncio.run(self.backend.fetch_usage_snapshot(account_id))
                with oauth_manager.account_generation_guard(expected) as current:
                    if not current:
                        raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
                    oauth_manager.validate_usage_commit(account_id)
                    usage, result = self._save_and_evaluate_usage(account_id, usage)
            action_result = (result or {}).get("action")
            if action_result in {"disabled", "disabled_updated"}:
                reporting.quota(account_id, usage=usage, windows=(result or {}).get("hit_windows", []), source="notification_action", observation=observation)
            elif action_result in {"resumed", "resumed_manual"}:
                reporting.quota(account_id, recovered=True, usage=usage, source="notification_action", observation=observation)
            failed = bool(action_result) and ("failed" in action_result or "conflict" in action_result or action_result == "noop_missing")
            unknown = not action_result or any(term in action_result for term in ("unknown", "stale"))
            status = "failed" if failed else "unknown" if unknown else "succeeded"
            return {"status": status, "quota_action": action_result,
                    "stages": [{"name": "额度快照", "status": "succeeded"},
                               {"name": "状态评估/保存", "status": status}]}

    def set_notification_policy(self, context, account_id, expected_account, *, allow_credits):
        """Exact CAS setter for confirmed notification intent; never a toggle or network call."""
        self._require(context, Capability.WRITE)
        account = self._account(account_id)
        if revision(account) != revision(expected_account):
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if self.backend.provider_of(account) != "openai" or allow_credits is not False:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        outcome = self.backend.update_account_conditional(account_id, expected_account, allow_credits=False)
        self._raise_conditional_status(outcome)
        if outcome.get("status") != "updated":
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        self.backend.evaluate_cached_quota(account_id)
        self._audit(context, "oauth.account.allow-credits", account_id)
        return {"allowed": self._account(account_id).get("allowCredits") is True}

    def clear_notification_cooldown(self, context, account_id, expected_account, model):
        """Clear only a confirmed exact model under the account lifecycle lock."""
        from src import oauth_manager, cooldown
        self._require(context, Capability.WRITE)
        from src.notifications.providers import capabilities
        if "clear_cooldown" not in capabilities(self._account(account_id)):
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        expected = oauth_manager.account_state_key(expected_account)
        with oauth_manager.account_generation_guard(expected) as current:
            if not current or revision(self._account(account_id)) != revision(expected_account):
                raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
            cooldown.clear(expected, model=model)
        self._audit(context, "oauth.model.clear-cooldown", account_id)
        return {"model": model, "cleared": True}

    @audit_failures("oauth.account.allow-credits", target_arg="account_id")
    def toggle_openai_credits(self, context: ManagementContext, account_id: str) -> dict:
        """Toggle workspace-local spending consent, then re-evaluate quota."""
        self._require(context, Capability.WRITE)
        account = self._legacy_account(account_id)
        if self.backend.provider_of(account) != "openai":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        allowed = account.get("allowCredits") is not True
        result = self.backend.update_account_conditional(
            self.backend.account_id(account), account, allow_credits=allowed,
        )
        self._raise_conditional_status(result)
        if result.get("status") != "updated":
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        self._audit(context, "oauth.account.allow-credits", account_id)
        # Turning off applies the existing gate immediately, even if WHAM is
        # unavailable. Turning on requires a fresh read before lifting a pause.
        if not allowed:
            self.backend.evaluate_cached_quota(account_id)
        refreshed = self.refresh_usage_now(context, account_id)
        return {**refreshed, "allowed": allowed}

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
            "add_attempt": add_context.current(),
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
        with add_context.bind(payload.get("add_attempt")):
            return self._commit_bound_replace_plan(context, token, payload, candidate, flow_binding)

    @diagnose(operation="account_replace", stage="account_save")
    def _commit_bound_replace_plan(self, context, token, payload, candidate, flow_binding):
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

    @diagnose(operation="account_create", stage="account_save")
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
        fields_required = () if entry.get("provider") == "zhipu" else ("uid", "access_token", "refresh_token") if entry.get("provider") == "workbuddy" else ("uid", "access_token", "machine_id") if entry.get("provider") == "qoder" else ("email", "access_token", "refresh_token")
        required = [field for field in fields_required if not entry.get(field)]
        if required:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=[ErrorField(field, "REQUIRED", "Field is required") for field in required],
            )
        self._ensure_legacy_identity_safe(entry)
        if entry.get("provider") == "qoder" and self.backend.find_exact_identity(entry) is not None:
            raise ManagementError(ManagementErrorCode.IDENTITY_CONFLICT,
                fields=[ErrorField("qoder", "RELOGIN_REQUIRED", "账户已存在，请从原账户重授权，保留绑定设备")])
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
                if entry.get("provider") == "qoder":
                    raise ManagementError(ManagementErrorCode.IDENTITY_CONFLICT)
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
            if flow_id.startswith(("wbflow_", "zhflow_", "qdflow_")):
                if command.completed is not True:
                    raise ManagementError(ManagementErrorCode.INVALID_REQUEST)
                device = self._flows.device_for(flow_id)
                saved = device.completed_result(context.actor.subject_id, flow_id, flow_secret)
                if saved is not None:
                    return saved
                with device.lease(context.actor.subject_id, flow_id, flow_secret) as plan, \
                        device.saving(context.actor.subject_id, flow_id, flow_secret):
                    entry = device.ready(plan)
                    relogin = plan.payload.get("relogin")
                    if flow_id.startswith("zhflow_") and not relogin and not plan.payload.get("source_account_id"):
                        entry = self.backend.zhipu_login_entry(entry)
                    if relogin:
                        # Bind before any save or post-save provider side effects.
                        # A wrong browser identity must never become an add/upsert.
                        old = relogin["account"]
                        if flow_id.startswith("zhflow_"):
                            if (any(entry.get(k) != old.get(k) for k in ("provider", "site", "credential_mode", "subject"))
                                    or any(entry.get(k) and entry[k] != old.get(k) for k in ("organization_id", "project_id"))):
                                raise ManagementError(ManagementErrorCode.IDENTITY_CONFLICT)
                            entry.update({k: old[k] for k in ("organization_id", "project_id", "plan_scope") if k in old})
                        outcome = self.replace_account_entry(
                            context, relogin["target_key"], entry, expected_account=old,
                            authenticated_login=flow_id.startswith(("zhflow_", "qdflow_")),
                        )
                        self._raise_conditional_status(outcome)
                        if outcome.get("status") != "replaced":
                            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
                        account_id = outcome.get("account_key") or relogin["target_key"]
                        result = OAuthMutationResult(account_id, revision(self._account(account_id)), "replaced", post_save=outcome.get("_post_save"))
                    elif flow_id.startswith("zhflow_") and plan.payload.get("source_account_id"):
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
                            attempt = add_context.current()
                            if attempt is not None and attempt.target is not None:
                                # Explicit add routing must not change an existing
                                # account until its overwrite is confirmed.
                                raise
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
                with add_context.bind(completed.add_attempt):
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
