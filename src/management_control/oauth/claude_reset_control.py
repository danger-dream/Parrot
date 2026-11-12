"""Claude reset confirmations; identical contract for every management adapter."""
from __future__ import annotations

import asyncio
import re
import secrets

from src.management_auth.principal import Capability
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from .contracts import revision


class ClaudeResetControlMixin:
    def _claude_reset_account(self, account_id):
        account = self._account(account_id)
        if self.backend.provider_of(account) != "claude":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        return account

    def _claude_reset_revision(self, account) -> str:
        org = account.get("claude_organization_uuid") or account.get("organization_id") or ""
        return revision({
            "id": account.get("_id"),
            "provider": self.backend.provider_of(account),
            "email": account.get("email"),
            "subject": account.get("subject") or account.get("sub"),
            "organization_uuid": str(org),
            "generation_id": account.get("generationId"),
        })

    def refresh_claude_reset_status(self, context, account_id, program="cedar_ember"):
        self._require(context, Capability.WRITE)
        self._claude_reset_account(account_id)
        return asyncio.run(self.backend.claude_reset_status(account_id, program))

    def plan_claude_reset(self, context, account_id, program, grant_id: str | None = None):
        """Explanation stage: READ upstream only, bind identity and selected grant."""
        self._require(context, Capability.DESTRUCTIVE)
        self._claude_reset_account(account_id)
        if grant_id is not None:
            if program != "cedar_ember":
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=[
                    ErrorField("grantId", "UNSUPPORTED", "grantId is only supported for cedar_ember")])
            if not isinstance(grant_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", grant_id):
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=[
                    ErrorField("grantId", "INVALID_FORMAT", "grantId must match ^[a-z0-9_-]{1,40}$")])
        observation = asyncio.run(self.backend.claude_reset_status(account_id, program))
        selected_grant = None
        if program == "cedar_ember":
            selected_grant, reason = self.backend.claude_reset_select_grant(observation, grant_id)
            if reason is not None and grant_id is None:
                fallback_reason = self.backend.claude_reset_eligibility(observation, program)
                if fallback_reason is None:
                    cedar_obs = observation.get("cedar_ember") if isinstance(observation.get("cedar_ember"), dict) else {}
                    next_id = cedar_obs.get("next_grant_id")
                    selected_grant = {"id": next_id} if isinstance(next_id, str) and next_id else None
                    reason = None if selected_grant is not None else "no_grant"
                else:
                    reason = fallback_reason
        else:
            reason = self.backend.claude_reset_eligibility(observation, program)
        if reason or not observation.get("organization_uuid"):
            return {"available": False, "reason": reason or "organization_missing",
                    "grant_id": grant_id, "status": observation, "program": program}
        from src.oauth import claude_reset as cr
        selected_grant_id = selected_grant.get("id") if isinstance(selected_grant, dict) else None
        cedar_block = observation.get("cedar_ember") if isinstance(observation.get("cedar_ember"), dict) else {}
        cedar_grants = [g for g in (cedar_block.get("grants") or []) if isinstance(g, dict)]
        new_windows = {"seven_day_overage_included", "seven_day_cowork", "seven_day_omelette"}
        v321_card = bool(
            program == "cedar_ember"
            and (
                grant_id is not None
                or len(cedar_grants) > 1
                or selected_grant_id != cedar_block.get("next_grant_id")
                or any(
                    g.get("type") is not None
                    or g.get("gives_back_weekly") is not None
                    or g.get("unavailable_reason") is not None
                    for g in cedar_grants
                )
                or any(w in new_windows for w in (cedar_block.get("exhausted") or []))
                or any(
                    w in new_windows
                    for g in cedar_grants
                    for w in list(g.get("clears") or []) + list(g.get("blocking") or [])
                )
            )
        )
        token, plan = self._claude_reset_plans.create(
            actor_subject_id=context.actor.subject_id, kind="claude-reset-explain",
            revision=self._claude_reset_revision(self._account(account_id)), payload={
                "account_id": account_id, "program": program,
                "expected": observation["generation"],
                "organization_uuid": observation["organization_uuid"],
                "grant_id": selected_grant_id,
                "grant_label": selected_grant.get("label") if isinstance(selected_grant, dict) else None,
                "grant_kind": cr.grant_kind(selected_grant) if isinstance(selected_grant, dict) else None,
                "gives_back_weekly": bool(selected_grant.get("gives_back_weekly")) if isinstance(selected_grant, dict) else False,
                "v321_card": v321_card,
                "operation_id": secrets.token_urlsafe(24),
            })
        self._audit(context, "oauth.claude-reset.plan", account_id)
        return {"available": True, "plan_token": token, "expires_at": plan.expires_at,
                "grant_id": selected_grant_id, "status": observation, "program": program}

    def confirm_claude_reset(self, context, account_id, plan_token):
        self._require(context, Capability.DESTRUCTIVE)
        account = self._claude_reset_account(account_id)
        plan = self._claude_reset_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id,
                                               kind="claude-reset-explain")
        if plan.payload["account_id"] != account_id or self._claude_reset_revision(account) != plan.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        self._claude_reset_plans.consume(plan_token, actor_subject_id=context.actor.subject_id,
                                         kind="claude-reset-explain")
        token, final = self._claude_reset_plans.create(actor_subject_id=context.actor.subject_id,
            kind="claude-reset-execute", revision=plan.revision, payload=plan.payload)
        return {"plan_token": token, "program": plan.payload["program"],
                "grant_id": plan.payload["grant_id"],
                "grant_label": plan.payload.get("grant_label"),
                "grant_kind": plan.payload.get("grant_kind"),
                "gives_back_weekly": bool(plan.payload.get("gives_back_weekly")),
                "v321_card": bool(plan.payload.get("v321_card")),
                "expires_at": final.expires_at}

    def execute_claude_reset(self, context, account_id, plan_token):
        self._require(context, Capability.DESTRUCTIVE)
        account = self._claude_reset_account(account_id)
        plan = self._claude_reset_plans.inspect(plan_token, actor_subject_id=context.actor.subject_id,
                                               kind="claude-reset-execute")
        if plan.payload["account_id"] != account_id or self._claude_reset_revision(account) != plan.revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        # Consumed BEFORE asynchronous work. Double callbacks, including after
        # transport timeouts, cannot replay this capability.
        self._claude_reset_plans.consume(plan_token, actor_subject_id=context.actor.subject_id,
                                         kind="claude-reset-execute")
        payload = dict(plan.payload)
        payload.pop("account_id")
        program = payload.pop("program")
        for extra in ("grant_label", "grant_kind", "gives_back_weekly", "v321_card"):
            payload.pop(extra, None)
        try:
            result = asyncio.run(self.backend.claude_reset_redeem(account_id, program, **payload))
        except Exception:
            self._audit(context, "oauth.claude-reset.execute", account_id, "failed")
            raise
        self._audit(context, "oauth.claude-reset.execute", account_id)
        return result
