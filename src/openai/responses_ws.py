"""OpenAI / Codex Responses WebSocket ingress.

This endpoint is intentionally narrow: it supports the Responses WebSocket
transport used by Codex for model communication. It doesn't implement the
separate audio Realtime API.

Downstream protocol:
  - WebSocket upgrade on /v1/responses
  - Client sends JSON frames shaped like Codex ResponsesWsRequest:
      {"type":"response.create", ...payload...}
      {"type":"response.processed", "response_id":"..."}
  - Server relays upstream Responses event JSON frames back as WebSocket text.

The implementation reuses Parrot's existing OpenAI routing/auth/scheduler stack
as much as possible while keeping the actual transport as a transparent WS relay.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.exceptions import InvalidStatus, InvalidHandshake

from .. import (
    affinity, apikey_limiter, auth, blacklist, concurrency, config, cooldown, fingerprint, local_web_tools,
    log_db, model_mapping, model_pricing, network, notifier, oauth_manager, scheduler, scorer, translation, upstream,
)
from ..channel.base import Channel, UpstreamRequest
from ..channel.openai_oauth_channel import OpenAIOAuthChannel, _isolate_session_id
from .codex_identity_confuse import (
    ConfuseState,
    confuse_client_metadata,
    confuse_headers as confuse_identity_headers,
)
from ..client_ip import get_client_ip
from ..openai.transform.guard import GuardError, guard_responses_ingress
from ..openai.transform.responses_to_chat import resolve_current_input_items
from ..proxy.connector import SS2022Connector, SOCKS5Connector
from ..protocols import finalize as finalize_policy
from ..protocols import errors as protocol_errors
from ..protocols.runtime import (
    configured_transient_retry_delays,
    format_responses_ws_error,
    is_responses_ws_visible_event_type,
    is_retryable_responses_ws_error_before_accept,
    parse_retry_after_seconds,
    parse_wrapped_responses_ws_error,
    recovery_retry_allowed,
    responses_ws_http_status_from_attempt,
    retryable_transient_error_kind,
    should_cooldown,
    transient_retry_allowed,
    transient_retry_limit,
    ws_close_code_for_http_status,
)
from ..transports import (
    BusinessTimeoutError,
    RoundTimeouts,
    WsAttemptTiming,
    await_ws_owned,
    close_proxy_client,
    close_response_context,
    connect_upstream_ws,
    finalize_opened_http_response,
    http_url_to_ws,
    next_nonempty_http_chunk,
    open_response_with_proxy_chain,
    legacy_socks5_connector,
    open_socket_via_ss2022,
    read_next_responses_ws_step,
    read_until_first_responses_ws_visible_event,
    resolve_ws_route_chain,
    socks5h_url,
    wait_ws_round_io,
    WsProxyBytes,
    ws_event_type,
    ws_frame_size,
    ws_route_kwargs,
)
from .handler import (
    _count_msg_tool,
    _maybe_apply_auto_prompt_cache_key,
    _model_never_supported,
    _sanitize_headers,
    _store_enabled,
)
from .responses_ws_runtime import (
    dump_frame,
    identity_expose_frame,
    identity_log_text,
    loads_frame,
    map_ws_create_frame_for_upstream,
    merge_responses_ws_headers,
    request_body_from_ws_create,
    sync_prompt_cache_key_to_ws_create,
    sync_translated_body_to_ws_create,
)

# Headers used by Codex Responses WS. Most clients will send these to Parrot;
# forward them when present so sticky routing / observability survives the proxy.
_FORWARD_CLIENT_HEADERS = {
    "x-codex-beta-features",
    "x-codex-turn-state",
    "x-codex-turn-metadata",
    "x-codex-parent-thread-id",
    "x-codex-window-id",
    "x-openai-memgen-request",
    "x-openai-subagent",
    "x-responsesapi-include-timing-metrics",
    "x-client-request-id",
}


_WsProxyBytes = WsProxyBytes


@dataclass
class _WsAttemptResult:
    ok: bool = False
    connected: bool = False
    closed_after_accept: bool = False
    outcome: str = "transport_error"
    error_detail: str = ""
    error_code: Optional[str] = None
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    round_id: Optional[str] = None
    connect_ms: Optional[int] = None
    first_byte_ms: Optional[int] = None
    idle_ms: Optional[int] = None
    total_ms: Optional[int] = None
    response_completed: bool = False
    request_finalized: bool = False
    usage: dict = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    })
    usage_observed: Optional[bool] = None
    response_text: str = ""
    response_id: Optional[str] = None
    output_items: list[dict] = field(default_factory=list)
    proxy_name: Optional[str] = None
    proxy_bytes: _WsProxyBytes = field(default_factory=_WsProxyBytes)
    upstream_protocol: str = "openai-responses"
    upstream_transport: str = "ws"
    translator_ctx: Optional[dict] = None


def _retry_after_from_headers(headers: Any) -> float | None:
    try:
        raw = headers.get("Retry-After")
    except Exception:
        raw = None
    return parse_retry_after_seconds(raw)


def _attach_ws_retry_after(result: _WsAttemptResult, headers: Any) -> _WsAttemptResult:
    if result.retry_after_seconds is None:
        result.retry_after_seconds = _retry_after_from_headers(headers)
    return result


def _transient_retry_delay_seconds(
    retry_ordinal: int,
    cfg: dict,
    *,
    retry_after_seconds: float | None = None,
) -> float:
    if retry_after_seconds is not None:
        parsed = parse_retry_after_seconds(retry_after_seconds)
        if parsed is not None:
            return parsed
    delays = configured_transient_retry_delays(cfg)
    index = min(max(0, int(retry_ordinal)), len(delays) - 1)
    return delays[index] + random.uniform(0.0, 0.25)


async def _wait_for_transient_retry(
    retry_ordinal: int,
    cfg: dict,
    deadline_ts: float,
    *,
    retry_after_seconds: float | None = None,
) -> float | None:
    delay = _transient_retry_delay_seconds(
        retry_ordinal,
        cfg,
        retry_after_seconds=retry_after_seconds,
    )
    if deadline_ts > 0 and time.time() + delay >= deadline_ts:
        return None
    await asyncio.sleep(delay)
    return delay


def _persist_ws_route_round(
    route_attempt_id,
    timing: WsAttemptTiming,
    proxy_bytes: _WsProxyBytes,
    *,
    outcome: str,
    error_detail: str | None = None,
    terminal: bool,
):
    snapshot = (
        timing.finish(outcome, error_detail)
        if terminal
        else timing.snapshot(terminal=False)
    )
    if route_attempt_id is not None:
        try:
            log_db.update_proxy_attempt(
                route_attempt_id,
                started_at=snapshot.started_at,
                connect_ms=snapshot.connection_ms,
                first_byte_ms=snapshot.first_byte_ms,
                idle_ms=snapshot.idle_ms,
                total_ms=snapshot.total_ms,
                ws_handshake_ms=snapshot.ws_handshake_ms,
                ended_at=snapshot.ended_at if terminal else None,
                outcome=outcome,
                error_detail=(error_detail or "")[:4000] if error_detail else None,
                bytes_up=proxy_bytes.up,
                bytes_down=proxy_bytes.down,
            )
        except Exception:
            pass
    return snapshot


def _apply_ws_snapshot(result: _WsAttemptResult, timing: WsAttemptTiming, *, terminal: bool) -> _WsAttemptResult:
    snapshot = timing.snapshot(terminal=terminal)
    result.round_id = snapshot.round_id
    result.connect_ms = snapshot.connection_ms
    result.first_byte_ms = snapshot.first_byte_ms
    result.idle_ms = snapshot.idle_ms
    result.total_ms = snapshot.total_ms
    return result


def _apply_http_snapshot(result: _WsAttemptResult, opened, *, terminal: bool) -> _WsAttemptResult:
    if opened.timing is None:
        return result
    snapshot = opened.timing.snapshot(terminal=terminal)
    result.round_id = snapshot.round_id
    result.connect_ms = snapshot.connection_ms
    result.first_byte_ms = snapshot.first_byte_ms
    result.idle_ms = snapshot.idle_ms
    result.total_ms = snapshot.total_ms
    return result


def _sync_http_proxy_bytes(result_bytes: _WsProxyBytes, opened) -> None:
    result_bytes.up = int(opened.proxy_bytes.get("up") or 0)
    result_bytes.down = int(opened.proxy_bytes.get("down") or 0)


class _WsTracker:
    """Collect usage/output metadata from upstream WS text frames."""

    def __init__(self) -> None:
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}
        self.usage_observed = False
        self.actual_service_tier: Optional[str] = None
        self.actual_cost_ticks: Optional[int] = None
        self._billing_event_type: Optional[str] = None
        self.response_completed = False
        self.response_id: Optional[str] = None
        self.response_failed = False
        self.stream_error_message: Optional[str] = None
        self.stream_error_code: Optional[str] = None
        self.response_text_parts: list[str] = []
        self._items: dict[int, dict] = {}
        self._fc_args: dict[int, str] = {}
        self._msg_text: dict[tuple[int, int], str] = {}
        self._frames: list[str] = []

    def feed_text(self, text: str) -> None:
        if not text:
            return
        self._frames.append(text)
        try:
            evt = json.loads(text)
        except Exception:
            return
        if not isinstance(evt, dict):
            return
        typ = str(evt.get("type") or "")
        response_obj = evt.get("response") if isinstance(evt.get("response"), dict) else None
        usage_present = "usage" in evt or (
            isinstance(response_obj, dict) and "usage" in response_obj
        )
        normalized = model_pricing.normalize_response_billing(evt)
        if normalized.service_tier is not None:
            self.actual_service_tier = normalized.service_tier
        if normalized.actual_cost_ticks is not None:
            self.actual_cost_ticks = normalized.actual_cost_ticks
        if usage_present or normalized.service_tier is not None:
            self._billing_event_type = typ or "response.in_progress"
        if usage_present:
            self.usage_observed = normalized.usage_observed
            self.usage = {
                "input_tokens": normalized.input_tokens,
                "output_tokens": normalized.output_tokens,
                "cache_creation": normalized.cache_creation_tokens,
                "cache_read": normalized.cache_read_tokens,
            } if self.usage_observed else {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation": 0,
                "cache_read": 0,
            }
        if typ == "error" or isinstance(evt.get("error"), dict):
            self.response_failed = True
            self.stream_error_message = _format_ws_error(evt)
            self.stream_error_code = protocol_errors.extract_error_info(evt)[0]
            return
        if typ == "response.failed":
            self.response_failed = True
            self.stream_error_message = _format_ws_error(evt)
            self.stream_error_code = protocol_errors.extract_error_info(evt)[0]
        elif typ == "response.incomplete":
            if protocol_errors.is_responses_max_output_incomplete(evt):
                self.response_failed = True
                self.stream_error_code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                self.stream_error_message = protocol_errors.responses_max_output_context_error_message(
                    protocol_errors.responses_incomplete_reason(evt)
                )
            else:
                self.response_completed = True
        elif typ == "response.completed":
            self.response_completed = True

        if typ in ("response.completed", "response.failed", "response.incomplete"):
            resp = response_obj
            if isinstance(resp, dict) and isinstance(resp.get("id"), str):
                self.response_id = resp.get("id")
            if isinstance(resp, dict) and isinstance(resp.get("output"), list):
                for idx, item in enumerate(resp.get("output") or []):
                    if isinstance(item, dict):
                        self._items[idx] = dict(item)

        if typ == "response.output_item.added":
            idx = _safe_int(evt.get("output_index"), 0)
            item = evt.get("item")
            if isinstance(item, dict):
                self._items[idx] = dict(item)
        elif typ == "response.output_item.done":
            idx = _safe_int(evt.get("output_index"), 0)
            item = evt.get("item")
            if isinstance(item, dict):
                self._items[idx] = dict(item)
        elif typ == "response.output_text.delta":
            idx = _safe_int(evt.get("output_index"), 0)
            cidx = _safe_int(evt.get("content_index"), 0)
            delta = evt.get("delta")
            if isinstance(delta, str) and delta:
                self._msg_text[(idx, cidx)] = self._msg_text.get((idx, cidx), "") + delta
                self.response_text_parts.append(delta)
        elif typ == "response.function_call_arguments.delta":
            idx = _safe_int(evt.get("output_index"), 0)
            delta = evt.get("delta")
            if isinstance(delta, str) and delta:
                self._fc_args[idx] = self._fc_args.get(idx, "") + delta

    def get_output_items(self) -> list[dict]:
        out: list[dict] = []
        for idx in sorted(self._items.keys()):
            item = dict(self._items[idx])
            if item.get("type") == "message":
                content = list(item.get("content") or [])
                merged = {ci: text for (oi, ci), text in self._msg_text.items() if oi == idx}
                for ci in sorted(merged.keys()):
                    if ci < len(content) and isinstance(content[ci], dict):
                        if not content[ci].get("text"):
                            content[ci]["text"] = merged[ci]
                    else:
                        content.append({"type": "output_text", "text": merged[ci], "annotations": []})
                item["content"] = content
            elif item.get("type") == "function_call":
                args = self._fc_args.get(idx)
                if args and not item.get("arguments"):
                    item["arguments"] = args
            out.append(item)
        return out

    def get_full_response(self) -> str:
        return model_pricing.preserve_billing_evidence_tail(
            "\n".join(self._frames),
            usage=self.usage,
            usage_observed=self.usage_observed,
            service_tier=self.actual_service_tier,
            actual_cost_ticks=self.actual_cost_ticks,
            event_type=self._billing_event_type,
        )


async def handle_responses_ws(websocket: WebSocket) -> None:
    """FastAPI WebSocket handler for /v1/responses."""

    start_time = time.time()
    start_monotonic = time.monotonic()
    request_id = str(uuid.uuid4())
    accepted = False
    client_ip = _websocket_client_ip(websocket)

    key_name, allowed_models, err = auth.validate(websocket.headers)
    if err:
        await websocket.close(code=4401, reason=_trim_reason(err))
        return

    # A WebSocket server cannot read data frames until it accepts the upgrade.
    # Auth / protocol checks happen before this point; model-specific validation
    # follows after the first response.create frame.
    await websocket.accept()
    accepted = True

    try:
        first_message = await asyncio.wait_for(websocket.receive(), timeout=30.0)
    except asyncio.TimeoutError:
        await websocket.close(code=4400, reason="timeout waiting for first websocket frame")
        return
    except WebSocketDisconnect:
        return

    if first_message.get("type") == "websocket.disconnect":
        return

    if first_message.get("text") is not None:
        first_payload_raw: str | bytes = first_message["text"]
    elif first_message.get("bytes") is not None:
        first_payload_raw = first_message["bytes"]
    else:
        await websocket.close(code=4400, reason="first websocket frame must be text or bytes")
        return

    try:
        first_obj = _loads_frame(first_payload_raw)
    except Exception as exc:
        await websocket.close(code=4400, reason=_trim_reason(f"invalid json: {exc}"))
        return

    if not isinstance(first_obj, dict):
        await websocket.close(code=4400, reason="first websocket frame must be a JSON object")
        return
    if first_obj.get("type") != "response.create":
        await websocket.close(code=4400, reason="first websocket frame must be response.create")
        return

    body = _request_body_from_ws_create(first_obj)

    _ingress_line = "openai-responses"
    model_mapping.apply_default(body, _ingress_line)
    model_mapping.apply_mapping(body, _ingress_line)
    body["_client_visible_model"] = str(body.get("model") or "").strip()

    model = body.get("model")
    if not model or not isinstance(model, str):
        await websocket.close(code=4400, reason="model is required")
        return
    if allowed_models and model not in allowed_models:
        await websocket.close(
            code=4403,
            reason=_trim_reason(
                f"model '{model}' is not allowed for this API key "
                f"(allowed: {', '.join(allowed_models) or 'none'})"
            ),
        )
        return

    try:
        guard_responses_ingress(body, store_enabled=_store_enabled())
    except GuardError as ge:
        await websocket.close(code=_ws_close_code_for_http(ge.status), reason=_trim_reason(ge.message))
        return
    if body.get("background") is True:
        await websocket.close(code=4400, reason=_trim_reason("background async response is not supported on Responses WebSocket"))
        return

    local_web_tools.prepare_openai_responses_local_web_tools(body)

    # WS transport is streaming by definition. Ensure the upstream payload matches
    # Responses WS expectations even if the client omitted stream.
    body["stream"] = True
    body["_api_key_name"] = key_name or ""

    input_items = resolve_current_input_items(body)
    fp_query = fingerprint.fingerprint_query_responses(key_name or "", client_ip, input_items)
    _maybe_apply_auto_prompt_cache_key(
        body,
        fp_query=fp_query,
        api_key_name=key_name or "",
        client_ip=client_ip,
        model=model,
        ingress_protocol="responses",
    )

    msg_count, tool_count = _count_msg_tool(body, "responses")
    reasoning_effort = log_db.extract_reasoning_effort(body, "responses_ws")
    req_headers = _sanitize_headers(dict(websocket.headers))
    fast_mode = log_db.extract_fast_mode(body, "responses_ws", req_headers)
    log_body = {k: v for k, v in body.items() if not (isinstance(k, str) and k.startswith("_"))}
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, True, msg_count, tool_count,
        req_headers, log_body, fingerprint=fp_query, ingress_protocol="responses_ws",
        reasoning_effort=reasoning_effort,
        fast_mode=fast_mode,
    )

    result = scheduler.schedule(
        body,
        api_key_name=key_name or "",
        client_ip=client_ip,
        ingress_protocol="responses",
        fp_query=fp_query,
    )
    if result.affinity_hit:
        await asyncio.to_thread(log_db.update_pending, request_id, affinity_hit=1)

    if not result:
        msg = f"No available upstream channels for model: {model} (ingress=responses_ws)"
        exclusion_summary = result.exclusion_summary()
        print(
            f"[scheduler] no channels ingress=responses_ws model={model}: "
            f"{exclusion_summary}"
        )
        await asyncio.to_thread(
            log_db.finish_error, request_id, msg, 0,
            http_status=503, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=None,
            request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
        )
        ek = notifier.escape_html
        await notifier.throttled_notify_event(
            "no_channels",
            f"no_channels:responses_ws:{model}",
            f"🚨 <b>无可用渠道</b>（{notifier.provider_tag('openai')} Responses WS 入口）\n"
            f"客户端: <code>{ek(client_ip)}</code> / Key <code>{ek(str(key_name))}</code>\n"
            f"模型: <code>{ek(model)}</code>\n"
            f"筛选详情: <code>{ek(exclusion_summary)}</code>\n"
            "请按筛选详情检查渠道状态。",
        )
        code = 4404 if _model_never_supported(model) else 4503
        await websocket.close(code=code, reason=_trim_reason(msg))
        return

    try:
        key_lease = await apikey_limiter.acquire(key_name or "", None)
    except apikey_limiter.ApiKeyLimitError as exc:
        await asyncio.to_thread(
            log_db.finish_error, request_id, exc.message, 0,
            http_status=429, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=None,
            request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
        )
        await websocket.close(code=4429, reason=_trim_reason(exc.message))
        return

    ts = time.strftime("%H:%M:%S", time.localtime(start_time))
    first_list = result.candidates or result.saturated
    chosen = first_list[0][0].key if first_list else "?"
    sat_note = " queued" if (not result.candidates and result.saturated) else ""
    print(f"[{ts}] {client_ip} {key_name} → responses_ws:{model} "
          f"(msgs={msg_count}, tools={tool_count}) "
          f"{'★' if result.affinity_hit else ''}first={chosen}{sat_note}")

    # 翻译层（first frame）：放在调度之后，才能按生效渠道/账号判断。
    body = await translation.translate_body(body, ingress_protocol="responses", route=result)
    _sync_translated_body_to_ws_create(first_obj, body)

    try:
        try:
            accepted = await _run_ws_failover(
                websocket, first_obj=first_obj,
                schedule_result=result, body=body, request_id=request_id,
                api_key_name=key_name or "", client_ip=client_ip,
                start_time=start_time, start_monotonic=start_monotonic,
                fp_query=fp_query,
            )
        except Exception as exc:
            traceback.print_exc()
            request_elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
            await asyncio.to_thread(
                log_db.finish_error, request_id, f"unexpected: {exc}", 0,
                http_status=500, total_ms=None,
                request_elapsed_ms=request_elapsed_ms,
                affinity_hit=(1 if result.affinity_hit else 0),
            )
            if not accepted and websocket.application_state != WebSocketState.DISCONNECTED:
                try:
                    await websocket.close(code=4500, reason=_trim_reason(f"internal: {exc}"))
                except Exception:
                    pass
    finally:
        await key_lease.release()


async def _run_ws_failover(
    websocket: WebSocket,
    *,
    first_obj: dict,
    schedule_result,
    body: dict,
    request_id: str,
    api_key_name: str,
    client_ip: str,
    start_time: float,
    start_monotonic: float,
    fp_query: Optional[str],
) -> bool:
    cfg = config.get()
    deadline_ts = 0.0  # legacy transport argument; every upstream route owns its round total
    total_timeout_s = float((cfg.get("timeouts") or {}).get("total", 600))
    retry_deadline_ts = start_time + total_timeout_s
    queue_wait_s = float((cfg.get("concurrency") or {}).get("queueWaitSeconds", 30))

    affinity_hit = 1 if schedule_result.affinity_hit else 0
    client_visible_model = str(
        body.get("_client_visible_model") or body.get("model") or ""
    ).strip()
    client_key = getattr(schedule_result, "client_key", None)

    pending = [
        (ch, m) for ch, m in list(schedule_result.candidates)
        if _is_ws_capable_channel(ch)
    ]
    saturated_extras: list[tuple[Channel, str]] = []
    refreshed_once: set[str] = set()
    transient_limit = transient_retry_limit(cfg)
    transient_retries_used = 0
    retry_count = 0
    attempt_order = 0
    last_result: Optional[_WsAttemptResult] = None
    last_ch: Optional[Channel] = None
    last_model: Optional[str] = None
    accepted = websocket.application_state == WebSocketState.CONNECTED

    idx = 0
    while idx < len(pending):
        ch, resolved_model = pending[idx]
        attempt_order += 1
        last_ch, last_model = ch, resolved_model

        acquired = await concurrency.try_acquire(ch.key)
        if not acquired:
            saturated_extras.append((ch, resolved_model))
            idx += 1
            continue

        attempt_proxy = _pick_non_direct_proxy_name(ch, resolved_model)
        attempt_started_monotonic = time.monotonic()
        attempt_id = log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
            proxy_name=attempt_proxy,
            upstream_protocol=getattr(ch, "protocol", "openai-responses"),
            client_visible_model=client_visible_model,
        )
        if attempt_proxy:
            log_db.update_pending(request_id, proxy_name=attempt_proxy)

        try:
            result = await _try_ws_channel(
                websocket, first_obj=first_obj,
                ch=ch, resolved_model=resolved_model, body=body,
                deadline_ts=deadline_ts, start_time=start_time,
                request_id=request_id, retry_count_so_far=retry_count,
                affinity_hit=affinity_hit, api_key_name=api_key_name,
                client_ip=client_ip, fp_query=fp_query, client_key=client_key,
                retry_attempt_id=attempt_id,
                start_monotonic=start_monotonic,
                attempt_start_monotonic=attempt_started_monotonic,
            )
        finally:
            concurrency.release(ch.key)

        if result.proxy_name is None:
            result.proxy_name = attempt_proxy
        last_result = result
        accepted = accepted or result.closed_after_accept or result.ok or result.connected

        log_db.update_retry_attempt(
            attempt_id,
            final_round_id=result.round_id,
            connect_ms=result.connect_ms,
            first_byte_ms=result.first_byte_ms,
            idle_ms=result.idle_ms,
            total_ms=result.total_ms,
            attempt_elapsed_ms=int((time.monotonic() - attempt_started_monotonic) * 1000),
            ended_at=time.time(),
            outcome=result.outcome,
            error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
            proxy_name=result.proxy_name,
            bytes_up=result.proxy_bytes.up,
            bytes_down=result.proxy_bytes.down,
            response_body=result.response_text or None,
            usage=result.usage,
            usage_observed=result.usage_observed,
        )

        if result.ok:
            return accepted
        if result.closed_after_accept:
            if not result.request_finalized:
                await _finalize_ws_attempt_after_accept(
                    result, ch, resolved_model, request_id, retry_count,
                    affinity_hit, start_time, start_monotonic,
                )
            return accepted
        if result.outcome == "request_invalid":
            msg = result.error_detail or protocol_errors.responses_max_output_context_error_message()
            await _send_context_length_error_frame(websocket, msg)
            await _close_downstream(websocket, 4400, _trim_reason(msg))
            if not result.request_finalized:
                await _finalize_ws_attempt_after_accept(
                    result, ch, resolved_model, request_id, retry_count,
                    affinity_hit, start_time, start_monotonic,
                )
            return accepted

        if (
            recovery_retry_allowed("oauthRefresh", cfg)
            and ch.type == "oauth"
            and result.http_status in (401, 403)
            and ch.key not in refreshed_once
        ):
            refreshed_once.add(ch.key)
            ak = getattr(ch, "account_key", None) or getattr(ch, "email", "")
            try:
                await oauth_manager.force_refresh(ak)
                print(f"[responses_ws] OAuth 401/403 on {ch.key}, refreshed; retrying same channel")
                retry_count += 1
                continue
            except Exception as exc:
                print(f"[responses_ws] OAuth refresh failed for {ch.key}: {exc}")
                email = getattr(ch, "email", "?")
                try:
                    oauth_manager.set_enabled(ak, False, reason="auth_error")
                except Exception:
                    pass
                try:
                    ek = notifier.escape_html
                    prov = getattr(ch, "provider", "") or oauth_manager.provider_of(ak)
                    notifier.notify_event(
                        "oauth_refresh_failed",
                        "⚠ <b>OAuth Token 刷新失败</b>（Responses WS 请求路径触发）\n"
                        f"账号: <code>{ek(email)}</code> · {notifier.provider_tag(prov)}\n"
                        f"原因: <code>{ek(str(exc))}</code>\n"
                        "账号已被自动禁用 (auth_error)。请通过 TG Bot 重新登录或粘贴新 JSON。"
                    )
                except Exception:
                    pass

        transient_kind = retryable_transient_error_kind(ch, result)
        if (
            transient_retries_used < transient_limit
            and transient_retry_allowed(transient_kind, cfg)
        ):
            delay = await _wait_for_transient_retry(
                transient_retries_used,
                cfg,
                retry_deadline_ts,
                retry_after_seconds=result.retry_after_seconds,
            )
            if delay is not None:
                transient_retries_used += 1
                retry_count += 1
                print(
                    f"[responses_ws] transient {transient_kind} on "
                    f"{ch.key}/{resolved_model}; retrying same channel "
                    f"({transient_retries_used}/{transient_limit}) after {delay:.2f}s"
                )
                continue

        finalize_policy.apply_error_health_effects(
            finalize_policy.error_plan(result.outcome, failure_policy="runtime"),
            scorer=scorer,
            cooldown=cooldown,
            channel_key=ch.key,
            model=resolved_model,
            error_detail=result.error_detail,
            connect_ms=result.connect_ms,
        )
        retry_count += 1
        idx += 1

    saturated_all = [
        (ch, m) for ch, m in list(schedule_result.saturated)
        if _is_ws_capable_channel(ch)
    ] + saturated_extras
    if saturated_all:
        seen = set()
        deduped: list[tuple[Channel, str]] = []
        for ch, m in saturated_all:
            k = (ch.key, m)
            if k in seen:
                continue
            seen.add(k)
            deduped.append((ch, m))
        saturated_all = deduped
        queue_timeout = queue_wait_s  # queue wait is outside every upstream round
        if queue_timeout > 0:
            acquired = await concurrency.acquire_from_candidates(
                [(ch.key, (ch, m)) for ch, m in saturated_all], queue_timeout,
            )
            if acquired is not None:
                _ch_key, payload = acquired
                ch, resolved_model = payload  # type: ignore[assignment]
                attempt_order += 1
                last_ch, last_model = ch, resolved_model
                attempt_proxy = _pick_non_direct_proxy_name(ch, resolved_model)
                attempt_started_monotonic2 = time.monotonic()
                attempt_id = log_db.record_retry_attempt(
                    request_id, attempt_order, ch.key, ch.type, resolved_model,
                    time.time(), proxy_name=attempt_proxy,
                    upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                    client_visible_model=client_visible_model,
                )
                try:
                    result = await _try_ws_channel(
                        websocket, first_obj=first_obj,
                        ch=ch, resolved_model=resolved_model, body=body,
                        deadline_ts=deadline_ts, start_time=start_time,
                        request_id=request_id, retry_count_so_far=retry_count,
                        affinity_hit=affinity_hit, api_key_name=api_key_name,
                        client_ip=client_ip, fp_query=fp_query, client_key=client_key,
                        retry_attempt_id=attempt_id,
                        start_monotonic=start_monotonic,
                        attempt_start_monotonic=attempt_started_monotonic2,
                    )
                finally:
                    concurrency.release(ch.key)
                if result.proxy_name is None:
                    result.proxy_name = attempt_proxy
                last_result = result
                accepted = accepted or result.closed_after_accept or result.ok or result.connected
                log_db.update_retry_attempt(
                    attempt_id,
                    final_round_id=result.round_id,
                    connect_ms=result.connect_ms,
                    first_byte_ms=result.first_byte_ms,
                    idle_ms=result.idle_ms,
                    total_ms=result.total_ms,
                    attempt_elapsed_ms=int((time.monotonic() - attempt_started_monotonic2) * 1000),
                    ended_at=time.time(),
                    outcome=result.outcome,
                    error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
                    proxy_name=result.proxy_name,
                    bytes_up=result.proxy_bytes.up,
                    bytes_down=result.proxy_bytes.down,
                    response_body=result.response_text or None,
                    usage=result.usage,
                    usage_observed=result.usage_observed,
                )
                if result.ok:
                    return accepted
                if result.closed_after_accept:
                    if not result.request_finalized:
                        await _finalize_ws_attempt_after_accept(
                            result, ch, resolved_model, request_id, retry_count,
                            affinity_hit, start_time, start_monotonic,
                        )
                    return accepted
                if result.outcome == "request_invalid":
                    msg = (
                        result.error_detail
                        or protocol_errors.responses_max_output_context_error_message()
                    )
                    await _send_context_length_error_frame(websocket, msg)
                    await _close_downstream(websocket, 4400, _trim_reason(msg))
                    if not result.request_finalized:
                        await _finalize_ws_attempt_after_accept(
                            result, ch, resolved_model, request_id, retry_count,
                            affinity_hit, start_time, start_monotonic,
                        )
                    return accepted
                finalize_policy.apply_error_health_effects(
                    finalize_policy.error_plan(result.outcome, failure_policy="runtime"),
                    scorer=scorer,
                    cooldown=cooldown,
                    channel_key=ch.key,
                    model=resolved_model,
                    error_detail=result.error_detail,
                    connect_ms=result.connect_ms,
                )
                retry_count += 1
            else:
                msg = f"All candidate channels saturated; queue wait {queue_wait_s:.0f}s timed out."
                await asyncio.to_thread(
                    log_db.finish_error, request_id, msg, retry_count,
                    http_status=429, affinity_hit=affinity_hit,
                    total_ms=None,
                    request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
                )
                await _close_downstream(websocket, 4429, msg)
                return accepted

    err = (last_result.error_detail if last_result else "no candidates") or "unknown"
    http_status = _http_status_from_ws_outcome(last_result)
    await asyncio.to_thread(
        log_db.finish_error,
        request_id, err[:4000], retry_count,
        final_channel_key=(last_ch.key if last_ch else None),
        final_channel_type=(last_ch.type if last_ch else None),
        final_model=last_model,
        connect_ms=(last_result.connect_ms if last_result else None),
        first_token_ms=(last_result.first_byte_ms if last_result else None),
        idle_ms=(last_result.idle_ms if last_result else None),
        total_ms=(last_result.total_ms if last_result else None),
        final_round_id=(last_result.round_id if last_result else None),
        request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
        http_status=http_status,
        affinity_hit=affinity_hit,
        response_body=(last_result.response_text or None) if last_result else None,
        usage=(last_result.usage if last_result else None),
        usage_observed=(last_result.usage_observed if last_result else None),
        upstream_protocol=(getattr(last_ch, "protocol", "openai-responses") if last_ch else None),
        upstream_transport=(last_result.upstream_transport if last_result and last_ch is not None else ("ws" if last_ch is not None else None)),
        proxy_name=(last_result.proxy_name if last_result else None),
        proxy_bytes_up=(last_result.proxy_bytes.up if last_result else None),
        proxy_bytes_down=(last_result.proxy_bytes.down if last_result else None),
    )
    await _close_downstream(websocket, _ws_close_code_for_http(http_status), err)
    return accepted


async def _try_ws_channel(
    websocket: WebSocket,
    *,
    first_obj: dict,
    ch: Channel,
    resolved_model: str,
    body: dict,
    deadline_ts: float,
    start_time: float,
    request_id: str,
    retry_count_so_far: int,
    affinity_hit: int,
    api_key_name: str,
    client_ip: str,
    fp_query: Optional[str],
    client_key: Optional[str],
    retry_attempt_id,
    start_monotonic: float,
    attempt_start_monotonic: float,
) -> _WsAttemptResult:
    ch_proto = getattr(ch, "protocol", "anthropic")
    if ch_proto != "openai-responses":
        return _WsAttemptResult(
            outcome="guard_error",
            error_detail="Responses WebSocket requires an openai-responses upstream channel",
            http_status=400,
            upstream_protocol=ch_proto,
        )

    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 120))

    if _responses_ws_upstream_transport(ch) == "sse":
        return await _try_sse_channel(
            websocket, first_obj=first_obj, ch=ch, resolved_model=resolved_model, body=body,
            deadline_ts=deadline_ts, start_time=start_time, request_id=request_id,
            retry_count_so_far=retry_count_so_far, affinity_hit=affinity_hit,
            api_key_name=api_key_name, client_ip=client_ip, fp_query=fp_query, client_key=client_key,
            retry_attempt_id=retry_attempt_id,
            start_monotonic=start_monotonic,
            attempt_start_monotonic=attempt_start_monotonic,
        )

    try:
        upstream_req = await _build_ws_upstream_request(ch, body, resolved_model, websocket=websocket)
    except Exception as exc:
        if hasattr(exc, "status") and hasattr(exc, "message"):
            outcome = "candidate_guard" if getattr(exc, "scope", "request") == "candidate" else "guard_error"
            return _WsAttemptResult(
                outcome=outcome,
                error_detail=str(getattr(exc, "message", exc))[:2000],
                http_status=int(getattr(exc, "status", 400)),
                upstream_protocol=ch_proto,
            )
        traceback.print_exc()
        return _WsAttemptResult(
            outcome="transform_error",
            error_detail=f"transform error: {exc}",
            upstream_protocol=ch_proto,
        )

    route_chain = _resolve_ws_route_chain(ch, resolved_model)
    round_timeouts = RoundTimeouts.from_config(timeouts)
    last_error: Optional[_WsAttemptResult] = None
    route_order = 0

    for route_name, connector in route_chain:
        last_error = None
        route_order += 1
        proxy_bytes = _WsProxyBytes()
        proxy_name_used = None if connector is None else route_name
        route_log_name = route_name if connector is not None else "direct"
        route_type = str(getattr(connector, "type", "direct") or "direct")
        round_id = str(uuid.uuid4())
        route_attempt_id = None
        try:
            route_attempt_id = log_db.record_proxy_attempt(
                request_id,
                retry_attempt_id,
                route_order,
                route_log_name,
                time.time(),
                round_id=round_id,
                transport="ws",
                request_mode="ws",
            )
        except Exception:
            route_attempt_id = None

        timing = WsAttemptTiming(route_type=route_type, round_id=round_id)
        route_state = {"dispatched": False}
        relay_state: dict[str, Any] = {}
        upstream_ws = None
        try:
            if connector is not None:
                connector.stats.total_attempts += 1
                connector.stats.last_attempt_ts = time.time()
            upstream_ws = await _connect_upstream_ws(
                upstream_req.url,
                headers=upstream_req.headers,
                connector=connector,
                proxy_bytes=proxy_bytes,
                open_timeout=round_timeouts.connection + 0.5,
                timing=timing,
                round_timeouts=round_timeouts,
            )
            if not timing.connection_complete:
                timing.mark_handshake_complete()
            open_snapshot = _persist_ws_route_round(
                route_attempt_id,
                timing,
                proxy_bytes,
                outcome="open",
                terminal=False,
            )
            connect_ms = open_snapshot.connection_ms
            if connector is not None:
                connector.stats.total_successes += 1
                connector.stats.last_success_ts = time.time()
                connector.stats.last_latency_ms = int(connect_ms or 0)
            # Headers from a successful WS upgrade carry Codex quota snapshots.
            _maybe_record_codex_ws_snapshot(ch, getattr(upstream_ws, "response", None))

            relay_result = await _relay_ws_session(
                websocket, upstream_ws,
                first_obj=first_obj,
                ch=ch,
                resolved_model=resolved_model,
                body=body,
                request_id=request_id,
                retry_count_so_far=retry_count_so_far,
                affinity_hit=affinity_hit,
                api_key_name=api_key_name,
                client_ip=client_ip,
                fp_query=fp_query,
                client_key=client_key,
                start_time=start_time,
                start_monotonic=start_monotonic,
                attempt_start_monotonic=attempt_start_monotonic,
                deadline_ts=deadline_ts,
                connect_ms=connect_ms,
                first_byte_timeout=first_byte_timeout,
                idle_timeout=idle_timeout,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                translator_ctx=upstream_req.translator_ctx,
                timing=timing,
                round_timeouts=round_timeouts,
                route_attempt_id=route_attempt_id,
                retry_attempt_id=retry_attempt_id,
                on_dispatch=lambda: route_state.__setitem__("dispatched", True),
                relay_state=relay_state,
            )
            if not timing.terminal:
                _persist_ws_route_round(
                    route_attempt_id,
                    timing,
                    proxy_bytes,
                    outcome=relay_result.outcome,
                    error_detail=relay_result.error_detail,
                    terminal=True,
                )
            _apply_ws_snapshot(relay_result, timing, terminal=True)
            _attach_ws_retry_after(
                relay_result,
                getattr(getattr(upstream_ws, "response", None), "headers", None),
            )
            if relay_result.ok or relay_result.closed_after_accept:
                return relay_result
            last_error = relay_result
        except asyncio.CancelledError:
            async def finish_cancelled_round() -> None:
                captured = None
                sync_result = relay_state.get("sync_result")
                if callable(sync_result):
                    try:
                        captured = sync_result()
                    except Exception:
                        captured = None
                response_body = (
                    captured.response_text if captured is not None else None
                ) or None
                usage = captured.usage if captured is not None else None
                usage_observed = (
                    captured.usage_observed if captured is not None else None
                )
                if upstream_ws is not None:
                    try:
                        await upstream_ws.close()
                    except BaseException:
                        pass
                if timing.terminal:
                    return
                cancelled_snapshot = _persist_ws_route_round(
                    route_attempt_id,
                    timing,
                    proxy_bytes,
                    outcome="cancelled",
                    error_detail="cancelled",
                    terminal=True,
                )
                try:
                    await asyncio.to_thread(
                        log_db.update_retry_attempt,
                        retry_attempt_id,
                        final_round_id=cancelled_snapshot.round_id,
                        connect_ms=cancelled_snapshot.connection_ms,
                        first_byte_ms=cancelled_snapshot.first_byte_ms,
                        idle_ms=cancelled_snapshot.idle_ms,
                        total_ms=cancelled_snapshot.total_ms,
                        attempt_elapsed_ms=int((time.monotonic() - attempt_start_monotonic) * 1000),
                        ended_at=time.time(),
                        outcome="cancelled",
                        error_detail="cancelled",
                        proxy_name=proxy_name_used,
                        bytes_up=proxy_bytes.up,
                        bytes_down=proxy_bytes.down,
                        response_body=response_body,
                        usage=usage,
                        usage_observed=usage_observed,
                        settle=False,
                    )
                    await asyncio.to_thread(
                        log_db.finish_error,
                        request_id,
                        "client disconnected",
                        retry_count_so_far,
                        final_channel_key=ch.key,
                        final_channel_type=ch.type,
                        final_model=resolved_model,
                        connect_ms=cancelled_snapshot.connection_ms,
                        first_token_ms=cancelled_snapshot.first_byte_ms,
                        idle_ms=cancelled_snapshot.idle_ms,
                        total_ms=cancelled_snapshot.total_ms,
                        final_round_id=cancelled_snapshot.round_id,
                        request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
                        http_status=499,
                        affinity_hit=affinity_hit,
                        response_body=response_body,
                        usage=usage,
                        usage_observed=usage_observed,
                        upstream_protocol=ch_proto,
                        upstream_transport="ws",
                        proxy_name=proxy_name_used,
                        proxy_bytes_up=proxy_bytes.up,
                        proxy_bytes_down=proxy_bytes.down,
                        status="cancelled",
                    )
                except Exception:
                    pass

            await await_ws_owned(finish_cancelled_round())
            upstream_ws = None
            raise
        except BusinessTimeoutError as exc:
            last_error = _WsAttemptResult(
                outcome=exc.outcome,
                error_detail=exc.outcome,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                upstream_protocol=ch_proto,
            )
        except asyncio.TimeoutError as exc:
            last_error = _WsAttemptResult(
                outcome="transport_timeout",
                error_detail=f"websocket transport timeout: {exc}"[:2000],
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                upstream_protocol=ch_proto,
            )
        except InvalidStatus as exc:
            invalid_response = getattr(exc, "response", None)
            status = int(getattr(invalid_response, "status_code", 0) or 0)
            detail = _invalid_status_detail(exc)
            last_error = _WsAttemptResult(
                outcome="http_auth_error" if status in (401, 403) else "http_error",
                error_detail=detail,
                http_status=status or None,
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                upstream_protocol=ch_proto,
                retry_after_seconds=_retry_after_from_headers(
                    getattr(invalid_response, "headers", None)
                ),
            )
        except Exception as exc:
            connected = timing.connection_complete
            detail = f"{'websocket relay' if connected else 'connect'} error: {exc}"
            last_error = _WsAttemptResult(
                outcome="transport_error" if connected else "connect_error",
                error_detail=detail[:2000],
                proxy_name=proxy_name_used,
                proxy_bytes=proxy_bytes,
                upstream_protocol=ch_proto,
            )
        finally:
            if last_error is not None and not timing.terminal:
                _persist_ws_route_round(
                    route_attempt_id,
                    timing,
                    proxy_bytes,
                    outcome=last_error.outcome,
                    error_detail=last_error.error_detail,
                    terminal=True,
                )
                _apply_ws_snapshot(last_error, timing, terminal=True)
            if upstream_ws is not None:
                try:
                    await upstream_ws.close()
                except Exception:
                    pass
        if connector is not None and last_error is not None:
            connector.stats.total_failures += 1
            connector.stats.last_error = (last_error.error_detail or last_error.outcome)[:200]
        # Once a create frame may have left Parrot, retrying another proxy route
        # would create a second billable upstream request under one ledger row.
        if last_error is not None and route_state["dispatched"]:
            return last_error
        continue

    return last_error or _WsAttemptResult(
        outcome="proxy_connect_error",
        error_detail="proxy route has no usable target",
        upstream_protocol=ch_proto,
    )


def _responses_ws_upstream_transport(ch: Channel) -> str:
    """Return the upstream transport for Responses WS ingress.

    Existing behavior is WebSocket.  An explicit channel hint can route the
    WebSocket ingress to a normal HTTP/SSE Responses upstream, used by the
    protocol-runtime transport bridge tests and by providers that do not expose
    Responses WebSocket.
    """
    if isinstance(ch, OpenAIOAuthChannel):
        return "ws"
    value = str(getattr(ch, "responses_ws_upstream_transport", "ws") or "ws").strip().lower()
    return "sse" if value in ("sse", "http-sse", "http_sse") else "ws"


async def _try_sse_channel(
    websocket: WebSocket,
    *,
    first_obj: dict,
    ch: Channel,
    resolved_model: str,
    body: dict,
    deadline_ts: float,
    start_time: float,
    request_id: str,
    retry_count_so_far: int,
    affinity_hit: int,
    api_key_name: str,
    client_ip: str,
    fp_query: Optional[str],
    client_key: Optional[str],
    retry_attempt_id,
    start_monotonic: float,
    attempt_start_monotonic: float,
) -> _WsAttemptResult:
    ch_proto = getattr(ch, "protocol", "anthropic")
    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 120))
    total_timeout = int(timeouts.get("total", 600))
    proxy_bytes = _WsProxyBytes()

    try:
        http_body = dict(body)
        http_body["stream"] = True
        upstream_req = await ch.build_upstream_request(http_body, resolved_model, ingress_protocol="responses")
        log_db.update_pending_fast_mode_from_upstream(
            request_id, upstream_req.body, upstream_req.headers,
        )
    except Exception as exc:
        if hasattr(exc, "status") and hasattr(exc, "message"):
            outcome = "candidate_guard" if getattr(exc, "scope", "request") == "candidate" else "guard_error"
            return _WsAttemptResult(
                outcome=outcome,
                error_detail=str(getattr(exc, "message", exc))[:2000],
                http_status=int(getattr(exc, "status", 400)),
                upstream_protocol=ch_proto,
                upstream_transport="sse",
            )
        traceback.print_exc()
        return _WsAttemptResult(
            outcome="transform_error",
            error_detail=f"transform error: {exc}",
            upstream_protocol=ch_proto,
            upstream_transport="sse",
        )

    try:
        opened = await open_response_with_proxy_chain(
            channel=ch,
            resolved_model=resolved_model,
            upstream_req=upstream_req,
            connect_timeout=connect_timeout,
            first_byte_timeout=first_byte_timeout,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            response_mode="stream",
            request_id=request_id,
            retry_attempt_id=retry_attempt_id,
        )
    except asyncio.CancelledError:
        async def finish_cancelled_open() -> None:
            if retry_attempt_id is not None:
                await asyncio.to_thread(
                    log_db.update_retry_attempt,
                    retry_attempt_id,
                    attempt_elapsed_ms=int(
                        (time.monotonic() - attempt_start_monotonic) * 1000
                    ),
                    ended_at=time.time(),
                    outcome="cancelled",
                    error_detail=(
                        "upstream SSE bridge cancelled before response headers"
                    ),
                    settle=False,
                )
            await asyncio.to_thread(
                log_db.finish_error,
                request_id,
                "client disconnected",
                retry_count_so_far,
                final_channel_key=ch.key,
                final_channel_type=ch.type,
                final_model=resolved_model,
                request_elapsed_ms=int(
                    (time.monotonic() - start_monotonic) * 1000
                ),
                http_status=499,
                affinity_hit=affinity_hit,
                upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                upstream_transport="sse",
                status="cancelled",
            )

        try:
            await await_ws_owned(finish_cancelled_open())
        except BaseException:
            pass
        raise
    if opened.error is not None:
        error = opened.error
        return _WsAttemptResult(
            outcome=error.outcome,
            error_detail=error.error_detail,
            error_code=error.error_code,
            http_status=error.http_status,
            retry_after_seconds=error.retry_after_seconds,
            round_id=error.round_id,
            connect_ms=error.connect_ms,
            first_byte_ms=error.first_byte_ms,
            idle_ms=error.idle_ms,
            total_ms=error.total_ms,
            proxy_name=error.proxy_name,
            proxy_bytes=_WsProxyBytes(error.proxy_bytes_up, error.proxy_bytes_down),
            upstream_protocol=ch_proto,
            upstream_transport="sse",
        )

    response = opened.response
    status = int(response.status_code)
    _sync_http_proxy_bytes(proxy_bytes, opened)
    connect_ms = opened.connect_ms
    if status >= 400:
        parts: list[bytes] = []
        body_bytes = b""
        outcome = "http_auth_error" if status in (401, 403) else "http_error"
        detail = response.reason_phrase or f"HTTP {status}"
        try:
            aiter = response.aiter_bytes()
            while True:
                parts.append(await next_nonempty_http_chunk(
                    aiter, opened.timing, opened.round_timeouts,
                ))
        except StopAsyncIteration:
            if opened.timing is not None:
                opened.timing.mark_io_complete()
            body_bytes = b"".join(parts)
            detail = body_bytes.decode("utf-8", errors="replace")[:2000] or detail
        except asyncio.CancelledError:
            response_text = b"".join(parts).decode("utf-8", errors="replace")
            timing_snapshot = finalize_opened_http_response(
                opened, "cancelled", "HTTP error response read cancelled",
            )
            _sync_http_proxy_bytes(proxy_bytes, opened)
            normalized = model_pricing.normalize_response_billing(response_text)
            usage = {
                "input_tokens": normalized.input_tokens,
                "output_tokens": normalized.output_tokens,
                "cache_creation": normalized.cache_creation_tokens,
                "cache_read": normalized.cache_read_tokens,
            }

            async def finish_cancelled_error_response() -> None:
                await close_response_context(opened.ctx)
                await close_proxy_client(opened.proxy_client)
                await asyncio.to_thread(
                    log_db.update_retry_attempt,
                    retry_attempt_id,
                    final_round_id=(
                        timing_snapshot.round_id if timing_snapshot is not None else None
                    ),
                    connect_ms=(
                        timing_snapshot.connection_ms
                        if timing_snapshot is not None else connect_ms
                    ),
                    first_byte_ms=(
                        timing_snapshot.first_byte_ms
                        if timing_snapshot is not None else None
                    ),
                    idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
                    total_ms=(timing_snapshot.total_ms if timing_snapshot is not None else None),
                    attempt_elapsed_ms=int(
                        (time.monotonic() - attempt_start_monotonic) * 1000
                    ),
                    ended_at=time.time(),
                    outcome="cancelled",
                    error_detail="HTTP error response read cancelled",
                    proxy_name=opened.proxy_name,
                    bytes_up=proxy_bytes.up,
                    bytes_down=proxy_bytes.down,
                    response_body=response_text or None,
                    usage=usage,
                    usage_observed=normalized.usage_observed,
                    settle=False,
                )
                await asyncio.to_thread(
                    log_db.finish_error,
                    request_id,
                    "client disconnected",
                    retry_count_so_far,
                    final_channel_key=ch.key,
                    final_channel_type=ch.type,
                    final_model=resolved_model,
                    connect_ms=(
                        timing_snapshot.connection_ms
                        if timing_snapshot is not None else connect_ms
                    ),
                    first_token_ms=(
                        timing_snapshot.first_byte_ms
                        if timing_snapshot is not None else None
                    ),
                    idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
                    total_ms=(timing_snapshot.total_ms if timing_snapshot is not None else None),
                    final_round_id=(
                        timing_snapshot.round_id if timing_snapshot is not None else None
                    ),
                    request_elapsed_ms=int(
                        (time.monotonic() - start_monotonic) * 1000
                    ),
                    http_status=499,
                    affinity_hit=affinity_hit,
                    response_body=response_text or None,
                    usage=usage,
                    usage_observed=normalized.usage_observed,
                    upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                    upstream_transport="sse",
                    proxy_name=opened.proxy_name,
                    proxy_bytes_up=proxy_bytes.up,
                    proxy_bytes_down=proxy_bytes.down,
                    status="cancelled",
                )

            await await_ws_owned(finish_cancelled_error_response())
            raise
        except BusinessTimeoutError as exc:
            outcome = exc.outcome
            detail = exc.outcome
        except httpx.TimeoutException as exc:
            outcome = "transport_timeout"
            detail = f"HTTP response transport timeout: {exc}"
        except Exception as exc:
            detail = f"HTTP error response read failed: {exc}"
        if not body_bytes and parts:
            body_bytes = b"".join(parts)
        response_text = body_bytes.decode("utf-8", errors="replace")
        result = _WsAttemptResult(
            outcome=outcome,
            error_detail=f"HTTP {status}: {detail}"[:2000],
            http_status=status,
            response_text=response_text,
            proxy_name=opened.proxy_name,
            proxy_bytes=proxy_bytes,
            upstream_protocol=ch_proto,
            upstream_transport="sse",
            retry_after_seconds=_retry_after_from_headers(response.headers),
        )
        _sync_http_proxy_bytes(proxy_bytes, opened)
        finalize_opened_http_response(opened, result.outcome, result.error_detail)
        _apply_http_snapshot(result, opened, terminal=True)
        await close_response_context(opened.ctx)
        await close_proxy_client(opened.proxy_client)
        return result

    result = _WsAttemptResult(
        connected=True,
        outcome="connected",
        http_status=status,
        connect_ms=connect_ms,
        proxy_name=opened.proxy_name,
        proxy_bytes=proxy_bytes,
        upstream_protocol=ch_proto,
        upstream_transport="sse",
        translator_ctx=upstream_req.translator_ctx,
        retry_after_seconds=_retry_after_from_headers(response.headers),
    )
    tracker = _WsTracker()
    pending: list[str] = []
    committed = False
    buf = b""
    aiter = response.aiter_bytes()
    round_terminalized = False

    def terminalize_round(outcome: str, error_detail: str | None) -> None:
        nonlocal round_terminalized
        if round_terminalized:
            return
        finalize_opened_http_response(opened, outcome, error_detail)
        _apply_http_snapshot(result, opened, terminal=True)
        _sync_http_proxy_bytes(proxy_bytes, opened)
        round_terminalized = True

    def sync_tracker_result() -> _WsAttemptResult:
        """Preserve SSE frames before the outer attempt can be settled."""
        result.response_completed = tracker.response_completed
        result.usage = dict(tracker.usage)
        result.usage_observed = tracker.usage_observed
        result.response_text = tracker.get_full_response()
        result.response_id = tracker.response_id
        result.output_items = tracker.get_output_items()
        return result

    async def finalize_and_return() -> _WsAttemptResult:
        sync_tracker_result()
        terminalize_round(result.outcome, result.error_detail)
        if retry_attempt_id is not None:
            await asyncio.to_thread(
                log_db.update_retry_attempt,
                retry_attempt_id,
                final_round_id=result.round_id,
                connect_ms=result.connect_ms,
                first_byte_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                attempt_elapsed_ms=int(
                    (time.monotonic() - attempt_start_monotonic) * 1000
                ),
                ended_at=time.time(),
                outcome=result.outcome,
                error_detail=(result.error_detail or "")[:4000] or None,
                proxy_name=opened.proxy_name,
                bytes_up=proxy_bytes.up,
                bytes_down=proxy_bytes.down,
                settle=False,
            )
        request_elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
        result.request_finalized = True
        if result.ok:
            total_ms = result.total_ms
            finalize_policy.apply_success_health_effects(
                finalize_policy.success_plan(),
                scorer=scorer,
                cooldown=cooldown,
                channel_key=ch.key,
                model=resolved_model,
                connect_ms=result.connect_ms,
                first_byte_ms=result.first_byte_ms,
                total_ms=total_ms,
            )
            _write_responses_affinity(
                api_key_name=api_key_name,
                client_ip=client_ip,
                body=body,
                response_id=result.response_id,
                output_items=result.output_items,
                channel_key=ch.key,
                resolved_model=resolved_model,
                client_key=client_key,
                translator_ctx=result.translator_ctx,
            )
            await await_ws_owned(asyncio.to_thread(
                log_db.finish_success,
                request_id, ch.key, ch.type, resolved_model,
                input_tokens=result.usage["input_tokens"],
                output_tokens=result.usage["output_tokens"],
                cache_creation_tokens=result.usage["cache_creation"],
                cache_read_tokens=result.usage["cache_read"],
                connect_ms=result.connect_ms,
                first_token_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                final_round_id=result.round_id,
                request_elapsed_ms=request_elapsed_ms,
                retry_count=retry_count_so_far,
                affinity_hit=affinity_hit,
                response_body=result.response_text,
                http_status=status,
                usage_observed=result.usage_observed,
                upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                upstream_transport="sse",
                proxy_name=opened.proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            ))
        else:
            finalize_policy.apply_error_health_effects(
                finalize_policy.error_plan(result.outcome, failure_policy="runtime"),
                scorer=scorer,
                cooldown=cooldown,
                channel_key=ch.key,
                model=resolved_model,
                error_detail=result.error_detail,
                connect_ms=result.connect_ms,
            )
            await await_ws_owned(asyncio.to_thread(
                log_db.finish_error,
                request_id,
                (result.error_detail or result.outcome)[:4000],
                retry_count_so_far,
                final_channel_key=ch.key,
                final_channel_type=ch.type,
                final_model=resolved_model,
                connect_ms=result.connect_ms,
                first_token_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                final_round_id=result.round_id,
                request_elapsed_ms=request_elapsed_ms,
                http_status=result.http_status or _http_status_from_ws_outcome(result),
                affinity_hit=affinity_hit,
                response_body=result.response_text or None,
                usage=result.usage,
                usage_observed=result.usage_observed,
                upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                upstream_transport="sse",
                proxy_name=opened.proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            ))
        return result

    async def commit_pending() -> None:
        nonlocal committed
        if committed:
            return
        committed = True
        result.closed_after_accept = True
        _apply_http_snapshot(result, opened, terminal=False)
        for item in pending:
            await _send_downstream(websocket, item)
        pending.clear()

    try:
        while True:
            try:
                chunk = await next_nonempty_http_chunk(
                    aiter, opened.timing, opened.round_timeouts,
                )
            except StopAsyncIteration:
                if opened.timing is not None:
                    opened.timing.mark_io_complete()
                if tracker.response_completed:
                    result.ok = True
                    result.outcome = "success"
                    if committed:
                        await _close_downstream(websocket, 1000, "")
                        return await finalize_and_return()
                    return await finalize_and_return()
                result.outcome = "upstream_closed" if committed else "closed_before_first_byte"
                result.error_detail = "upstream SSE ended before response.completed"
                if committed:
                    await _close_downstream(websocket, 1011, _trim_reason(result.error_detail))
                    return await finalize_and_return()
                return sync_tracker_result()
            except BusinessTimeoutError as exc:
                result.outcome = exc.outcome
                result.error_detail = exc.outcome
                if committed:
                    await _close_downstream(websocket, 4504, result.error_detail)
                    return await finalize_and_return()
                return sync_tracker_result()
            except httpx.TimeoutException as exc:
                result.outcome = "transport_timeout"
                result.error_detail = f"upstream SSE transport timeout: {exc}"
                if committed:
                    await _close_downstream(websocket, 4504, result.error_detail)
                    return await finalize_and_return()
                return sync_tracker_result()
            except Exception as exc:
                result.outcome = "transport_error" if committed else "closed_before_first_byte"
                result.error_detail = f"read upstream SSE: {exc}"[:2000]
                if committed:
                    await _close_downstream(websocket, 1011, _trim_reason(result.error_detail))
                    return await finalize_and_return()
                return sync_tracker_result()

            _sync_http_proxy_bytes(proxy_bytes, opened)
            buf += chunk
            buf = buf.replace(b"\r\n", b"\n")
            buf, blocks = upstream.split_sse_events(buf)
            for block in blocks:
                event_name, data = upstream.parse_sse_event_bytes(block)
                if data is None:
                    continue
                if event_name and not data.get("type"):
                    data = dict(data)
                    data["type"] = event_name
                frame_text = _dump_frame(data)
                tracker.feed_text(frame_text)
                event_type = _ws_event_type(frame_text)
                if tracker.response_completed and opened.timing is not None:
                    opened.timing.mark_io_complete()

                if tracker.response_failed:
                    if opened.timing is not None:
                        opened.timing.mark_io_complete()
                    is_context_error = (
                        tracker.stream_error_code == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                    )
                    result.outcome = (
                        "request_invalid" if is_context_error
                        else "stream_upstream_error" if event_type == "response.failed"
                        else "upstream_error_json"
                    )
                    result.http_status = 400 if is_context_error else result.http_status
                    result.error_code = tracker.stream_error_code
                    result.error_detail = tracker.stream_error_message or frame_text[:2000]
                    if event_type == "response.failed" or committed or is_context_error:
                        if not committed:
                            if not is_context_error:
                                pending.append(frame_text)
                            await commit_pending()
                        elif not is_context_error:
                            await _send_downstream(websocket, frame_text)
                        if is_context_error:
                            await _send_context_length_error_frame(websocket, result.error_detail)
                        await _close_downstream(
                            websocket,
                            4400 if is_context_error else 1011,
                            _trim_reason(result.error_detail),
                        )
                        return await finalize_and_return()
                    return sync_tracker_result()

                visible = _is_ws_visible_event_type(event_type)
                if visible:
                    bl_hit = blacklist.match(frame_text, ch.key)
                    if bl_hit:
                        result.outcome = "blacklist_hit"
                        result.error_detail = f"blacklist: {bl_hit}"
                        if committed:
                            await _close_downstream(websocket, 1011, _trim_reason(result.error_detail))
                            return await finalize_and_return()
                        return sync_tracker_result()

                if not committed:
                    pending.append(frame_text)
                    if visible:
                        await commit_pending()
                    elif tracker.response_completed:
                        result.ok = True
                        result.outcome = "success"
                        await commit_pending()
                        await _close_downstream(websocket, 1000, "")
                        return await finalize_and_return()
                    continue

                await _send_downstream(websocket, frame_text)
                if tracker.response_completed:
                    result.ok = True
                    result.outcome = "success"
                    await _close_downstream(websocket, 1000, "")
                    return await finalize_and_return()
    except WebSocketDisconnect:
        result.outcome = "client_disconnected"
        result.error_detail = "client disconnected"
        if committed:
            return await finalize_and_return()
        return sync_tracker_result()
    except asyncio.CancelledError:
        async def finish_cancelled_bridge() -> None:
            if result.request_finalized:
                return
            result.outcome = "cancelled"
            result.error_detail = "upstream SSE bridge cancelled"
            sync_tracker_result()
            terminalize_round(result.outcome, result.error_detail)
            result.request_finalized = True
            try:
                await asyncio.to_thread(
                    log_db.update_retry_attempt,
                    retry_attempt_id,
                    final_round_id=result.round_id,
                    connect_ms=result.connect_ms,
                    first_byte_ms=result.first_byte_ms,
                    idle_ms=result.idle_ms,
                    total_ms=result.total_ms,
                    attempt_elapsed_ms=int((time.monotonic() - attempt_start_monotonic) * 1000),
                    ended_at=time.time(),
                    outcome="cancelled",
                    error_detail=result.error_detail,
                    proxy_name=opened.proxy_name,
                    bytes_up=proxy_bytes.up,
                    bytes_down=proxy_bytes.down,
                    response_body=result.response_text or None,
                    usage=result.usage,
                    usage_observed=result.usage_observed,
                    settle=False,
                )
                await asyncio.to_thread(
                    log_db.finish_error,
                    request_id,
                    "client disconnected",
                    retry_count_so_far,
                    final_channel_key=ch.key,
                    final_channel_type=ch.type,
                    final_model=resolved_model,
                    connect_ms=result.connect_ms,
                    first_token_ms=result.first_byte_ms,
                    idle_ms=result.idle_ms,
                    total_ms=result.total_ms,
                    final_round_id=result.round_id,
                    request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
                    http_status=499,
                    affinity_hit=affinity_hit,
                    response_body=result.response_text or None,
                    usage=result.usage,
                    usage_observed=result.usage_observed,
                    upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                    upstream_transport="sse",
                    proxy_name=opened.proxy_name,
                    proxy_bytes_up=proxy_bytes.up,
                    proxy_bytes_down=proxy_bytes.down,
                    status="cancelled",
                )
            except Exception:
                pass

        await await_ws_owned(finish_cancelled_bridge())
        raise
    finally:
        if not round_terminalized:
            terminalize_round(result.outcome, result.error_detail)
        try:
            await await_ws_owned(close_response_context(opened.ctx))
            await await_ws_owned(close_proxy_client(opened.proxy_client))
        except BaseException:
            pass


async def _relay_ws_session(
    websocket: WebSocket,
    upstream_ws,
    *,
    first_obj: dict,
    ch: Channel,
    resolved_model: str,
    body: dict,
    request_id: str,
    retry_count_so_far: int,
    affinity_hit: int,
    api_key_name: str,
    client_ip: str,
    fp_query: Optional[str],
    client_key: Optional[str],
    start_time: float,
    start_monotonic: float,
    attempt_start_monotonic: float,
    deadline_ts: float,
    connect_ms: int,
    first_byte_timeout: int,
    idle_timeout: int,
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
    translator_ctx: Optional[dict],
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
    route_attempt_id,
    retry_attempt_id,
    on_dispatch,
    relay_state: dict[str, Any],
) -> _WsAttemptResult:
    tracker = _WsTracker()
    result = _WsAttemptResult(
        connected=True,
        outcome="connected",
        connect_ms=connect_ms,
        proxy_name=proxy_name,
        proxy_bytes=proxy_bytes,
        upstream_protocol=getattr(ch, "protocol", "openai-responses"),
        translator_ctx=translator_ctx,
    )

    # ── Identity confuse state (shared across the session lifetime) ──
    _identity_confuse_state = ConfuseState()
    _session_pck = str(body.get("prompt_cache_key") or "").strip()
    _original_pck = str(first_obj.get("prompt_cache_key") or _session_pck).strip()
    if isinstance(ch, OpenAIOAuthChannel) and api_key_name:
        _identity_confuse_state = ConfuseState(enabled=True, auth_id=api_key_name)

    def _apply_identity_confuse_to_frame(obj: dict) -> dict:
        """Apply identity confuse to a WS create frame's prompt/cache metadata."""
        nonlocal _identity_confuse_state
        if not _identity_confuse_state.enabled:
            return obj
        obj = dict(obj)
        frame_raw_pck = str(obj.get("prompt_cache_key") or _original_pck).strip()
        cm = obj.get("client_metadata") if isinstance(obj.get("client_metadata"), dict) else {}
        confused_cm, _identity_confuse_state = confuse_client_metadata(
            api_key_name, cm, session_prompt_cache_key=_session_pck,
            state=_identity_confuse_state, original_prompt_cache_key=frame_raw_pck,
        )
        if confused_cm:
            obj["client_metadata"] = confused_cm
        elif "client_metadata" in obj:
            obj.pop("client_metadata", None)
        if _session_pck:
            obj["prompt_cache_key"] = _session_pck
        return obj

    def sync_tracker_result() -> _WsAttemptResult:
        """Hydrate attempt facts before any immutable settlement can run."""
        result.response_completed = tracker.response_completed
        result.usage = dict(tracker.usage)
        result.usage_observed = tracker.usage_observed
        result.response_text = _identity_log_text(
            tracker.get_full_response(), _identity_confuse_state,
        )
        result.response_id = tracker.response_id
        result.output_items = tracker.get_output_items()
        return result

    # The caller owns cancellation finalization. Expose a synchronous snapshot
    # closure so it can retain frames observed before this coroutine is
    # cancelled, without leaking tracker internals into the transport API.
    relay_state["sync_result"] = sync_tracker_result

    async def finalize_accepted_request() -> _WsAttemptResult:
        if result.request_finalized:
            return result
        sync_tracker_result()
        _persist_ws_route_round(
            route_attempt_id,
            timing,
            proxy_bytes,
            outcome=result.outcome,
            error_detail=result.error_detail,
            terminal=True,
        )
        _apply_ws_snapshot(result, timing, terminal=True)
        if retry_attempt_id is not None:
            await asyncio.to_thread(
                log_db.update_retry_attempt,
                retry_attempt_id,
                final_round_id=result.round_id,
                connect_ms=result.connect_ms,
                first_byte_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                attempt_elapsed_ms=int(
                    (time.monotonic() - attempt_start_monotonic) * 1000
                ),
                ended_at=time.time(),
                outcome=result.outcome,
                error_detail=(result.error_detail or "")[:4000] or None,
                proxy_name=proxy_name,
                bytes_up=proxy_bytes.up,
                bytes_down=proxy_bytes.down,
                settle=False,
            )
        result.request_finalized = True
        request_elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)

        if result.ok:
            finalize_policy.apply_success_health_effects(
                finalize_policy.success_plan(),
                scorer=scorer,
                cooldown=cooldown,
                channel_key=ch.key,
                model=resolved_model,
                connect_ms=result.connect_ms,
                first_byte_ms=result.first_byte_ms,
                total_ms=result.total_ms,
            )
            _write_responses_affinity(
                api_key_name=api_key_name,
                client_ip=client_ip,
                body=body,
                response_id=result.response_id,
                output_items=result.output_items,
                channel_key=ch.key,
                resolved_model=resolved_model,
                client_key=client_key,
                translator_ctx=result.translator_ctx,
            )
            await await_ws_owned(asyncio.to_thread(
                log_db.finish_success,
                request_id, ch.key, ch.type, resolved_model,
                input_tokens=result.usage["input_tokens"],
                output_tokens=result.usage["output_tokens"],
                cache_creation_tokens=result.usage["cache_creation"],
                cache_read_tokens=result.usage["cache_read"],
                connect_ms=result.connect_ms,
                first_token_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                final_round_id=result.round_id,
                request_elapsed_ms=request_elapsed_ms,
                retry_count=retry_count_so_far,
                affinity_hit=affinity_hit,
                response_body=result.response_text,
                http_status=101,
                usage_observed=result.usage_observed,
                upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                upstream_transport=result.upstream_transport,
                proxy_name=proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            ))
        else:
            finalize_policy.apply_error_health_effects(
                finalize_policy.error_plan(result.outcome, failure_policy="runtime"),
                scorer=scorer,
                cooldown=cooldown,
                channel_key=ch.key,
                model=resolved_model,
                error_detail=result.error_detail,
                connect_ms=result.connect_ms,
            )
            await await_ws_owned(asyncio.to_thread(
                log_db.finish_error,
                request_id,
                (result.error_detail or result.outcome)[:4000],
                retry_count_so_far,
                final_channel_key=ch.key,
                final_channel_type=ch.type,
                final_model=resolved_model,
                connect_ms=result.connect_ms,
                first_token_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                final_round_id=result.round_id,
                request_elapsed_ms=request_elapsed_ms,
                http_status=_http_status_from_ws_outcome(result),
                affinity_hit=affinity_hit,
                response_body=result.response_text or None,
                usage=result.usage,
                usage_observed=result.usage_observed,
                upstream_protocol=getattr(ch, "protocol", "openai-responses"),
                upstream_transport=result.upstream_transport,
                proxy_name=proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
                status=(
                    "cancelled"
                    if result.outcome in ("cancelled", "client_disconnected")
                    else "error"
                ),
            ))
        return result

    # Send first frame upstream before accepting downstream. If upstream rejects
    # before a downstream-visible event, the attempt can still fail over.
    try:
        first_upstream_obj = _map_ws_create_frame_for_upstream(first_obj, resolved_model, channel=ch)
        first_upstream_obj = _apply_identity_confuse_to_frame(first_upstream_obj)
        log_db.update_pending_fast_mode_from_upstream(request_id, first_upstream_obj)
        payload_to_send: str | bytes = _dump_frame(first_upstream_obj)
        if retry_attempt_id is not None:
            try:
                log_db.mark_retry_attempt_dispatch(retry_attempt_id, first_upstream_obj)
            except Exception:
                pass
        on_dispatch()
        proxy_bytes.count(up=_frame_size(payload_to_send))
        await wait_ws_round_io(
            upstream_ws.send(payload_to_send),
            timing=timing,
            round_timeouts=round_timeouts,
        )
    except BusinessTimeoutError as exc:
        result.outcome = exc.outcome
        result.error_detail = exc.outcome
        sync_tracker_result()
        return _apply_ws_snapshot(result, timing, terminal=True)
    except asyncio.TimeoutError as exc:
        result.outcome = "transport_timeout"
        result.error_detail = f"send first websocket frame transport timeout: {exc}"
        sync_tracker_result()
        return _apply_ws_snapshot(result, timing, terminal=True)
    except Exception as exc:
        result.outcome = "transport_error"
        result.error_detail = f"send first websocket frame: {exc}"
        return sync_tracker_result()

    pending_visible: list[str | bytes] = []
    first_wait = round_timeouts.first_byte
    try:
        first_visible = await _recv_until_first_visible_ws_event(
            upstream_ws, tracker, pending_visible, ch.key, first_wait,
            deadline_ts=deadline_ts, idle_timeout=idle_timeout,
            result=result, proxy_bytes=proxy_bytes,
            timing=timing, round_timeouts=round_timeouts,
            timeout_label_seconds=first_byte_timeout,
        )
    except asyncio.TimeoutError:
        result.outcome = "first_byte_timeout"
        result.error_detail = f"first websocket event timeout > {first_byte_timeout}s"
        return sync_tracker_result()
    except websockets.ConnectionClosed as exc:
        result.outcome = "closed_before_first_byte"
        result.error_detail = f"upstream closed before first visible websocket event: {exc}"
        return sync_tracker_result()
    except Exception as exc:
        result.outcome = "closed_before_first_byte"
        result.error_detail = f"upstream closed before first visible websocket event: {exc}"
        return sync_tracker_result()

    if first_visible is None:
        if result.ok or result.closed_after_accept:
            # A terminal Responses frame can contain the complete output even
            # when no delta/item event preceded it. It is not a failover commit
            # boundary, but once accepted it still must reach the downstream.
            if pending_visible:
                result.closed_after_accept = True
                _apply_ws_snapshot(result, timing, terminal=False)
                for item in pending_visible:
                    await _send_downstream(
                        websocket,
                        _identity_expose_frame(item, _identity_confuse_state),
                    )
            if result.ok:
                await _close_downstream(websocket, 1000, "")
            else:
                await _close_downstream(
                    websocket,
                    _ws_close_code_for_http(_http_status_from_ws_outcome(result)),
                    _trim_reason(result.error_detail or result.outcome),
                )
            return await finalize_accepted_request()
        return sync_tracker_result()

    _apply_ws_snapshot(result, timing, terminal=False)
    result.closed_after_accept = True
    for item in pending_visible:
        await _send_downstream(websocket, _identity_expose_frame(item, _identity_confuse_state))

    async def downstream_to_upstream() -> None:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                code = int(msg.get("code") or 1000)
                try:
                    await upstream_ws.close(code=code)
                except Exception:
                    pass
                return
            if msg.get("text") is not None:
                data: str | bytes = msg["text"]
                is_text = True
            elif msg.get("bytes") is not None:
                data = msg["bytes"]
                is_text = False
            else:
                continue
            try:
                obj = _loads_frame(data)
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("type") == "response.create":
                # One outer retry attempt represents exactly one real model call.
                # Supporting another create would require a fresh immutable
                # retry/usage row, so reject it instead of silently underbilling.
                result.outcome = "request_invalid"
                result.http_status = 400
                result.error_detail = (
                    "only one response.create is allowed per websocket session"
                )
                await _send_request_invalid_error_frame(
                    websocket, result.error_detail, param="type",
                )
                await _close_downstream(websocket, 4400, result.error_detail)
                return
            proxy_bytes.count(up=_frame_size(data))
            await wait_ws_round_io(
                upstream_ws.send(data, text=is_text),
                timing=timing,
                round_timeouts=round_timeouts,
            )

    async def upstream_to_downstream() -> None:
        nonlocal result
        while True:
            step = await read_next_responses_ws_step(
                upstream_ws,
                tracker,
                channel_key=ch.key,
                deadline_ts=deadline_ts,
                idle_timeout=idle_timeout,
                proxy_bytes=proxy_bytes,
                frame_transform=lambda frame: _identity_expose_frame(frame, _identity_confuse_state),
                skip_event_types=(),
                blacklist_before_error=True,
                timing=timing,
                round_timeouts=round_timeouts,
            )
            if step.outcome in (
                "connection_timeout", "first_byte_timeout", "idle_timeout",
                "total_timeout", "transport_timeout",
            ):
                result.outcome = step.outcome
                result.error_detail = step.error_detail
                await _close_downstream(websocket, 4504, result.error_detail)
                return
            if step.outcome == "upstream_closed":
                result.outcome = "upstream_closed"
                result.error_detail = step.error_detail
                await _close_downstream(websocket, step.close_code, step.close_reason)
                return
            if step.outcome == "blacklist_hit":
                result.outcome = "blacklist_hit"
                result.error_detail = step.error_detail
                await _close_downstream(websocket, 1011, _trim_reason(result.error_detail))
                return
            if step.outcome == "request_invalid":
                result.outcome = "request_invalid"
                result.http_status = 400
                result.error_detail = step.error_detail or protocol_errors.responses_max_output_context_error_message()
                await _send_context_length_error_frame(websocket, result.error_detail)
                await _close_downstream(websocket, 4400, _trim_reason(result.error_detail))
                return
            if step.data is not None and not step.skip_downstream:
                await _send_downstream(websocket, step.data)
            if step.outcome == "stream_upstream_error":
                result.outcome = "stream_upstream_error"
                result.error_detail = step.error_detail
                close_code = 1011 if step.data is not None else step.close_code
                close_reason = _trim_reason(result.error_detail) if step.data is not None else step.close_reason
                await _close_downstream(websocket, close_code, close_reason)
                return
            if step.outcome == "success":
                result.ok = True
                result.outcome = "success"
                close_code = 1000 if step.data is not None else step.close_code
                close_reason = "" if step.data is not None else step.close_reason
                await _close_downstream(websocket, close_code, close_reason)
                return
            if step.skip_downstream:
                continue

    t_down = asyncio.create_task(downstream_to_upstream())
    t_up = asyncio.create_task(upstream_to_downstream())
    tasks = {t_down, t_up}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if (
        t_down in done
        and result.outcome == "connected"
        and not tracker.response_completed
        and not tracker.response_failed
    ):
        result.outcome = "client_disconnected"
        result.error_detail = "client disconnected"
    for task in done:
        exc = task.exception()
        if exc is None:
            continue
        if isinstance(exc, WebSocketDisconnect):
            if tracker.response_completed:
                result.ok = True
                result.outcome = "success"
            elif tracker.response_failed:
                result.outcome = "stream_upstream_error"
                result.error_detail = tracker.stream_error_message or "upstream stream error"
            else:
                result.outcome = "client_disconnected"
                result.error_detail = "client disconnected"
            continue
        if isinstance(exc, BusinessTimeoutError):
            result.outcome = exc.outcome
            result.error_detail = exc.outcome
            continue
        result.outcome = "transport_error"
        result.error_detail = f"websocket relay error: {exc}"

    return await finalize_accepted_request()


