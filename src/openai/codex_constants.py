"""Codex CLI wire identity and forward-compatible service-tier helpers.

The default version follows the verified official Codex release, while the
runtime value comes from ``openaiOAuth.codexCliVersion``.  Every model-catalog,
HTTP and WebSocket caller must resolve the value through this module so a future
``minimal_client_version`` bump does not require a Parrot source release.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


DEFAULT_CODEX_CLI_VERSION = "0.144.0"
# Backward-compatible immutable aliases for older imports/tests. Production wire
# builders use ``codex_cli_version()`` / ``codex_cli_user_agent()`` below.
CODEX_CLI_VERSION = DEFAULT_CODEX_CLI_VERSION

# Codex originator（codex-rs/login/src/auth/default_client.rs DEFAULT_ORIGINATOR）
CODEX_ORIGINATOR = "codex_cli_rs"

_CODEX_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$"
)
_CODEX_COMPONENT_ALLOWED = frozenset("._:/-")


def normalize_codex_cli_version(value: Any) -> str | None:
    """Return one safe semantic Codex version, or ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    version = value.strip()
    return version if _CODEX_VERSION_RE.fullmatch(version) else None


def _runtime_provider_config() -> Mapping[str, Any]:
    """Read the normalized OpenAI OAuth config lazily to avoid import cycles."""
    try:
        from .. import config
        cfg = config.get()
        default = config.DEFAULT_CONFIG.get("openaiOAuth") or {}
        current = cfg.get("openaiOAuth") or {}
        legacy = (((cfg.get("oauth") or {}).get("providers") or {}).get("openai") or {})
        if not isinstance(default, dict):
            default = {}
        if not isinstance(current, dict):
            current = {}
        if not isinstance(legacy, dict):
            legacy = {}
        # Match OpenAIOAuthChannel's legacy fallback for partially loaded tests
        # and old installations; config normalization handles normal production.
        source = legacy if legacy and current == default else current
        merged = dict(default)
        merged.update(source)
        return merged
    except Exception:
        return {}


def codex_cli_version(provider_config: Mapping[str, Any] | None = None) -> str:
    """Return the configured effective Codex CLI version, safely defaulted."""
    cfg = provider_config if isinstance(provider_config, Mapping) else _runtime_provider_config()
    return (
        normalize_codex_cli_version(cfg.get("codexCliVersion"))
        or DEFAULT_CODEX_CLI_VERSION
    )


def build_codex_cli_user_agent(version: str | None = None) -> str:
    effective = normalize_codex_cli_version(version) or DEFAULT_CODEX_CLI_VERSION
    return (
        f"{CODEX_ORIGINATOR}/{effective}"
        " (Mac OS 26.5.0; arm64)"
        " iTerm.app/3.6.10"
    )


def codex_cli_user_agent(provider_config: Mapping[str, Any] | None = None) -> str:
    return build_codex_cli_user_agent(codex_cli_version(provider_config))


# Backward-compatible default identity. Runtime callers must use the function.
CODEX_CLI_USER_AGENT = build_codex_cli_user_agent(DEFAULT_CODEX_CLI_VERSION)


def codex_version_meets_minimum(current: Any, minimum: Any) -> bool | None:
    """Compare numeric SemVer cores; return ``None`` for an unknown schema."""
    current_text = normalize_codex_cli_version(current)
    minimum_text = normalize_codex_cli_version(minimum)
    if not current_text or not minimum_text:
        return None
    current_match = _CODEX_VERSION_RE.fullmatch(current_text)
    minimum_match = _CODEX_VERSION_RE.fullmatch(minimum_text)
    if not current_match or not minimum_match:
        return None
    current_core = tuple(int(value) for value in current_match.groups())
    minimum_core = tuple(int(value) for value in minimum_match.groups())
    return current_core >= minimum_core


def normalize_codex_service_tier(value: Any) -> str | None:
    """Validate one opaque Codex tier token without guessing its semantics."""
    if not isinstance(value, str):
        return None
    tier = value.strip()
    if not tier or len(tier) > 64:
        return None
    if not all(
        ch.isascii() and (ch.isalnum() or ch in _CODEX_COMPONENT_ALLOWED)
        for ch in tier
    ):
        return None
    return tier

# Responses WebSocket beta header（codex-rs/core/src/client.rs）
RESPONSES_WEBSOCKETS_BETA = "responses_websockets=2026-02-06"

# ChatGPT Codex backend 在 HTTP 请求和 WebSocket 握手阶段使用该 hint，
# 在读取 response.create body 前按最终 model/service_tier 选择连接路由。
CODEX_ROUTING_HINT_HEADER = "x-codex-routing-hint"


def build_codex_routing_hint(
    model: str | None,
    service_tier: str | None = None,
) -> str | None:
    """Build the official ``model=<id>[;tier=<id>]`` routing hint safely.

    Model and tier normally come from the account-scoped Codex catalog.  Keep a
    strict ASCII component grammar because downstream request fields are still
    untrusted and must never be able to inject another HTTP header/directive.
    The Codex ``default`` value is a client-side sentinel for standard routing,
    not a catalog tier, so it is represented by omitting ``tier``.
    """
    def component(value: str | None, *, max_length: int = 256) -> str:
        text = str(value or "").strip()
        if not text or len(text) > max_length or not all(
            ch.isascii() and (ch.isalnum() or ch in _CODEX_COMPONENT_ALLOWED)
            for ch in text
        ):
            return ""
        return text

    model_id = component(model)
    if not model_id:
        return None
    tier_id = normalize_codex_service_tier(service_tier) or ""
    if tier_id.lower() in {"default", "auto"}:
        tier_id = ""
    return f"model={model_id}" + (f";tier={tier_id}" if tier_id else "")


# Responses Lite 标记（官方 models.json: use_responses_lite=true）。
# 保留 gpt-5.6-* 前缀以兼容该系列后续变体；非该前缀的 Lite 模型必须显式登记。
CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
CODEX_RESPONSES_LITE_WS_METADATA_KEY = "ws_request_header_x_openai_internal_codex_responses_lite"
CODEX_RESPONSES_LITE_MODEL_PREFIXES = ("gpt-5.6-",)
CODEX_RESPONSES_LITE_MODELS = frozenset({
    "gpt-daybreak-blue-latest",
    "gpt-daybreak-red-latest",
    "codex-auto-review",
})


def codex_model_uses_responses_lite(model: str | None) -> bool:
    """Return whether official Codex marks this model as Responses Lite."""
    m = str(model or "").strip().lower()
    return m in CODEX_RESPONSES_LITE_MODELS or any(
        m.startswith(prefix) for prefix in CODEX_RESPONSES_LITE_MODEL_PREFIXES
    )
