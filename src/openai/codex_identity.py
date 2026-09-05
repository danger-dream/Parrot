"""Authoritative identity and lifecycle model for OpenAI OAuth Codex requests.

The three scopes in this module are deliberately independent:

* :class:`AccountIdentity` is one long-lived UUIDv4 installation per canonical
  ChatGPT workspace.
* :class:`LogicalSession` is a durable UUIDv7 root thread/window mapping under
  an OAuth owner plus hashed downstream namespace and hashed stable anchor.
* :class:`TurnContext` is request-local.  Retries reuse it, while a new logical
  turn receives a new UUIDv7 and an empty upstream turn-state token.

Only projections of :class:`RequestIdentitySnapshot` may reach Codex transports.
Raw downstream principals, anchors and installation/session identifiers are
never stored in the logical-session registry and are never projected upstream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping

from .. import state_db

OWNER_KIND = "chatgpt-account-id"
SCHEMA_VERSION = 1
ID_GENERATION_VERSION = 1
_ACCOUNT_CONTEXTS_KEY = "_codex_identity_contexts"
_NATIVE_IDENTITY_KEY = "_codex_native_identity"
_TURN_LEASES_KEY = "_codex_turn_serialization_leases"
TURN_MAPPING_TTL_SECONDS = 30 * 60
TURN_MAPPING_MAX_ENTRIES = 4096


class CodexIdentityError(ValueError):
    """Fail-closed identity validation or ownership error."""


@dataclass
class _ThreadLockEntry:
    lock: asyncio.Lock
    users: int = 0


class ThreadTurnLease:
    """One holder/waiter reference in the fine-grained thread turn queue."""

    def __init__(self, key: tuple[str, str], entry: _ThreadLockEntry):
        self._key = key
        self._entry = entry
        self._released = False

    @property
    def key(self) -> tuple[str, str]:
        return self._key

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._entry.lock.release()
        with _THREAD_LOCKS_GUARD:
            self._entry.users -= 1
            if self._entry.users == 0 and not self._entry.lock.locked():
                _THREAD_LOCKS.pop(self._key, None)


_THREAD_LOCKS: dict[tuple[str, str], _ThreadLockEntry] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@dataclass
class _TurnMappingEntry:
    context: "TurnContext"
    touched_at: float


_TURN_MAPPINGS: dict[tuple[str, str, str, str], _TurnMappingEntry] = {}
_TURN_MAPPINGS_GUARD = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uuid7(*, unix_ms: int | None = None) -> str:
    """Return an RFC 9562 UUIDv7 without depending on Python 3.14's uuid.uuid7."""
    timestamp = int(time.time() * 1000) if unix_ms is None else int(unix_ms)
    if timestamp < 0 or timestamp >= (1 << 48):
        raise ValueError("UUIDv7 timestamp must fit 48 bits")
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (
        (timestamp << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return str(uuid.UUID(int=value))


def canonical_uuid(value: Any, *, version: int, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CodexIdentityError(f"{field} must be a canonical UUIDv{version} string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CodexIdentityError(
            f"{field} must be a canonical UUIDv{version} string"
        ) from exc
    if parsed.version != version or str(parsed) != value:
        raise CodexIdentityError(f"{field} must be a canonical UUIDv{version} string")
    return value


def workspace_id_from_account(account: Mapping[str, Any]) -> str:
    return str(
        account.get("workspace_id") or account.get("chatgpt_account_id") or ""
    ).strip()


def owner_digest_for_workspace(workspace_id: str) -> str:
    workspace = str(workspace_id or "").strip()
    if not workspace:
        raise CodexIdentityError(
            "OpenAI OAuth Codex identity requires a canonical ChatGPT workspace/account ID"
        )
    digest = hashlib.sha256(b"openai\0" + workspace.encode("utf-8")).hexdigest()
    return "sha256:" + digest


def scoped_digest(kind: str, value: str) -> str:
    material = str(value or "").encode("utf-8")
    digest = hashlib.sha256(
        b"parrot:codex:" + str(kind).encode("ascii") + b"\0" + material
    ).hexdigest()
    return "sha256:" + digest


@dataclass(frozen=True)
class AccountIdentity:
    schema_version: int
    id_generation_version: int
    protocol_profile: str
    owner_kind: str
    owner_digest: str
    installation_id: str
    created_at: str

    def as_config(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "idGenerationVersion": self.id_generation_version,
            "protocolProfile": self.protocol_profile,
            "ownerKind": self.owner_kind,
            "ownerDigest": self.owner_digest,
            "installationId": self.installation_id,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class LogicalSession:
    owner_digest: str
    downstream_principal_digest: str
    downstream_anchor_digest: str
    session_id: str
    root_thread_id: str
    upstream_prompt_cache_key: str
    window_number: int
    context_window_id: str
    created_at: int
    last_used_at: int
    durable: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, Any], *, durable: bool = True) -> "LogicalSession":
        result = cls(
            owner_digest=str(row.get("owner_digest") or ""),
            downstream_principal_digest=str(
                row.get("downstream_principal_digest") or ""
            ),
            downstream_anchor_digest=str(row.get("downstream_anchor_digest") or ""),
            session_id=canonical_uuid(
                row.get("session_id"), version=7, field="session_id"
            ),
            root_thread_id=canonical_uuid(
                row.get("root_thread_id"), version=7, field="root_thread_id"
            ),
            upstream_prompt_cache_key=str(
                row.get("upstream_prompt_cache_key") or ""
            ),
            window_number=int(row.get("window_number") or 0),
            context_window_id=canonical_uuid(
                row.get("context_window_id"), version=7, field="context_window_id"
            ),
            created_at=int(row.get("created_at") or 0),
            last_used_at=int(row.get("last_used_at") or 0),
            durable=durable,
        )
        if result.root_thread_id != result.session_id:
            raise CodexIdentityError("root logical session requires root_thread_id == session_id")
        if result.upstream_prompt_cache_key != result.session_id:
            raise CodexIdentityError("logical-session prompt_cache_key must equal session_id")
        if result.window_number < 0:
            raise CodexIdentityError("window_number must be nonnegative")
        return result


@dataclass
class TurnContext:
    account_owner_digest: str
    logical_session_key: str
    thread_id: str
    turn_id: str
    turn_started_at_unix_ms: int
    request_kind: str = "turn"
    turn_state: str | None = None

    @classmethod
    def new(cls, logical_session: LogicalSession, *, request_kind: str = "turn") -> "TurnContext":
        return cls(
            account_owner_digest=logical_session.owner_digest,
            logical_session_key=logical_session.session_id,
            thread_id=logical_session.root_thread_id,
            turn_id=uuid7(),
            turn_started_at_unix_ms=int(time.time() * 1000),
            request_kind=request_kind,
        )

    def capture_turn_state(self, value: Any, *, owner_digest: str, turn_id: str) -> bool:
        """Capture an opaque token only for this exact account and logical turn."""
        if owner_digest != self.account_owner_digest or turn_id != self.turn_id:
            return False
        token = str(value or "").strip()
        if not token or "\r" in token or "\n" in token:
            return False
        self.turn_state = token
        return True


@dataclass(frozen=True)
class RequestIdentitySnapshot:
    installation_id: str
    owner_digest: str
    session_id: str
    thread_id: str
    turn_id: str
    window_id: str
    window_number: int
    context_window_id: str
    request_kind: str
    turn_started_at_unix_ms: int
    prompt_cache_key: str
    turn_state: str | None = None

    def turn_metadata(self) -> dict[str, Any]:
        # Field order follows codex-rs CodexTurnMetadataPayload's Serialize order.
        return {
            "installation_id": self.installation_id,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "window_id": self.window_id,
            "window_number": self.window_number,
            "context_window_id": self.context_window_id,
            "request_kind": self.request_kind,
            "turn_started_at_unix_ms": self.turn_started_at_unix_ms,
        }

    def canonical_turn_metadata(self) -> str:
        return json.dumps(
            self.turn_metadata(), ensure_ascii=True, separators=(",", ":")
        )

    def with_turn_state(self, value: str | None) -> "RequestIdentitySnapshot":
        return replace(self, turn_state=(str(value).strip() if value else None))


@dataclass
class RequestIdentityContext:
    account_identity: AccountIdentity
    logical_session: LogicalSession
    turn: TurnContext

    def snapshot(self) -> RequestIdentitySnapshot:
        return build_request_identity_snapshot(
            self.account_identity, self.logical_session, self.turn
        )


async def acquire_thread_turn(
    context: RequestIdentityContext,
) -> ThreadTurnLease:
    """Queue one upstream turn by canonical owner+thread, never by account alone."""
    key = (context.account_identity.owner_digest, context.logical_session.root_thread_id)
    with _THREAD_LOCKS_GUARD:
        entry = _THREAD_LOCKS.get(key)
        if entry is None:
            entry = _ThreadLockEntry(asyncio.Lock())
            _THREAD_LOCKS[key] = entry
        entry.users += 1
    try:
        await entry.lock.acquire()
    except BaseException:
        with _THREAD_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and not entry.lock.locked():
                _THREAD_LOCKS.pop(key, None)
        raise
    return ThreadTurnLease(key, entry)


async def acquire_request_turn_serialization(
    requested_body: MutableMapping[str, Any], context: RequestIdentityContext,
) -> ThreadTurnLease:
    """Acquire and remember a request-local lease, then refresh its durable window.

    The refresh happens after queue admission so a request that was waiting behind
    a successful compaction cannot send the stale pre-compaction window snapshot.
    """
    leases = requested_body.setdefault(_TURN_LEASES_KEY, {})
    if not isinstance(leases, dict):
        raise CodexIdentityError("invalid internal Codex turn lease map")
    key = (context.account_identity.owner_digest, context.logical_session.root_thread_id)
    existing = leases.get(key)
    if isinstance(existing, ThreadTurnLease) and not existing.released:
        return existing
    lease = await acquire_thread_turn(context)
    try:
        logical = context.logical_session
        if logical.durable:
            row = state_db.codex_logical_session_load(
                logical.owner_digest,
                logical.downstream_principal_digest,
                logical.downstream_anchor_digest,
            )
            if not isinstance(row, Mapping):
                raise CodexIdentityError("durable logical session disappeared before dispatch")
            context.logical_session = LogicalSession.from_row(row, durable=True)
        leases[key] = lease
        return lease
    except BaseException:
        lease.release()
        raise


def release_request_turn_serialization(requested_body: Any) -> int:
    """Release every live turn lease owned by one attempt body; idempotent."""
    if not isinstance(requested_body, MutableMapping):
        return 0
    leases = requested_body.pop(_TURN_LEASES_KEY, None)
    if not isinstance(leases, dict):
        return 0
    released = 0
    for lease in list(leases.values()):
        if isinstance(lease, ThreadTurnLease) and not lease.released:
            lease.release()
            released += 1
    return released


def active_thread_turn_queue_count() -> int:
    """Test/diagnostic view: live owner+thread queues, excluding reclaimed entries."""
    with _THREAD_LOCKS_GUARD:
        return len(_THREAD_LOCKS)


def account_identity_from_account(
    account: Mapping[str, Any], *, require: bool = True
) -> AccountIdentity | None:
    workspace_id = workspace_id_from_account(account)
    raw = account.get("codexIdentity")
    if not workspace_id:
        if raw or account.get("codexDeviceInstallationId"):
            raise CodexIdentityError(
                "Codex installation identity cannot be bound without a ChatGPT workspace/account ID"
            )
        if require:
            raise CodexIdentityError(
                "OpenAI OAuth workspace/account ID is unknown after token refresh"
            )
        return None
    if not isinstance(raw, Mapping):
        if require:
            raise CodexIdentityError("OpenAI OAuth account has no versioned codexIdentity")
        return None
    expected_owner = owner_digest_for_workspace(workspace_id)
    identity = AccountIdentity(
        schema_version=int(raw.get("schemaVersion") or 0),
        id_generation_version=int(raw.get("idGenerationVersion") or 0),
        protocol_profile=str(raw.get("protocolProfile") or "").strip(),
        owner_kind=str(raw.get("ownerKind") or "").strip(),
        owner_digest=str(raw.get("ownerDigest") or "").strip(),
        installation_id=canonical_uuid(
            raw.get("installationId"), version=4, field="installationId"
        ),
        created_at=str(raw.get("createdAt") or "").strip(),
    )
    if identity.schema_version != SCHEMA_VERSION:
        raise CodexIdentityError("unsupported codexIdentity schemaVersion")
    if identity.id_generation_version != ID_GENERATION_VERSION:
        raise CodexIdentityError("unsupported codexIdentity idGenerationVersion")
    if identity.owner_kind != OWNER_KIND or identity.owner_digest != expected_owner:
        raise CodexIdentityError("codexIdentity owner binding does not match workspace")
    if not identity.protocol_profile or not identity.created_at:
        raise CodexIdentityError("codexIdentity profile and createdAt are required")
    mirror = account.get("codexDeviceInstallationId")
    if mirror not in (None, "") and canonical_uuid(
        mirror, version=4, field="codexDeviceInstallationId"
    ) != identity.installation_id:
        raise CodexIdentityError("legacy installation mirror conflicts with codexIdentity")
    return identity


def _tombstone_for_owner(owner_digest: str) -> Mapping[str, Any] | None:
    try:
        return state_db.codex_identity_tombstone_load(owner_digest)
    except RuntimeError as exc:
        if "state store not started" in str(exc):
            return None
        raise


def normalize_account_identity(
    account: MutableMapping[str, Any],
    *,
    protocol_profile: str | None = None,
    new_identity_generation_version: int = ID_GENERATION_VERSION,
    allow_legacy_collision_rotation: bool = False,
    adopt_preserved_identity: bool = False,
    used_installations: MutableMapping[str, str] | None = None,
) -> bool:
    """Migrate/validate one OpenAI account without rotating a valid legacy UUIDv4.

    Unknown workspaces remain identity-less and therefore fail closed at dispatch.
    ``codexDeviceConvergenceEnabled`` is removed: per-workspace identity is now a
    protocol invariant rather than an optional transport behavior.
    """
    provider = str(account.get("provider") or account.get("type") or "").lower()
    if provider != "openai":
        return False
    changed = False
    if "codexDeviceConvergenceEnabled" in account:
        account.pop("codexDeviceConvergenceEnabled", None)
        changed = True
    workspace_id = workspace_id_from_account(account)
    if not workspace_id:
        # Legacy configs may already contain the authoritative workspace inside
        # an OAuth-issued JWT. Decode locally; never infer it from email or org.
        from ..oauth.openai import decode_id_token, extract_user_info
        for token_field in ("access_token", "id_token"):
            token = account.get(token_field)
            if not isinstance(token, str) or not token:
                continue
            try:
                info = extract_user_info(decode_id_token(token))
            except (ValueError, TypeError, AttributeError):
                continue
            workspace_id = workspace_id_from_account(info)
            if workspace_id:
                account["workspace_id"] = workspace_id
                account["chatgpt_account_id"] = str(
                    info.get("chatgpt_account_id") or workspace_id
                )
                changed = True
                break
    if not workspace_id:
        # A legacy account may legitimately await its first successful refresh,
        # but it may not carry an unbound installation identity.
        if account.get("codexIdentity") or account.get("codexDeviceInstallationId"):
            raise CodexIdentityError(
                "Codex installation identity requires a nonempty OpenAI workspace/account ID"
            )
        return changed

    selected_profile = str(protocol_profile or "").strip()
    if not selected_profile:
        raise CodexIdentityError(
            "Codex identity normalization requires the selected "
            "openaiOAuth.codexProtocolProfile"
        )

    owner_digest = owner_digest_for_workspace(workspace_id)
    existing_obj = account.get("codexIdentity")
    legacy_raw = account.get("codexDeviceInstallationId")
    identity: AccountIdentity | None = None
    legacy_only = not isinstance(existing_obj, Mapping)
    if isinstance(existing_obj, Mapping):
        identity = account_identity_from_account(account)
    else:
        try:
            generation_version = int(new_identity_generation_version)
        except (TypeError, ValueError) as exc:
            raise CodexIdentityError(
                "openaiOAuth.codexIdentity.newIdentityGenerationVersion must be an integer"
            ) from exc
        if generation_version != ID_GENERATION_VERSION:
            raise CodexIdentityError(
                "unsupported openaiOAuth.codexIdentity.newIdentityGenerationVersion"
            )
        installation_id = ""
        if legacy_raw not in (None, ""):
            installation_id = canonical_uuid(
                legacy_raw, version=4, field="codexDeviceInstallationId"
            )
        tombstone = _tombstone_for_owner(owner_digest)
        if tombstone:
            restored = canonical_uuid(
                tombstone.get("installation_id"),
                version=4,
                field="tombstone installation_id",
            )
            if installation_id and installation_id != restored and not adopt_preserved_identity:
                raise CodexIdentityError(
                    "configured installation conflicts with preserved owner tombstone"
                )
            installation_id = restored
        if not installation_id:
            installation_id = str(uuid.uuid4())
        identity = AccountIdentity(
            schema_version=SCHEMA_VERSION,
            id_generation_version=generation_version,
            protocol_profile=selected_profile,
            owner_kind=OWNER_KIND,
            owner_digest=owner_digest,
            installation_id=installation_id,
            created_at=str((tombstone or {}).get("created_at") or _utc_now()),
        )
        account["codexIdentity"] = identity.as_config()
        changed = True

    assert identity is not None
    if identity.protocol_profile != selected_profile:
        identity = replace(identity, protocol_profile=selected_profile)
        account["codexIdentity"] = identity.as_config()
        changed = True
    if used_installations is not None:
        prior_owner = used_installations.get(identity.installation_id)
        if prior_owner and prior_owner != identity.owner_digest:
            if allow_legacy_collision_rotation and legacy_only:
                identity = replace(
                    identity,
                    installation_id=str(uuid.uuid4()),
                    created_at=_utc_now(),
                )
                account["codexIdentity"] = identity.as_config()
                changed = True
            else:
                raise CodexIdentityError(
                    "different OpenAI workspaces cannot share a Codex installation identity"
                )
        used_installations[identity.installation_id] = identity.owner_digest

    if account.get("codexDeviceInstallationId") != identity.installation_id:
        account["codexDeviceInstallationId"] = identity.installation_id
        changed = True
    return changed


def normalize_account_identities(
    accounts: Any,
    *,
    protocol_profile: str | None = None,
    new_identity_generation_version: int = ID_GENERATION_VERSION,
) -> bool:
    if not isinstance(accounts, list):
        return False
    changed = False
    used: dict[str, str] = {}
    owners: dict[str, AccountIdentity] = {}
    # Existing versioned identities win over legacy per-login UUIDs regardless
    # of config order. Within each group retain original order and credentials.
    ordered = sorted(
        (account for account in accounts if isinstance(account, MutableMapping)),
        key=lambda account: not isinstance(account.get("codexIdentity"), Mapping),
    )
    for account in ordered:
        if isinstance(account, MutableMapping):
            explicit_identity = isinstance(account.get("codexIdentity"), Mapping)
            changed = normalize_account_identity(
                account,
                protocol_profile=protocol_profile,
                new_identity_generation_version=new_identity_generation_version,
                allow_legacy_collision_rotation=True,
                adopt_preserved_identity=True,
                used_installations=used,
            ) or changed
            current = account_identity_from_account(account, require=False)
            if current is None:
                continue
            prior = owners.get(current.owner_digest)
            if prior is not None and prior.installation_id != current.installation_id:
                if explicit_identity:
                    raise CodexIdentityError(
                        "duplicate canonical OpenAI owner has conflicting Codex identities"
                    )
                # The discarded legacy UUID no longer occupies an owner slot.
                used.pop(current.installation_id, None)
                account["codexIdentity"] = prior.as_config()
                account["codexDeviceInstallationId"] = prior.installation_id
                current = prior
                changed = True
            owners[current.owner_digest] = current
    return changed


def register_account_identity(account: Mapping[str, Any]) -> AccountIdentity:
    identity = account_identity_from_account(account)
    assert identity is not None
    try:
        row = state_db.codex_identity_tombstone_claim(
            identity.owner_digest,
            identity.installation_id,
            identity.id_generation_version,
            created_at=identity.created_at,
        )
    except RuntimeError as exc:
        # Account-management helpers are also used during pre-lifespan tooling.
        # The versioned config object remains durable and startup synchronization
        # claims its tombstone before outbound traffic.
        if "state store not started" in str(exc):
            return identity
        raise
    if str(row.get("installation_id") or "") != identity.installation_id:
        raise CodexIdentityError("owner tombstone installation conflict")
    return identity


def sync_configured_identity_tombstones(accounts: Any) -> int:
    count = 0
    for account in accounts if isinstance(accounts, list) else []:
        if not isinstance(account, Mapping):
            continue
        if str(account.get("provider") or account.get("type") or "").lower() != "openai":
            continue
        identity = account_identity_from_account(account, require=False)
        if identity is None:
            continue
        register_account_identity(account)
        count += 1
    return count


def _new_logical_row(
    owner_digest: str,
    principal_digest: str,
    anchor_digest: str,
) -> dict[str, Any]:
    now = state_db.now_ms()
    session_id = uuid7(unix_ms=now)
    return {
        "owner_digest": owner_digest,
        "downstream_principal_digest": principal_digest,
        "downstream_anchor_digest": anchor_digest,
        "session_id": session_id,
        "root_thread_id": session_id,
        "upstream_prompt_cache_key": session_id,
        "window_number": 0,
        "context_window_id": uuid7(unix_ms=now),
        "created_at": now,
        "last_used_at": now,
    }


def resolve_logical_session(
    owner_digest: str,
    downstream_principal_digest: str,
    downstream_anchor_digest: str,
    *,
    durable: bool,
) -> LogicalSession:
    candidate = _new_logical_row(
        owner_digest, downstream_principal_digest, downstream_anchor_digest
    )
    if durable:
        row = state_db.codex_logical_session_resolve(candidate)
    else:
        row = candidate
    return LogicalSession.from_row(row, durable=durable)


def advance_context_window(
    logical_session: LogicalSession, *, expected_window_number: int
) -> LogicalSession:
    if not logical_session.durable:
        if logical_session.window_number != expected_window_number:
            raise CodexIdentityError("logical-session window CAS conflict")
        return replace(
            logical_session,
            window_number=logical_session.window_number + 1,
            context_window_id=uuid7(),
            last_used_at=state_db.now_ms(),
        )
    row = state_db.codex_logical_session_advance_window(
        logical_session.owner_digest,
        logical_session.downstream_principal_digest,
        logical_session.downstream_anchor_digest,
        expected_window_number=expected_window_number,
        context_window_id=uuid7(),
    )
    return LogicalSession.from_row(row, durable=True)


def _parse_turn_metadata(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def resolve_downstream_anchor(payload: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Return ``(kind, raw_value, durable)`` in the approved priority order."""
    native = payload.get(_NATIVE_IDENTITY_KEY)
    native_headers = native.get("headers") if isinstance(native, Mapping) else {}
    native_metadata = native.get("client_metadata") if isinstance(native, Mapping) else {}
    client_metadata = payload.get("client_metadata")
    metadata_candidates = [
        value for value in (native_metadata, client_metadata) if isinstance(value, Mapping)
    ]
    for metadata in metadata_candidates:
        session = str(metadata.get("session_id") or "").strip()
        if session:
            return "native-session", session, True
        turn_meta = _parse_turn_metadata(metadata.get("x-codex-turn-metadata"))
        session = str(turn_meta.get("session_id") or "").strip()
        if session:
            return "native-turn-session", session, True
    if isinstance(native_headers, Mapping):
        for key, value in native_headers.items():
            if str(key).lower() in {"session-id", "session_id"} and str(value).strip():
                return "native-session-header", str(value).strip(), True
        for key, value in native_headers.items():
            if str(key).lower() == "x-codex-turn-metadata":
                session = str(_parse_turn_metadata(value).get("session_id") or "").strip()
                if session:
                    return "native-turn-header", session, True

    prompt_cache_key = str(payload.get("prompt_cache_key") or "").strip()
    client_fields = payload.get("_client_body_fields")
    if prompt_cache_key and isinstance(client_fields, list) and "prompt_cache_key" in client_fields:
        return "prompt-cache-key", prompt_cache_key, True
    if prompt_cache_key:
        return "parrot-affinity", prompt_cache_key, True

    for key in ("_parrot_stable_anchor", "_response_store_anchor", "_affinity_anchor"):
        value = str(payload.get(key) or "").strip()
        if value:
            return "parrot-affinity", value, True
    # The random raw component exists only in this request-local object and is
    # neither persisted nor projected.
    return "request", secrets.token_hex(32), False


def _downstream_turn_carrier(payload: Mapping[str, Any]) -> str:
    """Read only turn-id carriers already accepted by the native ingress parsers."""
    native = payload.get(_NATIVE_IDENTITY_KEY)
    native_headers = native.get("headers") if isinstance(native, Mapping) else {}
    native_metadata = native.get("client_metadata") if isinstance(native, Mapping) else {}
    client_metadata = payload.get("client_metadata")
    for metadata in (native_metadata, client_metadata):
        if not isinstance(metadata, Mapping):
            continue
        direct = str(metadata.get("turn_id") or "").strip()
        if direct:
            return direct
        nested = str(
            _parse_turn_metadata(metadata.get("x-codex-turn-metadata")).get("turn_id")
            or ""
        ).strip()
        if nested:
            return nested
    if isinstance(native_headers, Mapping):
        for key, value in native_headers.items():
            if str(key).lower() == "x-codex-turn-metadata":
                nested = str(_parse_turn_metadata(value).get("turn_id") or "").strip()
                if nested:
                    return nested
    return ""


def _cleanup_turn_mappings_locked(now: float) -> None:
    expired = [
        key for key, entry in _TURN_MAPPINGS.items()
        if now - entry.touched_at >= TURN_MAPPING_TTL_SECONDS
    ]
    for key in expired:
        _TURN_MAPPINGS.pop(key, None)
    overflow = len(_TURN_MAPPINGS) - TURN_MAPPING_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(_TURN_MAPPINGS, key=lambda key: _TURN_MAPPINGS[key].touched_at)
        for key in oldest[:overflow]:
            _TURN_MAPPINGS.pop(key, None)


def resolve_turn_context(
    logical_session: LogicalSession,
    payload: Mapping[str, Any],
    *,
    now_monotonic: float | None = None,
) -> TurnContext:
    """Map a trusted native downstream turn carrier to one opaque upstream UUIDv7.

    Raw downstream IDs are hashed for lookup only. A different explicit turn on
    the same owner/session retires the previous active mapping; otherwise expiry
    and the hard entry bound reclaim abandoned clients.
    """
    downstream_turn = _downstream_turn_carrier(payload)
    if not downstream_turn:
        return TurnContext.new(logical_session)
    now = time.monotonic() if now_monotonic is None else float(now_monotonic)
    digest = scoped_digest("downstream-turn", downstream_turn)
    scope = (
        logical_session.owner_digest,
        logical_session.session_id,
        logical_session.root_thread_id,
    )
    key = (*scope, digest)
    with _TURN_MAPPINGS_GUARD:
        _cleanup_turn_mappings_locked(now)
        existing = _TURN_MAPPINGS.get(key)
        if existing is not None:
            existing.touched_at = now
            return existing.context
        # A new explicit downstream turn is the observable end of the prior
        # mapping for this owner+logical thread.
        for old_key in [candidate for candidate in _TURN_MAPPINGS if candidate[:3] == scope]:
            _TURN_MAPPINGS.pop(old_key, None)
        context = TurnContext.new(logical_session)
        _TURN_MAPPINGS[key] = _TurnMappingEntry(context=context, touched_at=now)
        _cleanup_turn_mappings_locked(now)
        return context


def next_request_identity_context(
    previous: RequestIdentityContext, payload: Mapping[str, Any]
) -> RequestIdentityContext:
    """Resolve the next WS create as a same-turn continuation or a fresh turn."""
    return RequestIdentityContext(
        previous.account_identity,
        previous.logical_session,
        resolve_turn_context(previous.logical_session, payload),
    )


def clear_turn_mappings_for_tests() -> None:
    with _TURN_MAPPINGS_GUARD:
        _TURN_MAPPINGS.clear()


def resolve_request_identity_context(
    account: Mapping[str, Any], requested_body: MutableMapping[str, Any]
) -> RequestIdentityContext:
    identity = account_identity_from_account(account)
    assert identity is not None
    contexts = requested_body.setdefault(_ACCOUNT_CONTEXTS_KEY, {})
    if not isinstance(contexts, dict):
        raise CodexIdentityError("invalid internal Codex identity context map")
    existing = contexts.get(identity.owner_digest)
    if isinstance(existing, RequestIdentityContext):
        return existing

    principal_raw = str(
        requested_body.get("_api_key_name")
        or requested_body.get("_parrot_api_key_name")
        or "anonymous"
    )
    anchor_kind, anchor_raw, durable = resolve_downstream_anchor(requested_body)
    logical = resolve_logical_session(
        identity.owner_digest,
        scoped_digest("principal", principal_raw),
        scoped_digest("anchor:" + anchor_kind, anchor_raw),
        durable=durable,
    )
    context = RequestIdentityContext(
        identity, logical, resolve_turn_context(logical, requested_body)
    )
    contexts[identity.owner_digest] = context
    return context


def build_request_identity_snapshot(
    account_identity: AccountIdentity,
    logical_session: LogicalSession,
    turn: TurnContext,
) -> RequestIdentitySnapshot:
    if account_identity.owner_digest != logical_session.owner_digest:
        raise CodexIdentityError("account/logical-session owner mismatch")
    if turn.account_owner_digest != logical_session.owner_digest:
        raise CodexIdentityError("turn/account owner mismatch")
    if turn.logical_session_key != logical_session.session_id:
        raise CodexIdentityError("turn/logical-session mismatch")
    thread_id = canonical_uuid(
        logical_session.root_thread_id, version=7, field="thread_id"
    )
    canonical_uuid(turn.turn_id, version=7, field="turn_id")
    window_id = f"{thread_id}:{logical_session.window_number}"
    return RequestIdentitySnapshot(
        installation_id=account_identity.installation_id,
        owner_digest=account_identity.owner_digest,
        session_id=logical_session.session_id,
        thread_id=thread_id,
        turn_id=turn.turn_id,
        window_id=window_id,
        window_number=logical_session.window_number,
        context_window_id=logical_session.context_window_id,
        request_kind=turn.request_kind,
        turn_started_at_unix_ms=turn.turn_started_at_unix_ms,
        prompt_cache_key=logical_session.upstream_prompt_cache_key,
        turn_state=turn.turn_state,
    )


def next_turn_context(context: RequestIdentityContext) -> RequestIdentityContext:
    return RequestIdentityContext(
        context.account_identity,
        context.logical_session,
        TurnContext.new(context.logical_session),
    )


def _drop_headers(headers: Mapping[str, Any], names: set[str]) -> dict[str, str]:
    lowered = {name.lower() for name in names}
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in lowered
    }


def project_snapshot(
    snapshot: RequestIdentitySnapshot,
    headers: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    *,
    direct_installation_header: bool = False,
    create_client_metadata: bool = True,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Project one immutable snapshot to HTTP headers and a body/WS frame."""
    out_headers = _drop_headers(
        headers or {},
        {
            "session_id", "conversation_id", "conversation-id",
            "session-id", "thread-id", "x-client-request-id",
            "x-codex-window-id", "x-codex-turn-metadata",
            "x-codex-turn-state", "x-codex-installation-id",
        },
    )
    metadata_json = snapshot.canonical_turn_metadata()
    out_headers.update({
        "session-id": snapshot.session_id,
        "thread-id": snapshot.thread_id,
        "x-client-request-id": snapshot.thread_id,
        "x-codex-window-id": snapshot.window_id,
        "x-codex-turn-metadata": metadata_json,
    })
    if snapshot.turn_state:
        out_headers["x-codex-turn-state"] = snapshot.turn_state
    if direct_installation_header:
        out_headers["x-codex-installation-id"] = snapshot.installation_id

    if payload is None:
        return out_headers, None
    out_payload: dict[str, Any] = {
        key: value for key, value in payload.items()
        if not (isinstance(key, str) and key.startswith("_"))
    }
    out_payload["prompt_cache_key"] = snapshot.prompt_cache_key
    current_metadata = out_payload.get("client_metadata")
    if current_metadata is not None and not isinstance(current_metadata, Mapping):
        raise CodexIdentityError("client_metadata must be an object for Codex identity")
    if current_metadata is None and not create_client_metadata:
        return out_headers, out_payload
    metadata = dict(current_metadata or {})
    for key in (
        "x-codex-installation-id", "session_id", "thread_id", "turn_id",
        "x-codex-window-id", "x-codex-turn-metadata", "x-codex-turn-state",
    ):
        metadata.pop(key, None)
    metadata.update({
        "x-codex-installation-id": snapshot.installation_id,
        "session_id": snapshot.session_id,
        "thread_id": snapshot.thread_id,
        "turn_id": snapshot.turn_id,
        "x-codex-window-id": snapshot.window_id,
        "x-codex-turn-metadata": metadata_json,
    })
    if snapshot.turn_state:
        metadata["x-codex-turn-state"] = snapshot.turn_state
    out_payload["client_metadata"] = metadata
    return out_headers, out_payload


def capture_turn_state_event(
    translator_ctx: Mapping[str, Any] | None, frame: str | bytes | Mapping[str, Any]
) -> bool:
    """Capture the official event-level ``response.metadata.headers`` carrier."""
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(frame, str):
        try:
            frame = json.loads(frame)
        except (TypeError, ValueError):
            return False
    if not isinstance(frame, Mapping) or frame.get("type") != "response.metadata":
        return False
    headers = frame.get("headers")
    if not isinstance(headers, Mapping):
        return False
    return capture_turn_state(translator_ctx, headers)


def capture_turn_state(
    translator_ctx: Mapping[str, Any] | None, headers: Any
) -> bool:
    if not isinstance(translator_ctx, Mapping):
        return False
    context = translator_ctx.get("codex_identity_context")
    snapshot = translator_ctx.get("codex_identity_snapshot")
    if not isinstance(context, RequestIdentityContext) or not isinstance(
        snapshot, RequestIdentitySnapshot
    ):
        return False
    token = ""
    # ``websockets.Headers.items()`` raises when an unrelated header (notably
    # Set-Cookie) legally occurs more than once. Read only the target field and
    # reject an ambiguous repeated turn-state instead of enumerating all headers.
    if hasattr(headers, "get_all"):
        try:
            values = list(headers.get_all("x-codex-turn-state"))
        except Exception:
            values = []
        if len(values) == 1:
            token = str(values[0])
    elif isinstance(headers, Mapping):
        values = [
            value for key, value in headers.items()
            if str(key).lower() == "x-codex-turn-state"
        ]
        if len(values) == 1:
            token = str(values[0])
    elif hasattr(headers, "get"):
        try:
            token = str(headers.get("x-codex-turn-state") or "")
        except Exception:
            token = ""
    elif hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == "x-codex-turn-state":
                token = str(value)
                break
    return context.turn.capture_turn_state(
        token,
        owner_digest=snapshot.owner_digest,
        turn_id=snapshot.turn_id,
    )


def forget_owner_identity(owner_digest: str) -> bool:
    """Explicit high-risk forget path. Credentials must already be absent."""
    owner = str(owner_digest or "").strip()
    if not owner.startswith("sha256:"):
        raise CodexIdentityError("forget identity requires an owner digest")
    state_db.codex_logical_session_delete_owner(owner)
    state_db.compaction_owner_delete_owner(owner)
    return state_db.codex_identity_tombstone_delete(owner)
