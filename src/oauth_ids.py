"""OAuth 账户标识符工具。

设计目标
--------
`email` 只是账户的**显示字段**，不是主键；真正的主键是联合键
``account_key = f"{provider}:{email}"``。同一邮箱下允许同时存在
Claude OAuth 与 OpenAI OAuth 两个独立账号。

调用约定
--------
- 对外展示：继续用 `email`（TG 菜单、通知、日志里的人类可读部分）
- 内部路由 / state_db / channel.key / 冷却 / 亲和：一律用 `account_key`
- channel.key 统一格式：``oauth:{provider}:{email}``（三段式）

本模块提供的工具函数专门负责上述拼接与解析，避免在各处散落 f-string。
"""

from __future__ import annotations

from typing import Any

from .oauth import (
    DEFAULT_PROVIDER as _DEFAULT_PROVIDER,
    normalize_provider as _normalize_provider,
)


def account_key(acc_or_provider: dict | str, email: str | None = None) -> str:
    """构造 ``account_key = f"{provider}:{email}"``。

    支持两种调用形式：
      - `account_key(acc_dict)`：从账户 entry 读取 provider / email
      - `account_key(provider, email)`：显式传 provider 与 email
    """
    if isinstance(acc_or_provider, dict):
        provider = _normalize_provider(acc_or_provider.get("provider") or _DEFAULT_PROVIDER)
        acc_email = acc_or_provider.get("email") or ""
        return f"{provider}:{acc_email}"
    provider = _normalize_provider(acc_or_provider or _DEFAULT_PROVIDER)
    return f"{provider}:{email or ''}"


def split_account_key(key: str) -> tuple[str, str]:
    """反向解析：``account_key`` → ``(provider, email)``。

    - 合法三段式 `provider:email` → 精确拆分
    - 历史 email（不含 provider 前缀） → 兜底回退到 default provider
    """
    if not key:
        return (_DEFAULT_PROVIDER, "")
    if ":" in key:
        prov, _, rest = key.partition(":")
        prov = _normalize_provider(prov)
        if prov and rest:
            return (prov, rest)
    # 老数据 / 兜底：整段当 email
    return (_DEFAULT_PROVIDER, key)


def channel_key_for(acc_or_provider: dict | str, email: str | None = None) -> str:
    """构造 channel 层使用的 key：``oauth:{provider}:{email}``。"""
    return f"oauth:{account_key(acc_or_provider, email)}"


def email_from_channel_key(channel_key: str) -> str:
    """反向解析 channel key 得到 email（仅作显示用途）。"""
    if not channel_key.startswith("oauth:"):
        return channel_key
    body = channel_key[len("oauth:"):]
    _, email_part = split_account_key(body)
    return email_part


def provider_from_channel_key(channel_key: str) -> str:
    """反向解析 channel key 得到 provider。"""
    if not channel_key.startswith("oauth:"):
        return _DEFAULT_PROVIDER
    body = channel_key[len("oauth:"):]
    prov, _ = split_account_key(body)
    return prov


def is_account_key(value: Any) -> bool:
    """粗略判断字符串是否是三段式 account_key（含合法 provider 前缀）。"""
    if not isinstance(value, str) or ":" not in value:
        return False
    prov = value.split(":", 1)[0]
    return _normalize_provider(prov) == prov and prov in ("claude", "openai")
