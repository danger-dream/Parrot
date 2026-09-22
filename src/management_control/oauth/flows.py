"""Provider login and credential conversion for :class:`OAuthControl`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from src.management_control.errors import ErrorField, ManagementError, ManagementErrorCode

from .backend import OAuthBackend
from .models import (
    CompleteOAuthLoginCommand,
    JsonCredential,
    ManualCredential,
    OAuthCredential,
    OAuthLoginFlow,
    OAuthProvider,
    RefreshTokenCredential,
)
from .plans import OneShotPlanStore
from .workbuddy_flows import WorkBuddyFlows


@dataclass(frozen=True, slots=True)
class CompletedCredential:
    entry: dict
    source: str


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_expiry(token: dict, now: datetime, default_seconds: int) -> str:
    return _utc_text(now + timedelta(seconds=int(token.get("expires_in", default_seconds) or default_seconds)))


def _extract_code_state(value: str | None) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        return query.get("code", [""])[0].strip(), query.get("state", [""])[0].strip()
    if "=" in raw and "code" in raw:
        query = parse_qs(raw.lstrip("?"))
        code = query.get("code", [""])[0].strip()
        if code:
            return code, query.get("state", [""])[0].strip()
    code, separator, state = raw.partition("#")
    return code.strip(), state.strip() if separator else ""


class OAuthFlowService:
    def __init__(
        self,
        backend: OAuthBackend,
        *,
        clock=None,
        flow_store: OneShotPlanStore[dict] | None = None,
    ) -> None:
        self.backend = backend
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.workbuddy = WorkBuddyFlows(backend, clock=self._clock)
        self._flows = flow_store or OneShotPlanStore(
            prefix="oflow", ttl_seconds=1800, clock=self._clock,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("OAuth clock must be timezone-aware")
        return value

    def start(self, actor_subject_id: str, provider: OAuthProvider, *, realm="cn", client_profile=None) -> OAuthLoginFlow:
        if provider is OAuthProvider.WORKBUDDY:
            return self.workbuddy.start(actor_subject_id, realm=realm, client_profile=client_profile)
        payload: dict = {"provider": provider.value}
        instruction: str | None = None
        auth_url: str | None
        if provider is OAuthProvider.CLAUDE:
            verifier, challenge = self.backend.claude_pkce_generate()
            state = secrets.token_urlsafe(32)
            auth_url = self.backend.claude_build_login_url(challenge, state)
            payload.update(verifier=verifier, state=state)
        elif provider is OAuthProvider.OPENAI:
            verifier, challenge = self.backend.openai_pkce_generate()
            state = secrets.token_urlsafe(32)
            auth_url = self.backend.openai_build_login_url(challenge, state)
            payload.update(verifier=verifier, state=state)
        elif provider is OAuthProvider.XAI:
            verifier, challenge = self.backend.xai_pkce_generate()
            state = secrets.token_urlsafe(32)
            discovery = self.backend.xai_discover()
            authorization_endpoint = discovery.get("authorization_endpoint") or self.backend.xai_authorization_url()
            payload.update(
                verifier=verifier,
                state=state,
                token_endpoint=discovery.get("token_endpoint") or self.backend.xai_token_url(),
                redirect_uri=self.backend.xai_redirect_uri(),
            )
            auth_url = self.backend.xai_build_login_url(
                challenge, state, authorization_endpoint=authorization_endpoint,
            )
        elif provider is OAuthProvider.ANTIGRAVITY:
            state = secrets.token_urlsafe(32)
            auth_url = self.backend.antigravity_build_login_url(state)
            payload.update(
                state=state,
                token_endpoint=self.backend.antigravity_token_url(),
                redirect_uri=self.backend.antigravity_redirect_uri(),
            )
        else:
            params = self.backend.cursor_generate_login()
            auth_url = params.login_url
            payload.update(uuid=params.uuid, verifier=params.verifier)
            instruction = "Complete browser login, then submit completed=true."
        flow_id, flow_secret, plan = self._flows.create_split(
            actor_subject_id=actor_subject_id,
            kind=f"login:{provider.value}",
            revision="",
            payload=payload,
        )
        return OAuthLoginFlow(
            flow_id=flow_id,
            flow_secret=flow_secret,
            provider=provider,
            auth_url=auth_url,
            instruction=instruction,
            expires_at=plan.expires_at,
        )

    def complete(
        self,
        actor_subject_id: str,
        flow_id: str,
        flow_secret: str,
        command: CompleteOAuthLoginCommand,
    ) -> CompletedCredential:
        # The provider is encoded in stored plan kind, not trusted from input.
        candidates = [provider for provider in OAuthProvider]
        plan = None
        provider = None
        for item in candidates:
            try:
                plan = self._flows.inspect_parts(
                    flow_id,
                    flow_secret,
                    actor_subject_id=actor_subject_id,
                    kind=f"login:{item.value}",
                )
                provider = item
                break
            except ManagementError:
                continue
        if plan is None or provider is None:
            raise ManagementError(ManagementErrorCode.INVALID_OPERATION_STATE)
        payload = plan.payload
        self._validate_submission(provider, payload, command)
        # Atomic reservation follows side-effect-free validation and precedes
        # provider exchange. Exactly one concurrent completer can proceed.
        self._flows.consume_parts(
            flow_id,
            flow_secret,
            actor_subject_id=actor_subject_id,
            kind=f"login:{provider.value}",
        )
        try:
            entry = self._complete_provider(provider, payload, command)
        except ManagementError:
            raise
        except Exception as exc:
            raise ManagementError(
                ManagementErrorCode.UPSTREAM_ERROR,
                retryable=True,
            ) from exc
        return CompletedCredential(entry=entry, source=f"{provider.value} login")

    def _validate_submission(
        self,
        provider: OAuthProvider,
        payload: dict,
        command: CompleteOAuthLoginCommand,
    ) -> None:
        if provider is OAuthProvider.CURSOR:
            if command.completed is not True:
                raise ManagementError(ManagementErrorCode.INVALID_REQUEST)
            return
        source = command.callback_url or command.code
        if provider is OAuthProvider.ANTIGRAVITY:
            parsed = self.backend.antigravity_parse_callback(source or "")
            code, received_state = parsed.get("code") or "", parsed.get("state") or ""
        else:
            if (
                provider is OAuthProvider.OPENAI
                and str(source or "").strip().startswith(("http://", "https://"))
                and not self.backend.openai_validate_callback_url(str(source))
            ):
                raise ManagementError(
                    ManagementErrorCode.VALIDATION_FAILED,
                    fields=[ErrorField("callbackUrl", "INVALID", "Callback redirect does not match this flow")],
                )
            code, received_state = _extract_code_state(source)
            if command.state:
                received_state = command.state
        if not code:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=[ErrorField("code", "REQUIRED", "Authorization code is required")],
            )
        expected_state = str(payload.get("state") or "")
        if expected_state and (
            not received_state or not secrets.compare_digest(received_state, expected_state)
        ):
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)

    def _complete_provider(
        self,
        provider: OAuthProvider,
        payload: dict,
        command: CompleteOAuthLoginCommand,
    ) -> dict:
        now = self._now()
        if provider is OAuthProvider.CURSOR:
            if command.completed is not True:
                raise ManagementError(ManagementErrorCode.INVALID_REQUEST)
            tokens = self.backend.cursor_poll_login(payload.get("uuid", ""), payload.get("verifier", ""))
            subject = self.backend.cursor_subject(tokens.access_token)
            if not subject:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            profile = self.backend.cursor_profile(tokens.access_token, account_key=f"cursor:{subject}")
            email = str(profile.get("email") or "").strip() or self.backend.cursor_label(subject)
            return {
                "email": email,
                "label": email,
                "provider": "cursor",
                "type": "cursor",
                "subject": subject,
                "sub": subject,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expired": _utc_text(datetime.fromtimestamp(tokens.expires_at_ms / 1000, tz=timezone.utc)),
                "last_refresh": _utc_text(now),
                "enabled": True,
                "disabled_reason": None,
                "disabled_until": None,
                "models": [],
                "cursor_profile_name": str(profile.get("name") or "").strip(),
                "cursor_profile_id": str(profile.get("id") or "").strip(),
            }
        code_source = command.callback_url or command.code
        if provider is OAuthProvider.ANTIGRAVITY:
            parsed = self.backend.antigravity_parse_callback(code_source or "")
            code, received_state = parsed.get("code") or "", parsed.get("state") or ""
        else:
            code, received_state = _extract_code_state(code_source)
            if command.state:
                received_state = command.state
        if not code:
            raise ManagementError(
                ManagementErrorCode.VALIDATION_FAILED,
                fields=[ErrorField("code", "REQUIRED", "Authorization code is required")],
            )
        expected_state = str(payload.get("state") or "")
        if expected_state and (
            not received_state or not secrets.compare_digest(received_state, expected_state)
        ):
            raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
        if provider is OAuthProvider.CLAUDE:
            token = self.backend.claude_exchange_code(code, payload.get("verifier", ""), expected_state)
            profile = asyncio.run(self.backend.claude_fetch_profile(token.get("access_token", "")))
            email = str((profile.get("account") or {}).get("email") or "").strip()
            if not email:
                email = f"unnamed-{secrets.token_hex(8)}@local"
            return {
                "email": email,
                "provider": "claude",
                "type": "claude",
                "access_token": token.get("access_token", ""),
                "refresh_token": token.get("refresh_token", ""),
                "expired": _token_expiry(token, now, 28800),
                "last_refresh": _utc_text(now),
                "enabled": True,
                "disabled_reason": None,
                "disabled_until": None,
                "models": [],
                "scopes": token.get("scope", "") or "",
                **self.backend.claude_extract_plan(profile),
            }
        if provider is OAuthProvider.OPENAI:
            token = self.backend.openai_exchange_code(code, payload.get("verifier", ""))
            return self.openai_entry(token, now=now)
        if provider is OAuthProvider.XAI:
            token = self.backend.xai_exchange_code(
                code,
                payload.get("verifier", ""),
                redirect_uri=payload.get("redirect_uri") or self.backend.xai_redirect_uri(),
                token_endpoint=payload.get("token_endpoint") or self.backend.xai_token_url(),
            )
            return self.xai_entry(token, now=now)
        token = self.backend.antigravity_complete_login(
            code,
            redirect_uri=payload.get("redirect_uri") or self.backend.antigravity_redirect_uri(),
            token_endpoint=payload.get("token_endpoint") or self.backend.antigravity_token_url(),
        )
        return self.antigravity_entry(token, now=now)

    def credential_entry(self, credential: OAuthCredential) -> dict:
        if credential.provider is OAuthProvider.WORKBUDDY:
            raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)
        now = self._now()
        if isinstance(credential, ManualCredential):
            entry = {
                "email": credential.email.strip(),
                "provider": credential.provider.value,
                "type": credential.provider.value,
                "access_token": credential.access_token,
                "refresh_token": credential.refresh_token,
                "expired": credential.expires_at or "",
                "last_refresh": _utc_text(now),
                "enabled": True,
                "disabled_reason": None,
                "disabled_until": None,
                "models": [],
            }
            if credential.display_name:
                entry["label"] = credential.display_name
            if credential.identity_subject:
                entry.update(subject=credential.identity_subject, sub=credential.identity_subject)
            if credential.workspace_id:
                entry.update(workspace_id=credential.workspace_id, chatgpt_account_id=credential.workspace_id)
            if credential.project_id:
                entry["project_id"] = credential.project_id
            return entry
        if isinstance(credential, JsonCredential):
            try:
                value = json.loads(credential.payload)
            except Exception as exc:
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED) from exc
            if not isinstance(value, dict):
                raise ManagementError(ManagementErrorCode.VALIDATION_FAILED)
            return self.credential_entry(
                ManualCredential(
                    provider=credential.provider,
                    email=str(value.get("email") or ""),
                    access_token=str(value.get("access_token") or ""),
                    refresh_token=str(value.get("refresh_token") or ""),
                    display_name=value.get("label"),
                    identity_subject=value.get("subject") or value.get("sub"),
                    workspace_id=value.get("workspace_id") or value.get("chatgpt_account_id"),
                    project_id=value.get("project_id") or value.get("projectId"),
                    expires_at=value.get("expired"),
                )
            )
        if credential.provider is OAuthProvider.OPENAI:
            token = self.backend.openai_refresh(credential.refresh_token, email=credential.email_hint or None)
            token.setdefault("refresh_token", credential.refresh_token)
            return self.openai_entry(token, fallback_email=credential.email_hint or "", now=now)
        if credential.provider is OAuthProvider.XAI:
            token = self.backend.xai_refresh(credential.refresh_token)
            token.setdefault("refresh_token", credential.refresh_token)
            return self.xai_entry(token, fallback_email=credential.email_hint or "", now=now)
        raise ManagementError(ManagementErrorCode.UNSUPPORTED_VALUE)

    def openai_entry(self, token: dict, *, fallback_email: str = "", now: datetime | None = None) -> dict:
        now = now or self._now()
        id_token = str(token.get("id_token") or "")
        if not id_token:
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR)
        info = self.backend.openai_extract_user_info(self.backend.openai_decode_id_token(id_token))
        email = str(info.get("email") or token.get("email") or fallback_email or "")
        if not email:
            email = f"unnamed-openai-{int(now.timestamp())}@local"
        workspace_id = token.get("workspace_id") or info.get("workspace_id") or info.get("chatgpt_account_id", "")
        return {
            "email": email,
            "provider": "openai",
            "type": "openai",
            "access_token": token.get("access_token", ""),
            "refresh_token": token.get("refresh_token", ""),
            "expired": _token_expiry(token, now, 28800),
            "last_refresh": _utc_text(now),
            "enabled": True,
            "disabled_reason": None,
            "disabled_until": None,
            "models": [],
            "id_token": id_token,
            "chatgpt_account_id": token.get("chatgpt_account_id") or workspace_id,
            "workspace_id": workspace_id,
            "workspace_name": token.get("workspace_name") or info.get("workspace_name", ""),
            "workspace_type": token.get("workspace_type") or info.get("workspace_type", ""),
            "organization_id": token.get("organization_id") or info.get("organization_id", ""),
            "plan_type": token.get("plan_type") or info.get("plan_type", ""),
            "subscription_expires_at": token.get("subscription_expires_at", ""),
        }

    def xai_entry(self, token: dict, *, fallback_email: str = "", now: datetime | None = None) -> dict:
        now = now or self._now()
        id_token = str(token.get("id_token") or "")
        info = self.backend.xai_extract_user_info(self.backend.xai_decode_id_token(id_token)) if id_token else {}
        email = str(token.get("email") or info.get("email") or fallback_email or "")
        if not email:
            email = f"unnamed-xai-{int(now.timestamp())}@local"
        subject = str(token.get("subject") or token.get("sub") or info.get("subject") or "")
        return {
            "email": email,
            "provider": "xai",
            "type": "xai",
            "access_token": token.get("access_token", ""),
            "refresh_token": token.get("refresh_token", ""),
            "expired": _token_expiry(token, now, 3600),
            "last_refresh": _utc_text(now),
            "enabled": True,
            "disabled_reason": None,
            "disabled_until": None,
            "models": [],
            "id_token": id_token,
            "subject": subject,
            "sub": subject,
            "base_url": token.get("base_url") or token.get("baseUrl") or self.backend.xai_api_base_url(),
            "token_endpoint": token.get("token_endpoint") or self.backend.xai_token_url(),
            "redirect_uri": token.get("redirect_uri") or self.backend.xai_redirect_uri(),
        }

    def antigravity_entry(self, token: dict, *, now: datetime | None = None) -> dict:
        now = now or self._now()
        email = str(token.get("email") or "").strip()
        project_id = str(token.get("project_id") or token.get("projectId") or "").strip()
        if not email or not project_id:
            raise ManagementError(ManagementErrorCode.UPSTREAM_ERROR)
        return {
            "email": email,
            "provider": "antigravity",
            "type": "antigravity",
            "access_token": token.get("access_token", ""),
            "refresh_token": token.get("refresh_token", ""),
            "expired": _token_expiry(token, now, 3600),
            "last_refresh": _utc_text(now),
            "enabled": True,
            "disabled_reason": None,
            "disabled_until": None,
            "models": [],
            "project_id": project_id,
            "base_url": token.get("base_url") or token.get("baseUrl") or self.backend.antigravity_api_base_url(),
            "token_endpoint": token.get("token_endpoint") or self.backend.antigravity_token_url(),
            "redirect_uri": token.get("redirect_uri") or self.backend.antigravity_redirect_uri(),
        }
