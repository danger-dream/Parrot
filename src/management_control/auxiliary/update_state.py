"""Resumable update state for browser clients (WebUI round 2).

A stage plan token is returned once by ``stageUpdate`` and lives only in the
page that staged it. After a reload, another browser, a Telegram stage or an
automatic stage there is no token, so :meth:`issue_activation_plan` mints a
fresh actor-bound plan for the *currently staged* target. It requires the same
``management.update`` capability as staging plus ``If-Match`` on the current
state revision, and it retires any previously issued plan, so a leaked old
token can never activate.
"""

from __future__ import annotations

from typing import Any

from src.management_auth.principal import Capability

from ..context import ManagementContext
from ..errors import ManagementError, ManagementErrorCode
from .common import audit, ensure_revision, invalid_field, require, rfc3339_utc

STAGED = "staged"
_MAX_PLANS = 64


class UpdateStateMixin:
    def state_view(self, context: ManagementContext) -> dict[str, Any]:
        """Stage/target/revision/mode/repository in one read (no network)."""
        state = self.state(context)
        return {
            "stage": state.stage,
            "mode": state.mode,
            "from_version": state.from_version,
            "to_version": state.to_version,
            "target_version": state.target_version,
            "message": state.message,
            "repository": self.repository(context),
            "current_version": self.current_version,
            "busy": bool(self._updates.busy()),
            "activation_available": state.stage == STAGED and bool(state.target_version),
            "revision": state.revision,
        }

    def _prune_plans(self) -> None:
        now = self._clock()
        for digest, plan in list(self._plans.items()):
            if len(self._plans) <= _MAX_PLANS:
                break
            if digest != self._active_plan_digest and (plan.consumed or plan.expires_at <= now):
                self._plans.pop(digest, None)

    def issue_activation_plan(self, context: ManagementContext, *, expected_revision: str | None) -> dict[str, Any]:
        require(context, Capability.UPDATE)
        if not expected_revision:
            raise invalid_field("If-Match", "REQUIRED", "If-Match header is required")
        from .updates import _actor_key  # local: avoid an import cycle
        with self._lock:
            state = self._state_without_auth()
            if state.stage != STAGED or not state.target_version:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            ensure_revision(expected_revision, state.revision)
            token, digest = self._new_plan(actor_key=_actor_key(context), target_version=str(state.target_version))
            plan = self._plans[digest]
            plan.revision = state.revision
            plan.client_revision = state.revision
            plan.ready = True
            self._retire_plan(self._active_plan_digest)
            self._active_plan_digest = digest
            self._prune_plans()
            expires_at = rfc3339_utc(plan.expires_at)
        audit(self._audit_sink, context, action="updates.activation-plan", target=str(state.target_version))
        return {"plan_token": token, "target_version": state.target_version,
                "revision": state.revision, "expires_at": expires_at}