async def _build_ws_upstream_request(
    ch: Channel,
    body: dict,
    resolved_model: str,
    *,
    websocket: WebSocket,
) -> UpstreamRequest:
    if isinstance(ch, OpenAIOAuthChannel):
        # Reuse the OAuth channel's Codex transform/header logic. This returns
        # an HTTP UpstreamRequest; for WS we only need its URL base + headers.
        req = await ch.build_upstream_request(body, resolved_model, ingress_protocol="responses")
        ws_url = _http_url_to_ws(req.url)
        headers = _merge_ws_headers(req.headers, websocket)
        # Codex WebSocket uses the same session/thread identity header names as
        # official codex-rs. Keep old HTTP headers too for compatibility with the
        # internal endpoint while adding the WS names.
        sid = headers.get("session-id") or headers.get("session_id")
        tid = headers.get("thread-id") or sid
        if not sid:
            api_key_name = str(body.get("_api_key_name") or "")
            raw_anchor = str(body.get("prompt_cache_key") or "").strip()
            if api_key_name and raw_anchor:
                sid = _isolate_session_id(api_key_name, raw_anchor)
                tid = sid
        if sid:
            headers.setdefault("session-id", sid)
        if tid:
            headers.setdefault("thread-id", tid)
            headers.setdefault("x-client-request-id", tid)
        # Codex CLI only sends session-id / thread-id (hyphenated).
        for _ck in [k for k in list(headers) if str(k).lower() in ("session_id", "conversation_id", "conversation-id")]:
            del headers[_ck]
        # Identity confuse: obfuscate identity headers for OAuth channels.
        api_key_name = str(body.get("_api_key_name") or "")
        session_pck = sid or ""  # session_id is already the isolated prompt_cache_key
        if api_key_name and isinstance(ch, OpenAIOAuthChannel):
            _hdr_state = ConfuseState(enabled=True, auth_id=api_key_name)
            raw_anchor = str(body.get("prompt_cache_key") or "").strip()
            if session_pck:
                _hdr_state.original_prompt_cache_key = raw_anchor
                _hdr_state.confused_prompt_cache_key = session_pck
            headers = confuse_identity_headers(headers, _hdr_state,
                                               session_prompt_cache_key=session_pck)
        else:
            # Codex CLI only sends session-id / thread-id (hyphenated).
            for _ck in [k for k in list(headers) if str(k).lower() in ("session_id", "conversation_id", "conversation-id")]:
                del headers[_ck]
        return UpstreamRequest(url=ws_url, headers=headers, body=b"", translator_ctx=req.translator_ctx)

    # Third-party OpenAI Responses API channel. Only same-protocol channels are
    # valid here; chat upstream cannot speak Responses WS.
    req = await ch.build_upstream_request(body, resolved_model, ingress_protocol="responses")
    return UpstreamRequest(
        url=_http_url_to_ws(req.url),
        headers=_merge_ws_headers(req.headers, websocket),
        body=b"",
        dynamic_tool_map=req.dynamic_tool_map,
        translator_ctx=req.translator_ctx,
    )


