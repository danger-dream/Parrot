"""Transport-neutral Qoder views, daily credits and settings."""
from __future__ import annotations

import copy

from src.management_auth.principal import Capability
from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .contracts import revision


class QoderControlMixin:
    def _start_qoder_login(self, context, provider, *, realm=None, pat=None, relogin_account_id=None,
                           login_method="device", proxy_target=None):
        """Device flow on either site, or an international PAT exchanged at start."""
        from .models import OAuthProvider
        if provider is not OAuthProvider.QODER:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        relogin, realm, _profile, _site = self._prepare_login_context(provider, realm=realm, relogin_account_id=relogin_account_id)
        from src.oauth import add_context
        from src.proxy import manager
        if proxy_target is not None:
            manager.init()
            if (relogin or not isinstance(proxy_target, str)
                    or not manager.expand_target(proxy_target)
                    or not all(manager.get_connector(name) is not None for name in manager.expand_target(proxy_target))):
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        attempt = add_context.current() or add_context.AddAttempt(target=proxy_target, provider="qoder")
        with add_context.bind(None if relogin else attempt):
            flow = self._flows.qoder.start(context.actor.subject_id, realm=realm, pat=pat, relogin=relogin,
                                          login_method=login_method)
        self._audit(context, "oauth.login.start", provider.value)
        return flow

    def regenerate_qoder_device(self, context, flow_id, flow_secret, revision, *, platform=None):
        self._require(context, Capability.SECRETS_WRITE)
        return self._flows.qoder.regenerate(context.actor.subject_id, flow_id, flow_secret, revision, platform=platform)

    def confirm_qoder_device(self, context, flow_id, flow_secret, revision, *, pat=None):
        self._require(context, Capability.SECRETS_WRITE)
        flow = self._flows.qoder.confirm(context.actor.subject_id, flow_id, flow_secret, revision, pat=pat)
        self._audit(context, "oauth.qoder.device.confirm", flow_id)
        return flow

    def _qoder_account(self, account_id):
        account = self._account(account_id)
        if self.backend.provider_of(account) != "qoder":
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        return account

    def get_qoder(self, context, account_id):
        self._require(context, Capability.READ)
        self._qoder_account(account_id)
        return self.backend.qoder_snapshot(account_id)

    def refresh_qoder_status_now(self, context, account_id):
        self._require(context, Capability.WRITE)
        self._qoder_account(account_id)
        self._refresh_usage_account(account_id)
        return self.backend.qoder_snapshot(account_id)

    def checkin_qoder_now(self, context, account_id):
        """Claim today's credits; Qoder answers an already-made claim as claimed, so it never doubles."""
        self._require(context, Capability.WRITE)
        account = self._qoder_account(account_id)
        if account.get("qoder_relogin_required") or account.get("disabled_reason") in {"user", "auth_error"}:
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE,
                fields=[ErrorField("qoder", "ACCOUNT_PAUSED", "account_paused")])
        result = self.backend.qoder_checkin(account_id)
        self._audit(context, "oauth.qoder.checkin", account_id, str(result.get("status") or "unknown"))
        return result

    def update_qoder_settings(self, context, account_id, *, auto_checkin, expected_revision=None):
        self._require(context, Capability.WRITE)
        current = copy.deepcopy(self._qoder_account(account_id))
        if type(auto_checkin) is not bool:
            raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
        if expected_revision and revision(current) != expected_revision:
            raise ManagementError(ManagementErrorCode.REVISION_CONFLICT)
        result = self.backend.qoder_update_settings(account_id, current, auto_checkin)
        self._raise_conditional_status(result)
        self._audit(context, "oauth.qoder.settings", account_id)
        return {"auto_checkin": auto_checkin, "timezone": "Asia/Shanghai",
                "revision": revision(self._account(account_id))}
