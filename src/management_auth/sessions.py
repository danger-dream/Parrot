"""High-entropy, revocable, multi-device management sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from .policy import authorize
from .principal import (
    ADMINISTRATOR_CAPABILITIES,
    AuthMethod,
    Capability,
    ManagementPrincipal,
    Role,
)
from .store import ManagementStateStore


class SessionAuthenticationError(Exception):
    """Internal auth failure mapped by the transport to one stable public code."""

    def __init__(self, reason: str = "required") -> None:
        self.reason = reason
        super().__init__(reason)


class AuthenticationRateLimited(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle_timeout_seconds: int = 3 * 24 * 60 * 60
    absolute_timeout_seconds: int = 30 * 24 * 60 * 60
    touch_interval_seconds: int = 5 * 60


@dataclass(frozen=True, slots=True)
class IssuedSession:
    credential: str
    principal: ManagementPrincipal
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedSession:
    principal: ManagementPrincipal
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionDevice:
    """Non-secret description of one active session for the device list."""

    session_id: str
    auth_method: AuthMethod
    client_name: str | None
    device_summary: str | None
    source_address: str | None
    issued_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    idle_expires_at: datetime


class AuthenticationRateLimiter:
    """Bounded in-process global and per-source fixed-window limiter."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        window_seconds: int,
        per_source: int,
        global_limit: int,
        max_sources: int = 2_000,
    ) -> None:
        self._clock = clock
        self._window = window_seconds
        self._per_source = per_source
        self._global_limit = global_limit
        self._max_sources = max_sources
        self._global: deque[float] = deque()
        self._sources: dict[str, deque[float]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _trim(bucket: deque[float], cutoff: float) -> None:
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def consume(self, source: str) -> None:
        source_key = (source or "unknown")[:128]
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            self._trim(self._global, cutoff)
            bucket = self._sources.setdefault(source_key, deque())
            self._trim(bucket, cutoff)
            if len(self._global) >= self._global_limit or len(bucket) >= self._per_source:
                raise AuthenticationRateLimited
            self._global.append(now)
            bucket.append(now)
            if len(self._sources) > self._max_sources:
                empty = [key for key, item in self._sources.items() if not item]
                for key in empty[: len(self._sources) - self._max_sources]:
                    self._sources.pop(key, None)
                while len(self._sources) > self._max_sources:
                    self._sources.pop(next(iter(self._sources)))


class SessionService:
    def __init__(
        self,
        store: ManagementStateStore,
        *,
        management_key: str,
        policy: SessionPolicy,
        clock: Callable[[], float],
        rate_limit_window_seconds: int = 60,
        rate_limit_per_source: int = 5,
        rate_limit_global: int = 30,
    ) -> None:
        self._store = store
        self._management_key = management_key
        self._policy = policy
        self._clock = clock
        self._rate_limiter = AuthenticationRateLimiter(
            clock=clock,
            window_seconds=rate_limit_window_seconds,
            per_source=rate_limit_per_source,
            global_limit=rate_limit_global,
        )
        fingerprint = hashlib.sha256(management_key.encode("utf-8")).hexdigest()
        self._store.synchronize_credential(fingerprint)

    @staticmethod
    def _verifier(credential: str) -> bytes:
        return hashlib.sha256(credential.encode("utf-8")).digest()

    @staticmethod
    def _at(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    def _new_session_values(
        self,
        *,
        subject_id: str,
        auth_method: AuthMethod,
        roles: Iterable[Role],
        capabilities: Iterable[Capability],
        client_name: str | None = None,
        source_address: str | None = None,
        device_summary: str | None = None,
    ) -> tuple[str, dict]:
        now = self._clock()
        selector = secrets.token_urlsafe(12)
        credential = f"pms_{selector}.{secrets.token_urlsafe(32)}"
        values = {
            "session_id": f"ms_{secrets.token_urlsafe(18)}",
            "selector": selector,
            "verifier": self._verifier(credential),
            "subject_id": subject_id,
            "auth_method": auth_method.value,
            "roles": sorted(role.value for role in roles),
            "capabilities": sorted(capability.value for capability in capabilities),
            "issued_at": now,
            "last_seen_at": now,
            "expires_at": now + self._policy.absolute_timeout_seconds,
            "generation": self._store.credential_generation(),
            "client_name": client_name,
            "source_address": source_address,
            "device_summary": device_summary,
        }
        return credential, values

    def _issued(self, credential: str, values: dict) -> IssuedSession:
        principal = ManagementPrincipal(
            subject_id=values["subject_id"],
            auth_method=AuthMethod(values["auth_method"]),
            roles=frozenset(Role(value) for value in values["roles"]),
            capabilities=frozenset(Capability(value) for value in values["capabilities"]),
            issued_at=self._at(values["issued_at"]),
            session_id=values["session_id"],
        )
        return IssuedSession(
            credential=credential,
            principal=principal,
            expires_at=self._at(values["expires_at"]),
            idle_expires_at=self._at(
                min(
                    values["expires_at"],
                    values["last_seen_at"] + self._policy.idle_timeout_seconds,
                )
            ),
        )

    def create_from_management_key(
        self,
        presented_key: str,
        *,
        source: str,
        request_id: str,
        client_name: str | None = None,
        device_summary: str | None = None,
    ) -> IssuedSession:
        try:
            self._rate_limiter.consume(source)
        except AuthenticationRateLimited:
            self._store.record_audit(
                actor="anonymous",
                action="session.create.management-key",
                target="management-session",
                result="rate-limited",
                request_id=request_id,
            )
            raise
        expected = hashlib.sha256(self._management_key.encode("utf-8")).digest()
        presented = hashlib.sha256(presented_key.encode("utf-8")).digest()
        if not hmac.compare_digest(expected, presented):
            self._store.record_audit(
                actor="anonymous",
                action="session.create.management-key",
                target="management-session",
                result="denied",
                request_id=request_id,
            )
            raise SessionAuthenticationError("failed")
        issued = self.issue_for_principal(
            subject_id="administrator",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            roles=(Role.ADMINISTRATOR,),
            capabilities=ADMINISTRATOR_CAPABILITIES,
            client_name=client_name,
            source_address=source,
            device_summary=device_summary,
        )
        self._store.record_audit(
            actor="management-key",
            action="session.create.management-key",
            target=issued.principal.session_id or "management-session",
            result="succeeded",
            request_id=request_id,
        )
        return issued

    def issue_for_principal(
        self,
        *,
        subject_id: str,
        auth_method: AuthMethod,
        roles: Iterable[Role],
        capabilities: Iterable[Capability],
        client_name: str | None = None,
        source_address: str | None = None,
        device_summary: str | None = None,
    ) -> IssuedSession:
        credential, values = self._new_session_values(
            subject_id=subject_id,
            auth_method=auth_method,
            roles=roles,
            capabilities=capabilities,
            client_name=client_name,
            source_address=source_address,
            device_summary=device_summary,
        )
        self._store.insert_session(values)
        return self._issued(credential, values)

    def create_from_telegram_approval(
        self,
        *,
        approval_id: str,
        exchange_secret: str,
        request_id: str,
    ) -> IssuedSession:
        credential, values = self._new_session_values(
            subject_id="administrator",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(Role.ADMINISTRATOR,),
            capabilities=ADMINISTRATOR_CAPABILITIES,
        )
        state, approval = self._store.consume_approval_and_insert_session(
            approval_id,
            secret_verifier=self._verifier(exchange_secret),
            session_values=values,
        )
        if state == "authenticationFailed" or state == "notFound":
            raise SessionAuthenticationError("failed")
        if state == "alreadyConsumed":
            raise SessionAuthenticationError("consumed")
        if state != "consumed":
            raise SessionAuthenticationError(state)
        issued = self._issued(credential, values)
        self._store.record_audit(
            actor=f"telegram:{approval.get('decided_by') or 'administrator'}",
            action="session.create.telegram-approval",
            target=issued.principal.session_id or "management-session",
            result="succeeded",
            request_id=request_id,
        )
        return issued

    @staticmethod
    def _parse(credential: str) -> tuple[str, bytes]:
        if not credential.startswith("pms_") or len(credential) > 512:
            raise SessionAuthenticationError
        try:
            selector, _ = credential[4:].split(".", 1)
        except ValueError as exc:
            raise SessionAuthenticationError from exc
        if not selector:
            raise SessionAuthenticationError
        return selector, SessionService._verifier(credential)

    def verify(self, credential: str, *, touch: bool = True) -> VerifiedSession:
        selector, verifier = self._parse(credential)
        row = self._store.get_session(selector)
        if row is None or not hmac.compare_digest(bytes(row["verifier"]), verifier):
            raise SessionAuthenticationError
        now = self._clock()
        if row["revoked_at"] is not None or row["generation"] != self._store.credential_generation():
            raise SessionAuthenticationError
        if now >= row["expires_at"] or now - row["last_seen_at"] >= self._policy.idle_timeout_seconds:
            self._store.revoke_session(row["session_id"])
            raise SessionAuthenticationError("expired")
        last_seen = float(row["last_seen_at"])
        if touch and now - last_seen >= self._policy.touch_interval_seconds:
            if self._store.touch_session(selector, previous_seen_at=last_seen, now=now):
                last_seen = now
        principal = ManagementPrincipal(
            subject_id=row["subject_id"],
            auth_method=AuthMethod(row["auth_method"]),
            roles=frozenset(Role(value) for value in json.loads(row["roles_json"])),
            capabilities=frozenset(
                Capability(value) for value in json.loads(row["capabilities_json"])
            ),
            issued_at=self._at(row["issued_at"]),
            session_id=row["session_id"],
        )
        return VerifiedSession(
            principal=principal,
            expires_at=self._at(row["expires_at"]),
            idle_expires_at=self._at(
                min(row["expires_at"], last_seen + self._policy.idle_timeout_seconds)
            ),
        )

    def revoke_current(self, credential: str, *, request_id: str) -> None:
        verified = self.verify(credential, touch=False)
        self._store.revoke_session(verified.principal.session_id or "")
        self._store.record_audit(
            actor=verified.principal.subject_id,
            action="session.revoke-current",
            target=verified.principal.session_id or "management-session",
            result="succeeded",
            request_id=request_id,
        )

    def list_devices(
        self, principal: ManagementPrincipal, *, request_id: str,
    ) -> tuple[SessionDevice, ...]:
        """Active (unrevoked, unexpired, current-generation) sessions."""
        authorize(principal, Capability.READ)
        rows = self._store.list_active_sessions(
            now=self._clock(),
            idle_timeout_seconds=self._policy.idle_timeout_seconds,
        )
        self._store.record_audit(
            actor=principal.subject_id,
            action="session.list",
            target="management-sessions",
            result="succeeded",
            request_id=request_id,
        )
        return tuple(
            SessionDevice(
                session_id=row["session_id"],
                auth_method=AuthMethod(row["auth_method"]),
                client_name=row.get("client_name"),
                device_summary=row.get("device_summary"),
                source_address=row.get("source_address"),
                issued_at=self._at(row["issued_at"]),
                last_seen_at=self._at(row["last_seen_at"]),
                expires_at=self._at(row["expires_at"]),
                idle_expires_at=self._at(
                    min(
                        row["expires_at"],
                        row["last_seen_at"] + self._policy.idle_timeout_seconds,
                    )
                ),
            )
            for row in rows
        )

    def revoke_session(
        self, principal: ManagementPrincipal, session_id: str, *, request_id: str,
    ) -> bool:
        """Revoke one active session by its public id; False if none matched."""
        authorize(principal, Capability.WRITE)
        active = {
            row["session_id"]
            for row in self._store.list_active_sessions(
                now=self._clock(),
                idle_timeout_seconds=self._policy.idle_timeout_seconds,
            )
        }
        revoked = session_id in active and self._store.revoke_session(session_id)
        self._store.record_audit(
            actor=principal.subject_id,
            action="session.revoke",
            target=session_id if revoked else "management-session",
            result="succeeded" if revoked else "not-found",
            request_id=request_id,
        )
        return bool(revoked)

    def revoke_others(
        self, principal: ManagementPrincipal, *, request_id: str,
    ) -> int:
        """Revoke every session except the caller's own; returns the count."""
        authorize(principal, Capability.WRITE)
        keep = principal.session_id or ""
        count = self._store.revoke_other_sessions(keep)
        self._store.record_audit(
            actor=principal.subject_id,
            action="session.revoke-others",
            target=keep or "management-sessions",
            result="succeeded",
            request_id=request_id,
        )
        return count

    def revoke_all(self, principal: ManagementPrincipal, *, request_id: str) -> int:
        authorize(principal, Capability.DESTRUCTIVE)
        count = self._store.revoke_all_sessions()
        self._store.record_audit(
            actor=principal.subject_id,
            action="session.revoke-all",
            target="management-sessions",
            result="succeeded",
            request_id=request_id,
        )
        return count
