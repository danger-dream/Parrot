"""M4 故障转移集成测试。

使用 httpx.MockTransport 模拟上游行为，不触网。覆盖：
  - 非流式成功 / HTTP 500 → 切换 / 渠道语义 HTTP 400 → 切换并 cooldown
  - 流式成功完整转发
  - 上游首个 SSE event 是 error → 切换
  - 首包文本黑名单命中 → 切换
  - 全部候选失败 → 503
  - 亲和命中把绑定渠道顶首位
  - 连续 5xx 失败进入 cooldown，下次调度被排除

运行：./venv/bin/python -m src.tests.test_m4_failover
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import itertools
import json
import os
import sys
import time

import httpx
import pytest


class ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk




def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, config, cooldown, failover, fingerprint,
        log_db, scheduler, scorer, state_db, upstream,
    )
    from src.channel import registry, api_channel
    return {
        "affinity": affinity, "config": config, "cooldown": cooldown,
        "failover": failover, "fingerprint": fingerprint,
        "log_db": log_db, "scheduler": scheduler, "scorer": scorer,
        "state_db": state_db, "upstream": upstream,
        "registry": registry, "api_channel": api_channel,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["state_db"].client_affinity_delete()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"]._client_initialized = False
    m["affinity"].init()
    m["affinity"].client_init()
    m["cooldown"].init()
    m["scorer"].init()


# ─── Mock Transport 路由 ─────────────────────────────────────────

class MockRouter:
    """按 channel baseUrl 分发模拟响应。"""

    def __init__(self):
        self.handlers: dict[str, callable] = {}

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for base, handler in self.handlers.items():
            if url_str.startswith(base):
                return handler(request)
        return httpx.Response(404, text="no mock")


# ─── 常用响应工厂 ─────────────────────────────────────────────────

def json_ok_response():
    body = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "glm-5",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3},
    }
    return httpx.Response(200, json=body, headers={"content-type": "application/json"})


def http_500():
    return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "oops"}})


def http_402(secret: str = "sk-never-leak"):
    return httpx.Response(402, json={
        "error": {
            "type": "billing_error",
            "code": "insufficient_balance",
            "message": f"balance exhausted credential={secret}",
        },
    })


def http_channel_400():
    return httpx.Response(400, json={
        "type": "error",
        "error": {
            "type": "api_error",
            "code": "upstream_rejected",
            "message": "channel request was rejected",
        },
    })


def openai_context_length_error():
    return httpx.Response(200, json={
        "error": {
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
            "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
            "param": "input",
        }
    })


def openai_invalid_encrypted_content_error():
    return httpx.Response(400, json={
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_encrypted_content",
            "message": (
                "The encrypted content gAAA... could not be verified. "
                "Reason: Encrypted content could not be decrypted or parsed."
            ),
            "param": "input",
        }
    })


def sse_ok():
    payload = (
        b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":2}}}\n\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_first_event_error():
    payload = b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_first_event_context_length_error():
    payload = (
        b'data: {"type":"error","error":{"type":"invalid_request_error",'
        b'"message":"prompt is too long: 200001 tokens > 200000 maximum"}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_midstream_error():
    payload = (
        b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant","usage":{"input_tokens":10}}}\n\n'
        b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy later"}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_truncated_after_visible_output():
    payload = (
        b'data: {"type":"message_start","message":{"id":"msg_truncated","role":"assistant","usage":{"input_tokens":10}}}\n\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def responses_sse_midstream_error():
    payload = (
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_1","status":"in_progress"}}\n\n'
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n'
        b'event: error\n'
        b'data: {"type":"error","error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Our servers are currently overloaded. Please try again later.","param":null},"sequence_number":2}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})



def responses_sse_chunked_metadata_then_error():
    chunks = [
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_1","status":"in_progress"}}\n\n',
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n',
        b'event: error\n'
        b'data: {"type":"error","error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Our servers are currently overloaded. Please try again later.","param":null},"sequence_number":2}\n\n',
    ]
    return httpx.Response(200, stream=ChunkedByteStream(chunks),
                          headers={"content-type": "text/event-stream"})


def responses_sse_chunked_metadata_then_close():
    """Emit only pre-commit Responses metadata, then EOF without a terminal event."""
    chunks = [
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_eof","status":"in_progress"}}\n\n',
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_eof","status":"in_progress"}}\n\n',
    ]
    return httpx.Response(200, stream=ChunkedByteStream(chunks),
                          headers={"content-type": "text/event-stream"})


def responses_sse_chunked_metadata_then_context_length_error():
    chunks = [
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_1","status":"in_progress"}}\n\n',
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n',
        b'event: error\n'
        b'data: {"type":"error","error":{"type":"invalid_request_error","code":"context_length_exceeded","message":"Your input exceeds the context window of this model. Please adjust your input and try again.","param":"input"},"sequence_number":2}\n\n',
    ]
    return httpx.Response(200, stream=ChunkedByteStream(chunks),
                          headers={"content-type": "text/event-stream"})


def responses_sse_ok():
    payload = (
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_ok","status":"in_progress"}}\n\n'
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_ok","status":"in_progress"}}\n\n'
        b'event: response.output_item.added\n'
        b'data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n'
        b'event: response.output_text.delta\n'
        b'data: {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_1","output_index":0,"content_index":0,"delta":"ok"}\n\n'
        b'event: response.output_item.done\n'
        b'data: {"type":"response.output_item.done","sequence_number":4,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"ok","annotations":[]}]}}\n\n'
        b'event: response.completed\n'
        b'data: {"type":"response.completed","sequence_number":5,"response":{"id":"resp_ok","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","content":[{"type":"output_text","text":"ok","annotations":[]}]}],"usage":{"input_tokens":10,"output_tokens":1,"total_tokens":11}}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def responses_sse_item_then_error():
    payload = (
        b'event: response.created\n'
        b'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_1","status":"in_progress"}}\n\n'
        b'event: response.in_progress\n'
        b'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n'
        b'event: response.output_item.added\n'
        b'data: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n'
        b'event: error\n'
        b'data: {"type":"error","error":{"type":"service_unavailable_error","code":"server_is_overloaded","message":"Our servers are currently overloaded. Please try again later.","param":null},"sequence_number":3}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def chat_sse_ok():
    payload = (
        b'data: {"id":"chatcmpl_ok","object":"chat.completion.chunk","created":1,"model":"gpt-5.5","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl_ok","object":"chat.completion.chunk","created":1,"model":"gpt-5.5","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl_ok","object":"chat.completion.chunk","created":1,"model":"gpt-5.5","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}\n\n'
        b'data: [DONE]\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_with_blacklist():
    payload = (
        b'data: {"type":"message_start","message":{"id":"x","role":"assistant",'
        b'"content":[{"type":"text","text":"content_policy_violation detected"}],'
        b'"usage":{"input_tokens":0}}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})




class _MockStreamContext:
    def __init__(self, resp: httpx.Response, trace=None):
        self.resp = resp
        self.trace = trace

    async def __aenter__(self):
        if self.trace is not None:
            await self.trace("http11.send_request_body.started", {})
            await self.trace("http11.send_request_body.complete", {})
        return self.resp

    async def __aexit__(self, exc_type, exc, tb):
        await self.resp.aclose()
        return False


class _FailingEnterCtx:
    async def __aenter__(self):
        raise httpx.ConnectError("proxy down")

    async def __aexit__(self, *args):
        return False


class _ProxyMockClient:
    def __init__(self, name: str, router: MockRouter, *, fail_enter: bool = False):
        self.name = name
        self.router = router
        self.fail_enter = fail_enter
        self.closed = False
        self.requests: list[httpx.Request] = []

    def stream(self, method, url, *, headers=None, content=None, timeout=None, extensions=None):
        # This fake owns request upload, so it emits the same authoritative
        # send-body trace boundary as HTTPcore instead of inventing wall timing.
        trace = (extensions or {}).get("trace")
        if self.fail_enter:
            return _FailingEnterCtx()
        req = httpx.Request(method, url, headers=headers, content=content)
        self.requests.append(req)
        resp = self.router.handle(req)
        return _MockStreamContext(resp, trace=trace)

    async def aclose(self):
        self.closed = True


class _FakeProxyConnector:
    def __init__(self, name: str, router: MockRouter, *, fail_enter: bool = False):
        from src.proxy.connector import Connector
        # Avoid subclassing: failover only needs .type/.stats/create_httpx_client.
        from src.proxy.connector import ProxyStats
        self.name = name
        self.type = "socks5"
        self.stats = ProxyStats()
        self.router = router
        self.fail_enter = fail_enter
        self.clients: list[_ProxyMockClient] = []

    def create_httpx_client(self, **kwargs):
        c = _ProxyMockClient(self.name, self.router, fail_enter=self.fail_enter)
        self.clients.append(c)
        return c


def _patch_proxy_route(m, connectors: dict[str, object], chain: list[str]):
    """Patch proxy manager module view for one test."""
    from src.proxy import manager as pm
    old = {
        "init": pm.init,
        "is_configured": pm.is_configured,
        "resolve_proxy_chain": pm.resolve_proxy_chain,
        "get_connector": pm.get_connector,
    }
    pm.init = lambda: None
    pm.is_configured = lambda: True
    pm.resolve_proxy_chain = lambda **kwargs: list(chain)
    pm.get_connector = lambda name: connectors.get(name)
    return pm, old


def _restore_proxy_route(pm, old):
    pm.init = old["init"]
    pm.is_configured = old["is_configured"]
    pm.resolve_proxy_chain = old["resolve_proxy_chain"]
    pm.get_connector = old["get_connector"]

# ─── 用例 ────────────────────────────────────────────────────────

def _make_channel(m, name, base_url, real="glm-5", alias="glm-5", cc_mimicry=False):
    return m["api_channel"].ApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "cc_mimicry": cc_mimicry, "enabled": True,
    })


def _make_openai_channel(name, base_url, protocol="openai-responses",
                         real="gpt-5.5", alias="gpt-5.5"):
    from src.openai.channel.api_channel import OpenAIApiChannel
    return OpenAIApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "protocol": protocol,
        "enabled": True,
    })


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


_REQUEST_SEQ = itertools.count()


async def _call_proxy(m, router: MockRouter, body: dict, api_key="k1", client_ip="1.1.1.1",
                      ingress_protocol="anthropic", fp_query=None):
    """模拟 server.py /v1/messages 或 OpenAI handler 的核心调用链。"""
    # conftest makes MockTransport emit HTTPcore's authoritative send-body
    # boundary; proxy-specific fakes below do the same from their context owner.
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    request_id = f"req-{int(time.time()*1000)}-{next(_REQUEST_SEQ)}"
    start = time.time()

    msg_items = body.get("messages") if ingress_protocol == "anthropic" else body.get("input")
    await asyncio.to_thread(
        m["log_db"].insert_pending,
        request_id, client_ip, api_key, body.get("model"), bool(body.get("stream", True)),
        len(msg_items or []), len(body.get("tools") or []),
        {}, body, ingress_protocol=ingress_protocol,
    )

    sched_result = m["scheduler"].schedule(
        body, api_key_name=api_key, client_ip=client_ip,
        ingress_protocol=ingress_protocol, fp_query=fp_query,
    )
    if not sched_result.candidates:
        from src import errors as er
        resp = er.json_error_response(503, er.ErrType.API, "no candidates")
        await mock_client.aclose()
        return resp, request_id, sched_result

    resp = await m["failover"].run_failover(
        sched_result, body, request_id, api_key, client_ip,
        is_stream=bool(body.get("stream", True)), start_time=start,
        ingress_protocol=ingress_protocol,
    )
    # 非流式 resp 可以立刻关 client；流式需要等流消费完
    if not isinstance(resp, httpx.AsyncClient):  # 占位判断
        pass
    return resp, request_id, sched_result, mock_client


async def _consume_streaming_to_string(resp) -> str:
    chunks = []
    async for c in resp.body_iterator:
        if isinstance(c, str):
            chunks.append(c.encode())
        else:
            chunks.append(c)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _close_background(resp, mock_client):
    """StreamingResponse 的 background 任务在返回后由 FastAPI 调度；
    单测里我们手工关。"""
    try:
        await mock_client.aclose()
    except Exception:
        pass


# ─── 具体测试 ────────────────────────────────────────────────────

async def test_non_stream_success(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    if resp.status_code != 200:
        body_bytes = resp.body if hasattr(resp, "body") else b""
        print(f"    body={body_bytes[:500]!r}")
    assert resp.status_code == 200, f"status={resp.status_code}"
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chA"
    assert log["log"]["input_tokens"] == 10
    assert log["log"]["output_tokens"] == 5
    assert log["log"]["cache_read_tokens"] == 3
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["outcome"] == "success"
    # scorer 记录了一次 success
    stats = m["scorer"].get_stats("api:chA", "glm-5")
    assert stats["success_count"] == 1
    print("  [PASS] non_stream_success")


async def test_non_stream_500_then_ok(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chB"
    assert len(log["retry_chain"]) == 2
    assert log["retry_chain"][0]["outcome"] == "http_error"
    assert log["retry_chain"][1]["outcome"] == "success"
    # chA 进入 cooldown
    assert m["cooldown"].is_blocked("api:chA", "glm-5")
    # chB success
    assert m["scorer"].get_stats("api:chB", "glm-5")["success_count"] == 1
    print("  [PASS] non_stream 500 → switch → success; chA cooldown")


async def test_all_fail_503(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: http_500())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    # 按设计 doc §10.1：全候选耗尽 → 503 api_error
    # （只有最后一次失败是 timeout/transport 时才回 504/502）
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"
    await mc.aclose()
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error"
    assert len(log["retry_chain"]) == 2
    print("  [PASS] all_fail → 503")


async def test_402_switches_candidate_and_all_402_preserves_terminal_status(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_402())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])
    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}

    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    await mc.aclose()
    assert m["cooldown"].is_blocked("api:chA", "glm-5")
    assert not m["cooldown"].is_blocked("api:chB", "glm-5")

    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_402("first-secret"))
    router.register("https://chb", lambda r: http_402("second-secret"))
    _install_channels(m, [chA, chB])
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 402
    text = resp.body.decode()
    assert "balance_exhausted" in text
    assert "first-secret" not in text and "second-secret" not in text
    await mc.aclose()


async def test_channel_semantic_400_still_switches_and_cools_down(m):
    """A structured channel-side 400 must remain on normal failover/health paths."""
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_channel_400())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    assert [item["outcome"] for item in log["retry_chain"]] == ["http_error", "success"]
    assert m["cooldown"].is_blocked("api:chA", "glm-5")
    assert m["scorer"].get_stats("api:chA", "glm-5")["total_requests"] == 1
    assert m["scorer"].get_stats("api:chB", "glm-5")["success_count"] == 1
    print("  [PASS] channel 400 → switch/cooldown → next success")


async def test_context_length_error_short_circuits_failover(m):
    _setup(m)
    router = MockRouter()
    chb_calls = {"count": 0}

    def chb_handler(req):
        chb_calls["count"] += 1
        return responses_sse_ok()

    router.register("https://cha", lambda r: openai_context_length_error())
    router.register("https://chb", chb_handler)
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-responses")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5.5", "stream": False, "store": False,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="responses")
    assert resp.status_code == 400, f"expected 400, got {resp.status_code} body={getattr(resp, 'body', b'')[:500]!r}"
    await mc.aclose()

    assert chb_calls["count"] == 0
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error", log["log"]
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["outcome"] == "request_invalid"
    assert not m["cooldown"].is_blocked("api:chA", "gpt-5.5")
    print("  [PASS] context_length_exceeded → request_invalid 400 without failover")


async def test_invalid_encrypted_content_retries_same_channel_without_ec(m):
    _setup(m)
    router = MockRouter()
    calls: list[dict] = []
    chb_calls = {"count": 0}

    def cha_handler(req):
        payload = json.loads(req.content)
        calls.append(payload)
        has_ec = any(
            isinstance(item, dict)
            and item.get("type") == "reasoning"
            and item.get("encrypted_content")
            for item in (payload.get("input") or [])
        )
        if has_ec:
            return openai_invalid_encrypted_content_error()
        return httpx.Response(200, json={
            "id": "resp_retry_ok",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "message",
                "id": "msg_retry_ok",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "recovered", "annotations": []}],
            }],
            "output_text": "recovered",
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        })

    def chb_handler(req):
        chb_calls["count"] += 1
        return responses_sse_ok()

    router.register("https://cha", cha_handler)
    router.register("https://chb", chb_handler)
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-responses")
    _install_channels(m, [chA, chB])

    body = {
        "model": "gpt-5.5",
        "stream": False,
        "input": [
            {"type": "reasoning", "id": "rs_bad", "summary": [], "encrypted_content": "gAAAAbad"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
    }
    # EC replay is accepted only with an exact owner binding.
    m["affinity"].upsert("ec-owner-fp", chA.key, "gpt-5.5")
    resp, rid, sr, mc = await _call_proxy(
        m, router, body, ingress_protocol="responses", fp_query="ec-owner-fp",
    )
    assert resp.status_code == 200, getattr(resp, "body", b"")[:500]
    await mc.aclose()

    assert chb_calls["count"] == 0
    assert len(calls) == 2
    assert any(item.get("type") == "reasoning" for item in calls[0]["input"])
    assert calls[1]["input"][0] == {
        "type": "reasoning", "id": "rs_bad", "summary": [],
    }
    assert "encrypted_content" not in calls[1]["input"][0]
    assert calls[1]["input"][1] == {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "continue"}],
    }
    out = json.loads(resp.body)
    assert out["output_text"] == "recovered"
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["final_channel_key"] == "api:chA"
    assert [item["outcome"] for item in log["retry_chain"]] == ["request_invalid", "success"]
    assert not m["cooldown"].is_blocked("api:chA", "gpt-5.5")
    assert not m["cooldown"].is_blocked("api:chB", "gpt-5.5")
    print("  [PASS] invalid_encrypted_content → same-channel retry without EC")


async def test_stream_success_full_forward(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200

    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "message_start" in body_text
    assert "content_block_delta" in body_text
    assert "message_stop" in body_text

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["input_tokens"] == 10
    assert log["log"]["output_tokens"] == 7
    assert log["log"]["cache_read_tokens"] == 2
    print("  [PASS] stream_success_full_forward")


async def test_http_stream_affinity_rebinds_only_after_complete_finalizer(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])
    stable_fp = "stable-http-stream-complete"
    m["affinity"].upsert(stable_fp, chA.key, "glm-5")

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, fp_query=stable_fp)
    assert resp.status_code == 200
    # Returning a StreamingResponse is not a successful terminal event yet.
    assert m["affinity"].get(stable_fp)["channel_key"] == chA.key

    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "message_stop" in body_text
    assert m["affinity"].get(stable_fp)["channel_key"] == chB.key
    assert m["log_db"].log_detail(rid)["log"]["status"] == "success"


async def test_stream_first_event_error_switches(m, monkeypatch):
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    router.register("https://cha", lambda r: sse_first_event_error())
    router.register("https://chb", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)
    # 下游看到的应是 chB 的完整流
    assert "message_stop" in body_text

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chB"
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["upstream_error_json", "upstream_error_json", "upstream_error_json", "success"], outcomes
    print("  [PASS] stream overload retries same channel twice → switch → chB ok")


async def test_stream_context_length_first_event_short_circuits_failover(m):
    _setup(m)
    router = MockRouter()
    chb_calls = {"count": 0}

    def chb_handler(req):
        chb_calls["count"] += 1
        return sse_ok()

    router.register("https://cha", lambda r: sse_first_event_context_length_error())
    router.register("https://chb", chb_handler)
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 400
    await mc.aclose()

    assert chb_calls["count"] == 0
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error", log["log"]
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["outcome"] == "request_invalid"
    assert "prompt is too long" in log["log"]["error_message"]
    assert not m["cooldown"].is_blocked("api:chA", "glm-5")
    print("  [PASS] stream context overflow first event → request_invalid 400 without failover")


async def test_stream_midstream_error_logs_upstream_error(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: sse_midstream_error())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "busy later" in body_text
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error", log["log"]
    assert "busy later" in log["log"]["error_message"]
    assert log["log"]["error_message"] != "client disconnected"
    assert log["retry_chain"][0]["outcome"] == "stream_upstream_error"
    assert log["retry_chain"][0]["total_ms"] is not None
    print("  [PASS] stream midstream error → DB records upstream error, not client disconnected")


@pytest.mark.parametrize(
    "stream_response",
    [sse_midstream_error, sse_truncated_after_visible_output],
    ids=["terminal-error", "truncated"],
)
async def test_http_started_stream_error_or_truncation_does_not_rebind(m, stream_response):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: stream_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])
    stable_fp = f"stable-http-stream-{stream_response.__name__}"
    m["affinity"].upsert(stable_fp, chA.key, "glm-5")

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, fp_query=stable_fp)
    assert resp.status_code == 200
    assert m["affinity"].get(stable_fp)["channel_key"] == chA.key

    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert m["affinity"].get(stable_fp)["channel_key"] == chA.key
    assert m["log_db"].log_detail(rid)["log"]["status"] == "error"
    assert "error" in body_text


async def test_responses_error_before_visible_chunk_switches(m, monkeypatch):
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    router.register("https://cha", lambda r: responses_sse_midstream_error())
    router.register("https://chb", lambda r: responses_sse_ok())
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-responses")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5.5", "stream": True, "store": False,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="responses")
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "server_is_overloaded" not in body_text
    assert "event: response.created" in body_text
    assert "event: response.output_text.delta" in body_text
    assert "event: response.completed" in body_text
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["final_channel_key"] == "api:chB"
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["upstream_error_json", "upstream_error_json", "upstream_error_json", "success"], outcomes
    print("  [PASS] responses overload retries same channel twice → switch → chB ok")




async def test_responses_chunked_metadata_error_before_visible_chunk_switches(m, monkeypatch):
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    router.register("https://cha", lambda r: responses_sse_chunked_metadata_then_error())
    router.register("https://chb", lambda r: responses_sse_ok())
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-responses")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5.5", "stream": True, "store": False,
            "input": [{"role": "user", "content": [{"type":"input_text", "text":"hi"}]}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="responses")
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "server_is_overloaded" not in body_text
    assert "event: response.created" in body_text
    assert "event: response.output_text.delta" in body_text
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["final_channel_key"] == "api:chB"
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["upstream_error_json", "upstream_error_json", "upstream_error_json", "success"], outcomes
    print("  [PASS] responses chunked overload retries twice → switch → chB ok")


async def test_responses_precommit_eof_persists_received_sse(m):
    """An EOF before Chat-visible output must still retain received Responses SSE."""
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: responses_sse_chunked_metadata_then_close())
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    _install_channels(m, [chA])

    body = {"model": "gpt-5.5", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="chat")
    assert resp.status_code == 503
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    expected = (
        'event: response.created\n'
        'data: {"type":"response.created","sequence_number":0,"response":{"id":"resp_eof","status":"in_progress"}}\n\n'
        'event: response.in_progress\n'
        'data: {"type":"response.in_progress","sequence_number":1,"response":{"id":"resp_eof","status":"in_progress"}}\n\n'
    )
    assert log["log"]["status"] == "error", log["log"]
    assert log["log"]["error_message"] == "upstream closed stream before first downstream chunk"
    assert log["retry_chain"][0]["outcome"] == "closed_before_first_byte"
    assert log["detail"]["response_body"] == expected
    assert m["cooldown"].is_blocked("api:chA", "gpt-5.5")
    stats = m["scorer"].get_stats("api:chA", "gpt-5.5")
    assert stats["total_requests"] == 1
    assert stats["success_count"] == 0
    print("  [PASS] pre-commit EOF retains received SSE in error log")


async def test_responses_context_length_before_visible_chunk_short_circuits_failover(m):
    _setup(m)
    router = MockRouter()
    chb_calls = {"count": 0}

    def chb_handler(req):
        chb_calls["count"] += 1
        return responses_sse_ok()

    router.register("https://cha", lambda r: responses_sse_chunked_metadata_then_context_length_error())
    router.register("https://chb", chb_handler)
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-responses")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5.5", "stream": True, "store": False,
            "input": [{"role": "user", "content": [{"type":"input_text", "text":"hi"}]}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="responses")
    assert resp.status_code == 400
    await mc.aclose()

    assert chb_calls["count"] == 0
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error", log["log"]
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["outcome"] == "request_invalid"
    assert "context_length_exceeded" in log["log"]["error_message"]
    assert not m["cooldown"].is_blocked("api:chA", "gpt-5.5")
    assert m["scorer"].get_stats("api:chA", "gpt-5.5") is None
    print("  [PASS] responses metadata→context overflow before visible chunk → request_invalid 400 without failover")


async def test_responses_to_chat_error_after_item_added_before_chat_bytes_switches(m, monkeypatch):
    _setup(m)
    monkeypatch.setattr(m["failover"], "_overload_retry_delay_seconds", lambda _ordinal: 0.0)
    router = MockRouter()
    router.register("https://cha", lambda r: responses_sse_item_then_error())
    router.register("https://chb", lambda r: chat_sse_ok())
    chA = _make_openai_channel("chA", "https://cha", protocol="openai-responses")
    chB = _make_openai_channel("chB", "https://chb", protocol="openai-chat")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5.5", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body, ingress_protocol="chat")
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "server_is_overloaded" not in body_text
    assert '"content": "ok"' in body_text or '"content":"ok"' in body_text
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["final_channel_key"] == "api:chB"
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["upstream_error_json", "upstream_error_json", "upstream_error_json", "success"], outcomes
    print("  [PASS] responses→chat pre-visible overload retries twice → switch → chB ok")


async def test_stream_blacklist_switch(m):
    _setup(m)
    # 在 config 里配置黑名单
    m["config"].update(lambda c: c.setdefault("contentBlacklist", {}).__setitem__("default", ["content_policy_violation"]))

    router = MockRouter()
    router.register("https://cha", lambda r: sse_with_blacklist())
    router.register("https://chb", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    log = m["log_db"].log_detail(rid)
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["blacklist_hit", "success"], outcomes
    assert "message_stop" in body_text

    # 清黑名单
    m["config"].update(lambda c: c.setdefault("contentBlacklist", {}).__setitem__("default", []))
    print("  [PASS] stream blacklist_hit → switch")


async def test_affinity_pins_channel(m):
    _setup(m)
    # 禁用探索率，让评分排序确定
    m["config"].update(lambda c: c.setdefault("scoring", {}).__setitem__("explorationRate", 0.0))

    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    # 多轮对话 → fingerprint 可算
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    body = {"model": "glm-5", "stream": False, "max_tokens": 100, "messages": msgs}

    # 第一次：两个渠道都可用，随便选一个
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    first_choice = m["log_db"].log_detail(rid)["log"]["final_channel_key"]
    await mc.aclose()

    # 由于第一次写入亲和是基于 messages + assistant_response
    # 下次 messages = msgs + [assistant_response] + [new_user]
    a1_obj = json.loads(resp.body)
    next_msgs = msgs + [{"role": "assistant", "content": a1_obj["content"]},
                        {"role": "user", "content": "q3"}]
    body2 = {"model": "glm-5", "stream": False, "max_tokens": 100, "messages": next_msgs}

    resp2, rid2, sr2, mc2 = await _call_proxy(m, router, body2)
    assert resp2.status_code == 200
    await mc2.aclose()
    # 亲和命中
    assert sr2.affinity_hit, "expected affinity hit on 2nd request"
    second_choice = m["log_db"].log_detail(rid2)["log"]["final_channel_key"]
    assert second_choice == first_choice, f"expected same channel, got {first_choice} vs {second_choice}"

    # 恢复
    m["config"].update(lambda c: c.setdefault("scoring", {}).__setitem__("explorationRate", 0.2))
    print(f"  [PASS] affinity pinned to {first_choice}")


async def test_cooldown_excludes_from_next(m):
    _setup(m)

    router = MockRouter()
    # 让 chA 前 6 次失败进入永久 cooldown，然后 chB 始终成功
    call_count = {"a": 0}

    def chA_handler(req):
        call_count["a"] += 1
        return http_500()

    router.register("https://cha", chA_handler)
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}

    # 第一次：chA fail → chB ok (chA 进入 cooldown 1 分钟)
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    await mc.aclose()
    assert resp.status_code == 200
    assert m["cooldown"].is_blocked("api:chA", "glm-5")

    # 第二次：chA 已被 cooldown 排除，仅 chB 参与，立即成功
    a_called_before = call_count["a"]
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    await mc.aclose()
    assert resp.status_code == 200
    keys = [c[0].key for c in sr.candidates]
    assert "api:chA" not in keys, f"chA should be excluded, got {keys}"
    assert call_count["a"] == a_called_before, "chA should not be called"
    print("  [PASS] cooldown excludes chA from next schedule")


async def test_proxy_group_pre_header_connect_error_switches_proxy_same_channel(m):
    """Proxy group failover: connect error before response headers should try next proxy in same channel."""
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    p1 = _FakeProxyConnector("p1", router, fail_enter=True)
    p2 = _FakeProxyConnector("p2", router, fail_enter=False)
    pm, old = _patch_proxy_route(m, {"p1": p1, "p2": p2}, ["p1", "p2"])
    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    try:
        resp, rid, sr, mc = await _call_proxy(m, router, body)
    finally:
        _restore_proxy_route(pm, old)
    assert resp.status_code == 200
    await mc.aclose()
    assert len(p1.clients) == 1
    assert len(p2.clients) == 1
    assert p1.clients[0].closed is True
    assert p2.clients[0].closed is True
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["proxy_name"] == "p2"
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["proxy_name"] == "p2"
    assert p1.stats.total_failures == 1
    assert p2.stats.total_successes == 1


async def test_proxy_group_http_error_does_not_switch_proxy(m):
    """HTTP response headers lock the proxy attempt; 5xx must not try next proxy in group."""
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    p1 = _FakeProxyConnector("p1", router, fail_enter=False)
    p2 = _FakeProxyConnector("p2", router, fail_enter=False)
    pm, old = _patch_proxy_route(m, {"p1": p1, "p2": p2}, ["p1", "p2"])
    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    try:
        resp, rid, sr, mc = await _call_proxy(m, router, body)
    finally:
        _restore_proxy_route(pm, old)
    assert resp.status_code == 503
    await mc.aclose()
    assert len(p1.clients) == 1
    assert len(p2.clients) == 0
    log = m["log_db"].log_detail(rid)
    assert log["retry_chain"][0]["outcome"] == "http_error"
    assert log["retry_chain"][0]["proxy_name"] == "p1"


async def amain():
    m = _import_modules()

    # 备份 config
    orig = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_non_stream_success,
        test_non_stream_500_then_ok,
        test_all_fail_503,
        test_channel_semantic_400_still_switches_and_cools_down,
        test_context_length_error_short_circuits_failover,
        test_stream_success_full_forward,
        test_stream_first_event_error_switches,
        test_stream_context_length_first_event_short_circuits_failover,
        test_stream_midstream_error_logs_upstream_error,
        test_responses_error_before_visible_chunk_switches,
        test_responses_chunked_metadata_error_before_visible_chunk_switches,
        test_responses_precommit_eof_persists_received_sse,
        test_responses_context_length_before_visible_chunk_short_circuits_failover,
        test_responses_to_chat_error_after_item_added_before_chat_bytes_switches,
        test_invalid_encrypted_content_retries_same_channel_without_ec,
        test_stream_blacklist_switch,
        test_affinity_pins_channel,
        test_cooldown_excludes_from_next,
        test_proxy_group_pre_header_connect_error_switches_proxy_same_channel,
        test_proxy_group_http_error_does_not_switch_proxy,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                await t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig)
        m["config"].update(_restore)
        # 清 state.db
        _setup(m)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
