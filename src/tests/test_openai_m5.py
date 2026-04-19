"""MS-5 previous_response_id 本地 Store 测试。

覆盖：
  Store 单元
    - init / save / lookup 基本路径
    - TTL 过期 → ResponseExpired
    - api_key_name 不匹配 → ResponseForbidden
    - 未知 response_id → ResponseNotFound
    - expand_history：多层链展开 + 循环防御 + 深度截断
    - cleanup_expired

  端到端（responses 入口 + openai-chat 上游，非流式 + 流式）
    - 第一次请求 → 返回的 resp.id 已写入 Store
    - 第二次请求带 previous_response_id → 上游收到 [user, assistant, user] 展开
    - 链深度 >1 的续接
    - 未知 prev_id → 404；异 Key → 403
    - 流式链路的 Store 写入

运行：./venv/bin/python -m src.tests.test_openai_m5
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
    from src.openai import handler as openai_handler, store as openai_store
    from src.openai.channel.registration import register_factories
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "log_db": log_db,
        "scheduler": scheduler, "scorer": scorer, "state_db": state_db,
        "upstream": upstream, "registry": registry, "api_channel": api_channel,
        "openai_handler": openai_handler, "openai_store": openai_store,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["openai_store"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["openai_store"]._reset_for_test()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"].init()
    m["cooldown"].init()
    m["scorer"].init()


# ─── Store 单元 ──────────────────────────────────────────────────


def test_store_save_lookup_basic(m):
    _setup(m)
    s = m["openai_store"]
    s.save("resp_1", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1",
           input_items=[{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "hi"}]}],
           output_items=[{"type": "message", "id": "msg_1", "role": "assistant",
                          "content": [{"type": "output_text", "text": "hi there",
                                       "annotations": []}]}])
    rec = s.lookup("resp_1", api_key_name="k1")
    assert rec.response_id == "resp_1"
    assert rec.parent_id is None
    assert rec.api_key_name == "k1"
    assert rec.input_items[0]["content"][0]["text"] == "hi"
    assert rec.output_items[0]["content"][0]["text"] == "hi there"
    print("  [PASS] store: save + lookup 往返")


def test_store_not_found(m):
    _setup(m)
    s = m["openai_store"]
    try:
        s.lookup("resp_missing", api_key_name="k1")
    except s.ResponseNotFound:
        print("  [PASS] store: 未知 response_id → ResponseNotFound")
        return
    assert False, "expected ResponseNotFound"


def test_store_forbidden(m):
    _setup(m)
    s = m["openai_store"]
    s.save("resp_1", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1", input_items=[], output_items=[])
    try:
        s.lookup("resp_1", api_key_name="k2")
    except s.ResponseForbidden:
        print("  [PASS] store: api_key_name 不匹配 → ResponseForbidden")
        return
    assert False


def test_store_expired(m):
    _setup(m)
    s = m["openai_store"]
    s.save("resp_e", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1", input_items=[], output_items=[], ttl_seconds=1)
    # 直接 poke DB 把 expires_at 提前
    conn = s._get_conn()
    with s._write_lock:
        conn.execute("UPDATE openai_response_store SET expires_at=? WHERE response_id=?",
                     (time.time() - 10, "resp_e"))
        conn.commit()
    try:
        s.lookup("resp_e", api_key_name="k1")
    except s.ResponseExpired:
        print("  [PASS] store: 过期 → ResponseExpired")
        return
    assert False


def test_store_expand_chain(m):
    _setup(m)
    s = m["openai_store"]
    # 构造三层链：resp_a → resp_b → resp_c
    s.save("resp_a", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1",
           input_items=[{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Q1"}]}],
           output_items=[{"type": "message", "id": "m1", "role": "assistant",
                          "content": [{"type": "output_text", "text": "A1",
                                       "annotations": []}]}])
    s.save("resp_b", "resp_a", api_key_name="k1", model="gpt-5",
           channel_key="api:c1",
           input_items=[{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Q2"}]}],
           output_items=[{"type": "message", "id": "m2", "role": "assistant",
                          "content": [{"type": "output_text", "text": "A2",
                                       "annotations": []}]}])
    s.save("resp_c", "resp_b", api_key_name="k1", model="gpt-5",
           channel_key="api:c1",
           input_items=[{"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Q3"}]}],
           output_items=[{"type": "message", "id": "m3", "role": "assistant",
                          "content": [{"type": "output_text", "text": "A3",
                                       "annotations": []}]}])
    items = s.expand_history("resp_c", api_key_name="k1")
    texts = []
    for it in items:
        if it.get("type") == "message":
            for c in it.get("content") or []:
                if c.get("type") in ("input_text", "output_text"):
                    texts.append(c.get("text", ""))
    # 老→新：Q1 A1 Q2 A2 Q3 A3
    assert texts == ["Q1", "A1", "Q2", "A2", "Q3", "A3"], texts
    print("  [PASS] store: expand_history 三层链展开按时间顺序")


def test_store_cleanup_expired(m):
    _setup(m)
    s = m["openai_store"]
    s.save("r_ok", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1", input_items=[], output_items=[])
    s.save("r_old", None, api_key_name="k1", model="gpt-5",
           channel_key="api:c1", input_items=[], output_items=[])
    # 手动把 r_old 过期
    conn = s._get_conn()
    with s._write_lock:
        conn.execute("UPDATE openai_response_store SET expires_at=? WHERE response_id=?",
                     (time.time() - 10, "r_old"))
        conn.commit()
    cleared = s.cleanup_expired()
    assert cleared == 1
    # r_ok 应还在
    rec = s.lookup("r_ok", api_key_name="k1")
    assert rec.response_id == "r_ok"
    # r_old 没了
    try:
        s.lookup("r_old", api_key_name="k1")
        assert False, "expected NotFound after cleanup"
    except s.ResponseNotFound:
        pass
    print("  [PASS] store: cleanup_expired 只清理过期，保留有效")


# ─── 端到端测试支架 ──────────────────────────────────────────────


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


async def _call(m, router, ingress, body, api_key="ccp-alice"):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    req = FakeRequest({"Authorization": f"Bearer {api_key}"},
                      json.dumps(body).encode("utf-8"))
    resp = await m["openai_handler"].handle(req, ingress_protocol=ingress)
    return resp, mock_client


async def _consume_streaming_to_string(resp) -> str:
    chunks = []
    async for c in resp.body_iterator:
        if isinstance(c, str):
            chunks.append(c.encode())
        else:
            chunks.append(c)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _chat_json(content: str, *, usage=None):
    body = {
        "id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": "gpt-5",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
    }
    body["usage"] = usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    return httpx.Response(200, json=body,
                          headers={"content-type": "application/json"})


def _chat_sse(content: str):
    payload = (
        f'data: {{"id":"c","object":"chat.completion.chunk","choices":[{{"index":0,"delta":{{"role":"assistant"}},"finish_reason":null}}]}}\n\n'
        f'data: {{"id":"c","object":"chat.completion.chunk","choices":[{{"index":0,"delta":{{"content":"{content}"}},"finish_reason":"stop"}}],"usage":{{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}}}\n\n'
        f'data: [DONE]\n\n'
    ).encode()
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


# ─── 端到端：Store 读写 ─────────────────────────────────────────


async def test_e2e_first_then_followup(m):
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    # 上游每次都返回 "ok"
    router.register("https://c.example", lambda r: _chat_json("ok"))
    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])

    # 第一次请求
    body1 = {"model": "gpt-5", "stream": False, "input": "hello"}
    resp1, mc1 = await _call(m, router, "responses", body1)
    assert resp1.status_code == 200
    out1 = json.loads(resp1.body)
    resp_id_1 = out1["id"]
    assert resp_id_1.startswith("resp_")
    await mc1.aclose()

    # Store 里应该有这条记录
    s = m["openai_store"]
    rec = s.lookup(resp_id_1, api_key_name="alice")
    assert rec.parent_id is None
    assert rec.input_items[0]["content"][0]["text"] == "hello"
    # output_items 里是翻译后的 responses 风格 message
    assert rec.output_items[0]["type"] == "message"
    assert rec.output_items[0]["content"][0]["text"] == "ok"

    # 第二次请求：带 previous_response_id
    router.requests.clear()
    body2 = {"model": "gpt-5", "stream": False,
             "previous_response_id": resp_id_1,
             "input": "follow up"}
    resp2, mc2 = await _call(m, router, "responses", body2)
    assert resp2.status_code == 200, f"body={resp2.body!r}"
    out2 = json.loads(resp2.body)
    resp_id_2 = out2["id"]
    assert resp_id_2 != resp_id_1
    # 上游收到展开后的 messages：[user hello, assistant ok, user follow up]
    up_body = json.loads(router.last_request.content)
    msgs = up_body["messages"]
    assert msgs[0] == {"role": "user", "content": "hello"}
    assert msgs[1] == {"role": "assistant", "content": "ok"}
    assert msgs[2] == {"role": "user", "content": "follow up"}
    await mc2.aclose()

    # 新记录的 parent 指向上一次
    rec2 = s.lookup(resp_id_2, api_key_name="alice")
    assert rec2.parent_id == resp_id_1
    assert rec2.input_items[0]["content"][0]["text"] == "follow up"
    # input_items 只存"本次"（不含展开历史）
    assert len(rec2.input_items) == 1

    print("  [PASS] 端到端：第一次 + 第二次 prev_id 续接 + Store 链正确")


async def test_e2e_chain_depth_three(m):
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    counter = {"n": 0}
    answers = ["A1", "A2", "A3"]

    def _handler(req):
        i = counter["n"]
        counter["n"] = min(i + 1, len(answers) - 1)
        return _chat_json(answers[i])
    router.register("https://c.example", _handler)

    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])

    # Round 1
    resp, mc = await _call(m, router, "responses",
                            {"model": "gpt-5", "stream": False, "input": "Q1"})
    assert resp.status_code == 200
    rid1 = json.loads(resp.body)["id"]
    await mc.aclose()

    # Round 2
    resp, mc = await _call(m, router, "responses",
                            {"model": "gpt-5", "stream": False,
                             "previous_response_id": rid1, "input": "Q2"})
    assert resp.status_code == 200
    rid2 = json.loads(resp.body)["id"]
    await mc.aclose()

    # Round 3
    router.requests.clear()
    resp, mc = await _call(m, router, "responses",
                            {"model": "gpt-5", "stream": False,
                             "previous_response_id": rid2, "input": "Q3"})
    assert resp.status_code == 200
    up_body = json.loads(router.last_request.content)
    msgs = up_body["messages"]
    # 期望 6 条历史 + 当前 Q3 = 7 条
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in msgs] == ["Q1", "A1", "Q2", "A2", "Q3"]
    await mc.aclose()
    print("  [PASS] 端到端：三轮链 expand_history 正确")


async def test_e2e_unknown_prev_id_404(m):
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])
    body = {"model": "gpt-5", "stream": False,
            "previous_response_id": "resp_does_not_exist", "input": "hi"}
    resp, mc = await _call(m, router, "responses", body)
    assert resp.status_code == 404
    out = json.loads(resp.body)
    assert out["error"]["type"] == "not_found_error"
    assert router.last_request is None
    await mc.aclose()
    print("  [PASS] 端到端：未知 previous_response_id → 404")


async def test_e2e_cross_key_forbidden_403(m):
    _setup(m)
    _install_keys(m, {
        "alice": {"key": "ccp-alice"},
        "bob":   {"key": "ccp-bob"},
    })
    router = MockRouter()
    router.register("https://c.example", lambda r: _chat_json("ok"))
    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])

    # alice 发一次
    resp, mc = await _call(m, router, "responses",
                            {"model": "gpt-5", "stream": False, "input": "hi"},
                            api_key="ccp-alice")
    assert resp.status_code == 200
    rid = json.loads(resp.body)["id"]
    await mc.aclose()

    # bob 用 alice 的 resp_id → 403
    resp, mc = await _call(m, router, "responses",
                            {"model": "gpt-5", "stream": False,
                             "previous_response_id": rid, "input": "hi"},
                            api_key="ccp-bob")
    assert resp.status_code == 403, f"status={resp.status_code}"
    out = json.loads(resp.body)
    assert out["error"]["type"] == "permission_error"
    await mc.aclose()
    print("  [PASS] 端到端：跨 Key 的 prev_id → 403")


async def test_e2e_store_disabled_prev_id_400(m):
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])
    # 关闭 Store
    def _disable(c):
        c.setdefault("openai", {}).setdefault("store", {})["enabled"] = False
    m["config"].update(_disable)
    try:
        body = {"model": "gpt-5", "stream": False,
                "previous_response_id": "resp_x", "input": "hi"}
        resp, mc = await _call(m, router, "responses", body)
        # ingress guard 首先拦：previous_response_id requires openai.store.enabled=true
        assert resp.status_code == 400
        out = json.loads(resp.body)
        assert "openai.store" in out["error"]["message"]
        await mc.aclose()
    finally:
        def _enable(c):
            c.setdefault("openai", {}).setdefault("store", {})["enabled"] = True
        m["config"].update(_enable)
    print("  [PASS] 端到端：Store 关闭 + 带 prev_id → 400")


async def test_e2e_stream_store_save(m):
    """流式路径收尾也写 Store。"""
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    router.register("https://c.example", lambda r: _chat_sse("streamed"))
    ch = _make_openai_channel(m, "oaiC", "https://c.example", protocol="openai-chat")
    _install_channels(m, [ch])

    body = {"model": "gpt-5", "stream": True, "input": "hi"}
    resp, mc = await _call(m, router, "responses", body)
    assert resp.status_code == 200
    text = await _consume_streaming_to_string(resp)
    # 解析 SSE events，找 response.completed
    rid = None
    for block in text.split("\n\n"):
        block = block.strip()
        if not block.startswith("event: response.completed"):
            continue
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                rid = payload.get("response", {}).get("id")
                break
        if rid:
            break
    assert rid is not None, f"stream 应含 response.completed 带 resp_id；得到：{text[:400]}"
    await mc.aclose()

    # Store 应该有这条
    s = m["openai_store"]
    rec = s.lookup(rid, api_key_name="alice")
    assert rec.parent_id is None
    # output_items 里能看到 message 项的 text=streamed
    msg_items = [it for it in rec.output_items if it.get("type") == "message"]
    assert msg_items
    assert msg_items[0]["content"][0]["text"] == "streamed"
    print("  [PASS] 端到端：流式路径 close() 也写 Store")


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
        test_store_save_lookup_basic,
        test_store_not_found,
        test_store_forbidden,
        test_store_expired,
        test_store_expand_chain,
        test_store_cleanup_expired,
        _async(test_e2e_first_then_followup),
        _async(test_e2e_chain_depth_three),
        _async(test_e2e_unknown_prev_id_404),
        _async(test_e2e_cross_key_forbidden_403),
        _async(test_e2e_store_disabled_prev_id_400),
        _async(test_e2e_stream_store_save),
    ]
    passed = 0
    try:
        print("── MS-5 OpenAI previous_response_id Store ───")
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
        try:
            m["openai_store"]._reset_for_test()
        except Exception:
            pass

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
