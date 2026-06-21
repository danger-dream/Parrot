"""第三方 API 渠道：兼容 Anthropic 标准的云厂商 Coding Plan。"""

from __future__ import annotations

import re
import uuid
from typing import Optional

from ..openai.transform import chat_to_anthropic, responses_to_anthropic
from ..providers import registry as provider_registry
from ..transform import cc_mimicry, standard
from .base import Channel, ChannelDisplay, UpstreamRequest
from .url_utils import resolve_upstream_url


# ─── 模型别名解析 ─────────────────────────────────────────────────

_SEP_PATTERN = re.compile(r"[,，;；\s]+")
_COLON_PATTERN = re.compile(r"[:：]")


def parse_models_input(raw: str) -> list[dict]:
    """把 TG Bot 输入的模型列表解析为 [{"real":..., "alias":...}, ...]。

    格式：`<真实名>[:<别名>]`，以 ,/，/;/；/空白 分隔。
    - `gpt-5.4` → real=alias=gpt-5.4
    - `GLM-5:glm-5` → real=GLM-5, alias=glm-5
    重复的 alias 会抛 ValueError。
    """
    items = [x for x in _SEP_PATTERN.split((raw or "").strip()) if x]
    if not items:
        raise ValueError("模型列表不能为空")
    out: list[dict] = []
    seen_aliases: set[str] = set()
    for item in items:
        parts = _COLON_PATTERN.split(item)
        if len(parts) == 1:
            real = alias = parts[0].strip()
        elif len(parts) == 2:
            real = parts[0].strip()
            alias = parts[1].strip()
        else:
            raise ValueError(f"模型项格式错误：{item}")
        if not real or not alias:
            raise ValueError(f"模型项不能为空：{item}")
        if alias in seen_aliases:
            raise ValueError(f"别名重复：{alias}")
        seen_aliases.add(alias)
        out.append({"real": real, "alias": alias})
    return out


# ─── ApiChannel ──────────────────────────────────────────────────

