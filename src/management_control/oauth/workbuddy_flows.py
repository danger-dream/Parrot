"""Actor-bound reusable WorkBuddy login flows; never consume a pending poll."""
from __future__ import annotations

import copy
import hashlib
import hmac
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock, RLock

from src.management_control.errors import ManagementError, ManagementErrorCode

from .models import OAuthLoginFlow, OAuthLoginPoll, OAuthProvider
from .plans import OneShotPlanStore


class WorkBuddyFlows:
    def __init__(self, backend, *, clock=None):
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = OneShotPlanStore(prefix="wbflow", ttl_seconds=300, clock=self.clock)
        self._known = OrderedDict()
        self._known_lock = RLock()

    def start(self, actor: str, *, realm="cn", client_profile=None, relogin=None) -> OAuthLoginFlow:
        try:
            payload = (self.backend.workbuddy_start_login() if realm == "cn" and client_profile in (None, "cli")
                       else self.backend.workbuddy_start_login(realm=realm, client_profile=client_profile))
        except Exception:
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from None
        payload = dict(payload, lock=Lock(), last_poll=None, relogin=copy.deepcopy(relogin))
        flow_id, secret, plan = self.store.create_split(actor_subject_id=actor, kind="workbuddy-login", revision="", payload=payload)
        with self._known_lock:
            for key, value in list(self._known.items()):
                if self.clock().timestamp() - value["expires_at"].timestamp() > 600:
                    self._known.pop(key, None)
            while len(self._known) >= 512:
                self._known.popitem(last=False)
            self._known[flow_id] = {"actor": actor, "verifier": hashlib.sha256(secret.encode()).digest(),
                                    "expires_at": plan.expires_at, "status": "pending"}
        region = "国际区（Google/GitHub）" if realm == "global" else "中国区 CLI"
        return OAuthLoginFlow(flow_id, secret, OAuthProvider.WORKBUDDY, payload["auth_url"],
                              f"在浏览器完成{region}授权；轮询确认身份后直接保存账户，无需再次确认。", plan.expires_at)

    def _terminal(self, actor, flow_id, secret):
        with self._known_lock:
            record = self._known.get(flow_id)
            digest = hashlib.sha256(str(secret or "").encode()).digest()
            if not record or not hmac.compare_digest(record["verifier"], digest) or record["actor"] != actor:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if record["status"] == "pending" and not record.get("saving") and record["expires_at"] <= self.clock():
                record["status"] = "expired"
                try:
                    self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
                except ManagementError:
                    pass
            if record["status"] != "pending":
                result = record.get("result")
                return OAuthLoginPoll(
                    flow_id, record["status"], record["expires_at"], copy.deepcopy(record.get("preview")),
                    result.account_id if result else None, result.status if result else None,
                    result.revision if result else None,
                )
        return None

    @contextmanager
    def lease(self, actor: str, flow_id: str, secret: str):
        plan = self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
        lock = plan.payload["lock"]
        if not lock.acquire(blocking=False):
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT, retryable=True)
        try:
            # Another actor-bound request may have consumed it before lock acquisition.
            plan = self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
            yield plan
        finally:
            lock.release()

    @contextmanager
    def saving(self, actor, flow_id, secret):
        # Cancellation can interrupt vendor polling, but not a local commit that
        # has already begun. No network wait is made while holding this lock.
        with self._known_lock:
            if self._terminal(actor, flow_id, secret) is not None:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            self._known[flow_id]["saving"] = True
        try:
            yield
        finally:
            with self._known_lock:
                if flow_id in self._known:
                    self._known[flow_id].pop("saving", None)

    @staticmethod
    def preview(payload: dict) -> dict | None:
        entry = payload.get("entry")
        if not entry:
            return None
        return {key: entry.get(key) for key in ("realm", "uid", "enterprise_id", "nickname", "label", "expired")}

    def poll(self, actor: str, flow_id: str, secret: str) -> OAuthLoginPoll:
        terminal = self._terminal(actor, flow_id, secret)
        if terminal is not None:
            return terminal
        with self.lease(actor, flow_id, secret) as plan:
            payload = plan.payload
            now = self.clock().timestamp()
            if payload.get("status") != "ready" and (payload.get("last_poll") is None or now - payload["last_poll"] >= 3):
                payload["last_poll"] = now
                try:
                    self.backend.workbuddy_poll_login(payload)
                except Exception:
                    # Token polling may have succeeded while identity lookup
                    # failed. Preserve that phase and credential candidate for
                    # the next identity-only poll; do not report it as a login.
                    if payload.get("status") != "identity_pending":
                        raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from None
                self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
            return OAuthLoginPoll(flow_id, payload.get("status", "pending"), plan.expires_at, self.preview(payload))

    def ready(self, plan) -> dict:
        if plan.payload.get("status") != "ready" or not plan.payload.get("entry"):
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        if plan.expires_at <= self.clock():
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        return copy.deepcopy(plan.payload["entry"])

    def completed_result(self, actor: str, flow_id: str, secret: str):
        terminal = self._terminal(actor, flow_id, secret)
        if terminal is not None and terminal.status == "completed":
            with self._known_lock:
                return self._known[flow_id]["result"]
        return None

    def finish(self, actor: str, flow_id: str, secret: str, *, completed: bool = False,
               result=None, preview=None) -> None:
        try:
            self.store.consume_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
        except ManagementError:
            # A verified save may outlive the flow TTL while post-save metadata
            # synchronizes. Its success must not be reported as a failed login.
            if not completed:
                raise
        with self._known_lock:
            if flow_id in self._known:
                self._known[flow_id].update(
                    status="completed" if completed else "cancelled",
                    result=result, preview=copy.deepcopy(preview),
                )

    def cancel(self, actor: str, flow_id: str, secret: str) -> None:
        with self._known_lock:
            terminal = self._terminal(actor, flow_id, secret)
            if terminal is not None:
                if terminal.status in {"cancelled", "expired"}:
                    return
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if self._known[flow_id].get("saving"):
                raise ManagementError(ManagementErrorCode.STATE_CONFLICT, retryable=True)
            plan = self.store.consume_parts(flow_id, secret, actor_subject_id=actor, kind="workbuddy-login")
            self._known[flow_id]["status"] = "cancelled"
        # In-flight polling owns its payload until its post-request inspect fails;
        # consuming the store above prevents that result from ever being saved.
        lock = plan.payload["lock"]
        if lock.acquire(blocking=False):
            try:
                plan.payload.clear()
            finally:
                lock.release()
