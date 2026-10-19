"""Antigravity / Google Code Assist OAuth channel.

Exposes the account as an OpenAI Responses-family upstream. The request is
translated to Gemini generateContent and wrapped in the Cloud Code envelope
before it leaves the process. Gemini JSON/SSE is restored to standard
Responses by ``AntigravityOAuthAdapter`` before the Responses toolkit.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Optional

from .. import cache_hints, config, media_config, oauth_manager
from ..antigravity import DispatchGuardError
from ..oauth import antigravity as ag_provider
from ..oauth_ids import account_key as _account_key
from ..openai.transform import anthropic_to_responses, chat_to_responses, guard, responses_to_chat
from ..providers import antigravity_codec, remote_image, registry as provider_registry
from .base import Channel, ChannelDisplay, UpstreamRequest, build_dispatch_metadata
from .oauth_helpers import request_api_key_name as _request_api_key_name


def _provider_cfg() -> dict:
    default = config.DEFAULT_CONFIG.get("antigravityOAuth") or {}
    current = config.get().get("antigravityOAuth") or {}
    default = default if isinstance(default, dict) else {}
    current = current if isinstance(current, dict) else {}
    merged = dict(default)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged.get(key) or {})
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


@asynccontextmanager
async def _guarded_stream_context(request, factory, validate):
    """Use the existing raw HTTP context hook, not a request-supplied callback."""
    validate()
    extensions = dict(request.extensions or {})
    trace = extensions.get("trace")
    # This admission flag is NOT dispatch/billing evidence. Native transports
    # invoke the synchronous callback at their real handoff (possibly a worker).
    # Their later trace/connection events are observations, not rejection gates.
    handed_off = threading.Event()

    def native_handoff():
        validate()
        handed_off.set()

    extensions["parrot_before_native_handoff"] = native_handoff

    async def guarded_trace(name, info):
        first_headers = not handed_off.is_set() and name in {
            "http11.send_request_headers.started", "http2.send_request_headers.started",
        }
        if first_headers:
            validate()
        if trace is not None:
            await trace(name, info)
        if first_headers:
            # The timing callback can await dispatch-log persistence itself.
            validate()
            handed_off.set()

    extensions["trace"] = guarded_trace
    ctx = factory(replace(request, extensions=extensions))
    try:
        response = await ctx.__aenter__()
        yield response
    finally:
        await ctx.__aexit__(None, None, None)


def _wants_stream(body: dict) -> bool:
    return bool(body.get("stream"))


def _session_anchor(requested_body: dict, payload: dict) -> str:
    """Same idea as OpenAI session_id / xAI x-grok-conv-id: pin the conversation.

    Prefer the client/prompt cache key over hashing the first user text. The
    first-user fallback changes whenever the opening turn changes, which breaks
    Antigravity session affinity for Claude Code / Responses clients.
    """
    for source in (payload, requested_body):
        if not isinstance(source, dict):
            continue
        key = str(source.get("prompt_cache_key") or "").strip()
        if key:
            return key
        metadata = source.get("metadata")
        if isinstance(metadata, dict):
            for field in ("session_id", "sessionId"):
                value = str(metadata.get(field) or "").strip()
                if value:
                    return value
    return str(cache_hints.anthropic_session_id(requested_body) or "").strip()


class AntigravityOAuthChannel(Channel):
    """provider='antigravity' OAuth account, exposed as openai-responses."""

    type = "oauth"
    provider = "antigravity"
    cc_mimicry = False
    protocol = "openai-responses"
    upstream_stream_only = False

    def __init__(self, account: dict):
        self.email = account["email"]
        self.project_id = str(account.get("project_id") or account.get("projectId") or "").strip()
        if not self.project_id:
            raise ValueError("AntigravityOAuthChannel requires project_id")
        self.account_key = _account_key(account)
        self.key = f"oauth:{self.account_key}"
        self.display_name = self.email
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0

        cfg = _provider_cfg()
        fingerprint = cfg.get("tlsFingerprint") or {}
        if not isinstance(fingerprint, dict):
            fingerprint = {}
        if bool(fingerprint.get("enabled", False)):
            profile = str(fingerprint.get("profile") or ag_provider.DEFAULT_TLS_PROFILE).strip()
            self.tls_fingerprint = profile or None
        else:
            self.tls_fingerprint = None
        self.base_url = ag_provider.effective_request_base_url(account)

        selected_models = list(account.get("models") or [])
        disabled_models = {
            str(model).strip() for model in account.get("disabledModels") or []
            if str(model).strip()
        }
        from ..image_catalog import is_image_name
        self.models = [model for model in selected_models
                       if model not in disabled_models and not is_image_name(model)
                       and ag_provider.model_at_generation_endpoint(account, model)]
        self.state_key = oauth_manager.account_state_key(account)
        self.image_models = media_config.account_models(account, 'image')

    def supports_model(self, requested_model: str) -> Optional[str]:
        if requested_model in self.models:
            return requested_model
        return None

    def _selected_request_models(self) -> list[str]:
        live = oauth_manager.get_account(self.account_key)
        if not live:
            return list(self.models)
        if ag_provider.effective_request_base_url(live) != self.base_url:
            return []
        return [model for model in self.models
                if model in live.get("models", [])
                and model not in (live.get("disabledModels") or [])
                and ag_provider.model_at_generation_endpoint(live, model)]

    def validate_dispatch(self, resolved_model: str, requested_models: tuple[str, ...]) -> None:
        """Recheck this prepared route after awaits; never retarget the request."""
        from .. import channel_state, model_state
        if channel_state.is_deleted(self.state_key):
            raise DispatchGuardError(400, "invalid_request_error", "Antigravity account generation was removed", scope="candidate")
        live = oauth_manager.get_account(self.account_key)
        if live:
            if oauth_manager.account_state_key(live) != self.state_key:
                raise DispatchGuardError(400, "invalid_request_error", "Antigravity account generation changed", scope="candidate")
            if ag_provider.effective_request_base_url(live) != self.base_url or not ag_provider.model_at_generation_endpoint(live, resolved_model):
                raise DispatchGuardError(400, "invalid_request_error", "Antigravity generation endpoint changed; refresh its model catalog", param="model", scope="candidate")
            if resolved_model not in live.get("models", []) or resolved_model in (live.get("disabledModels") or []):
                raise DispatchGuardError(400, "invalid_request_error", "Antigravity model is no longer selected or enabled", param="model", scope="candidate")
        for model in {*requested_models, resolved_model}:
            if model and (not model_state.is_global_enabled(model, source_key=self.key)
                          or not model_state.is_source_enabled(self.key, model)):
                raise DispatchGuardError(400, "invalid_request_error", "Model is disabled for this source", param="model", scope="candidate")

    def resolve_request_model(self, requested_model: str, body: dict, ingress: str) -> Optional[str]:
        from ..antigravity import argon
        return argon.resolve(requested_model, self._selected_request_models(), body, ingress)

    def list_client_models(self) -> list[str]:
        return list(self.models)

    def supports_media_model(self, kind: str, requested_model: str) -> bool:
        """Return whether this OAuth account can serve one configured image model."""
        from .. import channel_state, model_state
        if kind != 'image' or channel_state.is_deleted(self.state_key):
            return False
        try:
            account = oauth_manager.get_account(self.account_key)
        except Exception:
            return False
        if not isinstance(account, dict):
            return False
        return (
            oauth_manager.account_state_key(account) == self.state_key
            and media_config.oauth_state(account, 'image')['enabled']
            and requested_model in media_config.account_models(account, 'image')
            and model_state.is_global_enabled(requested_model, source_key=self.key)
            and model_state.is_source_enabled(self.key, requested_model)
        )

    async def build_media_headers(self) -> dict[str, str]:
        """Reuse the existing OAuth lifecycle for JSON image generation."""
        access_token = await oauth_manager.ensure_channel_token(self)
        return self._build_headers(access_token, stream=False)

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "responses",
    ) -> UpstreamRequest:
        if ingress_protocol not in ("anthropic", "chat", "responses"):
            raise ValueError(
                "AntigravityOAuthChannel only serves anthropic / openai-chat / "
                f"openai-responses ingress; got {ingress_protocol!r}."
            )

        from ..antigravity import argon
        # body.model remains the trusted post-ingress-mapping request identity;
        # only the response/pricing identity is rewritten for a bare Argon tier.
        requested_models = tuple(str(requested_body.get(key) or "").strip()
                                 for key in ("model", "_client_visible_model"))
        def validate():
            self.validate_dispatch(resolved_model, requested_models)
        validate()
        argon_config = None
        if argon.is_argon(resolved_model):
            selected = self._selected_request_models()
            expected = argon.resolve(str(requested_body.get("model") or resolved_model), selected, requested_body, ingress_protocol)
            if expected != resolved_model:
                raise guard.GuardError(400, "invalid_request_error", "Argon must resolve to an enabled exact model before dispatch", param="model", scope="candidate")
            argon_config = argon.thinking_config(resolved_model, requested_body, ingress_protocol)
        stream = _wants_stream(requested_body)
        # Gemini inlineData cannot consume ordinary remote URLs.  Resolve them at
        # this provider boundary so other bridges retain their existing policy.
        if ingress_protocol in ("chat", "responses"):
            requested_body = await remote_image.prepare_antigravity_images(requested_body)
        translator_ctx: Optional[dict]
        namespace_tool_map = None
        local_store_context = None
        if ingress_protocol == "responses":
            prepared, namespace_tool_map = antigravity_codec.prepare_responses_request(
                requested_body, api_key_name=_request_api_key_name(requested_body),
            )
            # Locally expanded history may contain images from another route;
            # apply the same provider image conversion as to current input.
            prepared = await remote_image.prepare_antigravity_images(prepared)
            local_store_context = {
                "api_key_name": _request_api_key_name(requested_body),
                "channel_key": self.key,
                "parent_id": requested_body.get("previous_response_id"),
                "current_input_items": responses_to_chat.resolve_current_input_items(requested_body),
            }
            payload = provider_registry.filter_request_payload(
                self, prepared, protocol="openai-responses",
            )
            translator_ctx = {
                "ingress": "responses",
                "upstream_protocol": "openai-responses",
                "model_for_response": resolved_model,
            }
        elif ingress_protocol == "chat":
            guard.guard_chat_to_responses(requested_body)
            payload = chat_to_responses.translate_request(requested_body)
            stream_opts = requested_body.get("stream_options") or {}
            include_usage = bool(stream_opts.get("include_usage")) if isinstance(stream_opts, dict) else False
            translator_ctx = {
                "ingress": "chat",
                "upstream_protocol": "openai-responses",
                "response_translator": "chat_to_responses",
                "model_for_response": resolved_model,
                "include_usage": include_usage,
            }
        else:
            payload = anthropic_to_responses.translate_request(
                requested_body,
                target_model=resolved_model,
                codex_oauth=False,
                allow_reasoning_effort=True,
            )
            cache_hints.apply_anthropic_cache_to_openai_payload(
                requested_body,
                payload,
                model=resolved_model,
                api_key_name=_request_api_key_name(requested_body),
                client_ip=requested_body.get("_parrot_client_ip"),
            )
            translator_ctx = {
                "ingress": "anthropic",
                "upstream_protocol": "openai-responses",
                "response_translator": "anthropic_to_responses",
                "model_for_response": resolved_model,
                "request_body": requested_body,
                # Provider-scoped opt-in: no other Responses upstream may expose
                # opaque reasoning material as Anthropic thinking blocks.
                "antigravity_anthropic_reasoning_bridge": True,
            }

        payload["model"] = resolved_model
        if argon_config is not None:
            # Override bridge-derived defaults, never the selected wire model.
            payload["reasoning"] = {"summary": "auto" if argon_config["includeThoughts"] else "none"}
            if argon_config.get("thinkingLevel"):
                payload["reasoning"]["effort"] = argon_config["thinkingLevel"]
        payload = await remote_image.prepare_antigravity_images(payload, tool_outputs=True)
        payload = provider_registry.filter_request_payload(
            self, payload, protocol="openai-responses",
        )
        gemini = antigravity_codec.responses_to_gemini(payload)
        envelope = antigravity_codec.wrap_cloud_code(
            gemini,
            model=resolved_model,
            project_id=self.project_id,
            stream=stream,
            session_id=_session_anchor(requested_body, payload),
        )
        translator_ctx["antigravity_stream"] = antigravity_codec.GeminiStreamToResponses(
            model=resolved_model,
            request_body=requested_body if ingress_protocol == "responses" else payload,
            namespace_tool_map=namespace_tool_map,
            local_store_context=local_store_context,
        )
        translator_ctx["antigravity_stream_requested"] = stream

        validate()
        access_token = await oauth_manager.ensure_channel_token(self)
        validate()
        headers = self._build_headers(access_token, stream=stream)
        url = (
            f"{self.base_url}/{ag_provider.API_VERSION}:streamGenerateContent?alt=sse"
            if stream
            else f"{self.base_url}/{ag_provider.API_VERSION}:generateContent"
        )
        return UpstreamRequest(
            url=url,
            headers=headers,
            body=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx=translator_ctx,
            dispatch_metadata=build_dispatch_metadata(
                envelope, "openai-responses", headers,
            ),
            stream_context_hook=lambda request, factory: _guarded_stream_context(request, factory, validate),
        )

    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        return chunk

    def display(self) -> ChannelDisplay:
        return ChannelDisplay(
            key=self.key,
            type="oauth",
            display_name=self.display_name,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=list(self.models),
        )

    def _build_headers(self, access_token: str, *, stream: bool) -> dict[str, str]:
        return {
            "authorization": f"Bearer {access_token}",
            "accept": "text/event-stream" if stream else "application/json",
            "content-type": "application/json",
            "user-agent": str(_provider_cfg().get("userAgent") or ag_provider.request_user_agent()),
        }
