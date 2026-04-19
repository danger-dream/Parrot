"""MS-4 OpenAI 跨变体 SSE 流式翻译集成测试。

覆盖：
  单元级
  - stream_r2c（responses → chat）：文本 delta、tool_call 首次出现、
    arguments 增量、reasoning、usage、error、finish_reason=length
  - stream_c2r（chat → responses）：content delta 序列、tool_call 序列、
    reasoning、finish_reason 映射、中途 error chunk 终止

  端到端
  - chat ingress + openai-responses 上游（stream）→ 下游得到 chat.completion.chunk
  - responses ingress + openai-chat 上游（stream）→ 下游得到 response.* 事件

运行：./venv/bin/python -m src.tests.test_openai_m4
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys

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
    from src.openai.transform import stream_r2c, stream_c2r
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "log_db": log_db,
        "scheduler": scheduler, "scorer": scorer, "state_db": state_db,
        "upstream": upstream, "registry": registry, "api_channel": api_channel,
        "openai_handler": openai_handler,
        "stream_r2c": stream_r2c, "stream_c2r": stream_c2r,
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


# ─── 单元级工具 ──────────────────────────────────────────────────


def _run_translator(translator, chunks: list[bytes]) -> list[bytes]:
    """把一串字节喂给 translator.feed()，收尾再调 close()；返回所有下游帧。"""
    out: list[bytes] = []
    for c in chunks:
        out.extend(translator.feed(c))
    out.extend(translator.close())
    return out


def _parse_chat_frames(frames: list[bytes]) -> tuple[list[dict], bool]:
    """把 chat SSE 字节帧解析为 JSON 列表 + 是否结束（[DONE]）。"""
    text = b"".join(frames).decode("utf-8", errors="replace")
    parts = text.split("\n\n")
    objs: list[dict] = []
    done = False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        for line in part.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            try:
                objs.append(json.loads(data))
            except Exception:
                pass
    return objs, done


def _parse_responses_frames(frames: list[bytes]) -> list[tuple[str, dict]]:
    """把 responses SSE 字节帧解析为 (event_name, payload) 列表。"""
    text = b"".join(frames).decode("utf-8", errors="replace")
    parts = text.split("\n\n")
    out: list[tuple[str, dict]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        event_name = ""
        data_str = ""
        for line in part.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            out.append((event_name, json.loads(data_str)))
        except Exception:
            out.append((event_name, {}))
    return out


# ─── 单元：stream_r2c ────────────────────────────────────────────


def test_r2c_text_flow(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5", include_usage=False)
    events = [
        b'event: response.created\ndata: {"type":"response.created","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n',
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_1","output_index":0,"content_index":0,"delta":"hel"}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":4,"item_id":"msg_1","output_index":0,"content_index":0,"delta":"lo"}\n\n',
        b'event: response.output_item.done\ndata: {"type":"response.output_item.done","sequence_number":5,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"hello","annotations":[]}]}}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":6,"response":{"id":"resp_1","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","content":[{"type":"output_text","text":"hello","annotations":[]}]}],"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}\n\n',
    ]
    frames = _run_translator(tr, events)
    objs, done = _parse_chat_frames(frames)
    assert done, "expected [DONE]"
    # 首帧是 role chunk
    assert objs[0]["choices"][0]["delta"].get("role") == "assistant"
    # 两条 content delta
    contents = [o["choices"][0]["delta"].get("content") for o in objs
                if o["choices"][0]["delta"].get("content")]
    assert "".join(contents) == "hello"
    # 末帧是 finish_reason=stop
    assert objs[-1]["choices"][0]["finish_reason"] == "stop"
    print("  [PASS] r2c: output_text → role + content + finish_reason=stop")


def test_r2c_tool_call(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5")
    events = [
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":1,"output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_A","name":"get_w","arguments":"","status":"in_progress"}}\n\n',
        b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":2,"item_id":"fc_1","output_index":0,"delta":"{\\"city"}\n\n',
        b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","sequence_number":3,"item_id":"fc_1","output_index":0,"delta":"\\":\\"SF\\"}"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":4,"response":{"id":"resp_1","status":"completed","output":[{"type":"function_call","id":"fc_1","call_id":"call_A","name":"get_w","arguments":"{\\"city\\":\\"SF\\"}"}]}}\n\n',
    ]
    frames = _run_translator(tr, events)
    objs, done = _parse_chat_frames(frames)
    assert done
    # 第一条 tool_call chunk 带 id + name + arguments=""
    first_tc_chunk = None
    for o in objs:
        tcs = o["choices"][0]["delta"].get("tool_calls")
        if tcs and tcs[0].get("id"):
            first_tc_chunk = tcs[0]
            break
    assert first_tc_chunk is not None
    assert first_tc_chunk["id"] == "call_A"
    assert first_tc_chunk["type"] == "function"
    assert first_tc_chunk["function"]["name"] == "get_w"
    assert first_tc_chunk["function"]["arguments"] == ""
    # 拼接所有 arguments 片段
    pieces: list[str] = []
    for o in objs:
        tcs = o["choices"][0]["delta"].get("tool_calls")
        if not tcs:
            continue
        for tc in tcs:
            fn = tc.get("function") or {}
            if fn.get("arguments"):
                pieces.append(fn["arguments"])
    assert "".join(pieces) == "{\"city\":\"SF\"}"
    assert objs[-1]["choices"][0]["finish_reason"] == "tool_calls"
    print("  [PASS] r2c: function_call 首包带 id/name，arguments 增量流，finish=tool_calls")


def test_r2c_incomplete_length(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5")
    events = [
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":1,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":2,"item_id":"msg_1","output_index":0,"content_index":0,"delta":"partial"}\n\n',
        b'event: response.incomplete\ndata: {"type":"response.incomplete","sequence_number":3,"response":{"id":"resp_1","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[]}}\n\n',
    ]
    frames = _run_translator(tr, events)
    objs, done = _parse_chat_frames(frames)
    assert done
    assert objs[-1]["choices"][0]["finish_reason"] == "length"
    print("  [PASS] r2c: response.incomplete max_output_tokens → finish_reason=length")


def test_r2c_reasoning(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5")
    events = [
        b'event: response.reasoning_summary_text.delta\ndata: {"type":"response.reasoning_summary_text.delta","sequence_number":1,"delta":"thinking"}\n\n',
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":2,"delta":"hi"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":3,"response":{"id":"resp_1","status":"completed","output":[]}}\n\n',
    ]
    frames = _run_translator(tr, events)
    objs, done = _parse_chat_frames(frames)
    assert done
    rc_chunks = [o for o in objs if o["choices"][0]["delta"].get("reasoning_content")]
    assert len(rc_chunks) == 1
    assert rc_chunks[0]["choices"][0]["delta"]["reasoning_content"] == "thinking"
    print("  [PASS] r2c: reasoning_summary_text.delta → delta.reasoning_content")


def test_r2c_include_usage(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5", include_usage=True)
    events = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":1,"delta":"hi"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","sequence_number":2,"response":{"id":"resp_1","status":"completed","output":[],"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"input_tokens_details":{"cached_tokens":3}}}}\n\n',
    ]
    frames = _run_translator(tr, events)
    objs, done = _parse_chat_frames(frames)
    assert done
    usage_chunks = [o for o in objs if o.get("usage") and not o["choices"]]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["prompt_tokens"] == 10
    assert usage_chunks[0]["usage"]["completion_tokens"] == 5
    assert usage_chunks[0]["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
    print("  [PASS] r2c: include_usage=True 末帧发 usage chunk")


def test_r2c_failed(m):
    T = m["stream_r2c"].StreamTranslator
    tr = T(model="gpt-5")
    events = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":1,"delta":"hi"}\n\n',
        b'event: response.failed\ndata: {"type":"response.failed","sequence_number":2,"response":{"id":"resp_1","status":"failed","error":{"message":"rate limited","type":"rate_limit_exceeded"}}}\n\n',
    ]
    frames = _run_translator(tr, events)
    text = b"".join(frames).decode()
    assert '"error"' in text
    assert "rate limited" in text
    assert "[DONE]" in text
    print("  [PASS] r2c: response.failed → error frame + [DONE]")


# ─── 单元：stream_c2r ────────────────────────────────────────────


def test_c2r_text_flow(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hel"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    names = [n for n, _ in events]
    # 起始两个元事件
    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    # 应该有 output_item.added (message) + content_part.added + >=2 output_text.delta
    assert "response.output_item.added" in names
    assert "response.content_part.added" in names
    delta_events = [p for n, p in events if n == "response.output_text.delta"]
    text = "".join(e.get("delta", "") for e in delta_events)
    assert text == "hello"
    # 末尾应有 output_text.done / content_part.done / output_item.done / response.completed
    assert "response.output_text.done" in names
    assert "response.content_part.done" in names
    assert "response.output_item.done" in names
    assert names[-1] == "response.completed"
    # response.completed 的 response.output 含完整 message
    completed = [p for n, p in events if n == "response.completed"][0]
    output = completed["response"]["output"]
    assert output[0]["type"] == "message"
    assert output[0]["content"][0]["text"] == "hello"
    # usage 映射
    assert completed["response"]["usage"]["input_tokens"] == 10
    assert completed["response"]["usage"]["output_tokens"] == 2
    print("  [PASS] c2r: content chunks → message item + output_text.delta + completed")


def test_c2r_tool_call(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_A","type":"function","function":{"name":"get_w","arguments":""}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"x\\""}}]},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":1}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    names = [n for n, _ in events]
    assert "response.output_item.added" in names
    added = [p for n, p in events if n == "response.output_item.added"]
    assert added[0]["item"]["type"] == "function_call"
    assert added[0]["item"]["call_id"] == "call_A"
    assert added[0]["item"]["name"] == "get_w"
    # arguments delta
    args = "".join(p.get("delta", "") for n, p in events
                    if n == "response.function_call_arguments.delta")
    assert args == "{\"x\":1}"
    # 收尾
    assert "response.function_call_arguments.done" in names
    completed = [p for n, p in events if n == "response.completed"][0]
    out = completed["response"]["output"]
    assert out[0]["type"] == "function_call"
    assert out[0]["arguments"] == "{\"x\":1}"
    print("  [PASS] c2r: tool_calls 流 → function_call item + arguments.delta + completed")


def test_c2r_length_incomplete(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    last_name, last_payload = events[-1]
    assert last_name == "response.incomplete"
    assert last_payload["response"]["status"] == "incomplete"
    assert last_payload["response"]["incomplete_details"]["reason"] == "max_output_tokens"
    print("  [PASS] c2r: finish_reason=length → response.incomplete max_output_tokens")


def test_c2r_reasoning(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning_content":"why "},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning_content":"math"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"2"},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    names = [n for n, _ in events]
    # reasoning item 在 message item 之前
    assert "response.reasoning_summary_part.added" in names
    assert "response.reasoning_summary_text.delta" in names
    assert names.index("response.output_item.added") < names.index("response.content_part.added")
    # completed 含两个 output items
    completed = [p for n, p in events if n == "response.completed"][0]
    types = [it["type"] for it in completed["response"]["output"]]
    assert types == ["reasoning", "message"]
    print("  [PASS] c2r: reasoning_content 流 → reasoning item 在 message 之前")


def test_c2r_upstream_error(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n',
        b'data: {"error":{"message":"upstream down","type":"server_error"}}\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    last_name, last_payload = events[-1]
    assert last_name == "response.failed"
    assert last_payload["response"]["status"] == "failed"
    assert "upstream down" in last_payload["response"]["error"]["message"]
    print("  [PASS] c2r: 上游 error chunk → response.failed")


def test_c2r_empty_content_only_tool_calls(m):
    """content=null 但有 tool_calls：不应创建空 message item。"""
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5")
    chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_X","type":"function","function":{"name":"f","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    frames = _run_translator(tr, chunks)
    events = _parse_responses_frames(frames)
    completed = [p for n, p in events if n == "response.completed"][0]
    types = [it["type"] for it in completed["response"]["output"]]
    assert types == ["function_call"]
    print("  [PASS] c2r: 空 content 仅 tool_calls → 只含 function_call item")


# ─── 端到端：真实跨变体 stream ───────────────────────────────────


def _make_openai_channel(m, name, base_url, protocol="openai-chat", real="gpt-5", alias="gpt-5"):
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


class MockRouter:
    def __init__(self):
        self.handlers: dict[str, callable] = {}
        self.last_request: httpx.Request | None = None

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        url_str = str(request.url)
        for base, handler in self.handlers.items():
            if url_str.startswith(base):
                return handler(request)
        return httpx.Response(404, text="no mock")


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
    def __init__(self, headers, body_bytes):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient()
    async def body(self): return self._body


async def _call_openai_handler(m, router, ingress_protocol, body):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    req = FakeRequest({"Authorization": "Bearer ccp-test"},
                      json.dumps(body).encode("utf-8"))
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


async def test_e2e_chat_to_responses_stream(m):
    _setup(m)
    _install_keys(m, {"k": {"key": "ccp-test"}})
    router = MockRouter()

    # 上游 responses SSE
    def _handler(req):
        payload = (
            b'event: response.created\ndata: {"type":"response.created","sequence_number":1,"response":{"id":"resp_1","status":"in_progress"}}\n\n'
            b'event: response.output_item.added\ndata: {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","id":"msg_1","role":"assistant","status":"in_progress","content":[]}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_1","output_index":0,"content_index":0,"delta":"hi"}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","sequence_number":4,"item_id":"msg_1","output_index":0,"content_index":0,"delta":" there"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","sequence_number":5,"response":{"id":"resp_1","status":"completed","output":[{"type":"message","id":"msg_1","role":"assistant","content":[{"type":"output_text","text":"hi there","annotations":[]}]}],"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n\n'
        )
        return httpx.Response(200, content=payload,
                              headers={"content-type": "text/event-stream"})
    router.register("https://r.example", _handler)

    ch = _make_openai_channel(m, "oaiR", "https://r.example", protocol="openai-responses")
    _install_channels(m, [ch])

    body = {"model": "gpt-5", "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
            "stream_options": {"include_usage": True}}
    resp, mc = await _call_openai_handler(m, router, "chat", body)
    assert resp.status_code == 200

    text = await _consume_streaming_to_string(resp)
    # 下游收到的应该是 chat.completion.chunk 格式
    assert "chat.completion.chunk" in text
    assert "[DONE]" in text
    # 不应出现 responses 的 event: 行（下游是 chat 无 event:）
    assert "event: response." not in text
    # content 拼接
    objs, _ = _parse_chat_frames([text.encode()])
    contents = [o["choices"][0]["delta"].get("content") for o in objs
                if o.get("choices") and o["choices"][0]["delta"].get("content")]
    assert "".join(contents) == "hi there"
    # finish_reason
    finished = [o for o in objs if o.get("choices") and o["choices"][0].get("finish_reason")]
    assert finished[-1]["choices"][0]["finish_reason"] == "stop"
    # include_usage → 末帧带 usage
    usage_chunks = [o for o in objs if o.get("usage") and not o["choices"]]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["prompt_tokens"] == 5
    await mc.aclose()
    print("  [PASS] 端到端：chat ingress → openai-responses 上游 stream")


async def test_e2e_responses_to_chat_stream(m):
    _setup(m)
    _install_keys(m, {"k": {"key": "ccp-test"}})
    router = MockRouter()

    def _handler(req):
        payload = (
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
            b'data: [DONE]\n\n'
        )
        return httpx.Response(200, content=payload,
                              headers={"content-type": "text/event-stream"})
    router.register("https://c.example", _handler)

    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])

    body = {"model": "gpt-5", "stream": True, "input": "ping"}
    resp, mc = await _call_openai_handler(m, router, "responses", body)
    assert resp.status_code == 200

    text = await _consume_streaming_to_string(resp)
    # 下游是 responses SSE
    assert "event: response.created" in text
    assert "event: response.completed" in text
    assert "response.output_text.delta" in text
    # 不应出现 chat.completion.chunk / [DONE]
    assert "chat.completion.chunk" not in text
    assert "[DONE]" not in text

    events = _parse_responses_frames([text.encode()])
    deltas = [p.get("delta", "") for n, p in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "hello world"
    completed = [p for n, p in events if n == "response.completed"][0]
    assert completed["response"]["status"] == "completed"
    await mc.aclose()
    print("  [PASS] 端到端：responses ingress → openai-chat 上游 stream")


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
        test_r2c_text_flow,
        test_r2c_tool_call,
        test_r2c_incomplete_length,
        test_r2c_reasoning,
        test_r2c_include_usage,
        test_r2c_failed,
        test_c2r_text_flow,
        test_c2r_tool_call,
        test_c2r_length_incomplete,
        test_c2r_reasoning,
        test_c2r_upstream_error,
        test_c2r_empty_content_only_tool_calls,
        _async(test_e2e_chat_to_responses_stream),
        _async(test_e2e_responses_to_chat_stream),
    ]
    passed = 0
    try:
        print("── MS-4 OpenAI Cross-Variant Streaming ──────")
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

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
