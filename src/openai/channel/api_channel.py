"""OpenAI 家族 API 渠道。

由 `src/channel/registry.py` 的 factory 分派触发：config 中 protocol 为
`openai-chat` 或 `openai-responses` 的 channel entry 会实例化本类。

MS-3 起：跨变体请求（chat→openai-responses / responses→openai-chat）会
先过 transform.guard 的跨变体 guard，再调用对应 translate_request；同时在
UpstreamRequest.translator_ctx 里带上"响应反向所用的 translate_response"
函数名，failover 的非流式路径据此做反向。SSE 流式翻译在 MS-4 接入。
"""

from __future__ import annotations

import json
from typing import Optional

from ...channel.base import Channel, ChannelDisplay, UpstreamRequest
from ..transform import (
    chat_to_responses, common, guard, responses_to_chat,
)


# User-Agent 故意不伪装成官方 SDK：上游看到 proxy 身份便于排错，也避免与
# anthropic 家族的 CC 伪装语义混淆。
_UA = "anthropic-proxy/openai-adapter"


class OpenAIApiChannel(Channel):
    """OpenAI 家族（chat / responses 上游）的 API 渠道。"""

    type = "api"
    cc_mimicry = False  # OpenAI 家族永远不走 Claude Code 伪装

    def __init__(self, entry: dict):
        self.name = entry["name"]
        self.key = f"api:{self.name}"
        self.display_name = self.name
        self.base_url = (entry.get("baseUrl") or "").rstrip("/")
        self.api_key = entry.get("apiKey", "")
        self.models: list[dict] = list(entry.get("models") or [])
        self.enabled = bool(entry.get("enabled", True))
        self.disabled_reason = entry.get("disabled_reason")
        self.protocol = entry.get("protocol", "openai-chat")
        if self.protocol not in ("openai-chat", "openai-responses"):
            raise ValueError(
                f"OpenAIApiChannel got invalid protocol: {self.protocol!r}"
            )

    def supports_model(self, requested_model: str) -> Optional[str]:
        for m in self.models:
            if m.get("alias") == requested_model:
                return m.get("real")
        return None

    def list_client_models(self) -> list[str]:
        return [m.get("alias", "") for m in self.models if m.get("alias")]

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        """按 (ingress_protocol, self.protocol) 分派。

        - `(chat, openai-chat)` / `(responses, openai-responses)` → 同协议透传
        - `(chat, openai-responses)` → chat→responses 翻译
        - `(responses, openai-chat)` → responses→chat 翻译
        - 其他组合：scheduler family 过滤应已拦住；这里做防御性报错
        """
        if ingress_protocol not in ("chat", "responses"):
            raise ValueError(
                f"OpenAIApiChannel got non-openai ingress_protocol={ingress_protocol!r}; "
                "scheduler should have filtered this at family level."
            )

        if ingress_protocol == "chat" and self.protocol == "openai-chat":
            return self._build_chat_passthrough(requested_body, resolved_model)
        if ingress_protocol == "responses" and self.protocol == "openai-responses":
            return self._build_responses_passthrough(requested_body, resolved_model)
        if ingress_protocol == "chat" and self.protocol == "openai-responses":
            return self._build_chat_to_responses(requested_body, resolved_model)
        if ingress_protocol == "responses" and self.protocol == "openai-chat":
            return self._build_responses_to_chat(requested_body, resolved_model)

        raise RuntimeError(
            f"unreachable: ingress={ingress_protocol!r} protocol={self.protocol!r}"
        )

    # ─── 同协议透传 ────────────────────────────────────────────

    def _build_chat_passthrough(self, body: dict, resolved_model: str) -> UpstreamRequest:
        payload = common.filter_chat_passthrough(body)
        payload["model"] = resolved_model
        return UpstreamRequest(
            url=f"{self.base_url}/v1/chat/completions",
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx=None,
        )

    def _build_responses_passthrough(self, body: dict, resolved_model: str) -> UpstreamRequest:
        payload = common.filter_responses_passthrough(body)
        payload["model"] = resolved_model
        return UpstreamRequest(
            url=f"{self.base_url}/v1/responses",
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx=None,
        )

    # ─── 跨变体翻译 ────────────────────────────────────────────

    def _build_chat_to_responses(self, body: dict, resolved_model: str) -> UpstreamRequest:
        """chat ingress → openai-responses 上游。"""
        guard.guard_chat_to_responses(body)
        payload = chat_to_responses.translate_request(body)
        payload["model"] = resolved_model
        # 下游 chat 是否显式要求末帧 usage（stream_options.include_usage）
        stream_opts = body.get("stream_options") or {}
        include_usage = bool(stream_opts.get("include_usage")) if isinstance(stream_opts, dict) else False
        return UpstreamRequest(
            url=f"{self.base_url}/v1/responses",
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx={
                "ingress": "chat",
                "upstream_protocol": "openai-responses",
                # failover 按此字段选非流式响应反向函数 + 流式 translator
                "response_translator": "chat_to_responses",
                "model_for_response": resolved_model,
                "include_usage": include_usage,
            },
        )

    def _build_responses_to_chat(self, body: dict, resolved_model: str) -> UpstreamRequest:
        """responses ingress → openai-chat 上游。"""
        # Store 开关决定是否允许 previous_response_id
        from .. import store as _store
        store_enabled = _store.is_enabled()
        guard.guard_responses_to_chat(body, store_enabled=store_enabled)

        api_key_name = str(body.get("_api_key_name") or "")
        # 记录"本次请求的"input items（不含 previous_response_id 展开的历史），
        # 作为 Store.save 的 input_items 字段
        current_input_items = responses_to_chat.resolve_current_input_items(body)
        payload = responses_to_chat.translate_request(body, api_key_name=api_key_name)
        payload["model"] = resolved_model
        return UpstreamRequest(
            url=f"{self.base_url}/v1/chat/completions",
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx={
                "ingress": "responses",
                "upstream_protocol": "openai-chat",
                "response_translator": "responses_to_chat",
                "model_for_response": resolved_model,
                "previous_response_id": body.get("previous_response_id"),
                "api_key_name": api_key_name,
                "channel_key": self.key,
                "current_input_items": current_input_items,
            },
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": _UA,
        }

    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        # OpenAI 家族不做工具名还原，原样返回
        return chunk

    def display(self) -> ChannelDisplay:
        return ChannelDisplay(
            key=self.key,
            type="api",
            display_name=self.name,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=self.list_client_models(),
        )
