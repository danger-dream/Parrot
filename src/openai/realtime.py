"""Codex Realtime transparent relay backed by existing OpenAI OAuth accounts.

This module deliberately doesn't parse or transform realtime protocol frames.  It
only authenticates a Parrot client, injects the selected Codex OAuth identity
upstream, and relays WebSocket / WebRTC-call traffic unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from .. import apikey_limiter, auth, concurrency, config, errors, load_balancing, network, scorer
from ..channel import registry
from ..channel.openai_oauth_channel import OpenAIOAuthChannel
from ..transports import WsProxyBytes, connect_upstream_ws, resolve_ws_route_chain


# This is only an internal scheduler/proxy-routing bucket.  It is never sent to
# the realtime backend and doesn't imply a model mapping.
_REALTIME_ROUTE_MODEL_PREFIX = "__codex_realtime__"
_REALTIME_WS_API_BASE_URL = "https://api.openai.com"
_CALL_BINDING_TTL_SECONDS = 300

# The client bearer is intentionally excluded: Parrot replaces it with the
# selected OAuth account's bearer.  These headers are the realtime session and
# Codex-control metadata that must retain their wire meaning.
_REALTIME_FORWARD_HEADERS = frozenset({
    "openai-alpha",
    "openai-beta",
    "x-session-id",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-installation-id",
    "x-openai-organization",
    "x-openai-project",
})

_HOP_BY_HOP_RESPONSE_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    # httpx exposes decoded content, so these wire-level headers cannot be
    # forwarded unchanged.
    "content-encoding",
    # An upstream ChatGPT cookie is neither needed nor safe to hand to a
    # downstream Parrot API-key client.
    "set-cookie",
})


@dataclass(frozen=True)
class _CallBinding:
    channel_key: str
    api_key_name: str
    model: str
    expires_at: float


_call_bindings: dict[str, _CallBinding] = {}
_call_bindings_lock = asyncio.Lock()


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _connect_timeout_seconds() -> float:
    return _positive_float((config.get().get("timeouts") or {}).get("connect"), 10.0)


def _call_timeout() -> httpx.Timeout:
    timeouts = config.get().get("timeouts") or {}
    connect = _connect_timeout_seconds()
    first_byte = _positive_float(timeouts.get("firstByte"), 30.0)
    return httpx.Timeout(first_byte, connect=connect)


def _route_model(model: str | None) -> str:
    normalized = str(model or "default").strip() or "default"
    # Keep malformed query values from becoming large in-memory routing keys.
    return f"{_REALTIME_ROUTE_MODEL_PREFIX}:{normalized[:160]}"


def _codex_backend_base_url() -> str:
    """Return the configured Codex backend root, without its /responses suffix."""
    raw = str(
        ((config.get().get("openaiOAuth") or {}).get("codexUpstreamUrl"))
        or "https://chatgpt.com/backend-api/codex/responses"
    ).strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("openaiOAuth.codexUpstreamUrl must be an absolute URL")

    path = parsed.path.rstrip("/")
    if path.endswith("/responses"):
        path = path[:-len("/responses")]
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _realtime_ws_url(path: str, query: str) -> str:
    """Build the OpenAI API URL used by Codex realtime WebSockets."""
    base = urlsplit(_REALTIME_WS_API_BASE_URL)
    if base.scheme == "https":
        scheme = "wss"
    elif base.scheme == "http":
        scheme = "ws"
    elif base.scheme in ("ws", "wss"):
        scheme = base.scheme
    else:
        raise ValueError(f"unsupported OpenAI realtime upstream scheme: {base.scheme}")

    joined_path = f"{base.path.rstrip('/')}/{path.lstrip('/')}"
    return urlunsplit((scheme, base.netloc, joined_path, query, ""))


def _realtime_call_url(query: str) -> str:
    """Build the ChatGPT Codex backend URL used to create WebRTC calls."""
    base = urlsplit(_codex_backend_base_url())
    if base.scheme not in ("http", "https"):
        raise ValueError(f"unsupported Codex realtime call scheme: {base.scheme}")

    joined_path = f"{base.path.rstrip('/')}/realtime/calls"
    return urlunsplit((base.scheme, base.netloc, joined_path, query, ""))


def _raw_query(scope: dict) -> str:
    raw = scope.get("query_string") or b""
    if isinstance(raw, bytes):
        return raw.decode("ascii", errors="surrogateescape")
    return str(raw)


def _should_forward_realtime_header(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _REALTIME_FORWARD_HEADERS
        or lowered.startswith("x-codex-")
        or lowered.startswith("x-openai-")
    )


async def _build_realtime_headers(
    channel: OpenAIOAuthChannel,
    downstream_headers,
    *,
    content_type: str | None = None,
) -> dict[str, str]:
    """Build OAuth headers once, then retain only realtime-safe client metadata."""
    headers = await channel.build_realtime_headers()
    installation_id = str(
        getattr(channel, "codex_device_installation_id", "") or ""
    )
    for name, value in downstream_headers.items():
        if _should_forward_realtime_header(str(name)):
            headers[str(name)] = str(value)
    # The selected OAuth workspace always wins over a downstream carrier.
    for name in [key for key in headers if str(key).lower() == "x-codex-installation-id"]:
        headers.pop(name, None)
    if not installation_id:
        raise ValueError("OpenAI OAuth realtime identity is unavailable")
    headers["x-codex-installation-id"] = installation_id

    if content_type is not None:
        headers["content-type"] = content_type
        headers["accept"] = str(downstream_headers.get("accept") or "*/*")
    return headers


def _eligible_oauth_channels(model: str | None) -> list[OpenAIOAuthChannel]:
    """Choose from the existing OpenAI OAuth pool without treating realtime as Responses."""
    candidates: list[tuple[OpenAIOAuthChannel, str]] = []
    marker = _route_model(model)
    for channel in registry.all_channels():
        if not isinstance(channel, OpenAIOAuthChannel):
            continue
        if not channel.enabled or channel.disabled_reason:
            continue
        candidates.append((channel, marker))

    selection = str(config.get().get("channelSelection") or "smart").lower()
    if selection == "smart":
        candidates = scorer.sort_by_score(candidates)
    elif selection == "priority":
        candidates = load_balancing.sort_candidates_by_priority(
            candidates, config.get(), requested_model=str(model or marker),
        )
    return [channel for channel, _marker in candidates]


async def _acquire_first_available(
    candidates: Iterable[OpenAIOAuthChannel],
) -> OpenAIOAuthChannel | None:
    """Realtime sessions fail fast rather than waiting in a channel queue."""
    for channel in candidates:
        if await concurrency.try_acquire(channel.key):
            return channel
    return None


async def _store_call_binding(
    call_id: str,
    *,
    channel: OpenAIOAuthChannel,
    api_key_name: str,
    model: str | None,
) -> None:
    if not call_id:
        return
    now = time.monotonic()
    binding = _CallBinding(
        channel_key=channel.key,
        api_key_name=api_key_name,
        model=str(model or ""),
        expires_at=now + _CALL_BINDING_TTL_SECONDS,
    )
    async with _call_bindings_lock:
        expired = [key for key, value in _call_bindings.items() if value.expires_at <= now]
        for key in expired:
            _call_bindings.pop(key, None)
        _call_bindings[call_id] = binding


async def _lookup_call_binding(call_id: str) -> _CallBinding | None:
    if not call_id:
        return None
    now = time.monotonic()
    async with _call_bindings_lock:
        binding = _call_bindings.get(call_id)
        if binding is not None and binding.expires_at <= now:
            _call_bindings.pop(call_id, None)
            binding = None
        return binding


def _call_id_from_location(location: str | None) -> str:
    if not location:
        return ""
    path = urlsplit(location).path
    candidate = path.rstrip("/").rsplit("/", 1)[-1].strip()
    return candidate if candidate else ""


def _backend_call_model(body: bytes) -> str | None:
    """Read only the model needed for existing API-key allowlist enforcement."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session = payload.get("session")
    if not isinstance(session, dict):
        return None
    model = session.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _model_allowed(model: str | None, allowed_models: list[str]) -> bool:
    if not allowed_models:
        return True
    return bool(model and model in allowed_models)