def _merge_ws_headers(upstream_headers: dict[str, str], websocket: WebSocket) -> dict[str, str]:
    return merge_responses_ws_headers(
        upstream_headers,
        websocket.headers,
        forward_client_headers=_FORWARD_CLIENT_HEADERS,
    )


def _http_url_to_ws(url: str) -> str:
    return http_url_to_ws(url)


async def _connect_upstream_ws(
    url: str,
    *,
    headers: dict[str, str],
    connector,
    proxy_bytes: _WsProxyBytes,
    open_timeout: float,
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
):
    return await connect_upstream_ws(
        url,
        headers=headers,
        connector=connector,
        proxy_bytes=proxy_bytes,
        open_timeout=open_timeout,
        timing=timing,
        round_timeouts=round_timeouts,
        open_socket_func=_open_socket_via_ss2022,
        connect_func=websockets.connect,
    )


def _socks5h_url(url: str) -> str:
    return socks5h_url(url)


async def _open_socket_via_ss2022(
    url: str,
    connector: SS2022Connector,
    proxy_bytes: _WsProxyBytes,
    *,
    timeout: float,
):
    return await open_socket_via_ss2022(
        url, connector, proxy_bytes, timeout=timeout,
    )


def _ws_proxy_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    return ws_route_kwargs(ch, resolved_model)


