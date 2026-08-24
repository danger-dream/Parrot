"""xAI / Grok OAuth channel.

The OAuth provider itself lives in ``src.oauth.xai``.  This channel exposes the
account as an OpenAI Responses-family upstream and sends requests to
``https://api.x.ai/v1/responses`` with the OAuth access token.

xAI's HTTP Responses path is treated as SSE-only. Non-stream downstream calls
are therefore handled by Parrot's
existing upstream-stream-only aggregation path.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .. import cache_hints, config, local_web_tools, oauth_manager
from ..channel.url_utils import resolve_upstream_url
from ..oauth import xai as xai_provider
from ..providers import registry as provider_registry
from ..openai.transform import (
    anthropic_to_responses,
    chat_to_responses,
    guard,
)
from .base import Channel, ChannelDisplay, UpstreamRequest


_UA = "parrot/xai-oauth-adapter"


def _provider_cfg() -> dict:
    default = config.DEFAULT_CONFIG.get("xaiOAuth") or {}
    current = config.get().get("xaiOAuth") or {}
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


def _isolate_session_id(api_key_name: str, raw: str) -> str:
    if not raw:
        return ""
    material = f"xai:k{api_key_name or '-'}:{raw}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _sanitize_xai_payload(payload: dict, *, stream: bool = True) -> dict:
    """Apply conservative xAI Responses HTTP compatibility tweaks.

    Keep only the conservative fields required by the xAI Responses HTTP path.
    xAI documents
    ``stream_options.include_usage`` as the REST/SSE path for receiving the
    final usage/cost block, so keep only that safe stream option and force it on
    for Parrot accounting.
    """
    out = dict(payload)
    out["stream"] = bool(stream)
    out.pop("previous_response_id", None)
    out.pop("prompt_cache_retention", None)
    out.pop("safety_identifier", None)
    # xAI 的 Responses 兼容层不接受 OpenAI Responses 的 arbitrary metadata。
    # 下游客户端常带 metadata 做业务追踪；这里丢弃，避免上游 400：
    #   {"code":"400","error":"Argument not supported: metadata"}
    out.pop("metadata", None)
    tier = str(out.get("service_tier") or "").strip().lower()
    if tier in {"priority", "default"}:
        out["service_tier"] = tier
    else:
        out.pop("service_tier", None)
    out["stream_options"] = {"include_usage": True}
    local_web_tools.prepare_xai_responses_native_web_search_tools(out)
    # xAI HTTP rejects tool_choice when tools are absent/empty.
    tools = out.get("tools")
    if not tools:
        out.pop("tool_choice", None)
        out.pop("parallel_tool_calls", None)
    return out


class XAIOAuthChannel(Channel):
    """provider="xai" OAuth account, exposed as an OpenAI Responses channel."""

    type = "oauth"
    provider = "xai"
    cc_mimicry = False
    protocol = "openai-responses"
    upstream_stream_only = True

    def __init__(self, account: dict, default_models: list[str] | None = None):
        from ..oauth_ids import account_key as _account_key

        self.email = account["email"]
        self.account_key = _account_key(account)
        self.key = f"oauth:{self.account_key}"
        self.subject = str(account.get("subject") or account.get("sub") or "")
        self.display_name = self.email
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0

        self.base_url = str(
            account.get("base_url")
            or account.get("baseUrl")
            or _provider_cfg().get("apiBaseUrl")
            or _provider_cfg().get("baseUrl")
            or xai_provider.api_base_url()
        ).rstrip("/")
        self.api_path = (
            str(account.get("apiPath") or _provider_cfg().get("responsesPath") or "").strip()
            or None
        )

        models = account.get("models") or []
        if models:
            selected_models = list(models)
        elif default_models:
            selected_models = list(default_models)
        else:
            selected_models = list(
                _provider_cfg().get("defaultModels")
                or (config.DEFAULT_CONFIG.get("xaiOAuth") or {}).get("defaultModels")
                or []
            )
        disabled_models = {
            str(model).strip() for model in account.get("disabledModels") or []
            if str(model).strip()
        }
        self.models = [model for model in selected_models if model not in disabled_models]

        provider_cfg = _provider_cfg()
        image_models = (
            account.get("imageModels")
            if "imageModels" in account
            else provider_cfg.get("imageModels")
        )
        video_models = (
            account.get("videoModels")
            if "videoModels" in account
            else provider_cfg.get("videoModels")
        )
        # 媒体模型不并入 self.models，避免被普通文本 /responses 调度器选中。
        self.image_models = [
            str(m) for m in (image_models or []) if str(m).strip()
        ]
        self.video_models = [
            str(m) for m in (video_models or []) if str(m).strip()
        ]

    def supports_model(self, requested_model: str) -> Optional[str]:
        if requested_model not in self.models:
            return None
        return requested_model

    def list_client_models(self) -> list[str]:
        return list(self.models)

    def supports_media_model(self, kind: str, requested_model: str) -> bool:
        """Return whether this OAuth account is enabled for one Imagine model."""
        if kind == "image":
            models = self.image_models
        elif kind == "video":
            models = self.video_models
        else:
            models = []
        return requested_model in models

    async def build_media_headers(self) -> dict[str, str]:
        """Reuse the existing OAuth lifecycle and return JSON Imagine headers."""
        access_token = await oauth_manager.ensure_valid_token(self.account_key)
        headers = self._build_headers(access_token)
        headers["accept"] = "application/json"
        return headers

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "responses",
    ) -> UpstreamRequest:
        if ingress_protocol not in ("anthropic", "chat", "responses"):
            raise ValueError(
                "XAIOAuthChannel only serves anthropic / openai-chat / openai-responses "
                f"ingress; got {ingress_protocol!r}. ProtocolMatrix should have guarded this route."
            )

        translator_ctx: Optional[dict]
        if ingress_protocol == "responses":
            if str(requested_body.get("previous_response_id") or "").strip():
                raise ValueError(
                    "previous_response_id is not supported on xAI OAuth HTTP route; "
                    "use prompt_cache_key/session_id or route this request to a native API/WS channel."
                )
            payload = provider_registry.filter_request_payload(
                self,
                requested_body,
                protocol="openai-responses",
            )
            translator_ctx = None
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
            self,
            payload,
            protocol="openai-responses",
        )
        payload = _sanitize_xai_payload(payload, stream=True)

        access_token = await oauth_manager.ensure_valid_token(self.account_key)
        headers = self._build_headers(access_token)
        prompt_cache_key = str(payload.get("prompt_cache_key") or "").strip()
        if prompt_cache_key:
            conv_id = prompt_cache_key
            if _provider_cfg().get("isolateSessionId", True):
                api_key_name = _request_api_key_name(requested_body)
                if api_key_name:
                    conv_id = _isolate_session_id(api_key_name, prompt_cache_key) or prompt_cache_key
            headers["x-grok-conv-id"] = conv_id

        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/responses"),
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
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

    def _build_headers(self, access_token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {access_token}",
            "accept": "text/event-stream",
            "content-type": "application/json",
            "connection": "Keep-Alive",
            "user-agent": str(_provider_cfg().get("userAgent") or _UA),
        }
