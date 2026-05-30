"""Human-friendly OAuth error messages for Telegram/Admin UI.

Keep low-level httpx/OpenAI/Anthropic exceptions out of user-facing messages.
The raw exception is still available to callers for logs; this module only turns
known failures into concise Chinese guidance with a stable detail code.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
from typing import Any

import httpx


@dataclass(frozen=True)
class OAuthDisplayError:
    """Structured, user-safe OAuth error description."""

    code: str
    title: str
    reason: str
    action: str
    retryable: bool = False
    auth_error: bool = False
    status: int | None = None
    provider: str = ""
    operation: str = ""
    technical: str = ""


def _norm_provider(provider: str | None) -> str:
    p = (provider or "").lower().strip()
    if p in {"openai", "oa", "chatgpt", "codex"}:
        return "openai"
    if p in {"claude", "anthropic"}:
        return "claude"
    return p or "oauth"


def _norm_operation(operation: str | None) -> str:
    op = (operation or "").lower().strip()
    aliases = {
        "token": "refresh_token",
        "refresh": "refresh_token",
        "force_refresh": "refresh_token",
        "usage": "fetch_usage",
        "quota": "fetch_usage",
        "exchange": "exchange_code",
        "login": "exchange_code",
        "probe": "probe_usage",
    }
    return aliases.get(op, op or "oauth")


def _technical_from_exception(exc: BaseException | str) -> str:
    if isinstance(exc, str):
        return exc[:500]
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def _http_status(exc: BaseException | str) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return int(exc.response.status_code)
        except Exception:
            return None
    if isinstance(exc, str):
        m = re.search(r"\bHTTP\s+(\d{3})\b", exc, flags=re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"\b(401|403|408|409|429|500|502|503|504)\b", exc)
        if m:
            return int(m.group(1))
    return None


def _response_error_code(exc: BaseException | str) -> str:
    """Best-effort extraction of OAuth error code from HTTP response body."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return ""
    try:
        data: Any = exc.response.json()
    except Exception:
        try:
            text = exc.response.text or ""
        except Exception:
            text = ""
        return text[:200].lower()

    candidates: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key in ("error", "error_code", "code", "type", "error_description", "message", "detail"):
                val = obj.get(key)
                if isinstance(val, str):
                    candidates.append(val)
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return " ".join(candidates).lower()[:500]


def _is_invalid_token(status: int | None, body_code: str) -> bool:
    if status == 401:
        return True
    markers = ("invalid_grant", "invalid_token", "expired", "revoked")
    if status == 400:
        return any(s in body_code for s in markers)
    return any(s in body_code for s in markers)


