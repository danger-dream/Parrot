"""Native search bridge parameters: only expressible, candidate-checked mappings.

Same-protocol payloads remain native. Cross-protocol constraints are never
silently removed, and unverified native WebFetch is not invented.
"""
from __future__ import annotations

import copy

from .openai.transform.guard import GuardError

SOURCE_KEY = "_parrot_native_search_source"


def _fail(field):
    raise GuardError(400, "invalid_request_error", f"native_search_constraint_unsupported: {field}; use a compatible native candidate or managed policy", param="tools", scope="candidate")


def _only(tool, fields):
    for key in tool:
        if key not in fields:
            _fail(key)


def _location(value):
    if not isinstance(value, dict):
        _fail("user_location")
    _only(value, {"type", "city", "region", "country", "timezone"})
    if value.get("type", "approximate") != "approximate":
        _fail("user_location.type")
    return {"type": "approximate", **copy.deepcopy(value)}


def to_responses(tool: dict, *, target="generic") -> dict:
    if tool.get("type") != "web_search_20250305":
        _fail(str(tool.get("type")))
    _only(tool, {"type", "name", "allowed_domains", "blocked_domains", "user_location"})
    if tool.get("name", "web_search") != "web_search":
        _fail("name")
    out = {"type": "web_search"}
    filters = {}
    if "allowed_domains" in tool:
        filters["allowed_domains"] = copy.deepcopy(tool["allowed_domains"])
    if "blocked_domains" in tool:
        filters["excluded_domains"] = copy.deepcopy(tool["blocked_domains"])
    if filters:
        out["filters"] = filters
    if "user_location" in tool:
        out["user_location"] = _location(tool["user_location"])
    check_responses_target(out, target=target)
    return out


def check_responses_target(tool: dict, *, target: str):
    # xAI's verified search wire exposes domain filters, not OpenAI's location
    # or context-size controls. OpenAI documents allow-list, not exclusion-list.
    filters = tool.get("filters") or {}
    if target == "xai" and any(k in tool for k in ("user_location", "search_context_size")):
        _fail("xAI user_location/search_context_size")
    if target == "openai" and "excluded_domains" in filters:
        _fail("OpenAI filters.excluded_domains")


def to_anthropic(tool: dict) -> dict:
    typ = str(tool.get("type") or "")
    if not (typ == "web_search" or typ.startswith("web_search_")):
        _fail(typ)
    _only(tool, {"type", "filters", "user_location", "external_web_access"})
    if "external_web_access" in tool and tool["external_web_access"] not in (True, "live"):
        _fail("Anthropic external_web_access (offline/cached)")
    out = {"type": "web_search_20250305", "name": "web_search"}
    filters = tool.get("filters") or {}
    if not isinstance(filters, dict):
        _fail("filters")
    _only(filters, {"allowed_domains", "excluded_domains"})
    if filters.get("allowed_domains") and filters.get("excluded_domains"):
        _fail("Anthropic simultaneous allowed_domains and blocked_domains")
    if "allowed_domains" in filters:
        out["allowed_domains"] = copy.deepcopy(filters["allowed_domains"])
    if "excluded_domains" in filters:
        out["blocked_domains"] = copy.deepcopy(filters["excluded_domains"])
    if "user_location" in tool:
        out["user_location"] = _location(tool["user_location"])
    return out


def _target(channel):
    return "xai" if (getattr(channel, "provider", "") == "xai" or getattr(channel, "provider_id", "") == "xai") else "openai"


def adapt_payload(channel, payload: dict) -> dict:
    """Consume a translator-owned marker before provider serialization."""
    if payload.get(SOURCE_KEY) != "anthropic":
        return payload
    out = dict(payload)
    out.pop(SOURCE_KEY, None)
    for tool in out.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "web_search":
            check_responses_target(tool, target=_target(channel))
    return out


def validate_candidate(body, ingress, channel):
    from . import search_tool_policy
    from .protocols.matrix import ProtocolGuardError
    if search_tool_policy.needs_loop(body):
        body, _ = search_tool_policy.compile_request(body, ingress)
    upstream = getattr(channel, "protocol", "anthropic")
    try:
        for tool, _ in search_tool_policy.declarations(body):
            if search_tool_policy.kind(tool) != "hosted":
                continue
            if upstream == "openai-chat":
                _fail("Chat upstream has no native hosted search tool")
            if ingress == "anthropic" and upstream == "openai-responses":
                to_responses(tool, target=_target(channel))
            elif ingress in ("responses", "chat") and upstream == "anthropic":
                to_anthropic(tool)
    except GuardError as exc:
        raise ProtocolGuardError(exc.message) from exc
