"""MS-2 OpenAI 家族同协议透传集成测试。

覆盖：
  - chat → openai-chat 透传（非流式 + 流式）
  - responses → openai-responses 透传（非流式 + 流式）
  - 同家族跨变体暂不支持 → 503（MS-3 会实现翻译）
  - CapabilityGuard：chat n>1 → 400
  - allowedProtocols：不匹配 ingress → 403
  - /v1/models：按 Key allowedProtocols 家族过滤
  - 同家族多候选失败切换

运行：./venv/bin/python -m src.tests.test_openai_m2
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys
import time

import httpx


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, auth, config, cooldown, errors, failover, log_db,
        scheduler, scorer, state_db, upstream,
    )
    from src.channel import registry, api_channel
    from src.openai import handler as openai_handler
    from src.openai.channel.registration import register_factories
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "log_db": log_db,
        "scheduler": scheduler, "scorer": scorer, "state_db": state_db,
        "upstream": upstream, "registry": registry, "api_channel": api_channel,
        "openai_handler": openai_handler,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"].init()
    m["cooldown"].init()
    m["scorer"].init()


# ─── Mock Transport + Channel 工厂 ───────────────────────────────


class MockRouter:
    """按 URL 前缀分发模拟响应；捕获最后一次收到的 request 供断言。"""

    def __init__(self):
        self.handlers: dict[str, callable] = {}
        self.last_request: httpx.Request | None = None
        self.requests: list[httpx.Request] = []

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        self.requests.append(request)
        url_str = str(request.url)
        for base, handler in self.handlers.items():
            if url_str.startswith(base):
                return handler(request)
        return httpx.Response(404, text="no mock")


def _make_openai_channel(m, name, base_url, protocol="openai-chat",
                        real="gpt-5", alias="gpt-5"):
    from src.openai.channel.api_channel import OpenAIApiChannel
    return OpenAIApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "protocol": protocol,
        "models": [{"real": real, "alias": alias}],
        "enabled": True,
    })


def _make_anthropic_channel(m, name, base_url, real="claude-sonnet-4-5", alias="sonnet"):
    return m["api_channel"].ApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "cc_mimicry": False, "enabled": True,
    })


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


# ─── 响应工厂 ─────────────────────────────────────────────────────


def chat_json_ok():
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hello"},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                      "prompt_tokens_details": {"cached_tokens": 3}},
        },
        headers={"content-type": "application/json"},
    )


def responses_json_ok():
    return httpx.Response(
        200,
        json={
            "id": "resp_1", "object": "response", "status": "completed",
            "created_at": 1, "model": "gpt-5",
            "output": [{
                "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }],
            "output_text": "hello",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 3}},
        },
        headers={"content-type": "application/json"},
    )


def chat_sse_ok():
    payload = (
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hel"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"prompt_tokens_details":{"cached_tokens":3}}}\n\n'
        b'data: [DONE]\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def responses_sse_ok():
    payload = (
        b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n\n'
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n'
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"hello"}\n\n'
        b'event: response.output_text.done\ndata: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,"content_index":0,"text":"hello"}\n\n'
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"hello","annotations":[]}]}}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","content":[{"type":"output_text","text":"hello","annotations":[]}]}],"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"input_tokens_details":{"cached_tokens":3}}}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def chat_http_500():
    return httpx.Response(500, json={"error": {"message": "oops", "type": "server_error"}})


# ─── FakeRequest（FastAPI Request 最小模拟） ─────────────────────


class FakeHeaders:
    """模拟 Starlette Headers：dict(headers) 能拿到所有 (k, v) 对。"""

    def __init__(self, data: dict[str, str]):
        self._d = {k.lower(): v for k, v in data.items()}

    def get(self, k: str, default=None):
        return self._d.get(k.lower(), default)

    def items(self):
        return self._d.items()

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k: str):
        return self._d[k.lower()]

    def __iter__(self):
        return iter(self._d.keys())

    def __len__(self):
        return len(self._d)


class FakeClient:
    def __init__(self, host: str = "1.2.3.4"):
        self.host = host


class FakeRequest:
    """FastAPI Request 的最小子集：够 openai.handler.handle 用。"""

    def __init__(self, headers: dict, body_bytes: bytes, client_ip: str = "1.2.3.4"):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient(client_ip)

    async def body(self) -> bytes:
        return self._body


# ─── 测试辅助：安装 mock transport，调 handler，收响应 ───────────


async def _call_openai_handler(m, router, ingress_protocol, body, api_key="ccp-test",
                               extra_headers: dict | None = None):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    headers = {"Authorization": f"Bearer {api_key}"}
    if extra_headers:
        headers.update(extra_headers)
    req = FakeRequest(headers, json.dumps(body).encode("utf-8"))
    resp = await m["openai_handler"].handle(req, ingress_protocol=ingress_protocol)
    return resp, mock_client


async def _consume_streaming_to_string(resp) -> str:
    chunks = []
    async for c in resp.body_iterator:
        if isinstance(c, str):
            chunks.append(c.encode())
        else:
            chunks.append(c)
    return b"".join(chunks).decode("utf-8", errors="replace")


# ─── API Key 配置 ─────────────────────────────────────────────────


def _install_keys(m, keys: dict):
    def _mutate(cfg):
        cfg["apiKeys"] = keys
    m["config"].update(_mutate)


def _default_key(name="kopen", key="ccp-test", allowed_models=None, allowed_protos=None):
    entry = {"key": key, "allowedModels": list(allowed_models or [])}
    if allowed_protos is not None:
        entry["allowedProtocols"] = list(allowed_protos)
    return {name: entry}


# ─── 实际用例 ────────────────────────────────────────────────────


async def test_chat_non_stream_success(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://a.example", lambda r: chat_json_ok())
    chA = _make_openai_channel(m, "oaiA", "https://a.example")
    _install_channels(m, [chA])

    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50, "stream": False}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200, f"status={resp.status_code}, body={resp.body[:300]!r}"
    # 上游请求 URL + header
    up = router.last_request
    assert str(up.url) == "https://a.example/v1/chat/completions"
    assert up.headers["authorization"].startswith("Bearer ")
    # model 被替换为 resolved_model（alias==real 时相同）
    up_body = json.loads(up.content)
    assert up_body["model"] == "gpt-5"
    assert up_body["messages"] == [{"role": "user", "content": "hi"}]
    # 下游 body
    out = json.loads(resp.body)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "hello"
    await mc.aclose()
    print("  [PASS] chat non-stream passthrough (chat → openai-chat)")


async def test_responses_non_stream_success(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://r.example", lambda r: responses_json_ok())
    chR = _make_openai_channel(m, "oaiR", "https://r.example",
                               protocol="openai-responses", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chR])

    body = {"model": "gpt-5", "input": "hi", "max_output_tokens": 50, "stream": False}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 200, f"status={resp.status_code}, body={resp.body[:300]!r}"
    up = router.last_request
    assert str(up.url) == "https://r.example/v1/responses"
    up_body = json.loads(up.content)
    assert up_body["input"] == "hi"
    out = json.loads(resp.body)
    assert out["object"] == "response"
    assert out["output_text"] == "hello"
    await mc.aclose()
    print("  [PASS] responses non-stream passthrough (responses → openai-responses)")


async def test_chat_stream_success(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://a.example", lambda r: chat_sse_ok())
    chA = _make_openai_channel(m, "oaiA", "https://a.example")
    _install_channels(m, [chA])

    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50, "stream": True,
            "stream_options": {"include_usage": True}}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await mc.aclose()
    assert "data: {" in body_text
    assert "[DONE]" in body_text
    assert "chat.completion.chunk" in body_text
    print("  [PASS] chat stream passthrough forwards SSE verbatim")


async def test_responses_stream_success(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://r.example", lambda r: responses_sse_ok())
    chR = _make_openai_channel(m, "oaiR", "https://r.example",
                               protocol="openai-responses", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chR])

    body = {"model": "gpt-5", "input": "hi", "max_output_tokens": 50, "stream": True}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await mc.aclose()
    assert "event: response.created" in body_text
    assert "event: response.completed" in body_text
    assert "response.output_text.delta" in body_text
    print("  [PASS] responses stream passthrough forwards SSE verbatim")


async def test_chat_500_switches_next(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://a.example", lambda r: chat_http_500())
    router.register("https://b.example", lambda r: chat_json_ok())
    chA = _make_openai_channel(m, "oaiA", "https://a.example")
    chB = _make_openai_channel(m, "oaiB", "https://b.example")
    _install_channels(m, [chA, chB])

    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50, "stream": False}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200
    # oaiA 第一次就 500 → cooldown
    assert m["cooldown"].is_blocked("api:oaiA", "gpt-5")
    await mc.aclose()
    print("  [PASS] chat 500 → switch next candidate")


async def test_guard_chat_n_gt_1(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    router.register("https://a.example", lambda r: chat_json_ok())
    chA = _make_openai_channel(m, "oaiA", "https://a.example")
    _install_channels(m, [chA])

    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}], "n": 2}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 400
    out = json.loads(resp.body)
    assert out["error"]["type"] == "invalid_request_error"
    assert out["error"]["param"] == "n"
    # 上游未被调用
    assert router.last_request is None, "guard must reject before contacting upstream"
    await mc.aclose()
    print("  [PASS] guard rejects chat n>1 → 400")


async def test_allowed_protocols_mismatch_403(m):
    _setup(m)
    # Key 只允许 anthropic，然后走 chat 入口 → 403
    _install_keys(m, _default_key(allowed_protos=["anthropic"]))
    router = MockRouter()
    chA = _make_openai_channel(m, "oaiA", "https://a.example")
    _install_channels(m, [chA])

    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 403, f"status={resp.status_code}"
    out = json.loads(resp.body)
    assert out["error"]["type"] == "permission_error"
    await mc.aclose()
    print("  [PASS] allowedProtocols=anthropic blocks chat ingress → 403")


# 注：MS-2 时这里曾有 test_cross_variant_not_implemented_yet，验证 MS-2 阶段
# 跨变体请求会 503。MS-3 已实现跨变体翻译，这个用例移到 test_openai_m3.py 里
# 以真实上游 mock 验证端到端行为。


async def test_no_candidates_family_filtered(m):
    """Key 无限制，但 ingress=chat 且 registry 里只有 anthropic 渠道 → 无候选 → 404 (model never supported)."""
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    chA = _make_anthropic_channel(m, "anth1", "https://a.example",
                                   real="sonnet", alias="sonnet")
    _install_channels(m, [chA])

    body = {"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    # model 在 anthropic 渠道中存在 → 不是 NOT_FOUND；是 SERVER/503
    assert resp.status_code == 503
    await mc.aclose()
    print("  [PASS] family filter: anthropic-only registry returns 503 for chat ingress")


def test_list_models_family_filter(m):
    """/v1/models 按 allowedProtocols 过滤家族。同步测试直接调用 registry 接口。"""
    _setup(m)
    chA = _make_anthropic_channel(m, "anth1", "https://a.example",
                                   real="sonnet", alias="sonnet")
    chO = _make_openai_channel(m, "oai1", "https://o.example",
                                protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chA, chO])

    all_m = m["registry"].available_models_for_families(None)
    anth_only = m["registry"].available_models_for_families({"anthropic"})
    openai_only = m["registry"].available_models_for_families({"openai"})

    assert set(all_m) == {"sonnet", "gpt-5"}
    assert anth_only == ["sonnet"]
    assert openai_only == ["gpt-5"]

    # auth.get_allowed_protocols
    _install_keys(m, {"k1": {"key": "ccp-a", "allowedProtocols": ["anthropic"]},
                      "k2": {"key": "ccp-b", "allowedProtocols": ["chat", "responses"]},
                      "k3": {"key": "ccp-c"}})
    assert m["auth"].get_allowed_protocols("k1") == ["anthropic"]
    assert m["auth"].get_allowed_protocols("k2") == ["chat", "responses"]
    assert m["auth"].get_allowed_protocols("k3") == []
    print("  [PASS] /v1/models family filter: registry.available_models_for_families + auth.get_allowed_protocols")


# ─── 驱动 ────────────────────────────────────────────────────────


def _async(fn):
    def _w(m):
        asyncio.run(fn(m))
    _w.__name__ = fn.__name__
    return _w


def main() -> int:
    m = _import_modules()
    orig_cfg = m["config"].get().copy()

    tests = [
        _async(test_chat_non_stream_success),
        _async(test_responses_non_stream_success),
        _async(test_chat_stream_success),
        _async(test_responses_stream_success),
        _async(test_chat_500_switches_next),
        _async(test_guard_chat_n_gt_1),
        _async(test_allowed_protocols_mismatch_403),
        _async(test_no_candidates_family_filtered),
        test_list_models_family_filter,
    ]
    passed = 0
    try:
        print("── MS-2 OpenAI Passthrough Tests ────────────")
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig_cfg)
        m["config"].update(_restore)
        # 清空 state.db
        m["state_db"].perf_delete()
        m["state_db"].error_delete()
        m["state_db"].affinity_delete()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
