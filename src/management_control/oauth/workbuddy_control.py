"""Transport-neutral WorkBuddy views, confirmations and action operations."""
from __future__ import annotations

import copy

from src.management_auth.principal import Capability
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .contracts import public_value, revision


class WorkBuddyControlMixin:
    def _workbuddy_account(self, account_id):
        account = self._account(account_id)
        if self.backend.provider_of(account) != "workbuddy":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        return account

    @staticmethod
    def _workbuddy_error(error):
        kinds = {"unsupported_region", "account_paused", "status_unknown", "activity_inactive",
                 "free_trial_terms_confirmation_required", "disabled", "business_date_changed",
                 "stale_generation", "auto_not_enabled", "account_missing"}
        kind = getattr(error, "kind", None)
        if kind in kinds:
            return ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE,
                fields=[ErrorField("workbuddy", kind.upper(), kind)])
        return ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True)

    def get_workbuddy(self, context, account_id):
        self._require(context, Capability.READ)
        self._workbuddy_account(account_id)
        return {"snapshot": self.backend.workbuddy_snapshot(account_id),
                "actions": self.backend.workbuddy_action_history(account_id)}

    def get_workbuddy_records(self, context, account_id, *, page=1, page_size=20):
        self._require(context, Capability.READ)
        self._workbuddy_account(account_id)
        if page < 1 or not 1 <= page_size <= 100:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        values = self.backend.workbuddy_action_history(account_id, limit=None)
        start = (page - 1) * page_size
        return {"items": values[start:start + page_size], "page": page,
                "page_size": page_size, "total": len(values), "has_next": start + page_size < len(values)}

    def workbuddy_policy(self, context):
        self._require(context, Capability.READ)
        return {"client_profile": "cli", "client_profiles_by_realm": {"cn": "cli", "global": "workbuddy-cli"},
                "supported_client_profiles_by_realm": {"cn": ["cli"], "global": ["workbuddy-cli", "ide"]},
                "client_routes": {"workbuddy-cli": "WorkBuddy CLI · www.workbuddy.ai", "ide": "CodeBuddy IDE · www.codebuddy.ai"},
                "browser_login_realms": ["cn", "global"], "import_realms": [],
                "effects_enabled": self.backend.workbuddy_effects_enabled(), "auto_checkin_default": False,
                "auto_checkin_time": "09:05", "auto_checkin_times": ["09:05", "21:05"],
                "timezone": "Asia/Shanghai", "auto_trial": False}

    def refresh_workbuddy_status_now(self, context, account_id):
        self._require(context, Capability.WRITE)
        self._workbuddy_account(account_id)
        # Reconciliation only queries existing uncertain actions, even in refresh
        # protection mode. It cannot create an intent or dispatch an activity.
        self.backend.workbuddy_reconcile_pending(account_id)
        self._refresh_usage_account(account_id)
        return self.backend.workbuddy_snapshot(account_id)

    def refresh_workbuddy_status(self, context, account_id, store):
        self._require(context, Capability.WRITE)
        self._workbuddy_account(account_id)
        return self._start_operation(context, store, kind="oauth.workbuddy.status.refresh",
            worker=lambda: self.refresh_workbuddy_status_now(context, account_id))

    def plan_workbuddy_action(self, context, account_id, action, *, allow_unknown=False,
                             free_trial_confirmed=False, retry_failed=False):
        self._require(context, Capability.WRITE)
        account = self._workbuddy_account(account_id)
        if action not in {"checkin", "claim_trial"}:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        try:
            observation = self.backend.workbuddy_inspect_action(account_id, action,
                allow_unknown=allow_unknown, free_trial_confirmed=free_trial_confirmed)
        except Exception as exc:
            raise self._workbuddy_error(exc) from None
        token, plan = self._workbuddy_plans.create(actor_subject_id=context.actor.subject_id,
            kind="workbuddy-action", revision=revision(account), payload={
                "account_id": account_id, "action": action,
                "expected_day": observation["business_date"],
                "expected_generation": self.backend.workbuddy_credential_generation(account),
                "allow_unknown": bool(allow_unknown), "free_trial_confirmed": bool(free_trial_confirmed),
                "retry_failed": bool(retry_failed)})
        self._audit(context, "oauth.workbuddy.plan", account_id)
        return {"plan_token": token, "account_id": account_id, "action": action,
                "business_date": observation["business_date"], "expires_at": plan.expires_at,
                "observed_status": observation.get("observed_status"),
                "prior_result": observation.get("prior_result"),
                "allow_unknown": bool(allow_unknown), "free_trial_confirmed": bool(free_trial_confirmed),
                "retry_failed": bool(retry_failed)}

    def _consume_workbuddy_plan(self, context, account_id, plan_token):
        self._require(context, Capability.WRITE)
        account = self._workbuddy_account(account_id)
        plan = self._workbuddy_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id, kind="workbuddy-action")
        if plan.payload["account_id"] != account_id:
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        if revision(account) != plan.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self._workbuddy_plans.consume(plan_token, actor_subject_id=context.actor.subject_id, kind="workbuddy-action")
        return copy.deepcopy(plan.payload)

    def _execute_workbuddy_plan(self, context, payload, *, before_submit=None):
        values = dict(payload)
        account_id, action = values.pop("account_id"), values.pop("action")
        try:
            result = self.backend.workbuddy_execute_action(account_id, action,
                actor=context.actor.subject_id, before_submit=before_submit, **values)
        except ManagementError:
            raise
        except Exception as exc:
            raise self._workbuddy_error(exc) from None
        self._audit(context, "oauth.workbuddy." + action, account_id, result.get("status", "unknown"))
        return result

    def execute_workbuddy_action_now(self, context, account_id, plan_token):
        return self._execute_workbuddy_plan(context, self._consume_workbuddy_plan(context, account_id, plan_token))

    def execute_workbuddy_action(self, context, account_id, plan_token, store):
        payload = self._consume_workbuddy_plan(context, account_id, plan_token)
        operation = store.create(context, kind="oauth.workbuddy." + payload["action"], cancellable=True)
        def run():
            try:
                if store.cancel_requested(operation.id):
                    return
                store.mark_running(operation.id)
                result = self._execute_workbuddy_plan(context, payload,
                    before_submit=lambda: store.seal_cancellation(operation.id))
                if not store.cancel_requested(operation.id):
                    store.succeed(operation.id, public_value(result, camel_case_keys=True))
            except Exception as exc:
                if store.cancel_requested(operation.id):
                    return
                code = exc.code if isinstance(exc, ManagementError) else ManagementErrorCode.UPSTREAM_ERROR
                store.fail(operation.id, code=code, message=code.value, retryable=False)
        if self._executor is not None:
            self._executor.submit(run)
        else:
            store.submit(operation.id, run)
        return operation

    def update_workbuddy_settings(self, context, account_id, *, auto_checkin, expected_revision=None):
        self._require(context, Capability.WRITE)
        current = copy.deepcopy(self._workbuddy_account(account_id))
        if type(auto_checkin) is not bool:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        if auto_checkin and current.get("realm") != "cn":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        if expected_revision and revision(current) != expected_revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        result = self.backend.workbuddy_update_settings(account_id, current, auto_checkin)
        self._raise_conditional_status(result)
        self._audit(context, "oauth.workbuddy.settings", account_id)
        return {"auto_checkin": auto_checkin, "timezone": "Asia/Shanghai", "scheduled_time": "09:05",
                "scheduled_times": ["09:05", "21:05"],
                "effects_enabled": self.backend.workbuddy_effects_enabled(),
                "revision": revision(self._account(account_id))}
