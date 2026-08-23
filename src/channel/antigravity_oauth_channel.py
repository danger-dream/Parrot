"""Antigravity / Google Code Assist OAuth channel.

Exposes the account as an OpenAI Responses-family upstream. The request is
translated to Gemini generateContent and wrapped in the Cloud Code envelope
before it leaves the process. Gemini JSON/SSE is restored to standard
Responses by ``AntigravityOAuthAdapter`` before the Responses toolkit.
"""

from __future__ import annotations

import json
from typing import Optional

from .. import cache_hints, config, oauth_manager
from ..oauth import antigravity as ag_provider
from ..oauth_ids import account_key as _account_key
from ..openai.transform import anthropic_to_responses, chat_to_responses, guard
from ..providers import antigravity_codec, registry as provider_registry
from .base import Channel, ChannelDisplay, UpstreamRequest


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


def _request_api_key_name(body: dict) -> str:
    return str(body.get("_api_key_name") or body.get("_parrot_api_key_name") or "")


def _wants_stream(body: dict) -> bool:
    return bool(body.get("stream"))


class AntigravityOAuthChannel(Channel):
    """provider='antigravity' OAuth account, exposed as openai-responses."""

    type = "oauth"
    provider = "antigravity"
    cc_mimicry = False
    protocol = "openai-responses"
    upstream_stream_only = False

    def __init__(self, account: dict, default_models: list[str] | None = None):
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
        stored_base = str(
            account.get("request_base_url")
            or account.get("requestBaseUrl")
            or ""
        ).rstrip("/")
        # Login identity may persist prod cloudcode-pa as base_url for
        # loadCodeAssist. Generate/stream follow CPA and use daily first.
        prod = str(cfg.get("apiBaseUrl") or ag_provider.api_base_url()).rstrip("/")
        account_base = str(account.get("base_url") or account.get("baseUrl") or "").rstrip("/")
        if not stored_base and account_base and account_base != prod:
            stored_base = account_base
        self.base_url = str(
            stored_base
            or cfg.get("dailyApiBaseUrl")
            or ag_provider.request_api_base_url()
        ).rstrip("/")

        models = account.get("models") or []
        if models:
            self.models = list(models)
        elif default_models:
            self.models = list(default_models)
        else:
            self.models = list(cfg.get("defaultModels") or ag_provider.default_models())

        image_models = (
            account.get("imageModels")
            if "imageModels" in account
            else cfg.get("imageModels")
        )
        self.image_models = [str(m) for m in (image_models or []) if str(m).strip()]

    def supports_model(self, requested_model: str) -> Optional[str]:
        if requested_model in self.models:
            return requested_model
        return None

    def list_client_models(self) -> list[str]:
        return list(self.models)

    def supports_media_model(self, kind: str, requested_model: str) -> bool:
        if kind != "image":
            return False
        return requested_model in self.image_models

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "responses",
    ) -> UpstreamRequest:
        if ingress_protocol not in ("anthropic", "chat", "responses"):
            raise ValueError(
                "AntigravityOAuthChannel only serves anthropic / openai-chat / "
                f"openai-responses ingress; got {ingress_protocol!r}."
            )

        stream = _wants_stream(requested_body)
        translator_ctx: Optional[dict]
        if ingress_protocol == "responses":
            if str(requested_body.get("previous_response_id") or "").strip():
                raise ValueError(
                    "previous_response_id is not supported on Antigravity OAuth; "
                    "resend the conversation input instead."
                )
            payload = provider_registry.filter_request_payload(
                self, requested_body, protocol="openai-responses",
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
            }

        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(
            self, payload, protocol="openai-responses",
        )
        gemini = antigravity_codec.responses_to_gemini(payload)
        envelope = antigravity_codec.wrap_cloud_code(
            gemini,
            model=resolved_model,
            project_id=self.project_id,
            stream=stream,
        )
        translator_ctx["antigravity_stream"] = antigravity_codec.GeminiStreamToResponses(
            model=resolved_model,
            request_body=payload,
        )
        translator_ctx["antigravity_stream_requested"] = stream

        access_token = await oauth_manager.ensure_valid_token(self.account_key)
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