def _resolve_ws_route_chain(ch: Channel, resolved_model: str) -> list[tuple[str, Any | None]]:
    return resolve_ws_route_chain(ch, resolved_model)


def _legacy_socks5_connector() -> SOCKS5Connector | None:
    return legacy_socks5_connector()


def _pick_non_direct_proxy_name(ch: Channel, resolved_model: str) -> str | None:
    try:
        from ..proxy import manager as pm
        pm.init()
        if pm.is_configured():
            target = pm.resolve_proxy_target(**_ws_proxy_route_kwargs(ch, resolved_model))
            for name in pm.expand_target(target):
                conn = pm.get_connector(name)
                if conn is not None and getattr(conn, "type", "") != "direct":
                    return name
    except Exception:
        pass
    if _legacy_socks5_connector() is not None:
        return "legacy-socks5"
    return None


def _maybe_record_codex_ws_snapshot(ch: Channel, ws_response: Any) -> None:
    if not isinstance(ch, OpenAIOAuthChannel) or ws_response is None:
        return
    try:
        headers_obj = getattr(ws_response, "headers", None)
        if not headers_obj:
            return
        headers = {str(k): str(v) for k, v in headers_obj.items()}
        from .. import failover
        # Reuse HTTP failover's response-header path so passive quota snapshot,
        # threshold auto-disable, and notification behavior stay identical.
        fake_resp = type("_WsResp", (), {"headers": headers})()
        failover._maybe_record_codex_snapshot(ch, fake_resp)
    except Exception as exc:
        print(f"[responses_ws] codex snapshot record failed for {getattr(ch, 'email', '?')}: {exc}")


