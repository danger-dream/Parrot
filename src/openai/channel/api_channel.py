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
from ...channel.compatibility import (
    apply_forced_openai_fast_mode,
    forced_for_model,
    normalize_mode,
    normalize_models,
)
from ...channel.url_utils import resolve_upstream_url
from ... import cache_hints, local_web_tools
from ...providers import registry as provider_registry
from .. import deepseek_reasoning
from ..transform import (
    anthropic_to_chat, anthropic_to_responses, common,
    chat_to_responses, guard, responses_to_chat,
)


# User-Agent 故意不伪装成官方 SDK：上游看到 proxy 身份便于排错，也避免与
# anthropic 家族的 CC 伪装语义混淆。
_UA = "parrot/openai-adapter"


class OpenAIApiChannel(Channel):
    """OpenAI 家族（chat / responses 上游）的 API 渠道。"""

    type = "api"
    cc_mimicry = False  # OpenAI 家族永远不走 Claude Code 伪装

    def __init__(self, entry: dict):
        self.name = entry["name"]
        self.key = f"api:{self.name}"
        self.display_name = self.name
        self.base_url = (entry.get("baseUrl") or "").rstrip("/")
        # apiPath：若用户把完整调用路径填到 baseUrl，registry 会把末段识别为协议后缀
        # 并拆分存到这里。运行期非空 → `base_url + api_path`；否则走 default `/v1/xxx`。
        self.api_path = entry.get("apiPath") or None
        self.api_key = entry.get("apiKey", "")
        self.models: list[dict] = list(entry.get("models") or [])
        self.enabled = bool(entry.get("enabled", True))
        self.disabled_reason = entry.get("disabled_reason")
        self.protocol = entry.get("protocol", "openai-chat")
        if self.protocol not in ("openai-chat", "openai-responses"):
            raise ValueError(
                f"OpenAIApiChannel got invalid protocol: {self.protocol!r}"
            )
        # 通用渠道兼容设置必须作用在最终 OpenAI 出站 payload，而不是只在
        # Anthropic 转换前处理，否则跨协议请求会显示已开启但实际无效。
        self.omit_temperature = bool(entry.get("omitTemperature", False))
        self.omit_thinking = bool(entry.get("omitThinking", False))
        self.context_1m_mode = normalize_mode(entry.get("context1mMode"))
        self.context_1m_models = normalize_models(entry.get("context1mModels"))
        self.fast_mode = normalize_mode(entry.get("fastMode"))
        self.fast_models = normalize_models(entry.get("fastModels"))
        # Responses WebSocket ingress historically dials third-party Responses API
        # channels as WebSocket (https://.../v1/responses -> wss://...). Keep that
        # default for compatibility, while allowing explicit test/configured
        # channels to exercise the WS-client -> HTTP/SSE-upstream bridge.
        self.responses_ws_upstream_transport = str(
            entry.get("responsesWsUpstreamTransport")
            or entry.get("responses_ws_upstream_transport")
            or "ws"
        ).strip().lower()
        if self.responses_ws_upstream_transport not in ("ws", "sse"):
            self.responses_ws_upstream_transport = "ws"

    def _is_deepseek(self, model: str | None = None) -> bool:
        name = str(model or "").lower()
        return (
            name.startswith("deepseek-")
            or "deepseek" in self.name.lower()
            or "deepseek" in self.base_url.lower()
        )

    @staticmethod
    def _anthropic_thinking_type(body: dict) -> str | None:
        thinking = body.get("thinking")
        if isinstance(thinking, dict):
            typ = str(thinking.get("type") or "").strip().lower()
            return typ or None
        if body.get("output_config") is not None:
            return "enabled"
        return None

    @staticmethod
    def _tool_choice_forces_tool(choice) -> bool:
        if choice == "required":
            return True
        if isinstance(choice, dict):
            typ = choice.get("type")
            return typ in ("function", "required")
        return False

    def _apply_bigmodel_anthropic_bridge_compat(self, source_body: dict, payload: dict, resolved_model: str) -> None:
        if not common.supports_bigmodel_thinking(resolved_model):
            return
        thinking_type = self._anthropic_thinking_type(source_body)
        explicit_thinking = thinking_type is not None
        if not explicit_thinking:
            return

        # BigModel's OpenAI-compatible Chat API uses a provider-specific
        # `thinking` switch plus top-level `reasoning_effort` for GLM-5.2+.
        # Anthropic `adaptive` / `enabled` both map to BigModel `enabled`;
        # Anthropic `disabled` only stays disabled when no output_config effort
        # asks us to enable reasoning again.
        if thinking_type == "disabled" and source_body.get("output_config") is None:
            payload["thinking"] = {"type": "disabled"}
            payload.pop("reasoning_effort", None)
        else:
            payload["thinking"] = {"type": "enabled"}

    def _apply_deepseek_anthropic_bridge_compat(self, source_body: dict, payload: dict, resolved_model: str) -> None:
        if not self._is_deepseek(resolved_model):
            return
        thinking_type = self._anthropic_thinking_type(source_body)
        explicit_thinking = thinking_type is not None
        forced_tool_choice = self._tool_choice_forces_tool(payload.get("tool_choice"))

        if explicit_thinking and thinking_type != "disabled" and forced_tool_choice:
            raise guard.GuardError(
                400,
                "invalid_request_error",
                "DeepSeek thinking mode does not support forced/required tool_choice; use tool_choice=auto or disable thinking",
                param="tool_choice",
                scope="request",
            )

        if thinking_type == "disabled" or not explicit_thinking:
            # Anthropic Messages default semantics are visible-answer first.  If
            # the caller did not explicitly request thinking, disable DeepSeek's
            # default thinking mode to avoid burning the whole output budget in
            # reasoning_content and to keep forced tool_choice usable.
            payload["thinking"] = {"type": "disabled"}
        elif thinking_type in ("enabled", "adaptive"):
            payload["thinking"] = {"type": "enabled"}

        deepseek_reasoning.inject_into_chat_payload(payload)

    def supports_model(self, requested_model: str) -> Optional[str]:
        for m in self.models:
            if m.get("alias") == requested_model:
                return m.get("real")
        return None

    def list_client_models(self) -> list[str]:
        return [m.get("alias", "") for m in self.models if m.get("alias")]

    def forces_fast_mode(self, resolved_model: str) -> bool:
        return forced_for_model(self.fast_mode, self.fast_models, resolved_model)

    def _apply_compatibility(self, payload: dict, resolved_model: str) -> None:
        """在所有翻译/供应商适配完成后修改最终 OpenAI 出站字段。"""
        if self.omit_temperature:
            payload.pop("temperature", None)
        if self.omit_thinking:
            payload.pop("thinking", None)
        apply_forced_openai_fast_mode(self, payload, resolved_model)

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
        if ingress_protocol == "anthropic" and self.protocol == "openai-chat":
            return self._build_anthropic_to_chat(requested_body, resolved_model)
        if ingress_protocol == "anthropic" and self.protocol == "openai-responses":
            return self._build_anthropic_to_responses(requested_body, resolved_model)

        if ingress_protocol not in ("chat", "responses"):
            raise ValueError(
                f"OpenAIApiChannel got unsupported ingress_protocol={ingress_protocol!r}; "
                "ProtocolMatrix should have guarded this route."
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

    # ─── 跨家族翻译 ────────────────────────────────────────────

    def _build_anthropic_to_chat(self, body: dict, resolved_model: str) -> UpstreamRequest:
        """anthropic ingress → openai-chat 上游（Phase 8 first path, non-stream）。"""
        payload = anthropic_to_chat.translate_request(body, target_model=resolved_model)
        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-chat")
        cache_hints.apply_anthropic_cache_to_openai_payload(
            body,
            payload,
            model=resolved_model,
            api_key_name=body.get("_parrot_api_key_name"),
            client_ip=body.get("_parrot_client_ip"),
        )
        self._apply_bigmodel_anthropic_bridge_compat(body, payload, resolved_model)
        self._apply_deepseek_anthropic_bridge_compat(body, payload, resolved_model)
        self._apply_compatibility(payload, resolved_model)
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/chat/completions"),
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx={
                "ingress": "anthropic",
                "upstream_protocol": "openai-chat",
                "response_translator": "anthropic_to_chat",
                "model_for_response": resolved_model,
            },
        )

    def _build_anthropic_to_responses(self, body: dict, resolved_model: str) -> UpstreamRequest:
        """anthropic ingress → openai-responses 上游（Phase 8 third path, non-stream）。"""
        payload = anthropic_to_responses.translate_request(body, target_model=resolved_model)
        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-responses")
        cache_hints.apply_anthropic_cache_to_openai_payload(
            body,
            payload,
            model=resolved_model,
            api_key_name=body.get("_parrot_api_key_name"),
            client_ip=body.get("_parrot_client_ip"),
        )
        self._apply_compatibility(payload, resolved_model)
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/responses"),
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx={
                "ingress": "anthropic",
                "upstream_protocol": "openai-responses",
                "response_translator": "anthropic_to_responses",
                "model_for_response": resolved_model,
                "request_body": body,
            },
        )

    # ─── 同协议透传 ────────────────────────────────────────────

    def _build_chat_passthrough(self, body: dict, resolved_model: str) -> UpstreamRequest:
        payload = provider_registry.filter_request_payload(self, body, protocol="openai-chat")
        if (
            self._is_deepseek(resolved_model)
            or common.supports_bigmodel_thinking(resolved_model)
            or bool(body.get("_parrot_allow_openai_thinking"))
        ) and isinstance(body.get("thinking"), dict):
            # Internal-only/provider-specific escape hatch for non-standard Chat
            # `thinking` fields. Keep it out of the public allowlist so arbitrary
            # OpenAI-compatible channels do not receive unsupported fields.
            payload["thinking"] = body["thinking"]
        payload["model"] = resolved_model
        self._apply_compatibility(payload, resolved_model)
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/chat/completions"),
            headers=self._headers(),
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx=None,
        )

    def _build_responses_passthrough(self, body: dict, resolved_model: str) -> UpstreamRequest:
        payload = dict(body)
        local_web_tools.prepare_openai_responses_local_web_tools(payload)
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-responses")
        payload["model"] = resolved_model
        self._apply_compatibility(payload, resolved_model)
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/responses"),
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
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-responses")
        self._apply_compatibility(payload, resolved_model)
        # 下游 chat 是否显式要求末帧 usage（stream_options.include_usage）
        stream_opts = body.get("stream_options") or {}
        include_usage = bool(stream_opts.get("include_usage")) if isinstance(stream_opts, dict) else False
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/responses"),
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
        body = dict(body)
        local_web_tools.prepare_openai_responses_local_web_tools(body)
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
        payload = provider_registry.filter_request_payload(self, payload, protocol="openai-chat")
        self._apply_compatibility(payload, resolved_model)
        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/chat/completions"),
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
        if self._is_deepseek():
            try:
                deepseek_reasoning.cache_from_chat_response(json.loads(chunk))
            except Exception:
                pass
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
