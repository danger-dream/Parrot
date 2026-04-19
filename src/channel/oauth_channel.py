"""OAuth 渠道：调 api.anthropic.com，走完整 CC 伪装链路。"""

from __future__ import annotations

from typing import Optional

from .. import oauth_manager
from ..transform import cc_mimicry
from .base import Channel, ChannelDisplay, UpstreamRequest


class OAuthChannel(Channel):
    """代表一个 OAuth 账户。"""

    type = "oauth"
    cc_mimicry = True  # OAuth 强制，不从 config 读
    protocol = "anthropic"  # OAuth 永远是 anthropic 家族，显式声明

    def __init__(self, account: dict, default_models: list[str]):
        self.email = account["email"]
        self.key = f"oauth:{self.email}"
        self.display_name = self.email
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")

        models = account.get("models") or []
        self.models: list[str] = list(models) if models else list(default_models)

    def supports_model(self, requested_model: str) -> Optional[str]:
        return requested_model if requested_model in self.models else None

    def list_client_models(self) -> list[str]:
        return list(self.models)

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        _ = ingress_protocol  # OAuth 只服务 /v1/messages，忽略
        # OAuth：确保 token 有效 → 走完整 CC 伪装 → 拼 OAuth headers
        access_token = await oauth_manager.ensure_valid_token(self.email)

        body_with_model = {**requested_body, "model": resolved_model}
        payload, dynamic_map = cc_mimicry.transform_request(body_with_model, email=self.email)
        signed = cc_mimicry.sign_body(payload)
        headers = cc_mimicry.build_upstream_headers(access_token)

        return UpstreamRequest(
            url=f"{cc_mimicry.ANTHROPIC_API_BASE}/v1/messages?beta=true",
            headers=headers,
            body=signed,
            dynamic_tool_map=dynamic_map,
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
