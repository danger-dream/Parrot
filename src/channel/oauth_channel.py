"""OAuth 渠道：调 api.anthropic.com，走完整 CC 伪装链路。"""

from __future__ import annotations

from typing import Optional

from .. import oauth_manager
from ..openai.transform import chat_to_anthropic, responses_to_anthropic
from ..providers import registry as provider_registry
from ..transform import cc_mimicry
from .base import Channel, ChannelDisplay, UpstreamRequest


class OAuthChannel(Channel):
    """代表一个 OAuth 账户。"""

    type = "oauth"
    cc_mimicry = True  # OAuth 强制，不从 config 读
    protocol = "anthropic"  # OAuth 永远是 anthropic 家族，显式声明

    def __init__(self, account: dict, default_models: list[str]):
        from ..oauth_ids import account_key as _account_key
        self.email = account["email"]
        self.account_key = _account_key(account)   # provider:email
        self.key = f"oauth:{self.account_key}"
        self.display_name = self.email
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0

        # Account LKG is authoritative; provider defaults are fallback only.
        models = account.get("models") or []
        selected = list(models) if models else list(default_models)
        disabled = {
            str(model).strip() for model in account.get("disabledModels") or []
            if str(model).strip()
        }
        self.models: list[str] = [model for model in selected if model not in disabled]

    def supports_model(self, requested_model: str) -> Optional[str]:
        return requested_model if requested_model in self.models else None

    def list_client_models(self) -> list[str]:
        return list(self.models)

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        cc_request_context = cc_mimicry.request_context_from(requested_body)
        translator_ctx: Optional[dict] = None
        if ingress_protocol == "chat":
            stream_opts = requested_body.get("stream_options") or {}
            include_usage = bool(stream_opts.get("include_usage")) if isinstance(stream_opts, dict) else False
            requested_body = chat_to_anthropic.translate_request(requested_body)
            translator_ctx = {
                "ingress": "chat",
                "upstream_protocol": "anthropic",
                "response_translator": "chat_to_anthropic",
                "model_for_response": resolved_model,
                "include_usage": include_usage,
            }
        elif ingress_protocol == "responses":
            from ..openai import store as _store
            api_key_name = str(requested_body.get("_api_key_name") or "")
            previous_response_id = requested_body.get("previous_response_id")
            current_input_items = responses_to_anthropic.resolve_current_input_items(requested_body)
            responses_request_body = dict(requested_body)
            namespace_tool_map = responses_to_anthropic.NamespaceToolMap()
            requested_body = responses_to_anthropic.translate_request(
                requested_body, api_key_name=api_key_name, store_enabled=_store.is_enabled(),
                namespace_tool_map=namespace_tool_map,
            )
            translator_ctx = {
                "ingress": "responses",
                "upstream_protocol": "anthropic",
                "response_translator": "responses_to_anthropic",
                "model_for_response": resolved_model,
                "previous_response_id": previous_response_id,
                "api_key_name": api_key_name,
                "channel_key": self.key,
                "current_input_items": current_input_items,
                "request_body": responses_request_body,
                "namespace_tool_map": namespace_tool_map,
            }
        elif ingress_protocol != "anthropic":
            raise ValueError(
                f"OAuthChannel got unsupported ingress_protocol={ingress_protocol!r}; "
                "ProtocolMatrix should have guarded this route."
            )

        if cc_request_context:
            requested_body.update(cc_request_context)
        # OAuth：确保 token 有效 → 走完整 CC 伪装 → 拼 OAuth headers
        access_token = await oauth_manager.ensure_valid_token(self.account_key)

        body_with_model = provider_registry.filter_request_payload(
            self,
            {**requested_body, "model": resolved_model},
            protocol="anthropic",
            bridge=translator_ctx is not None,
        )
        body_with_model = cc_mimicry.ensure_request_context(body_with_model)
        sid = body_with_model[cc_mimicry.PARROT_CC_SESSION_ID_KEY]
        payload, dynamic_map = cc_mimicry.transform_request(
            body_with_model, email=self.email, session_id=sid, auth_mode="oauth")
        signed = cc_mimicry.sign_body(payload)
        downstream_betas = body_with_model.get(cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY)
        original_model = body_with_model.get(cc_mimicry.PARROT_ORIGINAL_MODEL_KEY)
        wants_context_1m = body_with_model.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY)
        wants_fast_mode = body_with_model.get(cc_mimicry.PARROT_WANTS_FAST_MODE_KEY)
        headers = cc_mimicry.build_upstream_headers(
            access_token, session_id=sid, auth_scheme="bearer", auth_mode="oauth",
            model=resolved_model, payload=payload,
            downstream_betas=downstream_betas, original_model=original_model,
            wants_context_1m=wants_context_1m,
            wants_fast_mode=wants_fast_mode)

        return UpstreamRequest(
            url=f"{cc_mimicry.ANTHROPIC_API_BASE}/v1/messages?beta=true",
            headers=headers,
            body=signed,
            dynamic_tool_map=dynamic_map,
            translator_ctx=translator_ctx,
        )

    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        return cc_mimicry._restore_tool_names_in_chunk(chunk, dynamic_map)

    def display(self) -> ChannelDisplay:
        return ChannelDisplay(
            key=self.key,
            type="oauth",
            display_name=self.email,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=self.list_client_models(),
        )
