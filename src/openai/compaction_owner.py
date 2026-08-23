"""Durable ownership and lossless routing rules for Codex compaction items."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .. import state_db

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
    """Canonical account boundary: workspace when known, auth account key otherwise.

    A refresh-derived workspace is copied onto the live channel before a request is
    dispatched.  Workspace-only canonical identity therefore survives the old
    channel key, registry rebuilds and canonical key migration, while retaining
    strict workspace isolation.
    """
    if not is_openai_oauth_channel(ch):
        return ""
    workspace = _workspace(ch)
    boundary = f"workspace:{workspace}" if workspace else f"account:{getattr(ch, 'account_key', '')}"
    return _digest_identity({"provider": "openai", "boundary": boundary})


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


def persist_observed(ch: Any, *values: Any) -> int:
    """Persist complete compactions only after this OAuth owner succeeded."""
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
    aliases = identity_aliases(ch)
    for ref in refs:
        state_db.compaction_owner_upsert(
            ref.compaction_id, ref.content_digest, str(ch.key), identity,
            compatible_identities=aliases,
        )
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
        # live channel proves the alias. This preserves existing state.db rows.
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
