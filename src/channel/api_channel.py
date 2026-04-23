"""第三方 API 渠道：兼容 Anthropic 标准的云厂商 Coding Plan。"""

from __future__ import annotations

import re
from typing import Optional

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
        # ingress_protocol 对 anthropic 渠道无意义：本类只服务 /v1/messages 入口。
        _ = ingress_protocol
        body_with_model = {**requested_body, "model": resolved_model}

        dynamic_map: Optional[dict] = None
        if self.cc_mimicry:
            payload, dynamic_map = cc_mimicry.transform_request(body_with_model, email="")
            signed = cc_mimicry.sign_body(payload)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": ",".join(cc_mimicry.BETAS),
                "User-Agent": cc_mimicry.CLI_USER_AGENT,
            }
        else:
            payload = standard.standard_transform(body_with_model)
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
