"""Transport-neutral helpers shared by this work package's controls."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Iterable, TypeVar

from src.management_auth.policy import CapabilityDenied, authorize
from src.management_auth.principal import AuthMethod, Capability, ManagementPrincipal
from src.management_control.context import AuditSink, ManagementContext, audit_record
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode


ItemT = TypeVar("ItemT")


class ModelKind(str, Enum):
    CHAT = "chat"
    IMAGE = "image"
    VIDEO = "video"


class ModelSourceType(str, Enum):
    GLOBAL = "global"
    OAUTH = "oauth"
    API = "api"


@dataclass(frozen=True, slots=True)
class ModelOwnerRef:
    type: ModelSourceType
    id: str | None = None


@dataclass(frozen=True, slots=True)
class ListPage(Generic[ItemT]):
    items: tuple[ItemT, ...]
    page: int
    page_size: int
    total: int
    revision: str

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


def stable_revision(value: Any) -> str:
    def default(item: Any) -> Any:
        if hasattr(item, "__dict__"):
            return vars(item)
        if isinstance(item, Enum):
            return item.value
        return str(item)

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
    ).encode("utf-8")
    return "rev_" + hashlib.sha256(payload).hexdigest()[:24]


class DomainControl:
    """Common capability, audit, revision and Telegram-context behavior."""

    _idempotency_lock = threading.RLock()
    _idempotency: OrderedDict[tuple[str, str, str], tuple[str, str]] = OrderedDict()
    _idempotency_limit = 512

    def __init__(self, *, audit_sink: AuditSink | None = None) -> None:
        self._audit_sink = audit_sink
        self._local = threading.local()

    def bind_telegram_actor(self, chat_id: int) -> ManagementContext:
        context = ManagementContext(
            request_id=f"telegram:{chat_id}",
            actor=ManagementPrincipal.administrator(
                subject_id=f"telegram:{chat_id}",
                auth_method=AuthMethod.TELEGRAM_ADMIN,
                issued_at=datetime.now(timezone.utc),
            ),
        )
        self._local.context = context
        return context

    def current_context(self) -> ManagementContext:
        context = getattr(self._local, "context", None)
        if isinstance(context, ManagementContext):
            return context
        # Compatibility for direct renderer/helper tests. Production Telegram
        # entry points bind the actual chat actor before reaching a use case.
        return ManagementContext(
            request_id="telegram:internal",
            actor=ManagementPrincipal.administrator(
                subject_id="telegram:internal",
                auth_method=AuthMethod.TELEGRAM_ADMIN,
                issued_at=datetime.now(timezone.utc),
            ),
        )

    def _context(self, context: ManagementContext | None) -> ManagementContext:
        return context or self.current_context()

    @staticmethod
    def _authorize(context: ManagementContext, capability: Capability) -> None:
        try:
            authorize(context.actor, capability)
        except CapabilityDenied as exc:
            raise ManagementError(ManagementErrorCode.CAPABILITY_DENIED) from exc

    def _read(self, context: ManagementContext | None) -> ManagementContext:
        actual = self._context(context)
        self._authorize(actual, Capability.READ)
        return actual

    def _write(
        self,
        context: ManagementContext | None,
        capability: Capability = Capability.WRITE,
    ) -> ManagementContext:
        actual = self._context(context)
        self._authorize(actual, capability)
        return actual

    def _audit(self, context: ManagementContext, action: str, target: str, result: str) -> None:
        if self._audit_sink is not None:
            self._audit_sink.record(
                audit_record(context, action=action, target=target, result=result)
            )

    @staticmethod
    def _check_revision(
        expected: str | None,
        current: str,
        *,
        required: bool = False,
    ) -> None:
        if required and not expected:
            raise ManagementError(
                ManagementErrorCode.CONFIRMATION_REQUIRED,
                "If-Match is required for this mutation",
            )
        if expected is not None and expected != current:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)

    @classmethod
    def _idempotent_replay(
        cls,
        context: ManagementContext,
        *,
        action: str,
        fingerprint: str,
        operation_store: Any,
    ) -> Any | None:
        key_value = context.idempotency_key
        if not key_value:
            return None
        ledger_key = (context.actor.session_id or context.actor.subject_id, key_value, action)
        with cls._idempotency_lock:
            current = cls._idempotency.get(ledger_key)
        if current is None:
            return None
        known_fingerprint, operation_id = current
        if known_fingerprint != fingerprint:
            raise ManagementError(
                ManagementErrorCode.STATE_CONFLICT,
                "Idempotency key was already used for a different request",
            )
        return operation_store.get(context, operation_id)

    @classmethod
    def _remember_idempotency(
        cls,
        context: ManagementContext,
        *,
        action: str,
        fingerprint: str,
        operation_id: str,
    ) -> None:
        key_value = context.idempotency_key
        if not key_value:
            return
        ledger_key = (context.actor.session_id or context.actor.subject_id, key_value, action)
        with cls._idempotency_lock:
            cls._idempotency[ledger_key] = (fingerprint, operation_id)
            cls._idempotency.move_to_end(ledger_key)
            while len(cls._idempotency) > cls._idempotency_limit:
                cls._idempotency.popitem(last=False)

    @staticmethod
    def _validation(path: str, code: str, message: str) -> ManagementError:
        return ManagementError(
            ManagementErrorCode.VALIDATION_FAILED,
            fields=(ErrorField(path=path, code=code, message=message),),
        )

    @staticmethod
    def _paginate(
        items: Iterable[ItemT], page: int, page_size: int, *, revision: str,
    ) -> ListPage[ItemT]:
        values = tuple(items)
        start = (page - 1) * page_size
        return ListPage(
            items=values[start:start + page_size],
            page=page,
            page_size=page_size,
            total=len(values),
            revision=revision,
        )
