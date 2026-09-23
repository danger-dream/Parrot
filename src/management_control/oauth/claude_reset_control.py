"""Claude reset confirmations; identical contract for every management adapter."""
from __future__ import annotations

import asyncio
import secrets

from src.management_auth.principal import Capability
from src.management_control.errors import ManagementError, ManagementErrorCode
from .contracts import revision


class ClaudeResetControlMixin:
    def _claude_reset_account(self, account_id):
        account = self._account(account_id)
        if self.backend.provider_of(account) != "claude":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        return account

    def refresh_claude_reset_status(self, context, account_id, program="cedar_ember"):
        self._require(context, Capability.WRITE)
        self._claude_reset_account(account_id)
        return asyncio.run(self.backend.claude_reset_status(account_id, program))

    def plan_claude_reset(self, context, account_id, program):
        """Explanation stage: READ upstream only, bind identity and selected grant."""
        self._require(context, Capability.DESTRUCTIVE)
        self._claude_reset_account(account_id)
        observation = asyncio.run(self.backend.claude_reset_status(account_id, program))
        reason = self.backend.claude_reset_eligibility(observation, program)
        if reason or not observation.get("organization_uuid"):
            return {"available": False, "reason": reason or "organization_missing", "status": observation}
        token, plan = self._claude_reset_plans.create(
            actor_subject_id=context.actor.subject_id, kind="claude-reset-explain",
            revision=revision(self._account(account_id)), payload={
                "account_id": account_id, "program": program,
                "expected": observation["generation"],
                "organization_uuid": observation["organization_uuid"],
                "grant_id": (observation.get("cedar_ember") or {}).get("next_grant_id") if program == "cedar_ember" else None,
                "operation_id": secrets.token_urlsafe(24),
            })
        self._audit(context, "oauth.claude-reset.plan", account_id)
        return {"available": True, "plan_token": token, "expires_at": plan.expires_at,
                "status": observation, "program": program}

    def confirm_claude_reset(self, context, account_id, plan_token):
        self._require(context, Capability.DESTRUCTIVE)
        account = self._claude_reset_account(account_id)
        plan = self._claude_reset_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id,
                                               kind="claude-reset-explain")
        if plan.payload["account_id"] != account_id or revision(account) != plan.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self._claude_reset_plans.consume(plan_token, actor_subject_id=context.actor.subject_id,
                                         kind="claude-reset-explain")
        token, final = self._claude_reset_plans.create(actor_subject_id=context.actor.subject_id,
            kind="claude-reset-execute", revision=plan.revision, payload=plan.payload)
        return {"plan_token": token, "program": plan.payload["program"],
                "grant_id": plan.payload["grant_id"], "expires_at": final.expires_at}

    def execute_claude_reset(self, context, account_id, plan_token):
        self._require(context, Capability.DESTRUCTIVE)
        account = self._claude_reset_account(account_id)
        plan = self._claude_reset_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id,
                                               kind="claude-reset-execute")
        if plan.payload["account_id"] != account_id or revision(account) != plan.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        # Consumed BEFORE asynchronous work. Double callbacks, including after
        # transport timeouts, cannot replay this capability.
        self._claude_reset_plans.consume(plan_token, actor_subject_id=context.actor.subject_id,
                                         kind="claude-reset-execute")
        payload = dict(plan.payload)
        payload.pop("account_id")
        program = payload.pop("program")
        try:
            result = asyncio.run(self.backend.claude_reset_redeem(account_id, program, **payload))
        except Exception:
            self._audit(context, "oauth.claude-reset.execute", account_id, "failed")
            raise
        self._audit(context, "oauth.claude-reset.execute", account_id)
        return result
