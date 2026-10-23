"""Status tracking for browser-redirect login flows (``oflow_*``).

Claude, OpenAI, xAI (Grok), Antigravity and Cursor logins finish by submitting
the callback/code (or ``completed=true`` for Cursor) to ``complete``. Their
one-shot plan is consumed at that moment, so poll/cancel need a small,
actor- and secret-bound status record that outlives the plan. The record
never holds credentials; it only mirrors the lifecycle:

``pending`` → (code accepted) ``confirmed`` → ``completed`` | ``failed``
``pending`` → ``cancelled`` | ``expired``
"""

from __future__ import annotations

import hashlib
import hmac
from collections import OrderedDict
from datetime import datetime, timedelta
from threading import RLock

from src.management_control.errors import ManagementError, ManagementErrorCode

from .models import OAuthLoginPoll


PREFIX = "oflow_"
_RETAIN_AFTER_EXPIRY = timedelta(minutes=10)
_MAX_RECORDS = 512


class BrowserFlowTracker:
    def __init__(self, store, *, clock, kind_for) -> None:
        self._store = store
        self._clock = clock
        self._kind_for = kind_for  # provider value -> plan kind
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def owns(flow_id: str) -> bool:
        return str(flow_id or "").startswith(PREFIX)

    def register(self, actor: str, flow_id: str, secret: str, provider: str, expires_at: datetime) -> None:
        now = self._clock()
        with self._lock:
            for key, value in list(self._records.items()):
                if value["expires_at"] + _RETAIN_AFTER_EXPIRY <= now:
                    self._records.pop(key, None)
            while len(self._records) >= _MAX_RECORDS:
                self._records.popitem(last=False)
            self._records[flow_id] = {
                "actor": actor,
                "verifier": hashlib.sha256(str(secret).encode()).digest(),
                "expires_at": expires_at,
                "provider": provider,
                "status": "pending",
                "result": None,
            }

    def _record(self, actor: str, flow_id: str, secret: str) -> dict:
        record = self._records.get(flow_id)
        digest = hashlib.sha256(str(secret or "").encode()).digest()
        expected = record["verifier"] if record else bytes(len(digest))
        if not hmac.compare_digest(expected, digest) or record is None or record["actor"] != actor:
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        if record["status"] == "pending" and record["expires_at"] <= self._clock():
            record["status"] = "expired"
        return record

    def mark(self, flow_id: str, status: str, result=None) -> None:
        with self._lock:
            record = self._records.get(flow_id)
            if record is None or record["status"] in {"completed", "cancelled"}:
                return
            record["status"] = status
            if result is not None:
                record["result"] = result

    def status_of(self, flow_id: str) -> str | None:
        with self._lock:
            record = self._records.get(flow_id)
            return record["status"] if record else None

    def poll(self, actor: str, flow_id: str, secret: str) -> OAuthLoginPoll:
        with self._lock:
            record = self._record(actor, flow_id, secret)
            result = record.get("result")
            return OAuthLoginPoll(
                flow_id, record["status"], record["expires_at"], None,
                result.account_id if result else None,
                result.status if result else None,
                result.revision if result else None,
            )

    def cancel(self, actor: str, flow_id: str, secret: str) -> None:
        with self._lock:
            record = self._record(actor, flow_id, secret)
            status = record["status"]
            if status in {"cancelled", "expired", "failed"}:
                return
            if status == "completed":
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if status == "pending":
                try:
                    self._store.consume_parts(
                        flow_id, secret, actor_subject_id=actor,
                        kind=self._kind_for(record["provider"]),
                    )
                except ManagementError:
                    pass  # Already expired/consumed: cancellation is still final.
            record["status"] = "cancelled"