async def _finalize_ws_attempt_after_accept(
    result: _WsAttemptResult,
    ch: Channel,
    resolved_model: str,
    request_id: str,
    retry_count_so_far: int,
    affinity_hit: int,
    start_time: float,
    start_monotonic: float,
) -> None:
    plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
    finalize_policy.apply_error_health_effects(
        plan,
        scorer=scorer,
        cooldown=cooldown,
        channel_key=ch.key,
        model=resolved_model,
        error_detail=result.error_detail,
        connect_ms=result.connect_ms,
    )
    await asyncio.shield(asyncio.to_thread(
        log_db.finish_error,
        request_id,
        (result.error_detail or result.outcome)[:4000],
        retry_count_so_far,
        final_channel_key=ch.key,
        final_channel_type=ch.type,
        final_model=resolved_model,
        connect_ms=result.connect_ms,
        first_token_ms=result.first_byte_ms,
        idle_ms=result.idle_ms,
        total_ms=result.total_ms,
        final_round_id=result.round_id,
        request_elapsed_ms=int((time.monotonic() - start_monotonic) * 1000),
        http_status=_http_status_from_ws_outcome(result),
        affinity_hit=affinity_hit,
        response_body=result.response_text or None,
        usage=result.usage,
        usage_observed=result.usage_observed,
        upstream_protocol=getattr(ch, "protocol", "openai-responses"),
        upstream_transport=result.upstream_transport,
        proxy_name=result.proxy_name,
        proxy_bytes_up=result.proxy_bytes.up,
        proxy_bytes_down=result.proxy_bytes.down,
    ))


