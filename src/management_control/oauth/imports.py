"""OAuth import preview, one-shot decisions, and post-publish effects."""

from __future__ import annotations

import copy
from collections.abc import Iterable

from src.management_auth.principal import Capability
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.oauth.openai_import import OpenAIImportCandidate as ParsedImportCandidate

from .contracts import audit_failures, revision, sanitize_text
from .models import (
    OAuthImportCandidate,
    OAuthImportCommitResult,
    OAuthImportDecision,
    OAuthImportPreview,
    OAuthImportProblem,
    OAuthProvider,
    RefreshTokenCredential,
)


class OAuthImportControlMixin:
    """Prepare verified credentials before placing them in an import plan."""

    @staticmethod
    def _unprepared_candidate(value) -> tuple[str, str] | None:
        if isinstance(value, ParsedImportCandidate):
            return value.email, value.refresh_token
        if isinstance(value, dict):
            email = str(value.get("email") or "").strip()
            refresh_token = str(value.get("refresh_token") or "").strip()
            if refresh_token:
                return email, refresh_token
        return None

    def _prepare_import_entry(self, value) -> dict:
        if isinstance(value, dict) and value.get("provider") == "zhipu":
            return self.backend.zhipu_prepare_credential(value)
        # Parsed material is never trusted as a complete account. Every candidate
        # must pass through the native refresh/token/identity conversion first.
        parts = self._unprepared_candidate(value)
        if parts is None:
            raise ValueError("import candidate has no refresh token")
        email, refresh_token = parts
        entry = self._flows.credential_entry(
            RefreshTokenCredential(
                provider=OAuthProvider.OPENAI,
                refresh_token=refresh_token,
                email_hint=email or None,
            )
        )
        if self.backend.provider_of(entry) != OAuthProvider.OPENAI.value:
            raise ValueError("import candidate is not an OpenAI account")
        if not (entry.get("workspace_id") or entry.get("chatgpt_account_id")):
            raise ValueError("OpenAI token has no canonical workspace identity")
        return entry

    @audit_failures("oauth.import.preview", target="oauthImport")
    def preview_import(
        self, context, *, format: str, payload: str | bytes, filename: str = "",
    ) -> OAuthImportPreview:
        self._require(context, Capability.SECRETS_WRITE)
        if format not in {"openai", "cpa", "sub2api", "zhipu"}:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)

        problems: list[OAuthImportProblem] = []
        try:
            parsed = self.backend.parse_import(format, payload, filename=filename)
        except Exception as exc:
            parsed = []
            problems.append(
                OAuthImportProblem(
                    index=None,
                    code="PARSE_FAILED",
                    message=type(exc).__name__,
                )
            )

        # Take the commit snapshot before supplier validation. Any account write
        # that could make the subsequent live conflict summary stale then fails CAS.
        expected_accounts = copy.deepcopy(self.backend.list_accounts())
        candidates: list[OAuthImportCandidate] = []
        safe_entries: list[dict] = []
        seen_identities: set[str] = set()
        for index, raw_candidate in enumerate(parsed):
            try:
                entry = self._prepare_import_entry(raw_candidate)
                identity = self.backend.account_id(entry)
                if identity in seen_identities:
                    problems.append(
                        OAuthImportProblem(
                            index=index,
                            code="DUPLICATE_IDENTITY",
                            message="Candidate resolves to a repeated identity",
                        )
                    )
                    continue
                seen_identities.add(identity)
                existing = self.backend.find_exact_identity(entry)
                provider = OAuthProvider(self.backend.provider_of(entry))
            except Exception as exc:
                problems.append(
                    OAuthImportProblem(
                        index=index,
                        code="INVALID_CANDIDATE",
                        message=type(exc).__name__,
                    )
                )
                continue
            candidate_id = f"candidate-{index + 1}"
            safe_entries.append(
                {"candidate_id": candidate_id, "entry": copy.deepcopy(entry)}
            )
            candidates.append(
                OAuthImportCandidate(
                    candidate_id=candidate_id,
                    provider=provider,
                    identity=sanitize_text(identity),
                    display_name=sanitize_text(
                        entry.get("label") or entry.get("email") or identity
                    ),
                    conflict_account_id=existing[0] if existing else None,
                )
            )

        import_id, import_secret, plan = self._import_plans.create_split(
            actor_subject_id=context.actor.subject_id,
            kind="import",
            revision=revision(expected_accounts),
            payload={
                "candidates": tuple(safe_entries),
                "expected_accounts": expected_accounts,
            },
        )
        return OAuthImportPreview(
            import_id=import_id,
            import_secret=import_secret,
            candidates=tuple(candidates),
            errors=tuple(problems),
            expires_at=plan.expires_at,
        )

    @audit_failures("oauth.import.commit", target="oauthImport")
    def commit_import(
        self,
        context,
        import_id: str,
        import_secret: str,
        decisions: Iterable[OAuthImportDecision],
    ) -> OAuthImportCommitResult:
        self._require(context, Capability.SECRETS_WRITE)
        plan = self._import_plans.inspect_parts(
            import_id,
            import_secret,
            actor_subject_id=context.actor.subject_id,
            kind="import",
        )
        candidates = tuple(plan.payload["candidates"])
        choices = {decision.candidate_id: decision.action for decision in decisions}
        valid_ids = {item["candidate_id"] for item in candidates}
        if set(choices) != valid_ids or any(
            action not in {"keep", "overwrite"} for action in choices.values()
        ):
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        for item in candidates:
            self._ensure_legacy_identity_safe(item["entry"])

        self._import_plans.consume_parts(
            import_id,
            import_secret,
            actor_subject_id=context.actor.subject_id,
            kind="import",
        )
        outcome = self.backend.commit_import_conditional(
            copy.deepcopy(plan.payload["expected_accounts"]), candidates, choices,
        )
        if outcome.get("status") == "revision_conflict":
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        if outcome.get("status") != "committed":
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)

        added = tuple(str(item) for item in outcome.get("added") or ())
        replaced = tuple(str(item) for item in outcome.get("replaced") or ())
        skipped = tuple(str(item) for item in outcome.get("skipped") or ())
        affected = set(added) | set(replaced)
        completed: set[str] = set()
        identities: dict[str, str] = {}
        for item in candidates:
            entry = item["entry"]
            account_id = self.backend.account_id(entry)
            if account_id in affected and account_id not in completed:
                # Credentials are already atomically published. Follow-up failures
                # remain observations and never roll back the committed batch.
                effects = self._post_save_account_effects(account_id, entry)
                identities[account_id] = effects.get("account_id", account_id)
                completed.add(account_id)
        self._audit(context, "oauth.import.commit", import_id)
        return OAuthImportCommitResult(tuple(identities.get(key, key) for key in added),
            tuple(identities.get(key, key) for key in replaced), skipped)
