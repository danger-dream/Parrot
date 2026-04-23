"""渠道 URL 处理工具。

背景：原有约定是用户填 baseUrl（如 `https://api.example.com`），代理按协议自动
追加 `/v1/messages` / `/v1/chat/completions` / `/v1/responses`。但少数上游（典型
代表：智谱 Coding Plan Max 的 OpenAI 入口）把接口挂在不标准子路径
`/api/coding/paas/v4/chat/completions`，不带 `/v1`，导致拼出来的 URL 永远 404。

解决方案：允许 baseUrl 直接填完整调用路径；代理在保存时按末段白名单识别并拆分
成 `(baseUrl, apiPath)` 两段存储；运行期若 apiPath 非空，直接 `baseUrl + apiPath`，
否则走老的"baseUrl + /v1/xxx"拼接。

末段白名单（按 `/` 分段取末段，大小写不敏感）：
- `messages`    → anthropic 协议
- `completions` → openai-chat 协议
- `responses`   → openai-responses 协议
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


# 末段 → 对应的协议名
_SUFFIX_TO_PROTOCOL: dict[str, str] = {
    "messages": "anthropic",
    "completions": "openai-chat",
    "responses": "openai-responses",
}


def detect_suffix_protocol(url_or_path: str) -> Optional[str]:
    """按 `/` 分段取末段，返回对应协议名或 None。

    只看最后一段，不关心中间路径长什么样。大小写不敏感。

    >>> detect_suffix_protocol("https://api.example.com/v1/messages")
    'anthropic'
    >>> detect_suffix_protocol("https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    'openai-chat'
    >>> detect_suffix_protocol("https://api.example.com")
    >>> detect_suffix_protocol("")
    """
    s = (url_or_path or "").strip().rstrip("/")
    if not s:
        return None
    last = s.rsplit("/", 1)[-1].lower()
    return _SUFFIX_TO_PROTOCOL.get(last)


def split_base_url(url: str) -> tuple[str, Optional[str]]:
    """若 URL 末段属于协议白名单，拆分为 `(baseUrl, apiPath)`；否则返回 `(url, None)`。

    - 协议白名单见模块文档。
    - baseUrl：`scheme://host[:port]`（不含 path）
    - apiPath：URL 的 path 部分，必以 `/` 开头，不以 `/` 结尾

    如果 URL 含 query / fragment → `ValueError`。

    >>> split_base_url("https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
    ('https://open.bigmodel.cn', '/api/coding/paas/v4/chat/completions')
    >>> split_base_url("https://api.example.com")
    ('https://api.example.com', None)
    >>> split_base_url("https://api.example.com/")
    ('https://api.example.com', None)
    """
    s = (url or "").strip().rstrip("/")
    if not s:
        return "", None
    parsed = urlparse(s)
    if parsed.query:
        raise ValueError("URL 不能包含 query string")
    if parsed.fragment:
        raise ValueError("URL 不能包含 fragment")
    if not parsed.scheme or not parsed.netloc:
        # 无效 URL（如用户输入纯路径），不做拆分，交回原值让上层再校验
        return s, None

    proto = detect_suffix_protocol(s)
    if proto is None:
        return s, None

    base = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")
    if not path:
        # 理论上末段匹配了白名单就必有 path；防御性处理
        return base, None
    return base, path


def normalize_api_path(api_path: Optional[str]) -> Optional[str]:
    """归一化 apiPath：确保以 `/` 开头、去除末尾 `/`、空串转 None。"""
    if not api_path:
        return None
    p = api_path.strip().rstrip("/")
    if not p:
        return None
    if not p.startswith("/"):
        p = "/" + p
    return p


def validate_api_path_for_protocol(api_path: Optional[str], protocol: str) -> Optional[str]:
    """校验 apiPath 是否与 protocol 匹配。匹配返回 None，不匹配返回错误说明。

    - apiPath 为空 → 返回 None（允许，走老拼接逻辑）
    - apiPath 末段不在白名单 → 错误
    - apiPath 末段协议与 protocol 不符 → 错误
    """
    if not api_path:
        return None
    last = api_path.rstrip("/").rsplit("/", 1)[-1].lower()
    expected = _SUFFIX_TO_PROTOCOL.get(last)
    if expected is None:
        return f"apiPath 末段 {last!r} 不是支持的后缀（messages / completions / responses）"
    if expected != protocol:
        return (
            f"apiPath 属于 {expected!r} 协议，与当前选择的协议 {protocol!r} 不匹配"
        )
    return None


def resolve_upstream_url(
    base_url: str,
    api_path: Optional[str],
    default_suffix: str,
) -> str:
    """拼接真实请求 URL。

    - `api_path` 非空 → `base_url + api_path`（优先）
    - 否则 → `base_url + default_suffix`（兼容老配置）
    """
    base = (base_url or "").rstrip("/")
    if api_path:
        p = api_path if api_path.startswith("/") else "/" + api_path
        return base + p.rstrip("/")
    return base + default_suffix