def _trim_close_reason(message: object, limit: int = 120) -> str:
    text = str(message or "").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


async def _reject_ws(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=_trim_close_reason(reason))
    except Exception:
        pass


async def _connect_realtime_upstream(
    url: str,
    *,
    headers: dict[str, str],
    channel: OpenAIOAuthChannel,
    model: str | None,
):
    """Connect through the same configured WS proxy chain as OAuth Responses WS."""
    proxy_bytes = WsProxyBytes()
    route_model = _route_model(model)
    last_error: Exception | None = None
    for _route_name, connector in resolve_ws_route_chain(channel, route_model):
        try:
            return await connect_upstream_ws(
                url,
                headers=headers,
                connector=connector,
                proxy_bytes=proxy_bytes,
                open_timeout=_connect_timeout_seconds(),
            )
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no usable realtime WebSocket proxy route")


async def _close_upstream(upstream_ws) -> None:
    try:
        await upstream_ws.close()
    except Exception:
        pass


async def _close_downstream_from_upstream(websocket: WebSocket, upstream_ws, error: Exception | None) -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    if error is not None:
        await _reject_ws(websocket, 1011, "upstream realtime connection closed")
        return

    code = getattr(upstream_ws, "close_code", None)
    if code in (1005, 1006, 1015) or not isinstance(code, int) or not 1000 <= code <= 4999:
        code = 1000
    reason = _trim_close_reason(getattr(upstream_ws, "close_reason", ""))
    await _reject_ws(websocket, code, reason)


