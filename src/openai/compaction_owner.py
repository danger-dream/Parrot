"""Durable ownership and lossless routing rules for Codex compaction items."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .. import state_db
from .codex_identity import (
    LogicalSession,
    RequestIdentityContext,
    owner_digest_for_workspace,
    uuid7,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactionRef:
    compaction_id: str
    content_digest: str


class CompactionRouteError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _walk(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        if value.get("type") == "compaction":
            yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def complete_refs(value: Any) -> list[CompactionRef]:
    refs: list[CompactionRef] = []
    seen: set[tuple[str, str]] = set()
    for item in _walk(value):
        compaction_id = item.get("id")
        encrypted = item.get("encrypted_content")
        if not isinstance(compaction_id, str) or not compaction_id:
            continue
        if not isinstance(encrypted, str) or not encrypted:
            continue
        digest = hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
        key = (compaction_id, digest)
        if key not in seen:
            refs.append(CompactionRef(*key))
            seen.add(key)
    return refs


def has_complete_compaction(value: Any) -> bool:
    return bool(complete_refs(value))


def is_openai_oauth_channel(ch: Any) -> bool:
    return (
        getattr(ch, "type", "") == "oauth"
        and getattr(ch, "protocol", "") == "openai-responses"
        and str(getattr(ch, "account_key", "")).startswith("openai:")
    )


def _digest_identity(payload: dict[str, str]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _workspace(ch: Any) -> str:
    return str(getattr(ch, "workspace_id", "") or getattr(ch, "chatgpt_account_id", ""))


def owner_identity(ch: Any) -> str:
    """Return the canonical OAuth owner digest; unknown workspaces have no owner."""
    if not is_openai_oauth_channel(ch):
        return ""
    workspace = _workspace(ch)
    return owner_digest_for_workspace(workspace) if workspace else ""


def _legacy_identities(ch: Any) -> set[str]:
    """Identities emitted by the initial owner algorithm, for in-place adoption."""
    if not is_openai_oauth_channel(ch):
        return set()
    account_key = str(getattr(ch, "account_key", ""))
    workspace = _workspace(ch)
    keys = {account_key}
    # A pre-workspace channel used openai:<email>; after refresh its live key is
    # deliberately unchanged.  Rebuilt channels use openai:<email>:<workspace>.
    if workspace and account_key.endswith(f":{workspace}"):
        keys.add(account_key[:-(len(workspace) + 1)])
    return {
        _digest_identity({"provider": "openai", "account_key": key, "workspace": ws})
        for key in keys for ws in ({workspace, ""} if workspace else {""})
    }


def identity_aliases(ch: Any) -> set[str]:
    identity = owner_identity(ch)
    return ({identity} if identity else set()) | _legacy_identities(ch)


def _request_scope(identity: str, values: tuple[Any, ...]) -> tuple[str, str]:
    model = ""
    logical_session_id = ""
    for value in values:
        if not isinstance(value, dict):
            continue
        model = model or str(value.get("model") or "").strip()
        contexts = value.get("_codex_identity_contexts")
        context = contexts.get(identity) if isinstance(contexts, dict) else None
        if isinstance(context, RequestIdentityContext):
            logical_session_id = context.logical_session.session_id
    return model, logical_session_id


def _request_context(
    identity: str, values: tuple[Any, ...]
) -> tuple[int, dict[str, Any], RequestIdentityContext] | None:
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        contexts = value.get("_codex_identity_contexts")
        context = contexts.get(identity) if isinstance(contexts, dict) else None
        if isinstance(context, RequestIdentityContext):
            return index, value, context
    return None


def advance_confirmed_compaction_window(
    ch: Any,
    values: tuple[Any, ...],
    response_refs: list[CompactionRef],
) -> bool:
    """Advance only for a successful response scoped to this owner/session/model.

    Callers invoke this from their already-established response success boundary.
    The durable transaction makes duplicate/concurrent processing idempotent.
    """
    identity = owner_identity(ch)
    scoped = _request_context(identity, values)
    if not identity or scoped is None or not response_refs:
        return False
    request_index, request, context = scoped
    logical = context.logical_session
    if not logical.durable or logical.owner_digest != identity:
        return False
    model = str(request.get("model") or "").strip()
    if not model:
        return False
    for response in values[request_index + 1:]:
        if not isinstance(response, dict):
            continue
        response_model = str(response.get("model") or "").strip()
        if response_model and response_model != model:
            return False
    result = state_db.codex_compaction_confirm_and_advance_window(
        logical.owner_digest,
        logical.downstream_principal_digest,
        logical.downstream_anchor_digest,
        expected_window_number=logical.window_number,
        context_window_id=uuid7(),
        model=model,
        logical_session_id=logical.session_id,
        refs=[(ref.compaction_id, ref.content_digest) for ref in response_refs],
    )
    row = result.get("logical_session") if isinstance(result, dict) else None
    if isinstance(row, dict):
        context.logical_session = LogicalSession.from_row(row, durable=True)
    return bool(isinstance(result, dict) and result.get("advanced"))


def persist_observed(ch: Any, *values: Any) -> int:
    """Persist compaction ownership and close a confirmed response window."""
    identity = owner_identity(ch)
    if not identity:
        return 0
    refs: list[CompactionRef] = []
    seen: set[CompactionRef] = set()
    for value in values:
        for ref in complete_refs(value):
            if ref not in seen:
                refs.append(ref)
                seen.add(ref)
    scoped = _request_context(identity, values)
    response_refs: list[CompactionRef] = []
    if scoped is not None:
        request_index = scoped[0]
        response_seen: set[CompactionRef] = set()
        for value in values[request_index + 1:]:
            for ref in complete_refs(value):
                if ref not in response_seen:
                    response_refs.append(ref)
                    response_seen.add(ref)
    model, logical_session_id = _request_scope(identity, values)
    aliases = identity_aliases(ch)
    for ref in refs:
        state_db.compaction_owner_upsert(
            ref.compaction_id, ref.content_digest, str(ch.key), identity,
            compatible_identities=aliases, model=model or None,
            logical_session_id=logical_session_id or None,
        )
    advance_confirmed_compaction_window(ch, values, response_refs)
    return len(refs)


def persist_observed_safe(ch: Any, *values: Any, path: str) -> bool:
    """Best-effort success-path persistence with structured observability.

    Owner-state storage is auxiliary to an already successful upstream call: a
    write failure must not turn a valid non-stream response into a client error,
    nor rewrite a partially delivered stream's terminal status.
    """
    try:
        persist_observed(ch, *values)
        return True
    except Exception as exc:
        _log.warning(
            "codex_compaction_owner_persist_failed",
            extra={
                "event": "codex_compaction_owner_persist_failed",
                "path": path,
                "channel_key": str(getattr(ch, "key", "")),
                "error": str(exc),
            },
            exc_info=True,
        )
        return False


def select_owner(refs: list[CompactionRef], channels: Iterable[Any],
                 exact_channel_key: str | None = None,
                 live_channels: Iterable[Any] | None = None) -> Any:
    """Resolve one proven owner or perform the explicitly allowed bootstrap."""
    eligible = [ch for ch in channels if is_openai_oauth_channel(ch)]
    live_oauth = [
        ch for ch in (live_channels if live_channels is not None else eligible)
        if is_openai_oauth_channel(ch)
    ]
    alias_owners: dict[str, list[Any]] = {}
    for ch in live_oauth:
        for alias in identity_aliases(ch):
            alias_owners.setdefault(alias, []).append(ch)

    stored = [state_db.compaction_owner_load(r.compaction_id, r.content_digest) for r in refs]
    known_rows = [row for row in stored if row]
    if known_rows:
        resolved: list[Any] = []
        for row in known_rows:
            owners = alias_owners.get(str(row["owner_identity"]), [])
            if len(owners) != 1:
                raise CompactionRouteError(
                    "compaction_owner_unavailable", "the recorded OpenAI OAuth owner is unavailable",
                )
            resolved.append(owners[0])
        identities = {owner_identity(ch) for ch in resolved}
        if len(identities) > 1:
            raise CompactionRouteError(
                "compaction_owner_conflict", "request compactions belong to different OAuth owners",
            )
        owner = resolved[0]
        # Adopt rows written by the old key+workspace hash only after a unique
        # live channel proves the alias. This preserves existing StateStore snapshots rows.
        for ref, row in zip(refs, stored):
            if row and row["owner_identity"] != owner_identity(owner):
                state_db.compaction_owner_upsert(
                    ref.compaction_id, ref.content_digest, str(owner.key), owner_identity(owner),
                    compatible_identities=identity_aliases(owner),
                )
        return owner

    if exact_channel_key:
        exact = next((ch for ch in live_oauth if ch.key == exact_channel_key), None)
        if exact is not None:
            return exact

    # Bootstrap proof is global identity uniqueness, never eligibility. A known
    # sole owner that is disabled/cooling/model-ineligible is handled by the
    # scheduler as unavailable rather than silently selecting another account.
    live_identities = {owner_identity(ch) for ch in live_oauth}
    if len(live_identities) == 1 and live_oauth:
        return live_oauth[0]
    raise CompactionRouteError(
        "compaction_owner_unknown",
        "cannot prove compaction ownership with multiple OpenAI OAuth owners",
    )
