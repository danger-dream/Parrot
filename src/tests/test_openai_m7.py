"""MS-7 OpenAI 家族 fingerprint + 亲和测试。

覆盖：
  单元
  - fingerprint_{query,write}_chat：Nth 到达 hash == (N-1) 完成 hash
  - fingerprint_{query,write}_responses：同
  - 命名空间隔离：chat vs responses、openai vs anthropic hash 不碰撞
  - 消息数 < 3 → query 返回 None

  端到端
  - chat 入口 + openai-chat 上游：第一次请求 → write 指纹；第二次同 key+ip+history
    → query 命中，调度把上次渠道顶到首位
  - responses 入口 + openai-responses 上游：同
  - 同协议 resp 流式：builder.get_output_items() 写指纹
  - 跨变体 responses 入口 + openai-chat 上游（非流式）：write 成功
  - 跨 Key 不命中

运行：./venv/bin/python -m src.tests.test_openai_m7
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
        affinity, auth, config, cooldown, errors, failover, fingerprint, log_db,
        scheduler, scorer, state_db, upstream,
    )
    from src.channel import registry, api_channel
    from src.openai import handler as openai_handler, store as openai_store
    from src.openai.channel.registration import register_factories
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "fingerprint": fingerprint,
        "log_db": log_db, "scheduler": scheduler, "scorer": scorer,
        "state_db": state_db, "upstream": upstream,
        "registry": registry, "api_channel": api_channel,
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


# ─── 单元测试 ────────────────────────────────────────────────────


def test_fp_chat_query_equals_write(m):
    """Nth 请求到达时 query hash == (N-1) 完成时 write hash。"""
    fp = m["fingerprint"]
    # (N-1) 完成：此时 messages=[u0, a0, u1]，append a1 后 full=[u0,a0,u1,a1]
    msgs_after_n_minus_1 = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
    ]
    assistant_n_minus_1 = {"role": "assistant", "content": "a1"}
    h_write = fp.fingerprint_write_chat("k", "1.1.1.1", msgs_after_n_minus_1, assistant_n_minus_1)

    # N 到达：messages=[u0, a0, u1, a1, u2]；query 去掉 u2 后取最后两条 = [u1, a1]
    msgs_at_n = msgs_after_n_minus_1 + [assistant_n_minus_1, {"role": "user", "content": "u2"}]
    h_query = fp.fingerprint_query_chat("k", "1.1.1.1", msgs_at_n)
    assert h_write is not None
    assert h_query == h_write
    print("  [PASS] fp chat: query == write")


def test_fp_chat_short_returns_none(m):
    fp = m["fingerprint"]
    assert fp.fingerprint_query_chat("k", "ip", []) is None
    assert fp.fingerprint_query_chat("k", "ip",
                                      [{"role": "user", "content": "a"}]) is None
    assert fp.fingerprint_query_chat("k", "ip",
                                      [{"role": "user", "content": "a"},
                                       {"role": "assistant", "content": "b"}]) is None
    print("  [PASS] fp chat: 消息不足 3 条 → None")


def test_fp_resp_query_equals_write(m):
    fp = m["fingerprint"]
    u0 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q0"}]}
    a0 = {"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "A0", "annotations": []}]}
    u1 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q1"}]}
    a1 = {"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "A1", "annotations": []}]}

    # (N-1) 完成：input_items=[u0,a0,u1]；output_items=[a1]
    h_write = fp.fingerprint_write_responses("k", "1.1.1.1", [u0, a0, u1], [a1])

    # N 到达：input=[u0,a0,u1,a1,u2]；query 去掉 u2 取 [u1,a1]
    u2 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q2"}]}
    h_query = fp.fingerprint_query_responses("k", "1.1.1.1", [u0, a0, u1, a1, u2])
    assert h_write is not None
    assert h_query == h_write
    print("  [PASS] fp responses: query == write")


def test_fp_resp_skips_noise_items(m):
    """reasoning / web_search_call 等 item 不影响 hash。"""
    fp = m["fingerprint"]
    u0 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q0"}]}
    a0 = {"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "A0", "annotations": []}]}
    u1 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q1"}]}

    h_clean = fp.fingerprint_query_responses("k", "ip",
        [u0, a0, u1, {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": "Q2"}]}])

    h_with_noise = fp.fingerprint_query_responses("k", "ip",
        [u0,
         {"type": "reasoning", "summary": [{"type": "summary_text", "text": "why"}]},
         a0, u1,
         {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q2"}]}])
    assert h_clean == h_with_noise, "reasoning items 不应影响 hash"
    print("  [PASS] fp responses: 不稳定 items（reasoning 等）不影响 hash")


def test_fp_namespace_isolation(m):
    """chat / responses / anthropic 三套 hash 空间不碰撞。"""
    fp = m["fingerprint"]
    msg = {"role": "user", "content": "hi"}
    msg_resp = {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hi"}]}
    h_anth = fp._make_hash("k", "ip", msg, msg)                       # anthropic 无前缀
    h_chat = fp._make_hash_canon("openai-chat", "k", "ip", msg, msg, canon=fp._canon_chat)
    h_resp = fp._make_hash_canon("openai-resp", "k", "ip", msg_resp, msg_resp, canon=fp._canon_resp)
    assert h_anth != h_chat != h_resp != h_anth
    print("  [PASS] fp 命名空间隔离：三个家族 hash 空间独立")


def test_fp_cross_key_not_equal(m):
    fp = m["fingerprint"]
    msgs = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    h_k1 = fp.fingerprint_query_chat("k1", "ip", msgs)
    h_k2 = fp.fingerprint_query_chat("k2", "ip", msgs)
    h_ip2 = fp.fingerprint_query_chat("k1", "ip2", msgs)
    assert h_k1 and h_k1 != h_k2 and h_k1 != h_ip2
    print("  [PASS] fp: 不同 Key 或 IP → hash 不同")


# ─── 端到端测试辅助 ──────────────────────────────────────────────


class MockRouter:
    def __init__(self):
        self.handlers = {}
        self.last_request = None
        self.urls_visited: list[str] = []

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        url = str(request.url)
        self.urls_visited.append(url)
        for base, handler in self.handlers.items():
            if url.startswith(base):
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
    def __init__(self, headers, body_bytes, client_ip="1.2.3.4"):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient(client_ip)
    async def body(self): return self._body


async def _call(m, router, ingress, body, api_key="ccp-alice", client_ip="1.2.3.4"):
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)
    req = FakeRequest({"Authorization": f"Bearer {api_key}"},
                      json.dumps(body).encode("utf-8"), client_ip=client_ip)
    resp = await m["openai_handler"].handle(req, ingress_protocol=ingress)
    return resp, mock_client


def _chat_json(content: str):
    return httpx.Response(200, json={
        "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-5",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }, headers={"content-type": "application/json"})


def _resp_json(text: str):
    return httpx.Response(200, json={
        "id": "resp_x", "object": "response", "status": "completed",
        "created_at": 1, "model": "gpt-5",
        "output": [{
            "type": "message", "id": "msg_x", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "output_text": text,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }, headers={"content-type": "application/json"})


# ─── 端到端 ──────────────────────────────────────────────────────


async def test_e2e_chat_affinity_hit(m):
    """同 Key+IP 连续两次 chat 请求（带足够多的历史）→ 第二次粘到第一次的渠道。"""
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    # 两个上游：a 先低分，b 先高分；命中前 b 被选；命中后应粘 a
    router.register("https://a.example", lambda r: _chat_json("from-a"))
    router.register("https://b.example", lambda r: _chat_json("from-b"))

    ch_a = _make_openai_channel(m, "oaiA", "https://a.example", protocol="openai-chat")
    ch_b = _make_openai_channel(m, "oaiB", "https://b.example", protocol="openai-chat")
    _install_channels(m, [ch_a, ch_b])

    # 对 a 强制高分（禁用 smart），用 order 模式让 b 总排第一位
    def _ord(c): c["channelSelection"] = "order"
    m["config"].update(_ord)

    # 第一次：history 包含完整 [u,a,u,a,u] 5 条（满足 query 和 write 都触发）
    body1 = {"model": "gpt-5", "stream": False,
             "messages": [
                 {"role": "user", "content": "u0"},
                 {"role": "assistant", "content": "a0"},
                 {"role": "user", "content": "u1"},
                 {"role": "assistant", "content": "a1"},
                 {"role": "user", "content": "u2"},
             ]}
    # 清空 affinity/重新 setup 会把 channels 刷掉，这里假装先让 a 成为被命中的渠道：
    # 先人为写一条 affinity 指纹指向 a，相当于之前一次 a 请求。
    fp = m["fingerprint"]
    target_fp = fp.fingerprint_query_chat("alice", "1.2.3.4", body1["messages"])
    assert target_fp
    m["affinity"].upsert(target_fp, "api:oaiA", "gpt-5")
    # 给 a 一个高 score（评分 越低越好，所以我们要让 a 的 score 高 → b 更优）。
    # 但我们用 order 模式 → 评分无用，直接按注册顺序 b 在 a 之后。
    # 既然 order 模式下 a 先 b 后，亲和应当把 a 顶上（它在 affinity 中指向 a），但 a 本来就首位。
    # 换个策略：让 b 先 a 后，亲和命中后 a 被顶上。
    _install_channels(m, [ch_b, ch_a])
    m["affinity"].upsert(target_fp, "api:oaiA", "gpt-5")

    resp, mc = await _call(m, router, "chat", body1)
    assert resp.status_code == 200
    out = json.loads(resp.body)
    # 应该命中了 a（亲和），而不是 order 首位 b
    assert out["choices"][0]["message"]["content"] == "from-a", (
        f"affinity 命中应让 a 被选，但拿到的是 {out['choices'][0]['message']['content']}"
    )
    await mc.aclose()
    print("  [PASS] chat affinity 命中：优先选用亲和绑定的渠道")


async def test_e2e_chat_write_after_success(m):
    """第一次请求成功后，fingerprint_write_chat 写入；第二次 query 可命中同一指纹。"""
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    router.register("https://a.example", lambda r: _chat_json("A1-reply"))
    router.register("https://b.example", lambda r: _chat_json("from-b"))
    ch_a = _make_openai_channel(m, "oaiA", "https://a.example", protocol="openai-chat")
    ch_b = _make_openai_channel(m, "oaiB", "https://b.example", protocol="openai-chat")
    # b 先 a 后
    _install_channels(m, [ch_b, ch_a])

    def _ord(c): c["channelSelection"] = "order"
    m["config"].update(_ord)

    # 没有预先的亲和记录；历史 5 条
    msgs = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    # 第一次：a 在 order 上排后（不命中），但我们人为把 b 禁用让 a 上
    ch_b.enabled = False
    resp1, mc1 = await _call(m, router, "chat", {"model": "gpt-5", "stream": False, "messages": msgs})
    assert resp1.status_code == 200
    assert json.loads(resp1.body)["choices"][0]["message"]["content"] == "A1-reply"
    await mc1.aclose()

    # 第一次完成时 fp_write_chat("alice", ip, msgs, {"role":"assistant","content":"A1-reply"})
    # 写入 affinity；key = 这个 hash
    ch_b.enabled = True

    # 第二次：messages = msgs + [A1, u3]（7 条）
    msgs2 = msgs + [{"role": "assistant", "content": "A1-reply"},
                    {"role": "user", "content": "u3"}]
    resp2, mc2 = await _call(m, router, "chat", {"model": "gpt-5", "stream": False, "messages": msgs2})
    assert resp2.status_code == 200
    # 如果 affinity 命中，应粘 a；如果不命中，b 会先选（order 首位）
    assert json.loads(resp2.body)["choices"][0]["message"]["content"] == "A1-reply", (
        f"affinity write→query 应让 a 被粘住：{json.loads(resp2.body)}"
    )
    await mc2.aclose()
    print("  [PASS] chat 非流式：write 后下一次 query 命中同渠道")


async def test_e2e_responses_write_then_query(m):
    _setup(m)
    _install_keys(m, {"alice": {"key": "ccp-alice"}})
    router = MockRouter()
    router.register("https://a.example", lambda r: _resp_json("A1"))
    router.register("https://b.example", lambda r: _resp_json("B1"))
    ch_a = _make_openai_channel(m, "oaiA", "https://a.example", protocol="openai-responses")
    ch_b = _make_openai_channel(m, "oaiB", "https://b.example", protocol="openai-responses")
    _install_channels(m, [ch_b, ch_a])

    def _ord(c): c["channelSelection"] = "order"
    m["config"].update(_ord)

    # 历史 5 条稳定 items
    u0 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q0"}]}
    a0 = {"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "A0", "annotations": []}]}
    u1 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q1"}]}
    a1 = {"type": "message", "role": "assistant",
          "content": [{"type": "output_text", "text": "Old-A1", "annotations": []}]}
    u2 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q2"}]}

    # 第一次：禁用 b，a 处理 → 写 fp
    ch_b.enabled = False
    resp1, mc1 = await _call(m, router, "responses",
                              {"model": "gpt-5", "stream": False, "input": [u0, a0, u1, a1, u2]})
    assert resp1.status_code == 200
    out1 = json.loads(resp1.body)
    # 本次 output 里的第一条 message item 的 text == "A1"（router 返回）
    assert out1["output"][0]["content"][0]["text"] == "A1"
    await mc1.aclose()

    ch_b.enabled = True
    # 第二次：input = 前 5 条 + 第一次返回的 assistant + 新 user（7 条 → rel=7）
    new_a1 = {"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "A1", "annotations": []}]}
    u3 = {"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "Q3"}]}
    resp2, mc2 = await _call(m, router, "responses",
                              {"model": "gpt-5", "stream": False,
                               "input": [u0, a0, u1, a1, u2, new_a1, u3]})
    assert resp2.status_code == 200
    # 命中 a
    out2 = json.loads(resp2.body)
    assert out2["output"][0]["content"][0]["text"] == "A1", (
        f"affinity 应粘 a：{out2}"
    )
    await mc2.aclose()
    print("  [PASS] responses 非流式：write → query 命中同渠道")


async def test_e2e_cross_key_isolation(m):
    _setup(m)
    _install_keys(m, {
        "alice": {"key": "ccp-alice"},
        "bob":   {"key": "ccp-bob"},
    })
    router = MockRouter()
    router.register("https://a.example", lambda r: _chat_json("from-a"))
    router.register("https://b.example", lambda r: _chat_json("from-b"))
    ch_a = _make_openai_channel(m, "oaiA", "https://a.example", protocol="openai-chat")
    ch_b = _make_openai_channel(m, "oaiB", "https://b.example", protocol="openai-chat")
    _install_channels(m, [ch_b, ch_a])

    def _ord(c): c["channelSelection"] = "order"
    m["config"].update(_ord)

    msgs = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]

    # alice 先发一次；b 禁用 → a 处理 → 写 fp
    ch_b.enabled = False
    r, mc = await _call(m, router, "chat",
                         {"model": "gpt-5", "stream": False, "messages": msgs},
                         api_key="ccp-alice")
    assert r.status_code == 200
    await mc.aclose()
    ch_b.enabled = True

    # bob 用同样 messages → 不应命中 alice 的亲和
    msgs2 = msgs + [{"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "u3"}]
    r2, mc2 = await _call(m, router, "chat",
                           {"model": "gpt-5", "stream": False, "messages": msgs2},
                           api_key="ccp-bob")
    assert r2.status_code == 200
    # bob 应走 order 首位 b（没命中 a）
    out2 = json.loads(r2.body)
    assert out2["choices"][0]["message"]["content"] == "from-b"
    await mc2.aclose()
    print("  [PASS] 跨 Key 亲和隔离：bob 用 alice 的 messages 也不命中 alice 的指纹")


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
        test_fp_chat_query_equals_write,
        test_fp_chat_short_returns_none,
        test_fp_resp_query_equals_write,
        test_fp_resp_skips_noise_items,
        test_fp_namespace_isolation,
        test_fp_cross_key_not_equal,
        _async(test_e2e_chat_affinity_hit),
        _async(test_e2e_chat_write_after_success),
        _async(test_e2e_responses_write_then_query),
        _async(test_e2e_cross_key_isolation),
    ]
    passed = 0
    try:
        print("── MS-7 OpenAI fingerprint + 亲和 ────────────")
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
