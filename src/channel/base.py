"""Channel 抽象基类。

统一 OAuth 账户和第三方 API 渠道为同一调度单位。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UpstreamRequest:
    """描述一次向上游发起的 HTTP 请求。

    `dynamic_tool_map` 随请求一起返回，避免存到 Channel 实例属性导致并发请求互相覆盖：
    每次 `build_upstream_request` 产生独立的 map，`restore_response` 用对应 map 还原。

    `translator_ctx` 仅在 OpenAI 家族的跨变体（chat↔responses）请求上使用：
    build_upstream_request 把"翻译前的 input_items"等上下文附带回去，供 failover 里的
    SSE translator / Store 写入使用。anthropic 渠道永远保持 None。
    """

    url: str
    headers: dict[str, str]
    body: bytes
    method: str = "POST"
    dynamic_tool_map: Optional[dict] = None
    translator_ctx: Optional[dict] = None


@dataclass
class ChannelDisplay:
    """TG Bot 展示用的渠道信息。"""

    key: str
    type: str               # "oauth" | "api"
    display_name: str
    enabled: bool
    disabled_reason: Optional[str]
    models: list[str] = field(default_factory=list)


class Channel(ABC):
    """所有渠道的抽象基类。

    属性：
      key: 唯一标识，格式 "oauth:<email>" 或 "api:<name>"
      type: "oauth" | "api"
      display_name: 面向用户展示的名字
      enabled: 总开关
      disabled_reason: None | "user" | "quota" | "auth_error"
      cc_mimicry: 是否走 CC 伪装链路（OAuth 强制 True，API 可选）
    """

    key: str
    type: str
    display_name: str
    enabled: bool
    disabled_reason: Optional[str]
    cc_mimicry: bool
    # 渠道的上游协议。默认 "anthropic"（现状），OpenAI 家族子类会覆盖为
    # "openai-chat" 或 "openai-responses"。scheduler / failover / probe 都依据它分派行为。
    protocol: str = "anthropic"
    # 上游是否仅支持流式响应（True 例：OpenAI Codex OAuth chatgpt.com backend）
    # 为 True 时，即使下游 is_stream=False，failover 也会用流式方式读取 SSE，
    # 再用 SSEAssistantBuilder 聚合成完整 JSON 返回给下游，避免 json.loads 空串。
    upstream_stream_only: bool = False

    @abstractmethod
    def supports_model(self, requested_model: str) -> Optional[str]:
        """若支持，返回上游真实模型名；否则 None。"""

    @abstractmethod
    def list_client_models(self) -> list[str]:
        """返回客户端可见的模型名列表。"""

    @abstractmethod
    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        """把下游请求体转换为对本渠道上游的请求。

        `ingress_protocol` 表示下游入口协议（"anthropic"/"chat"/"responses"），
        anthropic 家族子类忽略此参数，OpenAI 家族子类据此选择是否做跨变体翻译。
        """

    @abstractmethod
    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        """响应字节流的还原（如 OAuth / cc_mimicry 路径的工具名还原）。

        `dynamic_map` 由调用方从对应的 UpstreamRequest 中取出并传入，
        保证并发场景下还原映射与请求一一对应（不依赖 Channel 实例属性）。
        非 CC 伪装路径直接返回原样。"""

    @abstractmethod
    def display(self) -> ChannelDisplay: ...
