"""Actor-bound Qoder device preparation, confirmation and authorization."""
from __future__ import annotations

import copy
from contextlib import contextmanager

from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode
from src.oauth import add_context
from src.oauth.qoder import device_profile, device_identity, device_platforms, routes

from .models import OAuthLoginFlow, OAuthLoginPoll, OAuthProvider
from .workbuddy_flows import WorkBuddyFlows

NOTICE = ("最好不要多个账户共用出口，可在设置代理中指定；不同代理名称不等于不同出口。"
          "浏览器授权走浏览器自己的网络，不受 Parrot 代理设置控制。")


class QoderFlows(WorkBuddyFlows):
    kind = "qoder-login"
    prefix = "qdflow"
    # Match the existing add-attempt/input-state lifetime; drafting is part of login.
    ttl_seconds = 600

    def start(self, actor: str, *, realm="global", pat=None, relogin=None, login_method="device") -> OAuthLoginFlow:
        if pat is not None or login_method not in {"pat", "device"} or (login_method == "pat" and realm != "global"):
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED,
                fields=[ErrorField("pat", "PREPARE_FIRST", "先准备并确认设备档案，再提交 PAT")])
        old = (relogin or {}).get("account") or {}
        profile = (device_profile.upgrade(old["qoder_device_profile"]) if old.get("qoder_device_profile")
                   else device_profile.generate(old.get("machine_id"), platform=device_platforms.LOGIN_DEFAULT))
        device_profile.validate(profile)
        payload = {"realm": realm, "status": "preparing", "profile": profile, "login_method": login_method,
                   "profile_revision": device_profile.fingerprint(profile), "bound": bool(old.get("qoder_device_profile"))}
        flow_id, secret, plan = self._register(actor, payload, relogin)
        return self._view(flow_id, secret, plan)

    @contextmanager
    def lease(self, actor, flow_id, secret):
        with super().lease(actor, flow_id, secret) as plan:
            relogin = plan.payload.get("relogin")
            if relogin:
                # Relogin uses the existing account route, not the add-page selection.
                with routes.account_scope(relogin["target_key"]):
                    yield plan
            else:
                yield plan

    @contextmanager
    def saving(self, actor, flow_id, secret):
        with super().saving(actor, flow_id, secret):
            plan = self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind=self.kind)
            # Relogin routing is read-only. Do not republish an inherited route or
            # a group expansion as a new account rule while replacing credentials.
            with add_context.bind(None if plan.payload.get("relogin") else add_context.current()):
                yield

    def _view(self, flow_id, secret, plan):
        payload = plan.payload
        preview = device_profile.summary({"qoder_device_profile": payload["profile"]})
        preview["migration"] = bool((payload.get("relogin") or {}).get("account", {}).get("qoder_device_profile", {}).get("version") == device_profile.LEGACY_VERSION)
        preview.update(revision=payload["profile_revision"], phase=payload["status"],
                       platform_choices=device_platforms.choices(),
                       can_regenerate=payload["status"] == "preparing" and not payload["bound"],
                       proxy=device_identity.route_selection((payload.get("relogin") or {}).get("target_key", "")))
        return OAuthLoginFlow(flow_id, secret, OAuthProvider.QODER, payload.get("auth_url"),
                              NOTICE, plan.expires_at, device_preview=preview)

    def regenerate(self, actor, flow_id, secret, revision, *, platform=None):
        with self.lease(actor, flow_id, secret) as plan:
            p = plan.payload
            if p["status"] != "preparing" or p["bound"] or p["profile_revision"] != revision:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            old = (p.get("relogin") or {}).get("account") or {}
            selected = device_platforms.for_profile(p["profile"]) if platform is None else platform
            try:
                profile = device_profile.generate(old.get("machine_id"), platform=selected)
            except ValueError:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED,
                    fields=[ErrorField("platform", "UNSUPPORTED", "该平台的设备协议尚未适配，未更改档案")]) from None
            p["profile"] = profile
            p["profile_revision"] = device_profile.fingerprint(p["profile"])
            return self._view(flow_id, secret, plan)

    def confirm(self, actor, flow_id, secret, revision, *, pat=None):
        from src.oauth.qoder import QoderError
        with self.lease(actor, flow_id, secret) as plan:
            p = plan.payload
            if p["profile_revision"] != revision:
                raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
            if p["status"] not in {"preparing", "confirmed"} and not (p["login_method"] == "pat" and p["status"] == "identity_pending"):
                return self._view(flow_id, secret, plan)  # Idempotent; never exchange twice.
            p["status"] = "confirmed"  # Old regenerate/confirm buttons cannot replace this profile.
            try:
                if p["login_method"] == "pat":
                    if pat is None:
                        return self._view(flow_id, secret, plan)
                    entry = self.backend.qoder_pat_login(pat, realm=p["realm"], profile=p["profile"], pending=p)
                    p.pop("pat_candidate", None)
                    p.update(entry=entry, status="ready")
                else:
                    if pat is not None:
                        raise ValueError()
                    p.update(self.backend.qoder_start_login(realm=p["realm"], profile=p["profile"]))
                    p["last_poll"] = None
            except ValueError:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED,
                    fields=[ErrorField("pat", "INVALID", "Qoder PAT must start with pt- and is international only")]) from None
            except QoderError as exc:
                if exc.auth_error and exc.stage == "pat_exchange":
                    raise ManagementError(ManagementErrorCode.VALIDATION_FAILED,
                        fields=[ErrorField("pat", "REJECTED", "Qoder refused the PAT")]) from None
                raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from None
            except Exception:
                raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR, retryable=True) from None
            self.store.inspect_parts(flow_id, secret, actor_subject_id=actor, kind=self.kind)
            return self._view(flow_id, secret, plan)

    def _poll_payload(self, payload: dict) -> None:
        if payload.get("login_method") != "pat" and payload.get("status") not in {"preparing", "confirmed"}:
            self.backend.qoder_poll_login(payload)

    def poll(self, actor: str, flow_id: str, secret: str) -> OAuthLoginPoll:
        return self._poll(actor, flow_id, secret)

    @staticmethod
    def preview(payload: dict) -> dict | None:
        entry = payload.get("entry")
        if not entry:
            return None
        return {key: entry.get(key) for key in ("realm", "uid", "email", "nickname", "label", "expired",
                                                 "qoder_auth_method", "qoder_device_chat")}
