"""``WS /api/management/v1/events``: live request and runtime events.

Close codes: 4401 unauthenticated / session expired or revoked, 4403 Origin
not allowed, 1013 too many connections (or control plane not ready),
1001 server shutting down. Client messages are read and ignored.
"""

from __future__ import annotations

import asyncio
import json

import anyio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from src.management_auth import SessionAuthenticationError
from src.management_control.observability.live_events import LiveEventBus

from ..dependencies import (
    SESSION_COOKIE_NAME,
    ManagementRuntime,
    browser_origin_allowed,
    get_management_control_owner,
)
from ..schemas.observability import ConcurrencyData, RequestLogData
from ..schemas.webui import LiveHelloData, LiveRequestStartedData, LiveRequestUpdatedData
from .status import _revisioned


router = APIRouter()

CLOSE_UNAUTHENTICATED = 4401
CLOSE_ORIGIN_DENIED = 4403
CLOSE_TRY_AGAIN_LATER = 1013
CLOSE_GOING_AWAY = 1001
MAX_CONNECTIONS = 20
SESSION_CHECK_SECONDS = 15.0


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def encode_event(event_type: str, data: dict[str, Any]) -> str:
    """Wire form ``{"type", "ts", "data"}`` with the REST DTO shapes."""
    if event_type == "request.started":
        payload = LiveRequestStartedData.model_validate(data).model_dump(mode="json")
    elif event_type == "request.updated":
        payload = LiveRequestUpdatedData.model_validate(data).model_dump(
            mode="json", exclude_none=True,
        )
    elif event_type == "request.finished":
        # Identical to one GET /logs item so clients can upsert directly.
        payload = {"log": RequestLogData.model_validate(data["log"]).model_dump(mode="json")}
    elif event_type == "runtime.snapshot":
        # Identical to GET /runtime/concurrency ``data``.
        payload = _revisioned(ConcurrencyData, data).model_dump(mode="json")
    elif event_type == "hello":
        payload = LiveHelloData.model_validate(data).model_dump(mode="json")
    else:
        payload = json.loads(json.dumps(data, default=str))
    return json.dumps(
        {"type": event_type, "ts": _rfc3339(datetime.now(timezone.utc)), "data": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def event_bus(runtime: ManagementRuntime) -> LiveEventBus:
    bus = runtime.live_events
    if isinstance(bus, LiveEventBus):
        return bus
    observability = get_management_control_owner(runtime).observability
    bus = LiveEventBus(
        logs=observability.logs,
        status=observability.status,
        encode=encode_event,
        max_clients=MAX_CONNECTIONS,
    )
    runtime.live_events = bus
    return bus


def _credential(websocket: WebSocket) -> tuple[str | None, str]:
    header = websocket.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip(), "bearer"
    cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    return (cookie or None), "cookie"


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        if websocket.client_state is WebSocketState.CONNECTING:
            # Accept first: a pre-accept close becomes an HTTP 403 and browsers
            # would only ever see 1006 instead of the documented code.
            await websocket.accept()
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


@router.websocket("/events")
async def management_events(websocket: WebSocket) -> None:
    runtime = getattr(websocket.app.state, "management_runtime", None)
    if not isinstance(runtime, ManagementRuntime):
        await _close(websocket, CLOSE_TRY_AGAIN_LATER, "service not ready")
        return
    origin = websocket.headers.get("origin")
    credential, transport = _credential(websocket)
    if origin is not None:
        if not browser_origin_allowed(runtime, websocket.headers):
            await _close(websocket, CLOSE_ORIGIN_DENIED, "origin denied")
            return
    elif transport == "cookie" and credential:
        # A cookie is only ever sent by a browser, and browsers always send
        # Origin on WebSocket handshakes: refuse cross-site socket hijacking.
        await _close(websocket, CLOSE_ORIGIN_DENIED, "origin required")
        return
    if not credential:
        await _close(websocket, CLOSE_UNAUTHENTICATED, "session required")
        return
    try:
        verified = await asyncio.to_thread(runtime.sessions.verify, credential, touch=False)
    except SessionAuthenticationError:
        await _close(websocket, CLOSE_UNAUTHENTICATED, "session required")
        return
    try:
        bus = event_bus(runtime)
    except Exception:
        await _close(websocket, CLOSE_TRY_AGAIN_LATER, "service not ready")
        return
    subscription = bus.connect()
    if subscription is None:
        await _close(websocket, CLOSE_TRY_AGAIN_LATER, "too many connections")
        return
    try:
        await websocket.accept()
        await websocket.send_text(encode_event("hello", {
            "serverTime": datetime.now(timezone.utc),
            "version": runtime.application_version,
            "sessionExpiresAt": min(verified.expires_at, verified.idle_expires_at),
        }))
        await _serve(websocket, runtime, credential, subscription)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.disconnect(subscription)


async def _serve(websocket, runtime, credential, subscription) -> None:
    outcome: dict[str, int] = {}

    async with anyio.create_task_group() as group:

        async def drain_client() -> None:
            # Client messages carry no meaning; read them only to see disconnects.
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
            finally:
                group.cancel_scope.cancel()

        async def pump() -> None:
            next_check = anyio.current_time() + SESSION_CHECK_SECONDS
            try:
                while True:
                    message = ""
                    with anyio.move_on_after(max(0.0, next_check - anyio.current_time())):
                        message = await subscription.get()
                    if message is None:
                        outcome["code"] = CLOSE_GOING_AWAY
                        return
                    if anyio.current_time() >= next_check:
                        next_check = anyio.current_time() + SESSION_CHECK_SECONDS
                        try:
                            await asyncio.to_thread(
                                runtime.sessions.verify, credential, touch=False,
                            )
                        except SessionAuthenticationError:
                            outcome["code"] = CLOSE_UNAUTHENTICATED
                            return
                        except Exception:
                            pass
                    if message:
                        await websocket.send_text(message)
            finally:
                group.cancel_scope.cancel()

        group.start_soon(drain_client)
        group.start_soon(pump)

    code = outcome.get("code")
    if code is not None:
        reason = "session expired" if code == CLOSE_UNAUTHENTICATED else "server closing"
        await _close(websocket, code, reason)
