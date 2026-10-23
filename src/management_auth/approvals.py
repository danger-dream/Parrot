"""One-time, browser-bound Telegram management login approvals."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol

from .sessions import AuthenticationRateLimited, AuthenticationRateLimiter
from .store import ManagementStateStore, StoreCapacityError


# Unambiguous uppercase alphabet: no 0/1/O/I so the code reads back reliably.
CONFIRM_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CONFIRM_CODE_LENGTH = 4


def new_confirm_code() -> str:
    return "".join(secrets.choice(CONFIRM_CODE_ALPHABET) for _ in range(CONFIRM_CODE_LENGTH))


class ApprovalError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class ApprovalNotification:
    approval_id: str
    requested_at: datetime
    expires_at: datetime
    client_name: str
    source_address: str
    device_summary: str | None
    approve_callback: str
    deny_callback: str
    # Shown both to the browser and in the Telegram message so the approver
    # can confirm the request really originates from their own screen.
    confirm_code: str = ""


class ApprovalNotifier(Protocol):
    def send(self, admin_ids: tuple[int, ...], notification: ApprovalNotification) -> bool: ...


@dataclass(frozen=True, slots=True)
class IssuedApproval:
    approval_id: str
    exchange_secret: str
    expires_at: datetime
    poll_after_seconds: int = 2
    confirm_code: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: str
    status: ApprovalStatus
    expires_at: datetime
    poll_after_seconds: int = 2


class ApprovalService:
    def __init__(
        self,
        store: ManagementStateStore,
        *,
        clock: Callable[[], float],
        ttl_seconds: int,
        admin_ids_provider: Callable[[], tuple[int, ...]],
        telegram_configured_provider: Callable[[], bool],
        notifier: ApprovalNotifier,
        rate_limit_window_seconds: int = 60,
        rate_limit_per_source: int = 3,
        rate_limit_global: int = 30,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ttl = ttl_seconds
        self._admin_ids_provider = admin_ids_provider
        self._telegram_configured_provider = telegram_configured_provider
        self._notifier = notifier
        self._rate_limiter = AuthenticationRateLimiter(
            clock=clock,
            window_seconds=rate_limit_window_seconds,
            per_source=rate_limit_per_source,
            global_limit=rate_limit_global,
        )

    @staticmethod
    def _at(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    @staticmethod
    def _verifier(secret: str) -> bytes:
        return hashlib.sha256(secret.encode("utf-8")).digest()

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl)

    def available(self) -> bool:
        """Telegram is configured and at least one administrator id exists."""
        try:
            return bool(self._configured_admins())
        except Exception:
            return False

    def _configured_admins(self) -> tuple[int, ...]:
        if not self._telegram_configured_provider():
            return ()
        normalized: set[int] = set()
        for value in self._admin_ids_provider():
            try:
                normalized.add(int(value))
            except (TypeError, ValueError):
                continue
        return tuple(sorted(normalized))

    def create(
        self,
        *,
        client_name: str,
        source_address: str,
        device_summary: str | None,
        request_id: str,
    ) -> IssuedApproval:
        admins = self._configured_admins()
        if not admins:
            raise ApprovalError("unavailable")
        try:
            self._rate_limiter.consume(source_address)
        except AuthenticationRateLimited:
            self._store.record_audit(
                actor="anonymous",
                action="telegram-approval.create",
                target="telegram-approval",
                result="rate-limited",
                request_id=request_id,
            )
            raise
        now = self._clock()
        approval_id = f"map_{secrets.token_urlsafe(16)}"
        exchange_secret = f"max_{secrets.token_urlsafe(32)}"
        confirm_code = new_confirm_code()
        expires_at = now + self._ttl
        try:
            self._store.insert_approval(
                {
                    "approval_id": approval_id,
                    "secret_verifier": self._verifier(exchange_secret),
                    "created_at": now,
                    "expires_at": expires_at,
                    "client_name": client_name,
                    "source_address": source_address,
                    "device_summary": device_summary,
                    "request_id": request_id,
                    "confirm_code": confirm_code,
                }
            )
        except StoreCapacityError as exc:
            raise ApprovalError("capacity") from exc
        notification = ApprovalNotification(
            approval_id=approval_id,
            requested_at=self._at(now),
            expires_at=self._at(expires_at),
            client_name=client_name,
            source_address=source_address,
            device_summary=device_summary,
            approve_callback=f"mauth:a:{approval_id}",
            deny_callback=f"mauth:d:{approval_id}",
            confirm_code=confirm_code,
        )
        if len(notification.approve_callback.encode("utf-8")) > 64:
            self._store.deny_pending_approval(approval_id)
            raise ApprovalError("callbackTooLong")
        try:
            sent = self._notifier.send(admins, notification)
        except Exception as exc:
            self._store.deny_pending_approval(approval_id)
            raise ApprovalError("unavailable") from exc
        if not sent:
            self._store.deny_pending_approval(approval_id)
            raise ApprovalError("unavailable")
        self._store.record_audit(
            actor="anonymous",
            action="telegram-approval.create",
            target=approval_id,
            result="pending",
            request_id=request_id,
        )
        return IssuedApproval(
            approval_id=approval_id,
            exchange_secret=exchange_secret,
            expires_at=self._at(expires_at),
            confirm_code=confirm_code,
        )

    def get(self, approval_id: str, exchange_secret: str) -> ApprovalView:
        row = self._store.get_approval(approval_id)
        if row is None or not hmac.compare_digest(
            bytes(row["secret_verifier"]), self._verifier(exchange_secret)
        ):
            raise ApprovalError("authenticationFailed")
        return ApprovalView(
            approval_id=approval_id,
            status=ApprovalStatus(row["status"]),
            expires_at=self._at(row["expires_at"]),
        )

    def decide(self, approval_id: str, *, telegram_user_id: int, approved: bool) -> ApprovalStatus:
        admins = self._configured_admins()
        if not admins or int(telegram_user_id) not in admins:
            raise ApprovalError("forbidden")
        status = self._store.decide_approval(
            approval_id,
            approved=approved,
            actor_id=int(telegram_user_id),
        )
        if status == "notFound":
            raise ApprovalError("notFound")
        row = self._store.get_approval(approval_id)
        request_id = str((row or {}).get("request_id") or "telegram-callback")
        self._store.record_audit(
            actor=f"telegram:{int(telegram_user_id)}",
            action="telegram-approval.decide",
            target=approval_id,
            result=status,
            request_id=request_id,
        )
        if status == "alreadyDecided":
            raise ApprovalError("alreadyDecided")
        return ApprovalStatus(status)
