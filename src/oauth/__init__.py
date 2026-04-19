"""多 provider OAuth 支持层。

现有 Anthropic 家族的逻辑仍然集中在顶层 `oauth_manager` 模块里（token
刷新、PKCE、usage 拉取），保持对外 API 不变；本子包新增 provider 抽象
以及 Anthropic 之外 provider（OpenAI / Codex）的实现。

`oauth_manager` 内部按账户 `provider` 字段分派到这里：
  - provider="claude" (默认/老数据) → 继续走 oauth_manager 里的 anthropic 代码
  - provider="openai"               → 调 `src.oauth.openai` 里的函数
"""

from __future__ import annotations

from . import openai as _openai

# 常量：有效的 provider 值。新增 provider 时在此登记。
VALID_PROVIDERS: tuple[str, ...] = ("claude", "openai")

# 老数据（无 provider 字段）默认当作 claude。
DEFAULT_PROVIDER: str = "claude"


def normalize_provider(value: str | None) -> str:
    """规范化 provider 值；空或未知都回落到 claude。"""
    if not value:
        return DEFAULT_PROVIDER
    v = str(value).strip().lower()
    if v in VALID_PROVIDERS:
        return v
    return DEFAULT_PROVIDER


# 按名字拿到 provider 子模块（仅对非 claude 暴露；claude 在 oauth_manager 内部）。
_NON_CLAUDE_MODULES = {
    "openai": _openai,
}


def get_non_claude_module(provider: str):
    """拿到 provider 模块，仅对 non-claude 有效；claude 返回 None。"""
    return _NON_CLAUDE_MODULES.get(normalize_provider(provider))


__all__ = [
    "VALID_PROVIDERS",
    "DEFAULT_PROVIDER",
    "normalize_provider",
    "get_non_claude_module",
]
