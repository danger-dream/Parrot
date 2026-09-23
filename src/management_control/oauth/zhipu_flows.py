"""Actor-bound ZCode polling, cancellation and explicit org/project selection."""
from __future__ import annotations

import copy
import hashlib
import hmac
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock, RLock

from src.management_control.errors import ManagementError, ManagementErrorCode as Code
from .models import OAuthLoginFlow, OAuthLoginPoll, OAuthProvider
from .plans import OneShotPlanStore


class ZhipuFlows:
    def __init__(self, backend, *, clock=None):
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = OneShotPlanStore(prefix="zhflow", ttl_seconds=300, clock=self.clock)
        self.known = {}
        self.lock = RLock()
        self.actors = {}

    def _reserve(self, actor):
        # Reserve before IO, never hold a lock during an upstream request.
        with self.lock:
            generation = self.actors.get(actor, 0) + 1
            self.actors[actor] = generation
            for key, record in list(self.known.items()):
                if record["expires_at"] <= self.clock():
                    self.known.pop(key, None)
                elif record["actor"] == actor and not record.get("saving"):
                    record["status"] = "cancelled"
        return generation

    def _register(self, actor, generation, payload):
        with self.lock:
            if self.actors.get(actor) != generation:
                raise ManagementError(Code.INVALID_OPERATION_STATE)
            if len(self.known) >= 512:
                self.known.pop(next(iter(self.known)))
            payload.update(lock=Lock(), last_poll=None)
            flow_id, secret, plan = self.store.create_split(actor_subject_id=actor, kind="zhipu-login", revision="", payload=payload)
            self.known[flow_id] = {"actor": actor, "verifier": hashlib.sha256(secret.encode()).digest(),
                "status": "pending", "expires_at": plan.expires_at}
        return OAuthLoginFlow(flow_id, secret, OAuthProvider.ZHIPU, payload.get("auth_url", ""),
                              "授权后自动保存账户并配置个人项目、专用模型 Key（不存在则创建）、额度及模型。", plan.expires_at)

    def start(self, actor, *, site, callback_mode=False):
        generation = self._reserve(actor)
        payload = (self.backend.zhipu_start_callback_login(site=site) if callback_mode
                   else self.backend.zhipu_start_login(site=site))
        return self._register(actor, generation, payload)

    def projects(self, actor, account_id, account):
        generation = self._reserve(actor)
        choices = self.backend.zhipu_project_choices(account, account_id)
        return self._register(actor, generation, {
            "site": account["site"], "status": "select_project", "choices": choices,
            "credential": copy.deepcopy(account), "source_account_id": account_id,
            "source_account": copy.deepcopy(account),
        })

    def record(self, actor, flow_id, secret):
        with self.lock:
            record = self.known.get(flow_id)
            if not record or record["actor"] != actor or not hmac.compare_digest(record["verifier"], hashlib.sha256(str(secret).encode()).digest()):
                raise ManagementError(Code.INVALID_OPERATION_STATE)
            if record["status"] == "pending" and record["expires_at"] <= self.clock() and not record.get("saving"):
                record["status"] = "expired"
            return record

    def active(self, actor, flow_id, secret):
        if self.record(actor, flow_id, secret)["status"] != "pending":
            raise ManagementError(Code.INVALID_OPERATION_STATE)
        return self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind="zhipu-login")

    @contextmanager
    def lease(self, actor, flow_id, secret):
        plan = self.active(actor, flow_id, secret)
        lock = plan.payload["lock"]
        if not lock.acquire(blocking=False):
            raise ManagementError(Code.STATE_CONFLICT, retryable=True)
        try:
            yield self.active(actor, flow_id, secret)
        finally:
            lock.release()

    @contextmanager
    def saving(self, actor, flow_id, secret):
        with self.lock:
            self.active(actor, flow_id, secret)
            self.known[flow_id]["saving"] = True
        try:
            yield
        finally:
            with self.lock:
                self.known[flow_id].pop("saving", None)

    @staticmethod
    def preview(payload):
        entry = payload.get("entry") or {}
        if entry:
            return {key: entry.get(key) for key in ("site", "label", "subject", "organization_id", "project_id", "plan_scope", "entitlement", "management_status")}
        return {"site": payload.get("site"), "label": (payload.get("credential") or {}).get("label"),
                "choices": copy.deepcopy(payload.get("choices") or [])}

    def poll(self, actor, flow_id, secret):
        record = self.record(actor, flow_id, secret)
        if record["status"] != "pending":
            result = record.get("result")
            return OAuthLoginPoll(flow_id, record["status"], record["expires_at"], record.get("preview"),
                result.account_id if result else None, result.status if result else None, result.revision if result else None)
        with self.lease(actor, flow_id, secret) as plan:
            payload = plan.payload
            now = self.clock().timestamp()
            if payload.get("status") == "pending" and (payload["last_poll"] is None or now - payload["last_poll"] >= payload["interval"]):
                payload["last_poll"] = now
                self.backend.zhipu_poll_login(payload)
                self.active(actor, flow_id, secret)
            return OAuthLoginPoll(flow_id, payload["status"], plan.expires_at, self.preview(payload))

    def submit_callback(self, actor, flow_id, secret, callback_url):
        with self.lease(actor, flow_id, secret) as plan:
            payload = plan.payload
            if payload.get("status") != "awaiting_callback":
                raise ManagementError(Code.INVALID_OPERATION_STATE)
            self.backend.zhipu_accept_callback(payload, callback_url)
            self.active(actor, flow_id, secret)
            return OAuthLoginPoll(flow_id, payload["status"], plan.expires_at, self.preview(payload))

    def select(self, actor, flow_id, secret, organization_id, project_id):
        with self.lease(actor, flow_id, secret) as plan:
            payload = plan.payload
            choice = next((value for value in payload.get("choices") or [] if value["organization_id"] == organization_id and value["project_id"] == project_id), None)
            if payload.get("status") != "select_project" or choice is None:
                raise ManagementError(Code.INVALID_OPERATION_STATE)
            entry = self.backend.zhipu_select_project(copy.deepcopy(payload["credential"]), choice)
            self.active(actor, flow_id, secret)
            payload.update(entry=entry, status="ready")

    def ready(self, plan):
        if plan.payload.get("status") != "ready":
            raise ManagementError(Code.INVALID_OPERATION_STATE)
        return copy.deepcopy(plan.payload["entry"])

    def completed_result(self, actor, flow_id, secret):
        return self.record(actor, flow_id, secret).get("result")

    def finish(self, actor, flow_id, secret, *, completed=False, result=None, preview=None):
        with self.lock:
            record = self.record(actor, flow_id, secret)
            record.update(status="completed" if completed else "cancelled", result=result, preview=preview)
            try:
                self.store.consume_parts(flow_id, secret, actor_subject_id=actor, kind="zhipu-login")
            except ManagementError:
                if not completed:
                    raise

    def cancel(self, actor, flow_id, secret):
        with self.lock:
            record = self.record(actor, flow_id, secret)
            if record.get("saving"):
                raise ManagementError(Code.STATE_CONFLICT)
            if record["status"] in {"cancelled", "expired"}:
                return
            self.finish(actor, flow_id, secret)
