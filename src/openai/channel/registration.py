"""把 OpenAIApiChannel 注册到根 registry 的 factory 表。

由 `src/server.py` 的 lifespan 在 `registry.rebuild_from_config()` 之前调用一次。
"""

from __future__ import annotations

from ...channel import registry
from .api_channel import OpenAIApiChannel


def register_factories() -> None:
    registry.register_channel_factory("openai-chat", OpenAIApiChannel)
    registry.register_channel_factory("openai-responses", OpenAIApiChannel)
