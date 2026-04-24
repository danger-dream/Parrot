"""MS-3 OpenAI 跨变体非流式翻译集成测试。

覆盖：
  - chat → openai-responses：文本、function tool、assistant 历史、reasoning
  - responses → openai-chat：文本、instructions 展开、function_call、function_call_output
  - 单元断言：translate_request 构造的上游 body 是否符合 responses/chat 规范
  - 响应反向：上游 JSON 翻译成 ingress 期望的格式
  - 跨变体 guard：built-in tools / prev_id / input 含 built-in call item 等 400

运行：./venv/bin/python -m src.tests.test_openai_m3
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
    from src.openai.transform import chat_to_responses, responses_to_chat, guard
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "log_db": log_db,
        "scheduler": scheduler, "scorer": scorer, "state_db": state_db,
        "upstream": upstream, "registry": registry, "api_channel": api_channel,
        "openai_handler": openai_handler,
        "chat_to_responses": chat_to_responses,
        "responses_to_chat": responses_to_chat,
        "guard": guard,
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


# ─── MockRouter + 渠道工厂（与 m2 复用风格） ─────────────────────


class MockRouter:
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


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


def _install_keys(m, keys: dict):
    def _mutate(cfg):
        cfg["apiKeys"] = keys
    m["config"].update(_mutate)


def _default_key(name="kopen", key="ccp-test"):
    return {name: {"key": key, "allowedModels": []}}


# ─── FakeRequest（与 m2 一致） ────────────────────────────────────


class FakeHeaders:
    def __init__(self, data):
        self._d = {k.lower(): v for k, v in data.items()}
    def get(self, k, default=None): return self._d.get(k.lower(), default)
    def items(self): return self._d.items()
    def keys(self): return self._d.keys()
    def __getitem__(self, k): return self._d[k.lower()]
    def __iter__(self): return iter(self._d.keys())
    def __len__(self): return len(self._d)


class FakeClient:
    def __init__(self, host="1.2.3.4"): self.host = host


class FakeRequest:
    def __init__(self, headers, body_bytes, client_ip="1.2.3.4"):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient(client_ip)
    async def body(self): return self._body


async def _call_openai_handler(m, router, ingress_protocol, body, api_key="ccp-test"):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    req = FakeRequest({"Authorization": f"Bearer {api_key}"},
                      json.dumps(body).encode("utf-8"))
    resp = await m["openai_handler"].handle(req, ingress_protocol=ingress_protocol)
    return resp, mock_client


# ─── 单元级：translate_request / translate_response ──────────────


def test_c2r_translate_request_basics(m):
    c2r = m["chat_to_responses"]
    body = {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "you are x"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "get_w", "arguments": "{\"a\":1}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
        "max_completion_tokens": 100,
        "reasoning_effort": "medium",
        "tools": [{"type": "function", "function": {"name": "get_w",
                                                    "parameters": {"type": "object"}}}],
        "tool_choice": {"type": "function", "function": {"name": "get_w"}},
        "response_format": {"type": "json_object"},
    }
    out = c2r.translate_request(body)
    assert out["model"] == "gpt-5"
    assert out["max_output_tokens"] == 100
    assert out["reasoning"]["effort"] == "medium"
    assert out["text"]["format"] == {"type": "json_object"}
    # Patch 3 / #30: FunctionTool.strict 必填补默认 False
    assert out["tools"] == [{"type": "function", "name": "get_w",
                             "parameters": {"type": "object"}, "strict": False}]
    assert out["tool_choice"] == {"type": "function", "name": "get_w"}
    items = out["input"]
    # 期望：developer message + user message + function_call item + function_call_output item
    assert items[0]["type"] == "message" and items[0]["role"] == "developer"
    assert items[1]["type"] == "message" and items[1]["role"] == "user"
    assert items[1]["content"] == [{"type": "input_text", "text": "hi"}]
    # assistant with tool_calls: assistant message skipped (empty content), function_call item added
    fc = [it for it in items if it.get("type") == "function_call"]
    assert len(fc) == 1
    assert fc[0]["call_id"] == "call_1"
    assert fc[0]["name"] == "get_w"
    assert fc[0]["arguments"] == "{\"a\":1}"
    fco = [it for it in items if it.get("type") == "function_call_output"]
    assert len(fco) == 1
    assert fco[0]["output"] == "sunny"
    print("  [PASS] c2r translate_request: messages → input items, tools flattened")


def test_c2r_translate_response_basics(m):
    c2r = m["chat_to_responses"]
    resp = {
        "id": "resp_abc", "object": "response", "status": "completed",
        "created_at": 123, "model": "gpt-5",
        "output": [
            {"type": "reasoning", "id": "rs_1",
             "summary": [{"type": "summary_text", "text": "because math"}]},
            {"type": "message", "id": "msg_1", "role": "assistant",
             "content": [{"type": "output_text", "text": "Answer: 2", "annotations": []}]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1",
             "name": "get_w", "arguments": "{}"},
        ],
        "output_text": "Answer: 2",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                  "input_tokens_details": {"cached_tokens": 3},
                  "output_tokens_details": {"reasoning_tokens": 2}},
    }
    out = c2r.translate_response(resp, model="gpt-5")
    assert out["object"] == "chat.completion"
    assert out["model"] == "gpt-5"
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Answer: 2"
    assert msg["reasoning_content"] == "because math"
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["function"]["name"] == "get_w"
    # tool_calls 出现 → finish_reason = tool_calls
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    # usage 映射
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
    assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    print("  [PASS] c2r translate_response: output → choices[0].message, reasoning_content, tool_calls")


def test_r2c_translate_request_basics(m):
    r2c = m["responses_to_chat"]
    body = {
        "model": "gpt-5",
        "instructions": "be concise",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_1",
             "name": "get_w", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
        ],
        "max_output_tokens": 100,
        "reasoning": {"effort": "low"},
        "tools": [{"type": "function", "name": "get_w",
                   "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "get_w"},
        "text": {"format": {"type": "json_object"}},
    }
    out = r2c.translate_request(body)
    assert out["model"] == "gpt-5"
    assert out["max_completion_tokens"] == 100
    assert out["reasoning_effort"] == "low"
    assert out["response_format"] == {"type": "json_object"}
    assert out["tools"] == [{"type": "function",
                             "function": {"name": "get_w",
                                          "parameters": {"type": "object"}}}]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "get_w"}}
    msgs = out["messages"]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "be concise"
    assert msgs[1]["role"] == "user" and msgs[1]["content"] == "hi"
    # function_call 聚合到 assistant 的 tool_calls
    assistant = msgs[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_w"
    # function_call_output 展开为 tool 消息
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "call_1"
    assert msgs[3]["content"] == "sunny"
    print("  [PASS] r2c translate_request: input items → chat messages, tool_calls merge")


def test_r2c_translate_response_basics(m):
    r2c = m["responses_to_chat"]
    chat = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 123, "model": "gpt-5",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "The answer is 2",
                "reasoning_content": "because math",
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "get_w", "arguments": "{}"}}],
            },
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "prompt_tokens_details": {"cached_tokens": 3},
                  "completion_tokens_details": {"reasoning_tokens": 2}},
    }
    out = r2c.translate_response(chat, model="gpt-5")
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["model"] == "gpt-5"
    items = out["output"]
    types = [it["type"] for it in items]
    assert types == ["reasoning", "message", "function_call"]
    assert items[0]["summary"][0]["text"] == "because math"
    assert items[1]["content"][0]["text"] == "The answer is 2"
    assert items[2]["call_id"] == "call_1"
    assert items[2]["arguments"] == "{}"
    assert out["output_text"] == "The answer is 2"
    # usage 映射
    assert out["usage"]["input_tokens"] == 10
    assert out["usage"]["output_tokens"] == 5
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 3
    assert out["usage"]["output_tokens_details"]["reasoning_tokens"] == 2
    print("  [PASS] r2c translate_response: chat.completion → response output items")


# ─── 端到端：跨变体请求/响应打通 ─────────────────────────────────


def _captured_upstream_body(router: MockRouter) -> dict:
    assert router.last_request is not None, "upstream 未被调用"
    return json.loads(router.last_request.content)


async def test_chat_to_responses_text(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "resp_xyz", "object": "response", "status": "completed",
            "created_at": 1, "model": "gpt-5",
            "output": [{
                "type": "message", "id": "msg_1", "role": "assistant",
                "content": [{"type": "output_text", "text": "hi there", "annotations": []}],
            }],
            "output_text": "hi there",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })
    router.register("https://r.example", _handler)

    chR = _make_openai_channel(m, "oaiR", "https://r.example",
                               protocol="openai-responses", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chR])

    body = {"model": "gpt-5", "stream": False,
            "messages": [{"role": "user", "content": "ping"}],
            "max_completion_tokens": 50}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200

    # 1) 上游收到的是 Responses body（messages → input[]）
    up = _captured_upstream_body(router)
    assert str(router.last_request.url).endswith("/v1/responses")
    assert up["model"] == "gpt-5"
    assert "messages" not in up
    assert up["input"] == [{"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "ping"}]}]
    assert up["max_output_tokens"] == 50

    # 2) 下游收到的是 chat.completion 形式
    out = json.loads(resp.body)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "hi there"
    assert out["choices"][0]["finish_reason"] == "stop"
    await mc.aclose()
    print("  [PASS] chat → openai-responses 上游，文本响应反向为 chat.completion")


async def test_chat_to_responses_function_tool(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def _handler(req):
        return httpx.Response(200, json={
            "id": "resp_xyz", "object": "response", "status": "completed",
            "created_at": 1, "model": "gpt-5",
            "output": [{
                "type": "function_call", "id": "fc_1", "call_id": "call_A",
                "name": "get_w", "arguments": "{\"city\":\"SF\"}", "status": "completed",
            }],
            "output_text": "",
            "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
        })
    router.register("https://r.example", _handler)

    chR = _make_openai_channel(m, "oaiR", "https://r.example",
                               protocol="openai-responses")
    _install_channels(m, [chR])

    body = {"model": "gpt-5", "stream": False,
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function",
                       "function": {"name": "get_w", "parameters": {"type": "object"}}}]}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200

    # 上游 tools 已扁平化
    up = _captured_upstream_body(router)
    # Patch 3 / #30: FunctionTool.strict 必填补默认 False
    assert up["tools"] == [{"type": "function", "name": "get_w",
                             "parameters": {"type": "object"}, "strict": False}]
    # 下游拿到 tool_calls + finish_reason=tool_calls
    out = json.loads(resp.body)
    msg = out["choices"][0]["message"]
    assert msg["tool_calls"][0]["id"] == "call_A"
    assert msg["tool_calls"][0]["function"]["name"] == "get_w"
    assert msg["tool_calls"][0]["function"]["arguments"] == "{\"city\":\"SF\"}"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    await mc.aclose()
    print("  [PASS] chat → responses 上游 function_call 正确反向")


async def test_chat_to_responses_reasoning(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def _handler(req):
        return httpx.Response(200, json={
            "id": "resp_xyz", "object": "response", "status": "completed",
            "created_at": 1, "model": "gpt-5",
            "output": [
                {"type": "reasoning", "id": "rs_1",
                 "summary": [{"type": "summary_text", "text": "step1; step2"}]},
                {"type": "message", "id": "msg_1", "role": "assistant",
                 "content": [{"type": "output_text", "text": "done", "annotations": []}]},
            ],
            "output_text": "done",
            "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8,
                      "output_tokens_details": {"reasoning_tokens": 2}},
        })
    router.register("https://r.example", _handler)

    chR = _make_openai_channel(m, "oaiR", "https://r.example",
                               protocol="openai-responses")
    _install_channels(m, [chR])

    body = {"model": "gpt-5", "stream": False,
            "messages": [{"role": "user", "content": "compute"}]}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200
    out = json.loads(resp.body)
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "step1; step2"
    assert msg["content"] == "done"
    assert out["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2
    await mc.aclose()
    print("  [PASS] chat → responses 上游 reasoning 映射为 reasoning_content")


async def test_responses_to_chat_text(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def _handler(req):
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi there"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
    router.register("https://c.example", _handler)

    chC = _make_openai_channel(m, "oaiC", "https://c.example",
                               protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chC])

    body = {"model": "gpt-5", "stream": False,
            "instructions": "be short",
            "input": "ping",
            "max_output_tokens": 50}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 200

    # 上游收 chat body：instructions → system 消息
    up = _captured_upstream_body(router)
    assert str(router.last_request.url).endswith("/v1/chat/completions")
    assert up["model"] == "gpt-5"
    assert up["max_completion_tokens"] == 50
    assert up["messages"] == [
        {"role": "system", "content": "be short"},
        {"role": "user", "content": "ping"},
    ]

    # 下游是 responses body
    out = json.loads(resp.body)
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["output_text"] == "hi there"
    assert out["output"][0]["type"] == "message"
    await mc.aclose()
    print("  [PASS] responses → chat 上游，instructions → system，输出反向为 response")


async def test_responses_to_chat_function_call_roundtrip(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()

    def _handler(req):
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {
                             "role": "assistant", "content": None,
                             "tool_calls": [{"id": "call_1", "type": "function",
                                             "function": {"name": "get_w",
                                                          "arguments": "{\"x\":1}"}}],
                         }}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8},
        })
    router.register("https://c.example", _handler)

    chC = _make_openai_channel(m, "oaiC", "https://c.example",
                               protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chC])

    # 带历史：user question → assistant function_call → function_call_output → follow-up
    body = {
        "model": "gpt-5", "stream": False,
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "call it"}]},
            {"type": "function_call", "call_id": "call_0",
             "name": "prev", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_0", "output": "done"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "now retry"}]},
        ],
        "tools": [{"type": "function", "name": "get_w",
                   "parameters": {"type": "object"}}],
    }
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 200

    # 上游 messages：user / assistant(tool_calls=[call_0]) / tool(call_0) / user
    up = _captured_upstream_body(router)
    msgs = up["messages"]
    assert msgs[0] == {"role": "user", "content": "call it"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "call_0"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "prev"
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_0", "content": "done"}
    assert msgs[3] == {"role": "user", "content": "now retry"}
    assert up["tools"] == [{"type": "function",
                             "function": {"name": "get_w",
                                          "parameters": {"type": "object"}}}]

    # 下游 responses JSON：function_call item
    out = json.loads(resp.body)
    fc_items = [it for it in out["output"] if it["type"] == "function_call"]
    assert len(fc_items) == 1
    assert fc_items[0]["call_id"] == "call_1"
    assert fc_items[0]["name"] == "get_w"
    assert out["status"] == "completed"
    await mc.aclose()
    print("  [PASS] responses → chat 上游：function_call 历史+新调用双向打通")


# ─── 跨变体 guard ────────────────────────────────────────────────


async def test_guard_r2c_builtin_tool(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    chC = _make_openai_channel(m, "oaiC", "https://c.example",
                               protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chC])

    body = {"model": "gpt-5", "input": "hi",
            "tools": [{"type": "web_search_preview"}]}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 400, f"status={resp.status_code}"
    out = json.loads(resp.body)
    assert out["error"]["type"] == "invalid_request_error"
    assert "web_search_preview" in out["error"]["message"]
    assert router.last_request is None
    await mc.aclose()
    print("  [PASS] r2c guard: built-in tool web_search_preview → 400")


async def test_guard_r2c_previous_response_id_not_found(m):
    """MS-5 起 Store 默认开启；未知的 previous_response_id → 404（NotFound 映射）。"""
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    chC = _make_openai_channel(m, "oaiC", "https://c.example",
                               protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chC])

    body = {"model": "gpt-5", "input": "continue", "previous_response_id": "resp_abc"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 404
    out = json.loads(resp.body)
    assert "previous_response_id" in out["error"]["message"]
    assert router.last_request is None
    await mc.aclose()
    print("  [PASS] r2c guard: unknown previous_response_id → 404 NotFound")


async def test_guard_r2c_builtin_call_in_input(m):
    _setup(m)
    _install_keys(m, _default_key())
    router = MockRouter()
    chC = _make_openai_channel(m, "oaiC", "https://c.example",
                               protocol="openai-chat", real="gpt-5", alias="gpt-5")
    _install_channels(m, [chC])

    body = {"model": "gpt-5",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "file_search_call", "id": "x", "queries": ["a"]},
            ]}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 400
    out = json.loads(resp.body)
    assert "file_search_call" in out["error"]["message"]
    assert router.last_request is None
    await mc.aclose()
    print("  [PASS] r2c guard: input contains file_search_call → 400")


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
        test_c2r_translate_request_basics,
        test_c2r_translate_response_basics,
        test_r2c_translate_request_basics,
        test_r2c_translate_response_basics,
        _async(test_chat_to_responses_text),
        _async(test_chat_to_responses_function_tool),
        _async(test_chat_to_responses_reasoning),
        _async(test_responses_to_chat_text),
        _async(test_responses_to_chat_function_call_roundtrip),
        _async(test_guard_r2c_builtin_tool),
        _async(test_guard_r2c_previous_response_id_not_found),
        _async(test_guard_r2c_builtin_call_in_input),
    ]
    passed = 0
    try:
        print("── MS-3 OpenAI Cross-Variant Non-Stream ─────")
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
        m["state_db"].perf_delete()
        m["state_db"].error_delete()
        m["state_db"].affinity_delete()
        m["state_db"].client_affinity_delete()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