def _write_responses_affinity(
    *,
    api_key_name: str,
    client_ip: str,
    body: dict,
    response_id: Optional[str],
    output_items: list[dict],
    channel_key: str,
    resolved_model: str,
    client_key: Optional[str],
    translator_ctx: Optional[dict] = None,
) -> None:
    try:
        cur_input = resolve_current_input_items(body or {})
        fp_write = fingerprint.fingerprint_write_responses(
            api_key_name or "", client_ip or "", cur_input, output_items,
        )
        prompt_cache_key = str((body or {}).get("prompt_cache_key") or "") or None
        if fp_write:
            affinity.upsert(
                fp_write, channel_key, resolved_model,
                prompt_cache_key=prompt_cache_key,
            )
        if response_id:
            try:
                from ..openai import store as openai_store
                if openai_store.is_enabled():
                    openai_store.save(
                        str(response_id),
                        str((body or {}).get("previous_response_id") or "") or None,
                        api_key_name=api_key_name or "",
                        model=resolved_model,
                        channel_key=channel_key,
                        input_items=cur_input,
                        output_items=output_items,
                    )
            except Exception:
                pass
        if client_key:
            affinity.client_upsert(client_key, channel_key, resolved_model)
    except Exception:
        pass


def _request_body_from_ws_create(obj: dict) -> dict:
    return request_body_from_ws_create(obj)


