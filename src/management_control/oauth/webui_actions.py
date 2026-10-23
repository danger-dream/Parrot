"""OAuth actions Telegram already offers, exposed with Management contracts.

* Claude official quota reset (周额度重置卡 ``cedar_ember`` / 5h 重置
  ``juniper_tide``): plan (read-only explanation) → execute. The execute step
  performs Telegram's 「最终确认」 and the consuming POST in one call; the
  plan is one-shot and bound to the account revision.
* OpenAI reset cards: cached or freshly fetched card list with ``creditId``.
"""
from __future__ import annotations

import asyncio
import json

from src.management_auth.principal import Capability
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .upstream import mapped_upstream


CLAUDE_RESET_PROGRAMS = ("cedar_ember", "juniper_tide")


def _card(item: dict) -> dict:
    return {
        "credit_id": str(item.get("id") or "").strip(),
        "status": str(item.get("status") or "").strip() or None,
        "reset_type": str(item.get("reset_type") or "").strip() or None,
        "granted_at": item.get("granted_at") or None,
        "expires_at": item.get("expires_at") or None,
    }


def reset_credit_view(details: dict | None, *, source: str, cached_count=None) -> dict:
    details = details if isinstance(details, dict) else {}
    data = details.get("data") if isinstance(details.get("data"), list) else []
    cards = [_card(item) for item in data if isinstance(item, dict) and str(item.get("id") or "").strip()]
    count = details.get("available_count", cached_count)
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None
    return {"available_count": count, "cards": cards, "source": source,
            "fetched_at": details.get("fetched_at") or None}


class OAuthWebUiActionsMixin:
    # ── OpenAI reset cards ──────────────────────────────────────────
    def list_openai_reset_credits(self, context, account_id: str, *, refresh: bool = False) -> dict:
        self._require(context, Capability.WRITE if refresh else Capability.READ)
        account = self._account(account_id)
        if self.backend.provider_of(account) != "openai":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        row = self.backend.quota_load(account_id) or {}
        if refresh:
            with mapped_upstream():
                details = asyncio.run(self.backend.fetch_openai_reset_credits(account_id))
            return reset_credit_view(details, source="live")
        try:
            usage = json.loads(row.get("raw_data") or "{}")
        except (TypeError, ValueError):
            usage = {}
        usage = usage if isinstance(usage, dict) else {}
        openai = usage.get("openai") if isinstance(usage.get("openai"), dict) else {}
        details = openai.get("rate_limit_reset_credit_details") or usage.get("rate_limit_reset_credit_details")
        return reset_credit_view(details, source="cache", cached_count=row.get("openai_reset_credit_count"))

    # ── Claude official reset ───────────────────────────────────────
    @staticmethod
    def _claude_program(program: str) -> str:
        if program not in CLAUDE_RESET_PROGRAMS:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED, fields=[
                ErrorField("program", "UNSUPPORTED", "program must be cedar_ember or juniper_tide")])
        return program

    def plan_claude_reset_action(self, context, account_id: str, program: str) -> dict:
        """Read-only status + eligibility; returns a one-shot plan when executable."""
        self._claude_program(program)
        with mapped_upstream():
            plan = self.plan_claude_reset(context, account_id, program)
        observation = plan.get("status") if isinstance(plan.get("status"), dict) else {}
        grant_id = ((observation.get("cedar_ember") or {}).get("next_grant_id")
                    if program == "cedar_ember" else None)
        return {
            "available": bool(plan.get("available")),
            "reason": plan.get("reason"),
            "program": program,
            "plan_token": plan.get("plan_token"),
            "expires_at": plan.get("expires_at"),
            "grant_id": grant_id if plan.get("available") else None,
            "status": observation,
        }

    def execute_claude_reset_action(self, context, account_id: str, plan_token: str) -> dict:
        """Final confirmation + consuming POST. Never retried automatically."""
        final = self.confirm_claude_reset(context, account_id, plan_token)
        with mapped_upstream():
            result = self.execute_claude_reset(context, account_id, final["plan_token"])
        return {"program": final["program"], "grant_id": final.get("grant_id"),
                **(result if isinstance(result, dict) else {})}