async def _relay_ws_session(websocket: WebSocket, upstream_ws) -> None:
    """Relay text and binary websocket frames without examining their payloads."""
    async def downstream_to_upstream() -> str:
        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return "downstream_closed"
            if message_type != "websocket.receive":
                continue
            if message.get("text") is not None:
                await upstream_ws.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream_ws.send(message["bytes"])

    async def upstream_to_downstream() -> str:
        while True:
            data = await upstream_ws.recv()
            if isinstance(data, str):
                await websocket.send_text(data)
            else:
                await websocket.send_bytes(data)

    down_task = asyncio.create_task(downstream_to_upstream(), name="realtime-downstream-to-upstream")
    up_task = asyncio.create_task(upstream_to_downstream(), name="realtime-upstream-to-downstream")
    try:
        done, pending = await asyncio.wait(
            {down_task, up_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        upstream_error: Exception | None = None
        if up_task in done:
            try:
                up_task.result()
            except ConnectionClosed:
                # The close code / reason lives on the connection object.
                pass
            except Exception as exc:
                upstream_error = exc

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if up_task in done:
            await _close_downstream_from_upstream(websocket, upstream_ws, upstream_error)
    finally:
        for task in (down_task, up_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(down_task, up_task, return_exceptions=True)
        await _close_upstream(upstream_ws)


async def _resolve_bound_channel(
    call_id: str,
    *,
    api_key_name: str,
) -> tuple[OpenAIOAuthChannel | None, _CallBinding | None, str]:
    binding = await _lookup_call_binding(call_id)
    if binding is None:
        return None, None, "unknown or expired realtime call_id"
    if binding.api_key_name != api_key_name:
        return None, None, "realtime call_id belongs to a different API key"

    channel = registry.get_channel(binding.channel_key)
    if not isinstance(channel, OpenAIOAuthChannel):
        return None, None, "realtime call OAuth account is no longer available"
    if not channel.enabled or channel.disabled_reason:
        return None, None, "realtime call OAuth account is unavailable"
    return channel, binding, ""


async def handle_realtime_ws(
    websocket: WebSocket,
    *,
    path: str,
    live_call_id: str | None = None,
) -> None:
    """Authenticate and transparently relay one Codex realtime WebSocket session."""
    key_name, allowed_models, auth_error = auth.validate(websocket.headers)
    if auth_error:
        await _reject_ws(websocket, 4401, auth_error)
        return
    assert key_name is not None

    query_call_id = str(websocket.query_params.get("call_id") or "").strip()
    call_id = str(live_call_id or query_call_id).strip()
    model = str(websocket.query_params.get("model") or "").strip() or None

    channel: OpenAIOAuthChannel | None = None
    selected_model = model
    candidates: list[OpenAIOAuthChannel] = []
    if call_id:
        channel, binding, selection_error = await _resolve_bound_channel(
            call_id,
            api_key_name=key_name,
        )
        if channel is None or binding is None:
            await _reject_ws(websocket, 4404, selection_error)
            return
        # The call-create request already enforced this key's model allowlist.
        selected_model = model or binding.model or None
    else:
        if not _model_allowed(model, allowed_models):
            await _reject_ws(websocket, 4403, "model is not allowed for this API key")
            return
        candidates = _eligible_oauth_channels(model)

    key_lease = None
    channel_acquired = False
    upstream_ws = None
    try:
        try:
            key_lease = await apikey_limiter.acquire(key_name, None)
        except apikey_limiter.ApiKeyLimitError as exc:
            await _reject_ws(websocket, 4429, exc.message)
            return

        if call_id:
            assert channel is not None
            channel_acquired = await concurrency.try_acquire(channel.key)
            if not channel_acquired:
                await _reject_ws(websocket, 1013, "realtime OAuth account is at capacity")
                return
        else:
            channel = await _acquire_first_available(candidates)
            if channel is None:
                await _reject_ws(websocket, 1013, "no available OpenAI OAuth account for realtime")
                return
            channel_acquired = True

        assert channel is not None
        headers = await _build_realtime_headers(channel, websocket.headers)
        upstream_url = _realtime_ws_url(path, _raw_query(websocket.scope))
        try:
            upstream_ws = await _connect_realtime_upstream(
                upstream_url,
                headers=headers,
                channel=channel,
                model=selected_model,
            )
        except Exception as exc:
            print(
                f"[realtime] upstream websocket connect failed channel={channel.key} "
                f"type={type(exc).__name__}"
            )
            await _reject_ws(websocket, 1013, "unable to establish upstream realtime session")
            return

        await websocket.accept()
        await _relay_ws_session(websocket, upstream_ws)
        upstream_ws = None  # _relay_ws_session owns and closes it.
    finally:
        if upstream_ws is not None:
            await _close_upstream(upstream_ws)
        if channel_acquired and channel is not None:
            concurrency.release(channel.key)
        if key_lease is not None:
            await key_lease.release()


async def _post_realtime_call(
    url: str,
    *,
    headers: dict[str, str],
    body: bytes,
    channel: OpenAIOAuthChannel,
    model: str | None,
) -> httpx.Response:
    async with network.async_client(
        timeout=_call_timeout(),
        proxy_purpose="oauth_openai",
        proxy_channel=channel.key,
        proxy_model=_route_model(model),
        follow_redirects=False,
    ) as client:
        return await client.post(url, headers=headers, content=body)


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS:
            out[name] = value
    return out


async def handle_realtime_call(request: Request) -> Response:
    """Proxy Codex's backend-shaped WebRTC call creation request unchanged."""
    key_name, allowed_models, auth_error = auth.validate(request.headers)
    if auth_error:
        return errors.json_error_openai(401, errors.ErrTypeOpenAI.AUTH, auth_error)
    assert key_name is not None

    body = await request.body()
    model = _backend_call_model(body)
    if not _model_allowed(model, allowed_models):
        return errors.json_error_openai(
            403,
            errors.ErrTypeOpenAI.PERMISSION,
            "model is not allowed for this API key",
        )

    channel = await _acquire_first_available(_eligible_oauth_channels(model))
    if channel is None:
        return errors.json_error_openai(
            503,
            errors.ErrTypeOpenAI.SERVER,
            "no available OpenAI OAuth account for realtime",
        )

    try:
        content_type = str(request.headers.get("content-type") or "application/json")
        headers = await _build_realtime_headers(
            channel,
            request.headers,
            content_type=content_type,
        )
        upstream_url = _realtime_call_url(_raw_query(request.scope))
        try:
            upstream_response = await _post_realtime_call(
                upstream_url,
                headers=headers,
                body=body,
                channel=channel,
                model=model,
            )
        except httpx.TimeoutException:
            return errors.json_error_openai(
                504,
                errors.ErrTypeOpenAI.TIMEOUT,
                "upstream realtime call creation timed out",
            )
        except Exception as exc:
            print(
                f"[realtime] upstream call creation failed channel={channel.key} "
                f"type={type(exc).__name__}"
            )
            return errors.json_error_openai(
                502,
                errors.ErrTypeOpenAI.SERVER,
                "upstream realtime call creation failed",
            )

        if 200 <= upstream_response.status_code < 300:
            await _store_call_binding(
                _call_id_from_location(upstream_response.headers.get("location")),
                channel=channel,
                api_key_name=key_name,
                model=model,
            )
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=_safe_response_headers(upstream_response.headers),
        )
    finally:
        concurrency.release(channel.key)