def _sync_prompt_cache_key_to_ws_create(obj: dict, body: dict) -> None:
    sync_prompt_cache_key_to_ws_create(obj, body)


def _sync_translated_body_to_ws_create(obj: dict, body: dict) -> None:
    sync_translated_body_to_ws_create(obj, body)


def _map_ws_create_frame_for_upstream(obj: dict, model: str, *, channel: Channel | None = None) -> dict:
    return map_ws_create_frame_for_upstream(obj, model, channel=channel)


def _loads_frame(raw: str | bytes) -> Any:
    return loads_frame(raw)


def _dump_frame(obj: dict) -> str:
    return dump_frame(obj)


def _identity_expose_frame(data: str | bytes, state: ConfuseState) -> str | bytes:
    return identity_expose_frame(data, state)


def _identity_log_text(text: str, state: ConfuseState) -> str:
    return identity_log_text(text, state)


async def _send_downstream(websocket: WebSocket, data: str | bytes) -> None:
    if isinstance(data, bytes):
        await websocket.send_bytes(data)
    else:
        await websocket.send_text(data)


async def _send_request_invalid_error_frame(
    websocket: WebSocket,
    message: str,
    *,
    code: str = "invalid_request_error",
    param: str | None = None,
) -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await _send_downstream(websocket, _dump_frame({
            "type": "error",
            "code": code,
            "message": message,
            "param": param,
            "sequence_number": 0,
            "error_type": "invalid_request_error",
        }))
    except Exception:
        pass


