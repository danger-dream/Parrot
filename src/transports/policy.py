"""Transport selection and proxy policy helpers.

These helpers are pure policy wrappers used by Phase 9 to keep transport details
out of the failover orchestration loop.
"""

from __future__ import annotations

from typing import Optional

from ..channel.openai_oauth_channel import OpenAIOAuthChannel


def proxy_route_kwargs(channel, resolved_model: str) -> dict:
    """Build proxy routing context for a channel/model attempt."""
    proto = getattr(channel, "protocol", "anthropic") or "anthropic"
    purpose = "oauth_anthropic" if proto == "anthropic" else "oauth_openai"
    return {
        "channel_key": channel.key,
        "model": resolved_model,
        "purpose": purpose,
        "account_key": getattr(channel, "account_key", "") or "",
    }


def pick_non_direct_proxy_name(channel, resolved_model: str) -> str | None:
    try:
        from ..proxy import manager as pm
        pm.init()
        target = pm.resolve_proxy_target(**proxy_route_kwargs(channel, resolved_model))
        for name in pm.expand_target(target):
            conn = pm.get_connector(name)
            if conn is not None and conn.type != "direct":
                return name
    except Exception:
        pass
    return None


def proxy_byte_snapshot(proxy_bytes: Optional[dict]) -> tuple[int, int]:
    if not proxy_bytes:
        return 0, 0
    return int(proxy_bytes.get("up") or 0), int(proxy_bytes.get("down") or 0)


def responses_upstream_ws_enabled(cfg: Optional[dict] = None) -> bool:
    """Whether HTTP /v1/responses may use OAuth Codex WS upstream transport."""
    if cfg is None:
        from .. import config
        cfg = config.get()
    openai_cfg = cfg.get("openai") or {}
    if "responsesUpstreamWsForOAuth" in openai_cfg:
        return bool(openai_cfg.get("responsesUpstreamWsForOAuth"))
    transport = str(openai_cfg.get("responsesUpstreamTransport") or "").strip().lower()
    if transport in ("ws", "websocket", "websockets"):
        return True
    if "responsesUpstreamWs" in openai_cfg:
        return bool(openai_cfg.get("responsesUpstreamWs"))
    return False


def should_use_responses_upstream_ws(channel, *, ingress_protocol: str, cfg: Optional[dict] = None) -> bool:
    if ingress_protocol != "responses":
        return False
    if not isinstance(channel, OpenAIOAuthChannel):
        return False
    if getattr(channel, "protocol", "") != "openai-responses":
        return False
    return responses_upstream_ws_enabled(cfg)