class ApiChannel(Channel):
    """第三方兼容 Anthropic 标准的渠道。"""

    type = "api"

    def __init__(self, entry: dict):
        self.name = entry["name"]
        self.key = f"api:{self.name}"
        self.display_name = self.name
        self.base_url = (entry.get("baseUrl") or "").rstrip("/")
        # apiPath：若用户把完整路径（形如 `/api/coding/paas/v4/chat/completions`）填到
        # baseUrl，registry.add/update_api_channel 会把末段识别为协议后缀并拆分存到这里。
        # 运行期若非空 → 直接 `base_url + api_path`；否则走 default `/v1/xxx` 拼接。
        self.api_path = entry.get("apiPath") or None
        self.api_key = entry.get("apiKey", "")
        self.models: list[dict] = list(entry.get("models") or [])
        self.cc_mimicry = bool(entry.get("cc_mimicry", True))
        self.enabled = bool(entry.get("enabled", True))
        self.disabled_reason = entry.get("disabled_reason")
        # 并发限制：0 或缺省 → 用 concurrency.defaultMaxConcurrent（仍 0 则不限）
        try:
            self.max_concurrent = int(entry.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0
        # 是否在向上游发送前剔除 temperature 字段。部分第三方 Claude 中转对
        # 新模型（如 claude-opus-4-7）废弃了 temperature 参数，传任何值都会 400。
        # 默认 False，保持现网行为不变；按渠道开启。
        self.omit_temperature = bool(entry.get("omitTemperature", False))
        # 是否在向上游发送前剔除 thinking 字段（含相关 beta header）。
        # 部分第三方 Claude 中转把 thinking.enabled 当不支持参数 → 400。
        # 默认 False；按渠道开启。
        self.omit_thinking = bool(entry.get("omitThinking", False))
        # ApiChannel 只处理 anthropic 协议；openai-* 会被 registry factory 分派到
        # src/openai/channel/api_channel.py::OpenAIApiChannel。这里做防御性 assert
        # 保证配置中的 protocol 与实际类一致，避免误配置造成难查 bug。
        self.protocol = entry.get("protocol", "anthropic")
        assert self.protocol == "anthropic", (
            f"ApiChannel expects protocol='anthropic', got {self.protocol!r} "
            f"(should be dispatched to OpenAIApiChannel by registry factory)"
        )

    # ─── 模型匹配 ─────────────────────────────────────────────────

    def supports_model(self, requested_model: str) -> Optional[str]:
        for m in self.models:
            if m.get("alias") == requested_model:
                return m.get("real")
        return None

    def list_client_models(self) -> list[str]:
        return [m.get("alias", "") for m in self.models if m.get("alias")]

    # ─── 请求构造 ─────────────────────────────────────────────────

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        # Phase 8: OpenAI-family ingress can route to Anthropic upstream through
        # request/response translators while unsafe feature gaps stay guarded by matrix.
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
            requested_body = responses_to_anthropic.translate_request(
                requested_body, api_key_name=api_key_name, store_enabled=_store.is_enabled(),
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
            }
        elif ingress_protocol != "anthropic":
            raise ValueError(
                f"ApiChannel got unsupported ingress_protocol={ingress_protocol!r}; "
                "ProtocolMatrix should have guarded this route."
            )

        body_with_model = provider_registry.filter_request_payload(
            self,
            {**requested_body, "model": resolved_model},
            protocol="anthropic",
            bridge=translator_ctx is not None,
        )

        dynamic_map: Optional[dict] = None
        if self.cc_mimicry:
            # §7.4/§8：同一请求一个 session_id，同源喂给 body.metadata 与 header
            sid = str(uuid.uuid4())
            payload, dynamic_map = cc_mimicry.transform_request(
                body_with_model, email="", session_id=sid)
            if self.omit_temperature:
                payload.pop("temperature", None)
            if self.omit_thinking:
                payload.pop("thinking", None)
                # thinking 关掉后 context_management.clear_thinking_* 也没意义
                cm = payload.get("context_management")
                if isinstance(cm, dict):
                    edits = [e for e in (cm.get("edits") or [])
                             if not (isinstance(e, dict) and str(e.get("type", "")).startswith("clear_thinking_"))]
                    if edits:
                        cm["edits"] = edits
                    else:
                        payload.pop("context_management", None)
            signed = cc_mimicry.sign_body(payload)
            # omit_thinking 时过滤 thinking 类 beta；build_upstream_headers 内部还会
            # 再剔除 oauth-2025-04-20（messages 不带）。§7.5：复用统一 header 构造，
            # 补齐 Stainless 整层 + x-app + session-id，避免两处手拼漂移。
            # 第三方 API 渠道沿用 Bearer 鉴权（auth_scheme=bearer），不改成 x-api-key。
            betas = [b for b in cc_mimicry.BETAS
                     if not self.omit_thinking or "thinking" not in b]
            downstream_betas = body_with_model.get(cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY)
            original_model = body_with_model.get(cc_mimicry.PARROT_ORIGINAL_MODEL_KEY)
            wants_context_1m = body_with_model.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY)
            headers = cc_mimicry.build_upstream_headers(
                self.api_key, session_id=sid, betas=betas,
                auth_scheme="bearer", model=resolved_model, payload=payload,
                downstream_betas=downstream_betas, original_model=original_model,
                wants_context_1m=wants_context_1m)
        else:
            payload = standard.standard_transform(body_with_model)
            if self.omit_temperature:
                payload.pop("temperature", None)
            if self.omit_thinking:
                payload.pop("thinking", None)
            signed = standard.serialize(payload)
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }

        return UpstreamRequest(
            url=resolve_upstream_url(self.base_url, self.api_path, "/v1/messages"),
            headers=headers,
            body=signed,
            dynamic_tool_map=dynamic_map,
            translator_ctx=translator_ctx,
        )

    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        if dynamic_map:
            return cc_mimicry._restore_tool_names_in_chunk(chunk, dynamic_map)
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
