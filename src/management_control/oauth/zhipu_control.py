"""Shared API/TG Zhipu initialization and explicitly confirmed card consumption."""
from __future__ import annotations

import copy
from src.management_auth.principal import Capability
from src.management_control.errors import ManagementError, ManagementErrorCode as Code
from .contracts import revision
from .models import CompleteOAuthLoginCommand, OAuthMutationResult


class ZhipuControlMixin:
    def _zhipu_account(self, account_id, *, oauth=False):
        account = self._account(account_id)
        if self.backend.provider_of(account) != "zhipu" or (oauth and account.get("credential_mode") != "oauth"):
            raise ManagementError(Code.UNSUPPORTED_VALUE)
        return copy.deepcopy(account)

    def start_zhipu_callback_login(self, context, *, site):
        self._require(context, Capability.SECRETS_WRITE)
        flow = self._flows.zhipu.start(context.actor.subject_id, site=site, callback_mode=True)
        self._audit(context, "oauth.login.start", "zhipu")
        return flow

    def submit_zhipu_callback(self, context, flow_id, flow_secret, callback_url):
        self._require(context, Capability.SECRETS_WRITE)
        poll = self._flows.zhipu.submit_callback(context.actor.subject_id, flow_id, flow_secret, callback_url)
        if poll.status == "ready":
            self.complete_login_flow(context, flow_id, flow_secret, CompleteOAuthLoginCommand(completed=True))
            return self._flows.zhipu.poll(context.actor.subject_id, flow_id, flow_secret)
        return poll

    def _post_save_zhipu_effects(self, account_id, account, *, usage=None):
        """The complete API/TG continuation; optional IO never rolls back login."""
        account = self._account(account_id)  # Use the persisted generation, not the import/login candidate.
        effects = {"account_id": account_id, "initialization_pending": False,
                   "model_sync_future": None, "model_selection_before": None, "model_sync_error": None,
                   "usage": None, "usage_error": None, "quota_action": None,
                   "enrichment_error": None, "key_action": None, "reset": None, "reset_error": None}
        if (account.get("credential_mode") == "oauth" and not account.get("model_key")
                and account.get("plan_scope") == "personal"
                and not account.get("organization_id") and not account.get("project_id")):
            try:
                choices = self.backend.zhipu_project_choices(account, account_id)
                choice = self.backend.zhipu_default_personal_project(choices)
                entry = self.backend.zhipu_select_project(copy.deepcopy(account), choice)
                account_id, _ = self.backend.zhipu_bind_project(account_id, account, entry)
                effects["account_id"] = account_id
            except Exception as exc:
                effects["enrichment_error"] = exc
        account = self._account(account_id)
        if (account.get("credential_mode") == "oauth" and not account.get("model_key")
                and account.get("organization_id") and account.get("project_id")):
            try:
                self.backend.zhipu_enrich_account(account_id)
            except Exception as exc:
                effects["enrichment_error"] = exc
            account = self._account(account_id)  # Keep successful Key/subscription changes even after a partial failure.
            if (not account.get("model_key") and
                    getattr(effects.get("enrichment_error"), "kind", "") == "creation_confirmation_required"):
                # Login/explicit initialization owns creation of its dedicated
                # Key. Reuse durable action reconciliation, never a naked POST.
                try:
                    observed = copy.deepcopy(account)
                    plan = self.backend.zhipu_inspect_action(observed, account_id, "create_key")
                    outcome = self.backend.zhipu_execute_action(account_id, observed, plan,
                        actor="automatic:key-initialization")
                    effects["key_action"] = outcome
                    effects["enrichment_error"] = None
                except Exception as exc:
                    effects["enrichment_error"] = exc
                account = self._account(account_id)
        if account.get("model_key"):
            effects.update(self._start_post_save_model_sync(account_id))
            try:
                if usage is None:
                    result = self._refresh_usage_account(account_id, tolerate_evaluation_error=True)
                else:
                    saved, quota_action = self._save_and_evaluate_usage(account_id, usage, tolerate_evaluation_error=True)
                    result = {"usage": saved, "quota_action": quota_action}
                effects.update(usage=result.get("usage"), quota_action=result.get("quota_action"))
            except Exception as exc:
                effects["usage_error"] = exc
        else:
            effects["initialization_pending"] = True
        # Cards use OAuth credentials, not the model Key. Query them even when
        # project/Key/quota discovery failed, and preserve all other results.
        account = self._account(account_id)
        if account.get("credential_mode") == "oauth":
            try:
                effects["reset"] = self.backend.zhipu_reset_status(account, account_id)
            except Exception as exc:
                effects["reset_error"] = exc
        return effects

    def initialize_zhipu_account(self, context, account_id):
        """Resume setup without reauthorization; reconcile/create only its dedicated Key."""
        self._require(context, Capability.SECRETS_WRITE)
        account = self._zhipu_account(account_id)
        effects = self._post_save_zhipu_effects(account_id, account)
        account_id = effects["account_id"]
        self._audit(context, "oauth.zhipu.initialize", account_id)
        return OAuthMutationResult(account_id, revision(self._account(account_id)), "updated", post_save=effects)

    def _zhipu_initialization_summary(self, effects):
        """Serializable completion facts for confirmed actions in both adapters."""
        errors = {}
        future = effects.get("model_sync_future")
        if future is not None:
            try:
                model_result = future.result()
                if (model_result.get("metadata_sync") or {}).get("status") == "failed":
                    errors["metadata"] = "sync_failed"
            except Exception:
                errors["models"] = "sync_failed"
        for field in ("enrichment_error", "model_sync_error", "usage_error", "reset_error"):
            if effects.get(field) is not None:
                errors[field] = getattr(effects[field], "kind", "failed")
        key_action = effects.get("key_action")
        if key_action and key_action.get("status") != "succeeded":
            errors["key"] = key_action.get("status", "failed")
        account = self._account(effects["account_id"])
        return {"account_id": effects["account_id"], "model_key_configured": bool(account.get("model_key")),
                "models": len(self.backend.account_model_selection(effects["account_id"])["effective_models"]),
                "usage_ready": effects.get("usage") is not None, "reset_ready": effects.get("reset") is not None,
                "errors": errors}

    def start_zhipu_project_selection(self, context, account_id):
        self._require(context, Capability.SECRETS_WRITE)
        account = self._zhipu_account(account_id, oauth=True)
        return self._flows.zhipu.projects(context.actor.subject_id, account_id, account)

    def select_zhipu_project(self, context, flow_id, flow_secret, *, organization_id, project_id):
        self._require(context, Capability.SECRETS_WRITE)
        self._flows.zhipu.select(context.actor.subject_id, flow_id, flow_secret, organization_id, project_id)
        return self.complete_login_flow(context, flow_id, flow_secret, CompleteOAuthLoginCommand(completed=True))

    def zhipu_snapshot(self, account_id):
        return self.backend.zhipu_snapshot(account_id)

    def get_zhipu(self, context, account_id, *, reset_status=False):
        self._require(context, Capability.READ)
        account = self._zhipu_account(account_id, oauth=reset_status)
        value = {"snapshot": self.backend.zhipu_snapshot(account_id), "actions": self.backend.zhipu_action_history(account)}
        if reset_status:
            value["reset"] = self.backend.zhipu_reset_status(account, account_id)
        return value

    def plan_zhipu_action(self, context, account_id, action, *, reset_type=None, card_index=0):
        self._require(context, Capability.WRITE)
        if action == "create_key":
            self._require(context, Capability.SECRETS_WRITE)
        elif action == "use":
            self._require(context, Capability.DESTRUCTIVE)
        account = self._zhipu_account(account_id, oauth=True)
        observation = self.backend.zhipu_inspect_action(account, account_id, action, reset_type=reset_type, card_index=card_index)
        token, plan = self._zhipu_plans.create(actor_subject_id=context.actor.subject_id,
            kind="zhipu-action", revision=revision(account), payload={"account_id": account_id, "account": account, "observation": observation})
        self._audit(context, "oauth.zhipu.plan", account_id)
        return {"plan_token": token, "account_id": account_id, "action": action, "expires_at": plan.expires_at,
                "organization_id": account.get("organization_id"), "project_id": account.get("project_id"),
                "reset_type": reset_type, "card_index": card_index, "expire_at": observation.get("expire_at"),
                "resume_only": observation.get("resume_only", False)}

    def execute_zhipu_action_now(self, context, account_id, plan_token, *, cancellation=None, on_stage=None):
        self._require(context, Capability.WRITE)
        plan = self._zhipu_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id, kind="zhipu-action")
        payload = plan.payload
        if payload["observation"]["action"] == "create_key":
            self._require(context, Capability.SECRETS_WRITE)
        elif payload["observation"]["action"] == "use":
            self._require(context, Capability.DESTRUCTIVE)
        if payload["account_id"] != account_id:
            raise ManagementError(Code.INVALID_OPERATION_STATE)
        account = self._zhipu_account(account_id, oauth=True)
        if revision(account) != plan.revision:
            raise ManagementError(Code.REVISION_CONFLICT)
        self._zhipu_plans.consume(plan_token, actor_subject_id=context.actor.subject_id, kind="zhipu-action")
        options = {}
        if cancellation is not None:
            options["cancellation"] = cancellation
        if on_stage is not None:
            options["on_stage"] = on_stage
        result = self.backend.zhipu_execute_action(account_id, account, payload["observation"], actor=context.actor.subject_id, **options)
        if result.get("action") == "create_key" and result.get("status") == "succeeded":
            try:
                effects = self._post_save_zhipu_effects(account_id, self._account(account_id))
                result["initialization"] = self._zhipu_initialization_summary(effects)
            except Exception:
                # The Key operation has already succeeded. Never report it as a
                # failed create or lose its durable result due to later reads.
                result["initialization"] = {"errors": {"continuation": "failed"}}
        self._audit(context, "oauth.zhipu." + payload["observation"]["action"], account_id, result.get("status", "unknown"))
        return result
