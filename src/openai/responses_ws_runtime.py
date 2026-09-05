"""Pure helpers for OpenAI/Codex Responses WebSocket runtime paths."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from ..channel.compatibility import apply_forced_openai_fast_mode
from ..channel.openai_oauth_channel import OpenAIOAuthChannel, _provider_cfg
from ..openai.transform import codex_oauth_transform
from ..providers import registry as provider_registry
from ..transports.ws_runtime import http_url_to_ws
from .codex_constants import (
    CODEX_RESPONSES_LITE_HEADER,
    CODEX_RESPONSES_LITE_WS_METADATA_KEY,
    CodexModelPolicy,
    codex_cli_user_agent,
    codex_cli_version,
    codex_originator,
    codex_protocol_profile,
    codex_responses_websocket_beta,
    resolve_codex_model_policy,
)
from .codex_identity import RequestIdentitySnapshot, project_snapshot
from .codex_identity_mapper import ProtocolIdentityMap, expose_response_payload


_OPENAI_RESPONSES_API_CHANNEL = SimpleNamespace(protocol="openai-responses", type="api")
_OPENAI_CODEX_CHANNEL = SimpleNamespace(protocol="openai-responses", type="oauth")
_RESPONSES_WS_FRAME_EXTRA_FIELDS = frozenset({"generate"})


SKIP_WS_HEADERS = {
    "host",
    "connection",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
    "content-length",
    "accept-encoding",
    # HTTP/SSE negotiation and the real Lite header never belong on a WS
    # handshake. Lite is represented only in response.create.client_metadata.
    "accept",
    "content-type",
    "origin",
    CODEX_RESPONSES_LITE_HEADER,
}

SKIP_OAUTH_WS_HEADERS = set(SKIP_WS_HEADERS) | {"openai-beta"}


def drop_headers_case_insensitive(headers: dict[str, str], names: set[str]) -> dict[str, str]:
    drop = {n.lower() for n in names}
    return {k: v for k, v in (headers or {}).items() if str(k).lower() not in drop}


def get_header_case_insensitive(headers: dict[str, str] | None, key: str) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if str(k).lower() == key.lower():
            return str(v)
    return ""


def flatten_ws_response_headers(headers: Any) -> dict[str, str]:
    """Flatten WS handshake headers without failing on repeated fields.

    ``websockets.Headers.items()`` raises ``MultipleValuesError`` for legal
    repeated response fields such as ``Set-Cookie``.  ``raw_items()`` preserves
    those wire entries; converting the resulting pairs to a plain dict is safe
    for Codex quota parsing because it reads only single-valued ``x-codex-*``
    fields.
    """
    if headers is None:
        return {}
    raw_items = getattr(headers, "raw_items", None)
    if callable(raw_items):
        items = raw_items()
    else:
        items_method = getattr(headers, "items", None)
        if not callable(items_method):
            return {}
        items = items_method()
    return {str(key): str(value) for key, value in items}


def merge_oauth_responses_ws_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in SKIP_OAUTH_WS_HEADERS:
            continue
        out[str(k)] = str(v)
    out["OpenAI-Beta"] = codex_responses_websocket_beta()
    out.setdefault("originator", codex_originator())
    out.setdefault("version", codex_cli_version())
    # The channel-built header already used the same provider-config snapshot.
    # Preserve it across canonical casing instead of reverting to a static UA.
    configured_ua = get_header_case_insensitive(out, "user-agent")
    out = drop_headers_case_insensitive(out, {"user-agent"})
    out["User-Agent"] = configured_ua or codex_cli_user_agent()

    sid = out.get("session-id") or out.get("session_id")
    tid = out.get("thread-id") or sid
    if sid:
        out.setdefault("session-id", sid)
    if tid:
        out.setdefault("thread-id", tid)
        out.setdefault("x-client-request-id", tid)
    return drop_headers_case_insensitive(out, {"session_id", "conversation_id", "conversation-id"})


def ensure_oauth_responses_ws_session_headers(headers: dict[str, str], body: dict) -> None:
    """Retain authoritative UUIDv7 headers and remove only deprecated spellings."""
    sid = headers.get("session-id")
    tid = headers.get("thread-id")
    if sid and tid:
        headers["x-client-request-id"] = tid
    for key in [k for k in list(headers) if str(k).lower() in ("session_id", "conversation_id", "conversation-id")]:
        del headers[key]


def _filter_responses_payload(
    body: dict,
    *,
    channel=None,
    codex: bool = False,
    preserve_ws_frame_fields: bool = False,
) -> dict:
    selected = channel or (_OPENAI_CODEX_CHANNEL if codex else _OPENAI_RESPONSES_API_CHANNEL)
    filtered = provider_registry.filter_request_payload(
        selected,
        body,
        protocol="openai-responses",
    )
    if preserve_ws_frame_fields:
        for key in _RESPONSES_WS_FRAME_EXTRA_FIELDS:
            if key in body:
                filtered[key] = body[key]
    return filtered


def _channel_model_policy(channel: Any, model: str | None) -> CodexModelPolicy:
    resolver = getattr(channel, "codex_model_policy", None)
    if callable(resolver):
        return resolver(str(model or ""), _provider_cfg())
    return resolve_codex_model_policy(model, provider_config=_provider_cfg())


def _channel_uses_responses_lite(channel: Any, model: str | None) -> bool:
    return _channel_model_policy(channel, model).use_responses_lite


def _codex_transform_policy_kwargs(channel: Any, model: str | None, body: dict) -> dict:
    provider_config = _provider_cfg()
    resolver = getattr(channel, "codex_model_policy", None)
    if callable(resolver):
        policy = resolver(str(model or ""), provider_config)
    else:
        policy = resolve_codex_model_policy(
            model, provider_config=provider_config
        )
    base_instructions = policy.base_instructions
    configured_default = provider_config.get("defaultInstructions")
    if base_instructions is None and isinstance(configured_default, str):
        base_instructions = configured_default.strip() or None
    return {
        "base_instructions": base_instructions,
        "default_reasoning_effort": policy.default_reasoning_effort,
        "default_verbosity": policy.default_verbosity,
        "supported_reasoning_efforts": policy.reasoning_efforts,
        "multi_agent_reasoning_effort": policy.multi_agent_reasoning_effort,
        "lite_thread_context": str((body or {}).get("prompt_cache_key") or "").strip(),
        "use_responses_lite": policy.use_responses_lite,
        "request_field_policies": dict(
            codex_protocol_profile(provider_config).request_field_policies
        ),
    }


def _mark_codex_responses_lite_frame(
    frame_obj: dict,
    model: str | None = None,
    *,
    use_responses_lite: bool | None = None,
) -> None:
    is_lite = (
        use_responses_lite
        if isinstance(use_responses_lite, bool)
        else _channel_uses_responses_lite(None, model or frame_obj.get("model"))
    )
    if not is_lite:
        client_metadata = frame_obj.get("client_metadata")
        if isinstance(client_metadata, dict):
            client_metadata.pop(CODEX_RESPONSES_LITE_WS_METADATA_KEY, None)
        return
    client_metadata = frame_obj.get("client_metadata")
    if not isinstance(client_metadata, dict):
        client_metadata = {}
    client_metadata[CODEX_RESPONSES_LITE_WS_METADATA_KEY] = "true"
    frame_obj["client_metadata"] = client_metadata


def build_oauth_responses_ws_frame(body: dict, resolved_model: str, *, channel=None) -> dict:
    payload = _filter_responses_payload(body, channel=channel, codex=True)
    payload["model"] = resolved_model
    payload.pop("background", None)
    transform_policy = _codex_transform_policy_kwargs(
        channel, resolved_model, payload
    )
    responses_lite = transform_policy["use_responses_lite"]
    payload = codex_oauth_transform.apply_codex_oauth_transform(
        payload,
        resolved_model=resolved_model,
        transport="websocket",
        **transform_policy,
    )
    payload["type"] = "response.create"
    _mark_codex_responses_lite_frame(
        payload, resolved_model, use_responses_lite=responses_lite,
    )
    return payload


def _frame_from_oauth_upstream_request(upstream_req, resolved_model: str) -> dict | None:
    raw = getattr(upstream_req, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    frame_obj = dict(payload)
    frame_obj["model"] = frame_obj.get("model") or resolved_model
    frame_obj.pop("background", None)
    frame_obj["type"] = "response.create"
    return frame_obj


def prepare_oauth_responses_ws_request_parts(
    upstream_req,
    body: dict,
    resolved_model: str,
    *,
    channel=None,
) -> tuple[str, dict[str, str], str, ProtocolIdentityMap]:
    ws_url = http_url_to_ws(upstream_req.url)
    headers = merge_oauth_responses_ws_headers(upstream_req.headers)
    ensure_oauth_responses_ws_session_headers(headers, body)
    frame_obj = _frame_from_oauth_upstream_request(upstream_req, resolved_model)
    if frame_obj is None:
        frame_obj = build_oauth_responses_ws_frame(body, resolved_model, channel=channel)
    else:
        _mark_codex_responses_lite_frame(
            frame_obj,
            resolved_model,
            use_responses_lite=_channel_uses_responses_lite(channel, resolved_model),
        )

    translator_ctx = getattr(upstream_req, "translator_ctx", None) or {}
    snapshot = translator_ctx.get("codex_identity_snapshot")
    if not isinstance(snapshot, RequestIdentitySnapshot):
        raise ValueError("OpenAI OAuth WS request is missing its identity snapshot")
    headers, projected = project_snapshot(
        snapshot,
        headers,
        frame_obj,
        direct_installation_header=False,
        create_client_metadata=True,
    )
    assert projected is not None
    frame = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    return ws_url, headers, frame, ProtocolIdentityMap.from_request(body, snapshot)


def merge_responses_ws_headers(
    upstream_headers: dict[str, str],
    downstream_headers,
    *,
    forward_client_headers: set[str],
    preserve_upstream_user_agent: bool = False,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (upstream_headers or {}).items():
        lk = str(k).lower()
        if lk in SKIP_WS_HEADERS:
            continue
        if lk == "openai-beta":
            continue
        out[str(k)] = str(v)

    out["OpenAI-Beta"] = codex_responses_websocket_beta()
    out.setdefault("originator", codex_originator())
    out.setdefault("version", codex_cli_version())
    configured_ua = get_header_case_insensitive(out, "user-agent")
    out = drop_headers_case_insensitive(out, {"user-agent"})
    out["User-Agent"] = configured_ua or codex_cli_user_agent()

    incoming_sid = downstream_headers.get("session-id") or downstream_headers.get("session_id")
    incoming_tid = (
        downstream_headers.get("thread-id")
        or downstream_headers.get("conversation_id")
        or downstream_headers.get("x-codex-thread-id")
    )
    if incoming_sid:
        out.setdefault("session-id", incoming_sid)
    if incoming_tid:
        out.setdefault("thread-id", incoming_tid)

    for name in forward_client_headers:
        val = downstream_headers.get(name)
        if val:
            out[name] = val

    ua = downstream_headers.get("user-agent")
    if not preserve_upstream_user_agent and ua and "codex" in ua.lower():
        out["User-Agent"] = ua
    for key in [k for k in list(out) if str(k).lower() in ("session_id", "conversation_id", "conversation-id")]:
        del out[key]
    return out


def request_body_from_ws_create(obj: dict) -> dict:
    body = dict(obj)
    body.pop("type", None)
    body.pop("generate", None)
    body.pop("client_metadata", None)
    return body


def sync_prompt_cache_key_to_ws_create(obj: dict, body: dict) -> None:
    if body.get("model"):
        obj["model"] = body["model"]
    if body.get("prompt_cache_key"):
        obj["prompt_cache_key"] = body["prompt_cache_key"]
    if "background" in obj and "background" not in body:
        obj.pop("background", None)


def sync_translated_body_to_ws_create(obj: dict, body: dict) -> None:
    sync_prompt_cache_key_to_ws_create(obj, body)
    if "input" in body:
        obj["input"] = body["input"]
    if "instructions" in body:
        obj["instructions"] = body["instructions"]


def map_ws_create_frame_for_upstream(obj: dict, model: str, *, channel=None) -> dict:
    out = dict(obj)
    typ = out.pop("type", None)
    out = _filter_responses_payload(
        out,
        channel=channel,
        preserve_ws_frame_fields=True,
    )
    if model:
        out["model"] = model
    if "background" in out:
        out.pop("background", None)
    if channel is not None:
        apply_forced_openai_fast_mode(channel, out, model)
    if isinstance(channel, OpenAIOAuthChannel):
        transform_policy = _codex_transform_policy_kwargs(channel, model, out)
        responses_lite = transform_policy["use_responses_lite"]
        out = codex_oauth_transform.apply_codex_oauth_transform(
            out,
            resolved_model=model,
            transport="websocket",
            **transform_policy,
        )
        _mark_codex_responses_lite_frame(
            out, model, use_responses_lite=responses_lite,
        )
    if typ:
        out["type"] = typ
    return out


def loads_frame(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


def dump_frame(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def identity_expose_frame(data: str | bytes, state: ProtocolIdentityMap) -> str | bytes:
    if not state.enabled:
        return data
    if isinstance(data, str):
        return expose_response_payload(data.encode("utf-8"), state).decode("utf-8", errors="replace")
    if isinstance(data, (bytes, bytearray)):
        return expose_response_payload(bytes(data), state)
    return data


def identity_log_text(text: str, state: ProtocolIdentityMap) -> str:
    if not text or not state.enabled:
        return text
    try:
        return expose_response_payload(text.encode("utf-8"), state).decode("utf-8", errors="replace")
    except Exception:
        return text
