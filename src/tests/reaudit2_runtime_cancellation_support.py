"""Deterministic cancellation gates for P15 transport/stream ownership.

Every injected cancellation waits on an Event at the exact persistence await;
no timing sleep is used to discover the window. Log fakes intentionally mirror
production's explicit signatures so misspelled or surplus arguments fail fast.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketState

from src import failover
from src.openai import responses_ws
from src.protocols.runtime import AttemptResult
from src.transports import http_runtime
from src.transports.http_runtime import HttpStreamStartResult, OpenedHttpResponse
from src.transports.timing import HttpAttemptTiming, RoundTimeouts


class _StrictLogFakes:
    def __init__(self) -> None:
        self.proxy_records: list[dict[str, Any]] = []
        self.proxy_updates: list[dict[str, Any]] = []
        self.retry_records: list[dict[str, Any]] = []
        self.retry_updates: list[dict[str, Any]] = []
        self.request_terminals: list[dict[str, Any]] = []
        self.pending_records: list[dict[str, Any]] = []

    def record_proxy_attempt(
        self,
        request_id,
        retry_attempt_id,
        attempt_order,
        proxy_name,
        started_at,
        *,
        round_id=None,
        transport=None,
        request_mode=None,
    ):
        handle = f"proxy-{len(self.proxy_records) + 1}"
        self.proxy_records.append({
            "handle": handle,
            "request_id": request_id,
            "retry_attempt_id": retry_attempt_id,
            "attempt_order": attempt_order,
            "proxy_name": proxy_name,
            "started_at": started_at,
            "round_id": round_id,
            "transport": transport,
            "request_mode": request_mode,
        })
        return handle

    def update_proxy_attempt(
        self,
        proxy_attempt_id,
        started_at=None,
        connect_ms=None,
        first_byte_ms=None,
        idle_ms=None,
        total_ms=None,
        dns_ms=None,
        tcp_ms=None,
        proxy_tcp_ms=None,
        proxy_tunnel_ms=None,
        tls_ms=None,
        target_tls_ms=None,
        ws_handshake_ms=None,
        request_upload_ms=None,
        response_headers_wait_ms=None,
        response_body_first_byte_wait_ms=None,
        ended_at=None,
        outcome=None,
        error_detail=None,
        bytes_up=None,
        bytes_down=None,
    ) -> None:
        self.proxy_updates.append({
            "proxy_attempt_id": proxy_attempt_id,
            "started_at": started_at,
            "connect_ms": connect_ms,
            "first_byte_ms": first_byte_ms,
            "idle_ms": idle_ms,
            "total_ms": total_ms,
            "dns_ms": dns_ms,
            "tcp_ms": tcp_ms,
            "proxy_tcp_ms": proxy_tcp_ms,
            "proxy_tunnel_ms": proxy_tunnel_ms,
            "tls_ms": tls_ms,
            "target_tls_ms": target_tls_ms,
            "ws_handshake_ms": ws_handshake_ms,
            "request_upload_ms": request_upload_ms,
            "response_headers_wait_ms": response_headers_wait_ms,
            "response_body_first_byte_wait_ms": response_body_first_byte_wait_ms,
            "ended_at": ended_at,
            "outcome": outcome,
            "error_detail": error_detail,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
        })

    def record_retry_attempt(
        self,
        request_id,
        attempt_order,
        channel_key,
        channel_type,
        model,
        started_at,
        proxy_name=None,
        outbound_service_tier=None,
        upstream_protocol=None,
        client_visible_model=None,
    ):
        handle = f"retry-{len(self.retry_records) + 1}"
        self.retry_records.append({
            "handle": handle,
            "request_id": request_id,
            "attempt_order": attempt_order,
            "channel_key": channel_key,
            "channel_type": channel_type,
            "model": model,
            "started_at": started_at,
            "proxy_name": proxy_name,
            "outbound_service_tier": outbound_service_tier,
            "upstream_protocol": upstream_protocol,
            "client_visible_model": client_visible_model,
        })
        return handle

    def update_retry_attempt(
        self,
        attempt_id,
        final_round_id=None,
        connect_ms=None,
        first_byte_ms=None,
        idle_ms=None,
        attempt_elapsed_ms=None,
        request_upload_ms=None,
        response_headers_wait_ms=None,
        response_body_first_byte_wait_ms=None,
        total_ms=None,
        ended_at=None,
        outcome=None,
        error_detail=None,
        proxy_name=None,
        bytes_up=None,
        bytes_down=None,
        response_body=None,
        usage=None,
        usage_observed=None,
        settle=True,
    ) -> None:
        self.retry_updates.append({
            "attempt_id": attempt_id,
            "final_round_id": final_round_id,
            "connect_ms": connect_ms,
            "first_byte_ms": first_byte_ms,
            "idle_ms": idle_ms,
            "attempt_elapsed_ms": attempt_elapsed_ms,
            "request_upload_ms": request_upload_ms,
            "response_headers_wait_ms": response_headers_wait_ms,
            "response_body_first_byte_wait_ms": response_body_first_byte_wait_ms,
            "total_ms": total_ms,
            "ended_at": ended_at,
            "outcome": outcome,
            "error_detail": error_detail,
            "proxy_name": proxy_name,
            "bytes_up": bytes_up,
            "bytes_down": bytes_down,
            "response_body": response_body,
            "usage": usage,
            "usage_observed": usage_observed,
            "settle": settle,
        })

    def finish_error(
        self,
        request_id,
        error_message,
        retry_count=0,
        final_channel_key=None,
        final_channel_type=None,
        final_model=None,
        connect_ms=None,
        first_token_ms=None,
        idle_ms=None,
        total_ms=None,
        final_round_id=None,
        request_elapsed_ms=None,
        http_status=None,
        response_body=None,
        affinity_hit=0,
        upstream_protocol=None,
        upstream_transport=None,
        proxy_name=None,
        proxy_bytes_up=None,
        proxy_bytes_down=None,
        request_upload_ms=None,
        response_headers_wait_ms=None,
        response_body_first_byte_wait_ms=None,
        status="error",
        usage=None,
        usage_observed=None,
    ) -> None:
        self.request_terminals.append({
            "request_id": request_id,
            "error_message": error_message,
            "retry_count": retry_count,
            "final_channel_key": final_channel_key,
            "final_channel_type": final_channel_type,
            "final_model": final_model,
            "connect_ms": connect_ms,
            "first_token_ms": first_token_ms,
            "idle_ms": idle_ms,
            "total_ms": total_ms,
            "final_round_id": final_round_id,
            "request_elapsed_ms": request_elapsed_ms,
            "http_status": http_status,
            "response_body": response_body,
            "affinity_hit": affinity_hit,
            "upstream_protocol": upstream_protocol,
            "upstream_transport": upstream_transport,
            "proxy_name": proxy_name,
            "proxy_bytes_up": proxy_bytes_up,
            "proxy_bytes_down": proxy_bytes_down,
            "request_upload_ms": request_upload_ms,
            "response_headers_wait_ms": response_headers_wait_ms,
            "response_body_first_byte_wait_ms": response_body_first_byte_wait_ms,
            "status": status,
            "usage": usage,
            "usage_observed": usage_observed,
        })

    def insert_pending(
        self,
        request_id,
        client_ip,
        api_key_name,
        requested_model,
        is_stream,
        msg_count,
        tool_count,
        request_headers,
        request_body,
        fingerprint=None,
        ingress_protocol="anthropic",
        reasoning_effort=None,
        fast_mode=None,
        created_at=None,
    ) -> None:
        self.pending_records.append({
            "request_id": request_id,
            "client_ip": client_ip,
            "api_key_name": api_key_name,
            "requested_model": requested_model,
            "is_stream": is_stream,
            "msg_count": msg_count,
            "tool_count": tool_count,
            "request_headers": request_headers,
            "request_body": request_body,
            "fingerprint": fingerprint,
            "ingress_protocol": ingress_protocol,
            "reasoning_effort": reasoning_effort,
            "fast_mode": fast_mode,
            "created_at": created_at,
        })


class _CountingClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _Stats:
    def __init__(self) -> None:
        self.total_attempts = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_attempt_ts = 0.0
        self.last_success_ts = 0.0
        self.last_latency_ms = 0
        self.last_error = ""


class _Connector:
    type = "socks5"

    def __init__(self, client: _CountingClient) -> None:
        self.client = client
        self.stats = _Stats()

    def create_httpx_client(self, *, timeout, byte_counter, timing):
        self.timeout = timeout
        self.byte_counter = byte_counter
        self.timing = timing
        return self.client


class _CountingContext:
    def __init__(self) -> None:
        self.enter_calls = 0
        self.exit_calls = 0
        self.response = SimpleNamespace(status_code=200, headers={})

    async def __aenter__(self):
        self.enter_calls += 1
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_calls += 1


class _Tracker:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }
    usage_observed = False
    saw_stream_error = False
    saw_stream_end = False
    stream_error_message = None

    def get_full_response(self) -> str:
        return ""


class _HttpResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}


class _NeverIterated:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise AssertionError("pending stream must be explicitly aborted, not iterated")


class _FakeLease:
    def __init__(self) -> None:
        self.release_calls = 0

    async def release(self) -> None:
        self.release_calls += 1


class _FakeDownstreamWebSocket:
    def __init__(self) -> None:
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.application_state = WebSocketState.CONNECTING
        self.accept_calls = 0
        self.receive_calls = 0
        self.close_calls = 0

    async def accept(self) -> None:
        self.accept_calls += 1
        self.application_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        self.receive_calls += 1
        assert self.receive_calls == 1
        return {
            "type": "websocket.receive",
            "text": '{"type":"response.create","model":"m","input":[]}',
        }

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.close_calls += 1
        self.application_state = WebSocketState.DISCONNECTED


class _FakeUpstreamWebSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _patch_http_logs(monkeypatch, logs: _StrictLogFakes) -> tuple[Any, Any]:
    record_proxy = logs.record_proxy_attempt
    update_proxy = logs.update_proxy_attempt
    monkeypatch.setattr(http_runtime.log_db, "record_proxy_attempt", record_proxy)
    monkeypatch.setattr(http_runtime.log_db, "update_proxy_attempt", update_proxy)
    monkeypatch.setattr(http_runtime.upstream, "acquire_client", lambda: SimpleNamespace(client=object(), release=lambda: None))
    return record_proxy, update_proxy


def _http_open_kwargs() -> dict[str, Any]:
    return {
        "channel": SimpleNamespace(),
        "resolved_model": "m",
        "upstream_req": SimpleNamespace(
            method="POST",
            url="https://unit.invalid",
            headers={},
            body=b"{}",
            dispatch_metadata=None,
        ),
        "connect_timeout": 1,
        "first_byte_timeout": 1,
        "idle_timeout": 1,
        "total_timeout": 5,
        "response_mode": "stream",
        "request_id": "request-1",
        "retry_attempt_id": "retry-1",
    }



def _schedule_result(ch, *, queued: bool):
    return SimpleNamespace(
        candidates=[] if queued else [(ch, "m")],
        saturated=[(ch, "m")] if queued else [],
        affinity_hit=False,
        fp_query=None,
        client_key=None,
        bound_channel_key=None,
        encrypted_content_count=0,
    )


# Export underscore-prefixed strict fakes/helpers to the split gate modules.
__all__ = [name for name in globals() if not name.startswith("__")]