async def _send_context_length_error_frame(websocket: WebSocket, message: str) -> None:
    await _send_request_invalid_error_frame(
        websocket,
        message,
        code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
    )


async def _close_downstream(websocket: WebSocket, code: int, reason: str = "") -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=_trim_reason(reason))
    except Exception:
        pass


def _frame_size(data: str | bytes) -> int:
    return ws_frame_size(data)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _format_ws_error(evt: dict) -> str:
    return format_responses_ws_error(evt)


def _ws_event_type(data: str | bytes) -> str:
    return ws_event_type(data)


def _is_ws_visible_event_type(event_type: str) -> bool:
    # Keep the WS lock boundary aligned with HTTP/SSE Responses failover:
    # metadata/control events are not enough; only real assistant/tool output
    # makes the attempt irreversible.
    return is_responses_ws_visible_event_type(event_type)


async def _recv_until_first_visible_ws_event(
    upstream_ws,
    tracker: _WsTracker,
    pending_visible: list[str | bytes],
    channel_key: str,
    first_wait: float,
    *,
    deadline_ts: float,
    idle_timeout: int,
    result: _WsAttemptResult,
    proxy_bytes: _WsProxyBytes,
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
    timeout_label_seconds: float | int | None = None,
) -> str | bytes | None:
    step = await read_until_first_responses_ws_visible_event(
        upstream_ws,
        tracker,
        channel_key=channel_key,
        deadline_ts=deadline_ts,
        first_wait=first_wait,
        idle_timeout=idle_timeout,
        proxy_bytes=proxy_bytes,
        parse_wrapped_errors=True,
        timeout_detail_mode="event",
        timeout_label_seconds=timeout_label_seconds if timeout_label_seconds is not None else first_wait,
        use_tracker_error_detail=True,
        timing=timing,
        round_timeouts=round_timeouts,
    )
    pending_visible.extend(step.pending)
    _apply_ws_snapshot(result, timing, terminal=False)
    if step.ok:
        result.ok = True
        result.outcome = "success"
    elif step.outcome is not None:
        result.outcome = step.outcome
        result.error_detail = step.error_detail
        result.error_code = step.error_code
        result.http_status = step.http_status
    if step.closed_after_accept:
        result.closed_after_accept = True
    return step.visible_frame


def _parse_wrapped_ws_error(text: str) -> Optional[dict]:
    return parse_wrapped_responses_ws_error(text)


def _is_retryable_ws_error_before_accept(err: dict) -> bool:
    return is_retryable_responses_ws_error_before_accept(err)


def _invalid_status_detail(exc: InvalidStatus) -> str:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    body = ""
    try:
        body = getattr(resp, "body", b"") or b""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return f"HTTP {status}: {body[:1000]}" if status else str(exc)


def _http_status_from_ws_outcome(result: Optional[_WsAttemptResult]) -> int:
    return responses_ws_http_status_from_attempt(result)


def _ws_close_code_for_http(status: int) -> int:
    return ws_close_code_for_http_status(status)


def _trim_reason(reason: str, limit: int = 120) -> str:
    # WebSocket close reason is capped at 123 bytes. Keep a little margin.
    text = str(reason or "")
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def _websocket_client_ip(websocket: WebSocket) -> str:
    try:
        return get_client_ip(websocket)
    except Exception:
        try:
            return websocket.client.host if websocket.client else "?"
        except Exception:
            return "?"


def _is_ws_capable_channel(ch: Channel) -> bool:
    # WebSocket /v1/responses can only connect to upstreams that speak
    # OpenAI Responses WS. HTTP/SSE can translate responses↔chat, but there is
    # no equivalent WS transport for /v1/chat/completions; filter those
    # candidates before retry accounting/scoring so they are not treated as
    # failed channels.
    return getattr(ch, "protocol", "anthropic") == "openai-responses"


def _should_cooldown(outcome: str) -> bool:
    return should_cooldown(outcome)