def describe_oauth_error(
    exc: BaseException | str | OAuthDisplayError,
    *,
    provider: str | None = None,
    operation: str | None = None,
) -> OAuthDisplayError:
    """Convert an OAuth/probe exception or reason string into a safe UI error."""
    if isinstance(exc, OAuthDisplayError):
        return exc

    prov = _norm_provider(provider)
    op = _norm_operation(operation)
    status = _http_status(exc)
    body_code = _response_error_code(exc)
    technical = _technical_from_exception(exc)

    # Non-HTTP transport failures first.
    if isinstance(exc, httpx.TimeoutException) or (isinstance(exc, str) and "timeout" in exc.lower()):
        return OAuthDisplayError(
            code=f"{prov}_oauth_timeout",
            title="OAuth 请求超时",
            reason="上游认证或用量接口响应太慢，本次没有拿到结果。",
            action="稍后重试；如果连续失败，再检查网络或上游服务状态。",
            retryable=True,
            status=status,
            provider=prov,
            operation=op,
            technical=technical,
        )
    if isinstance(exc, httpx.RequestError):
        return OAuthDisplayError(
            code=f"{prov}_oauth_network_error",
            title="OAuth 网络连接失败",
            reason="当前无法连接上游认证或用量接口。",
            action="稍后重试；如果持续失败，检查服务器网络和 DNS。",
            retryable=True,
            status=status,
            provider=prov,
            operation=op,
            technical=technical,
        )

    # OpenAI / Codex OAuth.
    if prov == "openai":
        if op == "exchange_code" and _is_invalid_token(status, body_code):
            return OAuthDisplayError(
                code="openai_code_exchange_401" if status == 401 else "openai_code_exchange_invalid",
                title="登录 code 已失效",
                reason="这个 code 可能已经用过、过期、复制不完整，或不是本次登录会话生成的。",
                action="重新生成登录链接，并复制浏览器地址栏里的完整 callback URL。",
                auth_error=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if op == "refresh_token" and _is_invalid_token(status, body_code):
            return OAuthDisplayError(
                code="openai_token_401" if status == 401 else "openai_token_invalid",
                title="OpenAI 授权已失效",
                reason="refresh_token 可能已过期、被撤销，或账号重新登录后旧 token 被替换。",
                action="重新登录这个 OpenAI 账号。",
                auth_error=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 403:
            return OAuthDisplayError(
                code="openai_permission_403",
                title="OpenAI 拒绝访问",
                reason="当前账号或 token 没有访问 Codex / ChatGPT 接口的权限。",
                action="确认账号套餐和权限；如果账号正常，请重新登录。",
                auth_error=(op == "refresh_token"),
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 429:
            return OAuthDisplayError(
                code="openai_rate_limited",
                title="OpenAI 请求过于频繁",
                reason="上游触发了限流，本次刷新或探测没有完成。",
                action="稍后再试，避免连续批量刷新。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status and status >= 500:
            return OAuthDisplayError(
                code=f"openai_upstream_{status}",
                title="OpenAI 服务临时异常",
                reason="上游认证或 Codex 接口返回服务端错误。",
                action="稍后重试。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if isinstance(exc, str) and "not registered" in exc.lower():
            return OAuthDisplayError(
                code="openai_channel_not_registered",
                title="OpenAI 渠道未注册",
                reason="账号已在配置里，但运行中的渠道注册表还没有加载它。",
                action="重新加载或重启 Parrot 后再试。",
                retryable=True,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if op == "probe_usage" and isinstance(exc, str) and "no x-codex" in exc.lower():
            return OAuthDisplayError(
                code="openai_probe_no_usage_headers",
                title="OpenAI 用量探测未返回配额头",
                reason="探测请求成功返回，但响应里没有 x-codex-* 用量字段。",
                action="稍后再试；如果持续出现，可能是上游响应格式变化。",
                retryable=True,
                provider=prov,
                operation=op,
                technical=technical,
            )

    # Claude / Anthropic OAuth.
    if prov == "claude":
        if op == "exchange_code" and _is_invalid_token(status, body_code):
            return OAuthDisplayError(
                code="claude_code_exchange_401" if status == 401 else "claude_code_exchange_invalid",
                title="Claude 登录 code 已失效",
                reason="这个 code 可能已经用过、过期、复制不完整，或不是本次登录会话生成的。",
                action="重新生成登录链接，并复制完整 code。",
                auth_error=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if op == "refresh_token" and _is_invalid_token(status, body_code):
            return OAuthDisplayError(
                code="claude_token_401" if status == 401 else "claude_token_invalid",
                title="Claude 授权已失效",
                reason="refresh_token 可能已过期、被撤销，或账号授权状态异常。",
                action="重新登录这个 Claude 账号。",
                auth_error=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if op == "fetch_usage" and status == 403:
            return OAuthDisplayError(
                code="claude_usage_403",
                title="Claude 用量接口拒绝访问",
                reason="Token 可能还能调用模型，但当前账号没有权限读取 /api/oauth/usage，或授权状态异常。",
                action="先刷新 Token；如果仍失败，请重新登录 Claude 账号。",
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 403:
            return OAuthDisplayError(
                code="claude_permission_403",
                title="Claude 拒绝访问",
                reason="当前账号或 token 没有访问该接口的权限。",
                action="刷新 Token；如果仍失败，请重新登录 Claude 账号。",
                auth_error=(op == "refresh_token"),
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 429:
            return OAuthDisplayError(
                code="claude_rate_limited",
                title="Claude 请求过于频繁",
                reason="上游触发了限流，本次刷新或用量拉取没有完成。",
                action="稍后再试，避免连续批量刷新。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status and status >= 500:
            return OAuthDisplayError(
                code=f"claude_upstream_{status}",
                title="Claude 服务临时异常",
                reason="上游认证或用量接口返回服务端错误。",
                action="稍后重试。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )

    # Generic HTTP fallback.
    if status:
        if status == 401:
            return OAuthDisplayError(
                code=f"{prov}_oauth_401",
                title="OAuth 授权已失效",
                reason="token 已失效或不被上游接受。",
                action="重新登录对应账号。",
                auth_error=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 403:
            return OAuthDisplayError(
                code=f"{prov}_oauth_403",
                title="OAuth 权限不足",
                reason="上游拒绝访问该接口。",
                action="确认账号权限；如果账号正常，请重新登录。",
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status == 429:
            return OAuthDisplayError(
                code=f"{prov}_oauth_429",
                title="OAuth 请求被限流",
                reason="上游认为请求过于频繁。",
                action="稍后再试。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )
        if status >= 500:
            return OAuthDisplayError(
                code=f"{prov}_oauth_{status}",
                title="OAuth 上游服务异常",
                reason="上游认证或用量接口暂时不可用。",
                action="稍后重试。",
                retryable=True,
                status=status,
                provider=prov,
                operation=op,
                technical=technical,
            )

    return OAuthDisplayError(
        code=f"{prov}_{op}_failed",
        title="OAuth 操作失败",
        reason="本次操作没有完成，具体原因已记录到日志。",
        action="稍后重试；如果持续失败，请重新登录对应账号。",
        retryable=True,
        status=status,
        provider=prov,
        operation=op,
        technical=technical,
    )


def format_oauth_error_html(
    exc: BaseException | str | OAuthDisplayError,
    *,
    provider: str | None = None,
    operation: str | None = None,
    indent: str = "",
    include_title: bool = True,
    include_reason: bool = True,
    include_action: bool = True,
    include_code: bool = True,
) -> str:
    """Return Telegram-HTML-safe multi-line error text."""
    err = describe_oauth_error(exc, provider=provider, operation=operation)
    lines: list[str] = []
    if include_title:
        lines.append(f"❌ {html.escape(err.title)}")
    if include_reason and err.reason:
        lines.append(f"• 原因：{html.escape(err.reason)}")
    if include_action and err.action:
        lines.append(f"• 处理：{html.escape(err.action)}")
    if include_code and err.code:
        lines.append(f"• 详情码：<code>{html.escape(err.code)}</code>")
    if not lines:
        lines.append(f"❌ {html.escape(err.title)}")
    if indent:
        return "\n".join(indent + line if line else line for line in lines)
    return "\n".join(lines)


def technical_detail(exc: BaseException | str | OAuthDisplayError) -> str:
    """Raw-ish detail for logs only. Never put this directly in Telegram UI."""
    if isinstance(exc, OAuthDisplayError):
        return exc.technical
    return _technical_from_exception(exc)
