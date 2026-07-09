"""OAuth 账户标识符工具。

设计目标
--------
`email` 对 Claude 仍是身份字段；OpenAI 的真正主键是复合键。
Claude 仍使用 ``claude:<email>``；OpenAI 使用
``openai:<email>:<workspace_id>``。其中 workspace_id 优先取
entry.workspace_id，缺失时回退 entry.chatgpt_account_id。两者都缺失时
才回退旧的 ``openai:<email>``，保证老配置继续可用。
xAI 使用 ``xai:<subject>``；缺 subject 的导入/旧数据回退到
``xai:<email>``。历史临时格式 ``xai:<email>:<subject>`` 仍在解析路径兼容。

调用约定
--------
- 对外展示：继续用 `email`（TG 菜单、通知、日志里的人类可读部分）
- 内部路由 / state_db / channel.key / 冷却 / 亲和：一律用 `account_key`
- channel.key 统一格式：``oauth:{account_key}``

本模块提供的工具函数专门负责上述拼接与解析，避免在各处散落 f-string。
"""

from __future__ import annotations

from typing import Any

from .oauth import (
    DEFAULT_PROVIDER as _DEFAULT_PROVIDER,
    normalize_provider as _normalize_provider,
)


def openai_workspace_id(acc: dict) -> str:
    """OpenAI workspace/chatgpt account id for upstream headers and labels."""
    for key in ("workspace_id", "chatgpt_account_id"):
        value = str(acc.get(key) or "").strip()
        if value:
            return value
    return ""


def openai_account_identity_parts(acc: dict) -> tuple[str, str, str]:
    """Return OpenAI identity fields: email/workspace/chatgpt.

    The account key only uses email + normalized workspace id. The raw
    chatgpt_account_id is still returned for compatibility checks and upstream
    header selection, but it is not part of the canonical key because in current
    OpenAI/Codex data it is an alias of the workspace/account selector.
    """
    email = str(acc.get("email") or "").strip()
    workspace_id = str(acc.get("workspace_id") or acc.get("chatgpt_account_id") or "").strip()
    chatgpt_account_id = str(acc.get("chatgpt_account_id") or workspace_id or "").strip()
    return (email, workspace_id, chatgpt_account_id)


def openai_composite_identity(acc: dict) -> str:
    """OpenAI provider-local identity string.

    New-format identity is `email:workspace_id`. Legacy metadata-poor accounts
    intentionally stay `email` so old configs remain usable without a forced
    relogin.
    """
    email, workspace_id, _chatgpt_account_id = openai_account_identity_parts(acc)
    if workspace_id:
        return f"{email}:{workspace_id}"
    return email


def xai_subject(acc: dict) -> str:
    """xAI OIDC subject for stable account identity."""
    for key in ("subject", "sub"):
        value = str(acc.get(key) or "").strip()
        if value:
            return value
    return ""


def xai_composite_identity(acc: dict) -> str:
    """xAI provider-local identity string: `subject` when possible.

    The OIDC subject is the stable account id. Email stays display-only and is
    only used as a fallback for legacy/imported entries that lack id_token/sub.
    """
    subject = xai_subject(acc)
    if subject:
        return subject
    return str(acc.get("email") or "").strip()


def account_identity(acc: dict) -> str:
    """账户 entry → provider 内部身份片段。"""
    provider = _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)
    email = str(acc.get("email") or "")
    if provider == "openai":
        return openai_composite_identity(acc)
    if provider == "xai":
        return xai_composite_identity(acc)
    return email


def account_key(acc_or_provider: dict | str, email: str | None = None) -> str:
    """构造标准 account_key。

    支持两种调用形式：
      - `account_key(acc_dict)`：从账户 entry 读取 provider 与身份字段
      - `account_key(provider, email)`：显式传 provider 与旧 email 身份
    """
    if isinstance(acc_or_provider, dict):
        provider = _normalize_provider(acc_or_provider.get("provider") or _DEFAULT_PROVIDER)
        return f"{provider}:{account_identity(acc_or_provider)}"
    provider = _normalize_provider(acc_or_provider or _DEFAULT_PROVIDER)
    return f"{provider}:{email or ''}"


def split_account_key(key: str) -> tuple[str, str]:
    """反向解析：``account_key`` → ``(provider, identity)``。

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
    """构造 channel 层使用的 key：``oauth:{account_key}``。"""
    return f"oauth:{account_key(acc_or_provider, email)}"


def identity_from_channel_key(channel_key: str) -> str:
    """反向解析 channel key 得到 provider 内身份片段。"""
    if not channel_key.startswith("oauth:"):
        return channel_key
    body = channel_key[len("oauth:"):]
    _, identity = split_account_key(body)
    return identity


def email_from_channel_key(channel_key: str) -> str:
    """兼容旧调用名。

    对 Claude 返回 email；对 OpenAI/xAI 新 key 返回复合 identity。真正需要
    展示 email 的路径应回查 account entry。
    """
    return identity_from_channel_key(channel_key)


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
    return _normalize_provider(prov) == prov and prov in ("claude", "openai", "xai")
